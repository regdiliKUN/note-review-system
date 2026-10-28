"""性能基准：把「优化前后差多少」量化出来，顺便当性能回归门禁。

    python tests/bench.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

from backend import docmodel, review  # noqa: E402


def build_doc(n_blocks=1000, n_anns=500):
    blocks = []
    for i in range(n_blocks):
        kind = "heading" if i % 12 == 0 else "para"
        text = "".join(f"第{j}句讲的是某个知识点。" for j in range(4))
        blocks.append({"id": f"b{i}", "kind": kind, "level": 1 if kind == "heading" else 0,
                       "segs": [{"t": "text", "v": f"段落{i}：" + text}], "style": {}})
    anns = []
    for i in range(n_anns):
        bi = (i * 2) % n_blocks
        anns.append({
            "id": f"a{i}", "block_id": f"b{bi}", "op": "replace_sentence",
            "type": "表达优化", "target": "第0句讲的是某个知识点。",
            "replacement": "第0句说明了一个知识点。", "reason": "更书面",
            "severity": "medium", "status": "accepted" if i % 3 == 0 else "pending",
            "anchor": [0, 5, 0, 20], "sid": 0,
        })
    return blocks, anns


def old_apply_accepted(blocks, anns):
    """优化前的实现（每条批注线性扫一遍 blocks），用于对比。"""
    import copy
    out = copy.deepcopy(blocks)
    by_block = {}
    for a in anns:
        if a["status"] == "accepted" and a.get("anchor"):
            by_block.setdefault(a["block_id"], []).append(a)
    for bid, ops in by_block.items():
        b = next((x for x in out if x["id"] == bid), None)
        if not b:
            continue
        ops.sort(key=lambda a: (a["anchor"][0], a["anchor"][1]), reverse=True)
        for a in ops:
            b["segs"] = review.apply_edit(b["segs"], a["anchor"], a["replacement"])
    return out


def old_ser_blocks(blocks, anns, mode):
    """优化前的实现（每段都全量扫一遍批注表）。"""
    out = []
    for b in blocks:
        if b["kind"] == "table":
            continue
        out.append({"id": b["id"], "tokens": review.render_tokens(b, anns, mode)})
    return out


def timeit(fn, repeat=3):
    best = 1e9
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def main():
    blocks, anns = build_doc()
    print(f"规模：{len(blocks)} 段 / {len(anns)} 条批注\n")
    rows = []

    def new_apply():
        review.apply_accepted(blocks, anns)

    def new_views():
        grouped = {}
        for a in anns:
            grouped.setdefault(a["block_id"], []).append(a)
        for mode in ("original", "annotated", "revised"):
            for b in blocks:
                review.render_tokens(b, grouped.get(b["id"], []), mode)

    def old_views():
        for mode in ("original", "annotated", "revised"):
            old_ser_blocks(blocks, anns, mode)

    t_old_apply = timeit(lambda: old_apply_accepted(blocks, anns))
    t_new_apply = timeit(new_apply)
    rows.append(("应用已采纳修改", t_old_apply, t_new_apply))

    t_old_views = timeit(old_views, repeat=2)
    t_new_views = timeit(new_views, repeat=2)
    rows.append(("渲染三份视图", t_old_views, t_new_views))

    t_count = timeit(lambda: docmodel.count_images(blocks))
    rows.append(("统计图片数（单次）", t_count, 0.0))

    print(f"{'项目':<18}{'优化前':>12}{'优化后':>12}{'提速':>10}")
    print("-" * 54)
    for name, old, new in rows:
        if new <= 0:
            print(f"{name:<18}{'—':>12}{old:>10.2f}ms{'—':>10}")
            continue
        speed = old / new if new else 0
        print(f"{name:<18}{old:>10.2f}ms{new:>10.2f}ms{speed:>9.1f}×")

    print("\n门禁（超过即视为性能回归）：")
    ok = True
    for name, limit, fn in (
        ("应用已采纳修改 < 120ms", 120, new_apply),
        ("渲染三份视图 < 400ms", 400, new_views),
    ):
        t = timeit(fn, repeat=2)
        good = t < limit
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {name}（实测 {t:.1f}ms）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
