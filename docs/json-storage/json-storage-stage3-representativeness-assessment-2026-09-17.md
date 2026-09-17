# JSON Storage Stage 3 四布局代表性评估

> 状态：阶段性评估，`main` 工作负载为部分正式切片
>
> 评估日期：2026-09-17
>
> 适用设计：[阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)
>
> 量化证据：本地 gitignored 目录 `docs/temp/json-storage-stage3/runs/20260917-priority/main-matrix-attempt-1/`，不进入迁移分支

本文评估 Stage 3 四种长 payload 布局实验的代表性。评估对象是已经执行的部分正式切片、冻结合成语料和现有 Python adapter 实现；目的是说明这些结果能够支持哪些结论、不能支持哪些结论，以及进入生产推断前需要补齐的实验。

## 1. 范围与证据等级

代表性分为四个相互独立的维度，必须分别判定，不能合并为“实验结果是否可信”这一个问题。

| 维度 | 含义 | 本切片可判定范围 |
|---|---|---|
| 语义代表性 | 布局名称对应的逻辑数据组织与分析语义和生产模型的接近程度 | 四种布局的分析过滤、详情恢复、联合水位和清理契约由同一 runner 固定，覆盖同表、分表、双写和引用恢复 |
| 实现代表性 | 当前 Python adapter 与生产集成读写路径的接近程度 | `asset_ref` 采用应用侧 resolver 加本地内容寻址目录；数据库内调度和 extension 路径不在本次实现内 |
| 数据代表性 | 冻结语料与生产 Trace 在分布、并发和生命周期上的接近程度 | 合成语料可以区分 payload 放置、检索路径、响应字节和可压缩性控制；生产分布不在覆盖范围内 |
| 统计完备性 | 四轮 Latin square 与完整 workload／provenance 门禁的满足程度 | 只有 `main` 工作负载具备四轮完整证据，缺少两个等总字节控制与 `correctness_only` |

八 target `main` 工作负载（2 引擎 × 4 布局）构成一个**部分正式切片**。该切片的状态与边界如下。

| 项目 | 观测值 | 证据状态 |
|---|---|---|
| target 数 | 8（openGauss、ClickHouse 各 4 个布局） | `run-manifest.json` 的 `status=complete` |
| 轮次 | 每 target 4 轮四阶 Latin square | 每轮每场景 30 次查询样本，`batch` 场景每轮 5 次 |
| 操作数 | 每 target 1,340/1,340 次查询操作完成，失败样本 0 | 完整响应字节与 truth 校验通过 |
| 写入 | 190 个 block 全部完成，最终水位 48,534 | 联合水位与清理证据齐全 |
| 冻结输入身份 | `identity_sha256 = a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8` | 八 target 共用同一输入身份 |
| 引擎 | openGauss 6.0.0；ClickHouse 25.12.11.4 | 容器 `agent-trace-opengauss-v6`、`agent-trace-clickhouse-25-12` |
| 主机 | x86_64、8 CPU、16,291,952 KiB、WSL2 内核 6.6.114.1 | 八 target 同机串行执行 |

该切片缺少 `equal_total_few_large`、`equal_total_many_medium` 与 `correctness_only` 工作负载，尚未通过汇总器的完整 workload 与 provenance 门禁。以切片目录直接调用 `report/summarize.py` 的 `summarize()`，结果为 `ValueError: provenance evidence is incomplete`，因此本评估的数值全部来自各 target 的轮次产物重算，不由汇总器输出。

本切片不执行新的数据库实验，也不包含 Stage 3 candidate、part-state、混合负载和 Asset 故障实验。

## 2. 四种布局表示什么

四种布局使用同一逻辑记录、同一过滤语义、同一 190 个写入 block 和同一四阶 Latin square 顺序，只改变长 payload 的物理位置。

| 布局 | 数据组织 | 列表与预览路径 | 详情路径 | 写入完成条件 |
|---|---|---|---|---|
| `same_table` | `events` 同时保存分析列、内容元数据和 payload | 读取 `events` 的分析列或 preview | 从 `events` 恢复完整逻辑记录 | `events` block 可见 |
| `separate` | `events_analytics` 保存分析列与内容元数据，`event_payloads` 保存定位键与 payload | 读取 `events_analytics` | 组合分析行与 `event_payloads` 内容 | 两表联合水位覆盖该 block |
| `full_core` | `events_full` 保存完整记录，`events_core` 复制列表所需列、preview、长度和摘要 | 读取 `events_core` | 从 `events_full` 恢复完整记录 | Full 与 Core 可见且 Core 水位覆盖 Full |
| `asset_ref` | `events_analytics` 保存分析列与 Asset 引用，`assets` 表保存内容元数据与状态，本地内容寻址目录保存 bytes | 读取 `events_analytics` | 查询引用与 `assets`，再由 resolver 读取本地文件 | 对象原子发布、`assets.status=available`、引用可见 |

布局名称只描述逻辑 schema。openGauss 可以把大 TEXT 压缩或移入 TOAST relation，ClickHouse 在同一 data part 中按列保存 payload 流；物理结构以存储证据为准。

查询样本的时间字段把成本按归属拆分，便于判断差异来自数据库还是客户端。

| 成本类别 | 观测字段 | 四布局差异 |
|---|---|---|
| 数据库原生 | `query_complete_ms`；ClickHouse `scanned_bytes`、`scanned_rows`、`QueryFinish` | 由执行计划、列裁剪、压缩和后台维护决定 |
| Python／客户端编排 | `request_count`、`resolver.requests` | 只有 `asset_ref` 在数据库查询之外增加 resolver 调用，runner 定义 `request_count = 1 + resolver_requests` |
| 文件系统 I/O | `recovery_ms` 中的 `resolver.read_ms` | 只有 `asset_ref` 读取本地内容寻址目录 |
| 网络响应 | `response_bytes`（`database` 与 `resolver_payload`）、`database_protocol_bytes` | 详情与批量恢复的传输量随布局变化 |
| 校验 | `validation_ms` | 四布局一致：客户端长度、SHA-256 与 truth 行对比 |

`application_ready_ms` 是应用可用边界，覆盖上述全部分项；ClickHouse 的 `recovery_ms` 还包含 HTTP 响应体解析，openGauss 只包含行归一化。

## 3. 部分切片中的观测区分度

统计口径：先在同一轮内对样本取中位数，再对四轮取中位数，与 `report/summarize.py` 的 `round-first-four-round-median` 一致。下表数值来自 `main-matrix-attempt-1` 的轮次 `result.json` 与轮次 `run-manifest.json`。

openGauss（部分正式切片，四轮中位数，单位 ms）：

| 布局 | 列表 `list:middle` | 详情 `detail:text_2m` | 批量 `batch:main` | 写入 wall | 写入 rows/s |
|---|---:|---:|---:|---:|---:|
| `same_table` | 13.949 | 27.362 | 948.1 | 9,277.7 | 5,231.3 |
| `separate` | 14.039 | 27.636 | 937.0 | 11,470.1 | 4,231.4 |
| `full_core` | 14.075 | 27.178 | 934.1 | 11,938.3 | 4,065.4 |
| `asset_ref` | 13.884 | 33.501 | 2,502.9 | 14,285.4 | 3,397.5 |

ClickHouse（部分正式切片，四轮中位数，单位 ms）：

| 布局 | 列表 `list:middle` | 详情 `detail:text_2m` | 批量 `batch:main` | 写入 wall | 写入 rows/s |
|---|---:|---:|---:|---:|---:|
| `same_table` | 12.485 | 34.288 | 1,737.3 | 5,307.1 | 9,145.2 |
| `separate` | 13.286 | 34.690 | 1,678.4 | 7,936.6 | 6,115.2 |
| `full_core` | 13.294 | 35.371 | 1,779.9 | 8,476.7 | 5,725.6 |
| `asset_ref` | 13.396 | 25.092 | 1,795.8 | 9,979.8 | 4,863.3 |

### 3.1 asset_ref 的检索路径

`asset_ref` 的详情恢复是逐行外部读取：查询 `events_analytics` 与 `assets` 取得引用后，resolver 读取本地内容寻址文件。原始样本显示该路径是按返回行数增长的 N+1 形态。

| 场景 | 每样本请求数 | 构成 | 证据 |
|---|---:|---|---|
| `detail:text_2m` | 2 | 1 次 catalog 查询 + 1 次本地文件读取 | 两引擎一致，`resolver.requests=1` |
| `batch:main` | 161 | 1 次 catalog 查询 + 每返回行 1 次本地文件读取 | 两引擎一致，`resolver.requests=160` |

数据库侧查询次数不随返回行数增长，但每返回行都产生一次独立文件读取与一次客户端校验。`asset_ref` 的列表与预览场景不访问对象目录，`resolver.requests=0` 且 `resolver_payload_bytes=0`。

### 3.2 ClickHouse 详情扫描字节

ClickHouse `detail:text_2m` 的单次查询扫描量在轮次间为常量：

| 布局 | `scanned_bytes` | `scanned_rows` |
|---|---:|---:|
| `same_table` | 9,982,294 | 4,749 |
| `full_core` | 9,982,294 | 4,749 |
| `separate` | 6,325,425 | 12,326 |
| `asset_ref` | 1,467,129 | 8,192 |

该差异说明 `asset_ref` 的详情恢复把 payload 字节移出数据库扫描范围，`same_table` 与 `full_core` 在同一查询中读取 payload 列。openGauss 的 `scanned_bytes` 记录为 `unavailable`，只有 `scanned_rows`（每详情查询 1 行），因此该对比只在 ClickHouse 内有证据。

### 3.3 可以解释的信号

**openGauss `asset_ref` 的批量恢复与写入劣势。** 批量恢复中位数 2,502.9 ms，其余三布局为 934.1–948.1 ms。分项显示该差距来自检索路径切换：`asset_ref` 的数据库查询中位数降到 8.4 ms（其余布局 408.5–423.3 ms），resolver 读取升到 1,984.5 ms（其余布局 15.9–16.5 ms）。写入 wall 14,285.4 ms 高于 9,277.7–11,938.3 ms，其中 `asset_publish_ms` 中位数约 1.05 s。

**ClickHouse 的布局相关扫描字节与详情中位数。** 详情扫描字节如上表；对应的详情中位数为 `asset_ref` 25.092 ms、其余布局 34.288–35.371 ms。批量恢复中 `asset_ref` 的数据库查询中位数降到 13.3 ms、resolver 读取升到 1,284.1 ms，总量 1,795.8 ms 与其余布局 1,678.4–1,779.9 ms 接近。该差异在单主机、串行、无并发条件下取得，作为机制方向性证据。

**写入成本粗排序。** 两引擎一致：`same_table` < `separate` < `full_core` < `asset_ref`。openGauss 9,277.7 / 11,470.1 / 11,938.3 / 14,285.4 ms，ClickHouse 5,307.1 / 7,936.6 / 8,476.7 / 9,979.8 ms。`full_core` 与 `separate` 的差距小于各自到 `same_table` 的差距，该排序只作为方案级先后顺序。

**批量校验下限。** 四布局的批量 `validation_ms` 中位数都在 495–498 ms，是客户端对约 122.5 MiB 返回内容做长度与 SHA-256 校验的固定成本，不随布局变化。

### 3.4 尚未分辨的信号

列表与详情中位数在多数布局之间接近：openGauss 列表 13.884–14.075 ms、详情 27.178–27.636 ms；ClickHouse 列表 12.485–13.396 ms。这些差值小于四轮波动和主机噪声，本评估不据此形成布局结论。

## 4. 合成数据的区分能力与边界

冻结语料规模（`generation-manifest.json`，`status=complete`）：

| 项目 | 数值 |
|---|---:|
| 事件数 | 48,534 |
| 写入 block | 190（每 block 256 行） |
| `main` cohort payload | 160 条，原始 128,450,560 B（122.5 MiB） |
| 查询窗口 | 27,561 行，分页 256 |
| 语料 payload 文件 | 1,481 个（`main` 160 + 等总字节控制 1,320 + 边界样本 1） |
| seed | 20260907 |

`main` cohort 由四种 profile 等量组成：`text_64k`、`text_512k`、`text_2m`、`entropy_512k` 各 40 条。等总字节控制把同一 80 MiB 原始内容分别以 40 条 2 MiB（83,886,080 B）和 1,280 条 64 KiB（83,886,080 B）呈现，形成 80/80 MiB 的两组对照；该控制在本次切片中未执行。

两类内容的生成方式决定其压缩行为：

- 可压缩文本由 64 字符 SHA-256 marker 加固定短语 `" agent trace tool result observation reasoning step "` 循环拼接后截断，重复结构使 ZSTD 与 TOAST 压缩比远高于生产文本。
- 高熵内容由 seed、事件身份和 counter 经 SHA-256 扩展再做 URL-safe base64 编码，接近随机分布，用作压缩机制控制。

据此，语料能够区分的机制是 payload 放置、检索路径、响应字节和可压缩性对照；语料不代表生产 Trace 的 payload 大小分布、并发与混合读写、更新与删除 churn、缓存多样性、对象存储延迟、以及 extension 内部调度。

## 5. Extension 方案对比

数据库内或进程内 extension 比 Python `asset_ref` 更接近生产集成 LOB 路径，原因是进程穿越次数、事务与失败语义、缓存、调度和可观测性的归属都不同：

| 维度 | Python `asset_ref`（本切片） | extension 集成路径 |
|---|---|---|
| 进程穿越 | 每次详情恢复一次数据库查询加一次本地文件读取，按返回行数增长 | LOB 读取留在数据库进程内，减少客户端往返 |
| 事务与失败语义 | 引用可见性与文件发布的一致性由 runner 维护 | 依赖数据库事务、回滚和故障恢复语义 |
| 缓存 | 依赖操作系统页缓存，无数据库缓存参与 | 可以参与数据库缓冲、预热和淘汰策略 |
| 调度 | 单进程串行发起读取 | 由数据库调度、并发和内存管理决定 |
| 可观测性 | 只有应用侧计数和计时 | 可暴露数据库侧等待、读取量和内部指标 |

`asset_ref` 保留为应用侧参考布局，用于衡量“引用加外部对象”这一实现方式的成本。数据库内或 extension 承载的存储定义为第五个候选布局 `db_lob_ref`，它使用独立的查询、存储和失败语义，不重命名 `asset_ref`，也不并入原四布局矩阵的排序。

## 6. 最小后续实验集合

进入生产推断前至少需要以下实验，全部在同一主机上使用同一冻结输入和同一输出契约。

1. **同机 xstore／ClickHouse 串行对比。** 两引擎读取同一 48,534 条事件、190 个 block 和同一 payload 集合，复用现有 truth、响应字节和清理门禁，串行运行以避免资源争用。
2. **`db_lob_ref` 独立矩阵。** 在 xstore 上按 extension 能力报告实现第五布局，单独汇总，不与四布局结果合并排序。
3. **冷／热缓存区分。** 记录缓存控制动作与实际生效证据，冷热两组分别报告。
4. **并发。** 在单 target 内施加并发查询和混合读写负载，记录响应时间分布与资源水位。
5. **更新与删除生命周期。** 覆盖 payload 更新、引用替换、删除和保留策略对空间、扫描量和恢复路径的影响。
6. **extension 失败与恢复。** 覆盖 extension 不可用、部分失败和重启恢复后的可见性与内容完整性。
7. **数据库可见资源证据。** 记录扫描字节、读取行数、后台任务、内存与连接水位，用于把应用侧差异归因到数据库机制。

## 7. 结论边界与来源

本切片可以支持的结论：

- 四种布局在单主机、串行、无并发条件下存在写入成本先后顺序 `same_table` < `separate` < `full_core` < `asset_ref`；
- openGauss `asset_ref` 的批量恢复与其写入路径相对其余布局更慢，差距来自 resolver 逐行读取；
- ClickHouse 的详情扫描字节随布局改变，`asset_ref` 把 payload 字节移出数据库扫描范围；
- 客户端校验是全布局共有的固定成本。

本切片不能支持的结论：

- 任何布局在生产分布、并发或混合负载下的性能排名；
- 两引擎之间的绝对性能高低，数字包含行存与列存、访问结构、压缩、协议和后台维护的综合影响；
- 更新、删除、对象存储网络延迟、多租户隔离和保留策略下的行为；
- extension 承载 LOB 的成本与失败语义。

来源与依据：

- [阶段三实验设计与证据契约](json-storage-stage3-experiment-design-2026-09-09.md)
- [黄区 xstore 对比交接设计](../superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md)
- [黄区交接实施计划](../superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md)
- [布局矩阵 runner](../../experiments/json-storage-stage3/runner/run_layout_matrix.py)
- [汇总器与 workload 门禁](../../experiments/json-storage-stage3/report/summarize.py)
- [冻结输入生成器](../../experiments/json-storage-stage3/generator/generate_payloads.py)
- [Asset resolver 实现](../../experiments/json-storage-stage3/runner/assets.py)
- [不变量、计时与 adapter 契约](../../experiments/json-storage-stage3/runner/common.py)

结构化扫描清单：`agent-trace-json-storage-stage3-generation`（`generation-manifest.json`）、每 target `run-manifest.json`、每轮 `result.json` 与 `samples.jsonl`，均位于本地 `docs/temp/json-storage-stage3/runs/20260917-priority/main-matrix-attempt-1/`。
