# Agent Trace JSON 存储阶段三实验报告

> 实验完成日期：2026-09-19；文档修订日期：2026-09-20
>
> 状态：实验、结果验证和报告均已完成
>
> 数据库版本：openGauss 6.0.0 build `aee4abd5`；ClickHouse 25.12.11.4

配套的[JSON 存储原理](json-storage-principles-2026-09-09.md)解释 TOAST、MergeTree part 与列式压缩的一般机制。本文集中说明长 payload 的四种物理布局在实验契约、场景、结果和结论上的差异。

## 1. 结论

四种布局为：`same_table` 在一张表内保存分析列与 payload；`separate` 把 payload 移入独立表；`full_core` 在完整记录表之外复制一张列表专用窄表；`asset_ref` 把 payload 外置为本地内容寻址对象，库内只保留引用。组织方式、查询路径和写入完成条件见第 2.2 节。

四种布局的写入顺序在两个引擎上完全一致：`same_table` < `separate` < `full_core` < `asset_ref`。openGauss 的写入完成 wall time 分别为 9,185 / 11,346 / 11,739 / 13,770 ms，ClickHouse 为 5,642 / 8,400 / 9,014 / 10,449 ms。增量来自各布局必须执行的额外步骤：第二张表的行构造与提交、Full/Core 双写，以及 Asset 的对象发布（openGauss 1,040.85 ms，ClickHouse 1,134.98 ms）。

列表和预览查询在四种布局之间没有区分度。主 cohort 中 openGauss 四布局的应用可用 p50 落在 13.52–14.72 ms，ClickHouse 落在 12.63–14.82 ms；四布局的最大最小比在 openGauss 上为列表 1.069–1.087、预览 1.009–1.015，在 ClickHouse 上为列表 1.089–1.140、预览 1.108–1.128。机制证据是列表与预览各八个目标的 `payload_selected` 均为 false：openGauss 在四种布局下均只扫描 256 行，ClickHouse 不读取 payload 列数据流。该结果满足访问路径与读取量的全部门禁，属于“当前场景未分辨”的正式结论。

ClickHouse 的 `list:middle` 出现与朴素预期相反的读取量：`same_table` 扫描 23,947 行、1,800,361 bytes，而 `separate`、`full_core` 和 `asset_ref` 各扫描 38,912 行、3,361,448 bytes。把 payload 移出主表后，列表查询多扫描约 62% 的行。列表场景八个目标的 `payload_selected` 均为 false，该差异与 payload 读取无关；granule 划分证据与机制见第 4.2 节。该效应属于列存，不适用于 openGauss：后者通过索引在四种布局下均精确扫描 256 行。

等总字节分布控制给出了全实验最强的分离信号。在同样 80 MiB 原始内容下，`asset_ref` 的批量恢复由最快变为最慢，两个口径分别陈述。**跨组比值**（同一布局，40 个 2 MiB 对象 → 1,280 个 64 KiB 对象）：ClickHouse 由 777.26 ms 升至 8,085.18 ms，为 10.40 倍；openGauss 由 582.86 ms 升至 2,137.03 ms，为 3.67 倍。**组内最大最小比**（`equal_total_many_medium` 内最慢除以最快）：ClickHouse 7.554（`asset_ref` 8,085.18 ms 对 `full_core` 1,070.27 ms），openGauss 3.567（`asset_ref` 2,137.03 ms 对 `same_table` 599.05 ms）。在 `equal_total_few_large` 中 `asset_ref` 是两个引擎上最快的布局，这使反转具有判据意义。resolver 请求数由 200 增至 6,400，字节总量不变，说明成本由对象数量而非字节总量决定。

混合负载给出双向结论。`batch_loop` 相中，`asset_ref` 的前台 list 与 preview 只有 2 次和 1 次 scheduler drop，`same_table`、`separate`、`full_core` 分别为 477/505、653/666、582/581 次；同一相中 `asset_ref` 自身的批量恢复 p50 为 2,361.76 ms，是四种布局中最慢的，`same_table` 为 1,906.53 ms。竞争被移出数据库查询路径，并未消除。`detail_2m` 与 `trace_long` 两相相对 quiet 相的前台 p50 变化在 −3.39% 至 +3.91% 之间（`same_table` 在 −0.95% 至 +1.01% 内），最大 scheduler drop 为 5 次。该幅度在本负载下属于运行间波动，不表示可观测的干扰效应；两相的干扰强度不足，不构成隔离性结论。

长载荷只在与持续批量读取并发时显著影响列表与 preview：同表存放本身不拖慢列表，把 payload 移入同一引擎内的另一张表也不减轻批量读取的干扰，只有 `asset_ref` 把这部分竞争移出数据库。两条影响途径的逐项证据见第 5.2 节。

part 状态的影响大于布局选择，且方向在四种布局中一致。ClickHouse 碎片态（190 part）的 `list:first` p50 为 38.29–44.99 ms，单 part 态为 12.34–13.75 ms，同一布局内比值 2.90–3.27；而同一控制内同一状态下四种布局的最大最小比只有 1.114–1.175。part 状态因此是与布局正交的因素。

空间的两引擎结论不同。以 `same_table` 为基准，openGauss 的 `separate` 与 `full_core` 总占用为 1.530 倍和 1.586 倍，约为 1.5 倍，主因是索引重复：每张表的索引字节合计 21.92 MB。ClickHouse 的同两种布局只有 1.097 倍和 1.170 倍。`asset_ref` 把 payload 移出数据库后总占用最高：openGauss 为 2.884 倍（33.01 MB 库内加 128.45 MB 对象，合计 161.46 MB），ClickHouse 为 6.921 倍（131.73 MB 对 19.04 MB）。ClickHouse 的倍数依赖本实验语料的高可压缩性，按生产 Agent Trace 文本 3–6 倍压缩比折算后为 2.40–3.57 倍。

ClickHouse 的 `trace:*` 场景中 `separate` 显著离群：`trace:p25` 为 59.45 ms，其余三种布局为 11.75–13.80 ms；`trace:p50` 为 65.33 ms，其余三种布局为 16.76–18.58 ms；`trace:p95` 为 96.64 ms，其余三种布局为 33.77–37.61 ms。`trace:p50` 的查询完成 p50 为 61.15 ms，扫描 56,726 行、132,368,859 bytes，而 `same_table` 为 5,393 行、13,171,006 bytes。差异来自 JOIN 执行路径：过滤条件只施加在 SQL 左表 `events_analytics`，右表 `event_payloads` 没有等价谓词下推，payload 列被整表扫描。

Asset 故障实验的六个固定用例在两个引擎上全部符合设计预期，validation error 和 execution error 均为 0。混合负载共调度 243,194 个请求，成功 239,715 个，scheduler drop 3,479 个，查询失败 0 个。

跨引擎数字用于比较固定硬件和统一应用可用边界下的完整方案。结果同时包含行存或列存、索引与排序键、执行器、协议、压缩编解码、后台维护和客户端校验的影响，不能解释为布局本身的单因素差异，也不能外推为生产环境性能。

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

Full/Core 采用 runner 维护的显式双写和联合水位，两个引擎使用相同的一致性契约。结果正确性由独立程序根据冻结输入预先计算，所得结果以下简称 **truth**；实验查询不参与 truth 的生成。Asset 引用固定为下列形式，运行时 resolver 按引用和 `assets` 行核对 content type、encoding、长度和 SHA-256 后读取本地对象；truth 不参与定位、状态转换或错误分类。

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

payload 在数据库内使用保持 UTF-8 bytes 的 openGauss `TEXT` 或 ClickHouse `String CODEC(ZSTD(3))`。

openGauss 每张承载查询的表建立两个复合 B-tree：`(project_id,start_time,event_id)` 服务列表与预览的 keyset 分页，`(project_id,trace_id,start_time,event_id)` 服务 Trace 与单条详情定位。`event_id` 为主键。

```sql
-- openGauss same_table
CREATE TABLE events (
    ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
    trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
    project_id TEXT NOT NULL, start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, duration_ms BIGINT NOT NULL,
    span_type TEXT NOT NULL, framework TEXT NOT NULL, level TEXT NOT NULL,
    cohort TEXT, profile TEXT, content_type TEXT, encoding TEXT,
    content_length BIGINT, preview TEXT, sha256 TEXT, payload TEXT);
CREATE INDEX events_list_idx ON events (project_id,start_time,event_id);
CREATE INDEX events_trace_idx ON events (project_id,trace_id,start_time,event_id);
```

ClickHouse 每张表使用 `ENGINE=MergeTree ORDER BY (project_id,start_time,event_id)`，`assets` 表使用 `ORDER BY asset_id`。

```sql
-- ClickHouse same_table
CREATE TABLE events (
    ingest_seq UInt64, event_id String, trace_id String, span_id String,
    parent_span_id Nullable(String), project_id String, start_time DateTime64(3, 'UTC'),
    end_time DateTime64(3, 'UTC'), duration_ms Int64, span_type String, framework String,
    level String, cohort Nullable(String), profile Nullable(String),
    content_type Nullable(String), encoding Nullable(String), content_length Nullable(UInt64),
    preview Nullable(String), sha256 Nullable(String), payload String CODEC(ZSTD(3)))
ENGINE=MergeTree ORDER BY (project_id,start_time,event_id);

-- ClickHouse asset_ref catalog
CREATE TABLE assets (
    asset_id String, sha256 String, content_type String, encoding String,
    content_length UInt64, storage_path String,
    status Enum8('pending'=1,'available'=2,'failed'=3,'deleting'=4),
    updated_at DateTime64(3, 'UTC') DEFAULT now64(3), error_category Nullable(String))
ENGINE=MergeTree ORDER BY asset_id;
```

`separate` 和 `full_core` 的两张表各自建立同一组索引或排序键，`asset_ref` 的 `assets` 表只按 `asset_id` 组织。

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

混合负载在前台列表与 preview 负载之上按固定顺序执行五个相：`quiet` 无干扰，`detail_2m` 叠加 2 MiB 单条详情，`trace_long` 叠加长 Trace 恢复，`batch_loop` 叠加持续循环的批量恢复，`continuous_ingest` 叠加持续写入；各相的到达率、并发和时长见第 4.8 节。

列表与 preview 查询通过在投影中显式写入类型化 NULL 隔离 payload，使四种布局返回相同的结果形状。

```sql
-- openGauss 列表查询：preview 与 payload 均投影为 NULL
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       NULL::text AS preview, sha256, NULL::text AS payload_value
FROM events
WHERE project_id=%s AND start_time>=%s AND start_time<%s
  AND (start_time,event_id)>(%s,%s)
ORDER BY start_time,event_id LIMIT %s;
```

ClickHouse 主矩阵使用自然稳定的少量 part 作为查询就绪状态，不执行 `OPTIMIZE FINAL`；单 part 只出现在 part 状态控制中。openGauss 在写入完成后执行 `ANALYZE`。

### 3.3 计时与证据

载入侧使用两个阶段口径。一个布局涉及的全部表或 Asset 组件完成 190 个 block 写入并满足第 2.2 节联合水位，称为**写入完成**；写入完成 wall time 覆盖客户端行构造、数据库协议、双表写入、本地 Asset 写入和状态发布，不含随后的维护。在写入完成基础上，openGauss 执行 `ANALYZE`，ClickHouse 恢复后台 merge 并等待 active merge 连续三次为空且 active part 数不再变化，Full/Core 与 Asset 的联合水位同样满足；完成这些查询前维护的状态称为**查询就绪**。维护耗时计入写入完成到查询就绪的区间。

查询侧每个场景统一使用三个分项和一个总口径：

- **查询完成 p50**：数据库响应完整读取的时延中位数；`asset_ref` 只包含引用与 catalog 查询；
- **客户端恢复 p50**：双表组合、resolver 本地读取和结果规范化的时延中位数；
- **校验 p50**：完整 bytes 的长度与 SHA-256 核对时延中位数；
- **应用可用 p50**：每个样本从请求提交到校验完成的总时延中位数；该值不等于前三列中位数之和。

所有时延为四轮轮级统计量的中位数。

结果表的其余口径列如下。**有效 MiB/s** 为单个样本完成校验的 payload 字节除以该样本的应用可用时延，先按轮取 p50，再取四轮中位数；**数据库字节**与 **resolver 字节**为一轮全部正式样本的数据库响应字节合计与 resolver 读取字节合计，**请求数**同为一轮合计；**载入 wall ms** 即本节定义的写入完成 wall time。

ClickHouse 的 part 状态在第 4.9 节作为独立控制变量，四个状态的定义如下：**碎片态**为暂停 merge、190 个写入 block 各自成 part 的状态；**合并中**为恢复 merge 后合并仍在进行时的状态；**稳定态**为自然合并完成、active part 数不再变化的状态；**单 part 态**为执行 `OPTIMIZE FINAL` 后每张表只剩一个 part 的状态。

访问证据方面，openGauss 保存声明来源表、扫描行数和 `payload_selected`；其查询执行统计不提供可用的扫描字节计数，本文记为**不可观测**，不以 0 代替。ClickHouse 保存 `QueryFinish` 中的扫描行数与扫描字节、active part 数、mark 数和 merge 状态。**声明来源表**是该查询按布局目录读取的表，`asset_ref` 固定为 `events_analytics`；**`payload_selected`** 是该查询形态的投影是否包含 payload 列。两者由查询形态与布局目录在执行前确定，与 manifest 同时记录的查询语句一致；实际执行路径与读取量由查询计划、扫描行数和扫描字节独立记录。四种布局都在客户端收到完整 payload 后计算摘要，不使用服务端摘要代替内容传输。

空间口径为 openGauss 的 relation 已分配字节（含 heap、index 和 TOAST 分项）与 ClickHouse 的 active part 压缩字节，两者只作引擎内比较。Asset 目录字节单独统计。本文的 MB 指 10^6 bytes，MiB 指 2^20 bytes。

## 4. 场景化测试

### 4.1 载入、维护与空间

**1. 场景设计。** 四种布局按固定的 190 个 block 写入。写入完成以该布局涉及的全部表或 Asset 组件满足第 2.2 节联合水位为准，时间覆盖客户端行构造、数据库协议、双表写入、本地 Asset 写入和状态发布。Asset 发布时间单独分项记录。空间在写入完成后采集，openGauss 记录每张表的 heap、index 和 TOAST 分项，ClickHouse 记录每张表的 active part 压缩字节、part 数和 mark 数。ClickHouse 在载入期间保持后台 merge 开启，载入后每张承载查询的表留下 6 个 active part，`assets` 表留下 2 个，四轮均未执行 `OPTIMIZE FINAL`。openGauss 的 `ANALYZE` 与 ClickHouse 的 merge 等待都在写入完成之后执行，按第 3.3 节口径计入写入完成到查询就绪的区间，不计入写入完成 wall time。

**2. 测试目的与预期。** 该场景比较完整方案的写入成本与物理占用。第二张表、Core 复制和 Asset 发布预计增加必要写入步骤；索引与排序键的复制成本、payload 列的压缩率和 Asset 目录字节必须由分项数据验证。

**3. 实例 SQL。** openGauss 使用 `COPY` 写入各表并在写入完成后执行 `ANALYZE`；ClickHouse 使用 `INSERT ... FORMAT JSONEachRow`。`asset_ref` 在写入事件行之前完成对象的原子发布并把 `assets.status` 置为 `available`。

**4. 测试结果。** 主 cohort 的写入结果如下，均为四轮中位数。

| 布局 | openGauss wall ms | openGauss rows/s | openGauss Asset 发布 ms | ClickHouse wall ms | ClickHouse rows/s | ClickHouse Asset 发布 ms |
|---|---:|---:|---:|---:|---:|---:|
| `same_table` | **9,185.00** | **5,284.17** | 0 | **5,642.06** | **8,603.76** | 0 |
| `separate` | 11,345.62 | 4,277.86 | 0 | 8,399.86 | 5,777.96 | 0 |
| `full_core` | 11,738.58 | 4,134.67 | 0 | 9,013.57 | 5,385.47 | 0 |
| `asset_ref` | 13,770.23 | 3,524.58 | 1,040.85 | 10,449.09 | 4,644.81 | 1,134.98 |

写入完成之后的维护分项与载入到查询就绪的总 wall time 如下，均为四轮中位数。

| 布局 | openGauss `ANALYZE` ms | openGauss 载入到查询就绪 ms | ClickHouse merge 等待 ms | ClickHouse 载入到查询就绪 ms |
|---|---:|---:|---:|---:|
| `same_table` | 168.24 | 9,352.41 | 237.98 | 5,882.20 |
| `separate` | 242.96 | 11,588.44 | 242.97 | 8,643.33 |
| `full_core` | 330.06 | 12,084.48 | 243.83 | 9,256.99 |
| `asset_ref` | 185.18 | 13,958.53 | 247.91 | 10,695.88 |

openGauss 空间分项如下，单位 MB。

| 布局 | 表 | heap | index | TOAST | 表合计 | 库内合计 | Asset bytes | Asset 对象数 | 总计 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `same_table` | `events` | 10.85 | 21.92 | 23.18 | 55.98 | **55.98** | 0 | 0 | 55.98 |
| `separate` | `events_analytics` | 10.82 | 21.92 | 0.01 | 32.78 | 85.64 | 0 | 0 | 85.64 |
| | `event_payloads` | 7.74 | 21.92 | 23.18 | 52.86 | | | | |
| `full_core` | `events_core` | 10.82 | 21.92 | 0.01 | 32.78 | 88.75 | 0 | 0 | 88.75 |
| | `events_full` | 10.85 | 21.92 | 23.18 | 55.98 | | | | |
| `asset_ref` | `events_analytics` | 10.82 | 21.92 | 0.01 | 32.78 | **33.01** | 128.45 | 160 | 161.46 |
| | `assets` | 0.16 | 0.04 | 0.01 | 0.23 | | | | |

ClickHouse 空间分项如下，表字节为 active part 压缩字节，单位 MB。

| 布局 | 表 | active part | mark | 压缩字节 | 库内合计 | Asset bytes | Asset 对象数 | 总计 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `same_table` | `events` | 6 | 24 | 19.035 | **19.035** | 0 | 0 | 19.035 |
| `separate` | `events_analytics` | 6 | 17 | 3.237 | 20.891 | 0 | 0 | 20.891 |
| | `event_payloads` | 6 | 23 | 17.654 | | | | |
| `full_core` | `events_core` | 6 | 17 | 3.237 | 22.272 | 0 | 0 | 22.272 |
| | `events_full` | 6 | 24 | 19.035 | | | | |
| `asset_ref` | `events_analytics` | 6 | 17 | 3.250 | **3.283** | 128.45 | 160 | 131.733 |
| | `assets` | 2 | 4 | 0.033 | | | | |

以 `same_table` 为基准的总占用倍数如下。倍数在引擎内部计算，两个引擎的空间口径不同，不作跨引擎比较。

| 布局 | openGauss 总字节 | openGauss 倍数 | ClickHouse 总字节 | ClickHouse 倍数 |
|---|---:|---:|---:|---:|
| `same_table` | 55,975,936 | **1.000** | 19,035,002 | **1.000** |
| `separate` | 85,639,168 | 1.530 | 20,890,688 | 1.097 |
| `full_core` | 88,752,128 | 1.586 | 22,271,831 | 1.170 |
| `asset_ref` | 161,456,128 | 2.884 | 131,733,393 | 6.921 |

**5. 分析。** 写入顺序在两个引擎上完全一致，增量与各布局必须执行的额外步骤对应：`separate` 增加一张表的行构造与提交，`full_core` 在此基础上复制完整分析列集合，`asset_ref` 还增加 160 次本地对象发布。

openGauss 的双表布局把总占用提高到 `same_table` 的 1.530 倍和 1.586 倍，约 1.5 倍。分项数据指出主因是索引重复：每张表的索引字节合计 21.92 MB（manifest 记录的聚合值 `index_bytes = 21,921,792`，含 `list`、`trace` 两个复合 B-tree 与 `event_id` 主键索引，不提供单个索引的分解），两张表合计 43.84 MB，超过 `same_table` 全表占用的四分之三。`separate` 的 `event_payloads` 并不服务列表查询，仍携带同一组索引。ClickHouse 的同两种布局只有 1.097 倍和 1.170 倍，因为排序键不产生独立的二级结构，复制的分析列压缩后仅 3.237 MB。

`asset_ref` 把 128.45 MB 原始 bytes 留在数据库外，库内占用降至 33.01 MB（openGauss）和 3.283 MB（ClickHouse），但总占用最高：openGauss 为 `same_table` 的 2.884 倍，ClickHouse 为 6.921 倍。两个倍数的差距来自数据库侧压缩能力：ClickHouse 的 ZSTD(3) 把 128.45 MB payload 压缩到 15.79 MB（`events` 表 payload 列的压缩字节 15,793,420；按上表 `events` 减 `events_analytics` 得 15.80 MB，差值来自两张表非 payload 列的压缩结果不同），openGauss 的 TOAST 压缩后为 23.18 MB 且另有 21.92 MB 索引，基准本身更大。Asset 目录在两个引擎上都按原始 bytes 保存。ClickHouse 倍数的取值范围见第 6 节限制 1。

### 4.2 列表查询

**1. 场景设计。** 在固定项目和固定窗口内按 `start_time,event_id` 排序，使用 keyset 条件返回一页普通列，preview 与 payload 均投影为类型化 NULL。第一页 cursor 为窗口起点之前的哨兵值，`list:middle` 使用窗口中部的固定 cursor。固定窗口含 27,561 行，分页大小固定为 256，单页命中约占窗口的 0.93%。ClickHouse 查询在自然稳定的少量 part 上执行，四轮均未执行 `OPTIMIZE FINAL`，各表的 active part 数见第 4.1 节。每个引擎四轮，每个查询目标每轮预热 1 次、正式测量 30 次；下表的应用可用 p50 为四轮轮级中位数。

**2. 测试目的与预期。** 该场景先验证同表 payload 列能否被引擎实际裁剪，再比较独立表和 Core 是否进一步减少主表、mark、缓存或 part 启动成本。结果接近时，以声明来源表、`payload_selected`、扫描行数和扫描字节证明裁剪有效。

**3. 实例 SQL。**

```sql
-- openGauss same_table
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       NULL::text AS preview, sha256, NULL::text AS payload_value
FROM events
WHERE project_id=%s AND start_time>=%s AND start_time<%s
  AND (start_time,event_id)>(%s,%s)
ORDER BY start_time,event_id LIMIT %s;

-- ClickHouse full_core：列表路径读取 events_core
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length,
       CAST(NULL AS Nullable(String)) AS preview, sha256,
       CAST(NULL AS Nullable(String)) AS payload_value
FROM events_core
WHERE project_id={project_id:String}
  AND start_time>={start_time:DateTime64(3,'UTC')}
  AND start_time<{end_time:DateTime64(3,'UTC')}
  AND (start_time,event_id)>({cursor_time:DateTime64(3,'UTC')},{cursor_id:String})
ORDER BY start_time,event_id LIMIT {page_size:UInt64}
FORMAT JSONEachRow;
```

**4. 测试结果。** openGauss 结果如下。

| 布局 | `list:first` 应用可用 p50 | `list:middle` 应用可用 p50 | 声明来源 | 扫描行数 | 扫描字节 | `payload_selected` |
|---|---:|---:|---|---:|---|---|
| `same_table` | 13.60 ms | 13.62 ms | `events` | 256 | 不可观测 | false |
| `separate` | 14.54 ms | 14.72 ms | `events_analytics` | 256 | 不可观测 | false |
| `full_core` | 13.72 ms | 13.89 ms | `events_core` | 256 | 不可观测 | false |
| `asset_ref` | 13.65 ms | 13.54 ms | `events_analytics` | 256 | 不可观测 | false |

ClickHouse 结果如下。

| 布局 | `list:first` 应用可用 p50 | `list:middle` 应用可用 p50 | 声明来源 | `list:first` 扫描行数 / 字节 | `list:middle` 扫描行数 / 字节 | `payload_selected` |
|---|---:|---:|---|---:|---:|---|
| `same_table` | 12.63 ms | 13.61 ms | `events` | 12,147 / 898,816 | 23,947 / 1,800,361 | false |
| `separate` | 13.98 ms | 14.45 ms | `events_analytics` | 38,912 / 3,361,342 | 38,912 / 3,361,448 | false |
| `full_core` | 13.99 ms | 14.82 ms | `events_core` | 38,912 / 3,361,342 | 38,912 / 3,361,448 | false |
| `asset_ref` | 14.39 ms | 14.19 ms | `events_analytics` | 38,912 / 3,361,342 | 38,912 / 3,361,448 | false |

**5. 分析。** 四种布局的应用可用 p50 最大最小比在 openGauss 上为 1.069（`list:first`）和 1.087（`list:middle`），在 ClickHouse 上为 1.140 和 1.089。八个目标的 `payload_selected` 均为 false，声明来源表与设计一致，openGauss 精确扫描 256 行。控制变量与机制证据同时成立，该结果是“当前场景未分辨”的正式结论：payload 是否与分析列同表，不改变本工作负载的列表查询成本。

ClickHouse 的读取量与朴素预期相反。保留 payload 列的 `events` 表在 `list:middle` 只扫描 23,947 行、1,800,361 bytes，而三种把 payload 移出的布局各扫描 38,912 行、3,361,448 bytes，多扫描约 62%。机制是 MergeTree 的自适应 granularity，与 payload 是否被读取无关。

| 布局 | 列表来源表 | active part | mark | granule | 表行数 | 每 granule 平均行数 | `list:middle` 扫描行数 |
|---|---|---:|---:|---:|---:|---:|---:|
| `same_table` | `events` | 6 | **24** | **18** | 48,534 | 2,696 | **23,947** |
| `separate` | `events_analytics` | 6 | 17 | 11 | 48,534 | 4,412 | 38,912 |
| `full_core` | `events_core` | 6 | 17 | 11 | 48,534 | 4,412 | 38,912 |
| `asset_ref` | `events_analytics` | 6 | 17 | 11 | 48,534 | 4,412 | 38,912 |

granule 的行数上限与字节上限同时生效，宽表的 payload 列使字节上限先达到，granule 因此更多，机制见[JSON 存储原理](json-storage-principles-2026-09-09.md)。run manifest 的 `access.plans` 记录的 EXPLAIN 计划给出各表的 granule 总数：`events` 为 18，`events_core` 与 `events_analytics` 为 11，`event_payloads` 为 17；mark 数为 granule 数加每个 part 的一个终止 mark，6 个 part 下为 24、17 和 23，与第 4.1 节记录的 mark 数一致。按 48,534 行折算，宽表每个 granule 约 2,696 行，窄表约 4,412 行；granule 覆盖的主键区间越窄，主键裁剪越细，一页 keyset 结果读入的行数越少。计划给出的选中 granule 数属于计划期结果，运行期 `read_rows` 属于执行期结果，两者不相乘推算扫描行数。本轮 run manifest 未记录 `index_granularity_bytes` 的实际取值，mark 数、granule 数和扫描行数三项均为实测值。

`payload_selected` 在四种布局下均为 false，扫描字节也低于分层布局，两项证据共同排除“宽表因读取 payload 而扫描更多”的解释。该效应属于列存的 granule 划分，不适用于 openGauss：后者通过 `(project_id,start_time,event_id)` 复合索引在四种布局下均精确扫描 256 行，与表宽度无关。

openGauss 的访问路径由 `EXPLAIN ANALYZE` 计划和索引扫描计数共同确认，四轮的计划节点一致，覆盖全部 12 个查询目标。

| 布局 | `list:*` 与 `preview:*` | `detail:*` 与 `trace:*` | `batch:main` |
|---|---|---|---|
| `same_table` | `Index Scan using events_list_idx on events` | `Index Scan using events_trace_idx on events` | `Seq Scan on events` |
| `separate` | `Index Scan using events_analytics_list_idx on events_analytics` | `Index Scan using events_analytics_trace_idx on events_analytics` 与 `Index Scan using event_payloads_pkey on event_payloads`，构成 Nested Loop Left Join | `Seq Scan on events_analytics` 与 `Index Scan using event_payloads_pkey on event_payloads` |
| `full_core` | `Index Scan using events_core_list_idx on events_core` | `Index Scan using events_full_trace_idx on events_full` | `Seq Scan on events_full` |
| `asset_ref` | `Index Scan using events_analytics_list_idx on events_analytics` | `Index Scan using events_analytics_trace_idx on events_analytics` | `Seq Scan on events_analytics` |

`batch:main` 的过滤列 `cohort` 不属于第 3.1 节建立的任一索引，160 个对象的全量读取因此顺序扫描整表。

`idx_scan` 的每轮增量如下。

| 布局 | 索引 | 每轮增量 |
|---|---|---:|
| `same_table` | `events_list_idx` | 124 |
| | `events_trace_idx` | 217 |
| | `events_pkey` | 0 |
| `separate` | `events_analytics_list_idx` | 124 |
| | `events_analytics_trace_idx` | 217 |
| | `events_analytics_pkey` | 202 |
| | `event_payloads_pkey` | 2,748 |
| | `event_payloads_list_idx`、`event_payloads_trace_idx` | 0 |
| `full_core` | `events_core_list_idx` | 124 |
| | `events_full_trace_idx` | 217 |
| | `events_core_pkey`、`events_core_trace_idx`、`events_full_list_idx`、`events_full_pkey` | 0 |
| `asset_ref` | `events_analytics_list_idx` | 124 |
| | `events_analytics_trace_idx` | 217 |
| | `assets_pkey` | 1,692（三轮）、1,766（一轮） |
| | `events_analytics_pkey` | 0 |

除 `asset_ref` 的 `assets_pkey` 外，每个索引的增量在四轮中完全相同：列表索引 124、Trace 索引 217。该计数是整轮增量，覆盖载入、数据集核对、预热、正式样本和清理，不是单个样本的读取量。

`full_core` 的增量在两张表之间完全分开：列表索引的 124 次全部落在 `events_core`，Trace 索引的 217 次全部落在 `events_full`，两张表的其余四个索引均为 0。Core 与 Full 的职责划分按设计生效。

`asset_ref` 的 `assets_pkey` 在三轮为 1,692，在一轮为 1,766。四轮的正式样本数均为 335、resolver 请求数均为 1,040，该差值出现在计时样本之外。

### 4.3 Preview 查询

**1. 场景设计。** 使用与列表查询相同的过滤、排序和分页，仅把预先保存的 200 字符 preview 加入投影，payload 仍投影为类型化 NULL。四种布局返回相同的 UTF-8 文本，不在查询内重新扫描完整 payload 计算 preview。ClickHouse 查询在自然稳定的少量 part 上执行，四轮均未执行 `OPTIMIZE FINAL`，各表的 active part 数见第 4.1 节。每个引擎四轮，每个查询目标每轮预热 1 次、正式测量 30 次；下表的应用可用 p50 为四轮轮级中位数。

**2. 测试目的与预期。** 该场景测量列表结果携带 preview 的增量成本，并确认 Full/Core 的差异来自物理工作集与访问结构，而非只有 Core 预先计算 preview 的不对称实现。

**3. 实例 SQL。**

```sql
-- ClickHouse separate：preview 来自 events_analytics，payload 仍为 NULL
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length, preview, sha256,
       CAST(NULL AS Nullable(String)) AS payload_value
FROM events_analytics
WHERE project_id={project_id:String}
  AND start_time>={start_time:DateTime64(3,'UTC')}
  AND start_time<{end_time:DateTime64(3,'UTC')}
  AND (start_time,event_id)>({cursor_time:DateTime64(3,'UTC')},{cursor_id:String})
ORDER BY start_time,event_id LIMIT {page_size:UInt64}
FORMAT JSONEachRow;
```

**4. 测试结果。** openGauss 结果如下。

| 布局 | `preview:first` 应用可用 p50 | `preview:middle` 应用可用 p50 | 扫描行数 | 扫描字节 | `payload_selected` |
|---|---:|---:|---:|---|---|
| `same_table` | 13.64 ms | 13.85 ms | 256 | 不可观测 | false |
| `separate` | 13.58 ms | 13.65 ms | 256 | 不可观测 | false |
| `full_core` | 13.65 ms | 13.65 ms | 256 | 不可观测 | false |
| `asset_ref` | 13.52 ms | 13.75 ms | 256 | 不可观测 | false |

ClickHouse 结果如下。

| 布局 | `preview:first` 应用可用 p50 | `preview:middle` 应用可用 p50 | `preview:first` 扫描行数 / 字节 | `preview:middle` 扫描行数 / 字节 | `payload_selected` |
|---|---:|---:|---:|---:|---|
| `same_table` | 12.81 ms | 13.22 ms | 12,147 / 901,120 | 23,947 / 1,834,275 | false |
| `separate` | 13.88 ms | 14.50 ms | 38,912 / 3,440,670 | 38,912 / 3,440,976 | false |
| `full_core` | 13.95 ms | 14.64 ms | 38,912 / 3,440,670 | 38,912 / 3,440,976 | false |
| `asset_ref` | 14.45 ms | 14.54 ms | 38,912 / 3,440,670 | 38,912 / 3,440,976 | false |

**5. 分析。** openGauss 四布局的最大最小比为 1.009 和 1.015，ClickHouse 为 1.128 和 1.108，与列表查询同量级。八个目标的 `payload_selected` 同样全部为 false。preview 带来的增量很小：ClickHouse 同一布局的 preview 与列表扫描字节之差在 2,304–79,528 bytes 之间，符合 256 行 × 200 字符预存文本的量级。

四种布局在 preview 场景同样没有区分度。Full/Core 的 Core 表没有获得额外收益，说明该场景的成本不由“是否另建一张不含 payload 的窄表”决定。

### 4.4 单条详情恢复

**1. 场景设计。** 从 truth 固定选择三条可压缩 payload（64 KiB、512 KiB、2 MiB）和一条 512 KiB 高熵 payload。请求携带列表结果可提供的 `project_id`、`trace_id`、`start_time` 和 `event_id`。`same_table` 与 `full_core` 直接读取完整记录，`separate` 组合分析行与 payload 行，`asset_ref` 先读取事件引用与 catalog、再由 resolver 读取本地对象。四种路径都在客户端完成长度与 SHA-256 校验。ClickHouse 查询在自然稳定的少量 part 上执行，四轮均未执行 `OPTIMIZE FINAL`，各表的 active part 数见第 4.1 节。每个引擎四轮，每个 profile 每轮预热 1 次、正式测量 30 次；下表的应用可用、查询完成、客户端恢复和校验四列均为四轮轮级 p50 的中位数。

**2. 测试目的与预期。** 该场景比较单个详情请求的数据库定位、传输、本地读取、组合和校验成本，并分离 payload 大小与压缩率的影响。

**3. 实例 SQL。**

```sql
-- ClickHouse same_table
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length, preview, sha256,
       payload AS payload_value
FROM events
WHERE project_id={project_id:String} AND trace_id={trace_id:String}
  AND start_time={start_time:DateTime64(3,'UTC')} AND event_id={event_id:String}
FORMAT JSONEachRow;

-- ClickHouse separate：左表定位，右表提供 payload
SELECT a.event_id, a.trace_id, a.project_id, a.start_time, a.profile,
       a.content_type, a.encoding, a.content_length, a.preview, a.sha256,
       p.payload AS payload_value
FROM events_analytics AS a
LEFT JOIN event_payloads AS p ON p.event_id=a.event_id
WHERE a.project_id={project_id:String} AND a.trace_id={trace_id:String}
  AND a.start_time={start_time:DateTime64(3,'UTC')} AND a.event_id={event_id:String}
FORMAT JSONEachRow;

-- ClickHouse asset_ref：数据库只返回引用，内容由 resolver 读取
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length, preview, sha256,
       asset_id AS payload_value
FROM events_analytics
WHERE project_id={project_id:String} AND trace_id={trace_id:String}
  AND start_time={start_time:DateTime64(3,'UTC')} AND event_id={event_id:String}
FORMAT JSONEachRow;
```

**4. 测试结果。** openGauss 结果如下，扫描行数在四种布局和四个 profile 下均为 1，扫描字节不可观测。

| profile | 布局 | 应用可用 p50 | 查询完成 p50 | 客户端恢复 p50 | 校验 p50 | resolver 请求数 |
|---|---|---:|---:|---:|---:|---:|
| `text_64k` | `same_table` | 10.88 ms | 2.84 ms | 0.03 ms | 0.26 ms | 0 |
| | `separate` | 10.92 ms | 3.03 ms | 0.03 ms | 0.28 ms | 0 |
| | `full_core` | 10.85 ms | 2.84 ms | 0.03 ms | 0.27 ms | 0 |
| | `asset_ref` | 20.51 ms | 1.84 ms | 10.72 ms | 0.25 ms | 30 |
| `text_512k` | `same_table` | 15.13 ms | 4.96 ms | 0.05 ms | 2.00 ms | 0 |
| | `separate` | 14.87 ms | 5.11 ms | 0.05 ms | 1.98 ms | 0 |
| | `full_core` | **14.58 ms** | 4.85 ms | 0.05 ms | 1.97 ms | 0 |
| | `asset_ref` | 23.29 ms | 1.83 ms | 11.73 ms | 2.03 ms | 30 |
| `text_2m` | `same_table` | 26.14 ms | 9.97 ms | 0.17 ms | 7.99 ms | 0 |
| | `separate` | 27.21 ms | 10.49 ms | 0.18 ms | 8.18 ms | 0 |
| | `full_core` | 26.14 ms | 10.21 ms | 0.17 ms | 8.06 ms | 0 |
| | `asset_ref` | 32.57 ms | 1.82 ms | 15.07 ms | 8.06 ms | 30 |
| `entropy_512k` | `same_table` | **14.73 ms** | 4.94 ms | 0.05 ms | 2.02 ms | 0 |
| | `separate` | 15.10 ms | 5.30 ms | 0.05 ms | 2.06 ms | 0 |
| | `full_core` | 14.95 ms | 5.00 ms | 0.05 ms | 2.00 ms | 0 |
| | `asset_ref` | 23.06 ms | 1.82 ms | 11.79 ms | 1.99 ms | 30 |

ClickHouse 结果如下。

| profile | 布局 | 应用可用 p50 | 查询完成 p50 | 客户端恢复 p50 | 校验 p50 | 扫描行数 / 字节 |
|---|---|---:|---:|---:|---:|---:|
| `text_64k` | `same_table` | 9.11 ms | 8.03 ms | 0.18 ms | 0.29 ms | 5,393 / 3,347,384 |
| | `separate` | 10.59 ms | 9.38 ms | 0.19 ms | 0.29 ms | 11,932 / 1,717,705 |
| | `full_core` | **8.67 ms** | 7.53 ms | 0.17 ms | 0.28 ms | 5,393 / 3,347,384 |
| | `asset_ref` | 14.19 ms | 6.24 ms | 6.79 ms | 0.26 ms | 8,192 / 1,467,497 |
| `text_512k` | `same_table` | 14.80 ms | 10.94 ms | 1.08 ms | 1.99 ms | 1,161 / 5,506,186 |
| | `separate` | 17.73 ms | 13.91 ms | 1.05 ms | 1.98 ms | 7,859 / 12,734,884 |
| | `full_core` | **14.73 ms** | 10.68 ms | 1.07 ms | 1.99 ms | 1,161 / 5,506,186 |
| | `asset_ref` | 16.08 ms | 6.20 ms | 7.09 ms | 2.05 ms | 6,144 / 1,102,584 |
| `text_2m` | `same_table` | 36.50 ms | 22.79 ms | 4.23 ms | 8.46 ms | 4,749 / 9,982,294 |
| | `separate` | 36.86 ms | 22.81 ms | 4.29 ms | 8.39 ms | 12,326 / 6,325,425 |
| | `full_core` | 36.71 ms | 22.79 ms | 4.27 ms | 8.48 ms | 4,749 / 9,982,294 |
| | `asset_ref` | **26.41 ms** | 6.18 ms | 10.94 ms | 8.41 ms | 8,192 / 1,467,129 |
| `entropy_512k` | `same_table` | 17.36 ms | 13.60 ms | 1.05 ms | 1.98 ms | 4,469 / 8,362,112 |
| | `separate` | 20.05 ms | 16.08 ms | 1.06 ms | 2.00 ms | 12,591 / 11,320,453 |
| | `full_core` | 17.26 ms | 13.29 ms | 1.07 ms | 2.00 ms | 4,469 / 8,362,112 |
| | `asset_ref` | **16.49 ms** | 6.25 ms | 7.27 ms | 2.11 ms | 8,192 / 1,467,497 |

**5. 分析。** 详情场景按对象大小和引擎分化，方向相反。

openGauss 的 `asset_ref` 在四个 profile 上全部最慢，最大差距出现在 `text_64k`：20.51 ms 对 10.85 ms，最大最小比 1.890。分项数据解释了原因：`asset_ref` 的查询完成 p50 只有 1.84 ms，是四种布局中最低的，但客户端恢复 p50 为 10.72 ms，而数据库内布局仅 0.03 ms。数据库读取被两次额外请求（事件引用与 catalog）和一次本地文件读取取代，后者的固定开销在 64 KiB 对象上无法被摊薄。`same_table` 与 `full_core` 在 openGauss 上未分辨：四个 profile 的两者之差为 0.03、0.55、0.00 和 0.22 ms，方向不一致，`text_64k` 与 `text_512k` 上 `full_core` 更快，`text_2m` 与 `entropy_512k` 上 `same_table` 更快。

ClickHouse 的 `asset_ref` 在 `text_2m` 上最快（26.41 ms 对 36.50–36.86 ms，最大最小比 1.396），在 `text_64k` 上最慢（14.19 ms 对 8.67 ms，最大最小比 1.637）。其查询完成 p50 在四个 profile 上稳定在 6.18–6.25 ms，客户端恢复 p50 随对象大小由 6.79 ms 升至 10.94 ms；数据库内布局的查询完成 p50 则随对象大小由 7.53 ms 升至 22.79 ms。分界点出现在 512 KiB 与 2 MiB 之间：`asset_ref` 的查询完成与客户端恢复之和由 13.03 ms 升至 17.12 ms，数据库内布局的查询完成 p50 增幅更大。分项数据只区分查询完成、客户端恢复和校验三段，未把数据库侧时延分解为解压与列式读取。

`separate` 在 ClickHouse 上于三个 profile 中最慢，扫描行数明显高于 `same_table` 与 `full_core`（`text_512k` 为 7,859 对 1,161），JOIN 右表缺少可用的排序键前缀谓词是主要原因，该机制在第 4.5 节的完整 Trace 场景中表现得更明显。

### 4.5 完整 Trace 恢复

**1. 场景设计。** 从基础数据固定选择 Span 数量位于 p25、p50 和 p95 附近的三个 Trace，实际 Span 数分别为 5、6 和 31，返回其全部普通列、内容元数据和 payload，按 `start_time,event_id` 排序。四种布局的取回路径不同：`same_table` 与 `full_core` 分别从 `events` 与 `events_full` 单表恢复完整记录；`separate` 的过滤谓词只施加在 SQL 左表 `events_analytics`，再以 `p.event_id=a.event_id` 连接 `event_payloads`，该连接列不是 `event_payloads` 排序键 `(project_id,start_time,event_id)` 的前缀；`asset_ref` 先读取事件引用与 catalog，再由 resolver 读取本地对象。ClickHouse 查询在自然稳定的少量 part 上执行，四轮均未执行 `OPTIMIZE FINAL`，各表的 active part 数见第 4.1 节。每个引擎四轮，每个 Trace 每轮预热 1 次、正式测量 30 次；下表的应用可用、查询完成和客户端恢复三列均为四轮轮级 p50 的中位数。

**2. 测试目的与预期。** 该场景测量 Trace 排序、两表组合、Full 单表恢复和多个 Asset 请求的差异，补充单条点查，防止只用 event ID 访问掩盖实际详情读取形态。

**3. 实例 SQL。**

```sql
-- ClickHouse same_table
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length, preview, sha256,
       payload AS payload_value
FROM events
WHERE project_id={project_id:String} AND trace_id={trace_id:String}
  AND start_time>={start_time:DateTime64(3,'UTC')}
  AND start_time<{end_time:DateTime64(3,'UTC')}
ORDER BY start_time,event_id FORMAT JSONEachRow;

-- ClickHouse separate
SELECT a.event_id, a.trace_id, a.project_id, a.start_time, a.profile,
       a.content_type, a.encoding, a.content_length, a.preview, a.sha256,
       p.payload AS payload_value
FROM events_analytics AS a
LEFT JOIN event_payloads AS p ON p.event_id=a.event_id
WHERE a.project_id={project_id:String} AND a.trace_id={trace_id:String}
  AND a.start_time>={start_time:DateTime64(3,'UTC')}
  AND a.start_time<{end_time:DateTime64(3,'UTC')}
ORDER BY a.start_time,a.event_id FORMAT JSONEachRow;
```

**4. 测试结果。** openGauss 结果如下。

| Trace | 布局 | 应用可用 p50 | 查询完成 p50 | 客户端恢复 p50 | 扫描行数 | 扫描字节 |
|---|---|---:|---:|---:|---:|---|
| `p25` | `same_table` | 11.21 ms | 3.12 ms | 0.04 ms | 5 | 不可观测 |
| | `separate` | 11.99 ms | 3.52 ms | 0.05 ms | 5 | 不可观测 |
| | `full_core` | 11.12 ms | 3.16 ms | 0.04 ms | 5 | 不可观测 |
| | `asset_ref` | 20.63 ms | 2.20 ms | 10.52 ms | 5 | 不可观测 |
| `p50` | `same_table` | 15.38 ms | 5.28 ms | 0.07 ms | 6 | 不可观测 |
| | `separate` | 15.56 ms | 5.78 ms | 0.07 ms | 6 | 不可观测 |
| | `full_core` | 15.20 ms | 5.32 ms | 0.07 ms | 6 | 不可观测 |
| | `asset_ref` | 23.42 ms | 2.23 ms | 11.33 ms | 6 | 不可观测 |
| `p95` | `same_table` | 27.13 ms | 10.48 ms | 0.30 ms | 31 | 不可观测 |
| | `separate` | 27.41 ms | 10.74 ms | 0.30 ms | 31 | 不可观测 |
| | `full_core` | 27.05 ms | 10.51 ms | 0.28 ms | 31 | 不可观测 |
| | `asset_ref` | 35.47 ms | 2.34 ms | 16.37 ms | 31 | 不可观测 |

ClickHouse 结果如下。

| Trace | 布局 | 应用可用 p50 | 查询完成 p50 | 客户端恢复 p50 | 扫描行数 / 字节 |
|---|---|---:|---:|---:|---:|
| `p25` | `same_table` | 11.87 ms | 10.69 ms | 0.19 ms | 4,749 / 9,982,294 |
| | `separate` | 59.45 ms | 58.24 ms | 0.23 ms | 54,678 / 132,022,442 |
| | `full_core` | 11.75 ms | 10.61 ms | 0.20 ms | 4,749 / 9,982,294 |
| | `asset_ref` | 13.80 ms | 6.22 ms | 6.50 ms | 6,144 / 1,102,584 |
| `p50` | `same_table` | 18.58 ms | 14.53 ms | 1.11 ms | 5,393 / 13,171,006 |
| | `separate` | 65.33 ms | 61.15 ms | 1.17 ms | 56,726 / 132,368,859 |
| | `full_core` | 18.34 ms | 14.41 ms | 1.07 ms | 5,393 / 13,171,006 |
| | `asset_ref` | **16.76 ms** | 6.33 ms | 7.43 ms | 8,192 / 1,467,497 |
| `p95` | `same_table` | 36.96 ms | 22.46 ms | 4.61 ms | 6,415 / 15,178,796 |
| | `separate` | 96.64 ms | 81.81 ms | 4.66 ms | 56,726 / 132,369,424 |
| | `full_core` | 37.61 ms | 22.83 ms | 4.70 ms | 6,415 / 15,178,796 |
| | `asset_ref` | **33.77 ms** | 6.57 ms | 17.14 ms | 8,192 / 1,468,190 |

**5. 分析。** openGauss 四种布局的扫描行数与 Trace 的实际 Span 数完全一致（5、6、31），`(project_id,trace_id,start_time,event_id)` 复合索引在四种布局上都稳定生效，计划节点与 `idx_scan` 每轮增量见第 4.2 节。`asset_ref` 在三个 Trace 上均最慢，与第 4.4 节同源：查询完成 p50 降至 2.20–2.34 ms，客户端恢复 p50 升至 10.52–16.37 ms；每轮 resolver 请求数在 `p25` 与 `p50` 上均为 30，在 `p95` 上为 60（四轮合计 120、120 和 240），与第 4.4 节单条详情的每轮 30 次同口径。`same_table` 与 `full_core` 在三个 Trace 上的差值为 0.09、0.18 和 0.08 ms，两者未分辨。

ClickHouse 的 `separate` 是全实验最显著的离群值，三个 Trace 的最大最小比达到 5.061、3.899 和 2.862。机制来自 JOIN 执行路径：过滤谓词 `a.project_id`、`a.trace_id` 和时间范围只作用在 SQL 左表 `events_analytics`，JOIN 条件 `p.event_id=a.event_id` 不构成 SQL 右表排序键 `(project_id,start_time,event_id)` 的可用前缀，`event_payloads` 因此没有等价的谓词下推，payload 列被整表扫描。记录的 EXPLAIN 计划为 `Join (JOIN FillRightFirst)`，`event_payloads` 位于计划左输入并全表读取（`Condition: true`，`Granules: 17/17`），带过滤的 `events_analytics` 位于计划右输入（`Granules: 1/11`）。证据是扫描量：`separate` 在 `trace:p50` 扫描 56,726 行、132,368,859 bytes，`same_table` 只扫描 5,393 行、13,171,006 bytes，差距接近 10 倍；客户端恢复 p50 在四种布局上均为 1.07–1.17 ms，差异全部落在查询完成阶段。

同一 JOIN 语句在第 4.4 节的单条详情中只造成较小差距。记录的 `detail:text_512k` 计划显示，`event_id` 等值条件下推到 `event_payloads` 并进入主键判定（`Search Algorithm: generic exclusion search`），但 `event_id` 不是排序键前缀，granule 未被裁剪（`Parts: 6/6`，`Granules: 17/17`）；该场景下 `separate` 扫描 7,859 行，`same_table` 扫描 1,161 行，两者都远低于完整 Trace 场景的 56,726 行。`separate` 的代价由 `event_payloads` 侧的实际扫描量决定，与 payload 大小无关。

### 4.6 批量恢复

**1. 场景设计。** 按固定顺序读取主 payload 集合的全部 160 条原始 bytes，客户端流式计算逐对象和全批次摘要。ClickHouse 查询在自然稳定的少量 part 上执行，四轮均未执行 `OPTIMIZE FINAL`，各表的 active part 数见第 4.1 节。每个引擎四轮，每轮预热 1 次、正式测量 5 次；下表各时延列均为四轮轮级 p50 的中位数。

**2. 测试目的与预期。** 该场景比较顺序导出或离线恢复的有效吞吐和请求数量。SQL 或 resolver 必须实际传输完整内容。

**3. 实例 SQL。**

```sql
-- ClickHouse same_table
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length, preview, sha256,
       payload AS payload_value
FROM events
WHERE cohort={cohort:String} AND sha256 IS NOT NULL
ORDER BY start_time,event_id FORMAT JSONEachRow;

-- openGauss asset_ref：数据库只返回引用
SELECT event_id, trace_id, project_id, start_time, profile,
       content_type, encoding, content_length, preview, sha256,
       asset_id AS payload_value
FROM events_analytics
WHERE cohort=%s AND sha256 IS NOT NULL
ORDER BY start_time,event_id;
```

**4. 测试结果。** 主 cohort 的批量恢复结果如下。

| 引擎 | 布局 | 应用可用 p50 | 查询完成 p50 | 客户端恢复 p50 | 校验 p50 | 有效 MiB/s | 数据库字节 | resolver 字节 | 请求数 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| openGauss | `same_table` | 939.62 ms | 427.24 ms | 15.70 ms | 485.31 ms | 130.37 | 642,712,800 | 0 | 5 |
| | `separate` | 999.53 ms | 483.63 ms | 16.30 ms | 484.44 ms | 122.57 | 642,712,800 | 0 | 5 |
| | `full_core` | 946.92 ms | 429.20 ms | 15.92 ms | 487.41 ms | 129.37 | 642,712,800 | 0 | 5 |
| | `asset_ref` | 1,005.33 ms | 8.78 ms | 499.83 ms | 488.12 ms | 121.85 | 460,000 | 642,252,800 | 805 |
| ClickHouse | `same_table` | 1,821.15 ms | 863.49 ms | 351.47 ms | 515.83 ms | 67.33 | 642,712,800 | 0 | 5 |
| | `separate` | 1,717.76 ms | 805.28 ms | 343.58 ms | 511.98 ms | 71.31 | 642,712,800 | 0 | 5 |
| | `full_core` | 1,842.34 ms | 865.75 ms | 352.64 ms | 510.87 ms | 66.49 | 642,712,800 | 0 | 5 |
| | `asset_ref` | 1,745.31 ms | 12.88 ms | 1,223.38 ms | 508.49 ms | 70.19 | 460,000 | 642,252,800 | 805 |

**5. 分析。** 主 cohort 的四种布局在两个引擎上都没有形成明显差距，最大最小比为 1.070（openGauss）和 1.073（ClickHouse）。校验阶段占用固定成本：openGauss 四种布局为 484.44–488.12 ms，ClickHouse 为 508.49–515.83 ms，与 128.45 MB 内容的 SHA-256 计算量对应，且与布局无关。

`asset_ref` 的成本结构完全不同。数据库只返回 460,000 bytes 引用，查询完成 p50 降至 8.78 ms（openGauss）和 12.88 ms（ClickHouse），全部内容由 800 次 resolver 请求读取 642,252,800 bytes 完成（上表请求数 805 另含 5 次数据库查询），客户端恢复 p50 升至 499.83 ms 和 1,223.38 ms。在 160 个对象、平均 802,816 bytes 的分布下，这一转移的净效果接近持平；第 4.7 节改变对象数量后结论发生变化。

### 4.7 等总字节分布控制

**1. 场景设计。** 使用相同的 80 MiB 原始内容总量，比较 40 条 2 MiB payload（`equal_total_few_large`）与 1,280 条 64 KiB payload（`equal_total_many_medium`）。两组使用与主矩阵相同的四种布局、两个引擎和四阶 Latin square，只执行载入、空间、列表、单条详情和批量恢复。每组每个引擎四轮，非批量目标每轮预热 1 次、正式测量 30 次，批量目标每轮预热 1 次、正式测量 5 次；下表的应用可用 p50 为四轮轮级中位数。

**2. 测试目的与预期。** 该控制用于区分“少量大值”和“较多中等值”带来的 TOAST 指针、payload 表行数、mark 覆盖和 Asset 文件数量差异，判断布局成本由字节总量还是对象数量决定。

**3. 实例 SQL。** 与第 4.6 节相同，仅更换 `cohort` 参数。

**4. 测试结果。** 批量恢复结果如下。两组的 resolver 字节均为 419,430,400，即 400 MiB（5 次正式测量 × 80 MiB）。

| 引擎 | 布局 | `few_large` 应用可用 p50 | `many_medium` 应用可用 p50 | `few_large` 请求数 | `many_medium` 请求数 |
|---|---|---:|---:|---:|---:|
| openGauss | `same_table` | 620.58 ms | 599.05 ms | 5 | 5 |
| | `separate` | 625.91 ms | 605.05 ms | 5 | 5 |
| | `full_core` | 611.73 ms | 599.51 ms | 5 | 5 |
| | `asset_ref` | **582.86 ms** | 2,137.03 ms | 205 | 6,405 |
| ClickHouse | `same_table` | 1,106.58 ms | 1,075.88 ms | 5 | 5 |
| | `separate` | 1,335.73 ms | 1,115.58 ms | 5 | 5 |
| | `full_core` | 1,122.54 ms | 1,070.27 ms | 5 | 5 |
| | `asset_ref` | **777.26 ms** | 8,085.18 ms | 205 | 6,405 |

该场景产生两个口径的倍数，分别陈述。**跨组比值**为同一布局在两组之间的应用可用 p50 之比。

| 引擎 | 布局 | `few_large` p50 | `many_medium` p50 | 跨组比值 |
|---|---|---:|---:|---:|
| openGauss | `asset_ref` | 582.86 ms | 2,137.03 ms | **3.67** |
| ClickHouse | `asset_ref` | 777.26 ms | 8,085.18 ms | **10.40** |

**组内最大最小比**为同一组内最慢布局除以最快布局。

| 引擎 | `few_large` 组内最大最小比 | `few_large` 最快布局 | `many_medium` 组内最大最小比 | `many_medium` 最快布局 |
|---|---:|---|---:|---|
| openGauss | 1.074 | `asset_ref` | **3.567** | `same_table` |
| ClickHouse | 1.718 | `asset_ref` | **7.554** | `full_core` |

`asset_ref` 的分项成本如下。

| 引擎 | 组 | 查询完成 p50 | 客户端恢复 p50 | 校验 p50 | 有效 MiB/s | Asset 对象数 | 载入 Asset 发布 ms | 载入 wall ms |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| openGauss | `few_large` | 7.71 ms | 250.20 ms | 315.08 ms | 137.25 | 40 | 443.54 | 11,329.57 |
| | `many_medium` | 14.35 ms | 1,772.09 ms | 332.21 ms | 37.44 | 1,280 | 4,948.01 | 34,556.51 |
| ClickHouse | `few_large` | 12.25 ms | 430.62 ms | 332.05 ms | 102.92 | 40 | 473.77 | 7,405.46 |
| | `many_medium` | 18.48 ms | 7,713.97 ms | 340.23 ms | 9.89 | 1,280 | 5,296.20 | 34,323.35 |

列表查询在两组中仍无区分度：openGauss 最大最小比 1.018–1.031，ClickHouse 1.091–1.118。单条详情保持主矩阵的方向，openGauss `asset_ref` 在 `many_medium` 的 `detail:text_64k` 为 21.84 ms 对 11.25 ms，ClickHouse 为 13.85 ms 对 8.09 ms。

**5. 分析。** 这是全实验最强的分离信号，且分离方向与字节总量无关。两组的原始内容总量、resolver 传输字节和校验成本几乎相同（校验 p50 在四个目标上为 315.08–340.23 ms），唯一的显著变化是对象数量由 40 变为 1,280，resolver 请求数由 200 变为 6,400（上表请求数 205 和 6,405 各另含 5 次数据库查询）。

`asset_ref` 在 `equal_total_few_large` 中是两个引擎上最快的布局（582.86 ms 与 777.26 ms），在 `equal_total_many_medium` 中变为最慢。跨组比值为 3.67（openGauss）和 10.40（ClickHouse）；组内最大最小比由 1.074 升至 3.567（openGauss）、由 1.718 升至 7.554（ClickHouse）。两个口径回答不同问题：跨组比值度量同一布局对分布变化的敏感度，组内最大最小比度量在给定分布下选错布局的代价。客户端恢复 p50 承担了全部增量：ClickHouse 由 430.62 ms 升至 7,713.97 ms，openGauss 由 250.20 ms 升至 1,772.09 ms。

写入侧出现同方向证据：`many_medium` 的 Asset 发布时间为 4,948.01 ms 和 5,296.20 ms，`few_large` 为 443.54 ms 和 473.77 ms；载入 wall time 由 11,329.57 ms 和 7,405.46 ms 升至 34,556.51 ms 和 34,323.35 ms。三种数据库内布局在两组之间的变化方向相同，幅度不一：openGauss 的三种布局为 −2.00% 至 −3.47%；ClickHouse 的 `same_table` 为 −2.77%、`full_core` 为 −4.66%、`separate` 为 −16.48%（1,335.73 ms 降至 1,115.58 ms）。`separate` 的 −16.48% 与本组的轮间波动同量级：该布局在 `equal_total_many_medium` 的四轮轮级 p50 为 1,248.85、1,099.25、1,035.06 和 1,131.90 ms，最大比最小为 1.207，同组其余三种布局为 1.003–1.074；本实验不能判定该差值超出轮间波动。

结论是每对象固定成本主导 Asset 路径。判断是否外置 payload 时，对象数量是决定性变量，字节总量不是。

### 4.8 混合负载与读取隔离

**1. 场景设计。** 每个目标先预热 30 秒，再测量 300 秒。list 与 preview 各以 20 requests/s 的固定到达率运行，各使用两个并发 worker。两个前台流沿用第 4.2 与 4.3 节的投影：payload 在两者中均投影为类型化 NULL，preview 列在 list 流中投影为类型化 NULL、在 preview 流中返回预存的 200 字符文本，四种布局返回相同的结果形状。在该前台负载之上依次加入一类干扰，每类独立运行，使用全新 namespace 和相同随机种子。五个相按 `quiet`、`detail_2m`、`trace_long`、`batch_loop`、`continuous_ingest` 顺序执行：`quiet` 无干扰；`detail_2m` 加入 1 request/s 的 2 MiB 单条详情；`trace_long` 加入 0.2 requests/s 的长 Trace 恢复；`batch_loop` 加入一个持续循环的批量恢复 worker；`continuous_ingest` 加入 1 block/s 的持续写入，写入源为 45 个符合条件的 block，循环重放。

在固定到达率下，负载生成器未能按计划时刻发出的请求记为 **scheduler drop**：它不是查询失败，也不计入成功样本；本文在两者同时出现时分别统计。前台 list 与 preview 每相各调度 6,000 个请求（300 秒 × 20 requests/s），干扰流的调度数由各自到达率决定。前台 list 与 preview 的 p50、p95、p99 和 `detail_2m`、`trace_long`、`batch_loop` 三个干扰流的 p50、p95 均为应用可用时延；`continuous_ingest` 干扰流的 p50 与 p95 为单个 block 的写入完成时延。

**2. 测试目的与预期。** 该场景验证物理分层是否降低大内容读取或写入对分析查询的干扰。前台报告 p50、p95、p99 和请求完成情况；p99 只在成功样本达到 1,000 个时发布。

**3. 实例 SQL。** 前台使用第 4.2 与 4.3 节的列表和 preview 语句，干扰使用第 4.4、4.5、4.6 节的详情、Trace 和批量语句，`continuous_ingest` 使用第 4.1 节的写入入口。

**4. 测试结果。** 前台 list 的结果如下，每个目标调度 6,000 个请求。

| 相 | 布局 | p50 | p95 | p99 | 成功 | scheduler drop | 查询失败 |
|---|---|---:|---:|---:|---:|---:|---:|
| `quiet` | `same_table` | **14.82 ms** | 18.45 ms | 20.32 ms | 6,000 | 0 | 0 |
| | `separate` | 20.22 ms | 26.51 ms | 30.23 ms | 6,000 | 0 | 0 |
| | `full_core` | 21.26 ms | 28.19 ms | 34.04 ms | 6,000 | 0 | 0 |
| | `asset_ref` | 19.15 ms | 24.67 ms | 28.89 ms | 6,000 | 0 | 0 |
| `detail_2m` | `same_table` | **14.97 ms** | 18.40 ms | 20.30 ms | 6,000 | 0 | 0 |
| | `separate` | 21.01 ms | 27.42 ms | 31.04 ms | 6,000 | 0 | 0 |
| | `full_core` | 21.47 ms | 29.38 ms | 48.65 ms | 5,999 | 1 | 0 |
| | `asset_ref` | 19.48 ms | 25.45 ms | 29.69 ms | 6,000 | 0 | 0 |
| `trace_long` | `same_table` | **14.85 ms** | 18.13 ms | 19.85 ms | 6,000 | 0 | 0 |
| | `separate` | 20.91 ms | 28.14 ms | 34.50 ms | 5,998 | 2 | 0 |
| | `full_core` | 20.54 ms | 29.11 ms | 53.39 ms | 5,998 | 2 | 0 |
| | `asset_ref` | 19.37 ms | 25.05 ms | 29.17 ms | 6,000 | 0 | 0 |
| `batch_loop` | `same_table` | **17.78 ms** | 104.12 ms | 157.43 ms | 5,523 | 477 | 0 |
| | `separate` | 23.73 ms | 124.22 ms | 193.65 ms | 5,347 | 653 | 0 |
| | `full_core` | 23.84 ms | 116.43 ms | 175.32 ms | 5,418 | 582 | 0 |
| | `asset_ref` | 21.43 ms | **28.14 ms** | **39.15 ms** | **5,998** | **2** | 0 |
| `continuous_ingest` | `same_table` | **15.32 ms** | 18.75 ms | 25.55 ms | 6,000 | 0 | 0 |
| | `separate` | 18.73 ms | 25.91 ms | 31.57 ms | 6,000 | 0 | 0 |
| | `full_core` | 18.55 ms | 25.67 ms | 35.41 ms | 6,000 | 0 | 0 |
| | `asset_ref` | 17.57 ms | 23.25 ms | 28.00 ms | 6,000 | 0 | 0 |

前台 preview 的 `batch_loop` 相结果如下，其余相的 drop 不超过 5。

| 布局 | p50 | p95 | p99 | 成功 | scheduler drop | 查询失败 |
|---|---:|---:|---:|---:|---:|---:|
| `same_table` | **17.97 ms** | 107.76 ms | 158.36 ms | 5,495 | 505 | 0 |
| `separate` | 23.76 ms | 126.12 ms | 198.42 ms | 5,334 | 666 | 0 |
| `full_core` | 24.31 ms | 116.86 ms | 170.28 ms | 5,419 | 581 | 0 |
| `asset_ref` | 21.87 ms | **28.11 ms** | **38.18 ms** | **5,999** | **1** | 0 |

干扰流自身的结果如下。

| 相 | 布局 | p50 | p95 | 调度数 | 成功 |
|---|---|---:|---:|---:|---:|
| `detail_2m` | `same_table` | 40.54 ms | 45.12 ms | 300 | 300 |
| | `separate` | 49.28 ms | 60.73 ms | 300 | 300 |
| | `full_core` | 51.19 ms | 72.85 ms | 300 | 300 |
| | `asset_ref` | **38.49 ms** | 44.83 ms | 300 | 300 |
| `trace_long` | `same_table` | **41.47 ms** | 44.59 ms | 60 | 60 |
| | `separate` | 146.86 ms | 180.54 ms | 60 | 60 |
| | `full_core` | 51.36 ms | 64.95 ms | 60 | 60 |
| | `asset_ref` | 46.99 ms | 55.01 ms | 60 | 60 |
| `batch_loop` | `same_table` | **1,906.53 ms** | 2,068.17 ms | 156 | 156 |
| | `separate` | 2,166.33 ms | 2,351.50 ms | 137 | 137 |
| | `full_core` | 2,259.11 ms | 2,358.26 ms | 134 | 134 |
| | `asset_ref` | 2,361.76 ms | 2,498.58 ms | 127 | 127 |
| `continuous_ingest` | `same_table` | 30.82 ms | 50.32 ms | 300 | 300 |
| | `separate` | 47.08 ms | 78.19 ms | 300 | 300 |
| | `full_core` | 52.62 ms | 87.00 ms | 300 | 300 |
| | `asset_ref` | **30.19 ms** | 36.94 ms | 300 | 300 |

前台 p50 相对 quiet 相的变化百分比如下。

| 相 | 前台流 | `same_table` | `separate` | `full_core` | `asset_ref` |
|---|---|---:|---:|---:|---:|
| `detail_2m` | list | +1.01% | +3.91% | +0.99% | +1.72% |
| | preview | −0.50% | +2.73% | +1.49% | +2.39% |
| `trace_long` | list | +0.20% | +3.41% | −3.39% | +1.15% |
| | preview | −0.95% | +3.51% | −2.38% | +1.30% |
| `batch_loop` | list | +19.97% | +17.36% | +12.14% | +11.91% |
| | preview | +13.38% | +15.73% | +13.49% | +13.79% |
| `continuous_ingest` | list | +3.37% | −7.37% | −12.75% | −8.25% |
| | preview | +0.50% | −5.55% | −10.97% | −5.46% |

各相在预热前、测量前和测量后三次采样 ClickHouse 的 active part backlog 与 active merge 数。`quiet`、`detail_2m`、`trace_long` 和 `batch_loop` 四相的 backlog 在三次采样中不变：`same_table` 在 `quiet` 相为 6、其余三相为 5，`separate` 与 `full_core` 为 10，`asset_ref` 为 6。`continuous_ingest` 相的 backlog 随持续写入变化：`same_table` 由 5 增至 6 再增至 8，`separate` 为 10、8、10，`full_core` 由 10 增至 10 再增至 12，`asset_ref` 由 6 降至 5 并保持 5。全部相的全部采样点 active merge 数均为 0。

全部五相合计调度 243,194 个请求，成功 239,715 个，scheduler drop 3,479 个，查询失败 0 个。

**5. 分析。** `batch_loop` 是唯一产生显著扰动的相。`same_table`、`separate` 和 `full_core` 的前台 list p95 由 quiet 相的 18.45–28.19 ms 升至 104.12–124.22 ms，preview p95 由 18.58–28.17 ms 升至 107.76–126.12 ms，list 与 preview 合计 drop 分别为 982、1,319、1,163 次。`asset_ref` 的前台 p95 保持在 28.14 ms 和 28.11 ms，list 与 preview 各只有 2 次和 1 次 drop。该相的 part backlog 在三次采样中不变，active merge 数为 0，前台劣化不来自后台合并。

同一相中 `asset_ref` 自身的批量恢复 p50 为 2,361.76 ms，在四种布局中最慢，`same_table` 为 1,906.53 ms；其完成数也最低（127 对 156）。因此结论必须双向陈述：把 payload 移出数据库把竞争从数据库查询路径转移到本地文件系统与客户端，前台隔离改善，批量路径自身变慢，总竞争没有消除。

`detail_2m` 与 `trace_long` 两相没有产生可观测扰动。相对 quiet 相，前台 list 与 preview 的 p50 变化落在 −3.39% 至 +3.91% 之间，`same_table` 落在 −0.95% 至 +1.01% 之间；变化方向在布局之间不一致（`trace_long` 相 `separate` 为 +3.41%，`full_core` 为 −3.39%）；scheduler drop 最多 5 次（`trace_long` 相的 `full_core` preview）。该幅度在本负载下属于运行间波动，不作为干扰效应量。1 request/s 的 2 MiB 详情与 0.2 requests/s 的长 Trace 对应的字节速率远低于 `batch_loop`，因此结论是干扰强度不足以扰动前台，两相不支持隔离性判断。

`continuous_ingest` 相的 `separate`、`full_core` 和 `asset_ref` 前台 p50 低于 quiet 相（18.73/18.55/17.57 ms 对 20.22/21.26/19.15 ms）。五个相按固定顺序执行，相间比较包含运行顺序与缓存状态的影响，该差值不作为写入干扰降低查询时延的结论。

### 4.9 ClickHouse part 状态控制

**1. 场景设计。** part 状态是跨场景因素，不构成第五种布局。每轮按第 3.3 节定义的四个状态依次取样：暂停 merge 后取碎片态，恢复 merge 并在合并期间取合并中，等待自然稳定取稳定态，执行 `OPTIMIZE FINAL` 后取单 part 态。四个状态的查询来自同一运行序列，四种布局各覆盖 4 个状态，每状态 12 个查询目标各 30 个样本、合计 360 个样本。四个状态的 merge 成本归属不同：碎片态在暂停 merge 的条件下取样，合并成本被推迟到该状态之外；合并中在后台 merge 运行期间取样，合并成本与查询同时发生，落在查询期；稳定态与单 part 态分别在自然合并完成后和 `OPTIMIZE FINAL` 完成后取样，合并成本计入写入完成到查询就绪的区间。

**2. 测试目的与预期。** 该控制用于判断 part 状态与布局选择的相对权重，并确认 part 数不是唯一解释量。

**3. 实例 SQL。** 列表、preview、单条详情、完整 Trace 和批量恢复分别使用第 4.2、4.3、4.4、4.5 和 4.6 节的语句，只改变查询前的物理状态。12 个查询目标为 `list:first`、`list:middle`、`preview:first`、`preview:middle`、四个 `detail` profile、`trace:p25`、`trace:p50`、`trace:p95` 和 `batch:main`；本节的时延表给出列表、单条详情、完整 Trace 和批量恢复，读取量表按 list、preview、detail、trace 和 batch 五类聚合。

**4. 测试结果。** 各状态的物理结构如下。

| 布局 | 表 | 碎片态 part / mark | 合并中 part / mark | 稳定态 part / mark | 单 part 态 part / mark |
|---|---|---:|---:|---:|---:|
| `same_table` | `events` | 190 / 380 | 190 / 380 | 7 / 24 | 1 / 16 |
| `separate` | `events_analytics` | 190 / 380 | 190 / 380 | 3 / 10 | 1 / 7 |
| | `event_payloads` | 190 / 380 | 190 / 380 | 5 / 22 | 1 / 15 |
| `full_core` | `events_full` | 190 / 380 | 190 / 380 | 7 / 24 | 1 / 16 |
| | `events_core` | 190 / 380 | 190 / 380 | 3 / 10 | 1 / 7 |
| `asset_ref` | `events_analytics` | 190 / 380 | 190 / 380 | 3 / 10 | 1 / 7 |
| | `assets` | 2 / 4 | 2 / 4 | 2 / 4 | 1 / 2 |

合并中阶段观测到的 active merge 数为 6（`same_table`）、5（`separate`）、7（`full_core`）和 2（`asset_ref`）。

`list:first` 的应用可用 p50 如下。

| 布局 | 碎片态 | 合并中 | 稳定态 | 单 part 态 | 同布局最大最小比 |
|---|---:|---:|---:|---:|---:|
| `same_table` | 38.47 ms | 14.14 ms | 14.06 ms | **12.39 ms** | 3.10 |
| `separate` | 38.53 ms | 14.55 ms | 15.81 ms | **13.28 ms** | 2.90 |
| `full_core` | 38.29 ms | 14.42 ms | 14.51 ms | **12.34 ms** | 3.10 |
| `asset_ref` | 44.99 ms | 15.89 ms | 16.07 ms | **13.75 ms** | 3.27 |

`list:middle` 的应用可用 p50 如下。

| 布局 | 碎片态 | 合并中 | 稳定态 | 单 part 态 | 同布局最大最小比 |
|---|---:|---:|---:|---:|---:|
| `same_table` | 27.02 ms | 15.09 ms | 14.46 ms | **12.97 ms** | 2.08 |
| `separate` | 26.64 ms | 14.51 ms | 14.42 ms | **13.75 ms** | 1.94 |
| `full_core` | 27.11 ms | 14.33 ms | 14.45 ms | **13.42 ms** | 2.02 |
| `asset_ref` | 30.22 ms | 15.77 ms | 15.80 ms | **14.22 ms** | 2.13 |

其余场景的应用可用 p50 如下。

| 场景 | 布局 | 碎片态 | 合并中 | 稳定态 | 单 part 态 |
|---|---|---:|---:|---:|---:|
| `detail:text_64k` | `same_table` | **7.29 ms** | 8.56 ms | 8.76 ms | 9.63 ms |
| | `asset_ref` | 14.13 ms | **12.65 ms** | 13.45 ms | 16.62 ms |
| `detail:text_2m` | `same_table` | **31.24 ms** | 32.09 ms | 33.01 ms | 35.29 ms |
| | `asset_ref` | 27.45 ms | **26.22 ms** | 27.14 ms | 32.89 ms |
| `trace:p50` | `same_table` | **13.78 ms** | 15.28 ms | 16.70 ms | 15.08 ms |
| | `separate` | 59.30 ms | 60.53 ms | **58.84 ms** | 80.79 ms |
| | `full_core` | **14.14 ms** | 14.75 ms | 14.76 ms | 15.26 ms |
| `batch:main` | `same_table` | 1,624.01 ms | **1,470.25 ms** | 1,485.54 ms | 1,843.58 ms |
| | `full_core` | 1,662.33 ms | **1,488.25 ms** | 1,532.47 ms | 2,056.09 ms |

各状态的 `QueryFinish` 读取量中位数如下，按查询类别聚合，格式为行数 / MB。

| 布局 | 查询类别 | 碎片态 | 合并中 | 稳定态 | 单 part 态 |
|---|---|---:|---:|---:|---:|
| `same_table` | list | 28,288 / 2.245 | 33,389 / 2.753 | 29,222.5 / 2.333 | 17,932 / 1.339 |
| | preview | 21,504 / 1.851 | 29,222.5 / 2.351 | 29,222.5 / 2.351 | 17,932 / 1.358 |
| | detail | 256 / 0.633 | 3,543.5 / 8.557 | 3,543.5 / 8.557 | 4,669 / 8.920 |
| | trace | 256 / 0.567 | 4,448 / 7.243 | 4,448 / 7.243 | 4,890 / 7.243 |
| | batch | 27,392 / 133.296 | 48,534 / 137.139 | 48,534 / 137.139 | 48,534 / 137.059 |
| `separate` | list | 28,288 / 2.245 | 41,984 / 3.632 | 41,984 / 3.632 | 20,480 / 1.306 |
| | preview | 21,504 / 1.851 | 41,984 / 3.711 | 41,984 / 3.711 | 20,480 / 1.347 |
| | detail | 512 / 0.646 | 13,756 / 10.713 | 13,605 / 9.561 | 12,430 / 8.625 |
| | trace | 48,790 / 130.974 | 56,726 / 132.372 | 56,726 / 132.372 | 56,726 / 131.734 |
| | batch | 75,926 / 135.773 | 96,918 / 139.637 | 96,918 / 139.637 | 97,068 / 139.664 |
| `full_core` | list | 28,288 / 2.245 | 41,984 / 3.632 | 41,984 / 3.632 | 20,480 / 1.306 |
| | preview | 21,504 / 1.851 | 41,984 / 3.711 | 41,984 / 3.711 | 20,480 / 1.347 |
| | detail | 256 / 0.633 | 3,543.5 / 8.557 | 3,543.5 / 8.557 | 4,669 / 8.920 |
| | trace | 256 / 0.567 | 4,448 / 7.243 | 4,448 / 7.243 | 4,890 / 7.243 |
| | batch | 27,392 / 133.296 | 48,534 / 137.139 | 48,534 / 137.139 | 48,534 / 137.059 |
| `asset_ref` | list | 28,288 / 2.245 | 41,984 / 3.632 | 41,984 / 3.632 | 20,480 / 1.306 |
| | preview | 21,504 / 1.851 | 41,984 / 3.711 | 41,984 / 3.711 | 20,480 / 1.347 |
| | detail | 256 / 0.045 | 8,704 / 1.561 | 8,704 / 1.561 | 8,192 / 0.961 |
| | trace | 256 / 0.045 | 8,192 / 1.471 | 8,192 / 1.471 | 8,192 / 0.790 |
| | batch | 27,392 / 5.098 | 48,384 / 9.106 | 48,384 / 9.106 | 48,534 / 9.134 |

`asset_ref` 的 detail、trace 和 batch 只读取引用与 catalog，读取字节因此远低于其余三种布局。

**5. 分析。** 列表查询对 part 状态高度敏感，且四种布局方向一致：碎片态相对单 part 态的比值为 3.10、2.90、3.10 和 3.27（`list:first`）以及 2.08、1.94、2.02 和 2.13（`list:middle`）。本控制内同一状态下四种布局的差距为 1.114–1.175（`list:first`）和 1.096–1.134（`list:middle`）；第 4.2 节主矩阵在稳定 part 下的同引擎对应比值为 1.140（`list:first`）和 1.089（`list:middle`）。part 状态的影响约为布局选择的三倍，方向在四种布局中相同，因此是与布局正交的因素。

碎片态到稳定态的收益不来自读取量减少：`same_table` 的 list 查询 `QueryFinish` 中位数由碎片态的 28,288 行、2.245 MB 变为稳定态的 29,222.5 行、2.333 MB，两项均未下降，而 `list:first` 的应用可用 p50 由 38.47 ms 降至 14.06 ms；同期 `events` 的 part 数由 190 降至 7、mark 数由 380 降至 24，说明主要收益来自减少 part 与 mark 的重复读取启动和定位。稳定态到单 part 态的继续收益较小（`list:first` 由 14.06 ms 降至 12.39 ms）。

其他场景的方向并不相同。`detail:text_64k`、`detail:text_2m` 和 `trace:p50` 共 7 行中，单 part 态最慢的有 6 行，例外是 `trace:p50` 的 `same_table`，其最慢状态为稳定态（16.70 ms，单 part 态为 15.08 ms）；最快状态为碎片态的有 4 行，为合并中的有 2 行（`asset_ref` 的 `detail:text_64k` 12.65 ms 与 `detail:text_2m` 26.22 ms），为稳定态的有 1 行（`separate` 的 `trace:p50` 58.84 ms）。`batch:main` 在合并中最快，单 part 态最慢（`full_core` 由 1,488.25 ms 升至 2,056.09 ms）。

读取量证据给出两个相反的方向。范围扫描方面，单 part 态的读取量在四种布局上都最低：list 与 preview 为 17,932–20,480 行、1.306–1.358 MB，稳定态为 29,222.5–41,984 行、2.333–3.711 MB；单 part 态在 `list:first` 与 `list:middle` 上同时最快。点查与 Trace 方面，碎片态的读取量最低：`same_table`、`full_core` 与 `asset_ref` 的 detail 与 trace 中位数均为 256 行，`separate` 的 detail 为 512 行；单 part 态升至 4,669 行（detail）和 4,890 行（trace，`same_table` 与 `full_core`），碎片态在这两类场景上最快。批量恢复在合并中、稳定态和单 part 态读取整表且读取量相同量级（`same_table` 48,534 行、137.059–137.139 MB），碎片态为 27,392 行、133.296 MB，单 part 态在四个状态中仍最慢。

190 个碎片 part 把主键区间切得更细，点查裁剪到最少的行，范围扫描则要支付 190 个 part 的读取启动与 mark 定位成本；单 part 态方向相反，范围扫描读取量最低，点查落入覆盖范围更大的 granule。`OPTIMIZE FINAL` 在列表与 preview 上同时降低读取量和时延，在单条详情、完整 Trace 和批量恢复上提高时延，不是通用优化手段。

`separate` 在 `trace:p50` 的四个状态中均维持 58.84–80.79 ms，与第 4.5 节的 JOIN 路径结论一致：part 合并不改变 JOIN 右表缺少谓词下推的事实。

### 4.10 Asset 故障实验

**1. 场景设计。** 物理布局主矩阵通过后，对 `asset_ref` 独立执行六个确定性故障。每个用例使用独立的 `jsons3_af_<case>_<10hex>` 命名空间，注入后记录 resolver 分类、`assets` 状态转换、事件可见性、核对器输出，再执行恢复动作并重新核对。truth 只在独立验证阶段判断最终结果。

**2. 测试目的与预期。** 该实验验证 Asset 分层在缺失、损坏、元数据不一致和部分失败时能否保持明确状态，并确认失败不会被当作成功内容返回。该实验不计算时延，不产生性能排名。

**3. 实例 SQL。** resolver 按事件引用读取 catalog 行后核对元数据，再读取本地对象。

```sql
SELECT asset_id, sha256, content_type, encoding, content_length, storage_path, status
FROM assets
WHERE asset_id = :asset_id;
```

**4. 测试结果。** 六个用例在 openGauss 和 ClickHouse 上的结果完全一致。

| 用例 | 注入点 | resolver 分类 | 最终状态 | 状态转换 | 事件可见 | 内容可见 | orphan | 恢复动作 | 恢复后内容可见 | orphan（恢复后） |
|---|---|---|---|---|---|---|---:|---|---|---:|
| `missing` | `remove_published_object` | `missing` | `available` | `available` → `available` | 是 | 否 | 0 | `restore_missing_object` | 是 | 0 |
| `corrupt` | `modify_published_bytes` | `corrupt` | `available` | `available` → `available` | 是 | 否 | 0 | `replace_corrupt_object` | 是 | 0 |
| `metadata_mismatch` | `replace_catalog_metadata` | `metadata_mismatch` | `available` | `available` → `available` | 是 | 否 | 0 | `restore_catalog_metadata` | 是 | 0 |
| `upload_then_db_failure` | `fail_after_object_upload` | `missing` | `absent` | `absent` → `absent` | 否 | 否 | 1 | `remove_orphan_object` | 否 | 0 |
| `publish_failure` | `fail_pending_publication` | `failed` | `failed` | `pending` → `failed` | 是 | 否 | 0 | `confirm_failed_publication` | 否 | 0 |
| `delete_failure` | `fail_deleting_object_removal` | `deleting` | `deleting` | `available` → `deleting` | 是 | 否 | 0 | `confirm_delete_failure_state` | 否 | 0 |

两个引擎共 12 个用例的 `validation_errors` 为空、`execution_error` 为空，`namespace_removed` 全部为 true。

**5. 分析。** 六个用例全部符合第 2.2 节声明的状态语义，两个引擎无差异。三类与对象或元数据相关的故障（`missing`、`corrupt`、`metadata_mismatch`）在 catalog 状态仍为 `available` 时被 resolver 拦截，内容不可见而事件可见；错误分类来自引用与 catalog、对象 bytes 之间的独立核对，不依赖 catalog 状态字段。

`upload_then_db_failure` 是唯一产生 orphan 的用例：对象已上传但事件引用写入失败，核对器识别出 1 个不可达对象，事件不可见，恢复后 orphan 归零。该用例证明写入顺序为“先发布对象、后写事件引用”时，失败模式是可回收的孤儿对象，而非可见的悬空引用。

`publish_failure` 与 `delete_failure` 保持 `failed` 和 `deleting` 两个可诊断状态，恢复动作只确认状态，不发布成功结果。

## 5. 跨场景分析

### 5.1 布局选择

| 工作负载 | openGauss | ClickHouse | 证据 |
|---|---|---|---|
| 载入 | **`same_table`** | **`same_table`** | 写入顺序两引擎一致，增量对应额外写入步骤 |
| 库内空间 | **`asset_ref`**，其次 `same_table` | **`asset_ref`**，其次 `same_table` | 双表布局复制索引或分析列 |
| 总物理占用 | **`same_table`** | **`same_table`** | `asset_ref` 为 2.884 倍（openGauss）和 6.921 倍（ClickHouse） |
| 列表与 preview | 四布局未分辨 | 四布局未分辨 | `payload_selected` 全部为 false |
| 单条详情（≤512 KiB） | `same_table` 与 `full_core` 未分辨；避免 `asset_ref` | **`full_core`** | `asset_ref` 的每对象固定成本无法摊薄 |
| 单条详情（2 MiB） | `same_table` 与 `full_core` 未分辨；避免 `asset_ref` | **`asset_ref`** | 数据库内布局的查询完成 p50 随对象增大快于 Asset 路径 |
| 完整 Trace | `same_table` 与 `full_core` 未分辨；避免 `asset_ref` | **`asset_ref`** 或 `full_core`；避免 `separate` | JOIN 右表缺少谓词下推 |
| 批量恢复（大对象） | **`asset_ref`** | **`asset_ref`** | 每对象成本被大对象摊薄 |
| 批量恢复（中等对象） | `same_table` 与 `full_core` 未分辨；避免 `asset_ref` | `same_table` 与 `full_core` 未分辨；避免 `asset_ref` | 对象数量主导 |
| 前台隔离（持续批量读取） | **`asset_ref`** | **`asset_ref`** | 前台 drop 2/1 对 477/653/582 |

### 5.2 长载荷对非载荷查询的影响

列表与 preview 不读取 payload。长载荷通过两条途径影响它们：**存放途径**，即列表来源表是否含 payload；**并发途径**，即同时发生的 payload 读写占用共享资源。两条途径的机制见[阶段三原理与设计](json-storage-stage3-principles-design-2026-09-24.md)第 3.6 节。混合负载的结果来自 ClickHouse。

| 途径 | 引擎 | 比较 | 结果 | 判断 |
|---|---|---|---|---|
| 存放 | openGauss | 主矩阵四布局的应用可用 p50 最大最小比 | 列表 1.069–1.087，preview 1.009–1.015；四布局均扫描 256 行 | 同表 payload 不进入列表读取路径，影响未分辨 |
| 存放 | ClickHouse | 同上 | 列表 1.089–1.140，preview 1.108–1.128；`list:middle` 在 `same_table` 读取 23,947 行，其余三种布局 38,912 行 | payload 列使 granule 更窄，`same_table` 读取量更少，时延差异在 14% 以内 |
| 存放 | ClickHouse | 混合负载 `quiet` 相的前台 list p50 | `same_table` 14.82 ms，其余三种布局 19.15–21.26 ms | 方向与主矩阵一致，`same_table` 最快 |
| 存放 | ClickHouse | part 状态控制的 `list:first` | 同一状态下四布局之比 1.114–1.175，同一布局下四状态之比 2.90–3.27 | part 状态的影响约为布局的三倍 |
| 并发 | ClickHouse | `detail_2m`、`trace_long` 相对 `quiet` 的前台 p50 | −3.39% 至 +3.91%，scheduler drop 不超过 5 次 | 干扰强度不足，未观测到影响 |
| 并发 | ClickHouse | `batch_loop` 相的前台 list | 三种库内布局 p95 104.12–124.22 ms，drop 477、653、582 次；`asset_ref` p95 28.14 ms，drop 2 次 | 持续批量读取显著干扰前台 |
| 并发 | ClickHouse | `continuous_ingest` 相的前台 list | p50 相对 `quiet` 为 −12.75% 至 +3.37%，drop 0 次 | 1 block/s 的写入未产生可观测干扰 |

本工作负载上，长载荷对非载荷查询的显著影响只出现在持续批量读取与前台并发时。同表存放本身不拖慢列表：openGauss 未分辨，ClickHouse 的 `same_table` 反而读取更少。`batch_loop` 相中 `separate` 与 `full_core` 的前台 drop 高于 `same_table`（653、582 对 477），把 payload 移入同一引擎内的另一张表不提供前台隔离；`asset_ref` 把 payload 读取移出数据库后前台 list p95 为 28.14 ms，接近 quiet 相的 24.67 ms，代价是自身批量恢复变慢（第 5.3 节）。

混合负载在前台与干扰流共用一个客户端进程时测得：库内布局的批量结果需要在该进程内解析约 128 MB 的 JSON，客户端工作量的差别与数据库负载的差别方向相同，并发途径的幅度以各请求流分进程运行的重测为准。

以上结论成立于 0.33% 的 payload 密度、可完全驻留内存的数据集与每条前台流 2 个 worker。payload 密度升高或数据超出内存时，行存主 tuple 中残留的 payload 字节与缓冲池竞争会放大存放途径，该情形不在本实验覆盖范围内。

### 5.3 Asset 分层的成本转移边界

`asset_ref` 在所有场景中都表现为成本转移，而非成本消除。分项数据给出统一的解释。

| 指标 | 数据库内布局 | `asset_ref` | 场景 |
|---|---:|---:|---|
| 库内空间 | 19.035 MB | 3.283 MB | ClickHouse 主 cohort |
| 总物理占用 | 19.035 MB | 131.733 MB（6.921 倍） | ClickHouse 主 cohort |
| 总物理占用 | 55.976 MB | 161.456 MB（2.884 倍） | openGauss 主 cohort |
| 批量恢复查询完成 p50 | 863.49 ms | 12.88 ms | ClickHouse 主 cohort |
| 批量恢复客户端恢复 p50 | 351.47 ms | 1,223.38 ms | ClickHouse 主 cohort |
| 前台 list drop（`batch_loop`） | 477 | 2 | ClickHouse 混合负载 |
| 自身批量 p50（`batch_loop`） | 1,906.53 ms | 2,361.76 ms | ClickHouse 混合负载 |
| 批量恢复应用可用 p50（40 对象） | 1,122.54 ms | 777.26 ms | ClickHouse 等总字节控制 |
| 批量恢复应用可用 p50（1,280 对象） | 1,070.27 ms | 8,085.18 ms | ClickHouse 等总字节控制 |

转移的方向固定：数据库查询时间与库内空间下降，客户端恢复时间、总物理占用和每对象固定成本上升。转移是否划算取决于对象数量与单对象大小，第 4.7 节给出了判据。

### 5.4 part 状态与布局的正交性

| 因素 | 观测范围 | `list:first` 变化幅度 |
|---|---|---:|
| 布局选择 | part 状态控制内同一状态下四种布局 | 1.114–1.175 |
| part 状态 | part 状态控制内同一布局下四种状态 | 2.90–3.27 |

两个因素的效应方向在四种布局中一致，量级相差约三倍。ClickHouse 的 part backlog 控制优先于布局选择。四个状态按固定顺序执行，较小差异仍可能包含执行顺序与缓存影响。

### 5.5 跨引擎比较边界

固定输入、统一的普通列与查询参数、统一的逻辑结果和统一的应用可用边界（完整 bytes 到客户端并完成 SHA-256 校验）使方案级对照成立。

行存与列存、索引与排序键、执行器、压缩编解码、协议和后台维护仍是不可分离的引擎差异。空间统计口径不同：openGauss 为 relation 已分配字节，ClickHouse 为 active part 压缩字节。openGauss 的扫描字节不可观测，跨引擎的读取量只能按扫描行数对照。本文据此比较完整方案，不建立两个引擎之间的绝对性能排名，也不把单机热查询结果外推为生产性能。

## 6. 正确性、限制与后续测试

主矩阵与两组等总字节控制共 32 个 target、17,560 个正式样本，成功 17,560 个、失败 0 个；每个引擎的样本分布为主 cohort 5,360、`equal_total_few_large` 1,520、`equal_total_many_medium` 1,520、正确性专用 380。ClickHouse part 状态控制含 4 种布局 × 4 个状态 × 360 个样本，共 5,760 个。混合负载调度 243,194 个请求。Asset 故障实验含 2 个引擎 × 6 个用例。正确性专用运行含 8 个 target × 95 个样本，共 760 个，覆盖 `batch:correctness_only`、`detail:unicode_boundary`、`list:first` 和 `list:middle`，失败 0 个。全部正式样本合计 263,035 个。

所有公开结果均满足以下验证条件：

- 48,534 条基础 identity 一一对应，无缺失、额外或重复记录，输入身份 SHA-256 为 `a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8`；
- payload 归属、Trace 集合、content type、encoding、length、preview 和原始 SHA-256 与 truth 一致；
- 列表、preview、单条详情、完整 Trace 和批量恢复的返回集合及顺序与 truth 一致；
- 每个正式样本的响应字节完成校验，8 个正确性 target 的 `bytes_validated` 均为 true；
- 每个正式样本记录声明来源表、`payload_selected` 与扫描行数；ClickHouse 另记录 `QueryFinish` 的扫描行数与字节、active part 数、mark 数和 merge 状态；
- `separate` 的联合水位、Full/Core 的 Core 水位和 Asset 引用水位覆盖本轮全部成功写入；
- Asset 引用、catalog、对象路径和实际 bytes 相互一致；六个故障用例的 `validation_errors` 与 `execution_error` 均为空；
- 临时 schema、database、暂停的 merge 状态和 Asset 命名空间完成清理，故障实验的 6 个命名空间 `namespace_removed` 均为 true。

当前限制如下：

1. 可压缩语料的压缩比远高于生产文本。生成器（`generator/generate_payloads.py:78-95`）用一个 116 字节块循环填充到目标长度后截断，该块由 64 字符 SHA-256 marker 与 52 字符固定短语 `" agent trace tool result observation reasoning step "` 组成；高熵语料由 `seed:event_id:counter` 经 SHA-256 扩展后 URL-safe base64 编码得到。实测 zlib-6 压缩比如下（本环境未提供 zstd 命令行工具，压缩比以 zlib-6 测量，用于刻画语料可压缩性，不等同于 ClickHouse 的 ZSTD(3) 列编码结果）。

   | profile | 原始 bytes | zlib-6 bytes | 压缩比 |
   |---|---:|---:|---:|
   | `text_64k` | 65,536 | 343 | 191.1 |
   | `text_512k` | 524,288 | 1,899 | 276.1 |
   | `text_2m` | 2,097,152 | 7,233 | 289.9 |
   | `entropy_512k` | 524,288 | 397,109 | 1.3 |

   作为对照参考，生产 Agent Trace 文本（LLM prompt、completion 与工具输出）的 ZSTD 压缩比通常为 3–6 倍；该区间是参考口径，不是本实验的测量值。空间与压缩结论因此按双口径给出：ClickHouse 的 `asset_ref` 总占用实测为 `same_table` 的 6.921 倍（131,733,393 bytes 对 19,035,002 bytes），按参考压缩比折算后如下表。

   | 折算压缩比 | 库内总字节（`same_table`） | Asset 方案总字节 | 占用比 |
   |---|---:|---:|---:|
   | 实测 | 19,035,002 | 131,733,393 | **6.921** |
   | 3 倍 | 54,835,989 | 131,733,393 | 2.40 |
   | 4 倍 | 45,879,402 | 131,733,393 | 2.87 |
   | 5 倍 | 40,505,450 | 131,733,393 | 3.25 |
   | 6 倍 | 36,922,815 | 131,733,393 | 3.57 |

   折算只改变数据库内 payload 列的压缩后字节，非 payload 部分（3,241,582 bytes）和 Asset 目录字节保持不变。第 4.1 节的 ClickHouse 空间倍数、第 5.3 节的空间行和第 7 节的空间建议均按该区间理解。openGauss 的 2.884 倍同样依赖该语料，未提供对应的折算表。

2. `detail_2m` 与 `trace_long` 两相的干扰强度不足以扰动前台：前台 p50 相对 quiet 相的变化落在 −3.39% 至 +3.91% 之间，方向在布局之间不一致，scheduler drop 最多 5 次。该幅度按运行间波动处理，不作为干扰效应量；两相不支持隔离性结论，需要提高干扰字节速率后重测。

3. 全部查询在热状态下执行，复用已建立的连接，不清除操作系统缓存；冷缓存与首读成本不在覆盖范围内。

4. 单个 target 内除已定义的混合负载相外没有并发查询混合，全程没有更新与删除产生的 churn，因此不覆盖 MVCC 旧版本、TOAST 更新和 MergeTree mutation 的成本。

5. `asset_ref` 是应用侧的本地内容寻址文件路径，不代表数据库内 LOB 或扩展实现。数据库内 LOB 引用候选命名为 `db_lob_ref`，属于后续阶段。本实验也不覆盖网络、鉴权、跨区域复制、对象存储一致性、保留策略和多租户隔离。

6. 结果来自单机固定版本，不覆盖完整的 Collector/exporter 链路、多节点部署与故障恢复。宿主为 8 核、16,291,948 KiB 内存的单台机器。

## 7. 使用建议

payload 是否外置，按对象数量而非字节总量判断。第 4.7 节在字节总量相同的条件下，把对象数量由 40 提高到 1,280，使 `asset_ref` 的批量恢复由两个引擎上最快的布局变为最慢：同一布局的跨组比值为 10.40（ClickHouse）和 3.67（openGauss），`equal_total_many_medium` 组内的最大最小比为 7.554 和 3.567，写入侧的 Asset 发布时间同步升至约 5 秒。对象数量大而单对象小时保留数据库内布局；对象数量小而单对象达到 MiB 量级时外置可行。

openGauss 的双表布局须核算索引重复成本。每张表的索引字节合计 21.92 MB，其中包含 `(project_id,start_time,event_id)` 与 `(project_id,trace_id,start_time,event_id)` 两个复合索引和 `event_id` 主键索引，`separate` 与 `full_core` 因此把总占用提高到 1.530 倍和 1.586 倍，而列表与详情场景没有相应收益。仅当 payload 表确实不需要独立的列表或 Trace 访问路径时，才应裁剪其索引集合；在当前工作负载下，`same_table` 是 openGauss 的默认选择。

ClickHouse 优先控制 part backlog，其次才考虑布局。在 part 状态控制内，part 状态对列表查询的影响为 2.90–3.27 倍，布局选择只有 1.114–1.175 倍。稳定的少量 part 已经接近单 part 的列表性能，而 `OPTIMIZE FINAL` 在详情、Trace 和批量恢复场景中使延迟上升，不应作为常规操作。

ClickHouse 避免 `separate` 布局承载 Trace 或多行详情查询。JOIN 右表缺少可用的排序键前缀谓词，`trace:p50` 扫描 56,726 行、132,368,859 bytes，p50 为 65.33 ms，而 `same_table` 为 5,393 行、13,171,006 bytes、18.58 ms。需要分层时使用 `full_core`：它在列表、详情和 Trace 三类场景上与 `same_table` 同量级，代价是 17% 的空间与 60% 的写入时间增量。

大对象的持续批量读取与前台分析查询共存时，`asset_ref` 能把前台 p95 保持在 28 ms 量级并把 drop 降至个位数，代价是批量路径自身变慢约 24%。该取舍应按前台 SLO 与批量任务的时限分别核算，不合并为单一评分。

Asset 分层必须实现独立的内容核对。第 4.10 节的三类故障在 catalog 状态仍为 `available` 时被 resolver 拦截，说明状态字段不能替代引用、catalog 与对象 bytes 之间的三方核对。写入顺序采用“先发布对象、后写事件引用”，失败模式收敛为可回收的孤儿对象。

## 参考资料

- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段二报告](json-storage-stage2-report-2026-09-10.md)
- [阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-09.md)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse 自适应 index granularity](https://clickhouse.com/docs/guides/best-practices/sparse-primary-indexes)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/reference/statements/optimize)
- [ClickHouse JOIN 子句](https://clickhouse.com/docs/sql-reference/statements/select/join)
- [openGauss 6.0 TOAST 存储](https://docs.opengauss.org/en/docs/6.0.0/docs/DatabaseAdministrationGuide/toast-technology.html)
- [openGauss 6.0 CREATE INDEX](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/CREATE-INDEX.html)
