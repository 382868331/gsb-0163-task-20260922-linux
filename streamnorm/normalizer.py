"""Streaming canonical normalization over an explicit character table.

Pipeline (per canonical segment):

1. recursive decomposition (table-driven, cycle-free by construction),
2. stable reordering of non-starters by canonical combining class,
3. canonical composition: a character composes with the last starter only
   when the table lists the pair *and* it is not blocked — i.e. the previous
   uncomposed character's ccc is 0 or strictly less than the current ccc.

A *canonical segment* is a starter together with the non-starters that
follow it (or, at the very start of the input, a run of leading
non-starters).  Segments never interact, so the stream only needs to hold
one open segment across chunk boundaries; ``finish()`` flushes it.
"""

from __future__ import annotations

from typing import NamedTuple

from ._checks import require_int
from .errors import (
    InputError,
    NormalizerStateError,
    SegmentTooLongError,
)
from .table import CharacterTable

#: Default upper bound on one canonical segment, in code points.
DEFAULT_MAX_SEGMENT_LENGTH = 1024


class SourceInterval(NamedTuple):
    """Half-open ``[start, end)`` interval into the concatenated input.

    One interval is recorded per emitted canonical segment.  Intervals are
    normally a partition of ``[0, len(input))``; they may overlap only when
    a single input character's decomposition spans a segment boundary
    (documented in the README).
    """

    start: int
    end: int


class NormalizationResult(NamedTuple):
    """Result of :func:`normalize`: the normalized text and the set of
    source intervals (ordered, one per canonical segment)."""

    text: str
    segments: tuple


def _render_segment(buffer, table):
    """Reorder then compose one buffered segment of ``(code_point, ccc)``.

    ``buffer`` is non-empty and contains no starter past index 0 (segment
    invariant maintained by the feeder), so a single stable sort by ccc is
    exactly the canonical ordering.
    """
    ordered = sorted(buffer, key=lambda item: item[1])  # sorted() is stable
    out = []
    starter_pos = -1
    prev_cc = 0  # ccc of the last character that was *not* composed
    for cp, cc in ordered:
        if starter_pos >= 0 and (prev_cc == 0 or prev_cc < cc):
            composite = table.compose(out[starter_pos], cp)
            if composite is not None:
                out[starter_pos] = composite
                continue  # composed: prev_cc intentionally unchanged
        out.append(cp)
        if cc == 0:
            starter_pos = len(out) - 1
        prev_cc = cc
    return "".join(map(chr, out))


class StreamNormalizer:
    """Incremental canonical normalizer.

    Usage::

        n = StreamNormalizer(table)
        text = n.push(chunk0) + n.push(chunk1) + n.finish()

    ``push`` returns the normalized text of every segment completed by that
    chunk; the trailing incomplete segment is held back.  ``finish`` flushes
    it.  Source intervals accumulate in ``.segments``.

    Failure semantics (fixed contract):

    * Contract violations (:class:`InputError`) are raised before any state
      is touched; the instance stays usable.
    * :class:`SegmentTooLongError` is raised mid-stream; the instance is
      then *poisoned* — any further ``push``/``finish`` raises
      :class:`NormalizerStateError`.  Discard it and start over.  This is
      the documented partial-change behaviour: text returned by earlier
      ``push`` calls remains valid, the failed call emitted nothing.
    """

    def __init__(self, table, max_segment_length=DEFAULT_MAX_SEGMENT_LENGTH):
        if not isinstance(table, CharacterTable):
            raise InputError(
                f"table: expected CharacterTable, got {type(table).__name__}"
            )
        require_int(max_segment_length, name="max_segment_length", error=InputError)
        if max_segment_length < 1:
            raise InputError(
                f"max_segment_length: must be >= 1, got {max_segment_length}"
            )
        self._table = table
        self._max_segment_length = max_segment_length
        self._buffer = []       # list of (code_point, ccc), the open segment
        self._seg_first = None  # first input index contributing to the buffer
        self._seg_last = None   # last input index contributing to the buffer
        self._next_index = 0    # global index of the next input character
        self._segments = []
        self._finished = False
        self._failed = False

    @property
    def segments(self):
        """Tuple of :class:`SourceInterval` emitted so far, in order."""
        return tuple(self._segments)

    @property
    def pending_length(self):
        """Code points currently held in the unfinished segment."""
        return len(self._buffer)

    def _check_usable(self):
        if self._failed:
            raise NormalizerStateError(
                "this StreamNormalizer failed earlier (e.g. SegmentTooLongError) "
                "and must be discarded"
            )
        if self._finished:
            raise NormalizerStateError("finish() has already been called")

    def push(self, chunk):
        """Feed one chunk; return normalized text for completed segments."""
        self._check_usable()
        if not isinstance(chunk, str):
            # Raised before any mutation: the instance stays usable.
            raise InputError(f"chunk: expected str, got {type(chunk).__name__}")
        out = []
        try:
            for ch in chunk:
                self._feed_char(ord(ch), out)
        except Exception:
            self._failed = True
            raise
        return "".join(out)

    def finish(self):
        """Flush the trailing segment and close the stream."""
        self._check_usable()
        try:
            text = _render_segment(self._buffer, self._table) if self._buffer else ""
            if self._buffer:
                self._record_segment()
                self._buffer = []
        except Exception:
            self._failed = True
            raise
        self._finished = True
        return text

    def _feed_char(self, cp, out):
        index = self._next_index
        self._next_index += 1
        table = self._table
        for dcp in table.expansion_of(cp):
            cc = table.ccc_of(dcp)
            if cc == 0 and self._buffer:
                # A starter closes the current segment.
                out.append(_render_segment(self._buffer, table))
                self._record_segment()
                self._buffer = []
            if len(self._buffer) >= self._max_segment_length:
                raise SegmentTooLongError(
                    f"canonical segment starting at input index {self._seg_first} "
                    f"exceeds the limit of {self._max_segment_length} code points",
                    limit=self._max_segment_length,
                    segment_start=self._seg_first,
                )
            self._buffer.append((dcp, cc))
            if self._seg_first is None:
                self._seg_first = index
            self._seg_last = index

    def _record_segment(self):
        self._segments.append(SourceInterval(self._seg_first, self._seg_last + 1))
        self._seg_first = None
        self._seg_last = None


def normalize(chunks, table, *, max_segment_length=DEFAULT_MAX_SEGMENT_LENGTH):
    """Normalize an iterable of string chunks in one call.

    Returns a :class:`NormalizationResult` with the normalized text and the
    ordered set of source intervals.  All-or-nothing: on any error nothing
    is returned (the exception propagates).

    ``chunks`` must be an iterable of ``str`` — a single ``str``/``bytes``
    is rejected rather than silently treated as a sequence of 1-character
    chunks.
    """
    if isinstance(chunks, (str, bytes, bytearray)):
        raise InputError(
            f"chunks: expected an iterable of str, got {type(chunks).__name__}; "
            "pass [text] to normalize one string"
        )
    try:
        iterator = iter(chunks)
    except TypeError:
        raise InputError(
            f"chunks: expected an iterable of str, got {type(chunks).__name__}"
        ) from None
    normalizer = StreamNormalizer(table, max_segment_length=max_segment_length)
    parts = []
    for i, chunk in enumerate(iterator):
        if not isinstance(chunk, str):
            raise InputError(
                f"chunks[{i}]: expected str, got {type(chunk).__name__}"
            )
        parts.append(normalizer.push(chunk))
    parts.append(normalizer.finish())
    return NormalizationResult("".join(parts), normalizer.segments)
