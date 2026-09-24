# Agent Trace JSON 存储阶段三 XStore 横向比较实验报告

> 日期：2026-09-24
>
> 状态：ARM 主机 A（161）正式结果已回填；ARM 主机 B 的结果待回传
>
> 适用版本：XStore（GaussVector 103.0.0 build 66de5983，release）、ClickHouse 23.3.10.5；参照结果为 x86 主机的 openGauss 6.0.0 与 ClickHouse 25.12.11.4

本报告给出四种长载荷布局在 ARM 主机 A 上的实测结果：同机运行 XStore 与 ClickHouse 23.3，两个引擎串行。布局定义、两类引擎中的物理组织、Sidecar 实现、场景与查询语句见[阶段三原理与设计](json-storage-stage3-principles-design-2026-09-24.md)（下文简称"原理与设计"），本报告只保留判读结果所需的定义并引用其章节。x86 主机的参照结果见[阶段三实验报告](json-storage-stage3-report-2026-09-20.md)（下文简称"x86 报告"）。

文中事实按来源标注：**契约**指冻结输入与实验程序的定义；**x86 实测**指 x86 报告已验证的结果；**主机 A 实测**指本轮回传的运行证据。本轮运行存在四项已识别的测量因素：主机背景负载、XStore 中间页游标写法、XStore 客户端驱动开销、混合负载的客户端进程争用。它们的来源、影响范围与处理方法集中写在第 3.3 节，并在受影响的结果处就地标注。

字节量按二进制单位表示，1 MiB 为 1,048,576 字节；表中 MB 为 10^6 字节。

## 1. 结论

**同表载荷对不读取载荷的查询有影响，影响只出现在需要访问大量行的路径上。** XStore 把载荷不压缩地存放在主关系内（第 4.1 节），`same_table` 的 heap 为 186.5 MB，无载荷的 `events_analytics` 为 11.0 MB。列表第一页只回表 256 行，四种布局的应用可用 p50 为 28.2–28.6 ms，没有差异；中间页在本轮的游标写法下先过滤 13,652 行再返回 256 行，`same_table` 为 124.7 ms，其余三种布局为 58.2–59.1 ms，前者为后者的 2.1 倍。写入时每个 block 之后的水位查询 `MAX(ingest_seq)` 读取写目标的全部行，`same_table` 的水位确认合计 36.6 s，`asset_ref` 为 10.3 s，而两者的写入合计相近（9.5 s 与 9.8 s）。ClickHouse 的列表不读取载荷列，`same_table` 反而最快：列表第一页 23.8 ms，其余三种布局 34.5–35.4 ms，读取行数为 10,541 对 32,768，来自 granule 划分（第 4.2 节）。

**XStore 在库内布局下不压缩载荷，`asset_ref` 的总物理占用因此低于 `same_table`。** XStore `same_table` 库内占用 219.8 MB，为载荷原始字节的 1.71 倍；`asset_ref` 库内 44.6 MB 加对象 128.5 MB，合计 173.0 MB，为 `same_table` 的 0.787 倍。ClickHouse 以 ZSTD 压缩载荷列，`same_table` 只占 19.0 MB，`asset_ref` 的总占用为其 6.921 倍，与 x86 主机的倍数一致。`separate` 与 `full_core` 在 XStore 上为 1.188 倍和 1.202 倍，在 ClickHouse 上为 1.097 倍和 1.170 倍。

**XStore 的库内详情与完整 Trace 最快，耗时对载荷大小不敏感。** XStore 库内布局的单条详情为 10.6–27.3 ms，`text_64k` 到 `text_2m` 只增加约 15 ms；高熵与可压缩内容耗时相同，与载荷未压缩一致。ClickHouse 23.3 上 `separate` 的连接在详情与完整 Trace 中都整表读取右表（48,534 行，查询合计读取 56,726 行、约 133 MB），详情为 122.4–156.3 ms，完整 Trace 为 120.1–157.2 ms；x86 的 ClickHouse 25.12 在详情中把 `event_id` 条件下推到右表，只在完整 Trace 中整表读取。ClickHouse 的 `asset_ref` 在 2 MiB 详情上最快（26.2 ms，`same_table` 为 72.8 ms）。

**`asset_ref` 的批量恢复在大对象下最快、在大量中等对象下最慢，两个引擎方向一致。** `main` 批量恢复 XStore `asset_ref` 为 835.5 ms，库内布局为 1,406.5–1,541.9 ms；ClickHouse 为 1,604.2 ms 对 2,301.6–2,731.2 ms。等总字节控制中，1,280 个 64 KiB 对象使 `asset_ref` 成为最慢布局：XStore 2,037.1 ms，库内布局 952.8–1,033.3 ms；ClickHouse 11,693.1 ms，库内布局 1,587.9–1,750.3 ms。解析器请求数由 205 增至 6,405，每个对象的目录查询与文件读取在 ClickHouse 上约 8.9 ms，在 XStore 上约 1.3 ms。

**写入顺序两引擎不同。** 写入合计的四轮中位数：XStore 为 `same_table` 9,494 ms、`asset_ref` 9,817 ms、`separate` 12,798 ms、`full_core` 12,969 ms；ClickHouse 为 4,491、6,004、6,693、7,585 ms（同序为 `same_table`、`separate`、`full_core`、`asset_ref`）。XStore 上 `asset_ref` 的库内写入量最小，对象发布只占 665.7 ms，写入合计与 `same_table` 相近；ClickHouse 的 `asset_ref` 每个对象一次同步 mutation，写入最慢。

**ClickHouse 的 part 状态影响大于布局。** 碎片态（190 个 part）的 `list:first` 为 88.7–89.4 ms，单 part 态为 16.0–33.3 ms，同一布局内比值为 2.66–5.59；同一状态内四种布局的比值在碎片态为 1.008，其余状态为 1.30–2.08。

**混合负载的结果只作方向判断。** 前台请求流与干扰流运行在同一个客户端进程内，客户端争用进入了前台时延与调度丢弃（第 3.3 节）。在这一限制下：持续批量读取使两个引擎的库内布局前台 list p95 升至 212.7–412.9 ms，调度丢弃 1,528–2,134 次；ClickHouse `asset_ref` 的前台不受影响（p95 48.9 ms，丢弃 4 次），XStore `asset_ref` 丢弃最多（2,594 次）。分表布局在两个引擎上都没有减轻批量读取对前台的干扰。

**Asset 故障语义在两个引擎上与设计一致。** 六个用例的解析器错误分类、目录终态、事件可见性与孤儿处理全部符合设计，执行错误为 0。

主矩阵、part 状态与 Asset 故障的结果可用于同机布局比较。XStore 的小查询绝对耗时主要由客户端驱动与连接建立构成，跨引擎比较小查询时按第 3.3 节的分解读取。

## 2. 运行身份与数据规模

### 2.1 主机、引擎与工具包

| 项 | 值 | 来源 |
|---|---|---|
| 处理器 | 鲲鹏 920，4 路共 256 核，8 个 NUMA 节点，aarch64 | 主机 A 实测 |
| 内存 | 1,999 GiB，运行前可用 1,887 GiB | 主机 A 实测 |
| 数据盘 | `/data3`，md RAID（`/dev/md127`），xfs，可用 14 TiB | 主机 A 实测 |
| 操作系统 | EulerOS（内核 5.10.0-182.0.0.95.h1954.eulerosv2r13，aarch64） | 主机 A 实测 |
| XStore | GaussVector 103.0.0 build 66de5983，compiled at 2026-09-21 15:00:57，release；源码提交 ec1b24410e，构建命令 `build.sh -pm cloudnative -m release` | 主机 A 实测 |
| ClickHouse | 23.3.10.5；`parts_to_delay_insert` 1,000，`parts_to_throw_insert` 3,000，`max_server_memory_usage_to_ram_ratio` 0.5 | 主机 A 实测 |
| 工具包 | `stage3/xstore-yellow-handoff` 的 d851e02；127 份运行记录的代码全部与该提交一致 | 主机 A 实测 |
| 冻结输入 | 48,534 行、190 个 block，`main` 载荷 160 个对象、128,450,560 字节 | 契约 |

XStore 构建在本轮之前完成，版本与二进制在本轮全程未变。每个查询样本新建一条数据库连接，结果读取后关闭。

### 2.2 样本与门禁

| 运行 | 规模 | 结果 |
|---|---|---|
| 主矩阵 | 2 引擎 × 4 布局 × 4 workload，每个 target 2,195 个正式样本，合计 17,560 个 | 全部成功，响应字节全部通过校验 |
| part 状态控制 | 4 布局 × 4 状态 × 12 个查询目标 × 30 次，合计 5,760 个 | 全部完成 |
| 混合负载 | 2 引擎 × 4 布局 × 5 阶段，调度 486,440 个请求 | 成功 450,644 个，调度丢弃 35,796 个，查询失败与超时 0 个 |
| Asset 故障 | 2 引擎 × 6 用例 | 全部符合设计，执行错误 0 个 |
| 行存探针 | XStore 三种库内布局 | 完成，见第 4.1 节 |

主矩阵每个引擎四轮，按 Latin square 轮换布局；每个非批量目标每轮预热 1 次、测量 30 次，批量目标测量 5 次。表中 p50 为**四轮轮级中位数**：先在轮内取中位数，再对四轮取中位数。

## 3. 判读口径

### 3.1 计时分项

应用可用时间按样本计量，从查询开始到结果完成校验，分为以下部分（原理与设计第 1.5、8.1 节）：

| 分项 | 范围 |
|---|---|
| 查询完成 | 执行语句并由驱动读完全部结果行；`asset_ref` 此时取得引用 |
| 客户端恢复 | 结果规范化；`asset_ref` 在此阶段查询目录并读取对象 |
| 正确性校验 | 长度、SHA-256 与内容比对 |
| 分项之外 | 应用可用减去三个分项的剩余部分，主要为建立数据库连接，另含响应字节核算 |

表中各分项分别取四轮轮级中位数，因此"分项之外"一列由中位数相减得到，只用于量级判断。

### 3.2 比较单位

比较在同一主机、同一引擎、同一 workload、同一布局或同一 part 状态内进行。跨引擎比较的是完整方案，同时包含存储模型、索引与排序键、协议、驱动和客户端处理。跨主机的数值只用于核对方向。

### 3.3 测量因素与处理

| 因素 | 证据 | 影响范围 | 处理 |
|---|---|---|---|
| **主机背景负载** | 每个阶段开始前 1 分钟平均负载为 50.1–71.1（反馈契约阈值为 25.6）；主机上另有 3 个与本实验无关的 gaussdb 进程（其一名为 `gaussdb:tenanta`），CPU 占用 39.6%–240%；同期 CPU 空闲 94%–97%，写盘 `bo` 低于 5,000，可用内存 1,887 GiB | 全部计时结果；表现为轮间波动 | 以轮间波动作为分辨阈值：四轮轮级 p50 的最大最小比，XStore 的中位数为 1.049、90 分位为 1.122，ClickHouse 为 1.111 与 1.357。同一场景内布局差异低于这一量级时记为未分辨 |
| **XStore 中间页游标** | 适配器把行构造器游标展开为 `start_time > c OR (start_time = c AND event_id > id)`，XStore 不把它用作索引范围起点，四种布局的中间页都先过滤 13,652 行；行存探针显示，同一语句增加被蕴含的下界 `start_time >= c` 后，服务端 `Total runtime` 由 97.5 ms 降至 0.87 ms，结果集不变 | XStore 的 `list:middle` 与 `preview:middle` | 以测得值报告该写法下的成本，并作为"回表访问大量行"的证据；布局间的列表比较以第一页为准；修正写法的端到端结果由工具包修正后的重跑给出 |
| **XStore 客户端驱动** | XStore 经 ctypes 封装的 libpq 逐字段读取与转换结果；`list:first` 的服务端 `Total runtime` 为 0.63–0.64 ms，查询完成为 18.0–18.2 ms；同一连接上 `SELECT 1` 往返 p50 为 0.16 ms | XStore 全部查询的绝对耗时，返回行数越多影响越大 | 同一引擎内的布局比较不受影响（各布局返回相同的行）；跨引擎比较时同时给出服务端时间与客户端分项，第 4.2 节给出分解 |
| **混合负载的客户端争用** | 前台两条流与干扰流运行在同一个 Python 进程内。XStore 的 quiet 阶段前台 list p50 为 58.6–59.8 ms，其中查询完成为 46.1–46.5 ms，是主矩阵查询完成的约 2.5 倍，而服务端执行时间不足 1 ms；ClickHouse 库内布局在 `batch_loop` 阶段的调度丢弃中，调度线程自身迟到超过 50 ms 的占 51%–56% | 混合负载的前台时延与丢弃数 | 只比较同一布局内相对 quiet 阶段的变化与布局之间的方向；按丢弃原因拆分；隔离性结论待前台与干扰流分进程运行后确认 |

## 4. 场景结果

### 4.1 载入、维护与空间

**场景。** 按 190 个 block 写入 `main` workload，每个 block 后查询各写目标的水位，达到联合水位后提交下一个 block（原理与设计第 3.3、9.1 节）。**写入合计**为 190 个 block 写入请求耗时之和，**水位确认**为各 block 等待联合水位的耗时之和（运行清单的 `watermark_wait_ms`），**写入完成**为包含两者在内的墙钟时间，均取四轮中位数。空间取末轮的物理证据。

**写入结果（主机 A 实测）。**

| 引擎 | 布局 | 写入合计 | 单块 p50 | 水位确认 | 写入完成 | 对象发布 |
|---|---|---:|---:|---:|---:|---:|
| XStore | `same_table` | 9,493.5 ms | 35.2 ms | 36,572 ms | 47,468.9 ms | — |
| | `separate` | 12,797.5 ms | 55.3 ms | 39,254 ms | 53,796.9 ms | — |
| | `full_core` | 12,969.0 ms | 55.4 ms | 39,727 ms | 54,670.8 ms | — |
| | `asset_ref` | 9,817.3 ms | 51.2 ms | 10,297 ms | 21,703.1 ms | 665.7 ms |
| ClickHouse | `same_table` | 4,490.5 ms | 14.2 ms | 3,211 ms | 9,024.3 ms | — |
| | `separate` | 6,004.0 ms | 22.5 ms | 6,262 ms | 14,022.8 ms | — |
| | `full_core` | 6,692.8 ms | 27.2 ms | 6,873 ms | 15,489.0 ms | — |
| | `asset_ref` | 7,585.3 ms | 40.8 ms | 7,969 ms | 17,030.1 ms | 626.9 ms |

**空间结果（主机 A 实测）。** XStore 为关系已分配字节，ClickHouse 为 active part 的压缩字节。

| 引擎 | 布局 | 库内空间 | 对象存储 | 合计 | 相对 `same_table` |
|---|---|---:|---:|---:|---:|
| XStore | `same_table` | 219.82 MB（heap 186.52，索引 33.31） | — | 219.82 MB | 1.000 |
| | `separate` | 261.08 MB | — | 261.08 MB | 1.188 |
| | `full_core` | 264.31 MB | — | 264.31 MB | 1.202 |
| | `asset_ref` | 44.56 MB | 128.45 MB | 173.02 MB | 0.787 |
| ClickHouse | `same_table` | 19.03 MB | — | 19.03 MB | 1.000 |
| | `separate` | 20.89 MB | — | 20.89 MB | 1.097 |
| | `full_core` | 22.27 MB | — | 22.27 MB | 1.170 |
| | `asset_ref` | 3.28 MB | 128.45 MB | 131.73 MB | 6.921 |

**XStore 的大值存放（主机 A 实测，行存探针）。** 三种库内布局的全部关系 `reltoastrelid` 为 0，TOAST 分项为 0 字节。按 profile 统计，`sum(pg_column_size(payload))` 恰为 `sum(octet_length(payload))` 加每值 4 字节，例如 40 个 `text_2m` 为 83,886,240 对 83,886,080，高熵与可压缩内容相同。`events` 的 heap 比无载荷的 `events_analytics` 多 175.5 MB，为 `main` 载荷原始字节的 1.37 倍。三项证据共同表明载荷未压缩、存放在主关系内。2 MiB 的值大于单页，heap 另有 37% 的额外字节，值在页内的组织方式不在本轮证据范围内。

**分析。** XStore 不压缩载荷，是空间结论与 x86 openGauss 不同的原因：openGauss 以 TOAST 压缩可压缩文本，`asset_ref` 的总占用为 `same_table` 的 2.884 倍（x86 实测）；XStore 库内保存原始字节并带页内开销，外置后总占用反而下降到 0.787 倍。`separate` 与 `full_core` 的增量在 XStore 上约 41–44 MB，来自第二张表的普通列、元数据与三条索引（每张事件表索引约 33.3 MB）。ClickHouse 三个倍数与 x86 实测完全相同，因为数据与编码相同。

写入合计反映写入请求本身。XStore 双表布局比 `same_table` 多 3.3–3.5 s，来自第二张表的行构造与索引维护；`asset_ref` 库内只写窄表与目录，对象发布 665.7 ms，合计与 `same_table` 相近。ClickHouse 的 `asset_ref` 最慢，每个对象一次 `mutations_sync = 2` 的状态更新计入写入（原理与设计第 5.4 节）。

水位查询为 `SELECT COALESCE(MAX(ingest_seq)+1,0) FROM <写目标>`，`ingest_seq` 上没有索引，按表结构需要读取写目标的全部行（本轮未采集该查询的计划）。XStore 上水位确认为每 block 192.5–209.1 ms（`same_table`、`separate`、`full_core`）与 54.2 ms（`asset_ref`），与被读取 heap 的大小同向：前三者的写目标含 183–187 MB 的载荷 heap，`asset_ref` 的写目标为 11.0 MB 的 `events_analytics` 与目录表。ClickHouse 上为每 block 16.9–41.9 ms。这是一个不读取载荷的聚合查询被主关系中的载荷拖慢的实例。水位确认属于实验的同步屏障（原理与设计第 3.3 节），布局写入成本的比较以写入合计为准。

### 4.2 列表查询

**场景。** 固定窗口内按 `(start_time, event_id)` 做 keyset 分页，每页 256 行，载荷与预览投影为 NULL（原理与设计第 9.2 节）。XStore 第一页不带游标条件，中间页使用第 3.3 节的展开式游标。

**结果（主机 A 实测，`main` workload）。**

| 引擎 | 布局 | `list:first` p50 | `list:middle` p50 | 访问路径 | 读取量 |
|---|---|---:|---:|---|---|
| XStore | `same_table` | 28.6 ms | **124.7 ms** | `events_list_idx`；中间页 Filter 移除 13,652 行 | 返回 256 行 |
| | `separate` | 28.2 ms | 58.2 ms | `events_analytics_list_idx`；同上 | 返回 256 行 |
| | `full_core` | 28.2 ms | 59.1 ms | `events_core_list_idx`；同上 | 返回 256 行 |
| | `asset_ref` | 28.3 ms | 58.9 ms | `events_analytics_list_idx`；同上 | 返回 256 行 |
| ClickHouse | `same_table` | **23.8 ms** | **26.7 ms** | `events` 选中 2/6 part、9/15 granule | 10,541 行、1.92 MB；中间页 18,899 行 |
| | `separate` | 34.5 ms | 32.3 ms | `events_analytics` 选中 1/4 part、4/9 granule | 32,768 行、5.97 MB |
| | `full_core` | 34.9 ms | 33.6 ms | `events_core` 选中 1/2 part、4/7 granule | 32,768 行、5.97 MB |
| | `asset_ref` | 35.4 ms | 34.0 ms | `events_analytics` 选中 1/2 part、4/7 granule | 32,768 行、5.97 MB |

XStore 的 `list:first` 分项（四布局范围）：服务端 `Total runtime` 0.63–0.64 ms，查询完成 18.0–18.2 ms，客户端恢复 1.2 ms，校验 0.64–0.66 ms，分项之外 8.3–8.6 ms。ClickHouse 的 `list:first`：查询完成 18.1–30.2 ms，客户端恢复 2.1–2.4 ms，校验 0.68–0.74 ms，分项之外 2.4–3.1 ms。

**分析。** XStore 第一页在四种布局上相差 1.7%，低于轮间波动，未分辨。服务端执行不足 1 ms，查询完成与服务端时间之差约 17.4 ms，由协议传输与驱动读取、转换 256 行 × 11 列构成；同一连接上 `SELECT 1` 往返 0.16 ms，差值随返回行数增长（1 行的 `detail:text_64k` 查询完成 3.9 ms，256 行的列表 18 ms），与逐字段处理的开销一致。分项之外的 8.3–8.6 ms 包括连接建立与响应字节核算。XStore 与 ClickHouse 的列表差距因此主要在客户端路径：ClickHouse 服务端读取 1–6 MB，经 HTTP 返回并解析 JSON；XStore 服务端工作不足 1 ms，时间在驱动与连接上。

中间页展示了主关系中的载荷在回表路径上的代价。四种布局执行相同的计划，Index Scan 从窗口起点开始，把游标之前的 13,652 行逐行回表后按 Filter 丢弃。回表读取的行在 `same_table` 中位于 186.5 MB 的含载荷 heap 上，在其余布局中位于 11.0 MB 的窄 heap 上；前者查询完成 114.1 ms，后者 47.6–48.5 ms。行存探针在 `same_table` 上测得该计划的服务端 `Total runtime` 为 97.5 ms，与两页查询完成之差（96 ms）一致。增加被蕴含的下界后，扫描从游标位置开始，只访问 262 行（Filter 移除 6 行），`Total runtime` 为 0.87 ms，与第一页的 0.64 ms 同量级。四种布局在修正写法下的端到端结果由重跑给出。

ClickHouse 的方向与 x86 实测一致：含载荷的 `events` 使 granule 更窄（第一轮计划中 `events` 共 15 个 granule，窄表 7–9 个），一页 keyset 读取的行数更少，`same_table` 反而最快（原理与设计第 5.2 节）。主机 A 上 `same_table` 相对其余布局快 31%–33%，x86 为 10%–12%。各布局在本轮自然稳定态下的 part 数不同（`events` 6 个，`events_analytics` 2–4 个，`events_core` 2 个），读取量同时受 part 与 granule 边界影响。

### 4.3 预览查询

**场景。** 与列表共用窗口与分页，加入写入时预存的 200 字符预览列（原理与设计第 9.3 节）。

**结果（主机 A 实测）。**

| 引擎 | 布局 | `preview:first` p50 | `preview:middle` p50 |
|---|---|---:|---:|
| XStore | `same_table` | 28.3 ms | 124.7 ms |
| | `separate` | 28.3 ms | 58.3 ms |
| | `full_core` | 28.2 ms | 58.9 ms |
| | `asset_ref` | 28.3 ms | 58.9 ms |
| ClickHouse | `same_table` | 21.5 ms | 26.4 ms |
| | `separate` | 33.2 ms | 35.2 ms |
| | `full_core` | 34.1 ms | 33.2 ms |
| | `asset_ref` | 33.7 ms | 34.7 ms |

**分析。** 预览与列表的差异在两个引擎上都低于轮间波动，预存预览列的增量未分辨。中间页与 ClickHouse 的布局差异沿用第 4.2 节的机制。

### 4.4 单条详情

**场景。** 按项目、Trace、时间与事件标识恢复一行完整记录，四个样本为 64 KiB、512 KiB、2 MiB 可压缩文本与 512 KiB 高熵内容（原理与设计第 9.4 节）。

**结果（主机 A 实测，应用可用 p50）。**

| 引擎 | 布局 | `text_64k` | `text_512k` | `entropy_512k` | `text_2m` |
|---|---|---:|---:|---:|---:|
| XStore | `same_table` | 10.8 ms | 13.8 ms | 13.7 ms | 26.2 ms |
| | `separate` | 13.1 ms | 15.1 ms | 15.2 ms | 27.3 ms |
| | `full_core` | 10.6 ms | 13.7 ms | 13.6 ms | 25.8 ms |
| | `asset_ref` | 20.6 ms | 22.2 ms | 22.1 ms | 29.3 ms |
| ClickHouse | `same_table` | 13.4 ms | 19.4 ms | 36.3 ms | 72.8 ms |
| | `separate` | 122.4 ms | 128.1 ms | 126.9 ms | 156.3 ms |
| | `full_core` | 13.3 ms | 20.6 ms | 33.5 ms | 76.2 ms |
| | `asset_ref` | 16.4 ms | 18.3 ms | 22.9 ms | 26.2 ms |

XStore 的访问路径为主键索引取一行：`same_table` 与 `full_core` 为 `events_pkey`、`events_full_pkey`，`separate` 为两表主键的 Nested Loop，`asset_ref` 为 `events_analytics_pkey` 后逐对象查目录。XStore `text_64k` 的服务端时间为 0.10–0.20 ms，查询完成为 3.9–6.2 ms。ClickHouse `separate` 的右表 `event_payloads` 选中 6/6 part、16/16 granule，整表 48,534 行，查询合计读取 56,726 行、133.3 MB；`same_table` 读取一个 granule，`text_2m` 为 8,192 行、18.4 MB。

**分析。** XStore 库内布局的详情随载荷增大缓慢增长，`text_64k` 到 `text_2m` 增加约 15 ms，其中校验增加约 5 ms；高熵与可压缩内容耗时相同，与载荷未压缩一致。`asset_ref` 的目录查询与文件读取在客户端恢复中增加约 10–14 ms，在 2 MiB 时与库内布局接近。

ClickHouse 23.3 上 `separate` 的哈希连接先整表读取右表，与请求行数无关，比 `same_table` 多 83.5–109.0 ms（原理与设计第 5.3 节）。这与 x86 实测不同：ClickHouse 25.12 在详情中把 `event_id` 等值条件下推到右表，`separate` 只扫描 7,859 行，整表读取只出现在完整 Trace 中。ClickHouse 库内布局读取整个 granule 的载荷列，`text_2m` 在 `same_table` 上达 72.8 ms；`asset_ref` 只读引用与一个对象，大对象下最快。等长的高熵与可压缩样本在 ClickHouse 的 `same_table` 与 `full_core` 上相差 12.9–16.9 ms，两个样本所在 granule 的行数与读取字节不同（`same_table` 为 4,405 行、10.6 MB 对 2,688 行、3.6 MB），本轮证据不把该差异归因于压缩。

### 4.5 完整 Trace

**场景。** 按项目、Trace 与时间范围返回一条 Trace 的全部 Span 与载荷；三个样本分别为 5、6、31 个 Span（原理与设计第 9.5 节）。

**结果（主机 A 实测，应用可用 p50）。**

| 引擎 | 布局 | `trace:p25` | `trace:p50` | `trace:p95` |
|---|---|---:|---:|---:|
| XStore | `same_table` | 14.0 ms | 16.5 ms | 31.6 ms |
| | `separate` | 15.1 ms | 18.0 ms | 33.7 ms |
| | `full_core` | 13.2 ms | 16.3 ms | 31.6 ms |
| | `asset_ref` | 23.1 ms | 25.4 ms | 35.1 ms |
| ClickHouse | `same_table` | 26.8 ms | 22.3 ms | 65.3 ms |
| | `separate` | 120.1 ms | 131.0 ms | 157.2 ms |
| | `full_core` | 27.3 ms | 24.3 ms | 70.7 ms |
| | `asset_ref` | 20.1 ms | 19.6 ms | 33.6 ms |

**分析。** XStore 经 Trace 索引精确取回 5、6、31 行，库内布局之间的差异低于轮间波动；`asset_ref` 在 `trace:p95` 上需要 2 次目录查询与文件读取，比库内布局慢约 3 ms。ClickHouse `separate` 的右表整表读取与第 4.4 节相同；`asset_ref` 在三个样本上都最快，`trace:p95` 为 33.6 ms，约为 `same_table` 的一半。

### 4.6 批量恢复

**场景。** 按 cohort 取出 `main` 的 160 个对象、128,450,560 字节（原理与设计第 9.6 节）。

**结果（主机 A 实测）。**

| 引擎 | 布局 | 应用可用 p50 | 查询完成 | 客户端恢复 | 校验 | 访问路径 |
|---|---|---:|---:|---:|---:|---|
| XStore | `same_table` | 1,541.9 ms | 1,118.5 ms | 77.2 ms | 338.9 ms | Seq Scan `events` |
| | `separate` | 1,406.5 ms | 985.3 ms | 76.8 ms | 333.0 ms | Seq Scan `events_analytics` + 主键 Nested Loop |
| | `full_core` | 1,530.5 ms | 1,108.6 ms | 76.9 ms | 332.4 ms | Seq Scan `events_full` |
| | `asset_ref` | **835.5 ms** | 45.7 ms | 455.6 ms | 328.6 ms | Seq Scan `events_analytics` + 160 次目录查询 |
| ClickHouse | `same_table` | 2,301.6 ms | 1,136.5 ms | 712.1 ms | 346.6 ms | 6/6 part，读取 138.5 MB |
| | `separate` | 2,731.2 ms | 1,581.2 ms | 697.6 ms | 343.0 ms | 两表整表读取，141.6 MB |
| | `full_core` | 2,325.4 ms | 1,174.4 ms | 705.9 ms | 343.5 ms | 5/5 part，138.6 MB |
| | `asset_ref` | **1,604.2 ms** | 35.0 ms | 1,224.1 ms | 343.6 ms | 读取 10.3 MB 引用 + 160 个对象 |

**分析。** 两个引擎上 `asset_ref` 都最快，数据库只返回引用，对象由本机文件系统读取。XStore 库内布局的查询完成约 1.0–1.1 s，包括服务端顺序扫描与驱动读取 128 MB；ClickHouse 库内布局的客户端恢复约 0.7 s，来自 JSON 解析与字节还原。校验在全部布局上约 330–350 ms，是 128 MB 的 SHA-256 与内容比对。

### 4.7 等总字节控制

**场景。** 两个 workload 的表内载荷原始字节同为 83,886,080：40 个 2 MiB 对象，或 1,280 个 64 KiB 对象；批量查询按 workload 名过滤（原理与设计第 9.7 节）。

**结果（主机 A 实测）。**

| 引擎 | 布局 | 40 × 2 MiB | 1,280 × 64 KiB | 组间比值 |
|---|---|---:|---:|---:|
| XStore | `same_table` | 1,053.2 ms | 1,016.9 ms | 0.97 |
| | `separate` | 967.4 ms | 952.8 ms | 0.98 |
| | `full_core` | 1,056.4 ms | 1,033.3 ms | 0.98 |
| | `asset_ref` | **517.6 ms** | **2,037.1 ms** | 3.94 |
| ClickHouse | `same_table` | 1,651.5 ms | 1,587.9 ms | 0.96 |
| | `separate` | 1,709.2 ms | 1,750.3 ms | 1.02 |
| | `full_core` | 1,685.7 ms | 1,604.4 ms | 0.95 |
| | `asset_ref` | **664.1 ms** | **11,693.1 ms** | 17.61 |

`asset_ref` 每轮的请求数由 205 增至 6,405（5 个样本 × 每样本 1 次数据库查询加每对象 1 次目录查询），客户端恢复由 241.5 ms 增至 1,676.4 ms（XStore）、由 399.8 ms 增至 11,411.7 ms（ClickHouse）。

**分析。** 库内布局对对象数不敏感，组间比值为 0.95–1.02。`asset_ref` 的成本由对象数决定：1,280 个对象时，每个对象的目录查询与文件读取在 ClickHouse 上约 8.9 ms，在 XStore 上约 1.3 ms。等总字节下 `asset_ref` 由最快变为最慢，组内最大最小比在 XStore 上为 2.14，在 ClickHouse 上为 7.36，方向与 x86 实测一致（3.567 与 7.554）。

### 4.8 混合负载与读取隔离

**场景。** 单个引擎、单个布局内，前台 `list:first` 与 `preview:first` 各 20 次每秒、各 2 个 worker，依次叠加 2 MiB 详情、长 Trace、循环批量恢复与持续写入四种干扰流，每阶段 30 秒预热加 300 秒测量（原理与设计第 9.8 节）。本节数据受第 3.3 节的客户端进程争用影响，只作方向判断。

**前台 list 结果（主机 A 实测，每阶段调度 6,000 个请求）。**

| 引擎 | 布局 | quiet p50 / p95 / 丢弃 | detail_2m | trace_long | batch_loop | continuous_ingest |
|---|---|---|---|---|---|---|
| XStore | `same_table` | 59.7 / 84.4 / 121 | 59.5 / 82.1 / 96 | 59.5 / 95.7 / 159 | 79.5 / 412.9 / 2,134 | 61.5 / 120.1 / 405 |
| | `separate` | 58.6 / 67.9 / 51 | 57.9 / 73.7 / 82 | 58.6 / 84.0 / 106 | 68.1 / 407.1 / 1,955 | 60.0 / 116.5 / 353 |
| | `full_core` | 58.9 / 67.1 / 56 | 58.1 / 87.3 / 131 | 58.1 / 74.5 / 96 | 65.7 / 391.2 / 1,796 | 59.4 / 118.5 / 388 |
| | `asset_ref` | 59.8 / 92.6 / 159 | 63.1 / 157.9 / 797 | 61.7 / 147.6 / 639 | 149.4 / 214.7 / 2,594 | 61.1 / 141.1 / 656 |
| ClickHouse | `same_table` | 26.3 / 35.0 / 3 | 26.6 / 36.9 / 2 | 26.0 / 35.8 / 3 | 28.3 / 212.7 / 1,725 | 22.5 / 35.3 / 3 |
| | `separate` | 37.6 / 47.0 / 2 | 36.3 / 47.8 / 2 | 36.2 / 47.0 / 4 | 36.9 / 216.4 / 1,528 | 24.1 / 41.4 / 1 |
| | `full_core` | 38.4 / 48.6 / 2 | 36.4 / 47.4 / 2 | 35.9 / 46.3 / 3 | 38.5 / 244.1 / 1,811 | 37.1 / 47.5 / 5 |
| | `asset_ref` | 37.6 / 47.0 / 3 | 38.2 / 48.8 / 5 | 38.7 / 48.7 / 3 | 38.4 / 48.9 / 4 | 38.4 / 48.5 / 4 |

时延单位为 ms。前台 preview 的结果与 list 同量级，逐阶段数值见回传目录的 `feedback.txt` B3 项。

**干扰流自身（主机 A 实测，p50）。** `batch_loop`：XStore 1,648.1–1,801.4 ms，300 秒内完成 164–178 次；ClickHouse 2,087.5–2,911.5 ms，完成 103–140 次，`asset_ref` 最快。持续写入的单 block 写入：XStore 74.1–121.5 ms，ClickHouse 27.5–55.0 ms。

**丢弃原因（主机 A 实测）。** XStore 的丢弃合计 99.8% 为到达时两个 worker 都在忙，逐流最低为 96.4%。ClickHouse `batch_loop` 阶段的库内布局中，调度线程迟到超过 50 ms 的占 51%–56%（例如 `same_table` 1,725 次中 958 次）。

**分析。**

1. **批量读取的干扰方向。** 两个引擎的库内布局在 `batch_loop` 阶段都出现大量丢弃与 p95 上升；三种库内布局之间差异不大，分表没有减轻干扰。ClickHouse `asset_ref` 的前台在全部阶段与 quiet 持平，`batch_loop` 只丢弃 4 次，方向与 x86 实测一致。
2. **客户端争用的份额。** ClickHouse 丢弃中过半是调度线程自身迟到，该部分来自客户端进程；库内布局的批量结果需要在同一进程内解析约 128 MB 的 JSON，`asset_ref` 的批量结果只有引用，客户端工作量的差别与数据库负载的差别方向相同，本轮数据分不开两者。XStore quiet 阶段的前台查询完成已是主矩阵的约 2.5 倍，主矩阵中同一语句的服务端执行不足 1 ms，前台四个 worker 与调度线程共享同一进程，是已识别的客户端因素。XStore `asset_ref` 在 `detail_2m`、`trace_long`、`batch_loop` 阶段丢弃最多，解析器的文件读取与校验同样运行在该进程内。因此本节不给出数据库层面的隔离结论。
3. **持续写入。** ClickHouse 在 1 block 每秒的写入下前台不受影响；XStore 丢弃增至 353–656 次，p95 升至 116.5–141.1 ms。

前台与干扰流分进程运行后，本节按同一阶段设计重测，隔离性结论以重测为准。

### 4.9 ClickHouse part 状态控制

**场景。** 对受控表构造碎片态、合并中、自然稳定态与单 part 态，每个状态采样 `main` 的 12 个查询目标各 30 次（原理与设计第 5.5、9.9 节）。

**结果（主机 A 实测，`list:first` 应用可用 p50 与受控表 part 数）。**

| 布局 | 碎片态 | 合并中 | 自然稳定态 | 单 part 态 | 碎片态 / 单 part 态 |
|---|---:|---:|---:|---:|---:|
| `same_table` | 89.4 ms（190） | 22.6 ms（190，merge 6） | 25.6 ms（6） | 16.0 ms（1） | 5.59 |
| `separate` | 88.8 ms（190） | 28.8 ms（190，merge 5） | 29.9 ms（3） | 30.3 ms（1） | 2.93 |
| `full_core` | 89.0 ms（190） | 29.8 ms（190，merge 7） | 29.0 ms（3） | 31.1 ms（1） | 2.86 |
| `asset_ref` | 88.7 ms（190） | 29.4 ms（190，merge 2） | 33.2 ms（3） | 33.3 ms（1） | 2.66 |

括号内为采样前证明状态时的受控表 active part 数与活跃 merge 数。

**分析。** 碎片态在四种布局上都约 89 ms，190 个 part 的定位与打开成本主导查询，布局差异消失（最大最小比 1.008）。合并中状态的时延已接近自然稳定态；该状态只在采样前证明一次，采样期间 merge 的进度未记录。单 part 态下 `same_table` 为 16.0 ms，其余布局 30.3–33.3 ms，granule 划分的差异在单 part 下最显著（`events` 13 个 mark，窄表 7 个）。

`separate` 的详情与 Trace 在碎片态最快、单 part 态最慢（`text_64k` 为 81.6 ms 对 163.6 ms），右表整表读取在 part 更多时耗时更短；本轮证据未确定其原因。

### 4.10 对象故障与恢复

**场景。** 对 `asset_ref` 注入六种故障（原理与设计第 6.6、9.10 节）。

**结果（主机 A 实测，两个引擎相同）。**

| 用例 | 注入点 | 解析器分类 | 目录终态 | 事件可见 | 孤儿数（恢复前→后） | 恢复动作 |
|---|---|---|---|---|---|---|
| missing | 删除已发布对象 | missing | available | 是 | 0→0 | 恢复缺失对象 |
| corrupt | 修改已发布字节 | corrupt | available | 是 | 0→0 | 替换损坏对象 |
| metadata_mismatch | 替换目录元数据 | metadata_mismatch | available | 是 | 0→0 | 恢复目录元数据 |
| upload_then_db_failure | 对象上传后数据库写入失败 | missing | absent | 否 | 1→0 | 删除孤儿对象 |
| publish_failure | 发布过程失败 | failed | failed | 是 | 0→0 | 确认发布失败状态 |
| delete_failure | 删除对象时失败 | deleting | deleting | 是 | 0→0 | 确认删除失败状态 |

**分析。** 六个用例在两个引擎上的分类、状态转换与孤儿处理全部符合设计，执行错误为 0。

## 5. 跨场景分析

### 5.1 布局选择（主机 A）

| 工作负载 | XStore | ClickHouse 23.3 | 依据 |
|---|---|---|---|
| 写入 | `same_table` 与 `asset_ref` 未分辨 | `same_table` | 第 4.1 节写入合计 |
| 库内空间 | `asset_ref` | `asset_ref` | 第 4.1 节 |
| 总物理占用 | **`asset_ref`**（0.787 倍） | `same_table` | XStore 库内不压缩载荷 |
| 列表与预览第一页 | 四布局未分辨 | `same_table` | 第 4.2、4.3 节 |
| 需要回表大量行的列表 | 避开 `same_table` | 不适用 | 第 4.2 节中间页 |
| 单条详情 | `same_table` 与 `full_core` | 小对象 `same_table` 与 `full_core`，2 MiB `asset_ref`；避开 `separate` | 第 4.4 节 |
| 完整 Trace | `same_table` 与 `full_core` | `asset_ref`；避开 `separate` | 第 4.5 节 |
| 批量恢复（大对象） | `asset_ref` | `asset_ref` | 第 4.6、4.7 节 |
| 批量恢复（大量中等对象） | 库内布局；避开 `asset_ref` | 库内布局；避开 `asset_ref` | 第 4.7 节 |

### 5.2 长载荷对非载荷查询的影响

两条影响途径的机制见原理与设计第 3.6 节。

| 途径 | 引擎 | 比较 | 主机 A 结果 | 判断 |
|---|---|---|---|---|
| 存放 | XStore | 列表第一页四布局 | 28.2–28.6 ms，均回表 256 行 | 未分辨 |
| 存放 | XStore | 回表 13,908 行的中间页 | `same_table` 124.7 ms，其余 58.2–59.1 ms | 主关系中的载荷使回表读取变慢，为其 2.1 倍 |
| 存放 | XStore | 写入水位的全表聚合 | 每 block 192.5–209.1 ms 对 54.2 ms | 扫描含载荷 heap 的代价约为窄表的 3 倍 |
| 存放 | ClickHouse | 列表第一页四布局 | `same_table` 23.8 ms，其余 34.5–35.4 ms | 含载荷表 granule 更窄，读取更少 |
| 存放 | ClickHouse | part 状态 | 同状态布局比 1.008–2.08，同布局状态比 2.66–5.59 | part 状态影响更大 |
| 并发 | 两个引擎 | 混合负载 | 见第 4.8 节 | 分表未减轻批量读取的干扰；幅度待分进程重测 |

对 XStore 的结论是：主关系中的载荷不影响只按索引取少量行的查询，影响需要逐行访问大量行的查询，包括回表过滤、顺序扫描与聚合。第一页列表、详情与 Trace 属于前者，批量恢复、水位聚合与中间页的现有写法属于后者。把载荷移出主表（`separate`、`full_core`、`asset_ref`）消除后者的额外成本；修正游标写法同样消除中间页的这部分成本。ClickHouse 的列裁剪使列表不读取载荷列，同表载荷只通过 granule 划分改变读取量，方向对 `same_table` 有利。

### 5.3 外置对象的成本转移

| 指标 | 库内布局 | `asset_ref` | 引擎 |
|---|---:|---:|---|
| 总物理占用 | 219.82 MB（`same_table`） | 173.02 MB | XStore |
| 总物理占用 | 19.03 MB（`same_table`） | 131.73 MB | ClickHouse |
| 批量恢复查询完成 | 985.3–1,118.5 ms | 45.7 ms | XStore |
| 批量恢复客户端恢复 | 76.8–77.2 ms | 455.6 ms | XStore |
| 批量恢复（40 × 2 MiB） | 967.4–1,056.4 ms | 517.6 ms | XStore |
| 批量恢复（1,280 × 64 KiB） | 952.8–1,033.3 ms | 2,037.1 ms | XStore |
| 批量恢复（1,280 × 64 KiB） | 1,587.9–1,750.3 ms | 11,693.1 ms | ClickHouse |
| 写入合计 | 4,490.5–6,692.8 ms | 7,585.3 ms | ClickHouse |

转移方向在两个引擎上相同：数据库查询时间下降，客户端恢复时间与每对象固定成本上升。空间的方向取决于引擎是否压缩库内载荷：XStore 外置后总占用下降，ClickHouse 上升。

### 5.4 与 x86 参照结果的方向对照

| 结论 | 主机 A | x86 | 方向 |
|---|---|---|---|
| ClickHouse 空间倍数（`separate`、`full_core`、`asset_ref`） | 1.097、1.170、6.921 | 1.097、1.170、6.921 | 相同 |
| 行存 `asset_ref` 总物理占用 | XStore 0.787 倍 | openGauss 2.884 倍 | 相反，来自是否压缩 |
| ClickHouse `same_table` 列表读取更少 | 10,541 对 32,768 行 | 12,147 对 38,912 行 | 相同 |
| ClickHouse `separate` 右表整表读取 | 详情与 Trace 都整表读取 | 只在 Trace 整表读取；详情下推 `event_id`，扫描 7,859 行 | 详情不同，来自版本 |
| 等总字节下 `asset_ref` 由最快变最慢 | 两个引擎 | 两个引擎 | 相同 |
| part 状态影响大于布局 | 状态比 2.66–5.59 | 2.90–3.27 | 相同 |
| 行存列表中间页 | XStore 游标写法下回表过滤 13,652 行 | openGauss 精确扫描 256 行 | 写法不同 |

## 6. 原理与设计文档待实测项（主机 A）

| 项 | 主机 A 结果 | 证据 |
|---|---|---|
| XStore 大值的存放与压缩 | 留在主关系内，未压缩 | 第 4.1 节：`reltoastrelid`、TOAST 分项、`pg_column_size` 与 `octet_length`、heap 增量 |
| XStore 展开式游标能否作为索引范围起点 | 不能；增加被蕴含的下界后可以 | 行存探针两种写法的计划；主矩阵 `Rows Removed by Filter: 13652` |
| XStore 计划的缓冲证据 | 计划中无 Buffers 行 | 主矩阵计划文本 |
| ClickHouse 23.3 上 `separate` 右表的读取范围 | 详情与 Trace 都整表读取右表 48,534 行 | 第 4.4、4.5 节 |
| ClickHouse 23.3 上各表的 granule 划分 | 末轮存储：`events` 6 part、21 mark，窄表 5–6 part、15–17 mark | 第 4.1、4.2 节 |
| 行存持续写入阶段的写入代价 | XStore 单 block 74.1–121.5 ms | 第 4.8 节 |
| XStore 同表载荷对列表与预览的影响幅度 | 第一页未分辨；回表大量行时 2.1 倍 | 第 4.2、5.2 节 |
| 两个引擎上并发载荷读写对前台的干扰幅度 | 待分进程重测 | 第 4.8 节 |

## 7. 限制与后续

1. **主机背景负载。** 主机 A 运行期间有与本实验无关的 gaussdb 实例，负载高于反馈契约阈值。本报告以轮间波动作为分辨阈值，XStore 约 5%–12%，ClickHouse 约 11%–36%，低于该量级的布局差异记为未分辨。
2. **XStore 中间页游标。** 工具包修正为增加被蕴含下界的写法后，XStore 的主矩阵需要重跑，届时更新第 4.2、4.3 节的中间页结果。
3. **XStore 客户端驱动。** XStore 小查询的绝对耗时主要由驱动与连接建立构成；工具包优化驱动后，XStore 的主矩阵重跑，跨引擎的小查询比较以重跑结果为准。
4. **混合负载的客户端进程。** 前台与干扰流分进程运行后，两个引擎的混合负载重跑，第 4.8 节的隔离性结论以重跑为准。
5. **单台主机。** ARM 主机 B 的结果回传后，逐台报告并核对方向。
6. **输入边界。** 载荷行占 0.33%，数据集可完全驻留内存，只覆盖写入与读取，不覆盖更新、删除与冷缓存（原理与设计第 2.5 节）。

## 8. 使用建议（主机 A）

以下建议基于主机 A 的结果，ARM 主机 B 的结果到齐后复核。

- **XStore 上优先保持载荷与普通列分表或外置。** XStore 把载荷不压缩地存放在主关系内，需要逐行访问大量行的查询都会读到这些字节；`separate`、`full_core` 与 `asset_ref` 把列表、分页与聚合路径限制在 11 MB 量级的窄表上。
- **XStore 上的分页查询使用能作为索引范围起点的游标写法。** 在行构造器比较展开式之前加入被蕴含的下界 `start_time >= cursor_time`。
- **大对象为主、批量导出频繁时选择 `asset_ref`。** 两个引擎上批量恢复都最快，XStore 上总占用也最低；对象数量大、单对象小时避免 `asset_ref`，每对象的目录查询成本随对象数线性增长，在 ClickHouse 上尤甚。
- **ClickHouse 上避免 `separate` 的连接查询形态。** 右表缺少等价谓词时每次整表读取；需要分表时使用 `full_core` 或 `asset_ref`。
- **ClickHouse 上优先控制 part 数。** part 状态的影响是布局差异的数倍。

## 参考资料

- [阶段三原理与设计](json-storage-stage3-principles-design-2026-09-24.md)
- [阶段三实验报告（openGauss 与 ClickHouse 25.12）](json-storage-stage3-report-2026-09-20.md)
- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段三实验设计与证据契约](json-storage-stage3-experiment-design-2026-09-09.md)
