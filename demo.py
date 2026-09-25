#!/usr/bin/env python3
"""固定输入演示：显式字符表的流式规范组合。

运行::

    python demo.py

内容：
1. 用仓库内确定性字符表对一段固定输入做规范化，打印规范串码点序列；
2. 把同一输入按固定切点分成 3 个 chunk 流式 feed，验证分块结果与一次性
   结果一致；
3. 对规范串再规范化一次，验证幂等；
4. 打印输出来源区间集合；
5. 边界演示（真实触发、非预录）：循环分解表触发 DecompositionCycleError；
   单段 1025 码点触发 SegmentLimitError；
6. 检查点暂停/恢复：处理到一半导出带版本的 JSON 检查点（未闭合段原样
   保留、不 finish），从 JSON 恢复后接着处理剩余 chunk，核对与连续处理
   一致；
7. 两个**真实子进程**只经 stdin/stdout 传 JSON：进程 A 暂停并导出，
   进程 B 仅凭检查点 JSON 与 JSON 规则重建的同内容字符表恢复并跑完；
8. 表不匹配：用多一条规则的字符表恢复，指纹不一致被拒绝（真实子进程）。

所有结果都是现场计算，不使用 sleep 或预录结果。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from canoncompose import (
    DecompositionCycleError,
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
    O,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    build_cyclic_table,
    build_demo_table,
    build_demo_table_plus,
    describe,
)
from canoncompose.table import DictCharacterTable

REPO_ROOT = Path(__file__).resolve().parent
WORKER = REPO_ROOT / "checkpoint_worker.py"


def _format_spans(spans) -> str:
    return ", ".join(
        f"chunk {s.chunk_index}[{s.start}:{s.end}]" for s in spans
    ) or "(空)"


# 检查点演示用的固定切分：切点故意落在未闭合段内部。
_RESUME_CHUNKS = [
    chr(P_A_GRAVE_RING),
    chr(A) + chr(M_GRAVE),
    chr(M_DOTBELOW),
    chr(E) + chr(M_ACUTE),
]
_RESUME_CUT = 2


def _checkpoint_in_process() -> tuple[str, str, int]:
    """进程内保存→JSON 文本→恢复，返回 (暂停时挂起段描述, JSON 摘要)。"""
    stream = StreamNormalizer(build_demo_table())
    for chunk in _RESUME_CHUNKS[:_RESUME_CUT]:
        stream.feed(chunk)

    # 导出检查点并真的序列化成 JSON 文本（再解析回来），证明无 pickle。
    data = stream.export_checkpoint()
    blob = json.dumps(data, ensure_ascii=False, indent=2)
    data = json.loads(blob)

    pending = describe(tuple(entry[0] for entry in data["pending_segment"]))
    summary = (
        f"format={data['format']} v{data['version']}, "
        f"chunks={data['chunk_count']}, stream_pos={data['stream_pos']}, "
        f"finished={data['finished']}, 挂起段=[{pending}]"
    )

    restored = StreamNormalizer.restore_checkpoint(data, build_demo_table())
    for chunk in _RESUME_CHUNKS[_RESUME_CUT:]:
        restored.feed(chunk)
    resumed_result = restored.finish()

    continuous = normalize(_RESUME_CHUNKS, build_demo_table())
    assert resumed_result.codepoints == continuous.codepoints
    assert resumed_result.source_spans == continuous.source_spans
    print("剩余 chunk 接着处理的规范码点:", describe(resumed_result.codepoints))
    print("来源区间:", _format_spans(resumed_result.source_spans))
    print("=> 与从头连续处理完全一致（码点、来源区间、chunk 编号） ✓")
    return pending, summary, len(blob)


def _run_worker(mode: str, spec: dict) -> tuple[int, dict]:
    """启动真实子进程：stdin 收 JSON，stdout 回 JSON。"""
    proc = subprocess.run(
        [sys.executable, str(WORKER), mode],
        input=json.dumps(spec, ensure_ascii=False),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, payload


def _checkpoint_subprocesses() -> None:
    """两个真实子进程，全部通过 JSON 通信：保存进程 → 恢复进程。"""
    table = build_demo_table()
    rules = table.canonical_rules()  # JSON 兼容规则：另一进程据此重建表

    # 进程 A（全新进程）：喂前一半，导出检查点 JSON。
    rc, saved = _run_worker(
        "save",
        {"table": {"rules": rules}, "chunks": _RESUME_CHUNKS[:_RESUME_CUT]},
    )
    assert rc == 0, saved
    print(
        "进程 A 状态: chunk_count=",
        saved["state"]["chunk_count"],
        ", 挂起段=[",
        describe(tuple(saved["state"]["pending_segment"])),
        "], finished=",
        saved["state"]["finished"],
        sep="",
    )

    # 进程 B（另一个全新进程）：仅凭检查点 JSON + JSON 规则恢复并跑完。
    rc, resumed = _run_worker(
        "resume",
        {
            "table": {"rules": rules},
            "checkpoint": saved["checkpoint"],
            "chunks": _RESUME_CHUNKS[_RESUME_CUT:],
        },
    )
    assert rc == 0, resumed
    result = resumed["result"]
    continuous = normalize(_RESUME_CHUNKS, build_demo_table())
    print(
        "进程 B 结果码点:",
        describe(tuple(result["codepoints"])),
    )
    assert tuple(result["codepoints"]) == continuous.codepoints
    assert result["text"] == continuous.text
    print("=> 跨进程 JSON 保存/恢复与连续处理一致 ✓")


def _checkpoint_table_mismatch() -> None:
    """真实子进程中用内容不同的表恢复：指纹不匹配，拒绝且退出码非零。"""
    rc, saved = _run_worker(
        "save",
        {"table": "demo", "chunks": [chr(A), chr(M_GRAVE)]},
    )
    assert rc == 0, saved
    original_fp = saved["checkpoint"]["table_fingerprint"]

    rc, payload = _run_worker(
        "resume",
        {
            "table": "demo",
            "tamper": True,  # 多一条组合规则 → 指纹不同
            "checkpoint": saved["checkpoint"],
            "chunks": [],
        },
    )
    wrong_fp = build_demo_table_plus().fingerprint()
    print(f"原表指纹:   {original_fp[:16]}…")
    print(f"不匹配指纹: {wrong_fp[:16]}…")
    print(f"进程退出码: {rc}（非零表示被拒绝）")
    print(f"错误: {payload['error']}")
    assert rc == 1 and "fingerprint" in payload["error"]
    print("=> 表内容不同，恢复被检查点校验拒绝 ✓")


def main() -> None:
    started = time.perf_counter()
    table = build_demo_table()

    # 固定输入：两级预组合 + 需要 ccc 重排的记号 + 可组合段 + 普通 starter。
    text = (
        chr(P_A_GRAVE_RING)
        + chr(A) + chr(M_GRAVE) + chr(M_DOTBELOW)
        + chr(E) + chr(M_ACUTE)
        + chr(O)
    )
    print("=" * 68)
    print("1. 一次性规范化（正常结果）")
    print("-" * 68)
    print("输入码点:", describe(tuple(ord(c) for c in text)))
    result = normalize([text], table)
    print("规范码点:", describe(result.codepoints))
    print("来源区间:", _format_spans(result.source_spans))

    print()
    print("=" * 68)
    print("2. 跨 chunk 流式处理（切点在 starter 与其记号之间）")
    print("-" * 68)
    # 7 个输入码点，在 2、5 处切分：a 与其 grave 被切到不同 chunk。
    chunks = [text[:2], text[2:5], text[5:]]
    for i, ch in enumerate(chunks):
        print(f"feed chunk {i}: {describe(tuple(ord(c) for c in ch))}")
    stream = StreamNormalizer(table)
    for ch in chunks:
        stream.feed(ch)
    streamed = stream.finish()
    print("流式结果:", describe(streamed.codepoints))
    assert streamed.codepoints == result.codepoints, "分块结果与一次性结果不一致"
    print("=> 与一次性结果一致 ✓")

    print()
    print("=" * 68)
    print("3. 幂等：对规范串再规范化一次")
    print("-" * 68)
    second = normalize([result.text], table)
    print("二次结果:", describe(second.codepoints))
    assert second.codepoints == result.codepoints, "规范化不幂等"
    print("=> 规范串再规范化保持不变 ✓")

    print()
    print("=" * 68)
    print("4. 拒绝边界 A：循环分解表（真实触发）")
    print("-" * 68)
    try:
        normalize([chr(0xE200)], build_cyclic_table())
    except DecompositionCycleError as exc:
        rendered = " -> ".join(f"U+{cp:04X}" for cp in exc.chain)
        print(f"捕获 {type(exc).__name__}: {exc}")
        print(f"检测到的环: {rendered}")

    print()
    print("=" * 68)
    print("5. 拒绝边界 B：单段超过 1024 码点上限（真实触发）")
    print("-" * 68)
    # 一个 starter + 1024 个非 starter，分解后单段为 1025 个码点。
    big = 0xE400
    marks = tuple(0xE500 + i for i in range(1024))
    limit_table = DictCharacterTable(
        decompositions={big: (A, *marks)},
        combining_classes={m: 230 for m in marks},
    )
    try:
        normalize([chr(big)], limit_table)
    except SegmentLimitError as exc:
        print(f"捕获 {type(exc).__name__}: {exc}")
        print(f"上限={exc.limit}，尝试达到={exc.actual}，定位=chunk "
              f"{exc.chunk_index} 流偏移 {exc.stream_offset}")

    print()
    print("=" * 68)
    print("6. 暂停/恢复：处理到一半导出 JSON 检查点（不偷偷 finish）")
    print("-" * 68)
    print("输入 chunks:")
    for i, chunk in enumerate(_RESUME_CHUNKS):
        marker = "  <- 在此暂停" if i == _RESUME_CUT - 1 else ""
        print(f"  chunk {i}: {describe(tuple(ord(c) for c in chunk))}{marker}")
    pending, summary, blob_len = _checkpoint_in_process()
    print()
    print(f"检查点: {summary}")
    print(f"JSON 文本长度 {blob_len} 字符（纯 JSON，无 pickle、无对象地址）")

    print()
    print("=" * 68)
    print("7. 两个真实子进程：JSON 保存 → 另一进程凭 JSON 规则重建表恢复")
    print("-" * 68)
    _checkpoint_subprocesses()

    print()
    print("=" * 68)
    print("8. 表不匹配：用多一条规则的表恢复（真实子进程，必须拒绝）")
    print("-" * 68)
    _checkpoint_table_mismatch()

    elapsed = time.perf_counter() - started
    print()
    print("=" * 68)
    print(f"全部现场计算完成，用时 {elapsed * 1000:.1f} ms（无 sleep、无预录）。")


if __name__ == "__main__":
    main()
