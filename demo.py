#!/usr/bin/env python3
"""固定输入演示：显式字符表的流式规范组合。

运行::

    python demo.py

内容：
1. 用仓库内确定性字符表对一段固定输入做规范化，打印规范串码点序列；
2. 把同一输入按固定切点分成 3 个 chunk 流式 feed，验证分块结果与一次性
   结果一致；
3. 对规范串再规范化一次，验证幂等；
4. 来源反查：输入选区 → 输出区间（含 pending 与 mixed 标记）；
5. 边界演示（真实触发、非预录）：非法反查区间触发 InvalidInputError；
   循环分解表触发 DecompositionCycleError；单段 1025 码点触发
   SegmentLimitError。

所有结果都是现场计算，不使用 sleep 或预录结果。
"""

from __future__ import annotations

import time

from canoncompose import (
    DecompositionCycleError,
    InvalidInputError,
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
    describe,
)
from canoncompose.table import DictCharacterTable


def _format_spans(spans) -> str:
    return ", ".join(
        f"chunk {s.chunk_index}[{s.start}:{s.end}]" for s in spans
    ) or "(空)"


def _format_output_spans(spans) -> str:
    return ", ".join(
        f"输出[{s.start}:{s.end}]{' mixed' if s.mixed else ''}" for s in spans
    ) or "(空)"


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
    print("4. 来源反查：输入选区 → 输出区间（pending / mixed）")
    print("-" * 68)
    # chunk0 = a，chunk1 = grave + e：a 与 grave 跨 chunk 组合。
    qstream = StreamNormalizer(table)
    qstream.feed(chr(A))
    # 只 feed 了 chunk0：'a' 还在段缓冲中等待后续记号 → 只返回 pending。
    got = qstream.query(0, 0, 1)
    print("feed chunk0 后 query(chunk 0, [0:1]):")
    print("  输出区间:", _format_output_spans(got.output_spans))
    print("  pending :", _format_spans(got.pending_spans))
    # chunk1 的 'e' 是 starter，到达时闭合 a+grave 段并发布组合产物。
    qstream.feed(chr(M_GRAVE) + chr(E))
    got = qstream.query(0, 0, 1)
    print("feed chunk1 后 query(chunk 0, [0:1])（只选 'a'）:")
    print("  输出区间:", _format_output_spans(got.output_spans))
    print("  pending :", _format_spans(got.pending_spans))
    qresult = qstream.finish()
    print("finish 后规范码点:", describe(qresult.codepoints))
    # 只选 chunk1 的 grave：组合产物 P_A_GRAVE 混入选区外的 'a'。
    got = qstream.query(1, 0, 1)
    print("finish 后 query(chunk 1, [0:1])（只选 grave）:")
    print("  输出区间:", _format_output_spans(got.output_spans))
    # 选 chunk1 全部：P_A_GRAVE 仍 mixed，e 纯；相邻不同标志不合并。
    got = qstream.query(1, 0, 2)
    print("finish 后 query(chunk 1, [0:2])（grave + e）:")
    print("  输出区间:", _format_output_spans(got.output_spans))

    print()
    print("=" * 68)
    print("5. 拒绝边界 A：非法反查区间（真实触发）")
    print("-" * 68)
    try:
        qstream.query(1, 2, 1)  # start > end
    except InvalidInputError as exc:
        print(f"捕获 {type(exc).__name__}: {exc}")
    try:
        qstream.query(1, 0, 3)  # 超出 chunk 1 长度 2
    except InvalidInputError as exc:
        print(f"捕获 {type(exc).__name__}: {exc}")

    print()
    print("=" * 68)
    print("6. 拒绝边界 B：循环分解表（真实触发）")
    print("-" * 68)
    try:
        normalize([chr(0xE200)], build_cyclic_table())
    except DecompositionCycleError as exc:
        rendered = " -> ".join(f"U+{cp:04X}" for cp in exc.chain)
        print(f"捕获 {type(exc).__name__}: {exc}")
        print(f"检测到的环: {rendered}")

    print()
    print("=" * 68)
    print("7. 拒绝边界 C：单段超过 1024 码点上限（真实触发）")
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

    elapsed = time.perf_counter() - started
    print()
    print("=" * 68)
    print(f"全部现场计算完成，用时 {elapsed * 1000:.1f} ms（无 sleep、无预录）。")


if __name__ == "__main__":
    main()
