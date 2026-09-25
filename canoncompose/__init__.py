"""canoncompose：显式字符表的流式规范组合。

公开接口::

    normalize(chunks, table, *, segment_limit=1024) -> NormalizationResult
    StreamNormalizer(table, *, segment_limit=1024)  # feed/finish 流式 API
        .export_checkpoint()                        # 导出 JSON 兼容检查点
        .restore_checkpoint(checkpoint, table)      # 从检查点恢复（类方法）
    DictCharacterTable(...)                          # 字典字符表
        .fingerprint() / .canonical_rules()          # 内容指纹/规范排序规则
    CharacterTable                                   # 字符表协议基类
    NormalizationResult / SourcePoint / SourceSpan   # 结果数据结构
    CanonicalError / InvalidInputError / InvalidTableError /
        DecompositionCycleError / SegmentLimitError /
        CheckpointError                              # 错误类型

本库不读取、不引用任何系统 Unicode 数据库；规范化所需的分解、ccc 与
二元组合映射全部由调用方显式提供。
"""

from __future__ import annotations

from .engine import (
    CHECKPOINT_FORMAT,
    CHECKPOINT_VERSION,
    DEFAULT_SEGMENT_LIMIT,
    NormalizationResult,
    SourcePoint,
    SourceSpan,
    StreamNormalizer,
    normalize,
)
from .errors import (
    CanonicalError,
    CheckpointError,
    DecompositionCycleError,
    InvalidInputError,
    InvalidTableError,
    SegmentLimitError,
)
from .table import CharacterTable, DictCharacterTable
from .validators import validate_codepoint

__all__ = [
    "DEFAULT_SEGMENT_LIMIT",
    "CHECKPOINT_FORMAT",
    "CHECKPOINT_VERSION",
    "NormalizationResult",
    "SourcePoint",
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
    "CheckpointError",
]

__version__ = "1.1.0"
