"""字符表编译：开始处理文本前的一致性检查与定稿。

compile_table 对调用方提供的有限字符表做一次**完整事前检查**，
通过则返回定稿的 CompiledCharacterTable；失败则抛出指向具体
表项的异常（不返回笼统的"无效字符表"）：

1. **循环分解**：分解映射整体成环检测，抛 DecompositionCycleError
   （chain 给出闭合路径上的具体码点）。
2. **不一致的组合等级**（ccc）：抛 InconsistentLevelError，
   entry 字段指明具体表项——

   - 分解首元素等级须等于被分解字符自身等级（"decomposition"）；
   - 组合项右元素必须是等级 > 0 的非 starter（"composition-second"）；
   - 组合结果必须是等级 0 的 starter（"composition-result"）。

3. **矛盾组合**：组合项 (left, right) -> result 要求 result 的递归
   分解叶子序列与 left、right 各自递归分解叶子的拼接完全一致，
   否则抛 CompositionConflictError（携带 left/right/result 与两侧
   展开序列）。

编译是**原子**的：要么返回一个完全可用的定稿表，要么抛异常，不产生
半成品；编译失败不影响此前已编译出的任何表（各表持有独立的不可变
快照，无共享可变状态）。通过编译的表可重复用于任意多个相互独立的
文本流（normalize / StreamNormalizer）。

编译需要枚举有限表项，因此表对象须暴露三个只读映射属性
（DictCharacterTable 与本类均满足）：
decompositions、combining_classes、compositions。
"""

from __future__ import annotations

from typing import Mapping

from .errors import (
    CompositionConflictError,
    DecompositionCycleError,
    InconsistentLevelError,
    InvalidInputError,
    InvalidTableError,
)
from .table import CharacterTable
from .validators import MAX_CODEPOINT

__all__ = ["CompiledCharacterTable", "compile_table"]


def _hex_cp(codepoint: int) -> str:
    return f"U+{codepoint:04X}"


def _validate_entry_codepoint(value: object, *, owner: str) -> int:
    """校验表项中的码点，错误指向所属表项。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTableError(
            f"codepoint must be int, got {type(value).__name__} "
            f"in {owner}"
        )
    if value < 0 or value > MAX_CODEPOINT:
        raise InvalidTableError(
            f"codepoint out of range 0..0x{MAX_CODEPOINT:X} in {owner}"
        )
    return value


class CompiledCharacterTable(CharacterTable):
    """通过一致性检查的定稿字符表。

    持有源表内容的**不可变快照**：编译后修改源表不影响本表；
    本表可安全地重复用于任意多个独立文本流。除协议三方法外，
    额外提供 expansion() 返回编译期算好的递归分解叶子序列。
    """

    def __init__(
        self,
        decompositions: Mapping[int, tuple[int, ...]],
        combining_classes: Mapping[int, int],
        compositions: Mapping[tuple[int, int], int],
        expansions: Mapping[int, tuple[int, ...]],
    ) -> None:
        self._decomp = dict(decompositions)
        self._ccc = dict(combining_classes)
        self._comp = dict(compositions)
        self._expansions = dict(expansions)

    @property
    def decompositions(self) -> Mapping[int, tuple[int, ...]]:
        return dict(self._decomp)

    @property
    def combining_classes(self) -> Mapping[int, int]:
        return dict(self._ccc)

    @property
    def compositions(self) -> Mapping[tuple[int, int], int]:
        return dict(self._comp)

    def decomposition(self, codepoint: int) -> tuple[int, ...] | None:
        return self._decomp.get(codepoint)

    def combining_class(self, codepoint: int) -> int:
        return self._ccc.get(codepoint, 0)

    def composition(self, left: int, right: int) -> int | None:
        return self._comp.get((left, right))

    def expansion(self, codepoint: int) -> tuple[int, ...]:
        """返回编译期缓存的递归分解叶子序列（未列出码点返回自身）。"""
        return self._expansions.get(codepoint, (codepoint,))


def _snapshot_entries(
    table: object,
) -> tuple[
    dict[int, tuple[int, ...]],
    dict[int, int],
    dict[tuple[int, int], int],
]:
    """枚举并逐一校验有限表项，返回校验过的快照。"""
    mappings = []
    for name in ("decompositions", "combining_classes", "compositions"):
        mapping = getattr(table, name, None)
        if not isinstance(mapping, Mapping):
            raise InvalidInputError(
                f"table must expose a {name} mapping to be compiled"
            )
        mappings.append(mapping)
    raw_decomp, raw_ccc, raw_comp = mappings

    decomp: dict[int, tuple[int, ...]] = {}
    for key, value in raw_decomp.items():
        cp = _validate_entry_codepoint(key, owner="decomposition key")
        owner = f"decomposition entry for {_hex_cp(cp)}"
        if not isinstance(value, tuple) or len(value) == 0:
            raise InvalidTableError(
                "decomposition must be a non-empty tuple of codepoints, "
                f"got {value!r} in {owner}"
            )
        decomp[cp] = tuple(
            _validate_entry_codepoint(child, owner=owner) for child in value
        )

    ccc: dict[int, int] = {}
    for key, value in raw_ccc.items():
        cp = _validate_entry_codepoint(key, owner="combining class key")
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidTableError(
                "combining class must be int, got "
                f"{type(value).__name__}",
                codepoint=cp,
            )
        if value < 0:
            raise InvalidTableError(
                f"combining class must be non-negative, got {value}",
                codepoint=cp,
            )
        ccc[cp] = value

    comp: dict[tuple[int, int], int] = {}
    for key, value in raw_comp.items():
        if not isinstance(key, tuple) or len(key) != 2:
            raise InvalidTableError(
                f"composition key must be a (left, right) pair, got {key!r}"
            )
        left = _validate_entry_codepoint(key[0], owner="composition key")
        right = _validate_entry_codepoint(key[1], owner="composition key")
        result = _validate_entry_codepoint(
            value,
            owner=f"composition entry ({_hex_cp(left)}, {_hex_cp(right)})",
        )
        comp[(left, right)] = result

    return decomp, ccc, comp


def _check_decomposition_cycles(graph: Mapping[int, tuple[int, ...]]) -> None:
    """对整个分解图做成环检测，环上码点写入异常 chain。"""
    UNVISITED, ON_PATH, DONE = 0, 1, 2
    state: dict[int, int] = {}
    for root in graph:
        if state.get(root, UNVISITED) != UNVISITED:
            continue
        stack = [root]
        path = [root]
        state[root] = ON_PATH
        pending = {root: iter(graph[root])}
        while stack:
            node = stack[-1]
            descended = False
            for child in pending[node]:
                child_state = state.get(child, UNVISITED)
                if child_state == ON_PATH:
                    start = path.index(child)
                    raise DecompositionCycleError(
                        tuple(path[start:]) + (child,)
                    )
                if child_state == UNVISITED:
                    state[child] = ON_PATH
                    stack.append(child)
                    path.append(child)
                    pending[child] = iter(graph.get(child, ()))
                    descended = True
                    break
            if not descended:
                state[node] = DONE
                stack.pop()
                path.pop()


def _build_expansions(
    graph: Mapping[int, tuple[int, ...]],
    roots: set[int],
) -> dict[int, tuple[int, ...]]:
    """对（已无环的）分解图计算每个相关码点的递归叶子序列。"""
    expansions: dict[int, tuple[int, ...]] = {}
    for root in roots:
        if root in expansions:
            continue
        stack = [root]
        while stack:
            node = stack[-1]
            children = graph.get(node)
            if children is None:
                expansions[node] = (node,)
                stack.pop()
                continue
            if all(child in expansions for child in children):
                expansions[node] = tuple(
                    leaf for child in children for leaf in expansions[child]
                )
                stack.pop()
            else:
                for child in children:
                    if child not in expansions:
                        stack.append(child)
    return expansions


def compile_table(table: object) -> CompiledCharacterTable:
    """检查并编译调用方提供的有限字符表。

    通过则返回可重复使用的 CompiledCharacterTable；失败抛出指向
    具体表项的 InvalidTableError 子类异常，且不影响任何先前已
    编译的表。
    """
    decomp, ccc, comp = _snapshot_entries(table)

    # 1) 循环分解。
    _check_decomposition_cycles(decomp)

    # 2) 递归展开（供等级与矛盾检查使用）。
    roots: set[int] = set(decomp)
    for (left, right), result in comp.items():
        roots.update((left, right, result))
    expansions = _build_expansions(decomp, roots)

    def level(cp: int) -> int:
        return ccc.get(cp, 0)

    # 3) 不一致的组合等级。
    for cp, children in decomp.items():
        own = level(cp)
        head = level(children[0])
        if own != head:
            raise InconsistentLevelError(
                f"decomposition of {_hex_cp(cp)} starts with "
                f"{_hex_cp(children[0])} whose combining class differs "
                "from the decomposed character",
                entry="decomposition",
                codepoint=cp,
                expected_ccc=own,
                actual_ccc=head,
            )
    for (left, right), result in comp.items():
        right_level = level(right)
        if right_level == 0:
            raise InconsistentLevelError(
                f"composition ({_hex_cp(left)}, {_hex_cp(right)}) uses a "
                "starter (ccc 0) as the combining mark",
                entry="composition-second",
                codepoint=right,
                left=left,
                right=right,
                expected_ccc=1,
                actual_ccc=0,
            )
        result_level = level(result)
        if result_level != 0:
            raise InconsistentLevelError(
                f"composition ({_hex_cp(left)}, {_hex_cp(right)}) yields "
                f"{_hex_cp(result)} which is not a starter",
                entry="composition-result",
                codepoint=result,
                left=left,
                right=right,
                expected_ccc=0,
                actual_ccc=result_level,
            )

    # 4) 矛盾组合：组合结果的展开须等于两侧展开的拼接。
    for (left, right), result in comp.items():
        expected = expansions[left] + expansions[right]
        actual = expansions[result]
        if expected != actual:
            raise CompositionConflictError(
                left, right, result, expected, actual
            )

    return CompiledCharacterTable(decomp, ccc, comp, expansions)
