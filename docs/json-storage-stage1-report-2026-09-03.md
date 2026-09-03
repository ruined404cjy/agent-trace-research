# Agent Trace JSON 存储阶段调研与实验报告

> 状态：阶段报告
> 日期：2026-09-03
> 变更基线：`2724d21ca5e6310d4962d175fcb7e7c10e26b450`
> 范围：标准 openGauss 6.0.0、ClickHouse 25.12.11.4、多字段 JSON、长 payload

## 1. 结论

Agent Trace 存储属于持续追加写入、按时间范围过滤和聚合的实时分析型负载。指定
`trace_id` 的完整 Trace 回查和原始 payload 读取是必要的次级路径。存储设计应优先保证
时间裁剪、列裁剪、稳定维度聚合和持续摄入，再独立保证详情读取与原始内容恢复。

本阶段形成以下结论：

1. JSONB 与 ClickHouse native JSON 解决不同问题。openGauss JSONB 是行内二进制文档，
   通过 GIN 或表达式索引加速路径查询；ClickHouse native JSON 把叶路径组织成动态子列，
   超出预算的路径进入 shared data。两者不能按同名类型直接比较。
2. 面向实时分析的稳定设计原则是“强类型分析列 + 动态 residual + 选择性字段提升 + 长
   payload 分层”。开源系统和论文在该架构上具有一致方向，字段提升策略、residual 物理类型
   和外置阈值仍取决于数据与查询分布。
3. openGauss JSONB 九组实验表明，通用 GIN 提供冷路径检索能力，同时增加载入时间和索引
   空间；固定热点表达式索引在全部九组中被自然采用。该结果支持提升稳定热点字段，不支持
   为全部动态属性默认建立通用索引。
4. ClickHouse native JSON 的热点和冷路径过滤结果与 truth 一致，但完整对象重建省略嵌套
   空对象，未满足当前 canonical hash 契约。因此本阶段没有形成 openGauss 与 ClickHouse
   的有效性能排名。
5. 多字段与长字段是两条独立设计轴。多字段需要控制路径预算、类型冲突和查询列数；长字段
   需要控制主表读取、压缩编码、传输和生命周期。对象存储只能处理后者。

## 2. 项目边界与基线

### 2.1 工作负载

本报告使用以下目标负载：

- Span/Event 持续、频繁到达，写入以追加为主；
- 查询首先限定 project/tenant 和时间范围；
- 主要操作是过滤、分组、计数、分位数和特征统计；
- 常用条件集中在稳定 Trace 字段和少量热点属性；
- 任意动态路径探索、Trace 树回查和完整 payload 读取频率较低；
- 批次幂等、可见水位和内容校验属于摄入正确性要求。

这里使用“实时分析型”或“append-heavy OLAP”，避免与 CAP 中的 AP 含义混淆。

OpenTelemetry Span 的 intrinsic 信息、Attributes 和 Agent payload 具有不同结构。标准
Attributes 是键值属性；模型输入输出、工具参数和结果、消息列表、堆栈及多模态内容可能是
嵌套且较长的 JSON。逻辑 schema 应分别表示这两类数据，避免一个 `metadata` 列同时承担
分析属性、完整详情和原始归档。

### 2.2 版本基线

| 项目 | 状态 |
|---|---|
| exporter main | `9a49c8a9d6091633112fe793fcf12310859aeb7f` |
| exporter 18 列冻结 | `0c26c9ecf03acf0bd6aa3a3c103ba4e7a78b523a`，ADR-0010 |
| trace-synthesis main | `6472d8e1ac6cdb42494b79b28d4d5361919d4776`，v4 catalog 仍为 28 列 |
| 已验证端到端配对 | benchmark `9529c8f389673132757f4da9a96878926f22b94f` + exporter `54ca553a7ed09ad1751c82adab3aa52c6e9357b1` |
| 蓝区数据库 | openGauss 6.0.0 build `aee4abd5` |
| ClickHouse | 25.12.11.4 |

两仓 main 尚未形成联合冻结。端到端参照继续使用已验证历史配对；JSON 存储机制实验使用
独立 loader，并在 manifest 中记录 `data_path=independent_loader`。两类结果不合并为同一
性能基线。

## 3. 本阶段完成的工作

### 3.1 调研与数据准备

调研覆盖 PostgreSQL/openGauss JSONB、ClickHouse native JSON、Grafana Tempo dedicated
columns、Langfuse Full/Core 与 field overflow、Parquet Variant、Doris Variant、
Elasticsearch flattened、Snowflake/BigQuery/Databricks 半结构化类型，以及 Dremel、Sinew
和 AsterixDB 半结构化列存研究。

本地准备并审计了以下数据：

| 数据 | 当前用途 | 限制 |
|---|---|---|
| `whowhen-pro` text | 端到端参照，6,257 traces / 48,534 spans | 原始顶层属性只有 29 个，provider 和 ERROR 覆盖不足 |
| NVIDIA Open-SWE-Traces | payload 和 trajectory 结构参考，约 49 GiB | 固定 Parquet schema，不是 OTel 动态属性集合 |
| 确定性正确性数据 | missing/null、类型冲突、路径转义、数组顺序和重复键 | 小规模机制数据 |
| 确定性路径数据 | 50/500/5000 路径 × 1%/20%/95% 密度 | 均匀密度边界，不代表真实长尾分布 |

公开 Agent Trace 数据目前不能证明 500 或 5000 条 metadata 路径是生产分布。九组路径数据
用于观察引擎边界，并保持约 128 MiB 未压缩输入；后续仍需按真实 Span 统计路径全集、每行
宽度、逐路径密度、类型集合、基数、值长分位数和查询频率。

### 3.2 openGauss JSON 正确性

独立 loader 使用已验证历史 exporter schema 的 26 列行存 DDL，把 300 条确定性记录写入
标准 openGauss 6.0.0：

- 300 条 metadata canonical hash 全部一致；
- SQL NULL、路径 missing 和 JSON null 可区分；
- 布尔、整数、小数、对象、数组和跨行类型冲突保持；
- RFC 6901 转义键与数组顺序查询正确；
- 重复键输入被接受，路径读取保留末值 `2`；
- `pg_type` 证明环境包含 `jsonb`，该次 schema 的四个动态列仍使用 `JSON`。

该实验验证当前 JSON schema 在标准 openGauss 的语义，不经过 Collector/exporter，不形成
端到端性能结论。

### 3.3 蓝区端到端参照

使用已验证 benchmark/exporter 配对和 `whowhen-pro` 完成标准 openGauss 行存链路。为避免
exporter 默认 64 KiB 截断影响数据库容量判断，Collector 配置把属性上限和 benchmark 截断
阈值同步设为 1 MiB。

默认 `--batch-spans 8192` 产生约 28.8 MiB 的最大 OTLP 请求，出现 3 个失败 POST，丢失
24,576 spans。改为 `--batch-spans 1024` 后，48,534 spans 全部发送并落库，missing、extra
和 duplicate 均为 0。Replay 发送耗时 16.620 s，从开始到完整可见耗时 45.998 s。

完整查询集在 Q14 暴露 PostgreSQL 参数化 `SELECT`/`GROUP BY` 表达式不等价问题，因此没有
形成全查询性能基线。排除 Q10、Q11、Q13、Q14 后，Q01–Q09 和 Q15 的 50 次正式请求全部
执行成功；其中 Q08 返回空结果，只证明查询路径可执行。

该结果用于固定摄入约束和有效系统参照，不构成蓝黄性能比较，也不构成 JSONB/子列机制比较。

### 3.4 openGauss JSONB 路径组织

九组实验在同一 openGauss 6.0.0 实例比较三张行表：无索引 JSONB、`jsonb_ops` GIN 通用
索引和 `metadata.hot.tenant` 表达式 B-tree。每组分别载入同一 JSONL，查询使用同一 truth
命中集合。

| 观测 | 结果 |
|---|---|
| 正确性 | 九组 truth、整行 canonical hash 和索引能力门禁全部通过 |
| GIN 载入 | 九组均比无索引慢；GIN 大小 7.711–63.852 MiB |
| GIN 自然计划 | 九组中六组采用；宽路径或低选择性组可选择顺序扫描 |
| 冷路径 | 选择性较高时 GIN 明显缩短查询；`50×95%` 中采用 GIN 仍慢于顺序扫描 |
| 热点表达式索引 | 九组全部自然采用，热点查询中位数全部低于无索引布局 |
| 热点索引空间 | 随行数变化，不随动态路径全集直接增长 |

结果说明，JSONB 通用索引的价值由选择性决定；稳定热点采用显式列、生成列或定向索引更符合
目标分析负载。单次本机运行和固定载入顺序限制了小幅时间差的解释。

### 3.5 ClickHouse native JSON 路径组织

ClickHouse 探针复用同一数据和 truth，建立三种 MergeTree 布局：

| 布局 | 定义 |
|---|---|
| String | `String CODEC(ZSTD(3))`，查询时调用 JSON 提取函数 |
| native limited | `JSON(max_dynamic_paths=100)` |
| native hinted | `JSON(max_dynamic_paths=1000, hot.tenant String, hot.region String)` |

200 行集成测试证明，低预算表可进入 shared data，高预算表保留全部路径，过滤和 hash 门禁
均可执行。首个 128 MiB 正式组为 50 路径、20% 密度、250,200 行，结果为：

- String 布局的 250,200 条 metadata hash 全部一致；
- 两个 native JSON 布局各有 77,562 条 hash 不一致；
- 差异行全部包含空的 `metadata.paths` 对象，native JSON 重建时省略该空对象；
- 三种布局的热点与冷路径过滤 ID 集合均与 truth 一致；
- 低预算表记录 52 个 dynamic 路径，高预算加 hint 表记录 50 个 dynamic 路径；两个 type
  hint 不计入 dynamic 路径预算。

该 run 按停止条件标记为 `failed`，并停止剩余八组性能矩阵。结果证明 native JSON 的叶路径
过滤能力，也证明其不能直接承担当前完整对象重建契约。保留 raw String sidecar，或明确空
容器不属于业务语义后，才能继续分析性能。

## 4. openGauss JSONB 与 ClickHouse JSON 对比

| 维度 | openGauss JSONB | ClickHouse native JSON |
|---|---|---|
| 逻辑对象 | 二进制分解的 JSON 文档 | 逻辑 JSON 列 |
| 物理加速 | GIN 倒排、表达式/B-tree 索引 | 叶路径动态子列、type hint、shared data |
| 目标查询 | 文档包含、存在性、定向路径检索 | 大量行中读取、过滤和聚合少量路径 |
| 写入成本来源 | JSONB 解析、行存写入、索引维护 | 路径解析、类型推断、子列文件及 merge |
| 路径规模控制 | 选择是否建立通用或定向索引 | `max_dynamic_paths`、shared data serialization、`SKIP` |
| 整对象读取 | 保持 JSONB 结构语义，格式规范化 | 需要重建子列；本地实测省略空嵌套对象 |
| 适合本项目的角色 | 蓝区行存基线、语义对照、少量定向索引 | 分析型 residual 候选 |

当前证据不能回答哪个引擎性能更高。openGauss 已完成九组性能测试，ClickHouse 在第一组
正确性门禁停止；两侧没有形成相同正确性契约下的完整矩阵。后续比较还必须加入持续写入、
并发查询、part/merge 状态和时间范围裁剪，不能使用一次性载入结果代表流式负载。

## 5. 公认设计与未决选择

### 5.1 已形成一致方向的设计

代表性实现和研究形成以下架构共识：

1. 稳定且常用于过滤、排序、分组和聚合的字段使用强类型列。
2. 动态长尾字段进入 residual JSON、Map、Variant 或 shared data。
3. 根据查询频率、字段密度、类型稳定性、基数和值长选择提升字段。
4. 对自动或手工子列设置预算，避免把全局路径全集展开为无限物理列。
5. 分别定义 missing、JSON null、SQL NULL、类型冲突、数组顺序、路径转义和重建规则。
6. 长、高基数字段使用适合的压缩编码或独立物理层，避免拖累常规分析。

Tempo 使用 intrinsic 列、通用 `Attrs` 和少量 dedicated columns；Parquet Variant 支持完整
Variant 与可选 typed shredding；Sinew 使用物理列和 reservoir；ClickHouse 使用 dynamic
paths 与 shared data。这些实现体现相同分层思想。

### 5.2 没有公认固定答案的部分

- residual 使用 String、Map、JSON/Variant 还是 KV/EAV；
- 提升字段由人工 schema、查询统计还是存储引擎自动决定；
- dynamic path 数量和稀疏阈值；
- 热点值在 residual 中 copy 还是 move；
- 大字段保存在同表独立列、payload 表、LOB 还是对象存储；
- raw 原文的保存范围和保留周期。

这些选择依赖实际数据分布、查询列数、时间选择性、持续摄入成本和详情读取比例。固定星级或
跨引擎总排名无法替代目标 workload 实测。

## 6. 多字段特化设计

多字段 JSON 需要分别测量四个变量：

| 变量 | 主要影响 |
|---|---|
| 全局不同路径数 | schema、文件数、路径元数据和 merge 成本 |
| 单行叶路径数 | 单行解析、序列化和写入大小 |
| 逐路径密度与类型 | 稀疏压缩、字段提升收益、Dynamic 类型分流 |
| 单次查询访问路径数 | 列裁剪收益和整段解析成本 |

建议逻辑布局为：

```text
events_analytics
  tenant/project + time + trace/span identity
  + service/type/status/duration/model/token 等强类型列
  + workload 证明有价值的 promoted attributes
  + bounded dynamic residual
  + payload preview/length/hash/reference
```

稳定字段不进入动态路径竞争。扁平且类型统一的 OTel 属性可比较 Map；嵌套或跨行类型变化的
属性比较 native JSON/Variant。冷路径保留在 shared/residual 中。字段提升应同时考虑查询
频率、选择性、密度、类型稳定性、基数和值长，不能只依据“路径出现次数”。

当前九组均匀密度矩阵保留为机制回归。下一阶段增加同一数据集内的稳定核心、中频属性和
极稀疏长尾，并使查询只访问少量路径，模拟目标分析模式。

## 7. 长字段特化设计

长 payload 的决策依据包括长度、访问频率、是否参与 SQL 分析、内容类型和保留周期：

| 数据形态 | 建议候选 |
|---|---|
| 长字段很少读取，所在列可被可靠裁剪 | 同表独立 payload 列 |
| 详情读取独立于时间范围分析 | `event_payloads` 表，按 event/span ID 回查 |
| 极长、二进制、多模态或需要独立生命周期 | asset/object storage |
| 需要字段级分析的长 JSON | 提取分析特征；完整内容留在 payload/raw 层 |
| 需要精确审计和重放 | raw String 或原始对象，保存长度和 SHA-256 |

分析主表只保存预览、原始长度、内容哈希、内容类型和引用状态。对象引用采用结构化对象，包含
tenant/project、来源字段、MIME、encoding、长度、SHA-256、storage URI 和状态。

Full/Core 双表是候选机制，不是既定设计。列存引擎能够裁剪未选择的大列时，单分析表加独立
payload 列或 payload 表可能以更小写放大达到相同效果。下一实验应直接比较以下布局：

1. 单一宽表，payload 是独立列；
2. `events_analytics` + `event_payloads`；
3. Full/Core 物化双表；
4. `events_analytics` + asset reference。

## 8. 下一步实验

1. 审计可映射到统一 Span/Attribute 模型的公开和实际数据，生成路径、密度、类型、基数、
   长度和查询频率统计。
2. 明确 native JSON 的业务等价规则；需要精确重建时增加 raw sidecar，并重新运行 ClickHouse
   正确性门禁。
3. 比较“强类型列 + String residual”“强类型列 + Map”“强类型列 + 有预算 native JSON”。
4. 使用时间范围谓词，持续分批写入并并发运行过滤与聚合查询；分别记录稳态和合并后结果。
5. 记录 rows/s、可见延迟、CPU、RSS、写入字节、part/file 数、merge backlog、查询扫描行数
   和 bytes read。
6. 完成长 payload 四种布局对照，再决定 Full/Core、payload 表和对象存储的采用范围。

## 9. 发布范围

本阶段提交确定性生成器、openGauss/ClickHouse runner、对应测试、运行配置和文档化结果。
生成数据、外部数据集、容器状态、运行 manifest、结果 JSON、缓存和凭据保持在 gitignored
目录。早期 PostgreSQL 容器探针已由项目指定的标准 openGauss 6.0.0 探针替代，不进入发布
代码或结论。

## 10. 资料来源

### 10.1 本地证据

- [第一阶段实验基础设施与结果](../experiments/json-storage-stage1/README.md)
- [JSON 存储设计调研](json-storage-design-survey.md)
- [第一阶段穿刺实验设计](json-storage-spike-experiment-design.md)

### 10.2 官方资料与论文

- [OpenTelemetry Traces](https://opentelemetry.io/docs/concepts/signals/traces/)
- [openGauss JSON/JSONB Functions and Operators](https://docs.opengauss.org/en/docs/latest-lite/sql_reference/json-jsonb-functions-and-operators.html)
- [ClickHouse JSON Data Type](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse JSON shared data serialization](https://clickhouse.com/blog/json-data-type-gets-even-better)
- [Grafana Tempo block format](https://grafana.com/docs/tempo/latest/reference-tempo-architecture/block-format/)
- [Grafana Tempo dedicated attribute columns](https://grafana.com/docs/tempo/latest/operations/dedicated_columns/)
- [Apache Parquet Variant shredding](https://parquet.apache.org/docs/file-format/types/variantshredding/)
- Tahara, Diamond, Abadi, [Sinew: A SQL System for Multi-Structured Data](https://www.cs.umd.edu/~abadi/papers/sinew-sigmod14.pdf), SIGMOD 2014.
- Alkowaileet, Carey, [Columnar Formats for Schemaless LSM-based Document Stores](https://arxiv.org/abs/2111.11517), 2021.
