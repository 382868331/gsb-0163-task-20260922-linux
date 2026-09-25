"""反向来源索引测试：输入选区 → 已发布输出区间。

覆盖题面要求的四类结构：组合重排、一个字符多级分解、跨 chunk 合成、
前导组合符；另覆盖 pending 范围（不强制 finish）、空区间语义、非法
选区、feed 回滚后索引一致性。随机小输入以 tests/reference.py 的
逐输出来源集扫描为独立参照。
"""

from __future__ import annotations

import math
import random
import unittest

from canoncompose import (
    DictCharacterTable,
    InvalidInputError,
    SourceQueryResult,
    StreamNormalizer,
    normalize,
)
from canoncompose.samples import (
    A,
    E,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    P_E_ACUTE,
    build_cyclic_table,
    build_demo_table,
)

from reference import reference_normalize, reference_output_sources


def _ranges(result: SourceQueryResult) -> tuple[tuple[int, int, bool], ...]:
    return tuple((r.start, r.end, r.mixed) for r in result.ranges)


def _pending(result: SourceQueryResult) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (s.chunk_index, s.start, s.end) for s in result.pending
    )


def _scan_expected(
    out_sources: tuple[frozenset, ...],
    chunk_index: int,
    start: int,
    end: int,
) -> tuple[tuple[int, int, bool], ...]:
    """参照：逐输出扫描来源集，独立算出期望的 (start, end, mixed) 段。"""
    expected: list[tuple[int, int, bool]] = []
    for pos, sources in enumerate(out_sources):
        inside = [
            p for p in sources
            if p[0] == chunk_index and start <= p[1] < end
        ]
        if not inside:
            continue
        mixed = len(inside) != len(sources)
        if expected and expected[-1][2] == mixed and expected[-1][1] == pos:
            prev = expected[-1]
            expected[-1] = (prev[0], pos + 1, mixed)
        else:
            expected.append((pos, pos + 1, mixed))
    return tuple(expected)


class CompositionReorderQueryTests(unittest.TestCase):
    """组合重排：a + grave(230) + dotbelow(220) → [P_A_GRAVE, M_DOTBELOW]。"""

    def setUp(self) -> None:
        self.table = build_demo_table()
        self.sn = StreamNormalizer(self.table)
        self.sn.feed(chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW))
        self.result = self.sn.finish()
        self.assertEqual(
            self.result.codepoints, (P_A_GRAVE, M_DOTBELOW)
        )

    def test_select_mark_after_reorder_hits_composed_output(self) -> None:
        # 选 grave（输入偏移 1）：重排/组合后它并入输出位置 0 的 P_A_GRAVE；
        # 该输出来源还含选区外的 a → mixed。
        q = self.sn.query_output_ranges(0, 1, 2)
        self.assertEqual(_ranges(q), ((0, 1, True),))

    def test_select_reordered_dotbelow(self) -> None:
        # dotbelow（输入偏移 2）重排到 grave 之前，独占输出位置 1。
        q = self.sn.query_output_ranges(0, 2, 3)
        self.assertEqual(_ranges(q), ((1, 2, False),))

    def test_select_base_only(self) -> None:
        q = self.sn.query_output_ranges(0, 0, 1)
        self.assertEqual(_ranges(q), ((0, 1, True),))

    def test_select_base_and_mark_unmixes_output(self) -> None:
        # a 与 grave 都在选区内：P_A_GRAVE 的来源不再混入选区外输入。
        q = self.sn.query_output_ranges(0, 0, 2)
        self.assertEqual(_ranges(q), ((0, 1, False),))

    def test_select_whole_chunk_merges_consecutive(self) -> None:
        q = self.sn.query_output_ranges(0, 0, 3)
        self.assertEqual(_ranges(q), ((0, 2, False),))


class MultiLevelDecompositionQueryTests(unittest.TestCase):
    def test_one_char_two_level_decomposition(self) -> None:
        # P_A_GRAVE_RING → (P_A_GRAVE, ring) → (a, grave, ring)，
        # 重新组合回单个输出；单个输入字符映射到该输出。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(P_A_GRAVE_RING))
        sn.finish()
        q = sn.query_output_ranges(0, 0, 1)
        self.assertEqual(_ranges(q), ((0, 1, False),))

    def test_one_input_char_produces_multiple_outputs(self) -> None:
        # 一个输入字符分解为两个 starter → 两个输出位置，索引不得假设一对一。
        x = 0xE600
        table = DictCharacterTable(decompositions={x: (A, E)})
        sn = StreamNormalizer(table)
        sn.feed(chr(x) + "o")
        result = sn.finish()
        self.assertEqual(result.codepoints, (A, E, ord("o")))
        q = sn.query_output_ranges(0, 0, 1)
        self.assertEqual(_ranges(q), ((0, 2, False),))
        # 选区只覆盖后面的 'o'。
        q = sn.query_output_ranges(0, 1, 2)
        self.assertEqual(_ranges(q), ((2, 3, False),))

    def test_partial_decomposition_leaves_unmixed_and_mixed_runs(self) -> None:
        # 输入：P_E_ACUTE（分解 e+acute 后重组）+ 普通 'x'。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(P_E_ACUTE) + "x")
        sn.finish()
        q = sn.query_output_ranges(0, 0, 1)
        self.assertEqual(_ranges(q), ((0, 1, False),))
        q = sn.query_output_ranges(0, 1, 2)
        self.assertEqual(_ranges(q), ((1, 2, False),))


class CrossChunkQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_cross_chunk_composition_marks_mixed_from_either_side(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))          # chunk 0
        sn.feed(chr(M_GRAVE))    # chunk 1：与 chunk 0 的 a 跨块组合
        sn.finish()
        # 无论从组合的哪一端选区反查，都命中同一输出，且混入选区外来源。
        self.assertEqual(_ranges(sn.query_output_ranges(0, 0, 1)), ((0, 1, True),))
        self.assertEqual(_ranges(sn.query_output_ranges(1, 0, 1)), ((0, 1, True),))

    def test_adjacent_ranges_with_different_mixed_not_merged(self) -> None:
        # chunk1 = grave + "x"：grave 与 chunk0 的 a 跨块组合（mixed），
        # "x" 是独立 starter（不 mixed）。
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE) + "x")
        result = sn.finish()
        self.assertEqual(result.codepoints, (P_A_GRAVE, ord("x")))
        q = sn.query_output_ranges(1, 0, 2)
        # 相邻但 mixed 不同，不得合并为一段。
        self.assertEqual(_ranges(q), ((0, 1, True), (1, 2, False)))

    def test_empty_chunks_are_queryable_but_empty(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed("")
        sn.feed("a")
        sn.finish()
        # 空 chunk 只有空区间合法。
        self.assertEqual(
            _ranges(sn.query_output_ranges(0, 0, 0)), ()
        )
        with self.assertRaises(InvalidInputError):
            sn.query_output_ranges(0, 0, 1)
        self.assertEqual(
            _ranges(sn.query_output_ranges(1, 0, 1)), ((0, 1, False),)
        )


class LeadingMarkQueryTests(unittest.TestCase):
    def test_leading_combining_marks_queryable(self) -> None:
        # 前导组合符段不组合，逐字符直通输出。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(M_GRAVE) + chr(M_ACUTE) + chr(A))
        result = sn.finish()
        self.assertEqual(
            result.codepoints, (M_GRAVE, M_ACUTE, A)
        )
        q = sn.query_output_ranges(0, 0, 1)
        self.assertEqual(_ranges(q), ((0, 1, False),))
        # 两个前导记号一起选：连续且都不 mixed，合并为一段。
        q = sn.query_output_ranges(0, 0, 2)
        self.assertEqual(_ranges(q), ((0, 2, False),))
        q = sn.query_output_ranges(0, 2, 3)
        self.assertEqual(_ranges(q), ((2, 3, False),))


class PendingQueryTests(unittest.TestCase):
    def test_pending_reported_without_forcing_finish(self) -> None:
        table = build_demo_table()
        sn = StreamNormalizer(table)
        sn.feed(chr(A))          # chunk 0：段挂起，无已发布输出
        sn.feed(chr(M_GRAVE))    # chunk 1：同段挂起
        q = sn.query_output_ranges(0, 0, 1)
        # 没有已发布输出可命中；两个 chunk 的输入都在 pending。
        self.assertEqual(_ranges(q), ())
        self.assertEqual(_pending(q), ((0, 0, 1), (1, 0, 1)))
        # 查询不得强制 finish：之后仍可继续 feed。
        sn.feed(chr(E))          # chunk 2：starter 冲刷前一段
        q = sn.query_output_ranges(1, 0, 1)
        self.assertEqual(_ranges(q), ((0, 1, True),))
        # 'e' 仍在段缓冲中。
        self.assertEqual(_pending(q), ((2, 0, 1),))
        result = sn.finish()
        self.assertEqual(result.codepoints, (P_A_GRAVE, E))
        q = sn.query_output_ranges(2, 0, 1)
        self.assertEqual(_ranges(q), ((1, 2, False),))
        self.assertEqual(_pending(q), ())

    def test_pending_sources_standalone(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        self.assertEqual(sn.pending_sources(), ())
        sn.feed(chr(P_A_GRAVE_RING))
        self.assertEqual(
            tuple((s.chunk_index, s.start, s.end) for s in sn.pending_sources()),
            ((0, 0, 1),),
        )
        sn.finish()
        self.assertEqual(sn.pending_sources(), ())


class EmptyAndInvalidRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sn = StreamNormalizer(build_demo_table())
        self.sn.feed("ao")
        self.sn.finish()

    def test_empty_range_valid_returns_empty_result(self) -> None:
        for start in (0, 1, 2):
            with self.subTest(start=start):
                q = self.sn.query_output_ranges(0, start, start)
                self.assertEqual(q.ranges, ())
                self.assertEqual(q.pending, ())

    def test_empty_range_still_validates(self) -> None:
        # 空区间也要做合法性检查：越界/未 feed 的 chunk 照样拒绝。
        with self.assertRaises(InvalidInputError):
            self.sn.query_output_ranges(0, 3, 3)  # 超出 chunk 长度
        with self.assertRaises(InvalidInputError):
            self.sn.query_output_ranges(1, 0, 0)  # chunk 1 未 feed

    def test_reversed_range_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            self.sn.query_output_ranges(0, 2, 1)

    def test_negative_bounds_rejected(self) -> None:
        for args in ((-1, 0, 1), (0, -1, 1), (0, 0, -1)):
            with self.subTest(args=args), self.assertRaises(InvalidInputError):
                self.sn.query_output_ranges(*args)

    def test_bool_and_float_bounds_rejected(self) -> None:
        for bad in (True, 1.5, math.nan, math.inf):
            with self.subTest(bad=bad), self.assertRaises(InvalidInputError):
                self.sn.query_output_ranges(0, 0, bad)  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError):
            self.sn.query_output_ranges(False, 0, 1)  # type: ignore[arg-type]

    def test_query_before_any_feed_rejected(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        with self.assertRaises(InvalidInputError):
            sn.query_output_ranges(0, 0, 0)


class RollbackIndexTests(unittest.TestCase):
    def test_failed_feed_leaves_reverse_index_consistent(self) -> None:
        table = build_cyclic_table()
        sn = StreamNormalizer(table)
        sn.feed("a")
        with self.assertRaises(Exception):
            sn.feed(chr(0xE200))  # 分解环，feed 原子回滚
        # 回滚后：chunk 1 未登记；chunk 0 的 'a' 仍在段缓冲中（未发布）。
        with self.assertRaises(InvalidInputError):
            sn.query_output_ranges(1, 0, 1)
        q = sn.query_output_ranges(0, 0, 1)
        self.assertEqual(_ranges(q), ())
        self.assertEqual(_pending(q), ((0, 0, 1),))
        sn.feed("e")  # 记为 chunk 1；starter 冲刷出 'a'
        sn.finish()
        self.assertEqual(
            _ranges(sn.query_output_ranges(0, 0, 1)), ((0, 1, False),)
        )
        self.assertEqual(
            _ranges(sn.query_output_ranges(1, 0, 1)), ((1, 2, False),)
        )


class RandomizedCrossCheckTests(unittest.TestCase):
    """小输入随机选区：以逐输出来源集扫描为独立参照。"""

    def test_random_queries_match_per_output_scan(self) -> None:
        table = build_demo_table()
        alphabet = [
            A, E, ord("o"), M_GRAVE, M_ACUTE, M_DOTBELOW,
            P_A_GRAVE, P_E_ACUTE, P_A_GRAVE_RING,
        ]
        rng = random.Random(20260925)
        for trial in range(80):
            n = rng.randint(1, 12)
            whole = "".join(chr(rng.choice(alphabet)) for _ in range(n))
            max_cuts = min(n - 1, 3)
            k = rng.randint(0, max_cuts) if max_cuts > 0 else 0
            cuts = sorted(rng.sample(range(1, n), k=k)) if k else []
            chunks: list[str] = []
            prev = 0
            for c in cuts + [n]:
                chunks.append(whole[prev:c])
                prev = c

            sn = StreamNormalizer(table)
            for chunk in chunks:
                sn.feed(chunk)
            result = sn.finish()

            out_sources = reference_output_sources(chunks, table)
            ref_cps, _ = reference_normalize(chunks, table)
            self.assertEqual(result.codepoints, ref_cps, f"trial={trial}")
            self.assertEqual(len(out_sources), len(result.codepoints))

            for _ in range(6):
                chunk_index = rng.randrange(len(chunks))
                length = len(chunks[chunk_index])
                a = rng.randint(0, length)
                b = rng.randint(0, length)
                start, end = min(a, b), max(a, b)
                q = sn.query_output_ranges(chunk_index, start, end)
                if start == end:
                    self.assertEqual(q.ranges, (), f"trial={trial}")
                    continue
                expected = _scan_expected(
                    out_sources, chunk_index, start, end
                )
                self.assertEqual(
                    _ranges(q),
                    expected,
                    f"trial={trial} range=({chunk_index},{start},{end})",
                )
                # finish 之后不存在 pending。
                self.assertEqual(q.pending, ())

    def test_forward_index_still_intact(self) -> None:
        # 正向索引（source_spans）不受反向索引影响。
        result = normalize(
            [chr(A), chr(M_GRAVE), "x"], build_demo_table()
        )
        self.assertEqual(
            tuple((s.chunk_index, s.start, s.end) for s in result.source_spans),
            ((0, 0, 1), (1, 0, 1), (2, 0, 1)),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
