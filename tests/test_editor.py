"""按位置插入/删除接口的必要测试。

期望值来源：
1. 手算的固定小例子（组合、阻塞、重排、首尾编辑、来源区间）；
2. tests/reference.py 的独立朴素参考：编辑在"参考规范化结果"上拼接后，
   再用参考实现规范化，与被测编辑器结果逐一比对（参考不导入被测核心）；
3. 位置映射在测试内独立重建（直接由两序列 LCP/LCS 列表推导），
   不调用编辑器内部的映射构造。
"""

from __future__ import annotations

import math
import random
import unittest

from canoncompose import (
    INSERT_CHUNK_INDEX,
    DecompositionCycleError,
    DictCharacterTable,
    InvalidInputError,
    NormalizedEditor,
    SegmentLimitError,
    delete_text,
    insert_text,
    normalize,
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
    build_cyclic_table,
    build_demo_table,
    describe,
)

from reference import reference_normalize


def _spans(result):
    return tuple(
        (s.chunk_index, s.start, s.end) for s in result.source_spans
    )


def _ref_cps(text, table):
    cps, _ = reference_normalize([text], table)
    return cps


def _lcp_lcs(old, new):
    lcp = 0
    m = min(len(old), len(new))
    while lcp < m and old[lcp] == new[lcp]:
        lcp += 1
    lcs = 0
    while (
        lcp + lcs < len(old)
        and lcp + lcs < len(new)
        and old[len(old) - 1 - lcs] == new[len(new) - 1 - lcs]
    ):
        lcs += 1
    return lcp, lcs


def _expected_map(old_len, new_len, lcp, lcs):
    """测试内独立重建期望缝映射（按文档契约，与编辑器实现分开书写）。

    内部缝按其**所在侧**到区间两端的距离夹到较近边界（等距归起点）。
    """
    old_end = old_len - lcs
    new_end = new_len - lcs

    def map_old(i):
        if i < lcp:
            return i
        if i > old_end:
            return i - old_end + new_end
        if lcp == old_end:  # 旧侧变化区为空：起点缝归起点，共享缝归终点
            return lcp if i == 0 else new_end
        if i == old_end:
            return new_end
        if i == lcp:
            return lcp
        return lcp if i - lcp <= old_end - i else new_end

    def map_new(j):
        if j < lcp:
            return j
        if j > new_end:
            return j - new_end + old_end
        if lcp == new_end:  # 新侧变化区为空
            return lcp if j == 0 else old_end
        if j == new_end:
            return old_end
        if j == lcp:
            return lcp
        return lcp if j - lcp <= new_end - j else old_end

    o2n = tuple(map_old(i) for i in range(old_len + 1))
    n2o = tuple(map_new(j) for j in range(new_len + 1))
    return o2n, n2o


class InsertHandComputedTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_insert_mark_composes_with_base(self):
        # 手算："a" 规范化后为 (a,)；在尾部插入 grave(230) →
        # 候选 a+grave，段闭合组合为 P_A_GRAVE。
        # 连基字符码点都变了，所以 LCP=0。
        ed = NormalizedEditor(self.table, chr(A))
        result = ed.insert(1, chr(M_GRAVE))
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        self.assertEqual(ed.text, chr(P_A_GRAVE))
        self.assertEqual(result.unchanged_prefix, 0)
        self.assertEqual(result.unchanged_suffix, 0)
        self.assertEqual(result.old_changed_range, (0, 1))
        self.assertEqual(result.changed_range, (0, 1))
        self.assertEqual(result.inserted, chr(M_GRAVE))
        self.assertEqual(result.deleted, "")

    def test_insert_trailing_mark_leaves_base_unchanged(self):
        # 手算："a"+grave 已组合为 P_A_GRAVE；尾部插入 dotbelow(220)。
        # 候选重分解为 a,grave,dotbelow → 排序 a,dotbelow,grave：
        # dotbelow 无映射成 pending；grave(230)>220 组合 → (P_A_GRAVE, DOTBELOW)。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        result = ed.insert(1, chr(M_DOTBELOW))
        self.assertEqual(result.codepoints, (P_A_GRAVE, M_DOTBELOW))
        self.assertEqual(result.unchanged_prefix, 1)
        self.assertEqual(result.old_changed_range, (1, 1))
        self.assertEqual(result.changed_range, (1, 2))

    def test_insert_blocking_same_ccc(self):
        # 手算阻塞：规范化文本 (a, acute230)（acute 与 a 无组合映射）。
        # 在偏移 1 插入 grave(230)：候选 a,grave,acute，等 ccc 稳定序
        # grave 在 acute 前。grave 前无未组合项 → 组合成 P_A_GRAVE；
        # acute 前 pending 为空（prev 视 0）→ 允许查询但无映射 → 保留。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_ACUTE))
        self.assertEqual(ed.codepoints, (A, M_ACUTE))
        result = ed.insert(1, chr(M_GRAVE))
        self.assertEqual(
            result.codepoints,
            (P_A_GRAVE, M_ACUTE),
            describe(result.codepoints),
        )

    def test_insert_reorders_within_segment(self):
        # 在 a 后先放 grave（已组合为 P_A_GRAVE），再在"基字符之后"
        # 插入 ring 之外的高类记号场景：改用 dotbelow 已在上面覆盖；
        # 这里插入 ring(232)：候选 P_A_GRAVE + ring → 链式 P_A_GRAVE_RING。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        result = ed.insert(1, chr(M_RING))
        self.assertEqual(result.codepoints, (P_A_GRAVE_RING,))

    def test_insert_empty_is_noop(self):
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        before = ed.text
        result = ed.insert(1, "")
        self.assertEqual(result.text, before)
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        self.assertEqual(result.changed_range, (1, 1))
        self.assertEqual(result.old_changed_range, (1, 1))
        # 空编辑的映射必须是恒等。
        self.assertEqual(
            result.position_map.old_to_new,
            tuple(range(len(before) + 1)),
        )
        self.assertEqual(
            result.position_map.new_to_old,
            tuple(range(len(before) + 1)),
        )

    def test_insert_at_start_shifts_existing(self):
        # 首部插入 starter e：候选 e, (a+grave) → (e, P_A_GRAVE)。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        result = ed.insert(0, chr(E))
        self.assertEqual(result.codepoints, (E, P_A_GRAVE))
        self.assertEqual(result.unchanged_prefix, 0)
        self.assertEqual(result.unchanged_suffix, 1)
        self.assertEqual(result.old_changed_range, (0, 0))
        self.assertEqual(result.changed_range, (0, 1))

    def test_insert_combining_mark_at_start_never_composes(self):
        # 首编辑：在 P_A_GRAVE 前插入 grave → 前导 grave 自成段不组合。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        result = ed.insert(0, chr(M_GRAVE))
        self.assertEqual(result.codepoints, (M_GRAVE, P_A_GRAVE))

    def test_insert_at_end_appends(self):
        # 尾编辑：在 (e, P_A_GRAVE) 尾部插入 o。
        ed = NormalizedEditor(
            self.table, chr(E) + chr(A) + chr(M_GRAVE)
        )
        self.assertEqual(ed.codepoints, (E, P_A_GRAVE))
        result = ed.insert(2, chr(0x6F))  # 'o'
        self.assertEqual(result.codepoints, (E, P_A_GRAVE, 0x6F))
        self.assertEqual(result.changed_range, (2, 3))


class DeleteHandComputedTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_delete_trailing_uncombined_mark(self):
        # (P_A_GRAVE, dotbelow) 删掉偏移 1 的 dotbelow → P_A_GRAVE。
        ed = NormalizedEditor(
            self.table, chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
        )
        self.assertEqual(ed.codepoints, (P_A_GRAVE, M_DOTBELOW))
        result = ed.delete_range(1)
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        self.assertEqual(result.deleted, chr(M_DOTBELOW))
        self.assertEqual(result.old_changed_range, (1, 2))
        self.assertEqual(result.changed_range, (1, 1))
        self.assertEqual(result.unchanged_prefix, 1)

    def test_delete_first_codepoint(self):
        # (e, P_A_GRAVE) 删首码点 → P_A_GRAVE（后缀保留 1）。
        ed = NormalizedEditor(
            self.table, chr(E) + chr(A) + chr(M_GRAVE)
        )
        result = ed.delete_range(0, 1)
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        self.assertEqual(result.unchanged_prefix, 0)
        self.assertEqual(result.unchanged_suffix, 1)
        self.assertEqual(result.old_changed_range, (0, 1))
        self.assertEqual(result.changed_range, (0, 0))

    def test_delete_composed_char_removes_whole_cluster(self):
        # 规范化串里 P_A_GRAVE 是单个码点：删除它就去掉整个组合簇——
        # 位置是相对规范串的，不存在"只删基字符保留记号"的位置。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        result = ed.delete_range(0, 1)
        self.assertEqual(result.codepoints, ())
        self.assertEqual(result.text, "")

    def test_delete_empty_range_is_noop(self):
        ed = NormalizedEditor(self.table, chr(A))
        result = ed.delete_range(0, 0)
        self.assertEqual(result.codepoints, (A,))
        # 未变化：LCP 抵达尾部，变化区间退化为尾缝处的空区间。
        self.assertEqual(result.changed_range, (1, 1))
        self.assertEqual(result.old_changed_range, (1, 1))
        self.assertEqual(result.deleted, "")

    def test_delete_all_then_insert_recovers(self):
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        ed.delete_range(0, 1)
        self.assertEqual(ed.text, "")
        result = ed.insert(0, chr(A) + chr(M_GRAVE))
        self.assertEqual(result.codepoints, (P_A_GRAVE,))


class SourceMappingHandTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_spans_label_inserted_and_original_sources(self):
        # a(chunk0,off0) 后插入 grave(特殊 chunk -1, off0)，组合后来源并集。
        ed = NormalizedEditor(self.table, chr(A))
        result = ed.insert(1, chr(M_GRAVE))
        self.assertEqual(_spans(result), ((INSERT_CHUNK_INDEX, 0, 1), (0, 0, 1)))

    def test_spans_after_delete_drop_removed_points(self):
        # 原文 a grave dotbelow（chunk0 偏移 0/1/2），删掉规范串偏移 1 的
        # dotbelow 后，来源只剩偏移 0、1，连续合并为 [0,2)。
        ed = NormalizedEditor(
            self.table, chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
        )
        result = ed.delete_range(1)
        self.assertEqual(_spans(result), ((0, 0, 2),))

    def test_successive_inserts_get_distinct_source_labels(self):
        # 连续两次插入必须使用不同的负 chunk 下标，否则各自从 0 开始的
        # 偏移会被错误合并成一个区间。
        ed = NormalizedEditor(self.table, chr(A))
        r1 = ed.insert(1, chr(E))
        r2 = ed.insert(2, chr(0x6F))
        self.assertEqual(r2.codepoints, (A, E, 0x6F))
        self.assertEqual(_spans(r2), ((-2, 0, 1), (-1, 0, 1), (0, 0, 1)))
        self.assertEqual(
            _spans(r1), ((INSERT_CHUNK_INDEX, 0, 1), (0, 0, 1))
        )

    def test_position_map_insert_at_start(self):
        # 旧长 2（e, P_A_GRAVE），新长 3（grave?, ...）；用 starter 插入：
        # lcp=0, lcs=1，手算期望映射。
        ed = NormalizedEditor(
            self.table, chr(E) + chr(A) + chr(M_GRAVE)
        )
        result = ed.insert(0, chr(0x6F))
        self.assertEqual(len(result.codepoints), 3)
        # 旧缝 0→新0；旧缝1→新2；旧缝2→新3。
        self.assertEqual(result.position_map.old_to_new, (0, 2, 3))
        # 新缝1 落在变化区内部，夹到边界 0。
        self.assertEqual(result.position_map.new_to_old, (0, 0, 1, 2))

    def test_position_map_is_monotone_and_in_range(self):
        # 通用性质：映射保序（非降）、值在对侧范围内、首尾边界对应首尾。
        ed = NormalizedEditor(self.table, chr(A) + chr(M_GRAVE))
        result = ed.insert(1, chr(M_DOTBELOW))
        o2n = result.position_map.old_to_new
        n2o = result.position_map.new_to_old
        self.assertEqual(o2n[0], 0)
        self.assertEqual(o2n[-1], len(result.text))
        self.assertEqual(n2o[0], 0)
        # 旧规范串是单个 P_A_GRAVE（长 1），不是原始的 a+grave（长 2）。
        self.assertEqual(n2o[-1], 1)
        self.assertTrue(all(x <= y for x, y in zip(o2n, o2n[1:])))
        self.assertTrue(all(x <= y for x, y in zip(n2o, n2o[1:])))
        self.assertTrue(all(0 <= x <= len(result.text) for x in o2n))
        self.assertTrue(all(0 <= x <= 1 for x in n2o))
        # 保留缝（变化区之外）复合往返必须恒等。
        old_len = 1
        retained = (
            list(range(result.unchanged_prefix + 1))
            + list(range(old_len - result.unchanged_suffix, old_len + 1))
        )
        for i in dict.fromkeys(retained):
            self.assertEqual(n2o[o2n[i]], i)

    def test_retained_seams_roundtrip(self):
        # 变化区外的缝往返必须回到自身。
        # 旧规范串 (e, P_A_GRAVE, dotbelow)，长 3。
        ed = NormalizedEditor(
            self.table, chr(E) + chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
        )
        self.assertEqual(tuple(ed.codepoints), (E, P_A_GRAVE, M_DOTBELOW))
        old_len = len(ed.codepoints)
        result = ed.insert(2, chr(M_ACUTE))
        o2n, n2o = result.position_map.old_to_new, result.position_map.new_to_old
        lcp, lcs = result.unchanged_prefix, result.unchanged_suffix
        retained = list(range(lcp + 1)) + list(
            range(old_len - lcs, old_len + 1)
        )
        for i in retained:
            self.assertEqual(n2o[o2n[i]], i, f"seam {i}")


class FunctionalAndStateTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_functional_insert_delete(self):
        r1 = insert_text(chr(A), 1, chr(M_GRAVE), self.table)
        self.assertEqual(r1.codepoints, (P_A_GRAVE,))
        r2 = delete_text(
            chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW), 1, None, self.table
        )
        self.assertEqual(r2.codepoints, (P_A_GRAVE,))

    def test_constructor_normalizes_raw_text(self):
        ed = NormalizedEditor(self.table, chr(P_A_GRAVE_RING))
        self.assertEqual(ed.codepoints, (P_A_GRAVE_RING,))

    def test_empty_editor(self):
        ed = NormalizedEditor(self.table)
        self.assertEqual(ed.text, "")
        result = ed.insert(0, chr(A))
        self.assertEqual(result.codepoints, (A,))

    def test_stateful_sequence_matches_one_shot(self):
        # 连续插入两次，结果与把最终候选串一次性规范化一致（参考实现）。
        ed = NormalizedEditor(self.table, chr(A))
        ed.insert(1, chr(M_DOTBELOW))
        ed.insert(2, chr(M_GRAVE))
        final = chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE)
        self.assertEqual(ed.codepoints, _ref_cps(final, self.table))

    def test_set_text_replaces_and_returns_map(self):
        ed = NormalizedEditor(self.table, chr(A))
        result = ed.set_text(chr(E) + chr(M_ACUTE))
        # 参考独立计算期望。
        self.assertEqual(
            result.codepoints, _ref_cps(chr(E) + chr(M_ACUTE), self.table)
        )
        self.assertEqual(result.inserted, chr(E) + chr(M_ACUTE))
        self.assertEqual(result.deleted, chr(A))

    def test_editor_text_is_always_normalized(self):
        ed = NormalizedEditor(self.table)
        ed.set_text(chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE))
        once = normalize([ed.text], self.table)
        self.assertEqual(ed.codepoints, once.codepoints)


class RandomizedAgainstReferenceTests(unittest.TestCase):
    """小规模确定性枚举：随机文本 + 随机插入/删除，码点序列与独立参考一致。"""

    def setUp(self):
        self.table = build_demo_table()
        self.alphabet = [
            A, E, M_GRAVE, M_ACUTE, M_DOTBELOW, M_RING,
            P_A_GRAVE, P_A_GRAVE_RING,
        ]

    def test_random_edits(self):
        rng = random.Random(20260924)
        for trial in range(120):
            raw = "".join(
                chr(rng.choice(self.alphabet))
                for _ in range(rng.randint(0, 12))
            )
            ed = NormalizedEditor(self.table, raw)
            # 参考规范化作为"旧规范串"。
            old_cps = list(_ref_cps(raw, self.table))
            self.assertEqual(list(ed.codepoints), old_cps, f"init trial={trial}")

            for _step in range(rng.randint(1, 4)):
                old_cps = list(ed.codepoints)
                if rng.random() < 0.6 or not old_cps:
                    pos = rng.randint(0, len(old_cps))
                    ins = "".join(
                        chr(rng.choice(self.alphabet))
                        for _ in range(rng.randint(0, 4))
                    )
                    result = ed.insert(pos, ins)
                    # 独立期望：在参考规范串上同位置拼接后再参考规范化。
                    cand = (
                        "".join(chr(c) for c in old_cps[:pos])
                        + ins
                        + "".join(chr(c) for c in old_cps[pos:])
                    )
                else:
                    start = rng.randint(0, len(old_cps) - 1)
                    end = rng.randint(start, len(old_cps))
                    result = ed.delete_range(start, end)
                    cand = (
                        "".join(chr(c) for c in old_cps[:start])
                        + "".join(chr(c) for c in old_cps[end:])
                    )
                expected = _ref_cps(cand, self.table)
                self.assertEqual(
                    result.codepoints, expected,
                    f"trial={trial} got {describe(result.codepoints)} "
                    f"want {describe(expected)}",
                )
                # 区间/映射独立复算。
                lcp, lcs = _lcp_lcs(tuple(old_cps), expected)
                self.assertEqual(result.unchanged_prefix, lcp)
                self.assertEqual(result.unchanged_suffix, lcs)
                self.assertEqual(
                    result.changed_range, (lcp, len(expected) - lcs)
                )
                self.assertEqual(
                    result.old_changed_range, (lcp, len(old_cps) - lcs)
                )
                e_o2n, e_n2o = _expected_map(
                    len(old_cps), len(expected), lcp, lcs
                )
                self.assertEqual(result.position_map.old_to_new, e_o2n)
                self.assertEqual(result.position_map.new_to_old, e_n2o)
                # 结构性性质（独立于具体映射约定）：
                # 保序、值域合法；非空侧首尾缝锚定对侧首/尾；整侧被删空时
                # 唯一缝（0=首=尾）锚定对侧文档原点 0。
                o2n = result.position_map.old_to_new
                n2o = result.position_map.new_to_old
                self.assertTrue(all(x <= y for x, y in zip(o2n, o2n[1:])))
                self.assertTrue(all(x <= y for x, y in zip(n2o, n2o[1:])))
                self.assertTrue(all(0 <= x <= len(expected) for x in o2n))
                self.assertTrue(all(0 <= x <= len(old_cps) for x in n2o))
                if len(old_cps) > 0:
                    self.assertEqual(o2n[0], 0)
                if len(expected) > 0:
                    self.assertEqual(n2o[0], 0)
                if len(old_cps) > 0 and len(expected) > 0:
                    # 两侧均非空时尾缝各自锚定对侧尾缝。
                    self.assertEqual(o2n[-1], len(expected))
                    self.assertEqual(n2o[-1], len(old_cps))
                else:
                    # 一侧为空：空侧唯一缝（首=尾）锚定对侧文档原点。
                    self.assertEqual(o2n[0], 0)
                    self.assertEqual(n2o[0], 0)


class InvalidEditTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_insert_pos_out_of_range(self):
        ed = NormalizedEditor(self.table, chr(A))
        with self.assertRaises(InvalidInputError):
            ed.insert(2, "x")
        with self.assertRaises(InvalidInputError):
            ed.insert(-1, "x")

    def test_delete_bad_ranges(self):
        ed = NormalizedEditor(self.table, chr(A) + chr(E))
        with self.assertRaises(InvalidInputError):
            ed.delete_range(0, 3)
        with self.assertRaises(InvalidInputError):
            ed.delete_range(1, 0)
        with self.assertRaises(InvalidInputError):
            ed.delete_range(-1)
        with self.assertRaises(InvalidInputError):
            ed.delete_range(2)  # 删末端之后的一个码点

    def test_position_rejects_bool_float_nan_types(self):
        ed = NormalizedEditor(self.table, chr(A))
        for bad in (True, False, 1.0, 1.5, math.nan, math.inf, "1", None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    ed.insert(bad, "x")  # type: ignore[arg-type]

    def test_insertion_must_be_str(self):
        ed = NormalizedEditor(self.table, chr(A))
        with self.assertRaises(InvalidInputError):
            ed.insert(0, b"x")  # type: ignore[arg-type]

    def test_text_must_be_str(self):
        with self.assertRaises(InvalidInputError):
            NormalizedEditor(self.table, b"a")  # type: ignore[arg-type]
        ed = NormalizedEditor(self.table)
        with self.assertRaises(InvalidInputError):
            ed.set_text(123)  # type: ignore[arg-type]

    def test_bad_table_rejected(self):
        with self.assertRaises(InvalidInputError):
            NormalizedEditor(object())

    def test_segment_limit_validation(self):
        with self.assertRaises(InvalidInputError):
            NormalizedEditor(self.table, "a", segment_limit=0)
        with self.assertRaises(InvalidInputError):
            NormalizedEditor(self.table, "a", segment_limit=True)  # type: ignore[arg-type]


class AtomicityTests(unittest.TestCase):
    def setUp(self):
        self.table = build_demo_table()

    def test_cyclic_insert_fails_atomically(self):
        # 插入含分解环的码点：抛 DecompositionCycleError，文本与状态不变。
        # 用含环表（0xE200→0xE201→0xE200）；该表无组合映射，其余码点
        # 均按 starter 处理。
        table = build_cyclic_table()
        ed = NormalizedEditor(table, chr(A) + chr(E))
        before = ed.text
        with self.assertRaises(DecompositionCycleError):
            ed.insert(1, chr(0xE200))
        self.assertEqual(ed.text, before)
        self.assertEqual(ed.codepoints, (A, E))
        # 状态未半更新：继续编辑仍得到正确结果（grave 在该表中也是 starter）。
        result = ed.insert(2, chr(M_GRAVE))
        self.assertEqual(result.codepoints, (A, E, M_GRAVE))

    def test_cyclic_insert_with_cyclic_table(self):
        ed = NormalizedEditor(build_cyclic_table(), "a")
        with self.assertRaises(DecompositionCycleError):
            ed.insert(1, chr(0xE200))
        self.assertEqual(ed.text, "a")

    def test_segment_limit_enforced_and_atomic(self):
        # 小上限：插入会展开超限的码点，失败且状态不变。
        big = 0xE400
        marks = tuple(0xE500 + i for i in range(5))
        table = DictCharacterTable(
            decompositions={big: (A, *marks)},
            combining_classes={m: 230 for m in marks},
        )
        ed = NormalizedEditor(table, "zz", segment_limit=4)
        with self.assertRaises(SegmentLimitError):
            ed.insert(1, chr(big))
        self.assertEqual(ed.text, "zz")
        # 回滚后仍可做合法编辑。
        self.assertEqual(ed.insert(0, "z").codepoints, tuple(map(ord, "zzz")))

    def test_bad_position_does_not_touch_state(self):
        ed = NormalizedEditor(self.table, chr(A))
        for bad_call in (
            lambda: ed.insert(5, "x"),
            lambda: ed.delete_range(0, 9),
            lambda: ed.insert(0, 3),  # type: ignore[arg-type]
        ):
            with self.assertRaises(InvalidInputError):
                bad_call()
            self.assertEqual(ed.text, chr(A))

    def test_set_text_failure_keeps_old_text(self):
        # 含环表：set_text 一个环码点失败，原状态保持。
        ed = NormalizedEditor(build_cyclic_table(), chr(A) + chr(E))
        before = ed.text
        with self.assertRaises(DecompositionCycleError):
            ed.set_text(chr(0xE200))
        self.assertEqual(ed.text, before)
        self.assertEqual(ed.codepoints, (A, E))

    def test_insert_after_failure_keeps_chunk_coordinates(self):
        # 失败不消耗任何坐标：成功插入的来源仍从空状态正常记录。
        ed = NormalizedEditor(self.table, chr(A))
        with self.assertRaises(InvalidInputError):
            ed.insert(99, "x")
        result = ed.insert(1, chr(M_GRAVE))
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        self.assertEqual(
            _spans(result), ((INSERT_CHUNK_INDEX, 0, 1), (0, 0, 1))
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
