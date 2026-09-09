# Agent Trace JSON 存储阶段二实验设计

> 状态：已完成，六轮正式实验、18 个存储结构结果通过门禁
> 实验完成日期：2026-09-07；文档修订日期：2026-09-09
> 数据路径：独立载入程序
> 上游边界：[阶段一报告](json-storage-stage1-report-2026-09-09.md)第 6、7 节

## 1. 目标与证据边界

本实验使用同一 Agent Trace 数据、逻辑记录、查询、正确性门禁和原文恢复契约，比较 openGauss 6.0.0 与 ClickHouse 25.12.11.4 的动态属性存储结构。

实验覆盖真实 Trace 分布审计、持续分批写入、写入期间并发查询、后台维护、静态查询和空间统计。重点回答三类问题：稳定列查询是否受动态属性结构影响、路径查询如何受索引或子列组织影响、完整 Trace 回查需要承担哪些读取和恢复成本。

截至 2026-09-07，两仓远端状态为：

| 仓库 | 检查时间与提交 | 状态 |
|---|---|---|
| exporter_demo | 2026-09-07 · `81b55be` | SPEC v1.8 冻结 18 列 |
| trace-synthesis | 2026-09-07 · `ef3be14` | v4 database catalog revision `2026-09-02.3` 仍定义 28 列 |

两仓尚未形成联合冻结。实验不经过 Collector、exporter 或 benchmark，不形成当前两仓 `main` 的系统级性能结论。实验使用独立载入程序；完整提交和文件身份保存在运行清单中。

本阶段不修改 exporter_demo、trace-synthesis、数据库容器配置或公开数据集。实验使用现有单机容器，记录其实际资源限制；当前两个容器均未设置显式 CPU 或内存上限。

trace-synthesis 已增加跨 backend 的并发模式、QPS 口径和可比性设计；当前 database 与 Langfuse backend 的 event policy 仍不一致。阶段二采用这些设计中的同输入、同参数计划、同并发模式、阶段屏障、连接复用、计时边界和全成功样本门禁，不复用尚未满足统一语义的系统级结果。

Full/Core、长 payload 和 asset reference 属于独立的物理分层问题，顺延到[阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)。阶段二只回答动态属性存储的跨引擎差异。

## 2. 产物与目录

正式代码位于 `experiments/json-storage-stage2/`：

```text
experiments/json-storage-stage2/
  README.md
  audit/audit_real_traces.py
  generator/generate_cross_engine.py
  runner/run_cross_engine.py
  tests/
```

运行产物位于 gitignored 的 `docs/temp/json-storage-stage2/`。每个产物目录最后写入 `run-manifest.json`；只有状态为 `complete` 且全部门禁通过的运行进入统计。有效结果位于 `formal-20260907-retry-3/`，结果见[阶段二横向报告](json-storage-stage2-report-2026-09-09.md)。

## 3. 真实 Trace 分布审计

### 3.1 固定输入

审计输入为 trace-synthesis 已生成的 `whowhen-pro` text split：

| 项目 | 固定值 |
|---|---|
| 输入 | trace-synthesis `whowhen-pro` text split |
| spans | 48,534 |
| traces | 6,257 |
| 生成 seed | `42` |
| 生成器基线 | 2026-09-03 · trace-synthesis `6472d8e` |

输入文件、上游清单和生成结果的完整文件身份保存在运行清单中。报告正文只使用已经通过身份校验的数据。

数据来自真实公开 Agent 轨迹的确定性 Span 投影。时间戳由生成器构造，范围为 2030-01-01 00:00:00 至 01:44:17 UTC；时间分布只用于固定窗口，不代表生产到达过程。

### 3.2 统计口径

OTel Attribute 键按顶层键统计。键中的点属于键名；分析布局另行执行可逆的嵌套投影。审计输出：

- `P`：窗口内不同 Attribute 键数；
- `W`：每 Span Attribute 键数的 p50、p95、p99、最大值；
- `dᵢ`：每个键出现的 Span 比例；
- `cᵢ`：每个键的精确 distinct canonical 值数和相对行数；
- `Tᵢ`：每个键的 JSON 类型集合、各类型数量和冲突比例；
- `Lᵢ`：每个 canonical 值的 UTF-8 字节长度 p50、p95、p99 和最大值；
- `E`：相邻窗口的新增键、删除键、类型变化和稳定键集合；
- payload：完整行、attributes、`gen_ai.input.messages`、`gen_ai.output.messages`、工具参数和结果的字节分位数。

主窗口固定为 15 分钟左闭右开窗口。分组维度使用数据实际提供的 `source_dataset`、`framework`、`span.type` 和顶层 `schema_version`。数据未提供 tenant、project 和 instrumentation scope；审计把这些维度记录为 `unavailable`，不使用替代值推导多租户或 instrumentation 分布。

## 4. 公共横向数据契约

审计程序的结果决定 ClickHouse Native JSON 路径预算。预算取大于等于全局 `P` 的最小 2 的幂，并设置 128 的上限；本数据预期使用 32。预算、实际 `P` 和推导公式同时写入 `truth-manifest.json`。

公共生成器逐行读取固定输入并输出：

```text
ingest_seq, event_id, trace_id, span_id, parent_span_id,
project_id, start_time, end_time, duration_ms, span_type,
framework, level, attributes_analysis, attributes_map, raw_event
```

- `event_id` 为 `trace_id:span_id`，并执行唯一性门禁。
- `project_id` 固定为数据来源标识 `Leoxx/whowhen_pro`，只用于相同查询选择性，不解释为生产租户。
- `framework` 缺失时写入空字符串；`level` 由 Span status 确定。
- `attributes_analysis` 把点分隔的 Attribute 键可逆投影为嵌套 JSON。生成器拒绝标量/对象前缀冲突，并在运行清单中保存键映射。
- `attributes_map` 使用原始 Attribute 键作为 Map key，value 为该 Attribute 值的 canonical JSON 字符串，保持类型和数组顺序。
- `raw_event` 为输入 JSONL 去除行结束符后的原始 UTF-8 bytes。

generator 同时输出：

- canonical `dataset.jsonl`；
- `truth-manifest.json`，包含每行身份、分析结构、canonical JSON 与原文摘要，以及查询参数、最终结果和每个 INSERT block 水位结果；
- `run-manifest.json`，包含输入与产物身份、字节数、行数、block size、实验程序身份和生成命令。

固定 INSERT block 为 256 行，共 190 个 block。最后一个 block 保存剩余 150 行。各布局使用相同行序和 block 边界。

### 4.1 横向可比性契约

可比性契约版本固定为 `json-storage-cross-engine-v1`，所有完成状态运行清单记录以下语义：

- 两引擎使用相同输入、查询 catalog、参数计划、并发数、预热策略和返回内容；
- 每个 worker 在一个阶段内建立并复用一条独立连接；连接建立完成后，全部 worker 通过阶段屏障同时进入预热或正式测量；
- 查询延迟从语句提交开始，至结果完整读取结束；连接建立、正确性校验、结果排序规范化和 hash 计算不进入延迟；
- QPS 定义为 `成功样本数 × 1000 / 成功样本延迟总和(ms)`，报告名称为“请求等价速率”，不解释为饱和吞吐量；
- 正式轮次要求查询和正确性门禁全部成功。失败轮次保存诊断运行清单，重试结果使用新的运行标识，失败轮次不进入横向统计；
- 查询结果先完整读取，再在计时区间外完成规范化和 truth 核对。

## 5. 动态属性存储横向矩阵

### 5.1 存储结构

所有存储结构保留相同强类型列。分析表与原文表按 `event_id` 一一对应；原文表只承担字节级恢复，其空间单列并在总空间中计入。

| 引擎 | 存储结构 | 动态属性组织 |
|---|---|---|
| openGauss | openGauss JSONB | `JSONB`，无动态属性索引 |
| openGauss | openGauss JSONB + 表达式索引 | 同一 `JSONB`，增加 `gen_ai.operation.name` 表达式索引 |
| openGauss | openGauss JSONB + GIN | 同一 `JSONB`，增加 `jsonb_hash_ops` GIN 包含查询索引 |
| ClickHouse | ClickHouse String JSON | nested canonical JSON `String CODEC(ZSTD(3))` |
| ClickHouse | ClickHouse Map | `Map(String,String)`，value 为 canonical JSON 字符串 |
| ClickHouse | ClickHouse Native JSON | 审计派生 `max_dynamic_paths` 的 Native `JSON`，加稀疏 `fidelity_values Map(String,String)` |

ClickHouse Native JSON 负责路径分析。其分析表同时保存稀疏 `fidelity_values Map(String,String)`：一个原始 Attribute 的值递归包含 `null`、空对象或空数组时，保存该 Attribute 的完整 canonical JSON value。

Q04 先读取 ClickHouse Native JSON，再用 `fidelity_values` 覆盖相应值，从而恢复 ClickHouse Native JSON 不能区分的状态。Sidecar 的字节计入分析表空间。原文表保存逐行原始 UTF-8 bytes；ClickHouse Native JSON 和 Sidecar 只用于分析结构及其逻辑恢复。ClickHouse Map 查询使用原始 Attribute key；ClickHouse String JSON、openGauss JSONB 和 ClickHouse Native JSON 查询使用嵌套分析路径。

openGauss 6.0.0 的 `jsonb_ops` GIN 在写入空字符串时触发 `jsonb_gin.cpp:519` 的零长度复制错误，递归对象与数组中的空字符串同样受影响。openGauss JSONB + GIN 使用可保留这些值的 `jsonb_hash_ops`；Q05 保持 `attributes @> %s::jsonb`，并验证 truth、自然计划和禁用顺扫后的 GIN 计划。

DDL 与索引空间记录此 opclass，阶段一 `jsonb_ops` 结果保持其原有实验范围。根因与修复条件见 openGauss [v6.0.0 源码](https://gitee.com/opengauss/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_gin.cpp)和 [v6.0.2 源码](https://gitee.com/opengauss/openGauss-server/blob/v6.0.2/src/common/backend/utils/adt/jsonb_gin.cpp)。

首次 openGauss 运行因该错误失败，整轮结果已排除。诊断目录、运行标识和完整错误保存在实验 README 与运行清单中。

### 5.2 公共查询

查询均限定 `project_id`、固定时间范围和 `ingest_seq < watermark`。正式静态查询使用最终水位 48,534；写入期间查询使用启动查询前已经提交的 block 水位，因此后续 INSERT 不改变该次查询的 truth。

| ID | 语义 | 返回内容 |
|---|---|---|
| Q01 | 50% 时间窗口内按 `span_type` 分组 | 排序后的 `span_type,count` |
| Q02 | `gen_ai.operation.name = execute_tool`，按 `span_type` 分组 | 排序后的 `span_type,count` |
| Q03 | 50% 时间窗口按 `span_type` 计算 count、sum(duration_ms) | 排序后的聚合行 |
| Q04 | 固定代表性 `trace_id` 回查完整 Trace | 按 `start_time,event_id` 排序的身份和 attributes 内容摘要 |
| Q05 | `failure.mistake_mode = A.3` 的低密度路径过滤 | count 和 identity digest |

生成器独立计算每个查询在最终水位和每个 block 水位的预期结果及内容摘要。实验程序只读取 truth，不复用 SQL 结果计算期望值。

### 5.3 写入、并发和静态测量

每种存储结构执行三轮，执行顺序采用平衡轮换。每轮流程为：

1. 创建唯一临时 schema 或 database，记录 DDL 内容摘要。
2. 按 256 行 block 持续写入并逐 block 提交。
3. 前五个 block 提交后启动两个查询 worker，循环执行 Q01、Q02、Q03、Q05；每次固定启动前水位并核对对应 truth。
4. 记录 190 个 block 的写入耗时、rows/s、MiB/s、p50、p95、p99 和可见延迟。
5. 写入期间记录查询耗时、p50、p95、p99、错误数及结果门禁。
6. 写入结束后等待 ClickHouse merge backlog 连续三次为零或达到 120 秒上限；openGauss 完成 `ANALYZE`。等待行为计入后台维护，不计入载入时间。
7. 每条查询预热一次并正式测量 100 次；记录每次 wall time、median、p95、p99、执行计划和引擎可提供的 read rows/bytes、CPU、内存。
8. 核对行数、缺失/额外/重复 ID、分析结构摘要、原文摘要和 Q01～Q05 truth。
9. 记录分析表、原文表、索引或 part 的分项空间，以及 ClickHouse active part、merge backlog、dynamic/shared path 数。
10. 清理临时 schema 或 database；清理成功后发布完成运行清单。

缓存状态固定为 `query_warmup_1_no_os_cache_drop`。实验不清理宿主机页缓存。两引擎顺序执行，另一个容器保持空闲；运行清单记录宿主 CPU、内存、磁盘和容器资源配置。并发与静态查询均遵循 `json-storage-cross-engine-v1` 的连接、阶段屏障、计时和成功样本规则。

ClickHouse 每条测量查询使用唯一 query ID，HTTP 响应完整读取后结束延迟计时。每个写入并发阶段和静态阶段结束后，各执行一次 `SYSTEM FLUSH LOGS query_log`，随后批量读取本阶段全部 query ID 的 `QueryFinish`；静态阶段同时核对预热查询日志。采集和轮询均在延迟计时外。

最终样本保留 `query_log` 的 duration、read rows/bytes、memory、result rows/bytes 和 SelectedRows/SelectedBytes。缺失或重复终态、异常状态、缺失或无效指标均使阶段失败；全部日志回填并通过 truth 后生成摘要。

测量请求启用查询日志，管理请求关闭查询日志；具体 HTTP 参数保存在实验 README。该配置限制观测日志引入的后台写入和 merge。

早期 ClickHouse 运行因逐查询刷新全局日志而增加 system log part 和 merge，最终触发服务端总内存限制。实验程序改为阶段结束后批量采集查询日志，失败轮次全部排除。

另一组早期运行未在阶段屏障前显式建立 ClickHouse 连接，首个并发样本可能包含 TCP 建连时间，因此整组排除。有效运行均在连接成功后进入屏障。实验程序只清理本次创建并取得所有权的 schema 或 database。完整诊断见实验 README 与运行清单。

## 6. 运行清单与停止条件

运行清单至少记录：

- 运行标识、状态、开始/结束时间、复现命令和异常；
- research、exporter_demo、trace-synthesis 的本地 HEAD 与远端 `main`；
- 数据路径、输入与 truth 文件身份、seed、行数和 block 边界；
- DDL、查询 catalog 和实验程序的内容摘要；
- 数据库版本、容器镜像与资源配置、端口和宿主资源；
- 存储结构、运行轮次、执行顺序、缓存状态和后台维护等待结果；
- `comparability_contract_version`、连接复用方式、阶段屏障、延迟边界和 QPS 语义；
- correctness、raw recovery、cleanup 的完成状态；
- 结果文件名和文件身份。

出现以下情况时停止对应候选并发布 `status=failed` 的诊断运行清单：

- 输入或上游清单的文件身份不一致；
- 点键嵌套投影出现前缀冲突；
- 任一 truth、记录身份、分析结构摘要或原文摘要门禁失败；
- ClickHouse Native JSON、Map、JSONB、索引或目标查询在固定版本不可用；
- 写入期间查询结果与已提交水位 truth 不一致；
- 临时数据库对象清理失败；
- 需要修改 exporter、benchmark、容器镜像或完整产品服务才能继续。

失败候选保留已获得的环境、DDL、错误和指标，不进入横向性能排序。

## 7. 报告约束

阶段二报告正文只包含：

1. 两仓检查日期、短提交号与环境版本；
2. 真实 Trace 审计结果和可观测性限制；
3. 统一数据、查询、计时、正确性和恢复契约；
4. 按稳定列、路径过滤和完整 Trace 回查组织的横向结果；
5. 正确性、原文恢复和异常；
6. 动态属性存储建议和仍需系统级验证的事项。

完整提交、运行标识、镜像摘要、文件身份和诊断参数保存在运行清单或实验 README，不在报告正文展开。

报告引用阶段一，不重复阶段一背景或单引擎机制过程。阶段一数据不进入横向比例计算。跨引擎数字只在本阶段相同数据、查询、轮次、返回内容和门禁下比较。

阶段二报告不包含 Full/Core、长 payload 和 asset reference 的性能结论。这些结果由阶段三独立报告。

## 8. 参考资料

- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-09.md)
- [trace-synthesis 跨 backend 可比性分析](https://github.com/zfwang2021/trace-synthesis/blob/ef3be141cc17415de9fb5a9d8003c16a4cd679ac/docs/design/benchmark/cross-backend-comparability-analysis.md)
- [trace-synthesis 并发模式统一设计](https://github.com/zfwang2021/trace-synthesis/blob/ef3be141cc17415de9fb5a9d8003c16a4cd679ac/docs/design/benchmark/concurrency-mode-unification-design.md)
- [trace-synthesis QPS 指标统一设计](https://github.com/zfwang2021/trace-synthesis/blob/ef3be141cc17415de9fb5a9d8003c16a4cd679ac/docs/design/benchmark/qps-metric-unification-design.md)
