"""streamnorm — streaming canonical normalization over an explicit character table.

Public API:

* :class:`CharacterTable` — explicit, validated, immutable character table
  (recursive decompositions, canonical combining classes, binary
  compositions).  No system Unicode data is consulted anywhere.
* :func:`normalize` — one-shot normalization of an iterable of chunks.
* :class:`StreamNormalizer` — incremental ``push``/``finish`` interface.
* :class:`NormalizationResult`, :class:`SourceInterval` — result types.
* Error types rooted at :class:`NormalizationError`.
"""

from .errors import (
    CyclicDecompositionError,
    InputError,
    NormalizationError,
    NormalizerStateError,
    SegmentTooLongError,
    TableError,
)
from .normalizer import (
    DEFAULT_MAX_SEGMENT_LENGTH,
    NormalizationResult,
    SourceInterval,
    StreamNormalizer,
    normalize,
)
from .table import (
    MAX_CCC,
    MAX_EXPANSION,
    MAX_CODE_POINT,
    MIN_CODE_POINT,
    CharacterTable,
)

__version__ = "1.0.0"

__all__ = [
    "CharacterTable",
    "StreamNormalizer",
    "normalize",
    "NormalizationResult",
    "SourceInterval",
    "DEFAULT_MAX_SEGMENT_LENGTH",
    "MAX_CCC",
    "MAX_EXPANSION",
    "MAX_CODE_POINT",
    "MIN_CODE_POINT",
    "NormalizationError",
    "TableError",
    "CyclicDecompositionError",
    "InputError",
    "SegmentTooLongError",
    "NormalizerStateError",
    "__version__",
]
