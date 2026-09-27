# canoncompose — 显式字符表的流式规范组合

离线文本基础设施可复用模块：在**调用方显式提供的有限字符表**上执行
递归分解 → canonical combining class（ccc）稳定排序 → 阻塞二元组合，
支持跨 chunk 流式处理、来源区间追踪与段长度上限拒绝。

**不读取、不引用系统 Unicode 数据库，不绑定任何 Unicode 版本。**
本模块只实现本题契约描述的有限算法子集，**不宣称与 Unicode
NFC/NFD 等行业标准完整兼容**。

## 运行环境

- Python **3.14.7**（仅标准库；更低的 3.10+ 大概率可用但未验收）
- Linux 原生运行，**无需安装任何第三方依赖**，无需网络/账号/数据库/
  Docker 等。

## 准确命令

```bash
# 必要测试（63 个）
python -m unittest discover -s tests -v

# 固定输入演示（现场计算，约 2 ms，远小于 8 秒预算；无 sleep、无预录）
python demo.py
```

两条命令均在仓库根目录执行。

## 公开接口

```python
from canoncompose import (
    normalize,            # 一次性 API
    StreamNormalizer,     # 流式 API：feed / finish
    compile_table,        # 表编译：处理文本前的一致性检查与定稿
    CompiledCharacterTable,  # 通过检查、可复用的定稿表
    DictCharacterTable,   # 字典字符表
    CharacterTable,       # 字符表协议抽象基类（也可直接鸭子类型）
    NormalizationResult, SourcePoint, SourceSpan,
    CanonicalError, InvalidInputError, InvalidTableError,
    DecompositionCycleError, SegmentLimitError,
    TableCompilationError, CompositionConflictError, InconsistentLevelError,
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

### 表编译：开始处理文本前的一致性检查\n\n```python\ncompiled = compile_table(table)   # table 通常是 DictCharacterTable\nnormalize(chunks, compiled)       # 通过检查的表可重复用于多个独立文本流\n```\n\n编译需要枚举有限表项，因此待编译表须暴露三个只读映射属性：\n`decompositions`、`combining_classes`、`compositions`\n（`DictCharacterTable` 与编译产物 `CompiledCharacterTable` 均满足；\n只有三个查询方法、无法枚举的鸭子类型表不能编译，但仍可直接使用，\n其环在运行期检测）。\n\n编译在**任何文本处理之前**完成以下检查，错误全部指向具体表项：\n\n1. **循环分解**：对整个分解映射（不只是某段文本会碰到的码点）做\n   成环检测，抛 `DecompositionCycleError`，`chain` 给出闭合路径上的\n   具体码点。\n2. **不一致的组合等级**（`InconsistentLevelError`，`entry` 定位表项）：\n   - `"decomposition"`：被分解字符与分解首元素的 ccc 不一致；\n   - `"composition-second"`：组合项右元素 ccc 为 0（starter 不能作为\n     被组合的记号）；\n   - `"composition-result"`：组合结果 ccc 非 0（组合结果占据基字符\n     位置，必须是 starter）。\n3. **矛盾组合**（`CompositionConflictError`）：对每个\n   `(left, right) -> result`，要求 `result` 的递归分解叶子序列与\n   `left`、`right` 各自递归分解叶子的拼接完全一致；否则规范化结果会\n   依赖处理路径。异常携带 `left`/`right`/`result` 与两侧展开序列\n   `expected`/`actual`。\n\n另外，编译会逐一校验每个表项的类型与码点范围（拒绝 bool、非 int、\n负数/超范围 ccc、空分解元组、NaN 等）。\n\n**编译是原子的**：通过则返回一个完全可用的定稿表，失败则只抛异常，\n不产生半成品；编译失败不影响此前已编译的任何表（各编译表持有独立\n不可变快照，编译后再修改源表也不影响编译结果）。编译表满足字符表\n协议，可直接交给 `normalize` 与任意多个 `StreamNormalizer` 复用；\n`CompiledCharacterTable.expansion(cp)` 额外返回编译期预计算的递归\n分解叶子。\n
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
- 表编译期问题统一继承 `TableCompilationError`（`InvalidTableError` 子类）：
  矛盾组合 `CompositionConflictError`、不一致的组合等级 `InconsistentLevelError`，
  均携带指向具体表项的字段（码点、组合对、期望/实际展开或等级）；
  环无论在编译期还是运行期触发，都抛 `DecompositionCycleError`。
- 错误携带可定位字段：`chunk_index`、chunk 内 `offset`、流上
  `stream_offset`、`codepoint`。
- **数值输入拒绝 NaN/Infinity；整数参数拒绝 `bool`**（`True`/`False`
  虽为 `int` 子类也拒绝）；非 str chunk、非可迭代 chunks、缺方法的表
  同样拒绝。非法输入不静默纠正。
- **原子性**：`feed()` 中途任一步失败，本次 feed 对段缓冲、已输出缓冲、
  流偏移、chunk 计数的修改全部回滚，流恢复到调用前状态，可继续使用；
  `normalize()` 不返回部分结果（成功或抛异常）。
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
  保证运行内表视图一致；需要强一致保证时用 compile_table 先定稿，
  编译表是源表的不可变快照，可跨多个独立文本流复用。

## 已知限制

- 只实现本题契约的子集：不做兼容分解、重排（reordering 之外的
  normalization 形式）、singleton 分解、quick-check、流控等，不保证
  接受任意 Unicode 表后与 NFC/NFKC 一致。
- compile_table 提供编译期的自洽性检查（循环分解、矛盾组合、组合
  等级一致性）；但检查仅针对表中显式列出的有限表项，不推断未列出
  码点之间的隐含关系，也不做 quick-check 等 Unicode 专有判定。
- 只有三个查询方法、无法枚举全部表项的鸭子类型表无法编译，只能
  直接使用：此时环在运行期检测，矛盾组合/等级一致性不做事前检查。
- 来源区间以输入 chunk 的码点偏移表示；surrogate/编码层问题不存在
  （输入是 Python `str`，按码点遍历）。
- 超长单段（如单个分解出 1025+ 叶子的码点）直接拒绝，不做降级输出。

## 目录

```
canoncompose/          # 本题库
  __init__.py          # 公开接口
 engine.py            # 流式核心（分解/排序/组合/上限/原子回滚）
  compiler.py          # 表编译：循环/矛盾/等级检查与定稿快照
  table.py             # 字符表协议与字典实现
  samples.py           # 确定性 PUA 示例表（含环表/矛盾表/等级表）
  errors.py            # 错误类型与定位字段
  validators.py        # 数值/码点校验
tests/
  test_normalize.py    # 核心算法必要测试（44 个）
  test_compile.py      # 表编译/隔离/复用必要测试（19 个）
  reference.py         # 独立一次性朴素参考实现（不导入被测核心）
demo.py                # 固定输入演示
```

测试中的期望值来自 `tests/reference.py` 里独立编写的朴素分解-排序-组合
实现（**不调用被测核心**）；与被测结果的往返一致只作为补充断言。
