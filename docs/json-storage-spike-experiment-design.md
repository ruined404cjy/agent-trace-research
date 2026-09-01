# Agent Trace JSON 存储第一阶段穿刺实验设计

> 状态：实验设计草案
> 日期：2026-09-01
> 范围：多字段 JSON、Full/Core、JSON 长字段与外部引用
> 配套调研：[json-storage-design-survey.md](json-storage-design-survey.md)

## 1. 目标

第一阶段通过小型、可独立执行的实验验证三类存储机制：

1. JSONB 定向索引与 ClickHouse native JSON 动态子列在多字段 JSON 上的差异。
2. Full/Core 物理分层对列表查询、预览读取和完整详情读取的影响。
3. 数据库内联大值与外部 asset 引用在空间、读取和完整性方面的差异。

实验回答机制问题，不对完整产品或数据库作综合排名。现有 exporter、benchmark、
Langfuse 和 Tempo 用于提供实现证据，不要求直接改造、替换或完整复现。

## 2. 当前边界

### 2.1 现有项目状态

当前 exporter 使用单宽表 `events`，以 `tags`、`input`、`output`、`metadata` 四个
JSON 列保存动态内容。默认 `max_attr_value_length=65536` 在入库前截断属性，不能用于
验证数据库的大值容量。exporter 写入 28 列，benchmark v4 database catalog revision
`2026-09-01.6` 仍定义 26 列，当前组合不能形成有效性能基线。

benchmark 已提供查询参数 catalog、查询 type 参数化和 database/Langfuse backend，
但输入覆盖门禁仍处于候选设计阶段。公开数据的字段分布、ERROR 状态、provider 值域和
多模态覆盖不足，不能直接承担本实验的控制变量。

Langfuse 的可复核机制包括 `events_full/events_core`、metadata names/values、Map、
默认 2 MiB field overflow 和 media/object storage。Langfuse 的 input/output 是 String，
该实现不能代表 ClickHouse native JSON。

### 2.2 第一阶段范围

纳入：

- 使用确定性合成数据隔离路径数量、单值长度和总 JSON 大小。
- 使用独立数据库实例和最小 SQL 验证存储机制。
- 记录正确性、载入、查询、空间和关键执行计划。
- 在同一逻辑任务内比较，不统一排名 SQL、TraceQL 和对象下载。

暂不纳入：

- 修改 exporter 或 benchmark 源码。
- 完整部署和复现 Langfuse、Tempo 或 Agent Trace Demo。
- 多节点、高可用、长期容量和生产故障恢复。
- 完整 asset 状态机、权限、保留、删除传播和对象清理。
- KV/EAV、Parquet Variant 和 Trace 专用数据库实现。

这些项目仅在第一阶段结果显示明确需要时进入后续设计。

## 3. 公共数据与测量方法

### 3.1 逻辑记录

所有实验使用相同的逻辑记录：

```text
identity: event_id, trace_id, span_id, parent_span_id
hot: start_time, service_name, type, model, level
metadata: nested heterogeneous JSON
input/output: JSON value or long text
asset: optional content_type, content_length, sha256
```

生成器输出：

- canonical JSONL；
- 数据配置、seed 和输入 SHA-256；
- 每条记录的 canonical JSON hash；
- 查询参数及预期行数；
- asset 原始 bytes hash、MIME 和长度。

canonical hash 忽略 object key 顺序，保留数组顺序，并区分路径 missing、JSON null、
字符串和数值类型。

### 3.2 数据 profile

每个性能 profile 的未压缩输入目标为 128 MiB，允许在 64–256 MiB 范围内调整；通过
行数控制总量，避免不同单值长度直接造成数量级不同的运行成本。

| Profile | 主要变量 | 目的 |
|---|---|---|
| 正确性 | 300 条；missing、null、数组、嵌套、类型冲突 | 校验读写与重建语义 |
| 路径数量 | 50、500、5000 路径；短值 | 观察路径元数据、索引和 shared data |
| 路径密度 | 1%、20%、95% | 区分稀疏路径和稳定路径 |
| 等总大小 | 一个长值与多个短值形成约 512 KiB JSON | 区分字段数量与单值长度 |
| 长字段 | 64 KiB、512 KiB、2 MiB | 观察 LOB、Full/Core 和外部引用 |

媒体只使用少量固定 PNG、短音频和随机二进制验证 hash、MIME 与 resolver。媒体吞吐和
转码不进入第一阶段性能结果。

### 3.3 公共 workload

| 操作 | 测量内容 |
|---|---|
| 批量载入 | wall time、rows/s、MiB/s、峰值 RSS、最终空间 |
| 热点字段过滤 | 独立列或定向路径的过滤、排序和聚合 |
| 冷路径过滤 | 未提示、未建定向索引或进入 shared data 的路径 |
| 整对象读取 | 返回完整 metadata/input/output 并校验 hash |
| 列表与预览 | 返回稳定列及 200 字符 preview |
| 完整解析 | 从 inline、LOB 或 reference 恢复原始内容 |

查询执行一次预热和至少五次测量，报告每次结果、median 和范围。第一阶段不使用少量样本
计算 p99。可获取时记录 `EXPLAIN`、rows scanned、bytes read 和缓存状态；引擎不提供的
指标标为 unavailable，不以估算值代替。

## 4. 实验一：多字段 JSON 的路径组织

### 4.1 比较对象

PostgreSQL 使用同一 `jsonb` 列验证三种布局：

1. 无路径索引；
2. GIN 通用索引；
3. 两个热点路径的表达式索引或生成列索引。

ClickHouse 使用同一 JSON 文本验证三种布局：

1. `String CODEC(ZSTD)`，查询时使用 JSON 提取函数；
2. native `JSON`，设置较小动态路径预算，使 500/5000 路径进入 shared data；
3. native `JSON`，为两个热点路径设置 type hint，并提高动态路径预算。

数据库版本、image digest、JSON 参数和索引 DDL 在 run manifest 中固定。第一阶段不穷举
ClickHouse shared data 的所有序列化选项。

### 4.2 测量与判定

执行路径数量、路径密度和正确性 profile，测量：

- 载入时间和最终表、索引空间；
- 热点等值过滤和聚合；
- 冷路径过滤；
- 整对象读取；
- PostgreSQL GIN 与定向索引大小；
- ClickHouse 动态路径数量、shared data 路径和 merge 前后空间。

该实验区分：

- JSONB 通用索引的灵活性与索引放大；
- 定向索引或强类型列对稳定热点路径的收益；
- native JSON 自动子列对多字段、稀疏路径的收益和路径预算成本；
- String 整段解析在冷路径和整对象读取中的基线行为。

结果必须按机制分别解释。PostgreSQL 与 ClickHouse 的事务、并发和完整 SQL 能力不进入
本实验结论。

## 5. 实验二：Full/Core 物理分层

### 5.1 最小实现

参考 Langfuse 的 `events_full/events_core` 设计，复用实验一的 ClickHouse 实例建立：

```text
events_full
  identity + hot columns
  + input String + output String
  + metadata_names + metadata_values

events_core
  identity + hot columns
  + leftUTF8(input, 200) + leftUTF8(output, 200)
  + metadata_names + arrayMap(value -> leftUTF8(value, 200), metadata_values)
```

`events_core` 使用物化表或 materialized view。普通 view 不进入比较，因为它不能验证
物理空间和读取裁剪。该实验把公共 metadata 确定性转换为 names/values 数组，只复现
Langfuse 的表级机制，不启动 Langfuse web、worker、PostgreSQL 和完整 OTLP 摄入链路。
原始内容长度和 hash 保存在 truth manifest；当前 Langfuse Core 表中的 `input_length` 和
`output_length` 由截断后的字符串派生，不作为原始长度使用。

### 5.2 对照查询

使用等总大小和长字段 profile 比较：

1. Full 表直接计算 preview；
2. Core 表读取已物化 preview；
3. Full 表读取完整内容；
4. Core 先过滤、排序和分页，再按命中 ID 回查 Full。

记录 Full/Core 分项空间、载入写放大、物化可见延迟、bytes read、CPU 和查询时间。
Core 预览必须符合统一的 UTF-8 截断规则，Full 内容 hash 和命中 ID 必须与 truth manifest
一致。

如果稳定列查询已经能完全裁剪大列，报告该事实；Full/Core 的结论只保留 preview 计算、
回查和写放大方面的差异，不把预期收益写成既定结果。

## 6. 实验三：长 payload 的内联与引用

### 6.1 比较对象

复用实验一的 PostgreSQL 实例比较：

1. JSON payload 直接存为 JSONB，由数据库 TOAST 机制管理；
2. 主表保存结构化引用，payload 写入本地对象目录；
3. 已有 MinIO/S3 环境时，把对象目录替换为对象存储，但不把部署作为前置条件。

结构化引用至少包含：

```json
{
  "$ref": "asset:sha256:<digest>",
  "content_type": "application/json",
  "encoding": "utf-8",
  "content_length": 524288,
  "preview": "..."
}
```

非 JSON 文本和二进制只进入正确性 profile，分别使用 TEXT、BYTEA 或外部引用验证 hash、
MIME 和长度，不进入 JSONB 性能比较。本地对象目录用于验证引用与主表分离的读取路径，
不代表对象存储的网络、一致性和权限能力。报告必须明确这一限制。

### 6.2 测量与判定

使用 64 KiB、512 KiB、2 MiB 长字段以及等总大小 profile，测量：

- 主表、LOB/TOAST 和外部对象的分项空间；
- 不读取 payload 的列表查询；
- 读取 inline/LOB 内容的详情查询；
- 读取引用并通过 resolver 恢复内容；
- 数据库备份范围内与外部对象范围外的字节量；
- resolver 前后 SHA-256、MIME 和长度。

第一阶段不实现 pending/available/failed 状态机，不执行上传失败、删除传播和 orphan
清理。若外部引用在主表读取或容量上有明确收益，再设计带状态机和故障注入的后续实验。

## 7. 当前项目条件性参照

现有 exporter 与 benchmark schema 配对完成后，可以把相同数据和三类核心查询运行在
dstore JSON 上，作为当前项目参照。参照运行必须满足：

- exporter 写入列与 catalog、DDL 完全一致；
- 关闭 64 KiB 截断，或把截断 run 单独标识为数据丢失对照；
- schema preflight、输入 hash、写入计数和可见性检查通过；
- 记录 exporter、Collector 和数据库的分段资源指标。

配对未完成时跳过该参照，不阻塞三个独立机制实验。实验设计不要求为此修改 exporter 或
benchmark；需要修改时另行评审改动范围。

## 8. 执行顺序与停止条件

1. 检查数据库和容器能力，固定版本与资源限制。
2. 生成公共数据、truth manifest 和查询参数。
3. 先运行正确性 profile；失败时停止对应候选。
4. 运行多字段 JSON 实验。
5. 复用可用列式实例运行 Full/Core 实验。
6. 运行长 payload 内联与引用实验。
7. schema 配对已解决时补充当前项目参照。

任一候选出现以下情况时停止扩展：

- 无法区分 missing、JSON null 或类型冲突；
- 完整读取 hash 不一致；
- 目标机制在当前版本不可用；
- 为运行一个机制必须先开发完整 backend、worker 或产品服务；
- 小型数据已显示该机制不处理目标问题。

停止的候选保留环境、DDL、错误和已取得指标，不补写性能结论。

## 9. 实验产物

建议将正式脚本与结果放在：

```text
experiments/json-storage-stage1/
  README.md
  generator/
  schemas/
  queries/
  runs/<run-id>/
    manifest.yaml
    dataset.json
    correctness.json
    load.json
    queries.json
    storage.json
    notes.md
```

`manifest.yaml` 记录数据库版本、image digest、CPU/内存限制、DDL hash、数据 hash、seed、
缓存状态、准备命令和异常。比较表引用 run ID，不手工复制未关联的数字。

## 10. 结果解释与后续选项

| 第一阶段证据 | 后续选项 |
|---|---|
| 定向索引已满足热点路径需求 | 优先验证热点列或 residual，不增加通用索引 |
| native JSON 在宽路径上稳定降低读取或空间 | 评估 ClickHouse JSON 或同类自动子列能力 |
| Core 明显降低 preview 查询读取，写放大可接受 | 在目标引擎设计最小物化 Core 原型 |
| 外部引用降低主表压力，resolver 成本可接受 | 设计 asset 状态机、故障注入和保留实验 |
| 各机制差异小于运行波动 | 保持当前简单布局，补充真实 workload 后再评估 |
| 当前项目参照主要受 exporter 或 schema 影响 | 先修摄入和版本契约，不更换数据库 |

Tempo dedicated columns、KV/EAV、Parquet Variant、完整 Langfuse 复现和大规模容量实验
均作为后续选项，不在第一阶段预先排期。

## 11. 参考资料

- [JSON 存储设计调研](json-storage-design-survey.md)
- [Exporter schema](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/SCHEMA.md)
- [Exporter 引擎验证](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/references/engine-verification-2026-08-07.md)
- [Benchmark 设计](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/benchmark/DESIGN.md)
- [Benchmark v4 database catalog](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/benchmark/schema/v4/database/catalog.json)
- [Benchmark 输入覆盖 Draft ADR](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/adr/0037-benchmark-input-coverage-contract.md)
- [Langfuse events_full DDL](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/clickhouse/migrations/clustered/0039_create_events_full.up.sql)
- [Langfuse events_core materialized view](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/clickhouse/migrations/clustered/0041_create_events_core_mv.up.sql)
- [Langfuse field overflow](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/worker/src/features/observation-field-overflow/processObservationFieldOverflow.ts)
- [ClickHouse JSON](https://clickhouse.com/docs/reference/data-types/newjson)
- [PostgreSQL JSON](https://www.postgresql.org/docs/current/datatype-json.html)
- [PostgreSQL TOAST](https://www.postgresql.org/docs/current/storage-toast.html)
