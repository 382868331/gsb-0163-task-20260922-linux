#!/usr/bin/env python3
"""固定输入演示：显式字符表的流式规范组合。

运行::

    python demo.py

内容：
1. 用仓库内确定性字符表对一段固定输入做规范化，打印规范串码点序列；
2. 把同一输入按固定切点分成 3 个 chunk 流式 feed，验证分块结果与一次性
   结果一致；
3. 对规范串再规范化一次，验证幂等；
4. 反向来源索引：输入选区反查已发布输出区间（含 pending 与失败示例）；
5. 打印输出来源区间集合；
6. 边界演示（真实触发、非预录）：循环分解表触发 DecompositionCycleError；
   单段 1025 码点触发 SegmentLimitError。

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


def _format_ranges(ranges) -> str:
    return ", ".join(
        f"输出[{r.start}:{r.end}]{' 混入选区外来源' if r.mixed else ''}"
        for r in ranges
    ) or "(无交集)"


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
    print("4. 反向来源索引：输入选区反查已发布输出区间")
    print("-" * 68)
    rev = StreamNormalizer(table)
    rev.feed(chr(A))               # chunk 0：'a'，段挂起
    rev.feed(chr(M_GRAVE))         # chunk 1：grave，与 chunk 0 跨块同段
    q = rev.query_output_ranges(0, 0, 1)
    print("feed 'a'、grave 后（未 finish）查询 chunk 0[0:1]:")
    print("  命中输出:", _format_ranges(q.ranges))
    print("  pending :", _format_spans(q.pending), "（段缓冲中，未发布）")
    rev.feed(chr(E))               # chunk 2：starter 冲刷前一段（a+grave→à）
    q = rev.query_output_ranges(1, 0, 1)
    print("再 feed 'e' 后查询 chunk 1[0:1]（grave 一侧）:")
    print("  命中输出:", _format_ranges(q.ranges))
    print("  pending :", _format_spans(q.pending))
    rev.finish()
    q = rev.query_output_ranges(0, 0, 1)
    print("finish 后查询 chunk 0[0:1]（'a' 一侧）:")
    print("  命中输出:", _format_ranges(q.ranges))
    print("  pending :", _format_spans(q.pending))
    q = rev.query_output_ranges(0, 0, 0)
    print("空区间 chunk 0[0:0]（只做合法性检查）:",
          _format_ranges(q.ranges), "/", _format_spans(q.pending))
    try:
        rev.query_output_ranges(0, 2, 1)
    except InvalidInputError as exc:
        print(f"失败示例（真实触发）查询 chunk 0[2:1] → "
              f"{type(exc).__name__}: {exc}")

    print()
    print("=" * 68)
    print("5. 拒绝边界 A：循环分解表（真实触发）")
    print("-" * 68)
    try:
        normalize([chr(0xE200)], build_cyclic_table())
    except DecompositionCycleError as exc:
        rendered = " -> ".join(f"U+{cp:04X}" for cp in exc.chain)
        print(f"捕获 {type(exc).__name__}: {exc}")
        print(f"检测到的环: {rendered}")

    print()
    print("=" * 68)
    print("6. 拒绝边界 B：单段超过 1024 码点上限（真实触发）")
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
