"""异常类型与错误语义。

所有本库抛出的异常都继承 :class:`CanonicalError`，便于调用方统一捕获。
错误信息尽量携带可定位信息（chunk 下标、chunk 内码点偏移、流上全局偏移、
相关码点），不静默纠正非法输入。
"""

from __future__ import annotations

__all__ = [
    "CanonicalError",
    "InvalidInputError",
    "InvalidTableError",
    "DecompositionCycleError",
    "SegmentLimitError",
    "CheckpointError",
]


def _hex_cp(codepoint: int) -> str:
    return f"U+{codepoint:04X}"


def _location_prefix(
    *,
    chunk_index: int | None,
    offset: int | None,
    stream_offset: int | None,
) -> str:
    parts = []
    if chunk_index is not None:
        parts.append(f"chunk {chunk_index}")
    if offset is not None:
        parts.append(f"offset {offset}")
    prefix = " ".join(parts)
    if stream_offset is not None:
        prefix = f"{prefix} (stream offset {stream_offset})" if prefix else (
            f"stream offset {stream_offset}"
        )
    return f"{prefix}: " if prefix else ""


class CanonicalError(Exception):
    """本库所有异常的公共基类。"""


class InvalidInputError(CanonicalError, ValueError):
    """非法输入。

    携带字段（可能为 ``None``）：

    - ``chunk_index``：出错 chunk 的下标；
    - ``offset``：出错字符在该 chunk 内的码点偏移；
    - ``stream_offset``：出错字符在拼接输入流上的全局码点偏移；
    - ``codepoint``：相关码点（整数）。
    """

    def __init__(
        self,
        message: str,
        *,
        chunk_index: int | None = None,
        offset: int | None = None,
        stream_offset: int | None = None,
        codepoint: int | None = None,
    ) -> None:
        prefix = _location_prefix(
            chunk_index=chunk_index,
            offset=offset,
            stream_offset=stream_offset,
        )
        cp_part = f" [{_hex_cp(codepoint)}]" if codepoint is not None else ""
        super().__init__(f"{prefix}{message}{cp_part}")
        self.chunk_index = chunk_index
        self.offset = offset
        self.stream_offset = stream_offset
        self.codepoint = codepoint


class InvalidTableError(CanonicalError, ValueError):
    """字符表内容非法（返回值类型/范围不合法等）。

    - ``codepoint``：触发表查询的码点（如有）。
    """

    def __init__(self, message: str, *, codepoint: int | None = None) -> None:
        cp_part = f" [{_hex_cp(codepoint)}]" if codepoint is not None else ""
        super().__init__(f"{message}{cp_part}")
        self.codepoint = codepoint


class DecompositionCycleError(InvalidTableError):
    """递归分解映射中存在环。

    ``chain`` 给出首次闭合的访问路径（相邻码点具有分解边，
    最后一个码点等于路径中先前出现的码点），例如 ``(0x41, 0x42, 0x41)``。
    """

    def __init__(self, chain: tuple[int, ...]) -> None:
        rendered = " -> ".join(_hex_cp(cp) for cp in chain)
        super().__init__(f"decomposition cycle detected: {rendered}")
        self.chain = chain


class SegmentLimitError(CanonicalError):
    """单个规范段展开后的码点数超过上限。

    - ``limit``：上限（默认 1024）；
    - ``actual``：尝试追加后的段长度（``limit + 1``）；
    - 定位字段同 :class:`InvalidInputError`。
    """

    def __init__(
        self,
        message: str,
        *,
        limit: int,
        actual: int,
        chunk_index: int | None = None,
        offset: int | None = None,
        stream_offset: int | None = None,
        codepoint: int | None = None,
    ) -> None:
        prefix = _location_prefix(
            chunk_index=chunk_index,
            offset=offset,
            stream_offset=stream_offset,
        )
        cp_part = f" [{_hex_cp(codepoint)}]" if codepoint is not None else ""
        super().__init__(
            f"{prefix}{message} (segment limit {limit}, would be {actual}){cp_part}"
        )
        self.limit = limit
        self.actual = actual
        self.chunk_index = chunk_index
        self.offset = offset
        self.stream_offset = stream_offset
        self.codepoint = codepoint


class CheckpointError(CanonicalError, ValueError):
    """检查点导出/恢复失败。

    覆盖：当前字符表不支持检查点（非 :class:`DictCharacterTable`）、
    检查点格式/版本不认识、字段或来源坐标不合法、以及恢复时调用方
    传入的字符表指纹与导出时不匹配。恢复在校验全部通过前不构造
    任何对象，因此失败不会留下半恢复对象。
    """
