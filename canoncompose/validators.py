"""输入校验。

约定（公开契约，见 README）：

- 码点必须是 ``int``，范围 ``0..0x10FFFF``；
- 拒绝 ``bool``（``True``/``False`` 是 ``int`` 的子类，按本契约必须拒绝）；
- 数值输入若给了 ``float``，拒绝 ``NaN``/``Infinity``，并统一拒绝非整数；
  本库不产生浮点结果，浮点只可能出现在用户提供的码点参数中；
- chunk 必须是 ``str``，码点偏移按 Python 码点计；
- 校验失败抛 :class:`~canoncompose.errors.InvalidInputError`，携带定位信息。
"""

from __future__ import annotations

import math

from .errors import InvalidInputError

MAX_CODEPOINT = 0x10FFFF


def validate_codepoint(
    value: object,
    *,
    chunk_index: int | None = None,
    offset: int | None = None,
    stream_offset: int | None = None,
) -> int:
    """校验单个码点输入，返回规范化的 int。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            # 明确区分 NaN/Infinity，满足"数值输入拒绝 NaN/Infinity"。
            if math.isnan(value):
                raise InvalidInputError(
                    "codepoint is NaN",
                    chunk_index=chunk_index,
                    offset=offset,
                    stream_offset=stream_offset,
                )
            if math.isinf(value):
                raise InvalidInputError(
                    "codepoint is Infinity",
                    chunk_index=chunk_index,
                    offset=offset,
                    stream_offset=stream_offset,
                )
        raise InvalidInputError(
            f"codepoint must be int, got {type(value).__name__}",
            chunk_index=chunk_index,
            offset=offset,
            stream_offset=stream_offset,
        )
    cp = value
    if cp < 0 or cp > MAX_CODEPOINT:
        raise InvalidInputError(
            f"codepoint out of range 0..0x{MAX_CODEPOINT:X}",
            chunk_index=chunk_index,
            offset=offset,
            stream_offset=stream_offset,
            codepoint=cp,
        )
    return cp


def validate_non_negative_int(
    value: object,
    name: str,
    *,
    chunk_index: int | None = None,
) -> int:
    """校验查询边界类参数为非负 int（拒绝 bool 与 NaN/Infinity）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            if math.isnan(value):
                raise InvalidInputError(f"{name} is NaN", chunk_index=chunk_index)
            if math.isinf(value):
                raise InvalidInputError(
                    f"{name} is Infinity", chunk_index=chunk_index
                )
        raise InvalidInputError(
            f"{name} must be int, got {type(value).__name__}",
            chunk_index=chunk_index,
        )
    if value < 0:
        raise InvalidInputError(
            f"{name} must be non-negative, got {value}",
            chunk_index=chunk_index,
        )
    return value


def validate_chunk(chunk: object, chunk_index: int) -> str:
    """校验一个输入 chunk 必须是 str。"""
    if not isinstance(chunk, str):
        raise InvalidInputError(
            f"chunk must be str, got {type(chunk).__name__}",
            chunk_index=chunk_index,
        )
    return chunk


def validate_positive_int(value: object, name: str) -> int:
    """校验正整数参数（如段上限），拒绝 bool。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            if math.isnan(value):
                raise InvalidInputError(f"{name} is NaN")
            if math.isinf(value):
                raise InvalidInputError(f"{name} is Infinity")
        raise InvalidInputError(
            f"{name} must be int, got {type(value).__name__}"
        )
    if value <= 0:
        raise InvalidInputError(f"{name} must be positive, got {value}")
    return value
