# JSON Storage Stage 3 四布局代表性评估

> 状态：全部正式运行完成
>
> 评估日期：2026-09-17；结果更新日期：2026-09-20
>
> 适用设计：[阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)
>
> 量化证据：本地 gitignored 目录 `docs/temp/json-storage-stage3/runs/20260917-priority/` 及其合并汇总 `summary.json`（格式 `agent-trace-json-storage-stage3-combined-summary`，版本 1），不进入迁移分支

本文评估 Stage 3 四种长 payload 布局实验的代表性。评估对象是已完成的全部正式运行、冻结合成语料和现有 Python adapter 实现；目的是说明这些结果能够支持哪些结论、不能支持哪些结论，以及进入生产推断前需要补齐的实验。

## 1. 范围与证据等级

代表性分为四个相互独立的维度，必须分别判定，不能合并为“实验结果是否可信”这一个问题。

| 维度 | 含义 | 可判定范围 |
|---|---|---|
| 语义代表性 | 布局名称对应的逻辑数据组织与分析语义和生产模型的接近程度 | 四种布局的分析过滤、详情恢复、联合水位和清理契约由同一 runner 固定，覆盖同表、分表、双写和引用恢复 |
| 实现代表性 | 当前 Python adapter 与生产集成读写路径的接近程度 | `asset_ref` 采用应用侧 resolver 加本地内容寻址目录；数据库内调度和 extension 路径不在本次实现内 |
| 数据代表性 | 冻结语料与生产 Trace 在分布、并发和生命周期上的接近程度 | 合成语料可以区分 payload 放置、检索路径、响应字节和可压缩性控制；生产分布不在覆盖范围内 |
| 统计完备性 | 四轮 Latin square 与完整 workload／provenance 门禁的满足程度 | `main`、两组等总字节控制与 `correctness_only` 四个工作负载全部完成，汇总器通过完整门禁并产出合并汇总 |

正式运行的规模与状态如下。

| 项目 | 观测值 | 证据状态 |
|---|---|---|
| 矩阵 target 数 | 32（2 引擎 × 4 布局 × 4 工作负载） | 全部 `status=complete` |
| 矩阵正式样本 | 17,560 个，成功 17,560 个，失败 0 个 | 完整响应字节与 truth 校验通过 |
| 每引擎样本分布 | `main` 5,360、`equal_total_few_large` 1,520、`equal_total_many_medium` 1,520、`correctness_only` 380 | 四个工作负载均具备四轮证据 |
| 轮次 | 每 target 4 轮四阶 Latin square | 每轮每场景 30 次查询样本，`batch` 场景每轮 5 次 |
| part 状态控制 | 4 种布局 × 4 个状态，每状态 360 个样本，合计 5,760 个 | ClickHouse 单独控制，不构成第五种布局 |
| 混合负载 | 4 种布局合计调度 243,194 个请求，成功 239,715 个，scheduler drop 3,479 个，查询失败 0 个 | 全部偏差为 scheduler drop，两者分开陈述 |
| Asset 故障 | 2 引擎 × 6 个固定用例 | validation error 与 execution error 均为 0 |
| 正确性专用 | 8 target × 95 个样本，合计 760 个，失败 0 个 | 覆盖 `batch:correctness_only`、`detail:unicode_boundary`、`list:first`、`list:middle` |
| 正式样本合计 | 263,035 个 | 汇总器与各 target 产物一致 |
| 冻结输入身份 | `identity_sha256 = a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8` | 48,534 条记录、190 个 block、seed 20260907 |
| 引擎 | openGauss 6.0.0；ClickHouse 25.12.11.4 | 容器 `agent-trace-opengauss-v6`、`agent-trace-clickhouse-25-12` |
| 主机 | x86_64、8 CPU、16,291,948 KiB、WSL2 内核 6.6.114.1 | 全部 target 同机串行执行 |

汇总器 `report/summarize.py` 以 32 个矩阵 target、4 个 part 状态目录、4 个混合负载目录和 2 个 Asset 故障目录为输入，通过 workload 与 provenance 门禁后产出单份合并汇总，本文数值取自该汇总及其同源产物。统计口径为 `round-first-four-round-median`：先在同一轮内对样本取中位数，再对四轮取中位数。`correctness_only` 只参与正确性门禁，不进入性能统计；Asset 故障实验不绑定正式输入身份，其产出为状态与分类，不含时延。

本文只做代表性判定，不引入新的实验数据。

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

`recovery_ms` 覆盖查询后结果恢复阶段，`application_ready_ms` 是应用可用边界，覆盖上述全部分项；ClickHouse 的 `recovery_ms` 还包含 HTTP 响应体解析，openGauss 只包含行归一化。以下各表的应用可用 p50 取自样本字段 `application_ready_ms`，在轮次 `result.json` 中以 `latency_ms` 发布，均为四轮轮级中位数。

## 3. 完整矩阵中的观测区分度

### 3.1 等总字节分布控制

这是全实验最强的分离信号。两组使用相同的 80 MiB（83,886,080 bytes）原始内容总量，只改变对象数量：`equal_total_few_large` 为 40 条 2 MiB payload，`equal_total_many_medium` 为 1,280 条 64 KiB payload。批量恢复的应用可用 p50 如下，单位 ms。

| 引擎 | 布局 | `few_large`（40 × 2 MiB） | `many_medium`（1,280 × 64 KiB） |
|---|---|---:|---:|
| openGauss | `same_table` | 620.58 | 599.05 |
| | `separate` | 625.91 | 605.05 |
| | `full_core` | 611.73 | 599.51 |
| | `asset_ref` | 582.86 | 2,137.03 |
| ClickHouse | `same_table` | 1,106.58 | 1,075.88 |
| | `separate` | 1,335.73 | 1,115.58 |
| | `full_core` | 1,122.54 | 1,070.27 |
| | `asset_ref` | 777.26 | 8,085.18 |

`asset_ref` 在 `few_large` 中是两个引擎上最快的布局，在 `many_medium` 中变为最慢。该反转用两个口径分别度量，两者回答不同问题，不可互相替代。

| 口径 | 定义 | openGauss | ClickHouse |
|---|---|---:|---:|
| 跨组比值 | 同一布局 `many_medium` p50 除以 `few_large` p50，度量对分布变化的敏感度 | 3.67（`asset_ref`） | 10.40（`asset_ref`） |
| 组内最大最小比 | 同一组内最慢布局除以最快布局，度量在给定分布下选错布局的代价 | `many_medium` 3.567（`asset_ref` 2,137.03 对 `same_table` 599.05）；`few_large` 1.074 | `many_medium` 7.554（`asset_ref` 8,085.18 对 `full_core` 1,070.27）；`few_large` 1.718 |

两组的原始内容总量、resolver 传输字节（均为 419,430,400 bytes）和校验成本几乎相同，唯一显著变化是对象数量由 40 变为 1,280，resolver 请求数由 200 增至 6,400。客户端恢复 p50（`recovery_ms`）承担全部增量：ClickHouse 由 430.62 ms 升至 7,713.97 ms，openGauss 由 250.20 ms 升至 1,772.09 ms。写入侧同方向：Asset 发布时间由 443.54 ms 和 473.77 ms 升至 4,948.01 ms 和 5,296.20 ms。结论是每对象固定成本主导 Asset 路径，成本由对象数量决定，不由字节总量决定。

### 3.2 主 cohort 的四布局结果

openGauss（四轮中位数，单位 ms）：

| 布局 | 列表 `list:middle` | 详情 `detail:text_2m` | 批量 `batch:main` | 写入 wall | 写入 rows/s |
|---|---:|---:|---:|---:|---:|
| `same_table` | 13.62 | 26.14 | 939.62 | 9,185.00 | 5,284.17 |
| `separate` | 14.72 | 27.21 | 999.53 | 11,345.62 | 4,277.86 |
| `full_core` | 13.89 | 26.14 | 946.92 | 11,738.58 | 4,134.67 |
| `asset_ref` | 13.54 | 32.57 | 1,005.33 | 13,770.23 | 3,524.58 |

ClickHouse（四轮中位数，单位 ms）：

| 布局 | 列表 `list:middle` | 详情 `detail:text_2m` | 批量 `batch:main` | 写入 wall | 写入 rows/s |
|---|---:|---:|---:|---:|---:|
| `same_table` | 13.61 | 36.50 | 1,821.15 | 5,642.06 | 8,603.76 |
| `separate` | 14.45 | 36.86 | 1,717.76 | 8,399.86 | 5,777.96 |
| `full_core` | 14.82 | 36.71 | 1,842.34 | 9,013.57 | 5,385.47 |
| `asset_ref` | 14.19 | 26.41 | 1,745.31 | 10,449.09 | 4,644.81 |

主 cohort 的 160 个对象平均 802,816 bytes，批量恢复的四布局最大最小比为 1.070（openGauss）和 1.073（ClickHouse），成本转移接近持平；第 3.1 节改变对象数量后结论发生变化。

### 3.3 asset_ref 的检索路径

`asset_ref` 的详情恢复是逐行外部读取：查询 `events_analytics` 与 `assets` 取得引用后，resolver 读取本地内容寻址文件。原始样本显示该路径是按返回行数增长的 N+1 形态。

| 场景 | 每样本请求数 | 构成 | 证据 |
|---|---:|---|---|
| `detail:text_2m` | 2 | 1 次 catalog 查询 + 1 次本地文件读取 | 两引擎一致，每轮 30 个正式样本对应 60 次请求 |
| `batch:main` | 161 | 1 次 catalog 查询 + 每返回行 1 次本地文件读取 | 两引擎一致，每轮 5 个正式样本对应 805 次请求 |

数据库侧查询次数不随返回行数增长，但每返回行都产生一次独立文件读取与一次客户端校验。`asset_ref` 的列表与预览场景不访问对象目录：每轮 30 个样本对应 30 次请求，`resolver_bytes` 为 0。

### 3.4 ClickHouse 详情扫描字节

ClickHouse `detail:text_2m` 的单次查询扫描量在轮次间为常量：

| 布局 | `scanned_bytes` | `scanned_rows` |
|---|---:|---:|
| `same_table` | 9,982,294 | 4,749 |
| `full_core` | 9,982,294 | 4,749 |
| `separate` | 6,325,425 | 12,326 |
| `asset_ref` | 1,467,129 | 8,192 |

该差异说明 `asset_ref` 的详情恢复把 payload 字节移出数据库扫描范围，`same_table` 与 `full_core` 在同一查询中读取 payload 列。openGauss 的扫描字节记录为不可观测，只有扫描行数（每详情查询 1 行），因此该对比只在 ClickHouse 内有证据。

### 3.5 可以解释的信号

**写入成本顺序。** 两引擎完全一致：`same_table` < `separate` < `full_core` < `asset_ref`。增量对应各布局必须执行的额外步骤：第二张表的行构造与提交、Full/Core 双写，以及 160 次对象发布（openGauss 1,040.85 ms，ClickHouse 1,134.98 ms）。

**`asset_ref` 的每对象固定成本。** openGauss 在四个 profile 上全部最慢，最大差距出现在 `detail:text_64k`：20.51 ms 对 10.85 ms，最大最小比 1.890；其查询完成 p50 仅 1.84 ms，客户端恢复 p50 为 10.72 ms，而数据库内布局为 0.03 ms。固定开销在 64 KiB 对象上无法摊薄。

**ClickHouse 的大对象交叉。** `asset_ref` 在 `detail:text_2m` 上最快（26.41 ms 对 36.50–36.86 ms，最大最小比 1.396），在 `detail:text_64k` 上最慢（14.19 ms 对 8.67 ms，最大最小比 1.637）。分界点位于 512 KiB 与 2 MiB 之间：ZSTD 解压与列式读取的成本随 payload 增大快于本地文件读取。

**ClickHouse `separate` 的 JOIN 路径。** 过滤谓词只作用在 SQL 左表 `events_analytics`，JOIN 条件不构成右表排序键的可用前缀，`event_payloads` 没有等价谓词下推。`trace:p50` 扫描 56,726 行、132,368,859 bytes，p50 为 65.33 ms；`same_table` 扫描 5,393 行、13,171,006 bytes，p50 为 18.58 ms。

**ClickHouse 列表读取量与 granularity。** 保留 payload 列的 `events` 在 `list:middle` 只扫描 23,947 行、1,800,361 bytes，三种把 payload 移出的布局各扫描 38,912 行、3,361,448 bytes，多约 62%。宽表 24 个 mark、窄表 17 个 mark，granule 越细主键裁剪越精确。该效应属于列存的 granule 划分，与 payload 是否被读取无关，不适用于 openGauss。

**part 状态与布局正交。** ClickHouse 碎片态的 `list:first` p50 为 38.29–44.99 ms，单 part 态为 12.34–13.75 ms，同一布局内比值 2.90–3.27；同一状态下四种布局的最大最小比只有 1.114–1.175。两个因素方向一致，量级相差约三倍。

**持续批量读取下的隔离效果是双向的。** `batch_loop` 相中 `asset_ref` 的前台 list 与 preview 只有 2 次和 1 次 scheduler drop，`same_table`、`separate`、`full_core` 分别为 477/505、653/666、582/581 次；同一相中 `asset_ref` 自身的批量恢复 p50 为 2,361.76 ms，是四种布局中最慢，`same_table` 为 1,906.53 ms。竞争被移出数据库查询路径，并未消除。

**批量校验下限。** 四布局的批量 `validation_ms` 中位数在 openGauss 上为 484.44–488.12 ms，在 ClickHouse 上为 508.49–515.83 ms，是客户端对约 122.5 MiB 返回内容做长度与 SHA-256 校验的固定成本，不随布局变化。

### 3.6 当前场景未分辨的信号

**列表与预览。** 四布局的应用可用 p50 最大最小比在 openGauss 上为列表 1.069–1.087、预览 1.009–1.015，在 ClickHouse 上为列表 1.089–1.140、预览 1.108–1.128。机制证据是八个 target 的列表与预览查询 `payload_selected` 均为 false，声明来源表与设计一致，openGauss 在四种布局下均精确扫描 256 行。控制变量与机制证据同时成立，因此这是“当前场景未分辨”的正式结论：payload 是否与分析列同表，不改变本工作负载的列表与预览成本。

**`detail_2m` 与 `trace_long` 两相的干扰。** 前台 list 与 preview 的 p50 相对 quiet 相的变化落在 −3.39% 至 +3.91% 之间，scheduler drop 最多 5 次，变化方向在布局之间不一致。该幅度按运行间波动处理。结论是这两相的干扰强度不足以扰动前台，不构成隔离性良好的结论；需要提高干扰字节速率后重测。

## 4. 合成数据的区分能力与边界

冻结语料规模：

| 项目 | 数值 |
|---|---:|
| 事件数 | 48,534 |
| 写入 block | 190（每 block 256 行） |
| `main` cohort payload | 160 条，原始 128,450,560 B（122.5 MiB） |
| 查询窗口 | 27,561 行，分页 256 |
| 语料 payload 文件 | 1,481 个（`main` 160 + 等总字节控制 1,320 + 边界样本 1） |
| seed | 20260907 |

`main` cohort 由四种 profile 等量组成：`text_64k`、`text_512k`、`text_2m`、`entropy_512k` 各 40 条，可压缩内容合计 107,479,040 B，高熵内容合计 20,971,520 B。等总字节控制把同一 80 MiB 原始内容分别以 40 条 2 MiB 和 1,280 条 64 KiB 呈现，两组均已完成正式运行，结果见第 3.1 节。

语料的可压缩性远高于生产文本，这是最主要的数据代表性限制。可压缩 profile 由一个 116 字节块（64 字符 SHA-256 marker 加 52 字符固定短语）循环填充到目标长度后截断，重复结构使压缩比极高；高熵内容由 `seed:event_id:counter` 经 SHA-256 扩展后 URL-safe base64 编码得到，用作压缩机制控制。

| profile | 原始 bytes | zlib-6 bytes | 压缩比 |
|---|---:|---:|---:|
| `text_64k` | 65,536 | 343 | 191.1 |
| `text_512k` | 524,288 | 1,899 | 276.1 |
| `text_2m` | 2,097,152 | 7,233 | 289.9 |
| `entropy_512k` | 524,288 | 397,109 | 1.3 |

作为对照，生产 Agent Trace 文本（LLM prompt、completion 与工具输出）的压缩比通常为 3–6 倍；该区间是参考口径，不是本实验的测量值。空间结论因此按双口径给出：ClickHouse 的 `asset_ref` 总占用实测为 `same_table` 的 6.921 倍（131,733,393 bytes 对 19,035,002 bytes），按 3–6 倍参考压缩比折算后为 2.40–3.57 倍；折算只改变数据库内 payload 列的压缩后字节，非 payload 部分（3,241,582 bytes）与 Asset 目录字节保持不变。openGauss 实测为 2.884 倍（161,456,128 bytes 对 55,975,936 bytes），同样依赖本语料，不提供折算区间。

据此，语料能够区分的机制是 payload 放置、检索路径、对象数量、响应字节和可压缩性对照；语料不代表生产 Trace 的 payload 大小分布、压缩率、并发与混合读写、更新与删除 churn、缓存多样性、对象存储延迟，以及 extension 内部调度。

## 5. Extension 方案对比

数据库内或进程内 extension 比 Python `asset_ref` 更接近生产集成 LOB 路径，原因是进程穿越次数、事务与失败语义、缓存、调度和可观测性的归属都不同：

| 维度 | Python `asset_ref`（本实验） | extension 集成路径 |
|---|---|---|
| 进程穿越 | 每次详情恢复一次数据库查询加一次本地文件读取，按返回行数增长 | LOB 读取留在数据库进程内，减少客户端往返 |
| 事务与失败语义 | 引用可见性与文件发布的一致性由 runner 维护 | 依赖数据库事务、回滚和故障恢复语义 |
| 缓存 | 依赖操作系统页缓存，无数据库缓存参与 | 可以参与数据库缓冲、预热和淘汰策略 |
| 调度 | 单进程串行发起读取 | 由数据库调度、并发和内存管理决定 |
| 可观测性 | 只有应用侧计数和计时 | 可暴露数据库侧等待、读取量和内部指标 |

第 3.1 节确定成本由每对象固定开销主导。extension 消除进程穿越与文件打开，但不消除每个对象的 catalog 查找与 LOB 定位。据此形成可证伪预期：extension 应当改善多对象场景，但不会把该场景的成本曲线压平。该预期是待验证项，不是本实验结果。

`asset_ref` 保留为应用侧参考布局，用于衡量“引用加外部对象”这一实现方式的成本。数据库内或 extension 承载的存储定义为第五个候选布局 `db_lob_ref`，它使用独立的查询、存储和失败语义，不重命名 `asset_ref`，也不并入原四布局矩阵的排序。

## 6. 最小后续实验集合

进入生产推断前至少需要以下实验，全部在同一主机上使用同一冻结输入和同一输出契约。

1. **`db_lob_ref` 独立矩阵。** 按 extension 能力报告实现第五布局，单独汇总，不与四布局结果合并排序。
2. **冷／热缓存区分。** 记录缓存控制动作与实际生效证据，冷热两组分别报告。
3. **并发。** 在单 target 内施加并发查询和混合读写负载，记录响应时间分布与资源水位。
4. **更新与删除生命周期。** 覆盖 payload 更新、引用替换、删除和保留策略对空间、扫描量和恢复路径的影响。
5. **extension 失败与恢复。** 覆盖 extension 不可用、部分失败和重启恢复后的可见性与内容完整性。
6. **更高干扰强度的混合负载。** 提高干扰流的字节速率，使 `detail_2m` 与 `trace_long` 两类干扰进入可观测区间，再判定隔离性。
7. **接近生产可压缩性的语料。** 用压缩比落在 3–6 倍区间的内容重测空间与压缩结论，替代当前语料的折算口径。

## 7. 结论边界与来源

本实验可以支持的结论：

- 四种布局在单主机、串行条件下的写入成本顺序 `same_table` < `separate` < `full_core` < `asset_ref`，两引擎一致；
- Asset 路径的成本由对象数量决定而非字节总量，等总字节控制给出跨组比值 10.40 与 3.67、组内最大最小比 7.554 与 3.567；
- 列表与预览在当前工作负载下四布局未分辨，机制证据为八个 target 的 `payload_selected` 全部为 false；
- ClickHouse `separate` 在 Trace 与多行详情上的 JOIN 路径代价，以及列存 granule 划分导致的列表读取量差异；
- ClickHouse 的 part 状态影响大于布局选择，且与布局正交；
- 持续批量读取下 `asset_ref` 改善前台隔离，同时自身批量路径变慢，竞争被转移而非消除；
- `asset_ref` 库内占用最低、总物理占用最高，客户端校验是全布局共有的固定成本。

本实验不能支持的结论：

- 任何布局在生产分布、并发或混合负载下的性能排名；
- 两引擎之间的绝对性能高低，数字包含行存与列存、访问结构、压缩、协议和后台维护的综合影响；
- 隔离性的正面判断，`detail_2m` 与 `trace_long` 的干扰强度不足；
- 冷缓存与首读成本、更新与删除 churn、对象存储网络延迟、多租户隔离和保留策略下的行为；
- extension 承载 LOB（`db_lob_ref`）的成本与失败语义。

来源与依据：

- [阶段三实验设计与证据契约](json-storage-stage3-experiment-design-2026-09-09.md)
- [黄区 xstore 对比交接设计](../superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md)
- [黄区交接实施计划](../superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md)
- [布局矩阵 runner](../../experiments/json-storage-stage3/runner/run_layout_matrix.py)
- [汇总器与 workload 门禁](../../experiments/json-storage-stage3/report/summarize.py)
- [冻结输入生成器](../../experiments/json-storage-stage3/generator/generate_payloads.py)
- [Asset resolver 实现](../../experiments/json-storage-stage3/runner/assets.py)
- [不变量、计时与 adapter 契约](../../experiments/json-storage-stage3/runner/common.py)

结构化扫描清单：`agent-trace-json-storage-stage3-generation`（`generation-manifest.json`）、每 target `run-manifest.json`、每轮 `result.json` 与 `samples.jsonl`，以及合并汇总 `summary.json`，均位于本地 `docs/temp/json-storage-stage3/runs/20260917-priority/`。参与合并的目录为 `matrix-attempt-2`（ClickHouse 矩阵）、`matrix-attempt-3`（openGauss 矩阵）、`part-states`、`interference-attempt-2` 与 `asset-failures-attempt-1`。
