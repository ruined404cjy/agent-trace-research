# Agent Trace JSON 存储阶段二实验报告

> 实验完成日期：2026-09-14；文档修订日期：2026-09-14
>
> 状态：实验、结果验证和报告均已完成
>
> 数据库版本：openGauss 6.0.0 build `aee4abd5`；ClickHouse 25.12.11.4

配套的[JSON 存储原理](json-storage-principles-2026-09-09.md)解释 JSONB 与 Native JSON 的写入、物理存储、后台维护和查询机制。本文集中说明实验契约、场景、结果和结论。

## 1. 结论

本实验中，结构化 JSON 适合路径过滤和聚合，文本 JSON 适合直接返回完整文档。在包含 27,561 行的查询时间范围内，openGauss JSONB 和 ClickHouse Native JSON 在高密度过滤、低匹配率过滤和数值聚合中均低于同引擎文本表示；带总数的完整文档分页则由 openGauss JSON 和 ClickHouse String JSON 占优。

openGauss 调优结果取决于索引键、查询条件和实际执行协议。覆盖 `project_id`、JSONB 路径值和 `start_time` 的复合表达式 B-tree 将高密度过滤的完整结果获取 p50 从 96.80 ms 降至 16.15 ms，将低匹配率过滤从 89.11 ms 降至 2.87 ms；Trace 复合 B-tree 将完整 Trace 从 20.90 ms 降至 1.61 ms。三轮正式参数化查询均记录到预期的索引扫描增量。整个 JSONB 上的 `jsonb_hash_ops` GIN 也能加速 `@>` containment 谓词，但不能替代表达式索引服务 `#>> =`。

ClickHouse 数值 type hint 将 `experiment.duration_ms` 固定为 `Int64` 子列并减少该场景读取字节，但 7.35 ms 到 7.01 ms 的差异不足以确认稳定收益。将 `trace_id` 插入主排序键的项目与时间字段之间会扩大时间窗口查询的读取范围，该候选不适用本工作负载。

ClickHouse merge 成本必须计入方案。后台 merge 开启时，载入期间观测到正在执行的 merge，String JSON 与 Native JSON 的载入吞吐分别比暂停 merge 时低约 9.2% 和 8.7%；暂停 merge 会留下 190 个 part，并把合并成本及部分查询开销推迟到载入之后。本实验使用 `OPTIMIZE FINAL` 将两张表分别合并为一个 part，以控制后续查询的物理状态；普通查询不要求执行该操作。

跨引擎数字用于比较固定硬件和统一结果语义下的完整方案。结果同时包含行存或列存、索引、排序键、执行器、协议、压缩、维护策略和客户端结果处理的影响，不能解释为 JSONB 与 Native JSON 数据类型的单因素差异。

## 2. 数据与语义契约

### 2.1 冻结输入

数据源是 Hugging Face `Leoxx/whowhen_pro` 的 text split。确定性投影得到 48,534 个 Span，每 256 行组成一个实验写入 block，以下简称 **block**，共 190 个 block。查询固定使用时间范围 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)`，以下简称**固定窗口**；该窗口包含 27,561 行。

本文将键顺序固定、移除非必要空白并保持 UTF-8 字符的确定性 JSON 序列化文本称为 **canonical JSON 文本**。结果正确性由独立程序根据冻结输入预先计算，所得结果以下简称 **truth**；实验查询不参与 truth 的生成。

| 影响实验场景的特征 | 结果 |
|---|---|
| `gen_ai.operation.name=execute_tool` | 20,155 行，占窗口 73.13% |
| `gen_ai.operation.name=chat` | 4,277 行，占窗口 15.52% |
| `failure.mistake_mode=A.3` | 741 行，占窗口 2.69% |
| `gen_ai.output.messages` 非空值 | 4,277 个，canonical JSON 文本共 6,973,065 UTF-8 bytes |
| attributes canonical JSON 文本长度 | p50 1,042；p95 4,663；最大 62,197 UTF-8 bytes |

原数据缺少四种表示都能稳定聚合的纯数值 JSON 路径。载入程序为每行的 `attributes` 增加 `experiment.duration_ms`，值复制自同一行的普通列 `duration_ms`。固定窗口 truth 为 `count=27,561`、`sum=51,129,737`。

```json
{
  "experiment": {
    "duration_ms": 864
  }
}
```

### 2.2 表示与恢复语义

用于执行查询和聚合的表包含相同的普通强类型列和一个 `attributes` 列，以下简称**分析表**。每种结构另设一张按 `event_id` 保存首次解析前 UTF-8 文本的表，以下简称**原文表**。

ClickHouse Native JSON 无法单独保留 JSON null、空对象、空数组和含点号键的全部结构信息。实验增加名为 `fidelity_values` 的 `Map(String,String)` 稀疏补全列，只保存受影响的顶层分支或根对象，以下简称 **Sidecar**。查询将 Native JSON 路径与 Sidecar 合并，以便四种表示返回相同逻辑文档。Sidecar 是本实验的自定义设计，不属于 ClickHouse 官方功能，也不保存输入排版。

首次解析前文本能否完整返回，以原文 SHA-256 为判断依据，本文称为**字节级恢复**；派生后 `attributes` 能否恢复为相同 JSON 值，以 canonical JSON 文本的 SHA-256 为判断依据，本文称为**逻辑文档恢复**。两项检查独立执行。

| 表示 | `attributes` 接口 | 完整逻辑文档恢复 |
|---|---|---|
| openGauss JSON | `JSON` | 解析文本 datum |
| openGauss JSONB | `JSONB` | 遍历二进制文档并序列化 |
| ClickHouse String JSON | `String CODEC(ZSTD(3))` | 解析字符串 |
| ClickHouse Native JSON | `JSON(max_dynamic_paths=32)` | 汇集路径并应用自定义 Sidecar |

## 3. 结构与执行口径

### 3.1 存储结构

不增加查询专用访问结构的 openGauss JSON、openGauss JSONB、ClickHouse String JSON 和 ClickHouse Native JSON 组成**表示对照**。在 JSONB 上增加查询专用索引，或在 Native JSON 上增加 type hint、改变排序键，组成**调优候选**。

| 表示或调优候选 | JSON 或访问结构 | 排序键 |
|---|---|---|
| openGauss JSON | `JSON` | 主键 `event_id` |
| openGauss JSONB | `JSONB` | 主键 `event_id` |
| openGauss JSONB + 查询专用索引 | 两个复合表达式 B-tree、Trace 复合 B-tree、`jsonb_hash_ops` GIN | 主键 `event_id` |
| ClickHouse String JSON | `String CODEC(ZSTD(3))` | `(project_id,start_time,event_id)` |
| ClickHouse Native JSON | `JSON(max_dynamic_paths=32)` | `(project_id,start_time,event_id)` |
| Native JSON + 数值 type hint | ``JSON(max_dynamic_paths=32, `experiment.duration_ms` Int64)`` | `(project_id,start_time,event_id)` |
| Native JSON + Trace 排序键 | `JSON(max_dynamic_paths=32)` | `(project_id,trace_id,start_time,event_id)` |

两个复合表达式 B-tree 分别覆盖 `(project_id, gen_ai.operation.name, start_time)` 和 `(project_id, failure.mistake_mode, start_time)`，服务 `#>> =` 标量等值查询；`jsonb_hash_ops` GIN 建在完整 JSONB 上，服务 `@>` containment 查询。Trace B-tree 覆盖 `(project_id,trace_id,start_time,event_id)`。固定数值路径每行均存在，因此可声明非 Nullable `Int64` type hint；稀疏字符串路径保持自动路径，避免缺失行出现类型默认值。

### 3.2 固定变量

四种表示使用同一行序、block、查询参数和 truth。主要参数为 `project_id=Leoxx/whowhen_pro`、固定窗口、`operation_name=execute_tool`、`failure_mistake_mode=A.3`、固定 Trace ID 和 `page_size=256`。

每个表示或候选执行三轮。除分页外，每个查询每轮预热 1 次、测量 30 次；分页测量 10 次。查询阶段使用两个已建立连接的 worker，不清除操作系统缓存。

### 3.3 计时与证据

分析表和原文表的 190 个 block 全部写入成功，称为**写入完成**。在此基础上，openGauss 执行 `ANALYZE`；ClickHouse 等待后台 merge 空闲，再对两张表执行 `OPTIMIZE FINAL`，分别合并为一个 part。完成对应查询前维护的状态称为**查询就绪**。该状态用于建立受控查询起点，不表示 ClickHouse 日常查询需要执行 `OPTIMIZE FINAL`。

数据库返回完整响应后，客户端还需执行 JSON 解析、Native JSON 与 Sidecar 合并、排序和结果规范化，本文将这一处理阶段称为**客户端恢复**。客户端恢复完成后，结果达到**应用可用**状态，可以交给上层逻辑使用。

查询场景统一使用三个计时口径：

- **查询 p50**：从提交语句到完整读取响应的时延中位数；
- **客户端恢复 p50**：响应读取完成后，主机侧执行 JSON 解析、Native JSON 与 Sidecar 合并及结果规范化的时延中位数；
- **应用可用 p50**：每次样本的查询耗时与客户端恢复耗时相加后，再取中位数；该值不等于前两列中位数的简单相加。

各表示在查询就绪状态下执行的方案比较称为**基本实验**。为量化 ClickHouse part 合并的影响，另以独立运行序列控制四种物理状态，以下简称 **part 状态实验**：暂停 merge 后保留 190 个 part，称为**碎片态**；恢复 merge 并在后台合并期间查询，称为**合并中**；连续 3 次观察不到 `system.merges` 中的 active merge 后，在少量 part 上查询，称为**稳定态**；执行 `OPTIMIZE FINAL` 后在一个 part 上查询，称为**单 part 态**。

基本实验用于比较完整方案，按场景报告查询 p50 或应用可用 p50；part 状态实验只比较同一 ClickHouse 表示在物理状态演进过程中的查询 p50。两组结果来自不同运行序列，不作为配对样本合并计算。openGauss 另保存 `EXPLAIN ANALYZE`、索引定义和 `pg_stat_user_indexes.idx_scan`；ClickHouse 保存 `QueryFinish`、查询计划、part 数和路径清单。

## 4. 场景化测试

### 4.1 载入、维护与空间

**1. 场景设计。** 输入按固定顺序写入 190 个 block，每个 block 同时写分析表和原文表。**写入完成**截止到两张表的 190 个 block 全部写入成功；**查询就绪**在此基础上计入 openGauss `ANALYZE`，或 ClickHouse 等待后台 merge 空闲并对两张表执行 `OPTIMIZE FINAL` 的时间。ClickHouse 还在 merge 开启和暂停两种状态下载入：开启状态允许后台合并与写入并发，预期消耗部分写入资源并减少写入后的 part；暂停状态不执行后台合并，预期提高短期写入吞吐并留下 190 个 part。两种载入状态均不执行 `OPTIMIZE FINAL`。

**2. 测试目的与预期。** 该场景比较满足分析文档与原文恢复契约的方案级载入成本，并分辨后台 merge 在写入期和查询前承担的工作。文本表示预计减少写入期结构转换；JSONB 索引和 Native JSON 路径组织预计增加写入或空间成本。

**3. 实例 SQL。** 每个 block 使用下列入口；Native JSON 还写入 Sidecar 列 `fidelity_values`。

```sql
-- openGauss：同一事务内写入两张表
COPY analytics (
    ingest_seq,event_id,trace_id,span_id,parent_span_id,project_id,
    start_time,end_time,duration_ms,span_type,framework,level,attributes
) FROM STDIN;
COPY raw (event_id,raw_event) FROM STDIN;
ANALYZE analytics;

-- ClickHouse
INSERT INTO analytics FORMAT JSONEachRow;
INSERT INTO raw FORMAT JSONEachRow;

-- 单 part 对照；不作为常规查询就绪要求
OPTIMIZE TABLE analytics FINAL;
OPTIMIZE TABLE raw FINAL;
```

**4. 测试结果。** 表示对照和 JSONB 索引实验均报告两个载入计时终点。查询前维护时间只计入**查询就绪 rows/s**；空间为三轮中位数。

| 结构 | **写入完成** rows/s | **查询就绪** rows/s | 查询前维护 ms | 分析数据 MiB | 合计 MiB |
|---|---:|---:|---:|---:|---:|
| openGauss JSON | **6,311.92** | **6,164.95** | 183.31 | **71.125** | **154.406** |
| openGauss JSONB | 6,110.57 | 5,880.74 | 304.30 | 76.016 | 159.297 |
| openGauss JSONB + 查询专用索引 | 5,388.78 | 5,105.50 | 504.20 | 100.695 | 183.977 |
| ClickHouse String JSON | **6,584.06** | **5,674.52** | 1,197.05 | **15.529** | **33.328** |
| ClickHouse Native JSON | 5,443.31 | 4,639.07 | 1,556.00 | 35.868 | 53.667 |
| Native JSON + 数值 type hint | 5,717.71 | 4,841.87 | 1,517.31 | 35.858 | 53.657 |
| Native JSON + Trace 排序键 | 5,534.07 | 4,686.67 | 1,594.81 | 38.169 | 55.968 |

后台合并对载入的影响如下。开启状态在载入期间观测到最多 2 个 active merge；暂停状态在两张表中各留下 190 个 part。

| 表示 | merge 开启 rows/s | merge 暂停 rows/s | 开启后稳定 part（分析/原文） | 暂停后 part（分析/原文） |
|---|---:|---:|---:|---:|
| String JSON | 6,336.83 | **6,976.56** | 7 / 5 | 190 / 190 |
| Native JSON | 5,334.88 | **5,844.86** | 5 / 5 | 190 / 190 |

**5. 分析。** 两种表示的三轮结果均为 merge 开启时写入吞吐较低。开启目标在写入期已经完成大部分合并，证据是载入期间存在 active merge，而写入结束后的空闲等待仅约 0.21 秒；其资源竞争反映在载入阶段。暂停 merge 虽提高写入吞吐，却把 190 个 part 和后续合并工作交给查询阶段。强制压为单 part 还需约 1.18 秒（String）或 1.32 秒（Native），该成本只属于单 part 实验对照。

JSONB 索引同时增加写入、`ANALYZE` 和空间成本，收益应由对应查询场景判断。openGauss 与 ClickHouse 的空间口径分别是关系已分配字节和 active part 压缩字节，仅作引擎内比较。

### 4.2 普通列分组控制

**1. 场景设计。** 查询只读取 `project_id`、`start_time` 和普通列 `span_type`，不访问 `attributes`。本场景报告查询 p50，不另列分组结果规范化时间。ClickHouse part 状态实验在碎片态、合并中、稳定态和单 part 态执行同一查询；该控制项预期反映每个 part 的固定启动与定位成本，同时不因 String JSON 或 Native JSON 的属性读取方式产生明显差别。

**2. 测试目的与预期。** 该场景用于识别 JSON 表示之外的执行差异。同一引擎内的两种表示应处于同一量级。

**3. 实例 SQL。** 两端查询语义一致。

```sql
-- openGauss
SELECT span_type, count(*)
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time
GROUP BY span_type
ORDER BY span_type;

-- ClickHouse
SELECT span_type, count() AS count
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
GROUP BY span_type
ORDER BY span_type
FORMAT JSONEachRow;
```

**4. 测试结果。** 基本实验结果如下。

| 结构 | **查询 p50** |
|---|---:|
| openGauss JSON | 24.83 ms |
| openGauss JSONB | 25.45 ms |
| ClickHouse String JSON | 7.50 ms |
| ClickHouse Native JSON | 8.13 ms |

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 20.32 ms | 8.96 ms | 8.03 ms | 7.08 ms |
| Native JSON | 23.31 ms | 16.54 ms | 8.02 ms | 7.02 ms |

**5. 分析。** 基本实验在同一引擎内未形成有意义的表示差异，符合查询不读取 JSON 列的设计。part 状态实验中，两种表示从 190 个 part 合并至稳定态后均降至约 8 ms，继续压为单 part 的差异很小；该变化来自每个 part 的读取启动和 mark 定位开销，与 JSON 属性读取无关。该场景只作为后续路径场景的控制项；跨引擎差异还包含存储布局和执行器因素。

### 4.3 高密度路径过滤

**1. 场景设计。** 查询筛选 `gen_ai.operation.name=execute_tool` 后按 `span_type` 分组，命中固定窗口内 20,155 行，占 73.13%。客户端恢复对分组行进行排序和整数规范化，本场景以应用可用 p50 比较完整结果获取成本。openGauss 使用 `(project_id, attributes #>> '{gen_ai,operation,name}', start_time)` 复合表达式 B-tree；JSON 和未增加查询专用索引的 JSONB 作为同引擎对照。ClickHouse String JSON 使用 `JSONExtractString`，Native JSON 读取类型为 `String` 的路径子列。part 状态实验在四种已定义状态下执行同一查询；预期减少碎片 part 可以降低重复的 mark 和数据流定位工作，Native JSON 的路径读取可能更敏感。另用返回单行 `count(*)` 的同类查询比较 73.13%、15.52% 和 2.69% 三种选择率，以排除分组返回形状的影响，以下简称**选择率子场景**。

**2. 测试目的与预期。** 该场景比较文本解析与结构化路径访问，并验证高匹配率路径能否从索引获益。索引收益由正式参数化执行的扫描计数确认；选择率子场景用于判断优化器是否随命中比例改变扫描方式。

**3. 实例 SQL。**

```sql
-- openGauss 索引与正式查询
CREATE INDEX analytics_operation_name_idx ON analytics (
    project_id,
    ((attributes #>> '{gen_ai,operation,name}')),
    start_time
);

SELECT span_type, count(*)
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time
  AND attributes #>> '{gen_ai,operation,name}' = :operation_name
GROUP BY span_type
ORDER BY span_type;

-- ClickHouse String JSON
SELECT span_type, count() AS count
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
  AND JSONExtractString(attributes, 'gen_ai', 'operation', 'name') = {operation_name:String}
GROUP BY span_type
ORDER BY span_type
FORMAT JSONEachRow;

-- ClickHouse Native JSON
SELECT span_type, count() AS count
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
  AND attributes.gen_ai.operation.name.:String = {operation_name:String}
GROUP BY span_type
ORDER BY span_type
FORMAT JSONEachRow;
```

**4. 测试结果。** 基本实验结果如下。

| 结构 | **应用可用 p50** | 访问证据 |
|---|---:|---|
| openGauss JSON | 284.37 ms | 顺序扫描并解析文本 |
| openGauss JSONB | 96.80 ms | 无查询专用索引，顺序扫描 |
| openGauss JSONB + 复合表达式 B-tree | **16.15 ms** | 每轮 31 次正式执行均增加索引扫描计数 |
| ClickHouse String JSON | 80.54 ms | JSON 提取函数 |
| ClickHouse Native JSON | **9.90 ms** | dynamic path |

选择率子场景使用 `count(*)`。第一组结果设置 `plan_cache_mode=force_custom_plan`，但不添加扫描 Hint，由优化器选择扫描方式；第二组在相同查询中加入 `indexscan` Hint，指定索引扫描。

| 路径和值 | 命中行数 | 固定窗口选择率 | 无扫描 Hint p50 | `indexscan` Hint p50 |
|---|---:|---:|---:|---:|
| `operation.name=execute_tool` | 20,155 | 73.13% | 89.99 ms，顺序扫描 | **11.74 ms** |
| `operation.name=chat` | 4,277 | 15.52% | 89.11 ms，顺序扫描 | **3.99 ms** |
| `failure.mistake_mode=A.3` | 741 | 2.69% | **1.33 ms，索引扫描** | 1.78 ms |

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 45.13 ms | 53.11 ms | 48.35 ms | 73.09 ms |
| Native JSON | 41.95 ms | 9.55 ms | 9.81 ms | 8.92 ms |

**5. 分析。** JSONB 避免每行重新解析 JSON 文本，Native JSON 直接读取路径子列，因此两种结构化表示均降低路径提取成本。复合表达式索引把固定的项目、路径值和时间条件放在同一索引键中，与查询谓词保持一致；正式参数化查询的每轮 1 次预热和 30 次测量均产生索引扫描。

强制定制计划与正式参数化执行出现了不同选择：定制计划对 73.13% 和 15.52% 命中率采用顺序扫描，驱动参数化正式查询采用索引。Hint 探针证明索引在当前热数据和单机条件下确有执行收益，但也说明生产判断必须基于实际协议、计划缓存模式、统计信息和成本参数，不能只检查一份 `EXPLAIN`。

Native JSON 从碎片态进入合并中后，读取行数和字节没有下降，查询 p50 却从 41.95 ms 降至 9.55 ms，说明主要收益来自减少 part、mark 和路径数据流的重复定位。String JSON 没有相同趋势，且单 part 态读取量与延迟同时上升；part 合并不能替代逐行文本解析，也不保证单 part 最快。

### 4.4 低匹配率路径过滤

**1. 场景设计。** 查询筛选 `failure.mistake_mode=A.3`，命中 741 行，占固定窗口的 2.69%。客户端恢复对返回的 `event_id` 排序，并生成行数和身份哈希，本场景以应用可用 p50 比较完整结果获取成本。openGauss 标量等值查询使用 `#>> =` 和 `(project_id, attributes #>> '{failure,mistake_mode}', start_time)` 复合表达式 B-tree；GIN 查询使用结果等价的 `@>` containment 谓词和完整 JSONB 上的 `jsonb_hash_ops` 索引。ClickHouse String JSON 使用路径提取函数，Native JSON 读取 dynamic path。part 状态实验在四种已定义状态下执行同一查询；预期减少碎片 part 可以降低 Native JSON 的重复路径定位成本，String JSON 仍可能主要受逐行文本解析影响。

**2. 测试目的与预期。** 该场景验证低匹配率标量路径能否通过表达式 B-tree 缩小候选集，并比较 GIN 在匹配操作符类下的效果。

**3. 实例 SQL。**

```sql
-- 标量等值索引与查询
CREATE INDEX analytics_failure_mode_idx ON analytics (
    project_id,
    ((attributes #>> '{failure,mistake_mode}')),
    start_time
);

SELECT event_id
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time
  AND attributes #>> '{failure,mistake_mode}' = :failure_mistake_mode;

-- 整个 JSONB 上的 GIN 与匹配谓词
CREATE INDEX analytics_attributes_gin_idx
ON analytics USING gin (attributes jsonb_hash_ops);

SELECT event_id
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time
  AND attributes @> '{"failure":{"mistake_mode":"A.3"}}'::jsonb
ORDER BY event_id;
```

**4. 测试结果。** 基本实验结果如下。

| 结构或谓词 | 无查询专用索引的**应用可用 p50** | 使用查询专用索引的**应用可用 p50** | 访问路径 |
|---|---:|---:|---|
| openGauss JSON，`#>> =` | 274.78 ms | — | 顺序扫描并解析文本 |
| openGauss JSONB，`#>> =` | 89.11 ms | 表达式 B-tree：**2.87 ms** | 自然索引扫描 |
| openGauss JSONB，`@>` | 84.81 ms | GIN：**4.69 ms** | `analytics_attributes_gin_idx` |
| ClickHouse String JSON，路径等值 | 71.55 ms | — | JSON 提取函数 |
| ClickHouse Native JSON，路径等值 | **10.51 ms** | — | dynamic path |

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 33.41 ms | 46.84 ms | 43.25 ms | 62.44 ms |
| Native JSON | 29.03 ms | 8.93 ms | 8.98 ms | 7.51 ms |

**5. 分析。** 2.69% 的命中率使自然定制计划也选择复合表达式索引，三轮自然与 Hint 探针均记录完整索引扫描增量。表达式 B-tree 直接服务固定路径的文本等值条件；GIN 的键来自整个 JSONB，并要求查询使用与 `jsonb_hash_ops` 匹配的 containment 操作符。两者解决的谓词不同，当前标量等值场景由表达式索引提供更直接的访问路径。

Native JSON 在稳定态读取的行数和字节高于碎片态，查询 p50 仍由 29.03 ms 降至 8.98 ms，进一步表明大量 part 的固定定位成本主导该路径查询。String JSON 的读取行数和字节随合并后的 mark 范围扩大，延迟也随之上升；本场景没有证据支持对 String JSON 强制合并为单 part。

### 4.5 大值路径投影

**1. 场景设计。** 查询只返回 `gen_ai.output.messages`。固定窗口内有 4,277 个非空值，canonical JSON 文本共约 6.65 MiB。客户端恢复解析投影值，并计算非空数和 canonical JSON 文本的 UTF-8 字节数。ClickHouse part 状态实验在四种已定义状态下执行同一投影；碎片 part 预计增加读取启动和定位成本，但约 7 MiB 的结果编码、传输和客户端解析可能占主导，因此延迟不预期随 part 数严格单调变化。ClickHouse 另将 String JSON 和 Native JSON 的非空结果都转换为 JSON 文本，使两端采用相同返回类型和内容，以下简称**统一文本输出子场景**。

**2. 测试目的与预期。** 该场景比较单路径读取，并分开观察服务端查询、响应读取和客户端恢复。大响应可能使传输及编码成本高于路径定位成本。

**3. 实例 SQL。**

```sql
-- openGauss JSON / JSONB
SELECT attributes #>> '{gen_ai,output,messages}'
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time;

-- ClickHouse String JSON
SELECT JSONExtractRaw(attributes, 'gen_ai', 'output', 'messages') AS value
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
FORMAT JSONEachRow;

-- ClickHouse Native JSON
SELECT attributes.gen_ai.output.messages AS value
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
FORMAT JSONEachRow;
```

**4. 测试结果。** 基本实验结果如下。

| 结构 | **查询 p50** | **客户端恢复 p50** | **应用可用 p50** |
|---|---:|---:|---:|
| openGauss JSON | 397.37 ms | 99.74 ms | 502.33 ms |
| openGauss JSONB | **217.02 ms** | 107.17 ms | **326.90 ms** |
| ClickHouse String JSON | **159.24 ms** | 224.41 ms | 384.80 ms |
| ClickHouse Native JSON | 182.54 ms | **148.50 ms** | **333.66 ms** |

统一文本输出子场景中，String JSON 和 Native JSON 的查询 p50 分别为 140.25 ms 和 **106.47 ms**。

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 205.71 ms | 156.02 ms | 136.46 ms | 150.79 ms |
| Native JSON | 163.64 ms | 157.33 ms | 132.89 ms | 167.58 ms |

**5. 分析。** 表示对照的自然返回类型不同，不能把应用可用差异全部归因于存储读取。String JSON 的服务端查询较低，但客户端解析大文本的时间更高；Native JSON 的服务端需要组织路径值，客户端规范化时间较低。统一文本输出子场景保持逻辑结果和文本输出一致，Native JSON 仍较低，支持其单路径读取优势；该结论限于约 7 MiB 的数组/对象结果。

part 状态实验中，两种表示从碎片态进入稳定态均有改善，但单 part 态再次变慢。该查询返回约 7 MiB 内容，响应编码、传输和客户端解析削弱了 part 定位成本的影响；结果不支持把 part 数作为大值投影延迟的单调预测量。

### 4.6 完整 Trace

**1. 场景设计。** 查询固定 Trace ID，返回按 `start_time,event_id` 排序的 6 个 Span 及完整逻辑属性。客户端恢复统一时间格式、解析完整属性，并对 Native JSON 应用 Sidecar。ClickHouse part 状态实验在四种已定义状态下执行同一查询；减少碎片 part 预计降低普通列筛选和数据流启动成本，但完整属性汇集、Sidecar 返回以及 merge 对 Native JSON 路径表示的重组可能抵消该收益，因此不预期延迟严格随 part 数下降。

**2. 测试目的与预期。** 该场景模拟 Trace 详情读取，验证 openGauss Trace 索引和 ClickHouse 排序键能否缩小候选范围，同时观察完整文档重组成本。

**3. 实例 SQL。**

```sql
-- openGauss
CREATE INDEX analytics_trace_lookup_idx
ON analytics (project_id,trace_id,start_time,event_id);

SELECT start_time, event_id, attributes::text
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time
  AND trace_id = :trace_id
ORDER BY start_time, event_id;

-- ClickHouse Native JSON；String JSON 不返回 fidelity_values
SELECT start_time, event_id, attributes, fidelity_values
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
  AND trace_id = {trace_id:String}
ORDER BY start_time, event_id
FORMAT JSONEachRow;
```

**4. 测试结果。** 基本实验结果如下。

| 结构 | **查询 p50** | **客户端恢复 p50** | **应用可用 p50** | 读取或访问证据 |
|---|---:|---:|---:|---|
| openGauss JSON | 22.27 ms | 0.18 ms | 22.51 ms | 顺序扫描 |
| openGauss JSONB | 20.71 ms | 0.18 ms | 20.90 ms | 顺序扫描 |
| openGauss JSONB + Trace B-tree | **1.40 ms** | 0.18 ms | **1.61 ms** | 每轮 31 次正式执行均增加索引扫描计数 |
| ClickHouse String JSON | **13.21 ms** | **0.36 ms** | **13.65 ms** | 5,464 rows；9.36 MiB |
| ClickHouse Native JSON | 23.65 ms | 0.75 ms | 24.34 ms | 2,824 rows；1.10 MiB |
| Native JSON + Trace 排序键 | 27.80 ms | 0.72 ms | 28.74 ms | 4,129 rows；9.52 MiB |

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 13.18 ms | 9.88 ms | 10.96 ms | 11.11 ms |
| Native JSON | 16.81 ms | 23.16 ms | 22.82 ms | 30.36 ms |

**5. 分析。** openGauss 复合 B-tree 直接覆盖项目、Trace 和时间范围，稳定缩小候选集。ClickHouse Native JSON 读取更少行和字节，但查询时间仍高于 String JSON，说明还存在读取量之外的成本；按该查询的执行路径，这些成本包含完整路径汇集、序列化和 Sidecar 返回。

Trace 排序候选的计划出现 PrimaryKey 和 binary search，但它把 `trace_id` 插入 `project_id` 与 `start_time` 之间，改变了主要时间窗口的连续排序前缀；实际读取量和延迟均未改善。访问结构出现在计划中只证明其被使用，不证明设计有效。

part 状态实验没有显示 Native JSON 的合并收益。其读取范围从碎片态的 256 行、0.89 MiB 扩大到稳定态的 2,932 行、2.05 MiB，并在单 part 态达到 6,582 行、11.58 MiB，查询 p50 随之上升；merge 改变了 mark 覆盖范围，抵消了 part 数减少带来的启动收益。String JSON 的四种状态保持在相近量级。

### 4.7 带总数的偏移分页

**1. 场景设计。** 查询计算固定窗口的精确总数，按 `start_time,event_id` 排序，从约四分之一位置返回 256 行完整逻辑文档。客户端恢复解析页面文档、统一时间格式，并对 Native JSON 应用 Sidecar。ClickHouse part 状态实验在四种已定义状态下执行同一分页；预期从碎片态合并到稳定态可以减少分片读取和排序启动成本，而继续压为单 part 态的边际收益较小，完整文档返回和 Sidecar 恢复仍可能主导总延迟。

**2. 测试目的与预期。** 该场景代表需要总数的列表接口，重点观察排序、完整文档返回和 Sidecar 恢复；结果不代表 keyset pagination。

**3. 实例 SQL。**

```sql
-- openGauss
WITH filtered AS (
    SELECT start_time, event_id, attributes::text AS attributes
    FROM analytics
    WHERE project_id = :project_id
      AND start_time >= :start_time
      AND start_time < :end_time
), numbered AS (
    SELECT count(*) OVER () AS total_rows, start_time, event_id, attributes
    FROM filtered
    ORDER BY start_time, event_id
)
SELECT total_rows, start_time, event_id, attributes
FROM numbered
ORDER BY start_time, event_id
OFFSET (SELECT GREATEST(FLOOR((count(*) + 3) / 4.0) - 1, 0) FROM filtered)
LIMIT :page_size;

-- ClickHouse Native JSON；String JSON 省略 fidelity_values
WITH filtered AS (
    SELECT start_time, event_id, attributes, fidelity_values
    FROM analytics
    WHERE project_id = {project_id:String}
      AND start_time >= {start_time:DateTime64(3, 'UTC')}
      AND start_time < {end_time:DateTime64(3, 'UTC')}
)
SELECT count() OVER () AS total_rows,
       start_time, event_id, attributes, fidelity_values
FROM filtered
ORDER BY start_time, event_id
LIMIT {page_size:UInt64}
OFFSET (SELECT greatest(intDiv(count() + 3, 4) - 1, 0) FROM filtered)
FORMAT JSONEachRow;
```

**4. 测试结果。** 基本实验结果如下。

| 结构 | **查询 p50** | **客户端恢复 p50** | **应用可用 p50** | ClickHouse `read_rows` / `read_bytes` |
|---|---:|---:|---:|---:|
| openGauss JSON | 190.48 ms | 2.15 ms | **192.72 ms** | — |
| openGauss JSONB | 455.87 ms | 2.08 ms | 458.07 ms | — |
| openGauss JSONB + 查询专用索引 | 454.73 ms | 2.05 ms | 456.72 ms | — |
| ClickHouse String JSON | 59.15 ms | 3.22 ms | **62.69 ms** | 34,243 / 43.59 MiB |
| ClickHouse Native JSON | 93.34 ms | 8.34 ms | 101.77 ms | 30,876 / 53.16 MiB |
| Native JSON + 数值 type hint | 91.83 ms | 7.92 ms | 100.20 ms | 30,861 / 53.11 MiB |
| Native JSON + Trace 排序键 | 216.91 ms | 8.00 ms | 224.73 ms | 97,068 / 114.34 MiB |

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 71.89 ms | 43.86 ms | 40.23 ms | 55.91 ms |
| Native JSON | 129.81 ms | 81.68 ms | 77.87 ms | 84.45 ms |

**5. 分析。** openGauss JSONB 的路径访问优势在完整文档序列化中不成立；该查询需要遍历并输出 256 个文档。ClickHouse Native JSON 同时增加服务端查询和客户端恢复时间，且读取字节高于 String JSON，Sidecar 与完整路径汇集的成本没有被较少的读取行数抵消。

Trace 排序候选扩大了读取行数和字节，直接解释其明显退化。part 状态实验中，两种表示从碎片态合并至稳定态均明显改善，说明大量 part 的排序与读取启动成本高于少量 part；单 part 态的读取字节和延迟再次上升，强制从稳定态继续合并没有收益。

### 4.8 数值 JSON 路径聚合

**1. 场景设计。** 查询对每行均存在的 `experiment.duration_ms` 执行非空计数和求和，truth 为 `count=27,561`、`sum=51,129,737`。客户端恢复将单行聚合结果规范化为整数，本场景以应用可用 p50 比较完整结果获取成本。ClickHouse part 状态实验在四种已定义状态下执行同一聚合；预期 Native JSON 的路径子列在减少碎片 part 后降低重复读取启动和路径定位成本，String JSON 则可能继续由逐行文本解析主导。

**2. 测试目的与预期。** 该场景比较文本提取、JSONB 标量转换、Native JSON dynamic path 和显式 `Int64` type hint。type hint 的效果由路径库存、读取量和延迟共同判断。

**3. 实例 SQL。**

```sql
-- openGauss JSON / JSONB
SELECT count(attributes #> '{experiment,duration_ms}'),
       sum((attributes #>> '{experiment,duration_ms}')::bigint)
FROM analytics
WHERE project_id = :project_id
  AND start_time >= :start_time
  AND start_time < :end_time;

-- ClickHouse String JSON
SELECT countIf(JSONHas(attributes, 'experiment', 'duration_ms')) AS non_null_count,
       sum(JSONExtractInt(attributes, 'experiment', 'duration_ms')) AS sum
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
FORMAT JSONEachRow;

-- Native JSON dynamic path
SELECT count(attributes.experiment.duration_ms) AS non_null_count,
       sum(attributes.experiment.duration_ms.:Int64) AS sum
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
FORMAT JSONEachRow;

-- type hint 固定为 Int64 后省略 Dynamic 类型选择
SELECT count(attributes.experiment.duration_ms) AS non_null_count,
       sum(attributes.experiment.duration_ms) AS sum
FROM analytics
WHERE project_id = {project_id:String}
  AND start_time >= {start_time:DateTime64(3, 'UTC')}
  AND start_time < {end_time:DateTime64(3, 'UTC')}
FORMAT JSONEachRow;
```

**4. 测试结果。** 基本实验结果如下。

| 结构 | **应用可用 p50** | 路径归属或读取量 |
|---|---:|---|
| openGauss JSON | 532.97 ms | 文本提取并转为 `bigint` |
| openGauss JSONB | **172.43 ms** | JSONB 标量转为 `bigint` |
| ClickHouse String JSON | 88.26 ms | 42.17 MiB |
| ClickHouse Native JSON | **7.35 ms** | dynamic path；1.43 MiB |
| Native JSON + 数值 type hint | 7.01 ms | 固定 `Int64` 子列；0.95 MiB |

part 状态实验结果如下，表内均为查询 p50。

| 表示 | 碎片态（190 part） | 合并中（190 → 4 part） | 稳定态（4 part） | 单 part 态 |
|---|---:|---:|---:|---:|
| String JSON | 40.41 ms | 63.87 ms | 59.61 ms | 90.23 ms |
| Native JSON | 37.67 ms | 8.59 ms | 7.95 ms | 7.29 ms |

**5. 分析。** 结构化表示避免逐行解析完整文本，是本场景的主要差异。type hint 将该路径从 dynamic/shared 清单移到固定子列，并把读取量降低约三分之一；延迟只变化 0.34 ms，三轮证据不足以区分该收益与运行波动。

part 状态实验中，Native JSON 在读取量没有降低的情况下，从碎片态进入合并中即接近稳定态，说明主要收益来自减少路径子列在多个 part 中的启动和定位。String JSON 的读取字节随合并后的 mark 范围扩大，逐行文本解析成本也没有减少，因此延迟方向相反；Native JSON 的 merge 收益不能外推到 String JSON。

## 5. 跨场景分析

### 5.1 表示选择

| 工作负载 | openGauss | ClickHouse | 证据 |
|---|---|---|---|
| 双表载入与空间 | **JSON** | **String JSON** | 文本表示的写入和空间成本较低 |
| 只读普通列 | JSON 与 JSONB 未分辨 | String 与 Native 未分辨 | 查询不读取 `attributes` |
| 路径过滤与数值聚合 | **JSONB** | **Native JSON** | 避免文本解析；可使用表达式索引或路径子列 |
| 大值单路径投影 | **JSONB** | **Native JSON** | 统一输出控制支持路径读取优势 |
| 完整 Trace | JSONB 配合 Trace B-tree | **String JSON** | 定向定位与完整文档返回的成本不同 |
| 带总数的完整文档分页 | **JSON** | **String JSON** | 避免二进制文档序列化或路径汇集 |

### 5.2 访问结构与 part 合并

| 候选或状态 | 证据 | 判定 |
|---|---|---|
| openGauss 高密度复合表达式 B-tree | 正式参数化执行 96.80 → **16.15 ms**；每轮 31 次索引扫描 | 当前协议下有效；计划模式需持续核对 |
| openGauss 低匹配率复合表达式 B-tree | 89.11 → **2.87 ms**；自然计划和扫描计数一致 | 有效 |
| openGauss `jsonb_hash_ops` GIN | containment 84.81 → **4.69 ms** | 对匹配操作符有效 |
| openGauss Trace 复合 B-tree | 20.90 → **1.61 ms** | 有效 |
| Native JSON 数值 type hint | 读取量下降，7.35 → 7.01 ms | 性能收益未确认 |
| Native JSON Trace 排序键 | 读取范围与延迟扩大 | 无效 |
| ClickHouse 后台 merge 开启 | 写入期 active merge；写入后仅短暂等待 | 合并成本主要计入载入期 |
| ClickHouse `OPTIMIZE FINAL` 单 part | 额外约 1.18–1.32 秒；查询不总是改善 | 仅作受控对照 |

ClickHouse part 状态数据分别列在 4.2–4.8 的对应场景中。合并中阶段从 190 个 part 开始，查询期间最多观测到 6 个 active merge，阶段结束时约有 4 个 part。

Native JSON 的路径过滤和数值聚合对碎片态较敏感，稳定态已接近单 part 态；普通列查询在两种表示上都体现了 part 启动成本。大值投影、完整 Trace 和分页还受到 mark 覆盖范围、响应大小及完整文档重组影响，单 part 态并不稳定占优。String JSON 的路径过滤和聚合也没有随 part 减少而改善，因为 merge 不消除逐行文本解析。

part 合并会同时改变 marks、压缩块、读取行数和缓存状态，单独的 part 数不能预测所有查询。四种状态按碎片态、合并中、稳定态、单 part 态的顺序执行，较小差异仍可能包含执行顺序和缓存影响。

### 5.3 跨引擎比较边界

固定输入、普通列、查询参数、逻辑结果和客户端恢复使方案级对照成立。载入吞吐包含各自协议和客户端行构造；应用可用 p50 包含得到统一逻辑结果所需的恢复；Native JSON Sidecar 成本也计入结果。

行存与列存、索引与排序键、执行器、压缩和维护策略仍是不可分离的引擎差异。空间统计口径也不同。本文据此选择完整方案，不建立 JSONB 与 Native JSON 类型本身的跨引擎速度排名。

## 6. 正确性、限制与后续测试

表示与调优实验包含 7 个结构或候选、每个 3 轮，共 21 个目标运行和 3,990 个正式查询样本。索引访问实验另含 3 轮查询专用索引与无查询专用索引结构的配对；ClickHouse part 状态实验含 3 轮、每轮 4 个目标。所有公开结果均满足以下验证条件：

- 48,534 条分析记录和原文记录无缺失、额外或重复；
- 七个查询结果与 truth 一致；
- 逻辑分析文档与首次解析前原文分别通过 SHA-256 核对；
- openGauss 调优结果同时核对索引定义、计划和实际扫描增量；
- ClickHouse part 状态实验核对 190 part、merge 并发、稳定状态和单 part 状态；
- 临时 schema、database 和暂停的 merge 状态完成清理。

当前限制如下：

1. ClickHouse part 状态实验采用有限数据量的批量载入，尚未覆盖固定 offered load、长时间持续写入、part backlog 上限和追赶时间；
2. merge 的四个查询阶段按物理状态演进顺序执行，缓存影响与状态变化不能完全分离；
3. 当前大值投影返回约 7 MiB 内容，尚缺少少量标量投影；
4. 当前目标路径主要处于 dynamic path，尚缺少同一路径在 type hint、dynamic path 和 shared data 三种归属下的直接对照；
5. 当前分页包含精确总数和偏移，尚缺少 keyset pagination 与顺序导出；
6. 结果来自单机固定版本和热查询，不覆盖完整 Collector/exporter 链路、冷缓存、多节点与故障恢复。

## 7. 使用建议

稳定、高频且类型明确的业务字段继续使用普通强类型列。动态属性分析优先保留 openGauss JSONB 或 ClickHouse Native JSON；完整文档返回频繁时保留文本表示对照。

openGauss 表达式索引应覆盖查询中稳定出现的普通过滤列、JSONB 路径值和范围列。高密度路径也可能受益，但参数化计划、定制计划和 Hint 计划可能不同；上线前应按实际驱动协议核对计划与 `idx_scan`，Hint 只用于诊断。containment 查询使用 GIN 前应核对操作符类和谓词。

ClickHouse 排序键按主导过滤前缀设计。后台 merge 的资源成本应计入写入或查询就绪预算；稳定少量 part 通常已经足够，`OPTIMIZE FINAL` 只在明确需要并验证收益时使用。需要完整文档保真时，应把自定义 Sidecar 的写入、空间、响应和客户端恢复全部计入方案。

签名、字节级审计和精确重放保存首次解析前的原始 UTF-8 bytes。Sidecar 保存 canonical JSON 文本，只承担逻辑文档恢复。

## 参考资料

- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-09.md)
- [阶段二补充实验设计](json-storage-stage2-sup-experiment-design-2026-09-10.md)
- [ClickHouse JSON 数据类型](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/reference/statements/optimize)
- [ClickHouse：避免不必要的 OPTIMIZE FINAL](https://clickhouse.com/resources/engineering/clickhouse-optimize-table-final)
- [openGauss 6.0 JSON/JSONB 类型](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-types.html)
- [openGauss Scan Operation Hints](https://docs.opengauss.org/en/docs/6.0.0-lite/docs/PerformanceTuningGuide/scan-operation-hints.html)
- [openGauss Optimizer GUC Parameter Hints](https://docs.opengauss.org/en/docs/6.0.0/docs/PerformanceTuningGuide/optimizer-guc-parameter-hints.html)
