# Agent Trace JSON 存储阶段二横向实验报告

> 日期：2026-09-07
> 状态：六轮正式实验完成，18 个布局结果通过正确性与原文恢复门禁
> 数据路径：`independent_loader`
> 比较契约：`json-storage-cross-engine-v1`

## 1. 结果与适用范围

在本次 48,534 行固定数据、五类查询与三轮平衡顺序下，布局收益随查询变化。ClickHouse native JSON 的路径过滤 Q02、Q05 分别为 9.83 ms、7.57 ms；openGauss 热路径表达式索引布局的 Q02 为 20.59 ms，`jsonb_hash_ops` GIN 布局的 Q05 为 3.84 ms。完整 Trace 回查 Q04 中，ClickHouse String、Map、native JSON 分别为 13.38 ms、14.24 ms、29.57 ms，openGauss 三布局为 22.93～23.37 ms。这里的延迟均为每轮 p50 的三轮中位数。

因此，稳定高频路径适合表达式索引或 native JSON 路径读取；openGauss 包含查询可采用通过本数据正确性验证的 `jsonb_hash_ops` GIN；完整 residual 回查需要评估服务端物化、序列化与响应传输成本。上述结论对应本报告的静态查询和固定返回契约。持续写入结果提供同一加载流程的成本证据；写入期间的查询样本因加载速度和水位分布不同，作为负载干扰证据单独呈现。

本阶段完成真实输入分布审计与 residual 横向比较。Full/Core、长 payload 布局和 asset reference 的性能实验由[阶段三设计](json-storage-stage3-experiment-design.md)承担。机制背景见[阶段一报告](json-storage-stage1-report-2026-09-07.md)，本报告的横向数字全部来自本阶段正式根。

## 2. 输入审计与冻结身份

审计来源为 [audit.json](temp/json-storage-stage2/real-trace-audit-20260907/audit.json) 及同目录完成 manifest。源文件包含 48,534 个 span、6,257 个 trace、29 个原始 Attribute key。每行 key 数 p50/p95/p99/max 为 8/14/15/15；该样本覆盖的是窄 residual 分布。

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

六轮 manifest 中两同事仓远端 `main` 均与实验设计基线一致：exporter 为 `81b55be6d6912d18c4e2ac7102fd7906e9dac3e8`，trace-synthesis 为 `ef3be141cc17415de9fb5a9d8003c16a4cd679ac`。18 列 exporter 与 28 列 database catalog 的联合冻结仍待形成。正式 runner 所在 research 提交为 `6bdea39f08efea6b92e412c96a4a046b9f97b60f`；完整本地 HEAD 和远端身份保存在每轮 manifest。

| 冻结对象 | SHA-256 |
|---|---|
| 源 JSONL | `3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683` |
| 上游 manifest | `f46bbe843c5578faea9ddfb5e8eb3aac8b6dc4c2f4fb89beabab503043505e38` |
| audit.json | `195d4f977d0a089ca994a3280afa8e61035645b1906193a4ce08743a9d15eb81` |
| dataset.jsonl，302,518,948 bytes | `8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405` |
| truth-manifest.json，18,073,179 bytes | `b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04` |
| runner 内容摘要 | `7f2ac038fd30d0b5aae613bedc3dda4457c575ab296ba59088b7a5f24e2652cd` |
| summary.json | `d8c810f7126097eb2af240df494f8e6340a951159210cff9f3d4125481601494` |

## 3. 横向契约与正式轮次

宿主机为 8 个逻辑 CPU、16,682,950,656 bytes 内存、1,081,101,176,832 bytes 磁盘容量。两个容器均未设置显式 CPU/内存上限，六轮顺序运行。openGauss 为 6.0.0 build aee4abd5，端口 15432，镜像 digest 为 `enmotech/opengauss@sha256:9bd81380273944e5a02a2139c90954d4f46813b71810f7b23fe8f738014d03b5`；ClickHouse 为 25.12.11.4，HTTP 端口 18123，镜像 digest 为 `clickhouse/clickhouse-server@sha256:8a790dd3468db22b1d4e7b18a176f378ff5ff6053b9c48dd4ea1fa71a24c5ba6`。

seed 固定为 42；每布局写入相同 48,534 行、190 个 block，每 block 256 行，末 block 150 行。前五个 block 提交后启动两个独立连接 worker，查询固定已提交水位；每轮结束完成维护，再对每个查询预热一次、正式测量 100 次。缓存状态为 `query_warmup_1_no_os_cache_drop`。延迟从语句提交到完整响应读取结束，连接复用、返回排序、结果摘要和 truth 门禁保持一致。请求等价速率为 `成功样本数 × 1000 / 成功样本 latency_ms 之和`，单位请求/秒，表示该延迟口径下的请求等价速率。

查询固定 project `Leoxx/whowhen_pro`、时间范围 `[2030-01-01T00:00:00.000Z, 2030-01-01T00:52:08.500Z)` 及 `ingest_seq < watermark`。静态水位为 48,534。Q01 返回按 span_type 排序的计数；Q02 过滤 operation=`execute_tool` 后分组计数；Q03 返回分组 count、sum(duration_ms)；Q04 回查固定 trace `7399146000e457a19c92ade12291435c` 的身份与 attributes 摘要；Q05 过滤 mistake_mode=`A.3` 后返回 count 与 identity digest。最终 Q01/Q02/Q03/Q04 结果分别为 3/1/3/6 行，Q05 匹配 741 行。

retry-3 的两个引擎均在实际连接建立成功后进入阶段屏障。ClickHouse `connect_worker()` 返回前完成 TCP connect；连接建立位于查询延迟计时外。

| 引擎/轮次 | run ID | run-manifest |
|---|---|---|
| openGauss 1 | `opengauss-r1-165c9c48f5e54231a270d4b9c420478b` | [manifest](temp/json-storage-stage2/formal-20260907-retry-3/opengauss-round-1/run-manifest.json) |
| openGauss 2 | `opengauss-r2-789d3f28f7244aadab8841dbbdc60513` | [manifest](temp/json-storage-stage2/formal-20260907-retry-3/opengauss-round-2/run-manifest.json) |
| openGauss 3 | `opengauss-r3-e30c83fa440f48d9a654e0a618ed55ec` | [manifest](temp/json-storage-stage2/formal-20260907-retry-3/opengauss-round-3/run-manifest.json) |
| ClickHouse 1 | `clickhouse-r1-34a66da4709c436297fe73403918c5f7` | [manifest](temp/json-storage-stage2/formal-20260907-retry-3/clickhouse-round-1/run-manifest.json) |
| ClickHouse 2 | `clickhouse-r2-c3af84fa6c8e48369a1fb0c2fad9d6c7` | [manifest](temp/json-storage-stage2/formal-20260907-retry-3/clickhouse-round-2/run-manifest.json) |
| ClickHouse 3 | `clickhouse-r3-bc5233e2da7b44a7baafd9fb6b26a94f` | [manifest](temp/json-storage-stage2/formal-20260907-retry-3/clickhouse-round-3/run-manifest.json) |

唯一正式统计根为 `docs/temp/json-storage-stage2/formal-20260907-retry-3/`。每个 manifest 包含布局轮换顺序、完整命令、环境、输入 lineage 和三个 result 的字节数/SHA。汇总器校验全部身份、样本、日志和完成门禁，并从原始值重算指标。下面的数值采用每轮派生指标的三轮中位数；每轮 p50/p95/p99 使用 nearest-rank。全部三轮原值及 min/max 保存在 [summary.json](temp/json-storage-stage2/formal-20260907-retry-3/summary/summary.json)，不合并不同轮的样本后计算分位数。

## 4. Residual 结果

### 4.1 写入与空间

写入耗时为 190 个 block wall time 之和，含 analytics 与 raw 写入，维护另记。MiB/s 分子为所有 block 的原文输入字节数 103,792,865；dataset 文件同时含分析结构、身份和摘要，其文件体积作为重现产物身份记录。

| 布局 | rows/s 中位数 [min, max] | MiB/s | block p95/p99，ms | analytics MiB | raw MiB | total MiB |
|---|---:|---:|---:|---:|---:|---:|
| og_jsonb | 4,747 [4,716, 4,816] | 9.68 | 91.75 / 107.23 | 73.64 | 83.28 | 156.92 |
| og_jsonb_hot | 4,638 [4,625, 4,664] | 9.46 | 91.81 / 103.02 | 75.88 | 83.28 | 159.16 |
| og_jsonb_gin | 4,523 [4,471, 4,613] | 9.23 | 91.66 / 174.24 | 81.28 | 83.28 | 164.56 |
| ch_string | 5,860 [5,219, 5,935] | 11.95 | 67.19 / 88.93 | 15.35 | 17.65 | 33.01 |
| ch_map | 5,482 [5,299, 5,538] | 11.18 | 73.20 / 85.95 | 24.99 | 17.65 | 42.65 |
| ch_native | 3,688 [3,627, 3,698] | 7.52 | 104.43 / 122.00 | 27.69 | 17.65 | 45.34 |

openGauss 空间来自 `pg_total_relation_size`，包含 heap、TOAST、index 的分配字节；ClickHouse 空间来自 active part compressed bytes。表格保留这两个物理口径，本报告不据此计算跨引擎压缩比。各布局三轮空间读数相同。`og_jsonb_gin` 使用 `jsonb_hash_ops`；`ch_native` 的 `fidelity_values` 计入 analytics 空间。

openGauss 每轮 ANALYZE 均完成，runner 未记录其耗时。ClickHouse 每轮 merge backlog 连续三次为零，等待中位数 String/Map/native 为 0.216/0.215/0.215 秒。native 三轮观测到的 dynamic/shared path 并集数均为 35/9；该值汇总所有 active parts，相同路径可在不同 part 中处于不同类别，因此不能按单个 part 的 dynamic path budget 解读。

### 4.2 静态查询

每行各指标均为三轮中位数，每轮每查询 100 个成功样本。速率单位为请求/秒。

| 布局 | 查询 | p50 ms | p95 ms | p99 ms | 请求等价速率 |
|---|---|---:|---:|---:|---:|
| og_jsonb | Q01 | 26.93 | 29.31 | 31.16 | 36.91 |
| og_jsonb | Q02 | 112.49 | 135.71 | 178.71 | 8.67 |
| og_jsonb | Q03 | 32.26 | 34.46 | 35.69 | 30.75 |
| og_jsonb | Q04 | 22.93 | 24.54 | 25.43 | 43.29 |
| og_jsonb | Q05 | 88.77 | 92.42 | 96.22 | 11.26 |
| og_jsonb_hot | Q01 | 26.89 | 29.20 | 35.85 | 36.94 |
| og_jsonb_hot | Q02 | 20.59 | 22.77 | 24.74 | 47.54 |
| og_jsonb_hot | Q03 | 32.42 | 34.96 | 35.58 | 30.68 |
| og_jsonb_hot | Q04 | 23.05 | 24.52 | 25.36 | 43.22 |
| og_jsonb_hot | Q05 | 88.67 | 94.41 | 98.64 | 11.16 |
| og_jsonb_gin | Q01 | 26.96 | 28.81 | 31.46 | 36.81 |
| og_jsonb_gin | Q02 | 110.97 | 114.30 | 116.65 | 8.99 |
| og_jsonb_gin | Q03 | 32.37 | 36.12 | 39.67 | 30.53 |
| og_jsonb_gin | Q04 | 23.37 | 25.20 | 25.51 | 42.70 |
| og_jsonb_gin | Q05 | 3.84 | 5.38 | 5.70 | 246.83 |
| ch_string | Q01 | 7.88 | 9.71 | 9.95 | 126.12 |
| ch_string | Q02 | 85.31 | 95.41 | 104.18 | 11.62 |
| ch_string | Q03 | 9.14 | 10.75 | 11.92 | 108.02 |
| ch_string | Q04 | 13.38 | 15.24 | 16.34 | 74.13 |
| ch_string | Q05 | 69.58 | 74.97 | 81.68 | 14.20 |
| ch_map | Q01 | 7.91 | 9.42 | 10.13 | 125.43 |
| ch_map | Q02 | 51.34 | 61.00 | 62.96 | 19.28 |
| ch_map | Q03 | 8.55 | 10.26 | 10.78 | 114.96 |
| ch_map | Q04 | 14.24 | 16.45 | 20.51 | 70.11 |
| ch_map | Q05 | 35.11 | 41.14 | 44.41 | 28.19 |
| ch_native | Q01 | 8.12 | 9.65 | 10.36 | 121.98 |
| ch_native | Q02 | 9.83 | 11.64 | 12.43 | 100.66 |
| ch_native | Q03 | 8.69 | 10.60 | 10.91 | 114.17 |
| ch_native | Q04 | 29.57 | 34.59 | 37.38 | 33.25 |
| ch_native | Q05 | 7.57 | 8.96 | 9.45 | 131.28 |

Q01/Q03 主要读取公共标量列，ClickHouse 三布局结果接近。Q02/Q05 在 residual 上选择路径，native JSON 减少读取的字节。Q04 读取完整 residual，native 路径组织及额外返回的可逆状态参与服务端读取和响应传输。客户端 key 恢复、`fidelity_values` 合并、canonical 规范化和 truth 摘要均位于计时外，因此表中延迟不衡量这些客户端操作。openGauss GIN 的 Q05 收益对应包含查询及记录在 result 中的自然执行计划；其他四个查询未显示同等收益。

ClickHouse `QueryFinish` 的每轮 p50 再取三轮中位数如下，bytes 为引擎逻辑读取指标，memory 为该查询日志的内存指标。

| 布局/查询 | read_rows | read_bytes | memory bytes | server duration ms |
|---|---:|---:|---:|---:|
| ch_string / Q02 | 30,055 | 50,742,185 | 36,496,694 | 84 |
| ch_map / Q02 | 30,147 | 55,203,852 | 52,899,207 | 50 |
| ch_native / Q02 | 30,138 | 1,761,719 | 5,633,307 | 8 |
| ch_string / Q05 | 21,863 | 48,041,150 | 21,329,450 | 68 |
| ch_map / Q05 | 21,955 | 51,397,319 | 32,024,423 | 33 |
| ch_native / Q05 | 21,946 | 1,878,198 | 5,451,883 | 6 |

所有查询的 read/selected rows、bytes、memory、duration、result rows/bytes 及轮间范围保存在 summary 中。查询服务端耗时和客户端完整读取延迟为不同指标，横向表统一采用后者。

### 4.3 写入期间的并发查询

| 布局 | 每轮全部并发样本，中位数 [min, max] | Q02 p50 ms | Q05 p50 ms |
|---|---:|---:|---:|
| og_jsonb | 728 [720, 744] | 24.68 | 14.19 |
| og_jsonb_hot | 984 [956, 984] | 12.72 | 19.62 |
| og_jsonb_gin | 952 [952, 964] | 30.64 | 3.57 |
| ch_string | 572 [568, 592] | 41.13 | 36.17 |
| ch_map | 652 [648, 656] | 42.39 | 31.28 |
| ch_native | 1,436 [1,424, 1,464] | 17.35 | 16.53 |

样本覆盖各布局写入期间实际到达的已提交水位，truth 随水位独立核对。并发样本数和水位分布由写入持续时间与查询耗时共同决定，不具备同水位逐样本配对关系。summary 保留四个查询的样本数、水位 min/max、p50/p95/p99 和请求等价速率，主要横向判断使用 §4.2。

## 5. 正确性、恢复与异常

| 门禁 | 正式 retry-3 结果 |
|---|---|
| manifest / result | 6/6 complete；18/18 complete；全部 artifact SHA 校验通过 |
| 查询 truth | 静态 9,000，加写入并发 15,996，共 24,996 个样本全部成功 |
| analysis | 每布局每轮 48,534 行，canonical hash 全部匹配；缺失、额外、重复 ID 为零 |
| 原文恢复 | 每布局每轮 48,534 行，原始 UTF-8 SHA-256 全部匹配 |
| ClickHouse 日志 | 12,512 个正式样本具备唯一 QueryFinish 的八项非负整数指标；静态预热另经 runner 核对 |
| 维护与清理 | 18/18 cleanup 完成；ClickHouse merge backlog 为零；独立检查无实验临时 schema/database |

native JSON 不能独立表达本输入所有 null、空对象和空数组状态。`ch_native` 在 analytics 表保存稀疏 `fidelity_values Map(String,String)`：一个 Attribute 递归包含上述值时保留该 Attribute 的完整 canonical value，Q04 和 analysis 恢复时合并。该结构参与本阶段写入、回查与空间成本。所有布局均以单独 raw 表承担逐行原文恢复，analysis 与 raw 分别校验。

| 排除目录 / run ID | 失败证据与处理 |
|---|---|
| `formal-20260907/`；`opengauss-r1-04ca47c0b0d8487db41e2d255efb3875` | openGauss 6.0.0 `jsonb_ops` GIN 写入递归空字符串触发 `jsonb_gin.cpp:519`。正式 `og_jsonb_gin` 固定 `jsonb_hash_ops` 并通过空字符串与恢复回归测试。 |
| `formal-20260907-retry-1/`；`clickhouse-r3-83bd945702b64f2291febfa4c6ee5b82` | server-total memory limit。逐查询全局日志 flush 引入 system log parts 与 merge 观测扰动，正式 runner 改为阶段末批量 query-log 采集。 |
| `formal-20260907-retry-2/`；整根六轮 | ClickHouse worker 的 HTTPConnection 在阶段屏障前未显式 connect，首个并发样本存在计入 TCP 建连的风险。静态测量经过预热，不受该首次建连路径影响；为统一 runner 身份，整根退出统计。正式 runner 在连接成功后进入屏障。 |

三个历史目录仅保留诊断，包含的成功部分全部排除统计。retry-3 以新 run ID 完整重跑六轮。ClickHouse 正式测量请求固定 `log_queries=1`，管理请求为 `log_queries=0`；两类均固定 `log_processors_profiles=0`、`memory_profiler_step=0`、`log_query_settings=0`。每个并发/静态阶段结束后一次 `SYSTEM FLUSH LOGS query_log`，批量核对全部 query ID。采集在延迟计时外，日志生成仍属于此次执行环境的观测成本。

## 6. 采用建议与复现

对本数据的 residual 查询，建议保留如下候选：openGauss 以 JSONB 作为基线，对稳定高频路径增加表达式索引，对已验证的包含查询选择 `jsonb_hash_ops` GIN；ClickHouse 以 String 作为写入、完整回查与空间基线，以 native JSON 作为高频路径过滤候选。Map 在本次路径过滤上位于 String 与 native 之间，同时保持较低的完整回查延迟，可按查询组合评估。最终选择需要为真实 workload 明确查询频率与写入预算。

本实验覆盖一个冻结输入、单机容器、固定查询参数与热查询流程。结果未覆盖饱和吞吐、冷缓存、长时间维护稳态、故障恢复、分布式副本、当前 exporter/benchmark 全链路，亦未测得全部引擎的同口径 CPU/内存指标。进入系统级比较前，需要冻结两仓 schema/event policy 并复用本阶段的输入、返回和恢复门禁。

复现先按[实验 README](../experiments/json-storage-stage2/README.md)生成审计和统一输入、依次完成三轮；正式结果校验与汇总命令为：

```bash
python3 experiments/json-storage-stage2/report/summarize_results.py \
  --input docs/temp/json-storage-stage2/formal-20260907-retry-3 \
  --output docs/temp/json-storage-stage2/formal-20260907-retry-3/summary
sha256sum docs/temp/json-storage-stage2/formal-20260907-retry-3/summary/summary.json
```

汇总器拒绝缺轮、重复轮次/ID、失败状态、产物篡改、身份变化、布局/样本/水位偏差及 ClickHouse 日志缺项。正式根中的 result 文件集合须与 manifest 声明完全相同，`summary/` 派生目录单独排除。每个 DDL 原文摘要通过校验后，将 schema/database 名规范化为稳定占位符，核对同布局三轮 DDL 一致；六布局规范化文本与 SHA 保存在 summary 的 `ddl_identities`。相同正式根连续生成两次的 JSON bytes 与 SHA-256 相同。运行产物位于 gitignored 目录；公开仓库保留脚本、设计与报告，复现需具备冻结输入。

## 参考资料

- [阶段二实验设计](json-storage-stage2-experiment-design.md)。
- [真实 Trace 审计完成 manifest](temp/json-storage-stage2/real-trace-audit-20260907/run-manifest.json)。
- [正式机器汇总与全部产物身份](temp/json-storage-stage2/formal-20260907-retry-3/summary/summary.json)。
- [ClickHouse JSON 类型说明](https://clickhouse.com/docs/reference/data-types/newjson)，null/缺失语义与路径存储。
- [openGauss v6.0.0 jsonb_gin.cpp](https://gitee.com/opengauss/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_gin.cpp)。
