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
SUBTYPE_NUM = "一二三四五六七八九十"


def _cn(n: int) -> str:
    if n <= 10:
        return CN_NUM[n - 1]
    if n < 20:
        return "十" + CN_NUM[n - 11]
    return str(n)


def _normalize_heading_levels(blocks: List[Dict[str, Any]]) -> None:
    """把跳级的层级压平成连续的 1/2/3。"""
    levels = sorted({b.get("level", 1) for b in blocks
                     if b["kind"] == "heading" and b.get("level")})
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
    """返回 {"blocks":[...], "toc":[...]}，blocks 已带 heading_no 字段。"""
    blocks = copy.deepcopy(blocks_in)
    meta = meta or {}

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
        for b in blocks:
            if b["kind"] != "para" or any(s["t"] == "img" for s in b["segs"]):
                continue
            texts = [s for s in b["segs"] if s["t"] == "text"]
            if len(texts) != 1:
                continue
            v = texts[0]["v"]
            lines = [x.strip() for x in v.split("\n") if x.strip()]
            if len(lines) > 1:
                merged = []
                for ln in lines:
                    if merged and not re.search(r"[。！？；：…”』】\)]$", merged[-1]) \
                            and not re.match(r"^[一二三四五六七八九十\d]+[、.．）)]", ln):
                        merged[-1] += ln
                    else:
                        merged.append(ln)
                texts[0]["v"] = "\n".join(merged)

    # 3. 标题层级
    _normalize_heading_levels(blocks)
    if opts.get("ensure_heading", True):
        _ensure_headings(blocks)

    # 4. 编号
    if opts.get("number_headings", True):
        _number(blocks)

    # 5. 目录
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
        "summary": meta.get("summary", ""),
        "key_points": meta.get("key_points", []),
        "gaps": meta.get("gaps", []),
    }


def _ensure_headings(blocks: List[Dict[str, Any]]) -> None:
    """完全没有标题时，把开头的短行提成一级标题，保证有可导航结构。"""
    if any(b["kind"] == "heading" for b in blocks):
        return
    for b in blocks:
        if b["kind"] in ("para", "list"):
            t = "".join(s["v"] for s in b["segs"] if s["t"] == "text").strip()
            if 2 <= len(t) <= 30 and not t.endswith(("。", "，", "；")):
                b["kind"] = "heading"
                b["level"] = 1
                break


def _number(blocks: List[Dict[str, Any]]) -> None:
    c1 = c2 = c3 = 0
    for b in blocks:
        if b["kind"] != "heading":
            continue
        _strip_leading_no(b)
        lv = b.get("level", 1)
        if lv == 1:
            c1 += 1
            c2 = c3 = 0
            b["heading_no"] = f"{_cn(c1)}、"
        elif lv == 2:
            c2 += 1
            c3 = 0
            b["heading_no"] = f"（{_cn(c2)}）"
        else:
            c3 += 1
            b["heading_no"] = f"{c2}.{c3} " if c2 else f"{c3}. "


_LEAD_NO = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百]+[章节部分篇]"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|（[一二三四五六七八九十]+）"
    r"|\([一二三四五六七八九十]+\)"
    r"|\d+(?:\.\d+)*[、.．]?\s+"
    r"|\d+(?:\.\d+)+[、.．]?"
    r")\s*")


def _strip_leading_no(b: Dict[str, Any]) -> None:
    """标题里原有的「第三章 / 3.1 / 一、」先剥掉，避免和新编号重复。"""
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
