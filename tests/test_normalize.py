"""必要测试：核心算法、阻塞规则、跨块、环、上限、错误语义、原子性。

期望值来自 tests/reference.py 中的独立朴素实现（不导入被测核心），
以及测试内手写的固定预期；往返一致仅作为补充断言。
"""

from __future__ import annotations

import math
import random
import unittest

from canoncompose import (
    DEFAULT_SEGMENT_LIMIT,
    DictCharacterTable,
    NormalizationResult,
    SegmentLimitError,
    StreamNormalizer,
    compile_table,
    DecompositionCycleError,
    InvalidInputError,
    InvalidTableError,
    normalize,
)
from canoncompose.samples import (
    A,
    E,
    O,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    M_RING,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    P_E_ACUTE,
    build_cyclic_table,
    build_demo_table,
    describe,
)

from reference import reference_normalize


def _cps(result: NormalizationResult) -> tuple[int, ...]:
    return result.codepoints


def _spans(result: NormalizationResult) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (s.chunk_index, s.start, s.end) for s in result.source_spans
    )


class DecomposeSortComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_precomposed_decomposes_then_recomposes(self) -> None:
        # P_A_GRAVE 分解为 a + grave，段闭合时重新组合回 P_A_GRAVE。
        result = normalize([chr(P_A_GRAVE)], self.table)
        self.assertEqual(_cps(result), (P_A_GRAVE,))
        self.assertEqual(_spans(result), ((0, 0, 1),))

    def test_multilevel_decomposition_and_chain_composition(self) -> None:
        # P_A_GRAVE_RING → (P_A_GRAVE, ring) → (a, grave, ring)
        # 排序 a, grave(230), ring(232)；grave 组合后基字符变为 P_A_GRAVE，
        # ring 前一未组合 ccc 视为 0 < 232，继续组合为 P_A_GRAVE_RING。
        result = normalize([chr(P_A_GRAVE_RING)], self.table)
        self.assertEqual(_cps(result), (P_A_GRAVE_RING,))

    def test_ccc_reorder_changes_composition_order(self) -> None:
        # 输入顺序 a, grave(230), dotbelow(220)；排序后 dotbelow 在前。
        # dotbelow 无组合映射成为未组合项（ccc 220）；grave 的 230 > 220
        # 仍允许与基字符组合 → [P_A_GRAVE, M_DOTBELOW]。
        s = chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
        result = normalize([s], self.table)
        self.assertEqual(_cps(result), (P_A_GRAVE, M_DOTBELOW))
        self.assertEqual(result.text, chr(P_A_GRAVE) + chr(M_DOTBELOW))

    def test_same_ccc_when_first_mark_composed_second_is_allowed(self) -> None:
        # 题面规则以"前一未组合字符"为准：grave(230) 先组合进基字符后，
        # acute(230) 前面已无未组合非 starter（prev ccc 视为 0），允许查询
        # 组合映射；映射存在即组合。
        target = 0xE199
        table = DictCharacterTable(
            combining_classes={M_GRAVE: 230, M_ACUTE: 230},
            compositions={
                (A, M_GRAVE): P_A_GRAVE,
                (P_A_GRAVE, M_ACUTE): target,
            },
        )
        s = chr(A) + chr(M_GRAVE) + chr(M_ACUTE)
        result = normalize([s], table)
        self.assertEqual(_cps(result), (target,))

    def test_same_ccc_blocking_with_two_uncombined_marks(self) -> None:
        # 精确构造：两个 ccc 相同的记号，第一个无组合映射（成为未组合项），
        # 第二个即便有映射也必须被前一未组合项（等 ccc）阻塞。
        blocked_target = 0xE199
        table = DictCharacterTable(
            combining_classes={M_DOTBELOW: 230, M_ACUTE: 230},
            compositions={
                # 只有 (a, acute) 可组合；dotbelow 不可组合。
                (A, M_ACUTE): P_E_ACUTE,
                # 若错误地先组合 acute，该映射也不能被使用：
                (P_E_ACUTE, M_DOTBELOW): blocked_target,
            },
        )
        # 输入顺序使稳定排序后 dotbelow 在 acute 之前（同 ccc 保序）。
        s = chr(A) + chr(M_DOTBELOW) + chr(M_ACUTE)
        result = normalize([s], table)
        self.assertEqual(
            _cps(result),
            (A, M_DOTBELOW, M_ACUTE),
            f"等 ccc 阻塞失败，得到 {describe(_cps(result))}",
        )

    def test_leading_combining_marks_never_compose(self) -> None:
        # 流开头就是非 starter：前导段无基字符，不参与组合。
        s = chr(M_GRAVE) + chr(M_ACUTE) + chr(A)
        result = normalize([s], self.table)
        self.assertEqual(_cps(result), (M_GRAVE, M_ACUTE, A))

    def test_leading_mark_across_chunk_boundary(self) -> None:
        result = normalize([chr(M_GRAVE), chr(A) + chr(M_GRAVE)], self.table)
        # 前导 grave 不组合；随后的 a+grave 段正常组合。
        self.assertEqual(_cps(result), (M_GRAVE, P_A_GRAVE))

    def test_blocked_acute_then_higher_ccc_ring_can_compose(self) -> None:
        # a, acute(230, 无映射), ring(232, 与 P_A_GRAVE 无关):
        # 先验证严格小于规则：未组合 acute(230) 后，ring(232) 允许查询。
        table = DictCharacterTable(
            combining_classes={M_ACUTE: 230, M_RING: 232},
            compositions={(A, M_RING): P_A_GRAVE_RING},
        )
        s = chr(A) + chr(M_ACUTE) + chr(M_RING)
        result = normalize([s], table)
        # acute 无映射 → 未组合(230)；ring 的 232 > 230 → 允许查询并组合。
        self.assertEqual(_cps(result), (P_A_GRAVE_RING, M_ACUTE))

    def test_plain_starters_pass_through(self) -> None:
        result = normalize(["ao", "ea"], self.table)
        self.assertEqual(result.text, "aoea")
        self.assertEqual(
            _spans(result),
            ((0, 0, 2), (1, 0, 2)),
        )

    def test_empty_input(self) -> None:
        result = normalize([], self.table)
        self.assertEqual(result.text, "")
        self.assertEqual(result.codepoints, ())
        self.assertEqual(_spans(result), ())

    def test_empty_chunks_are_counted(self) -> None:
        result = normalize(["", "a", "", "e"], self.table)
        self.assertEqual(result.text, "ae")
        self.assertEqual(_spans(result), ((1, 0, 1), (3, 0, 1)))


class CrossChunkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_segment_pending_across_chunks_closes_on_finish(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE))
        result = sn.finish()
        self.assertEqual(_cps(result), (P_A_GRAVE,))
        # 来源跨越两个 chunk。
        self.assertEqual(
            _spans(result), ((0, 0, 1), (1, 0, 1))
        )

    def test_precomposed_split_then_starter_closes_segment(self) -> None:
        # 预组合字符在 chunk0 分解为 a+grave 挂起，chunk1 的 starter 冲刷。
        sn = StreamNormalizer(self.table)
        sn.feed(chr(P_A_GRAVE))
        # 此时尚无输出可取，段仍挂起；chunk1 的 starter 冲刷该段。
        sn.feed(chr(E))
        result = sn.finish()
        self.assertEqual(_cps(result), (P_A_GRAVE, E))

    def test_cross_chunk_starter_same_as_whole(self) -> None:
        whole = chr(A) + chr(M_GRAVE) + chr(E) + chr(M_ACUTE)
        for cut in range(len(whole) + 1):
            with self.subTest(cut=cut):
                r = normalize([whole[:cut], whole[cut:]], self.table)
                w = normalize([whole], self.table)
                self.assertEqual(_cps(r), _cps(w))

    def test_cross_chunk_ccc_reorder(self) -> None:
        # 'a' 在 chunk0；grave(230) 与 dotbelow(220) 在 chunk1，且 grave
        # 先到。跨块挂起段必须把 dotbelow 稳定排到 grave 之前。
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE) + chr(M_DOTBELOW))
        result = sn.finish()
        self.assertEqual(_cps(result), (P_A_GRAVE, M_DOTBELOW))

    def test_finish_is_idempotent_and_feed_after_finish_rejected(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        first = sn.finish()
        second = sn.finish()
        self.assertIs(first, second)
        with self.assertRaises(InvalidInputError):
            sn.feed("x")

    def test_many_chunkings_match_reference(self) -> None:
        # 确定性伪随机：固定种子，多种长度与切分，全部对齐独立参考。
        alphabet = [
            A, E, O, M_GRAVE, M_ACUTE, M_DOTBELOW, M_RING,
            P_A_GRAVE, P_E_ACUTE, P_A_GRAVE_RING,
        ]
        rng = random.Random(20260922)
        for trial in range(60):
            n = rng.randint(0, 40)
            seq = [chr(rng.choice(alphabet)) for _ in range(n)]
            whole = "".join(seq)
            # 随机切点（至少 1 段，最多 5 段）。
            max_cuts = min(n - 1, 4)
            k = rng.randint(0, max_cuts) if max_cuts > 0 else 0
            cuts = sorted(rng.sample(range(1, n), k=k)) if k else []
            chunks: list[str] = []
            prev = 0
            for c in cuts + [n]:
                chunks.append(whole[prev:c])
                prev = c
            # 独立参考使用与被测完全相同的 chunk 划分（来源坐标依赖划分）。
            ref_cps, ref_spans = reference_normalize(chunks, self.table)
            result = normalize(chunks, self.table)
            self.assertEqual(_cps(result), ref_cps, f"trial={trial}")
            self.assertEqual(
                _spans(result),
                ref_spans,
                f"spans trial={trial}",
            )
            # 整串与切分一致。
            self.assertEqual(
                _cps(normalize([whole], self.table)), _cps(result)
            )

    def test_idempotency_normalize_again(self) -> None:
        # 规范串再次规范化必须保持不变（幂等）。
        s = (
            chr(P_A_GRAVE_RING)
            + chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
            + chr(M_ACUTE) + chr(E)
        )
        once = normalize([s], self.table)
        twice = normalize([once.text], self.table)
        self.assertEqual(_cps(twice), _cps(once))
        self.assertEqual(_spans(twice), ((0, 0, len(once.text)),))


class CycleTests(unittest.TestCase):
    def test_direct_cycle_rejected(self) -> None:
        table = build_cyclic_table()
        with self.assertRaises(DecompositionCycleError) as ctx:
            normalize([chr(0xE200)], table)
        chain = ctx.exception.chain
        self.assertEqual(chain[0], chain[-1])
        self.assertIn(0xE200, chain)
        self.assertIn(0xE201, chain)

    def test_cycle_is_found_before_text_processing(self) -> None:
        table = build_cyclic_table()
        with self.assertRaises(DecompositionCycleError):
            compile_table(table)

    def test_failed_compilation_does_not_affect_previous_table(self) -> None:
        good = compile_table(build_demo_table())
        stream = StreamNormalizer(good)
        stream.feed("a")
        with self.assertRaises(DecompositionCycleError):
            compile_table(build_cyclic_table())
        stream.feed("e")
        self.assertEqual(stream.finish().text, "ae")

    def test_compiled_table_is_shared_by_independent_streams(self) -> None:
        compiled = compile_table(build_demo_table())
        first = StreamNormalizer(compiled)
        second = StreamNormalizer(compiled)
        first.feed(chr(A) + chr(M_GRAVE))
        second.feed(chr(E) + chr(M_ACUTE))
        self.assertEqual(first.finish().codepoints, (P_A_GRAVE,))
        self.assertEqual(second.finish().codepoints, (P_E_ACUTE,))

    def test_indirect_cycle_through_normal_prefix(self) -> None:
        # Z(普通) → X → Y → X：环仍可定位。
        z = 0xE300
        table = DictCharacterTable(
            decompositions={
                z: (0xE200,),
                0xE200: (0xE201,),
                0xE201: (0xE200,),
            }
        )
        with self.assertRaises(DecompositionCycleError) as ctx:
            normalize([chr(z)], table)
        self.assertEqual(ctx.exception.chain[0], ctx.exception.chain[-1])


class SegmentLimitTests(unittest.TestCase):
    def _exploding_table(self, n_leaves: int) -> DictCharacterTable:
        big = 0xE400
        marks = tuple(0xE500 + i for i in range(n_leaves - 1))
        return DictCharacterTable(
            decompositions={big: (A, *marks)},
            combining_classes={m: 230 for m in marks},
        ), big

    def test_default_limit_is_1024(self) -> None:
        self.assertEqual(DEFAULT_SEGMENT_LIMIT, 1024)

    def test_segment_exceeding_limit_rejected(self) -> None:
        table, big = self._exploding_table(DEFAULT_SEGMENT_LIMIT + 1)
        with self.assertRaises(SegmentLimitError) as ctx:
            normalize([chr(big)], table)
        self.assertEqual(ctx.exception.limit, 1024)
        self.assertEqual(ctx.exception.actual, 1025)

    def test_segment_at_limit_accepted(self) -> None:
        table, big = self._exploding_table(DEFAULT_SEGMENT_LIMIT)
        result = normalize([chr(big)], table)
        # a + 1023 marks；无组合映射，全部保留。
        self.assertEqual(len(result.codepoints), 1024)

    def test_custom_small_limit(self) -> None:
        table, big = self._exploding_table(5)  # a + 4 marks → 5 个码点
        self.assertEqual(len(normalize([chr(big)], table, segment_limit=5).codepoints), 5)
        with self.assertRaises(SegmentLimitError):
            normalize([chr(big)], table, segment_limit=4)

    def test_overflow_feed_is_atomic(self) -> None:
        # feed 中段闭合会先冲刷早先的 'e'，超限失败后冲刷也必须回滚。
        table, big = self._exploding_table(DEFAULT_SEGMENT_LIMIT + 1)
        sn = StreamNormalizer(table)
        sn.feed("e")
        with self.assertRaises(SegmentLimitError) as ctx:
            sn.feed(chr(A) + chr(big))
        self.assertEqual(ctx.exception.chunk_index, 1)
        result = sn.finish()
        self.assertEqual(result.text, "e")

    def test_new_starter_resets_segment_count(self) -> None:
        # 每个 starter 重新计段：1024 个 "a" 不成问题。
        result = normalize(["a" * 2000], build_demo_table())
        self.assertEqual(len(result.codepoints), 2000)


class TableCompilationTests(unittest.TestCase):
    def test_compiled_table_is_returned_unchanged(self) -> None:
        compiled = compile_table(build_demo_table())
        self.assertIs(compile_table(compiled), compiled)

    def test_compile_rejects_contradictory_composition(self) -> None:
        other = 0xE180
        table = DictCharacterTable(
            decompositions={
                P_A_GRAVE: (A, M_GRAVE),
                other: (E, M_ACUTE),
            },
            combining_classes={M_GRAVE: 230, M_ACUTE: 230},
            compositions={(A, M_GRAVE): other},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIn("contradicts recursive decomposition", str(ctx.exception))
        self.assertEqual(ctx.exception.entry.kind, "composition")

    def test_compile_rejects_non_starter_left_operand(self) -> None:
        table = DictCharacterTable(
            combining_classes={M_GRAVE: 230, M_ACUTE: 230},
            compositions={(M_GRAVE, M_ACUTE): P_A_GRAVE},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIn("left operand ccc", str(ctx.exception))
        self.assertEqual(ctx.exception.entry.key, (M_GRAVE, M_ACUTE))

    def test_compile_rejects_starter_right_operand(self) -> None:
        target = 0xE180
        table = DictCharacterTable(
            compositions={(A, E): target},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIn("right operand ccc", str(ctx.exception))
        self.assertEqual(ctx.exception.codepoint, E)

    def test_compile_rejects_non_starter_composition_result(self) -> None:
        table = DictCharacterTable(
            combining_classes={M_GRAVE: 230, P_A_GRAVE: 230},
            compositions={(A, M_GRAVE): P_A_GRAVE},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIn("composition result ccc", str(ctx.exception))
        self.assertEqual(ctx.exception.codepoint, P_A_GRAVE)

    def test_compile_rejects_starter_after_nonstarter_decomposition(self) -> None:
        table = DictCharacterTable(
            decompositions={P_A_GRAVE: (A, M_GRAVE, E)},
            combining_classes={M_GRAVE: 230},
            compositions={(A, M_GRAVE): P_A_GRAVE},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIn("starter after a non-starter", str(ctx.exception))
        self.assertEqual(ctx.exception.codepoint, P_A_GRAVE)

    def test_compile_rejects_nonstarter_recursive_expansion_with_starter(self) -> None:
        intermediate = 0xE181
        table = DictCharacterTable(
            decompositions={
                M_GRAVE: (intermediate,),
                intermediate: (A, M_ACUTE),
            },
            combining_classes={M_GRAVE: 230, M_ACUTE: 230},
            compositions={(A, M_ACUTE): intermediate},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIn("non-starter decomposition contains a starter", str(ctx.exception))
        self.assertEqual(ctx.exception.codepoint, M_GRAVE)

    def test_compiled_table_snapshot_is_independent(self) -> None:
        source = build_demo_table()
        compiled = compile_table(source)
        source.table_entries = None  # type: ignore[method-assign]
        first = StreamNormalizer(compiled)
        second = StreamNormalizer(compiled)
        first.feed(chr(A) + chr(M_GRAVE))
        second.feed(chr(E) + chr(M_ACUTE))
        self.assertEqual(first.finish().codepoints, (P_A_GRAVE,))
        self.assertEqual(second.finish().codepoints, (P_E_ACUTE,))

    def test_compiled_table_survives_source_mapping_mutation(self) -> None:
        source = build_demo_table()
        compiled = compile_table(source)
        source.table_entries().decompositions.clear()
        source.table_entries().combining_classes.clear()
        source.table_entries().compositions.clear()
        result = normalize([chr(A) + chr(M_GRAVE)], compiled)
        self.assertEqual(result.codepoints, (P_A_GRAVE,))


class InvalidInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_bool_codepoint_path_via_limit(self) -> None:
        with self.assertRaises(InvalidInputError):
            StreamNormalizer(self.table, segment_limit=True)  # type: ignore[arg-type]

    def test_float_nan_inf_limit_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(InvalidInputError):
                StreamNormalizer(self.table, segment_limit=value)  # type: ignore[arg-type]

    def test_non_positive_limit_rejected(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaises(InvalidInputError):
                StreamNormalizer(self.table, segment_limit=value)

    def test_non_string_chunk(self) -> None:
        with self.assertRaises(InvalidInputError) as ctx:
            normalize([b"ab"], self.table)  # type: ignore[list-item]
        self.assertEqual(ctx.exception.chunk_index, 0)
        sn = StreamNormalizer(self.table)
        with self.assertRaises(InvalidInputError):
            sn.feed(123)  # type: ignore[arg-type]

    def test_single_str_chunks_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            normalize("abc", self.table)  # type: ignore[arg-type]

    def test_non_iterable_chunks_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            normalize(123, self.table)  # type: ignore[arg-type]

    def test_non_enumerable_table_rejected_at_compilation(self) -> None:
        with self.assertRaises(InvalidTableError):
            StreamNormalizer(object())

    def test_error_is_value_error_and_base_exception(self) -> None:
        from canoncompose import CanonicalError

        with self.assertRaises(ValueError):
            normalize([b"x"], self.table)  # type: ignore[list-item]
        self.assertTrue(issubclass(InvalidInputError, CanonicalError))
        self.assertTrue(issubclass(SegmentLimitError, CanonicalError))


class InvalidTableTests(unittest.TestCase):
    def test_decomposition_must_be_tuple_or_none(self) -> None:
        class T:
            @staticmethod
            def table_entries():
                return {
                    "decompositions": {1: [A]},
                    "combining_classes": {},
                    "compositions": {},
                }
        with self.assertRaises(InvalidTableError):
            compile_table(T())

    def test_decomposition_empty_tuple_rejected(self) -> None:
        table = DictCharacterTable(decompositions={1: ()})
        with self.assertRaises(InvalidTableError):
            normalize([chr(1)], table)

    def test_decomposition_bad_codepoint_rejected(self) -> None:
        table = DictCharacterTable(decompositions={1: (0x110000,)})
        with self.assertRaises(InvalidTableError):
            normalize([chr(1)], table)

    def test_ccc_must_be_non_negative_int(self) -> None:
        class T:
            @staticmethod
            def table_entries():
                return {
                    "decompositions": {},
                    "combining_classes": {A: -1},
                    "compositions": {},
                }
        with self.assertRaises(InvalidTableError):
            compile_table(T())

        class T2:
            @staticmethod
            def table_entries():
                return {
                    "decompositions": {},
                    "combining_classes": {A: True},
                    "compositions": {},
                }
        with self.assertRaises(InvalidTableError):
            compile_table(T2())

    def test_composition_out_of_range_rejected(self) -> None:
        table = DictCharacterTable(
            combining_classes={M_ACUTE: 230},
            compositions={(A, M_ACUTE): 0x110000},
        )
        with self.assertRaises(InvalidTableError):
            normalize([chr(A), chr(M_ACUTE)], table)

    def test_error_points_to_specific_table_entry(self) -> None:
        table = DictCharacterTable(
            compositions={(A, M_ACUTE): P_E_ACUTE},
            combining_classes={M_ACUTE: 0},
        )
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertIsNotNone(ctx.exception.entry)
        self.assertIn("composition", str(ctx.exception))


class SourceSpanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_composed_output_unites_sources(self) -> None:
        # a 与 grave 分属不同 chunk，组合后来源区间集合覆盖两者。
        result = normalize([chr(A), chr(M_GRAVE)], self.table)
        self.assertEqual(_cps(result), (P_A_GRAVE,))
        self.assertEqual(
            _spans(result), ((0, 0, 1), (1, 0, 1))
        )

    def test_sources_cover_leading_and_composed_segments(self) -> None:
        # 前导记号段 + 后续 a+grave 组合段：来源集合覆盖全部三个输入点。
        result = normalize(
            [chr(M_GRAVE) + chr(A) + chr(M_GRAVE)], self.table
        )
        self.assertEqual(_cps(result), (M_GRAVE, P_A_GRAVE))
        # 输入偏移 0（前导 grave）、1（a）、2（grave，与 a 合并）连续。
        self.assertEqual(_spans(result), ((0, 0, 3),))


if __name__ == "__main__":
    unittest.main(verbosity=2)
