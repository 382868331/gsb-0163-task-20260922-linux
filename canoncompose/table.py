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

__all__ = ["CharacterTable", "DictCharacterTable"]


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
