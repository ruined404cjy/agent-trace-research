# Agent Trace JSON 存储阶段二组内汇报辅助材料

> 日期：2026-09-11
>
> 详细证据：[阶段二报告](json-storage-stage2-report-2026-09-10.md)
>
> 原理说明：[JSON 存储原理](json-storage-principles-2026-09-09.md)

## 1. 核心结论

四种 JSON 表示没有统一最优项。结构化表示适合路径过滤和聚合，文本表示适合直接返回完整文档；索引、type hint 和排序键只有在实际访问路径与查询前缀匹配时才有价值。

| 观察 | 当前结果 |
|---|---|
| openGauss 路径查询 | JSONB 的高密度过滤、低匹配率过滤和数值路径聚合均低于 JSON |
| ClickHouse 路径查询 | Native JSON 的三类路径查询均低于 String JSON |
| 完整文档 | openGauss JSON 的带总数分页低于 JSONB；ClickHouse String JSON 的完整 Trace 和分页低于 Native JSON + Sidecar |
| openGauss 定向索引 | 低匹配率表达式 B-tree 为 3.83 ms，等价 containment 的 GIN 为 4.45 ms，Trace 复合 B-tree 为 1.54 ms |
| ClickHouse 数值 type hint | 数值聚合从 7.35 ms 变为 7.01 ms，三轮证据不足以确认实质收益 |
| ClickHouse Trace 排序键 | Trace 查询变为 28.74 ms，分页变为 224.73 ms，读取行数同步扩大，候选无效 |

高密度 `gen_ai.operation.name=execute_tool` 在固定窗口命中 73.13% 的行。openGauss 自然计划继续使用顺序扫描，当前证据不支持为该谓词采用表达式索引。

## 2. 实验契约

输入包含 48,534 个 Span，按 256 行组成 190 个 block。四种表示共享行序、普通列、查询参数、完整文档 truth 和首次解析前原文。实验载入视图为每行增加 `experiment.duration_ms`，值复制自普通列 `duration_ms`，用于比较普通列与 JSON 数值路径的聚合结果。

| 表示 | 动态属性列 | 完整文档恢复 |
|---|---|---|
| openGauss JSON | `attributes JSON` | 解析文本 datum |
| openGauss JSONB | `attributes JSONB` | 遍历二进制文档并序列化 |
| ClickHouse String JSON | `attributes String` | 解析 JSON 字符串 |
| ClickHouse Native JSON | `attributes JSON(max_dynamic_paths=32)` | 汇集物理路径并应用稀疏 Sidecar |

Native JSON 的稀疏 Sidecar 是本实验自定义结构，用于补回 JSON null、空容器和含点号键等逻辑状态。它不是 ClickHouse 官方组件，也不承担输入 bytes 的逐字节恢复；原文表独立保存首次解析前的 UTF-8 文本。

## 3. 七类查询

| 查询 | 代表的访问模式 |
|---|---|
| 普通列分组控制 | 不读取 JSON，观察普通列执行基线 |
| 高密度路径过滤 | 高命中率字符串路径过滤后分组 |
| 低匹配率路径过滤 | 低命中率标量等值过滤与身份核对 |
| 大值路径投影 | 返回约 7 MiB 的数组或对象值 |
| 完整 Trace | 按 Trace 定位并返回 6 行完整逻辑文档 |
| 带总数的偏移分页 | 精确计数、排序并返回 256 行完整逻辑文档 |
| 数值 JSON 路径聚合 | 对确定性整数路径执行非空计数和求和 |

每个目标运行三轮。每轮除分页外的查询各测量 30 次，分页测量 10 次，共 190 个正式样本。ClickHouse 查询前将分析表和原文表分别合并为单个 active part；openGauss 记录自然计划和索引扫描计数，ClickHouse 记录 `QueryFinish`、路径清单和主键计划。

## 4. 结果解释边界

报告使用应用可用 p50：每个样本先将数据库端到端查询时间与客户端恢复时间相加，再计算轮内和轮间中位数。完整文档结果因此包含 JSON 解析、Native JSON 与 Sidecar 合并以及 truth 核对前的规范化开销。

跨引擎数字表示同一硬件和应用结果契约下的完整方案。行存或列存、排序键、索引、执行器、协议、压缩和维护策略同时参与结果，不能把差异归因于 JSON 类型本身。空间结果也只适合引擎内比较：openGauss 统计 heap、TOAST 和索引的已分配字节，ClickHouse 统计 active data part 的压缩后字节。

## 5. 当前选择

稳定、高频且类型明确的字段继续使用普通强类型列。openGauss 动态属性分析保留 JSONB 候选，并按低匹配率标量等值、containment 和 Trace 定位分别评估表达式 B-tree、GIN 和普通复合 B-tree。ClickHouse 路径分析保留 Native JSON 候选，完整文档读取频繁时同时保留 String JSON 对照。

后续测试优先补充小标量投影、同一路径在 type hint、dynamic path 和 shared data 间的受控对照、keyset pagination，以及固定 offered load 下的持续写入与 merge backlog。详细数字、访问计划和限制见[阶段二报告](json-storage-stage2-report-2026-09-10.md)。
