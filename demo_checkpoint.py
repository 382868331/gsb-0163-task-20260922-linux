#!/usr/bin/env python3
"""检查点演示：暂停 → JSON 检查点 → 另一个进程恢复 → 表不匹配拒绝。

运行::

    python demo_checkpoint.py

流程（全部现场计算，无 sleep、无预录）：

1. 本进程喂入前半 chunks，展示**暂停前**的挂起段状态，导出检查点为
   JSON 文件（带版本、含表指纹）；
2. 启动**另一个真实子进程**（``--resume``）从 JSON 文件恢复，接着喂
   剩余 chunks 并 finish，打印**恢复后**结果；父进程核对与从头连续
   处理完全一致；
3. 再启动一个子进程（``--resume-mismatch``）用**内容不同**的表恢复
   同一检查点，展示指纹不匹配被实际拒绝。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from canoncompose import (
    CheckpointError,
    StreamNormalizer,
    normalize,
)
from canoncompose.samples import (
    A,
    E,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    P_A_GRAVE_RING,
    build_demo_table,
    describe,
)
from canoncompose.table import DictCharacterTable

# 固定输入：两级递归预组合 + 跨块 ccc 重排 + 可组合段，共 3 个 chunk。
CHUNKS = [
    chr(P_A_GRAVE_RING) + chr(A),
    chr(M_GRAVE) + chr(M_DOTBELOW),
    chr(E) + chr(M_ACUTE),
]
SPLIT = 1  # 父进程喂 CHUNKS[:1]，子进程恢复后续喂 CHUNKS[1:]


def _format_spans(spans) -> str:
    return ", ".join(
        f"chunk {s.chunk_index}[{s.start}:{s.end}]" for s in spans
    ) or "(空)"


def _child_resume(checkpoint_path: str, *, mismatch: bool) -> int:
    """子进程入口：从 JSON 检查点恢复并续喂剩余 chunks。"""
    checkpoint = json.loads(Path(checkpoint_path).read_text(encoding="utf-8"))
    if mismatch:
        # 内容不同的表：缺组合规则、ccc 不同。
        table = DictCharacterTable(
            decompositions={P_A_GRAVE_RING: (A,)},
            combining_classes={M_GRAVE: 231},
        )
    else:
        table = build_demo_table()
    try:
        sn = StreamNormalizer.restore_checkpoint(checkpoint, table)
    except CheckpointError as exc:
        print(f"[子进程] 恢复被拒绝：{type(exc).__name__}: {exc}")
        return 0 if mismatch else 1
    if mismatch:
        print("[子进程] 错误：表不匹配却恢复成功")
        return 1
    for chunk in CHUNKS[SPLIT:]:
        sn.feed(chunk)
    result = sn.finish()
    print(f"[子进程] 恢复后续喂 {len(CHUNKS) - SPLIT} 个 chunk 并 finish")
    print("[子进程] 规范码点:", describe(result.codepoints))
    print("[子进程] 来源区间:", _format_spans(result.source_spans))
    print(
        "[子进程] RESULT "
        + json.dumps(
            {
                "codepoints": list(result.codepoints),
                "spans": [
                    [s.chunk_index, s.start, s.end]
                    for s in result.source_spans
                ],
            }
        )
    )
    return 0


def _run_child(checkpoint_path: str, mode: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            mode,
            checkpoint_path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def main() -> None:
    started = time.perf_counter()
    table = build_demo_table()

    print("=" * 68)
    print("1. 暂停前：喂入前半 chunks，段挂起未闭合")
    print("-" * 68)
    for i, chunk in enumerate(CHUNKS):
        print(f"chunk {i}: {describe(tuple(ord(c) for c in chunk))}")
    sn = StreamNormalizer(table)
    for chunk in CHUNKS[:SPLIT]:
        sn.feed(chunk)
    print(f"已喂 {SPLIT} 个 chunk；最后一个规范段仍未闭合（等后续 chunk）。")

    print()
    print("=" * 68)
    print("2. 导出检查点（JSON，带版本与表指纹；不冲刷挂起段）")
    print("-" * 68)
    checkpoint = sn.export_checkpoint()
    pretty = json.dumps(checkpoint, ensure_ascii=False, indent=2)
    print(pretty)
    tmpdir = tempfile.mkdtemp(prefix="canoncompose-checkpoint-")
    checkpoint_path = str(Path(tmpdir) / "checkpoint.json")
    Path(checkpoint_path).write_text(pretty, encoding="utf-8")
    print(f"已写入 {checkpoint_path}（{len(pretty)} 字符）")

    print()
    print("=" * 68)
    print("3. 恢复后：另一个真实子进程从 JSON 恢复并续喂剩余 chunks")
    print("-" * 68)
    proc = _run_child(checkpoint_path, "--resume")
    print(proc.stdout, end="")
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"子进程恢复失败（退出码 {proc.returncode}）")
    child_result = json.loads(
        next(
            line.split("RESULT ", 1)[1]
            for line in proc.stdout.splitlines()
            if line.startswith("[子进程] RESULT ")
        )
    )
    expected = normalize(CHUNKS, build_demo_table())
    assert tuple(child_result["codepoints"]) == expected.codepoints
    assert [tuple(s) for s in child_result["spans"]] == [
        (s.chunk_index, s.start, s.end) for s in expected.source_spans
    ]
    print("=> 与父进程从头连续处理的结果、来源区间、chunk 编号完全一致 ✓")

    print()
    print("=" * 68)
    print("4. 表不匹配：子进程用内容不同的表恢复同一检查点（实际拒绝）")
    print("-" * 68)
    proc = _run_child(checkpoint_path, "--resume-mismatch")
    print(proc.stdout, end="")
    if proc.returncode != 0 or "恢复被拒绝" not in proc.stdout:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("表不匹配未被拒绝，演示失败")
    print("=> 指纹不匹配被实际拒绝 ✓")

    elapsed = time.perf_counter() - started
    print()
    print("=" * 68)
    print(f"全部现场计算完成，用时 {elapsed * 1000:.0f} ms（无 sleep、无预录）。")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--resume", "--resume-mismatch"):
        sys.exit(
            _child_resume(
                sys.argv[2], mismatch=sys.argv[1] == "--resume-mismatch"
            )
        )
    main()
