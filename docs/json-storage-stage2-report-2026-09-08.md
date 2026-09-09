# Agent Trace JSON 存储阶段二横向实验报告

> 实验完成日期：2026-09-07；文档修订日期：2026-09-08
> 状态：六轮正式实验完成，18 个存储结构结果通过正确性与原文恢复门禁
> 数据路径：`independent_loader`
> 比较契约：`json-storage-cross-engine-v1`

## 1. 结果与适用范围

在 48,534 行固定数据、五类查询和三轮平衡顺序下，各存储结构的优势随查询场景变化。

- 稳定列聚合 Q01/Q03 不读取动态属性，ClickHouse 三种结构的 p50 均约为 **8–9 ms**，openGauss 三种结构约为 **27–32 ms**。该结果主要反映本次单机环境下两引擎处理聚合查询的差异，不用于判断 JSON 类型优劣。
- 路径过滤 Q02/Q05 中，ClickHouse Native JSON 分别为 **9.83 ms** 和 **7.57 ms**；openGauss 表达式索引将 Q02 降至 **20.59 ms**，`jsonb_hash_ops` GIN 将 Q05 降至全组最低的 **3.84 ms**。
- 完整 Trace 回查 Q04 中，ClickHouse String JSON 为 **13.38 ms**，ClickHouse Map 为 **14.24 ms**，openGauss 三种结构为 **22.93–23.37 ms**，ClickHouse Native JSON 为 **29.57 ms**。

上述延迟均为“每轮 p50，再取三轮中位数”。

稳定高频路径适合表达式索引或 Native JSON 直接子列读取。openGauss 包含查询可使用通过本数据正确性验证的 `jsonb_hash_ops` GIN。完整动态属性回查需同时考虑服务端物化、序列化、响应传输和客户端恢复。

持续写入结果用于说明同一载入流程的成本。写入期间的查询样本具有不同的已提交水位，因此只作为负载干扰证据，主要横向判断使用静态查询结果。

本阶段完成真实输入分布审计与动态属性横向比较。Full/Core、长 payload 布局和 asset reference 性能实验由[阶段三设计](json-storage-stage3-experiment-design-2026-09-08.md)承担。机制背景见[阶段一报告](json-storage-stage1-report-2026-09-08.md)。

## 2. 输入审计与版本基线

审计来源为固定 Trace 输入及其运行清单。源文件包含 48,534 个 span、6,257 个 trace、29 个原始 Attribute key。

每行 Attribute key 数的 p50/p95/p99/最大值为 8/14/15/15。该样本的动态属性较窄，不代表大量长尾路径竞争的生产分布。

| 审计项目 | 实测结果 |
|---|---|
| span 类型 | llm 17,486；tool 24,791；trace 6,257 |
| 15 分钟窗口 | 7 个；每窗口 key 数为 29、22、27、29、24、29、22 |
| 窗口路径变化 | 首窗口观测 29 个 key；后续新增 0/5/2/0/5/0，消失 7/0/0/5/0/7 |
| 混合类型 | `failure.mistake_agent`：null 3,587、string 5,340；`failure.mistake_step`：integer 7,174、null 1,859、string 1,622 |
| 类型冲突比例 | 上述两 key 分别为 40.18%、32.67%，分母为该 key 出现行数，分子为非主导类型行数 |
| 路径密度 | `span.type` 100%；`gen_ai.operation.name` 87.11%；输入/输出 messages 各 36.03%；tool arguments/result 各 51.08%；`prompt_text` 3.96% |
| 基数 | `raw_id`、`session_id`、`source_record_id` 各 6,257 个不同值，distinct/present 均为 1；`gen_ai.output.messages` 为 11,247 个不同值 |

| canonical UTF-8 长度，bytes | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| attributes | 1,042 | 4,663 | 7,697 | 62,197 |
| raw_event 原始 UTF-8 | 1,480 | 5,087 | 8,121 | 62,626 |
| gen_ai.input.messages | 2 | 2,732 | 4,123 | 61,475 |
| gen_ai.output.messages | 1,518 | 4,295 | 5,679 | 61,447 |
| gen_ai.tool.call.arguments | 392 | 1,858 | 2,841 | 21,436 |
| gen_ai.tool.call.result | 101 | 3,320 | 6,542 | 61,414 |

各 payload 分位数只统计出现该字段的行。tenant、project、instrumentation scope 在源数据中不可观测；framework 和 source_dataset 在 42,277 个子 span 上为空。窗口使用输入中 2030-01-01 的合成时间，路径变化描述此输入的时间切片，不能直接推断生产 schema 演化。横向生成器按冻结映射产生统一 project 字段，保留原始 key 与嵌套分析路径的可逆映射。

实验开始时两个同事仓的远端 `main` 与设计基线一致：exporter_demo 为 **2026-09-07 · `81b55be`**，trace-synthesis 为 **2026-09-07 · `ef3be14`**。实验程序基线为 **2026-09-08 · `6bdea39`**。

exporter_demo 的 18 列结构与 trace-synthesis 的 28 列 database catalog 尚未形成联合冻结，因此本实验使用独立载入程序，不代表 Collector、exporter 和 benchmark 的系统级性能。完整提交和文件身份保存在本地运行清单与实验汇总中。

## 3. 横向契约与正式轮次

实验在同一台 8 个逻辑 CPU、约 16 GiB 内存的主机上顺序运行。两个容器未设置显式 CPU 或内存上限。数据库版本为 **openGauss 6.0.0** 和 **ClickHouse 25.12.11.4**。镜像摘要、端口和容器资源配置保存在运行清单中。

seed 固定为 42；每种存储结构写入相同 48,534 行、190 个 block，每 block 256 行，末 block 150 行。前五个 block 提交后启动两个独立连接 worker，查询固定已提交水位；每轮结束完成维护，再对每个查询预热一次、正式测量 100 次。缓存状态为 `query_warmup_1_no_os_cache_drop`。

延迟从语句提交到完整响应读取结束，连接复用、返回排序、结果摘要和 truth 门禁保持一致。请求等价速率为 `成功样本数 × 1000 / 成功样本延迟总和(ms)`，单位为请求/秒，表示该延迟口径下的等价速率。

查询固定 project `Leoxx/whowhen_pro`、时间范围 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)` 及 `ingest_seq < watermark`。静态水位为 48,534。

Q01 按 `span_type` 分组计数；Q02 过滤 `gen_ai.operation.name=execute_tool` 后分组；Q03 计算分组数量和 `duration_ms` 总和；Q04 回查固定 Trace 的完整动态属性；Q05 过滤 `failure.mistake_mode=A.3`。最终 Q01/Q02/Q03/Q04 分别返回 3/1/3/6 行，Q05 匹配 741 行。

六轮有效运行均在连接建立成功后进入阶段屏障。连接建立不计入查询延迟。openGauss 和 ClickHouse 各完成三轮，每轮均保存独立运行清单。

本报告的统计全部来自本地证据目录 `docs/temp/json-storage-stage2/formal-20260907-retry-3/`。该目录不纳入版本库，复现入口位于实验 README。

汇总程序校验输入、样本、查询日志、正确性和清理状态，并从原始值重算指标。下文数值使用每轮指标的三轮中位数；每轮 p50/p95/p99 按 nearest-rank 计算。各轮原值和 min/max 保存在本地实验汇总中。

## 4. 动态属性存储结果

### 4.1 写入与空间

写入耗时为 190 个 block wall time 之和，含 analytics 与 raw 写入，维护另记。MiB/s 分子为所有 block 的原文输入字节数 103,792,865；dataset 文件同时含分析结构、身份和摘要，其文件体积作为重现产物身份记录。

载入速率与 block 延迟：

| 存储结构 | rows/s 中位数 [min, max] | MiB/s | block p95/p99，ms |
|---|---:|---:|---:|
| openGauss JSONB | 4,747 [4,716, 4,816] | 9.68 | 91.75 / 107.23 |
| openGauss JSONB + 表达式索引 | 4,638 [4,625, 4,664] | 9.46 | 91.81 / 103.02 |
| openGauss JSONB + GIN | 4,523 [4,471, 4,613] | 9.23 | 91.66 / 174.24 |
| ClickHouse String JSON | **5,860 [5,219, 5,935]** | **11.95** | 67.19 / 88.93 |
| ClickHouse Map | 5,482 [5,299, 5,538] | 11.18 | 73.20 / 85.95 |
| ClickHouse Native JSON | 3,688 [3,627, 3,698] | 7.52 | 104.43 / 122.00 |

表空间：

| 存储结构 | 分析表 MiB | 原文表 MiB | 总计 MiB |
|---|---:|---:|---:|
| openGauss JSONB | 73.64 | 83.28 | 156.92 |
| openGauss JSONB + 表达式索引 | 75.88 | 83.28 | 159.16 |
| openGauss JSONB + GIN | 81.28 | 83.28 | 164.56 |
| ClickHouse String JSON | 15.35 | 17.65 | 33.01 |
| ClickHouse Map | 24.99 | 17.65 | 42.65 |
| ClickHouse Native JSON | 27.69 | 17.65 | 45.34 |

**ClickHouse String JSON 在本载入流程中最快，ClickHouse Native JSON 最慢。** openGauss 表达式索引和 GIN 分别使载入速率相对无索引 JSONB 下降约 2.3% 和 4.7%。Native JSON 的载入包含路径解析、子列组织和 Sidecar 写入，这些步骤与 String JSON 直接保存文本不同。

openGauss 空间来自 `pg_total_relation_size`，包含 heap、TOAST 和索引的分配字节；ClickHouse 空间来自 active part 压缩字节。两者口径不同，因此只分别说明引擎内差异，不计算跨引擎压缩比。`jsonb_hash_ops` GIN 和 Native JSON Sidecar 的空间已计入表中。

openGauss 每轮 `ANALYZE` 均完成，本实验未单独计量其耗时。ClickHouse 每轮 merge backlog 连续三次为零，等待中位数在 String JSON、Map、Native JSON 下分别为 0.216、0.215、0.215 秒。

Native JSON 三轮观测到的 dynamic/shared path 并集数均为 35/9。该值汇总所有 active part；相同路径可在不同 part 中处于不同类别，不能按单个 part 的 dynamic path budget 解读。

### 4.2 静态查询

下列数值均为三轮中位数，每轮每查询包含 100 个成功样本。正文保留 p50 和 p95；p99、请求等价速率及各轮范围保存在本地实验汇总中。

#### 4.2.1 稳定列聚合

Q01 和 Q03 只读取强类型稳定列，用于观察不解析动态属性时的查询基线。

| 存储结构 | Q01 p50/p95，ms | Q03 p50/p95，ms |
|---|---:|---:|
| openGauss JSONB | 26.93 / 29.31 | 32.26 / 34.46 |
| openGauss JSONB + 表达式索引 | 26.89 / 29.20 | 32.42 / 34.96 |
| openGauss JSONB + GIN | 26.96 / 28.81 | 32.37 / 36.12 |
| ClickHouse String JSON | **7.88 / 9.71** | 9.14 / 10.75 |
| ClickHouse Map | 7.91 / 9.42 | **8.55 / 10.26** |
| ClickHouse Native JSON | 8.12 / 9.65 | 8.69 / 10.60 |

ClickHouse 三种结构在两条查询上接近，openGauss 三种结构也接近。**这两条查询不能用来证明某种 JSON 类型更快**，它们主要呈现本次环境下两个引擎的基础聚合差异。

#### 4.2.2 动态路径过滤

Q02 查询高频路径，Q05 查询低密度路径。

| 存储结构 | Q02 p50/p95，ms | Q05 p50/p95，ms |
|---|---:|---:|
| openGauss JSONB | 112.49 / 135.71 | 88.77 / 92.42 |
| openGauss JSONB + 表达式索引 | **20.59 / 22.77** | 88.67 / 94.41 |
| openGauss JSONB + GIN | 110.97 / 114.30 | **3.84 / 5.38** |
| ClickHouse String JSON | 85.31 / 95.41 | 69.58 / 74.97 |
| ClickHouse Map | 51.34 / 61.00 | 35.11 / 41.14 |
| ClickHouse Native JSON | **9.83 / 11.64** | **7.57 / 8.96** |

openGauss 表达式索引将 Q02 p50 从 112.49 ms 降至 **20.59 ms**，约为原来的 1/5.5；该索引不匹配 Q05，因此 Q05 没有同类收益。`jsonb_hash_ops` GIN 与 Q05 的 JSONB 包含谓词匹配，将 p50 从 88.77 ms 降至 **3.84 ms**。

ClickHouse Native JSON 将 Q02/Q05 p50 分别降至 String JSON 的约 1/8.7 和 1/9.2。Map 介于 String JSON 和 Native JSON 之间。**路径组织或匹配查询的索引，是这组场景的主要差异来源。**

ClickHouse `QueryFinish` 显示，Native JSON 在 Q02/Q05 中读取的逻辑数据约为 1.7–1.8 MiB，String JSON 约为 45.8–48.4 MiB。

| 存储结构 / 查询 | 读取 MiB | 内存 MiB | 服务端耗时 ms |
|---|---:|---:|---:|
| ClickHouse String JSON / Q02 | 48.39 | 34.81 | 84 |
| ClickHouse Map / Q02 | 52.65 | 50.45 | 50 |
| ClickHouse Native JSON / Q02 | **1.68** | **5.37** | **8** |
| ClickHouse String JSON / Q05 | 45.82 | 20.34 | 68 |
| ClickHouse Map / Q05 | 49.02 | 30.54 | 33 |
| ClickHouse Native JSON / Q05 | **1.79** | **5.20** | **6** |

服务端耗时来自 ClickHouse 查询日志；前表的延迟从 SQL 提交计算到客户端完整读取响应。横向对比统一使用后者。

#### 4.2.3 完整 Trace 回查

Q04 返回同一 Trace 的六条记录及完整动态属性。

| 存储结构 | Q04 p50/p95，ms |
|---|---:|
| openGauss JSONB | 22.93 / 24.54 |
| openGauss JSONB + 表达式索引 | 23.05 / 24.52 |
| openGauss JSONB + GIN | 23.37 / 25.20 |
| ClickHouse String JSON | **13.38 / 15.24** |
| ClickHouse Map | **14.24 / 16.45** |
| ClickHouse Native JSON | 29.57 / 34.59 |

**ClickHouse String JSON 在本次完整 Trace 回查中最快。** Native JSON 需要读取和物化多个路径，并额外返回 Sidecar，p50 约为 String JSON 的 2.2 倍。客户端 Sidecar 合并、canonical 规范化和 truth 校验在计时区间外，因此完整返回流程还需单独评估客户端恢复成本。

### 4.3 写入期间的并发查询

| 存储结构 | 每轮并发样本，中位数 [min, max] | Q02 p50 ms | Q05 p50 ms |
|---|---:|---:|---:|
| openGauss JSONB | 728 [720, 744] | 24.68 | 14.19 |
| openGauss JSONB + 表达式索引 | 984 [956, 984] | 12.72 | 19.62 |
| openGauss JSONB + GIN | 952 [952, 964] | 30.64 | 3.57 |
| ClickHouse String JSON | 572 [568, 592] | 41.13 | 36.17 |
| ClickHouse Map | 652 [648, 656] | 42.39 | 31.28 |
| ClickHouse Native JSON | 1,436 [1,424, 1,464] | 17.35 | 16.53 |

样本覆盖各存储结构写入期间实际到达的已提交水位，truth 随水位独立核对。并发样本数和水位分布由写入持续时间与查询耗时共同决定，不能按相同水位逐样本配对。**这组数据只说明写入干扰下的行为，不用于代替 §4.2 的静态横向比较。**

## 5. 正确性、恢复与异常

| 门禁 | 正式 retry-3 结果 |
|---|---|
| 运行清单 / 结果 | 6/6 完成；18/18 存储结构结果完成；文件身份全部匹配 |
| 查询 truth | 静态 9,000，加写入并发 15,996，共 24,996 个样本全部成功 |
| 分析结构恢复 | 每种存储结构每轮 48,534 行，canonical hash 全部匹配；缺失、额外、重复 ID 为零 |
| 原文恢复 | 每种存储结构每轮 48,534 行，原始 UTF-8 内容摘要全部匹配 |
| ClickHouse 日志 | 12,512 个正式样本具备唯一 QueryFinish 和完整的八项指标；静态预热同步核对 |
| 维护与清理 | 18/18 清理完成；ClickHouse merge backlog 为零；无实验临时 schema 或 database |

ClickHouse Native JSON 不能独立表达本输入中所有 null、空对象和空数组状态。分析表因此保存稀疏 `fidelity_values Map(String,String)` Sidecar：当一个 Attribute 递归包含这些值时，Sidecar 保存该 Attribute 的完整 canonical JSON value。Q04 和分析结构恢复在读取 Native JSON 后使用 Sidecar 覆盖相应值。

Sidecar 参与 Native JSON 的写入、空间和完整回查成本。所有存储结构还使用独立 raw 表保存逐行原始 UTF-8 文本。分析 JSON 恢复与原文恢复分别校验。

| 诊断目录 | 原因与处理 |
|---|---|
| `formal-20260907/` | openGauss 6.0.0 `jsonb_ops` GIN 写入递归空字符串时触发 `jsonb_gin.cpp:519`。有效实验改用 `jsonb_hash_ops`，并通过空字符串与恢复回归测试。 |
| `formal-20260907-retry-1/` | 逐查询刷新全局日志增加 system log part 与 merge，最终触发 ClickHouse 服务端总内存限制。有效实验改为阶段结束后批量采集。 |
| `formal-20260907-retry-2/` | ClickHouse 连接未在阶段屏障前显式建立，首个并发样本可能包含 TCP 建连时间。为保持统一计时边界，整组结果排除，有效实验在连接成功后进入屏障。 |

三个目录只保留诊断，其中的成功部分也全部排除。有效结果完整重跑六轮。ClickHouse 查询日志在每个并发或静态阶段结束后批量采集。查询日志生成仍属于本次执行环境的观测成本；只有刷新、读取和核对位于查询延迟计时外。具体观测开关保存在实验 README 和运行清单中。

## 6. 采用建议与证据边界

对本数据的动态属性查询，建议按场景保留以下候选：

- openGauss JSONB 作为 openGauss 基线。稳定高频路径使用表达式索引；已验证的包含查询使用 `jsonb_hash_ops` GIN。
- ClickHouse String JSON 作为 ClickHouse 写入、空间和完整 Trace 回查基线。
- ClickHouse Native JSON 用于高频路径过滤。它的完整回查需要同时计入 Sidecar 读取和客户端恢复。
- ClickHouse Map 的路径过滤性能位于 String JSON 与 Native JSON 之间，完整 Trace 回查接近 String JSON，适合按查询组合继续评估。

**存储结构选择需要结合路径查询频率、完整 Trace 回查比例和写入预算。** 单项最优结果不能直接推广为整体方案结论。

本实验覆盖一个冻结输入、单机容器、固定查询参数与热查询流程。饱和吞吐、冷缓存、长时间维护稳态、故障恢复、分布式副本和当前 exporter/benchmark 全链路仍需验证。全部引擎的同口径 CPU、内存指标也未纳入本阶段。

系统级比较需要先冻结两仓 schema 和 event policy，并复用本阶段的输入、返回和恢复门禁。

复现步骤见[实验 README](../experiments/json-storage-stage2/README.md)。正式结果通过以下命令重新校验和汇总：

```bash
python3 experiments/json-storage-stage2/report/summarize_results.py \
  --input docs/temp/json-storage-stage2/formal-20260907-retry-3 \
  --output docs/temp/json-storage-stage2/formal-20260907-retry-3/summary
```

汇总程序校验轮次、运行状态、输入和结果身份、样本与水位、查询日志、正确性、原文恢复和清理状态。运行清单、DDL 身份和详细诊断保存在实验目录；公开仓库保留实验程序、设计和报告。

## 参考资料

- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-08.md)。
- [ClickHouse JSON 类型说明](https://clickhouse.com/docs/reference/data-types/newjson)，null/缺失语义与路径存储。
- [openGauss v6.0.0 jsonb_gin.cpp](https://gitee.com/opengauss/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_gin.cpp)。
