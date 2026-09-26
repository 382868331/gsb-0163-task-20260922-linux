"""canoncompose：显式字符表的流式规范组合。

公开接口::

    normalize(chunks, table, *, segment_limit=1024) -> NormalizationResult
    StreamNormalizer(table, *, segment_limit=1024)  # feed/finish 流式 API
    DictCharacterTable(...)                          # 字典字符表
    CharacterTable                                   # 字符表协议基类
    NormalizationResult / SourcePoint / SourceSpan   # 结果数据结构
    StreamSearcher / find_all / Match                # 多模式流式查找
    CanonicalError / InvalidInputError / InvalidTableError /
        DecompositionCycleError / SegmentLimitError  # 错误类型

本库不读取、不引用任何系统 Unicode 数据库；规范化所需的分解、ccc 与
二元组合映射全部由调用方显式提供。
"""

from __future__ import annotations

from .engine import (
    DEFAULT_SEGMENT_LIMIT,
    NormalizationResult,
    SourcePoint,
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
from .search import Match, StreamSearcher, find_all
from .table import CharacterTable, DictCharacterTable
from .validators import validate_codepoint

__all__ = [
    "DEFAULT_SEGMENT_LIMIT",
    "NormalizationResult",
    "SourcePoint",
    "SourceSpan",
    "StreamNormalizer",
    "normalize",
    "Match",
    "StreamSearcher",
    "find_all",
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
