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

import hashlib
import json
from abc import ABC, abstractmethod
from typing import Mapping

from .errors import InvalidTableError
from .validators import MAX_CODEPOINT

__all__ = ["CharacterTable", "DictCharacterTable"]

# 指纹/规则规范化使用的固定名字，写入哈希载荷，避免不同规则类别互相顶替。
_FP_SPEC_VERSION = 1


def _validate_rule_codepoint(value: object, *, what: str) -> int:
    """校验规则表里的码点（键、分解子节点、组合结果），非法抛 InvalidTableError。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTableError(
            f"{what} must be int, got {type(value).__name__}"
        )
    if value < 0 or value > MAX_CODEPOINT:
        raise InvalidTableError(
            f"{what} out of range 0..0x{MAX_CODEPOINT:X}, got {value!r}"
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

    # ---- 规则快照与内容指纹（供检查点恢复时核对表身份） ------------------

    def canonical_rules(self) -> dict:
        """把有限规则整理成**规范排序**的 JSON 兼容结构。

        - 分解：``[[码点, [子码点, ...]], ...]`` 按码点升序；
        - ccc：``[[码点, ccc], ...]`` 按码点升序（显式写出的 ``ccc=0``
          条目保留，与缺省语义相同但内容不同，指纹也不同）；
        - 组合：``[[左, 右, 结果], ...]`` 按 ``(左, 右)`` 升序。

        排序使指纹只取决于规则内容，与构造时的插入顺序无关。规则内容
        非法（码点越界、分解为空元组、ccc 为负或 bool 等）时抛
        :class:`~canoncompose.errors.InvalidTableError`。
        """
        decomp: list[list] = []
        for cp, children in self._decomp.items():
            _validate_rule_codepoint(cp, what="decomposition key")
            if not isinstance(children, tuple) or len(children) == 0:
                raise InvalidTableError(
                    "decomposition rules must be non-empty tuples, got "
                    f"{children!r} for key U+{cp:04X}"
                )
            decomp.append(
                [
                    cp,
                    [
                        _validate_rule_codepoint(c, what="decomposition child")
                        for c in children
                    ],
                ]
            )
        decomp.sort(key=lambda entry: entry[0])

        ccc: list[list[int]] = []
        for cp, value in self._ccc.items():
            _validate_rule_codepoint(cp, what="combining class key")
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidTableError(
                    "combining class must be int, got "
                    f"{type(value).__name__} for key U+{cp:04X}"
                )
            if value < 0:
                raise InvalidTableError(
                    f"combining class must be non-negative, got {value} "
                    f"for key U+{cp:04X}"
                )
            ccc.append([cp, value])
        ccc.sort(key=lambda entry: entry[0])

        comp: list[list[int]] = []
        for key, result in self._comp.items():
            if not isinstance(key, tuple) or len(key) != 2:
                raise InvalidTableError(
                    f"composition key must be a (left, right) tuple, got {key!r}"
                )
            left = _validate_rule_codepoint(key[0], what="composition left")
            right = _validate_rule_codepoint(key[1], what="composition right")
            comp.append(
                [left, right, _validate_rule_codepoint(result, what="composition result")]
            )
        comp.sort(key=lambda entry: (entry[0], entry[1]))

        return {
            "decompositions": decomp,
            "combining_classes": ccc,
            "compositions": comp,
        }

    def fingerprint(self) -> str:
        """表内容指纹：规范排序规则的 JSON 的 SHA-256 十六进制串。

        只依赖规则内容，不依赖对象地址、进程或插入顺序；同内容的表在
        另一个进程重建后指纹相同。规则内容非法时抛
        :class:`~canoncompose.errors.InvalidTableError`。
        """
        payload = {
            "fingerprint_spec": _FP_SPEC_VERSION,
            "rules": self.canonical_rules(),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
