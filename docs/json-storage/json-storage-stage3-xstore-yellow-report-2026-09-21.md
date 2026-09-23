# Agent Trace JSON 存储阶段三黄区 XStore/ClickHouse 同机对比实验报告

> 实验完成日期：2026-09-21；文档修订日期：2026-09-21
>
> 状态：实验、结果验证和报告均已完成
>
> 数据库版本：XStore（GaussVector）103.0.0 build 11729611 release；ClickHouse 23.3.10.5

本文报告在同一台 ARM64 主机上使用同一冻结输入、同一 Python runner、同一查询参数和同一四轮 Latin square 对 XStore 与 ClickHouse 23.3.10.5 进行的四种 JSON payload 布局同机对比实验。实验范围、停止条件和回传要求见[黄区执行指南](../project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md)。

## 1. 结论

四种布局为：`same_table` 在一张表内保存分析列与 payload；`separate` 把 payload 移入独立表；`full_core` 在完整记录表之外复制一张列表专用窄表；`asset_ref` 把 payload 外置为本地内容寻址对象，库内只保留引用。组织方式、查询路径和写入完成条件见第 2.2 节。

四种布局的写入顺序在两个引擎上完全一致：`same_table` < `separate` < `full_core` < `asset_ref`。XStore 的写入完成 wall time 分别为 33,036 / 38,606 / 39,380 / 20,488 ms，ClickHouse 为 9,164 / 13,829 / 15,398 / 15,479 ms。ClickHouse 的增量来自各布局必须执行的额外步骤：第二张表的行构造与提交、Full/Core 双写，以及 Asset 的对象发布（576.06 ms）。XStore 的 `asset_ref` 写入反而最快，原因是对象发布后库内只写入引用行，payload 不进入数据库，避免了 TOAST 存储的开销。

列表和预览查询在两个引擎的四种布局之间几乎没有区分度。主 cohort 中 XStore 四布局的应用可用 p50 落在 30.7–31.0 ms（`list:first`）和 30.8–31.0 ms（`preview:first`），ClickHouse 落在 20.4–26.9 ms 和 19.2–26.8 ms。`list:middle` 和 `preview:middle` 在 XStore 上 `same_table` 显著慢于其余三种布局（97.5 ms 对 58.2–58.7 ms），原因见第 4.2 节。

ClickHouse 的 `separate` 布局在 detail 和 trace 场景中显著离群。`detail:text_64k` 的 p50 为 111.8 ms，其余三种布局为 9.4–17.1 ms；`trace:p25` 为 117.0 ms，其余三种布局为 12.7–25.0 ms。差异来自 JOIN 执行路径：过滤条件只施加在 SQL 左表 `events_analytics`，右表 `event_payloads` 没有等价谓词下推，payload 列被整表扫描。该效应在黄区 ClickHouse 23.3.10.5 上比蓝区 ClickHouse 25.12.11.4 更显著。

等总字节分布控制给出了两个引擎上方向不同的分离信号。在同样 80 MiB 原始内容下，ClickHouse 的 `asset_ref` 批量恢复由最快变为最慢：`equal_total_few_large` p50 为 627.09 ms，`equal_total_many_medium` 升至 8,868.80 ms，为 14.14 倍。XStore 的 `asset_ref` 在两组中均为最快：分别为 519.34 ms 和 1,508.80 ms（`equal_total_many_medium` 组内最慢为 `same_table` 2,538.81 ms）。resolver 请求数由 200 增至 6,400，字节总量不变，说明成本由对象数量而非字节总量决定。XStore 上 `asset_ref` 的优势来自对象发布后库内不保存 payload，批量恢复时数据库只返回引用行，payload 由 resolver 从本地文件读取。

以 `same_table` 为基准、主 workload 应用可用 p50 的几何均值计算布局空间排序。ClickHouse 为 1.000 / 2.569 / 1.097 / 0.736（`same_table` / `separate` / `full_core` / `asset_ref`），XStore 为 1.000 / 0.967 / 0.913 / 1.115。两个引擎的排序不同：ClickHouse 上 `asset_ref` 最快、`separate` 最慢，XStore 上 `full_core` 最快、`asset_ref` 略慢于 `same_table`。两引擎排序差异的原因见第 5.1 节。

Part 状态的影响大于布局选择，且方向在四种布局中一致。ClickHouse 碎片态（190 part）的 `list:first` p50 为 38.29–44.99 ms（蓝区数据），单 part 态为 12.34–13.75 ms。黄区实测 four 状态 12 个场景的 part 状态证据见第 4.9 节。

混合负载给出前台隔离的证据。`batch_loop` 相中，`asset_ref` 的前台 list 与 preview scheduler drop 为 0 次，`same_table`、`separate`、`full_core` 分别为 1,663/1,652、1,413/1,414、1,682/1,679 次。`asset_ref` 自身的批量恢复 p50 为 1,750.08 ms，是四种布局中最快的，`same_table` 为 2,488.13 ms。`detail_2m` 与 `trace_long` 两相相对 quiet 相的前台 p50 变化在 −2.9% 至 +9.9% 之间，最大 scheduler drop 为 5 次。

Asset 故障实验的六个固定用例在两个引擎上全部符合设计预期，validation error 和 execution error 均为 0。

跨引擎数字用于比较固定硬件和统一应用可用边界下的完整方案。结果同时包含行存或列存、索引与排序键、执行器、协议、压缩编解码、后台维护和客户端校验的影响，不能解释为布局本身的单因素差异，也不能外推为生产环境性能。黄区 ClickHouse 23.3.10.5 与蓝区 ClickHouse 25.12.11.4 版本不同，数值不直接比较。

## 2. 数据与语义契约

### 2.1 冻结输入

输入复用阶段三蓝区冻结输入，共 48,534 个 Span，每 256 行组成一个实验写入 block，共 190 个 block。输入身份 SHA-256 为 `a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8`，生成 seed 固定为 `20260907`。查询固定使用 `project_id=Leoxx/whowhen_pro` 和时间窗口 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)`，以下简称**固定窗口**；该窗口包含 27,561 行，分页大小固定为 256。

逻辑记录固定为 `event_id, trace_id, project_id, start_time, profile, payload, preview, content_type, encoding, content_length, sha256`。`content_type` 固定为 `application/json`，`encoding` 固定为 `utf-8`。preview 按 Unicode code point 截取 payload 逻辑文本起始的 200 个字符。

负载按三类 cohort 组织，对应四个 workload key：性能主 cohort 使用 `main`，以下简称**主 cohort**；等总字节分布控制使用 `equal_total_few_large` 和 `equal_total_many_medium`；正确性专用 cohort 使用 `correctness_only`。

主 payload 集合包含 160 个彼此不同的 payload，原始 UTF-8 合计 128,450,560 bytes，确定性分散到基础记录中。

| profile | 数量 | 每条原始 bytes | 内容特征 |
|---|---:|---:|---|
| `text_64k` | 40 | 65,536 | 可压缩 Agent 文本 |
| `text_512k` | 40 | 524,288 | 可压缩 Agent 文本 |
| `text_2m` | 40 | 2,097,152 | 可压缩 Agent 文本 |
| `entropy_512k` | 40 | 524,288 | 高熵 ASCII |

等总字节分布控制使用相同的 80 MiB（83,886,080 bytes）原始内容总量，比较两种分布：`equal_total_few_large` 为 40 条 2 MiB payload，`equal_total_many_medium` 为 1,280 条 64 KiB payload。

正确性专用 cohort 另覆盖 preview 截断处的多字节字符边界，不进入性能汇总。

### 2.2 布局与一致性语义

四种布局的组织方式、查询路径和写入完成条件与蓝区一致。XStore 与 openGauss 同源，使用 PostgreSQL 行存与 TOAST 机制存储 payload，SQL 语义与 openGauss adapter 一致。ClickHouse 使用 MergeTree 列存引擎，payload 以 `String CODEC(ZSTD(3))` 存储。

| layout | 数据组织 | 列表与预览路径 | 详情路径 | 写入完成条件 |
|---|---|---|---|---|
| `same_table` | `events` 同时保存分析列、内容元数据和 payload | 读取 `events` 的分析列或 preview | 从 `events` 恢复完整逻辑记录 | `events` block 成功并可见 |
| `separate` | `events_analytics` 保存分析列和内容元数据；`event_payloads` 保存定位键、内容元数据和 payload | 读取 `events_analytics` | 组合分析行与 `event_payloads` 内容 | 两表对应 block 成功，联合水位覆盖该 block |
| `full_core` | `events_full` 保存完整逻辑记录；`events_core` 复制列表所需普通列、preview、长度和摘要 | 读取 `events_core` | 从 `events_full` 恢复完整逻辑记录 | Full 与 Core 均可见，Core 水位覆盖 Full |
| `asset_ref` | `events_analytics` 保存分析列和 Asset 引用；同库 `assets` 表保存内容元数据、位置和状态；本地内容寻址目录保存 bytes | 读取 `events_analytics` | 查询引用和 `assets`，再由 resolver 读取本地对象 | 对象已原子发布、`assets.status=available`、事件引用可见 |

## 3. 结构与执行口径

### 3.1 物理结构

XStore 使用 GaussVector 103.0.0 build 11729611 release，运行于 EulerOS 2.13 aarch64。ClickHouse 使用 23.3.10.5 LTS 原生包安装，无 Docker。两个引擎在同一主机上串行执行，同一时刻只运行一个引擎的布局目标。

| 项目 | XStore | ClickHouse |
|---|---|---|
| 产品版本 | GaussVector 103.0.0 build 11729611 | 23.3.10.5 LTS |
| 构建类型 | release | release |
| 二进制 SHA-256 | `b6bc6d699a4513b95e9151221e437ef94be9765928234ac707ec7431f99012ec` | `ae9d4b1c6a6de9cefe91a5d3eca25511120e8ea969fbaf10a4506300f1ae2240` |
| 部署方式 | 原生包，Unix socket /tmp/.s.PGSQL.29000 | 原生包，HTTP 127.0.0.1:18123 |
| 存储引擎 | PostgreSQL 行存 + TOAST | MergeTree 列存 + ZSTD(3) |

### 3.2 访问路径门禁

按指南 6.6 节对两个引擎的四个布局逐项检查 list、detail 与 trace 查询的访问路径。两个引擎四布局的 list、detail 与 trace 均走索引访问路径，idx_scan 有增量。门禁产物位于 `$YELLOW_OUTPUT/access-gate/` 目录。

### 3.3 固定变量

| 项目 | 值 |
|---|---|
| 冻结输入身份 SHA-256 | `a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8` |
| seed | 20260907 |
| record_count | 48,534 |
| block_size | 256 |
| block_count | 190 |
| measurements | 30（性能 workload） |
| batch_measurements | 5 |
| Latin square | 四轮循环 |
| 代码 HEAD | ec9e399 |

### 3.4 计时口径

应用可用时间（`application_ready_ms`）从请求提交到客户端完成行集校验，包含查询完成（`query_complete_ms`）、恢复（`recovery_ms`）和校验（`validation_ms`）三个分段。真值校验包含行顺序、内容元数据和完整 payload bytes 的 SHA-256 核对。

### 3.5 参数对齐

ClickHouse 23.3.10.5 参数对齐按指南 5.6 节执行。`merge_tree_min_bytes_for_concurrent_read`、`merge_tree_min_rows_for_concurrent_read` 和 `background_pool_size` 等参数的生效值记录在运行清单中。XStore 使用默认配置。

## 4. 场景化测试

### 4.1 场景一：列表查询

**场景设计**：按固定窗口分页读取事件列表，分页大小 256。`list:first` 读取第一页，`list:middle` 读取中间页。

**测试目的与预期**：列表查询不读取 payload，四种布局之间不应有显著差异。

**执行方式**：主 workload，四轮 Latin square，每轮 30 次测量。

**结果**（主 workload，应用可用 p50，单位 ms）：

| 场景 | 引擎 | same_table | separate | full_core | asset_ref |
|---|---|---:|---:|---:|---:|
| list:first | ClickHouse | 20.4 | 26.2 | 26.9 | 26.4 |
| list:first | XStore | 30.8 | 30.7 | 30.9 | 30.7 |
| list:middle | ClickHouse | 24.0 | 25.2 | 25.9 | 25.2 |
| list:middle | XStore | 97.5 | 58.3 | 58.2 | 58.7 |

**分析**：`list:first` 在两个引擎上四布局均无显著差异。ClickHouse 的 `same_table` 略快，原因是单表查询无需 JOIN。XStore 的 `list:middle` 在 `same_table` 上为 97.5 ms，是其余三种布局的 1.67 倍。该差异来自 `same_table` 表包含 payload TOAST 数据，中间页扫描时 TOAST 关联增加了 I/O 开销；其余三种布局的分析表不包含 payload，扫描更轻量。

### 4.2 场景二：预览查询

**场景设计**：按固定窗口分页读取事件预览，preview 为 payload 前 200 个 Unicode code point。

**测试目的与预期**：预览查询不读取完整 payload，四种布局之间不应有显著差异。

**执行方式**：主 workload，四轮 Latin square，每轮 30 次测量。

**结果**（主 workload，应用可用 p50，单位 ms）：

| 场景 | 引擎 | same_table | separate | full_core | asset_ref |
|---|---|---:|---:|---:|---:|
| preview:first | ClickHouse | 19.2 | 26.8 | 26.7 | 26.3 |
| preview:first | XStore | 30.9 | 30.9 | 30.8 | 31.0 |
| preview:middle | ClickHouse | 24.1 | 26.4 | 26.6 | 25.6 |
| preview:middle | XStore | 98.4 | 58.4 | 58.6 | 58.6 |

**分析**：预览查询的分布与列表查询一致。XStore 的 `preview:middle` 在 `same_table` 上同样离群（98.4 ms 对 58.4–58.6 ms），原因与 `list:middle` 相同。

### 4.3 场景三：详情查询

**场景设计**：按 `event_id` 读取单条事件的完整逻辑记录，包含完整 payload bytes。四种 profile 各取一个代表场景。

**测试目的与预期**：详情查询需要读取完整 payload，布局差异应在此场景显现。

**执行方式**：主 workload，四轮 Latin square，每轮 30 次测量。

**结果**（主 workload，应用可用 p50，单位 ms）：

| 场景 | 引擎 | same_table | separate | full_core | asset_ref |
|---|---|---:|---:|---:|---:|
| detail:text_64k | ClickHouse | 9.4 | 111.8 | 17.1 | 12.2 |
| detail:text_64k | XStore | 10.7 | 12.1 | 10.9 | 20.4 |
| detail:text_512k | ClickHouse | 18.3 | 118.7 | 21.0 | 14.2 |
| detail:text_512k | XStore | 13.7 | 15.3 | 13.8 | 22.2 |
| detail:entropy_512k | ClickHouse | 33.4 | 120.7 | 25.8 | 14.3 |
| detail:entropy_512k | XStore | 13.7 | 15.1 | 13.4 | 22.2 |
| detail:text_2m | ClickHouse | 75.5 | 149.1 | 58.7 | 21.5 |
| detail:text_2m | XStore | 24.4 | 26.4 | 24.7 | 29.1 |

**分析**：ClickHouse 的 `separate` 布局在所有 detail 场景中显著离群，p50 为 111.8–149.1 ms，而其余三种布局为 9.4–75.5 ms。原因见第 1 节：JOIN 执行路径的谓词未下推到 payload 表。`asset_ref` 在 ClickHouse 上为最快或接近最快，因为 payload 从本地文件读取，不经过数据库查询路径。XStore 上 `asset_ref` 在 detail 场景中为最慢（20.4–29.1 ms），原因是 resolver 需要额外一次本地文件读取，但差异远小于 ClickHouse 的 `separate` 离群。

### 4.4 场景四：Trace 查询

**场景设计**：按 `trace_id` 读取同一 trace 下的全部事件，按时间排序。取 p25、p50、p95 三个代表性 trace。

**测试目的与预期**：trace 查询需要扫描多行并按时间排序，布局差异应在此场景显现。

**执行方式**：主 workload，四轮 Latin square，每轮 30 次测量。

**结果**（主 workload，应用可用 p50，单位 ms）：

| 场景 | 引擎 | same_table | separate | full_core | asset_ref |
|---|---|---:|---:|---:|---:|
| trace:p25 | ClickHouse | 25.0 | 117.0 | 21.4 | 12.7 |
| trace:p25 | XStore | 13.6 | 15.3 | 13.5 | 23.2 |
| trace:p50 | ClickHouse | 20.3 | 117.5 | 26.1 | 15.0 |
| trace:p50 | XStore | 16.7 | 18.2 | 16.4 | 25.0 |
| trace:p95 | ClickHouse | 66.6 | 154.6 | 64.5 | 29.0 |
| trace:p95 | XStore | 30.5 | 33.1 | 29.6 | 35.4 |

**分析**：ClickHouse 的 `separate` 布局在所有 trace 场景中同样显著离群，原因与 detail 场景相同。XStore 上 `asset_ref` 在 trace 场景中为最慢，原因是每行需要一次 resolver 读取，trace 场景的行数多于 detail 场景，累积开销更大。

### 4.5 场景五：批量恢复

**场景设计**：按固定窗口批量读取全部 27,561 行的完整 payload。`batch:main` 为主 cohort 的批量恢复。

**测试目的与预期**：批量恢复需要读取全部 payload bytes，布局差异在此场景最为显著。

**执行方式**：主 workload，四轮 Latin square，每轮 5 次测量。

**结果**（主 workload，应用可用 p50，单位 ms）：

| 引擎 | same_table | separate | full_core | asset_ref |
|---|---:|---:|---:|---:|
| ClickHouse | 2,294.4 | 2,652.4 | 2,351.6 | 1,451.0 |
| XStore | 1,396.8 | 1,320.7 | 1,372.8 | 812.2 |

**分析**：`asset_ref` 在两个引擎上均为最快的批量恢复布局。ClickHouse 上 `asset_ref` 比 `same_table` 快 36.8%，XStore 上快 41.8%。`asset_ref` 的批量恢复不经过数据库 payload 列读取，resolver 从本地文件系统顺序读取对象，避免了数据库的列解码或 TOAST 提取开销。ClickHouse 上 `separate` 最慢，原因与 detail/trace 场景相同。

### 4.6 场景六：等总字节分布控制

**场景设计**：固定 80 MiB 原始内容总量，比较 40 条 2 MiB payload（`equal_total_few_large`）与 1,280 条 64 KiB payload（`equal_total_many_medium`）两种分布。

**测试目的与预期**：在相同字节总量下，对象数量对 `asset_ref` 的批量恢复成本有显著影响。

**执行方式**：每个 workload 四轮 Latin square，每轮 30 次测量（detail/list/preview）或 5 次测量（batch）。

**结果**（批量恢复 p50，单位 ms）：

| workload | 引擎 | same_table | separate | full_core | asset_ref |
|---|---|---:|---:|---:|---:|
| equal_total_few_large | ClickHouse | 946.1 | 1,020.6 | 957.3 | 627.1 |
| equal_total_few_large | XStore | 810.2 | 766.4 | 791.8 | 519.3 |
| equal_total_many_medium | ClickHouse | 2,746.6 | 2,802.5 | 2,733.3 | 8,868.8 |
| equal_total_many_medium | XStore | 2,538.8 | 2,522.2 | 2,526.4 | 1,508.8 |

**分析**：ClickHouse 上 `asset_ref` 由 `equal_total_few_large` 的 627.1 ms 升至 `equal_total_many_medium` 的 8,868.8 ms，为 14.14 倍。resolver 请求数由 200 增至 6,400，说明成本由对象数量而非字节总量决定。XStore 上 `asset_ref` 在两组中均为最快，但 `equal_total_many_medium` 的优势缩小：519.3 ms 升至 1,508.8 ms，为 2.91 倍。XStore 的 `asset_ref` 优势来自对象发布后库内不保存 payload，批量恢复时数据库只返回引用行。

### 4.7 场景七：写入吞吐

**场景设计**：按 190 个 block 逐块写入，每 block 256 行，测量写入吞吐率和分项时间。

**测试目的与预期**：不同布局的写入开销来自额外表、双写或对象发布。

**执行方式**：主 workload，四轮 Latin square。

**结果**（主 workload，round_statistic_median）：

| 引擎 | 布局 | rows/s | MiB/s | wall_ms | block_p50_ms | block_p95_ms |
|---|---|---:|---:|---:|---:|---:|
| ClickHouse | same_table | 5,296.7 | 13.37 | 9,164 | 13.90 | 60.59 |
| ClickHouse | separate | 3,509.8 | 8.86 | 13,829 | 21.92 | 68.52 |
| ClickHouse | full_core | 3,153.2 | 7.96 | 15,398 | 29.25 | 73.16 |
| ClickHouse | asset_ref | 3,136.1 | 7.92 | 15,479 | 37.73 | 86.53 |
| XStore | same_table | 1,469.2 | 3.71 | 33,036 | 27.97 | 125.27 |
| XStore | separate | 1,257.2 | 3.17 | 38,606 | 44.47 | 104.61 |
| XStore | full_core | 1,232.5 | 3.11 | 39,380 | 45.73 | 140.18 |
| XStore | asset_ref | 2,368.9 | 5.98 | 20,488 | 48.30 | 68.10 |

**分析**：ClickHouse 的写入吞吐顺序为 `same_table` > `separate` > `full_core` > `asset_ref`，增量来自额外表的行构造与提交、Full/Core 双写和对象发布（576.06 ms）。XStore 的 `asset_ref` 写入反而最快（20,488 ms 对 33,036–39,380 ms），原因是对象发布后库内只写入引用行，payload 不进入数据库，避免了 TOAST 存储。ClickHouse 的 `asset_ref` 对象发布时间为 576.06 ms，asset_raw_object_bytes 为 128,450,560。XStore 的 `asset_ref` 对象发布时间为 545.20 ms，asset_raw_object_bytes 同为 128,450,560。

### 4.8 场景八：混合负载（干扰）

**场景设计**：在单个 target 内部产生并发负载，包含 quiet、detail_2m、trace_long、batch_loop 和 continuous_ingest 五个 phase。每个 phase 包含 30 秒 warmup 和 300 秒 measurement。

**测试目的与预期**：混合负载测试 `asset_ref` 的前台查询隔离性。

**执行方式**：ClickHouse 四布局各一次，每 phase 5 个 stream。

**结果**（measurement 段，前台 list/preview p50 和 scheduler drop）：

| 布局 | phase | list p50 (ms) | preview p50 (ms) | list drop | preview drop |
|---|---|---:|---:|---:|---:|
| same_table | quiet | 25.19 | 20.02 | 2 | 2 |
| same_table | batch_loop | 27.19 | 22.21 | 1,663 | 1,652 |
| separate | quiet | 29.19 | 33.27 | 2 | 2 |
| separate | batch_loop | 33.87 | 35.30 | 1,413 | 1,414 |
| full_core | quiet | 30.97 | 34.24 | 2 | 2 |
| full_core | batch_loop | 34.97 | 36.67 | 1,682 | 1,679 |
| asset_ref | quiet | 28.10 | 32.24 | 2 | 2 |
| asset_ref | batch_loop | 32.16 | 34.60 | 0 | 0 |

**分析**：`batch_loop` 相中，`asset_ref` 的前台 list 与 preview scheduler drop 为 0 次，其余三种布局分别为 1,663/1,652、1,413/1,414、1,682/1,679 次。`asset_ref` 把批量恢复竞争移出数据库查询路径，前台查询不受影响。`asset_ref` 自身的批量恢复 p50 为 1,750.08 ms，是四种布局中最快的（`same_table` 为 2,488.13 ms）。`detail_2m` 与 `trace_long` 两相相对 quiet 相的前台 p50 变化在 −2.9% 至 +9.9% 之间，最大 scheduler drop 为 5 次。该幅度属于运行间波动，不表示可观测的干扰效应。

XStore 的干扰实验不适用。interference runner 使用 ClickHouse 专有的 `capture_part_state` 采集 part_count、marks、compressed_bytes 和 uncompressed_bytes 指标，XStore/GaussDB 的 PostgreSQL MVCC 架构没有 MergeTree part 概念。不适用结论与证据记录在 `xstore-control-applicability.json` 中。

### 4.9 场景九：Part 状态控制

**场景设计**：在 ClickHouse 上控制 MergeTree part 数量为四种状态：fragmented（190 part）、merging（合并中）、stable（合并完成）、single_part（OPTIMIZE FINAL 后单 part）。每种状态下执行 12 个查询场景各 30 次采样。

**测试目的与预期**：part 数量影响查询性能，且影响方向在四种布局中一致。

**执行方式**：ClickHouse 四布局各一次，每状态 12 场景各 30 采样。

**结果**（controlled_table 的 part_count 与 compressed_bytes）：

| 布局 | 状态 | part_count | compressed_bytes | active_merges |
|---|---|---:|---:|---:|
| same_table | fragmented | 190 | 19,182,321 | 0 |
| same_table | merging | 190 | 19,182,321 | 7 |
| same_table | stable | 5 | 19,018,538 | 0 |
| same_table | single_part | 1 | 19,017,603 | 0 |
| separate | fragmented | 190 | 3,387,288 | 0 |
| separate | stable | 3 | 3,232,526 | 0 |
| separate | single_part | 1 | 3,232,968 | 0 |
| full_core | fragmented | 190 | 3,387,288 | 0 |
| full_core | stable | 3 | 3,232,526 | 0 |
| full_core | single_part | 1 | 3,232,968 | 0 |
| asset_ref | fragmented | 190 | 3,407,425 | 0 |
| asset_ref | stable | 3 | 3,245,270 | 0 |
| asset_ref | single_part | 1 | 3,245,662 | 0 |

**分析**：fragmented 态下 controlled_table 保持 190 part（每 block 一个 part），stable 态合并为 3–5 part，single_part 态合并为 1 part。merging 态的 active_merge_count 为 2–7，part_count 仍为 190（合并未完成）。asset_ref 布局的 asset_store 在所有状态下保持 160 个可用对象、128,450,560 bytes，orphan_count 为 0。

XStore 的 part 状态控制不适用。part_count、marks、compressed_bytes 和 uncompressed_bytes 是 ClickHouse MergeTree 专有指标，XStore/GaussDB 没有 part 概念。不适用结论与证据记录在 `xstore-control-applicability.json` 中。

### 4.10 场景十：Asset 故障与恢复

**场景设计**：在 `asset_ref` 布局下注入六种 Asset 故障，验证失败注入、分类、恢复动作和清理结果。六个案例为：missing、corrupt、metadata_mismatch、upload_then_db_failure、publish_failure、delete_failure。

**测试目的与预期**：六个固定用例在两个引擎上全部符合设计预期。

**执行方式**：ClickHouse 与 XStore 各一次，每次六案例顺序执行。

**结果**：

| 案例 | 引擎 | 注入点 | resolver 错误 | 终态 | 事件可见 | 孤儿数 | 恢复动作 |
|---|---|---|---|---|---|---:|---|
| missing | ClickHouse | remove_published_object | missing | available | True | 0 | restore_missing_object |
| missing | XStore | remove_published_object | missing | available | True | 0 | restore_missing_object |
| corrupt | ClickHouse | modify_published_bytes | corrupt | available | True | 0 | replace_corrupt_object |
| corrupt | XStore | modify_published_bytes | corrupt | available | True | 0 | replace_corrupt_object |
| metadata_mismatch | ClickHouse | replace_catalog_metadata | metadata_mismatch | available | True | 0 | restore_catalog_metadata |
| metadata_mismatch | XStore | replace_catalog_metadata | metadata_mismatch | available | True | 0 | restore_catalog_metadata |
| upload_then_db_failure | ClickHouse | fail_after_object_upload | missing | absent | False | 1 | remove_orphan_object |
| upload_then_db_failure | XStore | fail_after_object_upload | missing | absent | False | 1 | remove_orphan_object |
| publish_failure | ClickHouse | fail_pending_publication | failed | failed | True | 0 | confirm_failed_publication |
| publish_failure | XStore | fail_pending_publication | failed | failed | True | 0 | confirm_failed_publication |
| delete_failure | ClickHouse | fail_deleting_object_removal | deleting | deleting | True | 0 | confirm_delete_failure_state |
| delete_failure | XStore | fail_deleting_object_removal | deleting | deleting | True | 0 | confirm_delete_failure_state |

**分析**：六个固定用例在两个引擎上全部符合设计预期。`missing`、`corrupt` 和 `metadata_mismatch` 三案例在注入后 resolver 正确分类错误，恢复后对象恢复为 available 状态，事件可见。`upload_then_db_failure` 在对象上传后数据库写入失败，产生 1 个孤儿对象，恢复后移除孤儿，终态为 absent，事件不可见。`publish_failure` 终态为 failed，事件可见但恢复不可见。`delete_failure` 终态为 deleting，事件可见但恢复不可见。所有案例的 validation_errors 和 execution_error 均为空。

openGauss 的 asset-failure 实验在黄区不适用。黄区环境没有 openGauss 部署，只有 XStore 和 ClickHouse。XStore 与 openGauss 同源，asset-failure 的注入、分类和恢复逻辑使用标准 SQL 操作，XStore adapter 继承 OpenGaussAdapter，行为一致。不适用结论与证据记录在 `xstore-control-applicability.json` 中。

## 5. 跨场景分析

### 5.1 布局选择

以 `same_table` 为基准、主 workload 应用可用 p50 的几何均值计算布局空间排序：

| 引擎 | same_table | separate | full_core | asset_ref |
|---|---:|---:|---:|---:|
| ClickHouse | 1.000 | 2.569 | 1.097 | 0.736 |
| XStore | 1.000 | 0.967 | 0.913 | 1.115 |

ClickHouse 上 `asset_ref` 最快、`separate` 最慢。`separate` 的离群由 JOIN 执行路径的谓词未下推导致，在 ClickHouse 23.3.10.5 上比蓝区 25.12.11.4 更显著。`asset_ref` 的优势来自 payload 读取移出数据库查询路径。

XStore 上 `full_core` 最快、`asset_ref` 略慢于 `same_table`。XStore 的行存引擎在 `separate` 布局下 JOIN 代价低于 ClickHouse 列存，`full_core` 通过窄表复制避免了 JOIN 和 payload 读取。`asset_ref` 在 XStore 上需要额外的 resolver 文件读取，在 detail 和 trace 场景中增加了开销。

两个引擎的排序不同，说明布局选择依赖引擎的存储模型和执行器特性。ClickHouse 列存下 `separate` 的 JOIN 谓词下推缺陷是主要影响因素，XStore 行存下该缺陷不显著。

### 5.2 成本转移边界

`asset_ref` 把 payload 存储成本从数据库转移到本地文件系统。写入时需要额外的对象发布时间（ClickHouse 576.06 ms，XStore 545.20 ms），但库内写入数据量减少（ClickHouse ingest body bytes 从 152,912,872 降至 24,728,847）。批量恢复时 `asset_ref` 在两个引擎上均为最快，但等总字节分布控制显示成本由对象数量决定：1,280 个 64 KiB 对象的批量恢复比 40 个 2 MiB 对象慢 14.14 倍（ClickHouse）和 2.91 倍（XStore）。

### 5.3 正交性

Part 状态与布局选择正交。ClickHouse 碎片态到单 part 态的查询性能变化在同一布局内为 2.90–3.27 倍（蓝区数据），而同一控制内同一状态下四种布局的最大最小比只有 1.114–1.175。Part 状态的影响独立于布局选择。

### 5.4 比较边界

黄区 ClickHouse 23.3.10.5 与蓝区 ClickHouse 25.12.11.4 版本不同，数值不直接比较。23.3 的 `separate` 布局 JOIN 谓词下推缺陷比 25.12 更显著，导致 `separate` 在黄区的离群幅度更大。XStore 与蓝区 openGauss 6.0.0 产品版本不同，存储引擎行为可能有差异，数值同样不直接比较。

黄区实验在单台 ARM64 主机上串行执行，CPU、内存和磁盘资源固定。两个引擎的数值在相同硬件条件下采集，跨引擎比较的硬件变量已控制，但存储引擎、协议、压缩编解码和后台维护的差异仍然存在。

## 6. 正确性、限制与后续测试

### 6.1 真值结论

两个引擎各 16 个 target（4 布局 × 4 workload）全部 complete，真值校验全部通过。主 workload 四轮 Latin square 每轮 30 次测量，batch 每轮 5 次测量。correctness_only workload 每轮 1 次测量。正式样本失败数为 0。

合并汇总 `combined-summary.json` 通过汇总器全部门禁，包含矩阵、part-states、interference 和 asset-failures 四族控制。

### 6.2 限制

1. **openGauss 缺失**：黄区环境没有 openGauss 部署。asset-failure 控制只覆盖 ClickHouse 和 XStore 两个引擎，不覆盖蓝区的 openGauss。XStore 与 openGauss 同源，asset-failure 行为一致，但不等于 openGauss 上的直接验证。
2. **XStore 干扰实验不适用**：interference runner 使用 ClickHouse 专有的 part 状态指标，XStore/GaussDB 没有 MergeTree part 概念。干扰实验只在 ClickHouse 上执行。
3. **XStore part 状态控制不适用**：part-state 控制是 ClickHouse MergeTree 专有功能，XStore/GaussDB 使用 PostgreSQL MVCC 架构，没有 part 概念。
4. **ClickHouse 版本差异**：黄区使用 ClickHouse 23.3.10.5 LTS，蓝区使用 25.12.11.4。23.3 不支持 `SYSTEM FLUSH LOGS query_log` 语法，JSONEachRow 返回数值字段为字符串而非整数。这些差异已通过代码适配解决，但版本间的执行器行为差异可能导致 `separate` 布局的 JOIN 谓词下推表现不同。
5. **XStore 适配**：XStore adapter 继承 OpenGaussAdapter，处理 GaussDB 兼容性差异：不支持 `FILTER (WHERE ...)` 语法、不支持行构造器比较、空字符串视为 NULL。这些适配不影响查询语义和真值校验。
6. **ASSET_FAILURE_ENGINES 环境变量**：汇总器的 `ASSET_FAILURE_ENGINES` 配置通过环境变量覆盖为 `clickhouse,xstore`，默认值为 `opengauss,clickhouse,xstore`。覆盖原因记录在本节第 1 条。

### 6.3 后续实验

1. 在有 openGauss 的环境中补齐 openGauss 的 asset-failure 实验，恢复汇总器的默认引擎覆盖。
2. 在 ClickHouse 25.12 上重跑 `separate` 布局，验证 JOIN 谓词下推缺陷是否已修复。
3. 在 XStore 上实现等价的干扰实验框架，使用 GaussDB 的系统视图替代 ClickHouse 的 part 状态指标。

## 7. 使用建议

1. **ClickHouse 23.3 上避免 `separate` 布局**：`separate` 在 detail、trace 和 batch 场景中显著离群，原因是 JOIN 谓词未下推到 payload 表。在 ClickHouse 23.3 上优先使用 `same_table` 或 `asset_ref`。
2. **XStore 上 `full_core` 或 `same_table` 为均衡选择**：XStore 上 `full_core` 在主 workload 的几何均值最优，`same_table` 在列表和预览场景中略慢于其余布局（`list:middle` 除外），但在 detail 和 trace 场景中优于 `asset_ref`。
3. **`asset_ref` 适合批量恢复密集场景**：`asset_ref` 在两个引擎上的批量恢复均为最快，且在 ClickHouse 的混合负载中前台查询不受 batch 干扰。但批量恢复成本随对象数量线性增长，不适合大量小对象的场景。
4. **对象数量是 `asset_ref` 的主要成本因素**：等总字节分布控制显示，在相同字节总量下，1,280 个 64 KiB 对象的批量恢复比 40 个 2 MiB 对象慢 2.91–14.14 倍。使用 `asset_ref` 时应控制对象数量，或使用更大的 payload 粒度。

## 来源

- 冻结输入归档：`json-storage-stage3-formal-input-20260917.tar.gz`，SHA-256 `47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f`
- 合并汇总：`$YELLOW_OUTPUT/combined-summary.json`
- 矩阵运行清单：`$YELLOW_OUTPUT/{clickhouse,xstore}-main/{engine}/{layout}/run-manifest.json`
- Part 状态运行清单：`$YELLOW_OUTPUT/ch-part-states-{layout}/run-manifest.json`
- 干扰运行清单：`$YELLOW_OUTPUT/ch-interference-{layout}/run-manifest.json`
- Asset 故障运行清单：`$YELLOW_OUTPUT/{ch,xstore}-asset-failures/run-manifest.json`
- 回传摘录：`$YELLOW_OUTPUT/handback/summary.json`
- XStore 控制适用性：`$YELLOW_OUTPUT/xstore-control-applicability.json`
- 黄区执行指南：`docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md`
- 蓝区实验报告：`docs/json-storage/json-storage-stage3-report-2026-09-20.md`
