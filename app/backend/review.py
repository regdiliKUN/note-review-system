"""批注引擎：把模型返回的修改建议落成「可逐条采纳/丢弃」的操作对象。

关键约束：所有文字改动只在 text 片段内部进行，img 片段的下标与顺序永不变动。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .docmodel import IMG_MARK, block_plain, block_text_only

TYPES = ["错别字", "事实纠错", "知识补充", "表达优化", "逻辑归纳", "结构建议", "存疑待核"]
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# ---------------------------------------------------------------- 文本索引
def _stream(segs: List[Dict[str, Any]]) -> Tuple[str, List[Tuple[int, int]]]:
    chars, idx = [], []
    for si, s in enumerate(segs):
        if s["t"] == "text":
            for off, ch in enumerate(s["v"]):
                chars.append(ch)
                idx.append((si, off))
    return "".join(chars), idx


def _norm_stream(segs) -> Tuple[str, List[Tuple[int, int]]]:
    """把空白折叠后再建索引，容忍模型返回时对空格/换行的细微差异。"""
    raw, idx = _stream(segs)
    chars, nidx = [], []
    prev_space = False
    for ch, pos in zip(raw, idx):
        if ch in " \t\r\n\u3000":
            if prev_space:
                continue
            prev_space = True
            chars.append(" ")
        else:
            prev_space = False
            chars.append(ch)
        nidx.append(pos)
    return "".join(chars), nidx


def locate(segs, target: str) -> Optional[Tuple[int, int, int, int]]:
    """返回 (seg_start, off_start, seg_end, off_end)；end 为开区间。"""
    if not target:
        return None
    raw, ridx = _stream(segs)
    p = raw.find(target)
    if p >= 0:
        s = ridx[p]
        e = ridx[p + len(target) - 1]
        return (s[0], s[1], e[0], e[1] + 1)

    nt = re.sub(r"\s+", " ", target.strip())
    if not nt:
        return None
    norm, nidx = _norm_stream(segs)
    q = norm.find(nt)
    if q >= 0:
        s = nidx[q]
        e = nidx[q + len(nt) - 1]
        return (s[0], s[1], e[0], e[1] + 1)

    # 最后兜底：去掉所有空白再比一次
    bare = re.sub(r"\s+", "", target)
    if len(bare) < 2:
        return None
    braw = re.sub(r"\s+", "", raw)
    bp = braw.find(bare)
    if bp < 0:
        return None
    # 映射回原始偏移
    count = -1
    start = end = None
    for i, ch in enumerate(raw):
        if not ch.isspace():
            count += 1
            if count == bp and start is None:
                start = i
            if count == bp + len(bare) - 1:
                end = i
                break
    if start is None or end is None:
        return None
    s = ridx[start]
    e = ridx[end]
    return (s[0], s[1], e[0], e[1] + 1)


def apply_edit(segs, loc, repl: str) -> List[Dict[str, Any]]:
    """在 (seg_start,off_start,seg_end,off_end) 区间用 repl 替换；图片片段不受影响。"""
    import copy
    new = copy.deepcopy(segs)
    s_si, s_off, e_si, e_off = loc
    if s_si == e_si:
        v = new[s_si]["v"]
        new[s_si]["v"] = v[:s_off] + repl + v[e_off:]
        return new
    # 跨片段（中间夹着图片）：首片段保留前缀+新文字，末片段保留后缀，图片原地不动
    new[s_si]["v"] = new[s_si]["v"][:s_off] + repl
    for k in range(s_si + 1, e_si):
        if new[k]["t"] == "text":
            new[k]["v"] = ""
    new[e_si]["v"] = new[e_si]["v"][e_off:]
    return new


# ---------------------------------------------------------------- 句级锚点
SENT_END = "。！？；!?;\n…"


def block_sentences(b: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 block 切成句子，每个句子记住它落在哪个 seg 的哪一段。

    句子绝不跨越 seg 边界，因此锚点永远不会跨过图片。
    """
    sents: List[Dict[str, Any]] = []
    sid = 0
    for si, s in enumerate(b.get("segs", [])):
        if s["t"] != "text":
            continue
        v = s["v"]
        start = 0
        for i, ch in enumerate(v):
            if ch in SENT_END:
                piece = v[start:i + 1]
                if piece.strip():
                    sents.append({"sid": sid, "text": piece, "seg": si,
                                  "start": start, "end": i + 1})
                    sid += 1
                start = i + 1
        if start < len(v):
            piece = v[start:]
            if piece.strip():
                sents.append({"sid": sid, "text": piece, "seg": si,
                              "start": start, "end": len(v)})
                sid += 1
    return sents


# ---------------------------------------------------------------- 批注对象
def build_annotation(raw: Dict[str, Any], blocks_by_id: Dict[str, Dict],
                     seq: int,
                     sent_list: List[Dict[str, Any]] | None = None) -> Optional[Dict[str, Any]]:
    """把模型返回的一条建议落成批注对象。

    sent_list 可以传入调用方已经算好的句子列表 —— 一篇 1000 段、500 条批注的
    文档里，每次都重新切句会让审查尾段变成明显的卡顿。
    """
    bid = str(raw.get("block_id") or "").strip()
    b = blocks_by_id.get(bid)
    if not b or b["kind"] == "table":
        return None

    op = raw.get("op") or "replace_sentence"
    if op not in ("replace_sentence", "insert_after", "comment", "replace"):
        op = "replace_sentence"
    repl = raw.get("replacement") or ""
    if isinstance(repl, list):
        repl = "".join(str(x) for x in repl)
    repl = clean_replacement(str(repl))

    segs = b["segs"]
    sents = sent_list if sent_list is not None else block_sentences(b)
    anchor = None
    target = (raw.get("target") or "").strip()

    # 优先用句子编号定位（可靠）；没有 sid 时退回原文匹配
    sid = raw.get("sid")
    sent = None
    if isinstance(sid, int) or (isinstance(sid, str) and sid.isdigit()):
        sid = int(sid)
        sent = next((s for s in sents if s["sid"] == sid), None)

    if sent:
        target = sent["text"].strip()
        if op == "replace_sentence":
            anchor = (sent["seg"], sent["start"], sent["seg"], sent["end"])
        elif op == "insert_after":
            anchor = (sent["seg"], sent["end"], sent["seg"], sent["end"])
        else:
            anchor = (sent["seg"], sent["start"], sent["seg"], sent["start"])
    elif op != "comment":
        loc = locate(segs, target) if target else None
        if loc is None:
            op = "comment"
        else:
            anchor = loc if op != "insert_after" else (loc[2], loc[3], loc[2], loc[3])

    if op == "replace_sentence":
        if not anchor or not repl.strip():
            return None
        if repl.strip() == target:
            return None
    if op == "insert_after":
        if not anchor or not repl.strip():
            return None
    if op == "comment" and not (raw.get("reason") or repl):
        return None

    typ = raw.get("type") or "表达优化"
    if typ not in TYPES:
        typ = "表达优化"
    sev = raw.get("severity") or "medium"
    if sev not in SEVERITY_ORDER:
        sev = "medium"

    return {
        "id": f"a{seq}",
        "block_id": bid,
        "op": op,
        "type": typ,
        "target": target,
        "replacement": repl if op != "comment" else "",
        "reason": (raw.get("reason") or "").strip(),
        "severity": sev,
        "status": "pending",
        "anchor": list(anchor) if anchor else None,
        "sid": sent["sid"] if sent else None,
    }


# ---------------------------------------------------------------- 应用全部已采纳
def apply_accepted(blocks: List[Dict[str, Any]],
                   anns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """返回一份新的 blocks（已采纳的修改已合入），图片片段位置保持不变。"""
    import copy
    out = copy.deepcopy(blocks)
    by_block: Dict[str, List[Dict[str, Any]]] = {}
    for a in anns:
        if a["status"] == "accepted" and a["op"] in ("replace", "replace_sentence", "insert_after") and a.get("anchor"):
            by_block.setdefault(a["block_id"], []).append(a)
    if not by_block:
        return out

    # 先建 id -> block 索引：旧版对每条批注都线性扫一遍 blocks，
    # 1000 段 × 500 条批注就是 50 万次比较，点一次「全部保留」要卡好几秒。
    index = {b["id"]: b for b in out}

    for bid, ops in by_block.items():
        b = index.get(bid)
        if not b:
            continue
        # 从后往前改，避免偏移失效。
        # 同一位置既有「整句替换」又有「句后插入」时，必须先处理跨度更长的那个：
        # 否则插入的文字会被随后到达的替换区间吞掉（表现为补充的句子凭空消失）。
        ops.sort(key=lambda a: (a["anchor"][0], a["anchor"][1],
                                a["anchor"][3] - a["anchor"][2]), reverse=True)
        for a in ops:
            b["segs"] = apply_edit(b["segs"], a["anchor"], a["replacement"])
        # 清理空文字片段（不动图片）
        cleaned = []
        for s in b["segs"]:
            if s["t"] == "text" and not s["v"]:
                continue
            if s["t"] == "text":
                s["v"] = tidy_punct(s["v"])
            cleaned.append(s)
        b["segs"] = cleaned
    return out


_DUP_PUNCT = re.compile(r"([。，、；：！？,;:!?])\1+")
_SPACE_PUNCT = re.compile(r"[ \t]+([，。、；：！？）】」』])")

# 模型很喜欢在 replacement 前面加一句「修改后：」，整句塞进正文就成了病句
_REPL_PREFIX = re.compile(
    r"^\s*(?:修改后|修改为|改为|建议改为|建议|替换为|替换成|更正为|修正后|"
    r"正确写法|答案|output|replacement)\s*[：:]\s*", re.I)
_PLACEHOLDER = re.compile(r"^(?:…+|\.{3,}|-+|（略）|\(略\)|同上|无|略|N/?A)$", re.I)


def clean_replacement(t: str) -> str:
    """清洗模型给的替换文本：去前缀、去整体包裹的引号、去占位符。"""
    if not t:
        return ""
    s = str(t).strip()
    s = _REPL_PREFIX.sub("", s)
    for a, b in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
                 ("`", "`"), ("「", "」"), ("『", "』")):
        if len(s) >= 2 and s.startswith(a) and s.endswith(b):
            s = s[1:-1].strip()
    return s


def is_placeholder(t: str) -> bool:
    return bool(_PLACEHOLDER.match((t or "").strip()))


def keep_annotation(a: Dict[str, Any]) -> bool:
    """过滤掉「改了等于没改」和「说不清为什么改」的批注。

    模型的典型噪声：
      · replacement 是「……」或「（略）」这种占位符
      · 整句替换却完全没写 reason，作者无法判断要不要采纳
      · 只提醒不修改，但 reason 只有两个字
    """
    if a.get("op") == "comment":
        return len((a.get("reason") or "").strip()) >= 4
    repl = (a.get("replacement") or "").strip()
    if not repl or is_placeholder(repl):
        return False
    if a.get("op") in ("replace", "replace_sentence") and not (a.get("reason") or "").strip():
        return False
    return True


def dedupe_conflicts(anns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一处文字只允许一条「整句替换」。

    模型偶尔会对同一句给出两条互相矛盾的改写，两条都采纳会让文字变成
    半句 A 半句 B。这里保留严重度最高的一条，其余丢弃。
    """
    best: Dict[Any, Dict[str, Any]] = {}
    for a in anns:
        if a.get("op") != "replace_sentence" or not a.get("anchor"):
            continue
        key = (a["block_id"], tuple(a["anchor"]))
        cur = best.get(key)
        if cur is None or SEVERITY_ORDER.get(a["severity"], 1) < SEVERITY_ORDER.get(cur["severity"], 1):
            best[key] = a
    if not best:
        return anns
    keep_ids = {id(v) for v in best.values()}
    out = []
    for a in anns:
        if a.get("op") == "replace_sentence" and a.get("anchor") and id(a) not in keep_ids:
            continue
        out.append(a)
    return out


def tidy_punct(t: str) -> str:
    """修掉替换拼接后可能出现的「。。」「， 」这类瑕疵。"""
    t = _DUP_PUNCT.sub(r"\1", t)
    t = _SPACE_PUNCT.sub(r"\1", t)
    t = re.sub(r"([（【「『])[ \t]+", r"\1", t)
    return t


# ---------------------------------------------------------------- 视图 token
def apply_with_marks(segs: List[Dict[str, Any]], ops: List[Dict[str, Any]]):
    """从后往前应用修改，再在最终文本里定位每处改动，用于高亮。"""
    import copy
    new = copy.deepcopy(segs)
    ordered = sorted(ops, key=lambda a: (a["anchor"][0], a["anchor"][1]))
    for a in reversed(ordered):
        new = apply_edit(new, a["anchor"], a["replacement"])
    for s in new:
        if s["t"] == "text":
            s["v"] = tidy_punct(s["v"])

    marks = []
    cursor: Dict[int, int] = {}
    for a in ordered:
        si = a["anchor"][0]
        rep = tidy_punct(a["replacement"])
        if not rep or si >= len(new) or new[si]["t"] != "text":
            continue
        v = new[si]["v"]
        idx = v.find(rep, cursor.get(si, 0))
        if idx < 0:
            idx = v.find(rep)
        if idx < 0:
            continue
        marks.append((si, idx, len(rep), a["id"], a["type"]))
        cursor[si] = idx + len(rep)
    return new, marks


def _tokens_with_marks(segs, marks) -> List[Dict[str, Any]]:
    by_seg: Dict[int, List] = {}
    for (si, off, ln, aid, typ) in marks:
        if ln > 0:
            by_seg.setdefault(si, []).append((off, ln, aid, typ))
    toks: List[Dict[str, Any]] = []
    for si, s in enumerate(segs):
        if s["t"] == "img":
            toks.append({"k": "i", "img": s})
            continue
        v = s["v"]
        pos = 0
        for (off, ln, aid, typ) in sorted(by_seg.get(si, [])):
            if off > pos:
                toks.append({"k": "t", "v": v[pos:off]})
            end = min(off + ln, len(v))
            if end > off:
                toks.append({"k": "ins", "v": v[off:end], "op": aid, "type": typ})
            pos = max(pos, end)
        if pos < len(v):
            toks.append({"k": "t", "v": v[pos:]})
    return toks


def render_tokens(block: Dict[str, Any], anns: List[Dict[str, Any]],
                  mode: str) -> List[Dict[str, Any]]:
    """mode: original | annotated | revised

    调用方应传入「已经按 block_id 过滤好的」批注列表 ——
    _ser_blocks 会一次性分组，避免每段都全量扫一遍批注表。
    """
    if block["kind"] == "table" or mode == "original":
        return [{"k": "t", "v": s["v"]} if s["t"] == "text" else {"k": "i", "img": s}
                for s in block.get("segs", [])]

    ops = [a for a in anns if a["block_id"] == block["id"]]

    if mode == "revised":
        accepted = [a for a in ops
                    if a["status"] == "accepted"
                    and a["op"] in ("replace", "replace_sentence", "insert_after")
                    and a.get("anchor")]
        segs, marks = apply_with_marks(block.get("segs", []), accepted)
        return _tokens_with_marks(segs, marks)

    # annotated：原文 + 标记
    toks: List[Dict[str, Any]] = []
    by_seg: Dict[int, List[Dict[str, Any]]] = {}
    pending_markers: Dict[int, List[Dict[str, Any]]] = {}
    for a in ops:
        if not a.get("anchor"):
            pending_markers.setdefault(0, []).append(a)
            continue
        si, so, ei, eo = a["anchor"]
        by_seg.setdefault(si, []).append(a)

    segs = block.get("segs", [])
    for si, s in enumerate(segs):
        if s["t"] == "img":
            toks.append({"k": "i", "img": s})
            continue
        v = s["v"]
        items = sorted(by_seg.get(si, []), key=lambda a: a["anchor"][1])
        pos = 0
        for a in items:
            _, so, ei, eo = a["anchor"]
            end = eo if ei == si else len(v)
            if so > pos:
                toks.append({"k": "t", "v": v[pos:so]})
            if end > so:
                toks.append({"k": "mark", "v": v[so:end], "op": a["id"],
                             "type": a["type"], "sev": a["severity"]})
            else:
                toks.append({"k": "anchor", "op": a["id"], "type": a["type"],
                             "sev": a["severity"]})
            pos = max(pos, end)
        if pos < len(v):
            toks.append({"k": "t", "v": v[pos:]})
    for a in pending_markers.get(0, []):
        toks.insert(0, {"k": "anchor", "op": a["id"], "type": a["type"],
                        "sev": a["severity"]})
    return toks


# ---------------------------------------------------------------- 统计
def relocate(a: Dict[str, Any], block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条旧批注搬到「内容相同」的新段落上。

    增量审查复用的是「文本完全一致」的段落，但段落编号会变（前面插了段），
    因此 block_id 必须重映射；anchor 也不能直接沿用 —— 片段结构可能变了
    （比如图片挪了位置），所以按 target 原文重新定位一次。
    """
    nb = dict(a)
    nb["block_id"] = block["id"]
    if nb.get("op") == "comment":
        nb["anchor"] = None
        return nb
    target = (nb.get("target") or "").strip()
    if not target:
        return None
    loc = locate(block.get("segs", []), target)
    if loc is None:
        return None
    if nb.get("op") == "insert_after":
        nb["anchor"] = [loc[2], loc[3], loc[2], loc[3]]
    else:
        nb["anchor"] = list(loc)
    return nb


MAX_PER_BLOCK = 6


def cap_per_block(anns: List[Dict[str, Any]], limit: int = MAX_PER_BLOCK
                  ) -> List[Dict[str, Any]]:
    """同一段落最多保留 N 条批注（按严重度取前 N）。

    模型偶尔会对一段话挑出十几条鸡毛蒜皮，把真正重要的问题淹没掉。
    默认上限 6 条；`limit<=0` 表示不限制。
    """
    if limit <= 0:
        return anns
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for a in anns:
        buckets.setdefault(a.get("block_id") or "", []).append(a)
    out: List[Dict[str, Any]] = []
    for _bid, group in buckets.items():
        if len(group) <= limit:
            out.extend(group)
            continue
        # 结构建议/存疑待核这类「提示型」批注排在后面，优先保住实体改动
        group = sorted(group, key=lambda a: (SEVERITY_ORDER.get(a.get("severity"), 1),
                                             a.get("op") == "comment"))
        out.extend(group[:limit])
    return out


def finalize(anns: List[Dict[str, Any]],
             max_per_block: int = 0) -> List[Dict[str, Any]]:
    """收尾：消解冲突 → 限制每段条数 → 按严重度排序 → 重排 id。"""
    out = dedupe_conflicts(anns)
    if max_per_block:
        out = cap_per_block(out, max_per_block)
    out.sort(key=lambda a: (SEVERITY_ORDER.get(a.get("severity"), 1),
                            int(a["block_id"][1:]) if a["block_id"][1:].isdigit() else 0))
    for i, a in enumerate(out):
        a["id"] = f"a{i + 1}"
    return out


def stats(anns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统计批注。status 只认三个合法值，避免脏数据把 total 之类的键覆盖掉。"""
    d = {"total": len(anns), "pending": 0, "accepted": 0, "rejected": 0, "by_type": {}}
    for a in anns:
        st = a.get("status")
        if st in ("pending", "accepted", "rejected"):
            d[st] += 1
        typ = a.get("type") or "未分类"
        d["by_type"][typ] = d["by_type"].get(typ, 0) + 1
    return d


# ---------------------------------------------------------------- 图像问题 → 批注
def image_annotations(report: Dict[str, Any], start_seq: int = 0) -> List[Dict[str, Any]]:
    """把图像审查结果并入统一问题清单，可逐条追溯。

    - 有视觉模型：仅对「敏感 / 与正文不一致 / 需人工核对」的图片逐条生成批注（上限 40 条）；
    - 无视觉模型：只生成一条汇总批注，说明原因一次，不逐图复述（详情见「图像审查」页）。
    """
    items = (report or {}).get("items") or []
    if not items:
        return []

    if not report.get("vision"):
        first = items[0]
        reason = str(report.get("vision_reason") or "").strip()
        total = report.get("total", len(items))
        reason_txt = f"（{reason}）" if reason else ""
        return [{
            "id": f"a{start_seq + 1}",
            "block_id": first.get("block_id", ""),
            "op": "comment", "type": "存疑待核",
            "target": "（全文图片）", "replacement": "",
            "reason": (f"全文共 {total} 张图片本次未做内容识别{reason_txt}，图片内容需人工核对。"
                       f"位置已逐张标注，详见「图像审查」页。"),
            "severity": "medium", "status": "pending", "anchor": None, "sid": None,
        }]

    out: List[Dict[str, Any]] = []
    for it in items:
        flags = []
        if it.get("sensitive"):
            flags.append("含敏感信息")
        if it.get("inconsistent"):
            flags.append("与正文不一致")
        if it.get("needs_check"):
            flags.append("需人工核对")
        if not flags:
            continue
        notes = [str(it.get(k) or "").strip() for k in
                 ("sensitive_note", "inconsistent_note", "check_note")]
        reason = "；".join(n for n in notes if n) or "；".join(flags)
        out.append({
            "id": f"a{start_seq + len(out) + 1}",
            "block_id": it.get("block_id", ""),
            "op": "comment",
            "type": "存疑待核",
            "target": it.get("caption", ""),
            "replacement": "",
            "reason": f"[图{it.get('index')}·{'/'.join(flags)}] {reason}",
            "severity": "high" if it.get("sensitive") else "medium",
            "status": "pending",
            "anchor": None,
            "sid": None,
        })
        if len(out) >= 40:
            break
    return out
