# Agent Trace JSON 存储设计调研

> 状态：调研结论，供方案选择和穿刺实验使用
> 调研日期：2026-09-01
> 范围：多字段 JSON、JSON 内长值、Trace 大 payload、热点字段、半结构化查询
> 配套实验设计：[json-storage-spike-experiment-design.md](json-storage-spike-experiment-design.md)

## 1. 结论

JSON 存储方案需要同时回答三个独立问题：

1. **逻辑 schema**：哪些字段具有稳定语义，哪些字段保持动态。
2. **物理组织和查询加速**：整段存储、热点列、Map/KV、自动子列、索引和轻量投影如何组合。
3. **大值生命周期**：长文本、长 JSON 值和二进制对象放在哪里，如何寻址、校验、读取、保留和删除。

`JSONB`、热点列和对象存储分别作用于上述不同层次。`JSONB` 主要减少重复解析并支持路径索引；热点列减少常用查询的读取和解析；对象存储控制大值对主表、缓存、网络和保留流程的影响。单独采用其中一项无法覆盖多字段大 JSON 和特定长字段。

当前项目适合验证以下分层模型：

- 稳定且高频过滤、排序、聚合的字段使用独立强类型列。
- 动态属性保留在 residual JSON 或 Map 中；已提升字段从 residual 中移除，读取层按需重建完整对象。
- 列表页和常规分析使用 Core 投影，完整详情读取 Full 数据。
- 超过阈值的单个长值和多模态内容进入统一 asset 层，主表保留结构化引用。
- asset 引用携带内容类型、编码、长度、内容哈希、来源字段和状态。长文本与媒体共用物理设施，并保持业务语义可区分。

该模型仍需通过实验确定热点字段集合、长值阈值、residual 形态和 Full/Core 的物化方式。当前 exporter 的 64 KiB 截断发生在数据库容量边界之前，会直接丢失内容，不适合作为长期大 payload 方案。

## 2. 两类“大 JSON”

### 2.1 多字段导致整体较大

一条 JSON 包含数百到数千路径，每个值较短。主要压力来自：

- schema 和路径元数据增长；
- 稀疏列、动态类型和字段冲突；
- 任意路径过滤需要扫描或索引大量键值；
- 读取少量字段时产生整段解析和读取放大；
- 全量展开为物理列后产生列数、统计信息和 compaction 压力。

这类数据需要热点列、动态子列、Map/KV 或 residual 设计。仅把超长值移到对象存储不能缓解路径数量问题。

### 2.2 少数字段值很长

JSON 的 `input`、`output`、工具返回、堆栈、文档正文或 base64 媒体达到 MiB 级。主要压力来自：

- 行或 granule 过大，主表缓存命中下降；
- 压缩字典受高基数长字符串影响；
- 列表查询误读完整内容；
- 传输、序列化、重试和内存峰值增加；
- 保留、删除、权限和内容去重需要独立生命周期。

这类数据适合数据库内部 LOB/TOAST、Full/Core 投影或对象引用。对象引用方案还需解决一致性、孤儿对象、下载鉴权和引用解析。

### 2.3 二者的联系和边界

JSON 内的长字符串在物理层属于大 payload，可与图片、音频和其他文件共用对象存储、内容寻址、校验和清理设施。两者在语义层存在差异：

| 维度 | JSON 长字段 | 图片、音频等媒体 |
|---|---|---|
| 常用读取 | 路径读取、文本检索、详情展开 | 下载、流式读取、预览 |
| 内容类型 | `text/plain`、`application/json` | `image/*`、`audio/*` 等 |
| 局部查询 | 可能需要保留摘要或提取字段 | 通常只查元数据 |
| 重建要求 | 恢复原 JSON 结构和缺失/null 语义 | 恢复二进制字节和媒体元数据 |
| 阈值依据 | 查询热度、解析和行宽 | 网络、文件大小和媒体处理 |

统一 asset 层应把 `origin`、`field_path`、`content_type` 和 `encoding` 作为显式元数据。这样可以共享设施，同时保持读取与治理策略独立。

## 3. 存储设计与代表性实现

### 3.1 设计模式

#### 3.1.1 整段 JSON 文本或二进制 JSON

`JSON` 文本保留输入文本，写入成本低，路径读取需要解析。二进制 JSON/JSONB 在写入时解析为内部结构，读取路径更快，并可配置通用或定向索引。

PostgreSQL 的 `json` 保存原始文本，`jsonb` 保存分解后的二进制结构；通用 GIN 索引复制全部键和值，定向表达式索引只覆盖指定路径，通常更小、查询更快。MySQL JSON 使用二进制格式，JSON 列通过生成列或表达式间接建立索引。

适用场景：字段数量适中，需要事务读写或灵活路径查询。主要代价是整段存储、写入解析和索引放大。JSONB 没有定义对象存储、媒体、跨层删除和下载协议。

#### 3.1.2 热点强类型列与完整 JSON 并立

同一值同时保留在强类型列和完整 JSON 中。应用双写、生成列、表达式索引或物化列均可实现。

优点是兼容完整原始对象，查询稳定字段无需解析。代价是物理重复、写放大和一致性维护。生成列及表达式索引由数据库维护一致性；应用双写需要约束或校验。该模式适合先保留原始输入，再逐步确认热点字段的阶段。

当前 exporter 已经具有这种模式的一部分，但 `metadata` 保留了所有属性，提升规则和重复范围没有显式契约。

#### 3.1.3 热点列与 residual JSON/Map

热点值移动到独立列，剩余动态属性进入 residual。读取完整对象时合并两部分。Tempo dedicated columns、Sinew physical column + column reservoir、Parquet Variant shredding 都体现了该模式。

优点是稳态不重复热点值，动态字段仍可容纳。代价集中在：

- 写入侧需要确定字段归属；
- schema 迁移期间需要处理部分物化状态；
- 完整对象读取需要重建；
- null、missing、类型冲突和路径转义必须有精确定义。

Parquet Variant 规定 shredded 字段与 residual object 的键集合互斥，reader 负责重建；类型不匹配的值留在通用 `value`。这比应用约定更完整地定义了迁移和重建语义。

#### 3.1.4 自动子列化与稀有路径共享区

系统自动分析路径，把常见路径变成物理子列，把超出预算或稀有路径放进共享结构。

ClickHouse 原生 JSON 按 data part 管理动态路径，默认最多 1024 个动态路径；merge 时优先保留非 null 值较多的路径，稀有路径进入 shared data。shared data 在内存中是路径到二进制值的 `Map(String,String)`，磁盘有三种序列化：

- `map`：写入和整段读取较好，单路径读取需要扫描共享 Map；
- `map_with_buckets`：增加写入成本，单路径只读一个 bucket；
- `advanced`：单路径读取最好，写入和空间成本最高。

Apache Doris Variant 也把叶路径列式化，并将高度稀疏路径重新打包到 JSONB 共享列。该类方案减少人工 schema 管理，代价是写入类型推断、物理元数据、merge/compaction 和整对象重建。路径上限、稀疏阈值和类型冲突必须纳入测试。

#### 3.1.5 Map/KV 与 flattened object

动态属性统一存成 Map、键值数组、EAV 子表或搜索引擎 flattened 字段。该模式用一个 schema 容纳大量未知键，避免每个键都进入全局 mapping。

Langfuse 把 metadata 存为 `metadata_names Array(String)` 与 `metadata_values Array(String)`，usage/cost 等使用 Map。Elasticsearch `flattened` 用一个 mapping 处理整个对象的叶值，主要按 keyword 语义查询。Databricks 文档把 Map/Array 作为约 500 字段场景的平衡方案，同时指出内部字段缺少统计信息。

优势是 schema 稳定、写入简单。限制包括弱类型、键和值配对约束、路径级统计不足、范围比较和聚合能力下降。EAV 子表还会增加行数、join 和批量写入成本。

#### 3.1.6 Full/Core 双层投影

Full 保存完整内容；Core 保存常用列和截断预览。列表、搜索和聚合主要读取 Core，详情读取 Full。

该模式控制结果集和存储读取放大，与热点字段提取相互独立。普通 SQL view 只改变列选择，无法证明物理读取减少；有效实验需要物化 Core 表、列存投影或能提供等价物理裁剪的引擎机制。

Langfuse v4 是当前最直接的实现样本，详见第 3.2.1 节。

#### 3.1.7 数据库内部 LOB/TOAST

数据库把大字段压缩或移到关联的内部表/页，主行保留短指针。PostgreSQL TOAST 通常在行宽约 2 KiB 后触发，out-of-line 值被切成约 2 KiB chunk，主行磁盘指针为 18 字节。读取未选择大值的查询可以保持较小主表工作集。

该方案对应用透明，事务、备份和删除语义统一。它仍位于数据库容量、复制和备份范围内，也不自动提供媒体 MIME、内容去重、预签名下载和跨服务访问。

#### 3.1.8 外部对象引用

大字段或媒体进入本地文件系统、S3/MinIO 等对象存储，数据库保存 URI、token 或 asset ID。MongoDB GridFS 使用 `files` 与 `chunks` 集合存储超过 BSON 16 MiB 限制的文件，默认 chunk 为 255 KiB。Langfuse 使用媒体表、对象存储和内联引用 token。

该模式减少主表大值压力，适合大对象、流式读取和独立生命周期。实现必须覆盖：

- upload-first、record-first 或 outbox 的一致性策略；
- 内容哈希、幂等和同内容复用；
- 状态机、重试和失败回退；
- 引用解析、鉴权、签名 URL 与缓存；
- 数据保留、软删除、引用计数和孤儿清理；
- 备份恢复时数据库与对象的联合一致性。

### 3.2 代表性实现与横向比较

#### 3.2.1 Langfuse：热点列、Map、Full/Core 与对象化长字段

本地 Langfuse v4 的 `events_full` 采用以下组合：

- Trace/Span、模型、usage/cost、service 等高频字段为独立列；
- `input`、`output` 为 `String CODEC(ZSTD(3))`，并有物化长度列；
- metadata 使用 names/values 两个数组；usage/cost 使用 Map；
- `events_core` 由 materialized view 写入，input/output 和每个 metadata value 截为前 200 个 UTF-8 字符；
- 大行配置把 `index_granularity_bytes` 和 merge block 调为 64 MiB；
- input、output 或任一 metadata value 超过默认 2 MiB 时，可上传到 media bucket，并替换为 `@@@langfuseMedia:...@@@` 引用；该功能默认关闭；
- 上传批内并发为 3；上传失败时保留原值；
- Media 表记录 SHA-256、project、bucket path/name、content type、content length 和上传状态，project + hash 唯一；关联表记录 field 与 origin。

源码证据：

- [events_full DDL](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/clickhouse/migrations/clustered/0039_create_events_full.up.sql)
- [events_core materialized view](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/clickhouse/migrations/clustered/0041_create_events_core_mv.up.sql)
- [长字段 overflow 处理](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/worker/src/features/observation-field-overflow/processObservationFieldOverflow.ts)
- [阈值配置](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/worker/src/env.ts)
- [Media schema](https://github.com/langfuse/langfuse/blob/add6ca4aceb949905df887b88cac619756e003b7/packages/shared/prisma/schema.prisma)

Langfuse 同时覆盖“JSON 长字段”和“媒体大 payload”，两者共用 media/asset 设施；`contentType` 与 `origin` 区分文本 overflow 和媒体提取。它没有把 input/output 改为 ClickHouse 原生 JSON，也没有通过自动子列化解决数千 metadata 路径问题。Full/Core 与对象化长字段分别控制常规查询读取和极端字段大小。

#### 3.2.2 PostgreSQL/MySQL：JSONB/二进制 JSON 与定向索引

PostgreSQL 提供文本 `json`、二进制 `jsonb`、GIN 以及表达式索引，并由 TOAST 透明处理宽值。MySQL JSON 通过生成列或表达式建立定向索引。两者适合事务型路径查询和少量确定热点路径。

相较热点列 + residual：

- 通用 JSON 索引保持查询灵活，但索引复制范围大；
- 定向表达式索引接近热点字段方案，表面 schema 变化较少；
- 强类型独立列更适合排序、聚合、统计信息和跨字段约束；
- 内部 LOB 保护主行，仍会把大值纳入数据库复制、备份和容量管理。

#### 3.2.3 ClickHouse JSON：逻辑单列、内部自动分层

ClickHouse 25.3 起把开源 JSON 类型标记为 production ready。它把路径拆为子列，允许 type hint、`SKIP` 和动态路径/类型上限。逻辑上仍是一列，物理上形成热点动态子列与 shared data。

该方案最适合字段形态变化快且有路径分析需求的日志/事件。与手工热点列相比，运维 schema 负担较低；写入、存储和整对象读取成本更高。当前 Langfuse 的 `events_full` 不能代表 ClickHouse 原生 JSON，二者必须作为不同实验候选。

#### 3.2.4 Grafana Tempo：Trace 专用 Parquet 分层

Tempo 把 Trace 写成对象存储中的 Parquet block。intrinsic 字段为顶层列，其余属性默认在通用 `Attrs` 中；配置的 dedicated attributes 获得独立 Parquet 列。vParquet5 每个 span/resource/event scope 最多支持 20 个 string 和 5 个 int dedicated attributes。文档建议 int 属性在至少 5% 行出现时再提升。

长、高基数字符串可设为 blob dedicated column。默认每 row group 字典估算超过 4 MiB 时视为候选，编码从字典切换为 ZSTD。这里的 blob 是 Parquet 列编码选择；Tempo 对象存储保存整个 block，不是每个长字段各自保存 URI。

Tempo 适合验证“Trace 原生列式布局、热点属性和对象存储 block”的效果。它不属于当前关系型 Demo 的 SQL drop-in backend。

#### 3.2.5 Elasticsearch、Snowflake、BigQuery、Databricks

这些系统展示了多字段 JSON 的不同控制面：

| 系统 | 主要机制 | 主要目标 | 关键限制 |
|---|---|---|---|
| Elasticsearch | dynamic mapping、targeted mapping、`flattened` | 搜索和任意键摄入 | 默认总字段上限 1000；flattened 叶值主要按 keyword 处理 |
| Snowflake | VARIANT 自动提取为内部子列 | 列式分析和 schema-on-read | 含 JSON null 或混合类型的元素可能不提取，查询需扫描完整结构 |
| BigQuery | 原生 JSON 字段独立编码和处理 | 托管分析 | JSON 类型缺少相等/比较运算，不能直接作为分区或聚簇列 |
| Databricks | Struct、Map/Array、Variant 分级建议 | Delta 上的半结构化分析 | Struct 超过数百列可能退化；Map 内部缺少统计信息 |

共同结论是：已知且高频的过滤/分区键应进入强类型列；动态部分采用专用半结构化类型；极宽 schema 需要路径预算或共享结构。

#### 3.2.6 论文与开放格式

- Dremel 证明嵌套记录的列式 shredding/reassembly 可以在只读分析中减少无关列读取，是 Parquet 嵌套编码的重要基础。
- Sinew 在 RDBMS 上使用物理列和 column reservoir。catalog 统计路径密度与基数，后台 materializer 在二者间移动值；迁移中的 dirty 列由查询改写同时读取物理列和 reservoir。
- 面向 schemaless LSM 文档库的后续研究把 Dremel 扩展到异构类型和 LSM 生命周期，在 AsterixDB 实验中报告数量级查询改进和较小摄入影响。
- Parquet Variant shredding 把常用路径写入 typed column，把类型不匹配和剩余对象留在通用 value；规范定义 missing、null、部分 shredding、重建和跨文件 schema 冲突。

这些工作说明“热点列 + residual”可以由应用、存储引擎或文件格式实现。当前项目在应用层原型中应先固定重建语义和 workload，再评估是否值得向引擎能力演进。

### 3.3 方案能力比较

| 方向 | 写入效率 | 热点路径效率 | 冷路径效率 | 整对象效率 | schema 演进能力 | 长字段治理能力 |
|---|---:|---:|---:|---:|---:|---:|
| JSON 文本 | ★★★★★ | ★☆☆☆☆ | ★☆☆☆☆ | ★★★★★ | ★★★★★ | ★☆☆☆☆ |
| JSONB + 通用索引 | ★☆☆☆☆ | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ | ★★★★★ | ★☆☆☆☆ |
| JSONB + 定向索引/生成列 | ★★★☆☆ | ★★★★★ | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ | ★☆☆☆☆ |
| 热点列 + 完整 JSON | ★★★☆☆ | ★★★★★ | ★★★☆☆ | ★★★★★ | ★★★☆☆ | ★☆☆☆☆ |
| 热点列 + residual | ★☆☆☆☆ | ★★★★★ | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ | ★☆☆☆☆ |
| 自动子列 + shared data | ★☆☆☆☆ | ★★★★★ | ★★★☆☆ | ★★★☆☆ | ★★★★★ | ★☆☆☆☆ |
| Map/flattened/EAV | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ | ★★★★★ | ★☆☆☆☆ |
| Full/Core | ★☆☆☆☆ | ★★★★★ | ★★★☆☆ | ★★★★★ | ★★★☆☆ | ★★★☆☆ |
| 内部 LOB/TOAST | ★★★☆☆ | ★★★☆☆ | ★★★☆☆ | ★★★★★ | ★★★★★ | ★★★☆☆ |
| 对象引用 | ★☆☆☆☆ | ★★★★★ | ★☆☆☆☆ | ★☆☆☆☆ | ★★★★★ | ★★★★★ |

星级越高表示该维度的能力或效率越高。表中评分根据各机制的数据组织和读写路径作出，
属于定性比较，并非当前项目实测结果。组合方案的实际结果取决于数据分布、查询选择性、
物化方式和引擎实现。

“热点路径读取”与“大字段治理”是两条正交轴。实际方案通常需要从每条轴各选一层，例如“热点列 + residual JSON + Core 投影 + 对象引用”。

## 4. 面向当前项目的候选设计

### 4.1 当前项目 JSON 存储基线

当前 `events` 使用单宽表保存稳定 Trace/Span 列，并以 `tags`、`input`、`output`、
`metadata` 四个 JSON 列承载动态属性和模型输入输出。生产 profile 已验证 dstore
列存 `JSON`，尚未使用 `JSONB`；JSON 路径读取在执行阶段解析。dstore 也支持
`TEXT[]`、`VARCHAR[]` 及其数组操作符，但 Array 与 JSON 在现有布局下都需要扫描整列，
benchmark 也没有 tag 过滤 workload，因此 `tags` 继续使用 JSON。

exporter 把全部 span/event 属性写入 `metadata`，同时把部分 GenAI 输入输出提升到
`input`、`output`，尚未定义热点字段与 residual 的互斥或重建契约。默认
`max_attr_value_length=65536` 在入库前截断属性；JSON 列整体超限时只保存截断标记，
没有可恢复引用。现有实现也未提供物化 Core/Full、asset 状态机和多模态对象生命周期。
引擎验证已覆盖 50 MiB JSON 和更大的 TEXT，因此 64 KiB 是 exporter 策略，不能表示
数据库的容量边界。

现有 benchmark workload 包含 JSON 路径过滤、动态属性聚合、文本查询和 light/full
投影，并使用查询 type 参数化、参数 catalog 以及 trace 长度、payload 大小等参数分层
保证查询参数可复现。输入覆盖尚未成为运行门禁。对 `whowhen-pro` text split 的覆盖审计
显示，6,257 条 trace 都带 `failure.root_span`，只有 232 条投影出 ERROR status，
`gen_ai.provider.name` 只有 `unknown` 一个值；顺序 `--limit` 还可能只截取单一 framework。
当前默认输入仍是 text split，多模态 sidecar 契约处于 Draft 状态。因此现有数据不能
直接作为多字段 JSON、长 payload 和多模态存储的完整基线。

数据集与报告尚未覆盖以下设计变量：

- 50、500、5000 路径的字段数量、稀疏度和类型冲突；
- JSON null、路径缺失、嵌套结构和同名字段的正确性；
- 单个长值与多个短值在相同总大小下的差异；
- 2 MiB 以上长值、对象引用、多模态内容和对象存储故障；
- 主表、索引、residual、Core 和对象存储的分项空间。

### 4.2 建议的逻辑模型

```text
events_core
  trace/span 稳定列 + 常用维度 + input/output preview + 长度/引用状态

events_full
  trace/span 稳定列 + residual metadata + input/output inline-or-reference

assets
  asset_id + project_id + sha256 + content_type + encoding
  + content_length + storage_uri + status + created_at

event_assets
  project_id + trace_id + span_id + field_path + asset_id + origin
```

该逻辑模型不预设具体引擎实现。`events_core` 可由物化表、物化视图或引擎投影实现；`assets` 可先使用 MinIO/S3 兼容接口验证。
普通 SQL view 只改变列选择，无法保证减少物理读取和缓存占用；Core 层需要使用独立
物化表、物化视图或具备等价物理裁剪能力的引擎投影。

### 4.3 热点字段判定

热点字段应由实际查询和数据分布共同确定：

- 查询出现频率；
- 过滤、排序、group by、join 或分区需求；
- 非 null 密度；
- 类型稳定性；
- 基数与可用统计信息；
- 从 residual 提升后的空间和写放大。

首批候选包括现有同级列、`provider.name`、模型、token/cost、错误级别、service 信息。Tempo 的 5% 密度和 Sinew 的密度/基数策略可作为实验起点，不能直接作为生产阈值。

### 4.4 residual 契约

实验需要比较两种规则：

- **copy**：热点字段仍保留在原 JSON；兼容简单，存在重复和一致性成本。
- **move**：热点字段从 residual 移除；空间更小，reader 负责重建。

若选择 move，契约必须定义路径转义、数组、类型冲突、JSON null、missing、同名字段优先级和 schema version。Parquet Variant 的互斥键与 reader 重建规则可作为参考。

schema 引入和迁移阶段可先采用 copy，以完整 JSON 对账并保留回滚能力；稳态空间优化
再切换为 move。字节级原始输入若用于审计或重放，应单独保存为 raw payload 或 asset，
不由查询用 residual 同时承担归档职责。

逻辑正确性契约还应规定：object key 顺序不参与业务相等判断，数组顺序保留；路径
missing、JSON null 与 SQL NULL 分别表示缺失路径、显式空值和整列缺失；数值类型、
重复键、路径转义和类型冲突进入 truth manifest。嵌套 map/slice 应保持结构化 JSON，
继续字符串化属于需要单独验证的兼容行为。

### 4.5 长值引用契约

主表建议存结构化引用对象，避免只存裸 URI：

```json
{
  "$ref": "asset:sha256:<digest>",
  "content_type": "application/json",
  "encoding": "utf-8",
  "content_length": 3145728,
  "preview": "..."
}
```

该结构支持本地/对象存储迁移、完整性检查和统一 resolver。具体字段名、阈值和事务顺序由穿刺实验确定。

长文本、JSON 长值和媒体可共用内容寻址、上传、校验、关联和清理设施，并通过
`content_type`、`encoding`、`field_path` 与 `origin` 区分语义。asset 至少需要
`pending`、`available`、`failed`、`deleting` 状态。原型可采用 upload-first 并定期
核对孤儿对象与缺失引用；需要可恢复跨系统写入时再评估事务 outbox。

resolver 应按 project/tenant 鉴权并生成受限下载地址。脱敏、加密、访问审计、保留期
和删除传播需要覆盖 Full、Core、索引、缓存、备份及对象副本。

## 5. 工程验证与决策边界

### 5.1 基线与比较边界

可复现基线需要同时固定数据集与生成参数、Collector/exporter 版本和配置、schema
及索引、引擎 build 与 storage profile、查询 workload 和运行环境。每次运行还需保存
输入 hash、truth manifest、DDL/catalog hash、二进制 hash 和正确性结果。当前
exporter 写入 28 列，benchmark v4 database catalog revision `2026-09-01.6` 仍定义
26 列；匹配 schema 前产生的结果不能进入性能比较。

现有 database 与 Langfuse backend 的路径分别为：

```text
database: OTLP -> Collector/exporter -> openGauss/GaussDB
langfuse: OTLP -> Langfuse ingestion/worker -> ClickHouse/PostgreSQL/MinIO
```

端到端结果属于系统级比较，包含接收、队列、转换、schema 和存储实现的共同影响。
数据库 JSON 能力比较需要使用独立 loader，或采集分段时延、CPU 和 bytes read，拆分
摄入层与数据库执行。系统级结果与引擎级结果分别报告。

详细数据规格、workload、指标和运行门槛见配套的
[JSON 存储穿刺与对比实验设计](json-storage-spike-experiment-design.md)。

### 5.2 证据与修改层次

| 观测 | 需要补充的证据 | 优先修改层次 |
|---|---|---|
| 数据到达数据库前已截断或无效 | exporter marker 数、原始/写入 hash | exporter 转换和长值策略 |
| JSON 路径过滤读取大量无关字节 | query plan、bytes read、路径密度 | 热点列、定向索引、residual 或自动子列 |
| light/list 查询受 input/output 长度影响 | light/full bytes read 与延迟曲线 | 物化 Core/Full 分层 |
| 路径数增加导致元数据或 compaction 急剧增长 | 50/500/5000 路径实验 | Map/shared data、路径预算或专用半结构化类型 |
| 单字段达到 MiB 后内存、网络和行宽上升 | payload 阶梯、LOB/对象分项 | LOB 或 asset reference |
| Trace 树、跨 span 关系查询长期依赖复杂重建 | 目标查询集、关系型执行计划、总成本 | Trace 专用布局或专用数据库评估 |

修改顺序遵循证据所在层次。exporter 截断、schema 版本不一致和查询误读大列
应在数据库选型之前解决，否则更换引擎后仍会保留相同问题。

### 5.3 Schema 演进与引擎选择

当前 append-only events、批次指纹和 `events_dedup` 的幂等语义应扩展到 Core、KV
和 asset 表。schema 变更遵循以下顺序：

1. 发布带版本的 schema/catalog，并记录 exporter commit 与 DDL hash。
2. 增加兼容列或表，通过双写或回填记录迁移水位。
3. 使用 truth manifest 校验新旧读取结果、计数和 canonical hash。
4. 切换读取面并保留回滚窗口，稳定后停止旧写入。

启动时 schema preflight 应拒绝不兼容组合。关系型引擎仍满足稳定列查询和事务要求时，
优先调整 schema 与 exporter。完成热点列、residual、Full/Core 和 asset 的单变量验证后，
若动态路径规模、列式裁剪或 Trace 关系查询仍超出目标，再比较 ClickHouse、Tempo、
文档/搜索引擎或专用存储。更换或开发数据库的收益还需覆盖迁移、查询改写、运维、
备份恢复和长期维护成本。

## 6. 遗留问题

| 优先级 | 尚未确定的问题 | 所需证据或决策 |
|---|---|---|
| P0 | 可比较基线采用哪组 exporter 与 schema/catalog，以及 64 KiB 截断是否启用 | 精确提交映射、DDL/catalog hash、schema preflight、原始与入库 hash |
| P0 | 真实 Trace 的字段数量、稀疏度、类型冲突、Trace 宽度、payload 分布和查询频率 | 脱敏样本或生产统计直方图，以及可复现的查询样本 |
| P1 | dstore JSON 的解析、压缩、LOB 隔离和路径读取成本 | 路径数量与 payload 阶梯下的执行计划、bytes read、CPU 和分项空间 |
| P1 | 哪些路径需要提升，以及动态属性采用 Map、自动子列、KV 还是 residual | 路径密度、基数、类型稳定性、读写成本和结构化嵌套兼容性 |
| P1 | Core 使用物化视图、双写表还是引擎投影 | 三种机制的写放大、可见性、回填、查询读取量和恢复行为 |
| P1 | asset 的内联阈值、失败回退和跨系统一致性策略 | 长值大小曲线、故障注入、孤儿/缺失引用核对和恢复目标 |
| P2 | 脱敏、密钥、租户隔离、保留期限和删除传播规则 | 部署安全策略、合规要求及覆盖数据库、缓存、备份和对象副本的验证 |

## 7. 资料来源

### 7.1 本地项目资料

- [Exporter schema 说明](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/SCHEMA.md)
- [自研引擎验证报告](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/references/engine-verification-2026-08-07.md)
- [Exporter 与 Langfuse v4 字段对照](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/docs/references/langfuse-v4-vs-exporter-demo-field-mapping-2026-08-28.md)
- [dstore tags Array 探针](https://github.com/labmemW/exporter_demo/blob/a0b3441d473d5cb4fd7c06767d12b9f611521b9e/scripts/perf/probe-tags-array-xstore.sql)
- [大 payload 与多模态 Trace 调研](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/report/large-payload-multimodal-trace.md)
- [Benchmark v4 database catalog](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/benchmark/schema/v4/database/catalog.json)
- [Benchmark v4 Langfuse catalog](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/benchmark/schema/v4/langfuse/catalog.json)
- [Trace synthesis 架构与决策状态](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/architecture.md)
- [Benchmark query type 参数化 ADR](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/adr/0036-benchmark-query-type-parameterization.md)
- [Benchmark 输入覆盖 Draft ADR](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/adr/0037-benchmark-input-coverage-contract.md)
- [多模态 sidecar Draft ADR](https://github.com/zfwang2021/trace-synthesis/blob/3d4ef6235fbc28d1465daba756a26e18d8bf9366/docs/adr/0031-whowhen-pro-multimodal-sidecar-contract.md)

### 7.2 官方文档和开放源码

- [ClickHouse JSON data type](https://clickhouse.com/docs/reference/data-types/newjson)
- [Grafana Tempo dedicated attribute columns](https://grafana.com/docs/tempo/latest/operations/dedicated_columns/)
- [Grafana Tempo block format](https://grafana.com/docs/tempo/latest/reference-tempo-architecture/block-format/)
- [PostgreSQL JSON types](https://www.postgresql.org/docs/current/datatype-json.html)
- [PostgreSQL TOAST](https://www.postgresql.org/docs/current/storage-toast.html)
- [MySQL secondary indexes and generated columns](https://dev.mysql.com/doc/refman/8.4/en/create-table-secondary-indexes.html)
- [Elasticsearch flattened field](https://www.elastic.co/docs/reference/elasticsearch/mapping-reference/flattened)
- [Elasticsearch mapping explosion](https://www.elastic.co/docs/troubleshoot/elasticsearch/mapping-explosion)
- [Snowflake semi-structured considerations](https://docs.snowflake.com/en/user-guide/semistructured-considerations)
- [BigQuery JSON data](https://docs.cloud.google.com/bigquery/docs/json-data)
- [Databricks semi-structured data](https://docs.databricks.com/aws/en/semi-structured/)
- [MongoDB document size](https://www.mongodb.com/docs/v8.0/core/document/)
- [MongoDB GridFS](https://www.mongodb.com/docs/manual/core/gridfs/)
- [Apache Parquet Variant shredding](https://parquet.apache.org/docs/file-format/types/variantshredding/)
- [Apache Iceberg Variant](https://iceberg.apache.org/blog/variant-in-apache-iceberg/)
- [Apache Doris Variant](https://doris.apache.org/blog/variant-in-apache-doris-2.1/)

### 7.3 论文

- Melnik et al., [Dremel: Interactive Analysis of Web-Scale Datasets](https://research.google/pubs/dremel-interactive-analysis-of-web-scale-datasets/), 2010/2011.
- Tahara, Diamond, Abadi, [Sinew: A SQL System for Multi-Structured Data](https://www.cs.umd.edu/~abadi/papers/sinew-sigmod14.pdf), SIGMOD 2014.
- Alkowaileet, Carey, [Columnar Formats for Schemaless LSM-based Document Stores](https://arxiv.org/abs/2111.11517), 2021.
