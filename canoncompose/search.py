"""多模式流式查找：在规范组合后的码点流上匹配多个模式。

模式与文本使用**同一张字符表**分别规范组合，然后在规范码点序列上做
多模式精确匹配（Aho-Corasick；多个模式可共用后缀，命中允许重叠）。

契约要点：

- 文本按 chunk 输入。未闭合的规范段（starter 之后还可能有跨 chunk 的
  记号重排/组合）**不会为了查找被提前 finish**；只有已被核心冲刷确认
  的输出码点才参与匹配，因此已交付的命中在后续输入后不会被撤回。
- 每个命中同时给出：
    - start / end：规范串上的半开区间 [start, end)；
    - source_spans：该命中覆盖的**原输入来源区间集合**。来源可能
      不是连续片段（组合/重排会把不相邻的输入码点并入同一命中），
      这里按来源点并集精确合并，绝不用最小/最大下标假装连续。
- 不同原模式若规范后相同，仍分别带回各自的 pattern_id。
- 空模式（空串，或规范后为空）被拒绝；模式 id 必须可哈希且不重复。

feed() 的查找状态推进发生在底层规范化 feed 成功之后；底层 feed
原子回滚时查找状态不变，流可继续使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable, Iterable, Iterator

from .engine import (
    DEFAULT_SEGMENT_LIMIT,
    SourceSpan,
    StreamNormalizer,
    _points_to_spans,
    normalize,
)
from .errors import InvalidInputError
from .table import CharacterTable

__all__ = ["Match", "StreamSearcher", "find_all"]


@dataclass(frozen=True)
class Match:
    """一次命中。

    - pattern_id：调用方登记模式时给的 id；
    - start / end：规范串上的半开区间 [start, end)；
    - source_spans：原输入来源区间集合（按 chunk、偏移有序，
      同 chunk 相邻点已合并；可能不是连续片段）。
    """

    pattern_id: Hashable
    start: int
    end: int
    source_spans: tuple[SourceSpan, ...]


def _prepare_patterns(
    patterns: Iterable[tuple[Hashable, str]],
    table: CharacterTable,
    segment_limit: int,
) -> list[tuple[Hashable, tuple[int, ...]]]:
    """校验模式列表并把每个模式规范组合为码点序列。"""
    if isinstance(patterns, (str, bytes)):
        raise InvalidInputError(
            "patterns must be an iterable of (pattern_id, str) pairs, "
            "not a single string"
        )
    try:
        items = list(patterns)
    except TypeError as exc:
        raise InvalidInputError(
            f"patterns must be an iterable, got {type(patterns).__name__}"
        ) from exc

    prepared: list[tuple[Hashable, tuple[int, ...]]] = []
    seen_ids: set[Hashable] = set()
    for index, item in enumerate(items):
        try:
            pattern_id, pattern = item
        except (TypeError, ValueError) as exc:
            raise InvalidInputError(
                f"pattern entry #{index} must be a (pattern_id, str) pair, "
                f"got {item!r}"
            ) from exc
        try:
            hash(pattern_id)
        except TypeError as exc:
            raise InvalidInputError(
                f"pattern id must be hashable, got "
                f"{type(pattern_id).__name__}"
            ) from exc
        if pattern_id in seen_ids:
            raise InvalidInputError(
                f"duplicate pattern id: {pattern_id!r}"
            )
        seen_ids.add(pattern_id)
        if not isinstance(pattern, str):
            raise InvalidInputError(
                f"pattern must be str, got {type(pattern).__name__}"
            )
        if pattern == "":
            raise InvalidInputError(
                f"empty pattern is not allowed (id {pattern_id!r})"
            )
        canonical = normalize(
            [pattern], table, segment_limit=segment_limit
        ).codepoints
        if not canonical:
            raise InvalidInputError(
                f"pattern normalizes to empty (id {pattern_id!r})"
            )
        prepared.append((pattern_id, canonical))
    return prepared


class _AhoCorasick:
    """码点序列上的 Aho-Corasick 自动机（支持共用后缀与重叠命中）。"""

    def __init__(self, sequences: list[tuple[int, ...]]) -> None:
        # 邻接表用 dict 表示；0 为根。
        self._next: list[dict[int, int]] = [{}]
        self._out: list[list[int]] = [[]]
        for pat_index, seq in enumerate(sequences):
            node = 0
            for cp in seq:
                nxt = self._next[node].get(cp)
                if nxt is None:
                    nxt = len(self._next)
                    self._next[node][cp] = nxt
                    self._next.append({})
                    self._out.append([])
                node = nxt
            self._out[node].append(pat_index)

        # BFS 计算失败边，并把失败节点的输出并入（覆盖后缀命中）。
        self._fail: list[int] = [0] * len(self._next)
        queue: list[int] = []
        for child in self._next[0].values():
            queue.append(child)
        while queue:
            node = queue.pop(0)
            for cp, child in self._next[node].items():
                queue.append(child)
                fallback = self._fail[node]
                while fallback and cp not in self._next[fallback]:
                    fallback = self._fail[fallback]
                self._fail[child] = self._next[fallback].get(cp, 0)
                self._out[child].extend(self._out[self._fail[child]])

    def feed(self, state: int, cp: int) -> tuple[int, list[int]]:
        """推进一个码点，返回 (新状态, 该位置结束的模式下标列表)。"""
        while state and cp not in self._next[state]:
            state = self._fail[state]
        state = self._next[state].get(cp, 0)
        return state, self._out[state]


class StreamSearcher:
    """多模式流式查找器。

    用法::

        ss = StreamSearcher(table, [("p1", "à"), ("p2", "xà")])
        matches = ss.feed(chunk1)   # 本次新确认的命中
        matches += ss.feed(chunk2)
        matches += ss.finish()      # 冲刷最后一段后的剩余命中

    finish() 之后不能再 feed；重复 finish() 返回空元组
    （所有命中均已交付，不重复、不撤回）。
    """

    def __init__(
        self,
        table: CharacterTable,
        patterns: Iterable[tuple[Hashable, str]],
        *,
        segment_limit: int = DEFAULT_SEGMENT_LIMIT,
    ) -> None:
        prepared = _prepare_patterns(patterns, table, segment_limit)
        self._normalizer = StreamNormalizer(table, segment_limit=segment_limit)
        self._pattern_ids = [pid for pid, _ in prepared]
        self._pattern_lens = [len(seq) for _, seq in prepared]
        self._automaton = _AhoCorasick([seq for _, seq in prepared])
        self._state = 0
        self._consumed = 0
        self._finished = False

    def feed(self, chunk: str) -> tuple[Match, ...]:
        """喂入一个 chunk，返回本次新确认的命中（可能为空）。"""
        if self._finished:
            raise InvalidInputError("stream already finished")
        # 底层 feed 是原子的：失败时这里不会推进任何查找状态。
        self._normalizer.feed(chunk)
        return self._drain()

    def finish(self) -> tuple[Match, ...]:
        """冲刷最后的未闭合段，返回剩余命中。重复调用返回空元组。"""
        if self._finished:
            return ()
        self._normalizer.finish()
        self._finished = True
        return self._drain()

    def _drain(self) -> tuple[Match, ...]:
        """消费核心已确认（冲刷）的输出码点，返回新命中。

        只读取底层流已冲刷的输出；未闭合段留在核心内部，绝不提前
        finish。已冲刷输出只增不改，因此命中一经交付永不撤回。
        """
        out = self._normalizer._out  # 包内复用：已确认输出条目（只增不改）
        found: list[tuple[int, int, int]] = []  # (start, end, pattern_index)
        while self._consumed < len(out):
            cp = out[self._consumed][0]
            position = self._consumed + 1  # 当前码点的开区间端点
            self._state, hits = self._automaton.feed(self._state, cp)
            for pat_index in hits:
                start = position - self._pattern_lens[pat_index]
                found.append((start, position, pat_index))
            self._consumed += 1
        found.sort(key=lambda item: (item[1], item[0], item[2]))
        return tuple(
            Match(
                pattern_id=self._pattern_ids[pat_index],
                start=start,
                end=end,
                source_spans=_points_to_spans(
                    point
                    for _, origins in out[start:end]
                    for point in origins
                ),
            )
            for start, end, pat_index in found
        )


def find_all(
    chunks: Iterable[str],
    patterns: Iterable[tuple[Hashable, str]],
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> tuple[Match, ...]:
    """一次性多模式查找：等价于按序 feed 后 finish。

    chunks 是 str 的可迭代对象（不得直接传单个 str）。任何一步
    抛异常都不会返回部分结果。
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

    searcher = StreamSearcher(table, patterns, segment_limit=segment_limit)
    matches: list[Match] = []
    for chunk in iterator:
        matches.extend(searcher.feed(chunk))
    matches.extend(searcher.finish())
    return tuple(matches)
