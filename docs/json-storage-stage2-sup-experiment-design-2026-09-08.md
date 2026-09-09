# Agent Trace JSON 存储阶段二补充实验设计：四种存储结构横比与 ClickHouse 机制观察

> 状态：设计已确认，待实现和正式运行
> 日期：2026-09-08
> 数据路径：`independent_loader`
> 基础契约：`json-storage-cross-engine-v1`
> 补充契约：`json-storage-four-layout-v1`

## 1. 目标与证据边界

本实验在同一真实 Trace 输入、查询语义、返回内容、正确性和原文恢复契约下比较四种基础 JSON 存储结构：openGauss JSON、openGauss JSONB、ClickHouse String JSON 和 ClickHouse Native JSON。实验回答以下问题：

1. 四种结构在载入、路径过滤、路径投影和完整文档读取中的性能差异；
2. openGauss JSON 文本与 JSONB 二进制文档在同一引擎内的转换、查询和空间成本；
3. ClickHouse Native JSON 的 type hint、动态路径预算、data part、merge、`OPTIMIZE FINAL` 和 Sidecar 在完整流程中的作用；
4. 四种候选存储结构在统一查询负载下的适用场景。

阶段二原实验数据保持不变。本实验使用新契约和独立结果目录；[阶段二报告](json-storage-stage2-report-2026-09-08.md)保留已有实验的证据边界，并补充四种存储结构结果。

报告章节按 openGauss JSON/JSONB、ClickHouse String JSON/Native JSON 的处理流程和场景化比较组织。[阶段三实验](json-storage-stage3-experiment-design-2026-09-08.md)继续负责 Full/Core、长 payload 和 asset reference。

本实验不覆盖 Map、饱和吞吐、冷缓存、多节点、故障恢复、Collector/exporter 全链路和对象存储。数据库空间保留引擎原生口径，不计算跨引擎压缩比例。

## 2. 产物与目录

正式代码位于 `experiments/json-storage-stage2-sup/`：

```text
experiments/json-storage-stage2-sup/
  README.md
  generator/generate_supplement_truth.py
  runner/supplement_common.py
  runner/opengauss_four_layout.py
  runner/clickhouse_four_layout.py
  runner/run_four_layouts.py
  runner/run_opengauss_mechanisms.py
  runner/run_clickhouse_mechanisms.py
  report/summarize_results.py
  tests/
```

运行产物位于 gitignored 的 `docs/temp/json-storage-stage2-sup/`：

```text
docs/temp/json-storage-stage2-sup/
  input-20260908/
  smoke-*/
  formal-*/
  clickhouse-mechanisms-*/
```

`input-20260908/` 只保存补充 truth、查询 catalog 和来源运行清单，不复制阶段二 dataset。来源文件路径、字节数和内容摘要写入运行清单。正式结果完成后更新阶段二报告，并生成 `docs/json-storage-stage2-sup-pre-YYYY-MM-DD.md` 作为组内进展汇报辅助材料。

## 3. 输入与预处理边界

输入复用阶段二冻结数据：

| 项目 | 固定值 |
|---|---|
| dataset | `docs/temp/json-storage-stage2/cross-engine-input-20260907/dataset.jsonl` |
| 行数 | 48,534 |
| block | 256 行，共 190 个，末 block 150 行 |
| Native 动态路径预算 | 32 |

dataset、原始输入、truth 和查询 catalog 的字节数与内容摘要保存在运行清单中。实验程序在运行前核对这些信息。

公共生成器在数据库计时前完成以下步骤，并单独记录 wall time、输入字节和输出字节：

1. 读取原始 JSONL bytes；
2. 解析事件和 Attribute；
3. 把点分隔 Attribute key 投影为可逆嵌套路径；
4. 生成 canonical analysis JSON、Map 辅助值、稀疏 fidelity 值和 raw bytes；
5. 计算查询 truth、canonical 内容摘要与原文内容摘要。

数据库载入计时从提交一个预生成 block 开始，到该 block 成功且可见结束。客户端预处理、数据库载入和维护分别报告，避免把一次性数据准备解释为 JSON 类型的数据库成本。

## 4. 四种存储结构正式横向矩阵

### 4.1 基础存储结构

所有存储结构具有相同的身份、时间、项目和稳定分析列，并保存独立原文表。

| 存储结构 | 引擎 | 动态属性列 | 附加加速 |
|---|---|---|---|
| openGauss JSON | openGauss 6.0.0 | `attributes JSON NOT NULL` | 无 |
| openGauss JSONB | openGauss 6.0.0 | `attributes JSONB NOT NULL` | 无 |
| ClickHouse String JSON | ClickHouse 25.12.11.4 | `attributes String CODEC(ZSTD(3))` | 无 |
| ClickHouse Native JSON | ClickHouse 25.12.11.4 | `attributes JSON(max_dynamic_paths=32)` | 稀疏 `fidelity_values Map(String,String)` Sidecar |

ClickHouse Native JSON 的稀疏 Sidecar 只保存递归包含 JSON null、空对象或空数组的原始 Attribute canonical value。它与 ClickHouse Native JSON 一起构成可恢复的分析存储结构，成本计入载入、空间和完整读取。四种存储结构都通过独立原文表恢复摄入原文。

基础矩阵不创建 GIN、表达式索引、type hint、投影或物化热点列。Native JSON 的自动动态子列属于该类型的基础物理组织，不视为额外索引。

### 4.2 公共查询

全部查询固定 project、时间边界和可见水位。路径查询采用相同逻辑谓词，允许各类型使用对应的原生读取表达式。

| ID | 场景 | 返回契约 | 正式测量 |
|---|---|---|---:|
| S01 | 稳定列分组 | 排序后的 `span_type,count` | 100 |
| S02 | 高频路径等值过滤与分组 | 排序后的 `span_type,count` | 100 |
| S03 | 低密度路径等值过滤 | `count` 与排序 identity digest | 100 |
| S04 | 单路径投影 | 非空值数量与 UTF-8 总字节数 | 100 |
| S05 | 固定 trace 完整回查 | 排序身份与 canonical attributes | 100 |
| S06 | 固定 256 行完整文档页 | 排序身份与 canonical attributes | 20 |

S02、S03 和 S04 的 openGauss JSON/JSONB 使用相同 `#>>` 路径文本提取语义。ClickHouse String JSON 使用 JSON 提取函数，ClickHouse Native JSON 使用直接子列语法。S03 不使用 JSONB containment；GIN containment 只进入优化扩展证据。

查询延迟从已建立连接提交语句开始，到响应 bytes 完整读取结束。结果规范化、Native Sidecar 合并、canonical 序列化、hash 和 truth 核对位于该区间外，并另行记录客户端恢复耗时。S06 报告 median、p95 和范围，不报告 p99。

### 4.3 轮次与顺序

四种存储结构各执行四轮，采用以下 Latin square 顺序；任一时刻只有一种存储结构执行载入或查询：

| 轮次 | 顺序 |
|---|---|
| 1 | openGauss JSON → openGauss JSONB → ClickHouse String JSON → ClickHouse Native JSON |
| 2 | openGauss JSONB → ClickHouse Native JSON → openGauss JSON → ClickHouse String JSON |
| 3 | ClickHouse String JSON → openGauss JSON → ClickHouse Native JSON → openGauss JSONB |
| 4 | ClickHouse Native JSON → ClickHouse String JSON → openGauss JSONB → openGauss JSON |

每种存储结构按 190 个公共 block 载入。openGauss 完成 `ANALYZE`；ClickHouse 等待目标 database 的 merge backlog 连续三次为零。每个查询预热一次后执行正式测量。缓存状态固定为 `query_warmup_1_no_os_cache_drop`。

本矩阵不运行写入期间并发查询。阶段二已有并发证据使用不同的存储结构集合，继续单独引用；补充实验集中控制四种存储结构的载入和静态查询变量。

## 5. 引擎内优化扩展

### 5.1 openGauss

openGauss 内部结果分为基础类型和能力扩展：

| 存储结构 | 目的 |
|---|---|
| openGauss JSON / openGauss JSONB | 隔离 JSON 文本与 JSONB 的基础载入、路径查询和整文档读取 |
| openGauss JSON + 表达式索引 / openGauss JSONB + 表达式索引 | 使用同一热点路径表达式 B-tree，比较索引维护和查询收益 |
| openGauss JSONB + GIN | 使用 `jsonb_hash_ops` 和 containment，记录 JSONB 特有能力 |

热点表达式必须先通过 openGauss 6.0.0 能力探针，确认 JSON 与 JSONB 的表达式和自然计划均可用。openGauss JSONB + GIN 不进入 JSON/JSONB 成对比例，也不进入基础四种存储结构排名。

### 5.2 ClickHouse

ClickHouse 机制观察建立以下存储结构：

| 存储结构 | 控制变量 | 用途 |
|---|---|---|
| Native JSON（无 Sidecar） | 预算 32，无 Sidecar | 观察 Native JSON 自身的语义边界 |
| Native JSON（稀疏 Sidecar） | 预算 32，稀疏 Sidecar | 对应基础矩阵的 ClickHouse Native JSON |
| Native JSON（完整 Sidecar） | 预算 32，完整 canonical Sidecar | 观察完整 Sidecar 的载入、空间和整文档读取成本 |
| Native JSON（类型提示、稀疏 Sidecar） | 预算 32，两个热点 type hint，稀疏 Sidecar | 在相同预算下隔离手动 type hint |

该组是机制观察，不形成新的全结构性能排名。ClickHouse String JSON/Native JSON 正式性能来自四种存储结构矩阵；上述变体记录 DDL、载入、空间、路径库存、正确性和固定查询观察值。

## 6. ClickHouse 载入、merge 与 FINAL 流程

机制实验程序对其创建的临时表执行以下步骤：

1. 创建 MergeTree 表，并对该表暂停后台 merge；
2. 写入多个 block，记录首个 part 和全部零层 part 的列、压缩空间、dynamic/shared paths 与实际类型；
3. 使用相同 truth 完成 merge 前查询；
4. 恢复该表后台 merge，等待 backlog 稳定并记录新 part；
5. 执行 `OPTIMIZE TABLE ... FINAL`，记录强制合并耗时和最终 part；
6. 重复正确性查询，核对逻辑结果保持一致；
7. 在 `finally` 路径恢复 merge 并删除临时 database。

路径在 merge 后从 shared data 进入 dynamic subcolumn，或从 dynamic subcolumn 进入 shared data，只表示物理组织变化。type hint 路径使用声明类型，并与自动动态路径预算分开统计。

`SELECT ... FINAL` 不进入四种存储结构正式查询。独立正确性探针使用小型 `ReplacingMergeTree(version)` 创建跨 part 重复键，分别验证普通查询、查询时 `FINAL` 和 `OPTIMIZE TABLE ... FINAL`。

查询时 `FINAL` 在读取阶段应用合并语义；`OPTIMIZE ... FINAL` 强制生成合并后的物理 part。该探针只解释概念，不输出性能结论。

## 7. 正确性、指标与运行清单

所有正式存储结构必须通过以下门禁：

- 48,534 条分析记录与原文记录一一对应；
- identity 没有缺失、额外或重复；
- S01～S06 的结果与独立 truth 一致；
- 分析结构的 canonical 内容摘要全部匹配；
- 原始 UTF-8 bytes 内容摘要全部匹配；
- Native 的分析等价、逻辑文档恢复和原文恢复分别记录；
- 临时 schema、database、暂停 merge 状态和 active merge 全部清理。

每轮记录预处理、分析表 INSERT、原文表 INSERT、完整 block、维护和查询的分项耗时。openGauss 记录 heap、TOAST、index 和总分配空间；ClickHouse 记录 active part 压缩/未压缩 bytes、part、dynamic/shared paths 和 `QueryFinish` 指标。两引擎共同报告客户端完整响应延迟、返回 bytes、正确性和请求等价速率；引擎专有指标不做数值等同。

运行清单记录运行标识、状态、复现命令、四种存储结构的顺序、输入来源、文件校验信息、代码/DDL/查询摘要、容器与服务端版本、宿主资源、测量参数、正确性、维护、结果文件和清理状态。失败轮次使用新的运行标识重跑，失败产物只作诊断。

## 8. 执行门槛与停止条件

正式运行前依次完成：

1. 文档链接和命名检查；
2. 单元测试；
3. openGauss JSON/JSONB 运算符与表达式索引能力探针；
4. ClickHouse type hint、表级 merge 控制和路径库存探针；
5. 每个引擎至少完成一次小数据验证；
6. 四轮正式运行与确定性汇总。

Docker 服务、固定镜像或端口不可用时停止数据库小数据验证和正式运行，代码与单元测试可以继续。输入身份、truth、容器版本、查询结果、恢复或清理任一门禁失败时发布诊断结果，不进入正式汇总。

## 9. 报告与汇报辅助材料

阶段二报告和汇报辅助材料按以下顺序组织：

1. openGauss JSONB 的输入、解析、二进制表示、索引维护、路径查询和完整 JSON 返回流程；
2. ClickHouse Native JSON 的输入、解析、类型识别、dynamic path/shared data 分配、data part 写入、后台 merge、`OPTIMIZE TABLE ... FINAL` 和读取流程；
3. Sidecar 对比需要解决的原始信息缺口、增加的数据和新增恢复步骤；
4. openGauss JSON、openGauss JSONB、ClickHouse String JSON、ClickHouse Native JSON 在基础载入、路径查询、完整 JSON 和特殊负载中的结果；
5. 证据边界和存储结构选择建议。

`json-storage-stage2-sup-pre-YYYY-MM-DD.md` 按以下结构组织：

1. 当前问题、实验范围和结论摘要；
2. openGauss JSON/JSONB 载入与查询流程图；
3. openGauss JSON 与 JSONB 引擎内结果；
4. ClickHouse String/Native 载入、动态子列、shared data、merge 和 FINAL 流程图；
5. Native 信息边界及无 Sidecar、稀疏 Sidecar、完整 Sidecar 的作用；
6. 四种存储结构在各场景下的统一横向表；
7. 证据边界、当前建议和阶段三入口。

流程图使用仓内 SVG，图中文字和指标可独立阅读。材料区分已完成正式结果、机制观察和设计说明；比例只在相同数据、查询、返回契约和同轮实验内计算。

## 10. 完成标准

- 文档命名统一为带日期形式，报告主题只写入标题；
- 四种存储结构各四轮共 16 个正式结果全部完成；
- S01～S05 每种存储结构每轮各 100 个样本，S06 各 20 个样本，全部通过 truth；
- ClickHouse 四种机制存储结构和 FINAL 正确性探针完成；
- 汇总程序从原始产物重算结果，两次输出内容一致；
- 阶段二报告按核心处理流程重组，原正式结果与补充结果分别标识来源；
- 汇报辅助材料中的数字均可定位到正式汇总或机制运行清单；
- 运行后无实验临时数据库对象、暂停 merge 状态或 active merge。

## 11. 参考资料

- [阶段一实验设计](json-storage-stage1-experiment-design-2026-09-08.md)
- [阶段一报告](json-storage-stage1-report-2026-09-08.md)
- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-08.md)
- [阶段二报告](json-storage-stage2-report-2026-09-08.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-08.md)
