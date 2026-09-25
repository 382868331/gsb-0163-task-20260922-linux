"""流式规范组合核心。

算法分三步，与题面契约一一对应：

1. **递归分解**：沿字符表的一步分解递归展开到叶子，带 DFS 环检测，
   成环抛 :class:`~canoncompose.errors.DecompositionCycleError`。
2. **稳定 ccc 排序**：一个"规范段"由一个 starter（ccc=0）及其后到下一个
   starter 之前的全部非 starter 组成；非 starter 以稳定插入方式按 ccc
   非降序排列（ccc 相同保持来源先后）。流开头若先出现非 starter，构成
   无 starter 的前导段（只排序、不组合）。
3. **阻塞组合**：段闭合时（遇到下一个 starter 或 ``finish``）执行组合。
   基字符为段首 starter（可被组合结果替换并继续参与后续组合）；
   组合当前非 starter *C* 前，检查其前一"未组合"字符：仅当其 ccc 为 0
   （即 *C* 前面没有任何未组合非 starter）或严格小于 ccc(*C*) 时才允许
   查询 ``table.composition(base, C)``；等 ccc 的未组合字符构成阻塞。

跨 chunk 边界时未闭合的段保留在流状态中，``finish()`` 冲刷最后一段。
单个规范段展开后超过 ``segment_limit``（默认 1024）个码点时拒绝，
抛 :class:`~canoncompose.errors.SegmentLimitError`。

``feed()`` 是原子的：任一码点处理失败时，本次 feed 已造成的流状态变更
全部回滚（段缓冲、已输出缓冲、流偏移、chunk 计数、反向索引），不留下
未说明的部分变更。

反向来源索引：流在发布输出的同时增量维护"输入点 → 输出位置"的反向
索引，:meth:`StreamNormalizer.query_output_ranges` 据此把输入半开区间
映射回所有与之有交集的输出区间。递归分解可让一个输入字符产生多个
输出、组合又会合并不同来源，索引不假设一对一或整体单调；查询只覆盖
**已发布**输出，仍在规范段缓冲中的输入来源以 ``pending`` 区间单独返回，
查询不会触发 ``finish``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from .errors import (
    DecompositionCycleError,
    InvalidInputError,
    InvalidTableError,
    SegmentLimitError,
)
from .table import CharacterTable
from .validators import (
    MAX_CODEPOINT,
    validate_chunk,
    validate_non_negative_int,
    validate_positive_int,
)

__all__ = [
    "SourcePoint",
    "SourceSpan",
    "OutputRange",
    "SourceQueryResult",
    "NormalizationResult",
    "StreamNormalizer",
    "normalize",
    "DEFAULT_SEGMENT_LIMIT",
]

DEFAULT_SEGMENT_LIMIT = 1024


@dataclass(frozen=True)
class SourcePoint:
    """单个输入码点的来源位置。"""

    chunk_index: int
    offset: int
    stream_offset: int


@dataclass(frozen=True)
class SourceSpan:
    """同一 chunk 内连续来源码点合并出的半开区间 ``[start, end)``。"""

    chunk_index: int
    start: int
    end: int


@dataclass(frozen=True)
class OutputRange:
    """反查结果中的一段已发布输出：半开区间 ``[start, end)``（输出码点位置）。

    - ``mixed``：``True`` 表示该段输出的来源除选区内的输入外，还混入了
      选区之外的输入码点（例如跨 chunk 组合的另一端不在选区内）。
    """

    start: int
    end: int
    mixed: bool


@dataclass(frozen=True)
class SourceQueryResult:
    """输入选区反查结果（见 :meth:`StreamNormalizer.query_output_ranges`）。

    - ``ranges``：与选区有交集的已发布输出区间，按输出位置有序，
      相邻且 ``mixed`` 相同的段已合并；
    - ``pending``：查询时仍滞留在规范段缓冲中的输入来源区间
      （半开区间，按 chunk、偏移有序，连续点已合并）。这些输入尚未产生
      已发布输出，不参与 ``ranges`` 的交集判定。
    """

    ranges: tuple[OutputRange, ...]
    pending: tuple[SourceSpan, ...]


@dataclass(frozen=True)
class NormalizationResult:
    """规范化结果。

    - ``text``：规范串；
    - ``codepoints``：规范串的码点元组（与 ``text`` 一一对应）；
    - ``source_spans``：全部输出码点所覆盖的原输入来源区间集合
      （按 chunk、偏移有序，半开区间，相邻点已合并）。
    """

    text: str
    codepoints: tuple[int, ...]
    source_spans: tuple[SourceSpan, ...]

    def __str__(self) -> str:
        return self.text


# 内部条目：(码点, 来源点元组)，不可变，便于原子回滚。
_Entry = tuple[int, tuple[SourcePoint, ...]]

# 迭代 DFS 的栈帧：[码点, 一步子节点(None=未查询), 下一子节点下标, 累加叶子]
_Frame = list


def _merge_points_to_spans(points: set[SourcePoint]) -> tuple[SourceSpan, ...]:
    """把来源点集合合并为有序的同 chunk 连续半开区间。"""
    ordered = sorted(points, key=lambda p: (p.chunk_index, p.offset))
    spans: list[SourceSpan] = []
    for p in ordered:
        if (
            spans
            and spans[-1].chunk_index == p.chunk_index
            and spans[-1].end == p.offset
        ):
            prev = spans[-1]
            spans[-1] = SourceSpan(prev.chunk_index, prev.start, p.offset + 1)
        else:
            spans.append(SourceSpan(p.chunk_index, p.offset, p.offset + 1))
    return tuple(spans)


def _validate_table_codepoint(value: object, *, owner: int) -> int:
    """校验表返回的码点值，错误归类为 InvalidTableError。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTableError(
            f"codepoint must be int, got {type(value).__name__}",
            codepoint=owner,
        )
    if value < 0 or value > MAX_CODEPOINT:
        raise InvalidTableError(
            f"codepoint out of range 0..0x{MAX_CODEPOINT:X}, got U+{value:X}",
            codepoint=owner,
        )
    return value


def _cycle_chain(stack: list[_Frame], repeated: int) -> tuple[int, ...]:
    """从 DFS 栈截取闭合环：repeated 首次出现处到栈顶，再闭合到 repeated。"""
    start = next(i for i, frame in enumerate(stack) if frame[0] == repeated)
    chain = [frame[0] for frame in stack[start:]]
    chain.append(repeated)
    return tuple(chain)


class StreamNormalizer:
    """有状态的流式规范组合器。

    用法::

        sn = StreamNormalizer(table)
        sn.feed(chunk1)
        sn.feed(chunk2)
        result = sn.finish()

    表方法返回值在首次使用时校验；非法返回值抛
    :class:`~canoncompose.errors.InvalidTableError`。
    """

    def __init__(
        self,
        table: CharacterTable,
        *,
        segment_limit: int = DEFAULT_SEGMENT_LIMIT,
    ) -> None:
        self._check_table(table)
        self._table = table
        self._limit = validate_positive_int(segment_limit, "segment_limit")

        # 已验证无环的叶子展开缓存：cp -> 递归叶子码点元组。
        self._expand_cache: dict[int, tuple[int, ...]] = {}
        # ccc 查询结果缓存（同时保证同一次运行内表视图一致）。
        self._ccc_cache: dict[int, int] = {}

        self._seg: list[_Entry] = []
        self._out: list[_Entry] = []
        # 反向索引：(chunk_index, offset) -> 已发布输出位置列表（升序）。
        # 只覆盖 _out 中已发布的条目；段缓冲中的条目不进入索引。
        self._reverse: dict[tuple[int, int], list[int]] = {}
        # 已成功 feed 的各 chunk 长度（码点数），用于选区合法性校验。
        self._chunk_lengths: list[int] = []
        self._stream_pos = 0
        self._chunk_count = 0
        self._finished = False
        self._result: NormalizationResult | None = None

    @staticmethod
    def _check_table(table: object) -> None:
        if not all(
            callable(getattr(table, name, None))
            for name in ("decomposition", "combining_class", "composition")
        ):
            raise InvalidInputError(
                "table must provide decomposition(cp), combining_class(cp) "
                "and composition(left, right) methods"
            )

    # ---- 表查询与校验 -------------------------------------------------

    def _decomp_one(self, cp: int) -> tuple[int, ...] | None:
        raw = self._table.decomposition(cp)
        if raw is None:
            return None
        if not isinstance(raw, tuple) or len(raw) == 0:
            raise InvalidTableError(
                "decomposition() must return None or a non-empty tuple of "
                f"codepoints, got {raw!r}",
                codepoint=cp,
            )
        return tuple(_validate_table_codepoint(x, owner=cp) for x in raw)

    def _ccc(self, cp: int) -> int:
        cached = self._ccc_cache.get(cp)
        if cached is not None:
            return cached
        raw = self._table.combining_class(cp)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InvalidTableError(
                "combining_class() must return int, got "
                f"{type(raw).__name__}",
                codepoint=cp,
            )
        if raw < 0:
            raise InvalidTableError(
                f"combining_class() must be non-negative, got {raw}",
                codepoint=cp,
            )
        self._ccc_cache[cp] = raw
        return raw

    def _compose_one(self, left: int, right: int) -> int | None:
        raw = self._table.composition(left, right)
        if raw is None:
            return None
        return _validate_table_codepoint(raw, owner=left)

    # ---- 递归分解（迭代 DFS，局部环检测 + 已验证子图缓存） --------------

    def _expand(self, root: int) -> tuple[int, ...]:
        """返回 root 递归分解后的叶子码点元组（按分解顺序）。"""
        cached = self._expand_cache.get(root)
        if cached is not None:
            return cached

        stack: list[_Frame] = [[root, None, 0, []]]
        on_stack: set[int] = {root}

        while stack:
            frame = stack[-1]
            node = frame[0]

            if frame[1] is None:
                children = self._decomp_one(node)
                frame[1] = children
                if children is None:
                    # 叶子节点。
                    self._expand_cache[node] = (node,)
                    on_stack.discard(node)
                    stack.pop()
                    if stack:
                        stack[-1][3].append(node)
                    else:
                        return (node,)
                    continue

            children: tuple[int, ...] = frame[1]
            if frame[2] < len(children):
                child = children[frame[2]]
                frame[2] += 1
                if child in on_stack:
                    raise DecompositionCycleError(_cycle_chain(stack, child))
                cached_child = self._expand_cache.get(child)
                if cached_child is not None:
                    # 已验证无环的子图，直接并入。
                    frame[3].extend(cached_child)
                else:
                    on_stack.add(child)
                    stack.append([child, None, 0, []])
            else:
                expanded = tuple(frame[3])
                self._expand_cache[node] = expanded
                on_stack.discard(node)
                stack.pop()
                if stack:
                    stack[-1][3].extend(expanded)
                else:
                    return expanded

        # 不可达：循环仅在栈空时结束，而栈空时各分支均已 return。
        raise RuntimeError("unreachable expansion state")  # pragma: no cover

    # ---- 段维护 -------------------------------------------------------

    def _append_leaf(
        self,
        cp: int,
        origins: tuple[SourcePoint, ...],
        loc: SourcePoint,
    ) -> list[_Entry]:
        """把一个分解叶子加入当前段，返回因段闭合而冲刷出的输出条目。"""
        flushed: list[_Entry] = []
        ccc = self._ccc(cp)
        entry: _Entry = (cp, origins)

        if ccc == 0:
            if self._seg:
                flushed = self._compose_segment(self._seg)
            self._seg = [entry]
            return flushed

        if not self._seg:
            # 流开头的前导非 starter：自成前导段。
            self._seg = [entry]
            return flushed

        if len(self._seg) >= self._limit:
            raise SegmentLimitError(
                "canonical segment exceeds codepoint limit",
                limit=self._limit,
                actual=len(self._seg) + 1,
                chunk_index=loc.chunk_index,
                offset=loc.offset,
                stream_offset=loc.stream_offset,
                codepoint=cp,
            )

        # 稳定插入：越过 ccc 严格更大的既有条目，等 ccc 留在其后。
        pos = len(self._seg)
        while pos > 0 and self._ccc(self._seg[pos - 1][0]) > ccc:
            pos -= 1
        self._seg.insert(pos, entry)
        return flushed

    def _compose_segment(self, entries: list[_Entry]) -> list[_Entry]:
        """段闭合时的阻塞组合，返回组合后的输出条目。"""
        if not entries:
            return []
        if self._ccc(entries[0][0]) != 0:
            # 前导非 starter 段：没有基字符，不组合。
            return list(entries)

        base: _Entry = entries[0]
        pending: list[_Entry] = []
        for entry in entries[1:]:
            cur_ccc = self._ccc(entry[0])
            # 前一"未组合"字符：pending 为空时视为段首 starter（ccc=0）。
            prev_ccc = 0 if not pending else self._ccc(pending[-1][0])
            composed = None
            if prev_ccc == 0 or prev_ccc < cur_ccc:
                composed = self._compose_one(base[0], entry[0])
            if composed is not None:
                # 组合结果占据基字符位置，继续参与后续组合，ccc 视为 0。
                base = (composed, base[1] + entry[1])
            else:
                pending.append(entry)
        return [base, *pending]

    # ---- 输出发布与反向索引 ----------------------------------------------

    def _publish(self, entries: list[_Entry]) -> None:
        """把段闭合冲刷出的条目追加到已发布输出，并同步反向索引。"""
        pos = len(self._out)
        self._out.extend(entries)
        for i, (_, origins) in enumerate(entries):
            out_pos = pos + i
            for point in origins:
                key = (point.chunk_index, point.offset)
                bucket = self._reverse.get(key)
                if bucket is None:
                    self._reverse[key] = [out_pos]
                else:
                    bucket.append(out_pos)

    def _rebuild_reverse(self) -> None:
        """从已发布输出整体重建反向索引（仅用于 feed 失败回滚）。"""
        self._reverse = {}
        for pos, (_, origins) in enumerate(self._out):
            for point in origins:
                key = (point.chunk_index, point.offset)
                bucket = self._reverse.get(key)
                if bucket is None:
                    self._reverse[key] = [pos]
                else:
                    bucket.append(pos)

    # ---- 公开 API -----------------------------------------------------

    def feed(self, chunk: str) -> None:
        """喂入一个 chunk（str）。原子：失败时流状态回到调用前。"""
        if self._finished:
            raise InvalidInputError("stream already finished")
        validate_chunk(chunk, self._chunk_count)

        # 快照全部可变流状态（展开/ccc 缓存只存已验证结果，属纯增益；
        # 反向索引由 _out 派生，回滚 _out 后整体重建）。
        backup_seg = list(self._seg)
        backup_out_len = len(self._out)
        backup_pos = self._stream_pos
        backup_chunk_count = self._chunk_count

        try:
            chunk_index = self._chunk_count
            self._chunk_count += 1
            for offset, ch in enumerate(chunk):
                point = SourcePoint(chunk_index, offset, self._stream_pos)
                self._stream_pos += 1
                origins = (point,)
                for leaf in self._expand(ord(ch)):
                    flushed = self._append_leaf(leaf, origins, point)
                    if flushed:
                        self._publish(flushed)
            self._chunk_lengths.append(len(chunk))
        except Exception:
            self._seg = backup_seg
            del self._out[backup_out_len:]
            self._rebuild_reverse()
            self._stream_pos = backup_pos
            self._chunk_count = backup_chunk_count
            raise

    def finish(self) -> NormalizationResult:
        """冲刷最后一段并返回完整结果。可重复调用，返回同一结果。"""
        if self._finished:
            assert self._result is not None
            return self._result
        if self._seg:
            self._publish(self._compose_segment(self._seg))
            self._seg = []
        self._result = self._build_result()
        self._finished = True
        return self._result

    def _build_result(self) -> NormalizationResult:
        codepoints = tuple(cp for cp, _ in self._out)
        text = "".join(chr(cp) for cp in codepoints)

        points: set[SourcePoint] = set()
        for _, origins in self._out:
            points.update(origins)
        spans = _merge_points_to_spans(points)
        return NormalizationResult(text, codepoints, spans)

    # ---- 反向查询 -------------------------------------------------------

    def query_output_ranges(
        self,
        chunk_index: int,
        start: int,
        end: int,
    ) -> SourceQueryResult:
        """把输入半开区间 ``chunk[chunk_index][start:end]`` 反查为输出区间。

        返回与选区有交集的全部**已发布**输出区间（按输出位置合并连续且
        ``mixed`` 相同的段）；``mixed=True`` 表示该段输出的来源还混入选区
        之外的输入。仍在规范段缓冲中的输入来源以 ``pending`` 单独返回；
        查询不触发 ``finish()``，之后仍可继续 ``feed``。

        空区间（``start == end``）只做合法性检查，返回空结果。
        坐标合法性（类型、非负、``start <= end``、chunk 已 feed、
        ``end`` 不超过该 chunk 长度）不满足时抛
        :class:`~canoncompose.errors.InvalidInputError`。
        """
        self._validate_query_range(chunk_index, start, end)
        if start == end:
            return SourceQueryResult((), ())

        positions: set[int] = set()
        for offset in range(start, end):
            bucket = self._reverse.get((chunk_index, offset))
            if bucket:
                positions.update(bucket)

        ranges: list[OutputRange] = []
        for pos in sorted(positions):
            _, origins = self._out[pos]
            mixed = any(
                not (p.chunk_index == chunk_index and start <= p.offset < end)
                for p in origins
            )
            if ranges and ranges[-1].mixed == mixed and ranges[-1].end == pos:
                prev = ranges[-1]
                ranges[-1] = OutputRange(prev.start, pos + 1, mixed)
            else:
                ranges.append(OutputRange(pos, pos + 1, mixed))
        return SourceQueryResult(tuple(ranges), self.pending_sources())

    def pending_sources(self) -> tuple[SourceSpan, ...]:
        """当前仍滞留在规范段缓冲中的输入来源区间（不触发 ``finish``）。"""
        points: set[SourcePoint] = set()
        for _, origins in self._seg:
            points.update(origins)
        return _merge_points_to_spans(points)

    def _validate_query_range(
        self, chunk_index: object, start: object, end: object
    ) -> None:
        chunk_index = validate_non_negative_int(chunk_index, "chunk_index")
        start = validate_non_negative_int(
            start, "start", chunk_index=chunk_index
        )
        end = validate_non_negative_int(end, "end", chunk_index=chunk_index)
        if chunk_index >= len(self._chunk_lengths):
            raise InvalidInputError(
                f"chunk {chunk_index} has not been fed "
                f"({len(self._chunk_lengths)} chunk(s) so far)",
                chunk_index=chunk_index,
            )
        if start > end:
            raise InvalidInputError(
                f"query range start {start} > end {end}",
                chunk_index=chunk_index,
            )
        length = self._chunk_lengths[chunk_index]
        if end > length:
            raise InvalidInputError(
                f"query end {end} exceeds chunk {chunk_index} "
                f"length {length}",
                chunk_index=chunk_index,
            )


def normalize(
    chunks: Iterable[str],
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> NormalizationResult:
    """一次性规范化一个 chunk 序列。

    ``chunks`` 是 str 的可迭代对象（不得直接传单个 str）；处理等价于按序
    ``StreamNormalizer.feed`` 后 ``finish``。任何一步抛异常都不会返回部分
    结果（流对象为函数内部所有，随异常丢弃）。
    """
    if isinstance(chunks, str):
        raise InvalidInputError(
            "chunks must be an iterable of str, not a single str"
        )
    try:
        iterator: Iterator[str] = iter(chunks)
    except TypeError as exc:
        raise InvalidInputError(
            f"chunks must be an iterable of str, got {type(chunks).__name__}"
        ) from exc

    stream = StreamNormalizer(table, segment_limit=segment_limit)
    for chunk_index, chunk in enumerate(iterator):
        validate_chunk(chunk, chunk_index)
        stream.feed(chunk)
    return stream.finish()
