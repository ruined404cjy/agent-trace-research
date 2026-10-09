# Agent Trace JSON 存储设计决策

> 日期：2026-09-21；修订：2026-10-09
>
> 状态：逻辑模型与参数取值已确定；标注为"待定"的条目需要第 7 节列出的实验才能取值
>
> 证据基线：x86 主机（8 核、16,291,948 KiB 内存）上的 openGauss 6.0.0 build `aee4abd5` 与 ClickHouse 25.12.11.4；两台 ARM 主机（鲲鹏 920）上的 XStore（GaussVector 103.0.0 release）与 ClickHouse 23.3.10.5
>
> 语料画像：nvidia/Open-SWE-Traces 抽样，9 个 config、3,600 条轨迹、586,851 条消息

本文把[阶段一](json-storage-stage1-report-2026-09-09.md)、[阶段二](json-storage-stage2-report-2026-09-10.md)和阶段三（[x86 报告](json-storage-stage3-report-2026-09-20.md)、[XStore 报告](json-storage-stage3-xstore-report-2026-09-24.md)、[三台主机汇总](json-storage-stage3-pre-2026-10-08.md)）的实验结果，连同真实轨迹语料画像，收敛为一份可实施的存储设计。每条参数同时给出取值、支持它的证据和适用边界。[JSON 存储原理](json-storage-principles-2026-09-09.md)解释 TOAST、MergeTree part 和列式压缩的一般机制，[设计调研](json-storage-design-survey-2026-09-09.md)给出代表性系统的横向比较。

## 1. 设计概要

存储模型分四层：强类型分析列承担过滤、分组和聚合；动态属性列承担长尾属性的路径分析；长 payload 附带 preview 和内容元数据，其物理位置按引擎的大值存放方式确定；字节级审计需求由独立的原文表承担。openGauss 与 ClickHouse 上 payload 与分析列同表；XStore 把 payload 不压缩地存放在主表 heap 内，payload 因此放入独立的完整表，列表与统计查询读取不含 payload 的核心表。

| 决策项 | openGauss | XStore | ClickHouse |
|---|---|---|---|
| 长 payload 布局 | `same_table` | `full_core` | `same_table` |
| 动态属性表示 | `JSONB` | 待定 | `JSON(max_dynamic_paths=100)`；ARM 平台待定 |
| payload 外置 | 不启用 | 不启用 | 不启用 |
| 稳定过滤路径 | 复合表达式 B-tree | 待定 | type hint 或提升为普通列 |
| 排序或定位结构 | 列表与 Trace 两个复合 B-tree | 列表与 Trace 两个复合 B-tree；keyset 游标前加下界 | `ORDER BY (project_id,start_time,event_id)` |
| 常规物理维护 | `ANALYZE` | `ANALYZE` | 控制 part backlog，不执行 `OPTIMIZE FINAL` |

"待定"项的依据与所需实验见第 4.3、4.4 与第 7 节。

优先级高于上述全部选项的是 instrumentation 的消息记录约定：同一批轨迹按累积语义记录，存储量是按 delta 语义记录的 53.15 倍。该约定在建表之前确定，见第 6 节。

## 2. 目标负载

- Span 持续追加写入，查询首先限定 project 与时间范围；
- 主要操作是过滤、分组、计数和分位数统计；
- 常用条件集中在稳定 Trace 字段和少量热点属性；
- 完整 payload 读取、Trace 树回查和任意路径探索属于低频次级路径；
- 批次幂等、可见水位和内容校验属于摄入正确性要求。

该负载下的设计顺序是：先保证时间裁剪、列裁剪和持续摄入，再独立保证详情读取与原始内容恢复。

## 3. 逻辑模型

### 3.1 分层

| 层 | 内容 | 选择依据 |
|---|---|---|
| 分析列 | trace/span intrinsic 字段、时间、project、span 类型、框架、稳定 GenAI 字段 | 普通强类型列在两个引擎上都支持索引、排序键和列裁剪 |
| 动态属性 | 长尾属性与低频路径 | 路径数量不受 schema 变更约束 |
| payload | 完整输入输出消息、工具参数与结果 | 附 preview、`content_length`、`content_type`、`encoding` 和 `sha256`；openGauss 与 ClickHouse 上与分析列同表，XStore 上放入完整表 |
| 原文 | 首次解析前的 UTF-8 bytes | 签名、字节级审计和精确重放需要，与逻辑文档恢复分离 |

preview 按 Unicode code point 截取 payload 逻辑文本起始部分。列表与预览查询不投影 payload 列。

### 3.2 openGauss

```sql
CREATE TABLE events (
    ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
    trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
    project_id TEXT NOT NULL, start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, duration_ms BIGINT NOT NULL,
    span_type TEXT NOT NULL, framework TEXT NOT NULL, level TEXT NOT NULL,
    attributes JSONB,
    content_type TEXT, encoding TEXT, content_length BIGINT,
    preview TEXT, sha256 TEXT, payload TEXT);

CREATE INDEX events_list_idx ON events (project_id, start_time, event_id);
CREATE INDEX events_trace_idx ON events (project_id, trace_id, start_time, event_id);
```

`payload` 使用 `TEXT`，由 heap 与 TOAST 按长度自动分配，不需要显式的 LOB 接口。

### 3.3 ClickHouse

```sql
CREATE TABLE events (
    ingest_seq UInt64, event_id String, trace_id String, span_id String,
    parent_span_id Nullable(String), project_id String,
    start_time DateTime64(3, 'UTC'), end_time DateTime64(3, 'UTC'),
    duration_ms Int64, span_type String, framework String, level String,
    attributes JSON(max_dynamic_paths=100),
    content_type Nullable(String), encoding Nullable(String),
    content_length Nullable(UInt64), preview Nullable(String),
    sha256 Nullable(String), payload String CODEC(ZSTD(3)))
ENGINE=MergeTree ORDER BY (project_id, start_time, event_id);
```

Native JSON 无法单独保留 JSON null、空容器和含点号键的全部结构。需要完整逻辑文档恢复时，另设稀疏补全列保存受影响的顶层分支；该补全列的写入、空间、响应和客户端恢复成本全部计入方案。

### 3.4 XStore

```sql
CREATE TABLE events_core (
    ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
    trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
    project_id TEXT NOT NULL, start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, duration_ms BIGINT NOT NULL,
    span_type TEXT NOT NULL, framework TEXT, level TEXT NOT NULL,
    attributes JSONB,
    content_type TEXT, encoding TEXT, content_length BIGINT,
    preview TEXT, sha256 TEXT);

CREATE INDEX events_core_list_idx ON events_core (project_id, start_time, event_id);
CREATE INDEX events_core_trace_idx ON events_core (project_id, trace_id, start_time, event_id);
```

`events_full` 的列为 `events_core` 的全部列加 `payload TEXT`，建立同一组主键与两条复合 B-tree。每批写入在一个事务内先写 `events_full`、再写 `events_core`；列表、预览与统计读取 `events_core`，详情、Trace 与批量恢复读取 `events_full`，两条路径都不需要连接。

XStore 把空字符串视为 NULL，可能为空的文本列（如 `framework`）不声明 `NOT NULL`，读取时把 NULL 还原为空字符串。`attributes` 的类型待 XStore 上的动态属性实验确定（第 4.3 节），上例沿用 openGauss 的 `JSONB`。

## 4. 参数取值与判据

### 4.1 长 payload 布局

取值由引擎的大值存放方式决定：openGauss 与 ClickHouse 取 `same_table`，XStore 取 `full_core`。

| 引擎 | 大值存放 | 同表 payload 对不读 payload 的查询的影响 |
|---|---|---|
| openGauss | 超过约 2 KiB 的值压缩后移入 TOAST，主行只留指针，`same_table` 主表 heap 10.85 MB | 无，列表四布局都精确扫描 256 行 |
| ClickHouse | 按列存放，`payload` 列以 ZSTD 压缩 | 列裁剪不读取 payload 列；同表 granule 更窄，列表读取更少（ARM 主机 A 第一页 10,541 对 32,768 行） |
| XStore | 不压缩，存放在主表 heap 内，`same_table` 主表 heap 186.5 MB，无 payload 的窄表 11.0 MB | 按索引取一页不受影响；回表过滤大量行慢 2.1 倍，全表 `MAX` 聚合慢约 3.5 倍 |

目标负载以过滤、分组、计数和分位数统计为主（第 2 节），这些查询在 XStore 上逐行访问大量行，同表 payload 使其读取的页数随 heap 膨胀而增加。XStore 因此把 payload 移出列表与统计查询读取的表。

openGauss 与 ClickHouse 上的备选布局：

| 备选布局 | 不选择的原因 | 证据 |
|---|---|---|
| `separate` | openGauss 索引重复使总占用升至 1.530 倍；ClickHouse 的 JOIN 右表缺少排序键前缀谓词，25.12 在 Trace、23.3 在详情与 Trace 中整表读取右表 | 25.12 `trace:p50` 扫描 56,726 行、132,368,859 bytes，`same_table` 为 5,393 行、13,171,006 bytes；23.3 `separate` 详情 134–168 ms，其余布局 10.0–77.3 ms |
| `full_core` | 总占用 1.586 倍（openGauss）、写入时间增加约 60%（ClickHouse 25.12），当前负载下列表与详情无对应收益 | 列表与预览的四布局最大最小比为 1.009–1.140 |
| `asset_ref` | 每对象固定成本在真实对象规模下无法摊薄 | 等总字节控制中 `asset_ref` 由最快变为最慢，组内最大最小比 7.554（ClickHouse）和 3.567（openGauss） |

`full_core` 是 ClickHouse 上唯一的备用选项：它在列表、详情和 Trace 三类场景上与 `same_table` 同量级，代价是 17% 空间和 60% 写入时间。仅当列表查询必须与详情表物理隔离时启用。

XStore 上的备选布局（ARM 主机 B，时延单位 ms）：

| 指标 | `same_table` | `separate` | `full_core` | `asset_ref` |
|---|---:|---:|---:|---:|
| 列表第一页 | 22.7 | 22.7 | 22.7 | 22.7 |
| 单条详情（64 KiB / 2 MiB） | 10.8 / 24.0 | 12.1 / 24.8 | 10.4 / 23.5 | 20.1 / 27.5 |
| 完整 Trace p95 | 28.5 | 30.3 | 28.0 | 32.9 |
| 批量恢复（1,280 × 64 KiB） | 860 | 836 | 847 | 1,673 |
| 写入合计 | 8,442 | 10,981 | 11,564 | 8,793 |
| 总物理占用（相对 `same_table`） | 1.000 | 1.19 | 1.20 | 0.78 |

`full_core` 与 `separate` 在读取上同量级，`full_core` 的详情与 Trace 只读一张表，不经过连接，与 ClickHouse 的备用布局一致，因此取 `full_core`。代价是写入合计增加约 37%（两台 ARM 主机一致）与 20% 空间。`asset_ref` 在大量中等对象下最慢，理由与 openGauss、ClickHouse 相同（第 4.2 节）；它在 XStore 上总占用最低，原因是库内 payload 本身不压缩。

### 4.2 payload 外置

当前不启用。判据按对象数量而非字节总量给出。

| 条件 | 取值 |
|---|---|
| 外置的最小单对象大小 | ≥ 1 MiB（待阈值曲线实验确认精确值） |
| 外置的最大对象数量 | 单次批量恢复不超过约 200 个（待确认） |
| 当前语料落入的区间 | 单对象 p99 为 361,196 bytes，每 Trace 约 80 个 generation span |

真实分布中超过 512 KiB 的累积 prompt 占 0.10%，没有超过 2 MiB 的样本。目标负载因此落在"对象数量大、单对象中等"一侧，正是 `asset_ref` 表现最差的区间：1,280 个 64 KiB 对象的批量恢复中，`asset_ref` 在 XStore 上为库内布局的约 2 倍，在 ClickHouse 23.3 上为 5.4–7.4 倍。外置只对多模态 blob 这类确实达到 MiB 量级且数量少的内容成立。

前台隔离不作为外置的理由。各请求流分进程运行时，循环批量恢复、2 MiB 详情、长 Trace 与持续写入都没有明显拖慢前台列表，XStore 与 ClickHouse 23.3 的四种布局之间没有隔离差异（每条前台流 2 个 worker、20 次每秒）。

启用外置时，一致性契约固定为：先原子发布对象，再写入事件引用；resolver 按引用、catalog 行和对象 bytes 做三方核对，不以 catalog 状态字段代替内容核对；失败模式收敛为可回收的孤儿对象。

### 4.3 动态属性表示

openGauss 取 `JSONB`。路径过滤与数值聚合均低于同引擎的 JSON 文本表示；代价是完整文档返回时需要遍历二进制文档并序列化。

ClickHouse 的取值按查询构成决定：

| 查询构成 | 取值 | 证据 |
|---|---|---|
| 路径过滤与聚合为主 | Native JSON | 目标小字段查询比 String JSON 解析快 5–10 倍 |
| 完整文档返回为主 | String JSON | 完整 Native JSON 对象重建比 String JSON 内容读取慢 65–102 倍 |

目标负载以过滤、分组和聚合为主，完整文档读取为低频次级路径，因此取 Native JSON。该判定依赖查询构成，查询构成变化时重新核对。

动态路径预算取 100 量级。把预算从 100 提高到 1000 没有改变显式路径查询的数量级，却使载入吞吐降至 2,309 rows/s、压缩空间增至 22.230 MiB。稳定热点使用普通列或 type hint，预算外长尾留在 shared data。

两项取值待定：

| 平台 | 待定原因 | 候选 |
|---|---|---|
| XStore | `JSONB` 的取值来自 openGauss 的阶段一、二实验；XStore 与 openGauss 的 SQL 接口同源，其 `JSONB` 存储、路径查询与表达式索引未实测 | `JSONB` 或 JSON 文本 |
| ARM 主机上的 ClickHouse | 鲲鹏 920 为 ARMv8.2-A 且无 SVE，23.8 及以后的官方 ARM64 构建无法执行；可用的 23.3 不提供 `JSON(max_dynamic_paths=…)` 类型 | String JSON，或在该处理器上可运行的新版本自行构建后沿用 Native JSON |

### 4.4 访问结构

openGauss 的表达式索引覆盖查询中稳定出现的普通过滤列、JSONB 路径值和范围列三者的组合。

| 索引 | 效果 |
|---|---|
| `(project_id, 高密度路径值, start_time)` 复合表达式 B-tree | 完整结果获取 p50 由 96.80 ms 降至 16.15 ms |
| `(project_id, 低匹配率路径值, start_time)` 复合表达式 B-tree | 由 89.11 ms 降至 2.87 ms |
| `(project_id, trace_id, start_time, event_id)` | 完整 Trace 由 20.90 ms 降至 1.61 ms |
| 整个 JSONB 上的 `jsonb_hash_ops` GIN | containment 由 84.81 ms 降至 4.69 ms，不能替代表达式索引服务 `#>> =` |

参数化计划、定制计划和 Hint 计划可能不同。上线前按实际驱动协议核对执行计划与 `pg_stat_user_indexes.idx_scan`，Hint 只用于诊断。

XStore 沿用列表与 Trace 两个复合 B-tree，keyset 条件写为 `start_time >= :t AND (start_time > :t OR (start_time = :t AND event_id > :id))`。只有 OR 展开式时，该条件不被用作索引扫描的起点，中间页先过滤 13,652 行；加入由游标推出的下界后每页只过滤 6 行，两台 ARM 主机的中间页为 22.6–23.4 ms，与第一页同量级。XStore 上的表达式索引与 GIN 未实测，稳定过滤路径的取值待定。

ClickHouse 排序键按主导过滤前缀设计，取 `(project_id, start_time, event_id)`。把 `trace_id` 插入 project 与时间字段之间会扩大时间窗口查询的读取范围，该候选不适用本负载。

### 4.5 物理维护

ClickHouse 优先控制 part backlog。part 状态对列表查询的影响为 2.90–3.27 倍，布局选择只有 1.114–1.175 倍，两个因素正交。稳定的少量 part 已经接近单 part 的列表性能；`OPTIMIZE FINAL` 在详情、Trace 和批量恢复场景中使延迟上升，不作为常规操作。后台 merge 的资源成本计入写入预算：merge 开启时载入吞吐下降约 8.7%–9.2%。ARM 主机上的 ClickHouse 23.3 结论相同：暂停 merge、190 个 part 并存时列表为单 part 态的 2.66–5.59 倍（主机 A）与 3.1–5.4 倍（主机 B）。

openGauss 与 XStore 在写入完成后执行 `ANALYZE`。

## 5. 语料画像

### 5.1 方法

数据源为 nvidia/Open-SWE-Traces 的本地副本，覆盖 sweagent、openhands 和 minisweagent 三个框架与五个模型，共 9 个 config。每个 config 取字典序居中的 shard，读取首个 row group 的前 400 行，合计 3,600 条轨迹、586,851 条消息、287,860 个 assistant 消息。复现入口为 `experiments/payload-profile/profile_open_swe.py`。

单条消息按其 `content` 的 UTF-8 字节数计量。累积 prompt 指一次模型调用实际发送的完整对话，即该 assistant 消息之前全部消息的字节之和；OTel GenAI 约定下它对应 generation span 的 `gen_ai.input.messages`。

### 5.2 结果

| 指标 | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| 单条消息 bytes | 130 | 4,335 | 15,406 | 141,604 |
| 累积 prompt bytes | 97,738 | 240,859 | 361,196 | 682,773 |
| 每条轨迹消息数 | 155 | 305 | — | 522 |

累积 prompt 超过 64 KiB 的占 71.39%，超过 512 KiB 的占 0.10%，没有超过 2 MiB 的样本。分框架差异集中在超过 64 KiB 的比例：minisweagent/qwen36_27b 为 32.58%，openhands/deepseek_v4_flash 为 82.22%。

压缩比方面，整条轨迹文本聚合后的 zlib-6 压缩比 p10 为 4.31、p50 为 5.14、p90 为 6.61；单条消息单独压缩时 p50 为 2.44。

### 5.3 对既有结论的修正

| 既有口径 | 实测 | 影响 |
|---|---|---|
| 阶段三语料 zlib-6 压缩比 191.1–289.9 | 4.31–6.61 | 空间结论按实测折算，不再使用 3–6 倍参考区间 |
| 阶段三 payload 档位 64 KiB / 512 KiB / 2 MiB | 主体落在 50–360 KB | 2 MiB 档在真实分布中不出现，阈值曲线需要在 16 KiB–1 MiB 加密采样 |
| 单条消息即 payload | 单条 p50 仅 130 bytes | payload 的量级由 instrumentation 约定决定，不由消息内容决定 |

whowhen_pro 的观测与之一致：其 attributes canonical JSON 文本长度 p50 为 1,042、p95 为 4,663、最大 62,197 bytes，属于 delta 语义下的量级。

## 6. Instrumentation 约定与存储量级

同一批轨迹按两种约定记录，存储量相差 53.15 倍。

| 约定 | 每个 generation span 记录的内容 | 抽样总字节 |
|---|---|---:|
| 累积 | 该次调用发送的完整对话 | 31,790,565,601（29.61 GiB） |
| delta | 自上次模型调用以来的新增消息 | 598,111,343（0.557 GiB） |

每条轨迹平均 80 个 generation span，累积语义下第 k 个 span 重复保存前 k−1 个 span 已保存的内容。三个候选约定如下。

| 候选 | 存储量 | 查询语义 | 代价 |
|---|---|---|---|
| delta | 等于对话文本量 | 重建完整 prompt 需要按 trace 聚合多行 | 单 span 不自足 |
| 累积 | 约 53 倍 | 单 span 自足 | 写入、空间与传输同比例上升 |
| 内容寻址去重 | 接近 delta | 单 span 保存消息 id 列表，读取时按 id 组装 | 增加消息表与组装逻辑 |

该约定决定 payload 的大小分布，进而决定第 4.1 和 4.2 节的全部取值，因此在建表之前确定。当前 `trace-synthesis` 的 converter 采用 delta 语义：每个 llm span 只携带自上一个 llm span 以来新增的消息。多数真实 SDK 采用累积语义，两者的存储规划不能互相套用。

## 7. 证据缺口与后续实验

| 优先级 | 缺口 | 需要的实验 |
|---|---|---|
| P0 | 累积与 delta 两种约定下的实际存储量、写入吞吐和查询构成 | 用同一批轨迹按两种约定各生成一份输入，比较载入、空间和列表/详情查询 |
| P1 | 内联与外置的精确阈值 | size 取 16 KiB–8 MiB 阶梯、count 取三档的二维扫描，产出阈值与对象数上限 |
| P1 | 冷缓存下的详情与批量恢复 | 分冷热两组执行详情、Trace 与批量恢复，记录缓存控制动作与生效证据 |
| P1 | 热点字段提升的判据 | 同一查询集下比较路径留在动态属性列、提升为普通列、提升后保留副本三种处理 |
| P1 | XStore 上的动态属性表示与访问结构 | 在 XStore 上执行阶段一、二的 `JSONB` 与 JSON 文本对照，以及复合表达式 B-tree 与 GIN 的查询目标 |
| P1 | ARM 主机上 ClickHouse 的动态属性表示 | 验证可在鲲鹏 920 上运行、提供 `JSON(max_dynamic_paths=…)` 的版本；或在 23.3 上测量 String JSON 的路径过滤与聚合成本 |
| P2 | 保留期删除与引用回收 | 覆盖时间分区裁剪与行删除对空间、扫描量和恢复路径的影响 |
| P2 | 更高前台并发、更小主机或冷数据下的混合负载 | 各请求流分进程运行，提高前台 worker 数与到达率，在更小规格主机或冷缓存下重测 |
| P2 | 数据库内 LOB 引用 | 按 xstore 或 extension 的实际能力实现 `db_lob_ref`，单独汇总 |

以下结论已充分，不再增加同条件下的对照：四种长 payload 布局在列表与预览上的差异、XStore 同表 payload 对大量行访问的影响、每条前台流 2 个 worker 时各布局的前台隔离性、动态路径预算的边界行为、ClickHouse part 状态与布局的正交性、`OPTIMIZE FINAL` 的适用范围。

## 8. 适用边界

全部性能数字来自单机固定版本与热缓存的条件（XStore 每个查询样本新建连接，其余引擎复用连接），不覆盖完整 Collector/exporter 链路、多节点部署与故障恢复。跨引擎数字表示固定硬件下的完整方案对照，同时包含行存与列存、索引与排序键、执行器、协议、压缩编解码和后台维护的影响，不构成两个引擎之间的绝对性能排名，也不外推为生产容量。两台 ARM 主机与其他用户共用，主机 B 上测量窗口内主机 CPU 占用明显升高的阶段已从结论中排除。

语料画像来自公开的软件工程 agent 轨迹数据集，覆盖文本类 payload，不覆盖多模态内容、其他任务域的 agent 和生产环境的查询频率构成。

## 参考资料

- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-09.md)
- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段二报告](json-storage-stage2-report-2026-09-10.md)
- [阶段三原理与设计](json-storage-stage3-principles-design-2026-09-24.md)
- [阶段三实验报告（openGauss 与 ClickHouse 25.12）](json-storage-stage3-report-2026-09-20.md)
- [阶段三 XStore 横向比较实验报告](json-storage-stage3-xstore-report-2026-09-24.md)
- [阶段三组内汇报](json-storage-stage3-pre-2026-10-08.md)
- [语料画像脚本](../../experiments/payload-profile/profile_open_swe.py)
- [nvidia/Open-SWE-Traces](https://huggingface.co/datasets/nvidia/Open-SWE-Traces)
- [OTel GenAI span content recording](https://github.com/open-telemetry/semantic-conventions/blob/main/docs/gen-ai/gen-ai-spans.md)
- [ClickHouse JSON 数据类型](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [openGauss 6.0 JSON/JSONB 类型](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-types.html)
- [openGauss 6.0 TOAST 存储](https://docs.opengauss.org/en/docs/6.0.0/docs/DatabaseAdministrationGuide/toast-technology.html)
