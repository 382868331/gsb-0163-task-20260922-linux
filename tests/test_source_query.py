"""来源反查（输入选区 → 输出区间）必要测试。

覆盖题面要求的四类场景：组合重排、一个字符多级分解、跨 chunk 合成、
前导组合符；另覆盖 pending 范围、空区间、非法区间、查询不强制 finish，
以及小输入上对独立参考（tests/reference.py 的逐输出来源集扫描）的
随机对照。正向索引（source_spans）的既有断言保留在 test_normalize.py。
"""

from __future__ import annotations

import math
import random
import unittest

from canoncompose import (
    DictCharacterTable,
    InvalidInputError,
    StreamNormalizer,
    normalize,
)
from canoncompose.samples import (
    A,
    E,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    M_RING,
    O,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    P_E_ACUTE,
    build_demo_table,
)

from reference import reference_normalize, reference_query


def _out(result) -> tuple[tuple[int, int, bool], ...]:
    return tuple((s.start, s.end, s.mixed) for s in result.output_spans)


def _pending(result) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (s.chunk_index, s.start, s.end) for s in result.pending_spans
    )


class ReorderQueryTests(unittest.TestCase):
    """组合重排场景下的反查。"""

    def setUp(self) -> None:
        self.table = build_demo_table()
        # a, grave(230), dotbelow(220) → 重排后 [P_A_GRAVE, M_DOTBELOW]
        self.text = chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
        self.sn = StreamNormalizer(self.table)
        self.sn.feed(self.text)
        self.result = self.sn.finish()
        self.assertEqual(
            self.result.codepoints, (P_A_GRAVE, M_DOTBELOW)
        )

    def test_query_base_char_is_mixed(self) -> None:
        # 只选 'a'：组合产物 P_A_GRAVE 还混入选区外的 grave。
        got = self.sn.query(0, 0, 1)
        self.assertEqual(_out(got), ((0, 1, True),))
        self.assertEqual(_pending(got), ())

    def test_query_composed_mark_is_mixed(self) -> None:
        # 只选 grave：同一个组合产物，来源同样混入选区外的 'a'。
        got = self.sn.query(0, 1, 2)
        self.assertEqual(_out(got), ((0, 1, True),))

    def test_query_reordered_mark_is_pure(self) -> None:
        # dotbelow 未参与组合，且被重排到 grave 之前输出。
        got = self.sn.query(0, 2, 3)
        self.assertEqual(_out(got), ((1, 2, False),))

    def test_query_base_and_mark_together_unmixes(self) -> None:
        # 同时选中 'a' 与 grave：组合产物来源全部落在选区内。
        got = self.sn.query(0, 0, 2)
        self.assertEqual(_out(got), ((0, 1, False),))

    def test_query_whole_range_merges_contiguous(self) -> None:
        got = self.sn.query(0, 0, 3)
        self.assertEqual(_out(got), ((0, 2, False),))


class MultiLevelDecompositionQueryTests(unittest.TestCase):
    """一个输入字符经多级分解/多叶子展开后的反查。"""

    def test_single_char_multilevel_recomposed(self) -> None:
        # P_A_GRAVE_RING → (P_A_GRAVE, ring) → (a, grave, ring) → 重组回 1 个输出。
        table = build_demo_table()
        sn = StreamNormalizer(table)
        sn.feed(chr(P_A_GRAVE_RING))
        sn.finish()
        got = sn.query(0, 0, 1)
        self.assertEqual(_out(got), ((0, 1, False),))

    def test_single_char_expands_to_multiple_outputs(self) -> None:
        # 一个输入码点分解为两个 starter → 两个输出，选该码点命中整段。
        x = 0xE600
        table = DictCharacterTable(decompositions={x: (A, E)})
        sn = StreamNormalizer(table)
        sn.feed(chr(x))
        sn.finish()
        got = sn.query(0, 0, 1)
        self.assertEqual(_out(got), ((0, 2, False),))

    def test_single_char_split_between_published_and_pending(self) -> None:
        # X → (grave, a)：grave 随前一段闭合并发布，a 留在段缓冲。
        x = 0xE601
        table = DictCharacterTable(
            decompositions={x: (M_GRAVE, A)},
            combining_classes={M_GRAVE: 230},
            compositions={(A, M_GRAVE): P_A_GRAVE},
        )
        sn = StreamNormalizer(table)
        sn.feed(chr(A) + chr(x))
        # 查询前：P_A_GRAVE 已发布（含 X 的 grave），X 的 'a' 仍在段缓冲。
        got = sn.query(0, 1, 2)
        self.assertEqual(_out(got), ((0, 1, True),))
        self.assertEqual(_pending(got), ((0, 1, 2),))
        # finish 后同一选区：两段相邻但 mixed 标志不同，不合并。
        sn.finish()
        got = sn.query(0, 1, 2)
        self.assertEqual(_out(got), ((0, 1, True), (1, 2, False)))
        self.assertEqual(_pending(got), ())


class CrossChunkQueryTests(unittest.TestCase):
    """跨 chunk 合成与 pending 范围。"""

    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_pending_reported_before_finish(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE))
        # 两个 chunk 的字符都还在段缓冲中：无输出区间，只有 pending。
        got = sn.query(0, 0, 1)
        self.assertEqual(_out(got), ())
        self.assertEqual(_pending(got), ((0, 0, 1),))
        got = sn.query(1, 0, 1)
        self.assertEqual(_out(got), ())
        self.assertEqual(_pending(got), ((1, 0, 1),))

    def test_cross_chunk_composition_is_mixed_from_either_side(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE))
        result = sn.finish()
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        # 从任一 chunk 单侧查询，组合产物都混入另一侧来源。
        self.assertEqual(_out(sn.query(0, 0, 1)), ((0, 1, True),))
        self.assertEqual(_out(sn.query(1, 0, 1)), ((0, 1, True),))
        self.assertEqual(_pending(sn.query(0, 0, 1)), ())

    def test_query_does_not_flush_or_finish(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        before = sn.query(0, 0, 1)
        # 查询是纯操作：重复查询结果一致，流仍可继续 feed。
        self.assertEqual(_out(sn.query(0, 0, 1)), _out(before))
        sn.feed(chr(M_GRAVE))
        result = sn.finish()
        # 若查询曾冲刷段缓冲，'a' 会被单独发布、无法与 grave 组合。
        self.assertEqual(result.codepoints, (P_A_GRAVE,))

    def test_published_prefix_stable_across_pending_growth(self) -> None:
        # 已发布部分不随后续 feed 改变：先发布 'x'，再挂起 a+grave。
        sn = StreamNormalizer(self.table)
        sn.feed("x" + chr(A))
        got = sn.query(0, 0, 1)
        self.assertEqual(_out(got), ((0, 1, False),))
        self.assertEqual(_pending(sn.query(0, 1, 2)), ((0, 1, 2),))
        sn.feed(chr(M_GRAVE))
        sn.finish()
        # finish 后 'x' 的输出区间不变，a+grave 组合为其后位置。
        self.assertEqual(_out(sn.query(0, 0, 1)), ((0, 1, False),))
        self.assertEqual(_out(sn.query(0, 1, 2)), ((1, 2, True),))


class LeadingMarkQueryTests(unittest.TestCase):
    """前导组合符（无 starter 的前导段）场景。"""

    def test_leading_marks_query(self) -> None:
        table = build_demo_table()
        text = chr(M_GRAVE) + chr(M_ACUTE) + chr(A)
        sn = StreamNormalizer(table)
        sn.feed(text)
        result = sn.finish()
        self.assertEqual(result.codepoints, (M_GRAVE, M_ACUTE, A))
        # 前导记号不组合：选区与输出一一对应，无混入。
        self.assertEqual(_out(sn.query(0, 0, 2)), ((0, 2, False),))
        self.assertEqual(_out(sn.query(0, 0, 1)), ((0, 1, False),))
        self.assertEqual(_out(sn.query(0, 2, 3)), ((2, 3, False),))

    def test_leading_mark_pending_then_published(self) -> None:
        table = build_demo_table()
        sn = StreamNormalizer(table)
        sn.feed(chr(M_GRAVE))
        got = sn.query(0, 0, 1)
        self.assertEqual(_out(got), ())
        self.assertEqual(_pending(got), ((0, 0, 1),))
        sn.feed(chr(A))  # starter 闭合前导段
        got = sn.query(0, 0, 1)
        self.assertEqual(_out(got), ((0, 1, False),))
        self.assertEqual(_pending(got), ())


class QueryValidationTests(unittest.TestCase):
    """空区间与非法区间的错误语义。"""

    def setUp(self) -> None:
        self.table = build_demo_table()
        self.sn = StreamNormalizer(self.table)
        self.sn.feed(chr(A) + chr(M_GRAVE))

    def test_empty_range_returns_empty_result(self) -> None:
        got = self.sn.query(0, 1, 1)
        self.assertEqual(_out(got), ())
        self.assertEqual(_pending(got), ())
        # 空 chunk 上空区间同样合法。
        sn = StreamNormalizer(self.table)
        sn.feed("")
        got = sn.query(0, 0, 0)
        self.assertEqual(_out(got), ())
        self.assertEqual(_pending(got), ())

    def test_empty_range_still_validated(self) -> None:
        # chunk 0 长度为 2：[2, 2) 是合法空区间，[3, 3) 越界。
        got = self.sn.query(0, 2, 2)
        self.assertEqual(_out(got), ())
        with self.assertRaises(InvalidInputError):
            self.sn.query(0, 3, 3)
        with self.assertRaises(InvalidInputError):
            self.sn.query(1, 0, 0)  # 只 feed 过 chunk 0

    def test_reversed_range_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            self.sn.query(0, 2, 1)

    def test_negative_and_bool_and_float_rejected(self) -> None:
        for args in [(-1, 0, 1), (0, -1, 1), (0, 0, -1)]:
            with self.subTest(args=args), self.assertRaises(InvalidInputError):
                self.sn.query(*args)
        with self.assertRaises(InvalidInputError):
            self.sn.query(True, 0, 1)  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError):
            self.sn.query(0, False, 1)  # type: ignore[arg-type]
        for bad in (math.nan, math.inf, -math.inf, 0.5):
            with self.subTest(bad=bad), self.assertRaises(InvalidInputError):
                self.sn.query(0, bad, 1)  # type: ignore[arg-type]

    def test_range_beyond_chunk_length_rejected(self) -> None:
        with self.assertRaises(InvalidInputError) as ctx:
            self.sn.query(0, 0, 3)
        self.assertEqual(ctx.exception.chunk_index, 0)

    def test_query_before_any_feed_rejected(self) -> None:
        sn = StreamNormalizer(self.table)
        with self.assertRaises(InvalidInputError):
            sn.query(0, 0, 1)

    def test_failed_feed_chunk_not_queryable(self) -> None:
        # 失败 feed 回滚 chunk 计数后，该编号不可查询。
        from canoncompose.samples import build_cyclic_table

        sn = StreamNormalizer(build_cyclic_table())
        sn.feed("a")
        from canoncompose import DecompositionCycleError

        with self.assertRaises(DecompositionCycleError):
            sn.feed(chr(0xE200))
        with self.assertRaises(InvalidInputError):
            sn.query(1, 0, 1)
        # chunk 0 仍可查（pending 中）。
        self.assertEqual(_pending(sn.query(0, 0, 1)), ((0, 0, 1),))


class MixedFlagMergingTests(unittest.TestCase):
    """相邻段的 mixed 标志合并规则。"""

    def test_adjacent_same_flag_merges(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed("ae")
        sn.finish()
        self.assertEqual(_out(sn.query(0, 0, 2)), ((0, 2, False),))

    def test_adjacent_different_flags_do_not_merge(self) -> None:
        # chunk0 = "a"，chunk1 = grave + "e"：a+grave 跨块组合为 P_A_GRAVE，
        # 输出 [P_A_GRAVE, e]。选 chunk1 全部：P_A_GRAVE 混入选区外的 'a'
        # （mixed），e 纯（非 mixed）——相邻但标志不同，不得合并。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE) + chr(E))
        sn.finish()
        got = sn.query(1, 0, 2)
        self.assertEqual(_out(got), ((0, 1, True), (1, 2, False),))


class RandomizedCrossCheckTests(unittest.TestCase):
    """小输入随机对照：逐输出来源集扫描（独立参考）作期望值。"""

    def test_random_queries_match_reference(self) -> None:
        table = build_demo_table()
        alphabet = [
            A, E, O, M_GRAVE, M_ACUTE, M_DOTBELOW, M_RING,
            P_A_GRAVE, P_E_ACUTE, P_A_GRAVE_RING,
        ]
        rng = random.Random(20260925)
        for trial in range(40):
            n = rng.randint(0, 30)
            whole = "".join(chr(rng.choice(alphabet)) for _ in range(n))
            max_cuts = min(n - 1, 4)
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
            # 正向索引保留且与参考一致（与反查共存）。
            ref_cps, ref_spans = reference_normalize(chunks, table)
            self.assertEqual(result.codepoints, ref_cps, f"trial={trial}")
            self.assertEqual(
                tuple(
                    (s.chunk_index, s.start, s.end)
                    for s in result.source_spans
                ),
                ref_spans,
                f"forward spans trial={trial}",
            )
            for _ in range(5):
                ci = rng.randrange(len(chunks))
                length = len(chunks[ci])
                lo = rng.randint(0, length)
                hi = rng.randint(lo, length)
                got = sn.query(ci, lo, hi)
                # finish 之后不存在 pending。
                self.assertEqual(
                    _pending(got), (), f"trial={trial} query=({ci},{lo},{hi})"
                )
                if lo == hi:
                    self.assertEqual(_out(got), ())
                    continue
                expected = reference_query(chunks, table, ci, lo, hi)
                self.assertEqual(
                    _out(got),
                    expected,
                    f"trial={trial} query=({ci},{lo},{hi})",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
