# Agent Trace JSON 存储阶段二调优矩阵实验设计

> 状态：正式实验完成
>
> 日期：2026-09-14
>
> 数据路径：独立载入程序
>
> 契约：`json-storage-tuned-matrix-v2`
>
> 原理说明：[JSON 存储原理](json-storage-principles-2026-09-09.md)

## 1. 目标

实验在同一冻结输入、派生数据、查询语义和完整文档契约下比较 openGauss JSON/JSONB 与 ClickHouse String/Native JSON，并验证定向索引、Native JSON type hint、排序键和 ClickHouse merge 状态的实际效果。

实验回答四类问题：

1. 文本 JSON 与结构化 JSON 在载入、路径查询和完整文档读取中的引擎内差异；
2. JSON 数值路径聚合与普通列控制能否得到一致、可验证的结果；
3. openGauss 索引和 ClickHouse 物理路径/排序键是否被执行器实际采用；
4. 调优收益伴随的写入、空间和维护成本，以及后台 merge 对载入和查询的影响。

跨引擎结果表示固定软硬件下的完整存储方案，不用于分离 JSON 类型的单因素成本。正式结论见[阶段二报告](json-storage-stage2-report-2026-09-10.md)。

## 2. 数据契约

输入复用 `docs/temp/json-storage-stage2/cross-engine-input-20260907/dataset.jsonl`，包含 48,534 行、190 个 block，block 上限为 256 行。输入及来源清单继续按固定字节数和 SHA-256 校验。

每行载入前在 `attributes_analysis` 副本中增加 `experiment.duration_ms`，其整数值复制自同一行的普通列 `duration_ms`。派生函数不修改冻结输入对象，并拒绝覆盖来源中已经存在的同名路径。

新版 truth 位于 `docs/temp/json-storage-stage2-sup/input-20260911-v2/`，包含：

- 七个查询的固定参数和结果；
- 派生后每行分析文档的 canonical SHA-256；
- 每行首次解析前原文的 SHA-256；
- 冻结 dataset、来源 truth 和运行清单的身份。

固定窗口包含 27,561 行，数值路径聚合结果为 `count=27,561`、`sum=51,129,737`。

## 3. 存储结构

### 3.1 表示对照

| 目标 | 动态属性列 | 排序或索引 |
|---|---|---|
| openGauss JSON | `attributes JSON` | 主键 `event_id` |
| openGauss JSONB | `attributes JSONB` | 主键 `event_id` |
| ClickHouse String JSON | `attributes String CODEC(ZSTD(3))` | `ORDER BY (project_id,start_time,event_id)` |
| ClickHouse Native JSON | `attributes JSON(max_dynamic_paths=32)` | `ORDER BY (project_id,start_time,event_id)` |

四种表示使用相同普通列和独立原文表。ClickHouse Native JSON 增加实验自定义的 `fidelity_values Map(String,String)` 稀疏 Sidecar，以恢复 JSON null、空容器和含点号键等 Native JSON 无法单独完整保留的结构。

### 3.2 调优候选

| 候选 | DDL 变化 | 验证目标 |
|---|---|---|
| openGauss JSONB + 索引 | 高密度和低匹配率路径复合表达式 B-tree、Trace 复合 B-tree、整个 JSONB 的 `jsonb_hash_ops` GIN | 正式参数化执行、诊断计划和 `idx_scan` 共同确认访问路径 |
| Native JSON + 数值 type hint | `experiment.duration_ms Int64` | 该路径不进入 dynamic/shared 清单，数值聚合结果一致 |
| Native JSON + Trace 排序键 | `ORDER BY (project_id,trace_id,start_time,event_id)` | 主键计划含 `trace_id` 和 binary search，同时记录所有场景 `read_rows` |

type hint 与 Trace 排序键分别运行，避免把两个变量的效果合并。稀疏字符串路径保持自动 dynamic/shared 组织，防止非 Nullable hint 在缺失行生成类型默认值并改变完整文档。

## 4. 查询契约

实现编号只用于 catalog、结果文件和 runner 关联；报告使用语义名称。

| 编号 | 语义名称 | 查询结果 |
|---|---|---|
| S01 | 普通列分组控制 | `span_type,count`；不读取 JSON |
| S02 | 高密度路径过滤 | `gen_ai.operation.name=execute_tool` 后分组 |
| S03 | 低匹配率路径过滤 | `failure.mistake_mode=A.3` 的数量和 identity |
| S04 | 大值路径投影 | `gen_ai.output.messages` 的值、非空数和 canonical bytes |
| S05 | 完整 Trace | 固定 Trace 的 6 行完整逻辑文档 |
| S06 | 带总数的偏移分页 | 窗口总数及固定位置的 256 行完整逻辑文档 |
| S07 | 数值 JSON 路径聚合 | `experiment.duration_ms` 的非空数和总和 |

两个表达式 B-tree 分别使用 `(project_id, attributes #>> 路径, start_time)`，服务高密度和低匹配率的 `#>> =` 谓词。GIN 使用与低匹配率查询结果等价的 `attributes @> ...::jsonb` containment，并单独记录无索引和有索引样本。

## 5. 执行顺序与计时

每个目标执行三轮。除 S06 外，每个查询每轮测量 30 次；S06 每轮测量 10 次。每轮每目标共 190 个正式样本，查询前各预热一次。两个已建立连接的 worker 并发消费固定任务序列。

载入计时从提交第一个预生成 block 开始，到分析表和原文表的第 190 个 block 均写入并可见结束。派生 JSON 和 Sidecar 的逐行构造包含在 block wall time；容器发现和 DDL 不计入载入速率。

报告同时给出写入吞吐和查询就绪吞吐。后者加入 openGauss `ANALYZE`；主矩阵的 ClickHouse 单 part 对照加入 merge 等待和两张表的 `OPTIMIZE TABLE ... FINAL`。merge 性能探针另行比较后台 merge 开启和暂停的载入，并在 190 part、并发 merge、稳定少量 part 和单 part 状态执行相同查询。

查询端到端时间覆盖语句提交和响应完整读取。客户端恢复时间覆盖 JSON 解析、Sidecar 合并、规范化和 truth 核对。应用可用时间按单样本相加，再计算轮内 p50 和三轮 p50 的中位数。

## 6. 访问路径与正确性门禁

openGauss 调优结果必须同时满足：

- 正式高密度参数化查询每轮产生 31 次 `analytics_operation_name_idx` 扫描；
- 高密度扫描 Hint 诊断计划出现 `analytics_operation_name_idx`，实际扫描增量达到样本数；
- 表达式 B-tree 计划出现 `analytics_failure_mode_idx`；
- GIN containment 计划出现 `analytics_attributes_gin_idx`；
- Trace 计划出现 `analytics_trace_lookup_idx`；
- `pg_stat_user_indexes.idx_scan` 记录实际扫描；
- 正式参数与计划参数一致。

ClickHouse 调优结果必须同时满足：

- 每个 active part 的 dynamic/shared path 清单已采集；
- 数值 hint 候选的 DDL 含 `experiment.duration_ms Int64`，该路径不进入自动路径清单；
- Trace 排序候选的 `EXPLAIN indexes=1` 显示含 `trace_id` 的 PrimaryKey 与 binary search；
- `QueryFinish` 完整记录每个正式样本的 `read_rows` 和 `read_bytes`。

全部结构还必须满足记录 identity、查询 truth、分析文档摘要、原文摘要和清理门禁。任何门禁失败的运行只作诊断，不进入汇总。

ClickHouse merge 性能探针还必须确认：暂停 merge 后分析表和原文表均为 190 个 active part；恢复后查询期间实际观测到 active merge；稳定和 `OPTIMIZE FINAL` 阶段达到记录的 part 状态；异常路径恢复两张表的 merge。

## 7. 正式结果集合

正式结果位于 gitignored 目录：

```text
docs/temp/json-storage-stage2-sup/
  input-20260911-v2/
  final-matrix-20260911/round-{1,2,3}/
  isolation-matrix-20260911/round-{1,2,3}/
  text-baseline-matrix-20260911/round-{1,2,3}/
  review-index-matrix-20260914/round-{1,2,3}/
  review-merge-matrix-20260914/round-{1,2,3}/
```

`final-matrix`、`isolation-matrix` 和 `text-baseline-matrix` 提供七个主目标。`review-index-matrix` 使用复合表达式索引复测 openGauss 调优与基线；`review-merge-matrix` 提供 ClickHouse merge 开启/暂停和四个查询阶段。失败或旧索引定义的运行保留为诊断，不进入报告汇总。

主报告使用 7 个结构或候选、每个 3 轮，共 21 个目标运行和 3,990 个正式查询样本。

## 8. 未覆盖范围

本矩阵不覆盖小标量投影、同一路径在 type hint/dynamic/shared 间的直接对照、keyset pagination、固定 offered load 的持续写入、多节点、冷缓存、故障恢复和 Collector/exporter 完整链路。merge 查询阶段按物理状态演进顺序执行，缓存影响与状态变化尚未完全分离。

## 参考资料

- [阶段二报告](json-storage-stage2-report-2026-09-10.md)
- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
