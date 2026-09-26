#!/usr/bin/env python3
"""本轮迭代演示：显式字符表上的多模式流式规范查找。

运行::

    python demo_iteration.py

内容（全部现场计算，无 sleep、无预录，远小于 90 秒预算）：
1. 登记多个模式（含递归分解等价写法、共用后缀、重叠），按 chunk
   喂入文本，打印每次 feed 新确认的命中，展示未闭合段不会提前 finish、
   已交付命中不撤回、不同模式同规范串仍各带自己的 id；
2. 边界情况（真实触发）：来源不是连续片段时，命中的来源区间是两个
   分离区间而不是 [min,max)；并演示空模式被拒绝的错误语义。
"""

from __future__ import annotations

import time

from canoncompose import InvalidInputError, StreamSearcher, find_all
from canoncompose.samples import (
    A,
    M_DOTBELOW,
    M_GRAVE,
    M_RING,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    build_demo_table,
    describe,
)


def _format_spans(spans) -> str:
    return ", ".join(
        f"chunk {s.chunk_index}[{s.start}:{s.end}]" for s in spans
    ) or "(空)"


def main() -> None:
    started = time.perf_counter()
    table = build_demo_table()

    print("=" * 72)
    print("1. 多模式流式查找（递归分解等价 / 共用后缀 / 重叠）")
    print("-" * 72)
    # 模式刻意覆盖：
    # - decomposed 与 precomposed 规范后是同一个串，仍各带各的 id；
    # - x-grave 与 grave 共用后缀（grave 是 x-grave 的后缀）；
    # - "a" 会与其它模式产生重叠命中。
    patterns = [
        ("precomposed", chr(P_A_GRAVE_RING)),
        ("decomposed", chr(A) + chr(M_GRAVE) + chr(M_RING)),
        ("x-grave", "x" + chr(A) + chr(M_GRAVE)),
        ("grave", chr(A) + chr(M_GRAVE)),
        ("a", chr(A)),
    ]
    for pid, pat in patterns:
        print(f"  模式 {pid!r:16} 原写法: {describe(tuple(ord(c) for c in pat))}")

    # 文本在关键边界被切开：第一个 chunk 结束时段还没闭合。
    text = (
        chr(A) + chr(M_GRAVE) + chr(M_RING)
        + "x" + chr(A) + chr(M_GRAVE)
        + chr(P_A_GRAVE_RING)
        + "aaa"
    )
    chunks = [
        text[:1],                       # 只有 starter a，段未闭合
        text[1:3],                      # grave + ring，段仍未闭合
        text[3:6],                      # x 闭合上一段，x-à 段又挂起
        text[6:8],                      # 预组合字符闭合上一段
        text[8:],                       # aaa 重叠命中
    ]
    print()
    ss = StreamSearcher(table, patterns)
    total = 0
    for i, chunk in enumerate(chunks):
        print(f"feed chunk {i}: {describe(tuple(ord(c) for c in chunk))}")
        new = ss.feed(chunk)
        total += len(new)
        if not new:
        # 段未闭合或本批无命中：证明不会为查找提前 finish。
            print("    （本批无新确认命中）")
        for m in new:
            print(
                f"    命中 id={m.pattern_id!r:14} 规范区间[{m.start}:{m.end}) "
                f"来源: {_format_spans(m.source_spans)}"
            )
    tail = ss.finish()
    total += len(tail)
    for m in tail:
        print(
            f"finish 交付 id={m.pattern_id!r:14} 规范区间[{m.start}:{m.end}) "
            f"来源: {_format_spans(m.source_spans)}"
        )
    assert ss.finish() == (), "重复 finish 不应重复交付命中"

    print()
    print("-" * 72)
    print("与一次性查找对比：")
    all_at_once = find_all([text], patterns, table)
    print(f"  流式累计 {total} 个命中，一次性 {len(all_at_once)} 个命中")
    assert total == len(all_at_once), "流式与一次性命中数不一致"

    print()
    print("=" * 72)
    print("2. 边界情况：来源不是连续片段（ccc 重排 + 组合）")
    print("-" * 72)
    # 输入 a + dotbelow(ccc=220) + grave(ccc=230)：
    # 稳定重排后顺序为 a, dotbelow, grave；grave 与 a 组合成 à，
    # 于是命中 à 的来源是输入偏移 0 和 2，中间偏移 1 属于另一个
    # 规范码点。正确来源区间是 [0,1)∪[2,3)，不能假装成 [0,3)。
    text2 = chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE)
    print(f"输入码点: {describe(tuple(ord(c) for c in text2))}")
    matches = find_all([text2], [("agrave", chr(P_A_GRAVE))], table)
    assert len(matches) == 1
    m = matches[0]
    print(
        f"命中 id={m.pattern_id!r} 规范区间[{m.start}:{m.end})，"
        f"来源区间: {_format_spans(m.source_spans)}"
    )
    assert tuple(m.source_spans) and m.source_spans[0].end == 1
    assert m.source_spans[1].start == 2
    print("=> 两个分离区间，中间的 dotbelow 输入码点没有被算进来源 ✓")

    print()
    print("-" * 72)
    print("错误语义：空模式被拒绝（真实触发）")
    try:
        StreamSearcher(table, [("empty", "")])
    except InvalidInputError as exc:
        print(f"捕获 {type(exc).__name__}: {exc}")

    elapsed = time.perf_counter() - started
    print()
    print("=" * 72)
    print(f"全部现场计算完成，用时 {elapsed * 1000:.1f} ms（无 sleep、无预录）。")


if __name__ == "__main__":
    main()
