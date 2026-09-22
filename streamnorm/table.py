"""Explicit character table: decompositions, combining classes, compositions.

The table is the *only* source of character knowledge for the normalizer.
Nothing in this package consults ``unicodedata`` or any system Unicode
database, so results are identical on every machine and every Python build.

A table provides three finite mappings:

* ``decompositions``: ``{code_point: (code_point, ...)}`` — canonical
  decomposition, applied recursively.  The graph must be acyclic; cycles are
  detected at construction and rejected with
  :class:`~streamnorm.errors.CyclicDecompositionError`.
* ``ccc``: ``{code_point: int}`` — canonical combining class, 0..254.
  Missing entries default to 0 (a "starter").
* ``compositions``: ``{(code_point, code_point): code_point}`` — binary
  canonical composition mapping.

All mappings are validated eagerly and stored immutably.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ._checks import require_int
from .errors import CyclicDecompositionError, TableError

MIN_CODE_POINT = 0x0000
MAX_CODE_POINT = 0x10FFFF
_SURROGATE_LO = 0xD800
_SURROGATE_HI = 0xDFFF

#: Largest accepted canonical combining class value.
MAX_CCC = 254

#: Maximum number of code points one input code point may expand into after
#: recursive decomposition.  This is a deliberate input limit (documented in
#: the README): it keeps pathological tables from expanding exponentially.
MAX_EXPANSION = 64


def _format_cp(cp):
    return f"U+{cp:04X}"


def _check_code_point(value, *, name):
    require_int(value, name=name, error=TableError)
    if not (MIN_CODE_POINT <= value <= MAX_CODE_POINT):
        raise TableError(
            f"{name}: code point {_format_cp(value) if value >= 0 else value} "
            f"out of range [U+0000, U+10FFFF]"
        )
    if _SURROGATE_LO <= value <= _SURROGATE_HI:
        raise TableError(f"{name}: surrogate code point {_format_cp(value)} is not a scalar value")
    return value


def _check_mapping(value, *, name):
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TableError(f"{name}: expected a mapping, got {type(value).__name__}")
    return value


def _check_decompositions(value):
    mapping = _check_mapping(value, name="decompositions")
    out = {}
    for key, targets in mapping.items():
        _check_code_point(key, name=f"decompositions key {key!r}")
        if isinstance(targets, (str, bytes, bytearray)) or not isinstance(targets, (tuple, list)):
            raise TableError(
                f"decompositions[{_format_cp(key)}]: expected a tuple/list of "
                f"code points, got {type(targets).__name__}"
            )
        if len(targets) == 0:
            raise TableError(f"decompositions[{_format_cp(key)}]: empty decomposition is not allowed")
        checked = []
        for i, part in enumerate(targets):
            _check_code_point(part, name=f"decompositions[{_format_cp(key)}][{i}]")
            checked.append(part)
        out[key] = tuple(checked)
    return out


def _check_ccc(value):
    mapping = _check_mapping(value, name="ccc")
    out = {}
    for key, cc in mapping.items():
        _check_code_point(key, name=f"ccc key {key!r}")
        require_int(cc, name=f"ccc[{_format_cp(key)}]", error=TableError)
        if not (0 <= cc <= MAX_CCC):
            raise TableError(f"ccc[{_format_cp(key)}]: {cc} out of range [0, {MAX_CCC}]")
        out[key] = cc
    return out


def _check_compositions(value):
    mapping = _check_mapping(value, name="compositions")
    out = {}
    for key, composite in mapping.items():
        if isinstance(key, (str, bytes, bytearray)) or not isinstance(key, (tuple, list)) or len(key) != 2:
            raise TableError(
                f"compositions key {key!r}: expected a (first, second) pair of code points"
            )
        first, second = key
        _check_code_point(first, name=f"compositions key {key!r}[0]")
        _check_code_point(second, name=f"compositions key {key!r}[1]")
        _check_code_point(composite, name=f"compositions[{key!r}]")
        out[(first, second)] = composite
    return out


def _build_expansions(decompositions):
    """Fully expand every decomposition recursively, detecting cycles.

    Returns ``{code_point: (code_point, ...)}`` covering every code point
    reachable from the decomposition graph.  Iterative post-order DFS, so
    deep (but finite) chains do not hit the Python recursion limit.
    """
    expansions = {}
    state = {}  # code point -> "visiting" | "done"

    for start in decompositions:
        if state.get(start) == "done":
            continue
        state[start] = "visiting"
        stack = [(start, 0)]  # (node, next child index)
        while stack:
            node, child_index = stack[-1]
            children = decompositions.get(node)
            if children is None:
                # Leaf: not itself decomposable.
                expansions[node] = (node,)
                state[node] = "done"
                stack.pop()
                continue
            if child_index < len(children):
                child = children[child_index]
                stack[-1] = (node, child_index + 1)
                child_state = state.get(child)
                if child_state == "visiting":
                    path = [n for n, _ in stack]
                    cycle_start = path.index(child)
                    cycle = tuple(path[cycle_start:]) + (child,)
                    pretty = " -> ".join(_format_cp(c) for c in cycle)
                    raise CyclicDecompositionError(
                        f"decomposition cycle detected: {pretty}", cycle=cycle
                    )
                if child_state != "done":
                    state[child] = "visiting"
                    stack.append((child, 0))
            else:
                parts = []
                for child in children:
                    parts.extend(expansions[child])
                    if len(parts) > MAX_EXPANSION:
                        raise TableError(
                            f"decomposition of {_format_cp(start)} expands beyond "
                            f"{MAX_EXPANSION} code points (limit MAX_EXPANSION)"
                        )
                expansions[node] = tuple(parts)
                state[node] = "done"
                stack.pop()
    return expansions


class CharacterTable:
    """Immutable explicit character table.

    Parameters
    ----------
    decompositions:
        Mapping ``{code_point: (code_point, ...)}`` or ``None``.
    ccc:
        Mapping ``{code_point: int}`` (0..254) or ``None``.
    compositions:
        Mapping ``{(first, second): composite}`` or ``None``.

    Raises
    ------
    TableError
        On any malformed entry (wrong types, bool/float where an int is
        required, out-of-range or surrogate code points, ccc out of range,
        decomposition expansions beyond ``MAX_EXPANSION``).
    CyclicDecompositionError
        If the decomposition graph contains a cycle.
    """

    __slots__ = ("_decompositions", "_ccc", "_compositions", "_expansions")

    def __init__(self, decompositions=None, ccc=None, compositions=None):
        decomp = _check_decompositions(decompositions)
        ccc_map = _check_ccc(ccc)
        comp = _check_compositions(compositions)
        expansions = _build_expansions(decomp)
        self._decompositions = MappingProxyType(decomp)
        self._ccc = MappingProxyType(ccc_map)
        self._compositions = MappingProxyType(comp)
        self._expansions = MappingProxyType(expansions)

    @property
    def decompositions(self):
        """Read-only view of ``{code_point: (code_point, ...)}``."""
        return self._decompositions

    @property
    def ccc(self):
        """Read-only view of ``{code_point: combining_class}``."""
        return self._ccc

    @property
    def compositions(self):
        """Read-only view of ``{(first, second): composite}``."""
        return self._compositions

    def ccc_of(self, code_point):
        """Canonical combining class of ``code_point`` (0 when unlisted)."""
        return self._ccc.get(code_point, 0)

    def expansion_of(self, code_point):
        """Fully decomposed code-point tuple (the code point itself when
        it has no decomposition)."""
        return self._expansions.get(code_point, (code_point,))

    def compose(self, first, second):
        """Composite of ``(first, second)`` or ``None`` when unlisted."""
        return self._compositions.get((first, second))

    def __repr__(self):
        return (
            f"CharacterTable(decompositions={len(self._decompositions)}, "
            f"ccc={len(self._ccc)}, compositions={len(self._compositions)})"
        )
