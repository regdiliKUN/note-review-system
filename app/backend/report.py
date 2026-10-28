"""审阅报告：把一次审查的结果导出成可以交给别人看的独立文件。

为什么单独一个模块：报告是「对外交付物」，和正文导出（docx/md）目标不同 ——
它需要统计、分组、逐条批注、图像问题汇总，而且必须自带样式、能直接打印。
"""
from __future__ import annotations

import csv
import io
import os
import time
from html import escape
from typing import Any, Dict, List

TYPE_ORDER = ["错别字", "事实纠错", "知识补充", "表达优化", "逻辑归纳", "结构建议", "存疑待核"]
TYPE_COLOR = {
    "错别字": "#ef4444", "事实纠错": "#f97316", "知识补充": "#0ea5e9",
    "表达优化": "#8b5cf6", "逻辑归纳": "#10b981", "结构建议": "#f59e0b",
    "存疑待核": "#94a3b8",
}
SEV_LABEL = {"high": "高", "medium": "中", "low": "低"}
STATUS_LABEL = {"pending": "待处理", "accepted": "已保留", "rejected": "已丢弃"}
OP_LABEL = {"replace_sentence": "整句替换", "replace": "替换",
            "insert_after": "句后补充", "comment": "仅提醒"}


def collect(doc: Dict[str, Any], anns: List[Dict[str, Any]],
            summary: Dict[str, Any], image_report: Dict[str, Any],
            stats: Dict[str, Any], meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """整理成报告数据结构（不含渲染）。"""
    meta = meta or {}
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for a in anns:
        by_type.setdefault(a.get("type") or "未分类", []).append(a)
    order = [t for t in TYPE_ORDER if t in by_type]
    order += [t for t in by_type if t not in order]

    sev_count = {"high": 0, "medium": 0, "low": 0}
    for a in anns:
        s = a.get("severity") or "medium"
        sev_count[s] = sev_count.get(s, 0) + 1

    ir = image_report or {}
    img_items = ir.get("items") or []
    img_flagged = [it for it in img_items
                   if it.get("sensitive") or it.get("inconsistent") or it.get("needs_check")]

    return {
        "title": doc.get("title") or "未命名笔记",
        "file": os.path.basename(doc.get("path") or ""),
        "path": doc.get("path") or "",
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": meta.get("model") or "",
        "vision_model": meta.get("vision") or "",
        "elapsed": meta.get("elapsed") or 0,
        "blocks": len(doc.get("blocks") or []),
        "images": (ir.get("total") or 0),
        "stats": stats or {},
        "sev": sev_count,
        "groups": [{"type": t, "color": TYPE_COLOR.get(t, "#8b5cf6"),
                    "items": by_type[t]} for t in order],
        "summary": (summary or {}).get("summary", ""),
        "key_points": (summary or {}).get("key_points") or [],
        "outline": (summary or {}).get("outline") or [],
        "gaps": (summary or {}).get("gaps") or [],
        "image_total": ir.get("total") or 0,
        "image_analyzed": ir.get("analyzed") or 0,
        "image_flagged": img_flagged[:60],
        "image_note": ir.get("note") or "",
        "map_reduce": bool((summary or {}).get("map_reduce")),
    }


# ---------------------------------------------------------------- HTML
_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:36px 20px 60px;background:#f4f6fa;color:#1b1f27;
 font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",system-ui,sans-serif;
 line-height:1.75;font-size:14px;}
.wrap{max-width:900px;margin:0 auto}
.card{background:#fff;border-radius:12px;padding:26px 30px;margin-bottom:18px;
 box-shadow:0 2px 12px rgba(20,30,60,.07)}
h1{font-size:23px;margin:0 0 6px;letter-spacing:.3px}
h2{font-size:16px;margin:0 0 14px;padding-bottom:9px;border-bottom:2px solid #eef1f6}
h3{font-size:14px;margin:20px 0 10px}
.sub{color:#6b7488;font-size:12.5px;line-height:1.9}
.sub b{color:#3b4351;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;margin:18px 0 4px}
.kpi{background:#f7f9fc;border:1px solid #e8edf5;border-radius:10px;padding:12px 14px;text-align:center}
.kpi .n{font-size:22px;font-weight:700;line-height:1.2}
.kpi .l{font-size:11.5px;color:#6b7488;margin-top:2px}
.bar{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:12.5px}
.bar .nm{width:78px;color:#3b4351;flex:none}
.bar .track{flex:1;height:8px;background:#eef1f6;border-radius:5px;overflow:hidden}
.bar .fill{height:100%;border-radius:5px}
.bar .ct{width:44px;text-align:right;color:#6b7488;flex:none}
.ann{border-left:3px solid #ccc;background:#fbfcfe;border-radius:0 8px 8px 0;
 padding:12px 15px;margin:10px 0;page-break-inside:avoid;break-inside:avoid}
.ann .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:7px}
.tag{font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px}
.meta{font-size:11px;color:#8b95a8}
.diff{font-size:13.5px;line-height:1.85;word-break:break-word}
.old{background:#fee2e2;color:#b91c1c;text-decoration:line-through;padding:0 2px;border-radius:3px}
.new{background:#dcfce7;color:#15803d;padding:0 2px;border-radius:3px}
.plus{background:#e0f2fe;color:#0369a1;padding:0 2px;border-radius:3px}
.reason{font-size:12.5px;color:#5b6472;margin-top:7px;padding-top:7px;border-top:1px dashed #e3e8f0}
.reason b{color:#3b4351}
ul{padding-left:20px;margin:8px 0}
li{margin:5px 0}
.muted{color:#8b95a8;font-size:12.5px}
.imgrow{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-bottom:1px dashed #eef1f6;font-size:12.5px}
.imgrow:last-child{border-bottom:none}
.pill{font-size:11px;padding:1px 7px;border-radius:20px;background:#fef3c7;color:#92400e;flex:none}
.empty{color:#8b95a8;font-size:13px;padding:14px 0;text-align:center}
.foot{text-align:center;color:#98a2b3;font-size:11.5px;margin-top:22px}
@media print{
 body{background:#fff;padding:0;font-size:12pt}
 .card{box-shadow:none;border:1px solid #e5e7eb;margin-bottom:12px;padding:18px 20px}
 .ann{background:#fff}
 @page{margin:16mm}
}
"""


def to_html(rep: Dict[str, Any]) -> str:
    st = rep.get("stats") or {}
    total = st.get("total", 0) or 0
    sev = rep.get("sev") or {}
    maxct = max([len(g["items"]) for g in rep["groups"]] or [1]) or 1

    kpis = "".join(
        f'<div class="kpi"><div class="n" style="color:{c}">{v}</div>'
        f'<div class="l">{escape(lbl)}</div></div>'
        for lbl, v, c in (
            ("批注总数", total, "#1b1f27"),
            ("待处理", st.get("pending", 0), "#f59e0b"),
            ("已保留", st.get("accepted", 0), "#16a34a"),
            ("已丢弃", st.get("rejected", 0), "#ef4444"),
            ("高优先级", sev.get("high", 0), "#ef4444"),
        ))

    bars = "".join(
        f'<div class="bar"><span class="nm">{escape(g["type"])}</span>'
        f'<span class="track"><i class="fill" style="width:'
        f'{round(len(g["items"]) / maxct * 100)}%;background:{g["color"]}"></i></span>'
        f'<span class="ct">{len(g["items"])}</span></div>'
        for g in rep["groups"]) or '<div class="empty">本次没有产生批注</div>'

    def ann_html(a: Dict[str, Any]) -> str:
        color = TYPE_COLOR.get(a.get("type") or "", "#8b5cf6")
        op = a.get("op") or ""
        if op == "comment":
            body = escape((a.get("target") or "")[:300])
        elif op == "insert_after":
            body = f'<span class="plus">＋ {escape(a.get("replacement") or "")}</span>'
        else:
            body = (f'<span class="old">{escape(a.get("target") or "")}</span>'
                    f' → <span class="new">{escape(a.get("replacement") or "")}</span>')
        return (
            f'<div class="ann" style="border-left-color:{color}">'
            f'<div class="top">'
            f'<span class="tag" style="background:{color}1f;color:{color}">'
            f'{escape(a.get("type") or "")}</span>'
            f'<span class="meta">{escape(OP_LABEL.get(op, op))} · '
            f'严重度 {SEV_LABEL.get(a.get("severity"), "中")} · '
            f'{escape(STATUS_LABEL.get(a.get("status"), "待处理"))}</span>'
            f'</div><div class="diff">{body}</div>'
            + (f'<div class="reason"><b>理由：</b>{escape(a.get("reason") or "")}</div>'
               if a.get("reason") else "")
            + "</div>")

    groups = ""
    for g in rep["groups"]:
        groups += (f'<h3>{escape(g["type"])}'
                   f'<span class="muted">（{len(g["items"])} 条）</span></h3>'
                   + "".join(ann_html(a) for a in g["items"]))
    if not groups:
        groups = '<div class="empty">这篇笔记没有发现需要修改的地方。</div>'

    summary_block = ""
    if rep.get("summary") or rep.get("key_points"):
        summary_block = '<div class="card"><h2>内容归纳</h2>'
        if rep.get("summary"):
            summary_block += f'<p>{escape(rep["summary"])}</p>'
        if rep.get("key_points"):
            summary_block += "<h3>要点</h3><ul>" + "".join(
                f"<li>{escape(p)}</li>" for p in rep["key_points"]) + "</ul>"
        if rep.get("outline"):
            summary_block += "<h3>建议结构</h3><ul>" + "".join(
                f"<li>{escape(p)}</li>" for p in rep["outline"]) + "</ul>"
        if rep.get("gaps"):
            summary_block += "<h3>建议补充</h3><ul>" + "".join(
                f"<li>{escape(p)}</li>" for p in rep["gaps"]) + "</ul>"
        summary_block += "</div>"

    img_block = ""
    if rep.get("image_total"):
        rows = "".join(
            f'<div class="imgrow"><span class="pill">图{it.get("index")}</span>'
            f'<div><b>{escape(it.get("caption") or "（无说明）")}</b><br>'
            f'<span class="muted">{escape(it.get("position") or "")}</span><br>'
            + escape("；".join(x for x in (
                it.get("sensitive_note"), it.get("inconsistent_note"),
                it.get("check_note")) if x) or "需人工核对")
            + "</div></div>"
            for it in rep["image_flagged"])
        img_block = (
            '<div class="card"><h2>图像审查</h2>'
            f'<p class="sub">全文 {rep["image_total"]} 张图片，'
            f'已自动分析 {rep["image_analyzed"]} 张，'
            f'其中 {len(rep["image_flagged"])} 张需关注。</p>'
            + (f'<p class="muted">{escape(rep["image_note"])}</p>' if rep.get("image_note") else "")
            + (rows or '<div class="empty">未发现图像问题</div>')
            + "</div>")

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>审阅报告 · {escape(rep['title'])}</title><style>{_CSS}</style></head><body>
<div class="wrap">
  <div class="card">
    <h1>审阅报告 · {escape(rep['title'])}</h1>
    <div class="sub">
      源文件：<b>{escape(rep['file'])}</b>　·　段落 <b>{rep['blocks']}</b>　·　
      图片 <b>{rep['images']}</b><br>
      生成时间：{escape(rep['generated'])}
      {('　·　模型 <b>' + escape(rep['model']) + '</b>') if rep.get('model') else ''}
      {('　·　用时 <b>' + str(rep['elapsed']) + 's</b>') if rep.get('elapsed') else ''}
    </div>
    <div class="kpis">{kpis}</div>
  </div>

  <div class="card"><h2>问题分布</h2>{bars}
    <p class="muted" style="margin-top:14px">
      高 {sev.get('high', 0)} 条 · 中 {sev.get('medium', 0)} 条 · 低 {sev.get('low', 0)} 条
    </p>
  </div>

  {summary_block}
  <div class="card"><h2>逐条批注</h2>{groups}</div>
  {img_block}
  <div class="foot">由「笔记审查总结系统」生成 · 建议逐条核对后再决定是否采纳</div>
</div></body></html>"""


# ---------------------------------------------------------------- Markdown
def to_markdown(rep: Dict[str, Any]) -> str:
    st = rep.get("stats") or {}
    sev = rep.get("sev") or {}
    L = [f"# 审阅报告 · {rep['title']}", ""]
    L.append(f"- 源文件：`{rep['file']}`")
    L.append(f"- 段落 {rep['blocks']} · 图片 {rep['images']}")
    L.append(f"- 生成时间：{rep['generated']}")
    if rep.get("model"):
        L.append(f"- 模型：{rep['model']}")
    L.append("")
    L.append("## 总览")
    L.append("")
    L.append("| 指标 | 数量 |")
    L.append("| --- | ---: |")
    L.append(f"| 批注总数 | {st.get('total', 0)} |")
    L.append(f"| 待处理 | {st.get('pending', 0)} |")
    L.append(f"| 已保留 | {st.get('accepted', 0)} |")
    L.append(f"| 已丢弃 | {st.get('rejected', 0)} |")
    L.append(f"| 高 / 中 / 低 | {sev.get('high', 0)} / {sev.get('medium', 0)} / {sev.get('low', 0)} |")
    L.append("")
    if rep.get("summary"):
        L += ["## 内容归纳", "", rep["summary"], ""]
    for key, label in (("key_points", "要点"), ("outline", "建议结构"), ("gaps", "建议补充")):
        if rep.get(key):
            L.append(f"### {label}")
            L.append("")
            L += [f"- {x}" for x in rep[key]]
            L.append("")
    L += ["## 逐条批注", ""]
    if not rep["groups"]:
        L += ["_这篇笔记没有发现需要修改的地方。_", ""]
    for g in rep["groups"]:
        L.append(f"### {g['type']}（{len(g['items'])} 条）")
        L.append("")
        for i, a in enumerate(g["items"], 1):
            status = STATUS_LABEL.get(a.get("status"), "待处理")
            sevl = SEV_LABEL.get(a.get("severity"), "中")
            L.append(f"{i}. **[{sevl}] {status}** "
                     f"（{OP_LABEL.get(a.get('op'), a.get('op'))}）")
            if a.get("op") == "comment":
                L.append(f"   - 原文：{a.get('target') or '—'}")
            elif a.get("op") == "insert_after":
                L.append(f"   - 补充：{a.get('replacement')}")
            else:
                L.append(f"   - 原文：{a.get('target')}")
                L.append(f"   - 改为：{a.get('replacement')}")
            if a.get("reason"):
                L.append(f"   - 理由：{a.get('reason')}")
        L.append("")
    if rep.get("image_total"):
        L += ["## 图像审查", "",
              f"全文 {rep['image_total']} 张图片，已分析 {rep['image_analyzed']} 张，"
              f"{len(rep['image_flagged'])} 张需关注。", ""]
        for it in rep["image_flagged"]:
            note = "；".join(x for x in (it.get("sensitive_note"), it.get("inconsistent_note"),
                                         it.get("check_note")) if x) or "需人工核对"
            L.append(f"- 图{it.get('index')}（{it.get('position') or ''}）："
                     f"{it.get('caption') or '无说明'} —— {note}")
        L.append("")
    L.append("---")
    L.append("_由「笔记审查总结系统」生成_")
    return "\n".join(L)


# ---------------------------------------------------------------- CSV
def to_csv(anns: List[Dict[str, Any]]) -> str:
    """批注清单 CSV。用 utf-8-sig，Excel 双击打开不乱码。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["序号", "类型", "严重度", "状态", "操作", "原文", "建议改为", "理由"])
    for i, a in enumerate(anns, 1):
        w.writerow([
            i,
            a.get("type") or "",
            SEV_LABEL.get(a.get("severity"), "中"),
            STATUS_LABEL.get(a.get("status"), "待处理"),
            OP_LABEL.get(a.get("op"), a.get("op") or ""),
            (a.get("target") or "").replace("\n", " "),
            (a.get("replacement") or "").replace("\n", " "),
            (a.get("reason") or "").replace("\n", " "),
        ])
    return "\ufeff" + buf.getvalue()


def write_csv(anns: List[Dict[str, Any]], out_path: str) -> str:
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(to_csv(anns))
    return out_path
