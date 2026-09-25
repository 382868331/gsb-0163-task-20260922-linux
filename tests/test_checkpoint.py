"""检查点（暂停/恢复）测试。

核心验证方式：对若干**可手算**的字符表与短输入，在每个 chunk 边界分别
导出检查点、（经 JSON 往返）恢复、接着喂剩余 chunks，核对结果码点、
来源区间、chunk 编号与从头连续处理完全一致。另覆盖：finished 状态
保存、导出数据被改不影响活对象、版本/坐标/表不匹配拒绝、自定义表
不支持保存但正常处理不受影响、真实子进程传 JSON。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from canoncompose import (
    CheckpointError,
    DictCharacterTable,
    InvalidInputError,
    NormalizationResult,
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
    P_A_GRAVE,
    P_A_GRAVE_RING,
    P_E_ACUTE,
    build_demo_table,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER = Path(__file__).resolve().parent / "_checkpoint_worker.py"


def _cps(result: NormalizationResult) -> tuple[int, ...]:
    return result.codepoints


def _spans(result: NormalizationResult) -> tuple[tuple[int, int, int], ...]:
    return tuple(
        (s.chunk_index, s.start, s.end) for s in result.source_spans
    )


def _roundtrip(chunks, table_factory, boundary, **kwargs):
    """在 boundary 处暂停→JSON 往返→恢复→续喂，返回结果。"""
    sn = StreamNormalizer(table_factory(), **kwargs)
    for chunk in chunks[:boundary]:
        sn.feed(chunk)
    checkpoint = sn.export_checkpoint()
    # 经 JSON 序列化/反序列化，模拟跨进程传输。
    revived = json.loads(json.dumps(checkpoint))
    restored = StreamNormalizer.restore_checkpoint(revived, table_factory())
    for chunk in chunks[boundary:]:
        restored.feed(chunk)
    return restored.finish()


def _check_every_boundary(test_case, chunks, table_factory, **kwargs):
    expected = normalize(chunks, table_factory(), **kwargs)
    for boundary in range(len(chunks) + 1):
        with test_case.subTest(boundary=boundary):
            result = _roundtrip(chunks, table_factory, boundary, **kwargs)
            test_case.assertEqual(_cps(result), _cps(expected))
            test_case.assertEqual(_spans(result), _spans(expected))


class BoundaryRoundTripTests(unittest.TestCase):
    """短输入在每个 chunk 边界保存/恢复，与连续处理一致。"""

    def test_leading_combining_marks(self) -> None:
        # 前导组合符段（无 starter，不组合）+ 后续 a+grave 组合段。
        chunks = [
            chr(M_GRAVE),
            chr(M_ACUTE) + chr(A),
            chr(M_GRAVE),
        ]
        _check_every_boundary(self, chunks, build_demo_table)
        # 手算核对：前导 grave/acute 原样保留，a+grave 组合为 P_A_GRAVE。
        expected = normalize(chunks, build_demo_table())
        self.assertEqual(_cps(expected), (M_GRAVE, M_ACUTE, P_A_GRAVE))

    def test_same_ccc_blocking(self) -> None:
        # 等 ccc 阻塞：dotbelow(230) 无映射成为未组合项，阻塞 acute(230)。
        def table_factory():
            return DictCharacterTable(
                combining_classes={M_DOTBELOW: 230, M_ACUTE: 230},
                compositions={(A, M_ACUTE): P_E_ACUTE},
            )

        chunks = [chr(A), chr(M_DOTBELOW), chr(M_ACUTE)]
        _check_every_boundary(self, chunks, table_factory)
        expected = normalize(chunks, table_factory())
        self.assertEqual(_cps(expected), (A, M_DOTBELOW, M_ACUTE))

    def test_recursive_decomposition_across_boundary(self) -> None:
        # 递归分解 P_A_GRAVE_RING → P_A_GRAVE → a+grave，跨块再组合。
        chunks = [
            chr(P_A_GRAVE_RING),
            chr(A) + chr(M_GRAVE),
            chr(E),
        ]
        _check_every_boundary(self, chunks, build_demo_table)
        expected = normalize(chunks, build_demo_table())
        self.assertEqual(
            _cps(expected), (P_A_GRAVE_RING, P_A_GRAVE, E)
        )

    def test_ccc_reorder_split_between_marks(self) -> None:
        # grave(230) 与 dotbelow(220) 分到不同 chunk：恢复后仍要稳定重排。
        chunks = [chr(A), chr(M_GRAVE), chr(M_DOTBELOW)]
        _check_every_boundary(self, chunks, build_demo_table)
        expected = normalize(chunks, build_demo_table())
        self.assertEqual(_cps(expected), (P_A_GRAVE, M_DOTBELOW))

    def test_chain_composition_after_restore(self) -> None:
        # a + grave + ring 分三块：恢复后链式组合 a→agrave→a-grave-ring。
        chunks = [chr(A), chr(M_GRAVE), chr(M_RING)]
        _check_every_boundary(self, chunks, build_demo_table)
        expected = normalize(chunks, build_demo_table())
        self.assertEqual(_cps(expected), (P_A_GRAVE_RING,))

    def test_segment_limit_failure_then_continue(self) -> None:
        # 超长段失败（原子回滚）后导出检查点，恢复继续处理。
        big = 0xE400
        marks = tuple(0xE500 + i for i in range(5))

        def table_factory():
            return DictCharacterTable(
                decompositions={big: (A, *marks)},
                combining_classes={m: 230 for m in marks},
            )

        def run_with_checkpoint():
            sn = StreamNormalizer(table_factory(), segment_limit=4)
            sn.feed("e")
            with self.assertRaises(SegmentLimitError):
                sn.feed(chr(A) + chr(big))  # 原子回滚
            checkpoint = sn.export_checkpoint()
            restored = StreamNormalizer.restore_checkpoint(
                json.loads(json.dumps(checkpoint)), table_factory()
            )
            restored.feed("a")
            return restored.finish()

        def run_continuous():
            sn = StreamNormalizer(table_factory(), segment_limit=4)
            sn.feed("e")
            with self.assertRaises(SegmentLimitError):
                sn.feed(chr(A) + chr(big))
            sn.feed("a")
            return sn.finish()

        resumed = run_with_checkpoint()
        continuous = run_continuous()
        self.assertEqual(_cps(resumed), _cps(continuous))
        self.assertEqual(_spans(resumed), _spans(continuous))
        self.assertEqual(resumed.text, "ea")

    def test_empty_stream_and_empty_chunks(self) -> None:
        # 边界 0（尚未喂任何数据）也允许保存/恢复。
        chunks = ["", chr(A) + chr(M_GRAVE), ""]
        _check_every_boundary(self, chunks, build_demo_table)


class CheckpointSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def test_export_does_not_finish_pending_segment(self) -> None:
        # 保存动作不能偷偷 finish：导出后原对象与恢复对象都还能续喂组合。
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))  # 段未闭合
        checkpoint = sn.export_checkpoint()
        # 原对象继续喂 grave：应组合，说明段没被导出动作闭合。
        sn.feed(chr(M_GRAVE))
        self.assertEqual(_cps(sn.finish()), (P_A_GRAVE,))
        # 恢复对象同样。
        restored = StreamNormalizer.restore_checkpoint(
            checkpoint, build_demo_table()
        )
        restored.feed(chr(M_GRAVE))
        self.assertEqual(_cps(restored.finish()), (P_A_GRAVE,))

    def test_finished_state_export_and_restore(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A) + chr(M_GRAVE))
        finished_result = sn.finish()
        checkpoint = sn.export_checkpoint()

        restored = StreamNormalizer.restore_checkpoint(
            checkpoint, build_demo_table()
        )
        # 结束语义保持：finish 幂等且结果相同，feed 被拒绝。
        first = restored.finish()
        self.assertIs(first, restored.finish())
        self.assertEqual(_cps(first), _cps(finished_result))
        self.assertEqual(_spans(first), _spans(finished_result))
        with self.assertRaises(InvalidInputError):
            restored.feed("x")

    def test_exported_data_mutation_does_not_affect_live_object(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(E))
        sn.feed(chr(A))
        checkpoint = sn.export_checkpoint()
        # 调用方大肆修改导出数据……
        checkpoint["stream"]["output"].clear()
        checkpoint["stream"]["pending_segment"][0]["cp"] = 0x41
        checkpoint["stream"]["pending_segment"][0]["origins"][0][0] = 99
        checkpoint["segment_limit"] = 3
        # ……活对象不受影响。
        sn.feed(chr(M_GRAVE))
        result = sn.finish()
        self.assertEqual(_cps(result), (E, P_A_GRAVE))

    def test_checkpoint_is_plain_json_data(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        checkpoint = sn.export_checkpoint()
        # json.dumps 默认参数即可序列化（无自定义类型）。
        text = json.dumps(checkpoint)
        self.assertIsInstance(text, str)
        self.assertEqual(checkpoint["version"], 1)

    def test_chunk_numbering_continues_after_restore(self) -> None:
        # 恢复后 chunk 编号接着走，不与已喂 chunk 冲突。
        sn = StreamNormalizer(self.table)
        sn.feed(chr(E))
        sn.feed(chr(A))
        checkpoint = sn.export_checkpoint()
        restored = StreamNormalizer.restore_checkpoint(
            checkpoint, build_demo_table()
        )
        restored.feed(chr(M_GRAVE))  # 应记为 chunk 2
        result = restored.finish()
        self.assertEqual(
            _spans(result), ((0, 0, 1), (1, 0, 1), (2, 0, 1))
        )


class CheckpointRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = build_demo_table()

    def _valid_checkpoint(self) -> dict:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        return sn.export_checkpoint()

    def test_unknown_version_rejected(self) -> None:
        checkpoint = self._valid_checkpoint()
        checkpoint["version"] = 999
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(checkpoint, self.table)
        del checkpoint["version"]
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(checkpoint, self.table)

    def test_unknown_format_tag_rejected(self) -> None:
        checkpoint = self._valid_checkpoint()
        checkpoint["format"] = "something-else"
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(checkpoint, self.table)

    def test_non_dict_checkpoint_rejected(self) -> None:
        for bad in (None, 42, "checkpoint", [1, 2, 3]):
            with self.subTest(bad=bad), self.assertRaises(CheckpointError):
                StreamNormalizer.restore_checkpoint(bad, self.table)

    def test_invalid_source_coordinates_rejected(self) -> None:
        cases = []
        # chunk_index 越界（只喂了 1 个 chunk）。
        cp1 = self._valid_checkpoint()
        cp1["stream"]["pending_segment"][0]["origins"][0][0] = 1
        cases.append(cp1)
        # 负偏移。
        cp2 = self._valid_checkpoint()
        cp2["stream"]["pending_segment"][0]["origins"][0][1] = -1
        cases.append(cp2)
        # stream_offset 越界（流位置为 1）。
        cp3 = self._valid_checkpoint()
        cp3["stream"]["pending_segment"][0]["origins"][0][2] = 1
        cases.append(cp3)
        # 来源点结构不是三元组。
        cp4 = self._valid_checkpoint()
        cp4["stream"]["pending_segment"][0]["origins"][0] = [0, 0]
        cases.append(cp4)
        # 码点超范围。
        cp5 = self._valid_checkpoint()
        cp5["stream"]["pending_segment"][0]["cp"] = 0x110000
        cases.append(cp5)
        # bool 不是合法坐标。
        cp6 = self._valid_checkpoint()
        cp6["stream"]["pending_segment"][0]["origins"][0][0] = True
        cases.append(cp6)
        for i, checkpoint in enumerate(cases):
            with self.subTest(case=i), self.assertRaises(CheckpointError):
                StreamNormalizer.restore_checkpoint(checkpoint, self.table)

    def test_finished_checkpoint_with_pending_segment_rejected(self) -> None:
        sn = StreamNormalizer(self.table)
        sn.feed(chr(A))
        checkpoint = sn.export_checkpoint()
        checkpoint["stream"]["finished"] = True  # 但段未闭合：矛盾。
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(checkpoint, self.table)

    def test_table_mismatch_rejected(self) -> None:
        checkpoint = self._valid_checkpoint()
        different = DictCharacterTable(
            decompositions={P_A_GRAVE: (A, M_GRAVE)},
            combining_classes={M_GRAVE: 231},  # 与演示表不同
            compositions={(A, M_GRAVE): P_A_GRAVE},
        )
        with self.assertRaises(CheckpointError) as ctx:
            StreamNormalizer.restore_checkpoint(checkpoint, different)
        self.assertIn("mismatch", str(ctx.exception))

    def test_same_content_different_insertion_order_accepted(self) -> None:
        checkpoint = self._valid_checkpoint()
        # 内容相同、插入顺序不同的表：指纹相同，允许恢复。
        reordered = DictCharacterTable(
            compositions={
                (P_A_GRAVE, M_RING): P_A_GRAVE_RING,
                (0x6F, M_ACUTE): 0xE103,
                (E, M_ACUTE): P_E_ACUTE,
                (A, M_GRAVE): P_A_GRAVE,
            },
            combining_classes={
                M_RING: 232,
                M_DOTBELOW: 220,
                M_ACUTE: 230,
                M_GRAVE: 230,
            },
            decompositions={
                P_A_GRAVE_RING: (P_A_GRAVE, M_RING),
                0xE103: (0x6F, M_ACUTE),
                P_E_ACUTE: (E, M_ACUTE),
                P_A_GRAVE: (A, M_GRAVE),
            },
        )
        restored = StreamNormalizer.restore_checkpoint(checkpoint, reordered)
        restored.feed(chr(M_GRAVE))
        self.assertEqual(_cps(restored.finish()), (P_A_GRAVE,))

    def test_failed_restore_then_valid_restore(self) -> None:
        # 失败恢复不留下半恢复对象：随后用合法数据仍可正常恢复。
        checkpoint = self._valid_checkpoint()
        broken = json.loads(json.dumps(checkpoint))
        broken["version"] = 42
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(broken, self.table)
        restored = StreamNormalizer.restore_checkpoint(
            checkpoint, build_demo_table()
        )
        restored.feed(chr(M_GRAVE))
        self.assertEqual(_cps(restored.finish()), (P_A_GRAVE,))


class CustomTableCheckpointTests(unittest.TestCase):
    """自定义字符表：正常处理不受影响，但不支持检查点保存。"""

    class LambdaTable:
        def __init__(self) -> None:
            self._base = build_demo_table()

        def decomposition(self, cp):
            return self._base.decomposition(cp)

        def combining_class(self, cp):
            return self._base.combining_class(cp)

        def composition(self, left, right):
            return self._base.composition(left, right)

    def test_custom_table_normal_processing_unaffected(self) -> None:
        result = normalize([chr(A) + chr(M_GRAVE)], self.LambdaTable())
        self.assertEqual(_cps(result), (P_A_GRAVE,))

    def test_custom_table_export_rejected(self) -> None:
        sn = StreamNormalizer(self.LambdaTable())
        sn.feed(chr(A))
        with self.assertRaises(CheckpointError):
            sn.export_checkpoint()
        # 拒绝后流对象仍正常可用。
        sn.feed(chr(M_GRAVE))
        self.assertEqual(_cps(sn.finish()), (P_A_GRAVE,))

    def test_restore_with_custom_table_rejected(self) -> None:
        sn = StreamNormalizer(build_demo_table())
        sn.feed(chr(A))
        checkpoint = sn.export_checkpoint()
        with self.assertRaises(CheckpointError):
            StreamNormalizer.restore_checkpoint(checkpoint, self.LambdaTable())


class SubprocessCheckpointTests(unittest.TestCase):
    """真实子进程间传 JSON 检查点（不是在内存里复制对象）。"""

    def _run_worker(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(WORKER), *args],
            cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"worker failed: {proc.stderr or proc.stdout}",
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_export_and_resume_in_two_processes(self) -> None:
        sys.path.insert(0, str(WORKER.parent))
        import _checkpoint_worker as worker  # 只为取固定输入，不作状态传递

        chunks = worker.CHUNKS
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = str(Path(tmp) / "checkpoint.json")
            export_info = self._run_worker("export", checkpoint_path)
            self.assertEqual(export_info["fed_chunks"], worker.SPLIT)
            # 检查点文件是真实 JSON 文本。
            text = Path(checkpoint_path).read_text(encoding="utf-8")
            self.assertEqual(json.loads(text)["version"], 1)

            resumed = self._run_worker("resume", checkpoint_path)

        expected = normalize(chunks, build_demo_table())
        self.assertEqual(tuple(resumed["codepoints"]), _cps(expected))
        self.assertEqual(
            tuple(tuple(s) for s in resumed["spans"]), _spans(expected)
        )

    def test_mismatched_table_rejected_in_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint_path = str(Path(tmp) / "checkpoint.json")
            self._run_worker("export", checkpoint_path)
            outcome = self._run_worker("resume_mismatch", checkpoint_path)
        self.assertEqual(outcome["rejected"], "CheckpointError")
        self.assertIn("mismatch", outcome["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
