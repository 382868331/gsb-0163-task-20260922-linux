"""多模式流式查找：在规范组合输出上查找多个模式。

模式与文本使用**同一张字符表**分别规范组合，然后在规范码点流上做朴素
匹配。命中同时报告：

- 规范串上的半开区间 ``[start, end)``；
- 原输入中的来源区间集合（精确的来源点并集合并，**不是**最小/最大
  下标围成的近似区间——组合与 ccc 重排可能使命中来源不连续）；
- 模式各自的 ``pattern_id``：不同写法的原模式规范后即使相同，也各自
  独立上报。

流式语义：

- 文本按 chunk 喂入；**未闭合的规范段不参与匹配**（段内还可能发生
  跨 chunk 的 ccc 重排与组合），查找器绝不为了出结果而提前 finish
  尚未闭合的段。因此跨段命中会延迟到段闭合后才上报。
- 已交付（feed/finish 返回值中）的命中只增不减，后续输入不会撤回。
- 支持重叠命中；空模式（规范后为空码点序列）被禁止。

本模块只做有限字符表上的精确匹配，不扩展成完整 Unicode 搜索引擎。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping

from .engine import (
    DEFAULT_SEGMENT_LIMIT,
    SourceSpan,
    StreamNormalizer,
    merge_source_spans,
    normalize,
)
from .errors import InvalidInputError
from .table import CharacterTable

__all__ = ["SearchHit", "StreamSearcher", "find_all"]


@dataclass(frozen=True)
class SearchHit:
    """一次命中。

    - ``pattern_id``：注册模式时调用方给出的 ID；
    - ``start`` / ``end``：命中在**规范串**上的半开区间 ``[start, end)``；
    - ``source_spans``：命中覆盖的**原输入**来源区间集合（与
      ``NormalizationResult.source_spans`` 同一合并规则）。
    """

    pattern_id: Any
    start: int
    end: int
    source_spans: tuple[SourceSpan, ...]


def _parse_patterns(
    patterns: object,
    table: CharacterTable,
    segment_limit: int,
) -> list[tuple[Any, tuple[int, ...]]]:
    """校验并规范化模式列表，返回 [(pattern_id, 规范码点元组)]。"""
    if isinstance(patterns, Mapping):
        items = list(patterns.items())
    else:
        try:
            items = list(patterns)  # type: ignore[arg-type]
        except TypeError as exc:
            raise InvalidInputError(
                "patterns must be a mapping or an iterable of "
                f"(pattern_id, text) pairs, got {type(patterns).__name__}"
            ) from exc

    parsed: list[tuple[Any, tuple[int, ...]]] = []
    for index, item in enumerate(items):
        if isinstance(item, str):
            raise InvalidInputError(
                f"pattern entry #{index} must be a (pattern_id, text) pair, "
                "got a bare str"
            )
        try:
            pattern_id, pattern_text = item
        except (TypeError, ValueError) as exc:
            raise InvalidInputError(
                f"pattern entry #{index} must be a (pattern_id, text) pair, "
                f"got {item!r}"
            ) from exc
        if not isinstance(pattern_text, str):
            raise InvalidInputError(
                f"pattern #{index} text must be str, got "
                f"{type(pattern_text).__name__}"
            )
        # 模式与文本走同一规范组合入口（同一张表、同一段上限）。
        canonical = normalize(
            [pattern_text], table, segment_limit=segment_limit
        ).codepoints
        if not canonical:
            raise InvalidInputError(
                f"pattern #{index} ({pattern_id!r}) is empty after "
                "normalization; empty patterns are not allowed"
            )
        parsed.append((pattern_id, canonical))
    return parsed


class StreamSearcher:
    """在流式规范组合输出上做多模式查找。

    用法::

        searcher = StreamSearcher(table, [("p1", "a"), ("p2", ...)])
        hits = searcher.feed(chunk)      # 返回本次新确认的命中
        ...
        hits += searcher.finish()        # 冲刷末段后返回剩余命中

    ``patterns`` 为 ``{pattern_id: 模式串}`` 映射或 ``(pattern_id, 模式串)``
    对的可迭代对象。模式在构造时即用同一张表规范化；空模式抛
    :class:`~canoncompose.errors.InvalidInputError`。
    """

    def __init__(
        self,
        table: CharacterTable,
        patterns: object,
        *,
        segment_limit: int = DEFAULT_SEGMENT_LIMIT,
    ) -> None:
        self._patterns = _parse_patterns(patterns, table, segment_limit)
        self._normalizer = StreamNormalizer(table, segment_limit=segment_limit)
        self._max_len = (
            max(len(canonical) for _, canonical in self._patterns)
            if self._patterns
            else 0
        )
        # 已闭合输出的本地累积快照（码点, 来源点元组）。
        self._flushed: list = []
        # 起始下标 < _resolved 的位置已全部模式判定完毕，不会重复上报。
        self._resolved = 0
        self._finished = False

    def feed(self, chunk: str) -> tuple[SearchHit, ...]:
        """喂入一个 chunk，返回本次新确认的命中（按起始位置、模式注册序）。

        底层 feed 是原子的：失败时查找器状态不变，可继续使用。
        """
        if self._finished:
            raise InvalidInputError("searcher already finished")
        self._normalizer.feed(chunk)
        return self._scan(final=False)

    def finish(self) -> tuple[SearchHit, ...]:
        """冲刷末段并返回剩余命中。可重复调用，之后返回空元组。"""
        if self._finished:
            return ()
        self._normalizer.finish()
        self._finished = True
        return self._scan(final=True)

    def _scan(self, *, final: bool) -> tuple[SearchHit, ...]:
        flushed = self._normalizer.flushed_entries()
        # 底层已冲刷输出只增不改，本地快照直接对齐到最新长度。
        if len(flushed) < len(self._flushed):
            raise RuntimeError(  # pragma: no cover - 防御性不变量
                "flushed output shrank unexpectedly"
            )
        self._flushed = list(flushed)
        n = len(self._flushed)
        codepoints = [cp for cp, _ in self._flushed]

        hits: list[SearchHit] = []
        # 起始位置 i 只有当 i + 最长模式 <= n 时才能完全判定；
        # final 时全部已闭合输出即为最终输出，判定所有剩余起始位置。
        limit = n if final else n - self._max_len + 1
        i = self._resolved
        while i < limit:
            for pattern_id, canonical in self._patterns:
                length = len(canonical)
                if (
                    i + length <= n
                    and tuple(codepoints[i:i + length]) == canonical
                ):
                    points = [
                        point
                        for _, origins in self._flushed[i:i + length]
                        for point in origins
                    ]
                    hits.append(
                        SearchHit(
                            pattern_id,
                            i,
                            i + length,
                            merge_source_spans(points),
                        )
                    )
            i += 1
        self._resolved = max(self._resolved, i)
        return tuple(hits)


def find_all(
    chunks: Iterable[str],
    table: CharacterTable,
    patterns: object,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> tuple[SearchHit, ...]:
    """一次性多模式查找：等价于逐 chunk feed 后 finish 的全部命中。

    ``chunks`` 的校验规则与 :func:`canoncompose.normalize` 相同
    （str 的可迭代对象，不得直接传单个 str）。
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
    hits: list[SearchHit] = []
    for chunk in iterator:
        hits.extend(searcher.feed(chunk))
    hits.extend(searcher.finish())
    return tuple(hits)
