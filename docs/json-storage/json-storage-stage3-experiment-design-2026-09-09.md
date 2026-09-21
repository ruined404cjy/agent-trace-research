# Agent Trace JSON 存储阶段三实验设计

> 状态：设计重构完成，待实现与执行
>
> 初始设计日期：2026-09-07；文档修订日期：2026-09-20
>
> 数据路径：独立载入程序
>
> 数据库版本：openGauss 6.0.0；ClickHouse 25.12.11.4
>
> 原理说明：[openGauss JSONB 与 ClickHouse Native JSON](json-storage-principles-2026-09-09.md)
>
> 上游证据：[阶段一报告](json-storage-stage1-report-2026-09-09.md)、[阶段二报告](json-storage-stage2-report-2026-09-10.md)

## 1. 目标与范围

本实验比较 openGauss 与 ClickHouse 中四种长 payload 逻辑布局：同表 payload 列、独立 payload 表、Full/Core 双表和 Asset reference。实验统一输入、写入顺序、查询结果、内容恢复和计时口径，回答以下问题：

1. 列表和预览查询能否在执行层稳定避开完整 payload，以及详情读取是否干扰分析查询；
2. 物理分层产生的写入、后台维护、空间和详情恢复成本；
3. payload 大小、出现分布和可压缩性如何影响各布局；
4. Asset reference 能否正确恢复内容，并在缺失、损坏和部分失败时保持明确状态。

阶段三复用阶段二的普通列、Trace 结构、写入 block、固定窗口和证据门禁，只把长 payload 及其物理布局作为主要变量。数据库内 payload 使用保持 UTF-8 bytes 的 openGauss TEXT 或 ClickHouse String。JSONB、Map、Native JSON 和长 payload 内部路径查询属于阶段二的结构化 JSON 问题，不进入主矩阵。

实验首先比较同一引擎内的四种完整布局。跨引擎数字只表示固定硬件、统一逻辑响应和统一应用可用边界下的方案级结果，同时包含行存或列存、访问结构、压缩、协议、后台维护和客户端处理的影响。

实验使用独立载入程序，不经过 Collector、exporter 或 benchmark。主矩阵中的 Asset 使用本地内容寻址目录，只验证分层机制及本地恢复成本；网络、鉴权、跨区域复制、对象存储一致性、保留策略和多租户隔离不进入性能结论。

## 2. 产物与执行阶段

计划代码位于 `experiments/json-storage-stage3/`：

```text
experiments/json-storage-stage3/
  README.md
  generator/generate_payloads.py
  runner/common.py
  runner/opengauss.py
  runner/clickhouse.py
  runner/assets.py
  runner/run_layout_matrix.py
  runner/run_clickhouse_part_states.py
  runner/run_interference.py
  runner/run_asset_failures.py
  report/summarize.py
  tests/
```

运行产物位于 gitignored 的 `docs/temp/json-storage-stage3/`。每个完成目录包含 `run-manifest.json`；缺少该文件或 `status` 不是 `complete` 的目录不进入汇总。最终结果写入 `docs/json-storage/json-storage-stage3-report-YYYY-MM-DD.md`。

实验按以下顺序执行：

1. 生成冻结输入和独立 truth，完成四种布局的小规模正确性穿刺；
2. 执行一轮 ClickHouse `asset_ref` 候选轮，使用 Latin square 的第一个运行位置、`main` workload 和标准的 30 次非批量、5 次批量测量。该轮的 truth identity、完整响应与已验证 payload 字节、访问计划与查询详情、每个 `QueryFinish`、写入水位与查询就绪水位、无强制合并的自然稳定 part、空间证据和清理全部成立时，正式矩阵才继续；候选轮只作为矩阵门禁，不进入比较性汇总；
3. 执行四轮主矩阵、ClickHouse part 状态控制和混合负载；
4. 物理布局主矩阵通过后，独立执行 Asset 故障实验；
5. 汇总器只接受正确性、机制证据和清理门禁全部通过的运行。

## 3. 数据与语义契约

基础记录复用 `docs/temp/json-storage-stage2/cross-engine-input-20260907/` 中的阶段二冻结输入，共 48,534 个 Span。每 256 行构成一个实验写入 block，共 190 个 block。查询固定使用 `project_id=Leoxx/whowhen_pro` 和时间窗口 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)`；该窗口包含 27,561 行，分页大小固定为 256。阶段三只读取基础记录中的普通列和 Trace 身份，不载入动态 `attributes`。

逻辑记录固定为：

```text
event_id, trace_id, project_id, start_time, profile,
payload, preview, content_type, encoding, content_length, sha256
```

`profile` 是实验生成器写入的 payload 分类，只用于分层选择详情样本和汇总，不作为列表查询的业务过滤条件。`content_type` 固定为 `application/json`，`encoding` 固定为 `utf-8`。preview 从 payload 的逻辑文本内容起始位置按 Unicode code point 截取 200 个字符，不按 UTF-8 bytes 截断。名为 `correctness_only` 的穿刺 workload 覆盖 preview 截断边界处的多字节字符，在每个引擎和每种布局上执行一轮；它不是性能 workload，不进入性能汇总与排名。

### 3.1 主 payload 集合

seed 固定为 `20260907`。主集合包含 160 个彼此不同的 payload，确定性分散到基础记录中：

| profile | 数量 | 每条原始 bytes | 内容特征 | 用途 |
|---|---:|---:|---|---|
| `text_64k` | 40 | 65,536 | 可压缩重复文本 | 当前 64 KiB 截断边界和小型长值 |
| `text_512k` | 40 | 524,288 | 可压缩重复文本 | 中型详情恢复 |
| `text_2m` | 40 | 2,097,152 | 可压缩重复文本 | 大型详情恢复与外置候选 |
| `entropy_512k` | 40 | 524,288 | 高熵 ASCII | 压缩机制控制 |

可压缩 profile 的内容由一个 116 bytes 基本块重复到目标长度后截断构成，基本块为 64 字符的 SHA-256 标记加固定的 52 字符短语，实测 zlib-6 压缩比为 64 KiB 191.1x、512 KiB 276.1x、2 MiB 289.9x，而生产 Agent 文本约为 3–6x。高熵内容由 seed、event ID 和 counter 经 SHA-256 扩展生成，实测压缩比为 1.3x。两类内容都保持每个 payload 唯一，避免内容寻址去重改变主矩阵空间。生成器调整尾部字段，使序列化前的原始 UTF-8 bytes 达到精确目标长度。

### 3.2 等总字节分布控制

布局收益还可能取决于 payload 出现行数。补充控制使用相同的 80 MiB 原始内容总量，比较 40 条 2 MiB payload 的 `equal_total_few_large` 与 1,280 条 64 KiB payload 的 `equal_total_many_medium`。两组使用相同的可压缩内容生成规则，不与高熵控制做全组合。

主集合对应的 `main` 与上述两组构成三个性能 workload。它们在同一 48,534 条 identity 和同一 190 个 block 边界上分别独立载入和测量，载入时不属于当前 workload 的 payload 字段写入 SQL NULL。一次载入全部 1,481 个 payload 会改变被测物理变量，因此三个性能 workload 之间不共享载入。

该控制用于区分“少量大值”和“较多中等值”带来的 heap/TOAST 指针、payload 表行数、ClickHouse mark 覆盖和 Asset 文件数量差异，不用于推导生产环境的 payload 密度阈值。它使用与主矩阵相同的四种布局、两个引擎和四阶 Latin square，但只执行载入、空间、列表、单条详情和批量恢复，不重复 Asset 故障与混合负载。

### 3.3 Truth 与写入顺序

数据目录保存基础记录身份、payload 原始文件、事件到 payload 的确定性映射、truth manifest 和生成 manifest。truth 至少记录 identity、Trace 归属、原始长度、preview、content type、encoding 和原始 SHA-256。实验查询不参与 truth 生成，运行时 Asset resolver 也不能读取 truth。

所有布局使用相同的 190 个 block、行序和 block 边界。payload 在 block 间确定性分散；没有 payload 的记录以 SQL NULL 表示整列缺失，不生成空引用或空 payload 行。完整 Trace 场景使用 Span 数量位于 p25、p50 和 p95 附近的三个 Trace，按 `trace_id` 打破并列，并把所选 Trace 的 payload 数量、profile 和总字节固化到 truth。

四种布局执行四轮，采用四阶 Latin square 轮换布局顺序，使每个布局在每个运行位置出现一次。openGauss 与 ClickHouse 分别执行轮换，运行顺序及随机种子写入 manifest。

## 4. 物理布局与一致性契约

“同表”描述逻辑 schema，不表示 payload bytes 一定留在原行。openGauss 可把大 TEXT 压缩或移入 TOAST relation；ClickHouse 在同一 data part 中按列保存 payload 数据流。实验必须记录这些实际结构，不能把布局名称当作物理证据。

| layout | 数据组织 | 列表与预览路径 | 详情路径 | 写入完成条件 |
|---|---|---|---|---|
| `same_table` | `events` 同时保存分析列、内容元数据和 payload | 读取 `events` 的分析列或 preview | 从 `events` 恢复完整逻辑记录 | `events` block 成功并可见 |
| `separate` | `events_analytics` 保存分析列和内容元数据；`event_payloads` 只保存定位键、内容元数据和 payload | 读取 `events_analytics` | 组合分析行与 `event_payloads` 内容 | 两表对应 block 成功，联合水位覆盖该 block |
| `full_core` | `events_full` 保存完整逻辑记录；`events_core` 复制列表所需普通列、preview、长度和摘要 | 读取 `events_core` | 从 `events_full` 恢复完整逻辑记录 | Full 与 Core 均可见，Core 水位覆盖 Full |
| `asset_ref` | `events_analytics` 保存分析列和 Asset 引用；同一数据库中的 `assets` 表保存内容元数据、位置和状态；本地内容寻址目录保存 bytes | 读取 `events_analytics` | 查询引用和 `assets`，再由 resolver 读取内容 | 对象已原子发布、`assets.status=available`、事件引用可见 |

Full/Core 主矩阵采用 runner 维护的显式双写和联合水位，使两个引擎使用相同的一致性契约。引擎原生 materialized view 的刷新或插入触发语义如需验证，作为独立机制探针运行，不与主矩阵结果合并。

所有布局使用相同的分析过滤语义。分析表和 Core 面向 `project_id`、`start_time`、`event_id` 排序或建立访问结构；独立 payload 表和 Full 可以增加面向详情定位或 Trace 回查的访问结构。布局专用索引、排序键和 projection 的写入、空间与维护成本必须计入方案，并通过实际执行计划或扫描统计确认生效。

openGauss 记录主表、payload 表、Core、Full、索引和 TOAST relation 的分项空间。ClickHouse 使用 `String CODEC(ZSTD(3))`，记录各表 active part、mark、压缩前后列字节和 merge 状态。Asset 方案分别记录数据库、catalog 和本地目录字节。

Asset reference 固定为：

```json
{
  "$ref": "asset:sha256:<digest>",
  "content_type": "application/json",
  "encoding": "utf-8",
  "content_length": 524288,
  "preview": "..."
}
```

运行时 resolver 根据事件引用和数据库 `assets` 行定位内容，核对两者的 content type、encoding、长度和 SHA-256，再读取本地对象。truth 只在独立验证阶段判断最终结果，不参与定位、状态转换或错误分类。

`asset_ref` 对每个被引用对象执行一次 catalog 查询；同一个逻辑查询内的全部 catalog 查询在两个引擎上复用同一个查询范围的 catalog 连接。该契约保持查询真值、对象数量、resolver 的本地文件读取次数和 catalog 与内容目录的归属边界不变，同时不让连接建立与拆除的传输开销随对象数量放大。

## 5. 写入、维护与计时口径

### 5.1 写入状态

一个布局涉及的所有表或 Asset 组件完成 block 写入并满足第 4 节的联合水位，称为**写入完成**。写入完成时间覆盖客户端行构造、数据库协议、双表写入、本地 Asset 写入和状态发布中属于该布局的必要步骤。

在写入完成基础上，openGauss 执行 `ANALYZE`；ClickHouse 恢复正常后台 merge，并等待 active merge 连续三次为空且 active part 数不再变化；Full/Core 和 Asset 的联合水位也必须满足。完成这些查询前维护的状态称为**查询就绪**。

ClickHouse 主矩阵使用自然稳定的少量 part 作为查询就绪状态，不执行 `OPTIMIZE FINAL`。单 part 仅用于 part 状态控制，避免把日常查询不要求的强制合并写入主结果。

载入同时报告：

- 写入完成 wall time、rows/s、原始 payload MiB/s 和 block p50/p95；
- 从载入开始到查询就绪的总 wall time，以及其中的 `ANALYZE`、merge 等待、Core 水位等待和 Asset 发布分项；
- 客户端提交字节、数据库分配字节、压缩后 active data bytes 和 Asset bytes，不把不同物理口径合为一个数值。

### 5.2 查询状态

数据库或 resolver 返回完整响应后，客户端还可能执行双表组合、Asset 读取、结果规范化、长度核对和 SHA-256 校验。本文使用以下口径：

- **查询完成**：数据库响应已完整读取；Asset 方案同时单独记录引用查询和本地内容读取；
- **客户端恢复**：完成双表组合、resolver 处理和结果规范化；
- **应用可用**：完整 bytes 已完成长度与 SHA-256 核对，可以交给上层逻辑。

完整方案比较使用每个样本从请求提交到应用可用的总时延。查询、resolver、恢复和校验分项仍单独保存，用于解释差异。四种布局都必须让完整 payload 到达客户端后再计算摘要，不能使用服务端摘要代替内容传输。

### 5.3 重复、缓存与统计

列表、preview、单条详情和完整 Trace 每轮预热一次、正式测量 30 次，报告轮内 p50、p95 和范围，再报告四轮中位数。批量恢复每轮预热一次、正式测量 5 次，报告 wall time、MiB/s 和范围。

主矩阵复用连接，不主动清除操作系统缓存，属于固定顺序的热查询对照。首读成本如需观察，使用全新 namespace 的独立运行，不与热查询样本混合。p99 只在固定 offered load 的混合负载中报告，并要求每个目标至少产生 1,000 个成功样本。

批量恢复的峰值内存在全部正式时延样本之外，由一次独立的、通过正确性校验的诊断采集给出，并标注实际观测方法。正式的应用可用样本不启用 Python 分配跟踪，因为逐样本跟踪会按布局改变分配成本。

点查不计算“请求等价速率”。批量恢复吞吐按客户端实际接收的 payload bytes 除以阶段 wall time；混合负载吞吐按成功完成数除以阶段 wall time，并同时报告失败数和 offered load。

## 6. 场景化测试

### 6.1 载入、维护与空间

**场景设计。** 四种布局按固定的 190 个 block 写入，分别记录写入完成和查询就绪。主矩阵的每一轮固定一个引擎、一种布局和一个性能 workload。ClickHouse 主矩阵保持后台 merge 开启；part 状态控制另比较暂停 merge 后的碎片态、恢复后的合并中、自然稳定态和单 part 态。

**目的与预期。** 该场景比较完整方案的写入、异步维护和空间成本。双表、Core 复制和 Asset 发布预计增加必要写入步骤；压缩率和 payload 出现分布可能改变空间及 merge 成本，结果必须由分项字节和后台状态验证。

### 6.2 列表查询

**场景设计。** 在固定项目和时间窗口内按 `start_time,event_id` 排序，使用 keyset 条件返回一页普通列，不选择 preview 或 payload。第一页 cursor 固定为窗口起点之前的哨兵值，后续页 cursor 来自上一页末行；主矩阵测量固定的第一页和窗口中部页。

```sql
SELECT event_id, trace_id, project_id, start_time, profile,
       content_length, sha256
FROM <list_source>
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time
  AND (start_time, event_id) > (:cursor_time, :cursor_id)
ORDER BY start_time, event_id
LIMIT :page_size;
```

**目的与预期。** 该场景先验证同表 payload 列能否被引擎实际裁剪，再比较独立表和 Core 是否进一步减少主表、mark、缓存或 part 启动成本。结果接近时，以读取列、read bytes、TOAST/缓冲访问和计划证明“裁剪有效”，不把无显著差异解释为实验失败。

### 6.3 Preview 查询

**场景设计。** 使用与列表查询相同的过滤、排序和分页，仅增加预先保存的 200 字符 preview。所有布局返回相同 UTF-8 文本，不在查询内重新扫描完整 payload 计算 preview。

**目的与预期。** 该场景测量列表结果携带 preview 的增量成本，并确认 Full/Core 的收益来自物理工作集和访问结构，而非只有 Core 预先计算 preview 的不对称实现。

### 6.4 单条详情恢复

**场景设计。** 从 truth 固定选择三条可压缩 payload 和一条高熵 payload。请求携带列表结果可提供的 `project_id`、`trace_id`、`start_time` 和 `event_id`，分别恢复 64 KiB、512 KiB、2 MiB 和高熵 512 KiB 的完整逻辑记录。

同表和 Full 直接读取完整记录；独立 payload 表组合分析行与 payload 行；Asset 先读取事件引用和 catalog，再由 resolver 读取内容。四种路径均在客户端完成长度和 SHA-256 校验。

**目的与预期。** 该场景比较单个详情请求的数据库定位、传输、本地读取、组合和校验成本，并分离 payload 大小与压缩率的影响。实际访问结构、读取行数、读取字节和返回字节必须与预期一致。

### 6.5 完整 Trace 恢复

**场景设计。** 从基础数据固定选择短、中、长三个 Trace，并保证每个 Trace 包含确定数量和总字节的 payload。查询返回 Trace 中全部普通列、内容元数据和 payload，按 `start_time,event_id` 排序。

**目的与预期。** 该场景测量详情表的 Trace 排序、两表组合、Full 单表恢复和多个 Asset 请求的差异。它补充单条点查，防止只用 event ID 访问掩盖 Agent Trace 的实际详情读取形态。

### 6.6 批量恢复

**场景设计。** 按固定顺序读取主 payload 集合的全部原始 bytes。客户端流式计算逐对象和全批次摘要，摘要计算分项单独记录，但应用可用时间包含必要完整性校验。

**目的与预期。** 该场景比较顺序导出或离线恢复的有效 MiB/s、峰值内存和请求数量。SQL 或 resolver 必须实际传输完整内容，不能只返回 length 或 SHA-256。

### 6.7 混合负载与读取隔离

**场景设计。** 每个目标先预热 30 秒，再测量 300 秒。列表和 preview 各以 20 requests/s 的固定到达率运行，分别使用两个并发 worker；随后保持该前台负载，依次加入 1 request/s 的 2 MiB 单条详情、0.2 requests/s 的长 Trace 恢复、一个持续循环的批量恢复 worker，或 1 block/s 的持续写入。每类干扰独立运行，使用全新 namespace 和相同随机种子，避免上一类干扰留下的 part、缓存或写入状态进入下一类结果。

持续写入只循环完整的 256 行 block：该 block 的全部记录位于前台查询窗口之外，且至少包含一条 `main` payload。冻结输入中满足条件的 block 共 45 个，首个 block 索引为 108。写入流按载入顺序循环重放这 45 个 block，使前台列表与预览的 truth 保持固定，同时写入路径仍然携带长 payload。run manifest 记录选择规则、45 个 block 索引、每个 block 的摘要和循环重放标记。无法构成该集合、集合身份发生变化或前台 truth 受影响时，该阶段不启动。

正式干扰运行的每个阶段使用一个由父进程持有的子进程：子进程执行负载并输出有序事件，父进程负责有界的 terminate、kill 与 join 生命周期，并发布全部产物。内联执行只用于诊断，不产生正式结果。

**目的与预期。** 该场景验证物理分层是否降低大内容读取或写入对分析查询的干扰。报告列表与 preview 的 p50、p95、p99、超时和完成吞吐，同时记录详情吞吐、block 延迟、CPU、内存、I/O、part backlog 和 active merge。没有该场景时，列表列裁剪成功只能证明单查询读取范围，不能证明工作集隔离。

### 6.8 ClickHouse part 状态控制

part 状态是载入、列表、详情和混合负载的跨场景因素，不构成第五种布局。每轮先暂停 merge 形成碎片态，再恢复 merge 并在合并中取样，随后等待自然稳定态；最后可执行 `OPTIMIZE FINAL` 形成单 part 控制。四个状态的查询来自同一运行序列，报告明确执行顺序和缓存限制，不与主矩阵样本配对合并。

该控制至少覆盖三个数据库内布局和 Asset 的分析表。每个阶段保存 active part 数、mark 数、压缩字节、active merge 和 `QueryFinish`。若单 part 不改善查询，按读取范围、marks、压缩块和缓存证据解释，不把 part 数单独作为性能原因。

## 7. 正确性与机制证据

所有布局必须通过以下门禁：

- 48,534 条基础 identity 一一对应，没有缺失、额外或重复记录；
- payload 归属、Trace 集合、content type、encoding、length、preview 和原始 SHA-256 与 truth 一致；
- 列表、preview、单条详情、完整 Trace 和批量恢复的返回集合及顺序与 truth 一致；
- `separate` 的联合水位、Full/Core 的 Core 水位和 Asset 引用水位覆盖本轮全部成功写入；
- Asset 引用、catalog、对象路径和实际 bytes 相互一致；
- 失败运行完成临时 schema、database、暂停 merge 和 Asset 目录清理，或明确记录无法清理的对象。

`correctness_only` workload 与其他穿刺样本一同通过上述门禁，其截断边界结果只用于判定正确性。

openGauss 保存 DDL、`EXPLAIN ANALYZE`、实际索引扫描统计，以及引擎可提供的缓冲和 relation 访问证据。ClickHouse 保存 DDL、查询计划、`system.query_log` 中正式样本的 `QueryFinish`、read rows/bytes、active part、mark 和 merge 记录。客户端保存响应 bytes、解析或组合时间、resolver 请求和校验时间。

任一结果不符合朴素预期时，先检查 truth、输出字节、执行计划、实际索引扫描、读取列、part/merge 状态和缓存顺序。访问路径未生效或输出契约不一致的结果只作诊断；控制变量与机制证据均成立后，接近结果可以作为“当前场景未分辨”的正式结论。

## 8. Asset 故障实验

Asset catalog 至少保存 `asset_id`、SHA-256、content type、encoding、content length、storage path、status 和更新时间。状态含义如下：

- `pending`：内容正在写入或校验，事件不能把它作为可用内容返回；
- `available`：对象已发布且元数据校验通过，可以被事件引用和解析；
- `failed`：发布或校验失败，保存稳定错误分类；
- `deleting`：删除流程已开始，在对象和引用核对完成前不发布删除完成。

物理布局主矩阵通过后，对 Asset reference 独立执行以下确定性故障：

| 故障 | 预期结果 |
|---|---|
| `available` 引用对应对象缺失 | resolver 返回 `missing`，记录 asset ID 和 digest，不返回成功内容 |
| 对象 bytes 被修改 | 长度或 SHA-256 校验失败，返回 `corrupt` |
| 引用与 catalog 的长度、content type 或 encoding 不一致 | 返回 `metadata_mismatch` |
| 对象上传完成后数据库引用写入失败 | 事件不可见；核对器把没有可达引用的对象识别为 orphan |
| `pending` 对象发布失败 | 状态转为 `failed`，事件读取不返回该对象 |
| `deleting` 状态下对象删除失败 | 保持可诊断的 `deleting` 或 `failed` 状态，不发布删除完成 |

故障实验记录注入点、状态转换、错误分类、可见内容、核对器输出和恢复动作。它验证本地机制，不形成网络重试、鉴权、远端一致性或生产级删除传播结论。

## 9. 指标与 Manifest

每轮记录：

- 主表、payload 表、Core、Full、索引、TOAST relation、active part、catalog 和 Asset 目录的分项空间；
- 写入完成、查询就绪和各维护阶段的 wall time，block 延迟、rows/s、原始 payload MiB/s 和客户端提交字节；
- 查询完成、客户端恢复和应用可用时延，返回 bytes、read rows/bytes、CPU、内存及引擎可提供的 I/O 计数；
- resolver 请求数、本地读取 bytes、内容校验时间和错误分类；
- 数据库备份范围内 bytes、外部 Asset bytes 和两者总量；两引擎空间口径分别解释，不建立类型级压缩排名。

提交字节与返回字节各发布两个显式字段：一个是跨引擎确定性的逻辑编码字节，按该次操作实际涉及的行与列计算；另一个是引擎协议层的 body 字节计数，只在客户端暴露该计数时发布。协议计数不可观测时——openGauss 不暴露该计数——发布 `unavailable`，不使用估算值，也不把两个字段合并或改写为单一的“实际字节数”。

写入放大分别报告客户端提交字节与 truth 原始字节之比、数据库加 Asset 的总物理占用与 truth 原始字节之比。压缩后的物理占用可能小于原始字节，报告使用“物理占用比”，不强行称为放大。

run manifest 至少记录 run ID、状态、完整复现命令、输入与 truth SHA-256、代码/DDL/查询 catalog SHA-256、数据库版本与镜像 digest、宿主资源、layout、轮次、布局顺序、缓存状态、测量次数、访问结构、写入和查询水位、part/merge 状态、正确性结果和清理状态。

## 10. 停止条件与报告边界

出现以下情况时停止对应候选，发布 `status=failed` 的诊断 manifest：

- 输入、truth、DDL 或查询 catalog 身份不一致；
- identity、preview、长度、Trace 集合或原始 SHA-256 门禁失败；
- 双表、Full/Core 或 Asset 无法确定联合水位；
- resolver 把缺失、损坏、未发布或元数据不一致的对象作为成功结果返回；
- 正式性能样本缺少声明的访问路径、`QueryFinish` 或结果字节证据；
- 临时数据库对象、本轮 Asset 目录或暂停的 merge 状态清理失败；
- 继续运行需要改变冻结输入、数据库版本、宿主资源或输出契约。

候选轮若显示某场景没有区分度，先核对机制证据和样本规模。访问路径正确且读取量符合预期时保留该结果；缺少必要控制时先修改设计和 truth，再启动正式矩阵。

阶段三报告只记录当前数据契约、四种布局、场景结果、机制证据、正确性、Asset 故障、适用范围和建议。JSONB、Native JSON、TOAST、MergeTree 与 part 的一般原理引用原理文档；阶段二结果只用于说明已复用的输入和实验口径，不重复搬运。

## 11. 参考资料

- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段一实验设计](json-storage-stage1-experiment-design-2026-09-09.md)
- [阶段二报告](json-storage-stage2-report-2026-09-10.md)
- [阶段二补充实验设计](json-storage-stage2-sup-experiment-design-2026-09-10.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-09.md)
