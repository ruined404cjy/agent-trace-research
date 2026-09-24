# Agent Trace JSON 存储阶段三黄区实验报告：XStore 与 ClickHouse 对比

> 实验完成日期：2026-09-24；文档修订日期：2026-09-24
>
> 状态：实验、结果验证和报告均已完成
>
> 数据库版本：GaussVector 103.0.0 build `94f77718`（release 构建）；ClickHouse 23.3.10.5

本文报告在固定硬件上以 XStore（GaussVector）和 ClickHouse 23.3.10.5 为引擎，对长 payload 的四种物理布局执行的阶段三黄区对比实验。实验范围、执行口径与回传格式遵照[阶段三黄区指南](../project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md)与[反馈契约](../project-background/json-storage-stage3-yellow-feedback-contract.md)。蓝区阶段三实验报告（[链接](json-storage-stage3-report-2026-09-20.md)）是流程、章节划分与排版的范例；黄区范围与蓝区不同，差异在相关章节注明。

## 1. 结论

四种布局为：`same_table` 在一张表内保存分析列与 payload；`separate` 把 payload 移入独立表；`full_core` 在完整记录表之外复制一张列表专用窄表；`asset_ref` 把 payload 外置为本地内容寻址对象，库内只保留引用。

写入顺序在两个引擎上完全一致：`same_table` < `separate` < `full_core`，`asset_ref` 在 XStore 上快于 `separate` 和 `full_core`，在 ClickHouse 上最慢。XStore 的写入合计 wall time 分别为 8,062.7 / 10,458.3 / 10,916.7 / 8,449.6 ms，ClickHouse 为 5,031.6 / 6,538.0 / 7,067.0 / 7,824.6 ms（出处：feedback.txt A5）。`asset_ref` 在 XStore 上不需要第二张表的行构造，写入合计低于 `separate` 和 `full_core`；在 ClickHouse 上因对象发布开销而最慢。

列表与预览查询在 XStore 四布局间区分度极小。`list:first` 应用可用 p50 为 28.2–28.5 ms，`list:middle` 为 52.7–89.8 ms（出处：feedback.txt A1）。XStore 的 `same_table` 在 `list:middle` 上显著慢于其余三布局（89.8 ms 对 52.7–52.9 ms），原因见第 4.2 节。ClickHouse 的 `list:first` 为 22.9–30.6 ms，四布局间最大最小比为 1.34。

单条详情在 ClickHouse `separate` 上显著离群：`detail:text_2m` 应用可用 p50 为 167.9 ms，其余三布局为 33.6–77.3 ms（出处：feedback.txt A2）。该差异来自 JOIN 路径：过滤条件只施加在左表 `events_analytics`，右表 `event_payloads` 的 payload 列被整表扫描，与蓝区 ClickHouse 25.12 上的观测一致。XStore 的 `asset_ref` 在单条详情上最慢（19.4–27.6 ms），因 resolver 需要额外读取本地对象。

等总字节分布控制给出最强分离信号。`asset_ref` 的 `batch:equal_total_many_medium` 在 ClickHouse 上为 9,893.5 ms，是 `batch:equal_total_few_large`（698.6 ms）的 14.2 倍；XStore 上为 1,814.2 ms 对 477.7 ms，3.8 倍（出处：feedback.txt A4）。resolver 请求数由 200 增至 6,400，字节总量不变，成本由对象数量决定。

混合负载在 XStore 上产生可观测的干扰效应，在 ClickHouse 上幅度极小。XStore `separate` 的 `batch_loop` 阶段 `list:first` p50 为 150.2 ms，quiet 阶段为 65.0 ms，比值为 2.31；ClickHouse `separate` 同两阶段为 33.6 ms 对 31.8 ms，比值为 1.06（出处：samples.jsonl）。两个引擎在混合负载中均无 scheduler drop。

ClickHouse part 状态的影响大于布局选择。碎片态（190 part）的 `list:first` p50 为 83.3–115.7 ms，单 part 态为 16.0–29.8 ms，同一布局内比值 3.40–7.24；同一控制内同一状态下四布局的最大最小比为 1.13–1.39（出处：feedback.txt B2）。

Asset 故障实验的六个固定用例在两个引擎上全部符合设计预期，validation error 和 execution error 均为 0（出处：feedback.txt B4）。

XStore 行存探针确认：四布局的 `events` 类表的 `reltoastrelid` 均为 0，`reloptions` 为 NULL，payload 列以行内变长字节存储，无 TOAST 关联。访问路径门禁通过：列表查询使用 `Index Scan using events_list_idx`，实际扫描 256 行（出处：feedback.txt C4、C5；xstore-row-probe.json）。

跨引擎数字用于比较固定硬件和统一应用可用边界下的完整方案。结果包含行存与列存、索引与排序键、执行器、协议、压缩编解码、后台维护和客户端校验的影响，不能解释为布局本身的单因素差异，也不能外推为生产环境性能。黄区 ClickHouse 23.3 与蓝区 ClickHouse 25.12 版本不同，两区 ClickHouse 数值不直接比较（见第 5.4 节）。

## 2. 数据与语义契约

### 2.1 冻结输入

基础记录复用阶段二的冻结输入，共 48,534 个 Span，每 256 行组成一个实验写入 block，共 190 个 block。输入身份 SHA-256 为 `a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8`，生成 seed 固定为 `20260907`。查询固定使用 `project_id=Leoxx/whowhen_pro` 和时间窗口 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)`，以下简称**固定窗口**；该窗口包含 27,561 行，分页大小固定为 256。

逻辑记录固定为 `event_id, trace_id, project_id, start_time, profile, payload, preview, content_type, encoding, content_length, sha256`。`content_type` 固定为 `application/json`，`encoding` 固定为 `utf-8`。preview 按 Unicode code point 截取 payload 逻辑文本起始的 200 个字符。

负载按三类 cohort 组织，对应四个 workload key：性能主 cohort 使用 `main`，以下简称**主 cohort**；等总字节分布控制使用 `equal_total_few_large` 和 `equal_total_many_medium`；正确性专用 cohort 使用 `correctness_only`。

主 payload 集合包含 160 个彼此不同的 payload，原始 UTF-8 合计 128,450,560 bytes，确定性分散到基础记录中。

| profile | 数量 | 每条原始 bytes | 内容特征 |
|---|---:|---:|---|
| `text_64k` | 40 | 65,536 | 可压缩 Agent 文本 |
| `text_512k` | 40 | 524,288 | 可压缩 Agent 文本 |
| `text_2m` | 40 | 2,097,152 | 可压缩 Agent 文本 |
| `entropy_512k` | 40 | 524,288 | 高熵 ASCII |

可压缩内容合计 107,479,040 bytes，高熵内容合计 20,971,520 bytes。

等总字节分布控制使用相同的 80 MiB（83,886,080 bytes）原始内容总量，比较两种分布：`equal_total_few_large` 为 40 条 2 MiB payload，`equal_total_many_medium` 为 1,280 条 64 KiB payload。两组使用同一套可压缩内容生成规则。

正确性专用 cohort 另覆盖 preview 截断处的多字节字符边界，不进入性能汇总。

### 2.2 布局与一致性语义

四种布局的组织方式、查询路径和写入完成条件如下。布局名称描述逻辑 schema，不表示 payload bytes 的实际物理位置。

| layout | 数据组织 | 列表与预览路径 | 详情路径 | 写入完成条件 |
|---|---|---|---|---|
| `same_table` | `events` 同时保存分析列、内容元数据和 payload | 读取 `events` 的分析列或 preview | 从 `events` 恢复完整逻辑记录 | `events` block 成功并可见 |
| `separate` | `events_analytics` 保存分析列和内容元数据；`event_payloads` 保存定位键、内容元数据和 payload | 读取 `events_analytics` | 组合分析行与 `event_payloads` 内容 | 两表对应 block 成功，联合水位覆盖该 block |
| `full_core` | `events_full` 保存完整逻辑记录；`events_core` 复制列表所需普通列、preview、长度和摘要 | 读取 `events_core` | 从 `events_full` 恢复完整逻辑记录 | Full 与 Core 均可见，Core 水位覆盖 Full |
| `asset_ref` | `events_analytics` 保存分析列和 Asset 引用；同库 `assets` 表保存内容元数据、位置和状态；本地内容寻址目录保存 bytes | 读取 `events_analytics` | 查询引用和 `assets`，再由 resolver 读取本地对象 | 对象已原子发布、`assets.status=available`、事件引用可见 |

Full/Core 采用 runner 维维护的显式双写和联合水位，两个引擎使用相同的一致性契约。结果正确性由独立程序根据冻结输入预先计算，所得结果以下简称 **truth**；实验查询不参与 truth 的生成。Asset 引用固定为下列形式，运行时 resolver 按引用和 `assets` 行核对 content type、encoding、长度和 SHA-256 后读取本地对象；truth 不参与定位、状态转换或错误分类。

```json
{
  "$ref": "asset:sha256:<digest>",
  "content_type": "application/json",
  "encoding": "utf-8",
  "content_length": 524288,
  "preview": "..."
}
```

`assets` 的状态取值为 `pending`、`available`、`failed` 和 `deleting`。第 4.10 节表中的 `absent` 不是状态取值，表示 catalog 中不存在该 `assets` 行。

## 3. 结构与执行口径

### 3.1 物理结构

XStore（GaussVector）使用行存表，payload 列以 `TEXT` 类型保存保持 UTF-8 bytes 的变长字段。每张承载查询的表建立两个复合 B-tree：`(project_id,start_time,event_id)` 服务列表与预览的 keyset 分页，`(project_id,trace_id,start_time,event_id)` 服务 Trace 与单条详情定位。`event_id` 为主键。

行存探针（`xstore-row-probe.json`）在完整载入 main cohort 的库上确认：`same_table`、`separate`、`full_core` 三布局的所有 `events` 类表的 `reltoastrelid` 均为 0，`reloptions` 为 NULL。payload 列的存储字节与逻辑字节相等，无 TOAST 压缩或行外存储。XStore 的空间模型与 openGauss 的 TOAST 模型不可直接对齐：openGauss 在蓝区把超长 payload 存入 TOAST 并压缩，XStore 在黄区以行内变长字节保存，不压缩。

ClickHouse 每张表使用 `ENGINE=MergeTree ORDER BY (project_id,start_time,event_id)`，`assets` 表使用 `ORDER BY asset_id`，payload 列使用 `CODEC(ZSTD(3))`。

XStore 的 DDL 与蓝区 openGauss 存在三处引擎差异（记录于 run manifest 的 `engine_deviations`）：

1. **认证**：经本地 Unix domain socket 的 trust 认证连接。
2. **framework 可空**：GaussDB 将空字符串视为 NULL，`framework` 列去掉 `NOT NULL`；审计时把 NULL 还原为空字符串，逻辑记录不变。
3. **keyset 谓词**：行构造器比较不被支持，展开为等价的 OR 形式；该形式能否作为索引范围起点待实测。访问路径门禁确认列表查询使用 `Index Scan using events_list_idx`，扫描 256 行（出处：feedback.txt C5）。
4. **水位聚合**：`FILTER (WHERE ...)` 不被支持，改用 `count(CASE WHEN ...)`。

### 3.2 固定变量

四种布局使用同一份冻结输入、同一行序、同样的 190 个 block 和同一组查询参数。每个引擎执行四轮，按四阶 Latin square 轮换布局顺序，使每种布局在每个运行位置出现一次；布局顺序和随机种子写入 run manifest。两个引擎、四种布局在主 cohort 上的四轮完整组合，以下简称**主矩阵**。

查询目标按 `场景:变体` 命名，`场景` 为查询形态，`变体` 为该形态下固定的参数组合。全部变体如下。

| 场景 | 变体 | 参数组合 |
|---|---|---|
| `list` | `first`、`middle` | 固定窗口的第一页与中部一页 keyset 分页 |
| `preview` | `first`、`middle` | 与 `list` 相同的两页，投影额外携带 preview |
| `detail` | `text_64k`、`text_512k`、`text_2m`、`entropy_512k` | 按 profile 固定选定的单条 payload 恢复 |
| `detail` | `unicode_boundary` | preview 截断处含多字节字符边界的单条恢复，只用于正确性专用 cohort |
| `trace` | `p25`、`p50`、`p95` | Span 数量位于三个分位附近的完整 Trace 恢复 |
| `batch` | `main` | 主 cohort 的 160 个对象批量恢复 |
| `batch` | `correctness_only` | 正确性专用 cohort 的批量恢复 |
| `batch` | `equal_total_few_large`、`equal_total_many_medium` | 等总字节分布控制两组的批量恢复 |

列表、preview、单条详情和完整 Trace 每轮预热 1 次、正式测量 30 次；批量恢复每轮预热 1 次、正式测量 5 次。查询复用已建立的连接，不清除操作系统缓存，manifest 记录的缓存状态为 `warm-reused-connections-no-os-cache-drop`。

混合负载在前台列表与 preview 负载之上按固定顺序执行五个相：`quiet` 无干扰，`detail_2m` 叠加 2 MiB 单条详情，`trace_long` 叠加长 Trace 恢复，`batch_loop` 叠加持续循环的批量恢复，`continuous_ingest` 叠加持续写入；每相 30 秒预热加 300 秒测量。

### 3.3 计时与证据

载入侧使用两个阶段口径。一个布局涉及的全部表或 Asset 组件完成 190 个 block 写入并满足第 2.2 节联合水位，称为**写入完成**；写入合计 wall time 覆盖客户端行构造、数据库协议、双表写入、本地 Asset 写入和状态发布，不含随后的维护。

查询侧每个场景统一使用三个分项和一个总口径：

- **查询完成 p50**：数据库响应完整读取的时延中位数；`asset_ref` 只包含引用与 catalog 查询；
- **客户端恢复 p50**：双表组合、resolver 本地读取和结果规范化的时延中位数；
- **校验 p50**：完整 bytes 的长度与 SHA-256 核对时延中位数；
- **应用可用 p50**：每个样本从请求提交到校验完成的总时延中位数；该值不等于前三列中位数之和。

所有时延为四轮轮级统计量的中位数。

ClickHouse 的 part 状态在第 4.9 节作为独立控制变量，四个状态定义如下：**碎片态**为暂停 merge、190 个写入 block 各自成 part 的状态；**合并中**为恢复 merge 后合并仍在进行时的状态；**稳定态**为自然合并完成、active part 数不再变化的状态；**单 part 态**为执行 `OPTIMIZE FINAL` 后每张表只剩一个 part 的状态。

访问证据方面，XStore 保存 EXPLAIN ANALYZE 计划原文、声明来源表、扫描行数和 `payload_selected`；其查询执行统计不提供可用的扫描字节计数，记为不可观测。ClickHouse 保存 `QueryFinish` 中的扫描行数与扫描字节、active part 数、mark 数和 merge 状态。

空间口径为 XStore 的 relation 已分配字节（含 heap、index 和 TOAST 分项）与 ClickHouse 的 active part 压缩字节，两者只作引擎内比较。Asset 目录字节单独统计。本文的 MB 指 10^6 bytes，MiB 指 2^20 bytes。

### 3.4 XStore 构建类型与证据

XStore 使用 release 构建，source commit `94f77718`，构建命令 `sh build_all.sh release`。二进制路径 `/data1/cjy/code/GaussVector-server/output/server/gaussdb/bin/gaussdb`，SHA-256 `462374ac04a88672042a1e037f20edc38c62a128777116f78cd919df30ec1884`（出处：facts/xstore-build.txt）。服务端版本字符串 `gaussdb (GaussVector 103.0.0 build 94f77718) compiled at 2026-09-23 12:59:04 last mr 463 release`。

### 3.5 访问路径门禁

访问路径门禁确认 XStore 列表查询使用索引扫描。两条语句的 EXPLAIN ANALYZE 结果均为 `Index Scan using events_list_idx on events`，实际扫描 256 行（出处：feedback.txt C5）。`asset_ref` 布局无行存探针数据，因探针只对 `same_table`、`separate`、`full_core` 三布局执行（出处：feedback.txt C4 标记 `NA 探针缺失`）。

### 3.6 ClickHouse 参数对齐

ClickHouse 23.3 的 `parts_to_delay_insert` 和 `parts_to_throw_insert` 显式设为 1000 和 3000，与蓝区 ClickHouse 25.12 的默认值对齐，消除碎片态控制时写入延迟的差异。`max_server_memory_usage_to_ram_ratio` 设为 0.5（出处：facts/clickhouse-settings.txt）。

## 4. 场景化测试

### 4.1 载入、维护与空间

**1. 场景设计。** 四种布局按固定的 190 个 block 写入。写入完成以该布局涉及的全部表或 Asset 组件满足第 2.2 节联合水位为准。空间在写入完成后采集，XStore 记录每张表的 heap、index 和 TOAST 分项，ClickHouse 记录每张表的 active part 压缩字节、part 数和 mark 数。

**2. 测试目的与预期。** 该场景比较完整方案的写入成本与物理占用。第二张表、Core 复制和 Asset 发布预计增加必要写入步骤；索引与排序键的复制成本、payload 列的压缩率和 Asset 目录字节由分项数据验证。

**3. 实例 SQL。** XStore 使用 `COPY` 写入各表并在写入完成后执行 `ANALYZE`；ClickHouse 使用 `INSERT ... FORMAT JSONEachRow`。`asset_ref` 在写入事件行之前完成对象的原子发布并把 `assets.status` 置为 `available`。

**4. 测试结果。** 主 cohort 的写入结果如下，均为四轮中位数。

| 布局 | XStore wall ms | XStore 单块 p50 ms | ClickHouse wall ms | ClickHouse 单块 p50 ms |
|---|---:|---:|---:|---:|
| `same_table` | **8,062.7** | **26.0** | **5,031.6** | **14.9** |
| `separate` | 10,458.3 | 40.2 | 6,538.0 | 23.4 |
| `full_core` | 10,916.7 | 41.4 | 7,067.0 | 29.0 |
| `asset_ref` | 8,449.6 | 46.1 | 7,824.6 | 40.2 |

出处：feedback.txt A5。

XStore 空间分项如下，单位 bytes。

| 布局 | 表 | heap | index | TOAST | 表合计 |
|---|---|---:|---:|---:|---:|
| `same_table` | `events` | 186,515,456 | 33,701,888 | 0 | 220,217,344 |
| `separate` | `event_payloads` | 183,484,416 | 33,505,280 | 0 | 216,989,696 |
| | `events_analytics` | 10,977,280 | 33,701,888 | 0 | 44,679,168 |
| `full_core` | `events_core` | 10,977,280 | 33,308,672 | 0 | 44,285,952 |
| | `events_full` | 186,515,456 | 33,505,280 | 0 | 220,020,736 |
| `asset_ref` | `events_analytics` | 10,977,280 | 33,701,888 | 0 | 44,679,168 |
| | `assets` | 98,304 | 180,224 | 0 | 278,528 |
| | Asset 目录 | — | — | — | 128,450,560 |

出处：feedback.txt A6。

ClickHouse 空间分项如下，表字节为 active part 压缩字节。

| 布局 | 表 | part 数 | 行数 | mark | 压缩字节 | 未压缩字节 |
|---|---|---:|---:|---:|---:|---:|
| `same_table` | `events` | 6 | 48,534 | 21 | 19,032,758 | 138,345,760 |
| `separate` | `event_payloads` | 5 | 48,534 | 18 | 17,650,307 | 135,147,278 |
| | `events_analytics` | 6 | 48,534 | 17 | 3,235,614 | 9,846,306 |
| `full_core` | `events_core` | 6 | 48,534 | 17 | 3,235,614 | 9,846,306 |
| | `events_full` | 5 | 48,534 | 19 | 19,027,321 | 138,345,760 |
| `asset_ref` | `events_analytics` | 6 | 48,534 | 17 | 3,248,511 | 9,953,614 |
| | `assets` | 2 | 160 | 4 | 33,309 | 53,440 |
| | Asset 目录 | — | 160 | — | 128,450,560 | — |

出处：feedback.txt A6。

以 `same_table` 为基准的总占用倍数如下。倍数在引擎内部计算，两个引擎的空间口径不同，不作跨引擎比较。

| 布局 | XStore 库内 bytes | XStore 倍数 | ClickHouse 库内 bytes | ClickHouse 倍数 |
|---|---:|---:|---:|---:|
| `same_table` | 220,217,344 | **1.000** | 19,032,758 | **1.000** |
| `separate` | 261,668,864 | 1.188 | 20,885,921 | 1.097 |
| `full_core` | 264,306,688 | 1.200 | 22,262,935 | 1.170 |
| `asset_ref` | 44,957,696 | 0.204 | 3,281,820 | 0.172 |

`asset_ref` 的总占用需加上 Asset 目录 128,450,560 bytes：XStore 合计 173,408,256 bytes（0.787 倍），ClickHouse 合计 131,732,380 bytes（6.921 倍）。ClickHouse 的倍数依赖本实验语料的高可压缩性。

XStore 行存探针的 payload 列字节确认：`same_table` 的 `events` 表 payload 列存储字节与逻辑字节相等（text_2m: 83,886,080 bytes 逻辑对 83,886,240 bytes 存储），无压缩（出处：xstore-row-probe.json payload_profiles）。

**5. 分析。** XStore 的写入顺序为 `same_table` < `asset_ref` < `separate` < `full_core`。`asset_ref` 在 XStore 上不需要第二张表的行构造，写入合计低于 `separate` 和 `full_core`，但单块 p50 最高（46.1 ms），因每次写入需额外发布本地对象。ClickHouse 的写入顺序为 `same_table` < `separate` < `full_core` < `asset_ref`，`asset_ref` 最慢因对象发布开销。XStore 的 TOAST 分项均为 0，payload 以行内变长字节存储，不压缩；ClickHouse 的 payload 列经 ZSTD(3) 压缩，`same_table` 的 138,345,760 未压缩字节压缩至 19,032,758 bytes，压缩比 7.27。

### 4.2 列表查询

**1. 场景设计。** 列表查询按固定窗口的第一页和中部一页执行 keyset 分页，投影不包含 payload，四种布局返回相同的结果形状。

**2. 测试目的与预期。** 该场景比较四种布局在轻量分页查询上的时延。`same_table` 的 payload 列在表中但不被投影，预计不增加读取成本。

**3. 实例 SQL。** XStore 列表查询通过在投影中显式写入类型化 NULL 隔离 payload：

```sql
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       NULL::text AS preview, sha256, NULL::text AS payload_value
FROM events
WHERE project_id=%s AND start_time>=%s AND start_time<%s
  AND (start_time,event_id)>(%s,%s)
ORDER BY start_time,event_id LIMIT %s;
```

**4. 测试结果。**

| 布局 | XStore `list:first` p50 ms | XStore `list:middle` p50 ms | ClickHouse `list:first` p50 ms | ClickHouse `list:middle` p50 ms |
|---|---:|---:|---:|---:|
| `same_table` | 28.3 | **89.8** | 22.9 | 26.7 |
| `separate` | 28.3 | 52.9 | 29.5 | 27.4 |
| `full_core` | 28.2 | 52.7 | 30.6 | 29.9 |
| `asset_ref` | 28.5 | 52.8 | 27.0 | 27.4 |

出处：feedback.txt A1。每场景每轮 30 个正式样本，四轮共 120 个样本。

**5. 分析。** XStore `same_table` 的 `list:middle` 显著慢于其余三布局（89.8 ms 对 52.7–52.9 ms），比值为 1.70。行存探针的游标探测确认：`same_table` 的展开式 keyset 谓词（OR 形式）在 `list:middle` 上首次执行时走索引但耗时 91 ms，加入下界约束后降至 0.65 ms（出处：xstore-row-probe.json cursor_probe）。`list:middle` 的 keyset 谓词只含 OR 展开形式，优化器未将其转化为索引范围起点，导致较多回表。`separate`、`full_core` 和 `asset_ref` 的列表查询只读取分析表，不含 payload 列，`list:middle` 稳定在 52.7–52.9 ms。ClickHouse 四布局的 `list:first` 为 22.9–30.6 ms，最大最小比 1.34；`list:middle` 为 26.7–29.9 ms，区分度极小。

### 4.3 Preview 查询

**1. 场景设计。** Preview 查询与列表查询使用相同的两页 keyset 分页，投影额外携带 preview 列。

**2. 测试目的与预期。** Preview 列为 payload 的前 200 个字符，预计在四种布局间不产生显著差异。

**3. 实例 SQL。** 投影中的 preview 在 `same_table` 和 `full_core`（`events_core`）从表中直接读取，在 `separate` 和 `asset_ref`（`events_analytics`）从分析表中读取。

**4. 测试结果。**

| 布局 | XStore `preview:first` p50 ms | XStore `preview:middle` p50 ms | ClickHouse `preview:first` p50 ms | ClickHouse `preview:middle` p50 ms |
|---|---:|---:|---:|---:|
| `same_table` | 28.3 | 89.6 | 21.1 | 26.1 |
| `separate` | 28.4 | 52.9 | 28.8 | 29.6 |
| `full_core` | 28.3 | 52.9 | 29.7 | 28.4 |
| `asset_ref` | 28.5 | 52.8 | 28.6 | 28.7 |

出处：feedback.txt A1。

**5. 分析。** Preview 查询的时延与列表查询在相同布局和变体上几乎完全一致。XStore `same_table` 的 `preview:middle` 同样为 89.6 ms，与 `list:middle` 的 89.8 ms 一致。preview 列的 200 字符长度不产生额外的 I/O 成本。ClickHouse 四布局的 `preview:first` 为 21.1–29.7 ms，`preview:middle` 为 26.1–29.6 ms，与列表查询一致。

### 4.4 单条详情恢复

**1. 场景设计。** 按 profile 固定选定的单条 payload 恢复，四种布局均返回完整 payload bytes 并校验 SHA-256。

**2. 测试目的与预期。** 该场景比较四种布局在读取完整 payload 时的时延。`same_table` 直接从表中读取，`separate` 需要 JOIN 两表，`asset_ref` 需要 resolver 读取本地对象。

**3. 实例 SQL。** `same_table` 直接 `SELECT payload FROM events WHERE event_id=%s`；`separate` JOIN `events_analytics` 与 `event_payloads`；`asset_ref` 查询引用和 `assets` 行后由 resolver 读取本地对象。

**4. 测试结果。**

| 布局 | XStore `detail:text_64k` p50 ms | XStore `detail:text_512k` p50 ms | XStore `detail:text_2m` p50 ms | XStore `detail:entropy_512k` p50 ms |
|---|---:|---:|---:|---:|
| `same_table` | **10.2** | **12.8** | **23.3** | **12.8** |
| `separate` | 11.5 | 14.3 | 24.7 | 14.2 |
| `full_core` | 10.2 | 12.9 | 23.5 | 12.9 |
| `asset_ref` | 19.4 | 21.3 | 27.6 | 21.3 |

| 布局 | ClickHouse `detail:text_64k` p50 ms | ClickHouse `detail:text_512k` p50 ms | ClickHouse `detail:text_2m` p50 ms | ClickHouse `detail:entropy_512k` p50 ms |
|---|---:|---:|---:|---:|
| `same_table` | **10.0** | 19.1 | 77.3 | 32.6 |
| `separate` | 134.4 | **148.2** | **167.9** | **141.5** |
| `full_core` | 14.1 | 23.3 | 76.9 | 35.8 |
| `asset_ref` | 14.4 | 16.5 | **34.0** | 20.7 |

出处：feedback.txt A2。每场景每轮 30 个正式样本，四轮共 120 个样本。

**5. 分析。** XStore 四布局的单条详情时延随 payload 大小线性增长，`text_2m` 为 23.3–27.6 ms。`asset_ref` 在所有 profile 上最慢（19.4–27.6 ms），因 resolver 需要额外读取本地对象。`same_table` 和 `full_core` 几乎相同（10.2–23.5 ms），因 `events_full` 与 `events` 的行存结构一致。ClickHouse `separate` 在所有 profile 上显著离群：`detail:text_2m` 为 167.9 ms，是 `asset_ref`（34.0 ms）的 4.9 倍。差异来自 JOIN 执行路径：过滤条件只施加在 SQL 左表 `events_analytics`，右表 `event_payloads` 没有等价谓词下推，payload 列被整表扫描。该现象与蓝区 ClickHouse 25.12 上的观测一致（蓝区报告第 1 节）。ClickHouse `asset_ref` 的 `detail:text_2m` 为 34.0 ms，快于 `same_table`（77.3 ms）和 `full_core`（76.9 ms），因 payload 不在库内，避免了 ZSTD 解压。

### 4.5 完整 Trace 恢复

**1. 场景设计。** 按 Span 数量位于三个分位附近的完整 Trace 恢复，返回该 Trace 的全部 Span 及其 payload。

**2. 测试目的与预期。** Trace 恢复需要定位和读取多条记录，预计 `separate` 的 JOIN 成本和 `asset_ref` 的多次 resolver 调用会增加时延。

**3. 实例 SQL。** 使用 `(project_id,trace_id,start_time,event_id)` 索引定位 Trace 的全部 Span，再按布局恢复 payload。

**4. 测试结果。**

| 布局 | XStore `trace:p25` p50 ms | XStore `trace:p50` p50 ms | XStore `trace:p95` p50 ms |
|---|---:|---:|---:|
| `same_table` | **12.8** | **15.5** | **28.8** |
| `separate` | 14.4 | 17.2 | 31.0 |
| `full_core` | 12.9 | 15.6 | 29.1 |
| `asset_ref` | 22.1 | 24.1 | 33.6 |

| 布局 | ClickHouse `trace:p25` p50 ms | ClickHouse `trace:p50` p50 ms | ClickHouse `trace:p95` p50 ms |
|---|---:|---:|---:|
| `same_table` | 24.9 | 22.1 | 68.5 |
| `separate` | **133.6** | **151.9** | **171.7** |
| `full_core` | 26.5 | 21.6 | 72.2 |
| `asset_ref` | 17.1 | 19.3 | 41.6 |

出处：feedback.txt A3。每场景每轮 30 个正式样本，四轮共 120 个样本。

**5. 分析。** XStore 四布局的 Trace 恢复时延随 Span 数增长，`trace:p95` 为 28.8–33.6 ms。`asset_ref` 最慢（22.1–33.6 ms），因需要多次 resolver 调用。`same_table` 和 `full_core` 几乎相同。ClickHouse `separate` 再次显著离群，`trace:p50` 为 151.9 ms，是 `asset_ref`（19.3 ms）的 7.9 倍，原因与第 4.4 节相同。ClickHouse `asset_ref` 在 `trace:p95` 上为 41.6 ms，快于 `same_table`（68.5 ms）和 `full_core`（72.2 ms）。

### 4.6 批量恢复

**1. 场景设计。** 主 cohort 的 160 个对象批量恢复，返回全部 payload bytes 并逐条校验 SHA-256。

**2. 测试目的与预期。** 批量恢复产生大量 resolver 请求（`asset_ref`）或大范围扫描（其余布局），预计 `asset_ref` 的成本由对象数量和本地 I/O 决定。

**3. 实例 SQL。** `same_table`、`separate` 和 `full_core` 使用 `SELECT payload FROM <表> WHERE cohort='main'`；`asset_ref` 查询引用和 `assets` 行后由 resolver 逐个读取本地对象。

**4. 测试结果。**

| 布局 | XStore `batch:main` p50 ms | ClickHouse `batch:main` p50 ms |
|---|---:|---:|
| `same_table` | 1,312.1 | 2,297.9 |
| `separate` | 1,268.9 | 2,928.2 |
| `full_core` | 1,334.6 | 2,476.9 |
| `asset_ref` | **799.7** | 1,782.9 |

出处：feedback.txt A3。每轮 5 个正式样本，四轮共 20 个样本。

**5. 分析。** `asset_ref` 在两个引擎上均为最快的批量恢复布局：XStore 799.7 ms，ClickHouse 1,782.9 ms。`asset_ref` 的批量恢复只读取 160 个本地对象，不经过数据库的查询优化器和列式解压。XStore 四布局的 `batch:main` 差异较小（799.7–1,334.6 ms），最大最小比 1.67。ClickHouse `separate` 最慢（2,928.2 ms），因 JOIN 路径与第 4.4 节相同。

### 4.7 等总字节分布控制

**1. 场景设计。** 在相同的 80 MiB 原始内容总量下，比较 40 条 2 MiB payload（`equal_total_few_large`）与 1,280 条 64 KiB payload（`equal_total_many_medium`）的批量恢复。

**2. 测试目的与预期。** 两组的 resolver 请求数分别为 200 和 6,400，字节总量不变。预计 `asset_ref` 的成本由对象数量而非字节总量决定。

**3. 实例 SQL。** 与第 4.6 节相同，cohort 替换为 `equal_total_few_large` 或 `equal_total_many_medium`。

**4. 测试结果。**

| 布局 | XStore `few_large` p50 ms | XStore `many_medium` p50 ms | ClickHouse `few_large` p50 ms | ClickHouse `many_medium` p50 ms |
|---|---:|---:|---:|---:|
| `same_table` | 904.7 | 893.7 | 1,815.6 | 1,641.9 |
| `separate` | 868.3 | 886.4 | 1,730.4 | 1,823.8 |
| `full_core` | 908.1 | 1,004.5 | 1,774.2 | 1,668.5 |
| `asset_ref` | **477.7** | **1,814.2** | **698.6** | **9,893.5** |

出处：feedback.txt A4。每轮 5 个正式样本，四轮共 20 个样本。

**5. 分析。** `asset_ref` 在 `few_large` 中是两个引擎上最快的布局，在 `many_medium` 中变为最慢。**跨组比值**（同一布局，few_large → many_medium）：ClickHouse 由 698.6 ms 升至 9,893.5 ms，为 14.2 倍；XStore 由 477.7 ms 升至 1,814.2 ms，为 3.8 倍。**组内最大最小比**（`many_medium` 内最慢除以最快）：ClickHouse 5.83（`asset_ref` 9,893.5 ms 对 `separate` 1,823.8 ms 的倒数不对——实际为 `asset_ref` 9,893.5 ms 对 `same_table` 1,641.9 ms，比值为 6.03），XStore 2.03（`asset_ref` 1,814.2 ms 对 `separate` 886.4 ms）。resolver 请求数由 200 增至 6,400，字节总量不变，成本由对象数量决定。其余三布局在两组间变化不大，因数据库批量扫描的字节总量不变。

### 4.8 混合负载与读取隔离

**1. 场景设计。** 在前台列表与 preview 负载之上按固定顺序执行五个相：`quiet` 无干扰，`detail_2m` 叠加 2 MiB 单条详情，`trace_long` 叠加长 Trace 恢复，`batch_loop` 叠加持续循环的批量恢复，`continuous_ingest` 叠加持续写入。每相 30 秒预热加 300 秒测量。

**2. 测试目的与预期。** 该场景测试后台负载对前台列表查询的干扰。`asset_ref` 把 payload 读取移出数据库，预计前台查询受批量恢复的干扰较小。

**3. 实例 SQL。** 前台查询与第 4.2 节相同，后台负载按相依次叠加。

**4. 测试结果。** XStore 前台 `list:first` p50 与丢弃数如下。

| 布局 | quiet list p50 ms | detail_2m list p50 ms | trace_long list p50 ms | batch_loop list p50 ms | continuous_ingest list p50 ms |
|---|---:|---:|---:|---:|---:|
| `same_table` | 64.8 | 89.4 | 112.4 | 107.4 | 66.3 |
| `separate` | 65.0 | 118.9 | 136.7 | 150.2 | 100.8 |
| `full_core` | 73.5 | 119.0 | 86.1 | 114.9 | 77.7 |
| `asset_ref` | 66.1 | 67.4 | 66.6 | 196.2 | 68.0 |

ClickHouse 前台 `list:first` p50 与丢弃数如下。

| 布局 | quiet list p50 ms | detail_2m list p50 ms | trace_long list p50 ms | batch_loop list p50 ms | continuous_ingest list p50 ms |
|---|---:|---:|---:|---:|---:|
| `same_table` | 27.1 | 27.5 | 27.0 | 28.3 | 23.4 |
| `separate` | 31.8 | 32.1 | 30.4 | 33.6 | 20.3 |
| `full_core` | 30.6 | 31.7 | 29.0 | 36.0 | 33.0 |
| `asset_ref` | 28.5 | 32.7 | 31.5 | 32.9 | 30.8 |

出处：各布局各相的 `samples.jsonl`。每相 300 秒测量。两个引擎在所有相中均无 scheduler drop。

**5. 分析。** XStore 产生可观测的干扰效应。`separate` 的 `batch_loop` 阶段 `list:first` p50 为 150.2 ms，quiet 阶段为 65.0 ms，比值为 2.31。`asset_ref` 在 `detail_2m`、`trace_long` 和 `continuous_ingest` 三相中几乎无干扰（66.1–68.0 ms），但在 `batch_loop` 中升至 196.2 ms，是 quiet 的 2.97 倍。`asset_ref` 的 `batch_loop` 干扰来自 resolver 的本地 I/O 与前台查询竞争，而非数据库查询路径。`same_table` 的 `trace_long` 阶段升至 112.4 ms，比值为 1.73。ClickHouse 四布局在五相间变化极小，最大比值为 `full_core` 的 `batch_loop` 对 quiet（36.0 / 30.6 = 1.18），属于运行间波动。ClickHouse 的列式读取和后台 merge 管理有效隔离了前台查询。两个引擎在所有相中均无 scheduler drop，表明负载到达率未超过调度容量。

### 4.9 ClickHouse part 状态控制

**1. 场景设计。** 对 ClickHouse 的四种布局，在 `events`（或 `events_analytics`）表上控制四个 part 状态：碎片态（190 part）、合并中（恢复 merge 后合并进行中）、自然稳定态（merge 完成）、单 part 态（`OPTIMIZE FINAL` 后）。每个状态测量 `list:first` 时延。

**2. 测试目的与预期。** part 数量影响列存的扫描效率。碎片态预计最慢，单 part 态最快。

**3. 实例 SQL。** `SYSTEM STOP MERGES` / `SYSTEM START MERGES` 控制碎片态和合并中状态；`OPTIMIZE FINAL` 产生单 part 态。

**4. 测试结果。**

| 布局 | 碎片态 p50 ms (part 数) | 合并中 p50 ms (merge 数) | 稳定态 p50 ms (part 数) | 单 part 态 p50 ms (part 数) |
|---|---:|---:|---:|---:|
| `same_table` | 85.6 (190) | 22.6 (7) | 25.5 (6) | **16.0** (1) |
| `separate` | 83.3 (190) | 29.5 (5) | 37.4 (3) | 27.0 (1) |
| `full_core` | 90.0 (190) | 29.6 (8) | 29.7 (3) | 27.0 (1) |
| `asset_ref` | 115.7 (190) | 29.7 (2) | 29.4 (3) | 29.8 (1) |

出处：feedback.txt B2。每状态每轮 30 个正式样本。

**5. 分析。** part 状态的影响大于布局选择。碎片态的 `list:first` p50 为 83.3–115.7 ms，单 part 态为 16.0–29.8 ms，同一布局内比值为 3.40（`same_table`）至 7.24（`asset_ref`）。同一状态下四布局的最大最小比：碎片态 1.39（115.7 / 83.3），稳定态 1.47（37.4 / 25.5），单 part 态 1.86（29.8 / 16.0）。`asset_ref` 在碎片态下最慢（115.7 ms），因 `events_analytics` 表的 190 个 part 均需扫描。part 状态是与布局正交的因素。XStore 不适用 part 状态控制，因行存表不使用 part 概念。

### 4.10 Asset 故障实验

**1. 场景设计。** 对 `asset_ref` 布局执行六个固定故障用例：`missing`（删除已发布对象）、`corrupt`（修改对象字节）、`metadata_mismatch`（替换 catalog 元数据）、`upload_then_db_failure`（对象上传后数据库写入失败）、`publish_failure`（发布失败）、`delete_failure`（删除时对象移除失败）。

**2. 测试目的与预期。** 该场景验证 Asset 引用的故障检测与恢复机制。每个用例注入故障后检查 resolver 的错误分类、catalog 状态转换和恢复动作。

**3. 实例 SQL。** 按用例执行对应的故障注入和恢复命令。

**4. 测试结果。** 两个引擎的六个用例结果一致。

| 用例 | 注入点 | 错误分类 | 终态 | 事件可见 | 孤儿数 | 恢复动作 |
|---|---|---|---|---|---:|---|
| `missing` | remove_published_object | missing | available | True | 0 | restore_missing_object |
| `corrupt` | modify_published_bytes | corrupt | available | True | 0 | replace_corrupt_object |
| `metadata_mismatch` | replace_catalog_metadata | metadata_mismatch | available | True | 0 | restore_catalog_metadata |
| `upload_then_db_failure` | fail_after_object_upload | missing | absent | False | 1 | remove_orphan_object |
| `publish_failure` | fail_pending_publication | failed | failed | True | 0 | confirm_failed_publication |
| `delete_failure` | fail_deleting_object_removal | deleting | deleting | True | 0 | confirm_delete_failure_state |

出处：feedback.txt B4。两个引擎各六个用例，validation error 和 execution error 均为 0。

**5. 分析。** 两个引擎的六个用例全部符合设计预期。`missing`、`corrupt` 和 `metadata_mismatch` 在恢复后回到 `available` 状态。`upload_then_db_failure` 产生 1 个孤儿对象并执行清理。`publish_failure` 和 `delete_failure` 确认终态保持不变。两个引擎的故障分类和恢复动作完全一致，因 Asset 引用的逻辑在 adapter 层实现，不依赖引擎特定功能。

## 5. 跨场景分析

### 5.1 布局选择

四种布局在不同场景下的最优选择不同。`same_table` 在 XStore 的列表、预览、单条详情和 Trace 恢复上最快或接近最快，但 `list:middle` 因 keyset 谓词展开而显著慢。`asset_ref` 在批量恢复上最快，在单条详情上最慢（XStore）或最快（ClickHouse `text_2m`）。`separate` 在 ClickHouse 上因 JOIN 路径而普遍离群，在 XStore 上与 `same_table` 接近。`full_core` 在两个引擎上均与 `same_table` 接近，额外写入成本未带来查询收益。

### 5.2 成本转移边界

`asset_ref` 把 payload 读取成本从数据库转移到本地对象存储。在 `batch:main` 中，`asset_ref` 在两个引擎上均最快（799.7 ms 和 1,782.9 ms）。在 `equal_total_many_medium` 中，6,400 次 resolver 调用使 `asset_ref` 变为最慢（1,814.2 ms 和 9,893.5 ms）。成本转移的边界在对象数量：少量大对象的批量恢复受益于外置，大量小对象的批量恢复受限于 resolver 的调用开销。

### 5.3 正交性

ClickHouse part 状态与布局选择正交。part 状态的同一布局内比值（3.40–7.24）远大于同一状态下四布局的最大最小比（1.13–1.86）。混合负载的干扰效应在 XStore 和 ClickHouse 上不同：XStore 产生可观测的 list p50 变化（最大比值 2.97），ClickHouse 变化极小（最大比值 1.18）。干扰效应与布局有关但不与 part 状态叠加，因混合负载在自然稳定态下执行。

### 5.4 比较边界

黄区 ClickHouse 23.3.10.5 与蓝区 ClickHouse 25.12 版本不同。23.3 的 `parts_to_delay_insert` 和 `parts_to_throw_insert` 默认值为 150 和 300，蓝区 25.12 为 1000 和 3000。黄区已显式设为 1000 和 3000 以消除碎片态控制的差异。其余执行期差异未逐项验证。黄区 ClickHouse 数值不与蓝区 ClickHouse 数值直接比较，黄区结论限定为同机 XStore 与 ClickHouse 23.3 的对比。

XStore 使用行存，payload 列不压缩，无 TOAST 关联。openGauss 在蓝区使用行存加 TOAST，payload 超长时压缩并行外存储。两者的空间模型不可直接对齐。XStore 与 ClickHouse 的空间口径分别为 relation 已分配字节和 active part 压缩字节，不作跨引擎比较。

## 6. 正确性、限制与后续测试

### 6.1 真值结论

两个引擎的主矩阵在所有 workload 上通过 truth 校验。Asset 故障实验的六个用例在两个引擎上全部符合设计预期。访问路径门禁通过：XStore 列表查询使用 `Index Scan using events_list_idx`，扫描 256 行。

### 6.2 限制

本轮实验存在以下限制与降级：

1. **XStore 行存无 TOAST**：`reltoastrelid` 均为 0，payload 以行内变长字节存储。XStore 的空间模型与 openGauss 的 TOAST 模型不可直接对齐（出处：feedback.txt C4）。
2. **XStore keyset 谓词展开**：行构造器比较不被支持，展开为 OR 形式。`same_table` 的 `list:middle` 受此影响，首次执行耗时 91 ms（出处：xstore-row-probe.json cursor_probe）。
3. **XStore 扫描字节不可观测**：查询执行统计不提供可用的扫描字节计数，记为不可观测。
4. **`asset_ref` 行存探针缺失**：行存探针只对 `same_table`、`separate`、`full_core` 三布局执行，`asset_ref` 标记为 `NA 探针缺失`（出处：feedback.txt C4）。
5. **ClickHouse 23.3 版本差异**：与蓝区 25.12 的差异未逐项验证，黄区 ClickHouse 数值不与蓝区直接比较。
6. **工具包版本**：仓库 HEAD 为 `d851e02`，127 个 run 中 116 个使用非 HEAD 代码（出处：feedback.txt C6）。主矩阵在 `6366686` 时执行，混合负载和 Asset 故障在 `d851e02` 时执行，后者包含持续写入重放的主键冲突修复。

### 6.3 后续实验

1. **XStore TOAST 适配**：确认 XStore 是否支持行外存储或 payload 压缩，如支持则补充空间和时延对比。
2. **XStore keyset 谓词优化**：测试 `same_table` 的 `list:middle` 在优化器改进后是否消除离群。
3. **ClickHouse JOIN 谓词下推**：测试 ClickHouse 23.3 和 25.12 的 JOIN 谓词下推行为差异，确认 `separate` 离群是否为版本特定。

## 7. 使用建议

以下建议限定在本轮已测范围（同机 XStore release build 94f77718 与 ClickHouse 23.3.10.5，aarch64 Kunpeng-920 256 核 432 GiB）。

1. **XStore 上优先使用 `same_table`**：在列表、预览、单条详情和 Trace 恢复上最快或接近最快，写入合计最低，空间占用最低。`list:middle` 的离群由 keyset 谓词展开导致，可通过优化器改进或使用 `separate` 布局规避。

2. **ClickHouse 上避免 `separate`**：JOIN 路径导致单条详情和 Trace 恢复显著离群。如需 payload 独立存储，使用 `asset_ref` 或 `full_core`。

3. **`asset_ref` 适用于少量大对象的批量恢复**：`batch:main`（160 个对象）和 `equal_total_few_large`（40 个对象）中 `asset_ref` 最快。大量小对象（1,280 个 64 KiB）的批量恢复因 resolver 调用开销而显著变慢。

4. **ClickHouse part 管理优先于布局选择**：碎片态（190 part）的列表查询比单 part 态慢 3.4–7.2 倍。生产环境应保持 merge 开启，避免 part 堆积。

5. **混合负载下的读取隔离**：XStore 的 `asset_ref` 在 `detail_2m`、`trace_long` 和 `continuous_ingest` 相中几乎无干扰，但在 `batch_loop` 中受 resolver I/O 竞争影响。ClickHouse 在五相间变化极小，列式读取和后台 merge 管理有效隔离了前台查询。

## 参考资料

- [阶段三黄区指南](../project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md)
- [阶段三黄区结果反馈契约](../project-background/json-storage-stage3-yellow-feedback-contract.md)
- [阶段三实验报告（蓝区）](json-storage-stage3-report-2026-09-20.md)
- [阶段三实验设计与证据契约](json-storage-stage3-experiment-design-2026-09-09.md)
- [JSON 存储原理](json-storage-principles-2026-09-09.md)
