# canoncompose — 显式字符表的流式规范组合

离线文本基础设施可复用模块：在**调用方显式提供的有限字符表**上执行
递归分解 → canonical combining class（ccc）稳定排序 → 阻塞二元组合，
支持跨 chunk 流式处理、来源区间追踪、段长度上限拒绝，以及**处理到一半
时导出 JSON 检查点、在另一个进程恢复后接着处理**。

**不读取、不引用系统 Unicode 数据库，不绑定任何 Unicode 版本。**
本模块只实现本题契约描述的有限算法子集，**不宣称与 Unicode
NFC/NFD 等行业标准完整兼容**。

## 运行环境

- Python **3.14.7**（仅标准库；更低的 3.10+ 大概率可用但未验收）
- Linux 原生运行，**无需安装任何第三方依赖**，无需网络/账号/数据库/
  Docker 等。

## 准确命令

```bash
# 统一使用隔离环境的解释器
PY=./.venv/bin/python

# 全部测试（79 个：原有 44 个 + 检查点 35 个）
$PY -m unittest discover -s tests -v

# 固定输入演示（现场计算，约 0.3 s，远小于 90 秒预算；无 sleep、无预录）
$PY demo.py
```

`python -m unittest discover -s tests -v` 的旧入口保持不变（把 `python`
换成上面的 `./.venv/bin/python` 即可）。两条命令均在仓库根目录执行。

## 公开接口

```python
from canoncompose import (
    normalize,            # 一次性 API
    StreamNormalizer,     # 流式 API：feed / finish / export_checkpoint /
                          #               restore_checkpoint
    DictCharacterTable,   # 字典字符表（fingerprint / canonical_rules）
    CharacterTable,       # 字符表协议抽象基类（也可直接鸭子类型）
    NormalizationResult, SourcePoint, SourceSpan,
    CanonicalError, InvalidInputError, InvalidTableError,
    DecompositionCycleError, SegmentLimitError, CheckpointError,
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

### 检查点：暂停后在另一个进程接着处理

文本导入被暂停时，可导出检查点；之后（甚至在**另一个进程**里）凭
JSON 数据和同内容字符表恢复，接着喂剩余 chunk，结果、来源区间、
chunk 编号与从头连续处理完全一致。

```python
sn = StreamNormalizer(table)
sn.feed(chunk1)
checkpoint = sn.export_checkpoint()          # 纯 JSON 兼容 dict
blob = json.dumps(checkpoint)                # 可落盘/跨进程（无 pickle）

# …另一个进程：调用方重建同内容表后恢复…
sn2 = StreamNormalizer.restore_checkpoint(
    json.loads(blob),
    rebuilt_same_table,
)
sn2.feed(chunk2)
result = sn2.finish()
```

检查点结构（`format`/`version` 为固定标识，版本不认识即拒绝）：

| 字段 | 内容 |
|---|---|
| `format`, `version` | `"canoncompose.checkpoint"`，`1` |
| `table_fingerprint` | 导出时表**内容**指纹（规范排序规则的 SHA-256，见下） |
| `segment_limit` | 段上限，随检查点恢复 |
| `stream_pos`, `chunk_count` | 已消费码点数 / 已 feed 的 chunk 数（编号从此继续） |
| `finished` | 是否已 `finish()` |
| `pending_segment` | **未闭合规范段**原样条目（绝不因保存而提前闭合） |
| `output` | 已闭合段的输出条目（码点 + 来源点） |

表身份只认**规则内容**，不认对象地址：

```python
fp = table.fingerprint()          # 三类有限规则规范排序后 SHA-256
rules = table.canonical_rules()   # 排序后的 JSON 兼容规则（可在别处重建表）
```

- 指纹把分解、ccc、组合三类规则**排序**后哈希，与插入顺序无关；同一
  有限规则集在另一个进程重建出的表指纹相同。
- 恢复时调用方传入表，库重新计算指纹与检查点比对：**内容不同即拒绝**
  （`CheckpointError`）。调用方负责保存/传递规则本身（可直接持久化
  `canonical_rules()` 的输出），检查点内只存指纹。
- 检查点功能只支持精确的 `DictCharacterTable`；覆盖其方法的子类与
  鸭子类型自定义表**正常规范化完全不受影响**，只是不能导出/恢复
  （因为指纹只覆盖有限规则，无法代表被覆盖的方法行为）。

检查点语义与安全保证：

- **保存不改变流**：`export_checkpoint()` 不调用 `finish`，未闭合段原样
  保留在 `pending_segment`——下一块字符仍可改变段内组合与排序；导出可
  重复调用，返回的是全新嵌套对象，调用方改动它不会反过来影响活对象。
- **已 `finish` 也可保存**：恢复后保持结束语义（`finish()` 幂等返回同一
  结果，`feed()` 被拒）。
- **恢复是全有或全无**：格式/版本不认识、必含字段缺失或类型非法、来源
  坐标不合法（越界、覆盖不完整、chunk 内偏移不连续等）、段结构违反
  ccc 不变式或表指纹不匹配时抛 `CheckpointError`，**不会留下半恢复
  对象**（所有校验通过后才构造流）。
- 展开/ccc 缓存是纯派生数据，不进检查点，恢复后按同内容表惰性重建。

`checkpoint_worker.py` 是仓库附带的极简跨进程样板：stdin 收 JSON
（`save` / `resume`），stdout 回 JSON，仅用标准库；`demo.py` 第 7、8 节
用它启动两个真实子进程演示恢复与表不匹配拒绝。

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
  （`InvalidTableError` 子类），超段上限 `SegmentLimitError`，检查点
  导出/恢复问题 `CheckpointError`（`ValueError` 子类）。
- 错误携带可定位字段：`chunk_index`、chunk 内 `offset`、流上
  `stream_offset`、`codepoint`。
- **数值输入拒绝 NaN/Infinity；整数参数拒绝 `bool`**（`True`/`False`
  虽为 `int` 子类也拒绝）；非 str chunk、非可迭代 chunks、缺方法的表
  同样拒绝。非法输入不静默纠正。
- **原子性**：`feed()` 中途任一步失败，本次 feed 对段缓冲、已输出缓冲、
  流偏移、chunk 计数的修改全部回滚，流恢复到调用前状态，可继续使用；
  `normalize()` 不返回部分结果（成功或抛异常）。
- **检查点恢复全有或全无**：格式版本、字段类型/范围、来源坐标、段结构、
  表指纹全部校验通过后才构造对象；任何一项不合法都抛 `CheckpointError`，
  不留下半恢复对象。
- 本算法**没有搜索预算/无解证明**之类概念（确定性结构变换），因此不
  存在"预算耗尽 vs 无解"的区分需求；能失败的情形都是显式非法输入/表/
  上限/检查点，错误类型如上分开。

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
- 检查点只存流状态与表**内容指纹**，不存表本身也不存派生缓存：调用方
  负责在恢复侧重建同内容表（`canonical_rules()` 的 JSON 输出可直接用于
  重建）；指纹用规范排序 + SHA-256，跨进程、跨插入顺序稳定，规则内容
  被任何一处改动都会变。
- 检查点功能限定在精确 `DictCharacterTable`：鸭子类型表与
  `DictCharacterTable` 子类（可能覆盖了方法行为）的常规规范化能力
  完全保留，只是无法用只覆盖字典规则的指纹证明内容一致。

## 已知限制

- 只实现本题契约的子集：不做兼容分解、重排（reordering 之外的
  normalization 形式）、singleton 分解、quick-check、流控等，不保证
  接受任意 Unicode 表后与 NFC/NFKC 一致。
- 不校验组合结果自身的 ccc 或分解一致性（表自洽性由调用方负责）。
- 来源区间以输入 chunk 的码点偏移表示；surrogate/编码层问题不存在
  （输入是 Python `str`，按码点遍历）。
- 超长单段（如单个分解出 1025+ 叶子的码点）直接拒绝，不做降级输出。
- 检查点只支持精确 `DictCharacterTable`，不支持自定义表；指纹按
  **显式规则条目**计算：显式写出的 `ccc=0` 条目与"缺省为 0"行为相同但
  指纹不同。跨进程请直接用 `canonical_rules()` 的输出重建表（条目逐一
  保留），不要手工等价改写。
- 检查点不做加密/访问控制：它只证明"恢复用表的规则内容与导出时一致"，
  不加密数据，也不防有文件写权限者自行构造检查点。

## 目录

```
canoncompose/          # 本题库
  __init__.py          # 公开接口
  engine.py            # 流式核心（分解/排序/组合/上限/原子回滚/检查点）
  table.py             # 字符表协议、字典实现、规则指纹
  samples.py           # 确定性 PUA 示例表（含环表/不匹配表构造）
  errors.py            # 错误类型与定位字段（含 CheckpointError）
  validators.py        # 数值/码点校验
tests/
  test_normalize.py    # 原有核心测试（44 个）
  test_checkpoint.py   # 检查点测试（35 个，含两个真实子进程用例）
  reference.py         # 独立一次性朴素参考实现（不导入被测核心）
demo.py                # 固定输入演示（含暂停/恢复、跨进程、表不匹配）
checkpoint_worker.py   # 跨进程 JSON save/resume 样板（stdin/stdout）
```

测试中的期望值来自 `tests/reference.py` 里独立编写的朴素分解-排序-组合
实现（**不调用被测核心**）；与被测结果的往返一致只作为补充断言。
检查点测试在多个可手算字符表上对短输入穷举每个 chunk 边界的
"导出 → JSON 往返 → 全新表恢复 → 继续处理"，并与连续处理及独立参考
逐一核对码点、来源区间与 chunk 编号；其中两个用例启动真实子进程、
只经 JSON 通信，不在内存里复制对象。
