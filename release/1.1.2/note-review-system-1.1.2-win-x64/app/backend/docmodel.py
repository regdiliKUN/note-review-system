"""文档模型：段落级 + 片段级（segment）结构。

设计要点（保证图片绝不丢失/错位）：
  每个段落(block)由有序的 seg 列表构成，seg 只有两种：
    - {"t": "text", "v": "..."}         纯文字
    - {"t": "img",  ...}                图片（原子单元）
  文字批改只在 text 段内部做替换，img 段的下标与顺序永不改变，
  因此无论改多少遍、重排多少遍，图片都停在与原文完全一致的位置。
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

IMG_MARK = "\u27e6图{}\u27e7"  # ⟦图1⟧

# VML（旧版 Word 图片）命名空间，python-docx 的 nsmap 里没有，手工写全 URI
VML_NS = "{urn:schemas-microsoft-com:vml}"
VML_IMAGEDATA = VML_NS + "imagedata"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
R_LINK = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}link"
WP_EXTENT = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent"
A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"


# ---------------------------------------------------------------- 数据结构
def new_block(kind: str, **kw) -> Dict[str, Any]:
    b = {
        "id": kw.pop("id", None),
        "kind": kind,          # heading / para / list / table / image
        "level": kw.pop("level", 0),
        "segs": kw.pop("segs", []),
        "style": kw.pop("style", {}),
    }
    b.update(kw)
    return b


def block_plain(b: Dict[str, Any]) -> str:
    """把 block 的 seg 拼成供模型阅读的纯文本，图片替换为 ⟦图N⟧ 标记。"""
    out, n = [], 0
    for s in b.get("segs", []):
        if s["t"] == "text":
            out.append(s["v"])
        else:
            n += 1
            out.append(IMG_MARK.format(n))
    return "".join(out)


def block_text_only(b: Dict[str, Any]) -> str:
    return "".join(s["v"] for s in b.get("segs", []) if s["t"] == "text")


def count_images(blocks: List[Dict[str, Any]]) -> int:
    n = 0
    for b in blocks:
        if b["kind"] == "table":
            for row in b.get("rows", []):
                for cell in row:
                    n += sum(1 for s in cell.get("segs", []) if s["t"] == "img")
        else:
            n += sum(1 for s in b.get("segs", []) if s["t"] == "img")
    return n


def all_images(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for b in blocks:
        if b["kind"] == "table":
            for row in b.get("rows", []):
                for cell in row:
                    out += [s for s in cell.get("segs", []) if s["t"] == "img"]
        else:
            out += [s for s in b.get("segs", []) if s["t"] == "img"]
    return out


# ---------------------------------------------------------------- 图片存储
class ImageStore:
    """把文档内的图片落盘到 <workdir>/assets/<docid>/ 下，返回相对路径。"""

    def __init__(self, root: str, doc_id: str):
        self.dir = os.path.join(root, doc_id)
        os.makedirs(self.dir, exist_ok=True)

    def save(self, blob: bytes, ext: str, idx: int) -> Dict[str, Any]:
        ext = (ext or "png").lower().lstrip(".")
        if ext in ("emf", "wmf"):
            ext = "emf"
        name = f"img_{idx:03d}_{hashlib.md5(blob).hexdigest()[:8]}.{ext}"
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(blob)
        return {"file": name, "path": path}


# ---------------------------------------------------------------- docx 解析
def _emu_to_px(v) -> Optional[int]:
    try:
        return max(1, int(int(v) / 9525))
    except Exception:
        return None


def parse_docx(path: str, store: ImageStore) -> Dict[str, Any]:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(path)
    doc_dir = os.path.dirname(os.path.abspath(path))
    blocks: List[Dict[str, Any]] = []
    img_seq = [0]

    def collect_images(el, rels):
        """从任意 XML 元素里按出现顺序抓出图片。"""
        found = []
        for blip in el.iter(A_BLIP):
            rid = blip.get(R_EMBED) or blip.get(R_LINK)
            if rid and rid in rels:
                found.append((rid, None, None))
        for imd in el.iter(VML_IMAGEDATA):
            rid = imd.get(R_ID)
            if rid and rid in rels:
                found.append((rid, None, None))
        return found

    def rels_of(part):
        return part.rels

    def run_segs(p_el, part) -> List[Dict[str, Any]]:
        segs: List[Dict[str, Any]] = []
        rels = part.rels

        def add_text(t: str):
            if not t:
                return
            if segs and segs[-1]["t"] == "text":
                segs[-1]["v"] += t
            else:
                segs.append({"t": "text", "v": t})

        def add_image(rid, cx=None, cy=None):
            # 优先读内嵌图片；失败则尝试外部链接图片（R_LINK）
            blob = None
            ext = None
            try:
                image_part = rels[rid].target_part
                blob = image_part.blob
                ext = str(image_part.partname).rsplit(".", 1)[-1]
            except Exception:
                try:
                    ref = getattr(rels[rid], "target_ref", None)
                except Exception:
                    ref = None
                if ref:
                    p = ref if os.path.isabs(ref) else os.path.join(doc_dir, ref)
                    if os.path.exists(p):
                        try:
                            blob = open(p, "rb").read()
                            ext = os.path.splitext(p)[1].lstrip(".") or "png"
                        except Exception:
                            blob = None
            if not blob:
                return
            img_seq[0] += 1
            info = store.save(blob, ext, img_seq[0])
            segs.append({
                "t": "img",
                "id": f"i{img_seq[0]}",
                "file": info["file"],
                "w": _emu_to_px(cx),
                "h": _emu_to_px(cy),
                "alt": "",
            })

        # 按 XML 文档顺序遍历，保证 文字/图片 相对位置准确
        for r in p_el.iter():
            tag = r.tag
            if tag == qn("w:t"):
                add_text(r.text or "")
            elif tag == qn("w:tab"):
                add_text("\t")
            elif tag in (qn("w:br"), qn("w:cr")):
                add_text("\n")
            elif tag == qn("w:drawing"):
                cx = cy = None
                for ext_el in r.iter(WP_EXTENT):
                    cx, cy = ext_el.get("cx"), ext_el.get("cy")
                    break
                for blip in r.iter(A_BLIP):
                    rid = blip.get(R_EMBED) or blip.get(R_LINK)
                    if rid:
                        add_image(rid, cx, cy)
            elif tag == VML_IMAGEDATA:
                rid = r.get(R_ID)
                if rid:
                    add_image(rid)
        # 去掉首尾空白 seg
        while segs and segs[0]["t"] == "text" and not segs[0]["v"].strip():
            segs.pop(0)
        while segs and segs[-1]["t"] == "text" and not segs[-1]["v"].strip():
            segs.pop()
        if segs and segs[0]["t"] == "text":
            segs[0]["v"] = segs[0]["v"].lstrip()
        return segs

    def handle_paragraph(p_el, container_blocks):
        from docx.text.paragraph import Paragraph
        p = Paragraph(p_el, doc)
        segs = run_segs(p_el, p.part if hasattr(p, "part") else doc.part)
        style = (p.style.name or "").lower()
        text = "".join(s["v"] for s in segs if s["t"] == "text").strip()

        if not segs or (not text and not any(s["t"] == "img" for s in segs)):
            return

        level = 0
        m = re.match(r"heading\s*(\d)", style)
        if m:
            level = int(m.group(1))
        elif style in ("title",):
            level = 1

        kind = "heading" if level else "para"
        if style.startswith("list") or p_el.find(".//" + qn("w:numPr")) is not None:
            kind = "list"
        if not text and any(s["t"] == "img" for s in segs):
            kind = "image"

        align = str(p.alignment) if p.alignment is not None else ""
        container_blocks.append(new_block(
            kind, level=level, segs=segs,
            style={"align": align, "bold": bool(text and p.runs and p.runs[0].bold)},
        ))

    def handle_table(tbl_el, container_blocks):
        from docx.table import Table
        t = Table(tbl_el, doc)
        rows = []
        for row in t.rows:
            cells = []
            for cell in row.cells:
                csegs: List[Dict[str, Any]] = []
                for p in cell.paragraphs:
                    s = run_segs(p._p, doc.part)
                    if s:
                        if csegs:
                            csegs.append({"t": "text", "v": "\n"})
                        csegs += s
                cells.append({"segs": csegs})
            rows.append(cells)
        if rows:
            container_blocks.append(new_block("table", rows=rows))

    body = doc.element.body

    def walk(el):
        """递归处理正文直接子节点，兼容 w:sdt 内容控件（其内仍可能有段落/表格/图片）。"""
        for child in el.iterchildren():
            if child.tag == qn("w:p"):
                handle_paragraph(child, blocks)
            elif child.tag == qn("w:tbl"):
                handle_table(child, blocks)
            elif child.tag == qn("w:sdt"):
                sdtc = child.find(qn("w:sdtContent"))
                if sdtc is not None:
                    walk(sdtc)

    walk(body)

    # 标题兜底：文档没有 heading 样式时，从文本特征推断
    if not any(b["kind"] == "heading" for b in blocks):
        infer_headings(blocks)

    for i, b in enumerate(blocks):
        b["id"] = f"b{i}"

    return {
        "blocks": blocks,
        "title": _guess_title(blocks),
    }


# ---------------------------------------------------------------- 纯文本 / Markdown
def parse_text(path: str, store: ImageStore) -> Dict[str, Any]:
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    ext = os.path.splitext(path)[1].lower()
    blocks: List[Dict[str, Any]] = []
    img_seq = [0]
    base = os.path.dirname(path)

    if ext in (".md", ".markdown"):
        lines = raw.split("\n")
        buf: List[str] = []
        in_code = False
        code_buf: List[str] = []

        def flush():
            if buf:
                txt = "\n".join(buf).strip()
                if txt:
                    blocks.append(new_block("para", segs=[{"t": "text", "v": txt}]))
                buf.clear()

        for line in lines:
            if line.strip().startswith("```"):
                if in_code:
                    blocks.append(new_block("para", segs=[{"t": "text", "v": "\n".join(code_buf)}],
                                            style={"code": True}))
                    code_buf.clear()
                    in_code = False
                else:
                    flush()
                    in_code = True
                continue
            if in_code:
                code_buf.append(line)
                continue
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                flush()
                blocks.append(new_block("heading", level=len(m.group(1)),
                                        segs=[{"t": "text", "v": m.group(2).strip()}]))
                continue
            # markdown 图片 ![alt](path)
            segs = _md_inline_images(line, base, store, img_seq)
            if segs is not None:
                flush()
                if segs:
                    blocks.append(new_block("para", segs=segs))
                continue
            # 列表项单独成块
            mli = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$", line)
            if mli:
                flush()
                blocks.append(new_block("list",
                                        segs=[{"t": "text", "v": "· " + _strip_md(mli.group(1))}]))
                continue
            if not line.strip():
                flush()
            else:
                buf.append(line)
        flush()
        # 清掉 **粗体** / *斜体* / `代码` / [链接](url) 这类标记
        for b in blocks:
            for s in b["segs"]:
                if s["t"] == "text":
                    s["v"] = _strip_md(s["v"])
    else:
        # .txt：空行分段，行首缩进/编号当作列表
        cur: List[str] = []
        for line in raw.split("\n"):
            if not line.strip():
                if cur:
                    txt = "\n".join(cur).strip()
                    blocks.append(new_block("para", segs=[{"t": "text", "v": txt}]))
                    cur = []
            else:
                cur.append(line)
        if cur:
            blocks.append(new_block("para", segs=[{"t": "text", "v": "\n".join(cur).strip()}]))

    infer_headings(blocks)
    for i, b in enumerate(blocks):
        b["id"] = f"b{i}"
    return {"blocks": blocks, "title": _guess_title(blocks)}


# ---------------------------------------------------------------- 旧版 Word（.doc，OLE2 复合文档）
_CFB_ENDOFCHAIN = 0xFFFFFFFE
_CFB_FREESECT = 0xFFFFFFFF


def _u16(b: bytes, off: int) -> int:
    return b[off] | (b[off + 1] << 8)


def _u32(b: bytes, off: int) -> int:
    return b[off] | (b[off + 1] << 8) | (b[off + 2] << 16) | (b[off + 3] << 24)


def _cfb_streams(data: bytes, wanted_names) -> Dict[str, bytes]:
    """解析 OLE2/CFB 复合文档，按名字取出指定流（常规 512B 扇区布局）。"""
    if len(data) < 512 or data[0] != 0xD0 or data[1] != 0xCF:
        raise ValueError("不是 OLE2 复合文档")
    ssz = 1 << _u16(data, 30)
    sssz = 1 << _u16(data, 32)
    dir_first = _u32(data, 48)
    mini_cutoff = _u32(data, 56)
    num_fat = _u32(data, 44)

    def read_sector(sector: int) -> bytes:
        off = (sector + 1) * ssz
        return data[off:off + ssz]

    difat = [_u32(data, 76 + k * 4) for k in range(min(109, num_fat))]
    # 扩展 DIFAT 链（>109 个 FAT 扇区的大文件）
    nxt = _u32(data, 68)
    guard = 0
    while nxt and nxt not in (_CFB_ENDOFCHAIN, _CFB_FREESECT) and len(difat) < num_fat and guard < 100000:
        guard += 1
        ds = read_sector(nxt)
        for k in range(ssz // 4 - 1):
            v = _u32(ds, k * 4)
            if v not in (_CFB_FREESECT, _CFB_ENDOFCHAIN):
                difat.append(v)
        nxt = _u32(ds, ssz - 4)

    fat: List[int] = []
    for s in difat:
        fs = read_sector(s)
        fat.extend(_u32(fs, e * 4) for e in range(ssz // 4))

    def chain(start: int) -> List[int]:
        out, s, g = [], start, 0
        while s not in (_CFB_ENDOFCHAIN, _CFB_FREESECT) and s < len(fat) and g < 1000000:
            g += 1
            out.append(s)
            s = fat[s]
        return out

    def sectors_to_bytes(sectors: List[int], size_limit=None) -> bytes:
        buf = b"".join(read_sector(s) for s in sectors)
        if size_limit is not None:
            buf = buf[:min(size_limit, len(buf))]
        return buf

    # 目录
    dir_data = sectors_to_bytes(chain(dir_first))
    entries = []
    for k in range(0, len(dir_data) - 127, 128):
        etype = dir_data[k + 66]
        if etype == 0 or etype > 5:
            continue
        raw = dir_data[k:k + 64]
        name = raw.decode("utf-16-le", errors="ignore").split("\x00")[0]
        entries.append({"name": name, "type": etype,
                        "start": _u32(dir_data, k + 116),
                        "size": _u32(dir_data, k + 120)})

    roots = [e for e in entries if e["type"] == 5]
    root = roots[0] if roots else None

    # 迷你流容器（root 的小流）与 MiniFAT
    mini_stream = b""
    minifat: List[int] = []
    mf = next((e for e in entries if e["name"] == "MiniFAT" and e["type"] == 2), None)
    if root is not None and root["size"] > 0 and mf is not None:
        mf_data = sectors_to_bytes(chain(mf["start"]), mf["size"])
        minifat = [_u32(mf_data, k) for k in range(0, len(mf_data) - 3, 4)]
        mini_stream = sectors_to_bytes(chain(root["start"]))

    def read_minichain(start: int, size: int) -> bytes:
        parts, s, g = [], start, 0
        while s not in (_CFB_ENDOFCHAIN, _CFB_FREESECT) and s < len(minifat) and g < 1000000:
            g += 1
            parts.append(mini_stream[s * sssz:(s + 1) * sssz])
            s = minifat[s]
        out = b"".join(parts)
        return out[:min(size, len(out))] if size is not None else out

    streams: Dict[str, bytes] = {}
    for e in entries:
        if e["type"] != 2 or e["name"] not in wanted_names:
            continue
        if e["size"] == 0:
            streams[e["name"]] = b""
        elif e["size"] < mini_cutoff:
            streams[e["name"]] = read_minichain(e["start"], e["size"])
        else:
            streams[e["name"]] = sectors_to_bytes(chain(e["start"]), e["size"])
    return streams


# ---------------------------------------------------------------- .doc 文本 + 图片锚点
def _doc_decode_seg(seg: bytes) -> str:
    """判断单段字节的真实编码。

    多数 .doc 由 Word 生成、FIB 的 fFarEast 位会置位；但 WPS 生成的 .doc 往往
    正文是 UTF-16LE 却没置位，旧代码按 cp1252 解码导致整篇中文乱码。这里做
    采样判定，优先尝试 UTF-16LE，再退回 gbk / cp1252。
    """
    try:
        t = seg.decode("utf-16-le")
        head = t[:2000]
        if head and sum(1 for ch in head if ch.isprintable() or ch in "\r\n\t") / len(head) > 0.75:
            return t
    except Exception:
        pass
    for enc in ("gbk", "cp1252"):
        try:
            t2 = seg.decode(enc)
            head = t2[:2000]
            if head and sum(1 for ch in head if ch.isprintable() or ch in "\r\n\t") / len(head) > 0.85:
                return t2
        except Exception:
            continue
    # 最后兜底
    try:
        return seg.decode("utf-16-le", errors="replace")
    except Exception:
        return seg.decode("gbk", errors="replace")


def _doc_emit_tokens(tokens: List, text: str) -> None:
    """把一段纯文本拆成 ('t', str) / ('img', 'pic'|'obj') token。

    遇到图片占位符就插入 img 锚点，保持与正文完全一致的相对顺序：
      0x0001 = 内联图片（二进制数据存放在 Data 流）
      0x0008 = 嵌入对象（二进制数据存放在 WordDocument 流）
    """
    buf = ""
    for ch in text:
        o = ord(ch)
        if o == 0x0001:
            if buf:
                tokens.append(("t", buf)); buf = ""
            tokens.append(("img", "pic"))
        elif o == 0x0008:
            if buf:
                tokens.append(("t", buf)); buf = ""
            tokens.append(("img", "obj"))
        elif o in (0x0007, 0x000B, 0x000C):
            # 分页/分栏/单元格等特殊分隔：折叠成换行
            if buf:
                tokens.append(("t", buf)); buf = ""
            tokens.append(("t", "\n"))
        elif o in (0x000E, 0x000F, 0x0010, 0x0011, 0x0012,
                   0x0013, 0x0014, 0x0015):
            # 其余控制字符：直接丢弃
            continue
        else:
            buf += ch
    if buf:
        tokens.append(("t", buf))


def _doc_clx_pieces(word_doc: bytes, clx: bytes) -> List[str]:
    """标准 CLX 分片表：FIB → PlcPcd，返回按文档顺序排列的文本片段。"""
    pieces: List[str] = []
    p = 0
    while p < len(clx):
        t = clx[p]
        if t == 1:  # Prc：跳过
            if p + 3 > len(clx):
                break
            p += 3 + _u16(clx, p + 1)
        elif t == 2:  # Pcdt → PlcPcd
            if p + 5 > len(clx):
                break
            lcb = _u32(clx, p + 1)
            plc = clx[p + 5:p + 5 + lcb]
            n = (len(plc) - 4) // 12
            if 0 < n < 1000000:
                for pi in range(n):
                    cp_start = _u32(plc, pi * 4)
                    cp_end = _u32(plc, pi * 4 + 4)
                    pcd = (n + 1) * 4 + pi * 8
                    if pcd + 8 <= len(plc) and cp_end - cp_start > 0:
                        fc = _u32(plc, pcd + 2)
                        if fc & 0x40000000:  # 压缩片：ANSI 单字节
                            off = (fc & 0x3FFFFFFF) // 2
                            seg = word_doc[off:off + cp_end - cp_start]
                            pieces.append(seg.decode("cp1252", errors="replace"))
                        else:  # 未压缩：UTF-16LE
                            off = fc & 0x3FFFFFFF
                            seg = word_doc[off:off + (cp_end - cp_start) * 2]
                            pieces.append(seg.decode("utf-16-le", errors="replace"))
            break
        else:
            break
    return pieces


def _doc_text_tokens(word_doc: bytes, table) -> List:
    """返回 ('t', str) / ('img', 'pic'|'obj') 交织的 token 序列（文档顺序）。

    优先走标准 CLX 分片表；若文档（如 WPS 生成）没有合法 CLX，则退回单段
    fcMin..fcMac 兜底，并用采样判定编码。两类路径都会把 0x0001/0x0008 图片
    占位符转成 img 锚点，保证图片位置与正文一致。
    """
    tokens: List = []
    if len(word_doc) >= 0x1AA:
        fc_clx = _u32(word_doc, 0x01A2)
        lcb_clx = _u32(word_doc, 0x01A6)
        if lcb_clx > 0 and fc_clx + lcb_clx <= len(word_doc):
            clx = word_doc[fc_clx:fc_clx + lcb_clx]
            if clx and clx[0] in (1, 2):
                for piece in _doc_clx_pieces(word_doc, clx):
                    _doc_emit_tokens(tokens, piece)
    # 单段兜底：仅当上面的标准路径没有产出任何内容时
    if not any(tok[0] in ("t", "img") for tok in tokens):
        fc_min, fc_mac = _u32(word_doc, 0x18), _u32(word_doc, 0x1C)
        if fc_min < fc_mac <= len(word_doc):
            seg = word_doc[fc_min:fc_mac]
            text = _doc_decode_seg(seg)
            _doc_emit_tokens(tokens, text)
    return tokens


def _img_info(blob: bytes):
    """无第三方依赖地识别 PNG/JPEG 并取宽高；不合法返回 None。

    用于 .doc 抠图时的合法性校验与尺寸读取，避免依赖 Pillow（打包环境未必带）。
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        if len(blob) < 24 or blob[12:16] != b"IHDR":
            return None
        w = int.from_bytes(blob[16:20], "big")
        h = int.from_bytes(blob[20:24], "big")
        if not (0 < w < 100000 and 0 < h < 100000):
            return None
        return ("png", w, h)
    if blob[:3] == b"\xff\xd8\xff":
        i, n = 2, len(blob)
        while i + 4 < n:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            if marker == 0xFF:
                i += 1
                continue
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
                i += 2
                continue
            seg_len = int.from_bytes(blob[i + 2:i + 4], "big")
            if seg_len < 2:
                return None
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if i + 9 > n:
                    return None
                h = int.from_bytes(blob[i + 5:i + 7], "big")
                w = int.from_bytes(blob[i + 7:i + 9], "big")
                if not (0 < w < 100000 and 0 < h < 100000):
                    return None
                return ("jpg", w, h)
            i += 2 + seg_len
        return None
    return None


def _doc_brute_images(buf: bytes) -> List[Dict[str, Any]]:
    """在字节流里按魔数 + 结束符抠出合法位图（PNG / JPEG）。

    .doc 的图片不以标准 Escher Blip 封装，而是以内联字节形式散落在 Data /
    WordDocument 流中。这里做无损抠取（不重编码、不改格式），再用纯 Python
    头部解析校验，返回按出现顺序排列的列表。
    """
    out: List[Dict[str, Any]] = []
    # PNG：起点 89 50 4E 47，终点 IEND(含 4 字节 CRC)
    i = 0
    while True:
        s = buf.find(b"\x89PNG", i)
        if s < 0:
            break
        e = buf.find(b"IEND", s)
        if e < 0:
            break
        blob = buf[s:e + 8]
        if _img_info(blob):
            out.append({"ext": "png", "blob": blob})
        i = s + 4
    # JPEG：起点 FF D8 FF，终点 FF D9
    i = 0
    while True:
        s = buf.find(b"\xff\xd8\xff", i)
        if s < 0:
            break
        e = buf.find(b"\xff\xd9", s + 3)
        if e < 0:
            break
        blob = buf[s:e + 2]
        if _img_info(blob):
            out.append({"ext": "jpg", "blob": blob})
        i = s + 3
    return out


def parse_doc(path: str, store) -> Dict[str, Any]:
    """旧版 .doc（Word 97-2003 二进制）：CFB 容器解析。

    关键修复：
      1) 文本与图片一起提取，图片按原文 0x0001/0x0008 锚点原位插入，不再丢失；
      2) 兼容 WPS 生成、无标准 CLX 的文档（单段 UTF-16LE 兜底 + 编码采样判定），
         解决旧版中文乱码问题；
      3) 从 Data / WordDocument 流无损抠出 PNG/JPEG，按文档顺序填充，绝不重编码。
    """
    with open(path, "rb") as f:
        data = f.read()
    streams = _cfb_streams(data, ("WordDocument", "1Table", "0Table", "Data"))
    word_doc = streams.get("WordDocument")
    if not word_doc:
        raise ValueError("文件中找不到 WordDocument 流，可能不是有效的 .doc 文档")
    flags = _u16(word_doc, 0x0A)
    table = streams.get("1Table") if (flags & 0x0200) else streams.get("0Table")
    if table is None:
        table = streams.get("1Table") or streams.get("0Table")

    tokens = _doc_text_tokens(word_doc, table)

    # 图片二进制：内联图片(0x0001)通常在 Data 流，嵌入对象(0x0008)在 WordDocument 流
    data_stream = streams.get("Data") or b""
    pic_pool = _doc_brute_images(data_stream)   # 0x0001 候选
    obj_pool = _doc_brute_images(word_doc)       # 0x0008 候选

    def dedup(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen, uniq = set(), []
        for it in pool:
            h = hashlib.md5(it["blob"]).hexdigest()
            if h not in seen:
                seen.add(h); uniq.append(it)
        return uniq
    pic_pool, obj_pool = dedup(pic_pool), dedup(obj_pool)

    pic_i, obj_i, img_seq = [0], [0], [0]

    def make_img(it: Dict[str, Any]) -> Dict[str, Any]:
        img_seq[0] += 1
        info = store.save(it["blob"], it["ext"], img_seq[0])
        meta = _img_info(it["blob"])
        w = meta[1] if meta else None
        h = meta[2] if meta else None
        return {"t": "img", "id": f"i{img_seq[0]}", "file": info["file"],
                "w": w, "h": h, "alt": ""}

    def next_img(kind: str):
        pool, idx = (pic_pool, pic_i) if kind == "pic" else (obj_pool, obj_i)
        if idx[0] < len(pool):
            it = pool[idx[0]]; idx[0] += 1
            return make_img(it)
        return None

    # 构建 blocks：按换行切段，img 锚点原位插入（文字/图片相对位置与原文一致）
    blocks: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []

    def flush():
        nonlocal cur
        if not cur:
            return
        cleaned = [s for s in cur if not (s["t"] == "text" and not s["v"].strip())]
        if not cleaned:
            cur = []
            return
        kind = "image" if all(s["t"] == "img" for s in cleaned) else "para"
        blocks.append(new_block(kind, segs=cleaned))
        cur = []

    for tok in tokens:
        if tok[0] == "img":
            seg = next_img(tok[1])
            if seg:
                cur.append(seg)
            continue
        text = tok[1].replace("\r\n", "\n").replace("\r", "\n")
        for j, part in enumerate(text.split("\n")):
            if j > 0:
                flush()
            if part:
                cur.append({"t": "text", "v": part})
    flush()

    # 末尾若仍有未能定位的多余图片，统一追加在文末（绝不丢弃任何图像）
    for it in pic_pool[pic_i[0]:] + obj_pool[obj_i[0]:]:
        cur.append(make_img(it))
    if cur:
        flush()

    if not any(b["kind"] == "heading" for b in blocks):
        infer_headings(blocks)
    for i, b in enumerate(blocks):
        b["id"] = f"b{i}"
    return {"blocks": blocks, "title": _guess_title(blocks)}


def _strip_md(t: str) -> str:
    """去掉行内 Markdown 标记，留下干净可批改的正文。"""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"~~(.+?)~~", r"\1", t)
    return t


def _md_inline_images(line: str, base: str, store: ImageStore, seq: List[int]):
    """如果整行主要是图片引用，转成 img seg；否则返回 None 交给普通文本处理。"""
    pat = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
    found = list(pat.finditer(line))
    if not found:
        return None
    segs: List[Dict[str, Any]] = []
    last = 0
    for m in found:
        if m.start() > last:
            t = line[last:m.start()].strip()
            if t:
                segs.append({"t": "text", "v": t})
        src = m.group(2)
        p = src if os.path.isabs(src) else os.path.join(base, src)
        if os.path.exists(p):
            seq[0] += 1
            blob = open(p, "rb").read()
            ext = os.path.splitext(p)[1].lstrip(".") or "png"
            info = store.save(blob, ext, seq[0])
            segs.append({"t": "img", "id": f"i{seq[0]}", "file": info["file"],
                         "w": None, "h": None, "alt": m.group(1)})
        last = m.end()
    tail = line[last:].strip()
    if tail:
        segs.append({"t": "text", "v": tail})
    return segs


# ---------------------------------------------------------------- 标题推断
HEAD_PATTERNS = [
    (re.compile(r"^第[一二三四五六七八九十百]+[章节部分篇]\s*"), 1),
    (re.compile(r"^[一二三四五六七八九十]+[、.．]\s*"), 1),
    (re.compile(r"^（[一二三四五六七八九十]+）"), 2),
    (re.compile(r"^\([一二三四五六七八九十]+\)"), 2),
    (re.compile(r"^\d+(\.\d+){1,2}\s+"), 3),
    (re.compile(r"^\d+[、.．]\s*\S"), 2),
    (re.compile(r"^[【\[](.{2,20})[】\]]$"), 2),
]


def infer_headings(blocks: List[Dict[str, Any]]):
    """对没有样式信息的文档，按中文笔记常见编号规律推断层级。"""
    for b in blocks:
        if b["kind"] in ("heading", "table", "image"):
            continue
        t = block_text_only(b).strip()
        if not t:
            continue
        first_line = t.split("\n")[0]
        is_short = len(first_line) <= 40 and not first_line.endswith(("。", "，", "；", "！", "？"))
        for pat, lvl in HEAD_PATTERNS:
            if pat.match(first_line) and is_short:
                b["kind"] = "heading"
                b["level"] = lvl
                break


def _guess_title(blocks: List[Dict[str, Any]]) -> str:
    for b in blocks[:6]:
        t = block_text_only(b).strip()
        if t and len(t) <= 40:
            return t.split("\n")[0]
    return "未命名笔记"


# ---------------------------------------------------------------- 导出 docx
def export_docx(blocks: List[Dict[str, Any]], asset_dir: str, out_path: str,
                title: str = "", toc: bool = False):
    """把 block 模型写成 docx：seg 顺序原样落盘，图片按原位插入。"""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Emu, Inches, Pt, RGBColor

    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "微软雅黑"
    st.font.size = Pt(10.5)
    try:
        from docx.oxml.ns import qn as _qn
        st._element.rPr.rFonts.set(_qn("w:eastAsia"), "微软雅黑")
    except Exception:
        pass
    # 正文行距与段距，导出后更接近正常文档的观感
    try:
        st.paragraph_format.line_spacing = 1.5
        st.paragraph_format.space_after = Pt(6)
    except Exception:
        pass

    # 标题样式统一成深色中文黑体，避免默认的蓝色 Calibri Light
    for i in range(1, 5):
        try:
            hs = doc.styles[f"Heading {i}"]
            hs.font.name = "微软雅黑"
            hs.font.color.rgb = RGBColor(0x1B, 0x24, 0x30)
            hs.font.size = Pt({1: 17, 2: 14.5, 3: 12.5, 4: 11.5}.get(i, 11))
            hs.font.bold = True
            hs._element.rPr.rFonts.set(
                __import__("docx").oxml.ns.qn("w:eastAsia"), "微软雅黑")
        except Exception:
            pass

    if title:
        h = doc.add_heading(title, level=0)
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER

    def write_segs(par, segs):
        for s in segs:
            if s["t"] == "text":
                for i, piece in enumerate(s["v"].split("\n")):
                    if i:
                        par.add_run().add_break()
                    if piece:
                        par.add_run(piece)
            else:
                p = os.path.join(asset_dir, s["file"])
                if not os.path.exists(p):
                    continue
                kw = {}
                if s.get("w"):
                    px = int(s["w"])
                    # 超过版心宽度时按 6 英寸缩放（.doc 抽出的图是原始像素宽，可能很大）
                    kw["width"] = Inches(6.0) if px > 560 else Emu(px * 9525)
                try:
                    run = par.add_run()
                    run.add_picture(p, **kw)
                except Exception:
                    pass

    for b in blocks:
        if b["kind"] == "table":
            rows = b.get("rows", [])
            if not rows:
                continue
            ncol = max(len(r) for r in rows)
            tbl = doc.add_table(rows=len(rows), cols=ncol)
            tbl.style = "Table Grid"
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    if ci >= ncol:
                        break
                    c = tbl.cell(ri, ci)
                    c.paragraphs[0].text = ""
                    write_segs(c.paragraphs[0], cell.get("segs", []))
            continue

        lvl = b.get("level", 0)
        if b["kind"] == "heading" and lvl:
            par = doc.add_heading("", level=min(lvl, 9))
        else:
            par = doc.add_paragraph()
            if b.get("style", {}).get("code"):
                par.paragraph_format.left_indent = Inches(0.3)
        write_segs(par, b.get("segs", []))
        if b["kind"] == "list":
            par.paragraph_format.left_indent = Inches(0.3)

    doc.save(out_path)
    return out_path


def export_html(blocks: List[Dict[str, Any]], asset_dir: str, out_path: str,
                title: str = "") -> str:
    from html import escape
    parts = []
    for b in blocks:
        if b["kind"] == "table":
            rows = b.get("rows", [])
            parts.append("<table>")
            for row in rows:
                parts.append("<tr>")
                for cell in row:
                    parts.append("<td>" + _html_segs(cell.get("segs", []), asset_dir) + "</td>")
                parts.append("</tr>")
            parts.append("</table>")
            continue
        lvl = b.get("level", 0)
        tag = f"h{min(max(lvl,1),6)}" if b["kind"] == "heading" and lvl else "p"
        parts.append(f"<{tag}>" + _html_segs(b.get("segs", []), asset_dir) + f"</{tag}>")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{escape(title or '笔记')}</title>
<style>
body{{max-width:820px;margin:40px auto;padding:0 24px;font-family:"Microsoft YaHei",system-ui,sans-serif;
line-height:1.85;color:#1a1a1a;background:#fff;}}
h1,h2,h3,h4{{line-height:1.4;margin:1.6em 0 .7em;}}
h1{{font-size:1.9em;border-bottom:2px solid #e5e7eb;padding-bottom:.3em;}}
h2{{font-size:1.45em;}} h3{{font-size:1.2em;}}
p{{margin:.8em 0;text-align:justify;}}
img{{max-width:100%;height:auto;display:block;margin:1em auto;border-radius:6px;}}
table{{border-collapse:collapse;width:100%;margin:1em 0;}}
td,th{{border:1px solid #d1d5db;padding:8px 12px;}}
</style></head><body>
<h1>{escape(title or '笔记')}</h1>
{''.join(parts)}
</body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def _html_segs(segs, asset_dir):
    from html import escape
    out = []
    for s in segs:
        if s["t"] == "text":
            out.append(escape(s["v"]).replace("\n", "<br>"))
        else:
            p = os.path.join(asset_dir, s["file"])
            try:
                b64 = base64.b64encode(open(p, "rb").read()).decode()
                ext = s["file"].rsplit(".", 1)[-1]
                mime = {"jpg": "jpeg", "svg": "svg+xml"}.get(ext, ext)
                out.append(f'<img src="data:image/{mime};base64,{b64}" alt="">')
            except Exception:
                pass
    return "".join(out)


# ---------------------------------------------------------------- 图像审查报告 → blocks
def image_report_blocks(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把图像审查结果转成可导出的 block 列表（标题 + 条目），让总结覆盖图像部分。"""
    items = (report or {}).get("items") or []
    if not items:
        return []
    blocks: List[Dict[str, Any]] = [
        new_block("heading", level=1, segs=[{"t": "text", "v": "图像说明与核对"}]),
        new_block("para", segs=[{"t": "text", "v":
            (report.get("note") or f"共 {len(items)} 张图片。")}]),
    ]
    for it in items:
        flags = []
        if it.get("sensitive"):
            flags.append("含敏感信息")
        if it.get("inconsistent"):
            flags.append("与正文不一致")
        if it.get("needs_check"):
            flags.append("需人工核对")
        line = f"图{it.get('index')}（{it.get('position', '')}）：{it.get('caption', '')}"
        if it.get("kind"):
            line += f"［{it.get('kind')}］"
        if flags:
            line += "　【" + "、".join(flags) + "】"
        note = "；".join(str(it.get(k) or "").strip() for k in
                         ("sensitive_note", "inconsistent_note", "check_note")
                         if str(it.get(k) or "").strip())
        if note:
            line += " " + note
        blocks.append(new_block("list", segs=[{"t": "text", "v": line}]))
    return blocks
