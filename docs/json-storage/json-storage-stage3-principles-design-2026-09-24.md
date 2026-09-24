# Agent Trace JSON 存储阶段三原理与设计：长载荷的四种布局

> 文档日期：2026-09-24
>
> 适用版本：XStore（GaussVector 103.0.0 release 构建）、openGauss 6.0.0、ClickHouse 23.3.10.5 与 25.12.11.4
>
> 对象：`same_table`、`separate`、`full_core`、`asset_ref` 四种长载荷布局，及其在行存与列存引擎中的实现

本文解释阶段三实验要比较的对象、这些对象在两类存储引擎中的物理组织和读写流程，以及实验场景的设计依据。它是阶段三各份实验报告的共同原理与设计基础，可独立阅读；实测数值、结果分析与结论写在实验报告中。JSONB、TOAST、MergeTree data part 与 merge 的通用机制见 [JSON 存储原理](json-storage-principles-2026-09-09.md)，本文只展开与长载荷位置有关的部分。

文中的事实分三类，并就近标明：一是冻结输入与实验程序的契约，出处为输入清单与代码；二是已在 x86 主机验证的机制，引用[阶段三实验报告（openGauss 与 ClickHouse 25.12）](json-storage-stage3-report-2026-09-20.md)的章节；三是只能由 ARM 主机实测确定的事实，集中列在第 12 章，由实验报告回填。

字节量按二进制单位表示，1 KiB 为 1,024 字节，1 MiB 为 1,048,576 字节。

## 1. 背景与问题

### 1.1 Agent Trace 中的长载荷

Agent Trace 以 Span 为基本记录。一条 Span 由标识（`event_id`、`trace_id`、`span_id`、`parent_span_id`）、项目与时间、类型与状态等普通列组成；其中一部分 Span 还携带模型输入输出、工具调用结果或检索文档这类长文本，本文称为**载荷**（payload）。载荷的分布有两个特点：大多数 Span 不携带载荷，携带载荷的 Span 单条可达数百 KiB 到数 MiB。

应用对同一批 Span 有性质不同的访问：

| 访问形态 | 典型用途 | 是否需要载荷 |
|---|---|---|
| 分页列表 | 按项目与时间浏览 Span 时间线 | 不需要 |
| 预览 | 列表中附带载荷开头的一小段文本 | 只需要固定长度的预览 |
| 单条详情 | 打开一条 Span 查看完整内容 | 需要一个完整载荷 |
| 完整 Trace | 展开一条 Trace 的全部 Span | 需要该 Trace 的全部载荷 |
| 批量恢复 | 导出、离线分析或重放 | 需要一批完整载荷 |
| 持续写入 | 在线采集 | 写入全部列 |

载荷放在哪里，决定了这些访问各自读取多少数据、写入需要几步、空间如何计算，以及数据库之外的组件要承担多少一致性责任。

### 1.2 研究问题

阶段三比较四种组织同一份逻辑记录的布局，回答四个问题：

1. 长载荷是否、在哪些场景、以多大幅度影响不读取载荷的列表与预览查询：同表存放的载荷是否进入列表的读取路径，并发的载荷读写是否干扰前台查询（第 3.6 节）；
2. 物理分层带来的写入、后台维护、空间与详情恢复成本；
3. 载荷的大小、出现分布与可压缩性如何改变各布局的成本；
4. 把载荷外置为数据库之外的对象后，能否正确恢复内容，并在缺失、损坏和部分失败时保持明确状态。

### 1.3 与前两个阶段的关系

阶段一和阶段二研究 Span 属性的 JSON 表示，即 openGauss JSONB 与 ClickHouse Native JSON 在路径查询与文档恢复上的差异。阶段三沿用阶段二的普通列、Trace 结构、写入 block 与查询窗口，只把长载荷的物理位置作为变量。载荷在数据库内以保持 UTF-8 字节的 `TEXT`（行存）或 `String`（ClickHouse）保存，不使用 JSON 类型，也不查询载荷内部路径。

### 1.4 被测引擎与存储模型

| 引擎 | 存储模型 | 版本 | 运行位置 | 角色 |
|---|---|---|---|---|
| XStore | 行存：行式表加 B-tree 索引 | GaussVector 103.0.0 release 构建 | ARM 主机 A、ARM 主机 B | 主对照的行存引擎 |
| ClickHouse | 列存：MergeTree data part | 23.3.10.5 | ARM 主机 A、ARM 主机 B | 主对照的列存引擎 |
| openGauss | 行存：heap 表加 B-tree 索引，大值经 TOAST 处理 | 6.0.0 | x86 主机 | 参照对照的行存引擎 |
| ClickHouse | 列存：MergeTree data part | 25.12.11.4 | x86 主机 | 参照对照的列存引擎 |

XStore 是 GaussDB 系的行存引擎，SQL 接口与系统目录与 openGauss 同源；实验适配器复用 openGauss 的建表、写入与查询逻辑，差异见第 4.5 节。每台主机上的两个引擎构成一组同机对照。

两类存储模型对载荷位置的敏感点不同。行存以整行为读写单位，载荷是否留在主行内、是否被压缩或移出，决定读取普通列时要经过多少页；列存以列为读写单位，查询不投影载荷时不读取载荷列的数据流，载荷位置的影响转到排序键裁剪、part 数和多表连接上。第 4 章与第 5 章分别展开。

### 1.5 运行与计时术语

一个引擎的一种布局在一个 workload 上的一次完整运行称为一个**轮次**；两个引擎、四种布局在三个性能 workload 上的四轮组合称为**主矩阵**。一个**查询目标**是按 `查询形态:变体` 命名的固定查询，例如 `list:middle`、`detail:text_2m`，全部目标见第 9 章。

载入侧有两个阶段，与具体查询样本无关：

| 阶段 | 含义 |
|---|---|
| **写入完成** | 190 个 block 全部写入且每个 block 达到联合水位（第 3.3 节）；时间覆盖客户端行构造、数据库协议、双写、对象发布与目录状态转换 |
| **查询就绪** | 在写入完成基础上完成查询前维护：行存 `ANALYZE`，ClickHouse 等待 merge 收敛；维护时间计入写入完成到查询就绪的区间 |

查询侧每个样本记录三个分项和一个总量：

| 分项 | 含义 |
|---|---|
| **查询完成** | 首次数据库查询的响应被客户端完整读取；`asset_ref` 此时只取得事件引用 |
| **客户端恢复** | 结果规范化；`asset_ref` 的目录查询、对象读取与装配也在此阶段 |
| **正确性校验** | 长度、SHA-256 与内容比对 |
| **应用可用** | 从请求提交到校验完成的样本级总时间 |

## 2. 输入数据与载荷模型

### 2.1 基础记录

基础记录来自公开数据集 `Leoxx/whowhen_pro` 的 text split，经确定性 Trace 投影得到 48,534 条 Span，与阶段二使用同一份冻结输入。记录按 `ingest_seq` 排序，每 256 行构成一个写入 block，共 190 个 block，最后一个 block 为 150 行。

### 2.2 载荷生成

生成器以种子 `20260907` 为 1,481 条记录生成载荷，每个载荷对应一条记录，内容互不重复。载荷分为五种 **profile**：

| profile | 单条字节 | 内容 | 用途 |
|---|---:|---|---|
| `text_64k` | 65,536 | 可压缩文本 | 小型长值 |
| `text_512k` | 524,288 | 可压缩文本 | 中型详情 |
| `text_2m` | 2,097,152 | 可压缩文本 | 大型详情与外置候选 |
| `entropy_512k` | 524,288 | 高熵 ASCII 文本 | 与 `text_512k` 等长的压缩机制对照 |
| `unicode_boundary` | 1,024 | 截断处含多字节字符 | 只用于正确性门禁 |

可压缩文本由一个 116 字节的基本块重复到目标长度构成，基本块含 64 个字符的 SHA-256 标记和一句固定短语，zlib-6 压缩比为 191 至 290 倍。高熵内容由种子、事件标识与计数器经 SHA-256 扩展生成，压缩比约 1.3 倍。生成器调整尾部，使每个载荷的 UTF-8 字节精确等于目标长度。

每条载荷另存三项元数据：`content_type` 固定为 `application/json`，`encoding` 固定为 `utf-8`，`preview` 为载荷开头的 200 个 Unicode code point。`content_length` 与 `sha256` 由原始字节计算。

### 2.3 cohort 与 workload

载荷分属三个 **cohort**，即一组在同一次运行中写入表内的载荷：

| cohort | 对象数 | 原始字节 | profile 构成 |
|---|---:|---:|---|
| `main` | 160 | 128,450,560 | 四种性能 profile 各 40 |
| `equal_total_control` | 1,320 | 167,772,160 | `text_2m` 40 与 `text_64k` 1,280 |
| `correctness_only` | 1 | 1,024 | `unicode_boundary` 1 |

一次运行称为一个 **workload**，每个 workload 只写入所属载荷，其余记录的载荷及其内容元数据为空：行存写 SQL NULL，ClickHouse 的 `payload` 列声明为非空 `String`，写空字符串，其余内容元数据列写 NULL。`equal_total_control` 拆为两个 workload：`equal_total_few_large` 只写入 40 个 `text_2m`，`equal_total_many_medium` 只写入 1,280 个 `text_64k`，两者原始总字节同为 83,886,080。载入时这些载荷行的 `cohort` 列取 workload 名，批量查询按 workload 名过滤。

| workload | 表内载荷对象数 | 表内载荷原始字节 |
|---|---:|---:|
| `main` | 160 | 128,450,560 |
| `equal_total_few_large` | 40 | 83,886,080 |
| `equal_total_many_medium` | 1,280 | 83,886,080 |
| `correctness_only` | 1 | 1,024 |

空间比值的分母取当前 workload 的表内载荷原始字节。`main` 中携带载荷的行占 160/48,534，即 0.33%。

### 2.4 查询参数

全部查询参数来自冻结的 **truth** 清单，即独立程序在实验前按冻结输入计算的预期结果；实验查询不参与 truth 的生成。

列表与预览固定在项目 `Leoxx/whowhen_pro` 的时间窗口 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)` 内，窗口有 27,561 行，分页大小为 256。第一页使用窗口起点之前的哨兵游标，中间页使用窗口中部的固定游标。

单条详情按四种性能 profile 各固定一条记录。完整 Trace 按 Span 数固定三条：

| 标识 | Span 数 | `main` 中的载荷对象数 | 载荷原始字节 | profile 构成 |
|---|---:|---:|---:|---|
| `p25` | 5 | 1 | 65,536 | `text_64k` 1 |
| `p50` | 6 | 1 | 524,288 | `text_512k` 1 |
| `p95` | 31 | 2 | 2,162,688 | `text_2m` 1、`text_64k` 1 |

冻结输入中 `p95` 另有 2 个 `text_64k` 载荷属于 `equal_total_control`，在 `main` workload 中写为空值。

批量恢复按 cohort 取出全部携带载荷的行。

### 2.5 代表性与边界

输入保留了真实 Trace 的结构、时间分布与 Span 数分布，并在其上确定性地放置载荷，使四种布局在同一行集、同一行序上比较。载荷大小覆盖三个数量级，可压缩与高熵两类内容分离了压缩的作用，等总字节的两组 workload 分离了对象数量与单对象大小。

输入有四项边界：

| 边界 | 影响 |
|---|---|
| 可压缩文本的压缩比远高于生产文本常见的 3 至 6 倍 | 放大 ClickHouse 列压缩与 openGauss TOAST 压缩的空间收益，空间结论按压缩比分开解释 |
| 载荷密度为 0.33% | 结论适用于少数 Span 携带长载荷的数据集，不外推到高密度数据 |
| 48,534 行、约 122.5 MiB 载荷 | 数据集可完全驻留内存，不覆盖超出单机内存与跨节点的规模 |
| 单项目、固定窗口 | 不覆盖多租户与跨窗口访问 |

## 3. 四种布局

### 3.1 逻辑记录与统一返回契约

四种布局保存同一份逻辑记录：

```text
event_id, trace_id, project_id, start_time, profile,
content_type, encoding, content_length, preview, sha256, payload
```

其中 `profile`、`content_type`、`encoding`、`content_length`、`preview` 与 `sha256` 合称**内容元数据**。同一查询在四种布局上返回相同的行、相同的行序、相同的列和相同的载荷字节。列表查询把 `preview` 与 `payload` 投影为类型化 NULL，预览查询只把 `payload` 投影为 NULL，详情、Trace 与批量查询返回完整载荷。全部载荷在客户端完成长度与 SHA-256 校验后才视为可交付，服务端摘要不能代替内容传输。

### 3.2 布局定义

| 布局 | 写目标 | 列表与预览读取 | 详情、Trace 与批量读取 | 载荷位置 |
|---|---|---|---|---|
| `same_table` | `events` | `events` | `events` | 与普通列同表 |
| `separate` | `events_analytics`、`event_payloads` | `events_analytics` | 两表按 `event_id` 连接 | 独立的载荷表 |
| `full_core` | `events_full`、`events_core` | `events_core` | `events_full` | 全量表；另有一份不含载荷的列表副本 |
| `asset_ref` | `events_analytics`、`assets`，以及数据库外的对象存储目录 | `events_analytics` | 读取引用后由解析器读取对象 | 数据库外的内容寻址文件 |

布局名称描述逻辑 schema，载荷字节的实际物理位置由引擎决定（第 4、5 章）。四种布局包含的对象与每行的列组成如下；行存以 schema、ClickHouse 以 database 作为每次运行的命名空间：

```text
same_table
└── events：普通列 + 内容元数据 + payload

separate
├── events_analytics：普通列 + 内容元数据（无 payload）
└── event_payloads：定位键（event_id、trace_id、project_id、start_time）+ 内容元数据 + payload

full_core
├── events_full：普通列 + 内容元数据 + payload
└── events_core：与 events_full 相同的普通列与内容元数据（无 payload）

asset_ref
├── events_analytics：普通列 + 内容元数据 + asset_id
├── assets（目录表）：asset_id、sha256、content_type、encoding、content_length、storage_path、status、updated_at、error_category
└── 对象存储目录：<root>/<sha256 前两位>/<sha256>
```

`separate` 与 `full_core` 的两张表都保存全部 48,534 行，没有载荷的行在载荷表或全量表中按第 2.3 节写空值。`asset_ref` 的对象存储目录只保存当前 workload 的载荷对象。

### 3.3 写入路径与联合水位

四种布局按同一顺序写入 190 个 block。每个 block 写入后，程序对该布局的每个写目标查询已可见的 `MAX(ingest_seq)+1`；`asset_ref` 的目录表另要求被事件行引用的目录行全部为 `available`。全部写目标都达到本 block 末行的 `ingest_seq + 1`，称该 block 达到**联合水位**，然后才提交下一个 block。联合水位使每个查询开始时各写目标的数据处于同一个 block 边界，多表布局的查询因此看到一致的状态；它是实验的同步屏障，生产系统的异步采集不在写入之间等待可见性。

| 布局 | 每个 block 的写入步骤 |
|---|---|
| `same_table` | 写入 `events` |
| `separate` | 写入 `events_analytics`，再写入 `event_payloads` |
| `full_core` | 写入 `events_full`，再写入 `events_core` |
| `asset_ref` | 第 6.4 节的四步：登记、发布、转为可用、写入引用行 |

`full_core` 由实验程序显式双写两张表，不使用物化视图或触发器。行存在一个事务内写完一个 block 的两个写目标；ClickHouse 对两个写目标各执行一次 INSERT，两次写入之间没有事务，一致性由联合水位保证。每个 block 记录各写目标的写入时间（`write_target_ms`）、`asset_ref` 的对象发布时间（`asset_publish_ms`）与达到联合水位时的可见值，这些字段是第 9.1 节解释写入差异的证据。

### 3.4 读取路径

| 查询 | `same_table` | `separate` | `full_core` | `asset_ref` |
|---|---|---|---|---|
| 列表、预览 | `events` 投影普通列 | `events_analytics` | `events_core` | `events_analytics` |
| 单条详情 | `events` 按四个等值条件取一行 | `events_analytics` 左连接 `event_payloads` | `events_full` | `events_analytics` 取引用，逐对象查 `assets` 后读文件 |
| 完整 Trace | `events` 按 Trace 与时间范围取多行 | 同上的左连接 | `events_full` | 同上，每个载荷对象一次目录查询与一次文件读取 |
| 批量恢复 | `events` 按 cohort 过滤 | 左连接 | `events_full` | 同上 |

### 3.5 成本转移

| 布局 | 额外写入 | 额外空间 | 列表路径的收益来源 | 详情路径的额外工作 | 一致性责任 |
|---|---|---|---|---|---|
| `same_table` | 无 | 无 | 取决于引擎能否避开载荷 | 无 | 数据库 |
| `separate` | 第二张表的行构造与提交 | 载荷表的普通定位列与索引或排序结构 | 列表表不含载荷 | 两表连接 | 数据库内两表的联合水位 |
| `full_core` | 列表副本的行构造与提交 | 完整复制的普通列与索引或排序结构 | 列表表不含载荷 | 无 | 数据库内两表的联合水位 |
| `asset_ref` | 目录表两次写入与对象发布 | 目录表；对象以原始字节保存 | 列表表不含载荷 | 目录查询、文件读取与校验移到客户端 | 应用侧解析器与目录状态 |

该表给出成本出现的位置。成本的大小由引擎的物理机制决定，第 4、5 章说明这些机制，实验报告给出实测值。

### 3.6 长载荷对非载荷查询的影响

列表与预览是本实验中不读取载荷的查询：两者都把 `payload` 投影为 NULL，预览只读取写入时预存的 200 字符 `preview` 列。长载荷通过两条途径影响这两类查询，布局通过改变这两条途径起作用。

| 途径 | 含义 | 行存 | ClickHouse |
|---|---|---|---|
| **存放途径** | 列表读取的表中是否含载荷字节 | 主 tuple 中保留的载荷字节使同样行数分散在更多 heap 页上，沿索引回表与顺序扫描经过的页随之增加；载荷外置后主 tuple 只保留指针（第 4.2 节） | 列裁剪使列表不读取 `payload` 列流；`payload` 列改变 granule 划分，含载荷的表 granule 更窄，一页读入的行数更少（第 5.2 节）；merge 重写含载荷的 part（第 5.5 节） |
| **并发途径** | 同时发生的载荷读写占用共享资源 | 详情、批量读取与写入占用 CPU、I/O、共享缓冲区与连接；同表时载荷页与普通列页使用同一缓冲池 | 批量读取的解压与传输占用 CPU 与内存带宽；持续写入产生新 part 并触发 merge |

四种布局在两条途径上的位置：

| 布局 | 存放途径 | 并发途径 |
|---|---|---|
| `same_table` | 列表表含载荷，影响取决于引擎机制 | 载荷读写与列表作用于同一张表 |
| `separate`、`full_core` | 列表表不含载荷 | 载荷读写作用于另一张表，仍与列表共享同一引擎的 CPU、I/O 与缓冲 |
| `asset_ref` | 列表表不含载荷 | 载荷读取移到文件系统与客户端解析器，数据库只执行目录查询 |

分表只切断存放途径；并发途径只有在载荷读取离开数据库时才被移出，移出的竞争落到文件系统与客户端。两条途径由不同场景测量：

| 途径 | 场景 | 比较方式 | 证据 |
|---|---|---|---|
| 存放 | 列表、预览（第 9.2、9.3 节） | 同一主机、同一引擎、同一轮内 `same_table` 与其余三种布局的应用可用 p50 之比 | 行存：计划的访问节点、`Rows Removed by Filter`、`idx_scan`；ClickHouse：`read_rows`、`read_bytes`、mark 与 granule 数 |
| 存放 | part 状态（第 9.9 节） | 同一状态下四种布局之比，对照同一布局下四种状态之比 | 计划中的 `Parts` 与 `Granules` |
| 并发 | 混合负载（第 9.8 节） | 同一布局各阶段相对 `quiet` 的前台 p50、p95 变化与调度丢弃数 | 样本状态、阶段快照 |

x86 主机已验证的结果（[阶段三实验报告](json-storage-stage3-report-2026-09-20.md)第 4.2、4.8、4.9 节）：

| 引擎 | 途径 | 结果 |
|---|---|---|
| openGauss 6.0.0 | 存放 | 四种布局的列表 p50 最大最小比为 1.069–1.087，预览为 1.009–1.015；列表在四种布局下都只扫描 256 行，同表载荷没有进入列表的读取路径 |
| ClickHouse 25.12 | 存放 | 列表 p50 最大最小比为 1.089–1.140；`list:middle` 在 `same_table` 上读取 23,947 行，其余三种布局读取 38,912 行，同表载荷使列表读取量减少 |
| ClickHouse 25.12 | 并发（前台与干扰流共用一个客户端进程时测得） | `detail_2m` 与 `trace_long` 阶段的前台 p50 相对 `quiet` 变化在 ±4% 以内；`batch_loop` 阶段三种库内布局的前台 list p95 由 18.45–28.19 ms 升至 104.12–124.22 ms，`same_table`、`separate`、`full_core` 的调度丢弃为 477、653、582 次，分表没有减轻干扰；`asset_ref` 的 p95 为 28.14 ms、调度丢弃 2 次 |
| ClickHouse 25.12 | 存放与 part 状态 | 同一 part 状态下四种布局的 `list:first` 之比为 1.114–1.175，同一布局下四种状态之比为 2.90–3.27 |

这些结果成立于本实验的输入与负载边界：载荷行只占 0.33%，数据集可完全驻留内存，前台每条请求流只有 2 个 worker（第 2.5、9.8 节）。载荷密度升高或数据超出内存时，存放途径中的页数增量与缓冲池竞争随之放大。XStore 的大值存放方式决定其存放途径的大小，由第 12 章的实测项确定。

## 4. 行存引擎中的长载荷

本章以 `same_table` 的建表语句为共同实例，适用于 openGauss 与 XStore。

### 4.1 表结构与索引

```sql
CREATE TABLE events (
  ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
  trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
  project_id TEXT NOT NULL, start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
  end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, duration_ms BIGINT NOT NULL,
  span_type TEXT NOT NULL, framework TEXT NOT NULL, level TEXT NOT NULL,
  cohort TEXT, profile TEXT, content_type TEXT, encoding TEXT,
  content_length BIGINT, preview TEXT, sha256 TEXT, payload TEXT);
CREATE INDEX events_list_idx ON events (project_id, start_time, event_id);
CREATE INDEX events_trace_idx ON events (project_id, trace_id, start_time, event_id);
```

按声明顺序说明与载荷有关的部分：

1. `event_id TEXT NOT NULL PRIMARY KEY` 建立唯一 B-tree 索引 `events_pkey`，用于按事件标识定位一行，也用于 `separate` 的两表连接。
2. `payload TEXT` 保存载荷的 UTF-8 文本。`TEXT` 为变长类型，默认存储策略允许压缩与移出主行，见第 4.2 节。
3. `events_list_idx` 的列顺序与列表查询的过滤和排序一致：先按项目等值、再按时间范围、最后按 `event_id` 打破并列，使 keyset 分页可以沿索引顺序读取一页。
4. `events_trace_idx` 服务单条详情与完整 Trace：详情查询给出项目、Trace、时间与事件标识四个等值条件，Trace 查询给出项目与 Trace 等值加时间范围。

因此每张事件表有三个索引：主键加两条复合 B-tree。`separate` 的 `event_payloads`、`full_core` 的两张表都建立同一组索引；`asset_ref` 的 `assets` 以 `asset_id` 为主键。`cohort` 不在任何索引中，批量恢复按 cohort 过滤时由优化器选择顺序扫描。

### 4.2 大值在行存中的存放

行存以 heap tuple 为一行的物理单位，tuple 放在固定大小的页中。openGauss 使用 TOAST 处理大值：一行的预计大小超过 `TOAST_TUPLE_THRESHOLD`（默认 8 KiB 页下为 2,032 字节）时，先用 PGLZ 尝试压缩可变长列，压缩后仍超过目标时把该值切成约 2 KiB 的 chunk 移入该表关联的 TOAST relation，主 tuple 只保留外置指针。机制细节见 [JSON 存储原理](json-storage-principles-2026-09-09.md) 第 2.1 节。

载荷的存放方式决定了读取普通列时经过的数据量：

```text
events 的一行（openGauss，载荷外置）
├── heap tuple：普通列 + 内容元数据 + payload 外置指针
└── TOAST relation：payload 的有序 chunk（压缩后）

events 的一行（载荷留在主行）
└── heap tuple：普通列 + 内容元数据 + payload 全部字节
```

载荷外置时，列表查询沿索引取得 heap tuple 后只读取普通列，不访问 TOAST relation；载荷留在主行时，heap 页同时容纳载荷字节，同样行数的普通列分散在更多页上，沿索引回表读取的页数随之增加。

XStore 的大值存放位置、压缩方式与页组织由实测确定，依据是行存探针（`experiments/json-storage-stage3/tools/probe_row_storage.py`）在完整载入 `main` workload 后采集的三项事实：各表的 `reltoastrelid` 与 `reloptions`，按 profile 分组的 `sum(octet_length(payload))` 与 `sum(pg_column_size(payload))`，以及主矩阵每轮记录的 heap、index 与 TOAST 分项空间。在 openGauss 的语义下，`pg_column_size` 返回值的存储字节：值留在主行且未压缩时为 `octet_length` 加 4 字节头部，值外置且未压缩时等于 `octet_length`，值被压缩时小于 `octet_length`。两个量的比值只区分"压缩"与"未压缩"，值在主行还是外置由 `reltoastrelid` 与 TOAST 分项空间判定。

### 4.3 查询的访问路径

列表查询沿 `events_list_idx` 读取窗口内的索引项，并按 keyset 游标从上一页末行之后继续：

```sql
-- openGauss：行构造器游标
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       NULL::text AS preview, sha256, NULL::text AS payload_value
FROM events
WHERE project_id = %s AND start_time >= %s AND start_time < %s
  AND (start_time, event_id) > (%s, %s)
ORDER BY start_time, event_id LIMIT %s;
```

```sql
-- XStore 第一页：游标为空时省略游标条件
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       NULL::text AS preview, sha256, NULL::text AS payload_value
FROM events
WHERE project_id = %s AND start_time >= %s AND start_time < %s
ORDER BY start_time, event_id LIMIT %s;
```

```sql
-- XStore 中间页：被蕴含的下界加同一游标的展开形式，cursor_time 绑定三次
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       NULL::text AS preview, sha256, NULL::text AS payload_value
FROM events
WHERE project_id = %s AND start_time >= %s AND start_time < %s
  AND start_time >= %s
  AND (start_time > %s OR (start_time = %s AND event_id > %s))
ORDER BY start_time, event_id LIMIT %s;
```

行构造器 `(start_time, event_id) > (c_t, c_id)` 可以直接作为复合索引的范围起点，索引扫描从游标位置开始，读到 256 行即停止。展开式与它逻辑等价，但优化器只有在把 `start_time > c_t OR (start_time = c_t AND …)` 识别为范围条件时才能从游标位置起扫；否则索引扫描从窗口起点 `start_time >= %s` 开始，把游标之前的索引项作为过滤条件逐行丢弃，中间页的读取量随游标位置增长。XStore 适配器在展开式之前增加被它蕴含的下界 `start_time >= c_t`，结果集不变，该下界是普通的范围条件，可以作为 Index Cond 使扫描从游标位置开始。行存探针在 XStore 的 `same_table` 上对 `list:middle` 的两种写法各执行一次 `EXPLAIN (ANALYZE, BUFFERS)`：不带下界的展开式与适配器发出的带下界写法，两份计划记录展开式本身能否作为范围起点，以及下界的作用。

其余查询的访问路径：

| 查询 | 预期访问路径 |
|---|---|
| 单条详情 | `events_trace_idx` 四列等值，取一行 |
| 完整 Trace | `events_trace_idx` 项目与 Trace 等值加时间范围 |
| 批量恢复 | 顺序扫描，按 `cohort` 与 `sha256 IS NOT NULL` 过滤后排序 |
| `separate` 的详情与 Trace | 左表按上述索引取行，右表 `event_payloads` 按 `event_payloads_pkey` 逐行连接 |

### 4.4 查询前维护

写入 190 个 block 后，程序对全部写目标执行 `ANALYZE`，更新优化器统计。该步骤计入查询就绪阶段（第 1.5 节）。

### 4.5 XStore 适配

XStore 适配器继承 openGauss 适配器，只在以下四处改写，改写内容随每次运行的布局定义写入运行清单：

| 项 | 改写 | 对逻辑结果的影响 |
|---|---|---|
| `framework` 列 | 去掉 `NOT NULL`，因为引擎把空字符串视为 NULL | 数据核对时把 NULL 还原为空字符串，逻辑记录不变 |
| keyset 游标 | 非空游标的行构造器比较展开为第 4.3 节的 OR 形式，并在其前增加被蕴含的下界 `start_time >= cursor_time`；空游标（第一页）省略游标条件 | 结果集不变；下界使索引范围从游标处开始 |
| 水位聚合 | `count(*) FILTER (WHERE …)` 改写为 `count(CASE WHEN … END)` | 无 |
| 连接 | 经本机 Unix domain socket 以 trust 认证连接，运行账号由 `XSTORE_USER` 提供 | 无 |

### 4.6 行存的访问证据

一轮的全部正式样本与批量内存诊断结束后，程序对每个成功样本的语句与绑定值重新执行一次 `EXPLAIN (ANALYZE, BUFFERS)`，保存计划文本；随后读取一次 `pg_stat_user_indexes.idx_scan`。该计数是本轮 schema 建立以来的累计值，包含载入、数据核对、预热、正式样本与 EXPLAIN 重跑，用来证明索引被实际使用，不能换算为单个样本的索引访问次数。计划中缺少 Buffers 行时（XStore 的计划不输出该行），访问结构记为"有索引路径、无缓冲证据"。运行清单中的 `scanned_rows` 取自计划文本中第一个 `actual … rows=` 的值，即计划顶层节点的实际输出行数；列表查询的顶层节点是 `Limit`，该值等于返回行数，不等于底层扫描读取的索引项数。底层读取量从计划中的扫描节点与 `Rows Removed by Filter` 读出。行存没有与 ClickHouse `read_bytes` 对应的扫描字节计数，该字段记为 `unavailable`。

### 4.7 行存的影响因素

| 因素 | 对写入与维护的影响 | 对查询的影响 |
|---|---|---|
| 载荷留在主行或外置 | 外置时写入 TOAST relation 的 chunk 与索引 | 留在主行时列表回表经过的页更多；外置时详情需要拼接 chunk |
| 值是否压缩 | 压缩增加写入 CPU，减少写入字节 | 读取载荷时解压；高熵内容压缩无收益 |
| 每张表三个索引 | 双表布局的索引维护与空间加倍 | 提供列表、详情与 Trace 的索引路径 |
| 游标谓词形态 | 无 | 决定中间页从游标位置还是从窗口起点起扫 |
| `cohort` 无索引 | 无 | 批量恢复顺序扫描整表 |

## 5. ClickHouse 中的长载荷与 part 状态

本章以 `same_table` 与 `asset_ref` 目录表的建表语句为共同实例，适用于 23.3.10.5 与 25.12.11.4。

### 5.1 表结构

```sql
CREATE TABLE events (
  ingest_seq UInt64, event_id String, trace_id String, span_id String,
  parent_span_id Nullable(String), project_id String,
  start_time DateTime64(3, 'UTC'), end_time DateTime64(3, 'UTC'),
  duration_ms Int64, span_type String, framework String, level String,
  cohort Nullable(String), profile Nullable(String),
  content_type Nullable(String), encoding Nullable(String),
  content_length Nullable(UInt64), preview Nullable(String),
  sha256 Nullable(String), payload String CODEC(ZSTD(3)))
ENGINE = MergeTree ORDER BY (project_id, start_time, event_id);

CREATE TABLE assets (
  asset_id String, sha256 String, content_type String, encoding String,
  content_length UInt64, storage_path String,
  status Enum8('pending' = 1, 'available' = 2, 'failed' = 3, 'deleting' = 4),
  updated_at DateTime64(3, 'UTC') DEFAULT now64(3), error_category Nullable(String))
ENGINE = MergeTree ORDER BY asset_id;
```

1. `payload String CODEC(ZSTD(3))` 把载荷列的数据流按 ZSTD 第 3 级压缩。压缩按列、按块进行，可压缩文本与高熵文本在同一列中得到不同的压缩比。
2. `ORDER BY (project_id, start_time, event_id)` 决定 part 内的行序和稀疏主键索引。列表、详情和 Trace 的过滤都以 `project_id` 开头，排序键前缀可以裁剪 granule；`trace_id` 不在排序键中，Trace 查询在命中的时间范围内再按 `trace_id` 过滤。
3. 实验表没有建立跳数索引、projection 或物化视图，读取范围只由排序键裁剪决定。
4. `assets` 按 `asset_id` 排序，一次目录查询按主键前缀定位。`status` 以 `Enum8` 保存四种状态，`updated_at` 在状态更新时写入当前时间。

### 5.2 列裁剪与 granule

MergeTree 的每一列在 part 中有独立的数据流。查询只读取投影和过滤涉及的列流，列表查询不投影 `payload`，因此不读取载荷列的数据流，这一点对四种布局相同。

载荷列仍通过 granule 的划分影响列表查询。**granule** 是 ClickHouse 定位与读取的最小连续行集合，其行数同时受 `index_granularity`（默认 8,192 行）与 `index_granularity_bytes`（默认 10 MiB）限制，先达到的上限生效。`events` 含载荷列，携带大载荷的行使字节上限先达到，granule 更多、每个 granule 覆盖的主键区间更窄；`events_analytics` 与 `events_core` 不含载荷，granule 更少、更宽。列表查询按排序键裁剪到若干 granule 后读取其中全部行，granule 越窄，一页 keyset 结果读入的行数越少。该效应属于列存的 granule 划分，与载荷是否被读取无关。

```text
events 的一个 part（含 payload 列）
├── granule 0：行 [0, n0)，n0 受 index_granularity_bytes 限制
├── granule 1：行 [n0, n1)
├── …
├── 列流：event_id、start_time、…、payload（ZSTD(3)）
└── mark：每个 granule 的起点在各列流中的偏移

events_analytics 的一个 part（无 payload 列）
├── granule 0：行 [0, 8192)
└── …
```

### 5.3 `separate` 的连接

`separate` 的详情、Trace 与批量查询以 `events_analytics` 为左表、`event_payloads` 为右表做 `LEFT JOIN … ON p.event_id = a.event_id`，过滤条件只写在左表上：

```sql
SELECT a.event_id, a.trace_id, a.project_id, a.start_time,
       a.profile, a.content_type, a.encoding, a.content_length,
       a.preview, a.sha256, p.payload AS payload_value
FROM events_analytics AS a LEFT JOIN event_payloads AS p ON p.event_id = a.event_id
WHERE a.project_id = {project_id:String} AND a.trace_id = {trace_id:String}
  AND a.start_time >= {start_time:DateTime64(3,'UTC')}
  AND a.start_time <  {end_time:DateTime64(3,'UTC')}
ORDER BY a.start_time, a.event_id
FORMAT JSONEachRow;
```

ClickHouse 的哈希连接先读取右表构建哈希表。左表过滤条件没有等价地传递到右表时，右表需要读取全部 part 的连接键与载荷列，读取量随右表规模增长，与本次请求的行数无关。该机制已在 x86 主机的 ClickHouse 25.12 上验证：`trace:p50` 在 `separate` 上读取 56,726 行，右表被整表扫描（[阶段三实验报告](json-storage-stage3-report-2026-09-20.md)第 4.5 节）。23.3 上右表实际读取的 part、granule 与字节由查询计划和 `QueryFinish` 记录。

### 5.4 目录表的状态更新

ClickHouse 不提供逐行原地更新。`asset_ref` 把目录行从 `pending` 转为 `available` 使用变更语句：

```sql
ALTER TABLE assets UPDATE status = {status:String},
  error_category = NULL, updated_at = now64(3)
WHERE asset_id = {asset_id:String} SETTINGS mutations_sync = 2;
```

该语句提交一个 mutation：后台重写包含命中行的 part 并原子替换，`mutations_sync = 2` 使语句在 mutation 完成后才返回，后续查询因此立即读到新状态。每个载荷对象一次状态更新，即一次 mutation，其耗时计入写入完成时间。目录读取按 `updated_at` 取最新一行：

```sql
SELECT asset_id, sha256, content_type, encoding, content_length,
       storage_path, toString(status) AS status,
       toString(updated_at) AS updated_at, error_category
FROM assets WHERE asset_id = {asset_id:String}
ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow;
```

### 5.5 part 状态

每批 INSERT 生成新的 part，后台 merge 把同一 partition 内的相邻 part 合并为更大的 part，merge 规则见 [JSON 存储原理](json-storage-principles-2026-09-09.md) 第 3.5 节。本实验把**受控表**（承载列表查询的表：`same_table` 的 `events`、`separate` 与 `asset_ref` 的 `events_analytics`、`full_core` 的 `events_core`）的 part 组织分为四种状态：

| 状态 | 含义 | 产生方式 | 判定条件 | merge 成本所在阶段 |
|---|---|---|---|---|
| **碎片态** | merge 暂停，每个写入 block 各自成 part | 写入前对写目标执行 `SYSTEM STOP MERGES`，再写入 190 个 block | database 内没有活跃 merge，受控表 active part 数不少于 2 | 推迟到后续状态 |
| **合并中** | 后台 merge 正在合并这些 part | 执行 `SYSTEM START MERGES` | `system.merges` 中受控表存在活跃 merge | 与采样开始时正在进行的 merge 重叠，重叠部分落在查询期 |
| **自然稳定态** | 后台 merge 自行收敛 | 持续轮询 | 连续三次观察无活跃 merge 且 active part 数不变 | 等待时间计入查询就绪 |
| **单 part 态** | 每张写目标只剩一个 part | 执行 `OPTIMIZE TABLE … FINAL` | 受控表 active part 数为 1 | `OPTIMIZE` 耗时计入该状态的准备 |

四个状态在同一次运行中依次产生：

```text
part 状态控制的一次运行
├── 1. 建表，SYSTEM STOP MERGES
├── 2. 写入 190 个 block → 碎片态：采样
├── 3. SYSTEM START MERGES → 观察到活跃 merge 时立即采样 → 合并中
├── 4. 等待 merge 收敛 → 自然稳定态：采样
├── 5. OPTIMIZE TABLE … FINAL → 单 part 态：采样
└── 6. 清理 database
```

每个状态采样 `main` workload 的全部 12 个查询目标，每个目标 30 次，共 360 个样本。程序在采样前轮询判定条件，最长 60 秒；条件成立后立即连续采样，超时则终止该运行，不发布该状态的数据。判定只在采样开始前证明一次，合并中状态的 merge 可能在 360 个样本采完之前结束，实验报告按运行清单中的状态观察记录解释这段重叠。`asset_ref` 只对受控表暂停 merge，`assets` 保持 merge 开启，因为目录状态更新依赖后台执行 mutation。

主矩阵以自然稳定态作为 ClickHouse 的查询起点：日常写入后，后台 merge 自行收敛到这一状态。单 part 态只作为机制控制，用于分离 part 数的影响。

part 状态通过三条途径影响查询，结果解释需要同时核对对应证据：

| 途径 | 机制 | 证据 |
|---|---|---|
| 读取流数量 | 每个 part 都要按排序键定位并打开所需列流，part 越多，定位与合并读取结果的固定开销越多 | `QueryFinish` 的 `read_rows`、查询计划中的 `Parts` 与 `Granules` |
| mark 与 granule | part 各自有 granule 边界与末尾 mark，同样行数下 part 越多 mark 越多 | `system.parts` 的 `marks` |
| 后台资源 | 合并中的 merge 占用 CPU、内存与磁盘带宽 | `system.merges`、主机资源快照 |

碎片态有 190 个 part。ClickHouse 23.3 默认的 `parts_to_delay_insert` 为 150、`parts_to_throw_insert` 为 300（[23.3 MergeTree 设置定义](https://github.com/ClickHouse/ClickHouse/blob/v23.3.10.5-lts/src/Storages/MergeTree/MergeTreeSettings.h)），单个 partition 的 active part 超过前者时延迟 INSERT；ARM 主机的 ClickHouse 把两者设为 1,000 与 3,000，使碎片态的写入不触发写入保护。25.12 的默认值即为 1,000 与 3,000。

### 5.6 列存的访问证据

每条正式查询以唯一的 `query_id` 执行，程序从 `system.query_log` 读取对应的 `QueryFinish` 记录，保存 `read_rows`、`read_bytes`、结果行数与字节；正式样本结束后对同一语句执行 `EXPLAIN indexes = 1`，保存各表选中的 part 与 granule。每轮记录 `system.parts` 中各表的 active part 数、行数、mark、压缩前后字节，`system.parts_columns` 中每列的压缩前后字节，以及 `system.merges` 中的活跃 merge。

## 6. `asset_ref` 的 Sidecar 实现

本文把 `asset_ref` 中由实验程序实现、运行在应用进程内的对象存储与解析器合称 **Sidecar**。目录表 `assets` 位于数据库内，由 Sidecar 读写。[JSON 存储原理](json-storage-principles-2026-09-09.md) 第 3.9 节的 `fidelity_values` 也是实验自定义的 Sidecar，两者服务不同阶段、保存不同内容。

### 6.1 组件

```text
应用进程
├── 写入程序：登记目录行、发布对象、转换状态、写入引用行
└── 解析器：按引用查目录、读对象、校验内容
数据库（同一 schema 或 database）
├── events_analytics：每行一个可空的 asset_id
└── assets：每个对象一行目录记录
本机文件系统（对象存储目录）
└── <root>/
    ├── 0d/0d3671d1…e67e8ca：一个载荷对象，文件名为其 SHA-256
    └── a4/a4020a5a…b466c
```

### 6.2 内容寻址与原子发布

对象以载荷原始字节的 SHA-256 为标识，`asset_id` 与 `sha256` 取同一值，路径为 `<root>/<前两位>/<sha256>`。发布一个对象的步骤：

1. 计算目标路径；同名文件已存在且内容与待写字节一致时直接复用，不重复落盘；
2. 在目标目录内创建临时文件，写入全部字节并 `fsync`；
3. 读回临时文件，核对长度与 SHA-256；
4. 用 `os.replace` 把临时文件原子改名为目标路径。

读回校验不一致时以 `corrupt` 类别报告，文件系统错误以 `failed` 类别报告，两种情况都删除临时文件。原子改名保证目标路径上只出现完整对象。

### 6.3 目录表与状态

| 状态 | 含义 |
|---|---|
| `pending` | 对象正在写入或校验，事件不能把它作为可用内容返回 |
| `available` | 对象已发布且元数据校验通过 |
| `failed` | 发布或校验失败，`error_category` 保存错误分类 |
| `deleting` | 删除流程已开始，对象与引用核对完成前不发布删除完成 |

```text
（无记录）──登记──> pending ──发布成功──> available ──开始删除──> deleting
                     │
                     └──发布失败──> failed
```

故障用例的终态还会出现 `absent`，表示目录表中没有该对象的行；状态取值只有上表四种。

### 6.4 写入四步

`asset_ref` 的每个 block 按以下顺序写入，四步都计入写入完成时间：

1. 为本 block 新出现的每个对象向 `assets` 插入一行，状态为 `pending`；
2. 按第 6.2 节发布对象；
3. 把对应目录行更新为 `available`，发布失败时更新为 `failed` 并终止该 block；
4. 向 `events_analytics` 批量写入该 block 的全部事件行，携带载荷的行写入 `asset_id`。

目录表因此每个对象写入两次，第一次早于对象发布。行存中第 3 步是一条 `UPDATE`；ClickHouse 中是第 5.4 节的一次 mutation。

### 6.5 解析流程

数据库查询返回事件行后，客户端对每个带 `asset_id` 的行执行：

1. 按 `asset_id` 查询 `assets`，目录行不存在时返回 `missing`；
2. 核对目录行的 `asset_id` 与 `sha256` 都等于引用值，不一致返回 `metadata_mismatch`；
3. 核对状态为 `available`，否则返回该状态作为错误类别；
4. 核对目录行的 `content_type`、`encoding`、`content_length` 与事件行一致，不一致返回 `metadata_mismatch`；
5. 按 `storage_path` 读取对象文件：路径与内容地址不一致返回 `metadata_mismatch`，文件不存在返回 `missing`，其他文件系统错误返回 `failed`；
6. 按 `encoding` 解码并截取 200 个 code point，与事件行的 `preview` 比较；长度、SHA-256 或 preview 不一致时返回 `corrupt`；
7. 全部通过后把对象字节作为该行的载荷。

同一个逻辑查询内的全部目录查询复用一个查询范围内的连接。目录查询与文件读取发生在数据库查询返回之后，计入客户端恢复阶段；Trace 与批量查询涉及多个对象时，按对象逐个执行。

### 6.6 故障语义

六个固定故障用例在两个引擎上以相同方式注入，检查解析器的错误分类、目录状态转换、事件可见性与孤儿对象：

| 用例 | 注入点 | 预期解析结果 | 预期终态 | 恢复动作 |
|---|---|---|---|---|
| `missing` | 删除已发布对象 | `missing` | `available` | 恢复对象 |
| `corrupt` | 修改已发布对象的字节 | `corrupt` | `available` | 替换对象 |
| `metadata_mismatch` | 改写目录行元数据 | `metadata_mismatch` | `available` | 恢复目录元数据 |
| `upload_then_db_failure` | 对象发布后数据库写入失败 | 事件不可见 | `absent` | 核对器识别孤儿对象并删除 |
| `publish_failure` | `pending` 对象发布失败 | `failed` | `failed` | 确认失败状态 |
| `delete_failure` | 删除对象时失败 | `deleting` 或 `failed` | `deleting` 或 `failed` | 确认删除未完成 |

**孤儿对象**指对象存储目录中存在、但不可达的文件；一个对象可达，要求存在引用它的事件行，且该事件行与目录行能按 `asset_id` 连接。**核对器**是实验程序中执行这一判定的组件：它按事件表与目录表的连接结果得到可达路径集合，列出对象存储目录中的全部文件，差集即孤儿清单。该场景只记录分类与状态，不产生时延统计。

### 6.7 边界

Sidecar 读取本机文件系统，代表应用侧外置对象这一类方案，不代表远程对象存储的网络、鉴权与一致性语义，也不代表数据库进程内的大对象读取。后者属于独立候选，不在四种布局之内。

## 7. 实验程序的实现与代表性

### 7.1 组成

四种布局、写入与查询都由一套 Python 程序实现：

```text
experiments/json-storage-stage3/
├── generator/generate_payloads.py      载荷生成与 truth
├── runner/common.py                    逻辑记录、查询与证据的公共类型
├── runner/opengauss.py                 行存适配器
├── runner/xstore.py                    XStore 适配器，继承行存适配器
├── runner/clickhouse.py                列存适配器
├── runner/assets.py                    内容寻址对象存储与解析器
├── runner/run_layout_matrix.py         主矩阵：写入、维护、测量、校验、清理
├── runner/run_clickhouse_part_states.py  part 状态控制
├── runner/run_interference.py          混合负载
├── runner/run_asset_failures.py        对象故障
├── report/summarize.py                 汇总与门禁
└── tools/                              冻结输入打包、行存探针、执行驱动与结果打包
```

每个适配器实现同一组接口：

| 接口 | 职责 |
|---|---|
| `create` | 建立 schema 或 database 与该布局的全部表、索引 |
| `ingest_block` | 写入一个 block 的全部写目标，`asset_ref` 另执行第 6.4 节四步 |
| `wait_write_complete` | 查询全部写目标的可见行数，判定联合水位 |
| `wait_query_ready` | 行存执行 `ANALYZE`；ClickHouse 等待 merge 收敛 |
| `run_query` | 执行一条查询、读取完整响应并完成客户端恢复 |
| `collect_access_evidence` | 采集计划、索引增量或 `QueryFinish` |
| `collect_storage` | 采集各写目标的空间与物理状态 |
| `audit_dataset` | 按 truth 核对全部记录与载荷 |
| `cleanup` | 删除本次运行的 schema、database 与对象存储目录 |

### 7.2 代表性来源

四种布局是 schema 级的组织方式，生产系统可以直接采用同样的表结构和查询。实验在真实引擎上执行真实 DDL 与 SQL，TOAST、列裁剪、granule 划分、merge 与 mutation 按引擎自身机制运行，实验程序不模拟这些行为。四种布局返回同一份经过校验的完整内容，输出契约相同，差异来自布局与引擎。

### 7.3 实现带来的偏差

| 实现选择 | 偏差方向 |
|---|---|
| 客户端为单线程 Python；行存经 libpq 使用文本协议，写入使用文本格式的 `COPY`；ClickHouse 使用 HTTP 与 `JSONEachRow` | 两个引擎的结果编码不同，大结果的传输与解码成本不对称，ClickHouse 的 JSON 解码计入查询完成与客户端恢复 |
| 每个样本在客户端完成完整 SHA-256 校验 | 批量与大对象查询的校验时间在应用可用时间中占固定比例，与布局无关 |
| `full_core` 使用显式双写 | 不反映物化视图或触发器的写入与一致性特性 |
| 解析器逐对象查询目录 | 对象数量多时目录查询次数线性增长，批量目录查询等优化不在范围内 |
| 主矩阵复用连接、不清除操作系统缓存 | 结果代表热缓存下的查询，不覆盖冷启动 |
| 只覆盖写入与读取 | 不覆盖载荷改写、删除传播与垃圾回收 |

## 8. 执行口径

### 8.1 计时与统计

计时阶段的定义见第 1.5 节。写入另报告两个量：**写入合计**为一轮中 190 个 block 的 `ingest.wall_ms` 之和，只含各 block 的写入调用；**写入完成时间**为从第一个 block 开始到最后一个 block 达到联合水位的墙钟时间，另含每个 block 之后的可见性查询。**单块 p50** 为一轮 block 写入时间的中位数。

应用可用 p50 先在每轮内对全部样本取中位数，再对四轮的轮内中位数取中位数，不由分项中位数相加得到。

### 8.2 轮次与顺序

每个引擎在三个性能 workload 上各执行四轮，按四阶 Latin square 轮换布局的执行位次，使每种布局在每个位次出现一次；`correctness_only` 执行一轮。轮内查询顺序固定，每个目标每轮预热 1 次，非批量目标正式测量 30 次，批量目标正式测量 5 次。

### 8.3 证据与门禁

每次运行保存输入、truth、DDL 与查询目录的身份，代码文件的摘要，引擎版本与二进制身份，以及第 4.6 节、第 5.6 节的访问证据和清理确认。一轮运行只有在以下条件全部成立时才进入汇总：全部记录与 truth 一致；全部样本通过校验；每个 block 达到联合水位；列表、详情与 Trace 走预期的访问路径；清理确认 schema、database 与对象存储目录已删除。

### 8.4 空间口径

行存记录每张写目标的 heap、index、TOAST 与 relation 总字节；ClickHouse 记录 active part 的压缩与未压缩字节，并按列细分；`asset_ref` 的对象存储目录单独记录字节与对象数，不与库内空间相加。空间只在同一引擎内比较，两种物理口径不换算。

## 9. 场景设计

十个场景覆盖载入与空间、五类查询、等总字节控制、混合负载、part 状态与对象故障：

| 场景 | 查询目标 | workload | 受控变量 |
|---|---|---|---|
| 载入、维护与空间 | 写入 190 个 block | 三个性能 workload | 布局的写入步骤与载荷位置 |
| 列表 | `list:first`、`list:middle` | 全部 | 列表来源表是否含载荷；游标位置 |
| 预览 | `preview:first`、`preview:middle` | `main` | 在列表之上增加预览列 |
| 单条详情 | `detail:text_64k`、`text_512k`、`text_2m`、`entropy_512k` | `main` | 载荷大小与可压缩性 |
| 完整 Trace | `trace:p25`、`p50`、`p95` | `main` | Span 数、对象数与总字节 |
| 批量恢复 | `batch:main` | `main` | 大结果集的传输、装配与校验 |
| 等总字节控制 | `batch:equal_total_few_large`、`batch:equal_total_many_medium` | 两个等总字节 workload | 对象数量与单对象大小 |
| 混合负载 | 前台列表与预览 | `main` | 叠加的干扰流 |
| part 状态 | `main` 的全部 12 个目标 | `main` | ClickHouse 受控表的 part 状态 |
| 对象故障 | 六个故障用例 | 专用 | 注入点 |

非 `main` workload 执行列表两页、一个详情和本 workload 的批量恢复。

### 9.1 载入、维护与空间

按 190 个 block 写入，记录写入完成、查询就绪、写入合计与单块 p50，写入结束后采集空间。该场景检验第 3.5 节列出的额外写入步骤与额外空间在各引擎上的实际大小，并用第 8.4 节的分项解释空间差异。

### 9.2 列表

检验列表查询是否避开载荷，以及列表来源表是否含载荷如何改变读取范围，即第 3.6 节的存放途径。行存关注沿列表索引回表读取的页数与中间页游标的起扫位置；列存关注 granule 划分对读取行数的影响。四种布局返回相同页面，读取范围由计划、索引增量、`read_rows` 与响应字节判定。SQL 见第 4.3 节；ClickHouse `full_core` 的列表查询如下：

```sql
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       CAST(NULL AS Nullable(String)) AS preview, sha256,
       CAST(NULL AS Nullable(String)) AS payload_value
FROM events_core
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3,'UTC')}
  AND start_time <  {end_time:DateTime64(3,'UTC')}
  AND (start_time, event_id) > ({cursor_time:DateTime64(3,'UTC')}, {cursor_id:String})
ORDER BY start_time, event_id LIMIT {page_size:UInt64}
FORMAT JSONEachRow;
```

### 9.3 预览

与列表共用窗口、游标与分页，把写入时保存的 200 字符预览加入投影，载荷仍投影为 NULL。该场景检验预览列带来的增量读取与传输；四种布局都读取预存的预览列，不在查询内截取载荷。

### 9.4 单条详情

按项目、Trace、时间与事件标识四个等值条件恢复一行完整记录。四个样本覆盖三种大小与一种高熵内容，`entropy_512k` 与 `text_512k` 等长，用于观察压缩与解压对读取的影响。判定用的证据为三个分项时间、行存计划中的访问节点、ClickHouse 的 `read_rows` 与 `read_bytes`、数据库响应字节与解析器读取字节。

```sql
-- 行存 separate：主表与载荷表按 event_id 连接
SELECT a.event_id, a.trace_id, a.project_id, a.start_time,
       a.profile, a.content_type, a.encoding, a.content_length,
       a.preview, a.sha256, p.payload AS payload_value
FROM events_analytics a LEFT JOIN event_payloads p ON p.event_id = a.event_id
WHERE a.project_id = %s AND a.trace_id = %s AND a.start_time = %s AND a.event_id = %s;

-- 行存 asset_ref：数据库返回引用，解析器随后按第 6.5 节读取对象
SELECT event_id, trace_id, project_id, start_time,
       profile, content_type, encoding, content_length,
       preview, sha256, asset_id AS payload_value
FROM events_analytics
WHERE project_id = %s AND trace_id = %s AND start_time = %s AND event_id = %s;

SELECT asset_id, sha256, content_type, encoding, content_length,
       storage_path, status, updated_at, error_category
FROM assets WHERE asset_id = %s;
```

### 9.5 完整 Trace

按项目、Trace 与时间范围返回一条 Trace 的全部 Span 与载荷。三个样本的 Span 数、对象数与总字节同时变化（第 2.4 节），用于检验多行读取、`separate` 右表读取范围与 `asset_ref` 逐对象解析的次数。证据为计划中的 Trace 索引或排序键裁剪、`separate` 右表的 `read_rows`，以及解析器请求数。ClickHouse `separate` 的 SQL 见第 5.3 节。

### 9.6 批量恢复

按 cohort 取出全部携带载荷的行，`main` 为 160 个对象、128,450,560 字节。该场景检验大结果集的数据库传输、客户端装配与校验，并记录有效吞吐。证据为三个分项时间、数据库响应字节、解析器读取字节与请求数，以及行存计划中的顺序扫描节点。

```sql
SELECT event_id, trace_id, project_id, start_time,
       profile, content_type, encoding, content_length,
       preview, sha256, payload AS payload_value
FROM events
WHERE cohort = %s AND sha256 IS NOT NULL
ORDER BY start_time, event_id;
```

### 9.7 等总字节控制

两个 workload 的表内载荷原始总字节相同，对象数量相差 32 倍：40 个 2 MiB 对象与 1,280 个 64 KiB 对象。两组载荷行在写入时 `cohort` 列取各自的 workload 名，批量查询按该名过滤，只返回本次载入的对象。该场景把"对象数量"与"总字节"分开：数据库内布局的传输量相近，`asset_ref` 的目录查询与文件读取次数随对象数线性变化，ClickHouse 的目录状态更新次数也随对象数变化。

### 9.8 混合负载与读取隔离

每次只运行一个引擎的一种布局，两个引擎串行。客户端持续发送两条前台请求流，再在不同阶段叠加一种由客户端发起的干扰流；数据库自身的后台 merge 另行记录。每条请求流在独立的客户端进程内运行，调度线程与 worker 都在该进程内，各流以父进程选定的同一 monotonic 起点对齐，客户端解析大结果集与解析器读文件的工作不进入其他流的进程。

| 参数 | 取值 |
|---|---|
| 前台请求 | `list:first` 与 `preview:first`，各 20 次每秒，各 2 个 worker，单请求超时 30 秒 |
| 阶段时长 | 30 秒预热加 300 秒测量 |
| 迟到容忍 | 请求未能在计划时刻后 0.05 秒内发出，或到达时 worker 全部占用，记为**调度丢弃**（scheduler drop） |

| 阶段 | 干扰流 | 到达率 |
|---|---|---|
| `quiet` | 无 | 不适用 |
| `detail_2m` | `detail:text_2m` | 1.0 次每秒 |
| `trace_long` | `trace:p95` | 0.2 次每秒 |
| `batch_loop` | `batch:main` | 连续，前一次返回后立即发起下一次 |
| `continuous_ingest` | 循环重放 45 个固定 block | 1.0 block 每秒 |

持续写入只重放满足三个条件的 block：256 行满块、全部记录位于前台查询窗口之外、至少包含一个 `main` 载荷。冻结输入中有 45 个这样的 block，重放它们使写入路径携带长载荷，同时前台结果与 truth 保持一致。阶段预载已写入这些 block，重放时每一轮给 `event_id` 加上轮次后缀，其余列与 `ingest_seq` 不变；行存的 `event_id` 主键因此不冲突，两个引擎写入相同的行。每个阶段使用独立的 schema 或 database，避免上一阶段留下的 part、缓存或写入状态进入下一阶段。

样本状态分为成功、查询失败、超时和调度丢弃。前台 p99 只在该流有至少 1,000 个成功样本时发布。每个阶段在预热前、测量前和测量后采集 CPU、内存、I/O 与存储快照：ClickHouse 记录 part 数、mark、压缩字节与活跃 merge，行存记录 heap、index 与 TOAST 字节，积压 part 数记为 0。前台时延与丢弃计数在两个引擎上同口径，物理快照按各自的存储模型解释。

该场景检验外置或分表是否把大内容读取、批量导出与持续写入的资源竞争移出前台列表路径，以及移出的代价落在哪里，即第 3.6 节的并发途径。

### 9.9 ClickHouse part 状态控制

只在 ClickHouse 上执行，按第 5.5 节构造并采样四个状态。该场景与主矩阵独立汇总，不与主矩阵样本配对。它检验 part 状态改变时各类查询的读取范围与时延如何变化，并与同一状态下四种布局之间的差异对照。

### 9.10 对象故障与恢复

只对 `asset_ref` 执行，两个引擎各运行第 6.6 节的六个用例，检验一致性责任移到应用侧后是否被正确承担。结果为分类与状态，不参与数值比较。

## 10. 引擎参数与理由

| 引擎 | 参数 | 取值 | 施加位置 | 理由 |
|---|---|---|---|---|
| ClickHouse 23.3 | `parts_to_delay_insert` | 1,000 | 服务端配置 `merge_tree` 段 | 高于碎片态的 190 个 part，写入不触发延迟 |
| ClickHouse 23.3 | `parts_to_throw_insert` | 3,000 | 同上 | 碎片态写入不触发拒绝 |
| ClickHouse | `max_server_memory_usage_to_ram_ratio` | 0.5 | 服务端配置根段 | 各主机使用相同比例，服务端内存上限随物理内存变化，实际上限由运行记录给出 |
| ClickHouse | 载荷列编码 | `ZSTD(3)` | 建表语句 | 固定库内载荷的压缩级别 |
| ClickHouse | 排序键 | `(project_id, start_time, event_id)` | 建表语句 | 与列表过滤和排序一致 |
| ClickHouse | ARM 运行版本 | 23.3.10.5 | 安装包 | 鲲鹏 920 为 ARMv8.2-A 且无 SVE，23.8 及以后的官方 ARM64 构建在该处理器上无法执行 |
| 行存 | 索引 | 每张事件表主键加两条复合 B-tree | 建表语句 | 提供按键定位、列表与 Trace 的访问路径 |
| 行存 | 查询前维护 | `ANALYZE` | 写入完成后 | 使优化器统计覆盖全部数据 |
| XStore | 构建类型 | release | 服务端构建 | debug 构建的耗时不具可比性 |

ClickHouse 服务端参数的生效值在服务就绪后读取并保存；XStore 的构建提交、构建命令、二进制摘要与版本查询结果按主机记录。

## 11. 两套环境与可比性边界

| 项 | ARM 主机 A | ARM 主机 B | x86 主机 |
|---|---|---|---|
| 引擎 | XStore、ClickHouse 23.3.10.5 | XStore、ClickHouse 23.3.10.5 | openGauss 6.0.0、ClickHouse 25.12.11.4 |
| 处理器 | 鲲鹏 920 | 鲲鹏 920 | x86-64，8 CPU |
| 运行方式 | 原生安装，两引擎串行 | 原生安装，两引擎串行 | 容器，两引擎串行 |

比较按三层进行：先在同一主机、同一引擎内比较四种布局；再在同一主机内比较两个引擎的完整方案；最后核对不同主机上结论的方向。处理器、内存与 ClickHouse 版本在主机之间不同，跨主机的绝对耗时与同机比值不构成单因素比较，只用于检查方向是否一致。跨引擎数字比较的是完整方案，同时包含行存或列存、索引与排序键、写入协议、压缩、后台维护、Sidecar 与客户端处理的影响，不解释为单一数据类型的速度排名。

## 12. 待实测确定项

以下事实只能由 ARM 主机的运行证据确定，实验报告逐项给出结论与证据位置：

| 项 | 确定依据 |
|---|---|
| XStore 大值留在主行还是外置、是否压缩 | 行存探针的 `reltoastrelid`、`pg_column_size` 与 `octet_length`；各表 TOAST 分项空间 |
| XStore 展开式游标能否作为索引范围起点，带下界写法的索引范围 | 行存探针的两种写法计划；主矩阵 `list:middle` 与 `preview:middle` 计划中的 Index Cond 与 `Rows Removed by Filter` |
| XStore 计划的缓冲证据 | 主矩阵计划文本是否含 Buffers 行 |
| ClickHouse 23.3 上 `separate` 右表的读取范围 | `separate` 详情与 Trace 的 `read_rows` 与计划中的右表 `Parts`、`Granules` |
| ClickHouse 23.3 上各表的 granule 划分 | 各表 mark 数与列表查询的 `read_rows` |
| 行存持续写入阶段的写入代价 | 混合负载 `continuous_ingest` 阶段的写入样本与存储快照 |
| XStore 同表载荷对列表与预览的影响幅度 | 同机 `same_table` 与其余三种布局的列表、预览 p50 之比，计划中的访问节点与读取量 |
| 两个引擎上并发载荷读写对前台的干扰幅度 | 混合负载各阶段相对 `quiet` 的前台 p50、p95 与调度丢弃数 |
| 两个引擎的服务端参数与构建身份 | 各主机的构建记录、参数生效值与运行清单 |

## 参考资料

- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)
- [阶段三实验报告（openGauss 与 ClickHouse 25.12）](json-storage-stage3-report-2026-09-20.md)
- [阶段二实验报告](json-storage-stage2-report-2026-09-10.md)
- [openGauss 6.0 TOAST 阈值与外置结构定义](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/include/access/tuptoaster.h)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse 稀疏主键索引与自适应 index granularity](https://clickhouse.com/docs/guides/best-practices/sparse-primary-indexes)
- [ClickHouse ALTER UPDATE 与 mutation](https://clickhouse.com/docs/sql-reference/statements/alter/update)
- [ClickHouse SYSTEM STOP MERGES](https://clickhouse.com/docs/sql-reference/statements/system)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/reference/statements/optimize)
- [ClickHouse 23.3 MergeTree 设置定义](https://github.com/ClickHouse/ClickHouse/blob/v23.3.10.5-lts/src/Storages/MergeTree/MergeTreeSettings.h)
- 实验程序：`experiments/json-storage-stage3/`（第 7.1 节）
