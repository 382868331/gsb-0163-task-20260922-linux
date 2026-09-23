"""编辑接口测试：按字符位置插入/删除、变化区间与新旧映射。

期望值分两类：
- 手算的固定用例（含组合、阻塞组合、首尾边界、空操作）；
- 确定性伪随机用例，用 tests/reference.py 的独立朴素实现核对编辑后的
  规范码点序列（拼接与切分由测试自行完成，不经过被测编辑接口），
  映射部分只断言不依赖被测实现细节的不变量。
"""

from __future__ import annotations

import math
import random
import unittest

from canoncompose import (
    DictCharacterTable,
    EditResult,
    InvalidInputError,
    DecompositionCycleError,
    SegmentLimitError,
    normalize,
    normalize_delete,
    normalize_insert,
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
)

from reference import reference_normalize


def _spans(result) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (s.chunk_index, s.start, s.end) for s in result.source_spans
    )


class InsertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_insert_plain_starter_in_middle(self) -> None:
        # "aoe" 在偏移 1 插入 'x' → "axoe"，全部 starter 直通。
        edit = normalize_insert(["aoe"], 1, "x", self.table)
        self.assertEqual(edit.result.codepoints, (A, ord("x"), O, E))
        self.assertEqual(edit.previous.codepoints, (A, O, E))
        # 公共前缀 'a'、公共后缀 'oe'。
        self.assertEqual(edit.old_span, (1, 1))
        self.assertEqual(edit.new_span, (1, 2))
        self.assertEqual(edit.mapping.new_to_old, (0, None, 1, 2))
        self.assertEqual(edit.mapping.old_to_new, (0, 2, 3))
        self.assertTrue(edit.changed)
        # 编辑后 chunk 划分为 ["a", "x", "oe"]。
        self.assertEqual(
            _spans(edit.result), ((0, 0, 1), (1, 0, 1), (2, 0, 2))
        )

    def test_insert_combining_mark_composes_with_previous_starter(self) -> None:
        # "ae" 在偏移 1 插入 grave(230) → a+grave 组合为 P_A_GRAVE。
        edit = normalize_insert(["ae"], 1, chr(M_GRAVE), self.table)
        self.assertEqual(edit.result.codepoints, (P_A_GRAVE, E))
        self.assertEqual(edit.old_span, (0, 1))
        self.assertEqual(edit.new_span, (0, 1))
        # 组合产物无旧对应；末尾 'e' 仍一一对应。
        self.assertEqual(edit.mapping.new_to_old, (None, 1))
        self.assertEqual(edit.mapping.old_to_new, (None, 1))

    def test_insert_at_head_leading_mark_never_composes(self) -> None:
        # 首部边界：前导记号段无基字符，不组合。
        edit = normalize_insert(["a"], 0, chr(M_GRAVE), self.table)
        self.assertEqual(edit.result.codepoints, (M_GRAVE, A))
        self.assertEqual(edit.old_span, (0, 0))
        self.assertEqual(edit.new_span, (0, 1))
        self.assertEqual(edit.mapping.new_to_old, (None, 0))
        self.assertEqual(edit.mapping.old_to_new, (1,))

    def test_insert_at_tail_composes(self) -> None:
        # 尾部边界：grave 追加到 'a' 后，组合吞掉整个旧输出。
        edit = normalize_insert(["a"], 1, chr(M_GRAVE), self.table)
        self.assertEqual(edit.result.codepoints, (P_A_GRAVE,))
        self.assertEqual(edit.old_span, (0, 1))
        self.assertEqual(edit.new_span, (0, 1))
        self.assertEqual(edit.mapping.new_to_old, (None,))
        self.assertEqual(edit.mapping.old_to_new, (None,))

    def test_insert_triggers_ccc_reorder(self) -> None:
        # 在 "a"+grave 末尾插入 dotbelow(220)：重排到 grave 之前，
        # grave 仍与 a 组合 → (P_A_GRAVE, dotbelow)。
        edit = normalize_insert(
            ["a" + chr(M_GRAVE)], 2, chr(M_DOTBELOW), self.table
        )
        self.assertEqual(edit.result.codepoints, (P_A_GRAVE, M_DOTBELOW))
        self.assertEqual(edit.previous.codepoints, (P_A_GRAVE,))
        self.assertEqual(edit.old_span, (1, 1))
        self.assertEqual(edit.new_span, (1, 2))
        self.assertEqual(edit.mapping.new_to_old, (0, None))
        self.assertEqual(edit.mapping.old_to_new, (0,))

    def test_insert_empty_text_is_noop(self) -> None:
        edit = normalize_insert(["ae"], 1, "", self.table)
        self.assertEqual(edit.result.codepoints, (A, E))
        self.assertFalse(edit.changed)
        self.assertEqual(edit.old_span, (2, 2))
        self.assertEqual(edit.new_span, (2, 2))
        self.assertEqual(edit.mapping.new_to_old, (0, 1))
        self.assertEqual(edit.mapping.old_to_new, (0, 1))

    def test_insert_into_empty_input(self) -> None:
        edit = normalize_insert([], 0, "a", self.table)
        self.assertEqual(edit.result.text, "a")
        self.assertEqual(edit.previous.codepoints, ())
        self.assertEqual(edit.old_span, (0, 0))
        self.assertEqual(edit.new_span, (0, 1))
        self.assertEqual(edit.mapping.new_to_old, (None,))
        self.assertEqual(edit.mapping.old_to_new, ())

    def test_insert_multichunk_source_spans(self) -> None:
        # 多 chunk 输入：插入文本自成一个 chunk，原 chunk 边界保留。
        edit = normalize_insert(["ao", "ea"], 2, "x", self.table)
        self.assertEqual(edit.result.text, "aoxea")
        self.assertEqual(
            _spans(edit.result), ((0, 0, 2), (1, 0, 1), (2, 0, 2))
        )
        self.assertEqual(
            _spans(edit.previous), ((0, 0, 2), (1, 0, 2))
        )
        self.assertEqual(edit.old_span, (2, 2))
        self.assertEqual(edit.new_span, (2, 3))
        self.assertEqual(edit.mapping.new_to_old, (0, 1, None, 2, 3))
        self.assertEqual(edit.mapping.old_to_new, (0, 1, 3, 4))

    def test_insert_chain_composition_across_edit_point(self) -> None:
        # 在 "a"+grave 后插入 ring(232)：grave 先组合成 P_A_GRAVE，
        # ring 继续链式组合为 P_A_GRAVE_RING。
        edit = normalize_insert(
            ["a" + chr(M_GRAVE)], 2, chr(M_RING), self.table
        )
        self.assertEqual(edit.result.codepoints, (P_A_GRAVE_RING,))
        self.assertEqual(edit.old_span, (0, 1))
        self.assertEqual(edit.new_span, (0, 1))
        self.assertEqual(edit.mapping.new_to_old, (None,))


class DeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_delete_tail_mark_undoes_composition(self) -> None:
        # 删除 "a"+grave 末尾的 grave：组合被撤销，输出回到 'a'。
        edit = normalize_delete(["a" + chr(M_GRAVE)], 1, 2, self.table)
        self.assertEqual(edit.previous.codepoints, (P_A_GRAVE,))
        self.assertEqual(edit.result.codepoints, (A,))
        self.assertEqual(edit.old_span, (0, 1))
        self.assertEqual(edit.new_span, (0, 1))
        self.assertEqual(edit.mapping.new_to_old, (None,))
        self.assertEqual(edit.mapping.old_to_new, (None,))

    def test_delete_head(self) -> None:
        edit = normalize_delete(["ea"], 0, 1, self.table)
        self.assertEqual(edit.result.codepoints, (A,))
        self.assertEqual(edit.old_span, (0, 1))
        self.assertEqual(edit.new_span, (0, 0))
        self.assertEqual(edit.mapping.old_to_new, (None, 0))
        self.assertEqual(edit.mapping.new_to_old, (1,))

    def test_delete_everything(self) -> None:
        edit = normalize_delete(["ae"], 0, 2, self.table)
        self.assertEqual(edit.result.text, "")
        self.assertEqual(edit.result.codepoints, ())
        self.assertEqual(edit.old_span, (0, 2))
        self.assertEqual(edit.new_span, (0, 0))
        self.assertEqual(edit.mapping.old_to_new, (None, None))
        self.assertEqual(edit.mapping.new_to_old, ())
        self.assertTrue(edit.changed)

    def test_delete_empty_range_is_noop(self) -> None:
        edit = normalize_delete(["ae"], 1, 1, self.table)
        self.assertEqual(edit.result.codepoints, (A, E))
        self.assertFalse(edit.changed)
        self.assertEqual(edit.old_span, (2, 2))
        self.assertEqual(edit.new_span, (2, 2))
        self.assertEqual(edit.mapping.old_to_new, (0, 1))

    def test_delete_removes_blocking_mark_and_unblocks_composition(self) -> None:
        # dotbelow 与 acute 同为 ccc 230：未组合的 dotbelow 阻塞 acute。
        # 删除 dotbelow 后阻塞解除，acute 与 a 组合。
        blocked_target = 0xE199
        table = DictCharacterTable(
            combining_classes={M_DOTBELOW: 230, M_ACUTE: 230},
            compositions={
                (A, M_ACUTE): P_E_ACUTE,
                (P_E_ACUTE, M_DOTBELOW): blocked_target,
            },
        )
        s = "a" + chr(M_DOTBELOW) + chr(M_ACUTE)
        edit = normalize_delete([s], 1, 2, table)
        # 编辑前：阻塞生效，三者都保留。
        self.assertEqual(
            edit.previous.codepoints, (A, M_DOTBELOW, M_ACUTE)
        )
        # 编辑后：a+acute → P_E_ACUTE。
        self.assertEqual(edit.result.codepoints, (P_E_ACUTE,))
        self.assertEqual(edit.old_span, (0, 3))
        self.assertEqual(edit.new_span, (0, 1))
        self.assertEqual(edit.mapping.old_to_new, (None, None, None))
        self.assertEqual(edit.mapping.new_to_old, (None,))

    def test_delete_across_chunk_boundary(self) -> None:
        # ["ao", "ea"] 删除拼接串 [1, 3)（'o' 与 'e'）→ "aa"。
        edit = normalize_delete(["ao", "ea"], 1, 3, self.table)
        self.assertEqual(edit.result.text, "aa")
        self.assertEqual(_spans(edit.result), ((0, 0, 1), (1, 0, 1)))
        self.assertEqual(edit.old_span, (1, 3))
        self.assertEqual(edit.new_span, (1, 1))
        self.assertEqual(edit.mapping.old_to_new, (0, None, None, 1))
        self.assertEqual(edit.mapping.new_to_old, (0, 3))

    def test_delete_precomposed_char_removes_whole_segment(self) -> None:
        # P_A_GRAVE 是一个输入字符：删除它等于删除整个组合段。
        edit = normalize_delete(
            [chr(P_A_GRAVE) + "e"], 0, 1, self.table
        )
        self.assertEqual(edit.previous.codepoints, (P_A_GRAVE, E))
        self.assertEqual(edit.result.codepoints, (E,))
        self.assertEqual(edit.old_span, (0, 1))
        self.assertEqual(edit.new_span, (0, 0))
        self.assertEqual(edit.mapping.old_to_new, (None, 0))


class EditErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_insert_offset_out_of_range(self) -> None:
        for bad in (-1, 3, 100):
            with self.subTest(offset=bad), self.assertRaises(InvalidInputError):
                normalize_insert(["ae"], bad, "x", self.table)

    def test_insert_offset_rejects_bool_and_float(self) -> None:
        for bad in (True, 1.5, math.nan, math.inf, -math.inf, "1"):
            with self.subTest(offset=bad), self.assertRaises(InvalidInputError):
                normalize_insert(["ae"], bad, "x", self.table)  # type: ignore[arg-type]

    def test_insert_text_must_be_str(self) -> None:
        for bad in (123, None, b"x"):
            with self.subTest(text=bad), self.assertRaises(InvalidInputError):
                normalize_insert(["ae"], 1, bad, self.table)  # type: ignore[arg-type]

    def test_delete_range_out_of_bounds(self) -> None:
        for start, end in ((-1, 1), (0, 3), (2, 1), (1, 0)):
            with self.subTest(start=start, end=end), self.assertRaises(
                InvalidInputError
            ):
                normalize_delete(["ae"], start, end, self.table)

    def test_delete_positions_reject_bool_and_float(self) -> None:
        with self.assertRaises(InvalidInputError):
            normalize_delete(["ae"], False, 1, self.table)  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError):
            normalize_delete(["ae"], 0, math.nan, self.table)  # type: ignore[arg-type]

    def test_chunks_contract_same_as_normalize(self) -> None:
        with self.assertRaises(InvalidInputError):
            normalize_insert("abc", 0, "x", self.table)  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError):
            normalize_delete(123, 0, 1, self.table)  # type: ignore[arg-type]
        with self.assertRaises(InvalidInputError):
            normalize_insert([b"ab"], 0, "x", self.table)  # type: ignore[list-item]

    def test_table_errors_propagate_unchanged(self) -> None:
        # 分解环：编辑接口不吞掉核心错误。
        with self.assertRaises(DecompositionCycleError):
            normalize_insert(["a"], 1, chr(0xE200), build_cyclic_table())

    def test_segment_limit_applies_to_edited_input(self) -> None:
        big = 0xE400
        marks = tuple(0xE500 + i for i in range(1024))
        table = DictCharacterTable(
            decompositions={big: (A, *marks)},
            combining_classes={m: 230 for m in marks},
        )
        with self.assertRaises(SegmentLimitError):
            normalize_insert(["a"], 1, chr(big), table)

    def test_invalid_edit_does_not_consume_anything(self) -> None:
        # 接口无状态：非法调用抛错后，同一批输入可正常再次使用。
        with self.assertRaises(InvalidInputError):
            normalize_insert(["ae"], 99, "x", self.table)
        edit = normalize_insert(["ae"], 1, "x", self.table)
        self.assertEqual(edit.result.text, "axe")


class EditRandomizedCrossCheckTests(unittest.TestCase):
    """确定性伪随机编辑：码点序列对独立参考，映射只断言不变量。"""

    def test_random_edits_match_reference(self) -> None:
        table = build_demo_table()
        alphabet = [
            A, E, ord("o"), M_GRAVE, M_ACUTE, M_DOTBELOW, M_RING,
            P_A_GRAVE, P_E_ACUTE, P_A_GRAVE_RING,
        ]
        rng = random.Random(20260924)
        for trial in range(80):
            n = rng.randint(0, 30)
            whole = "".join(chr(rng.choice(alphabet)) for _ in range(n))
            # 随机切成 1..3 个 chunk（切分由测试自己完成）。
            k = rng.randint(0, min(2, n))
            cuts = sorted(rng.sample(range(1, n + 1), k=k)) if k else []
            chunks, prev = [], 0
            for c in cuts + [n]:
                chunks.append(whole[prev:c])
                prev = c

            if rng.random() < 0.5 and n > 0:
                # 删除 [start, end)。
                start = rng.randint(0, n)
                end = rng.randint(start, n)
                edited = whole[:start] + whole[end:]
                edit = normalize_delete(chunks, start, end, table)
            else:
                # 在 offset 处插入 0..3 个字符。
                offset = rng.randint(0, n)
                piece = "".join(
                    chr(rng.choice(alphabet))
                    for _ in range(rng.randint(0, 3))
                )
                edited = whole[:offset] + piece + whole[offset:]
                edit = normalize_insert(chunks, offset, piece, table)

            # 独立参考：对拼接后的编辑结果整体规范化（单 chunk）。
            ref_cps, _ = reference_normalize([edited], table)
            self.assertEqual(
                edit.result.codepoints, ref_cps, f"trial={trial}"
            )
            # previous 与直接 normalize 一致。
            self.assertEqual(
                edit.previous.codepoints,
                normalize(chunks, table).codepoints,
                f"trial={trial} previous",
            )
            self._assert_mapping_invariants(edit, f"trial={trial}")

    def _assert_mapping_invariants(self, edit: EditResult, tag: str) -> None:
        old_cps = edit.previous.codepoints
        new_cps = edit.result.codepoints
        n2o = edit.mapping.new_to_old
        o2n = edit.mapping.old_to_new
        self.assertEqual(len(n2o), len(new_cps), tag)
        self.assertEqual(len(o2n), len(old_cps), tag)
        # 部分逆映射：n2o[i]==j 当且仅当 o2n[j]==i。
        for i, j in enumerate(n2o):
            if j is None:
                continue
            self.assertEqual(o2n[j], i, tag)
            self.assertEqual(new_cps[i], old_cps[j], tag)
        for j, i in enumerate(o2n):
            if i is None:
                continue
            self.assertEqual(n2o[i], j, tag)
        # 映射恰好覆盖变化区间之外：前缀恒等、后缀平移。
        os_, oe = edit.old_span
        ns, ne = edit.new_span
        self.assertEqual(os_, ns, tag)  # 公共前缀长度一致
        for i in range(ns):
            self.assertEqual(n2o[i], i, tag)
        for i in range(ns, ne):
            self.assertIsNone(n2o[i], tag)
        shift = len(new_cps) - len(old_cps)
        for i in range(ne, len(new_cps)):
            self.assertEqual(n2o[i], i - shift, tag)
        for j in range(os_, oe):
            self.assertIsNone(o2n[j], tag)
        # changed 标志与区间一致。
        self.assertEqual(
            edit.changed, os_ != oe or ns != ne, tag
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
