# canoncompose — 显式字符表的流式规范组合

离线文本基础设施可复用模块：在**调用方显式提供的有限字符表**上执行
递归分解 → canonical combining class（ccc）稳定排序 → 阻塞二元组合，
支持跨 chunk 流式处理、来源区间追踪与段长度上限拒绝；并提供在规范文本上
**按码点位置插入/删除、重规范化并输出变化区间与新旧来源映射**的编辑接口。

**不读取、不引用系统 Unicode 数据库，不绑定任何 Unicode 版本。**
本模块只实现本题契约描述的有限算法子集，**不宣称与 Unicode
NFC/NFD 等行业标准完整兼容**。

## 运行环境

- Python **3.14.7**（仅标准库；更低的 3.10+ 大概率可用但未验收）
- Linux 原生运行，**无需安装任何第三方依赖**，无需网络/账号/数据库/
  Docker 等。

## 准确命令

```bash
# 必要测试（83 个：原规范化 44 + 编辑接口 39）
python -m unittest discover -s tests -v

# 固定输入演示（现场计算，约 2 ms，远小于 90 秒预算；无 sleep、无预录）
python demo.py
```

两条命令均在仓库根目录执行。

## 公开接口

```python
from canoncompose import (
    normalize,            # 一次性 API
    StreamNormalizer,     # 流式 API：feed / finish
    NormalizedEditor,     # 按位置编辑：insert / delete_range / set_text
    EditResult, PositionMap, INSERT_CHUNK_INDEX,  # 编辑返回结构
    insert_text, delete_text,                      # 编辑的一次性便捷函数
    DictCharacterTable,   # 字典字符表
    CharacterTable,       # 字符表协议抽象基类（也可直接鸭子类型）
    NormalizationResult, SourcePoint, SourceSpan,
    CanonicalError, InvalidInputError, InvalidTableError,
    DecompositionCycleError, SegmentLimitError,
)
```

### 字符表协议

表对象提供三个方法，返回值会被逐一校验：

| 方法 | 语义 |
|---|---|
| `decomposition(cp) -> None \| tuple[int, ...]` | **一步**分解；`None` 表示无分解；否则非空码点元组。核心负责递归展开与环检测。 |
| `combining_class(cp) -> int` | ccc 非负整数；`0` 为 starter。未列出的码点惯例返回 `0`。 |
| `composition(left, right) -> int \| None` | 二元组合映射；返回码点才组合，`None` 不组合。是否调用还受阻塞规则约束。 |

码点为 `0..0x10FFFF` 的 `int`。

### 一次性 API

```python
result = normalize(chunks, table, *, segment_limit=1024)
```

- `chunks`：`str` 的**可迭代对象**（`list[str]`、生成器等）；直接传单个
  `str` 会被拒绝（防止把字符串误当字符序列）。
- 返回 `NormalizationResult`：
  - `text: str` 规范串；
  - `codepoints: tuple[int, ...]` 与 `text` 一一对应的码点元组；
  - `source_spans: tuple[SourceSpan, ...]` 输出所覆盖的**原输入来源区间
    集合**：每个 `SourceSpan(chunk_index, start, end)` 是同一 chunk 内
    半开区间 `[start, end)`，按 `(chunk_index, start)` 有序，连续点已
    合并。组合产生的码点其来源为被组合各码点来源的并集。

### 流式 API

```python
sn = StreamNormalizer(table, segment_limit=1024)
sn.feed(chunk1)   # 可多次调用
sn.feed(chunk2)
result = sn.finish()   # 冲刷最后一段；重复调用返回同一结果
```

starter 之后的非 starter 可能与**后续 chunk** 内容重排/组合，因此未闭合
的"规范段"保留在流内部，直到遇到下一个 starter 或 `finish()` 才输出。

### 按位置编辑 API（新增）

在**规范文本**上按码点位置插入/删除；沿用固定字符表，做法就是题面允许的
"全量规范化后求变化区间"：编辑前先规范化（构造时/`set_text` 时），把编辑
施加在规范串上，编辑后把候选串**全量重新规范化**，再用最长公共前缀
（LCP）与最长公共后缀（LCS）确定变化区间并逐输出追踪来源。

```python
editor = NormalizedEditor(table, text="", *, segment_limit=1024)
result = editor.insert(pos, insertion)      # 在规范串码点偏移 pos 处插入
result = editor.delete_range(start, end=None)  # 删除半开区间 [start,end)，end 缺省=start+1
result = editor.set_text(text)              # 整体替换（同样返回映射）
editor.text          # 当前规范串（str）
editor.codepoints    # 当前规范串码点元组

# 一次性便捷入口（无状态，内部等价于构造后单次编辑）：
insert_text(text, pos, insertion, table)
delete_text(text, start, end, table)        # end 可传 None
```

- 位置全部是**码点偏移**（不是字节/UTF-16 偏移），半开区间，Python 切片
  风格；`pos` 可等于文本长度（尾缝）。插入内容自身会参与全量规范化，因此
  插入的组合字符可能与相邻基字符/记号重排或组合，新串变化区间可能超出
  单纯的插入点；删除后两侧内容也可能重新组合，区间同样按重规范化后的
  LCP/LCS 重新计算。
- 返回 `EditResult`：

| 字段 | 含义 |
|---|---|
| `text` / `codepoints` | 编辑并重规范化后的规范串与其码点元组 |
| `changed_range` | **新**规范串中实际变化的半开区间 `(start, end)`（LCP 之后、LCS 之前）；无变化时 `start == end` |
| `old_changed_range` | **旧**规范串中的变化区间（删除处；纯插入时为空区间） |
| `unchanged_prefix` / `unchanged_suffix` | 两侧未变化码点数（即 LCP、LCS） |
| `position_map` | `PositionMap(old_to_new, new_to_old)`：新旧规范串之间的双向**缝位置**映射，长度分别为旧/新串长度 +1 |
| `source_spans` | 新规范串的来源区间集合：相对最近一次 `set_text`/构造输入的点记为 `chunk 0`；每次插入分别记为唯一负 chunk 下标（首次为 `INSERT_CHUNK_INDEX = -1`，再次为 `-2`、`-3`…），偏移是该次插入串内码点偏移；被删除的点不出现 |
| `inserted` / `deleted` | 本次插入的原文 / 被删除的原规范片段（`str`） |

**位置映射约定**：变化区间之前的缝恒等映射、之后的缝按长度差平移；落在
变化区间内部的缝夹到对侧区间两条边界缝中较近者（等距归起点）。当变化
区间在某一侧退化为空长度（纯插入/纯删除）时，两条边界缝在该侧重合成
一条共享缝：文档起点缝（偏移 0）固定锚到起点，其余共享缝跟随其后内容
锚到对侧区间终点。映射保序、首尾边界相互锚定；变化区之外的缝往返恒等。
一侧文档被整体删空时，该侧唯一的缝（首=尾）锚定对侧文档原点 0。

**原子性**：`insert` / `delete_range` / `set_text` 任何一步失败（非法位置、
类型错误、插入串触发表环/非法返回值/超段上限）都直接抛异常，编辑器的
文本、码点与来源状态保持调用前不变，可继续使用，不留下半更新状态。

## 算法与阻塞规则（契约）

1. **递归分解**：沿一步分解迭代 DFS 展开到叶子；检测到回到当前 DFS
   路径上节点的边即判定成环，抛 `DecompositionCycleError`（异常的
   `chain` 字段给出闭合路径）。已验证无环的子树结果缓存复用。
2. **稳定 ccc 排序**：段 = 一个 starter（ccc=0）及其后到下一 starter
   前的非 starter；非 starter 按 ccc **非降序稳定排列**（等 ccc 保持来源
   先后）。流开头先出现的非 starter 构成无 starter 的前导段：只排序，
   永不组合。
3. **阻塞组合**（段闭合时）：段首 starter 为基字符，可被组合结果替换并
   继续参与后续组合。对当前非 starter *C*，取其**前一未组合字符**
   （pending 列表末尾；没有则视为段首 starter，ccc=0）：仅当其 ccc 为
   `0` **或严格小于** ccc(*C*) 时才查询 `composition(base, C)`；
   **等 ccc 的未组合字符构成阻塞**。已组合进基字符的记号不属于"未组合
   字符"。

### 幂等与分块一致

- 规范串再次规范化保持不变（前提：表是固定的；示例数据满足）。
- 同一输入无论在何处切分成 chunk，输出码点序列一致（来源区间坐标本身
  依赖 chunk 划分，这是定义使然）。

## 输入上限

| 边界 | 值 | 超限行为 |
|---|---|---|
| 单个规范段展开后码点数 | **1024**（`segment_limit` 可调小，不可 ≤0） | 抛 `SegmentLimitError`（含 `limit`/`actual` 与定位） |
| 码点范围 | `0..0x10FFFF` | `InvalidInputError` / `InvalidTableError` |
| chunk 大小、chunk 数量、流总长度 | 无额外限制（受可用内存约束） | — |

1024 是**接口边界**；段在遇到下一个 starter 时重新计数。

## 错误语义

- 统一基类 `CanonicalError`；输入问题 `InvalidInputError`（`ValueError`
  子类），表返回值问题 `InvalidTableError`，环 `DecompositionCycleError`
  （`InvalidTableError` 子类），超段上限 `SegmentLimitError`。
- 错误携带可定位字段：`chunk_index`、chunk 内 `offset`、流上
  `stream_offset`、`codepoint`。
- **数值输入拒绝 NaN/Infinity；整数参数拒绝 `bool`**（`True`/`False`
  虽为 `int` 子类也拒绝）；非 str chunk、非可迭代 chunks、缺方法的表
  同样拒绝。非法输入不静默纠正。
- **原子性**：`feed()` 中途任一步失败，本次 feed 对段缓冲、已输出缓冲、
  流偏移、chunk 计数的修改全部回滚，流恢复到调用前状态，可继续使用；
  `normalize()` 不返回部分结果（成功或抛异常）。编辑接口
  （`insert`/`delete_range`/`set_text`）同样原子：失败时编辑器文本、码点
  与来源状态完全不变；位置参数（`pos`/`start`/`end`）必须是非负 `int`，
  拒绝 `bool` 与 `NaN`/`Infinity` 浮点，越界（`pos > len(text)`、
  `end > len(text)`、`start > end`）抛 `InvalidInputError`，插入串必须是
  `str`。
- 本算法**没有搜索预算/无解证明**之类概念（确定性结构变换），因此不
  存在"预算耗尽 vs 无解"的区分需求；能失败的情形都是显式非法输入/表/
  上限，错误类型如上分开。

## 误差口径

本库全部运算为码点整数的分解、整数 ccc 比较与字典查表，**没有任何浮点
运算或浮点结果**，因此不存在容差/误差口径，也没有用宽容差掩盖边界的
空间。

## 设计取舍

- 表只给**一步**分解：递归、环检测、展开缓存由核心负责，表实现最简。
- 排序采用"插入时稳定定位"而非段末整体排序，使跨 chunk 挂起段无需
  缓存排序键以外的额外结构；两者在稳定序下等价。
- 组合发生在**段闭合**时（下一个 starter 或 finish），保证跨块记号有
  机会先完成 ccc 重排；代价是输出有一段延迟，这正是流式规范算法的
  固有特征。
- 来源区间按"输出码点 → 输入来源点并集 → 同 chunk 连续点合并"计算，
  语义直观；不尝试输出字符级的多层 provenance 树。
- 表查询结果（分解叶子、ccc）在单次运行内缓存：既加速重复码点，也
  保证运行内表视图一致；表在运行中途被外部修改属于未定义用法。

## 已知限制

- 只实现本题契约的子集：不做兼容分解、重排（reordering 之外的
  normalization 形式）、singleton 分解、quick-check、流控等，不保证
  接受任意 Unicode 表后与 NFC/NFKC 一致。
- 不校验组合结果自身的 ccc 或分解一致性（表自洽性由调用方负责）。
- 来源区间以输入 chunk 的码点偏移表示；surrogate/编码层问题不存在
  （输入是 Python `str`，按码点遍历）。
- 超长单段（如单个分解出 1025+ 叶子的码点）直接拒绝，不做降级输出。
- 编辑接口按题面采用**编辑后全量重规范化**，未做局部重扫复杂度优化；
  变化区间由 LCP/LCS 码点比较得出。位置是相对**规范串**的码点位置：
  规范串中的预组合字符是单个码点，删除它即去掉整个组合簇，不提供
  "只删基字符保留记号"之类的簇内编辑。连续编辑的来源坐标相对最近一次
  `set_text` 的输入文本累积，不提供跨多次编辑的完整撤销历史。

## 目录

```
canoncompose/          # 本题库
  __init__.py          # 公开接口
  engine.py            # 流式核心（分解/排序/组合/上限/原子回滚）
  editor.py            # 按位置插入/删除（全量重规范化 + LCP/LCS 区间 + 双向映射）
  table.py             # 字符表协议与字典实现
  samples.py           # 确定性 PUA 示例表（含环表构造）
  errors.py            # 错误类型与定位字段
  validators.py        # 数值/码点校验
tests/
  test_normalize.py    # 规范化必要测试（44 个）
  test_editor.py       # 编辑接口必要测试（39 个）
  reference.py         # 独立一次性朴素参考实现（不导入被测核心）
demo.py                # 固定输入演示
```

测试中的期望值来自 `tests/reference.py` 里独立编写的朴素分解-排序-组合
实现（**不调用被测核心**），以及手算固定例子；编辑测试把"在参考规范串上
拼接后再用参考实现规范化"作为独立期望，位置映射在测试内按 README 契约
独立重建；与被测结果的往返一致只作为补充断言。
