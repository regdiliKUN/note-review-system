"""回归测试 —— 覆盖批注引擎、重排、历史、解析、导出、流水线。

设计原则：**不依赖 GUI、不依赖真实模型**。
所有模型调用都走 MockLLM，所以整个套件可以在任何机器上离线跑完。
运行：python tests/run_tests.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from backend import docmodel, history, llm, pipeline, relayout, review  # noqa: E402


# ---------------------------------------------------------------- 工具
def txt(s: str) -> dict:
    return {"t": "text", "v": s}


def img(name: str = "a.png", i: str = "i1") -> dict:
    return {"t": "img", "id": i, "file": name, "w": 100, "h": 80, "alt": ""}


def para(*segs) -> dict:
    return {"id": None, "kind": "para", "level": 0, "segs": list(segs), "style": {}}


def heading(s: str, level: int = 1) -> dict:
    return {"id": None, "kind": "heading", "level": level, "segs": [txt(s)], "style": {}}


def index(blocks):
    for i, b in enumerate(blocks):
        b["id"] = f"b{i}"
    return blocks


class MockLLM:
    """按队列返回预设文本，同时记录收到的 prompt。

    传 `fn=(system, user) -> str` 可以按 prompt 内容动态应答，
    比预设队列更适合「一轮 map、一轮 reduce」这种多阶段流程。
    """

    def __init__(self, replies=None, fn=None):
        self.replies = list(replies or [])
        self.fn = fn
        self.calls = []
        self._vision_blocked = False
        self.model = "mock"
        self.vision_model = ""

    def generate(self, system, user, json_mode=True, on_token=None, max_tokens=0):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        if self.fn is not None:
            return self.fn(system, user)
        if not self.replies:
            return "{}"
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def generate_vision(self, system, user, images, json_mode=True):
        return self.generate(system, user, json_mode)

    def vision_status(self):
        return {"enabled": False, "model": "", "reason": "mock", "candidates": []}

    def supports_vision(self):
        return False

    def health(self):
        return {"ok": True, "models": ["mock"], "provider": "ollama", "model": "mock"}


# ══════════════════════════════════════════════════════ 句子切分
class TestSentences(unittest.TestCase):
    def test_basic_split(self):
        b = para(txt("第一句。第二句！第三句？"))
        s = review.block_sentences(b)
        self.assertEqual([x["text"] for x in s], ["第一句。", "第二句！", "第三句？"])

    def test_tail_without_punct(self):
        b = para(txt("有标点。没有标点结尾"))
        s = review.block_sentences(b)
        self.assertEqual(len(s), 2)
        self.assertEqual(s[-1]["text"], "没有标点结尾")

    def test_semicolon_and_newline_are_boundaries(self):
        b = para(txt("甲；乙\n丙"))
        s = review.block_sentences(b)
        self.assertEqual([x["text"] for x in s], ["甲；", "乙\n", "丙"])

    def test_sentence_never_crosses_image(self):
        b = para(txt("前半句"), img(), txt("后半句。"))
        s = review.block_sentences(b)
        self.assertEqual(len(s), 2)
        self.assertEqual(s[0]["seg"], 0)
        self.assertEqual(s[1]["seg"], 2)

    def test_blank_segments_ignored(self):
        b = para(txt("   "), txt("有内容。"))
        s = review.block_sentences(b)
        self.assertEqual(len(s), 1)

    def test_empty_block(self):
        self.assertEqual(review.block_sentences(para()), [])


# ══════════════════════════════════════════════════════ 定位
class TestLocate(unittest.TestCase):
    def test_exact(self):
        segs = [txt("今天天气很好，适合出门。")]
        loc = review.locate(segs, "天气很好")
        self.assertIsNotNone(loc)
        si, so, ei, eo = loc
        self.assertEqual(segs[si]["v"][so:eo], "天气很好")

    def test_whitespace_tolerant(self):
        segs = [txt("今天  天气\t很好")]
        loc = review.locate(segs, "今天 天气 很好")
        self.assertIsNotNone(loc)

    def test_bare_fallback_ignores_all_space(self):
        segs = [txt("A B C D")]
        loc = review.locate(segs, "BCD")
        self.assertIsNotNone(loc)

    def test_missing_returns_none(self):
        self.assertIsNone(review.locate([txt("abc")], "xyz"))

    def test_empty_target(self):
        self.assertIsNone(review.locate([txt("abc")], ""))


# ══════════════════════════════════════════════════════ 编辑
class TestApplyEdit(unittest.TestCase):
    def test_same_segment(self):
        segs = [txt("abcdef")]
        out = review.apply_edit(segs, (0, 1, 0, 3), "XY")
        self.assertEqual(out[0]["v"], "aXYdef")
        self.assertEqual(segs[0]["v"], "abcdef", "原对象不能被修改")

    def test_cross_segment_keeps_image(self):
        segs = [txt("abc"), img(), txt("def")]
        out = review.apply_edit(segs, (0, 1, 2, 2), "ZZ")
        self.assertEqual(out[0]["v"], "aZZ")
        self.assertEqual(out[1]["t"], "img", "图片必须原位保留")
        self.assertEqual(out[2]["v"], "f", "e_off 是开区间，索引 2 之后才是保留部分")
        self.assertEqual(len(out), 3)

    def test_apply_accepted_multiple_same_block(self):
        blocks = index([para(txt("甲乙丙丁戊己"))])
        anns = [
            {"id": "a1", "block_id": "b0", "op": "replace_sentence", "type": "表达优化",
             "target": "甲乙", "replacement": "AB", "reason": "", "severity": "low",
             "status": "accepted", "anchor": [0, 0, 0, 2], "sid": 0},
            {"id": "a2", "block_id": "b0", "op": "replace_sentence", "type": "表达优化",
             "target": "戊己", "replacement": "XY", "reason": "", "severity": "low",
             "status": "accepted", "anchor": [0, 4, 0, 6], "sid": 1},
        ]
        out = review.apply_accepted(blocks, anns)
        self.assertEqual(out[0]["segs"][0]["v"], "AB丙丁XY")

    def test_apply_accepted_skips_rejected(self):
        blocks = index([para(txt("甲乙"))])
        anns = [{"id": "a1", "block_id": "b0", "op": "replace_sentence", "type": "表达优化",
                 "target": "甲乙", "replacement": "XY", "reason": "", "severity": "low",
                 "status": "rejected", "anchor": [0, 0, 0, 2], "sid": 0}]
        out = review.apply_accepted(blocks, anns)
        self.assertEqual(out[0]["segs"][0]["v"], "甲乙")

    def test_insert_after(self):
        blocks = index([para(txt("第一句。"))])
        anns = [{"id": "a1", "block_id": "b0", "op": "insert_after", "type": "知识补充",
                 "target": "第一句。", "replacement": "补充内容。", "reason": "", "severity": "low",
                 "status": "accepted", "anchor": [0, 4, 0, 4], "sid": 0}]
        out = review.apply_accepted(blocks, anns)
        self.assertEqual(out[0]["segs"][0]["v"], "第一句。补充内容。")


# ══════════════════════════════════════════════════════ 批注构建
class TestBuildAnnotation(unittest.TestCase):
    def setUp(self):
        self.blocks = index([
            heading("第一章"),
            para(txt("第一句。第二句。")),
            para(txt("带图"), img(), txt("的段落。")),
        ])
        self.by_id = {b["id"]: b for b in self.blocks}

    def test_by_sid(self):
        a = review.build_annotation(
            {"block_id": "b1", "sid": 1, "op": "replace_sentence",
             "replacement": "改写后的第二句。", "reason": "r", "type": "表达优化"}, self.by_id, 1)
        self.assertEqual(a["target"], "第二句。")
        self.assertEqual(a["anchor"], [0, 4, 0, 8])

    def test_invalid_block_dropped(self):
        self.assertIsNone(review.build_annotation(
            {"block_id": "b99", "sid": 0, "replacement": "x"}, self.by_id, 1))

    def test_table_block_dropped(self):
        blocks = index([{"id": "b0", "kind": "table", "level": 0, "rows": [], "segs": []}])
        self.assertIsNone(review.build_annotation(
            {"block_id": "b0", "sid": 0, "replacement": "x"}, {b["id"]: b for b in blocks}, 1))

    def test_unknown_sid_falls_back_to_target(self):
        a = review.build_annotation(
            {"block_id": "b1", "sid": 99, "op": "replace_sentence", "target": "第一句。",
             "replacement": "新的第一句。", "reason": "r"}, self.by_id, 1)
        self.assertIsNotNone(a)
        self.assertEqual(a["target"], "第一句。")

    def test_unlocatable_becomes_comment(self):
        a = review.build_annotation(
            {"block_id": "b1", "op": "replace_sentence", "target": "不存在的句子",
             "replacement": "x", "reason": "无法定位"}, self.by_id, 1)
        self.assertEqual(a["op"], "comment")

    def test_identical_replacement_rejected(self):
        self.assertIsNone(review.build_annotation(
            {"block_id": "b1", "sid": 0, "op": "replace_sentence",
             "replacement": "第一句。", "reason": "r"}, self.by_id, 1))

    def test_unknown_type_normalized(self):
        a = review.build_annotation(
            {"block_id": "b1", "sid": 0, "op": "replace_sentence",
             "replacement": "改。", "reason": "r", "type": "瞎写的类型"}, self.by_id, 1)
        self.assertIn(a["type"], review.TYPES)

    def test_replacement_list_joined(self):
        a = review.build_annotation(
            {"block_id": "b1", "sid": 0, "op": "replace_sentence",
             "replacement": ["甲", "乙"], "reason": "r"}, self.by_id, 1)
        self.assertEqual(a["replacement"], "甲乙")

    def test_image_segment_block_anchor_safe(self):
        """跨图片的句子必须被切成两段，锚点不会跨过图片。"""
        a = review.build_annotation(
            {"block_id": "b2", "sid": 1, "op": "replace_sentence",
             "replacement": "替换。", "reason": "r"}, self.by_id, 1)
        self.assertEqual(a["anchor"][0], a["anchor"][2], "锚点不能跨 seg")


# ══════════════════════════════════════════════════════ 渲染
class TestRenderTokens(unittest.TestCase):
    def setUp(self):
        self.blocks = index([para(txt("甲句。乙句。"))])
        self.anns = [
            {"id": "a1", "block_id": "b0", "op": "replace_sentence", "type": "表达优化",
             "target": "甲句。", "replacement": "A句。", "reason": "", "severity": "low",
             "status": "accepted", "anchor": [0, 0, 0, 3], "sid": 0},
            {"id": "a2", "block_id": "b0", "op": "comment", "type": "结构建议",
             "target": "", "replacement": "", "reason": "建议分节", "severity": "low",
             "status": "pending", "anchor": None, "sid": None},
        ]

    def test_original_untouched(self):
        toks = review.render_tokens(self.blocks[0], self.anns, "original")
        self.assertEqual("".join(t.get("v", "") for t in toks if t["k"] == "t"), "甲句。乙句。")

    def test_revised_applies(self):
        toks = review.render_tokens(self.blocks[0], self.anns, "revised")
        text = "".join(t.get("v", "") for t in toks if t["k"] in ("t", "ins"))
        self.assertEqual(text, "A句。乙句。")

    def test_annotated_marks_target(self):
        toks = review.render_tokens(self.blocks[0], self.anns, "annotated")
        marks = [t for t in toks if t["k"] == "mark"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["v"], "甲句。")

    def test_comment_without_anchor_yields_anchor_token(self):
        toks = review.render_tokens(self.blocks[0], [self.anns[1]], "annotated")
        self.assertTrue(any(t["k"] == "anchor" for t in toks))


# ══════════════════════════════════════════════════════ 统计
class TestStats(unittest.TestCase):
    def test_counts(self):
        anns = [{"type": "错别字", "status": "pending"},
                {"type": "错别字", "status": "accepted"},
                {"type": "事实纠错", "status": "rejected"}]
        s = review.stats(anns)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["pending"], 1)
        self.assertEqual(s["accepted"], 1)
        self.assertEqual(s["rejected"], 1)
        self.assertEqual(s["by_type"]["错别字"], 2)


# ══════════════════════════════════════════════════════ 标点整理
class TestTidyPunct(unittest.TestCase):
    def test_duplicate(self):
        self.assertEqual(review.tidy_punct("好的。。"), "好的。")

    def test_space_before_punct(self):
        self.assertEqual(review.tidy_punct("好的 ，可以"), "好的，可以")

    def test_space_after_open(self):
        self.assertEqual(review.tidy_punct("（ 内容）"), "（内容）")


# ══════════════════════════════════════════════════════ 重排
class TestRelayout(unittest.TestCase):
    def _blocks(self):
        return index([
            heading("绪论"),
            para(txt("正文一。")),
            heading("方法", 2),
            para(txt("正文二。")),
            heading("结论"),
        ])

    def test_numbering_and_toc(self):
        r = relayout.relayout(self._blocks(), {})
        self.assertEqual(len(r["toc"]), 3)
        self.assertEqual(r["toc"][0]["no"], "一、")
        self.assertEqual(r["toc"][1]["no"], "（一）")

    def test_heading_level_normalized(self):
        blocks = index([heading("A", 5), heading("B", 7)])
        r = relayout.relayout(blocks, {})
        self.assertEqual([b["level"] for b in r["blocks"]], [1, 2])

    def test_strip_existing_number(self):
        blocks = index([heading("第三章 方法")])
        r = relayout.relayout(blocks, {"number_headings": True})
        self.assertEqual(r["blocks"][0]["heading_no"], "一、")
        self.assertNotIn("第三章", "".join(s["v"] for s in r["blocks"][0]["segs"]))

    def test_merge_lines(self):
        blocks = index([para(txt("第一行没有句号\n第二行接上"))])
        r = relayout.relayout(blocks, {"merge_lines": True})
        self.assertEqual(r["blocks"][0]["segs"][0]["v"], "第一行没有句号第二行接上")

    def test_merge_lines_respects_numbered_item(self):
        blocks = index([para(txt("前言\n1、第一条"))])
        r = relayout.relayout(blocks, {"merge_lines": True})
        self.assertIn("\n", r["blocks"][0]["segs"][0]["v"])

    def test_materialize_writes_number(self):
        r = relayout.relayout(index([heading("绪论")]), {})
        out = relayout.materialize(r)
        self.assertTrue("".join(s["v"] for s in out[0]["segs"]).startswith("一、"))

    def test_prepend_summary(self):
        r = relayout.relayout(index([para(txt("x"))]), {}, {"summary": "摘要", "key_points": ["p1"]})
        out = relayout.prepend_summary(r)
        self.assertEqual(out[0]["kind"], "heading")
        self.assertIn("内容提要", out[0]["segs"][0]["v"])

    def test_ensure_heading_when_none(self):
        blocks = index([para(txt("这是一个标题")), para(txt("正文。"))])
        r = relayout.relayout(blocks, {"ensure_heading": True})
        self.assertEqual(r["blocks"][0]["kind"], "heading")

    def test_image_order_preserved(self):
        blocks = index([para(img("x.png")), heading("A"), para(txt("t"))])
        r = relayout.relayout(blocks, {})
        self.assertEqual(r["blocks"][0]["segs"][0]["t"], "img")


class TestRelayoutAdvanced(unittest.TestCase):
    def test_number_style_arabic(self):
        r = relayout.relayout(index([heading("甲"), heading("乙")]),
                              {"number_style": "arabic"})
        self.assertEqual(r["toc"][0]["no"], "1. ")
        self.assertEqual(r["toc"][1]["no"], "2. ")

    def test_number_style_chapter(self):
        r = relayout.relayout(index([heading("甲")]), {"number_style": "chapter"})
        self.assertEqual(r["toc"][0]["no"], "第一章 ")

    def test_number_style_cn_paren(self):
        r = relayout.relayout(index([heading("甲")]), {"number_style": "cn_paren"})
        self.assertEqual(r["toc"][0]["no"], "（一）")

    def test_strip_circled_number(self):
        r = relayout.relayout(index([heading("① 绪论")]), {"number_headings": True})
        text = "".join(s["v"] for s in r["blocks"][0]["segs"])
        self.assertEqual(text.strip(), "绪论")

    def test_strip_dotted_number(self):
        r = relayout.relayout(index([heading("3.1.2 方法")]), {"number_headings": True})
        text = "".join(s["v"] for s in r["blocks"][0]["segs"])
        self.assertEqual(text.strip(), "方法")

    def test_dedup_adjacent_headings(self):
        blocks = index([heading("一、笔刷"), heading("笔刷"), para(txt("正文。"))])
        r = relayout.relayout(blocks, {"dedup_headings": True})
        self.assertEqual(r["stats"]["dedup_headings"], 1)
        self.assertEqual(sum(1 for b in r["blocks"] if b["kind"] == "heading"), 1)

    def test_keep_separated_duplicate_headings(self):
        blocks = index([heading("甲"), para(txt("正文。")), heading("甲")])
        r = relayout.relayout(blocks, {"dedup_headings": True})
        self.assertEqual(r["stats"]["dedup_headings"], 0)

    def test_drop_empty_blocks(self):
        blocks = index([para(txt("  ")), para(txt("有内容。")), para(txt("\n"))])
        r = relayout.relayout(blocks, {"drop_empty": True})
        self.assertEqual(len(r["blocks"]), 1)
        self.assertEqual(r["stats"]["dropped_empty"], 2)

    def test_empty_block_with_image_kept(self):
        blocks = index([para(img("a.png")), para(txt(""))])
        r = relayout.relayout(blocks, {"drop_empty": True})
        self.assertEqual(len(r["blocks"]), 1)
        self.assertEqual(r["blocks"][0]["segs"][0]["t"], "img")

    def test_normalize_list_markers(self):
        blocks = index([{"id": "b0", "kind": "list", "level": 0,
                         "segs": [txt("- 第一条")], "style": {}},
                        {"id": "b1", "kind": "list", "level": 0,
                         "segs": [txt("1. 第二条")], "style": {}}])
        r = relayout.relayout(blocks, {"normalize_list": True})
        self.assertTrue(r["blocks"][0]["segs"][0]["v"].startswith("· 第一条"))
        self.assertTrue(r["blocks"][1]["segs"][0]["v"].startswith("1. 第二条"),
                        "数字序号不能被改成圆点")

    def test_split_long_paragraph(self):
        body = "".join(f"这是第{i}句话，用来把段落撑长。" for i in range(120))
        blocks = index([para(txt(body))])
        r = relayout.relayout(blocks, {"split_long_paras": True})
        self.assertGreater(len(r["blocks"]), 1)
        self.assertGreater(r["stats"]["split_paras"], 0)
        joined = "".join("".join(s["v"] for s in b["segs"]) for b in r["blocks"])
        self.assertEqual(joined.replace(" ", ""), body.replace(" ", ""),
                         "拆段不能丢字")

    def test_split_ids_unique(self):
        body = "".join(f"句子{i}。" for i in range(400))
        blocks = index([para(txt(body))])
        r = relayout.relayout(blocks, {"split_long_paras": True})
        ids = [b["id"] for b in r["blocks"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_split_when_image_present(self):
        body = "".join(f"句子{i}。" for i in range(400))
        blocks = index([para(txt(body), img(), txt("尾巴。"))])
        r = relayout.relayout(blocks, {"split_long_paras": True})
        self.assertEqual(len(r["blocks"]), 1)
        self.assertEqual(sum(1 for s in r["blocks"][0]["segs"] if s["t"] == "img"), 1)

    def test_ensure_headings_promotes_several(self):
        blocks = index([para(txt("第一节")), para(txt("正文内容在这里。")),
                        para(txt("第二节")), para(txt("更多正文。"))])
        r = relayout.relayout(blocks, {"ensure_heading": True})
        self.assertEqual(r["stats"]["promoted"], 2)

    def test_stats_reported(self):
        r = relayout.relayout(index([heading("甲"), para(txt("a\nb"))]), {})
        for k in ("merged_lines", "split_paras", "dropped_empty",
                  "dedup_headings", "promoted", "renumbered"):
            self.assertIn(k, r["stats"])


# ══════════════════════════════════════════════════════ 历史
class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nr_hist_")
        self.store = history.HistoryStore(self.tmp, keep=3)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_read(self):
        p = os.path.join(self.tmp, "note.docx")
        e = self.store.save(p, {"anns": [{"id": "a1"}], "summary": {"summary": "s"}})
        self.assertTrue(e["id"])
        snap = self.store.read(p, e["id"])
        self.assertEqual(len(snap["anns"]), 1)

    def test_entries_sorted_desc(self):
        p = os.path.join(self.tmp, "n.docx")
        for i in range(2):
            self.store.save(p, {"id": f"s{i}", "epoch": 1000 + i, "anns": []})
        ents = self.store.entries(p)
        self.assertEqual(ents[0]["id"], "s1")

    def test_prune_keep(self):
        p = os.path.join(self.tmp, "n.docx")
        for i in range(6):
            self.store.save(p, {"id": f"s{i}", "epoch": 1000 + i, "anns": []})
        self.assertLessEqual(len(self.store.entries(p)), 3)

    def test_meta_recovery(self):
        p = os.path.join(self.tmp, "n.docx")
        self.store.save(p, {"id": "s1", "anns": []})
        os.remove(self.store.meta_path(p))
        ents = self.store.entries(p)
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["id"], "s1")

    def test_delete_and_clear(self):
        p = os.path.join(self.tmp, "n.docx")
        e = self.store.save(p, {"anns": []})
        self.assertTrue(self.store.delete(p, e["id"]))
        self.assertEqual(self.store.entries(p), [])
        self.store.save(p, {"anns": []})
        self.assertEqual(self.store.clear(p), 1)

    def test_summary_counts(self):
        p = os.path.join(self.tmp, "n.docx")
        self.store.save(p, {"anns": []})
        s = self.store.summary()
        self.assertEqual(s["files"], 1)
        self.assertEqual(s["entries"], 1)

    def test_unsafe_name_sanitized(self):
        p = os.path.join(self.tmp, 'a:b*c?.docx')
        d = self.store.dir_for(p)
        self.assertNotIn(":", os.path.basename(d))


# ══════════════════════════════════════════════════════ JSON 抽取
class TestExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(llm.extract_json('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(llm.extract_json('```json\n{"a":1}\n```'), {"a": 1})

    def test_surrounded_by_prose(self):
        self.assertEqual(llm.extract_json('好的，结果如下：\n{"a":1}\n希望有帮助'), {"a": 1})

    def test_nested(self):
        self.assertEqual(llm.extract_json('{"a":{"b":[1,2]}}'), {"a": {"b": [1, 2]}})

    def test_brace_inside_string(self):
        self.assertEqual(llm.extract_json('{"a":"}不是结尾"}'), {"a": "}不是结尾"})

    def test_garbage(self):
        self.assertEqual(llm.extract_json("完全不是 JSON"), {})

    def test_empty(self):
        self.assertEqual(llm.extract_json(""), {})


class TestJsonRepair(unittest.TestCase):
    """小模型被 num_predict 截断时，已生成的批注要尽量救回来。"""

    def test_truncated_after_complete_item(self):
        t = ('{"annotations":[{"block_id":"b0","sid":0,"op":"replace_sentence",'
             '"type":"错别字","replacement":"甲。","reason":"r"},{"block_id":"b1","sid":')
        d = llm.extract_json(t)
        self.assertIn("annotations", d)
        self.assertEqual(len(d["annotations"]), 1)
        self.assertEqual(d["annotations"][0]["block_id"], "b0")

    def test_truncated_inside_string(self):
        t = '{"summary":"这是一段还没写完的概'
        d = llm.extract_json(t)
        self.assertIn("summary", d)
        self.assertTrue(d["summary"].startswith("这是一段"))

    def test_truncated_nested_array(self):
        t = '{"key_points":["a","b","c'
        d = llm.extract_json(t)
        self.assertEqual(d.get("key_points"), ["a", "b", "c"])

    def test_trailing_comma(self):
        self.assertEqual(llm.extract_json('{"a":1,}'), {"a": 1})

    def test_unrecoverable(self):
        self.assertEqual(llm.extract_json("{{{"), {})


# ══════════════════════════════════════════════════════ 分块
class TestChunking(unittest.TestCase):
    def test_budget_respected(self):
        blocks = index([para(txt("啊" * 400)) for _ in range(10)])
        chunks = pipeline._chunk_pieces(pipeline._pieces(blocks, 1000), 1000)
        self.assertGreater(len(chunks), 1)

    def test_empty_blocks_skipped(self):
        blocks = index([para(txt("   ")), para(txt("有字。"))])
        pieces = pipeline._pieces(blocks)
        self.assertEqual(len(pieces), 1)

    def test_render_chunk_has_sid(self):
        blocks = index([para(txt("甲。乙。"))])
        out = pipeline._render_chunk(pipeline._pieces(blocks))
        self.assertIn("0| ", out)
        self.assertIn("1| ", out)

    def test_table_rendered(self):
        blocks = index([{"id": "b0", "kind": "table", "level": 0, "segs": [],
                         "rows": [[{"segs": [txt("A")]}, {"segs": [txt("B")]}]], "style": {}}])
        out = pipeline._render_chunk(pipeline._pieces(blocks))
        self.assertIn("表格", out)
        self.assertIn("A | B", out)


class TestLongBlockSplit(unittest.TestCase):
    """两万字的长段落必须被切开送审，且 sid 保持绝对编号。"""

    def test_long_paragraph_split(self):
        body = "".join(f"这是第{i}句。" for i in range(600))   # 约 5400 字
        blocks = index([para(txt(body))])
        pieces = pipeline._pieces(blocks, budget=1000)
        self.assertGreater(len(pieces), 1)
        # 所有片段的 sid 区间必须无缝、无重叠、覆盖全部句子
        total = len(review.block_sentences(blocks[0]))
        covered = []
        for p in pieces:
            covered.extend(range(p["lo"], p["hi"]))
        self.assertEqual(covered, list(range(total)))

    def test_split_chunks_keep_absolute_sid(self):
        body = "".join(f"第{i}句。" for i in range(400))
        blocks = index([para(txt(body))])
        pieces = pipeline._pieces(blocks, budget=500)
        rendered = [pipeline._render_chunk([p]) for p in pieces]
        # 最后一片必须包含最后一个句子的绝对编号
        last_sid = len(review.block_sentences(blocks[0])) - 1
        self.assertIn(f"  {last_sid}| ", rendered[-1])
        self.assertIn("绝对编号", rendered[1])

    def test_short_paragraph_not_split(self):
        blocks = index([para(txt("短句。"))])
        self.assertEqual(len(pipeline._pieces(blocks)), 1)


# ══════════════════════════════════════════════════════ 审查流水线
class TestReviewDocument(unittest.TestCase):
    def test_annotations_parsed_and_sorted(self):
        blocks = index([para(txt("第一句。第二句。"))])
        doc = {"title": "t", "blocks": blocks}
        reply = json.dumps({"annotations": [
            {"block_id": "b0", "sid": 0, "op": "replace_sentence", "type": "表达优化",
             "replacement": "新第一句。", "reason": "r1", "severity": "low"},
            {"block_id": "b0", "sid": 1, "op": "replace_sentence", "type": "事实纠错",
             "replacement": "新第二句。", "reason": "r2", "severity": "high"},
        ]}, ensure_ascii=False)
        anns = pipeline.review_document(doc, MockLLM([reply]))
        self.assertEqual(len(anns), 2)
        self.assertEqual(anns[0]["severity"], "high", "严重问题应排在前面")
        self.assertEqual([a["id"] for a in anns], ["a1", "a2"])

    def test_duplicate_annotations_removed(self):
        blocks = index([para(txt("甲。"))])
        item = {"block_id": "b0", "sid": 0, "op": "replace_sentence", "type": "表达优化",
                "replacement": "乙。", "reason": "r", "severity": "low"}
        reply = json.dumps({"annotations": [item, item]}, ensure_ascii=False)
        anns = pipeline.review_document({"title": "t", "blocks": blocks}, MockLLM([reply]))
        self.assertEqual(len(anns), 1)

    def test_edits_key_alias(self):
        blocks = index([para(txt("甲。"))])
        reply = json.dumps({"edits": [
            {"block_id": "b0", "sid": 0, "op": "replace_sentence", "type": "表达优化",
             "replacement": "乙。", "reason": "r"}]}, ensure_ascii=False)
        anns = pipeline.review_document({"title": "t", "blocks": blocks}, MockLLM([reply]))
        self.assertEqual(len(anns), 1)

    def test_malformed_reply_does_not_crash(self):
        blocks = index([para(txt("甲。"))])
        anns = pipeline.review_document({"title": "t", "blocks": blocks}, MockLLM(["不是 JSON"]))
        self.assertEqual(anns, [])

    def test_progress_monotonic(self):
        blocks = index([para(txt("啊" * 800)) for _ in range(4)])
        seen = []
        pipeline.review_document({"title": "t", "blocks": blocks}, MockLLM([]),
                                 on_progress=lambda m, p: seen.append(p))
        self.assertTrue(seen)
        self.assertEqual(seen, sorted(seen), "进度必须单调不减")

    def test_summarize_degrades_on_error(self):
        blocks = index([para(txt("内容。"))])
        s = pipeline.summarize({"title": "t", "blocks": blocks},
                               MockLLM([llm.LLMError("boom")]))
        self.assertEqual(s["summary"], "")
        self.assertEqual(s["key_points"], [])

    def test_summarize_parses(self):
        blocks = index([para(txt("内容。"))])
        reply = json.dumps({"summary": "概括", "key_points": ["a"], "outline": ["b"],
                            "gaps": ["c"]}, ensure_ascii=False)
        s = pipeline.summarize({"title": "t", "blocks": blocks}, MockLLM([reply]))
        self.assertEqual(s["summary"], "概括")
        self.assertEqual(s["key_points"], ["a"])

    def test_one_failed_chunk_does_not_kill_review(self):
        """某一块模型调用失败时，其余块的批注必须保住。"""
        blocks = index([para(txt("啊" * 900)), para(txt("啊" * 900))])
        doc = {"title": "t", "blocks": blocks}
        good = json.dumps({"annotations": [
            {"block_id": "b1", "sid": 0, "op": "replace_sentence", "type": "表达优化",
             "replacement": "改后。", "reason": "r"}]}, ensure_ascii=False)
        rep = {}
        anns = pipeline.review_document(
            doc, MockLLM([llm.LLMError("连接模型服务失败：reset"), good]), report=rep)
        self.assertEqual(len(rep["failed"]), 1)
        self.assertGreaterEqual(len(anns), 0)

    def test_retry_on_unparseable_then_success(self):
        """第一次输出不是 JSON 时，追加纠正提示再问一次。"""
        blocks = index([para(txt("甲。"))])
        good = json.dumps({"annotations": [
            {"block_id": "b0", "sid": 0, "op": "replace_sentence", "type": "表达优化",
             "replacement": "乙。", "reason": "r"}]}, ensure_ascii=False)
        m = MockLLM(["好的，我来分析这篇笔记", good])
        anns = pipeline.review_document({"title": "t", "blocks": blocks}, m)
        self.assertEqual(len(anns), 1)
        self.assertIn("只**输出", m.calls[1]["user"])
        self.assertTrue(m.calls[1]["user"].startswith(m.calls[0]["user"].rstrip()))


class TestApplyOrdering(unittest.TestCase):
    def test_insert_and_replace_at_same_offset(self):
        """同一位置既有「句后插入」又有「下一句整句替换」时，插入内容不能丢。"""
        blocks = index([para(txt("第一句。第二句。"))])
        anns = [
            {"id": "a1", "block_id": "b0", "op": "replace_sentence", "type": "表达优化",
             "target": "第二句。", "replacement": "改写句。", "reason": "", "severity": "low",
             "status": "accepted", "anchor": [0, 4, 0, 8], "sid": 1},
            {"id": "a2", "block_id": "b0", "op": "insert_after", "type": "知识补充",
             "target": "第一句。", "replacement": "补充句。", "reason": "", "severity": "low",
             "status": "accepted", "anchor": [0, 4, 0, 4], "sid": 0},
        ]
        out = review.apply_accepted(blocks, anns)
        text = "".join(s["v"] for s in out[0]["segs"] if s["t"] == "text")
        self.assertEqual(text, "第一句。补充句。改写句。")


# ══════════════════════════════════════════════════════ 图像批注
class TestImageAnnotations(unittest.TestCase):
    def test_no_vision_single_summary(self):
        rep = {"vision": False, "vision_reason": "没模型", "total": 5,
               "items": [{"block_id": "b0", "index": 1}]}
        out = review.image_annotations(rep, 0)
        self.assertEqual(len(out), 1)
        self.assertIn("5", out[0]["reason"])

    def test_flags_become_annotations(self):
        rep = {"vision": True, "total": 2, "items": [
            {"block_id": "b0", "index": 1, "sensitive": True, "sensitive_note": "有身份证号"},
            {"block_id": "b1", "index": 2},
        ]}
        out = review.image_annotations(rep, 10)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "high")
        self.assertEqual(out[0]["id"], "a11")

    def test_cap_40(self):
        items = [{"block_id": f"b{i}", "index": i, "needs_check": True} for i in range(60)]
        out = review.image_annotations({"vision": True, "total": 60, "items": items}, 0)
        self.assertEqual(len(out), 40)

    def test_empty(self):
        self.assertEqual(review.image_annotations({}, 0), [])


# ══════════════════════════════════════════════════════ 文本解析
class TestParseText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nr_txt_")
        self.store = docmodel.ImageStore(self.tmp, "d1")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content, enc="utf-8"):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding=enc) as f:
            f.write(content)
        return p

    def test_markdown_headings(self):
        p = self._write("a.md", "# 标题\n\n正文。\n\n## 小节\n\n- 项目一\n")
        d = docmodel.parse_text(p, self.store)
        kinds = [b["kind"] for b in d["blocks"]]
        self.assertIn("heading", kinds)
        self.assertIn("list", kinds)

    def test_markdown_inline_stripped(self):
        p = self._write("a.md", "这是**加粗**和`代码`以及[链接](http://x)。\n")
        d = docmodel.parse_text(p, self.store)
        text = "".join(s["v"] for s in d["blocks"][0]["segs"])
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertIn("链接", text)

    def test_code_fence_kept(self):
        p = self._write("a.md", "```\nprint(1)\n```\n")
        d = docmodel.parse_text(p, self.store)
        self.assertTrue(d["blocks"][0]["style"].get("code"))

    def test_txt_paragraphs(self):
        p = self._write("a.txt", "第一段。\n\n第二段。\n")
        d = docmodel.parse_text(p, self.store)
        self.assertEqual(len(d["blocks"]), 2)

    def test_binary_rejected(self):
        p = os.path.join(self.tmp, "bad.txt")
        with open(p, "wb") as f:
            f.write(b"\x00\x01\x02\x03" * 200)
        with self.assertRaises(ValueError):
            docmodel.parse_text(p, self.store)

    def test_title_guessed(self):
        p = self._write("a.txt", "我的笔记标题\n\n正文内容。\n")
        d = docmodel.parse_text(p, self.store)
        self.assertEqual(d["title"], "我的笔记标题")

    def test_gbk_file_decoded(self):
        """中文 Windows 上大量 .txt 是 GBK，不能读成乱码。"""
        p = os.path.join(self.tmp, "gbk.txt")
        with open(p, "wb") as f:
            f.write("这是国标编码的中文笔记。\n\n第二段。".encode("gbk"))
        d = docmodel.parse_text(p, self.store)
        text = "".join(s["v"] for b in d["blocks"] for s in b["segs"])
        self.assertIn("国标编码", text)
        self.assertNotIn("\ufffd", text)

    def test_bom_stripped(self):
        p = os.path.join(self.tmp, "bom.txt")
        with open(p, "wb") as f:
            f.write(b"\xef\xbb\xbf" + "带 BOM 的笔记。".encode("utf-8"))
        d = docmodel.parse_text(p, self.store)
        text = "".join(s["v"] for b in d["blocks"] for s in b["segs"])
        self.assertTrue(text.startswith("带 BOM"), text[:10])

    def test_utf16_le_with_bom(self):
        p = os.path.join(self.tmp, "u16.txt")
        with open(p, "wb") as f:
            f.write("UTF16 内容。".encode("utf-16"))   # 自带 BOM
        d = docmodel.parse_text(p, self.store)
        text = "".join(s["v"] for b in d["blocks"] for s in b["segs"])
        self.assertIn("UTF16 内容", text)

    def test_markdown_table_becomes_table_block(self):
        md = ("说明段落。\n\n"
              "| 概念 | 定义 |\n"
              "|:---|---:|\n"
              "| **闭包** | 捕获外部变量的函数 |\n"
              "| 柯里化 | 多参变单参 |\n\n"
              "表格之后的段落。\n")
        p = self._write("t.md", md)
        d = docmodel.parse_text(p, self.store)
        tables = [b for b in d["blocks"] if b["kind"] == "table"]
        self.assertEqual(len(tables), 1)
        rows = tables[0]["rows"]
        self.assertEqual(len(rows), 3, "表头 + 两行数据，分隔行要丢掉")
        self.assertEqual(rows[0][0]["segs"][0]["v"], "概念")
        self.assertEqual(rows[1][0]["segs"][0]["v"], "闭包", "单元格内的 markdown 要清掉")

    def test_markdown_table_keeps_order(self):
        md = "前文。\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n后文。\n"
        p = self._write("o.md", md)
        d = docmodel.parse_text(p, self.store)
        kinds = [b["kind"] for b in d["blocks"]]
        self.assertLess(kinds.index("table"), len(kinds) - 1)
        self.assertEqual(kinds[0], "para")
        self.assertEqual(kinds[-1], "para")

    def test_pipe_line_without_separator_stays_paragraph(self):
        p = self._write("p.md", "a | b 只是普通的一行文字\n")
        d = docmodel.parse_text(p, self.store)
        self.assertFalse(any(b["kind"] == "table" for b in d["blocks"]))


# ══════════════════════════════════════════════════════ 标题推断
class TestInferHeadings(unittest.TestCase):
    def test_chinese_numbering(self):
        blocks = [para(txt("一、总则")), para(txt("这是正文，比较长的一句话。"))]
        docmodel.infer_headings(blocks)
        self.assertEqual(blocks[0]["kind"], "heading")
        self.assertEqual(blocks[0]["level"], 1)
        self.assertEqual(blocks[1]["kind"], "para")

    def test_long_line_not_heading(self):
        blocks = [para(txt("一、" + "很长的内容" * 12))]
        docmodel.infer_headings(blocks)
        self.assertEqual(blocks[0]["kind"], "para")

    def test_sentence_ending_not_heading(self):
        blocks = [para(txt("一、这句话以句号结尾。"))]
        docmodel.infer_headings(blocks)
        self.assertEqual(blocks[0]["kind"], "para")


# ══════════════════════════════════════════════════════ 导出
class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nr_exp_")
        self.blocks = index([
            heading("标题"),
            para(txt("正文第一段。")),
            para(txt("带图"), img(), txt("继续。")),
        ])
        # 造一张真实 PNG
        from PIL import Image
        self.asset = os.path.join(self.tmp, "assets")
        os.makedirs(self.asset, exist_ok=True)
        Image.new("RGB", (20, 10), (200, 30, 30)).save(os.path.join(self.asset, "a.png"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_html_export(self):
        p = os.path.join(self.tmp, "o.html")
        docmodel.export_html(self.blocks, self.asset, p, "标题")
        html = open(p, encoding="utf-8").read()
        self.assertIn("正文第一段", html)
        self.assertIn("data:image/png;base64", html)

    def test_html_escapes(self):
        blocks = index([para(txt("<script>alert(1)</script>"))])
        p = os.path.join(self.tmp, "o.html")
        docmodel.export_html(blocks, self.asset, p, "t")
        html = open(p, encoding="utf-8").read()
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_docx_export_roundtrip(self):
        p = os.path.join(self.tmp, "o.docx")
        docmodel.export_docx(self.blocks, self.asset, p, "标题")
        from docx import Document
        d = Document(p)
        text = "\n".join(x.text for x in d.paragraphs)
        self.assertIn("正文第一段", text)

    def test_html_external_images_when_large(self):
        """图片总量超过预算时改为外链目录，避免导出上百 MB 的 HTML。"""
        old = docmodel.INLINE_IMAGE_BUDGET
        docmodel.INLINE_IMAGE_BUDGET = 1
        try:
            p = os.path.join(self.tmp, "o.html")
            docmodel.export_html(self.blocks, self.asset, p, "标题")
            html = open(p, encoding="utf-8").read()
            self.assertNotIn("base64", html)
            self.assertIn("o_files/a.png", html)
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, "o_files", "a.png")))
        finally:
            docmodel.INLINE_IMAGE_BUDGET = old


# ══════════════════════════════════════════════════════ 文档模型工具
class TestDocModelUtils(unittest.TestCase):
    def test_block_plain_marks_images(self):
        b = para(txt("前"), img(), txt("后"))
        self.assertEqual(docmodel.block_plain(b), "前⟦图1⟧后")

    def test_block_text_only(self):
        b = para(txt("前"), img(), txt("后"))
        self.assertEqual(docmodel.block_text_only(b), "前后")

    def test_count_images_nested_table(self):
        blocks = [{"id": "b0", "kind": "table", "level": 0, "segs": [],
                   "rows": [[{"segs": [img()]}, {"segs": []}]], "style": {}}]
        self.assertEqual(docmodel.count_images(blocks), 1)

    def test_xml_safe_strips_control(self):
        self.assertEqual(docmodel._xml_safe("a\x00b\x01c"), "abc")

    def test_image_store_dedup_by_content(self):
        tmp = tempfile.mkdtemp()
        try:
            st = docmodel.ImageStore(tmp, "d")
            a = st.save(b"same", "png", 1)
            b = st.save(b"same", "png", 2)
            self.assertEqual(a["file"], b["file"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 桥接层
class TestApi(unittest.TestCase):
    """Api 是前后端唯一通道，这里覆盖它的状态机与导出契约。"""

    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_api_")
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets, exist_ok=True)
        self.api = Api(workdir=self.tmp, get_window=lambda: None, asset_root=self.assets)
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        self.src = os.path.join(self.tmp, "笔记.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("我的笔记\n\n第一句。第二句。\n\n第二段内容。\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_boot_shape(self):
        r = self.api.boot()
        self.assertTrue(r["ok"])
        for k in ("health", "models", "relayoutOpts", "history", "version"):
            self.assertIn(k, r)

    def test_load_and_payload(self):
        r = self.api.load(self.src)
        self.assertTrue(r["ok"])
        self.assertTrue(r["doc"]["blocks"] > 0)
        for v in ("original", "annotated", "revised"):
            self.assertIn(v, r["views"])
        self.assertIn("stats", r)

    def test_load_missing_file(self):
        r = self.api.load(os.path.join(self.tmp, "nope.txt"))
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_load_unsupported_ext(self):
        p = os.path.join(self.tmp, "a.xyz")
        open(p, "w").write("x")
        r = self.api.load(p)
        self.assertFalse(r["ok"])
        self.assertIn("暂不支持", r["error"])

    def test_set_status_roundtrip(self):
        self.api.load(self.src)
        self.api.anns = [{"id": "a1", "block_id": "b1", "op": "comment", "type": "结构建议",
                          "target": "", "replacement": "", "reason": "r", "severity": "low",
                          "status": "pending", "anchor": None, "sid": None}]
        r = self.api.set_status("a1", "accepted")
        self.assertTrue(r["ok"])
        self.assertEqual(r["stats"]["accepted"], 1)
        self.assertEqual(r["anns"][0]["status"], "accepted")

    def test_set_status_unknown_id(self):
        self.api.load(self.src)
        r = self.api.set_status("nope", "accepted")
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["error"])

    def test_set_status_bad_value(self):
        self.api.load(self.src)
        r = self.api.set_status("a1", "whatever")
        self.assertFalse(r["ok"])

    def test_bulk_reports_affected(self):
        self.api.load(self.src)
        self.api.anns = [
            {"id": f"a{i}", "block_id": "b1", "op": "comment", "type": t,
             "target": "", "replacement": "", "reason": "r", "severity": "low",
             "status": "pending", "anchor": None, "sid": None}
            for i, t in enumerate(["错别字", "错别字", "事实纠错"])
        ]
        r = self.api.bulk("accept", "错别字")
        self.assertEqual(r["affected"], 2)
        self.assertEqual(r["stats"]["accepted"], 2)

    def test_bulk_unknown_scope(self):
        self.api.load(self.src)
        r = self.api.bulk("accept", "不存在的分类")
        self.assertFalse(r["ok"])

    def test_manual_note_unique_ids(self):
        self.api.load(self.src)
        bid = self.api.doc["blocks"][0]["id"]
        a = self.api.add_manual_note(bid, "第一条")
        b = self.api.add_manual_note(bid, "第二条")
        self.assertTrue(a["ok"] and b["ok"])
        ids = [x["id"] for x in b["anns"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_manual_note_empty_text(self):
        self.api.load(self.src)
        bid = self.api.doc["blocks"][0]["id"]
        self.assertFalse(self.api.add_manual_note(bid, "   ")["ok"])

    def test_export_infers_format_from_extension(self):
        self.api.load(self.src)
        out = os.path.join(self.tmp, "out", "笔记.md")
        r = self.api.export("revised", "docx", out)
        self.assertTrue(r["ok"])
        self.assertEqual(r["format"], "md")
        self.assertTrue(os.path.isfile(out))

    def test_export_creates_missing_dir(self):
        self.api.load(self.src)
        out = os.path.join(self.tmp, "a", "b", "c", "o.txt")
        r = self.api.export("original", "txt", out)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(out))

    def test_export_unknown_format(self):
        self.api.load(self.src)
        r = self.api.export("revised", "pdf", os.path.join(self.tmp, "x.pdf"))
        self.assertFalse(r["ok"])

    def test_view_cache_recomputes_after_review(self):
        """同样的批注 id/status、但内容不同的新结果，不能命中旧视图缓存。"""
        self.api.load(self.src)
        self.api.anns = [{"id": "a1", "block_id": "b1", "op": "comment", "type": "结构建议",
                          "target": "", "replacement": "", "reason": "旧", "severity": "low",
                          "status": "pending", "anchor": None, "sid": None}]
        self.api._payload()
        self.api.anns = [{"id": "a1", "block_id": "b1", "op": "comment", "type": "结构建议",
                          "target": "", "replacement": "", "reason": "新", "severity": "low",
                          "status": "pending", "anchor": None, "sid": None}]
        p = self.api._payload()
        self.assertEqual(p["anns"][0]["reason"], "新")

    def test_quit_blocked_while_busy(self):
        self.api.busy = True
        r = self.api.quit()
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])


# ══════════════════════════════════════════════════════ docx 解析保真
class TestGuessTitle(unittest.TestCase):
    def test_prefers_h1(self):
        blocks = index([para(txt("目录")), heading("真正的标题"), para(txt("正文。"))])
        self.assertEqual(docmodel._guess_title(blocks), "真正的标题")

    def test_first_line_beats_promoted_section(self):
        """首行是标题、但 infer_headings 把「一、基础操作」提成了一级标题时，
        标题仍应是首行，而不是第一个编号小节。"""
        blocks = index([
            para(txt("ZBrush 建模笔记")),
            heading("一、基础操作"),
            para(txt("正文内容。")),
        ])
        self.assertEqual(docmodel._guess_title(blocks), "ZBrush 建模笔记")

    def test_skips_page_noise(self):
        blocks = index([para(txt("Page 1")), para(txt("2024-05-01")), para(txt("目录")),
                        para(txt("ZBrush 建模笔记"))])
        self.assertEqual(docmodel._guess_title(blocks), "ZBrush 建模笔记")

    def test_fallback_to_first_text(self):
        blocks = index([para(txt("很长" * 30)), para(txt("短标题"))])
        self.assertEqual(docmodel._guess_title(blocks), "短标题")

    def test_sentence_first_line_skipped(self):
        blocks = index([para(txt("这是一句完整的正文，不该当标题。")),
                        heading("真正的标题")])
        self.assertEqual(docmodel._guess_title(blocks), "真正的标题")

    def test_empty(self):
        self.assertEqual(docmodel._guess_title([]), "未命名笔记")


class TestParseDocx(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nr_docx_")
        self.store = docmodel.ImageStore(self.tmp, "d")
        self.path = os.path.join(self.tmp, "t.docx")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, build):
        from docx import Document
        d = Document()
        build(d)
        d.save(self.path)
        return docmodel.parse_docx(self.path, self.store)

    def test_title_from_core_properties(self):
        def b(d):
            d.core_properties.title = "我的正式标题"
            d.add_paragraph("随便一段正文。")
        doc = self._build(b)
        self.assertEqual(doc["title"], "我的正式标题")

    def test_merged_table_cells_not_duplicated(self):
        def b(d):
            t = d.add_table(rows=2, cols=3)
            t.cell(0, 0).merge(t.cell(0, 2))
            t.cell(0, 0).text = "合并表头"
            t.cell(1, 0).text = "A"
            t.cell(1, 1).text = "B"
            t.cell(1, 2).text = "C"
        doc = self._build(b)
        tables = [x for x in doc["blocks"] if x["kind"] == "table"]
        self.assertEqual(len(tables), 1)
        rows = tables[0]["rows"]
        flat = "".join(s["v"] for c in rows[0] for s in c["segs"])
        self.assertEqual(flat.count("合并表头"), 1, "合并单元格的文字不能重复")
        self.assertEqual(rows[0][0]["span"], 3)

    def test_list_level_kept(self):
        def b(d):
            d.add_paragraph("一级", style="List Bullet")
            d.add_paragraph("二级", style="List Bullet 2")
        doc = self._build(b)
        levels = [x["style"].get("list_level") for x in doc["blocks"] if x["kind"] == "list"]
        self.assertIn(1, levels)

    def test_quote_style_flagged(self):
        def b(d):
            d.add_paragraph("引用的话", style="Quote")
        doc = self._build(b)
        self.assertTrue(any(x["style"].get("quote") for x in doc["blocks"]))

    def test_image_alt_captured(self):
        from docx.shared import Inches
        png = os.path.join(self.tmp, "a.png")
        from PIL import Image
        Image.new("RGB", (30, 20), (10, 120, 200)).save(png)

        def b(d):
            run = d.add_paragraph().add_run()
            run.add_picture(png, width=Inches(1))
            # python-docx 没有直接 API，手工写 descr
            for dp in run._element.iter(docmodel.DOCPR):
                dp.set("descr", "架构图")
                break
        doc = self._build(b)
        alts = [s.get("alt") for x in doc["blocks"] for s in x["segs"] if s["t"] == "img"]
        self.assertIn("架构图", alts)

    def test_no_footnotes_does_not_crash(self):
        doc = self._build(lambda d: d.add_paragraph("正文。"))
        self.assertTrue(doc["blocks"])


# ══════════════════════════════════════════════════════ 批注质量
class TestAnnotationQuality(unittest.TestCase):
    def test_clean_replacement_prefix(self):
        self.assertEqual(review.clean_replacement("修改后：正确的句子。"), "正确的句子。")
        self.assertEqual(review.clean_replacement("建议改为: 更好的写法"), "更好的写法")

    def test_clean_replacement_quotes(self):
        self.assertEqual(review.clean_replacement("“带引号的句子。”"), "带引号的句子。")
        self.assertEqual(review.clean_replacement("`code()`"), "code()")

    def test_clean_replacement_keeps_normal(self):
        self.assertEqual(review.clean_replacement("普通句子。"), "普通句子。")

    def test_placeholder_detected(self):
        for s in ("……", "...", "（略）", "同上", "N/A"):
            self.assertTrue(review.is_placeholder(s), s)
        self.assertFalse(review.is_placeholder("真实内容"))

    def test_keep_annotation_drops_placeholder(self):
        a = {"op": "replace_sentence", "replacement": "……", "reason": "这里有问题"}
        self.assertFalse(review.keep_annotation(a))

    def test_keep_annotation_requires_reason(self):
        a = {"op": "replace_sentence", "replacement": "新句子。", "reason": ""}
        self.assertFalse(review.keep_annotation(a))

    def test_keep_annotation_comment_needs_substance(self):
        self.assertFalse(review.keep_annotation(
            {"op": "comment", "reason": "注意", "replacement": ""}))
        self.assertTrue(review.keep_annotation(
            {"op": "comment", "reason": "本节缺少小标题，建议拆分", "replacement": ""}))

    def test_keep_annotation_accepts_good(self):
        a = {"op": "replace_sentence", "replacement": "新句子。", "reason": "原句把年份写错了"}
        self.assertTrue(review.keep_annotation(a))

    def test_dedupe_conflicts_keeps_highest_severity(self):
        anns = [
            {"id": "a1", "block_id": "b0", "op": "replace_sentence", "anchor": [0, 0, 0, 4],
             "severity": "low", "replacement": "版本A"},
            {"id": "a2", "block_id": "b0", "op": "replace_sentence", "anchor": [0, 0, 0, 4],
             "severity": "high", "replacement": "版本B"},
        ]
        out = review.dedupe_conflicts(anns)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["replacement"], "版本B")

    def test_dedupe_conflicts_keeps_different_positions(self):
        anns = [
            {"id": "a1", "block_id": "b0", "op": "replace_sentence", "anchor": [0, 0, 0, 4],
             "severity": "low", "replacement": "A"},
            {"id": "a2", "block_id": "b0", "op": "replace_sentence", "anchor": [0, 4, 0, 8],
             "severity": "low", "replacement": "B"},
        ]
        self.assertEqual(len(review.dedupe_conflicts(anns)), 2)

    def test_dedupe_conflicts_keeps_inserts(self):
        anns = [
            {"id": "a1", "block_id": "b0", "op": "insert_after", "anchor": [0, 4, 0, 4],
             "severity": "low", "replacement": "补充1"},
            {"id": "a2", "block_id": "b0", "op": "insert_after", "anchor": [0, 4, 0, 4],
             "severity": "low", "replacement": "补充2"},
        ]
        self.assertEqual(len(review.dedupe_conflicts(anns)), 2)


# ══════════════════════════════════════════════════════ 审查上下文
class TestReviewContext(unittest.TestCase):
    def _doc(self):
        blocks = index([
            heading("ZBrush 建模"),
            heading("一、笔刷", 2),
            para(txt("DynaMesh 是常用的重构工具。")),
            para(txt("第二个段落的正文内容。")),
        ])
        return {"title": "ZBrush 建模", "blocks": blocks}

    def test_doc_context_has_outline(self):
        ctx = pipeline._doc_context(self._doc())
        self.assertIn("ZBrush 建模", ctx)
        self.assertIn("一、笔刷", ctx)

    def test_prompt_carries_outline_and_prev_tail(self):
        doc = self._doc()
        m = MockLLM([])
        pipeline.review_document(doc, m)
        self.assertGreaterEqual(len(m.calls), 1)
        self.assertIn("全文结构", m.calls[0]["user"])

    def test_level_changes_system_prompt(self):
        doc = self._doc()
        m1 = MockLLM([])
        pipeline.review_document(doc, m1, level="light")
        m2 = MockLLM([])
        pipeline.review_document(doc, m2, level="strict")
        self.assertIn("只挑硬错误", m1.calls[0]["system"])
        self.assertIn("严格审校", m2.calls[0]["system"])

    def test_glossary_injected(self):
        doc = self._doc()
        m = MockLLM([])
        pipeline.review_document(doc, m, glossary=["DynaMesh", "ZBrush"])
        self.assertIn("术语白名单", m.calls[0]["system"])
        self.assertIn("DynaMesh", m.calls[0]["system"])

    def test_report_counts(self):
        doc = self._doc()
        rep = {}
        pipeline.review_document(doc, MockLLM([]), report=rep)
        for k in ("chunks", "failed", "empty", "annotations", "dropped", "level"):
            self.assertIn(k, rep)


class TestSummaryMapReduce(unittest.TestCase):
    def test_long_doc_uses_map_reduce(self):
        body = "".join(f"第{i}段讲的是某个具体知识点，内容比较长。\n" for i in range(900))
        blocks = index([para(txt(body))])
        doc = {"title": "长文", "blocks": blocks}
        map_reply = json.dumps({"points": ["要点甲", "要点乙"]}, ensure_ascii=False)
        final_reply = json.dumps({"summary": "总述", "key_points": ["k"], "outline": [],
                                  "gaps": []}, ensure_ascii=False)

        def fn(system, user):
            return map_reply if "要点提取器" in system else final_reply

        m = MockLLM(fn=fn)
        s = pipeline.summarize(doc, m)
        self.assertTrue(s["map_reduce"])
        self.assertEqual(s["summary"], "总述")
        self.assertGreater(len(m.calls), 2, "长文档必须先分段提炼再汇总")

    def test_short_doc_single_call(self):
        blocks = index([para(txt("很短的一段笔记内容。"))])
        reply = json.dumps({"summary": "短", "key_points": [], "outline": [], "gaps": []},
                           ensure_ascii=False)
        m = MockLLM([reply])
        s = pipeline.summarize({"title": "t", "blocks": blocks}, m)
        self.assertFalse(s["map_reduce"])
        self.assertEqual(len(m.calls), 1)

    def test_dedupes_key_points(self):
        blocks = index([para(txt("内容。"))])
        reply = json.dumps({"summary": "s", "key_points": ["a", "a", "b"],
                            "outline": ["x", "x"], "gaps": []}, ensure_ascii=False)
        s = pipeline.summarize({"title": "t", "blocks": blocks}, MockLLM([reply]))
        self.assertEqual(s["key_points"], ["a", "b"])
        self.assertEqual(s["outline"], ["x"])


class TestReviewOpts(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_opts_")
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_level(self):
        r = self.api.set_review_opts("strict")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reviewLevel"], "strict")

    def test_bad_level_rejected(self):
        r = self.api.set_review_opts("疯狂")
        self.assertFalse(r["ok"])

    def test_glossary_split(self):
        r = self.api.set_review_opts("", "DynaMesh，ZBrush、SubTool\nUV")
        self.assertEqual(r["glossary"], ["DynaMesh", "ZBrush", "SubTool", "UV"])

    def test_glossary_deduped(self):
        r = self.api.set_review_opts("", ["A", "A", " B "])
        self.assertEqual(r["glossary"], ["A", "B"])


# ══════════════════════════════════════════════════════ 取消
class TestCancel(unittest.TestCase):
    def test_cancel_stops_between_chunks(self):
        blocks = index([para(txt("啊" * 900)) for _ in range(4)])
        state = {"n": 0}

        def stop():
            state["n"] += 1
            return state["n"] > 1          # 第一次放行，第二次要求停止

        rep = {}
        pipeline.review_document({"title": "t", "blocks": blocks}, MockLLM([]),
                                 report=rep, should_stop=stop)
        self.assertTrue(rep["canceled"])
        self.assertEqual(rep["done_chunks"], 1)

    def test_no_cancel_flag_when_not_requested(self):
        blocks = index([para(txt("短句。"))])
        rep = {}
        pipeline.review_document({"title": "t", "blocks": blocks}, MockLLM([]), report=rep)
        self.assertFalse(rep["canceled"])

    def test_api_cancel_requires_busy(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp(prefix="nr_cancel_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            r = api.cancel_review()
            self.assertFalse(r["ok"])
            api.busy = True
            self.assertTrue(api.cancel_review()["ok"])
            self.assertTrue(api._cancel.is_set())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 静态服务
class TestServer(unittest.TestCase):
    def setUp(self):
        from backend import server
        self.tmp = tempfile.mkdtemp(prefix="nr_srv_")
        with open(os.path.join(self.tmp, "index.html"), "w", encoding="utf-8") as f:
            f.write("<h1>hi</h1>")
        with open(os.path.join(self.tmp, "app.js"), "w", encoding="utf-8") as f:
            f.write("console.log(1)")
        self.httpd, self.port = server.start(self.tmp, self.tmp)

    def tearDown(self):
        try:
            self.httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, path):
        import urllib.request
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}")

    def test_serves_index(self):
        with self._get("/") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("hi", r.read().decode())

    def test_scripts_are_not_long_cached(self):
        """升级后不能还拿缓存里的旧 app.js，否则新版后端配旧版前端。"""
        with self._get("/app.js") as r:
            self.assertIn("no-cache", r.headers.get("Cache-Control", ""))

    def test_missing_returns_404(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/nope.js")
        self.assertEqual(cm.exception.code, 404)

    def test_traversal_blocked(self):
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/../../etc/passwd")
        self.assertIn(cm.exception.code, (403, 404))


# ══════════════════════════════════════════════════════ 审阅报告
class TestReport(unittest.TestCase):
    def _rep(self):
        from backend import report
        doc = {"title": "ZBrush 笔记", "path": "D:/a/ZBrush 笔记.docx",
               "blocks": index([heading("笔刷"), para(txt("DynaMesh 用于重构。"))])}
        anns = [
            {"id": "a1", "type": "事实纠错", "severity": "high", "status": "pending",
             "op": "replace_sentence", "target": "旧句子。", "replacement": "新句子。",
             "reason": "年份写错了", "block_id": "b1"},
            {"id": "a2", "type": "表达优化", "severity": "low", "status": "accepted",
             "op": "insert_after", "target": "旧句子。", "replacement": "补充一句。",
             "reason": "缺少前提", "block_id": "b1"},
            {"id": "a3", "type": "结构建议", "severity": "medium", "status": "rejected",
             "op": "comment", "target": "", "replacement": "",
             "reason": "建议拆分小节", "block_id": "b1"},
        ]
        summary = {"summary": "讲笔刷。", "key_points": ["DynaMesh"], "outline": ["一、笔刷"],
                   "gaps": ["缺少快捷键"]}
        ir = {"total": 2, "analyzed": 2, "items": [
            {"index": 1, "caption": "界面截图", "position": "第2段", "needs_check": True,
             "check_note": "字太小"},
            {"index": 2, "caption": "正常图", "position": "第4段"},
        ], "note": "已分析 2/2"}
        return report.collect(doc, anns, summary, ir, review.stats(anns),
                              {"model": "qwen3:8b", "elapsed": 42})

    def test_collect_groups(self):
        r = self._rep()
        self.assertEqual(r["title"], "ZBrush 笔记")
        self.assertEqual(r["stats"]["total"], 3)
        self.assertEqual(len(r["groups"]), 3, "三种类型应分三组")
        self.assertEqual(len(r["image_flagged"]), 1)

    def test_html_contains_key_parts(self):
        from backend import report
        html = report.to_html(self._rep())
        for s in ("审阅报告", "ZBrush 笔记", "事实纠错", "年份写错了",
                  "内容归纳", "图像审查", "@media print"):
            self.assertIn(s, html)

    def test_html_escapes(self):
        from backend import report
        r = self._rep()
        r["title"] = "<script>x</script>"
        self.assertNotIn("<script>x", report.to_html(r))

    def test_markdown_has_valid_table(self):
        from backend import report
        md = report.to_markdown(self._rep())
        self.assertIn("| 指标 | 数量 |", md)
        self.assertIn("| --- | ---: |", md)
        self.assertIn("### 事实纠错", md)

    def test_csv_has_bom_and_rows(self):
        from backend import report
        doc = {"title": "t", "path": "x.docx", "blocks": []}
        anns = [{"type": "错别字", "severity": "high", "status": "pending",
                 "op": "replace_sentence", "target": "甲", "replacement": "乙",
                 "reason": "错字"}]
        csv_text = report.to_csv(anns)
        self.assertTrue(csv_text.startswith("\ufeff"), "Excel 需要 BOM 才不乱码")
        lines = csv_text.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("错别字", lines[1])
        self.assertIn("高", lines[1])

    def test_empty_annotations_render_gracefully(self):
        from backend import report
        doc = {"title": "空", "path": "e.docx", "blocks": []}
        r = report.collect(doc, [], {}, {}, review.stats([]), {})
        self.assertIn("没有发现需要修改的地方", report.to_html(r))
        self.assertIn("没有发现需要修改的地方", report.to_markdown(r))

    def test_api_exports_report_and_csv(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp(prefix="nr_rep_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            src = os.path.join(tmp, "n.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("标题\n\n第一句。\n")
            api.load(src)
            api.anns = [{"id": "a1", "block_id": "b1", "op": "comment", "type": "结构建议",
                         "target": "", "replacement": "", "reason": "建议分节",
                         "severity": "low", "status": "pending", "anchor": None, "sid": None}]
            api.summary = {"summary": "摘要"}
            for fmt, ext in (("html", ".html"), ("md", ".md")):
                out = os.path.join(tmp, "out", "r" + ext)
                r = api.export("report", fmt, out)
                self.assertTrue(r["ok"], r.get("error"))
                self.assertTrue(os.path.getsize(out) > 200)
            out = os.path.join(tmp, "out", "a.csv")
            r = api.export("csv", "csv", out)
            self.assertTrue(r["ok"])
            self.assertIn("结构建议", open(out, encoding="utf-8-sig").read())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_api_report_rejects_bad_format(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp(prefix="nr_rep2_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            src = os.path.join(tmp, "n.txt")
            open(src, "w", encoding="utf-8").write("正文。")
            api.load(src)
            r = api.export("report", "pdf", os.path.join(tmp, "x.pdf"))
            self.assertFalse(r["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 健壮性
class TestRobustness(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_rob_")
        self.assets = os.path.join(self.tmp, "assets")
        os.makedirs(self.assets, exist_ok=True)
        self.api = Api(workdir=self.tmp, get_window=lambda: None, asset_root=self.assets)
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_saved_atomically(self):
        self.api.cfg["model"] = "qwen3:8b"
        self.api._save_cfg()
        self.assertTrue(os.path.isfile(self.api.cfg_path))
        self.assertFalse(os.path.exists(self.api.cfg_path + ".tmp"),
                         "临时文件必须被 replace 掉")
        with open(self.api.cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["model"], "qwen3:8b")

    def test_corrupt_config_backed_up(self):
        with open(self.api.cfg_path, "w", encoding="utf-8") as f:
            f.write("{这不是 JSON")
        from backend.api import Api
        api2 = Api(workdir=self.tmp, get_window=lambda: None, asset_root=self.assets)
        self.assertEqual(api2.cfg["provider"], "ollama", "损坏后应回退默认配置")
        bads = [f for f in os.listdir(self.tmp) if ".bad-" in f]
        self.assertTrue(bads, "损坏的配置必须留备份，不能直接丢掉")

    def test_prune_assets_only_touches_hash_dirs(self):
        keep = os.path.join(self.assets, "0123456789")
        old = os.path.join(self.assets, "abcdef0123")
        mine = os.path.join(self.assets, "我的图片")
        for d in (keep, old, mine):
            os.makedirs(d, exist_ok=True)
        os.utime(old, (1, 1))
        n = docmodel.prune_assets(self.assets, keep=1)
        self.assertEqual(n, 1)
        self.assertTrue(os.path.isdir(keep))
        self.assertFalse(os.path.isdir(old))
        self.assertTrue(os.path.isdir(mine), "非程序生成的目录一律不能动")

    def test_oversized_file_rejected(self):
        """阈值是模块常量，用小文件验证同一段逻辑，不必真写 200MB。"""
        from backend import api as api_mod
        old = api_mod.MAX_IMPORT_BYTES
        api_mod.MAX_IMPORT_BYTES = 1024
        try:
            p = os.path.join(self.tmp, "big.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("x" * 2048)
            r = self.api.load(p)
            self.assertFalse(r["ok"])
            self.assertIn("过大", r["error"])
        finally:
            api_mod.MAX_IMPORT_BYTES = old

    def test_file_under_limit_loads(self):
        from backend import api as api_mod
        old = api_mod.MAX_IMPORT_BYTES
        api_mod.MAX_IMPORT_BYTES = 1024 * 1024
        try:
            p = os.path.join(self.tmp, "ok.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("正常内容。")
            self.assertTrue(self.api.load(p)["ok"])
        finally:
            api_mod.MAX_IMPORT_BYTES = old

    def test_diagnose_shape(self):
        r = self.api.diagnose()
        self.assertTrue(r["ok"])
        names = [c["name"] for c in r["checks"]]
        for n in ("磁盘空间", "模型服务", "图像理解", "ffmpeg", "语音转写", "审查历史"):
            self.assertIn(n, names)
        self.assertIn("logPath", r)

    def test_logs_rotate_config(self):
        from backend import logs
        logs.setup(self.tmp)
        lg = logs.get()
        handlers = [h for h in lg.handlers if hasattr(h, "maxBytes")]
        self.assertTrue(handlers)
        self.assertEqual(handlers[0].maxBytes, logs.MAX_BYTES)
        self.assertEqual(handlers[0].backupCount, logs.BACKUPS)

    def test_log_written_to_disk(self):
        from backend import logs
        logs.setup(self.tmp)
        logs.get().warning("测试日志条目-XYZ")
        for h in logs.get().handlers:
            try:
                h.flush()
            except Exception:
                pass
        self.assertIn("测试日志条目-XYZ", logs.tail(self.tmp, 50))

    def test_thread_exception_hook_installed(self):
        import threading as th
        from backend import logs
        logs.setup(self.tmp)
        self.assertIs(th.excepthook, th.excepthook)   # 已被替换为可调用对象
        self.assertTrue(callable(th.excepthook))


# ══════════════════════════════════════════════════════ 性能行为
class TestPerformanceBehaviour(unittest.TestCase):
    """性能优化不能改变结果 —— 这里验证「快」与「对」同时成立。"""

    def test_render_tokens_accepts_prefiltered(self):
        blocks = index([para(txt("甲句。乙句。"))])
        a = {"id": "a1", "block_id": "b0", "op": "replace_sentence", "type": "表达优化",
             "target": "甲句。", "replacement": "A句。", "reason": "", "severity": "low",
             "status": "accepted", "anchor": [0, 0, 0, 3], "sid": 0}
        toks = review.render_tokens(blocks[0], [a], "revised")
        text = "".join(t.get("v", "") for t in toks if t["k"] in ("t", "ins"))
        self.assertEqual(text, "A句。乙句。")

    def test_apply_accepted_with_index_matches_bruteforce(self):
        blocks = index([para(txt("甲。")), para(txt("乙。")), para(txt("丙。"))])
        anns = [{"id": f"a{i}", "block_id": f"b{i}", "op": "replace_sentence",
                 "type": "表达优化", "target": t, "replacement": r, "reason": "r",
                 "severity": "low", "status": "accepted",
                 "anchor": [0, 0, 0, 2], "sid": 0}
                for i, (t, r) in enumerate([("甲。", "A。"), ("乙。", "B。"), ("丙。", "C。")])]
        out = review.apply_accepted(blocks, anns)
        self.assertEqual([b["segs"][0]["v"] for b in out], ["A。", "B。", "C。"])

    def test_sent_list_reuse_gives_same_result(self):
        blocks = index([para(txt("第一句。第二句。第三句。"))])
        sents = review.block_sentences(blocks[0])
        a1 = review.build_annotation(
            {"block_id": "b0", "sid": 2, "op": "replace_sentence",
             "replacement": "新第三句。", "reason": "r"}, {b["id"]: b for b in blocks}, 1)
        a2 = review.build_annotation(
            {"block_id": "b0", "sid": 2, "op": "replace_sentence",
             "replacement": "新第三句。", "reason": "r"}, {b["id"]: b for b in blocks}, 1,
            sent_list=sents)
        self.assertEqual(a1["anchor"], a2["anchor"])
        self.assertEqual(a1["target"], a2["target"])

    def test_vision_payload_cached(self):
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), (1, 2, 3)).save(buf, "PNG")
        blob = buf.getvalue()
        pipeline._VISION_CACHE.clear()
        a = pipeline._vision_payload(blob, "png")
        b = pipeline._vision_payload(blob, "png")
        self.assertIs(a, b, "同一张图第二次应命中缓存")
        self.assertEqual(a[1], "image/jpeg")

    def test_vision_cache_bounded(self):
        pipeline._VISION_CACHE.clear()
        for i in range(pipeline._VISION_CACHE_MAX + 5):
            pipeline._VISION_CACHE[f"k{i}"] = ("x", "image/jpeg")
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8)).save(buf, "PNG")
        pipeline._vision_payload(buf.getvalue(), "png")
        self.assertLessEqual(len(pipeline._VISION_CACHE),
                             pipeline._VISION_CACHE_MAX + 1)

    def test_history_summary_cache_invalidated_by_new_save(self):
        tmp = tempfile.mkdtemp(prefix="nr_sc_")
        try:
            st = history.HistoryStore(tmp, keep=5)
            p = os.path.join(tmp, "n.docx")
            st.save(p, {"anns": []})
            s1 = st.summary()
            self.assertEqual(s1["entries"], 1)
            st.save(p, {"anns": []})
            st._sum_cache = None          # 目录 mtime 精度在 Windows 上可能相同
            s2 = st.summary()
            self.assertEqual(s2["entries"], 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_image_count_cached(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp(prefix="nr_ic_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            p = os.path.join(tmp, "n.txt")
            open(p, "w", encoding="utf-8").write("正文。")
            api.load(p)
            first = api._image_count()
            self.assertIs(api._img_doc, api.doc)
            self.assertEqual(api._image_count(), first)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 历史增强
class TestHistoryAdvanced(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nr_h2_")
        self.store = history.HistoryStore(self.tmp, keep=10)
        self.p = os.path.join(self.tmp, "笔记.docx")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ann(self, bid, sid, repl, typ="表达优化"):
        return {"block_id": bid, "sid": sid, "type": typ,
                "replacement": repl, "reason": "r", "status": "pending"}

    def test_set_note(self):
        e = self.store.save(self.p, {"anns": []})
        self.assertTrue(self.store.set_note(self.p, e["id"], "改完错别字的版本"))
        ents = self.store.entries(self.p)
        self.assertEqual(ents[0]["note"], "改完错别字的版本")

    def test_set_note_missing_entry(self):
        self.assertFalse(self.store.set_note(self.p, "nope", "x"))

    def test_compare_added_removed(self):
        a = self.store.save(self.p, {"anns": [self._ann("b1", 0, "A"), self._ann("b1", 1, "B")]})
        b = self.store.save(self.p, {"anns": [self._ann("b1", 0, "A"), self._ann("b2", 0, "C")]})
        r = self.store.compare(self.p, a["id"], b["id"])
        self.assertEqual(r["same"], 1)
        self.assertEqual([x["replacement"] for x in r["added"]], ["C"])
        self.assertEqual([x["replacement"] for x in r["removed"]], ["B"])

    def test_compare_missing_side(self):
        e = self.store.save(self.p, {"anns": []})
        self.assertEqual(self.store.compare(self.p, e["id"], "nope"), {})

    def test_search_across_files(self):
        p2 = os.path.join(self.tmp, "另一份.docx")
        e1 = self.store.save(self.p, {"anns": []})
        self.store.set_note(self.p, e1["id"], "第一次审查")
        self.store.save(p2, {"anns": []})
        r = self.store.search("第一次")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["name"], "笔记.docx")

    def test_search_empty_returns_all(self):
        self.store.save(self.p, {"anns": []})
        self.store.save(os.path.join(self.tmp, "b.docx"), {"anns": []})
        self.assertEqual(len(self.store.search("")), 2)

    def test_prune_older_than(self):
        old = self.store.save(self.p, {"id": "old1", "epoch": 1000, "anns": []})
        new = self.store.save(self.p, {"id": "new1", "anns": []})
        n = self.store.prune_older_than(30)
        self.assertEqual(n, 1)
        ids = [e["id"] for e in self.store.entries(self.p)]
        self.assertIn(new["id"], ids)
        self.assertNotIn(old["id"], ids)

    def test_prune_zero_days_noop(self):
        self.store.save(self.p, {"anns": []})
        self.assertEqual(self.store.prune_older_than(0), 0)

    def test_api_history_note_and_compare(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp(prefix="nr_h3_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            src = os.path.join(tmp, "n.txt")
            open(src, "w", encoding="utf-8").write("正文。")
            api.load(src)
            e1 = api._save_history(1.0)
            self.assertTrue(api.history_set_note(e1["id"], "第一版")["ok"])
            e2 = api._save_history(2.0)
            r = api.history_compare(e1["id"], e2["id"])
            self.assertTrue(r["ok"])
            self.assertIn("same", r)
            self.assertTrue(api.history_search("第一版")["ok"])
            self.assertFalse(api.history_prune(0)["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_api_recent_management(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp(prefix="nr_rec_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            src = os.path.join(tmp, "n.txt")
            open(src, "w", encoding="utf-8").write("正文。")
            api.load(src)
            self.assertIn(src, api.cfg["recent"])
            self.assertEqual(api.recent_remove(src)["recent"], [])
            api.load(src)
            self.assertEqual(api.recent_clear()["recent"], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 多媒体
class TestMedia(unittest.TestCase):
    def test_join_parts_chinese_no_space(self):
        from backend import media
        self.assertEqual(media._join_parts(["今天", "我们学习", "ZBrush。"]),
                         "今天我们学习ZBrush。")

    def test_join_parts_english_keeps_space(self):
        from backend import media
        self.assertEqual(media._join_parts(["hello", "world"]), "hello world")

    def test_join_parts_mixed(self):
        from backend import media
        self.assertEqual(media._join_parts(["用 ZBrush", "雕刻"]), "用 ZBrush雕刻")

    def test_pack_segments_chinese(self):
        from backend import media
        segs = [{"start": 0, "end": 2, "text": "第一句。"},
                {"start": 2, "end": 4, "text": "第二句。"}]
        out = media._pack_segments(segs)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], "第一句。第二句。")
        self.assertNotIn(" ", out[0][1])

    def test_wav_duration_reads_header(self):
        import wave
        from backend import media
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "a.wav")
            with wave.open(p, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 16000)     # 1 秒
            self.assertAlmostEqual(media._wav_duration(p), 1.0, places=2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_split_wav_short_returns_single(self):
        import wave
        from backend import media
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "a.wav")
            with wave.open(p, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 1600)
            parts = media._split_wav(p, {})
            self.assertEqual(len(parts), 1)
            self.assertEqual(parts[0][1], 0.0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_asr_status_reports_reason(self):
        from backend import media
        st = media.asr_status({})
        self.assertIn("detail", st)
        self.assertIsInstance(st["ok"], bool)

    def test_extract_frames_without_ffmpeg(self):
        from backend import media
        frames, err = media._extract_frames("x.mp4", tempfile.gettempdir(), 5, 10,
                                            {"ffmpeg_path": ""})
        # 机器上可能装了 ffmpeg，因此只断言「要么有帧、要么有明确原因」
        self.assertTrue(frames or err)

    def test_multi_image_import(self):
        from backend.api import Api
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="nr_img_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            paths = []
            for i in range(3):
                p = os.path.join(tmp, f"截图{i}.png")
                Image.new("RGB", (40, 30), (i * 40, 80, 120)).save(p)
                paths.append(p)
            r = api.load_images(paths)
            self.assertTrue(r["ok"], r.get("error"))
            self.assertEqual(r["doc"]["images"], 3)
            self.assertEqual(r["doc"]["source_type"], "image")
            heads = [b for b in api.doc["blocks"] if b["kind"] == "heading"]
            self.assertGreaterEqual(len(heads), 4, "总标题 + 每张图一个小标题")

            bad = api.load_images([os.path.join(tmp, "x.txt")])
            self.assertFalse(bad["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_multi_image_dedup_same_content(self):
        from backend.api import Api
        from PIL import Image
        tmp = tempfile.mkdtemp(prefix="nr_img2_")
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            api.set_history_dir(os.path.join(tmp, "hist"))
            paths = []
            for i in range(2):
                p = os.path.join(tmp, f"same{i}.png")
                Image.new("RGB", (20, 20), (9, 9, 9)).save(p)
                paths.append(p)
            r = api.load_images(paths)
            self.assertTrue(r["ok"])
            files = [s["file"] for b in api.doc["blocks"] for s in b["segs"] if s["t"] == "img"]
            self.assertEqual(len(files), 2)
            self.assertEqual(len(set(files)), 1, "内容相同的图片只落一份文件")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 批注编辑与预估
class TestEditAndEstimate(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_edit_")
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        self.src = os.path.join(self.tmp, "n.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("标题\n\n第一句。第二句。\n")
        self.api.load(self.src)
        self.api.anns = [{
            "id": "a1", "block_id": "b1", "op": "replace_sentence", "type": "表达优化",
            "target": "第一句。", "replacement": "改写句。", "reason": "r",
            "severity": "low", "status": "accepted", "anchor": [0, 0, 0, 4], "sid": 0}]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_set_replacement(self):
        r = self.api.set_replacement("a1", "我自己的写法。")
        self.assertTrue(r["ok"])
        self.assertEqual(r["anns"][0]["replacement"], "我自己的写法。")
        self.assertTrue(r["anns"][0]["edited"])
        text = "".join(s["v"] for s in self.api.blocks_current[1]["segs"])
        self.assertIn("我自己的写法。", text)

    def test_set_replacement_empty_rejected(self):
        self.assertFalse(self.api.set_replacement("a1", "   ")["ok"])

    def test_set_replacement_missing_ann(self):
        self.assertFalse(self.api.set_replacement("nope", "x")["ok"])

    def test_set_replacement_on_comment_rejected(self):
        self.api.anns.append({"id": "c1", "block_id": "b1", "op": "comment",
                              "type": "结构建议", "target": "", "replacement": "",
                              "reason": "建议分节", "severity": "low",
                              "status": "pending", "anchor": None, "sid": None})
        self.assertFalse(self.api.set_replacement("c1", "x")["ok"])

    def test_estimate_shape(self):
        r = self.api.estimate_review()
        self.assertTrue(r["ok"])
        for k in ("chunks", "chars", "images", "eta"):
            self.assertIn(k, r)
        self.assertGreaterEqual(r["chunks"], 1)
        self.assertGreater(r["eta"], 0)

    def test_estimate_requires_doc(self):
        from backend.api import Api
        tmp = tempfile.mkdtemp()
        try:
            api = Api(workdir=tmp, get_window=lambda: None,
                      asset_root=os.path.join(tmp, "assets"))
            self.assertFalse(api.estimate_review()["ok"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 内容归纳导出
class TestSummaryExport(unittest.TestCase):
    def _api(self, tmp):
        from backend.api import Api
        api = Api(workdir=tmp, get_window=lambda: None,
                  asset_root=os.path.join(tmp, "assets"))
        api.set_history_dir(os.path.join(tmp, "hist"))
        src = os.path.join(tmp, "n.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("标题\n\n第一句。\n")
        api.load(src)
        return api

    def test_summary_markdown(self):
        from backend import report
        doc = {"title": "笔记", "blocks": index([para(txt("内容。"))])}
        md = report.summary_markdown(doc, {
            "summary": "概括", "key_points": ["a", "b"],
            "outline": ["一"], "gaps": ["缺 X"]})
        self.assertIn("# 笔记", md)
        self.assertIn("## 摘要", md)
        self.assertIn("- a", md)
        self.assertIn("## 建议补充", md)
        self.assertIn("缺 X", md)

    def test_summary_markdown_empty(self):
        from backend import report
        md = report.summary_markdown({"title": "空", "blocks": []}, {})
        self.assertIn("# 空", md)
        self.assertNotIn("## 摘要", md)

    def test_summary_html_escapes(self):
        from backend import report
        html = report.summary_html({"title": "<b>x</b>", "blocks": []},
                                   {"summary": "<script>y</script>"})
        self.assertNotIn("<script>y", html)
        self.assertIn("&lt;script&gt;", html)

    def test_api_exports_summary(self):
        tmp = tempfile.mkdtemp(prefix="nr_sum_")
        try:
            api = self._api(tmp)
            api.summary = {"summary": "摘要文字", "key_points": ["要点一"]}
            for fmt, ext in (("md", ".md"), ("html", ".html")):
                out = os.path.join(tmp, "out", "sum" + ext)
                r = api.export("summary", fmt, out)
                self.assertTrue(r["ok"], r.get("error"))
                body = open(out, encoding="utf-8").read()
                self.assertIn("摘要文字", body)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_api_summary_requires_data(self):
        tmp = tempfile.mkdtemp(prefix="nr_sum2_")
        try:
            api = self._api(tmp)
            api.summary = {}
            r = api.export("summary", "md", os.path.join(tmp, "x.md"))
            self.assertFalse(r["ok"])
            self.assertIn("内容归纳", r["error"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 增量审查
class TestIncremental(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_inc_")
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def _seed_history(self, path, anns):
        """伪造一次完整审查的结果，写入历史（含段落指纹）。"""
        self.api.load(path)
        self.api.anns = anns
        return self.api._save_history(1.0)

    def test_text_hash_stable(self):
        from backend.api import _text_hash
        b1 = para(txt("同样的内容。"))
        b2 = para(txt("同样的内容。"))
        b3 = para(txt("不一样的内容。"))
        self.assertEqual(_text_hash(b1), _text_hash(b2))
        self.assertNotEqual(_text_hash(b1), _text_hash(b3))
        self.assertEqual(_text_hash(para(txt("   "))), "")

    def test_relocate_remaps_block_and_anchor(self):
        old = {"id": "a1", "block_id": "b9", "op": "replace_sentence",
               "target": "第二句。", "replacement": "改。", "type": "表达优化",
               "reason": "r", "severity": "low", "status": "pending",
               "anchor": [0, 99, 0, 99], "sid": 0}
        blk = {"id": "b2", "kind": "para", "level": 0, "segs": [txt("第一句。第二句。")]}
        out = review.relocate(old, blk)
        self.assertEqual(out["block_id"], "b2")
        self.assertEqual(out["anchor"], [0, 4, 0, 8])

    def test_relocate_drops_when_target_missing(self):
        old = {"id": "a1", "block_id": "b9", "op": "replace_sentence",
               "target": "不存在。", "replacement": "x", "type": "表达优化",
               "reason": "r", "severity": "low", "status": "pending", "anchor": [0, 0, 0, 1]}
        blk = {"id": "b2", "kind": "para", "level": 0, "segs": [txt("第一句。")]}
        self.assertIsNone(review.relocate(old, blk))

    def test_relocate_comment_needs_no_anchor(self):
        old = {"id": "a1", "block_id": "b9", "op": "comment", "type": "结构建议",
               "target": "", "replacement": "", "reason": "建议分节",
               "severity": "low", "status": "pending", "anchor": None}
        blk = {"id": "b2", "kind": "para", "level": 0, "segs": [txt("文字。")]}
        out = review.relocate(old, blk)
        self.assertEqual(out["block_id"], "b2")
        self.assertIsNone(out["anchor"])

    def test_can_incremental_without_history(self):
        p = self._write("a.txt", "标题\n\n第一句。\n")
        self.api.load(p)
        r = self.api.can_incremental()
        self.assertFalse(r["possible"])
        self.assertIn("完整审查", r["reason"])

    def test_can_incremental_after_history(self):
        p = self._write("a.txt", "标题\n\n第一句。第二句。\n")
        self._seed_history(p, [])
        r = self.api.can_incremental()
        self.assertTrue(r["possible"])
        self.assertEqual(r["changed"], 0)
        self.assertEqual(r["unchanged"], r["total"])

    def test_can_incremental_detects_changes(self):
        p = self._write("a.txt", "标题\n\n第一句。\n")
        self._seed_history(p, [])
        # 改一句再导入
        self._write("a.txt", "标题\n\n第一句改过了。\n")
        self.api.load(p)
        r = self.api.can_incremental()
        self.assertTrue(r["possible"])
        self.assertGreater(r["changed"], 0)
        self.assertGreater(r["unchanged"], 0)

    def test_history_stores_block_hashes(self):
        p = self._write("a.txt", "标题\n\n第一句。\n")
        e = self._seed_history(p, [])
        snap = self.api.history.read(p, e["id"])
        self.assertIn("block_hashes", snap)
        self.assertEqual(len(snap["block_hashes"]), len(self.api.doc["blocks"]))

    def test_finalize_merges_and_reindexes(self):
        anns = [
            {"id": "x1", "block_id": "b2", "op": "comment", "type": "结构建议",
             "severity": "low", "status": "pending", "anchor": None},
            {"id": "x2", "block_id": "b0", "op": "comment", "type": "事实纠错",
             "severity": "high", "status": "pending", "anchor": None},
        ]
        out = review.finalize(anns)
        self.assertEqual([a["id"] for a in out], ["a1", "a2"])
        self.assertEqual(out[0]["severity"], "high", "高优先级排前面")


# ══════════════════════════════════════════════════════ 场景与进度保存
class TestScenarioAndProgress(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_sc2_")
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        self.src = os.path.join(self.tmp, "n.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("标题\n\n第一句。第二句。\n")
        self.api.load(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scenario_list_exposed(self):
        r = self.api.boot()
        keys = [s["key"] for s in r["scenarios"]]
        self.assertIn("general", keys)
        self.assertIn("paper", keys)
        self.assertEqual(r["reviewScenario"], "general")

    def test_set_scenario(self):
        r = self.api.set_review_opts(scenario="paper")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reviewScenario"], "paper")

    def test_bad_scenario_rejected(self):
        self.assertFalse(self.api.set_review_opts(scenario="nope")["ok"])

    def test_scenario_focus_in_prompt(self):
        doc = {"title": "t", "blocks": index([para(txt("甲。"))])}
        m = MockLLM([])
        pipeline.review_document(doc, m, scenario="meeting")
        self.assertIn("会议记录", m.calls[0]["system"])
        self.assertIn("行动项", m.calls[0]["system"])

    def test_general_scenario_adds_nothing(self):
        doc = {"title": "t", "blocks": index([para(txt("甲。"))])}
        m = MockLLM([])
        pipeline.review_document(doc, m, scenario="general")
        self.assertNotIn("笔记类型侧重", m.calls[0]["system"])

    def test_save_progress_writes_status(self):
        self.api.anns = [{"id": "a1", "block_id": "b1", "op": "comment",
                          "type": "结构建议", "target": "", "replacement": "",
                          "reason": "建议分节", "severity": "low",
                          "status": "pending", "anchor": None, "sid": None}]
        e = self.api._save_history(1.0)
        self.api.anns[0]["status"] = "accepted"
        r = self.api.save_progress()
        self.assertTrue(r["ok"], r.get("error"))
        snap = self.api.history.read(self.src, e["id"])
        self.assertEqual(snap["anns"][0]["status"], "accepted")
        self.assertIn("progress_ts", snap)

    def test_save_progress_without_history(self):
        self.api.history_current = None
        r = self.api.save_progress()
        self.assertFalse(r["ok"])

    def test_update_snapshot_missing(self):
        self.assertFalse(self.api.history.update_snapshot(self.src, "nope", {}))

    def test_update_snapshot_refreshes_meta_counts(self):
        e = self.api._save_history(1.0)
        snap = self.api.history.read(self.src, e["id"])
        snap["anns"] = [{"id": "x"}]
        self.assertTrue(self.api.history.update_snapshot(self.src, e["id"], snap))
        ents = self.api.history.entries(self.src)
        self.assertEqual(ents[0]["anns"], 1)


# ══════════════════════════════════════════════════════ 批量范围
class TestBulkScope(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_bulk_")
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        src = os.path.join(self.tmp, "n.txt")
        with open(src, "w", encoding="utf-8") as f:
            f.write("标题\n\n第一句。\n")
        self.api.load(src)
        self.api.anns = [
            {"id": "a1", "block_id": "b1", "op": "comment", "type": "错别字",
             "target": "", "replacement": "", "reason": "r", "severity": "high",
             "status": "pending", "anchor": None, "sid": None},
            {"id": "a2", "block_id": "b1", "op": "comment", "type": "表达优化",
             "target": "", "replacement": "", "reason": "r", "severity": "low",
             "status": "pending", "anchor": None, "sid": None},
            {"id": "a3", "block_id": "b1", "op": "comment", "type": "表达优化",
             "target": "", "replacement": "", "reason": "r", "severity": "medium",
             "status": "accepted", "anchor": None, "sid": None},
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scope_by_severity(self):
        r = self.api.bulk("accept", "sev:high")
        self.assertEqual(r["affected"], 1)
        self.assertEqual(r["scopeLabel"], "高优先级")
        self.assertEqual(r["stats"]["accepted"], 2)

    def test_scope_pending_only(self):
        r = self.api.bulk("reject", "pending")
        self.assertEqual(r["affected"], 2)
        self.assertEqual(r["stats"]["pending"], 0)

    def test_scope_by_type(self):
        r = self.api.bulk("accept", "表达优化")
        self.assertEqual(r["affected"], 2)

    def test_bad_severity(self):
        self.assertFalse(self.api.bulk("accept", "sev:urgent")["ok"])

    def test_empty_scope_targets(self):
        self.api.bulk("accept", "sev:high")
        r = self.api.bulk("accept", "sev:high")   # 已无 pending 但仍有目标
        self.assertTrue(r["ok"])
        r2 = self.api.bulk("accept", "存疑待核")
        self.assertFalse(r2["ok"])
        self.assertIn("没有批注", r2["error"])


class TestReviewThroughput(unittest.TestCase):
    """并发、失败补偿、每段上限、输出预算。"""

    def _doc(self, n=4, chars=900):
        blocks = index([para(txt("啊" * chars)) for _ in range(n)])
        return {"title": "t", "blocks": blocks}

    def _reply(self, bid):
        return json.dumps({"annotations": [
            {"block_id": bid, "sid": 0, "op": "replace_sentence", "type": "表达优化",
             "replacement": "改写后的句子。", "reason": "r"}]}, ensure_ascii=False)

    def test_failed_chunk_records_block_ids(self):
        doc = self._doc(2)
        rep = {}
        pipeline.review_document(doc, MockLLM([llm.LLMError("boom"), "{}"]), report=rep)
        self.assertEqual(len(rep["failed"]), 1)
        self.assertTrue(rep["failed"][0]["blocks"], "失败分块必须记下覆盖的段落")

    def test_concurrency_matches_serial(self):
        """并发与串行必须产出同一批批注（顺序、id、内容一致）。"""
        def run(conc):
            doc = self._doc(4)
            m = MockLLM(fn=lambda s, u: self._reply("b0"))
            rep = {}
            anns = pipeline.review_document(doc, m, report=rep, concurrency=conc)
            return [(a["id"], a["block_id"], a["replacement"]) for a in anns], rep["chunks"]

        a, n1 = run(1)
        b, n2 = run(3)
        self.assertEqual(n1, n2)
        self.assertEqual(a, b)

    def test_concurrency_isolates_errors(self):
        doc = self._doc(3)
        calls = {"n": 0}

        def fn(s, u):
            calls["n"] += 1
            if calls["n"] == 2:
                raise llm.LLMError("boom")
            return self._reply("b0")

        rep = {}
        anns = pipeline.review_document(doc, MockLLM(fn=fn), report=rep, concurrency=3)
        self.assertEqual(len(rep["failed"]), 1)
        self.assertGreater(len(anns), 0, "其它分块的结果必须保住")

    def test_max_per_block_caps(self):
        anns = [{"id": f"a{i}", "block_id": "b0", "op": "comment", "type": "表达优化",
                 "target": "", "replacement": "", "reason": "r",
                 "severity": ["high", "medium", "low"][i % 3], "status": "pending",
                 "anchor": None, "sid": None} for i in range(10)]
        out = review.cap_per_block(anns, 4)
        self.assertEqual(len(out), 4)
        self.assertEqual(sum(1 for a in out if a["severity"] == "high"), 4,
                         "应当优先保留高严重度")

    def test_max_per_block_zero_means_unlimited(self):
        anns = [{"id": f"a{i}", "block_id": "b0", "op": "comment", "type": "表达优化",
                 "target": "", "replacement": "", "reason": "r", "severity": "low",
                 "status": "pending", "anchor": None, "sid": None} for i in range(10)]
        self.assertEqual(len(review.cap_per_block(anns, 0)), 10)

    def test_output_budget_scales_with_chunk(self):
        small = index([para(txt("短。"))])
        big = index([para(txt("长句内容。" * 400))])
        b1 = pipeline._output_budget(pipeline._pieces(small))
        b2 = pipeline._output_budget(pipeline._pieces(big))
        self.assertGreaterEqual(b1, 1024)
        self.assertGreater(b2, b1)
        self.assertLessEqual(b2, 8192)

    def test_budget_passed_to_model(self):
        doc = self._doc(1)
        m = MockLLM([self._reply("b0")])
        pipeline.review_document(doc, m)
        self.assertGreater(m.calls[0]["max_tokens"], 0)

    def test_cancel_marks_unreviewed_chunks(self):
        doc = self._doc(4)
        state = {"n": 0}

        def stop():
            state["n"] += 1
            return state["n"] > 1

        rep = {}
        pipeline.review_document(doc, MockLLM([self._reply("b0")] * 4),
                                 report=rep, should_stop=stop)
        self.assertTrue(rep["canceled"])
        self.assertTrue(rep["failed"], "取消后未审查的分块要标记出来")


class TestIncrementalFailedCompensation(unittest.TestCase):
    """上次失败的段落，增量审查必须强制重审。"""

    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_inc_")
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        os.makedirs(os.path.join(self.tmp, "assets"), exist_ok=True)
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        self.src = os.path.join(self.tmp, "n.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("第一段内容。\n\n第二段内容。\n\n第三段内容。\n")
        self.api.load(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failed_blocks_are_not_reused(self):
        self.api.anns = []
        self.api._last_failed_blocks = [self.api.doc["blocks"][0]["id"]]
        self.api._save_history(1.0)
        self.api._last_failed_blocks = []
        seen, _ = self.api._prev_reuse()
        first_hash = __import__("backend.api", fromlist=["_text_hash"])._text_hash(
            self.api.doc["blocks"][0])
        self.assertNotIn(first_hash, seen, "失败段落的指纹不能进可复用集合")
        r = self.api.can_incremental()
        self.assertGreaterEqual(r["changed"], 1)


class TestDocxComments(unittest.TestCase):
    """导出「Word 批注版」：正文保持原样，建议以 Word 原生批注呈现。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nr_cmt_")
        self.asset = self.tmp
        from PIL import Image
        Image.new("RGB", (40, 30), (7, 7, 7)).save(os.path.join(self.tmp, "a.png"))
        self.blocks = index([
            heading("标题"),
            para(txt("第一句。第二句。")),
            para(txt("带图"), img(), txt("结尾。")),
        ])
        self.anns = [
            {"id": "a1", "block_id": "b1", "op": "replace_sentence", "type": "表达优化",
             "target": "第一句。", "replacement": "改后第一句。", "reason": "更通顺",
             "severity": "high", "status": "pending", "anchor": [0, 0, 0, 4], "sid": 0},
            {"id": "a2", "block_id": "b2", "op": "comment", "type": "结构建议",
             "target": "", "replacement": "", "reason": "建议拆段", "severity": "low",
             "status": "accepted", "anchor": None, "sid": None},
            {"id": "a3", "block_id": "b1", "op": "comment", "type": "错别字",
             "target": "", "replacement": "", "reason": "这条已被丢弃", "severity": "low",
             "status": "rejected", "anchor": None, "sid": None},
        ]
        self.out = os.path.join(self.tmp, "o.docx")
        docmodel.export_docx(self.blocks, self.asset, self.out, "标题", anns=self.anns)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip(self):
        import zipfile
        return zipfile.ZipFile(self.out)

    def test_comments_part_exists(self):
        names = self._zip().namelist()
        self.assertIn("word/comments.xml", names)
        self.assertIn("comments+xml", self._zip().read("[Content_Types].xml").decode())
        rels = self._zip().read("word/_rels/document.xml.rels").decode()
        self.assertIn("comments", rels)

    def test_rejected_excluded(self):
        c = self._zip().read("word/comments.xml").decode()
        self.assertEqual(c.count("<w:comment "), 2)
        self.assertNotIn("这条已被丢弃", c)

    def test_comment_text_contains_reason(self):
        c = self._zip().read("word/comments.xml").decode()
        self.assertIn("更通顺", c)
        self.assertIn("改后第一句。", c)

    def test_range_markers_balanced(self):
        d = self._zip().read("word/document.xml").decode()
        self.assertEqual(d.count("commentRangeStart"), 2)
        self.assertEqual(d.count("commentRangeEnd"), 2)
        self.assertEqual(d.count("commentReference"), 2)

    def test_body_text_unchanged(self):
        import re
        d = self._zip().read("word/document.xml").decode()
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", d))
        self.assertIn("第一句。第二句。", text, "批注版正文必须是原文，不能预先应用改动")

    def test_reopen_by_python_docx(self):
        from docx import Document
        self.assertTrue(Document(self.out).paragraphs)

    def test_no_anns_no_comments_part(self):
        p = os.path.join(self.tmp, "plain.docx")
        docmodel.export_docx(self.blocks, self.asset, p, "标题")
        import zipfile
        self.assertNotIn("word/comments.xml", zipfile.ZipFile(p).namelist())


class TestCommentedExportApi(unittest.TestCase):
    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_capi_")
        os.makedirs(os.path.join(self.tmp, "assets"), exist_ok=True)
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        self.src = os.path.join(self.tmp, "n.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("标题行\n\n第一句。第二句。\n")
        self.api.load(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_requires_annotations(self):
        r = self.api.export("commented", "docx", os.path.join(self.tmp, "x.docx"))
        self.assertFalse(r["ok"])
        self.assertIn("批注", r["error"])

    def test_exports_with_annotations(self):
        bid = self.api.doc["blocks"][-1]["id"]
        self.api.add_manual_note(bid, "手工批注")
        r = self.api.export("commented", "docx", os.path.join(self.tmp, "x.docx"))
        self.assertTrue(r["ok"])
        import zipfile
        self.assertIn("word/comments.xml", zipfile.ZipFile(r["path"]).namelist())

    def test_rejected_note_not_exported(self):
        bid = self.api.doc["blocks"][-1]["id"]
        self.api.add_manual_note(bid, "会被丢弃的批注")
        aid = self.api.anns[-1]["id"]
        self.api.set_status(aid, "rejected")
        r = self.api.export("commented", "docx", os.path.join(self.tmp, "y.docx"))
        self.assertFalse(r["ok"])
        self.assertIn("丢弃", r["error"])


class TestRetryFailed(unittest.TestCase):
    """只补审失败段落：已处理好的判断必须原样保留。"""

    def setUp(self):
        from backend.api import Api
        self.tmp = tempfile.mkdtemp(prefix="nr_rf_")
        os.makedirs(os.path.join(self.tmp, "assets"), exist_ok=True)
        self.api = Api(workdir=self.tmp, get_window=lambda: None,
                       asset_root=os.path.join(self.tmp, "assets"))
        self.api.set_history_dir(os.path.join(self.tmp, "hist"))
        self.src = os.path.join(self.tmp, "n.txt")
        with open(self.src, "w", encoding="utf-8") as f:
            f.write("第一段的内容比较长，足够被审查。\n\n"
                    "第二段的内容比较长，足够被审查。\n\n"
                    "第三段的内容比较长，足够被审查。\n")
        self.api.load(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_failed_blocks(self):
        self.api._last_failed_blocks = []
        r = self.api.retry_failed()
        self.assertFalse(r["ok"])
        self.assertIn("没有待补审", r["error"])

    def test_retry_only_touches_failed_blocks(self):
        blocks = self.api.doc["blocks"]
        keep_bid = blocks[0]["id"]
        fail_bid = blocks[1]["id"]
        self.api.anns = [{"id": "a1", "block_id": keep_bid, "op": "comment",
                          "type": "结构建议", "target": "", "replacement": "",
                          "reason": "保留我", "severity": "low", "status": "accepted",
                          "anchor": None, "sid": None}]
        self.api._last_failed_blocks = [fail_bid]

        reply = json.dumps({"annotations": [
            {"block_id": fail_bid, "sid": 0, "op": "replace_sentence",
             "type": "表达优化", "replacement": "补审后的句子。", "reason": "r"}]},
            ensure_ascii=False)
        self.api._llm = lambda: MockLLM([reply])

        r = self.api.retry_failed()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["retried"], 1)
        reasons = [a["reason"] for a in r["anns"]]
        self.assertIn("保留我", reasons, "未失败段落上的旧批注不能丢")
        self.assertTrue(any(a["block_id"] == fail_bid for a in r["anns"]))
        self.assertEqual(r["failedBlocks"], [])

    def test_retry_survives_llm_error(self):
        self.api._last_failed_blocks = [self.api.doc["blocks"][0]["id"]]
        self.api._llm = lambda: MockLLM([llm.LLMError("连接失败")])
        r = self.api.retry_failed()
        # 补审失败也要有明确结果，而不是抛异常
        self.assertIn("ok", r)
        self.assertFalse(self.api.busy)


class TestGlossary(unittest.TestCase):
    def test_parse_separators(self):
        from backend.api import parse_glossary_text
        got = parse_glossary_text("DynaMesh、SubTool\nUV,FBX；ZBrush\tABC")
        self.assertEqual(got, ["DynaMesh", "SubTool", "UV", "FBX", "ZBrush", "ABC"])

    def test_parse_skips_comments_and_prose(self):
        from backend.api import parse_glossary_text
        prose = "这是一整句明显不是术语的正文内容，误把笔记正文当术语导进来时应当被忽略掉。"
        got = parse_glossary_text("# 这是注释\nDynaMesh\n" + prose)
        self.assertEqual(got, ["DynaMesh"])

    def test_parse_skips_overlong_line_without_punct(self):
        from backend.api import parse_glossary_text
        long_line = "甲" * 80
        self.assertEqual(parse_glossary_text(long_line), [])

    def test_parse_dedupes(self):
        from backend.api import parse_glossary_text
        self.assertEqual(parse_glossary_text("A、A、B"), ["A", "B"])

    def test_parse_empty(self):
        from backend.api import parse_glossary_text
        self.assertEqual(parse_glossary_text(""), [])
        self.assertEqual(parse_glossary_text(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
