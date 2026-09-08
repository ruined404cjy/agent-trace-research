# Agent Trace JSON 存储阶段一实验设计

> 状态：阶段一已结束；多字段 JSON 机制矩阵已完成，Full/Core 与 asset 实验顺延阶段三
> 初始设计日期：2026-09-04；文档修订日期：2026-09-08
> 范围：多字段 JSON、Full/Core、JSON 长字段与外部引用
> 配套调研：[json-storage-design-survey-2026-09-08.md](json-storage-design-survey-2026-09-08.md)
> 阶段一报告：[json-storage-stage1-report-2026-09-08.md](json-storage-stage1-report-2026-09-08.md)

## 1. 目标

第一阶段通过小型、可独立执行的实验验证三类存储机制：

1. 标准 openGauss JSONB 索引与 ClickHouse native JSON 动态子列各自在多字段 JSON 上的机制和边界。
2. Full/Core 物理分层对列表查询、预览读取和完整详情读取的影响。
3. 数据库内联大值与外部 asset 引用在空间、读取和完整性方面的差异。

实验回答机制问题，不对完整产品或数据库作综合排名。现有 exporter、benchmark、 Langfuse 和 Tempo 用于提供实现证据，不要求直接改造、替换或完整复现。

阶段一实际完成目标 1 的引擎内机制实验。目标 2、3 已形成实验设计，尚未实现 runner 或正式运行；两项目标合并进入 [阶段三实验设计](json-storage-stage3-experiment-design-2026-09-08.md)。阶段二只执行 residual 的 openGauss/ClickHouse 统一横向比较。

## 2. 当前边界

### 2.1 现有项目状态

exporter 的 ADR-0010 在提交 `0c26c9ecf03acf0bd6aa3a3c103ba4e7a78b523a` 冻结 18 列最小 OTel 单表，删除 `tags` 等 10 个 Langfuse 导向列及 split 模型；当前 main 为 `9a49c8a9d6091633112fe793fcf12310859aeb7f`。`input`、`output`、`metadata` 继续保存动态内容。默认 `max_attr_value_length=65536` 在入库前截断属性，不能用于验证数据库的大值容量。

trace-synthesis 当前 main `6472d8e1ac6cdb42494b79b28d4d5361919d4776` 的 v4 database catalog 仍定义 28 列，因此两仓 main 尚未形成联合冻结。系统级回归继续使用已经验证的 benchmark `9529c8f389673132757f4da9a96878926f22b94f` 与 exporter `54ca553a7ed09ad1751c82adab3aa52c6e9357b1`；机制实验使用明确记录为 `independent_loader` 的独立路径。两类结果分别报告。

benchmark 已提供查询参数 catalog、查询 type 参数化和 database/Langfuse backend，但输入覆盖门禁仍处于候选设计阶段。公开数据的字段分布、ERROR 状态、provider 值域和多模态覆盖不足，不能直接承担本实验的控制变量。

Langfuse 的可复核机制包括 `events_full/events_core`、metadata names/values、Map、默认 2 MiB field overflow 和 media/object storage。Langfuse 的 input/output 是 String，该实现不能代表 ClickHouse native JSON。

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

### 2.3 目标工作负载

当前项目按实时分析型、append-heavy OLAP 负载设计：Span/Event 持续分批追加，查询主要在 project/tenant 和时间范围内过滤少量字段并执行分组、计数、分位数和特征统计。指定 `trace_id` 的 Trace 回查和完整 payload 读取属于必要的次级路径。批次幂等、可见水位和内容校验继续作为摄入正确性门禁。

第一阶段 runner 使用一次性批量载入隔离 JSON 机制，只能回答正确性、索引、路径组织和静态空间问题。持续摄入能力需要在第二阶段加入并发查询、part/merge 或 compaction 状态、写入字节和可见延迟后单独判断。

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

canonical hash 忽略 object key 顺序，保留数组顺序，并区分路径 missing、JSON null、字符串和数值类型。

### 3.2 数据 profile

已完成的九组路径 profile 以约 128 MiB 未压缩输入为目标，通过改变行数控制总量。该方法适合限制单次机制实验成本，不适合跨 profile 推导路径基数或密度的性能影响，因为行数会随路径数和密度变化。ClickHouse 补充矩阵已固定 50,000 行和值形式，并把输入字节量作为独立结果记录；热点查询选择性固定，冷路径查询只在同一 profile 内比较布局。

路径实验使用以下变量：

| 符号 | 定义 | 统计范围 |
|---|---|---|
| `P` | 全局不同叶路径数 | tenant/project、Agent/工作流、instrumentation 版本和时间窗口 |
| `W` | 每行实际叶路径数 | p50、p95、p99、最大值 |
| `dᵢ` | 路径 `i` 的非空行数除以窗口总行数 | 每条路径及密度分层 |
| `cᵢ` | 路径 `i` 的 distinct 值数及其相对行数 | 每条路径 |
| `Tᵢ` | 路径 `i` 的类型集合和冲突比例 | 每条路径及 schema epoch |
| `Lᵢ` | 路径 `i` 值长度 | p50、p95、p99、最大值 |
| `Aᵢ` | 路径 `i` 的过滤、排序、分组、聚合和投影访问 | 查询 workload |
| `E` | 相邻窗口新增、删除、类型变化和稳定路径集合 | schema epoch |

| Profile | 主要变量 | 目的 |
|---|---|---|
| 正确性 | 300 条；missing、null、数组、嵌套、类型冲突 | 校验读写与重建语义 |
| 路径数量 | 50、500、5000 路径；短值 | 观察路径元数据、索引和 shared data |
| 路径密度 | 1%、20%、95% | 区分稀疏路径和稳定路径 |
| 等总大小 | 一个长值与多个短值形成约 512 KiB JSON | 区分字段数量与单值长度 |
| 长字段 | 64 KiB、512 KiB、2 MiB | 观察 LOB、Full/Core 和外部引用 |

路径数量与密度矩阵用于观察引擎在受控边界上的行为，不表示 Agent Trace 的已测生产分布。每个动态路径在每 100 行中分别出现 1、20 或 95 次；同组路径采用相同密度，未模拟长尾或 Zipf 分布。`whowhen-pro` text split 的原始属性审计只发现 29 个顶层属性，不能支持 500/5000 路径假设。已下载的 Open-SWE-Traces 是 agent trajectory 数据，顶层及 metadata 使用固定 Parquet schema，也不能直接校准 OTel span attribute 路径。九组产物保留为机制资产；ClickHouse 先用 `50×20%` 和 `500×1%` 完成语义验证，再补充 98/99 路径预算边界、`10×95% + 40×20% + 450×1%` 混合密度以及 `50×95%`、`500×10%`、`5000×1%` 等单行宽度矩阵。真实 Trace 审计完成前，5000 路径只表示压力边界。

后续组合按用途划分：

| 用途 | 组合 | 执行条件 |
|---|---|---|
| 代表性候选 | 50 路径混合密度 | 使用少量 95% 稳定字段、部分 20%–50% 可选字段和大量 1%–5% 长尾字段；真实数据审计后固定比例 |
| 条件性代表候选 | 500 路径混合密度 | tenant/project、Agent/工作流、版本或时间窗口统计支持该基数后运行 |
| 机制梯度 | `50/500 × 1%/5%/20%/50%/95%` | 固定行数、值长和查询选择性；2% 和 10% 只用于细化已发现的转折 |
| 路径预算边界 | 98 和 99 个动态路径加两个未提示热点路径 | 精确验证 limited 布局总路径数 100 和 101 的行为 |
| 等单行宽度 | `50×95%`、`500×10%`、`5000×1%` | 固定行数和值长，每行约 50 个动态字段 |
| 压力边界 | `5000×1%` | 验证大路径 namespace、shared data、GIN 放大和路径元数据 |
| 当前裁剪 | `5000×20%`、`5000×95%` | 数据证据或明确引擎边界目标出现后再运行 |

媒体只使用少量固定 PNG、短音频和随机二进制验证 hash、MIME 与 resolver。媒体吞吐和转码不进入第一阶段性能结果。

### 3.3 公共 workload

| 操作 | 测量内容 |
|---|---|
| 批量载入 | wall time、rows/s、MiB/s、峰值 RSS、最终空间 |
| 热点字段过滤 | 独立列或定向路径的过滤、排序和聚合 |
| 冷路径过滤 | 未提示、未建定向索引或进入 shared data 的路径 |
| 整对象读取 | 返回完整 metadata/input/output 并校验 hash |
| 列表与预览 | 返回稳定列及 200 字符 preview |
| 完整解析 | 从 inline、LOB 或 reference 恢复原始内容 |

正确性查询返回排序后的命中 ID 并与 truth 核对。性能查询使用 `count`、分组或聚合，避免把大量 ID 排序、序列化和网络传输计入路径读取耗时。查询执行一次预热和至少五次测量，报告每次结果、median 和范围；布局执行顺序在三轮独立运行中采用平衡轮换。第一阶段不使用少量样本计算 p99。ClickHouse 使用唯一 query ID 从 `system.query_log` 读取最终 `read_rows`、`read_bytes`、内存和 ProfileEvents；其他引擎记录等价执行指标。引擎不提供的指标标为 unavailable，不以估算值代替。

ClickHouse 补充矩阵已使用 `start_time` 排序键和 50% 时间范围谓词。下一阶段增加 project/tenant 强类型列，并在持续分批写入期间执行相同查询，记录稳态摄入和后台合并后的两组结果；强制 `OPTIMIZE FINAL` 只用于解释物理布局变化，不代表在线稳态。

## 4. 实验一：多字段 JSON 的路径组织

### 4.1 比较对象

标准 openGauss 6.0.0 使用同一 `jsonb` 列验证三种布局：

1. 无路径索引；
2. GIN 通用索引；
3. 一个热点路径的表达式索引或生成列索引。

ClickHouse 使用同一 JSON 文本验证三种布局：

1. `String CODEC(ZSTD)`，查询时使用 JSON 提取函数；
2. native `JSON(max_dynamic_paths=100)` 加压缩 canonical sidecar（列名 `metadata_raw`），使 500 路径进入 shared data；
3. native `JSON(max_dynamic_paths=1000, hot.tenant String, hot.region String)`，固定两个热点路径并提高动态路径预算，同时保存压缩 canonical sidecar。

数据库版本、image digest、JSON 参数和索引 DDL 在 run manifest 中固定。第一阶段不穷举 ClickHouse shared data 的所有序列化选项。ClickHouse 25.12.11.4 使用 `map_with_buckets` 写零层 part，并在多 part 合并后使用 `advanced`；runner 固定上述设置，分别记录 merge 前后的 part、空间和 dynamic/shared 路径数。

### 4.2 测量与判定

执行路径数量、路径密度和正确性 profile，测量：

- 载入时间和最终表、索引空间；
- 热点等值过滤和聚合；
- 冷路径过滤；
- 整对象读取；
- openGauss GIN 与定向索引大小；
- ClickHouse 动态路径数量、shared data 路径和 merge 前后空间。

该实验区分：

- JSONB 通用索引的灵活性与索引放大；
- 定向索引或强类型列对稳定热点路径的收益；
- native JSON 自动子列对多字段、稀疏路径的收益和路径预算成本；
- String 整段解析在冷路径和整对象读取中的基线行为。

结果必须按机制分别解释。openGauss 与 ClickHouse 的事务、并发和完整 SQL 能力不进入本实验结论。

ClickHouse native `JSON` 按叶路径扁平存储，不能称为 PostgreSQL/openGauss 语义的 `JSONB`。首个 50 路径×20% 密度正式组发现两个 native JSON 布局各有 77,562 条完整对象回读差异，均来自空的 `metadata.paths` 被省略；热点和冷路径过滤命中集合仍与 truth 一致。

修正后的门禁区分分析等价和 canonical 文档保真。native JSON 负责路径分析，压缩 canonical sidecar 负责逻辑 metadata 恢复；native JSON 回读差异保留为引擎语义观察。`50×20%` 和 `500×1%` 语义重跑均通过两项门禁。`500×1%` 低预算布局合并后为 100 个 dynamic、402 个 shared 路径，高预算布局为 500 个 dynamic、0 个 shared 路径。

当前 sidecar 由解析后的 metadata 重新序列化，字节级审计和重放需要另存原始输入 bytes。

性能补充实验把正确性 ID 查询与性能查询分离。性能 SQL 使用直接子列语法，在 50% 时间窗口内按热点 region 分组计数；每次使用唯一 query ID，从 `system.query_log` 的 `QueryFinish` 读取最终扫描指标。等宽与混合 profile 分别运行三轮并轮换布局顺序。

预算边界、混合密度和等单行宽度实验达到本阶段目标。limited 布局在 98 条业务路径加两个热点路径时为 100 dynamic / 0 shared，增加一条业务路径后为 100 dynamic / 1 shared。混合密度 profile 让 98 条长尾路径先占满业务路径预算；三轮 merge 均换入 50 条高/中密度路径并换出 50 条长尾路径，排除了按字典序或首次出现顺序保留的解释。最终保留全部 10 条 95% 路径、全部 40 条 20% 路径和 48 条 1% 路径，其余 402 条 1% 路径进入 shared data。固定每行约 50 个动态字段时，路径全集从 50 增至 5000，native merge 中位数从约 0.27–0.29 s 增至约 32 s；直接子列查询显著减少读取量，但完整 native 对象重建比 String 内容读取慢约 65–102 倍，canonical sidecar 恢复到 String 同量级。native 载入吞吐和 sidecar 后总空间高于 String。详细数字见 [9 月 7 日阶段一报告](json-storage-stage1-report-2026-09-08.md)。

## 5. 实验二：Full/Core 物理分层（顺延阶段三）

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

`events_core` 使用物化表或 materialized view。普通 view 不进入比较，因为它不能验证物理空间和读取裁剪。该实验把公共 metadata 确定性转换为 names/values 数组，只复现 Langfuse 的表级机制，不启动 Langfuse web、worker、PostgreSQL 和完整 OTLP 摄入链路。原始内容长度和 hash 保存在 truth manifest；当前 Langfuse Core 表中的 `input_length` 和 `output_length` 由截断后的字符串派生，不作为原始长度使用。

### 5.2 对照查询

使用等总大小和长字段 profile 比较：

1. Full 表直接计算 preview；
2. Core 表读取已物化 preview；
3. Full 表读取完整内容；
4. Core 先过滤、排序和分页，再按命中 ID 回查 Full。

记录 Full/Core 分项空间、载入写放大、物化可见延迟、bytes read、CPU 和查询时间。Core 预览必须符合统一的 UTF-8 截断规则，Full 内容 hash 和命中 ID 必须与 truth manifest 一致。

如果稳定列查询已经能完全裁剪大列，报告该事实；Full/Core 的结论只保留 preview 计算、回查和写放大方面的差异，不把预期收益写成既定结果。

## 6. 实验三：长 payload 的内联与引用（顺延阶段三）

### 6.1 比较对象

复用实验一的 openGauss 实例比较：

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

非 JSON 文本和二进制只进入正确性 profile，分别使用 TEXT、BYTEA 或外部引用验证 hash、 MIME 和长度，不进入 JSONB 性能比较。本地对象目录用于验证引用与主表分离的读取路径，不代表对象存储的网络、一致性和权限能力。报告必须明确这一限制。

### 6.2 测量与判定

使用 64 KiB、512 KiB、2 MiB 长字段以及等总大小 profile，测量：

- 主表、LOB/TOAST 和外部对象的分项空间；
- 不读取 payload 的列表查询；
- 读取 inline/LOB 内容的详情查询；
- 读取引用并通过 resolver 恢复内容；
- 数据库备份范围内与外部对象范围外的字节量；
- resolver 前后 SHA-256、MIME 和长度。

第一阶段不实现 pending/available/failed 状态机，不执行上传失败、删除传播和 orphan 清理。若外部引用在主表读取或容量上有明确收益，再设计带状态机和故障注入的后续实验。

## 7. 当前项目系统级参照

当前项目以蓝区标准开源 openGauss 6.0.0 对标黄区 GaussVector。两端运行同一逻辑数据、 exporter schema 和 benchmark workload。蓝区标准 openGauss 使用行存，黄区 GaussVector 使用其目标存储形态；本阶段把两端作为系统环境比较，不把结果解释为受控的行存与列存机制差异。

18 列冻结与 trace-synthesis 28 列 catalog 配对完成前，使用 benchmark `9529c8f389673132757f4da9a96878926f22b94f` 与 exporter `54ca553a7ed09ad1751c82adab3aa52c6e9357b1` 的已验证历史配对，或运行不依赖列数适配的独立 loader。独立 loader 只形成对应数据库的机制与正确性证据，不形成 exporter、Collector 或 benchmark 的端到端性能结论。

系统级参照运行必须满足：

- exporter 写入列与 catalog、DDL 完全一致；
- 关闭 64 KiB 截断，或把截断 run 单独标识为数据丢失对照；
- schema preflight、输入 hash、写入计数和可见性检查通过；
- 记录 exporter、Collector 和数据库的分段资源指标。

使用独立 loader 时，manifest 必须记录 `data_path=independent_loader`、引用的 schema 提交、 DDL hash 和输入 hash。使用 exporter 与 benchmark 时，报告必须记录两者的精确提交与配对校验结果。

蓝区正确性探针与系统级参照的当前运行记录见 [第一阶段实验基础设施](../experiments/json-storage-stage1/README.md)。

## 8. 执行顺序与停止条件

1. 检查数据库和容器能力，固定版本与资源限制。
2. 生成公共数据、truth manifest 和查询参数。
3. 先在蓝区 openGauss 6.0.0 运行当前 JSON schema 的正确性 profile；失败时停止系统级参照。
4. 黄区可用时使用相同输入和 truth 运行 GaussVector 正确性 profile。
5. 使用已验证提交配对或完成列数适配的版本运行当前项目系统级 workload。
6. 运行多字段 JSON 机制实验。
7. Full/Core 实验未在阶段一执行，按阶段三统一四布局契约实施。
8. 长 payload 内联与引用实验未在阶段一执行，按阶段三原文恢复和 asset 故障门禁实施。
9. 正确性契约明确后，运行带时间范围查询和持续分批摄入的第二阶段对照。

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

`manifest.yaml` 记录数据库版本、image digest、CPU/内存限制、DDL hash、数据 hash、seed、缓存状态、准备命令和异常。比较表引用 run ID，不手工复制未关联的数字。

## 10. 结果解释与后续选项

| 第一阶段证据 | 后续选项 |
|---|---|
| 定向索引已满足热点路径需求 | 优先验证热点列或 residual，不增加通用索引 |
| native JSON 在宽路径上稳定降低读取或空间 | 评估 ClickHouse JSON 或同类自动子列能力 |
| Core 明显降低 preview 查询读取，写放大可接受 | 在目标引擎设计最小物化 Core 原型 |
| 外部引用降低主表压力，resolver 成本可接受 | 设计 asset 状态机、故障注入和保留实验 |
| 各机制差异小于运行波动 | 保持当前简单布局，补充真实 workload 后再评估 |
| 当前项目参照主要受 exporter 或 schema 影响 | 先修摄入和版本契约，不更换数据库 |

Tempo dedicated columns、KV/EAV、Parquet Variant、完整 Langfuse 复现和大规模容量实验均作为后续选项，不在第一阶段预先排期。

预算边界、混合密度和等单行宽度组已经完成。[阶段二](json-storage-stage2-experiment-design-2026-09-08.md)审计真实 Trace 窗口并统一比较“强类型列 + String residual”“强类型列 + Map”和“强类型列 + 有路径预算的 native JSON”，同时加入持续写入、后台 merge 与并发查询。[阶段三](json-storage-stage3-experiment-design-2026-09-08.md)比较同表独立列、独立 payload 表、Full/Core 物化和 asset reference。九组均匀密度产物保留为机制资产；`5000×20%` 和 `5000×95%` 缺少场景依据，不进入阶段一结论。

## 11. 参考资料

- [JSON 存储设计调研](json-storage-design-survey-2026-09-08.md)
- [Exporter 18 列冻结 ADR-0010](https://github.com/labmemW/exporter_demo/blob/0c26c9ecf03acf0bd6aa3a3c103ba4e7a78b523a/docs/adr/0010-otel-minimal-schema.md)
- [当前 Benchmark v4 database catalog](https://github.com/zfwang2021/trace-synthesis/blob/6472d8e1ac6cdb42494b79b28d4d5361919d4776/benchmark/schema/v4/database/catalog.json)
- [Exporter schema](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/SCHEMA.md)
- [Exporter 引擎验证](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/references/engine-verification-2026-08-07.md)
- [Benchmark 设计](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/benchmark/DESIGN.md)
- [Benchmark v4 database catalog](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/benchmark/schema/v4/database/catalog.json)
- [Benchmark 输入覆盖 Draft ADR](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/adr/0037-benchmark-input-coverage-contract.md)
- [Langfuse events_full DDL](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/clickhouse/migrations/clustered/0039_create_events_full.up.sql)
- [Langfuse events_core materialized view](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/clickhouse/migrations/clustered/0041_create_events_core_mv.up.sql)
- [Langfuse field overflow](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/worker/src/features/observation-field-overflow/processObservationFieldOverflow.ts)
- [ClickHouse JSON](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse PostgreSQL CDC JSON/JSONB 映射](https://clickhouse.com/docs/integrations/clickpipes/postgres/faq#how-are-json-and-jsonb-columns-replicated-from-postgres)
- [ClickHouse JSONBench](https://github.com/ClickHouse/JSONBench)
- [PostgreSQL JSON](https://www.postgresql.org/docs/current/datatype-json.html)
- [PostgreSQL TOAST](https://www.postgresql.org/docs/current/storage-toast.html)
