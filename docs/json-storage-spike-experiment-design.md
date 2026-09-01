# Agent Trace JSON 存储穿刺与对比实验设计

> 状态：待执行实验设计
> 日期：2026-09-01
> 输入调研：[json-storage-design-survey.md](json-storage-design-survey.md)
> 目标：用可复现证据选择多字段 JSON、长字段和大 payload 的存储组合

## 1. 实验要回答的问题

本实验围绕五个决策问题组织：

1. 稳定热点字段应保留在 JSON、复制为同级列，还是移动为同级列并使用 residual JSON？
2. 动态路径应使用 JSON、JSONB、Map/KV、自动子列还是 Trace 专用 Parquet 布局？
3. Full/Core 物化能否显著降低列表、搜索和常规分析的读取放大？
4. JSON 内单个长值与媒体 payload 是否应进入同一 asset 层，阈值设在何处？
5. 候选方案接入现有 exporter 和 benchmark 的改造范围、运行成本与运维复杂度是多少？

实验分阶段进行。独立微基准先验证机制，只有通过正确性和基本收益门槛的方案才进入现有 Demo 改造。

## 2. 范围与边界

### 2.1 纳入范围

- 当前 exporter + 自研 openGauss/GaussDB 兼容引擎基线；
- Langfuse v4 的 `events_full`/`events_core`、metadata Map 形态和长字段 overflow；
- PostgreSQL JSONB + 定向索引/生成列 + TOAST；
- ClickHouse 原生 JSON 动态子列与 shared data；
- Grafana Tempo Parquet dedicated/generic/blob columns；
- 当前引擎上的 residual、Full/Core、对象引用和 KV 子表原型；
- 可选的 Parquet Variant 离线读写穿刺。

### 2.2 暂不纳入

- 生产级跨地域对象存储、CDN 和灾备；
- UI 媒体渲染；
- 全量权限系统；
- 生产数据迁移；
- 引擎内核开发；
- 完整替换现有 benchmark 框架。

对象存储原型仍需实现最小状态、幂等、校验和清理验证，避免只测正常上传路径。

## 3. 执行前置条件

### 3.1 固定版本

本设计使用以下本地基线：

| 组件 | 提交 |
|---|---|
| exporter | `4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b` |
| benchmark | `e0b9c83e3bd8bd7bb78d68225f29df0753f5432e` |
| Langfuse | `983c2a6e5bbe9e8f35fe10eb017c9abd6220833b` |

2026-09-01 初次 GitHub 拉取因 TLS 握手失败；后续 Langfuse 拉取成功并确认
`main` 提交不变，exporter 与 benchmark 仍未刷新远端引用。执行实验前需重新
`git fetch` 并决定继续使用该基线或整体更新。任何更新都要重新记录提交和
schema hash。

### 3.2 先解决 exporter/benchmark schema 配对

当前 exporter `events` 比 benchmark v4 database catalog 多 `service_name` 和 `service_version`。benchmark 的 GaussDB cleanup 会重建 26 列表，而 exporter 会按 28 列写入。

P0 必须产出填写完整的版本矩阵：

| 字段 | 必填内容 |
|---|---|
| exporter | repository、40 位 commit、工作树状态、二进制 SHA-256、构建工具链 |
| benchmark | repository、40 位 commit、catalog revision/hash、fixture hash |
| schema | 列清单、类型、nullable、索引、storage profile、DDL hash |
| engine | 产品、版本、build、实例规格、关键参数 |
| runtime | Collector 配置 hash、容器 image digest、对象存储版本 |

可选择以下任一配对方式：

- 更新 benchmark v4 database catalog、snapshot、cleanup 和查询结果列，匹配 28 列 exporter；
- 检出未包含这两列的 exporter commit，匹配现有 26 列 catalog。

实验报告需要基于[版本 manifest 模板](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/e2e-version-manifest.yaml)生成实际文件。schema preflight、readiness warmup、visibility 和 cleanup 全部通过后，才开始性能计时。

### 3.3 环境分层

| 环境 | 用途 | 允许结论 |
|---|---|---|
| 本地容器 | PostgreSQL、ClickHouse、Langfuse、Tempo、MinIO 机制复现 | 功能、相对趋势、改造可行性 |
| 标准 openGauss row profile | SQL 和 exporter 联调 | 接口正确性，不代表 dstore 性能 |
| 目标自研引擎 xstore/dstore | E0、E5–E8 性能与引擎边界 | 当前项目生产候选结论 |

环境申请、镜像下载和目标区排队时间不计入本文人日估算。

## 4. 候选实验矩阵

### 4.1 总览

| ID | 候选 | 主要机制 | 与当前 Demo 的关系 | 初始工作量 |
|---|---|---|---|---:|
| E0 | 当前 exporter + 目标引擎 | 热点列 + 完整 JSON + 64 KiB 截断 | 基线 | 0.5–1 人日 |
| E1 | PostgreSQL | JSONB + 定向/通用索引 + TOAST | 独立参考实现 | 1–2 人日 |
| E2 | ClickHouse native JSON | 动态子列 + shared data | 独立参考实现 | 1–2 人日 |
| E3 | Langfuse v4 | Full/Core + arrays/Map + media overflow | 现有 benchmark backend | 2–4 人日 |
| E4 | Grafana Tempo | Parquet intrinsic/dedicated/generic/blob | Trace 专用参考实现 | 2–4 人日 |
| E5 | exporter residual | 热点列 + residual JSON，copy/move 对比 | exporter 小型分支 | 1–2 人日 |
| E6 | 目标引擎 Full/Core | 物化轻量表 + Full 表 | exporter/schema 分支 | 3–5 人日 |
| E7 | exporter asset overflow | inline-or-reference + MinIO/S3 | exporter 中型分支 | 5–8 人日 |
| E8 | 目标引擎 KV 子表 | 热点列 + 动态属性行式展开 | exporter/schema 分支 | 3–5 人日 |
| E9 | Parquet Variant | typed shredding + residual value | 可选离线参考 | 2–3 人日 |

E7 的 5–8 人日只覆盖可验证原型。包含完整保留、恢复、权限、配额、监控和生产清理的实现预计为 10–15 人日。

### 4.2 E0：当前项目基线

目的：建立所有候选的相对基准，并量化当前 64 KiB 策略的数据损失。

配置至少包含：

- `storage_profile=row`：本地接口基线；
- `storage_profile=xstore`：目标引擎基线；
- `max_attr_value_length=65536`：当前默认；
- `max_attr_value_length=0`：仅在确认目标引擎和 Collector 内存安全后执行，作为无应用截断对照；
- `events` 与 `events_dedup` 两种读取面，继续保留现有去重开销对照。

通过条件：truth manifest 一致；截断率、原始字节数和数据库实际保存字节数可以解释；所有后续结果均引用同一基线 run ID。

### 4.3 E1：PostgreSQL JSONB 参考

建立同一逻辑 `events` 表，比较四种 schema：

1. `metadata json` 文本；
2. `metadata jsonb`，无索引；
3. `metadata jsonb` + 通用 GIN；
4. `metadata jsonb` + 热点路径表达式索引或 stored generated column。

另外记录主表与 TOAST 表空间、TOAST 命中和 full projection 延迟。该实验回答二进制解析与定向索引价值，不直接推导目标 dstore 的 JSONB 能力。

通过条件：四种形态使用相同逻辑数据；查询结果一致；分别报告 heap、TOAST、index 空间；冷热路径和整对象读取均有结果。

### 4.4 E2：ClickHouse 原生 JSON 参考

使用与 Langfuse compose 相同主版本的独立 ClickHouse 实例，避免修改 Langfuse 表。至少比较：

- `String CODEC(ZSTD)` 原文；
- `JSON(max_dynamic_paths=100)`；
- `JSON(max_dynamic_paths=1000)`；
- `JSON(max_dynamic_paths=5000)`，在版本和资源允许时执行；
- shared data 的 `map`、`map_with_buckets`、`advanced` 序列化；
- 10 个稳定热点路径使用 type hint；
- 全对象读取与单路径读取。

观察 data part merge 前后动态路径集合、shared data 路径、part 大小和查询 bytes read。路径数量、稀疏度和类型冲突必须同时变化。

通过条件：确认动态路径上限后的 fallback 行为；merge 前后结果一致；单路径、冷路径、整对象三类读取的成本可分解。

### 4.5 E3：Langfuse v4 复现

现有 benchmark 已有 `--schema v4 --backend langfuse` 路径，直接向 Langfuse OTLP endpoint 写入，不经过 exporter。首轮使用本地 Langfuse checkout 和 pinned image，不创建远端 fork。

分三组运行：

1. Full/Core 默认：overflow 关闭；
2. overflow 开启，阈值 2 MiB；
3. overflow 开启，阈值依次为 64 KiB、512 KiB、2 MiB，观察主表和对象存储转移曲线。

需要额外采集：

- `events_full`、`events_core` 分项空间；
- input/output/metadata lengths；
- 列表查询实际读取 Core 或 Full；
- media 表、关联表和 MinIO 对象数/字节；
- uploaded/reused/failed 和 bytes removed 指标；
- 同内容复用、上传失败保留原值、删除后对象清理。

注意：Langfuse 的 input/output 是 String，metadata 是 names/values 数组。该实验不能代替 ClickHouse native JSON 实验。

通过条件：benchmark v4 Langfuse 正确性通过；Core/Full 和 overflow 三层收益可以分项；失败注入没有静默丢失内容。

### 4.6 E4：Tempo Trace 专用参考

使用 OTLP 摄入、MinIO/S3 block storage 和 TraceQL 查询。测试 vParquet5：

- 全部动态属性留在 generic `Attrs`；
- 10 个 string 热点 dedicated columns；
- 20 个 string + 5 个 int dedicated columns；
- 高基数长字符串 dedicated column 分别使用默认 dictionary 和 `blob` 选项；
- 1%、5%、20%、95% 路径密度。

Tempo 查询语义与关系型 SQL 不同。结果只比较摄入、对象空间、特定 Trace/属性查询的 bytes read 和延迟，不把 TraceQL 与 SQL 聚合做单一排名。

通过条件：generic/dedicated/blob 的物理 block 证据可复核；5% 附近的稀疏属性拐点有实测；Trace 重建哈希一致。

### 4.7 E5：热点列 + residual JSON

在 exporter 独立 worktree 中实现最小变体：

- copy：保持当前全部 `metadata`，继续写热点同级列；
- move：`attrsToJSON` 接受排除路径集合，已提升路径不写 residual；
- reader/replay 验证可把同级列与 residual 重建为规范 JSON；
- 至少选择 10 个热点字段，包含字符串、数值、布尔和缺失值。

改动面：`convert.go`、配置或固定路径表、单元测试、schema 说明；若新增同级列，还需 `schema.go`、benchmark catalog/snapshot/fixture/query。单纯排除已有同级列不要求数据库新增列。

通过条件：move 形态没有逻辑信息丢失；copy 与 move 的写入字节、存储、热点查询和全对象重建差异可量化；null/missing/type conflict 规则有测试。

### 4.8 E6：目标引擎 Full/Core

目标引擎当前 Demo 只有 `events` Full 表。实验建立实际物化的 `events_core`：

- 保留常用 Trace/Span 和分析字段；
- input/output/metadata 保存 200 字符预览、长度和引用状态；
- Full 保留完整 inline 值或 asset 引用；
- 列表、搜索和 Q09 light 读取 Core；详情和 Q09 full 读取 Full。

首选实现顺序：

1. post-load SQL 物化，验证读取收益；
2. 双写或数据库物化能力，验证持续摄入成本；
3. 增量一致性与去重语义。

普通 view 不进入结果集，因为它无法验证物理空间与读取裁剪。

通过条件：Core 与 Full 对同一 `event_version` 一致；Q09 light bytes read 和 p95 明显下降；双写或物化延迟、空间和故障语义完整记录。

### 4.9 E7：长字段与媒体统一 asset 层

在 exporter worktree 中实现最小 asset 原型：

- 候选字段：input、output、metadata 的任一叶值；
- 阈值：64 KiB、512 KiB、2 MiB；
- 存储：MinIO/S3 compatible；
- 内容寻址：project + SHA-256；
- 引用：asset ID、content type、encoding、content length、preview；
- 关联：trace ID、span ID、field path、origin；
- 状态：pending、available、failed、deleting；
- resolver：详情读取时显式选择是否解引用；
- reconcile：扫描缺失引用和孤儿对象。

需要比较三种失败策略：

| 策略 | 顺序 | 优点 | 风险 |
|---|---|---|---|
| upload-first | 对象成功后写事件 | 事件引用始终可读 | 事件写失败产生孤儿对象 |
| record-first | 先写 pending 引用，再异步上传 | 摄入延迟低 | 短期引用不可读，需状态机 |
| outbox | 事件与 outbox 同事务，worker 上传并切状态 | 可恢复性强 | 组件和运维复杂度较高 |

原型优先实现 upload-first + reconcile，作为最小可验证闭环；随后用故障注入判断是否需要 outbox。

通过条件：引用解析后的规范内容 hash 与输入一致；重复内容复用；对象存储超时、上传成功后 DB 失败、DB 成功后对象删除均能被检测；删除和 retention 没有永久孤儿。

### 4.10 E8：动态属性 KV 子表

把 metadata 动态叶路径展开到 `event_attributes`：

```text
project_id, trace_id, span_id, event_version,
path, value_type, string_value, number_value, bool_value
```

测试按 path/value 索引、批量写入、1/50/500/5000 属性数和 trace 重建。该方案与当前单宽表的 append-only + dedup view 组合较复杂，必须把 event_version 纳入唯一逻辑键和查询去重。

通过条件：动态路径过滤有可解释收益；总行数、写入放大、join、dedup 和重建成本均在报告中；不能只报告单路径命中延迟。

### 4.11 E9：Parquet Variant 可选实验

使用支持 Variant shredding 的 pinned writer/reader，把相同数据写为 unshredded Variant 和 10/50 个 typed shredded paths。检查：

- typed path 的统计信息和 data skipping；
- 类型不匹配值进入 residual；
- missing 与 JSON null；
- 不同文件使用不同 shredding schema 时的读取；
- 重建内容 hash。

该实验用于验证格式语义，不直接进入当前在线摄入链路。

## 5. 数据设计

### 5.1 统一逻辑记录

所有候选接收同一规范事件：

```text
identity: project_id, trace_id, span_id, parent_span_id, event_version
hot: start_time, type, name, level, service_name, model, token/cost
metadata: nested heterogeneous JSON
input/output: JSON value
assets: optional image/audio/binary payload descriptors
```

数据生成器同时输出：

- OTLP/HTTP 可回放输入；
- canonical JSONL；
- truth manifest：每条记录的规范 JSON SHA-256；
- asset manifest：原始 bytes SHA-256、MIME、长度和重复组；
- query parameters：热点值、冷路径、selectivity 和预期计数。

canonical hash 应忽略 JSON object key 顺序，保留数组顺序，并区分 missing、JSON null、字符串和数值类型。

### 5.2 因子与取值

| 因子 | 取值 |
|---|---|
| 路径数 | 50、500、5000 |
| 路径密度 | 1%、5%、20%、50%、95% |
| 热点路径数 | 1、10、50 |
| 嵌套深度 | 1、10、100；500 只做边界正确性 |
| 类型 | 稳定、int/string 混合、null、missing |
| 单长值 | 4 KiB、64 KiB、512 KiB、2 MiB、20 MiB |
| 总 JSON 大小来源 | 一个长值、许多 1 KiB 小值 |
| 内容基数 | 重复率 0%、50%、95% |
| 媒体 | PNG、JPEG、短音频、随机二进制 |
| 表达形式 | inline base64、data URI、asset reference |
| Trace 宽度 | 1、10、100、1000 spans/trace |

完整笛卡尔积规模过大，使用下列固定 suite 覆盖主要交互。

### 5.3 固定 suite

| Suite | 记录数 | 目的 |
|---|---:|---|
| C0 | 1,000 | 类型、null/missing、重建、错误输入正确性 |
| W1 | 100,000 | 50/500/5000 路径和稀疏度 |
| L1 | 100,000 | 单字段 4 KiB–20 MiB 阶梯 |
| S1 | 100,000 | 同为约 2 MiB：一个长值与许多小值对比 |
| M1 | 10,000 | 图片、音频、base64、引用、内容去重 |
| P1 | 1,000,000 | 稳态摄入和查询性能 |
| P2 | 10,000,000 | 通过 P1 后执行的容量与 compaction 测试 |

每个 suite 使用固定 seed。报告记录生成器 commit、参数和文件 hash。

## 6. Workload

| ID | 操作 | 关键变量 |
|---|---|---|
| Q1 | trace ID 点查 | trace 宽度、冷热缓存 |
| Q2 | 热点字段等值/范围过滤 | selectivity 0.1%、1%、10% |
| Q3 | 冷路径过滤 | 路径密度、是否在 shared/residual |
| Q4 | JSON path group by/aggregate | 类型稳定性、基数 |
| Q5 | metadata key existence | 1%、5%、20%、95% 密度 |
| Q6 | input/output/metadata 文本搜索 | 文本长度、命中/未命中 |
| Q7 | light/Core 分页 | trace 宽度、page size 20/100/1000 |
| Q8 | full/detail 读取 | payload 4 KiB–20 MiB、是否解引用 |
| Q9 | 规范 JSON 重建 | 路径数、嵌套、null/missing |
| Q10 | schema promotion/demotion | 回填量、在线读取一致性 |
| Q11 | retention/delete | 主表、Core、KV、asset 联合清理 |
| Q12 | 故障注入 | 对象超时、DB 失败、worker 重启 |

摄入使用三类到达模式：

- bulk：测最大 rows/s 和 MiB/s；
- constant：100、1000、5000 spans/s；
- burst：稳态 1000 spans/s，每 60 秒突发到 5000 spans/s，持续 10 秒。

每个查询分别执行 cold、首次 warmup 后 warm、稳定 warm 三组。现有 benchmark 的 readiness warmup 与查询 warmup 分开记录。

## 7. 指标与采集

### 7.1 正确性

- 摄入记录数、Trace/Span ID 集合和去重结果；
- canonical JSON hash；
- asset bytes hash、MIME、长度；
- missing、JSON null、类型冲突和数组顺序；
- inline、preview、reference、resolved 四种读取语义；
- 上传失败、清理失败和部分物化状态的可见性。

正确性是硬门槛。hash 或预期计数不一致的 run 不进入性能排名。

### 7.2 性能

- 摄入 rows/s、spans/s、MiB/s、p50/p95/p99；
- 查询 latency、QPS、rows scanned、bytes read、bytes returned；
- CPU、RSS、磁盘 IO、网络、对象存储请求数；
- merge/compaction、ANALYZE、index build 和 materialization 时间；
- Core 延迟、asset upload 延迟和 resolver 延迟。

### 7.3 空间

分别报告：

- Full 主表；
- Core 表；
- JSON/Map/residual/shared data；
- 索引、统计信息和元数据；
- TOAST/LOB/KV 子表；
- 对象存储数据与元数据；
- 临时 part、merge 和重复数据。

派生指标包括压缩率、每事件字节、写放大、索引放大、Core/Full 比和 asset 去重率。

### 7.4 工程成本

- 改动文件与代码行；
- 新增服务和运行依赖；
- schema migration/backfill 时间；
- 日常运维任务、告警和清理作业；
- 查询语义变更和 SDK/resolver 需求；
- 故障恢复步骤与人工介入次数。

## 8. 公平性约束

1. 使用同一 logical dataset、seed、truth manifest 和查询 selectivity。
2. 记录并固定 CPU、内存、磁盘、容器限制、引擎版本和 image digest。
3. 所有候选完成必要 compaction、merge 或 ANALYZE 后再执行稳定查询；同时单独报告整理成本。
4. inline 候选统一最大 payload；对象引用候选同时报告未解引用和完整解引用延迟。
5. 通用索引、热点索引和无索引分别标识，空间计入总成本。
6. Tempo、关系型数据库和 ClickHouse 的查询语义分别分组，只比较相同逻辑任务。
7. 关闭 64 KiB 截断的 run 必须单独标识；截断后的 marker 结果不能视为成功保存大值。
8. 不使用 `latest` image tag，不引用未提交工作树结果。

## 9. 分阶段执行计划

### P0：静态证据和版本门禁，1–2 人日

1. 重新获取远端更新，固定三个仓库 commit。
2. 选择匹配的 exporter/catalog，填写版本 manifest。
3. 固定 C0/W1/L1 数据规范和 truth manifest。
4. 记录所有候选 DDL、配置和 image digest。
5. 运行当前 benchmark schema preflight。

产物：版本矩阵、数据 manifest、候选配置清单。schema 不匹配时停止后续 E0/E5–E8。

### P1：独立机制穿刺，5–9 人日

并行运行 E1、E2、E3 和 E4 的 C0/W1/L1：

- E1 回答 JSONB/index/TOAST；
- E2 回答自动子列和 shared data；
- E3 回答 Langfuse Full/Core/overflow；
- E4 回答 Trace 专用 Parquet dedicated/blob。

产物：各候选的正确性、空间、关键查询和改造边界。每个候选均可独立停止。

### P2：当前项目原型，9–15 人日

按顺序执行：

1. E5 residual copy/move；
2. E6 Full/Core 物化；
3. E7 asset overflow；
4. E8 KV 仅在 E2/E5 无法满足动态冷路径需求时执行。

每一步使用独立 worktree 和 schema version。通过前一步后再叠加下一层，保持单变量对比。

### P3：端到端对比，3–6 人日

对通过门槛的 2–3 个组合运行 P1/P2 数据规模和完整 workload，至少包括：

- 当前 E0；
- Langfuse E3；
- 当前项目最佳组合，例如 E5 + E6 + E7；
- E2 或 E4 中最有价值的参考方案。

产物：决策矩阵、推荐阈值、容量模型、生产化缺口和后续任务拆分。

## 10. Demo 改造与分支策略

### 10.1 工作区边界

根目录是多个仓库组成的工作区。实验分支分别创建在实际仓库：

```text
.worktrees/
  exporter-json-residual       experiment/json-residual
  exporter-asset-overflow      experiment/asset-overflow
  benchmark-json-storage       experiment/json-storage
```

先使用本地 `git worktree`。远端 fork 仅在需要多人协作、CI 或 PR 时创建。上游 Langfuse、Tempo 和 ClickHouse 首轮使用 pinned checkout/image，不维护长期 fork。

### 10.2 各组件改造可行性

| 组件 | 可行性 | 说明 |
|---|---|---|
| benchmark → Langfuse | 高 | v4 Langfuse backend 已存在，补数据、采集和 overflow 验证 |
| benchmark → PostgreSQL | 中 | 建议先用独立 runner；接入统一 backend 需新增连接、catalog 和 SQL 方言 |
| benchmark → ClickHouse JSON | 中 | 可复用 ClickHouse driver/SQL 经验，需新增 native JSON schema，不能复用 Langfuse 表代表 |
| benchmark → Tempo | 低至中 | OTLP 摄入可复用，查询和结果模型需专用 adapter，不属于 SQL backend |
| exporter → residual | 高 | 转换层改动集中，已有同级列可先避免 DDL 变化 |
| exporter → Full/Core | 中 | 需要真实物化和一致性策略，单纯 view 无效 |
| exporter → asset | 中 | S3 client 容易接入，事务边界、状态、resolver 和清理构成主要工作量 |
| exporter → KV | 中 | SQL/批写可实现，append-only 去重和查询 join 改动较大 |

### 10.3 “替换 Langfuse”的准确范围

现有系统存在两条链路：

```text
database backend: OTLP -> Collector/exporter -> openGauss/GaussDB
Langfuse backend:  OTLP -> Langfuse -> ClickHouse/PostgreSQL/MinIO
```

因此有两种不同的替换含义：

- **benchmark 参考 backend 替换**：新增 PostgreSQL、ClickHouse JSON 或 Tempo adapter，用于横向机制比较。
- **当前摄入 Demo 存储实现替换**：修改 exporter 和目标引擎 schema，落地 residual、Core 或 asset。

E1/E2/E4 首轮只做参考 backend。通过机制门槛后再决定是否进入 exporter 适配，避免在证据不足时同时修改摄入、存储和查询三层。

## 11. 报告结构

每个 run 生成一个自包含目录：

```text
artifacts/json-storage/<run-id>/
  manifest.yaml
  dataset-manifest.json
  schema.sql
  config/
  correctness.json
  ingest.json
  queries.json
  storage.json
  resources.json
  failures.json
  logs/
```

`manifest.yaml` 必须记录 commits、工作树状态、image digest、硬件、配置 hash、suite、seed、冷热状态、准备命令、起止时间和异常。报告中的每张比较表引用 run ID。

## 12. 决策门槛

### 12.1 通用门槛

- correctness、canonical hash 和预期计数全部通过；
- 没有静默截断、静默跳过路径或不可见上传失败；
- 空间包含主表、索引、Core、KV 和对象存储全部分项；
- p95/p99 至少重复 3 轮，趋势稳定；
- schema migration 和故障恢复有可执行步骤。

### 12.2 分层决策

| 层 | 进入下一阶段的证据 |
|---|---|
| 热点列/residual | 热点查询或空间有明确收益，重建正确，迁移复杂度可控 |
| 自动子列/Map/KV | 5000 路径下保持摄入与查询稳定，冷路径成本可接受 |
| Full/Core | light workload 的 bytes read 和 p95 显著降低，写放大可接受 |
| asset overflow | 2–20 MiB payload 主表压力显著下降，失败与清理闭环通过 |

“显著”的最终数值在 P0 根据当前 E0 波动确定。建议以超过三轮基线变异范围，并同时改善目标指标至少 20% 作为初始门槛；该阈值必须在首轮基线后固定，不能按候选结果调整。

## 13. 推荐执行顺序

最小有效路径如下：

1. 修复版本配对并建立 E0。
2. 同时完成 E2 ClickHouse JSON 和 E3 Langfuse，分开验证自动子列与 Full/Core/overflow。
3. 完成 E1，确认 JSONB/定向索引/TOAST 的参考上限。
4. 在当前 exporter 实现 E5 residual。
5. 依据 Q7/Q8 bytes read 决定是否实现 E6 Full/Core。
6. 依据 L1/M1 的主表、内存和网络曲线实现 E7 asset。
7. E4 Tempo 和 E9 Parquet Variant用于判断中长期 Trace 专用列式布局；E8 KV 作为动态路径查询仍不达标时的备选。

该顺序先分离 JSON 路径组织与大值生命周期，再组合当前项目方案，能把收益和成本归因到具体设计层。

## 14. 参考资料

- [JSON 存储设计调研](json-storage-design-survey.md)
- [Benchmark 运行说明](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/README.md)
- [Benchmark v4 database catalog](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/schema/v4/database/catalog.json)
- [Benchmark v4 Langfuse catalog](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/benchmark/schema/v4/langfuse/catalog.json)
- [Exporter 测试计划](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/TEST_PLAN.md)
- [Trace 摄入 Demo 黄区指南](trace-ingestion-demo-yellow-zone-guide.md)
- [Langfuse docker compose](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/docker-compose.yml)
- [Langfuse events_full DDL](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/packages/shared/clickhouse/migrations/clustered/0039_create_events_full.up.sql)
- [Langfuse overflow 实现](https://github.com/langfuse/langfuse/blob/983c2a6e5bbe9e8f35fe10eb017c9abd6220833b/worker/src/features/observation-field-overflow/processObservationFieldOverflow.ts)
- [Grafana Tempo dedicated columns](https://grafana.com/docs/tempo/latest/operations/dedicated_columns/)
- [ClickHouse JSON](https://clickhouse.com/docs/reference/data-types/newjson)
- [PostgreSQL JSON](https://www.postgresql.org/docs/current/datatype-json.html)
- [PostgreSQL TOAST](https://www.postgresql.org/docs/current/storage-toast.html)
- [Parquet Variant shredding](https://parquet.apache.org/docs/file-format/types/variantshredding/)
