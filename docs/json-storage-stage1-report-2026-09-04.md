# Agent Trace JSON 存储阶段调研与实验报告

> 状态：阶段报告
> 日期：2026-09-04
> 变更基线：`2724d21ca5e6310d4962d175fcb7e7c10e26b450`
> 范围：标准 openGauss 6.0.0、ClickHouse 25.12.11.4、多字段 JSON、长 payload

## 1. 结论

Agent Trace 存储属于持续追加写入、按时间范围过滤和聚合的实时分析型负载。指定 `trace_id` 的完整 Trace 回查和原始 payload 读取是必要的次级路径。存储设计应优先保证时间裁剪、列裁剪、稳定维度聚合和持续摄入，再独立保证详情读取与原始内容恢复。

本阶段形成以下结论：

1. JSONB 与 ClickHouse native JSON 解决不同问题。openGauss JSONB 是行内二进制文档，通过 GIN 或表达式索引加速路径查询；ClickHouse native JSON 把叶路径组织成动态子列，超出预算的路径进入 shared data。两者不能按同名类型直接比较。
2. 面向实时分析的稳定设计原则是“强类型分析列 + 动态 residual + 选择性字段提升 + 长 payload 分层”。开源系统和论文在该架构上具有一致方向，字段提升策略、residual 物理类型和外置阈值仍取决于数据与查询分布。
3. openGauss JSONB 九组合成边界实验表明，通用 GIN 提供冷路径检索能力，同时增加载入时间和索引空间；固定热点表达式索引在全部九组中被自然采用。该结果支持提升稳定热点字段，不支持为全部动态属性默认建立通用索引。
4. ClickHouse native JSON 的热点和冷路径查询结果与 truth 一致。目标小字段查询相对 String 解析快约 5–10 倍，完整 native 对象重建则比 String 内容读取慢约 65–102 倍；压缩 raw sidecar 提供与 String 同量级的完整文档读取路径。预算边界、混合密度和等单行宽度实验进一步证明动态路径预算、按非空出现量保留子列以及 path namespace 对 merge 成本的影响。
5. 多字段与长字段是两条独立设计轴。多字段需要控制路径预算、类型冲突和查询列数；长字段需要控制主表读取、压缩编码、传输和生命周期。对象存储只能处理后者。

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

OpenTelemetry Span 的 intrinsic 信息、Attributes 和 Agent payload 具有不同结构。标准 Attributes 是键值属性；模型输入输出、工具参数和结果、消息列表、堆栈及多模态内容可能是嵌套且较长的 JSON。逻辑 schema 应分别表示这两类数据，避免一个 `metadata` 列同时承担分析属性、完整详情和原始归档。

### 2.2 版本基线

| 项目 | 状态 |
|---|---|
| exporter main | `9a49c8a9d6091633112fe793fcf12310859aeb7f` |
| exporter 18 列冻结 | `0c26c9ecf03acf0bd6aa3a3c103ba4e7a78b523a`，ADR-0010 |
| trace-synthesis main | `6472d8e1ac6cdb42494b79b28d4d5361919d4776`，v4 catalog 仍为 28 列 |
| 已验证端到端配对 | benchmark `9529c8f389673132757f4da9a96878926f22b94f` + exporter `54ca553a7ed09ad1751c82adab3aa52c6e9357b1` |
| 蓝区数据库 | openGauss 6.0.0 build `aee4abd5` |
| ClickHouse | 25.12.11.4 |

两仓 main 尚未形成联合冻结。端到端参照继续使用已验证历史配对；JSON 存储机制实验使用独立 loader，并在 manifest 中记录 `data_path=independent_loader`。两类结果不合并为同一性能基线。

## 3. 本阶段完成的工作

### 3.1 调研与数据准备

调研覆盖 PostgreSQL/openGauss JSONB、ClickHouse native JSON、Grafana Tempo dedicated columns、Langfuse Full/Core 与 field overflow、Parquet Variant、Doris Variant、 Elasticsearch flattened、Snowflake/BigQuery/Databricks 半结构化类型，以及 Dremel、Sinew 和 AsterixDB 半结构化列存研究。

本地准备并审计了以下数据：

| 数据 | 当前用途 | 限制 |
|---|---|---|
| `whowhen-pro` text | 端到端参照，6,257 traces / 48,534 spans | 原始顶层属性只有 29 个，provider 和 ERROR 覆盖不足 |
| NVIDIA Open-SWE-Traces | payload 和 trajectory 结构参考，约 49 GiB | 固定 Parquet schema，不是 OTel 动态属性集合 |
| 确定性正确性数据 | missing/null、类型冲突、路径转义、数组顺序和重复键 | 小规模机制数据 |
| 确定性路径数据 | 初始九组均匀矩阵；固定 50,000 行的预算边界、混合密度和等宽矩阵 | 合成机制资产，不代表真实 Trace 分布 |

公开 Agent Trace 数据目前不能证明 500 或 5000 条 metadata 路径是生产分布。初始九组路径数据用于观察引擎边界，并保持约 128 MiB 未压缩输入；ClickHouse 首轮只使用 `50×20%` 和 `500×1%` 做语义验证。补充实验固定 50,000 行，运行 98/99 路径预算边界、500 路径混合密度和三个等单行宽度组。`5000×1%` 继续标为压力边界；后续先按真实 Span 统计路径全集、每行宽度、逐路径密度、类型集合、基数、值长分位数和访问模式，再生成具有业务代表性的 profile。

### 3.2 openGauss JSON 正确性

独立 loader 使用已验证历史 exporter schema 的 26 列行存 DDL，把 300 条确定性记录写入标准 openGauss 6.0.0：

- 300 条 metadata canonical hash 全部一致；
- SQL NULL、路径 missing 和 JSON null 可区分；
- 布尔、整数、小数、对象、数组和跨行类型冲突保持；
- RFC 6901 转义键与数组顺序查询正确；
- 重复键输入被接受，路径读取保留末值 `2`；
- `pg_type` 证明环境包含 `jsonb`，该次 schema 的四个动态列仍使用 `JSON`。

该实验验证当前 JSON schema 在标准 openGauss 的语义，不经过 Collector/exporter，不形成端到端性能结论。

### 3.3 蓝区端到端参照

使用已验证 benchmark/exporter 配对和 `whowhen-pro` 完成标准 openGauss 行存链路。为避免 exporter 默认 64 KiB 截断影响数据库容量判断，Collector 配置把属性上限和 benchmark 截断阈值同步设为 1 MiB。

默认 `--batch-spans 8192` 产生约 28.8 MiB 的最大 OTLP 请求，出现 3 个失败 POST，丢失 24,576 spans。改为 `--batch-spans 1024` 后，48,534 spans 全部发送并落库，missing、extra 和 duplicate 均为 0。Replay 发送耗时 16.620 s，从开始到完整可见耗时 45.998 s。

完整查询集在 Q14 暴露 PostgreSQL 参数化 `SELECT`/`GROUP BY` 表达式不等价问题，因此没有形成全查询性能基线。排除 Q10、Q11、Q13、Q14 后，Q01–Q09 和 Q15 的 50 次正式请求全部执行成功；其中 Q08 返回空结果，只证明查询路径可执行。

该结果用于固定摄入约束和有效系统参照，不构成蓝黄性能比较，也不构成 JSONB/子列机制比较。

### 3.4 openGauss JSONB 路径组织

九组合成边界实验在同一 openGauss 6.0.0 实例比较三张行表：无索引 JSONB、 `jsonb_ops` GIN 通用索引和 `metadata.hot.tenant` 表达式 B-tree。每组分别载入同一 JSONL，查询使用同一 truth 命中集合。

| 观测 | 结果 |
|---|---|
| 正确性 | 九组 truth、整行 canonical hash 和索引能力门禁全部通过 |
| GIN 载入 | 九组均比无索引慢；GIN 大小 7.711–63.852 MiB |
| GIN 自然计划 | 九组中六组采用；宽路径或低选择性组可选择顺序扫描 |
| 冷路径 | 选择性较高时 GIN 明显缩短查询；`50×95%` 中采用 GIN 仍慢于顺序扫描 |
| 热点表达式索引 | 九组全部自然采用，热点查询中位数全部低于无索引布局 |
| 热点索引空间 | 随行数变化，不随动态路径全集直接增长 |

结果说明，JSONB 通用索引的价值由选择性决定；稳定热点采用显式列、生成列或定向索引更符合目标分析负载。单次本机运行和固定载入顺序限制了小幅时间差的解释。

### 3.5 ClickHouse native JSON 路径组织

ClickHouse 探针复用同一数据和 truth，建立三种 MergeTree 布局：

| 布局 | 定义 |
|---|---|
| String | `String CODEC(ZSTD(3))`，查询时调用 JSON 提取函数 |
| native limited | `JSON(max_dynamic_paths=100)` + `metadata_raw String CODEC(ZSTD(3))` |
| native hinted | `JSON(max_dynamic_paths=1000, hot.tenant String, hot.region String)` + `metadata_raw` |

相较首次停止的探针，本轮改动如下：

| 项目 | 首次探针 | 本轮探针 |
|---|---|---|
| 正确性门禁 | native JSON 必须直接重建相同 canonical JSON | 分离分析等价与 canonical 文档保真 |
| native 布局 | 只保存 native JSON | 增加 `metadata_raw String CODEC(ZSTD(3))` |
| native 回读差异 | 触发整轮停止 | 记录为引擎语义观察 |
| 正式矩阵 | 计划顺序运行九组 | 先运行两个语义组，再运行预算边界、混合密度和等单行宽度矩阵 |
| 差异证据 | 保存全部 mismatch ID | 保存总数和最多 10 个样本 |

探针将正确性拆为两个门禁：分析等价门禁使用 native JSON 子列执行路径过滤；文档保真门禁使用 String 布局的 `metadata` 或 native 布局的 `metadata_raw` 对 canonical hash。native JSON 的完整对象回读继续记录为引擎语义观察，不再阻止路径分析。

第一轮语义重跑使用 `50×20%` 的 250,200 行和 `500×1%` 的 291,100 行。两组、三种布局的热点与冷路径 ID 集合均与 truth 一致；两个 raw sidecar 的 canonical hash 差异均为 0。`50×20%` 的两个 native JSON 列各有 77,562 条对象重建差异，均为省略空的 `metadata.paths`；`500×1%` 每行都有路径叶值，native JSON 重建差异为 0。该轮证明了正确性契约和 sidecar 机制，原性能计时包含完整 ID 排序与传输，且统一使用 `getSubcolumn(...)::String` 后没有得到预期列裁剪，因此不进入性能结论。

正式性能矩阵改用直接子列语法：动态值读取为 `metadata.path.:String`，type hint 路径直接按名读取。正确性查询单独返回排序 ID；性能查询在 `start_time` 排序键的 50% 时间窗口内执行按 `hot.region` 分组的 `count()`，只返回至多四行。整对象读取使用同一时间窗口，并对实际内容执行 `cityHash64` 聚合：String 直接读取 `metadata`，native 重建使用 `toJSONString(metadata)`，sidecar 使用 `metadata_raw`。该方法强制数据库消费内容并只返回一个标量，排除了 String `sum(length(...))` 仅读取 offset 的优化和网络返回量差异。每次查询使用唯一 query ID，从 `system.query_log` 的 `QueryFinish` 记录读取耗时、`read_rows`、`read_bytes`、内存和 ProfileEvents。所有 profile 固定 50,000 行；等宽和混合组分别运行三轮，并采用三种布局的平衡顺序轮换。完成门禁同时核对合并后行数、缺失/额外/重复 ID、dynamic/shared 路径全集、精确预算和路径保留优先级。

| profile | 动态路径分布 | 每行动态字段期望值 | 输入 JSONL |
|---|---:|---:|---:|
| 预算内边界 | `98×20%` | 19.6 | 32.429 MiB |
| 预算外边界 | `99×20%` | 19.8 | 32.572 MiB |
| 混合密度 | `10×95% + 40×20% + 450×1%` | 22.0 | 34.145 MiB |
| 等宽低基数 | `50×95%` | 47.5 | 52.384 MiB |
| 等宽中基数 | `500×10%` | 50.0 | 54.173 MiB |
| 等宽高基数压力 | `5000×1%` | 50.0 | 54.173 MiB |

预算边界结果精确符合 `max_dynamic_paths=100` 的定义。limited 布局中，98 条业务路径加两个未提示热点路径得到 100 dynamic / 0 shared；增加第 99 条业务路径后保持 100 dynamic，并把 `paths.p00098` 放入 shared data。hinted 布局的两个热点 type hint 不计入预算，因此分别得到 98 和 99 个 dynamic path，shared 均为 0。

混合密度路径同时控制编号和首次出现顺序：`p00499` 等字典序靠后的路径属于 95% 稳定组；每个 100 行周期先由 1% 长尾路径占据前 5 行，高/中密度路径从第 6 行开始出现。最先出现的 98 条业务路径全部为长尾路径，与两个热点一起占满 limited 的 100 路径预算。三轮 merge 后均换入 50 条高/中密度路径并换出 50 条长尾路径；换入路径的最小非空出现次数为 10,000，换出路径的最大值为 500。最终保留全部 10 条 95% 稳定路径、全部 40 条 20% 可选路径和 48 条 1% 长尾路径，其余 402 条长尾路径进入 shared data。该反例排除了字典序和首次出现顺序，验证 ClickHouse merge 优先保留非空出现量更高的路径；查询频率未参与本次选择。

等单行宽度结果如下。载入速率和 merge 是三轮中位数；压缩空间取 merge 后活动 part，native 数字包含 `metadata_raw` sidecar；dynamic/shared 取三轮一致值。

| profile | 布局 | load rows/s | merge | 压缩空间 | dynamic/shared |
|---|---|---:|---:|---:|---:|
| 50×95% | String | 12,992 | 0.069 s | 2.428 MiB | — |
| 50×95% | native limited | 8,644 | 0.288 s | 3.499 MiB | 52 / 0 |
| 50×95% | native hinted | 8,588 | 0.272 s | 3.499 MiB | 50 / 0 |
| 500×10% | String | 12,450 | 0.164 s | 4.049 MiB | — |
| 500×10% | native limited | 6,666 | 2.579 s | 6.392 MiB | 100 / 402 |
| 500×10% | native hinted | 5,059 | 1.864 s | 5.448 MiB | 500 / 0 |
| 5000×1% | String | 12,474 | 0.159 s | 3.503 MiB | — |
| 5000×1% | native limited | 6,612 | 31.946 s | 9.632 MiB | 100 / 4902 |
| 5000×1% | native hinted | 2,309 | 31.693 s | 22.230 MiB | 1000 / 4000 |

查询表中的耗时和读取量是三轮共 15 个 `QueryFinish` 样本的中位数。热点查询的 tenant 选择性在三个 profile 中相同；冷路径选择性随逐路径密度变化，因此冷路径数字只用于组内布局比较。

| profile | 布局 | 热点耗时 / read | 冷路径耗时 / read |
|---|---|---:|---:|
| 50×95% | String | 37 ms / 24.547 MiB | 41 ms / 24.547 MiB |
| 50×95% | native limited | 7 ms / 1.311 MiB | 6 ms / 1.128 MiB |
| 50×95% | native hinted | 7 ms / 0.954 MiB | 7 ms / 0.949 MiB |
| 500×10% | String | 58 ms / 25.719 MiB | 62 ms / 25.719 MiB |
| 500×10% | native limited | 7 ms / 1.184 MiB | 7 ms / 0.950 MiB |
| 500×10% | native hinted | 6 ms / 0.816 MiB | 6 ms / 0.747 MiB |
| 5000×1% | String | 59 ms / 25.719 MiB | 64 ms / 25.719 MiB |
| 5000×1% | native limited | 8 ms / 1.180 MiB | 9 ms / 0.940 MiB |
| 5000×1% | native hinted | 6 ms / 0.791 MiB | 8 ms / 0.374 MiB |

整对象内容读取表同样汇总三轮共 15 个 QueryFinish 样本。耗时与读取量是服务端 hash 聚合指标，不包含把 25,000 份 JSON 返回客户端的网络传输。

| profile | String 内容读取 | native limited 重建 | native hinted 重建 | limited sidecar | hinted sidecar |
|---|---:|---:|---:|---:|---:|
| 50×95% | 17 ms / 24.547 MiB | 1,418 ms / 24.807 MiB | 1,362 ms / 24.378 MiB | 14 ms / 18.728 MiB | 16 ms / 18.728 MiB |
| 500×10% | 39 ms / 25.719 MiB | 3,451 ms / 57.259 MiB | 2,806 ms / 121.631 MiB | 32 ms / 19.647 MiB | 33 ms / 19.615 MiB |
| 5000×1% | 38 ms / 25.719 MiB | 3,877 ms / 61.521 MiB | 2,466 ms / 250.321 MiB | 33 ms / 19.622 MiB | 16 ms / 19.610 MiB |

14 个正式运行的分析等价与文档保真门禁全部通过，合并后行数、ID 身份、路径全集和整对象 digest 门禁也全部通过。直接子列访问下，native JSON 对目标小列过滤与分组的服务端耗时为 String 解析的约 1/5–1/10，读取量为约 1/19–1/69；完整 native 对象重建则比 String 内容读取慢约 65–102 倍，内存中位数为 29.876–284.807 MiB，而 String 为 16.226–16.912 MiB。raw sidecar 的耗时为 14–33 ms，与 String 的 17–39 ms 同量级，且全部内容 digest 一致。该结果支持“native 用于分析子列、raw/String 用于完整详情”的双路径，不支持用 native 重建承担高频整对象读取。

native 的载入吞吐低于 String，并承担子列组织和 raw sidecar 空间。固定约 50 个字段/行时，path namespace 从 50 增至 5000，limited 和 hinted 的 merge 中位数分别从 0.288/0.272 s 增至 31.946/31.693 s，证明全局路径基数会独立放大 merge 成本。

提高动态路径预算需要针对实际访问路径。`5000×1%` 中从 100 提高到 1000 个动态路径没有改变两个显式查询的数量级，却使 hinted 载入吞吐降至 2,309 rows/s，压缩空间增至 22.230 MiB。稳定热点应使用强类型列或 type hint；预算外长尾留在 shared data。该结论适用于本次合成数据和直接子列查询，不替代真实 Trace 分布审计。

当前 `metadata_raw` 保存解析后重新序列化的 canonical JSON，保证逻辑结构恢复。字节级审计和重放还需要摄入层保存原始输入 bytes，包括原始键顺序、空白和重复键处理规则。详细机制和跨项目设计比较见 [JSON 存储设计调研](json-storage-design-survey.md)。

## 4. openGauss JSONB 与 ClickHouse JSON 对比

| 维度 | openGauss JSONB | ClickHouse native JSON |
|---|---|---|
| 逻辑对象 | 二进制分解的 JSON 文档 | 逻辑 JSON 列 |
| 物理加速 | GIN 倒排、表达式/B-tree 索引 | 叶路径动态子列、type hint、shared data |
| 目标查询 | 文档包含、存在性、定向路径检索 | 大量行中读取、过滤和聚合少量路径 |
| 写入成本来源 | JSONB 解析、行存写入、索引维护 | 路径解析、类型推断、子列文件及 merge |
| 路径规模控制 | 选择是否建立通用或定向索引 | `max_dynamic_paths`、shared data serialization、`SKIP` |
| 整对象读取 | 保持 JSONB 结构语义，格式规范化 | 子列重建遵循叶路径语义；精确恢复使用 raw sidecar |
| 适合本项目的角色 | 蓝区行存基线、语义对照、少量定向索引 | 分析型 residual 候选 |

当前证据不能回答哪个引擎性能更高。openGauss 使用 JSONB 行表和 GIN/B-tree 索引，ClickHouse 使用 MergeTree 动态子列和 shared data；两侧的物理布局、索引、空间口径和正式矩阵不同。ClickHouse 等宽实验已经包含时间范围裁剪和分组计数，但仍是一次性分批载入。跨引擎比较还需统一行数、查询、正确性契约和 raw 保存范围，并加入持续写入、并发查询及 part/merge 状态。

## 5. 公认设计与未决选择

### 5.1 已形成一致方向的设计

代表性实现和研究形成以下公认架构原则。这里的“公认”表示多类系统和研究反复采用同一分层方向，不表示存在统一的字段选择算法或阈值：

1. 稳定且常用于过滤、排序、分组和聚合的字段使用强类型列。
2. 动态长尾字段进入 residual JSON、Map、Variant 或 shared data。
3. 对自动或手工子列设置预算，把预算外路径保留在可查询的 residual 或共享结构中。
4. 分别定义 missing、JSON null、SQL NULL、类型冲突、数组顺序、路径转义和重建规则。
5. 长、高基数字段使用适合的压缩编码或独立物理层，控制常规分析的读取量。

Tempo 使用 intrinsic 列、通用 `Attrs` 和少量 dedicated columns；Parquet Variant 支持完整 Variant 与可选 typed shredding；Sinew 使用物理列和 reservoir；ClickHouse 使用 dynamic paths 与 shared data。这些实现体现相同分层思想。

字段提升的具体信号具有不同证据等级：

| 信号 | 证据与适用范围 |
|---|---|
| 查询用途与访问频率 | PostgreSQL 表达式索引、Tempo dedicated column 和 Databricks 手工抽取均由 workload 选择；该信号是人工设计的常用依据，不是各引擎通用的自动指标 |
| 非空密度 | ClickHouse merge、Doris/StarRocks 自动子列和 Sinew 物化策略均使用出现量或密度，是自动组织中最普遍的信号 |
| 类型稳定性 | typed column、type hint 和 Variant shredding 都需要类型契约；冲突值通常回落到动态或 residual 表示 |
| 基数和值长 | Sinew、Tempo 等用其判断列化、字典编码和 blob 编码成本；具体阈值属于实现与 workload 参数 |

因此，查询频率参与本项目的 workload 收益评估，但不能单独称为公认的自动提升指标。ClickHouse 当前 merge 按非 null 值数量选择动态路径，并不读取查询日志。各项目的具体机制和来源见 [JSON 存储设计调研](json-storage-design-survey.md)。

### 5.2 没有公认固定答案的部分

- residual 使用 String、Map、JSON/Variant 还是 KV/EAV；
- 提升字段由人工 schema、查询统计还是存储引擎自动决定；
- workload 收益、密度、类型、基数和值长如何组合，以及各自阈值；
- dynamic path 数量和稀疏阈值；
- 热点值在 residual 中 copy 还是 move；
- 大字段保存在同表独立列、payload 表、LOB 还是对象存储；
- raw 原文的保存范围和保留周期。

这些选择依赖实际数据分布、查询列数、时间选择性、持续摄入成本和详情读取比例。固定星级或跨引擎总排名无法替代目标 workload 实测。

## 6. 多字段特化设计

多字段 JSON 需要分别定义和测量以下变量：

| 符号 | 变量 | 定义 | 主要影响 |
|---|---|---|---|
| `P` | 全局 path 基数 | 指定 tenant/project、Agent/工作流、instrumentation 版本和时间窗口内的不同叶路径总数 | schema、路径元数据、文件数和 merge 成本 |
| `W` | 单行字段数 | 每行实际出现的叶路径数，报告 p50、p95、p99 和最大值 | 单行解析、序列化、写入大小和内存 |
| `dᵢ` | 逐 path 密度 | 路径 `i` 出现的行数除以窗口总行数 | 稀疏压缩、子列选择和索引收益 |
| `cᵢ` | 值基数 | 路径 `i` 的 distinct 值数及其相对行数 | 选择性、字典编码和索引效率 |
| `Tᵢ` | 类型集合 | 路径 `i` 跨行出现的类型及冲突比例 | typed subcolumn、Dynamic 类型分流和回退策略 |
| `Lᵢ` | 值长 | 路径 `i` 值长度的分位数 | 压缩、I/O、LOB 和外置策略 |
| `Aᵢ` | 查询访问 | 路径 `i` 在过滤、排序、分组、聚合和投影中的访问次数 | 人工字段提升与定向索引收益 |
| `E` | schema epoch | 相邻时间窗口的路径新增、删除、类型变化和稳定集合 | schema 演进、预算和回填范围 |

建议逻辑布局为：

```text
events_analytics
  tenant/project + time + trace/span identity
  + service/type/status/duration/model/token 等强类型列
  + workload 证明有价值的 promoted attributes
  + bounded dynamic residual
  + payload preview/length/hash/reference
```

稳定字段不进入动态路径竞争。扁平且类型统一的 OTel 属性可比较 Map；嵌套或跨行类型变化的属性比较 native JSON/Variant。冷路径保留在 shared/residual 中。人工提升以查询操作和收益为入口，再检查选择性、密度、类型稳定性、基数和值长；自动组织至少需要密度、类型和明确的路径预算。

九组均匀密度产物保留为机制资产。ClickHouse 首轮使用 `50×20%` 和 `500×1%` 完成语义验证；修正测量口径后增加 98/99 路径预算边界、500 路径混合密度以及 `50×95%`、`500×10%`、`5000×1%` 等单行宽度矩阵。`5000×1%` 只支持引擎压力边界结论，5000 路径高密度组合继续裁剪。下一阶段审计真实 Trace，再决定代表性主矩阵。

后续 profile 分为以下类别：

| 类别 | 组合 | 解释 |
|---|---|---|
| 代表性候选 | 50 路径混合密度；有真实数据支持后增加 500 路径混合密度 | 少量高密度稳定字段、中等密度可选字段和低密度长尾字段并存 |
| 机制梯度 | `50/500 × 1%/5%/20%/50%/95%` | 观察密度变化；这些数值不是字段提升阈值 |
| 局部细化 | 2% 和 10% | 只在 1%–20% 之间发现计划或性能转折后运行 |
| 等单行宽度 | `50×95%`、`500×10%`、`5000×1%` | 每行约 50 个动态字段，用于区分全局 namespace 与单行宽度 |
| 压力边界 | `5000×1%` | 验证路径预算、shared data、GIN 放大和路径元数据 |
| 当前裁剪 | `5000×20%`、`5000×95%` | 分别约 1000 和 4750 个字段/行，缺少 Agent Trace 场景支持 |

现有公开数据只能支持“50 路径量级更接近已观察样本”的判断，尚不能把任一均匀密度 profile 定义为代表性 workload。代表性结论需要按 tenant/project、Agent/工作流、instrumentation 版本和时间窗口完成真实 Trace 审计。

## 7. 长字段特化设计

长 payload 的决策依据包括长度、访问频率、是否参与 SQL 分析、内容类型和保留周期：

| 数据形态 | 建议候选 |
|---|---|
| 长字段很少读取，所在列可被可靠裁剪 | 同表独立 payload 列 |
| 详情读取独立于时间范围分析 | `event_payloads` 表，按 event/span ID 回查 |
| 极长、二进制、多模态或需要独立生命周期 | asset/object storage |
| 需要字段级分析的长 JSON | 提取分析特征；完整内容留在 payload/raw 层 |
| 需要精确审计和重放 | raw String 或原始对象，保存长度和 SHA-256 |

分析主表只保存预览、原始长度、内容哈希、内容类型和引用状态。对象引用采用结构化对象，包含 tenant/project、来源字段、MIME、encoding、长度、SHA-256、storage URI 和状态。

Full/Core 双表是候选机制，不是既定设计。列存引擎能够裁剪未选择的大列时，单分析表加独立 payload 列或 payload 表可能以更小写放大达到相同效果。下一实验应直接比较以下布局：

1. 单一宽表，payload 是独立列；
2. `events_analytics` + `event_payloads`；
3. Full/Core 物化双表；
4. `events_analytics` + asset reference。

## 8. 下一步实验

1. 审计可映射到统一 Span/Attribute 模型的公开和实际数据，按 tenant/project、Agent/工作流、版本和时间窗口统计 `P/W/dᵢ/cᵢ/Tᵢ/Lᵢ/E`，据此定义代表性混合 profile 和 schema epoch。
2. 使用时间范围谓词持续分批写入，并发运行过滤与聚合查询，比较“强类型列 + String residual”“强类型列 + Map”“强类型列 + 有预算 native JSON”；记录 part 数、merge backlog、写入延迟和查询尾延迟。
3. 根据真实审计结果运行 `50/500 × 1%/5%/20%/50%/95%` 中得到数据支持的组合；只有在 1%–20% 间出现转折时增加 2% 和 10% 局部梯度。
4. 明确 native JSON 的业务等价规则、canonical sidecar 保留周期和字节级原文要求；当前 sidecar 已通过 canonical 重建门禁。
5. 完成长 payload 四种布局对照，再决定 Full/Core、payload 表和对象存储的采用范围。

## 9. 发布范围

本阶段提交确定性生成器、openGauss/ClickHouse runner、对应测试、运行配置和文档化结果。生成数据、外部数据集、容器状态、运行 manifest、结果 JSON、缓存和凭据保持在 gitignored 目录。早期 PostgreSQL 容器探针已由项目指定的标准 openGauss 6.0.0 探针替代，不进入发布代码或结论。

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
