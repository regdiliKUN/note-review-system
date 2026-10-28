"""后端链路自测：解析 -> 审查(可选) -> 视图 -> 导出，重点校验图片不丢不错位。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "app"))

from backend import docmodel, relayout as rl, review  # noqa

SAMPLE = os.path.join(ROOT, "app", "build", "示例笔记.docx")
ASSETS = os.path.join(ROOT, "data", "assets")

store = docmodel.ImageStore(ASSETS, "testdoc")
doc = docmodel.parse_docx(SAMPLE, store)
blocks = doc["blocks"]

print("标题:", doc["title"])
print("段落数:", len(blocks))
print("图片数:", docmodel.count_images(blocks))
for b in blocks:
    tag = f"{b['id']:>4} {b['kind']:<8} L{b.get('level',0)}"
    txt = docmodel.block_plain(b)[:56].replace("\n", " ")
    print(f"  {tag} | {txt}")
print()

# 图片顺序快照
imgs = docmodel.all_images(blocks)
print("图片顺序:", [i["file"][:14] for i in imgs])

# 模拟几条批注
anns = []
raws = [
    {"block_id": "b4", "op": "replace", "type": "事实纠错",
     "target": "学习率是最重要的超参数", "replacement": "学习率是影响训练最重要的超参数之一",
     "reason": "原文表述过于绝对", "severity": "medium"},
    {"block_id": "b6", "op": "replace", "type": "事实纠错",
     "target": "2014 年由 Kingma 等人提出的",
     "replacement": "2014 年由 Kingma 和 Ba 在论文中提出",
     "reason": "补充合作者，表述更准确", "severity": "low"},
    {"block_id": "b4", "op": "insert_after", "type": "知识补充",
     "target": "收敛会很慢", "replacement": "。实际中常配合学习率调度（如余弦退火）使用。",
     "reason": "补充常用实践", "severity": "medium"},
]
by_id = {b["id"]: b for b in blocks}
for i, r in enumerate(raws, 1):
    a = review.build_annotation(r, by_id, i)
    assert a, f"批注 {i} 构建失败"
    a["status"] = "accepted" if i != 2 else "rejected"
    anns.append(a)

print("\n批注:", [(a["id"], a["op"], a["status"], a["anchor"]) for a in anns])

cur = review.apply_accepted(blocks, anns)
print("\n--- 修正后 ---")
for b in cur:
    if b["id"] in ("b4", "b6"):
        print(" ", b["id"], repr(docmodel.block_plain(b)))

# 关键校验：图片数量与顺序不变
assert docmodel.count_images(cur) == docmodel.count_images(blocks), "图片数量变了！"
assert [i["file"] for i in docmodel.all_images(cur)] == [i["file"] for i in imgs], "图片顺序变了！"
print("\n[OK] 图片数量与顺序完全保持")

# 视图 token
for mode in ("original", "annotated", "revised"):
    toks = review.render_tokens(blocks[4], anns, mode)
    print(f"  {mode:9}", [(t["k"], (t.get("v") or "")[:16]) for t in toks][:6])

# 重排
relaid = rl.relayout(cur, {"add_toc": True, "number_headings": True,
                           "merge_lines": True, "normalize_text": True,
                           "add_summary": True, "ensure_heading": True},
                     {"summary": "本章讲训练流程。", "key_points": ["前向传播", "反向传播"]})
print("\n目录:", [t["text"] for t in relaid["toc"]])
assert docmodel.count_images(relaid["blocks"]) == docmodel.count_images(blocks), "重排后图片数变了！"
assert [i["file"] for i in docmodel.all_images(relaid["blocks"])] == [i["file"] for i in imgs], "重排后图片顺序变了！"
print("[OK] 重排后图片数量与顺序完全保持")

# 导出
out = os.path.join(ROOT, "app", "build")
mat = rl.materialize(relaid)
final = rl.prepend_summary({"blocks": mat, "summary": relaid["summary"],
                            "key_points": relaid["key_points"]})
docmodel.export_docx(final, store.dir, os.path.join(out, "导出测试.docx"), doc["title"])
docmodel.export_html(final, store.dir, os.path.join(out, "导出测试.html"), doc["title"])

# 校验导出的 docx 图片数
import zipfile
with zipfile.ZipFile(os.path.join(out, "导出测试.docx")) as z:
    media = [n for n in z.namelist() if n.startswith("word/media/")]
print("[OK] 导出 docx 内嵌图片:", len(media), "张")
assert len(media) == docmodel.count_images(blocks), "导出后图片数量不符！"
print("\n全部校验通过")
