"""独立一次性朴素参考实现。

本模块 **不导入 canoncompose**，期望值全部在这里独立计算：
递归分解（朴素递归）→ ccc 稳定排序 → 阻塞组合。只用于测试中与被测
流式核心交叉验证；"往返一致"之外的正确性以本参考为准。
"""

from __future__ import annotations


def _decompose(cp, table, out, seen):
    """朴素递归分解，带环保护（测试数据正常无环）。"""
    if cp in seen:
        raise RuntimeError(f"cycle in reference data at U+{cp:04X}")
    children = table.decomposition(cp)
    if children is None:
        out.append(cp)
        return
    seen = seen | {cp}
    for child in children:
        _decompose(child, table, out, seen)


def _naive_compose(segment, table, ccc_of):
    """段内阻塞组合，返回码点列表。规则与题面一致。"""
    if not segment or ccc_of(segment[0]) != 0:
        return list(segment)
    base = segment[0]
    pending = []
    for cp in segment[1:]:
        cur = ccc_of(cp)
        prev = 0 if not pending else ccc_of(pending[-1])
        composed = None
        if prev == 0 or prev < cur:
            composed = table.composition(base, cp)
        if composed is not None:
            base = composed
        else:
            pending.append(cp)
    return [base, *pending]


def _reference_core(chunks, table):
    """一次性返回 (码点元组, 每个输出码点的来源点集合元组)。

    来源点为 ``(chunk_index, offset, stream_offset)``。
    """
    # 1) 全量递归分解，逐叶子记录来源。
    leaves = []  # (cp, (chunk_index, offset, stream_offset))
    stream_pos = 0
    for chunk_index, chunk in enumerate(chunks):
        for offset, ch in enumerate(chunk):
            point = (chunk_index, offset, stream_pos)
            stream_pos += 1
            expanded = []
            _decompose(ord(ch), table, expanded, frozenset())
            for leaf in expanded:
                leaves.append((leaf, point))

    # 2) 按 ccc=0 切段，段内稳定排序。
    segments = []
    current = []
    for cp, point in leaves:
        if table.combining_class(cp) == 0:
            if current:
                segments.append(current)
            current = [(cp, point)]
        else:
            current.append((cp, point))
    if current:
        segments.append(current)

    ordered = []
    for seg in segments:
        # Python 排序稳定：等 ccc 保持来源先后。
        ordered.extend(
            sorted(seg, key=lambda item: table.combining_class(item[0]))
        )

    # 3) 在排序后的流上重新按 starter 切段并组合。
    comp_segments = []
    current = []
    for item in ordered:
        if table.combining_class(item[0]) == 0:
            if current:
                comp_segments.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        comp_segments.append(current)

    out_cps = []
    out_points = []
    for seg in comp_segments:
        cps = [item[0] for item in seg]
        points = [item[1] for item in seg]
        # 朴素组合时同步并来源：用一个小包装跟踪每个输出码点的来源集合。
        bag = {i: {points[i]} for i in range(len(cps))}
        if cps and table.combining_class(cps[0]) == 0:
            base_i = 0
            pending_is = []
            for i in range(1, len(cps)):
                cur = table.combining_class(cps[i])
                prev = (
                    0
                    if not pending_is
                    else table.combining_class(cps[pending_is[-1]])
                )
                composed = None
                if prev == 0 or prev < cur:
                    composed = table.composition(cps[base_i], cps[i])
                if composed is not None:
                    cps[base_i] = composed
                    bag[base_i] |= bag[i]
                else:
                    pending_is.append(i)
            keep = [base_i, *pending_is]
        else:
            keep = list(range(len(cps)))
        for i in keep:
            out_cps.append(cps[i])
            out_points.append(frozenset(bag[i]))

    return tuple(out_cps), tuple(out_points)


def reference_normalize(chunks, table):
    """一次性返回 (码点元组, 来源区间元组)。

    区间为 ``(chunk_index, start, end)`` 半开区间，同 chunk 相邻点已合并。
    """
    out_cps, out_points = _reference_core(chunks, table)
    # 展平来源点并合并同 chunk 连续偏移（独立复算区间集合）。
    all_points = set()
    for bag_i in out_points:
        all_points |= bag_i
    spans = []
    for ci, off, _so in sorted(all_points):
        if spans and spans[-1][0] == ci and spans[-1][2] == off:
            spans[-1] = (ci, spans[-1][1], off + 1)
        else:
            spans.append((ci, off, off + 1))
    return out_cps, tuple(spans)


def reference_normalize_points(chunks, table):
    """一次性返回 (码点元组, 每个输出码点的来源点 frozenset 元组)。

    供多模式查找的朴素参考按命中区间精确合并来源点（不取最小/最大下标）。
    """
    return _reference_core(chunks, table)
