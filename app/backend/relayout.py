"""重新排版引擎。

原则：只调整「结构呈现」（标题层级、编号、目录、段落整理、版式），
     绝不打乱 block 的先后顺序 —— block 是图片的载体，
     顺序不变即图片位置与语义结构完整保留。
"""
from __future__ import annotations

import copy
import re
from typing import Any, Dict, List

CN_NUM = "一二三四五六七八九十"

# 编号风格：中文顿号 / 中文括号 / 阿拉伯数字 / 第X章
NUMBER_STYLES = {
    "cn": ("{cn}、", "（{cn}）", "{n}.{m} "),
    "cn_paren": ("（{cn}）", "{n}.{m} ", "{n}.{m}.{k} "),
    "arabic": ("{n}. ", "{n}.{m} ", "{n}.{m}.{k} "),
    "chapter": ("第{cn}章 ", "第{cn}节 ", "{n}.{m} "),
}

MAX_PARA = 520          # 超过这个字数的纯文本段落会被按句切开


def _cn(n: int) -> str:
    if n <= 0:
        return str(n)
    if n <= 10:
        return CN_NUM[n - 1]
    if n < 20:
        return "十" + CN_NUM[n - 11]
    if n < 100:
        tens, ones = divmod(n, 10)
        return CN_NUM[tens - 1] + "十" + (CN_NUM[ones - 1] if ones else "")
    return str(n)


def _normalize_heading_levels(blocks: List[Dict[str, Any]]) -> None:
    """把跳级的层级压平成连续的 1/2/3。"""
    levels = sorted({b.get("level", 1) for b in blocks
                     if b["kind"] == "heading" and b.get("level")})
    if not levels:
        return
    mapping = {lv: min(i + 1, 3) for i, lv in enumerate(levels)}
    for b in blocks:
        if b["kind"] == "heading":
            b["level"] = mapping.get(b.get("level", 1), 1)


def _clean_text(t: str) -> str:
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def relayout(blocks_in: List[Dict[str, Any]], opts: Dict[str, Any],
             meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """返回 {"blocks":[...], "toc":[...], "stats":{...}}，blocks 已带 heading_no 字段。"""
    blocks = copy.deepcopy(blocks_in)
    meta = meta or {}
    stats = {"merged_lines": 0, "split_paras": 0, "dropped_empty": 0,
             "dedup_headings": 0, "promoted": 0, "renumbered": 0}

    # 1. 文本规整
    if opts.get("normalize_text", True):
        for b in blocks:
            if b["kind"] == "table":
                continue
            for s in b["segs"]:
                if s["t"] == "text":
                    s["v"] = _clean_text(s["v"])

    # 2. 段落合并：被硬换行切断的长句接回去
    if opts.get("merge_lines", True):
        stats["merged_lines"] = _merge_lines(blocks)

    # 3. 列表符号统一
    if opts.get("normalize_list", True):
        _normalize_list_markers(blocks)

    # 4. 去掉空块
    if opts.get("drop_empty", True):
        before = len(blocks)
        blocks = [b for b in blocks
                  if b["kind"] == "table"
                  or any(s["t"] == "img" or (s.get("v") or "").strip() for s in b["segs"])]
        stats["dropped_empty"] = before - len(blocks)

    # 5. 连续重复标题去重（"二、笔刷" 紧接着又一个 "笔刷"）
    if opts.get("dedup_headings", True):
        stats["dedup_headings"] = _dedup_headings(blocks)

    # 6. 标题层级
    _normalize_heading_levels(blocks)
    if opts.get("ensure_heading", True):
        stats["promoted"] = _ensure_headings(blocks)

    # 7. 超长段落按句切分
    if opts.get("split_long_paras", True):
        blocks, stats["split_paras"] = _split_long_paras(blocks)

    # 8. 编号
    if opts.get("number_headings", True):
        stats["renumbered"] = _number(blocks, opts.get("number_style") or "cn")

    # 9. 目录
    toc = []
    if opts.get("add_toc", True):
        for b in blocks:
            if b["kind"] == "heading":
                txt = "".join(s["v"] for s in b["segs"] if s["t"] == "text").strip()
                if not txt:
                    continue
                no = b.get("heading_no", "")
                toc.append({"id": b["id"], "level": b.get("level", 1),
                            "no": no, "text": txt, "full": no + txt})

    return {
        "blocks": blocks,
        "toc": toc,
        "stats": stats,
        "summary": meta.get("summary", ""),
        "key_points": meta.get("key_points", []),
        "outline": meta.get("outline", []),
        "gaps": meta.get("gaps", []),
    }


def _merge_lines(blocks: List[Dict[str, Any]]) -> int:
    n = 0
    for b in blocks:
        if b["kind"] != "para" or any(s["t"] == "img" for s in b["segs"]):
            continue
        texts = [s for s in b["segs"] if s["t"] == "text"]
        if len(texts) != 1:
            continue
        v = texts[0]["v"]
        lines = [x.strip() for x in v.split("\n") if x.strip()]
        if len(lines) <= 1:
            continue
        merged: List[str] = []
        for ln in lines:
            if merged and not re.search(r"[。！？；：…”』】\)]$", merged[-1]) \
                    and not re.match(r"^[一二三四五六七八九十\d]+[、.．）)]", ln) \
                    and not re.match(r"^[-*·•]\s", ln):
                merged[-1] += ln
                n += 1
            else:
                merged.append(ln)
        texts[0]["v"] = "\n".join(merged)
    return n


_LIST_MARK = re.compile(r"^\s*([-*+·•▪◦‣●○■□]|\d+[.)])\s+")


def _normalize_list_markers(blocks: List[Dict[str, Any]]) -> None:
    """把各种项目符号统一成「· 」，序号列表保持数字形式。"""
    for b in blocks:
        if b["kind"] != "list":
            continue
        for s in b["segs"]:
            if s["t"] != "text" or not s["v"].strip():
                continue
            m = _LIST_MARK.match(s["v"])
            if not m:
                break
            mark = m.group(1)
            if re.match(r"\d", mark):
                break
            s["v"] = "· " + s["v"][m.end():]
            break


def _block_text(b: Dict[str, Any]) -> str:
    return "".join(s["v"] for s in b["segs"] if s["t"] == "text").strip()


def _dedup_headings(blocks: List[Dict[str, Any]]) -> int:
    """相邻的重复标题只保留一个（去掉编号后比较）。"""
    out: List[Dict[str, Any]] = []
    n = 0
    prev_key = None
    for b in blocks:
        if b["kind"] == "heading":
            key = _LEAD_NO.sub("", _block_text(b)).strip()
            key = re.sub(r"\s+", "", key)
            if key and key == prev_key:
                n += 1
                continue
            prev_key = key
        elif _block_text(b):
            prev_key = None
        out.append(b)
    blocks[:] = out
    return n


def _ensure_headings(blocks: List[Dict[str, Any]]) -> int:
    """完全没有标题时，把开头若干个短行提成一级标题，保证有可导航结构。

    只提升普通段落：列表项本身就是「条目」，把「1. 第二条」提成标题
    会让它被重新编号成「一、第二条」，语义直接变味。
    """
    if any(b["kind"] == "heading" for b in blocks):
        return 0
    n = 0
    for b in blocks:
        if n >= 6:
            break
        if b["kind"] != "para":
            continue
        t = _block_text(b)
        if 2 <= len(t) <= 30 and not t.endswith(("。", "，", "；", "、", "：")) \
                and "\n" not in t:
            b["kind"] = "heading"
            b["level"] = 1
            n += 1
    return n


_SENT = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")


def _split_long_paras(blocks: List[Dict[str, Any]]):
    """把超长纯文本段落按句切开，避免一屏全是字。

    只处理「单段纯文字」：带图片的段落一律不拆 —— 图片是原子单元，
    拆段会让它失去原本的归属。
    """
    out: List[Dict[str, Any]] = []
    split = 0
    for b in blocks:
        if b["kind"] != "para" or any(s["t"] == "img" for s in b["segs"]):
            out.append(b)
            continue
        texts = [s for s in b["segs"] if s["t"] == "text"]
        if len(texts) != 1 or len(texts[0]["v"]) <= MAX_PARA:
            out.append(b)
            continue
        sents = [x for x in _SENT.findall(texts[0]["v"]) if x.strip()]
        if len(sents) < 2:
            out.append(b)
            continue
        buf: List[str] = []
        size = 0
        pieces: List[str] = []
        for s in sents:
            buf.append(s)
            size += len(s)
            if size >= MAX_PARA * 0.7:
                pieces.append("".join(buf).strip())
                buf, size = [], 0
        if buf:
            pieces.append("".join(buf).strip())
        for i, piece in enumerate(pieces):
            nb = copy.deepcopy(b)
            nb["segs"] = [{"t": "text", "v": piece}]
            if i:
                nb["id"] = f"{b['id']}s{i}"
            out.append(nb)
        split += max(0, len(pieces) - 1)
    blocks[:] = out
    return blocks, split


def _number(blocks: List[Dict[str, Any]], style: str = "cn") -> int:
    fmt = NUMBER_STYLES.get(style) or NUMBER_STYLES["cn"]
    c1 = c2 = c3 = 0
    n = 0
    for b in blocks:
        if b["kind"] != "heading":
            continue
        _strip_leading_no(b)
        lv = b.get("level", 1)
        if lv == 1:
            c1 += 1
            c2 = c3 = 0
            b["heading_no"] = fmt[0].format(cn=_cn(c1), n=c1)
        elif lv == 2:
            c2 += 1
            c3 = 0
            b["heading_no"] = fmt[1].format(cn=_cn(c2), n=c1, m=c2)
        else:
            c3 += 1
            b["heading_no"] = fmt[2].format(cn=_cn(c3), n=c1, m=c2, k=c3)
        n += 1
    return n


_LEAD_NO = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+[章节部分篇]"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|（[一二三四五六七八九十]+）"
    r"|\([一二三四五六七八九十]+\)"
    r"|[（(]\s*\d+\s*[)）]"
    r"|[①-⑳]"
    r"|\d+(?:\.\d+)*[、.．]?\s+"
    r"|\d+(?:\.\d+)+[、.．]?"
    r")\s*")


def _strip_leading_no(b: Dict[str, Any]) -> None:
    """标题里原有的「第三章 / 3.1 / 一、/ ①」先剥掉，避免和新编号重复。"""
    for s in b["segs"]:
        if s["t"] == "text" and s["v"].strip():
            stripped = _LEAD_NO.sub("", s["v"], count=1)
            if stripped.strip():
                s["v"] = stripped
            break


def materialize(relaid: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 heading_no 真正写进文字，用于导出。"""
    out = []
    for b in relaid["blocks"]:
        b2 = copy.deepcopy(b)
        no = b2.pop("heading_no", "")
        if no and b2["kind"] == "heading":
            for s in b2["segs"]:
                if s["t"] == "text":
                    s["v"] = no + s["v"].lstrip()
                    break
        out.append(b2)
    return out


def prepend_summary(relaid: Dict[str, Any]) -> List[Dict[str, Any]]:
    """在重排版顶部插入「内容提要」，作为归纳成果。"""
    blocks = relaid["blocks"]
    summary = relaid.get("summary") or ""
    points = relaid.get("key_points") or []
    if not summary and not points:
        return blocks
    from .docmodel import new_block
    head = []
    if summary:
        head.append(new_block("heading", level=1, segs=[{"t": "text", "v": "内容提要"}],
                              heading_no=""))
        head.append(new_block("para", segs=[{"t": "text", "v": summary}]))
    if points:
        head.append(new_block("heading", level=2,
                              segs=[{"t": "text", "v": "要点"}], heading_no=""))
        for p in points:
            head.append(new_block("list", segs=[{"t": "text", "v": "· " + p}]))
    return head + blocks
