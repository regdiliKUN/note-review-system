"""AI 审查流水线：句级送审、汇总归纳、进度回调。"""
from __future__ import annotations

import base64
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List

from .docmodel import block_plain, block_text_only
from .llm import LLM, LLMError, extract_json
from .review import block_sentences, build_annotation

SYSTEM_REVIEW = """你是一位资深的中文笔记审校与知识编辑专家。用户会给你一篇零散的个人笔记，\
按段落和句子编号列出。你要找出真正的问题，并给出可以直接采纳的修改。

每个句子都有编号，你只需要引用编号（sid），不需要复制原文 —— 系统会自己定位。

审校维度（type 只能取以下七个值之一）：
- 错别字：错字、别字、同音误用、标点误用
- 事实纠错：事实、数据、年份、人名、定义、概念上的错误
- 知识补充：缺失但重要的定义、前提、例子、背景
- 表达优化：啰嗦、指代不清、语序混乱、口语化，改成通顺的书面表达
- 逻辑归纳：把散落的同类要点合并、提炼成要点句
- 结构建议：层级混乱、缺标题、段落切分不当
- 存疑待核：无法确认、需要作者自己核实的内容

op 的含义：
- replace_sentence：用 replacement 整句替换第 sid 句
- insert_after：在第 sid 句之后追加 replacement（不改原句）
- comment：只提醒、不改文字，replacement 留空

硬性规则：
1. replacement 必须是完整通顺的句子，不能是片段、不能用省略号。
2. 不要为改而改。原文意思正确、只是风格不同的地方不要提。宁少勿滥。
3. 一条批注只处理一个问题，方便作者逐条取舍。
4. reason 必须具体说明依据，让作者能判断要不要采纳。
5. 优先处理高价值问题：事实错误、关键知识缺失 > 措辞润色。
6. 每段最多给 2-3 条批注；没有真问题就返回空数组。

只输出 JSON，不要任何解释文字。格式示例：
{"annotations":[
 {"block_id":"b4","sid":0,"op":"replace_sentence","type":"事实纠错",
  "replacement":"修正后的完整句子。","reason":"原句把 Adam 的提出年份写错了，应为 2014 年。","severity":"high"},
 {"block_id":"b4","sid":1,"op":"insert_after","type":"知识补充",
  "replacement":"需要补充的新句子。","reason":"缺少关键前提。","severity":"medium"},
 {"block_id":"b9","sid":null,"op":"comment","type":"结构建议",
  "replacement":"","reason":"本节缺小标题，建议拆分。","severity":"low"}
]}
severity 取 high / medium / low。"""

SYSTEM_SUMMARY = """你是中文笔记的结构编辑。请阅读整篇笔记，输出一份 JSON 归纳结果。\
只输出 JSON，不要解释。"""


def _table_plain(b) -> str:
    rows = []
    for row in b.get("rows", []):
        cells = []
        for c in row:
            cells.append("".join(s["v"] for s in c.get("segs", []) if s["t"] == "text").strip())
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _chunk_blocks(blocks: List[Dict[str, Any]], budget: int = 1500) -> List[List[Dict]]:
    chunks, cur, size = [], [], 0
    for b in blocks:
        if b["kind"] == "table":
            t = _table_plain(b)
        else:
            t = block_plain(b)
        if not t.strip():
            continue
        n = len(t) + 24
        if cur and size + n > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def _render_chunk(blocks: List[Dict[str, Any]]) -> str:
    out = []
    for b in blocks:
        if b["kind"] == "table":
            out.append(f"[{b['id']}｜表格]\n{_table_plain(b)}")
            continue
        label = {"heading": "标题", "list": "列表", "image": "图片"}.get(b["kind"], "正文")
        sents = block_sentences(b)
        if not sents:
            if b["kind"] == "image":
                out.append(f"[{b['id']}｜图片]\n（此处有一张图片，不可删除或移动）")
            continue
        lines = [f"[{b['id']}｜{label}]"]
        for s in sents:
            lines.append(f"  {s['sid']}| {s['text'].strip()}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def review_document(doc: Dict[str, Any], llm: LLM,
                    on_progress: Callable[[str, float], None] | None = None
                    ) -> List[Dict[str, Any]]:
    blocks = doc["blocks"]
    chunks = _chunk_blocks(blocks)
    by_id = {b["id"]: b for b in blocks}
    anns: List[Dict[str, Any]] = []
    seq = [0]
    total = max(1, len(chunks))

    for i, chunk in enumerate(chunks):
        if on_progress:
            on_progress(f"正在审查第 {i + 1}/{total} 段…", i / total * 0.9)
        user = (
            f"文档标题：{doc.get('title', '未命名')}\n\n"
            f"以下是笔记的第 {i + 1}/{total} 部分（[b3｜正文] 是段落编号，"
            f"行首数字是句号编号 sid）：\n\n"
            f"{_render_chunk(chunk)}\n\n"
            f"请审校以上内容，按格式输出 JSON。"
        )
        raw = llm.generate(SYSTEM_REVIEW, user, json_mode=True)
        data = extract_json(raw)
        items = data.get("annotations") or data.get("edits") or []
        if isinstance(items, dict):
            items = [items]
        for it in items:
            if not isinstance(it, dict):
                continue
            seq[0] += 1
            a = build_annotation(it, by_id, seq[0])
            if not a:
                continue
            key = (a["block_id"], a.get("sid"), a["type"], a["replacement"])
            if any((x["block_id"], x.get("sid"), x["type"], x["replacement"]) == key
                   for x in anns):
                continue
            anns.append(a)

    # 严重问题排前面，方便作者先看要紧的
    anns.sort(key=lambda a: (SEVERITY_RANK(a["severity"]),
                             int(a["block_id"][1:]) if a["block_id"][1:].isdigit() else 0))
    for i, a in enumerate(anns):
        a["id"] = f"a{i + 1}"
    if on_progress:
        on_progress("正在生成整体归纳…", 0.92)
    return anns


def SEVERITY_RANK(s: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(s, 1)


def summarize(doc: Dict[str, Any], llm: LLM) -> Dict[str, Any]:
    blocks = [b for b in doc["blocks"] if b["kind"] != "table"]
    text = "\n".join(block_plain(b) for b in blocks)
    text = re.sub(r"⟦图\d+⟧", "", text)
    if len(text) > 6000:
        text = text[:6000]
    user = (
        f"标题：{doc.get('title','')}\n\n笔记全文：\n{text}\n\n"
        "请输出 JSON：\n"
        '{"summary":"用2-4句话概括这篇笔记讲了什么（150字内）",'
        '"key_points":["要点1","要点2","要点3"],'
        '"outline":["建议的一级目录1","一级目录2"],'
        '"gaps":["笔记中明显缺失、建议补充的内容"]}'
    )
    try:
        raw = llm.generate(SYSTEM_SUMMARY, user, json_mode=True)
        d = extract_json(raw)
    except LLMError:
        d = {}
    return {
        "summary": d.get("summary", ""),
        "key_points": d.get("key_points") or [],
        "outline": d.get("outline") or [],
        "gaps": d.get("gaps") or [],
    }


# ---------------------------------------------------------------- 图像理解
IMG_SYSTEM = """你是中文笔记的图像审校助手。用户会给出一张笔记中的图片，以及它前后的正文文字。
请完成三件事：
1) 判断图片类型：截图 / 图表 / 流程图 / 公式 / 手写笔记 / 照片 / 界面 / 其他；
2) 用一句话（不超过 40 字）说明这张图讲了什么；
3) 检查三类问题（没有就填 false）：
   - inconsistent 与正文不一致：图片内容与前后文描述矛盾、图不对题；
   - sensitive 含敏感信息：出现身份证号、手机号、住址、账号密码、密钥、他人隐私等；
   - needs_check 需人工核对：图片模糊/看不清/信息不完整/疑似错图。
只输出 JSON，不要解释。格式：
{"kind":"截图","caption":"...","inconsistent":false,"inconsistent_note":"",
 "sensitive":false,"sensitive_note":"","needs_check":false,"check_note":""}"""


def _img_mime(ext: str) -> str:
    e = (ext or "").lower()
    return {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "gif": "image/gif", "bmp": "image/bmp", "webp": "image/webp",
            "tif": "image/tiff", "tiff": "image/tiff"}.get(e, "image/png")


VISION_MAX_SIDE = 1024


def _vision_payload(blob: bytes, ext: str):
    """把图片缩到视觉模型够用的尺寸再送审。

    只影响「送模型的副本」：磁盘上的原图、界面显示和导出始终是原始格式与清晰度，
    不做任何有损替换。没有 Pillow 时退回原图。
    """
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(blob))
        im.load()
        if max(im.size) > VISION_MAX_SIDE:
            im.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE), Image.LANCZOS)
        if im.mode in ("RGBA", "LA", "P"):
            conv = im.convert("RGBA")
            bg = Image.new("RGB", conv.size, (255, 255, 255))
            bg.paste(conv, mask=conv.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90, optimize=True)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    except Exception:
        return base64.b64encode(blob).decode(), _img_mime(ext)


def analyze_images(doc: Dict[str, Any], llm: LLM,
                   on_progress: Callable[[str, float], None] | None = None,
                   max_images: int = 300, concurrency: int = 3) -> Dict[str, Any]:
    """逐图理解：生成说明、标注原文位置、识别不一致/敏感信息/需人工核对。

    返回 {"total","analyzed","vision","note","items":[...]}，items 顺序与文档一致。
    无视觉模型或调用失败时优雅降级为「需人工核对」，绝不丢图。
    """
    blocks = doc["blocks"]
    asset_dir = doc.get("asset_dir", "")

    items: List[Dict[str, Any]] = []
    last_heading = ""
    for bi, b in enumerate(blocks):
        if b["kind"] == "table":
            continue
        if b["kind"] == "heading":
            last_heading = block_plain(b).strip()
        for s in b.get("segs", []):
            if s["t"] == "img":
                items.append({"bi": bi, "block": b, "seg": s, "heading": last_heading})

    total = len(items)
    report: Dict[str, Any] = {"total": total, "analyzed": 0, "items": [],
                              "vision": llm.supports_vision(), "note": ""}
    if total == 0:
        report["note"] = "文档中没有图片"
        return report

    def pos_label(it: Dict[str, Any]) -> str:
        head = f"，位于「{it['heading']}」之后" if it["heading"] else ""
        return f"第{it['bi'] + 1}段（{it['block']['id']}）{head}"

    def base_entry(idx: int, it: Dict[str, Any]) -> Dict[str, Any]:
        s = it["seg"]
        return {
            "index": idx + 1, "image_id": s.get("id", ""),
            "block_id": it["block"]["id"], "file": s.get("file", ""),
            "ext": (s.get("file", "").rsplit(".", 1)[-1] or "").lower(),
            "w": s.get("w"), "h": s.get("h"),
            "position": pos_label(it), "kind": "未识别",
            "caption": "", "inconsistent": False, "inconsistent_note": "",
            "sensitive": False, "sensitive_note": "",
            "needs_check": False, "check_note": "", "analyzed": False,
        }

    # 无视觉模型：全部标为需人工核对，但仍给出位置
    if not llm.supports_vision():
        for idx, it in enumerate(items):
            e = base_entry(idx, it)
            e["caption"] = f"图片 {idx + 1}（未配置视觉模型，无法自动说明）"
            e["needs_check"] = True
            e["check_note"] = "未配置多模态模型，图片内容需人工核对"
            report["items"].append(e)
        report["note"] = "未配置多模态（视觉）模型：已为每张图标注位置并列入人工核对清单"
        return report

    limit = min(total, max_images)

    def work(idx: int) -> Dict[str, Any]:
        """处理单张图片，返回它的报告条目（不直接写 report，便于并发后按序归位）。"""
        it = items[idx]
        e = base_entry(idx, it)
        ext = e["ext"]
        # 矢量/特殊格式：无法直接送视觉模型
        if ext in ("emf", "wmf", "tif", "tiff", "svg"):
            e["kind"] = "矢量/特殊格式"
            e["caption"] = f"图片 {idx + 1}（{ext.upper()} 矢量图，未做像素级识别）"
            e["needs_check"] = True
            e["check_note"] = "矢量图无法直接识别，需人工核对"
            return e
        path = os.path.join(asset_dir, e["file"])
        try:
            with open(path, "rb") as f:
                b64, mime = _vision_payload(f.read(), ext)
        except Exception as ex:
            e["needs_check"] = True
            e["check_note"] = f"图片文件读取失败：{ex}"
            return e
        ctx: List[str] = []
        own = block_text_only(it["block"]).strip()
        if own:
            ctx.append("本段：" + own[:200])
        for j in range(it["bi"] - 1, -1, -1):
            if blocks[j]["kind"] != "table" and block_text_only(blocks[j]).strip():
                ctx.append("上文：" + block_text_only(blocks[j]).strip()[:200]); break
        for j in range(it["bi"] + 1, len(blocks)):
            if blocks[j]["kind"] != "table" and block_text_only(blocks[j]).strip():
                ctx.append("下文：" + block_text_only(blocks[j]).strip()[:200]); break
        user = (f"笔记标题：{doc.get('title', '')}\n图片位置：{e['position']}\n"
                + "\n".join(ctx) + "\n\n请分析这张图片并按格式输出 JSON。")
        try:
            raw = llm.generate_vision(IMG_SYSTEM, user,
                                      [{"b64": b64, "mime": mime}], json_mode=True)
            d = extract_json(raw)
        except LLMError as ex:
            e["needs_check"] = True
            e["check_note"] = f"视觉模型调用失败：{ex}"
            return e
        except Exception as ex:
            e["needs_check"] = True
            e["check_note"] = f"图像分析出错：{ex}"
            return e
        e["kind"] = str(d.get("kind") or "其他")[:12]
        e["caption"] = str(d.get("caption") or "").strip()[:80] or f"图片 {idx + 1}"
        for k in ("inconsistent", "sensitive", "needs_check"):
            e[k] = bool(d.get(k))
        for k in ("inconsistent_note", "sensitive_note", "check_note"):
            e[k] = str(d.get(k) or "").strip()[:200]
        e["analyzed"] = True
        return e

    # 并发送审：本机 Ollama 单卡通常能并行 2~4 路，串行处理几百张图会等到天荒地老
    results: List[Dict[str, Any] | None] = [None] * limit
    workers = max(1, min(int(concurrency or 1), limit))
    done = 0
    if on_progress:
        on_progress(f"正在理解图片 0/{limit}…", 0.9)
    if workers == 1 or limit == 1:
        for idx in range(limit):
            results[idx] = work(idx)
            done += 1
            if on_progress:
                on_progress(f"正在理解图片 {done}/{limit}…", 0.9 + 0.08 * (done / limit))
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, i): i for i in range(limit)}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception as ex2:  # 兜底：线程内异常也不能让整篇审查崩掉
                    e = base_entry(i, items[i])
                    e["needs_check"] = True
                    e["check_note"] = f"图像分析异常：{ex2}"
                    results[i] = e
                done += 1
                if on_progress:
                    on_progress(f"正在理解图片 {done}/{limit}…", 0.9 + 0.08 * (done / limit))
    for e in results:
        if e is not None:
            report["items"].append(e)

    # 超过上限的图片：不丢，标为需人工核对
    for idx in range(limit, total):
        e = base_entry(idx, items[idx])
        e["kind"] = "未自动分析"
        e["caption"] = f"图片 {idx + 1}（超出自动分析上限 {limit}，需人工核对）"
        e["needs_check"] = True
        e["check_note"] = "超出自动分析数量上限，请人工核对"
        report["items"].append(e)

    report["analyzed"] = sum(1 for it in report["items"] if it.get("analyzed"))
    report["note"] = (f"已自动分析 {report['analyzed']}/{total} 张图片"
                      + (f"（上限 {max_images}，其余需人工核对）" if total > limit else ""))
    return report
