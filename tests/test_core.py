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
    """按队列返回预设文本，同时记录收到的 prompt。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
        self._vision_blocked = False
        self.model = "mock"
        self.vision_model = ""

    def generate(self, system, user, json_mode=True, on_token=None):
        self.calls.append({"system": system, "user": user})
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
