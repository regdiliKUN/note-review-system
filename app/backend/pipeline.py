"""AI 审查流水线：句级送审、汇总归纳、进度回调。"""
from __future__ import annotations

import base64
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List

from .docmodel import block_plain, block_text_only
from .llm import LLM, LLMError, VisionUnsupported, extract_json
from .review import (block_sentences, build_annotation, dedupe_conflicts,
                     finalize as review_finalize,
                     keep_annotation as review_keep)

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

SYSTEM_MAP = """你是中文笔记的要点提取器。用户会给你一篇长笔记的其中一段，\
请提炼出 3-6 条「事实性要点」——只写笔记里真正讲到的内容，不要评价、不要补充、\
不要写成「本段介绍了……」这种空话。每条不超过 30 字。
只输出 JSON：{"points":["要点1","要点2"]}"""

# 审校力度：直接决定模型「敢不敢改」
REVIEW_LEVELS = {
    "light": "只挑硬错误：错别字、事实/数据错误、明显病句。措辞风格上的差异一律不提，"
             "没有硬错误就返回空数组。",
    "normal": "按标准力度审校：硬错误 + 确实值得改的表达与结构问题。",
    "strict": "严格审校：除硬错误外，还要指出可以更精确、更简洁的表达，"
              "并主动补充关键缺失的定义与前提。允许给出较多批注。",
}

# 审查场景：不同类型的笔记，值得关心的东西完全不同。
# 与其让用户自己写一大段提示词，不如给几个开箱可用的关注点。
REVIEW_SCENARIOS = {
    "general": ("通用笔记", ""),
    "paper": ("论文 / 学术",
              "这是一篇学术性笔记：请特别关注概念定义是否准确、引用与出处是否缺失、"
              "论证是否有跳跃、术语前后是否一致。不要为了口语顺滑去改写学术表达。"),
    "meeting": ("会议记录",
                "这是一份会议记录：请特别关注决议与行动项是否明确（谁、做什么、何时）、"
                "人名与部门名是否前后一致、是否有结论缺失、时间地点等事实是否自相矛盾。"),
    "study": ("学习笔记",
              "这是一份学习笔记：请特别关注定义是否完整、例子是否缺失、"
              "易混淆概念是否被混为一谈、推导步骤是否有跳步。可以主动补充关键前提。"),
    "code": ("技术 / 代码笔记",
             "这是一份技术笔记：请特别关注 API 名称、参数、版本号、命令是否正确，"
             "代码块内的标识符不要改写；概念解释与官方文档冲突时要指出。"),
    "product": ("产品 / 需求",
                "这是一份产品需求笔记：请特别关注边界条件是否定义、角色权限是否清楚、"
                "异常流程是否缺失、指标口径是否明确。"),
}


def scenario_focus(key: str) -> str:
    item = REVIEW_SCENARIOS.get((key or "general").lower())
    return item[1] if item else ""


SUMMARY_DIRECT_LIMIT = 6000     # 超过这个字数就走 map-reduce


def _system_review(level: str = "normal", glossary: list | None = None,
                   scenario: str = "general") -> str:
    parts = [SYSTEM_REVIEW]
    lvl = REVIEW_LEVELS.get((level or "normal").lower(), REVIEW_LEVELS["normal"])
    parts.append(f"\n本次审校力度：{lvl}")
    focus = scenario_focus(scenario)
    if focus:
        parts.append(f"\n笔记类型侧重：{focus}")
    if glossary:
        terms = [str(g).strip() for g in glossary if str(g).strip()][:80]
        if terms:
            parts.append(
                "术语白名单（作者固定的写法与专有名词，**不要**当成错别字或表达问题去改）："
                + "、".join(terms))
    return "\n".join(parts)


def _doc_context(doc: Dict[str, Any]) -> str:
    """给每一块都带上全文语境。

    只把这一块单独丢给模型，它看不到上下文，很容易把「上文已经定义过的术语」
    当成「缺失的知识」再补充一遍 —— 这正是旧版批注里最扎眼的一类噪声。
    """
    title = (doc.get("title") or "").strip() or "未命名"
    heads = []
    for b in doc["blocks"]:
        if b["kind"] == "heading":
            t = block_text_only(b).strip()
            if t:
                heads.append(t.replace("\n", " ")[:30])
        if len(heads) >= 12:
            break
    out = [f"文档标题：{title}"]
    if heads:
        out.append("全文结构：" + " ／ ".join(heads))
    return "\n".join(out)


def _table_plain(b) -> str:
    rows = []
    for row in b.get("rows", []):
        cells = []
        for c in row:
            cells.append("".join(s["v"] for s in c.get("segs", []) if s["t"] == "text").strip())
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _block_cost(b: Dict[str, Any]) -> int:
    t = _table_plain(b) if b["kind"] == "table" else block_plain(b)
    return len(t) + 24


def _pieces(blocks: List[Dict[str, Any]], budget: int = 1500) -> List[Dict[str, Any]]:
    """把 block 列表摊平成「可送审片段」。

    普通段落整段作为一个片段；**超过预算的长段落按句切开**，
    每片记住自己的 sid 区间（sid 始终是段落内的绝对编号）。
    这样两万字的单个段落也不会把上下文窗口顶爆，
    而模型返回的 sid 仍然能准确落回原段落。
    """
    out: List[Dict[str, Any]] = []
    for b in blocks:
        if b["kind"] == "table":
            if _table_plain(b).strip():
                out.append({"b": b, "lo": 0, "hi": 0, "sents": []})
            continue
        sents = block_sentences(b)
        if not sents:
            if b["kind"] == "image":
                out.append({"b": b, "lo": 0, "hi": 0, "sents": []})
            continue
        total = sum(len(s["text"]) for s in sents) + 24
        if total <= budget:
            out.append({"b": b, "lo": 0, "hi": len(sents), "sents": sents})
            continue
        lo, size = 0, 0
        for i, s in enumerate(sents):
            size += len(s["text"])
            if size >= budget and i > lo:
                out.append({"b": b, "lo": lo, "hi": i, "sents": sents})
                lo, size = i, len(s["text"])
        out.append({"b": b, "lo": lo, "hi": len(sents), "sents": sents})
    return out


def _chunk_pieces(pieces: List[Dict[str, Any]], budget: int = 1500) -> List[List[Dict]]:
    chunks: List[List[Dict]] = []
    cur: List[Dict] = []
    size = 0
    for p in pieces:
        b = p["b"]
        if b["kind"] == "table":
            n = len(_table_plain(b)) + 24
        else:
            n = sum(len(s["text"]) for s in p["sents"][p["lo"]:p["hi"]]) + 24
        if cur and size + n > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def _chunk_blocks(blocks: List[Dict[str, Any]], budget: int = 1500) -> List[List[Dict]]:
    """兼容接口：只按 block 分组（不拆分长段落）。"""
    chunks, cur, size = [], [], 0
    for b in blocks:
        t = _table_plain(b) if b["kind"] == "table" else block_plain(b)
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


def _render_chunk(pieces: List[Dict[str, Any]]) -> str:
    out = []
    for p in pieces:
        b = p["b"]
        if b["kind"] == "table":
            out.append(f"[{b['id']}｜表格]\n{_table_plain(b)}")
            continue
        label = {"heading": "标题", "list": "列表", "image": "图片"}.get(b["kind"], "正文")
        sents = p.get("sents") or block_sentences(b)
        sel = sents[p["lo"]:p["hi"]]
        if not sel:
            if b["kind"] == "image":
                out.append(f"[{b['id']}｜图片]\n（此处有一张图片，不可删除或移动）")
            continue
        lines = [f"[{b['id']}｜{label}]"]
        if p["lo"] > 0 or p["hi"] < len(sents):
            lines[0] += f"（本段较长，这是其中第 {p['lo'] + 1}–{p['hi']} 句，"
            lines[0] += "句号编号沿用整段的绝对编号）"
        for s in sel:
            lines.append(f"  {s['sid']}| {s['text'].strip()}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _ask_json(llm: LLM, system: str, user: str, attempts: int = 2) -> Dict[str, Any]:
    """要求模型返回 JSON；解析不出来时，追加一句纠正再问一次。

    小模型经常第一轮输出「好的，我来分析……」这类前言，
    第二轮带上明确的纠正提示后成功率会明显上升。
    """
    prompt = user
    for i in range(max(1, attempts)):
        raw = llm.generate(system, prompt, json_mode=True)
        data = extract_json(raw)
        if data:
            return data
        if i == 0:
            prompt = (user + "\n\n注意：上一次输出无法被解析为 JSON。"
                             "这次请**只**输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。")
    return {}


def review_document(doc: Dict[str, Any], llm: LLM,
                    on_progress: Callable[[str, float], None] | None = None,
                    report: Dict[str, Any] | None = None,
                    level: str = "normal",
                    glossary: list | None = None,
                    should_stop: Callable[[], bool] | None = None,
                    only_ids: set | None = None,
                    scenario: str = "general"
                    ) -> List[Dict[str, Any]]:
    """逐块送审。单块失败不会拖垮整篇 —— 失败的分块会记进 report。

    should_stop 返回 True 时在分块之间停下（用户点了取消），
    已完成的批注全部保留，不会白跑。
    only_ids 非空时只审这些段落（增量审查用），其余段落直接跳过。
    """
    blocks = doc["blocks"]
    if only_ids is not None:
        blocks = [b for b in blocks if b["id"] in only_ids]
    pieces = _pieces(blocks)
    chunks = _chunk_pieces(pieces)
    by_id = {b["id"]: b for b in doc["blocks"]}
    # 每个段落的句子切分只做一次，供所有批注复用
    sents_map: Dict[str, List[Dict[str, Any]]] = {}
    for p in pieces:
        sents_map.setdefault(p["b"]["id"], p["sents"])
    anns: List[Dict[str, Any]] = []
    seen: set = set()
    seq = [0]
    total = max(1, len(chunks))
    failed: List[Dict[str, Any]] = []
    empty = 0
    dropped = 0
    canceled = False
    system = _system_review(level, glossary, scenario)
    doc_ctx = _doc_context(doc)
    prev_tail = ""

    for i, chunk in enumerate(chunks):
        if should_stop is not None and should_stop():
            canceled = True
            break
        if on_progress:
            on_progress(f"正在审查第 {i + 1}/{total} 段…", i / total * 0.9)
        user = (
            f"{doc_ctx}\n\n"
            f"以下是笔记的第 {i + 1}/{total} 部分（[b3｜正文] 是段落编号，"
            f"行首数字是句号编号 sid）：\n"
            + (f"\n【紧接上文】{prev_tail}\n" if prev_tail else "\n")
            + f"\n{_render_chunk(chunk)}\n\n"
            f"请审校以上内容，按格式输出 JSON。"
        )
        try:
            data = _ask_json(llm, system, user)
        except LLMError as e:
            # 一块失败不该让整篇白跑：记下来，继续下一块
            failed.append({"chunk": i + 1, "error": str(e)})
            continue
        items = data.get("annotations") or data.get("edits") or []
        if isinstance(items, dict):
            items = [items]
        got = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            a = build_annotation(it, by_id, seq[0] + 1,
                                 sent_list=sents_map.get(str(it.get("block_id") or "").strip()))
            if not a:
                dropped += 1
                continue
            if not review_keep(a):
                dropped += 1
                continue
            key = (a["block_id"], a.get("sid"), a["type"], a["replacement"])
            if key in seen:
                continue
            seen.add(key)
            seq[0] += 1
            a["id"] = f"a{seq[0]}"
            anns.append(a)
            got += 1
        if not got and not items:
            empty += 1
        # 记录本块末尾两句，作为下一块的「紧接上文」
        last = chunk[-1]
        if last["b"]["kind"] != "table":
            sents = block_sentences(last["b"])
            tail = sents[max(0, last["hi"] - 2):last["hi"]]
            prev_tail = "".join(s["text"] for s in tail).strip()[:200]

    # 同一句的冲突改写只留一条，再按严重度排序
    before = len(anns)
    anns = review_finalize(anns)
    conflicts = before - len(anns)
    if report is not None:
        report.update({"chunks": len(chunks), "failed": failed, "empty": empty,
                       "annotations": len(anns), "dropped": dropped,
                       "conflicts": conflicts, "level": level,
                       "canceled": canceled,
                       "done_chunks": total if not canceled else i})
    if on_progress:
        on_progress("已取消，保留已完成的部分" if canceled else "正在生成整体归纳…",
                    0.92 if not canceled else 0.5)
    return anns


def SEVERITY_RANK(s: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(s, 1)


def _split_text(text: str, size: int) -> List[str]:
    """按段落边界切分长文本，尽量不把句子切断。"""
    parts, cur, n = [], [], 0
    for para in text.split("\n"):
        if n + len(para) > size and cur:
            parts.append("\n".join(cur))
            cur, n = [], 0
        cur.append(para)
        n += len(para) + 1
    if cur:
        parts.append("\n".join(cur))
    return [p for p in parts if p.strip()]


def summarize(doc: Dict[str, Any], llm: LLM,
              on_progress: Callable[[str, float], None] | None = None) -> Dict[str, Any]:
    """整篇归纳。

    长文档不再「截断到 6000 字」—— 那等于把后半篇直接扔掉。
    现在先分段提炼要点（map），再用要点做整体归纳（reduce）。
    """
    blocks = [b for b in doc["blocks"] if b["kind"] != "table"]
    text = "\n".join(block_plain(b) for b in blocks)
    text = re.sub(r"⟦图\d+⟧", "", text).strip()
    if not text:
        return {"summary": "", "key_points": [], "outline": [], "gaps": [],
                "map_reduce": False, "covered": 0}

    map_reduce = False
    covered = len(text)
    if len(text) > SUMMARY_DIRECT_LIMIT:
        parts = _split_text(text, 5000)[:16]
        points: List[str] = []
        for i, p in enumerate(parts):
            if on_progress:
                on_progress(f"正在提炼要点 {i + 1}/{len(parts)}…", 0.93 + 0.03 * (i / len(parts)))
            try:
                d = _ask_json(llm, SYSTEM_MAP,
                              f"这是长笔记的第 {i + 1}/{len(parts)} 部分：\n\n{p}\n\n"
                              "请按格式输出 JSON。", attempts=1)
            except LLMError:
                continue
            for x in (d.get("points") or []):
                x = str(x).strip()
                if x and x not in points:
                    points.append(x)
        if points:
            map_reduce = True
            text = "以下是这篇长笔记各部分提炼出的要点（按原文顺序）：\n" + "\n".join(
                f"- {p}" for p in points[:120])
            covered = len(text)

    user = (
        f"标题：{doc.get('title', '')}\n\n笔记全文：\n{text}\n\n"
        "请输出 JSON：\n"
        '{"summary":"用2-4句话概括这篇笔记讲了什么（150字内）",'
        '"key_points":["要点1","要点2","要点3"],'
        '"outline":["建议的一级目录1","一级目录2"],'
        '"gaps":["笔记中明显缺失、建议补充的内容"]}'
    )
    try:
        d = _ask_json(llm, SYSTEM_SUMMARY, user, attempts=1)
    except LLMError:
        d = {}

    def _uniq(v, limit=12):
        out, seen = [], set()
        for x in (v or []):
            s = str(x).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s[:120])
            if len(out) >= limit:
                break
        return out

    return {
        "summary": str(d.get("summary") or "").strip()[:600],
        "key_points": _uniq(d.get("key_points"), 12),
        "outline": _uniq(d.get("outline"), 12),
        "gaps": _uniq(d.get("gaps"), 10),
        "map_reduce": map_reduce,
        "covered": covered,
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
# 同一张图在文档里重复出现（页眉 logo、重复截图）时，压缩+编码只做一次
_VISION_CACHE: Dict[str, tuple] = {}
_VISION_CACHE_MAX = 256


def _vision_payload(blob: bytes, ext: str):
    """把图片缩到视觉模型够用的尺寸再送审。

    只影响「送模型的副本」：磁盘上的原图、界面显示和导出始终是原始格式与清晰度，
    不做任何有损替换。没有 Pillow 时退回原图。
    """
    import hashlib
    key = hashlib.md5(blob).hexdigest()
    hit = _VISION_CACHE.get(key)
    if hit is not None:
        return hit
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
        out = (base64.b64encode(buf.getvalue()).decode(), "image/jpeg")
    except Exception:
        out = (base64.b64encode(blob).decode(), _img_mime(ext))
    if len(_VISION_CACHE) >= _VISION_CACHE_MAX:
        _VISION_CACHE.clear()
    _VISION_CACHE[key] = out
    return out


def analyze_images(doc: Dict[str, Any], llm: LLM,
                   on_progress: Callable[[str, float], None] | None = None,
                   max_images: int = 300, concurrency: int = 3) -> Dict[str, Any]:
    """逐图理解：生成说明、标注原文位置、识别不一致/敏感信息/需人工核对。

    返回 {"total","analyzed","vision","vision_model","vision_reason","note","items":[...]}，
    items 顺序与文档一致。

    降级策略：没有可用的视觉模型时，**只给一次全局说明**，每张图仅保留位置信息，
    不再逐条复述「未配置视觉模型」这类占位文案（旧版会把同一句话重复几百遍）。
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
    # ---- 视觉能力：先问模型服务的真实能力；没配置但本机装了视觉模型就自动启用
    vs = llm.vision_status()
    if not vs.get("enabled") and vs.get("auto"):
        llm.vision_model = vs["auto"]
        vs = llm.vision_status()
        vs["detected_by"] = "auto-picked"
    report: Dict[str, Any] = {
        "total": total, "analyzed": 0, "items": [],
        "vision": bool(vs.get("enabled")),
        "vision_model": vs.get("model", ""),
        "vision_reason": "" if vs.get("enabled") else (vs.get("reason") or "本机没有可用的多模态模型"),
        "vision_detected_by": vs.get("detected_by", ""),
        "note": "",
    }
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
            "position": pos_label(it), "heading": it["heading"], "para": it["bi"] + 1,
            "kind": "", "caption": "", "inconsistent": False, "inconsistent_note": "",
            "sensitive": False, "sensitive_note": "",
            "needs_check": False, "check_note": "", "analyzed": False,
        }

    # ---- 降级：不写占位文案，只保留位置；原因统一放在 report 里说一次
    if not report["vision"]:
        for idx, it in enumerate(items):
            e = base_entry(idx, it)
            e["needs_check"] = True
            report["items"].append(e)
        report["note"] = (f"全文共 {total} 张图片，已按原文位置逐张标注；"
                          f"本次未做图片内容识别（{report['vision_reason']}）。")
        return report

    limit = min(total, max_images)

    def work(idx: int) -> Dict[str, Any]:
        """处理单张图片，返回它的报告条目（不直接写 report，便于并发后按序归位）。"""
        it = items[idx]
        e = base_entry(idx, it)
        s = it["seg"]
        # 视频关键帧在解析阶段已经过视觉模型，这里直接复用，不重复送审
        pre_cap = (s.get("pre_caption") or "").strip()
        if pre_cap:
            e["kind"] = s.get("pre_kind") or "视频关键帧"
            e["caption"] = pre_cap
            e["analyzed"] = True
            return e
        ext = e["ext"]
        # 矢量/特殊格式：无法直接送视觉模型
        if ext in ("emf", "wmf", "tif", "tiff", "svg"):
            e["kind"] = "矢量/特殊格式"
            e["caption"] = f"{ext.upper()} 矢量图，未做像素级识别"
            e["needs_check"] = True
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
        alt = (s.get("alt") or "").strip()
        if alt:
            ctx.append("作者为该图写的替代文字：" + alt[:120])
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
        if llm._vision_blocked:      # 别的线程已经确认模型不收图片，别再试
            e["needs_check"] = True
            return e
        try:
            raw = llm.generate_vision(IMG_SYSTEM, user,
                                      [{"b64": b64, "mime": mime}], json_mode=True)
            d = extract_json(raw)
        except VisionUnsupported as ex:
            e["needs_check"] = True
            e["check_note"] = str(ex)
            return e
        except LLMError as ex:
            e["needs_check"] = True
            e["check_note"] = f"视觉模型调用失败：{ex}"
            return e
        except Exception as ex:
            e["needs_check"] = True
            e["check_note"] = f"图像分析出错：{ex}"
            return e
        e["kind"] = str(d.get("kind") or "其他")[:12]
        e["caption"] = str(d.get("caption") or "").strip()[:80]
        # 标记为 false 时模型常回一句「未发现敏感信息」之类的空话，不要带进文档
        for flag, note in (("inconsistent", "inconsistent_note"),
                           ("sensitive", "sensitive_note"),
                           ("needs_check", "check_note")):
            on = bool(d.get(flag))
            e[flag] = on
            e[note] = str(d.get(note) or "").strip()[:200] if on else ""
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

    # 超过上限的图片：不丢，标为需人工核对（说明只写一次，不逐条复述）
    for idx in range(limit, total):
        e = base_entry(idx, items[idx])
        e["kind"] = "未自动分析"
        e["needs_check"] = True
        report["items"].append(e)

    report["analyzed"] = sum(1 for it in report["items"] if it.get("analyzed"))
    if llm._vision_blocked and report["analyzed"] < limit:
        report["vision"] = False
        report["vision_reason"] = "模型服务拒绝了图片输入（已自动停止后续识别）"
    note = f"已自动分析 {report['analyzed']}/{total} 张图片"
    if total > limit:
        note += f"（单次上限 {max_images} 张，其余需人工核对）"
    if report["vision_reason"] and report["analyzed"] == 0:
        note += f"；未识别原因：{report['vision_reason']}"
    report["note"] = note
    return report
