"""按字符位置的插入/删除编辑接口。

在固定字符表上，对**已规范化文本**按码点位置做编辑：

- 编辑前先全量规范化文本（"可以全量规范化后求变化区间"），因此调用方
  传入未规范化文本也会先得到规范结果，再在规范串上施加编辑；
- 删除/插入位置以**规范化后**的规范串码点偏移计（半开区间，Python 风格）；
- 编辑后再次全量规范化，按最长公共前缀/后缀（LCP/LCS）求**变化区间**；
- 逐输出码点追踪来源，给出新旧规范串之间的双向位置映射。

所有计算都是确定性的码点整数变换，无浮点；编辑操作是原子的：任何一步
失败（非法位置、非法类型、插入串或重规范化触发表/上限错误）都抛异常，
编辑器状态保持调用前不变，不留下半更新状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from .engine import (
    DEFAULT_SEGMENT_LIMIT,
    SourcePoint,
    SourceSpan,
    normalize_with_sources,
)
from .errors import InvalidInputError
from .table import CharacterTable
from .validators import validate_positive_int

__all__ = [
    "EditResult",
    "PositionMap",
    "NormalizedEditor",
    "INSERT_CHUNK_INDEX",
    "insert_text",
    "delete_text",
]


@dataclass(frozen=True)
class PositionMap:
    """新旧规范串之间的逐码点位置映射。

    所有偏移均为码点偏移（半开区间用相邻偏移表示）。

    - ``old_to_new``：长度为 ``len(旧规范串)+1``；``old_to_new[i]`` 是旧串
      偏移 ``i``（第 ``i`` 个码点之前的缝）映射到的新串缝偏移。
    - ``new_to_old``：长度为 ``len(新规范串)+1``，方向相反。
    - 变化区间之前的缝恒等映射、之后的缝按长度差平移；落在变化区间内部
      的缝夹到对侧变化区间的边界缝（取较近者，等距归起点）。当变化区间
      在某一侧退化为空长度（纯插入/纯删除）时，两条边界缝在该侧重合成
      同一条缝：文档起点缝（0）固定映射到起点，其余共享边界缝跟随其
      后面的内容（映射到对侧区间**终点**）。因此首尾边界始终对应首尾。
    """

    old_to_new: tuple[int, ...]
    new_to_old: tuple[int, ...]


@dataclass(frozen=True)
class EditResult:
    """一次编辑的结果。

    - ``text``：编辑并重规范化后的规范串；
    - ``codepoints``：与 ``text`` 一一对应的码点元组；
    - ``source_spans``：新规范串相对**当前编辑器文本**（编辑前输入）的来源
      区间集合（被删除的区间不出现在其中；插入内容记为特殊 chunk 下标
      ``INSERT_CHUNK_INDEX``）；
    - ``changed_range``：新规范串中实际发生变化的半开码点区间
      ``(start, end)``——LCP 之后、LCS 之前的部分；无变化时 ``start == end``；
    - ``old_changed_range``：旧规范串中的变化区间
      （删除/重排发生处；插入时可为空区间）；
    - ``position_map``：新旧规范串的双向缝位置映射；
    - ``unchanged_prefix`` / ``unchanged_suffix``：两侧未变化码点数（LCP/LCS）；
    - ``inserted``：本次插入的原文（删除操作为空串）；
    - ``deleted``：本次删除掉的原文片段。
    """

    text: str
    codepoints: tuple[int, ...]
    source_spans: tuple[SourceSpan, ...]
    changed_range: tuple[int, int]
    old_changed_range: tuple[int, int]
    position_map: PositionMap
    unchanged_prefix: int
    unchanged_suffix: int
    inserted: str
    deleted: str


# 插入内容在来源坐标中的 chunk 下标：与真实编辑文本（chunk 0）区分。
INSERT_CHUNK_INDEX = -1


def _check_index(value: object, name: str) -> int:
    """位置参数必须是非负 int（拒绝 bool；拒绝 NaN/Infinity 浮点）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float):
            import math

            if math.isnan(value):
                raise InvalidInputError(f"{name} is NaN")
            if math.isinf(value):
                raise InvalidInputError(f"{name} is Infinity")
        raise InvalidInputError(
            f"{name} must be int, got {type(value).__name__}"
        )
    if value < 0:
        raise InvalidInputError(f"{name} must be non-negative, got {value}")
    return value


def _merge_spans(points: set[SourcePoint]) -> tuple[SourceSpan, ...]:
    """与 engine 相同的"同 chunk 连续点合并"规则，独立复算。"""
    spans: list[SourceSpan] = []
    for p in sorted(points, key=lambda q: (q.chunk_index, q.offset)):
        if (
            spans
            and spans[-1].chunk_index == p.chunk_index
            and spans[-1].end == p.offset
        ):
            prev = spans[-1]
            spans[-1] = SourceSpan(prev.chunk_index, prev.start, p.offset + 1)
        else:
            spans.append(SourceSpan(p.chunk_index, p.offset, p.offset + 1))
    return tuple(spans)


def _build_position_map(
    old_len: int, new_len: int, lcp: int, lcs: int
) -> PositionMap:
    """由变化区间（lcp..old_len-lcs 与 lcp..new_len-lcs）构造双向缝映射。

    区间外的缝一一对应（前缀恒等、后缀按长度差平移）；区间内部的缝夹到
    对侧区间的两条边界缝（取较近者，等距归起点）。当变化区间在某一侧
    退化为空长度（纯插入/纯删除）时，该侧两条边界缝重合成一条共享缝：
    文档起点缝（偏移 0）固定对应起点，其余共享缝跟随其后的内容映射到
    对侧区间终点。首尾边界因此始终对应首尾，映射保序。
    """
    old_start = new_start = lcp
    old_end = old_len - lcs
    new_end = new_len - lcs

    def map_old(i: int) -> int:
        if i < old_start:
            return i
        if i > old_end:
            return i - old_end + new_end
        if old_start == old_end:
            # 旧侧区间为空（纯插入）：共享缝。
            return new_start if i == 0 else new_end
        if i == old_end:
            return new_end
        if i == old_start:
            return new_start
        return new_start if i - old_start <= old_end - i else new_end

    def map_new(j: int) -> int:
        if j < new_start:
            return j
        if j > new_end:
            return j - new_end + old_end
        if new_start == new_end:
            # 新侧区间为空（纯删除）：共享缝。
            return old_start if j == 0 else old_end
        if j == new_end:
            return old_end
        if j == new_start:
            return old_start
        return old_start if j - new_start <= new_end - j else old_end

    old_to_new = tuple(map_old(i) for i in range(old_len + 1))
    new_to_old = tuple(map_new(j) for j in range(new_len + 1))
    return PositionMap(old_to_new, new_to_old)


class NormalizedEditor:
    """持有固定字符表与当前规范串的按位置编辑器。

    用法::

        editor = NormalizedEditor(table)
        editor.set_text(raw_text)          # 可选；也可在构造时传入
        result = editor.insert(2, "ab")    # 在规范串偏移 2 处插入
        result = editor.delete_range(0, 1) # 删除规范串 [0,1)
        editor.text                        # 当前规范串

    ``insert`` / ``delete_range`` 成功后更新内部状态并返回 :class:`EditResult`；
    失败时状态不变。字符表在编辑器生命周期内视为固定（运行内查询有缓存，
    中途外部改表属于未定义用法，与流式 API 一致）。
    """

    def __init__(
        self,
        table: CharacterTable,
        text: str = "",
        *,
        segment_limit: int = DEFAULT_SEGMENT_LIMIT,
    ) -> None:
        if not all(
            callable(getattr(table, name, None))
            for name in ("decomposition", "combining_class", "composition")
        ):
            raise InvalidInputError(
                "table must provide decomposition(cp), combining_class(cp) "
                "and composition(left, right) methods"
            )
        if not isinstance(text, str):
            raise InvalidInputError(
                f"text must be str, got {type(text).__name__}"
            )
        self._table = table
        self._limit = validate_positive_int(segment_limit, "segment_limit")
        self._text = ""
        self._codepoints: tuple[int, ...] = ()
        # 当前规范串每个码点的来源（相对最近一次 set_text 的输入）：
        # [(来源点集合)]；初始为空串。
        self._origin_sets: list[frozenset[SourcePoint]] = []
        # 每次插入分配一个唯一负 chunk 下标，避免多次插入的来源点
        # （各自偏移都从 0 开始）相互冲突被错误合并。
        self._next_insert_index = INSERT_CHUNK_INDEX
        if text:
            self.set_text(text)

    # ---- 属性 ---------------------------------------------------------

    @property
    def text(self) -> str:
        """当前规范串。"""
        return self._text

    @property
    def codepoints(self) -> tuple[int, ...]:
        """当前规范串的码点元组。"""
        return self._codepoints

    @property
    def table(self) -> CharacterTable:
        return self._table

    # ---- 内部规范化 ----------------------------------------------------

    def _normalize(
        self, candidate: str
    ) -> tuple[str, tuple[int, ...], tuple[frozenset[SourcePoint], ...]]:
        """全量规范化候选串，返回 (规范串, 码点, 逐输出来源集合)。

        候选串中 base 部分码点偏移与其在候选串中的偏移一致（删除场景会
        留下两段，偏移不一致，因此调用方需自行改造来源点，见 _apply）。
        """
        result, per_output = normalize_with_sources(
            candidate, self._table, segment_limit=self._limit
        )
        origin_sets = tuple(frozenset(pts) for pts in per_output)
        return result.text, result.codepoints, origin_sets

    # ---- 公开 API -----------------------------------------------------

    def set_text(self, text: str) -> EditResult:
        """把当前文本整体替换为 ``text`` 的规范化结果。

        返回的 :class:`EditResult` 中 ``deleted`` 为旧文本、``inserted`` 为
        ``text`` 原文，变化区间与映射按旧规范串 → 新规范串给出，可用于
        初始化后直接取得坐标映射。原子：``text`` 非法或重规范化失败时
        状态不变。
        """
        if not isinstance(text, str):
            raise InvalidInputError(
                f"text must be str, got {type(text).__name__}"
            )
        old_text, old_cps, old_origins, old_index = self._snapshot()
        try:
            new_text, new_cps, raw_origins = self._normalize(text)
            # set_text 的来源点直接相对传入 text（其在候选串中的偏移即自身
            # 偏移，chunk 0），无需改造；插入计数器重置。
            new_origins = list(raw_origins)
            spans = self._spans_from_origins(new_origins)
            return self._commit(
                old_text,
                old_cps,
                old_origins,
                new_text,
                new_cps,
                new_origins,
                spans,
                INSERT_CHUNK_INDEX,
                inserted=text,
                deleted=old_text,
            )
        except Exception:
            self._restore(old_text, old_cps, old_origins, old_index)
            raise

    def insert(self, pos: int, insertion: str) -> EditResult:
        """在当前规范串的码点偏移 ``pos`` 处插入 ``insertion`` 后重规范化。

        ``0 <= pos <= len(text)``；``pos`` 是码点偏移（不是 UTF-16/字节偏移）。
        插入的内容本身会先参与全量规范化，可能与相邻记号发生重排/组合，
        因此新串变化区间可能超出单纯的插入位置。失败时状态不变。
        """
        pos = _check_index(pos, "pos")
        if not isinstance(insertion, str):
            raise InvalidInputError(
                f"insertion must be str, got {type(insertion).__name__}"
            )
        if pos > len(self._text):
            raise InvalidInputError(
                f"pos {pos} out of range for text of length {len(self._text)}"
            )
        return self._apply(pos, pos, insertion)

    def delete_range(self, start: int, end: int | None = None) -> EditResult:
        """删除当前规范串的半开码点区间 ``[start, end)`` 后重规范化。

        ``end`` 省略时等于 ``start + 1``（删除单个码点）。要求
        ``0 <= start <= end <= len(text)``。删除后相邻内容可能重新组合/重排，
        因此变化区间按重规范化后的 LCP/LCS 重新计算。失败时状态不变。
        """
        start = _check_index(start, "start")
        if end is None:
            end = start + 1
        end = _check_index(end, "end")
        n = len(self._text)
        if start > end:
            raise InvalidInputError(
                f"start ({start}) must be <= end ({end})"
            )
        if end > n:
            raise InvalidInputError(
                f"end {end} out of range for text of length {n}"
            )
        deleted = self._text[start:end]
        return self._apply(start, end, "", deleted_fragment=deleted)

    # ---- 核心：拼接候选串 → 全量规范化 → 对齐 --------------------------

    def _snapshot(self):
        return (
            self._text,
            self._codepoints,
            list(self._origin_sets),
            self._next_insert_index,
        )

    def _restore(self, old_text, old_cps, old_origins, old_index) -> None:
        self._text = old_text
        self._codepoints = old_cps
        self._origin_sets = old_origins
        self._next_insert_index = old_index

    def _apply(
        self,
        start: int,
        end: int,
        insertion: str,
        *,
        deleted_fragment: str | None = None,
    ) -> EditResult:
        old_text, old_cps, old_origins, old_index = self._snapshot()
        insert_index = old_index
        deleted = (
            old_text[start:end] if deleted_fragment is None else deleted_fragment
        )
        try:
            # 候选串 = 旧规范串[:start] + 插入 + 旧规范串[end:]。
            candidate = old_text[:start] + insertion + old_text[end:]

            # 构造候选串每个码点的"编辑前来源"：
            # - 保留段直接沿用旧输出来源（编辑前文本坐标）；
            # - 插入段标注本次插入唯一的负 chunk 下标，偏移为插入串内偏移。
            candidate_sources: list[frozenset[SourcePoint]] = []
            candidate_sources.extend(old_origins[:start])
            for off in range(len(insertion)):
                candidate_sources.append(
                    frozenset({SourcePoint(insert_index, off, off)})
                )
            candidate_sources.extend(old_origins[end:])

            result, per_output = normalize_with_sources(
                candidate, self._table, segment_limit=self._limit
            )

            # 规范化可能重排/组合：用候选串内的 SourcePoint（chunk 0，
            # offset = 候选串偏移）把每个新输出映射回 candidate_sources。
            new_origins: list[frozenset[SourcePoint]] = []
            covered: set[SourcePoint] = set()
            for points in per_output:
                bag: set[SourcePoint] = set()
                for p in points:
                    # 规范化候选串是单 chunk 输入：chunk_index 必为 0。
                    bag.update(candidate_sources[p.offset])
                new_origins.append(frozenset(bag))
                covered.update(bag)

            spans = _merge_spans(covered)
            return self._commit(
                old_text,
                old_cps,
                old_origins,
                result.text,
                result.codepoints,
                new_origins,
                spans,
                # 空插入不产生来源点，不占用新下标。
                insert_index - 1 if insertion else insert_index,
                inserted=insertion,
                deleted=deleted,
            )
        except Exception:
            self._restore(old_text, old_cps, old_origins, old_index)
            raise

    @staticmethod
    def _spans_from_origins(
        origin_sets: list[frozenset[SourcePoint]],
    ):
        covered: set[SourcePoint] = set()
        for bag in origin_sets:
            covered.update(bag)
        return _merge_spans(covered)

    def _commit(
        self,
        old_text: str,
        old_cps: tuple[int, ...],
        old_origins: list[frozenset[SourcePoint]],
        new_text: str,
        new_cps: tuple[int, ...],
        new_origins: list[frozenset[SourcePoint]],
        spans,
        new_index: int,
        *,
        inserted: str,
        deleted: str,
    ) -> EditResult:
        # LCP：保留相同的码点（码点相同即其来源/语义相同）。
        lcp = 0
        max_lcp = min(len(old_cps), len(new_cps))
        while lcp < max_lcp and old_cps[lcp] == new_cps[lcp]:
            lcp += 1
        # LCS：从尾部保留，但不得越过 LCP。
        lcs = 0
        while (
            lcp + lcs < len(old_cps)
            and lcp + lcs < len(new_cps)
            and old_cps[len(old_cps) - 1 - lcs]
            == new_cps[len(new_cps) - 1 - lcs]
        ):
            lcs += 1

        old_changed = (lcp, len(old_cps) - lcs)
        new_changed = (lcp, len(new_cps) - lcs)
        pmap = _build_position_map(len(old_cps), len(new_cps), lcp, lcs)

        # 提交状态。
        self._text = new_text
        self._codepoints = new_cps
        self._origin_sets = new_origins
        self._next_insert_index = new_index

        return EditResult(
            text=new_text,
            codepoints=new_cps,
            source_spans=spans,
            changed_range=new_changed,
            old_changed_range=old_changed,
            position_map=pmap,
            unchanged_prefix=lcp,
            unchanged_suffix=lcs,
            inserted=inserted,
            deleted=deleted,
        )


# ---- 函数式便捷入口（无状态包装） ----------------------------------------


def insert_text(
    text: str,
    pos: int,
    insertion: str,
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> EditResult:
    """一次性接口：在 ``text`` 规范化后的偏移 ``pos`` 处插入并返回结果。

    等价于 ``NormalizedEditor(table, text).insert(pos, insertion)``，
    适合只做一次编辑的场景。
    """
    return NormalizedEditor(
        table, text, segment_limit=segment_limit
    ).insert(pos, insertion)


def delete_text(
    text: str,
    start: int,
    end: int | None,
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> EditResult:
    """一次性接口：删除 ``text`` 规范化后区间 ``[start, end)`` 并返回结果。

    等价于 ``NormalizedEditor(table, text).delete_range(start, end)``。
    """
    return NormalizedEditor(
        table, text, segment_limit=segment_limit
    ).delete_range(start, end)
