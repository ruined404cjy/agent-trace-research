# Agent Trace JSON 存储阶段二实验设计

> 状态：待执行
> 日期：2026-09-07
> 数据路径：`independent_loader`
> 上游边界：[阶段一报告](json-storage-stage1-report-2026-09-07.md)第 6、7 节

## 1. 目标与证据边界

本实验使用同一 Agent Trace 数据、逻辑记录、查询、正确性门禁和原文恢复契约，比较标准 openGauss 6.0.0 与 ClickHouse 25.12.11.4 的 residual 布局。实验覆盖真实 Trace 分布审计、持续分批写入、写入期间并发查询、后台维护、静态查询和空间统计。

截至 2026-09-07，两仓远端状态为：

| 仓库 | 远端 `main` | 冻结状态 |
|---|---|---|
| exporter_demo | `81b55be6d6912d18c4e2ac7102fd7906e9dac3e8` | SPEC v1.8 冻结 18 列；`schema.go` SHA-256 为 `e624c680a3a0d6e9137075e9c0d49809872a4ec95aed8ad43feab84eef70340b` |
| trace-synthesis | `ef3be141cc17415de9fb5a9d8003c16a4cd679ac` | v4 database catalog revision `2026-09-02.3` 仍定义 28 列；catalog SHA-256 为 `8cddfb40af5bb2df302318a5d64f2906ecf904290ea56cd0d4155a7cbdd4e19c` |

两仓尚未形成联合冻结。实验不经过 Collector、exporter 或 benchmark，不形成当前两仓 `main` 的系统级性能结论。所有 run manifest 记录 `data_path=independent_loader`。

本阶段不修改 exporter_demo、trace-synthesis、数据库容器配置或公开数据集。实验使用现有单机容器，记录其实际资源限制；当前两个容器均未设置显式 CPU 或内存上限。

trace-synthesis 已增加跨 backend 的并发模式、QPS 口径和可比性设计；当前 database 与 Langfuse backend 的 event policy 仍不一致。阶段二采用这些设计中的同输入、同参数计划、同并发模式、阶段屏障、连接复用、计时边界和全成功样本门禁，不复用尚未满足统一语义的系统级结果。

Full/Core、长 payload 和 asset reference 属于独立的物理分层问题，顺延到 [阶段三实验设计](json-storage-stage3-experiment-design.md)。阶段二结果只回答 residual 的跨引擎差异。

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

运行产物位于 gitignored 的 `docs/temp/json-storage-stage2/`。每个产物目录最后写入 `run-manifest.json`；缺少该文件或 `status` 不是 `complete` 的目录不构成有效运行。最终结果写入 `docs/json-storage-stage2-cross-engine-report-YYYY-MM-DD.md`。

## 3. 真实 Trace 分布审计

### 3.1 固定输入

审计输入为 trace-synthesis 已生成的 `whowhen-pro` text split：

| 项目 | 固定值 |
|---|---|
| 输入 | `/home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl` |
| spans | 48,534 |
| traces | 6,257 |
| 数据 SHA-256 | `3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683` |
| 上游 manifest SHA-256 | `f46bbe843c5578faea9ddfb5e8eb3aac8b6dc4c2f4fb89beabab503043505e38` |
| 生成 seed | `42` |
| 生成器基线 | trace-synthesis `6472d8e1ac6cdb42494b79b28d4d5361919d4776` |

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

审计程序的结果决定 native JSON 路径预算。预算取大于等于全局 `P` 的最小 2 的幂，并设置 128 的上限；本数据预期使用 32。预算、实际 `P` 和推导公式同时进入 truth manifest。

公共生成器逐行读取固定输入并输出：

```text
ingest_seq, event_id, trace_id, span_id, parent_span_id,
project_id, start_time, end_time, duration_ms, span_type,
framework, level, attributes_analysis, attributes_map, raw_event
```

- `event_id` 为 `trace_id:span_id`，并执行唯一性门禁。
- `project_id` 固定为数据来源标识 `Leoxx/whowhen_pro`，只用于相同查询选择性，不解释为生产租户。
- `framework` 缺失时写入空字符串；`level` 由 Span status 确定。
- `attributes_analysis` 把点分隔的 Attribute 键可逆投影为嵌套 JSON。生成器拒绝标量/对象前缀冲突，并在 manifest 中保存键映射。
- `attributes_map` 使用原始 Attribute 键作为 Map key，value 为该 Attribute 值的 canonical JSON 字符串，保持类型和数组顺序。
- `raw_event` 为输入 JSONL 去除行结束符后的原始 UTF-8 bytes。

generator 同时输出：

- canonical `dataset.jsonl`；
- `truth-manifest.json`，包含每行身份、analysis/canonical/raw SHA-256、查询参数、最终结果和每个 INSERT block 水位结果；
- `run-manifest.json`，包含输入与产物 SHA-256、字节数、行数、block size、代码 SHA-256 和生成命令。

固定 INSERT block 为 256 行，共 190 个 block。最后一个 block 保存剩余 150 行。各布局使用相同行序和 block 边界。

### 4.1 横向可比性契约

可比性契约版本固定为 `json-storage-cross-engine-v1`，所有完成 manifest 记录以下语义：

- 两引擎使用相同输入、查询 catalog、参数计划、并发数、预热策略和返回内容；
- 每个 worker 在一个阶段内建立并复用一条独立连接；连接建立完成后，全部 worker 通过阶段屏障同时进入预热或正式测量；
- 查询延迟从语句提交开始，至结果完整读取结束；连接建立、正确性校验、结果排序规范化和 hash 计算不进入延迟；
- QPS 定义为 `成功样本数 × 1000 / 成功样本延迟总和(ms)`，报告名称为“请求等价速率”，不解释为饱和吞吐量；
- 正式轮次要求查询和正确性门禁全部成功。失败轮次保存诊断 manifest，重试结果使用新的 run ID，失败轮次不进入横向统计；
- 查询结果先完整读取，再在计时区间外完成规范化和 truth 核对。

## 5. Residual 横向矩阵

### 5.1 布局

所有布局保留相同强类型列。分析表与 raw 表按 `event_id` 一一对应；raw 表只承担字节级恢复，其空间单列并在总空间中计入。

| 引擎 | layout ID | residual 组织 |
|---|---|---|
| openGauss | `og_jsonb` | `JSONB`，无 residual 索引 |
| openGauss | `og_jsonb_hot` | 同一 `JSONB`，增加 `gen_ai.operation.name` 表达式索引 |
| openGauss | `og_jsonb_gin` | 同一 `JSONB`，增加 `jsonb_hash_ops` GIN 包含查询索引 |
| ClickHouse | `ch_string` | nested canonical JSON `String CODEC(ZSTD(3))` |
| ClickHouse | `ch_map` | `Map(String,String)`，value 为 canonical JSON 字符串 |
| ClickHouse | `ch_native` | 审计派生 `max_dynamic_paths` 的 native `JSON`，加稀疏 `fidelity_values Map(String,String)` |

ClickHouse native JSON 负责路径分析。`ch_native` 的 analytics 表同时保存稀疏 `fidelity_values Map(String,String)`：仅当一个原始 Attribute 的值递归包含 `null`、空对象或空数组时，保存该原始 Attribute key 与完整 canonical JSON value。Q04 先从 native JSON residual 恢复，再以该 Map 覆盖对应 key，从而恢复 native JSON 无法区分的状态；该 Map 作为 analytics residual 的可逆状态补充，其字节计入 analytics 空间。raw 表只承担逐行原始 UTF-8 bytes 恢复，native JSON 和 `fidelity_values` 都不承担原文恢复。Map 查询使用原始 Attribute key；String、JSONB 和 native JSON 查询使用嵌套分析路径。

openGauss 6.0.0 的 `jsonb_ops` GIN 在写入空字符串时触发 `jsonb_gin.cpp:519` 的零长度复制错误，递归对象与数组中的空字符串同样受影响。`og_jsonb_gin` 使用可保留这些值的 `jsonb_hash_ops`；Q05 保持 `attributes @> %s::jsonb`，并验证 truth、自然计划和禁用顺扫后的 GIN 计划。DDL 与索引空间记录此 opclass，阶段一 `jsonb_ops` 结果保持其原有实验范围。根因与修复条件见 openGauss [v6.0.0 源码](https://gitee.com/opengauss/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_gin.cpp)和 [v6.0.2 源码](https://gitee.com/opengauss/openGauss-server/blob/v6.0.2/src/common/backend/utils/adt/jsonb_gin.cpp)。

首次 openGauss 正式轮次 `opengauss-r1-04ca47c0b0d8487db41e2d255efb3875` 因该错误失败；其诊断保存在 `docs/temp/json-storage-stage2/formal-20260907/opengauss-round-1/`，按失败轮次规则排除整轮统计。兼容布局的正式轮次使用新的 run ID 与输出目录。

### 5.2 公共查询

查询均限定 `project_id`、固定时间范围和 `ingest_seq < watermark`。正式静态查询使用最终水位 48,534；写入期间查询使用启动查询前已经提交的 block 水位，因此后续 INSERT 不改变该次查询的 truth。

| ID | 语义 | 返回内容 |
|---|---|---|
| Q01 | 50% 时间窗口内按 `span_type` 分组 | 排序后的 `span_type,count` |
| Q02 | `gen_ai.operation.name = execute_tool`，按 `span_type` 分组 | 排序后的 `span_type,count` |
| Q03 | 50% 时间窗口按 `span_type` 计算 count、sum(duration_ms) | 排序后的聚合行 |
| Q04 | 固定代表性 `trace_id` 回查完整 Trace | 按 `start_time,event_id` 排序的身份和 attributes 内容摘要 |
| Q05 | `failure.mistake_mode = A.3` 的低密度路径过滤 | count 和 identity digest |

生成器独立计算每个查询在最终水位和每个 block 水位的预期结果及 SHA-256。runner 只消费 truth，不复用 SQL 结果计算期望值。

### 5.3 写入、并发和静态测量

每个布局执行三轮，布局顺序采用平衡轮换。每轮流程为：

1. 创建唯一临时 schema 或 database，记录 DDL SHA-256。
2. 按 256 行 block 持续写入并逐 block 提交。
3. 前五个 block 提交后启动两个查询 worker，循环执行 Q01、Q02、Q03、Q05；每次固定启动前水位并核对对应 truth。
4. 记录 190 个 block 的 wall time、rows/s、MiB/s、p50、p95、p99 和可见延迟。
5. 写入期间记录查询 wall time、p50、p95、p99、错误数及结果门禁。
6. 写入结束后等待 ClickHouse merge backlog 连续三次为零或达到 120 秒上限；openGauss 完成 `ANALYZE`。等待行为计入后台维护，不计入载入时间。
7. 每条查询预热一次并正式测量 100 次；记录每次 wall time、median、p95、p99、执行计划和引擎可提供的 read rows/bytes、CPU、内存。
8. 核对行数、缺失/额外/重复 ID、analysis canonical hash、raw SHA-256 和 Q01～Q05 truth。
9. 记录分析表、raw 表、索引/part 的分项空间，以及 ClickHouse active part、merge backlog、dynamic/shared path 数。
10. 清理临时 schema 或 database；清理成功后发布完成 manifest。

缓存状态固定为 `query_warmup_1_no_os_cache_drop`。实验不清理宿主机页缓存。两引擎顺序执行，另一个容器保持空闲；manifest 记录宿主 CPU、内存、磁盘和容器资源配置。并发与静态查询均遵循 `json-storage-cross-engine-v1` 的连接、阶段屏障、计时和成功样本规则。

## 6. Manifest 与停止条件

run manifest 至少记录：

- run ID、状态、开始/结束时间、完整复现命令和异常；
- research、exporter_demo、trace-synthesis 的本地 HEAD 与远端 `main`；
- 数据路径、输入文件、输入/数据/truth SHA-256、seed、行数和 block 边界；
- DDL、查询 catalog 和 runner SHA-256；
- 数据库版本、镜像 digest、容器配置、端口和宿主资源；
- layout、运行轮次、布局顺序、缓存状态和后台维护等待结果；
- `comparability_contract_version`、连接复用方式、阶段屏障、延迟边界和 QPS 语义；
- correctness、raw recovery、cleanup 的完成状态；
- 指标文件名、字节数和 SHA-256。

出现以下情况时停止对应候选并发布 `status=failed` 的诊断 manifest：

- 输入或上游 manifest SHA-256 不一致；
- 点键嵌套投影出现前缀冲突；
- 任一 truth、identity、canonical hash 或 raw SHA-256 门禁失败；
- native JSON、Map、JSONB、索引或目标查询在固定版本不可用；
- 写入期间查询结果与已提交水位 truth 不一致；
- 临时数据库对象清理失败；
- 需要修改 exporter、benchmark、容器镜像或完整产品服务才能继续。

失败候选保留已获得的环境、DDL、错误和指标，不进入横向性能排序。

## 7. 报告约束

阶段二报告只包含：

1. 两仓与环境基线增量；
2. 真实 Trace 审计结果和可观测性限制；
3. 统一契约与 run ID；
4. residual 横向结果；
5. 正确性、原文恢复和异常；
6. residual 布局建议和仍需系统级验证的事项。

报告引用阶段一，不重复阶段一背景或单引擎机制过程。阶段一数据不进入横向比例计算。跨引擎数字只在本阶段相同数据、查询、轮次、返回内容和门禁下比较。

阶段二报告不包含 Full/Core、长 payload 和 asset reference 的性能结论。这些结果由阶段三独立报告。

## 8. 参考资料

- [阶段一报告](json-storage-stage1-report-2026-09-07.md)
- [JSON 存储设计调研](json-storage-design-survey.md)
- [trace-synthesis 跨 backend 可比性分析](https://github.com/zfwang2021/trace-synthesis/blob/ef3be141cc17415de9fb5a9d8003c16a4cd679ac/docs/design/benchmark/cross-backend-comparability-analysis.md)
- [trace-synthesis 并发模式统一设计](https://github.com/zfwang2021/trace-synthesis/blob/ef3be141cc17415de9fb5a9d8003c16a4cd679ac/docs/design/benchmark/concurrency-mode-unification-design.md)
- [trace-synthesis QPS 指标统一设计](https://github.com/zfwang2021/trace-synthesis/blob/ef3be141cc17415de9fb5a9d8003c16a4cd679ac/docs/design/benchmark/qps-metric-unification-design.md)
