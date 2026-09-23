"""按字符位置插入/删除的编辑接口。

在**原始输入**（chunks 拼接后的文本）上按码点位置应用一次插入或删除，
然后用同一字符表对编辑后的输入**全量重新规范化**，并求出新旧规范输出
之间的变化区间与下标映射。

位置约定（公开契约）：

- ``offset`` / ``start`` / ``end`` 都是原始输入 chunks 拼接串上的码点
  偏移（半开区间 ``[start, end)``），与 chunk 划分无关；
- 插入把 ``text`` 放到 ``offset`` 处（``0 <= offset <= 总长度``）；
- 删除移除 ``[start, end)``（``0 <= start <= end <= 总长度``）；
- 编辑应用于**原始输入**而非规范串，因此组合/重排会按全量重扫自然生效
  （例如在 starter 后插入组合记号会触发组合，删除阻塞记号会解除阻塞）。

变化区间与映射通过对新旧规范码点序列做**公共前缀 + 公共后缀裁剪**得到：
前缀、后缀部分一一对应（映射为恒等），中间部分互不映射（``None``）。
单次局部编辑下这就是最小变化区间，且完全确定（不依赖模糊匹配）。

编辑后输入的 chunk 划分：保留原 chunk 边界，在编辑点处切开，插入文本
自成一个 chunk；因此 ``result.source_spans`` 的 ``chunk_index`` 相对
**编辑后**的 chunk 序列计数，``previous.source_spans`` 相对原 chunks。
新旧对应关系请使用 :class:`EditResult` 的下标映射，不要直接比较两个
结果的 ``chunk_index``。

本模块的函数都是无状态的：任何一步失败都抛异常且不产生部分结果
（底层 :func:`~canoncompose.engine.normalize` 本身不返回部分结果）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .engine import DEFAULT_SEGMENT_LIMIT, NormalizationResult, normalize
from .errors import InvalidInputError
from .table import CharacterTable
from .validators import validate_chunk

__all__ = [
    "EditMapping",
    "EditResult",
    "normalize_insert",
    "normalize_delete",
]


@dataclass(frozen=True)
class EditMapping:
    """新旧规范输出码点下标映射。

    - ``new_to_old[i]``：新规范输出第 ``i`` 个码点对应的旧规范输出下标；
      无对应（编辑引入或重排/组合导致）为 ``None``；
    - ``old_to_new[j]``：旧规范输出第 ``j`` 个码点对应的新规范输出下标；
      无对应（被删除或被组合吸收）为 ``None``。

    两者互为部分逆映射：``new_to_old[i] == j`` 当且仅当
    ``old_to_new[j] == i``。
    """

    new_to_old: tuple[int | None, ...]
    old_to_new: tuple[int | None, ...]


@dataclass(frozen=True)
class EditResult:
    """一次插入/删除编辑的结果。

    - ``result``：编辑后输入的全量规范化结果；
    - ``previous``：编辑前输入的规范化结果（同一字符表、同一上限）；
    - ``old_span`` / ``new_span``：变化区间，分别是旧/新规范输出码点
      序列上的半开区间 ``(start, end)``；空区间（``start == end``）表示
      该侧没有变化的码点；
    - ``mapping``：新旧规范输出的码点下标映射。
    """

    result: NormalizationResult
    previous: NormalizationResult
    old_span: tuple[int, int]
    new_span: tuple[int, int]
    mapping: EditMapping

    @property
    def changed(self) -> bool:
        """编辑是否改变了规范输出（变化区间有一侧非空）。"""
        return self.old_span[0] != self.old_span[1] or (
            self.new_span[0] != self.new_span[1]
        )


# ---- 输入校验 -----------------------------------------------------------


def _materialize_chunks(chunks: Iterable[str]) -> list[str]:
    """与 normalize 相同的 chunks 契约：可迭代的 str，拒绝单个 str。"""
    if isinstance(chunks, str):
        raise InvalidInputError(
            "chunks must be an iterable of str, not a single str"
        )
    try:
        iterator = iter(chunks)
    except TypeError as exc:
        raise InvalidInputError(
            f"chunks must be an iterable of str, got "
            f"{type(chunks).__name__}"
        ) from exc
    return [validate_chunk(chunk, i) for i, chunk in enumerate(iterator)]


def _validate_position(value: object, name: str, total: int) -> int:
    """校验位置参数：int（拒绝 bool/NaN/Infinity），范围 0..total。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            if math.isnan(value):
                raise InvalidInputError(f"{name} is NaN")
            if math.isinf(value):
                raise InvalidInputError(f"{name} is Infinity")
        raise InvalidInputError(
            f"{name} must be int, got {type(value).__name__}"
        )
    if value < 0 or value > total:
        raise InvalidInputError(
            f"{name} out of range 0..{total}, got {value}"
        )
    return value


# ---- 编辑后 chunk 序列构造 ------------------------------------------------


def _splice_insert(chunks: list[str], offset: int, text: str) -> list[str]:
    """在拼接串 offset 处插入 text；插入文本自成一个 chunk。"""
    if not text:
        return list(chunks)
    new_chunks: list[str] = []
    pos = 0
    inserted = False
    for chunk in chunks:
        # 首个满足 pos <= offset <= pos + len(chunk) 的 chunk 承载插入点；
        # 边界归属前一 chunk 末尾，确定性规则。
        if not inserted and pos <= offset <= pos + len(chunk):
            cut = offset - pos
            # 切开的空半片不保留，避免引入无内容的 chunk 工件。
            if chunk[:cut]:
                new_chunks.append(chunk[:cut])
            new_chunks.append(text)
            if chunk[cut:]:
                new_chunks.append(chunk[cut:])
            inserted = True
        else:
            new_chunks.append(chunk)
        pos += len(chunk)
    if not inserted:
        # 仅当 chunks 为空（offset 必为 0）时到达。
        new_chunks.append(text)
    return new_chunks


def _splice_delete(chunks: list[str], start: int, end: int) -> list[str]:
    """删除拼接串 [start, end)；被截断的 chunk 保留剩余部分。"""
    if start == end:
        return list(chunks)
    new_chunks: list[str] = []
    pos = 0
    for chunk in chunks:
        c0, c1 = pos, pos + len(chunk)
        pos = c1
        if c1 <= start or c0 >= end:
            new_chunks.append(chunk)
            continue
        left = chunk[: max(0, start - c0)]
        right = chunk[min(len(chunk), end - c0):]
        if left:
            new_chunks.append(left)
        if right:
            new_chunks.append(right)
    return new_chunks


# ---- 变化区间与映射 -------------------------------------------------------


def _diff_outputs(
    old_cps: tuple[int, ...], new_cps: tuple[int, ...]
) -> tuple[tuple[int, int], tuple[int, int], EditMapping]:
    """公共前后缀裁剪，返回 (old_span, new_span, mapping)。"""
    prefix = 0
    limit = min(len(old_cps), len(new_cps))
    while prefix < limit and old_cps[prefix] == new_cps[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < len(old_cps) - prefix
        and suffix < len(new_cps) - prefix
        and old_cps[len(old_cps) - 1 - suffix]
        == new_cps[len(new_cps) - 1 - suffix]
    ):
        suffix += 1

    old_span = (prefix, len(old_cps) - suffix)
    new_span = (prefix, len(new_cps) - suffix)

    old_to_new: list[int | None] = []
    for j in range(len(old_cps)):
        if j < prefix:
            old_to_new.append(j)
        elif j >= len(old_cps) - suffix:
            old_to_new.append(j - len(old_cps) + len(new_cps))
        else:
            old_to_new.append(None)
    new_to_old: list[int | None] = []
    for i in range(len(new_cps)):
        if i < prefix:
            new_to_old.append(i)
        elif i >= len(new_cps) - suffix:
            new_to_old.append(i - len(new_cps) + len(old_cps))
        else:
            new_to_old.append(None)

    return old_span, new_span, EditMapping(
        new_to_old=tuple(new_to_old), old_to_new=tuple(old_to_new)
    )


def _apply_edit(
    chunk_list: list[str],
    table: CharacterTable,
    segment_limit: int,
    new_chunks: list[str],
) -> EditResult:
    previous = normalize(chunk_list, table, segment_limit=segment_limit)
    result = normalize(new_chunks, table, segment_limit=segment_limit)
    old_span, new_span, mapping = _diff_outputs(
        previous.codepoints, result.codepoints
    )
    return EditResult(
        result=result,
        previous=previous,
        old_span=old_span,
        new_span=new_span,
        mapping=mapping,
    )


# ---- 公开 API -------------------------------------------------------------


def normalize_insert(
    chunks: Iterable[str],
    offset: int,
    text: str,
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> EditResult:
    """在原始输入 ``offset`` 处插入 ``text`` 并全量重新规范化。

    - ``chunks``：原始输入，契约同 :func:`~canoncompose.normalize`；
    - ``offset``：拼接串上的码点偏移，``0 <= offset <= 总长度``；
    - ``text``：要插入的字符串（``""`` 为合法空操作）。

    非法位置/类型抛 :class:`~canoncompose.errors.InvalidInputError`；
    字符表与上限问题沿用核心错误类型。
    """
    if not isinstance(text, str):
        raise InvalidInputError(
            f"text must be str, got {type(text).__name__}"
        )
    chunk_list = _materialize_chunks(chunks)
    total = sum(len(c) for c in chunk_list)
    pos = _validate_position(offset, "offset", total)
    return _apply_edit(
        chunk_list, table, segment_limit, _splice_insert(chunk_list, pos, text)
    )


def normalize_delete(
    chunks: Iterable[str],
    start: int,
    end: int,
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> EditResult:
    """删除原始输入 ``[start, end)`` 并全量重新规范化。

    - ``start`` / ``end``：拼接串上的码点偏移，
      ``0 <= start <= end <= 总长度``；``start == end`` 为合法空操作。

    非法位置/类型抛 :class:`~canoncompose.errors.InvalidInputError`。
    """
    chunk_list = _materialize_chunks(chunks)
    total = sum(len(c) for c in chunk_list)
    lo = _validate_position(start, "start", total)
    hi = _validate_position(end, "end", total)
    if lo > hi:
        raise InvalidInputError(
            f"start must not exceed end, got start={lo}, end={hi}"
        )
    return _apply_edit(
        chunk_list, table, segment_limit, _splice_delete(chunk_list, lo, hi)
    )
