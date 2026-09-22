"""Error types for streamnorm.

Every error raised by this package derives from :class:`NormalizationError`,
so callers can catch one base class.  Concrete subclasses also derive from
the closest built-in exception (``ValueError`` / ``TypeError`` /
``RuntimeError``) so existing ``except ValueError`` style code keeps working.

Error semantics (fixed contract):

* :class:`TableError` — the explicit character table is malformed.  Raised
  eagerly by ``CharacterTable(...)``; nothing is normalized afterwards.
* :class:`CyclicDecompositionError` — the decomposition graph has a cycle.
  Carries the offending cycle in ``.cycle``.
* :class:`InputError` — an argument violates the input contract (wrong type,
  ``bool`` where an int is required, NaN/Infinity, a non-string chunk, ...).
  Raised *before* any state is mutated, so the normalizer stays usable.
* :class:`SegmentTooLongError` — one canonical segment grew past the
  configured code-point limit.  Raised mid-stream; the instance is then
  poisoned and must be discarded (documented partial state).
* :class:`NormalizerStateError` — ``push``/``finish`` called after
  ``finish()`` or after a poisoning failure.
"""


class NormalizationError(Exception):
    """Base class for every error raised by streamnorm."""


class TableError(NormalizationError, ValueError):
    """The explicit character table is malformed."""


class CyclicDecompositionError(TableError):
    """The decomposition graph contains a cycle.

    ``cycle`` is a tuple of code points ``(c0, c1, ..., c0)`` showing the
    loop that was detected.
    """

    def __init__(self, message, cycle=()):
        super().__init__(message)
        self.cycle = tuple(cycle)


class InputError(NormalizationError, TypeError):
    """An argument violates the input contract.

    The message always names the offending parameter and the received value
    (or its type), so the call site is locatable.
    """


class SegmentTooLongError(NormalizationError, ValueError):
    """A single canonical segment exceeded the configured code-point limit.

    Attributes: ``limit`` (the configured maximum) and ``segment_start``
    (index into the concatenated input where the offending segment began).
    """

    def __init__(self, message, *, limit, segment_start):
        super().__init__(message)
        self.limit = limit
        self.segment_start = segment_start


class NormalizerStateError(NormalizationError, RuntimeError):
    """The streaming normalizer was used after finish() or after a failure."""
