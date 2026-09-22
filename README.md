# streamnorm — 显式字符表的流式规范组合

离线文本基础设施用的可复用模块：在**显式给定的字符表**上做流式 canonical
规范组合（递归分解 → 按 canonical combining class 稳定重排 → 按二元组合映射
与阻塞规则组合）。全部字符知识来自调用方传入的表，**不读取 `unicodedata`
或任何系统 Unicode 数据**，因此结果与机器、Python 构建、系统 Unicode 版本无关。

- Python：3.14.7（仅标准库，安装 Python 后无需任何其他依赖）
- 平台：Linux 原生；核心、测试、演示全部离线（无网络、账号、密钥、数据库、Docker）
- 范围声明：本模块只实现本 README 与代码契约描述的显式表规范组合子集，
  **不宣称兼容完整 Unicode 规范化标准**（无兼容性分解、无 Hangul 算法合成、
  无组合排除表等；表里有什么就算什么）。

## 运行

```bash
python -m unittest discover -s tests -v   # 必要测试（含独立朴素参考对拍）
python demo.py                            # 约 8 秒内的确定性演示
```

## 接口

```python
from streamnorm import (
    CharacterTable, StreamNormalizer, normalize,
    NormalizationResult, SourceInterval,
    NormalizationError, TableError, CyclicDecompositionError,
    InputError, SegmentTooLongError, NormalizerStateError,
)
```

### CharacterTable(decompositions=None, ccc=None, compositions=None)

显式字符表，构造时全量校验并冻结（只读视图）：

- `decompositions`: `{码点: (码点, ...)}` 递归 canonical 分解；图必须无环，
  环在构造时被检测并抛出 `CyclicDecompositionError`（携带环路径）。
- `ccc`: `{码点: 0..254}` canonical combining class；缺省为 0（starter）。
- `compositions`: `{(码点, 码点): 码点}` 二元组合映射。

方法：`ccc_of(cp)`、`expansion_of(cp)`（完全展开）、`compose(a, b)`。

### normalize(chunks, table, *, max_segment_length=1024) -> NormalizationResult

- `chunks`：`str` 的可迭代对象（不允许直接传单个 `str`/`bytes`，避免把
  字符串静默当作单字符块序列）。
- 返回 `NormalizationResult(text, segments)`：`text` 为规范串；`segments`
  为 `SourceInterval(start, end)` 的有序集合——每个规范段对应原输入
  （所有 chunk 拼接后）的半开区间 `[start, end)`。
- 全有或全无：出错时抛异常，不返回部分结果。

### StreamNormalizer(table, max_segment_length=1024)

流式接口：`push(chunk) -> str` 返回该块推进后已完成的规范段文本，未结束的
规范段跨 chunk 保留；`finish() -> str` 冲刷尾段并关闭流。`.segments` 累计
来源区间，`.pending_length` 为当前未冲刷段长度。

### 算法（每规范段）

1. 递归分解（表驱动，构造期已排除环）；
2. starter 后的非 starter 按 ccc **稳定**排序（首字符若是 starter 保持首位；
   文本开头的连续组合符构成无 starter 的首段，同样排序但不参与组合）；
3. 组合：仅当表中列出 `(最近 starter, 当前字符)` 且**前一未组合字符的 ccc
   为 0 或严格小于当前字符 ccc** 时才组合；组合后 `prev_ccc` 不前进。

段 = 一个 starter 及其后的非 starter（或文本开头的纯组合符段）。starter
到达即关闭当前段，因此流式只需缓存一个未结束的段。

## 输入上限（接口边界）

| 上限 | 值 | 说明 |
|---|---|---|
| 单规范段码点数 | 默认 1024（`max_segment_length` 可调，≥1） | 按**分解后**码点计；超限抛 `SegmentTooLongError`（含 `limit`、`segment_start`） |
| 单码点分解展开长度 | 64（`MAX_EXPANSION`） | 构造表时校验，防指数膨胀 |
| ccc 取值 | 0..254 | 构造表时校验 |
| 码点范围 | U+0000..U+10FFFF，排除代理区 | 构造表时校验 |

## 错误语义（固定契约）

- 整数参数拒绝 `bool`；数值输入拒绝 float（含 NaN/Infinity）——
  抛 `InputError`/`TableError`，消息含参数名与收到的值，可定位，不静默纠正。
- `TableError`/`CyclicDecompositionError`：表非法，构造期抛出，之后不做任何规范化。
- `InputError`：调用契约违反，**在任何状态变更之前**抛出，实例仍可继续使用。
- `SegmentTooLongError`：流式处理中段超限，**该实例随后被标记为失败**
  （再调用 `push`/`finish` 抛 `NormalizerStateError`），必须丢弃换新实例。
  已说明的部分状态：此前 `push` 已返回的文本仍然有效，失败调用本身未产出文本。
- `finish()` 后再 `push`/`finish`：抛 `NormalizerStateError`。
- 浮点误差口径：本模块**没有任何浮点输入或输出**，全部结果为离散文本与整数
  区间，无误差容忍问题。

## 设计取舍

- **显式表而非系统 Unicode 数据**：可复现、可审计、离线；代价是表由调用方
  负责提供，模块不判断表是否“符合 Unicode”。
- **段级流式**：内存占用以单个规范段为界（≤ `max_segment_length` 码点），
  与输入总长无关；段间无交互，因此分块方式不影响结果。
- **构造期环检测 + 展开缓存**：分解图在 `CharacterTable(...)` 时一次性
  校验并展开（迭代式 DFS，不受递归深度限制），运行期零检测开销。
- **稳定排序用 `sorted`**：Python 排序保证稳定，段内仅首字符可能 ccc 为 0，
  单次按键排序即等价于标准插入式重排。

## 已知限制

- 仅实现上述显式表子集，不宣称完整行业标准（Unicode NFC）兼容。
- 组合只针对“最近的 starter”；表中第一个元素为非 starter 的组合对不会触发。
- 来源区间通常恰好划分 `[0, len(input))`；仅当**单个输入字符的分解跨越段
  边界**（展开序列中部出现 starter）时，相邻区间会在该字符处重叠——这是
  有文档记录的预期行为（见 `tests/test_normalizer.py` 对应用例）。
- 幂等性对“良构表”成立（组合目标的分解与其组分一致、分解展开按 canonical
  顺序、组合对首元素为 starter）；对任意手工表不额外保证。
- 段超限失败后实例不可恢复（见错误语义），需要调用方重建。

## 文件布局

```
streamnorm/            # 本模块（核心，仅标准库）
  __init__.py          # 公开 API
  table.py             # CharacterTable：校验、环检测、分解展开
  normalizer.py        # StreamNormalizer / normalize / 结果类型
  errors.py            # 错误层级
  _checks.py           # 内部参数校验
tests/
  naive_reference.py   # 独立一次性朴素参考（不 import 被测核心）
  fixtures.py          # 确定性生成的测试表
  test_table.py        # 表校验、环、参数契约
  test_normalizer.py   # 阻塞、排序、流式、区间、段上限
  test_fuzz.py         # 流式 vs 朴素参考对拍、区间划分、幂等（补充）
demo.py                # 固定输入演示：正常结果 + 实际触发的拒绝边界
```
