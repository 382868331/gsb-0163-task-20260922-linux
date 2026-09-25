"""字符表协议。

本库不引用系统 Unicode 数据库，也不绑定任何 Unicode 版本；规范化所需的
全部知识都由调用方提供的 *字符表* 给出。字符表是满足 :class:`CharacterTable`
协议的任意对象（鸭子类型即可），包含三个方法：

``decomposition(cp) -> None | tuple[int, ...]``
    码点的一步分解。``None`` 表示无分解；否则为非空码点元组。
    核心会沿该映射递归展开，因此只需提供一步分解；映射成环会被检测并拒绝。

``combining_class(cp) -> int``
    canonical combining class（ccc）。非负整数；``0`` 表示 starter。
    缺失码点惯例返回 ``0``。

``composition(a, b) -> int | None``
    相邻二元组合映射：仅当返回码点而非 ``None`` 时才发生组合。
    是否允许调用还受核心的阻塞规则约束（见 :mod:`canoncompose.engine`）。

返回值会被核心逐一校验，类型或范围非法时抛
:class:`~canoncompose.errors.InvalidTableError`。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping

from .errors import InvalidTableError
from .validators import MAX_CODEPOINT

__all__ = ["CharacterTable", "DictCharacterTable"]


def _check_rule_codepoint(value: object, *, owner: int | None) -> int:
    """校验规则表里的码点（键或值），非法时抛 InvalidTableError。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTableError(
            f"table rule codepoint must be int, got {type(value).__name__}",
            codepoint=owner,
        )
    if value < 0 or value > MAX_CODEPOINT:
        raise InvalidTableError(
            f"table rule codepoint out of range 0..0x{MAX_CODEPOINT:X}, "
            f"got U+{value:X}",
            codepoint=owner,
        )
    return value


class CharacterTable(ABC):
    """字符表抽象基类。子类实现三个查询方法即可。"""

    @abstractmethod
    def decomposition(self, codepoint: int) -> tuple[int, ...] | None:
        """返回一步分解；无分解返回 ``None``。"""

    @abstractmethod
    def combining_class(self, codepoint: int) -> int:
        """返回 canonical combining class（非负整数，0 为 starter）。"""

    @abstractmethod
    def composition(self, left: int, right: int) -> int | None:
        """返回二元组合码点；不可组合返回 ``None``。"""


class DictCharacterTable(CharacterTable):
    """基于字典的字符表，便于构造确定性测试/演示数据。

    - ``decompositions``：``{码点: 一步分解元组}``，未列出即无分解；
    - ``combining_classes``：``{码点: ccc}``，未列出按 ``0``（starter）；
    - ``compositions``：``{(左码点, 右码点): 组合码点}``，未列出即不可组合。
    """

    def __init__(
        self,
        decompositions: Mapping[int, tuple[int, ...]] | None = None,
        combining_classes: Mapping[int, int] | None = None,
        compositions: Mapping[tuple[int, int], int] | None = None,
    ) -> None:
        self._decomp: dict[int, tuple[int, ...]] = dict(decompositions or {})
        self._ccc: dict[int, int] = dict(combining_classes or {})
        self._comp: dict[tuple[int, int], int] = dict(compositions or {})

    def decomposition(self, codepoint: int) -> tuple[int, ...] | None:
        return self._decomp.get(codepoint)

    def combining_class(self, codepoint: int) -> int:
        return self._ccc.get(codepoint, 0)

    def composition(self, left: int, right: int) -> int | None:
        return self._comp.get((left, right))

    def canonical_rules(self) -> dict:
        """返回三项有限规则的规范排序表示（JSON 兼容）。

        规则按键排序、每项内部顺序固定，因此**内容相同**（与构造时的
        插入顺序无关）的两个 ``DictCharacterTable`` 必然得到相同表示；
        内容不同则表示不同。供检查点指纹使用，见
        :meth:`canoncompose.engine.StreamNormalizer.export_checkpoint`。

        表示中所有码点/ccc 都经过校验；表内容非法时抛
        :class:`~canoncompose.errors.InvalidTableError`。
        """
        decomp: list[tuple[int, tuple[int, ...]]] = []
        for cp, children in self._decomp.items():
            _check_rule_codepoint(cp, owner=None)
            if not isinstance(children, tuple) or len(children) == 0:
                raise InvalidTableError(
                    "decomposition rule must map to a non-empty tuple, "
                    f"got {children!r}",
                    codepoint=cp,
                )
            decomp.append(
                (cp, tuple(_check_rule_codepoint(c, owner=cp) for c in children))
            )

        ccc_items: list[tuple[int, int]] = []
        for cp, ccc in self._ccc.items():
            _check_rule_codepoint(cp, owner=None)
            if isinstance(ccc, bool) or not isinstance(ccc, int) or ccc < 0:
                raise InvalidTableError(
                    f"combining class must be a non-negative int, got {ccc!r}",
                    codepoint=cp,
                )
            ccc_items.append((cp, ccc))

        comp_items: list[tuple[int, int, int]] = []
        for pair, result in self._comp.items():
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(p, int) and not isinstance(p, bool) for p in pair)
            ):
                raise InvalidTableError(
                    f"composition rule key must be a (left, right) int pair, "
                    f"got {pair!r}"
                )
            left, right = pair
            _check_rule_codepoint(left, owner=None)
            _check_rule_codepoint(right, owner=None)
            _check_rule_codepoint(result, owner=left)
            comp_items.append((left, right, result))

        return {
            "decompositions": [
                [cp, list(children)] for cp, children in sorted(decomp)
            ],
            "combining_classes": [[cp, ccc] for cp, ccc in sorted(ccc_items)],
            "compositions": [
                [left, right, result] for left, right, result in sorted(comp_items)
            ],
        }
