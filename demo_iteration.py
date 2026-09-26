#!/usr/bin/env python3
"""固定输入演示：多模式流式查找（本次迭代新功能）。

运行::

    python demo_iteration.py

内容（全部现场计算，无 sleep、无预录）：
1. 多模式查找：不同写法的同一规范串分别带回各自 ID；
2. 流式分块：段未闭合时命中不提前交付，闭合后补发，且已交付不撤回；
3. 重叠命中与多个模式共用后缀；
4. 命中来源区间可能不连续（ccc 重排 + 组合），输出精确区间集合；
5. 边界演示（真实触发）：空模式被 InvalidInputError 拒绝。
"""

from __future__ import annotations

import time

from canoncompose import InvalidInputError, StreamSearcher, find_all
from canoncompose.samples import (
    A,
    E,
    M_ACUTE,
    M_DOTBELOW,
    M_GRAVE,
    M_RING,
    P_A_GRAVE,
    P_A_GRAVE_RING,
    build_demo_table,
    describe,
)


def _fmt_spans(spans) -> str:
    return ", ".join(
        f"chunk {s.chunk_index}[{s.start}:{s.end}]" for s in spans
    ) or "(空)"


def _print_hits(hits) -> None:
    if not hits:
        print("  (无命中)")
    for h in hits:
        print(
            f"  命中 id={h.pattern_id!r} 规范区间 [{h.start}, {h.end})"
            f" 来源: {_fmt_spans(h.source_spans)}"
        )


def main() -> None:
    started = time.perf_counter()
    table = build_demo_table()

    print("=" * 68)
    print("1. 一次性多模式查找：同一规范串的不同写法分别带回各自 ID")
    print("-" * 68)
    patterns = [
        ("p-decomposed", chr(A) + chr(M_GRAVE)),   # 分解写法
        ("p-precomposed", chr(P_A_GRAVE)),          # 预组合写法，规范后相同
        ("p-ring", chr(P_A_GRAVE_RING)),            # 两级预组合（递归分解）
        ("p-e-a-grave", chr(E) + chr(A) + chr(M_GRAVE)),  # 与上行共用后缀
    ]
    text = chr(E) + chr(A) + chr(M_GRAVE) + chr(A) + chr(M_GRAVE) + chr(M_RING)
    print("文本码点:", describe(tuple(ord(c) for c in text)))
    hits = find_all([text], table, patterns)
    _print_hits(hits)
    ids_at_1 = sorted(h.pattern_id for h in hits if h.start == 1)
    assert ids_at_1 == ["p-decomposed", "p-precomposed"], ids_at_1
    print("=> 写法不同、规范后相同的两个模式在位置 1 各自命中 ✓")

    print()
    print("=" * 68)
    print("2. 流式分块：未闭合段不提前交付，已交付命中不撤回")
    print("-" * 68)
    searcher = StreamSearcher(table, [("p", chr(A) + chr(M_GRAVE))])
    chunks = [chr(A), chr(M_GRAVE), chr(E)]
    delivered = []
    for i, chunk in enumerate(chunks):
        new_hits = searcher.feed(chunk)
        delivered.extend(new_hits)
        print(f"feed chunk {i} ({describe(tuple(ord(c) for c in chunk))})"
              f" -> 新交付 {len(new_hits)} 条")
        _print_hits(new_hits)
    tail = searcher.finish()
    delivered.extend(tail)
    assert len(delivered) == 1 and delivered[0].start == 0
    assert [ (s.chunk_index, s.start, s.end) for s in delivered[0].source_spans ] == [
        (0, 0, 1), (1, 0, 1),
    ]
    print("=> chunk 0/1 时段未闭合不交付；chunk 2 闭合后交付且来源跨 chunk ✓")

    print()
    print("=" * 68)
    print("3. 重叠命中")
    print("-" * 68)
    hits = find_all(["aaaa"], table, [("p-aa", "aa")])
    _print_hits(hits)
    assert [(h.start, h.end) for h in hits] == [(0, 2), (1, 3), (2, 4)]
    print("=> 'aa' 在 'aaaa' 中重叠命中 3 次 ✓")

    print()
    print("=" * 68)
    print("4. 命中来源可以不连续：输出精确区间集合而非最小/最大下标")
    print("-" * 68)
    # a + dotbelow(220) + grave(230)：重排后 grave 与 a 组合成 P_A_GRAVE，
    # 命中来源是偏移 0 与 2（偏移 1 的 dotbelow 不属于该命中）。
    text = chr(A) + chr(M_DOTBELOW) + chr(M_GRAVE)
    hits = find_all([text], table, [("p", chr(P_A_GRAVE))])
    _print_hits(hits)
    spans = [(s.chunk_index, s.start, s.end) for s in hits[0].source_spans]
    assert spans == [(0, 0, 1), (0, 2, 3)], spans
    print("=> 来源为 [0,1) 与 [2,3) 两段，没有用 [0,3) 假装连续 ✓")

    print()
    print("=" * 68)
    print("5. 拒绝边界：空模式（真实触发）")
    print("-" * 68)
    try:
        StreamSearcher(table, [("empty", "")])
    except InvalidInputError as exc:
        print(f"捕获 {type(exc).__name__}: {exc}")
        print("=> 空模式被明确拒绝 ✓")

    elapsed = time.perf_counter() - started
    print()
    print("=" * 68)
    print(f"全部现场计算完成，用时 {elapsed * 1000:.1f} ms（无 sleep、无预录）。")


if __name__ == "__main__":
    main()
