"""canoncompose：显式字符表的流式规范组合。

公开接口::

    normalize(chunks, table, *, segment_limit=1024) -> NormalizationResult
    StreamNormalizer(table, *, segment_limit=1024)  # feed/finish 流式 API
        # 另提供 query_output_ranges / pending_sources 输入选区反查
    DictCharacterTable(...)                          # 字典字符表
    CharacterTable                                   # 字符表协议基类
    NormalizationResult / SourcePoint / SourceSpan   # 结果数据结构
    OutputRange / SourceQueryResult                  # 反查结果数据结构
    CanonicalError / InvalidInputError / InvalidTableError /
        DecompositionCycleError / SegmentLimitError  # 错误类型

本库不读取、不引用任何系统 Unicode 数据库；规范化所需的分解、ccc 与
二元组合映射全部由调用方显式提供。
"""

from __future__ import annotations

from .engine import (
    DEFAULT_SEGMENT_LIMIT,
    NormalizationResult,
    OutputRange,
    SourcePoint,
    SourceQueryResult,
    SourceSpan,
    StreamNormalizer,
    normalize,
)
from .errors import (
    CanonicalError,
    DecompositionCycleError,
    InvalidInputError,
    InvalidTableError,
    SegmentLimitError,
)
from .table import CharacterTable, DictCharacterTable
from .validators import validate_codepoint

__all__ = [
    "DEFAULT_SEGMENT_LIMIT",
    "NormalizationResult",
    "OutputRange",
    "SourcePoint",
    "SourceQueryResult",
    "SourceSpan",
    "StreamNormalizer",
    "normalize",
    "CharacterTable",
    "DictCharacterTable",
    "validate_codepoint",
    "CanonicalError",
    "InvalidInputError",
    "InvalidTableError",
    "DecompositionCycleError",
    "SegmentLimitError",
]

__version__ = "1.0.0"
