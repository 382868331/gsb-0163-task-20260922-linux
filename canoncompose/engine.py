"""流式规范组合核心。

算法分三步，与题面契约一一对应：

1. **表编译与递归分解**：构造流时先完整检查表并生成编译快照；分解图中
   的环在处理文本前抛出
   :class:`~canoncompose.errors.DecompositionCycleError`。
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
全部回滚（段缓冲、已输出缓冲、流偏移、chunk 计数），不留下未说明的
部分变更。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from .errors import (
    InvalidInputError,
    SegmentLimitError,
)
from .compiler import CompiledCharacterTable, compile_table
from .table import CharacterTable
from .validators import validate_chunk, validate_positive_int

__all__ = [
    "SourcePoint",
    "SourceSpan",
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

class StreamNormalizer:
    """有状态的流式规范组合器。

    用法::

        sn = StreamNormalizer(table)
        sn.feed(chunk1)
        sn.feed(chunk2)
        result = sn.finish()

    传入原始 :class:`~canoncompose.table.DictCharacterTable` 时会自动调用
    :func:`~canoncompose.compile_table`；也可以显式编译一次后传给多个独立
    流。非法表在处理文本前抛
    :class:`~canoncompose.errors.InvalidTableError`，并尽量携带具体表项。
    """

    def __init__(
        self,
        table: CharacterTable,
        *,
        segment_limit: int = DEFAULT_SEGMENT_LIMIT,
    ) -> None:
        self._table: CompiledCharacterTable = compile_table(table)
        self._limit = validate_positive_int(segment_limit, "segment_limit")

        self._seg: list[_Entry] = []
        self._out: list[_Entry] = []
        self._stream_pos = 0
        self._chunk_count = 0
        self._finished = False
        self._result: NormalizationResult | None = None

    def _ccc(self, cp: int) -> int:
        return self._table.combining_class(cp)

    def _compose_one(self, left: int, right: int) -> int | None:
        return self._table.composition(left, right)

    # ---- 递归分解：展开已在表编译期完成并检查 ------------------------

    def _expand(self, root: int) -> tuple[int, ...]:
        """返回 root 递归分解后的叶子码点元组（按分解顺序）。"""
        return self._table.expansion(root)

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

    # ---- 公开 API -----------------------------------------------------

    def feed(self, chunk: str) -> None:
        """喂入一个 chunk（str）。原子：失败时流状态回到调用前。"""
        if self._finished:
            raise InvalidInputError("stream already finished")
        validate_chunk(chunk, self._chunk_count)

        # 快照全部可变流状态（展开/ccc 缓存只存已验证结果，属纯增益）。
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
                        self._out.extend(flushed)
        except Exception:
            self._seg = backup_seg
            del self._out[backup_out_len:]
            self._stream_pos = backup_pos
            self._chunk_count = backup_chunk_count
            raise

    def finish(self) -> NormalizationResult:
        """冲刷最后一段并返回完整结果。可重复调用，返回同一结果。"""
        if self._finished:
            assert self._result is not None
            return self._result
        if self._seg:
            self._out.extend(self._compose_segment(self._seg))
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
        return NormalizationResult(text, codepoints, tuple(spans))


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
