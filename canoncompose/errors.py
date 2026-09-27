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
    "TableCompilationError",
    "CompositionConflictError",
    "InconsistentLevelError",
    "SegmentLimitError",
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


class TableCompilationError(InvalidTableError):
    """字符表编译（事前一致性检查）失败的公共基类。

    所有编译期错误都指向具体表项，便于调用方定位修正。
    """


class CompositionConflictError(TableCompilationError):
    """组合映射与分解映射互相矛盾。

    组合项 (left, right) -> result 要求 result 的递归分解叶子序列与
    left、right 各自递归分解叶子的拼接完全一致，否则规范化结果依赖
    处理路径，表自身矛盾。

    - left / right / result：矛盾组合项的三个码点；
    - expected：left、right 递归分解叶子的拼接；
    - actual：result 的递归分解叶子。
    """

    def __init__(
        self,
        left: int,
        right: int,
        result: int,
        expected: tuple[int, ...],
        actual: tuple[int, ...],
    ) -> None:
        exp = " ".join(_hex_cp(cp) for cp in expected)
        act = " ".join(_hex_cp(cp) for cp in actual)
        super().__init__(
            "composition contradicts decomposition: "
            f"composition({_hex_cp(left)}, {_hex_cp(right)}) = "
            f"{_hex_cp(result)}, but the pair expands to ({exp}) "
            f"while the result expands to ({act})",
            codepoint=result,
        )
        self.left = left
        self.right = right
        self.result = result
        self.expected = expected
        self.actual = actual


class InconsistentLevelError(TableCompilationError):
    """表项的 canonical combining class（组合等级）不一致。

    entry 字段指明出问题的表项种类与定位：

    - "decomposition"：codepoint 的分解首元素等级与该字符自身等级
      不一致（规范分解保持首元素等级）；
    - "composition-second"：组合项 (left, right) 的右元素等级为 0
      （starter 不可能作为被组合的记号）；
    - "composition-result"：组合结果 codepoint 的等级非 0
      （组合结果占据基字符位置，必须是 starter）。

    携带字段：entry、codepoint、left/right（组合项时）、
    expected_ccc、actual_ccc。
    """

    def __init__(
        self,
        message: str,
        *,
        entry: str,
        codepoint: int,
        expected_ccc: int,
        actual_ccc: int,
        left: int | None = None,
        right: int | None = None,
    ) -> None:
        super().__init__(
            f"{message} (entry {entry}, expected ccc {expected_ccc}, "
            f"got {actual_ccc})",
            codepoint=codepoint,
        )
        self.entry = entry
        self.left = left
        self.right = right
        self.expected_ccc = expected_ccc
        self.actual_ccc = actual_ccc


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
