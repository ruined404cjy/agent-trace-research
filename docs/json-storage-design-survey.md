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

## 3. 当前项目基线

### 3.1 代码版本和资料状态

本次检查使用以下本地 checkout：

| 仓库 | 分支 | 本地提交 | 状态 |
|---|---|---|---|
| `exporter_demo` | `main` | `4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b` | 工作树干净 |
| `trace-synthesis` | `main` | `e0b9c83e3bd8bd7bb78d68225f29df0753f5432e` | 工作树干净 |
| `langfuse` | `main` | `983c2a6e5bbe9e8f35fe10eb017c9abd6220833b` | 工作树干净 |

2026-09-01 初次对三个仓库执行 `git pull --ff-only` 时，GitHub HTTPS 在 TLS
握手阶段返回 `OpenSSL SSL_connect: SSL_ERROR_SYSCALL`。后续重试中 Langfuse
拉取成功，`main` 仍为表中提交，并获取 `v4.25.0`、`v4.26.0` tag；exporter 与
trace-synthesis 仍未刷新远端引用。后两个提交是本地可复核基线，不代表重新确认的
远端 HEAD。外部设计事实使用同日访问的官方文档和论文核验。

### 3.2 exporter 的现有行为

当前 `events` 是单宽表，包含稳定 Trace/Span 列以及 `tags`、`input`、`output`、`metadata` 四个 JSON 列。生产 profile 使用自研引擎 dstore 列存，已验证的列存类型为 `JSON`，未包含 `JSONB`。实现见：

- [schema.go](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/schema.go)
- [convert.go](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/convert.go)
- [config.go](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/config.go)
- [引擎验证报告](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/references/engine-verification-2026-08-07.md)

摄入侧行为如下：

1. `attrsToJSON` 把全部 span/event 属性写入 `metadata`，没有排除已经提升为同级列的属性。
2. generation 的输入、输出又从 `gen_ai.input.messages` 和 `gen_ai.output.messages` 提取到独立 JSON 列。相同内容可能同时出现在 `metadata` 与 `input`/`output`。
3. 默认 `max_attr_value_length=65536`。字符串按 UTF-8 字节预算截断；bytes 先转 base64 再截断；Map/Slice 先序列化成 JSON 字符串再截断。
4. 单个 JSON 列整体超过限制时，整列替换为 `{"_truncated":true,"_original_bytes":N}`。
5. 截断标记没有保留对象引用，原内容无法恢复。

引擎验证覆盖 1 KiB 至 50 MiB JSON、约 1 GiB TEXT、JSON 嵌套和列存 LOB 隔离；JSON 路径读取在执行时解析。该证据说明 exporter 的 64 KiB 限制主要是应用策略，不能代表引擎单字段容量。容量可写也不等于常规查询适合内联读取大值。

### 3.3 exporter 与 benchmark 的版本不一致

exporter 当前 `events` 写入列共 28 个，新增了 `service_name` 和 `service_version`。benchmark v4 database catalog 的 `events` 仍为 26 列，revision 为 `2026-08-31.3`，其 `cleanup_gaussdb.sql` 也会重建 26 列表：

- [exporter schema](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/schema.go)
- [benchmark catalog](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/schema/v4/database/catalog.json)
- [benchmark cleanup](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/schema/v4/database/cleanup_gaussdb.sql)

catalog 记录 schema version、revision、catalog hash 和 exporter 名称，但 `source_evidence.files` 为空，也没有 exporter commit。运行报告可以记录 schema/catalog 和运行进程信息；[E2E 版本 manifest](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/e2e-version-manifest.yaml)仍是待填写模板。当前协调机制依赖人工选择精确版本和 preflight schema check，尚未形成 exporter commit 到 catalog revision 的可执行映射。

因此，后续存储实验必须先填写版本矩阵，并使用匹配的 exporter 与 catalog。直接组合上述两个本地 HEAD 会在 schema preflight 或插入阶段失败。

### 3.4 benchmark 的覆盖缺口

现有 v4 workload 已覆盖 JSON 路径过滤、动态 JSON 聚合、文本查询以及 light/full 投影查询，具备扩展基础。当前数据和报告尚未系统覆盖：

- 50、500、5000 路径的字段数量阶梯；
- 路径稀疏度、类型冲突、JSON null 与缺失值；
- 单长值与多小值形成同等总大小时的差异；
- 2 MiB 以上长值、对象存储引用和多模态数据；
- 主表、索引、residual、Core 表与对象存储的分项空间；
- 对象上传失败、重复内容、孤儿清理和保留期一致性。

## 4. 设计模式分类

### 4.1 整段 JSON 文本或二进制 JSON

`JSON` 文本保留输入文本，写入成本低，路径读取需要解析。二进制 JSON/JSONB 在写入时解析为内部结构，读取路径更快，并可配置通用或定向索引。

PostgreSQL 的 `json` 保存原始文本，`jsonb` 保存分解后的二进制结构；通用 GIN 索引复制全部键和值，定向表达式索引只覆盖指定路径，通常更小、查询更快。MySQL JSON 使用二进制格式，JSON 列通过生成列或表达式间接建立索引。

适用场景：字段数量适中，需要事务读写或灵活路径查询。主要代价是整段存储、写入解析和索引放大。JSONB 没有定义对象存储、媒体、跨层删除和下载协议。

### 4.2 热点强类型列与完整 JSON 并立

同一值同时保留在强类型列和完整 JSON 中。应用双写、生成列、表达式索引或物化列均可实现。

优点是兼容完整原始对象，查询稳定字段无需解析。代价是物理重复、写放大和一致性维护。生成列及表达式索引由数据库维护一致性；应用双写需要约束或校验。该模式适合先保留原始输入，再逐步确认热点字段的阶段。

当前 exporter 已经具有这种模式的一部分，但 `metadata` 保留了所有属性，提升规则和重复范围没有显式契约。

### 4.3 热点列与 residual JSON/Map

热点值移动到独立列，剩余动态属性进入 residual。读取完整对象时合并两部分。Tempo dedicated columns、Sinew physical column + column reservoir、Parquet Variant shredding 都体现了该模式。

优点是稳态不重复热点值，动态字段仍可容纳。代价集中在：

- 写入侧需要确定字段归属；
- schema 迁移期间需要处理部分物化状态；
- 完整对象读取需要重建；
- null、missing、类型冲突和路径转义必须有精确定义。

Parquet Variant 规定 shredded 字段与 residual object 的键集合互斥，reader 负责重建；类型不匹配的值留在通用 `value`。这比应用约定更完整地定义了迁移和重建语义。

### 4.4 自动子列化与稀有路径共享区

系统自动分析路径，把常见路径变成物理子列，把超出预算或稀有路径放进共享结构。

ClickHouse 原生 JSON 按 data part 管理动态路径，默认最多 1024 个动态路径；merge 时优先保留非 null 值较多的路径，稀有路径进入 shared data。shared data 在内存中是路径到二进制值的 `Map(String,String)`，磁盘有三种序列化：

- `map`：写入和整段读取较好，单路径读取需要扫描共享 Map；
- `map_with_buckets`：增加写入成本，单路径只读一个 bucket；
- `advanced`：单路径读取最好，写入和空间成本最高。

Apache Doris Variant 也把叶路径列式化，并将高度稀疏路径重新打包到 JSONB 共享列。该类方案减少人工 schema 管理，代价是写入类型推断、物理元数据、merge/compaction 和整对象重建。路径上限、稀疏阈值和类型冲突必须纳入测试。

### 4.5 Map/KV 与 flattened object

动态属性统一存成 Map、键值数组、EAV 子表或搜索引擎 flattened 字段。该模式用一个 schema 容纳大量未知键，避免每个键都进入全局 mapping。

Langfuse 把 metadata 存为 `metadata_names Array(String)` 与 `metadata_values Array(String)`，usage/cost 等使用 Map。Elasticsearch `flattened` 用一个 mapping 处理整个对象的叶值，主要按 keyword 语义查询。Databricks 文档把 Map/Array 作为约 500 字段场景的平衡方案，同时指出内部字段缺少统计信息。

优势是 schema 稳定、写入简单。限制包括弱类型、键和值配对约束、路径级统计不足、范围比较和聚合能力下降。EAV 子表还会增加行数、join 和批量写入成本。

### 4.6 Full/Core 双层投影

Full 保存完整内容；Core 保存常用列和截断预览。列表、搜索和聚合主要读取 Core，详情读取 Full。

该模式控制结果集和存储读取放大，与热点字段提取相互独立。普通 SQL view 只改变列选择，无法证明物理读取减少；有效实验需要物化 Core 表、列存投影或能提供等价物理裁剪的引擎机制。

Langfuse v4 是当前最直接的实现样本，详见第 5.1 节。

### 4.7 数据库内部 LOB/TOAST

数据库把大字段压缩或移到关联的内部表/页，主行保留短指针。PostgreSQL TOAST 通常在行宽约 2 KiB 后触发，out-of-line 值被切成约 2 KiB chunk，主行磁盘指针为 18 字节。读取未选择大值的查询可以保持较小主表工作集。

该方案对应用透明，事务、备份和删除语义统一。它仍位于数据库容量、复制和备份范围内，也不自动提供媒体 MIME、内容去重、预签名下载和跨服务访问。

### 4.8 外部对象引用

大字段或媒体进入本地文件系统、S3/MinIO 等对象存储，数据库保存 URI、token 或 asset ID。MongoDB GridFS 使用 `files` 与 `chunks` 集合存储超过 BSON 16 MiB 限制的文件，默认 chunk 为 255 KiB。Langfuse 使用媒体表、对象存储和内联引用 token。

该模式减少主表大值压力，适合大对象、流式读取和独立生命周期。实现必须覆盖：

- upload-first、record-first 或 outbox 的一致性策略；
- 内容哈希、幂等和同内容复用；
- 状态机、重试和失败回退；
- 引用解析、鉴权、签名 URL 与缓存；
- 数据保留、软删除、引用计数和孤儿清理；
- 备份恢复时数据库与对象的联合一致性。

## 5. 代表性实现对比

### 5.1 Langfuse：热点列、Map、Full/Core 与对象化长字段

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

- [events_full DDL](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/packages/shared/clickhouse/migrations/clustered/0039_create_events_full.up.sql)
- [events_core materialized view](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/packages/shared/clickhouse/migrations/clustered/0041_create_events_core_mv.up.sql)
- [长字段 overflow 处理](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/worker/src/features/observation-field-overflow/processObservationFieldOverflow.ts)
- [阈值配置](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/worker/src/env.ts)
- [Media schema](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/packages/shared/prisma/schema.prisma)

Langfuse 同时覆盖“JSON 长字段”和“媒体大 payload”，两者共用 media/asset 设施；`contentType` 与 `origin` 区分文本 overflow 和媒体提取。它没有把 input/output 改为 ClickHouse 原生 JSON，也没有通过自动子列化解决数千 metadata 路径问题。Full/Core 与对象化长字段分别控制常规查询读取和极端字段大小。

### 5.2 PostgreSQL/MySQL：JSONB/二进制 JSON 与定向索引

PostgreSQL 提供文本 `json`、二进制 `jsonb`、GIN 以及表达式索引，并由 TOAST 透明处理宽值。MySQL JSON 通过生成列或表达式建立定向索引。两者适合事务型路径查询和少量确定热点路径。

相较热点列 + residual：

- 通用 JSON 索引保持查询灵活，但索引复制范围大；
- 定向表达式索引接近热点字段方案，表面 schema 变化较少；
- 强类型独立列更适合排序、聚合、统计信息和跨字段约束；
- 内部 LOB 保护主行，仍会把大值纳入数据库复制、备份和容量管理。

### 5.3 ClickHouse JSON：逻辑单列、内部自动分层

ClickHouse 25.3 起把开源 JSON 类型标记为 production ready。它把路径拆为子列，允许 type hint、`SKIP` 和动态路径/类型上限。逻辑上仍是一列，物理上形成热点动态子列与 shared data。

该方案最适合字段形态变化快且有路径分析需求的日志/事件。与手工热点列相比，运维 schema 负担较低；写入、存储和整对象读取成本更高。当前 Langfuse 的 `events_full` 不能代表 ClickHouse 原生 JSON，二者必须作为不同实验候选。

### 5.4 Grafana Tempo：Trace 专用 Parquet 分层

Tempo 把 Trace 写成对象存储中的 Parquet block。intrinsic 字段为顶层列，其余属性默认在通用 `Attrs` 中；配置的 dedicated attributes 获得独立 Parquet 列。vParquet5 每个 span/resource/event scope 最多支持 20 个 string 和 5 个 int dedicated attributes。文档建议 int 属性在至少 5% 行出现时再提升。

长、高基数字符串可设为 blob dedicated column。默认每 row group 字典估算超过 4 MiB 时视为候选，编码从字典切换为 ZSTD。这里的 blob 是 Parquet 列编码选择；Tempo 对象存储保存整个 block，不是每个长字段各自保存 URI。

Tempo 适合验证“Trace 原生列式布局、热点属性和对象存储 block”的效果。它不属于当前关系型 Demo 的 SQL drop-in backend。

### 5.5 Elasticsearch、Snowflake、BigQuery、Databricks

这些系统展示了多字段 JSON 的不同控制面：

| 系统 | 主要机制 | 主要目标 | 关键限制 |
|---|---|---|---|
| Elasticsearch | dynamic mapping、targeted mapping、`flattened` | 搜索和任意键摄入 | 默认总字段上限 1000；flattened 叶值主要按 keyword 处理 |
| Snowflake | VARIANT 自动提取为内部子列 | 列式分析和 schema-on-read | 含 JSON null 或混合类型的元素可能不提取，查询需扫描完整结构 |
| BigQuery | 原生 JSON 字段独立编码和处理 | 托管分析 | JSON 类型缺少相等/比较运算，不能直接作为分区或聚簇列 |
| Databricks | Struct、Map/Array、Variant 分级建议 | Delta 上的半结构化分析 | Struct 超过数百列可能退化；Map 内部缺少统计信息 |

共同结论是：已知且高频的过滤/分区键应进入强类型列；动态部分采用专用半结构化类型；极宽 schema 需要路径预算或共享结构。

### 5.6 论文与开放格式

- Dremel 证明嵌套记录的列式 shredding/reassembly 可以在只读分析中减少无关列读取，是 Parquet 嵌套编码的重要基础。
- Sinew 在 RDBMS 上使用物理列和 column reservoir。catalog 统计路径密度与基数，后台 materializer 在二者间移动值；迁移中的 dirty 列由查询改写同时读取物理列和 reservoir。
- 面向 schemaless LSM 文档库的后续研究把 Dremel 扩展到异构类型和 LSM 生命周期，在 AsterixDB 实验中报告数量级查询改进和较小摄入影响。
- Parquet Variant shredding 把常用路径写入 typed column，把类型不匹配和剩余对象留在通用 value；规范定义 missing、null、部分 shredding、重建和跨文件 schema 冲突。

这些工作说明“热点列 + residual”可以由应用、存储引擎或文件格式实现。当前项目在应用层原型中应先固定重建语义和 workload，再评估是否值得向引擎能力演进。

## 6. 横向比较

| 方向 | 写入成本 | 热点路径读取 | 冷路径读取 | 整对象读取 | schema 演进 | 长字段治理 |
|---|---:|---:|---:|---:|---:|---:|
| JSON 文本 | 低 | 差 | 差 | 好 | 好 | 弱 |
| JSONB + 通用索引 | 中至高 | 中 | 中 | 中 | 好 | 弱 |
| JSONB + 定向索引/生成列 | 中 | 好 | 中 | 中 | 中 | 弱 |
| 热点列 + 完整 JSON | 中 | 好 | 中 | 好 | 中 | 弱 |
| 热点列 + residual | 中至高 | 好 | 中 | 需重建 | 中 | 弱 |
| 自动子列 + shared data | 高 | 好 | 中 | 需重建 | 好 | 弱 |
| Map/flattened/EAV | 中 | 中 | 中 | 中 | 好 | 弱 |
| Full/Core | 写放大 | 好 | 取决于 Full | 好 | 中 | 中 |
| 内部 LOB/TOAST | 透明至中 | 与 JSON 形态相关 | 与 JSON 形态相关 | 好 | 好 | 中 |
| 对象引用 | 上传和协调成本 | 依赖摘要/提取列 | 需解析引用 | 需远端读取 | 好 | 强 |

“热点路径读取”与“大字段治理”是两条正交轴。实际方案通常需要从每条轴各选一层，例如“热点列 + residual JSON + Core 投影 + 对象引用”。

## 7. 面向当前项目的候选设计

### 7.1 建议的逻辑模型

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

### 7.2 热点字段判定

热点字段应由实际查询和数据分布共同确定：

- 查询出现频率；
- 过滤、排序、group by、join 或分区需求；
- 非 null 密度；
- 类型稳定性；
- 基数与可用统计信息；
- 从 residual 提升后的空间和写放大。

首批候选包括现有同级列、`provider.name`、模型、token/cost、错误级别、service 信息。Tempo 的 5% 密度和 Sinew 的密度/基数策略可作为实验起点，不能直接作为生产阈值。

### 7.3 residual 契约

实验需要比较两种规则：

- **copy**：热点字段仍保留在原 JSON；兼容简单，存在重复和一致性成本。
- **move**：热点字段从 residual 移除；空间更小，reader 负责重建。

若选择 move，契约必须定义路径转义、数组、类型冲突、JSON null、missing、同名字段优先级和 schema version。Parquet Variant 的互斥键与 reader 重建规则可作为参考。

### 7.4 长值引用契约

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

## 8. 工程问题及现有结论

### 8.1 测试基线由什么组成

可复现基线是以下内容的联合身份：

| 层次 | 必须固定的内容 |
|---|---|
| 数据 | 数据集、生成参数、随机 seed、输入文件 hash、truth manifest |
| 摄入 | Collector 配置、exporter commit、二进制 hash、队列、批次、并发和重试参数 |
| schema | schema version、catalog revision/hash、DDL、索引、读写表面 |
| 引擎 | 产品和 build、storage profile、实例资源、关键配置 |
| 查询 | workload、SQL/TraceQL、参数选择性、cold/warm 状态、ANALYZE/compaction 状态 |
| 报告 | 完整命令、起止时间、运行 ID、日志和分项资源指标 |

当前 exporter 与 benchmark 不能组成有效的 database 性能基线。exporter 写入
28 列，benchmark v4 database catalog 和 cleanup 仍定义 26 列；直接组合会先遇到
schema 错误。基线建立的第一项工作是选择匹配提交或同步 catalog，并填写实际的
E2E version manifest。只有 schema preflight、发送确认清单、数据库可见性和去重
结果全部通过后，性能数据才可进入横向比较。

### 8.2 当前 benchmark 比较的是数据库还是完整系统

当前两个 backend 的数据路径不同：

```text
database: OTLP -> Collector/exporter -> openGauss/GaussDB
langfuse: OTLP -> Langfuse ingestion/worker -> ClickHouse/PostgreSQL/MinIO
```

直接结果属于**系统级比较**，同时包含接收端、队列、批处理、转换、schema、
存储引擎和查询实现的差异。需要回答“数据库 JSON 能力差异”时，应建立独立
loader 或采集分段时延、CPU 和 bytes read，把 exporter/Langfuse ingestion 与
数据库执行分开。系统级结果和引擎级结果分别报告，二者不合并排名。

### 8.3 如何定位瓶颈并选择修改层次

| 观测 | 需要补充的证据 | 优先修改层次 |
|---|---|---|
| 数据到达数据库前已截断或无效 | exporter marker 数、原始/写入 hash | exporter 转换和长值策略 |
| exporter CPU、RSS 或队列等待高，数据库空闲 | 转换、序列化、flush 分段指标 | exporter 批处理、并发和序列化 |
| JSON 路径过滤读取大量无关字节 | query plan、bytes read、路径密度 | 热点列、定向索引、residual 或自动子列 |
| light/list 查询受 input/output 长度影响 | light/full bytes read 与延迟曲线 | 物化 Core/Full 分层 |
| 路径数增加导致元数据或 compaction 急剧增长 | 50/500/5000 路径实验 | Map/shared data、路径预算或专用半结构化类型 |
| 单字段达到 MiB 后内存、网络和行宽上升 | payload 阶梯、LOB/对象分项 | LOB 或 asset reference |
| Trace 树、跨 span 关系查询长期依赖复杂重建 | 目标查询集、关系型执行计划、总成本 | Trace 专用布局或专用数据库评估 |

修改顺序遵循证据所在层次。exporter 截断、schema 版本不一致和查询误读大列
应在数据库选型之前解决，否则更换引擎后仍会保留相同问题。

### 8.4 JSON 正确性语义如何定义

当前 exporter 已经形成以下可观察语义：

- 整个 JSON 列缺失时写 SQL `NULL`；JSON 内显式 null 保持 JSON null。
- OTel attribute map 的键唯一；`attrsToJSON` 生成新的 metadata JSON。
- metadata 中的嵌套 map/slice 会先序列化，再作为 **JSON 字符串**写入，结构类型
  不再作为嵌套 JSON 保留。
- 字符串形式的 input/output 只执行 JSON 合法性检查，合法内容按原始字节写入；
  map/slice 形式由 `json.Marshal` 生成。
- 非法 input/output 写 `_invalid_json` 标记；整列超限写 `_truncated` 标记，原始
  内容无法恢复。

长期契约应明确：object key 顺序不属于业务语义，数组顺序属于业务语义；missing、
JSON null 和 SQL NULL 分别表示缺失路径、显式空值和整列缺失；数值类型、路径转义、
重复键和类型冲突进入 truth manifest。嵌套 map/slice 的字符串化需要作为兼容行为
单独测试，再决定保留或迁移为结构化 JSON。

### 8.5 热点字段采用 copy 还是 move

两种形态适用于不同阶段：

- schema 引入和迁移阶段使用 copy，保留完整 JSON，同时写热点列，便于回滚和
  对账；数据库生成列或表达式索引可以降低双写漂移风险。
- 稳态空间优化使用 move，热点路径从 residual 移除，由 reader 按 schema version
  重建完整逻辑对象。
- 字节级原始输入具有审计或重放价值时，单独保存 raw payload 或 asset。raw payload
  与查询用 residual 分工，避免让查询 JSON 永久承担原文归档职责。

当前 exporter 没有 residual 排除清单，也没有重建 reader，现状属于未定义重复范围
的 copy。E5 应先用已有同级列实现 copy/move 对照，再决定稳态形态。

### 8.6 Full/Core 是否需要物化

需要。普通 view 只改变 SQL 投影，不能证明物理读取、缓存占用和存储布局得到改善。
有效对比需要独立 Core 表、物化视图或具备等价物理裁剪能力的引擎投影。Core 保存
常用列、preview、完整长度和引用状态；Full 保存完整 inline 内容或 asset reference。
列表、搜索和常规分析读 Core，详情、重放和审计读 Full。

### 8.7 写入正确性和 schema 演进如何处理

当前 exporter 采用 append-only events、批次指纹和 `events_dedup`：同一批重发由
`ingest_batches` 阻止，查询按 `(trace_id, span_id)` 选择最大 `event_version`。
该语义需要和 JSON schema 一起固定，避免新增 Core、KV 或 asset 表后出现不同去重
口径。

schema 变更按以下顺序执行：

1. 发布新 schema/catalog，并建立 exporter commit 到 catalog revision 的显式映射。
2. 先增加兼容列或表，不删除旧读取面。
3. exporter 双写或后台回填，记录迁移水位和 schema version。
4. 使用 truth manifest 对比新旧读取结果、计数和 canonical hash。
5. 切换 benchmark 和服务读取面，保留回滚窗口。
6. 稳定后停止旧写入，再清理旧数据。

启动时的 schema preflight 应拒绝不兼容组合。运行报告还需记录 exporter commit 和
二进制 hash，补足当前只记录 catalog 身份的缺口。

### 8.8 长字段和媒体共用 asset 后如何保证一致性

长文本、长 JSON 值和媒体可共用内容寻址、对象存储、校验、关联和清理设施；
`content_type`、`encoding`、`field_path` 和 `origin` 保持语义可区分。

原型阶段适合使用 upload-first：对象上传成功后写事件引用，并通过 reconcile 查找
孤儿对象和缺失引用。它的实现范围最小，可以直接验证主表减负和 resolver。生产阶段
若要求摄入低延迟和可恢复的跨系统状态转换，应评估事务 outbox。任何策略都必须让
pending、failed、available、deleting 状态可查询，上传失败不得静默变成成功。

对象上传失败时保留原值可以保护数据，但会让主表重新承受极端大值。兜底内联上限、
死信、告警和重试次数应形成独立配置。

### 8.9 安全与保留的已知要求

input、output、工具参数、检索文档和媒体可能包含凭据、个人信息和业务数据。无论内容
位于 JSON、LOB 还是对象存储，都需要按 project/tenant 隔离，并使脱敏、加密、访问
审计、保留期和删除传播覆盖 Full、Core、索引、缓存、备份与对象副本。对象引用不能
直接暴露永久公开 URI；resolver 应执行访问检查并生成受限下载地址。

具体分类规则、密钥管理、保留期限和合规要求依赖项目部署策略，列入遗留问题。

### 8.10 何时修改 schema，何时更换或开发专用数据库

满足以下条件后再进入数据库替换决策：

1. 已建立正确且稳定的 E0，排除 exporter 截断、版本不一致和错误查询面。
2. 已完成热点列、residual、Full/Core 和 asset 的单变量实验，确认瓶颈仍位于引擎。
3. 候选引擎在相同逻辑 workload、正确性和资源预算下产生稳定收益。
4. 收益覆盖数据迁移、查询改写、运维、备份恢复、监控和人员学习成本。

关系型引擎仍满足稳定列查询和事务要求时，优先调整 schema 与 exporter。动态路径
规模、列式裁剪或 Trace 关系查询持续超出目标引擎能力时，再评估 ClickHouse、Tempo、
文档/搜索引擎或新开发的 Trace 专用存储。自行开发数据库还需要现有引擎无法通过扩展
或组合满足核心 workload 的证据，并单独评估多年维护成本。

### 8.11 性能结论至少需要哪些指标

延迟和 rows/s 只能描述结果，不能定位原因。每个 run 至少记录：

- 摄入 rows/s、MiB/s、p50/p95/p99 和队列等待；
- 查询 p50/p95/p99、rows scanned、bytes read、bytes returned；
- exporter、数据库和对象存储的 CPU、RSS、磁盘和网络；
- Full、Core、residual/shared data、索引、LOB/KV 和对象存储的分项空间；
- compaction、merge、ANALYZE、索引构建和 schema materialization 成本；
- 截断、上传、复用、失败、孤儿和 resolver 指标；
- correctness hash、预期计数和版本 manifest。

## 9. 遗留问题

| 优先级 | 尚未确定的问题 | 所需证据或决策 | 对应实验/动作 |
|---|---|---|---|
| P0 | exporter 28 列与 benchmark 26 列采用哪个匹配版本 | 远端更新、提交映射、catalog 和 DDL hash | 先完成版本同步与 schema preflight |
| P0 | `CONTEXT.md`、旧蓝/黄区指南仍使用三表术语，当前 exporter 已采用 events 单宽表 | 确定历史指南保留方式和当前权威入口 | 更新领域语言与文档状态标记 |
| P0 | 真实 Trace 的字段数、稀疏度、类型冲突、Trace 宽度和 payload 分布 | 脱敏样本或生产统计直方图 | 固定 W1/L1/M1 数据参数 |
| P0 | 64 KiB 截断在基线中保留、关闭还是改为引用 | Collector 内存边界和目标引擎容量 | E0 三种策略对照，截断 run 单独标识 |
| P1 | dstore JSON 的解析、压缩、LOB 和路径读取实际成本 | 目标引擎 plan、bytes read、CPU、分项空间 | E0 xstore + JSON 路径微基准 |
| P1 | 哪些路径应提升，密度和查询频率阈值是多少 | workload 频率、路径密度、基数和类型稳定性 | E2/E4/E5 的 1%–95% 密度对照 |
| P1 | nested map/slice 应继续字符串化还是保留结构 | 兼容数据比例、路径查询和重建需求 | C0 correctness 与 E5 residual 实验 |
| P2 | 目标引擎用何种机制物化 Core | 物化视图、双写和 post-load 的能力与开销 | E6 三阶段验证 |
| P2 | asset 阈值和跨系统一致性策略 | 64 KiB–20 MiB 曲线、故障注入和恢复目标 | E7 upload-first/outbox 对照 |
| P2 | 脱敏、密钥、租户隔离和保留期限 | 部署安全策略和合规要求 | 安全评审及删除传播测试 |
| P3 | Map、自动子列、KV 或 residual 的最终动态属性形态 | 5000 路径下摄入、查询、空间和运维结果 | E2/E5/E8 横向比较 |
| P3 | 是否更换或开发专用数据库 | 通过正确性门槛的 P3 系统级结果和总成本 | 决策评审，不以单条查询决定 |

## 10. 资料来源

### 10.1 本地项目资料

- [Exporter schema 说明](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/SCHEMA.md)
- [自研引擎验证报告](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/references/engine-verification-2026-08-07.md)
- [Langfuse v4 schema 字典](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/references/langfuse-v4-events-schema-dictionary-2026-08-24.md)
- [大 payload 与多模态 Trace 调研](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/docs/report/large-payload-multimodal-trace.md)
- [Benchmark v4 database catalog](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/schema/v4/database/catalog.json)
- [Benchmark v4 Langfuse catalog](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/schema/v4/langfuse/catalog.json)

### 10.2 官方文档和开放源码

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

### 10.3 论文

- Melnik et al., [Dremel: Interactive Analysis of Web-Scale Datasets](https://research.google/pubs/dremel-interactive-analysis-of-web-scale-datasets/), 2010/2011.
- Tahara, Diamond, Abadi, [Sinew: A SQL System for Multi-Structured Data](https://www.cs.umd.edu/~abadi/papers/sinew-sigmod14.pdf), SIGMOD 2014.
- Alkowaileet, Carey, [Columnar Formats for Schemaless LSM-based Document Stores](https://arxiv.org/abs/2111.11517), 2021.
