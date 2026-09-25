"""检查点（暂停/恢复）测试。

覆盖题面要求：

- 几个**可手算**字符表上的短输入，在**每个 chunk 边界**导出/JSON 往返/
  恢复，核对码点结果、来源区间、chunk 编号与从头连续处理完全一致；
  场景含前导组合符、等 ccc 阻塞、递归分解/链式组合、超长段失败后继续；
- 导出不偷偷 finish（挂起段原样保留）、导出 dict 被改动不影响活对象、
  finished 状态可保存且恢复后保持结束语义；
- 指纹只取决于规则内容（排序无关），表内容不同即拒绝恢复；
- 各类坏检查点（版本、字段、来源坐标、段形状、表不匹配）全部拒绝，
  不产生半恢复对象；
- 自定义（鸭子类型/子类）字符表正常处理不受影响，只是不能保存；
- **两个真实子进程**经 stdin/stdout 传 JSON 完成 save 与 resume，
  另一个进程凭 JSON 规则重建同内容字符表，不用 pickle、不共享对象。
"""

from __future__ import annotations

import copy
import json
import random
import subprocess
import sys
import unittest
from pathlib import Path

from canoncompose import (
    CHECKPOINT_FORMAT,
    CheckpointError,
    DictCharacterTable,
    InvalidTableError,
    SegmentLimitError,
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
    build_demo_table_plus,
    describe,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference import reference_normalize  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER = REPO_ROOT / "checkpoint_worker.py"


def _spans(result) -> tuple[tuple[int, int, int], ...]:
    return tuple((s.chunk_index, s.start, s.end) for s in result.source_spans)


def _json_roundtrip(obj: object) -> object:
    """真实 JSON 序列化往返（模拟落盘后在另一进程读回）。"""
    return json.loads(json.dumps(obj))


def _resume(data: dict, table: DictCharacterTable) -> StreamNormalizer:
    return StreamNormalizer.restore_checkpoint(data, table)


class _BoundaryAssertions:
    """各手算场景共用：每个 chunk 边界保存→JSON→新表恢复→核对连续结果。"""

    def assertEveryBoundaryResumes(
        self,
        chunks: list[str],
        make_table,
        *,
        segment_limit: int = 1024,
        expected_cps: tuple[int, ...] | None = None,
        expected_spans: tuple[tuple[int, int, int], ...] | None = None,
    ) -> None:
        table = make_table()
        continuous = normalize(chunks, table, segment_limit=segment_limit)
        if expected_cps is not None:
            self.assertEqual(  # type: ignore[attr-defined]
                continuous.codepoints,
                expected_cps,
                f"手算预期不符: {describe(continuous.codepoints)}",
            )
        if expected_spans is not None:
            self.assertEqual(  # type: ignore[attr-defined]
                _spans(continuous), expected_spans
            )

        for cut in range(len(chunks) + 1):
            with self.subTest(cut=cut):  # type: ignore[attr-defined]
                # 导出方：喂前 cut 个 chunk。
                exporter = StreamNormalizer(
                    make_table(), segment_limit=segment_limit
                )
                for chunk in chunks[:cut]:
                    exporter.feed(chunk)
                data = exporter.export_checkpoint()

                # 导出不改变活对象：原 exporter 继续跑也必须一致。
                for chunk in chunks[cut:]:
                    exporter.feed(chunk)
                without_resume = exporter.finish()
                self.assertEqual(  # type: ignore[attr-defined]
                    without_resume.codepoints, continuous.codepoints
                )

                # 恢复方：JSON 往返 + 全新同内容表（模拟另一进程重建）。
                restored = _resume(
                    _json_roundtrip(data), make_table()
                )
                self.assertEqual(  # type: ignore[attr-defined]
                    restored._chunk_count, cut
                )
                for chunk in chunks[cut:]:
                    restored.feed(chunk)
                result = restored.finish()
                self.assertEqual(  # type: ignore[attr-defined]
                    result.codepoints, continuous.codepoints
                )
                self.assertEqual(  # type: ignore[attr-defined]
                    _spans(result), _spans(continuous)
                )
                self.assertEqual(result.text, continuous.text)
                # chunk 编号连续：来源覆盖的最大 chunk 下标不重号。
                if result.source_spans:
                    self.assertEqual(  # type: ignore[attr-defined]
                        max(s.chunk_index for s in result.source_spans),
                        max(s.chunk_index for s in continuous.source_spans),
                    )
                # finish 幂等。
                self.assertIs(restored.finish(), result)


class HandComputedBoundaryTests(unittest.TestCase, _BoundaryAssertions):
    def test_leading_combining_marks(self) -> None:
        # 前导组合符：grave acute 是无 starter 的前导段，只排序不组合；
        # 随后 a + grave 正常组合。逐码点切 chunk，每个边界都保存/恢复。
        def table():
            return DictCharacterTable(
                decompositions={P_A_GRAVE: (A, M_GRAVE)},
                combining_classes={M_GRAVE: 230, M_ACUTE: 230},
                compositions={(A, M_GRAVE): P_A_GRAVE},
            )

        chunks = [chr(M_GRAVE), chr(M_ACUTE), chr(A), chr(M_GRAVE)]
        self.assertEveryBoundaryResumes(
            chunks,
            table,
            expected_cps=(M_GRAVE, M_ACUTE, P_A_GRAVE),
            expected_spans=((0, 0, 1), (1, 0, 1), (2, 0, 1), (3, 0, 1)),
        )

    def test_leading_marks_with_empty_chunk_keeps_indices(self) -> None:
        # 空 chunk 也占编号：恢复后编号必须连续，来源区间与连续处理一致。
        def table():
            return DictCharacterTable(
                combining_classes={M_GRAVE: 230, M_ACUTE: 230},
            )

        chunks = ["", chr(M_GRAVE), "", chr(M_ACUTE) + chr(A)]
        self.assertEveryBoundaryResumes(
            chunks,
            table,
            expected_cps=(M_GRAVE, M_ACUTE, A),
        )

    def test_equal_ccc_blocking(self) -> None:
        # dotbelow/acute 同为 230，稳定排序后 dotbelow 在前且无组合映射，
        # 等 ccc 阻塞 acute：即使 (a, acute) 有映射也不得组合。
        blocked_target = 0xE199

        def table():
            return DictCharacterTable(
                combining_classes={M_DOTBELOW: 230, M_ACUTE: 230},
                compositions={
                    (A, M_ACUTE): P_E_ACUTE,
                    (P_E_ACUTE, M_DOTBELOW): blocked_target,
                },
            )

        chunks = [chr(A), chr(M_DOTBELOW), chr(M_ACUTE)]
        self.assertEveryBoundaryResumes(
            chunks,
            table,
            expected_cps=(A, M_DOTBELOW, M_ACUTE),
            # 三个码点分属三个 chunk，组合没有发生，来源不跨块合并。
            expected_spans=((0, 0, 1), (1, 0, 1), (2, 0, 1)),
        )

    def test_recursive_decomposition_and_chain_composition(self) -> None:
        # P_A_GRAVE_RING 递归展开 a+grave+ring 挂起；跨边界保存的是展开
        # 后的叶子。第二段 a+grave+ring 经链式组合再次成为 P_A_GRAVE_RING。
        chunks = [
            chr(P_A_GRAVE_RING),
            chr(A),
            chr(M_GRAVE),
            chr(M_RING),
        ]
        self.assertEveryBoundaryResumes(
            chunks,
            build_demo_table,
            expected_cps=(P_A_GRAVE_RING, P_A_GRAVE_RING),
            # 第一个输出来自 chunk0 单个预组合；第二个链式组合 a/grave/ring
            # 分属 chunk 1/2/3，来源区间各自独立。
            expected_spans=(
                (0, 0, 1),
                (1, 0, 1),
                (2, 0, 1),
                (3, 0, 1),
            ),
        )

    def test_pending_reorder_after_resume(self) -> None:
        # 保存时段内只有 a+grave；恢复后下一块的 dotbelow(220) 必须排到
        # grave(230) 之前——下一块字符能改变排序，保存不能提前闭合段。
        chunks = [chr(A), chr(M_GRAVE), chr(M_DOTBELOW)]
        self.assertEveryBoundaryResumes(
            chunks,
            build_demo_table,
            expected_cps=(P_A_GRAVE, M_DOTBELOW),
            # a、grave 分属 chunk 0、1，组合后两段来源；dotbelow 在 chunk 2。
            expected_spans=((0, 0, 1), (1, 0, 1), (2, 0, 1)),
        )

    def test_saving_does_not_finish_pending_segment(self) -> None:
        # 直接检查检查点内容：挂起段在 pending_segment 里、finished=False、
        # output 没有提前冲刷的段。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE))
        data = sn.export_checkpoint()
        self.assertFalse(data["finished"])
        self.assertEqual(
            [entry[0] for entry in data["pending_segment"]], [A, M_GRAVE]
        )
        self.assertEqual(data["output"], [])
        # 活对象仍未 finish：还能 feed，且下一个 starter 才冲刷。
        self.assertFalse(sn._finished)
        sn.feed(chr(E))
        self.assertEqual(
            [entry[0] for entry in sn._out], [P_A_GRAVE]
        )

    def test_segment_limit_failure_then_resume_and_continue(self) -> None:
        # 超长段失败是原子的；失败前后导出的检查点一致，恢复后可继续，
        # chunk 编号接着计，且恢复出的流同样拒绝超限 chunk。
        limit = 4
        big = 0xE400
        marks = (0xE500, 0xE501, 0xE502, 0xE503)

        def table():
            return DictCharacterTable(
                decompositions={big: (A, *marks)},
                combining_classes={m: 230 for m in marks},
            )

        sn = StreamNormalizer(table(), segment_limit=limit)
        sn.feed("e")  # chunk 0
        sn.feed("a")  # chunk 1
        before = json.dumps(sn.export_checkpoint(), sort_keys=True)
        with self.assertRaises(SegmentLimitError) as ctx:
            sn.feed(chr(big))  # 展开 5 叶子，第 5 个超限
        self.assertEqual(ctx.exception.chunk_index, 2)
        # 原子回滚：失败后检查点与失败前完全相同。
        after = json.dumps(sn.export_checkpoint(), sort_keys=True)
        self.assertEqual(before, after)

        for label, payload in (("before-failure", before), ("after-failure", after)):
            with self.subTest(label):
                restored = _resume(json.loads(payload), table())
                self.assertEqual(restored._chunk_count, 2)
                # 恢复后超限仍被拒（segment_limit 随之恢复）。
                with self.assertRaises(SegmentLimitError):
                    restored.feed(chr(big))
                # 回滚后仍可继续正常 chunk，编号为 chunk 2。
                restored.feed("o")
                result = restored.finish()
                self.assertEqual(result.codepoints, (ord("e"), A, O))
                self.assertEqual(
                    _spans(result),
                    ((0, 0, 1), (1, 0, 1), (2, 0, 1)),
                )

        # 与"从头处理不含坏 chunk 的序列"一致。
        continuous = normalize(["e", "a", "o"], table(), segment_limit=limit)
        self.assertEqual(continuous.codepoints, (ord("e"), A, O))

    def test_segment_limit_is_restored_from_checkpoint(self) -> None:
        # 小上限随检查点恢复并继续生效：demo 表中 P_A_GRAVE_RING 展开为
        # 3 个叶子，limit=2 时恢复后喂它必须同样超限。
        sn = StreamNormalizer(build_demo_table(), segment_limit=2)
        sn.feed(chr(A))
        data = _json_roundtrip(sn.export_checkpoint())
        self.assertEqual(data["segment_limit"], 2)
        restored = _resume(data, build_demo_table())
        self.assertEqual(restored._limit, 2)
        with self.assertRaises(SegmentLimitError):
            restored.feed(chr(P_A_GRAVE_RING))


class FinishedStateTests(unittest.TestCase):
    def test_finished_checkpoint_keeps_terminal_semantics(self) -> None:
        table = build_demo_table()
        sn = StreamNormalizer(table)
        sn.feed(chr(A) + chr(M_GRAVE))
        first = sn.finish()
        data = _json_roundtrip(sn.export_checkpoint())
        self.assertTrue(data["finished"])
        self.assertEqual(data["pending_segment"], [])

        restored = _resume(data, build_demo_table())
        again = restored.finish()
        self.assertEqual(again.codepoints, first.codepoints)
        self.assertEqual(_spans(again), _spans(first))
        self.assertEqual(again.text, first.text)
        # 恢复后 finish 同样幂等、feed 同样被拒。
        self.assertIs(restored.finish(), again)
        with self.assertRaises(Exception):
            restored.feed("x")

    def test_empty_finished_stream_roundtrip(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        result = sn.finish()
        data = _json_roundtrip(sn.export_checkpoint())
        restored = _resume(data, build_demo_table())
        again = restored.finish()
        self.assertEqual(again.codepoints, ())
        self.assertEqual(again.text, "")
        self.assertEqual(again.source_spans, ())
        self.assertEqual(again.codepoints, result.codepoints)


class ExportIsolationTests(unittest.TestCase):
    def test_mutating_exported_dict_does_not_touch_live_object(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE))
        data = sn.export_checkpoint()
        snapshot = copy.deepcopy(data)

        # 调用方任意篡改导出的嵌套结构。
        data["output"].append(["bogus", []])
        data["pending_segment"][0][0] = 0x10FFFF
        data["pending_segment"][0][1].append([9, 9, 9])
        data["table_fingerprint"] = "tampered"
        data["stream_pos"] = 999
        data["finished"] = True

        # 活对象不受影响：再导出一份与快照相同，且继续处理结果正确。
        again = sn.export_checkpoint()
        self.assertEqual(
            json.dumps(again, sort_keys=True),
            json.dumps(snapshot, sort_keys=True),
        )
        sn.feed(chr(E))
        result = sn.finish()
        self.assertEqual(result.codepoints, (P_A_GRAVE, E))

    def test_checkpoint_is_json_serialisable_without_custom_encoders(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(P_A_GRAVE_RING))
        data = sn.export_checkpoint()
        # 默认编码器即可序列化；键都是字符串、值都是 JSON 基本类型。
        encoded = json.dumps(data)
        decoded = json.loads(encoded)
        restored = _resume(decoded, build_demo_table())
        restored.feed(chr(A))
        self.assertEqual(
            restored.finish().codepoints, (P_A_GRAVE_RING, A)
        )

    def test_export_is_repeatable_and_does_not_advance_stream(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(A))
        first = json.dumps(sn.export_checkpoint(), sort_keys=True)
        second = json.dumps(sn.export_checkpoint(), sort_keys=True)
        self.assertEqual(first, second)
        self.assertEqual(sn._chunk_count, 1)
        self.assertEqual(sn._stream_pos, 1)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_order_independent_but_content_sensitive(self) -> None:
        t1 = DictCharacterTable(
            decompositions={P_A_GRAVE: (A, M_GRAVE)},
            combining_classes={M_GRAVE: 230},
            compositions={(A, M_GRAVE): P_A_GRAVE},
        )
        t2 = DictCharacterTable(
            compositions={(A, M_GRAVE): P_A_GRAVE},
            combining_classes={M_GRAVE: 230},
            decompositions={P_A_GRAVE: (A, M_GRAVE)},
        )
        self.assertEqual(t1.fingerprint(), t2.fingerprint())
        # 多一条规则 → 指纹不同。
        self.assertNotEqual(t1.fingerprint(), build_demo_table().fingerprint())
        self.assertNotEqual(
            build_demo_table().fingerprint(), build_demo_table_plus().fingerprint()
        )

    def test_rules_roundtrip_rebuild_same_fingerprint(self) -> None:
        # canonical_rules 输出可在另一进程重建同内容表。
        original = build_demo_table()
        rules = original.canonical_rules()
        rebuilt = DictCharacterTable(
            decompositions={cp: tuple(children) for cp, children in rules["decompositions"]},
            combining_classes={cp: ccc for cp, ccc in rules["combining_classes"]},
            compositions={
                (left, right): result
                for left, right, result in rules["compositions"]
            },
        )
        self.assertEqual(rebuilt.fingerprint(), original.fingerprint())

    def test_invalid_rules_rejected_when_fingerprinting(self) -> None:
        with self.assertRaises(InvalidTableError):
            DictCharacterTable(decompositions={1: ()}).fingerprint()
        with self.assertRaises(InvalidTableError):
            DictCharacterTable(combining_classes={1: -1}).fingerprint()
        with self.assertRaises(InvalidTableError):
            DictCharacterTable(combining_classes={1: True}).fingerprint()
        with self.assertRaises(InvalidTableError):
            DictCharacterTable(
                compositions={(1, 2): 0x110000}
            ).fingerprint()


class InvalidCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(A))
        sn.feed(chr(M_GRAVE))
        self.data = _json_roundtrip(sn.export_checkpoint())

    def _assert_rejected(self, data: object, table=None) -> None:
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(
                data, table if table is not None else build_demo_table()
            )

    def test_non_dict_payloads(self) -> None:
        for bad in (None, [], "x", 42, [{"format": CHECKPOINT_FORMAT}]):
            with self.subTest(bad=bad):
                self._assert_rejected(bad)

    def test_format_and_version(self) -> None:
        bad = copy.deepcopy(self.data)
        bad["format"] = "something.else"
        self._assert_rejected(bad)
        del bad["format"]
        self._assert_rejected(copy.deepcopy(self.data) | {"format": "?"})
        for version in (2, 0, -1, "1", True, None):
            broken = copy.deepcopy(self.data)
            broken["version"] = version
            self._assert_rejected(broken)
        missing = copy.deepcopy(self.data)
        del missing["version"]
        self._assert_rejected(missing)

    def test_missing_fields(self) -> None:
        for key in (
            "segment_limit",
            "table_fingerprint",
            "stream_pos",
            "chunk_count",
            "finished",
            "pending_segment",
            "output",
        ):
            broken = copy.deepcopy(self.data)
            del broken[key]
            self._assert_rejected(broken)

    def test_bad_scalar_field_types(self) -> None:
        cases = {
            "segment_limit": (0, -1, True, "4", 1.5, None),
            "stream_pos": (-1, True, "2", 1.5),
            "chunk_count": (-1, True, "2", 1.5),
            "finished": ("true", 0, 1, None),
            "table_fingerprint": (123, None, []),
        }
        for key, values in cases.items():
            for value in values:
                broken = copy.deepcopy(self.data)
                broken[key] = value
                with self.subTest(key=key, value=value):
                    self._assert_rejected(broken)

    def test_fingerprint_mismatch_rejected(self) -> None:
        self._assert_rejected(self.data, build_demo_table_plus())
        broken = copy.deepcopy(self.data)
        broken["table_fingerprint"] = "0" * 64
        self._assert_rejected(broken)

    def test_restore_requires_exact_dict_table(self) -> None:
        class ChildTable(DictCharacterTable):
            pass

        self._assert_rejected(self.data, ChildTable())

        class DuckTable:
            def __init__(self):
                self._t = build_demo_table()

            def decomposition(self, cp):
                return self._t.decomposition(cp)

            def combining_class(self, cp):
                return self._t.combining_class(cp)

            def composition(self, left, right):
                return self._t.composition(left, right)

        self._assert_rejected(self.data, DuckTable())

    def test_bad_entry_shapes(self) -> None:
        for bad_segment in (
            "x",
            [[]],
            [[0x110000, [[0, 0, 0]]]],
            [[-1, [[0, 0, 0]]]],
            [[True, [[0, 0, 0]]]],
            [[A, []]],
            [[A, [(0, 0, 0)]]],
            [[A, [[0, 0]]]],
            [[A, [[0, 0, -1]]]],
            [[A, [[0, -1, 0]]]],
            [[A, [[-1, 0, 0]]]],
            [[A, [[True, 0, 0]]]],
            [[A, [[0, "0", 0]]]],
            [[A, [[5, 0, 0]]]],   # chunk_index >= chunk_count(2)
            [[A, [[0, 0, 9]]]],   # stream_offset >= stream_pos(2)
        ):
            broken = copy.deepcopy(self.data)
            broken["pending_segment"] = bad_segment
            with self.subTest(bad=bad_segment):
                self._assert_rejected(broken)

    def test_output_entry_validated_too(self) -> None:
        # 真实冲刷输出：["a","e"] 未 finish，output=[a], seg=[e]。
        sn = StreamNormalizer(build_demo_table())
        sn.feed("a")
        sn.feed("e")
        good = _json_roundtrip(sn.export_checkpoint())
        broken = copy.deepcopy(good)
        broken["output"][0][1][0][0] = 9  # chunk_index 越界
        self._assert_rejected(broken)

    def test_source_coverage_must_be_exact(self) -> None:
        # 删掉挂起段 → 来源点缺了 stream_offset=1。
        broken = copy.deepcopy(self.data)
        broken["pending_segment"] = []
        self._assert_rejected(broken)

    def test_source_offsets_must_be_contiguous(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed("ab")
        sn.feed("e")
        data = _json_roundtrip(sn.export_checkpoint())  # out=[a,b], seg=[e]
        broken = copy.deepcopy(data)
        broken["pending_segment"][0][1] = [[1, 2, 2]]  # chunk1 offset 跳号
        self._assert_rejected(broken)

    def test_same_stream_offset_must_map_to_same_coordinates(self) -> None:
        # P_A_GRAVE 未闭合：段两叶子共享来源 (0,0,0)。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(P_A_GRAVE))
        data = _json_roundtrip(sn.export_checkpoint())
        broken = copy.deepcopy(data)
        broken["pending_segment"][1][1] = [[0, 1, 0]]  # 同 stream_offset 异坐标
        self._assert_rejected(broken)

    def test_finished_with_nonempty_segment_rejected(self) -> None:
        broken = copy.deepcopy(self.data)
        broken["finished"] = True
        self._assert_rejected(broken)

    def test_pending_segment_shape_validated(self) -> None:
        # 段首之后出现 starter。
        broken = copy.deepcopy(self.data)
        broken["pending_segment"][1][0] = E
        self._assert_rejected(broken)

        # ccc 降序：ring(232) 排到 grave(230) 之前。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(P_A_GRAVE_RING))
        data = _json_roundtrip(sn.export_checkpoint())
        reordered = copy.deepcopy(data)
        entries = reordered["pending_segment"]
        entries[1], entries[2] = entries[2], entries[1]
        self._assert_rejected(reordered)

        # 段长度超过声明的 segment_limit。
        too_small = copy.deepcopy(data)
        too_small["segment_limit"] = 1
        self._assert_rejected(too_small)

    def test_leading_segment_must_be_non_decreasing_too(self) -> None:
        # 前导组合符段 [grave(230), dotbelow(220)] 降序，必须拒绝。
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(M_GRAVE))
        sn.feed(chr(M_DOTBELOW))
        data = _json_roundtrip(sn.export_checkpoint())
        self.assertEqual(
            [entry[0] for entry in data["pending_segment"]],
            [M_DOTBELOW, M_GRAVE],  # 引擎正确地稳定排成 220,230
        )
        broken = copy.deepcopy(data)
        entries = broken["pending_segment"]
        entries[0], entries[1] = entries[1], entries[0]  # 改成 230,220
        self._assert_rejected(broken)

    def test_failed_restore_leaves_no_half_restored_object(self) -> None:
        # 恢复失败只抛异常；旁边一个正在使用的活流不受任何影响。
        live = StreamNormalizer(build_demo_table())
        live.feed(chr(A))
        broken = copy.deepcopy(self.data)
        broken["version"] = 999
        self._assert_rejected(broken)
        live.feed(chr(M_GRAVE))
        result = live.finish()
        self.assertEqual(result.codepoints, (P_A_GRAVE,))
        self.assertEqual(_spans(result), ((0, 0, 1), (1, 0, 1)))


class CustomTableUnsupportedTests(unittest.TestCase):
    """自定义表正常处理完全不受影响，只是不能导出/恢复。"""

    def test_duck_table_normalizes_but_cannot_checkpoint(self) -> None:
        inner = build_demo_table()

        class DuckTable:
            def decomposition(self, cp):
                return inner.decomposition(cp)

            def combining_class(self, cp):
                return inner.combining_class(cp)

            def composition(self, left, right):
                return inner.composition(left, right)

        table = DuckTable()
        result = normalize([chr(A), chr(M_GRAVE)], table)
        self.assertEqual(result.codepoints, (P_A_GRAVE,))

        sn = StreamNormalizer(table)
        sn.feed(chr(A))
        with self.assertRaises(CheckpointError):
            sn.export_checkpoint()
        # 未导出成功不影响继续处理。
        sn.feed(chr(M_GRAVE))
        self.assertEqual(sn.finish().codepoints, (P_A_GRAVE,))

    def test_dict_subclass_cannot_checkpoint(self) -> None:
        class ChildTable(DictCharacterTable):
            pass

        table = ChildTable()
        result = normalize(["ao"], table)
        self.assertEqual(result.text, "ao")
        sn = StreamNormalizer(table)
        sn.feed("a")
        with self.assertRaises(CheckpointError):
            sn.export_checkpoint()


class FuzzResumeTests(unittest.TestCase):
    def test_random_chunkings_with_resume_match_reference(self) -> None:
        table_factory = build_demo_table
        alphabet = [
            A, E, O, M_GRAVE, M_ACUTE, M_DOTBELOW, M_RING,
            P_A_GRAVE, P_E_ACUTE, P_A_GRAVE_RING,
        ]
        rng = random.Random(20260925)
        for trial in range(80):
            n = rng.randint(0, 30)
            seq = [chr(rng.choice(alphabet)) for _ in range(n)]
            whole = "".join(seq)
            max_cuts = min(n - 1, 4)
            k = rng.randint(0, max_cuts) if n > 1 else 0
            cuts = sorted(rng.sample(range(1, n), k=k)) if k else []
            chunks: list[str] = []
            prev = 0
            for c in cuts + [n]:
                chunks.append(whole[prev:c])
                prev = c

            ref_cps, ref_spans = reference_normalize(chunks, table_factory())

            # 随机选一个暂停边界；每 5 轮再额外穷举一次所有边界。
            cuts_to_try = {rng.randint(0, len(chunks))}
            if trial % 5 == 0:
                cuts_to_try.update(range(len(chunks) + 1))
            for cut in cuts_to_try:
                exporter = StreamNormalizer(table_factory())
                for chunk in chunks[:cut]:
                    exporter.feed(chunk)
                data = _json_roundtrip(exporter.export_checkpoint())
                restored = _resume(data, table_factory())
                for chunk in chunks[cut:]:
                    restored.feed(chunk)
                result = restored.finish()
                self.assertEqual(
                    result.codepoints, ref_cps, f"trial={trial} cut={cut}"
                )
                self.assertEqual(
                    _spans(result), ref_spans, f"spans trial={trial} cut={cut}"
                )


def _run_worker(mode: str, spec: dict) -> tuple[int, dict, str]:
    """启动真实子进程，stdin/stdout 只走 JSON。"""
    proc = subprocess.run(
        [sys.executable, str(WORKER), mode],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    stdout = proc.stdout
    payload = json.loads(stdout) if stdout.strip() else {}
    return proc.returncode, payload, proc.stderr


class SubprocessResumeTests(unittest.TestCase):
    def test_save_and_resume_in_two_real_processes(self) -> None:
        table = build_demo_table()
        chunks = [
            chr(P_A_GRAVE_RING),
            chr(A) + chr(M_GRAVE),
            chr(M_DOTBELOW),
            chr(E) + chr(M_ACUTE),
        ]
        cut = 2
        continuous = normalize(chunks, build_demo_table())

        # 进程 1：仅喂前一半，导出检查点 JSON。
        save_spec = {
            # 关键：表规则也以 JSON 传入，另一进程据此重建同内容表。
            "table": {"rules": table.canonical_rules()},
            "chunks": chunks[:cut],
        }
        rc, save_payload, err = _run_worker("save", save_spec)
        self.assertEqual(rc, 0, err)
        self.assertIn("checkpoint", save_payload)
        state = save_payload["state"]
        self.assertEqual(state["chunk_count"], cut)
        self.assertFalse(state["finished"])
        # 暂停时第一段已闭合冲刷、第二段 a+grave 挂起。
        self.assertEqual(state["pending_segment"], [A, M_GRAVE])

        # 确保检查点真的是可落盘的 JSON 文本（此处经字符串再解析一次）。
        checkpoint = json.loads(json.dumps(save_payload["checkpoint"]))

        # 进程 2：全新进程，仅凭检查点 JSON + JSON 规则表恢复并跑完。
        resume_spec = {
            "table": {"rules": table.canonical_rules()},
            "checkpoint": checkpoint,
            "chunks": chunks[cut:],
        }
        rc, resume_payload, err = _run_worker("resume", resume_spec)
        self.assertEqual(rc, 0, err)
        result = resume_payload["result"]
        self.assertEqual(tuple(result["codepoints"]), continuous.codepoints)
        self.assertEqual(
            tuple(tuple(s) for s in result["spans"]),
            _spans(continuous),
        )
        self.assertEqual(result["text"], continuous.text)

    def test_real_process_table_mismatch_is_rejected(self) -> None:
        # 进程 A 用 demo 表暂停。
        rc, save_payload, err = _run_worker(
            "save",
            {"table": "demo", "chunks": [chr(A), chr(M_GRAVE)]},
        )
        self.assertEqual(rc, 0, err)
        # 进程 B 用多一条规则的表恢复 → 指纹不符，非零退出 + error JSON。
        rc, payload, err = _run_worker(
            "resume",
            {
                "table": "demo",
                "tamper": True,
                "checkpoint": save_payload["checkpoint"],
                "chunks": [],
            },
        )
        self.assertEqual(rc, 1, err)
        self.assertIn("fingerprint", payload["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
