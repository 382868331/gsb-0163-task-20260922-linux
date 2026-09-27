"""字符表编译（事前一致性检查）的必要测试。

期望值均为测试内手写的固定预期，不调用被测核心计算期望值；
与 tests/reference.py 的交叉验证仅作为补充。
"""

from __future__ import annotations

import unittest

from canoncompose import (
    CompiledCharacterTable,
    CompositionConflictError,
    DecompositionCycleError,
    InconsistentLevelError,
    InvalidInputError,
    InvalidTableError,
    StreamNormalizer,
    TableCompilationError,
    compile_table,
    normalize,
)
from canoncompose.samples import (
    A,
    E,
    M_ACUTE,
    M_GRAVE,
    M_RING,
    O,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    P_O_ACUTE,
    CYCLE_X,
    CYCLE_Y,
    build_contradictory_table,
    build_cyclic_table,
    build_demo_table,
    build_inconsistent_level_table,
)
from canoncompose.table import DictCharacterTable

from reference import reference_normalize


class CompileValidTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiled = compile_table(build_demo_table())

    def test_demo_table_compiles(self) -> None:
        self.assertIsInstance(self.compiled, CompiledCharacterTable)
        # 协议三方法照常可用。
        self.assertEqual(self.compiled.decomposition(P_A_GRAVE), (A, M_GRAVE))
        self.assertEqual(self.compiled.combining_class(M_GRAVE), 230)
        self.assertEqual(self.compiled.combining_class(A), 0)
        self.assertEqual(self.compiled.composition(A, M_GRAVE), P_A_GRAVE)
        self.assertIsNone(self.compiled.composition(A, A))

    def test_expansion_precomputed(self) -> None:
        self.assertEqual(
            self.compiled.expansion(P_A_GRAVE_RING), (A, M_GRAVE, M_RING)
        )
        self.assertEqual(self.compiled.expansion(A), (A,))

    def test_compiled_table_reusable_across_streams(self) -> None:
        text1 = chr(P_A_GRAVE_RING) + chr(A) + chr(M_GRAVE)
        text2 = chr(E) + chr(M_ACUTE) + chr(O)
        first = normalize([text1], self.compiled)
        # 同一编译表直接喂给第二个相互独立的流。
        stream = StreamNormalizer(self.compiled)
        stream.feed(text2[:2])
        stream.feed(text2[2:])
        second = stream.finish()
        # 第三次：一次性 API 复用同一表。
        third = normalize([text1, text2], self.compiled)

        self.assertEqual(first.codepoints, (P_A_GRAVE_RING, P_A_GRAVE))
        self.assertEqual(second.codepoints, (0xE101, O))
        self.assertEqual(
            third.codepoints, first.codepoints + second.codepoints
        )
        # 来源区间追踪在编译表上继续保留。
        self.assertEqual(
            tuple(
                (s.chunk_index, s.start, s.end) for s in first.source_spans
            ),
            ((0, 0, 3),),
        )
        self.assertEqual(
            tuple(
                (s.chunk_index, s.start, s.end) for s in second.source_spans
            ),
            ((0, 0, 2), (1, 0, 1)),
        )

    def test_compiled_matches_independent_reference(self) -> None:
        # 补充交叉验证：编译表上的结果与独立朴素参考一致。
        text = chr(P_A_GRAVE_RING) + chr(A) + chr(M_GRAVE) + chr(E)
        expected_cps, _ = reference_normalize([text], build_demo_table())
        self.assertEqual(
            normalize([text], self.compiled).codepoints, expected_cps
        )

    def test_compiled_table_is_snapshot_of_source(self) -> None:
        source = build_demo_table()
        compiled = compile_table(source)
        # 编译后修改源表内部映射，不影响已定稿的编译表。
        source._decomp[A] = (E,)
        source._comp.clear()
        self.assertEqual(compiled.decomposition(P_A_GRAVE), (A, M_GRAVE))
        self.assertIsNone(compiled.decomposition(A))
        self.assertEqual(compiled.composition(A, M_GRAVE), P_A_GRAVE)

    def test_recompiling_compiled_table(self) -> None:
        again = compile_table(self.compiled)
        self.assertEqual(again.decompositions, self.compiled.decompositions)
        self.assertEqual(again.compositions, self.compiled.compositions)


class CompileCycleTests(unittest.TestCase):
    def test_cycle_points_at_specific_entries(self) -> None:
        with self.assertRaises(DecompositionCycleError) as ctx:
            compile_table(build_cyclic_table())
        chain = ctx.exception.chain
        # 环闭合路径上的码点都来自具体表项 X/Y。
        self.assertEqual(chain[0], chain[-1])
        self.assertEqual(set(chain), {CYCLE_X, CYCLE_Y})
        self.assertIsInstance(ctx.exception, InvalidTableError)

    def test_self_cycle(self) -> None:
        table = DictCharacterTable(decompositions={0xE300: (0xE300,)})
        with self.assertRaises(DecompositionCycleError) as ctx:
            compile_table(table)
        self.assertEqual(ctx.exception.chain, (0xE300, 0xE300))


class CompileConflictTests(unittest.TestCase):
    def test_contradictory_composition_rejected(self) -> None:
        with self.assertRaises(CompositionConflictError) as ctx:
            compile_table(build_contradictory_table())
        exc = ctx.exception
        # 错误指向具体组合项 (a, grave) -> P_O_ACUTE。
        self.assertEqual(exc.left, A)
        self.assertEqual(exc.right, M_GRAVE)
        self.assertEqual(exc.result, P_O_ACUTE)
        self.assertEqual(exc.expected, (A, M_GRAVE))
        self.assertEqual(exc.actual, (O, M_ACUTE))
        self.assertIsInstance(exc, TableCompilationError)
        self.assertIsInstance(exc, InvalidTableError)

    def test_composition_result_without_matching_decomposition(self) -> None:
        # (a, grave) -> X，X 无分解：展开 (X,) != (a, grave)，矛盾。
        table = DictCharacterTable(
            decompositions={},
            combining_classes={M_GRAVE: 230},
            compositions={(A, M_GRAVE): 0xE600},
        )
        with self.assertRaises(CompositionConflictError) as ctx:
            compile_table(table)
        self.assertEqual(ctx.exception.result, 0xE600)
        self.assertEqual(ctx.exception.actual, (0xE600,))


class CompileLevelTests(unittest.TestCase):
    def test_composition_result_must_be_starter(self) -> None:
        with self.assertRaises(InconsistentLevelError) as ctx:
            compile_table(build_inconsistent_level_table())
        exc = ctx.exception
        self.assertEqual(exc.entry, "composition-result")
        self.assertEqual(exc.codepoint, M_RING)
        self.assertEqual(exc.left, A)
        self.assertEqual(exc.right, M_GRAVE)
        self.assertEqual(exc.expected_ccc, 0)
        self.assertEqual(exc.actual_ccc, 232)
        self.assertIsInstance(exc, TableCompilationError)

    def test_composition_second_must_be_non_starter(self) -> None:
        table = DictCharacterTable(
            decompositions={0xE610: (A, E)},
            compositions={(A, E): 0xE610},
        )
        with self.assertRaises(InconsistentLevelError) as ctx:
            compile_table(table)
        exc = ctx.exception
        self.assertEqual(exc.entry, "composition-second")
        self.assertEqual(exc.codepoint, E)
        self.assertEqual(exc.actual_ccc, 0)

    def test_decomposition_head_level_must_match(self) -> None:
        # 被分解字符 ccc=5，但分解首元素 ccc=0：等级不一致。
        table = DictCharacterTable(
            decompositions={0xE620: (A, M_GRAVE)},
            combining_classes={0xE620: 5, M_GRAVE: 230},
        )
        with self.assertRaises(InconsistentLevelError) as ctx:
            compile_table(table)
        exc = ctx.exception
        self.assertEqual(exc.entry, "decomposition")
        self.assertEqual(exc.codepoint, 0xE620)
        self.assertEqual(exc.expected_ccc, 5)
        self.assertEqual(exc.actual_ccc, 0)


class CompileEntryValidationTests(unittest.TestCase):
    def test_bool_ccc_rejected(self) -> None:
        table = DictCharacterTable(combining_classes={M_GRAVE: True})
        with self.assertRaises(InvalidTableError) as ctx:
            compile_table(table)
        self.assertEqual(ctx.exception.codepoint, M_GRAVE)

    def test_out_of_range_codepoint_rejected(self) -> None:
        table = DictCharacterTable(decompositions={0x110000: (A,)})
        with self.assertRaises(InvalidTableError):
            compile_table(table)

    def test_empty_decomposition_rejected(self) -> None:
        table = DictCharacterTable(decompositions={0xE700: ()})
        with self.assertRaises(InvalidTableError):
            compile_table(table)

    def test_non_mapping_table_rejected(self) -> None:
        with self.assertRaises(InvalidInputError):
            compile_table(object())

    def test_nan_codepoint_rejected(self) -> None:
        table = DictCharacterTable(combining_classes={float("nan"): 1})
        with self.assertRaises(InvalidTableError):
            compile_table(table)


class CompileFailureIsolationTests(unittest.TestCase):
    def test_failed_compile_does_not_affect_previous_tables(self) -> None:
        good = compile_table(build_demo_table())
        before = normalize([chr(P_A_GRAVE)], good)
        # 多次编译失败（环、矛盾、等级不一致）不影响先前可用的表。
        for bad in (
            build_cyclic_table(),
            build_contradictory_table(),
            build_inconsistent_level_table(),
        ):
            with self.assertRaises(InvalidTableError):
                compile_table(bad)
        after = normalize([chr(P_A_GRAVE)], good)
        self.assertEqual(before.codepoints, after.codepoints)
        self.assertEqual(before.source_spans, after.source_spans)
        self.assertEqual(after.codepoints, (P_A_GRAVE,))


if __name__ == "__main__":
    unittest.main()
