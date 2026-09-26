"""多模式流式查找的必要测试。

期望值来源：
- tests/reference.py 的独立朴素参考（不导入被测核心）上做逐起始位置
  朴素匹配，作为位置与来源区间的对照；
- 测试内手写的固定预期（前导/空模式/重叠等语义）。

短输入的全部切块位置逐个穷举：每个短文本枚举所有 2^(n-1) 种 chunk
划分，命中（规范位置 + 来源区间集合）必须与同划分下的朴素参考一致。
"""

from __future__ import annotations

import unittest

from canoncompose import (
    InvalidInputError,
    SearchHit,
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

from reference import reference_normalize_detailed


def _merge_spans(points):
    """测试内独立复算的来源点 -> 区间集合合并（不调用被测核心）。"""
    spans = []
    for ci, off, _so in sorted(set(points)):
        if spans and spans[-1][0] == ci and spans[-1][2] == off:
            spans[-1] = (ci, spans[-1][1], off + 1)
        else:
            spans.append((ci, off, off + 1))
    return tuple(spans)


def naive_find(chunks, table, patterns):
    """朴素参考：整串规范化后逐起始位置比对每个模式。

    返回与 find_all 可比对的元组列表
    (pattern_id, start, end, spans-as-tuples)，按 (起始位置, 模式注册序)。
    """
    cps, bags = reference_normalize_detailed(chunks, table)
    canonical_patterns = [
        (pid, reference_normalize_detailed([ptext], table)[0])
        for pid, ptext in patterns
    ]
    hits = []
    for i in range(len(cps)):
        for pid, pcps in canonical_patterns:
            length = len(pcps)
            if i + length <= len(cps) and tuple(cps[i:i + length]) == pcps:
                points = set()
                for bag in bags[i:i + length]:
                    points |= bag
                hits.append((pid, i, i + length, _merge_spans(points)))
    return hits


def _hit_tuple(hit: SearchHit):
    return (
        hit.pattern_id,
        hit.start,
        hit.end,
        tuple((s.chunk_index, s.start, s.end) for s in hit.source_spans),
    )


def _all_chunkings(text: str):
    """枚举 text 的全部 2^(n-1) 种 chunk 划分（每段非空）。"""
    n = len(text)
    for mask in range(1 << max(n - 1, 0)):
        chunks, prev = [], 0
        for i in range(1, n):
            if mask & (1 << (i - 1)):
                chunks.append(text[prev:i])
                prev = i
        chunks.append(text[prev:])
        yield chunks


# 覆盖：递归分解、前导组合符、ccc 重排、重叠候选、共享后缀场景。
SHORT_TEXTS = [
    "",
    chr(A),
    chr(M_GRAVE),
    chr(M_GRAVE) + chr(M_ACUTE) + chr(A),
    chr(A) + chr(M_GRAVE) + chr(M_RING),
    chr(P_A_GRAVE_RING) + chr(E),
    chr(A) + chr(M_GRAVE) + chr(A) + chr(M_GRAVE),
    chr(E) + chr(A) + chr(M_GRAVE) + chr(A) + chr(M_GRAVE),
    chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE),
    chr(M_GRAVE) + chr(A) + chr(M_GRAVE) + chr(M_RING),
]

# 多个模式共用后缀 (P_A_GRAVE,)；p_ag 与 p_pre 规范后完全相同。
PATTERNS = [
    ("p_ag", chr(A) + chr(M_GRAVE)),
    ("p_pre", chr(P_A_GRAVE)),
    ("p_ring", chr(P_A_GRAVE_RING)),
    ("p_eag", chr(E) + chr(A) + chr(M_GRAVE)),
    ("p_grave", chr(M_GRAVE)),
]


class ExhaustiveChunkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_all_chunkings_match_naive_reference(self) -> None:
        for text in SHORT_TEXTS:
            for chunks in _all_chunkings(text):
                with self.subTest(text=repr(text), chunks=chunks):
                    expected = naive_find(chunks, self.table, PATTERNS)
                    got = [
                        _hit_tuple(h)
                        for h in find_all(chunks, self.table, PATTERNS)
                    ]
                    self.assertEqual(got, expected)

    def test_streaming_incremental_equals_find_all(self) -> None:
        text = chr(E) + chr(A) + chr(M_GRAVE) + chr(A) + chr(M_GRAVE)
        for chunks in _all_chunkings(text):
            searcher = StreamSearcher(self.table, PATTERNS)
            incremental = []
            for chunk in chunks:
                incremental.extend(searcher.feed(chunk))
            incremental.extend(searcher.finish())
            batch = find_all(chunks, self.table, PATTERNS)
            self.assertEqual(
                [_hit_tuple(h) for h in incremental],
                [_hit_tuple(h) for h in batch],
                f"chunks={chunks}",
            )


class SemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_same_canonical_pattern_reported_per_id(self) -> None:
        # p_ag 与 p_pre 规范后相同，命中必须分别带回各自 ID。
        hits = find_all(
            [chr(A) + chr(M_GRAVE)],
            self.table,
            [("p_ag", chr(A) + chr(M_GRAVE)), ("p_pre", chr(P_A_GRAVE))],
        )
        self.assertEqual(
            [_hit_tuple(h) for h in hits],
            [
                ("p_ag", 0, 1, ((0, 0, 2),)),
                ("p_pre", 0, 1, ((0, 0, 2),)),
            ],
        )

    def test_overlapping_hits(self) -> None:
        hits = find_all(["aaa"], self.table, [("p", "aa")])
        self.assertEqual(
            [(h.start, h.end) for h in hits],
            [(0, 2), (1, 3)],
        )

    def test_shared_suffix_patterns(self) -> None:
        # "e a grave" 上 p_eag=[0,2) 与 p_ag=[1,2) 共享后缀命中。
        text = chr(E) + chr(A) + chr(M_GRAVE)
        hits = find_all(
            [text],
            self.table,
            [("p_eag", text), ("p_ag", chr(A) + chr(M_GRAVE))],
        )
        self.assertEqual(
            [(h.pattern_id, h.start, h.end) for h in hits],
            [("p_eag", 0, 2), ("p_ag", 1, 2)],
        )

    def test_recursive_decomposition_pattern_matches_spelled_out_text(self):
        # 模式是两级预组合字符，文本是完全展开写法 a+grave+ring；
        # 两者规范后都是 (P_A_GRAVE_RING,)，递归分解使写法不同也能命中。
        hits = find_all(
            [chr(A) + chr(M_GRAVE) + chr(M_RING)],
            self.table,
            [("p", chr(P_A_GRAVE_RING))],
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0].start, hits[0].end), (0, 1))

    def test_leading_combining_marks_searchable(self) -> None:
        # 前导组合符段不组合，但仍是规范输出的一部分，可被命中。
        text = chr(M_GRAVE) + chr(M_ACUTE) + chr(A)
        hits = find_all(
            [text],
            self.table,
            [("p", chr(M_GRAVE) + chr(M_ACUTE))],
        )
        self.assertEqual(
            [_hit_tuple(h) for h in hits],
            [("p", 0, 2, ((0, 0, 2),))],
        )

    def test_noncontiguous_source_spans_are_exact(self) -> None:
        # a + dotbelow(220) + grave(230)：重排后 grave 与 a 组合，
        # 命中 P_A_GRAVE 的来源是偏移 0 和 2，不是 [0,3) 的近似。
        text = chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE)
        hits = find_all([text], self.table, [("p", chr(P_A_GRAVE))])
        self.assertEqual(len(hits), 1)
        self.assertEqual(
            _hit_tuple(hits[0]),
            ("p", 0, 1, ((0, 0, 1), (0, 2, 3))),
        )

    def test_hit_waits_for_segment_closure(self) -> None:
        # 段未闭合时不得提前上报；闭合后命中来源跨 chunk。
        searcher = StreamSearcher(
            self.table, [("p", chr(A) + chr(M_GRAVE))]
        )
        self.assertEqual(searcher.feed(chr(A)), ())
        self.assertEqual(searcher.feed(chr(M_GRAVE)), ())
        hits = searcher.feed(chr(E))  # starter 闭合上一段
        self.assertEqual(
            [_hit_tuple(h) for h in hits],
            [("p", 0, 1, ((0, 0, 1), (1, 0, 1)))],
        )
        self.assertEqual(searcher.finish(), ())

    def test_delivered_hits_never_retracted(self) -> None:
        # 已交付命中在后续输入后保持，且最终集合包含它们。
        searcher = StreamSearcher(self.table, [("p", "aa")])
        first = searcher.feed("aa")
        # 第二个 'a' 所在段尚未闭合，命中 (0,2) 不能提前交付。
        self.assertEqual([(h.start, h.end) for h in first], [])
        second = searcher.feed("a")
        self.assertEqual([(h.start, h.end) for h in second], [(0, 2)])
        rest = searcher.finish()
        all_hits = [_hit_tuple(h) for h in (*first, *second, *rest)]
        self.assertEqual(
            all_hits,
            [
                _hit_tuple(h)
                for h in find_all(["aa", "a"], self.table, [("p", "aa")])
            ],
        )

    def test_empty_pattern_list_matches_nothing(self) -> None:
        searcher = StreamSearcher(self.table, [])
        self.assertEqual(searcher.feed("abc"), ())
        self.assertEqual(searcher.finish(), ())

    def test_finish_idempotent_returns_empty(self) -> None:
        searcher = StreamSearcher(self.table, [("p", "a")])
        searcher.feed("a")
        first = searcher.finish()
        self.assertEqual(len(first), 1)
        self.assertEqual(searcher.finish(), ())


class InvalidPatternTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_empty_pattern_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamSearcher(self.table, [("bad", "")])

    def test_bare_str_pattern_entry_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamSearcher(self.table, ["abc"])

    def test_non_pair_pattern_entry_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamSearcher(self.table, [("p", "a", "extra")])

    def test_non_str_pattern_text_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamSearcher(self.table, [("p", 123)])

    def test_non_iterable_patterns_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamSearcher(self.table, 42)

    def test_mapping_patterns_accepted(self) -> None:
        hits = find_all(["ab"], self.table, {"p": "ab"})
        self.assertEqual([(h.start, h.end) for h in hits], [(0, 2)])

    def test_single_str_chunks_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            find_all("abc", self.table, [("p", "a")])

    def test_non_iterable_chunks_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            find_all(123, self.table, [("p", "a")])

    def test_non_str_chunk_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            find_all([b"a"], self.table, [("p", "a")])

    def test_feed_after_finish_rejected(self) -> None:
        searcher = StreamSearcher(self.table, [("p", "a")])
        searcher.finish()
        with self.assertRaises(InvalidInputError):
            searcher.feed("a")

    def test_bool_segment_limit_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamSearcher(self.table, [("p", "a")], segment_limit=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
