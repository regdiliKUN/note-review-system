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


def _xml_safe(t: str) -> str:
    """去掉会让 Word/XML 写入失败的控制字符（模型输出偶尔会带）。"""
    if not t:
        return ""
    return "".join(ch for ch in str(t)
                   if ch in "\n\t\r" or (ord(ch) >= 32 and ord(ch) != 0x7f))

# VML（旧版 Word 图片）命名空间，python-docx 的 nsmap 里没有，手工写全 URI
VML_NS = "{urn:schemas-microsoft-com:vml}"
VML_IMAGEDATA = VML_NS + "imagedata"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
R_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
R_LINK = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}link"
WP_EXTENT = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}extent"
A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
DOCPR = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}docPr"


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


def prune_assets(root: str, keep: int = 40) -> int:
    """清理历史导入残留的图片目录。

    每次导入都会按 (路径+修改时间) 生成一个新的 doc_id 目录，同一份笔记改十次
    就会留下十个图片副本，长期下来占满磁盘。这里只删除**程序自己生成的**
    10 位十六进制目录，保留最近 keep 个；任何不符合该命名的目录一律不动。
    """
    removed = 0
    try:
        items = []
        for fn in os.listdir(root):
            d = os.path.join(root, fn)
            if not os.path.isdir(d) or not re.fullmatch(r"[0-9a-f]{10}", fn):
                continue
            try:
                items.append((os.path.getmtime(d), d))
            except Exception:
                continue
        items.sort(reverse=True)
        for _, d in items[max(1, keep):]:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    except Exception:
        pass
    return removed


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
    """把文档内的图片落盘到 <workdir>/assets/<docid>/ 下，返回相对路径。

    内容级去重：同一张图在文档里出现多次（页眉 logo、重复截图）只落一份文件，
    文件名里的 md5 前缀就是去重键。这样几百张图的笔记不会把磁盘写爆，
    导出的 docx/html 也不会把同一张图重复嵌入几十遍。
    """

    def __init__(self, root: str, doc_id: str):
        self.dir = os.path.join(root, doc_id)
        os.makedirs(self.dir, exist_ok=True)
        # 懒加载：把目录里已有的 img_<序号>_<hash8>.<ext> 建成 hash8 -> 文件名 索引，
        # 这样「上次导入留下的图」也能被复用，不必重写一遍。
        self._by_hash: Dict[str, str] | None = None

    def _index(self) -> Dict[str, str]:
        if self._by_hash is not None:
            return self._by_hash
        idx: Dict[str, str] = {}
        try:
            for fn in os.listdir(self.dir):
                if not fn.startswith("img_"):
                    continue
                stem, _, ext = fn.rpartition(".")
                parts = stem.split("_")
                if len(parts) >= 3 and len(parts[-1]) == 8:
                    idx.setdefault(parts[-1], fn)
        except Exception:
            pass
        self._by_hash = idx
        return idx

    def save(self, blob: bytes, ext: str, idx: int) -> Dict[str, Any]:
        ext = (ext or "png").lower().lstrip(".")
        if ext in ("emf", "wmf"):
            ext = "emf"
        if ext == "jpeg":
            ext = "jpg"
        digest = hashlib.md5(blob).hexdigest()
        key = digest[:8]
        index = self._index()
        hit = index.get(key)
        if hit:
            path = os.path.join(self.dir, hit)
            if os.path.exists(path):
                return {"file": hit, "path": path}
        name = f"img_{idx:03d}_{key}.{ext}"
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(blob)
        index[key] = name
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

        def add_image(rid, cx=None, cy=None, alt=""):
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
                "alt": (alt or "")[:200],
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
                alt = ""
                for dp in r.iter(DOCPR):
                    alt = dp.get("descr") or dp.get("title") or ""
                    break
                for blip in r.iter(A_BLIP):
                    rid = blip.get(R_EMBED) or blip.get(R_LINK)
                    if rid:
                        add_image(rid, cx, cy, alt)
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
        style_raw = p.style.name or ""
        style = style_raw.lower()
        text = "".join(s["v"] for s in segs if s["t"] == "text").strip()

        if not segs or (not text and not any(s["t"] == "img" for s in segs)):
            return

        level = 0
        m = re.match(r"heading\s*(\d)", style)
        if m:
            level = int(m.group(1))
        elif style in ("title", "标题"):
            level = 1

        # 列表层级：Word 的 numPr/ilvl 决定缩进深度，丢了它重排后会全部顶格
        num = p_el.find(".//" + qn("w:numPr"))
        list_level = 0
        if num is not None:
            ilvl = num.find(qn("w:ilvl"))
            if ilvl is not None:
                try:
                    list_level = max(0, min(int(ilvl.get(qn("w:val")) or 0), 8))
                except Exception:
                    list_level = 0
        else:
            # 很多文档用「列表项目 2」这类样式编号，段落里并没有直接的 numPr
            m2 = re.search(r"(?:list|列表|項目|项目)\D*(\d)\s*$", style_raw, re.I)
            if m2:
                list_level = max(0, min(int(m2.group(1)) - 1, 8))

        flags: Dict[str, Any] = {}
        if "quote" in style or "引用" in style_raw:
            flags["quote"] = True
        if "preformatted" in style or "code" in style or "代码" in style_raw:
            flags["code"] = True
        if num is not None or style.startswith("list") or "列表" in style_raw:
            flags["list"] = True

        kind = "heading" if level else "para"
        if flags.get("list") and not level:
            kind = "list"
        if not text and any(s["t"] == "img" for s in segs):
            kind = "image"

        align = str(p.alignment) if p.alignment is not None else ""
        container_blocks.append(new_block(
            kind, level=level, segs=segs,
            style={"align": align,
                   "bold": bool(text and p.runs and p.runs[0].bold),
                   "list_level": list_level, **flags},
        ))

    def handle_table(tbl_el, container_blocks):
        from docx.table import Table
        t = Table(tbl_el, doc)
        rows = []
        for row in t.rows:
            cells = []
            seen_tc = set()
            for cell in row.cells:
                # 合并单元格在 python-docx 里会返回同一个 tc 多次，
                # 旧版直接遍历会把表头文字重复写好几遍。
                key = id(cell._tc)
                span = 1
                vmerge = ""
                try:
                    tcPr = cell._tc.tcPr
                    if tcPr is not None:
                        gs = tcPr.find(qn("w:gridSpan"))
                        if gs is not None:
                            span = max(1, int(gs.get(qn("w:val")) or 1))
                        vm = tcPr.find(qn("w:vMerge"))
                        if vm is not None:
                            vmerge = "restart" if (vm.get(qn("w:val")) or "").lower() == "restart" \
                                else "continue"
                except Exception:
                    pass
                if key in seen_tc:
                    cells.append({"segs": [], "span": span, "vmerge": "continue"})
                    continue
                seen_tc.add(key)
                csegs: List[Dict[str, Any]] = []
                for p in cell.paragraphs:
                    s = run_segs(p._p, doc.part)
                    if s:
                        if csegs:
                            csegs.append({"t": "text", "v": "\n"})
                        csegs += s
                cells.append({"segs": csegs, "span": span, "vmerge": vmerge})
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

    # 脚注 / 尾注：学术类笔记的引用来源常写在这里，旧版直接丢掉
    notes = _docx_notes(doc)
    if notes:
        blocks.append(new_block("heading", level=1,
                                segs=[{"t": "text", "v": "脚注与尾注"}]))
        for label, text in notes:
            blocks.append(new_block("list", segs=[{"t": "text", "v": f"{label} {text}"}]))

    # 标题兜底：文档没有 heading 样式时，从文本特征推断
    if not any(b["kind"] == "heading" for b in blocks):
        infer_headings(blocks)

    for i, b in enumerate(blocks):
        b["id"] = f"b{i}"

    # 文档属性里的标题最权威，比「第一段前 40 字」可靠得多
    title = ""
    try:
        title = (doc.core_properties.title or "").strip()
    except Exception:
        pass

    return {
        "blocks": blocks,
        "title": title or _guess_title(blocks),
    }


def _docx_notes(doc) -> List[tuple]:
    """抽取 word/footnotes.xml 与 endnotes.xml 里的注释正文。"""
    out: List[tuple] = []
    try:
        from docx.oxml import parse_xml
        from docx.oxml.ns import qn

        for part in doc.part.package.parts:
            name = str(getattr(part, "partname", ""))
            if name.endswith("footnotes.xml"):
                kind, tag = "脚注", qn("w:footnote")
            elif name.endswith("endnotes.xml"):
                kind, tag = "尾注", qn("w:endnote")
            else:
                continue
            try:
                root = parse_xml(part.blob)
            except Exception:
                continue
            for el in root.iter(tag):
                fid = el.get(qn("w:id"))
                if fid in (None, "-1", "0"):
                    continue
                text = "".join(t.text or "" for t in el.iter(qn("w:t"))).strip()
                if text:
                    out.append((f"[{kind} {fid}]", text))
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 纯文本 / Markdown
def _read_text_auto(path: str) -> str:
    """按真实编码读文本文件。

    旧版一律按 UTF-8 + errors=replace 读，中文 Windows 上大量 .txt 是 GBK，
    结果整篇变成「锟斤拷」却不会报错，用户以为 AI 审得不准。
    这里按 BOM → UTF-8 → GB18030 的顺序试探，最后才退回有损解码。
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", "replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_text(path: str, store: ImageStore) -> Dict[str, Any]:
    raw = _read_text_auto(path)
    ext = os.path.splitext(path)[1].lower()
    # 二进制文件被当文本读会变成一堆控制字符，后面导出 Word 会直接报错
    ctrl = sum(1 for ch in raw[:4000] if ord(ch) < 9 or 13 < ord(ch) < 32)
    if raw and ctrl > max(8, len(raw[:4000]) * 0.02):
        raise ValueError(f"{ext or '该'} 文件不是纯文本，无法按文本解析")
    blocks: List[Dict[str, Any]] = []
    img_seq = [0]
    base = os.path.dirname(path)

    if ext in (".md", ".markdown"):
        lines = raw.split("\n")
        buf: List[str] = []
        in_code = False
        code_buf: List[str] = []

        # 先把 Markdown 表格整块抽出来，换成占位符。
        # 旧版把 `| a | b |` 当普通段落，导出 Word 后是一堆竖线，
        # 而笔记里用表格对齐知识点其实非常常见。
        tables: List[List[List[str]]] = []
        pre: List[str] = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            if ln.strip().startswith("|") and i + 1 < len(lines) and _is_md_sep(lines[i + 1]):
                rows: List[List[str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    if not _is_md_sep(lines[i]):
                        rows.append(_md_cells(lines[i]))
                    i += 1
                if rows:
                    tables.append(rows)
                    pre.append(f"\x00TBL{len(tables) - 1}\x00")
                continue
            pre.append(ln)
            i += 1
        lines = pre

        def flush():
            if buf:
                txt = "\n".join(buf).strip()
                if txt:
                    blocks.append(new_block("para", segs=[{"t": "text", "v": txt}]))
                buf.clear()

        for line in lines:
            mt = re.match(r"^\x00TBL(\d+)\x00$", line)
            if mt:
                flush()
                rows = tables[int(mt.group(1))]
                blocks.append(new_block("table", rows=[
                    [{"segs": [{"t": "text", "v": _strip_md(c).strip()}]} for c in row]
                    for row in rows]))
                continue
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


def _is_md_sep(line: str) -> bool:
    """Markdown 表格的分隔行：|---|:--:|---|"""
    s = line.strip()
    if "|" not in s or "-" not in s:
        return False
    return bool(re.fullmatch(r"\|?[\s:|-]+\|[\s:|-]*", s))


def _md_cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


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
    """猜标题。

    旧版只取「前 6 块里第一个不超过 40 字的文本」，于是大量笔记的标题变成
    「目录」「Page 1」「2024-05-01」这类噪声。现在按可信度分级：
    一级标题 > 开头的短文本（且不是页码/日期/目录等占位）。
    """
    for b in blocks:
        if b["kind"] == "heading" and b.get("level") == 1:
            t = block_text_only(b).strip()
            if t and len(t) <= 60:
                return t.split("\n")[0]
    noise = re.compile(r"^(目录|目\s*录|contents?|page\s*\d+|第?\s*\d+\s*页|"
                       r"\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}日?|\d+)$", re.I)
    for b in blocks[:8]:
        t = block_text_only(b).strip()
        if not t:
            continue
        first = t.split("\n")[0].strip()
        if not first or len(first) > 40 or noise.match(first):
            continue
        return first
    for b in blocks[:8]:
        t = block_text_only(b).strip()
        if t:
            return t.split("\n")[0][:40]
    return "未命名笔记"


# ---------------------------------------------------------------- 导出 docx
def _cell_at(row: List[Dict[str, Any]], target: int):
    """按 gridSpan 展开后的逻辑列号取单元格。"""
    ci = 0
    for cell in row:
        span = max(1, int(cell.get("span") or 1))
        if ci == target:
            return cell
        if ci > target:
            return None
        ci += span
    return None


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
                for i, piece in enumerate(_xml_safe(s["v"]).split("\n")):
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
            ncol = max(sum(max(1, int(c.get("span") or 1)) for c in r) for r in rows)
            tbl = doc.add_table(rows=len(rows), cols=ncol)
            tbl.style = "Table Grid"
            # 先按 gridSpan 落位写内容（合并单元格的续格跳过，避免重复文字）
            for ri, row in enumerate(rows):
                ci = 0
                for cell in row:
                    span = max(1, int(cell.get("span") or 1))
                    if ci >= ncol:
                        break
                    if cell.get("vmerge") != "continue":
                        c = tbl.cell(ri, ci)
                        c.paragraphs[0].text = ""
                        write_segs(c.paragraphs[0], cell.get("segs", []))
                    ci += span
            # 再按原始合并关系还原横向/纵向合并
            for ri, row in enumerate(rows):
                ci = 0
                for cell in row:
                    span = max(1, int(cell.get("span") or 1))
                    if span > 1 and ci + span - 1 < ncol:
                        try:
                            tbl.cell(ri, ci).merge(tbl.cell(ri, ci + span - 1))
                        except Exception:
                            pass
                    if cell.get("vmerge") == "restart":
                        r2 = ri + 1
                        while r2 < len(rows):
                            nxt = _cell_at(rows[r2], ci)
                            if not nxt or nxt.get("vmerge") != "continue":
                                break
                            r2 += 1
                        if r2 - 1 > ri:
                            try:
                                tbl.cell(ri, ci).merge(tbl.cell(r2 - 1, ci))
                            except Exception:
                                pass
                    ci += span
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
            depth = int(b.get("style", {}).get("list_level") or 0)
            par.paragraph_format.left_indent = Inches(0.3 + 0.24 * min(depth, 8))

    doc.save(out_path)
    return out_path


INLINE_IMAGE_BUDGET = 8 * 1024 * 1024      # 单文件 HTML 内联图片的总预算


def export_html(blocks: List[Dict[str, Any]], asset_dir: str, out_path: str,
                title: str = "") -> str:
    """导出网页。

    图片策略：总量小于 8MB 时内联成 base64（单文件，发给别人直接能看）；
    超过则落到 `<文件名>_files/` 目录用相对路径引用 —— 否则几百张图的笔记
    会导出一个上百 MB、浏览器直接卡死的 HTML。
    """
    from html import escape

    total_img = 0
    for s in all_images(blocks):
        try:
            total_img += os.path.getsize(os.path.join(asset_dir, s["file"]))
        except Exception:
            pass
    inline = total_img <= INLINE_IMAGE_BUDGET
    files_dir = ""
    if not inline:
        stem = os.path.splitext(os.path.basename(out_path))[0]
        files_dir = os.path.join(os.path.dirname(out_path), stem + "_files")
        os.makedirs(files_dir, exist_ok=True)

    parts = []
    for b in blocks:
        if b["kind"] == "table":
            rows = b.get("rows", [])
            parts.append("<table>")
            for row in rows:
                parts.append("<tr>")
                for cell in row:
                    parts.append("<td>" + _html_segs(cell.get("segs", []), asset_dir,
                                                     inline, files_dir) + "</td>")
                parts.append("</tr>")
            parts.append("</table>")
            continue
        lvl = b.get("level", 0)
        tag = f"h{min(max(lvl,1),6)}" if b["kind"] == "heading" and lvl else "p"
        parts.append(f"<{tag}>" + _html_segs(b.get("segs", []), asset_dir,
                                             inline, files_dir) + f"</{tag}>")
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


def _html_segs(segs, asset_dir, inline=True, files_dir=""):
    from html import escape
    out = []
    for s in segs:
        if s["t"] == "text":
            out.append(escape(s["v"]).replace("\n", "<br>"))
        else:
            p = os.path.join(asset_dir, s["file"])
            try:
                if inline:
                    b64 = base64.b64encode(open(p, "rb").read()).decode()
                    ext = s["file"].rsplit(".", 1)[-1].lower()
                    mime = {"jpg": "jpeg", "svg": "svg+xml", "tif": "tiff"}.get(ext, ext)
                    out.append(f'<img src="data:image/{mime};base64,{b64}" alt="">')
                else:
                    shutil.copy2(p, os.path.join(files_dir, s["file"]))
                    rel = os.path.basename(files_dir) + "/" + s["file"]
                    out.append(f'<img src="{escape(rel)}" alt="">')
            except Exception:
                pass
    return "".join(out)


# ---------------------------------------------------------------- 图像审查报告 → blocks
def _img_flags(it: Dict[str, Any]) -> List[str]:
    flags = []
    if it.get("sensitive"):
        flags.append("含敏感信息")
    if it.get("inconsistent"):
        flags.append("与正文不一致")
    if it.get("needs_check"):
        flags.append("需人工核对")
    return flags


def _flag_text(flags: List[str]) -> str:
    return "　【" + "、".join(flags) + "】" if flags else ""


def image_report_blocks(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把图像审查结果转成可导出的 block 列表（标题 + 条目），让总结覆盖图像部分。

    旧版把每张图单独列一行，并逐行重复「未配置视觉模型，无法自动说明」这类占位文案，
    几百张图就是几百行同样的废话。现在改成：
      · 全局状态只在开头说一次；
      · 有真实说明的图逐条列出（含图注/类型/问题）；
      · 状态相同的相邻图合并成一行（图474–481 共 8 张）；
      · 末尾兜底去重 + 行数上限，保证附录永远可读。
    """
    items = (report or {}).get("items") or []
    if not items:
        return []
    total = int(report.get("total") or len(items))
    vision = bool(report.get("vision"))
    reason = str(report.get("vision_reason") or "").strip()

    lead = str(report.get("note") or "").strip() or f"全文共 {total} 张图片。"
    blocks: List[Dict[str, Any]] = [
        new_block("heading", level=1, segs=[{"t": "text", "v": "图像说明与核对"}]),
        new_block("para", segs=[{"t": "text", "v": lead}]),
    ]
    if not vision:
        why = (f"原因：{reason}。" if reason else "")
        blocks.append(new_block("para", segs=[{"t": "text", "v":
            why + "下面按原文位置分组列出图片，便于逐组人工核对；"
                  "在「设置 → 视觉模型」填入本机已安装的多模态模型后重新审查，"
                  "即可自动生成每张图的说明。"}]))
    elif report.get("analyzed", 0) < total:
        blocks.append(new_block("para", segs=[{"t": "text", "v":
            "未生成说明的图片（超出单次识别上限或模型调用失败）只保留位置，"
            "可在「图像审查」页按图补看。"}]))

    lines: List[str] = []

    def single_line(it: Dict[str, Any]) -> str:
        parts = [f"图{it.get('index')}"]
        pos = str(it.get("position") or "").strip()
        if pos:
            parts.append(pos)
        cap = str(it.get("caption") or "").strip()
        if cap:
            parts.append(cap)
        kind = str(it.get("kind") or "").strip()
        if kind and kind not in ("未识别", "未自动分析"):
            parts.append(f"［{kind}］")
        line = "｜".join(parts) + _flag_text(_img_flags(it))
        note = "；".join(str(it.get(k) or "").strip() for k in
                         ("sensitive_note", "inconsistent_note", "check_note")
                         if str(it.get(k) or "").strip())
        if note:
            line += " " + note
        return line

    def group_line(run: List[Dict[str, Any]]) -> str:
        a, b = run[0], run[-1]
        idx_a, idx_b = a.get("index"), b.get("index")
        head = f"图{idx_a}" if idx_a == idx_b else f"图{idx_a}–{idx_b}（{len(run)} 张）"
        pa, pb = a.get("para"), b.get("para")
        pos = ""
        if pa and pb:
            pos = f"第{pa}段" if pa == pb else f"第{pa}–{pb}段"
        heading = str(a.get("heading") or "").strip()
        if heading:
            pos += ("，" if pos else "") + f"位于「{heading}」之后"
        return head + ("｜" + pos if pos else "") + _flag_text(_img_flags(a))

    seen = set()

    def push(line: str):
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            lines.append(line)

    # 按原文顺序走：需要单独说的图逐条写，其余连续的同类图合并成一行
    run: List[Dict[str, Any]] = []
    run_key = None
    for it in items:
        flags = _img_flags(it)
        has_detail = (bool(str(it.get("caption") or "").strip())
                      or "含敏感信息" in flags or "与正文不一致" in flags)
        if has_detail:
            if run:
                push(group_line(run))
                run, run_key = [], None
            push(single_line(it))
            continue
        key = (str(it.get("heading") or ""), tuple(flags))
        if run and key != run_key:
            push(group_line(run))
            run = []
        run.append(it)
        run_key = key
    if run:
        push(group_line(run))

    limit = 200
    for line in lines[:limit]:
        blocks.append(new_block("list", segs=[{"t": "text", "v": line}]))
    if len(lines) > limit:
        blocks.append(new_block("para", segs=[{"t": "text", "v":
            f"另有 {len(lines) - limit} 组图片未在此展开，可在「图像审查」页逐张查看位置与状态。"}]))
    return blocks
