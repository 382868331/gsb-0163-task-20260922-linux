"""字符表的显式检查与编译。

编译器在处理任何文本前完整遍历有限字符表，生成不可变快照。检查包括：
分解环、非法表项、分解中的 ccc 顺序、组合映射与分解互相矛盾，以及
组合左/右/结果码点的 ccc 等级不一致。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from .errors import (
    DecompositionCycleError,
    InvalidTableError,
    TableEntry,
)
from .table import CharacterTable, DictCharacterTable, TableMappings
from .validators import MAX_CODEPOINT

__all__ = ["CompiledCharacterTable", "compile_table"]


_TableMappings = TableMappings


class CompiledCharacterTable(CharacterTable):
    """通过完整检查后的不可变字符表快照，可被多个独立流复用。"""

    def __init__(self, mappings: _TableMappings) -> None:
        self._decomp = dict(mappings.decompositions)
        self._ccc = dict(mappings.combining_classes)
        self._comp = dict(mappings.compositions)
        self._expansions: dict[int, tuple[int, ...]] = {}

    def decomposition(self, codepoint: int) -> tuple[int, ...] | None:
        return self._decomp.get(codepoint)

    def combining_class(self, codepoint: int) -> int:
        return self._ccc.get(codepoint, 0)

    def composition(self, left: int, right: int) -> int | None:
        return self._comp.get((left, right))

    def expansion(self, codepoint: int) -> tuple[int, ...]:
        """返回复编译期算好的递归叶子展开；无分解时为单码点元组。"""
        return self._expansions.get(codepoint, (codepoint,))

    def table_entries(self) -> _TableMappings:
        return _TableMappings(
            MappingProxyType(self._decomp),
            MappingProxyType(self._ccc),
            MappingProxyType(self._comp),
        )


def _is_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int)


def _codepoint(value: object, entry: TableEntry) -> int:
    if not _is_int(value):
        raise InvalidTableError(
            f"codepoint must be int, got {type(value).__name__}",
            entry=entry,
        )
    if value < 0 or value > MAX_CODEPOINT:
        raise InvalidTableError(
            f"codepoint out of range 0..0x{MAX_CODEPOINT:X}, got U+{value:X}",
            entry=entry,
        )
    return value


def _mappings_from_raw(raw: object) -> _TableMappings:
    if type(raw) is DictCharacterTable:
        exported = raw.table_entries()
        return _TableMappings(*exported)
    exporter = getattr(raw, "table_entries", None)
    if callable(exporter):
        try:
            exported = exporter()
        except (TypeError, ValueError) as exc:
            raise InvalidTableError(
                f"table_entries() failed: {exc}"
            ) from exc
        if isinstance(exported, _TableMappings):
            return exported
        if isinstance(exported, Mapping):
            try:
                return _TableMappings(
                    exported["decompositions"],
                    exported["combining_classes"],
                    exported["compositions"],
                )
            except (KeyError, TypeError) as exc:
                raise InvalidTableError(
                    "table_entries() mapping must contain "
                    "'decompositions', 'combining_classes' and "
                    "'compositions'"
                ) from exc
        if isinstance(exported, tuple) and len(exported) == 3:
            return _TableMappings(*exported)
        raise InvalidTableError(
            "table_entries() must return three mappings or a mapping with "
            "decompositions/combining_classes/compositions"
        )

    raise InvalidTableError(
        "cannot compile a non-enumerable table: provide table_entries() "
        "returning decompositions, combining_classes and compositions"
    )


def _validate_maps(raw_maps: _TableMappings) -> tuple[
    dict[int, tuple[int, ...]],
    dict[int, int],
    dict[tuple[int, int], int],
]:
    decompositions: dict[int, tuple[int, ...]] = {}
    ccc: dict[int, int] = {}
    compositions: dict[tuple[int, int], int] = {}

    for label, mapping in (
        ("decompositions", raw_maps.decompositions),
        ("combining_classes", raw_maps.combining_classes),
        ("compositions", raw_maps.compositions),
    ):
        if not isinstance(mapping, Mapping):
            raise InvalidTableError(f"{label} must be a mapping")

    for source, children in raw_maps.decompositions.items():
        entry = TableEntry(TableEntry.DECOMPOSITION, source)
        source_cp = _codepoint(source, entry)
        if not isinstance(children, tuple) or not children:
            raise InvalidTableError(
                "decomposition must be a non-empty tuple",
                entry=entry,
                codepoint=source_cp,
            )
        decompositions[source_cp] = tuple(
            _codepoint(child, entry) for child in children
        )

    for cp, value in raw_maps.combining_classes.items():
        entry = TableEntry(TableEntry.COMBINING_CLASS, cp)
        cp_value = _codepoint(cp, entry)
        if not _is_int(value) or value < 0:
            raise InvalidTableError(
                "combining class must be a non-negative int, got "
                f"{value!r}",
                entry=entry,
                codepoint=cp_value,
            )
        ccc[cp_value] = value

    for key, result in raw_maps.compositions.items():
        if not isinstance(key, tuple) or len(key) != 2:
            raise InvalidTableError(
                "composition key must be a (left, right) codepoint pair, "
                f"got {key!r}"
            )
        left = _codepoint(
            key[0], TableEntry(TableEntry.COMPOSITION, key)
        )
        right = _codepoint(
            key[1], TableEntry(TableEntry.COMPOSITION, key)
        )
        pair = (left, right)
        result_cp = _codepoint(
            result, TableEntry(TableEntry.COMPOSITION, pair)
        )
        compositions[pair] = result_cp

    return decompositions, ccc, compositions


def _class_of(cp: int, ccc: Mapping[int, int]) -> int:
    return ccc.get(cp, 0)


def _check_decomposition_order(
    decompositions: Mapping[int, tuple[int, ...]],
    ccc: Mapping[int, int],
    expansions: Mapping[int, tuple[int, ...]],
) -> None:
    for source in decompositions:
        entry = TableEntry(TableEntry.DECOMPOSITION, source)
        source_class = _class_of(source, ccc)
        classes = tuple(_class_of(cp, ccc) for cp in expansions[source])

        previous = 0
        seen_nonstarter = False
        for position, value in enumerate(classes):
            if value == 0:
                if source_class > 0:
                    raise InvalidTableError(
                        "non-starter decomposition contains a starter at "
                        f"position {position}",
                        entry=entry,
                        codepoint=source,
                    )
                if seen_nonstarter:
                    raise InvalidTableError(
                        "decomposition has a starter after a non-starter at "
                        f"position {position}",
                        entry=entry,
                        codepoint=source,
                    )
            else:
                seen_nonstarter = True
                if value < previous:
                    raise InvalidTableError(
                        "decomposition is not in non-decreasing ccc order at "
                        f"position {position}: {previous} > {value}",
                        entry=entry,
                        codepoint=source,
                    )
                previous = value


def _check_composition_levels_and_consistency(
    decompositions: Mapping[int, tuple[int, ...]],
    ccc: Mapping[int, int],
    compositions: Mapping[tuple[int, int], int],
    expansions: Mapping[int, tuple[int, ...]],
) -> None:
    for (left, right), result in compositions.items():
        entry = TableEntry(TableEntry.COMPOSITION, (left, right))
        left_class = _class_of(left, ccc)
        right_class = _class_of(right, ccc)
        result_class = _class_of(result, ccc)

        if left_class != 0:
            raise InvalidTableError(
                f"composition left operand ccc must be 0, got {left_class}",
                entry=entry,
                codepoint=left,
            )
        if right_class <= 0:
            raise InvalidTableError(
                f"composition right operand ccc must be positive, got "
                f"{right_class}",
                entry=entry,
                codepoint=right,
            )
        if result_class != 0:
            raise InvalidTableError(
                "composition result ccc must be 0, got "
                f"{result_class}",
                entry=entry,
                codepoint=result,
            )

        if result not in decompositions:
            continue

        result_leaves = expansions[result]
        pair_leaves = expansions.get(left, (left,)) + expansions.get(
            right, (right,)
        )
        if result_leaves != pair_leaves:
            raise InvalidTableError(
                "composition contradicts recursive decomposition: "
                f"U+{result:04X} expands to {result_leaves!r}, pair expands "
                f"to {pair_leaves!r}",
                entry=entry,
                codepoint=result,
            )


def _check_cycles(decompositions: Mapping[int, tuple[int, ...]]) -> None:
    state: dict[int, int] = {}
    stack: list[int] = []

    def visit(node: int) -> None:
        if state.get(node) == 1:
            start = stack.index(node)
            raise DecompositionCycleError(
                tuple(stack[start:]) + (node,)
            )
        if state.get(node) == 2:
            return
        state[node] = 1
        stack.append(node)
        for child in decompositions.get(node, ()):
            visit(child)
        stack.pop()
        state[node] = 2

    for node in decompositions:
        visit(node)


def _build_expansions(
    decompositions: Mapping[int, tuple[int, ...]],
) -> dict[int, tuple[int, ...]]:
    expansions: dict[int, tuple[int, ...]] = {}

    def expand(cp: int) -> tuple[int, ...]:
        cached = expansions.get(cp)
        if cached is not None:
            return cached
        children = decompositions.get(cp)
        if children is None:
            return (cp,)
        leaves: list[int] = []
        for child in children:
            leaves.extend(expand(child))
        value = tuple(leaves)
        expansions[cp] = value
        return value

    for source in decompositions:
        expand(source)
    return expansions


def compile_table(
    table: CharacterTable,
) -> CompiledCharacterTable:
    """检查并编译字符表；成功返回不可变快照，失败不产生部分编译表。"""
    if isinstance(table, CompiledCharacterTable):
        return table

    mappings = _validate_maps(_mappings_from_raw(table))
    decompositions, ccc, compositions = mappings
    _check_cycles(decompositions)
    expansions = _build_expansions(decompositions)
    _check_decomposition_order(decompositions, ccc, expansions)
    _check_composition_levels_and_consistency(
        decompositions, ccc, compositions, expansions
    )
    compiled = CompiledCharacterTable(
        _TableMappings(decompositions, ccc, compositions)
    )
    compiled._expansions = expansions
    return compiled
