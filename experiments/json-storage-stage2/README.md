# Agent Trace JSON 存储阶段二实验

本目录比较 openGauss 6.0.0 与 ClickHouse 25.12.11.4 的 residual JSON 布局。数据路径固定为 `independent_loader`，不经过 Collector、exporter、benchmark 或产品服务。

阶段二已完成 6 个正式 run、18 个布局结果。结论见[阶段二横向报告](../../docs/json-storage-stage2-report-2026-09-08.md)。正式统计仅使用 `formal-20260907-retry-3/`；`formal-20260907/`、`formal-20260907-retry-1/`、`formal-20260907-retry-2/` 的全部产物只用于诊断。

## 依赖与输入

使用 Python 3.11、`psycopg`、两个已运行的数据库容器和冻结输入。正式输入目录必须由生成器发布完成 manifest，默认位置为 `docs/temp/json-storage-stage2/cross-engine-input-20260907/`。runner 校验 dataset、truth、block size、watermark、identity 和可比性契约。

```bash
python3 experiments/json-storage-stage2/audit/audit_real_traces.py \
  --input /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl \
  --upstream-manifest /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/manifest.json \
  --output docs/temp/json-storage-stage2/real-trace-audit-20260907 \
  --window-minutes 15

python3 experiments/json-storage-stage2/generator/generate_cross_engine.py \
  --input /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl \
  --audit docs/temp/json-storage-stage2/real-trace-audit-20260907/audit.json \
  --output docs/temp/json-storage-stage2/cross-engine-input-20260907 \
  --block-size 256
```

## 运行

每个 layout 写入相同 dataset、256 行 block 边界、watermark-first truth、查询参数、两个查询 worker、一次预热和 100 次正式样本。openGauss 默认端口为 15432，ClickHouse 默认端口为 18123。

```bash
/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python \
  experiments/json-storage-stage2/runner/run_cross_engine.py \
  --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
  --output docs/temp/json-storage-stage2/formal-20260907-retry-3/opengauss-round-1 \
  --engine opengauss --container-name agent-trace-opengauss-v6 \
  --namespace json_s2_og_retry3_r1 --round 1 \
  --layout-order og_jsonb,og_jsonb_hot,og_jsonb_gin \
  --measurements 100 --block-size 256 --query-workers 2 \
  --maintenance-timeout-seconds 120
```

三轮顺序固定如下：

| 引擎 | 第 1 轮 | 第 2 轮 | 第 3 轮 |
| --- | --- | --- | --- |
| openGauss | `og_jsonb,og_jsonb_hot,og_jsonb_gin` | `og_jsonb_hot,og_jsonb_gin,og_jsonb` | `og_jsonb_gin,og_jsonb,og_jsonb_hot` |
| ClickHouse | `ch_string,ch_map,ch_native` | `ch_map,ch_native,ch_string` | `ch_native,ch_string,ch_map` |

openGauss 6.0.0 的 `jsonb_ops` GIN 写入递归空字符串时触发 `jsonb_gin.cpp:519` 错误。`og_jsonb_gin` 使用 `jsonb_hash_ops`，Q05 保持 JSONB `@>` 包含查询；DDL、GIN 空间、自然计划与禁用顺扫计划均记录该布局。回归测试同时验证空字符串的 Q04、analysis 和 raw 恢复。源码依据见 [v6.0.0](https://gitee.com/opengauss/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_gin.cpp) 与 [v6.0.2](https://gitee.com/opengauss/openGauss-server/blob/v6.0.2/src/common/backend/utils/adt/jsonb_gin.cpp)。

`formal-20260907/` 保留首次失败轮次 `opengauss-r1-04ca47c0b0d8487db41e2d255efb3875` 的诊断；`formal-20260907-retry-1/` 保留 ClickHouse 第三轮 `clickhouse-r3-83bd945702b64f2291febfa4c6ee5b82` 的 server-total memory limit 诊断及同批次产物。失败整轮排除统计。openGauss 与 ClickHouse 各三轮的六个完整重试 run 全部写入新根 `docs/temp/json-storage-stage2/formal-20260907-retry-3/`，最终汇总仅使用该新根。每次重试选择新的输出目录与 run ID。

`formal-20260907-retry-2/` 的 ClickHouse worker 在屏障前未显式建立 TCP 连接，首个并发样本存在建连计时风险。静态样本经过预热，为统一 runner 身份仍将整根退出统计。retry-3 的 worker 在连接成功后进入屏障；runner 仅清理本次运行成功创建并取得所有权的对象。

## 产物与门禁

每个 layout 的 `result.json` 记录 DDL、查询与 runner identity、写入 block、并发查询、维护、静态查询、执行计划、analysis correctness、raw recovery、空间和 cleanup。产物不保存密码或原始 payload。

顶层 `run-manifest.json` 在全部 result 已写入且全部 layout cleanup 成功后发布 `status=complete`。它记录输入、环境、容器资源、仓版本、可比性契约、门禁和 artifact SHA-256。任一写入、truth、analysis、raw、维护或 cleanup 失败时，runner 清理已创建对象并在最后发布 `status=failed` 诊断 manifest；失败轮次不进入横向统计。

ClickHouse 的 analytics 与 raw 写入是独立请求。analytics 成功而 raw 失败时 runner 不推进共享 watermark，并停止查询 worker。`ch_native` 使用 `fidelity_values` 保留 null、空对象和空数组的 canonical 值；`ch_map` 与 `ch_native` 的 analysis 校验按 `key_map` 恢复 nested analysis，raw 表仅用于原文恢复。

ClickHouse 每条测量请求使用唯一 query ID，完整响应读取后结束 latency。并发阶段与静态阶段各执行一次 `SYSTEM FLUSH LOGS query_log`，批量采集该阶段全部 ID 的唯一 `QueryFinish`；静态预热 ID 同时核对。日志采集与轮询位于 latency 之外。最终样本保留完整 `query_log` 指标；缺失、重复、异常终态或无效指标使阶段失败，全部回填与 truth 门禁通过后生成摘要。

HTTP 参数固定为测量请求 `log_queries=1`，管理请求 `log_queries=0`；两类请求均设置 `log_processors_profiles=0`、`memory_profiler_step=0`、`log_query_settings=0`。管理请求包含 DDL、INSERT、日志 flush/查询、EXPLAIN、空间、merge 和 cleanup。业务查询参数使用 `param_*`。这些设置减少 system logs 的观测扰动，并随 runner 源码摘要冻结。

运行后检查完成状态与临时对象清理：

```bash
find docs/temp/json-storage-stage2/formal-20260907-retry-3 -name run-manifest.json -print0 \
  | xargs -0 -n1 jq -r '[.status,.engine,.round,.gates.correctness,.gates.raw_recovery,.gates.cleanup] | @tsv'
```

## 汇总

```bash
python3 experiments/json-storage-stage2/report/summarize_results.py \
  --input docs/temp/json-storage-stage2/formal-20260907-retry-3 \
  --output docs/temp/json-storage-stage2/formal-20260907-retry-3/summary
```

汇总器校验六轮身份、18 个 result 的字节数/SHA、布局与轮次、样本/truth、原文恢复、维护和清理。ClickHouse 每个正式样本必须具有八项 QueryFinish 指标。指标由原始 block/sample 重算，使用每轮 nearest-rank 与三轮 median/min/max；JSON 输出不包含当前时间，支持字节级重复验证。空间分别标注 openGauss 分配字节和 ClickHouse active part 压缩字节。
