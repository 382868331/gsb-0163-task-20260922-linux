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
   单段 1025 码点触发 SegmentLimitError。

所有结果都是现场计算，不使用 sleep 或预录结果。
"""

from __future__ import annotations

import time

from canoncompose import (
    DecompositionCycleError,
    INSERT_CHUNK_INDEX,
    InvalidInputError,
    NormalizedEditor,
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
    print("4. 按位置插入（正常结果：触发组合、重排与来源映射）")
    print("-" * 68)
    # 起始规范文本 = 单独的 a。在尾缝（偏移 1）插入 grave → a+grave 组合。
    editor = NormalizedEditor(table, chr(A))
    print("编辑前规范码点:", describe(editor.codepoints))
    ins = editor.insert(1, chr(M_GRAVE))
    print("插入 grave 后 :", describe(ins.codepoints),
          f"变化区间={ins.changed_range} 旧区间={ins.old_changed_range}",
          f"LCP={ins.unchanged_prefix} LCS={ins.unchanged_suffix}")
    print("  旧→新缝映射:", ins.position_map.old_to_new)
    print("  新→旧缝映射:", ins.position_map.new_to_old)
    print("  来源区间   :", _format_spans(ins.source_spans),
          f"（chunk {INSERT_CHUNK_INDEX} 标记本次插入内容）")
    assert ins.codepoints == (P_A_GRAVE,)

    # 在已组合基字符之后插入 dotbelow(220)：重排后 dotbelow 成为未组合项。
    ins2 = editor.insert(1, chr(M_DOTBELOW))
    print("再插入 dotbelow(220) 后:", describe(ins2.codepoints))
    print("  变化区间=", ins2.changed_range,
          " 旧→新=", ins2.position_map.old_to_new)
    assert ins2.codepoints == (P_A_GRAVE, M_DOTBELOW)

    print()
    print("=" * 68)
    print("5. 按位置删除（边界：删除未组合记号后基字符保持组合态）")
    print("-" * 68)
    print("编辑前规范码点:", describe(editor.codepoints))
    dele = editor.delete_range(1)  # 删除 dotbelow
    print("删除规范串[1:2]后:", describe(dele.codepoints),
          f"被删={describe(tuple(ord(c) for c in dele.deleted))}")
    print("  变化区间=", dele.changed_range,
          " 旧区间=", dele.old_changed_range,
          " 新→旧=", dele.position_map.new_to_old)
    assert dele.codepoints == (P_A_GRAVE,)

    # 首/尾边界：删除最后一个码点。
    tail = editor.delete_range(len(editor.text) - 1)
    print("再删尾码点后:", describe(tail.codepoints) or "(空串)",
          "text=", repr(editor.text))
    assert editor.text == ""

    print()
    print("=" * 68)
    print("6. 编辑接口的实际错误（失败且不留下半更新状态）")
    print("-" * 68)
    editor.set_text(chr(A) + chr(E))
    before = editor.text
    # 6a. 越界位置。
    try:
        editor.insert(9, chr(M_GRAVE))
    except InvalidInputError as exc:
        print(f"6a 捕获 {type(exc).__name__}: {exc}")
    assert editor.text == before, "越界插入后状态被改动"
    # 6b. 插入含分解环的码点（环表编辑器，真实触发表环检测），
    #     失败后状态不变，仍可继续编辑。
    cyc_editor = NormalizedEditor(build_cyclic_table(), chr(A) + chr(E))
    cyc_before = cyc_editor.text
    try:
        cyc_editor.insert(1, chr(0xE200))
    except DecompositionCycleError as exc:
        print(f"6b 捕获 {type(exc).__name__}: {exc}")
    assert cyc_editor.text == cyc_before, "环插入失败后状态被改动"
    print("   失败后继续合法插入:", end=" ")
    after = cyc_editor.insert(2, chr(M_GRAVE))
    print(describe(after.codepoints))
    assert after.codepoints == (A, E, M_GRAVE)

    print()
    print("=" * 68)
    print("7. 拒绝边界 A：循环分解表（真实触发）")
    print("-" * 68)
    try:
        normalize([chr(0xE200)], build_cyclic_table())
    except DecompositionCycleError as exc:
        rendered = " -> ".join(f"U+{cp:04X}" for cp in exc.chain)
        print(f"捕获 {type(exc).__name__}: {exc}")
        print(f"检测到的环: {rendered}")

    print()
    print("=" * 68)
    print("8. 拒绝边界 B：单段超过 1024 码点上限（真实触发）")
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
