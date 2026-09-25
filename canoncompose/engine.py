"""流式规范组合核心。

算法分三步，与题面契约一一对应：

1. **递归分解**：沿字符表的一步分解递归展开到叶子，带 DFS 环检测，
   成环抛 :class:`~canoncompose.errors.DecompositionCycleError`。
2. **稳定 ccc 排序**：一个"规范段"由一个 starter（ccc=0）及其后到下一个
   starter 之前的全部非 starter 组成；非 starter 以稳定插入方式按 ccc
   非降序排列（ccc 相同保持来源先后）。流开头若先出现非 starter，构成
   无 starter 的前导段（只排序、不组合）。
3. **阻塞组合**：段闭合时（遇到下一个 starter 或 ``finish``）执行组合。
   基字符为段首 starter（可被组合结果替换并继续参与后续组合）；
   组合当前非 starter *C* 前，检查其前一"未组合"字符：仅当其 ccc 为 0
   （即 *C* 前面没有任何未组合非 starter）或严格小于 ccc(*C*) 时才允许
   查询 ``table.composition(base, C)``；等 ccc 的未组合字符构成阻塞。

跨 chunk 边界时未闭合的段保留在流状态中，``finish()`` 冲刷最后一段。
单个规范段展开后超过 ``segment_limit``（默认 1024）个码点时拒绝，
抛 :class:`~canoncompose.errors.SegmentLimitError`。

``feed()`` 是原子的：任一码点处理失败时，本次 feed 已造成的流状态变更
全部回滚（段缓冲、已输出缓冲、流偏移、chunk 计数），不留下未说明的
部分变更。

检查点（暂停/恢复）：``export_checkpoint()`` 把流状态导出为带版本的
JSON 兼容 dict——未闭合的规范段原样保留，保存动作本身**不会**隐式
``finish``；``restore_checkpoint()`` 在另一个进程/对象中恢复，恢复前
全量校验格式版本、字段、来源坐标与表指纹，任一不合法都不会产生半恢复
对象。检查点只记录 :class:`~canoncompose.table.DictCharacterTable`
有限规则的内容指纹（规范排序后的 SHA-256），恢复时由调用方传入同内容
的表，指纹不一致即拒绝；其他自定义字符表的正常处理不受影响，只是不
支持这项保存功能。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from .errors import (
    CheckpointError,
    DecompositionCycleError,
    InvalidInputError,
    InvalidTableError,
    SegmentLimitError,
)
from .table import CharacterTable, DictCharacterTable
from .validators import MAX_CODEPOINT, validate_chunk, validate_positive_int

__all__ = [
    "SourcePoint",
    "SourceSpan",
    "NormalizationResult",
    "StreamNormalizer",
    "normalize",
    "DEFAULT_SEGMENT_LIMIT",
    "CHECKPOINT_FORMAT",
    "CHECKPOINT_VERSION",
]

DEFAULT_SEGMENT_LIMIT = 1024

# 检查点格式标识与版本：不认识即拒绝恢复。
CHECKPOINT_FORMAT = "canoncompose.checkpoint"
CHECKPOINT_VERSION = 1


@dataclass(frozen=True)
class SourcePoint:
    """单个输入码点的来源位置。"""

    chunk_index: int
    offset: int
    stream_offset: int


@dataclass(frozen=True)
class SourceSpan:
    """同一 chunk 内连续来源码点合并出的半开区间 ``[start, end)``。"""

    chunk_index: int
    start: int
    end: int


@dataclass(frozen=True)
class NormalizationResult:
    """规范化结果。

    - ``text``：规范串；
    - ``codepoints``：规范串的码点元组（与 ``text`` 一一对应）；
    - ``source_spans``：全部输出码点所覆盖的原输入来源区间集合
      （按 chunk、偏移有序，半开区间，相邻点已合并）。
    """

    text: str
    codepoints: tuple[int, ...]
    source_spans: tuple[SourceSpan, ...]

    def __str__(self) -> str:
        return self.text


# 内部条目：(码点, 来源点元组)，不可变，便于原子回滚。
_Entry = tuple[int, tuple[SourcePoint, ...]]

# 迭代 DFS 的栈帧：[码点, 一步子节点(None=未查询), 下一子节点下标, 累加叶子]
_Frame = list


def _validate_table_codepoint(value: object, *, owner: int) -> int:
    """校验表返回的码点值，错误归类为 InvalidTableError。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTableError(
            f"codepoint must be int, got {type(value).__name__}",
            codepoint=owner,
        )
    if value < 0 or value > MAX_CODEPOINT:
        raise InvalidTableError(
            f"codepoint out of range 0..0x{MAX_CODEPOINT:X}, got U+{value:X}",
            codepoint=owner,
        )
    return value


def _cycle_chain(stack: list[_Frame], repeated: int) -> tuple[int, ...]:
    """从 DFS 栈截取闭合环：repeated 首次出现处到栈顶，再闭合到 repeated。"""
    start = next(i for i, frame in enumerate(stack) if frame[0] == repeated)
    chain = [frame[0] for frame in stack[start:]]
    chain.append(repeated)
    return tuple(chain)


# ---- 检查点序列化/校验辅助 ------------------------------------------------

#: restore 校验时的缺字段哨兵。
_MISSING = object()


def _entry_to_json(entry: _Entry) -> list:
    """(码点, 来源点元组) → JSON 兼容列表（全新对象，不与流共享）。"""
    cp, origins = entry
    return [cp, [[p.chunk_index, p.offset, p.stream_offset] for p in origins]]


def _ck_field(data: dict, key: str) -> object:
    """取检查点必含字段，缺失抛 CheckpointError。"""
    value = data.get(key, _MISSING)
    if value is _MISSING:
        raise CheckpointError(f"checkpoint missing required field {key!r}")
    return value


def _ck_int(data: dict, key: str, *, minimum: int) -> int:
    """校验检查点整数字段（拒绝 bool），返回 int。"""
    value = _ck_field(data, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CheckpointError(
            f"checkpoint field {key!r} must be int, got {type(value).__name__}"
        )
    if value < minimum:
        raise CheckpointError(
            f"checkpoint field {key!r} must be >= {minimum}, got {value}"
        )
    return value


def _ck_entries(
    raw: object,
    *,
    what: str,
    chunk_count: int,
    stream_pos: int,
) -> list[_Entry]:
    """解析并校验检查点条目列表，返回新的 (码点, 来源点元组) 列表。

    来源坐标合法性：三个分量都是非负 int（拒绝 bool）；
    ``chunk_index < chunk_count`` 且 ``stream_offset < stream_pos``；
    每条目的来源点非空（同一来源点可因"分解后又组合回来"重复出现，
    属正常）。
    """
    if not isinstance(raw, list):
        raise CheckpointError(f"checkpoint field {what!r} must be a list")
    entries: list[_Entry] = []
    for index, item in enumerate(raw):
        where = f"{what}[{index}]"
        if not isinstance(item, list) or len(item) != 2:
            raise CheckpointError(
                f"checkpoint entry {where} must be [codepoint, origins]"
            )
        cp, raw_origins = item
        if (
            isinstance(cp, bool)
            or not isinstance(cp, int)
            or cp < 0
            or cp > MAX_CODEPOINT
        ):
            raise CheckpointError(
                f"checkpoint entry {where} has invalid codepoint {cp!r}"
            )
        if not isinstance(raw_origins, list) or not raw_origins:
            raise CheckpointError(
                f"checkpoint entry {where} must have a non-empty origins list"
            )
        origins: list[SourcePoint] = []
        for raw_point in raw_origins:
            if not isinstance(raw_point, list) or len(raw_point) != 3:
                raise CheckpointError(
                    f"checkpoint origin in {where} must be "
                    "[chunk_index, offset, stream_offset]"
                )
            chunk_index, offset, stream_offset = raw_point
            for name, value in (
                ("chunk_index", chunk_index),
                ("offset", offset),
                ("stream_offset", stream_offset),
            ):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise CheckpointError(
                        f"checkpoint origin {name} in {where} must be int, "
                        f"got {type(value).__name__}"
                    )
                if value < 0:
                    raise CheckpointError(
                        f"checkpoint origin {name} in {where} must be "
                        f"non-negative, got {value}"
                    )
            if chunk_index >= chunk_count:
                raise CheckpointError(
                    f"checkpoint origin chunk_index {chunk_index} in {where} "
                    f"is out of range for chunk_count {chunk_count}"
                )
            if stream_offset >= stream_pos:
                raise CheckpointError(
                    f"checkpoint origin stream_offset {stream_offset} in "
                    f"{where} is out of range for stream_pos {stream_pos}"
                )
            point = SourcePoint(chunk_index, offset, stream_offset)
            origins.append(point)
        entries.append((cp, tuple(origins)))
    return entries


def _ck_source_consistency(
    seg: list[_Entry],
    out: list[_Entry],
    *,
    stream_pos: int,
) -> None:
    """校验来源坐标的全局一致性。

    引擎为第 *i* 个输入码点分配 ``stream_offset=i``，并对应唯一的
    ``(chunk_index, offset)``（按 chunk 顺序、chunk 内从 0 递增）；
    分解使同一来源点重复出现，组合只合并来源点。因此全部条目的
    stream_offset 集合必须恰好覆盖 ``0..stream_pos-1``，且按
    stream_offset 展开的 ``(chunk_index, offset)`` 序列必须等于
    "逐 chunk、chunk 内 offset 从 0 连续（空 chunk 可缺席）"的枚举序。
    """
    points = [p for _, origins in (*seg, *out) for p in origins]
    owner: dict[int, tuple[int, int]] = {}
    for p in points:
        key = (p.chunk_index, p.offset)
        if owner.setdefault(p.stream_offset, key) != key:
            raise CheckpointError(
                f"checkpoint stream_offset {p.stream_offset} maps to "
                "inconsistent (chunk_index, offset) pairs"
            )
    # 先做廉价的长度判断，避免 stream_pos 被伪造为天文数字时构造
    # range(大数)：长度不等就直接拒绝；相等时检查点本身已经包含同样多
    # 的来源点，代价不超过解析它的代价。
    if len(owner) != stream_pos or set(owner) != set(range(stream_pos)):
        raise CheckpointError(
            "checkpoint source stream_offsets do not cover "
            f"0..{stream_pos - 1} exactly"
        )
    # 每个出现过的 chunk 的长度（最大 offset + 1）。
    lengths: dict[int, int] = {}
    for chunk_index, offset in owner.values():
        if offset >= lengths.get(chunk_index, 0):
            lengths[chunk_index] = offset + 1
    expected = [
        (chunk_index, offset)
        for chunk_index in sorted(lengths)
        for offset in range(lengths[chunk_index])
    ]
    actual = [owner[so] for so in range(stream_pos)]
    if actual != expected:
        raise CheckpointError(
            "checkpoint source coordinates do not follow the per-chunk "
            "enumeration order"
        )


def _ck_segment_shape(
    seg: list[_Entry], table: DictCharacterTable, limit: int
) -> None:
    """校验未闭合段的结构不变式（用指纹已匹配的表查询 ccc）。

    段首可以是 starter 或前导非 starter；段首之后不得再出现 starter；
    若段首是非 starter，则整段非 starter 必须按 ccc 非降序（引擎稳定
    插入维持此序）。
    """
    if len(seg) > limit:
        raise CheckpointError(
            f"checkpoint pending segment length {len(seg)} exceeds "
            f"segment_limit {limit}"
        )
    head_ccc = table.combining_class(seg[0][0]) if seg else None
    prev_ccc: int | None = None
    for index, (cp, _) in enumerate(seg):
        ccc = table.combining_class(cp)
        if index == 0:
            continue
        if ccc == 0:
            raise CheckpointError(
                "checkpoint pending segment has a starter after the "
                "segment head"
            )
        # 段首是前导非 starter 时，它也参与非降序约束。
        base_ccc = prev_ccc if prev_ccc is not None else head_ccc
        if base_ccc is not None and ccc < base_ccc:
            raise CheckpointError(
                "checkpoint pending segment is not in non-decreasing ccc order"
            )
        prev_ccc = ccc


class StreamNormalizer:
    """有状态的流式规范组合器。

    用法::

        sn = StreamNormalizer(table)
        sn.feed(chunk1)
        sn.feed(chunk2)
        result = sn.finish()

    表方法返回值在首次使用时校验；非法返回值抛
    :class:`~canoncompose.errors.InvalidTableError`。
    """

    def __init__(
        self,
        table: CharacterTable,
        *,
        segment_limit: int = DEFAULT_SEGMENT_LIMIT,
    ) -> None:
        self._check_table(table)
        self._table = table
        self._limit = validate_positive_int(segment_limit, "segment_limit")

        # 已验证无环的叶子展开缓存：cp -> 递归叶子码点元组。
        self._expand_cache: dict[int, tuple[int, ...]] = {}
        # ccc 查询结果缓存（同时保证同一次运行内表视图一致）。
        self._ccc_cache: dict[int, int] = {}

        self._seg: list[_Entry] = []
        self._out: list[_Entry] = []
        self._stream_pos = 0
        self._chunk_count = 0
        self._finished = False
        self._result: NormalizationResult | None = None

    @staticmethod
    def _check_table(table: object) -> None:
        if not all(
            callable(getattr(table, name, None))
            for name in ("decomposition", "combining_class", "composition")
        ):
            raise InvalidInputError(
                "table must provide decomposition(cp), combining_class(cp) "
                "and composition(left, right) methods"
            )

    # ---- 表查询与校验 -------------------------------------------------

    def _decomp_one(self, cp: int) -> tuple[int, ...] | None:
        raw = self._table.decomposition(cp)
        if raw is None:
            return None
        if not isinstance(raw, tuple) or len(raw) == 0:
            raise InvalidTableError(
                "decomposition() must return None or a non-empty tuple of "
                f"codepoints, got {raw!r}",
                codepoint=cp,
            )
        return tuple(_validate_table_codepoint(x, owner=cp) for x in raw)

    def _ccc(self, cp: int) -> int:
        cached = self._ccc_cache.get(cp)
        if cached is not None:
            return cached
        raw = self._table.combining_class(cp)
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise InvalidTableError(
                "combining_class() must return int, got "
                f"{type(raw).__name__}",
                codepoint=cp,
            )
        if raw < 0:
            raise InvalidTableError(
                f"combining_class() must be non-negative, got {raw}",
                codepoint=cp,
            )
        self._ccc_cache[cp] = raw
        return raw

    def _compose_one(self, left: int, right: int) -> int | None:
        raw = self._table.composition(left, right)
        if raw is None:
            return None
        return _validate_table_codepoint(raw, owner=left)

    # ---- 递归分解（迭代 DFS，局部环检测 + 已验证子图缓存） --------------

    def _expand(self, root: int) -> tuple[int, ...]:
        """返回 root 递归分解后的叶子码点元组（按分解顺序）。"""
        cached = self._expand_cache.get(root)
        if cached is not None:
            return cached

        stack: list[_Frame] = [[root, None, 0, []]]
        on_stack: set[int] = {root}

        while stack:
            frame = stack[-1]
            node = frame[0]

            if frame[1] is None:
                children = self._decomp_one(node)
                frame[1] = children
                if children is None:
                    # 叶子节点。
                    self._expand_cache[node] = (node,)
                    on_stack.discard(node)
                    stack.pop()
                    if stack:
                        stack[-1][3].append(node)
                    else:
                        return (node,)
                    continue

            children: tuple[int, ...] = frame[1]
            if frame[2] < len(children):
                child = children[frame[2]]
                frame[2] += 1
                if child in on_stack:
                    raise DecompositionCycleError(_cycle_chain(stack, child))
                cached_child = self._expand_cache.get(child)
                if cached_child is not None:
                    # 已验证无环的子图，直接并入。
                    frame[3].extend(cached_child)
                else:
                    on_stack.add(child)
                    stack.append([child, None, 0, []])
            else:
                expanded = tuple(frame[3])
                self._expand_cache[node] = expanded
                on_stack.discard(node)
                stack.pop()
                if stack:
                    stack[-1][3].extend(expanded)
                else:
                    return expanded

        # 不可达：循环仅在栈空时结束，而栈空时各分支均已 return。
        raise RuntimeError("unreachable expansion state")  # pragma: no cover

    # ---- 段维护 -------------------------------------------------------

    def _append_leaf(
        self,
        cp: int,
        origins: tuple[SourcePoint, ...],
        loc: SourcePoint,
    ) -> list[_Entry]:
        """把一个分解叶子加入当前段，返回因段闭合而冲刷出的输出条目。"""
        flushed: list[_Entry] = []
        ccc = self._ccc(cp)
        entry: _Entry = (cp, origins)

        if ccc == 0:
            if self._seg:
                flushed = self._compose_segment(self._seg)
            self._seg = [entry]
            return flushed

        if not self._seg:
            # 流开头的前导非 starter：自成前导段。
            self._seg = [entry]
            return flushed

        if len(self._seg) >= self._limit:
            raise SegmentLimitError(
                "canonical segment exceeds codepoint limit",
                limit=self._limit,
                actual=len(self._seg) + 1,
                chunk_index=loc.chunk_index,
                offset=loc.offset,
                stream_offset=loc.stream_offset,
                codepoint=cp,
            )

        # 稳定插入：越过 ccc 严格更大的既有条目，等 ccc 留在其后。
        pos = len(self._seg)
        while pos > 0 and self._ccc(self._seg[pos - 1][0]) > ccc:
            pos -= 1
        self._seg.insert(pos, entry)
        return flushed

    def _compose_segment(self, entries: list[_Entry]) -> list[_Entry]:
        """段闭合时的阻塞组合，返回组合后的输出条目。"""
        if not entries:
            return []
        if self._ccc(entries[0][0]) != 0:
            # 前导非 starter 段：没有基字符，不组合。
            return list(entries)

        base: _Entry = entries[0]
        pending: list[_Entry] = []
        for entry in entries[1:]:
            cur_ccc = self._ccc(entry[0])
            # 前一"未组合"字符：pending 为空时视为段首 starter（ccc=0）。
            prev_ccc = 0 if not pending else self._ccc(pending[-1][0])
            composed = None
            if prev_ccc == 0 or prev_ccc < cur_ccc:
                composed = self._compose_one(base[0], entry[0])
            if composed is not None:
                # 组合结果占据基字符位置，继续参与后续组合，ccc 视为 0。
                base = (composed, base[1] + entry[1])
            else:
                pending.append(entry)
        return [base, *pending]

    # ---- 公开 API -----------------------------------------------------

    def feed(self, chunk: str) -> None:
        """喂入一个 chunk（str）。原子：失败时流状态回到调用前。"""
        if self._finished:
            raise InvalidInputError("stream already finished")
        validate_chunk(chunk, self._chunk_count)

        # 快照全部可变流状态（展开/ccc 缓存只存已验证结果，属纯增益）。
        backup_seg = list(self._seg)
        backup_out_len = len(self._out)
        backup_pos = self._stream_pos
        backup_chunk_count = self._chunk_count

        try:
            chunk_index = self._chunk_count
            self._chunk_count += 1
            for offset, ch in enumerate(chunk):
                point = SourcePoint(chunk_index, offset, self._stream_pos)
                self._stream_pos += 1
                origins = (point,)
                for leaf in self._expand(ord(ch)):
                    flushed = self._append_leaf(leaf, origins, point)
                    if flushed:
                        self._out.extend(flushed)
        except Exception:
            self._seg = backup_seg
            del self._out[backup_out_len:]
            self._stream_pos = backup_pos
            self._chunk_count = backup_chunk_count
            raise

    def finish(self) -> NormalizationResult:
        """冲刷最后一段并返回完整结果。可重复调用，返回同一结果。"""
        if self._finished:
            assert self._result is not None
            return self._result
        if self._seg:
            self._out.extend(self._compose_segment(self._seg))
            self._seg = []
        self._result = self._build_result()
        self._finished = True
        return self._result

    def _build_result(self) -> NormalizationResult:
        codepoints = tuple(cp for cp, _ in self._out)
        text = "".join(chr(cp) for cp in codepoints)

        points: set[SourcePoint] = set()
        for _, origins in self._out:
            points.update(origins)
        ordered = sorted(points, key=lambda p: (p.chunk_index, p.offset))

        spans: list[SourceSpan] = []
        for p in ordered:
            if (
                spans
                and spans[-1].chunk_index == p.chunk_index
                and spans[-1].end == p.offset
            ):
                prev = spans[-1]
                spans[-1] = SourceSpan(prev.chunk_index, prev.start, p.offset + 1)
            else:
                spans.append(SourceSpan(p.chunk_index, p.offset, p.offset + 1))
        return NormalizationResult(text, codepoints, tuple(spans))

    # ---- 检查点（暂停/恢复） ---------------------------------------------

    def export_checkpoint(self) -> dict:
        """导出检查点：带版本的 JSON 兼容 dict（``json.dumps`` 可直接序列化）。

        - 只读操作：**不会**隐式 ``finish``，未闭合的规范段原样保留在
          ``pending_segment`` 中，活对象状态不受任何影响；
        - 已 ``finish`` 的状态也允许导出，恢复后保持结束语义；
        - 返回结构是全新对象，调用方改动不会反过来影响本对象；
        - 仅支持精确的 :class:`DictCharacterTable`（不含覆盖方法的子类，
          因为指纹只覆盖有限规则内容，无法代表被覆盖的行为）；其他自定义
          表的正常处理不受影响，只是不支持本保存功能，调用抛
          :class:`~canoncompose.errors.CheckpointError`。

        检查点不保存展开/ccc 缓存：它们是纯派生数据，恢复后按同内容表
        惰性重建，语义不变。
        """
        table = self._table
        if type(table) is not DictCharacterTable:
            raise CheckpointError(
                "checkpoint export requires an exact DictCharacterTable, "
                f"got {type(table).__name__}"
            )
        return {
            "format": CHECKPOINT_FORMAT,
            "version": CHECKPOINT_VERSION,
            "segment_limit": self._limit,
            "table_fingerprint": table.fingerprint(),
            "stream_pos": self._stream_pos,
            "chunk_count": self._chunk_count,
            "finished": self._finished,
            "pending_segment": [_entry_to_json(entry) for entry in self._seg],
            "output": [_entry_to_json(entry) for entry in self._out],
        }

    @classmethod
    def restore_checkpoint(
        cls, checkpoint: dict, table: CharacterTable
    ) -> StreamNormalizer:
        """从检查点恢复流，继续 ``feed`` 剩余 chunk 或直接 ``finish``。

        ``checkpoint`` 是 ``export_checkpoint()`` 导出的（可经
        ``json.dumps``/``json.loads`` 往返的）dict；``table`` 由调用方
        传入，必须是与导出时**同内容**的精确
        :class:`DictCharacterTable`——指纹不一致即拒绝。

        恢复是全有或全无的：格式版本不认识、必含字段缺失/类型非法、
        来源坐标不合法、段结构违反不变式或表不匹配时抛
        :class:`~canoncompose.errors.CheckpointError`，不会产生半恢复
        对象。恢复后 chunk 编号、流偏移从检查点继续，结果与从头连续
        处理一致。
        """
        if not isinstance(checkpoint, dict):
            raise CheckpointError(
                f"checkpoint must be a dict, got {type(checkpoint).__name__}"
            )
        fmt = _ck_field(checkpoint, "format")
        if fmt != CHECKPOINT_FORMAT:
            raise CheckpointError(f"unrecognized checkpoint format {fmt!r}")
        version = _ck_field(checkpoint, "version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != CHECKPOINT_VERSION
        ):
            raise CheckpointError(
                f"unsupported checkpoint version {version!r}"
            )
        if type(table) is not DictCharacterTable:
            raise CheckpointError(
                "checkpoint restore requires an exact DictCharacterTable, "
                f"got {type(table).__name__}"
            )
        expected_fingerprint = _ck_field(checkpoint, "table_fingerprint")
        if not isinstance(expected_fingerprint, str):
            raise CheckpointError(
                "checkpoint field 'table_fingerprint' must be str, got "
                f"{type(expected_fingerprint).__name__}"
            )
        if table.fingerprint() != expected_fingerprint:
            raise CheckpointError(
                "table fingerprint mismatch: the checkpoint was exported "
                "with a different character table"
            )

        limit = _ck_int(checkpoint, "segment_limit", minimum=1)
        stream_pos = _ck_int(checkpoint, "stream_pos", minimum=0)
        chunk_count = _ck_int(checkpoint, "chunk_count", minimum=0)
        finished = _ck_field(checkpoint, "finished")
        if not isinstance(finished, bool):
            raise CheckpointError(
                f"checkpoint field 'finished' must be bool, got "
                f"{type(finished).__name__}"
            )
        seg = _ck_entries(
            _ck_field(checkpoint, "pending_segment"),
            what="pending_segment",
            chunk_count=chunk_count,
            stream_pos=stream_pos,
        )
        out = _ck_entries(
            _ck_field(checkpoint, "output"),
            what="output",
            chunk_count=chunk_count,
            stream_pos=stream_pos,
        )
        if finished and seg:
            raise CheckpointError(
                "finished checkpoint must have an empty pending segment"
            )
        _ck_source_consistency(seg, out, stream_pos=stream_pos)
        _ck_segment_shape(seg, table, limit)

        # 全部校验通过后才构造对象：失败路径不会留下半恢复对象。
        stream = cls(table, segment_limit=limit)
        stream._seg = seg
        stream._out = out
        stream._stream_pos = stream_pos
        stream._chunk_count = chunk_count
        if finished:
            # 结束语义：结果由输出缓冲确定性重建，finish 幂等、feed 拒绝。
            stream._result = stream._build_result()
            stream._finished = True
        return stream


def normalize(
    chunks: Iterable[str],
    table: CharacterTable,
    *,
    segment_limit: int = DEFAULT_SEGMENT_LIMIT,
) -> NormalizationResult:
    """一次性规范化一个 chunk 序列。

    ``chunks`` 是 str 的可迭代对象（不得直接传单个 str）；处理等价于按序
    ``StreamNormalizer.feed`` 后 ``finish``。任何一步抛异常都不会返回部分
    结果（流对象为函数内部所有，随异常丢弃）。
    """
    if isinstance(chunks, str):
        raise InvalidInputError(
            "chunks must be an iterable of str, not a single str"
        )
    try:
        iterator: Iterator[str] = iter(chunks)
    except TypeError as exc:
        raise InvalidInputError(
            f"chunks must be an iterable of str, got {type(chunks).__name__}"
        ) from exc

    stream = StreamNormalizer(table, segment_limit=segment_limit)
    for chunk_index, chunk in enumerate(iterator):
        validate_chunk(chunk, chunk_index)
        stream.feed(chunk)
    return stream.finish()
