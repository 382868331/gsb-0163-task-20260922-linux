"""多模式流式查找的必要测试。

期望值来自 tests/reference.py 的独立朴素参考（整串规范化后滑窗匹配），
并对每个短输入枚举全部切块位置验证分块一致；往返一致仅作补充。
"""

from __future__ import annotations

import unittest

from canoncompose import (
    InvalidInputError,
    SegmentLimitError,
    StreamSearcher,
    find_all,
)
from canoncompose.samples import (
    A,
    E,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    M_RING,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    build_demo_table,
)
from canoncompose.table import DictCharacterTable

from reference import reference_normalize_points


def _merge_spans(points):
    """测试侧独立复算：来源点集合 -> 有序半开区间元组。"""
    spans = []
    for ci, off, _so in sorted(points):
        if spans and spans[-1][0] == ci and spans[-1][2] == off:
            spans[-1] = (ci, spans[-1][1], off + 1)
        else:
            spans.append((ci, off, off + 1))
    return tuple(spans)


def naive_search(chunks, patterns, table):
    """朴素参考：整串一次性规范化后滑窗匹配（不调用被测查找）。"""
    cps, bags = reference_normalize_points(chunks, table)
    expected = []
    for order, (pid, pat) in enumerate(patterns):
        pcps, _ = reference_normalize_points([pat], table)
        n = len(pcps)
        for i in range(len(cps) - n + 1):
            if cps[i:i + n] == pcps:
                pts = set()
                for bag in bags[i:i + n]:
                    pts |= bag
                expected.append((pid, i, i + n, _merge_spans(pts), order))
    expected.sort(key=lambda m: (m[2], m[1], m[4]))
    return [(pid, s, e, spans) for pid, s, e, spans, _o in expected]


def actual_search(chunks, patterns, table):
    return [
        (
            m.pattern_id,
            m.start,
            m.end,
            tuple((s.chunk_index, s.start, s.end) for s in m.source_spans),
        )
        for m in find_all(chunks, patterns, table)
    ]


def all_chunkings(text):
    """枚举一段短文本的全部切块位置组合。"""
    n = len(text)
    for mask in range(1 << max(0, n - 1)):
        chunks, last = [], 0
        for i in range(n - 1):
            if mask >> i & 1:
                chunks.append(text[last:i + 1])
                last = i + 1
        chunks.append(text[last:])
        yield chunks


class ExhaustiveChunkingTests(unittest.TestCase):
    """短输入逐个切块位置，与整串朴素匹配参考对比。"""

    def setUp(self):
        self.table = build_demo_table()

    def _check_all_chunkings(self, text, patterns):
        count = 0
        for chunks in all_chunkings(text):
            count += 1
            with self.subTest(chunks=chunks):
                self.assertEqual(
                    actual_search(chunks, patterns, self.table),
                    naive_search(chunks, patterns, self.table),
                )
        self.assertEqual(count, 1 << max(0, len(text) - 1))

    def test_recursive_decomposition_patterns(self):
        # 递归分解：预组合两级字符与 a+grave+ring 写法规范后相同。
        text = (
            chr(P_A_GRAVE_RING)
            + chr(A) + chr(M_GRAVE) + chr(M_RING)
            + "b"
        )
        patterns = [
            ("precomposed", chr(P_A_GRAVE_RING)),
            ("decomposed", chr(A) + chr(M_GRAVE) + chr(M_RING)),
            ("b", "b"),
        ]
        self._check_all_chunkings(text, patterns)

    def test_leading_combining_marks(self):
        # 前导组合符：流开头就是非 starter，构成不组合的前导段。
        text = chr(M_GRAVE) + chr(A) + chr(M_ACUTE) + chr(M_GRAVE)
        patterns = [
            ("lead", chr(M_GRAVE)),
            ("lead-a", chr(M_GRAVE) + chr(A)),
            ("a-acute", chr(A) + chr(M_ACUTE)),
            ("grave-grave", chr(M_GRAVE) + chr(A) + chr(M_ACUTE) + chr(M_GRAVE)),
        ]
        self._check_all_chunkings(text, patterns)

    def test_patterns_sharing_suffixes(self):
        # 多个模式共用后缀；其中两个模式规范后相同但 id 不同。
        text = "x" + chr(A) + chr(M_GRAVE) + chr(A) + chr(M_GRAVE)
        patterns = [
            ("x-tail", "x" + chr(A) + chr(M_GRAVE)),
            ("tail-marks", chr(A) + chr(M_GRAVE)),
            ("tail-precomposed", chr(P_A_GRAVE)),
            ("x", "x"),
        ]
        self._check_all_chunkings(text, patterns)

    def test_overlapping_matches(self):
        text = "aaaa"
        patterns = [("a", "a"), ("aa", "aa"), ("aaa", "aaa")]
        self._check_all_chunkings(text, patterns)

    def test_reordered_marks_and_composition(self):
        # ccc 重排 + 组合：dotbelow(220) 排到 grave(230) 前。
        text = (
            chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE)
            + chr(E) + chr(M_ACUTE)
        )
        patterns = [
            ("a-grave", chr(P_A_GRAVE)),
            ("dotbelow", chr(M_DOTBELOW)),
            ("e-acute", chr(E) + chr(M_ACUTE)),
            ("whole", text),
        ]
        self._check_all_chunkings(text, patterns)


class MatchSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_overlapping_match_positions(self):
        matches = find_all(["aaaa"], [("aa", "aa")], self.table)
        self.assertEqual(
            [(m.start, m.end) for m in matches],
            [(0, 2), (1, 3), (2, 4)],
        )

    def test_same_canonical_pattern_keeps_both_ids(self):
        patterns = [
            ("marks", chr(A) + chr(M_GRAVE)),
            ("precomposed", chr(P_A_GRAVE)),
        ]
        matches = find_all([chr(P_A_GRAVE)], patterns, self.table)
        self.assertEqual(
            sorted(m.pattern_id for m in matches),
            ["marks", "precomposed"],
        )
        for m in matches:
            self.assertEqual((m.start, m.end), (0, 1))

    def test_non_contiguous_source_spans(self):
        # a + dotbelow(220) + grave(230)：重排后 grave 与 a 组合，
        # 命中 à 的来源是输入偏移 0 和 2，不是连续的 [0,3)。
        text = chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE)
        matches = find_all([text], [("pg", chr(P_A_GRAVE))], self.table)
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual((m.start, m.end), (0, 1))
        self.assertEqual(
            tuple((s.chunk_index, s.start, s.end) for s in m.source_spans),
            ((0, 0, 1), (0, 2, 3)),
        )

    def test_cross_chunk_source_spans(self):
        # a 与 grave 分属不同 chunk，组合后的命中来源跨 chunk。
        matches = find_all(
            [chr(A), chr(M_GRAVE)], [("pg", chr(P_A_GRAVE))], self.table
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            tuple((s.chunk_index, s.start, s.end) for s in matches[0].source_spans),
            ((0, 0, 1), (1, 0, 1)),
        )

    def test_no_match_on_empty_text_or_no_chunks(self):
        patterns = [("a", "a")]
        self.assertEqual(find_all([""], patterns, self.table), ())
        self.assertEqual(find_all([], patterns, self.table), ())

    def test_empty_pattern_list_gives_no_matches(self):
        self.assertEqual(find_all(["abc"], [], self.table), ())


class StreamingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_unclosed_segment_not_finished_early(self):
        # 模式 à = a + grave。只喂 a 时段未闭合，不得交付命中；
        # 喂 grave 后段仍未闭合（后面可能还有记号），仍不得交付；
        # 遇到下一个 starter 时段闭合，命中才确认。
        ss = StreamSearcher(self.table, [("pg", chr(P_A_GRAVE))])
        self.assertEqual(ss.feed(chr(A)), ())
        self.assertEqual(ss.feed(chr(M_GRAVE)), ())
        delivered = ss.feed("b")
        self.assertEqual(len(delivered), 1)
        self.assertEqual((delivered[0].start, delivered[0].end), (0, 1))
        self.assertEqual(ss.finish(), ())  # 无新增、无重复

    def test_delivered_matches_never_retracted(self):
        text = "x" + chr(A) + chr(M_GRAVE) + "ya"
        patterns = [("tail", chr(A) + chr(M_GRAVE)), ("a", "a"), ("y", "y")]
        chunks_list = [[text], [text[:2], text[2:]], [c for c in text]]
        for chunks in chunks_list:
            ss = StreamSearcher(self.table, patterns)
            streamed = []
            for chunk in chunks:
                streamed.extend(ss.feed(chunk))
            streamed.extend(ss.finish())
            self.assertEqual(
                streamed, list(find_all(chunks, patterns, self.table))
            )

    def test_finish_twice_returns_empty(self):
        ss = StreamSearcher(self.table, [("a", "a")])
        ss.feed("a")
        first = ss.finish()
        self.assertEqual(len(first), 1)
        self.assertEqual(ss.finish(), ())

    def test_feed_after_finish_rejected(self):
        ss = StreamSearcher(self.table, [("a", "a")])
        ss.finish()
        with self.assertRaises(InvalidInputError):
            ss.feed("a")

    def test_failed_feed_is_atomic(self):
        ss = StreamSearcher(self.table, [("a", "a")])
        with self.assertRaises(InvalidInputError):
            ss.feed(123)  # 非 str chunk
        # 流状态未变，可继续正常使用。
        self.assertEqual(len(ss.feed("a")), 0)
        self.assertEqual(len(ss.finish()), 1)

    def test_segment_limit_error_is_atomic(self):
        marks = tuple(0xE500 + i for i in range(8))
        big = 0xE400
        table = DictCharacterTable(
            decompositions={big: (A, *marks)},
            combining_classes={m: 230 for m in marks},
        )
        ss = StreamSearcher(table, [("a", "a")], segment_limit=4)
        with self.assertRaises(SegmentLimitError):
            ss.feed(chr(big))
        # 失败的 feed 全部回滚，后续输入不受影响。
        self.assertEqual(ss.feed("a"), ())
        self.assertEqual(len(ss.finish()), 1)


class PatternValidationTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_empty_pattern_rejected(self):
        with self.assertRaises(InvalidInputError):
            find_all(["a"], [("empty", "")], self.table)

    def test_non_str_pattern_rejected(self):
        with self.assertRaises(InvalidInputError):
            find_all(["a"], [("bad", 123)], self.table)

    def test_duplicate_pattern_id_rejected(self):
        with self.assertRaises(InvalidInputError):
            find_all(["a"], [("p", "a"), ("p", "b")], self.table)

    def test_unhashable_pattern_id_rejected(self):
        with self.assertRaises(InvalidInputError):
            find_all(["a"], [(["p"], "a")], self.table)

    def test_patterns_must_be_pairs(self):
        with self.assertRaises(InvalidInputError):
            find_all(["a"], ["a"], self.table)
        with self.assertRaises(InvalidInputError):
            find_all(["a"], "a", self.table)

    def test_chunks_single_str_rejected(self):
        with self.assertRaises(InvalidInputError):
            find_all("abc", [("a", "a")], self.table)


if __name__ == "__main__":
    unittest.main()
