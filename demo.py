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
5. 按字符位置编辑：插入组合记号触发组合、首部边界插入、跨 chunk 删除，
   打印变化区间与新旧下标映射；
6. 边界演示（真实触发、非预录）：循环分解表触发 DecompositionCycleError；
   单段 1025 码点触发 SegmentLimitError；编辑位置越界触发
   InvalidInputError。

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
    normalize_delete,
    normalize_insert,
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


def _format_mapping(edit) -> str:
    def side(values):
        return "(" + ", ".join("·" if v is None else str(v) for v in values) + ")"

    return (
        f"new→old {side(edit.mapping.new_to_old)}  "
        f"old→new {side(edit.mapping.old_to_new)}"
    )


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
    print("4. 按字符位置编辑：插入/删除后全量重新规范化 + 新旧映射")
    print("-" * 68)
    # 4a. 正常：在 "ae" 偏移 1 处插入 grave(230)，与 a 组合为 P_A_GRAVE。
    edit = normalize_insert(["ae"], 1, chr(M_GRAVE), table)
    print("插入 grave 到 'ae' 偏移 1:")
    print("  旧规范码点:", describe(edit.previous.codepoints))
    print("  新规范码点:", describe(edit.result.codepoints))
    print(f"  变化区间 old{edit.old_span} new{edit.new_span}")
    print(" ", _format_mapping(edit))

    # 4b. 边界：首部插入前导记号（永不组合）与跨 chunk 删除。
    edit_head = normalize_insert(["a"], 0, chr(M_GRAVE), table)
    print("首部边界：插入 grave 到 'a' 偏移 0（前导记号不组合）:")
    print("  新规范码点:", describe(edit_head.result.codepoints))
    print(f"  变化区间 old{edit_head.old_span} new{edit_head.new_span}")
    print(" ", _format_mapping(edit_head))

    edit_del = normalize_delete(["ao", "ea"], 1, 3, table)
    print("跨 chunk 删除：['ao','ea'] 删除拼接串 [1,3)（'o' 与 'e'）:")
    print("  新规范码点:", describe(edit_del.result.codepoints))
    print(f"  变化区间 old{edit_del.old_span} new{edit_del.new_span}")
    print(" ", _format_mapping(edit_del))

    # 4c. 实际错误：编辑位置越界（真实触发）。
    print("编辑位置越界（真实触发）:")
    try:
        normalize_insert(["ae"], 3, "x", table)
    except InvalidInputError as exc:
        print(f"  捕获 {type(exc).__name__}: {exc}")

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
