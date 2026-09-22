"""仓库内确定性示例数据（不依赖任何外部语料或系统 Unicode 数据）。

普通小写字母作为 starter；非 starter 记号与预组合字符使用私用区
（PUA, U+E000..U+F8FF）。数据刻意覆盖：

- 多级递归分解（:data:`P_A_GRAVE_RING` → :data:`P_A_GRAVE` → a + grave）；
- ccc 重排（dotbelow ccc=220 先于 grave ccc=230）；
- 链式组合（a + grave → agrave，agrave + ring(232) → a-grave-ring）；
- 同 ccc 阻塞（grave/acute 同为 230，未组合的前者阻塞后者）；
- 跨块挂起段；
- 分解环（:func:`build_cyclic_table`）。
"""

from __future__ import annotations

from .table import DictCharacterTable

__all__ = [
    "A",
    "E",
    "O",
    "M_GRAVE",
    "M_ACUTE",
    "M_DOTBELOW",
    "M_RING",
    "P_A_GRAVE",
    "P_E_ACUTE",
    "P_O_ACUTE",
    "P_A_GRAVE_RING",
    "CYCLE_X",
    "CYCLE_Y",
    "build_demo_table",
    "build_cyclic_table",
    "describe",
]

# ---- starter：普通小写字母，ccc 默认 0 --------------------------------
A = ord("a")
E = ord("e")
O = ord("o")

# ---- 非 starter 记号（PUA），ccc 模拟常见规范类 ------------------------
M_GRAVE = 0xE020      # ccc 230
M_ACUTE = 0xE021      # ccc 230（与 grave 同类，制造同 ccc 阻塞）
M_DOTBELOW = 0xE022   # ccc 220（小于 230，排序时前移）
M_RING = 0xE023       # ccc 232（大于 230，允许链式组合继续）

# ---- 预组合字符（PUA），均为 starter -----------------------------------
P_A_GRAVE = 0xE100          # à = a + grave
P_E_ACUTE = 0xE101          # é = e + acute
P_O_ACUTE = 0xE103          # ó = o + acute
P_A_GRAVE_RING = 0xE110     # 两级预组合：agrave + ring

# ---- 环表用码点 --------------------------------------------------------
CYCLE_X = 0xE200
CYCLE_Y = 0xE201


def build_demo_table() -> DictCharacterTable:
    """返回演示/测试用的确定性字符表。"""
    return DictCharacterTable(
        decompositions={
            P_A_GRAVE: (A, M_GRAVE),
            P_E_ACUTE: (E, M_ACUTE),
            P_O_ACUTE: (O, M_ACUTE),
            # 多级分解：P_A_GRAVE_RING → P_A_GRAVE → (a, grave)
            P_A_GRAVE_RING: (P_A_GRAVE, M_RING),
        },
        combining_classes={
            M_GRAVE: 230,
            M_ACUTE: 230,
            M_DOTBELOW: 220,
            M_RING: 232,
        },
        compositions={
            (A, M_GRAVE): P_A_GRAVE,
            (E, M_ACUTE): P_E_ACUTE,
            (O, M_ACUTE): P_O_ACUTE,
            # 基字符先被换成 agrave 后继续与 ring(232) 组合
            (P_A_GRAVE, M_RING): P_A_GRAVE_RING,
        },
    )


def build_cyclic_table() -> DictCharacterTable:
    """返回含分解环 X → Y → X 的字符表，用于环检测测试/演示。"""
    return DictCharacterTable(
        decompositions={
            CYCLE_X: (CYCLE_Y,),
            CYCLE_Y: (CYCLE_X,),
        },
        combining_classes={},
        compositions={},
    )


_NAMES = {
    A: "a",
    E: "e",
    O: "o",
    M_GRAVE: "U+E020<grave230>",
    M_ACUTE: "U+E021<acute230>",
    M_DOTBELOW: "U+E022<dotbelow220>",
    M_RING: "U+E023<ring232>",
    P_A_GRAVE: "U+E100<a-grave>",
    P_E_ACUTE: "U+E101<e-acute>",
    P_O_ACUTE: "U+E103<o-acute>",
    P_A_GRAVE_RING: "U+E110<a-grave-ring>",
    CYCLE_X: "U+E200<X>",
    CYCLE_Y: "U+E201<Y>",
}


def describe(codepoints: tuple[int, ...] | list[int]) -> str:
    """把码点序列渲染成可读字符串，供演示与断言信息使用。"""
    return " ".join(_NAMES.get(cp, f"U+{cp:04X}") for cp in codepoints)
