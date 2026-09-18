# Agent Trace JSON 存储阶段三执行指南

本目录实现并执行 openGauss 6.0.0 与 ClickHouse 25.12.11.4 的四种长 payload 布局比较。实验设计见
[阶段三实验设计](../../docs/json-storage/json-storage-stage3-experiment-design-2026-09-09.md)，实施顺序见
[阶段三实施计划](../../docs/superpowers/plans/2026-09-14-json-storage-stage3.md)。

## 四种布局与证据边界

| 布局 | 写目标 | 列表入口 | 详情入口 | 写入联合水位 |
| --- | --- | --- | --- | --- |
| `same_table` | `events` | `events` | `events` | 单个写目标 |
| `separate` | `events_analytics`、`event_payloads` | `events_analytics` | `event_payloads` | 两张表 |
| `full_core` | `events_full`、`events_core` | `events_core` | `events_full` | 两张表 |
| `asset_ref` | `events_analytics`、`assets` | `events_analytics` | `assets` catalog 与本地对象 | 事件与 Asset 联合 |

两个引擎使用同一套逻辑查询：列表读取分析列，preview 读取 200 字节预览，详情读取完整 payload
并在应用可用计时内核对长度与 SHA-256，Trace 按 `trace_id` 读取全部 Span，batch 按冻结 block 恢复。

### `asset_ref` 的证据边界

`asset_ref` 的 payload 字节由 Python `LocalAssetStore`（`runner/assets.py`）保存在运行目录隔离的对象
目录内，数据库 `assets` 表保存 `asset_id`、`sha256`、`content_length`、`storage_path` 与状态。详情读取
依次执行 catalog 查询、对象文件读取、长度与摘要校验，三段都在应用可用计时之内。可达性证据取自
`events_analytics JOIN assets`；孤立 catalog 行与 fault injector 自报路径不构成证据。

该布局代表应用侧引用式存储的成本，包含本地文件读取与 catalog 查询的协议开销：一次逻辑查询复用
一个 catalog 连接，对象查找仍逐个执行；另外三种布局的 payload 留在数据库存储结构内。计时口径不同
的 `asset_ref` 样本不进入同一汇总，运行前确认所有 `asset_ref` shard 使用同一连接语义。

## 解释器、输入与产物

| 项目 | 值 |
| --- | --- |
| 解释器 | `/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python`（Python 3.11） |
| openGauss | `127.0.0.1:15432`，容器 `agent-trace-opengauss-v6` |
| ClickHouse | `127.0.0.1:18123`，容器 `agent-trace-clickhouse-25-12` |
| 冻结源 | `docs/temp/json-storage-stage2/cross-engine-input-20260907/`，含 `dataset.jsonl`、`run-manifest.json`、`truth-manifest.json` |
| 正式输入 | `generate-input` 发布到 `docs/temp/json-storage-stage3/runs/<campaign>/input/`，同目录保留生产 `run-manifest.json` |
| 正式规模 | 48,534 行、256 行/block、190 block、seed `20260907`、160 个主 payload 共 128,450,560 字节 |

所有运行产物写入 gitignored 的 `docs/temp/json-storage-stage3/`，凭据与原始 payload 不进入提交。下文
用 `STAGE3_PY` 表示解释器绝对路径，用 `runs/campaign-20260918` 表示 campaign 目录；实验参数在每条
命令中显式给出，命令之间不共享变量。

输出目录规则：

- 每条命令使用新的 attempt 目录与新的随机 namespace，生产命令在输出目录已存在时直接拒绝启动。
- `running` 与 `failed` 目录保留为诊断证据，从不续写；重试写入新的 `attempt-N` 目录。
- 汇总只接收显式列出的 target 目录（每个目录携带自己的 complete manifest），runner 不扫描“最新”目录。
- 两条条件集成测试由环境变量启用：`RUN_OPENGAUSS_INTEGRATION=1`、`RUN_CLICKHOUSE_INTEGRATION=1`。

## 单元测试与条件集成

```bash
export STAGE3_PY=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python

$STAGE3_PY -m unittest discover -s experiments/json-storage-stage3/tests -v

RUN_OPENGAUSS_INTEGRATION=1 $STAGE3_PY -m unittest experiments/json-storage-stage3/tests/test_opengauss.py -v

RUN_CLICKHOUSE_INTEGRATION=1 $STAGE3_PY -m unittest experiments/json-storage-stage3/tests/test_clickhouse.py -v
```

## smoke 穿刺

smoke 输入由 runner 生成，其格式身份与正式输入分离，`load_formal_input()` 会拒绝该目录。

```bash
$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --create-smoke-input docs/temp/json-storage-stage3/runs/campaign-20260918/smoke-input

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/smoke-input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/smoke \
  --engines opengauss,clickhouse \
  --layouts same_table,separate,full_core,asset_ref \
  --workloads main,equal_total_few_large,equal_total_many_medium,correctness_only \
  --measurements 2 --batch-measurements 1
```

八个 target 全部发布 `status=complete` 后进入正式输入。

## 正式输入与 candidate

```bash
$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py generate-input \
  --source docs/temp/json-storage-stage2/cross-engine-input-20260907 \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/input

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py candidate \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/candidate/clickhouse-asset-ref
```

candidate 固定执行 ClickHouse `asset_ref`、Latin square 第一轮、`main` workload、30/5 次测量，只作正式
矩阵门禁，不进入比较汇总。它只在这些条件全部成立时发布 complete：正式输入与 truth identity、完整响应
bytes 与 payload 校验、access plan/details 覆盖全部成功样本、全部 `QueryFinish`、写入与就绪联合水位、
自然稳定 part、`optimize_final=false`、storage 证据与 cleanup。

## 主矩阵

主矩阵按 engine 与 workload 拆成八条独立命令，每条覆盖四布局。`main`、`equal_total_few_large`、
`equal_total_many_medium` 各执行四轮 Latin square，`correctness_only` 执行一轮正确性门禁；全部命令使用
`--measurements 30 --batch-measurements 5`。

```bash
$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/main/opengauss \
  --engines opengauss --layouts same_table,separate,full_core,asset_ref \
  --workloads main --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/main/clickhouse \
  --engines clickhouse --layouts same_table,separate,full_core,asset_ref \
  --workloads main --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/equal_total_few_large/opengauss \
  --engines opengauss --layouts same_table,separate,full_core,asset_ref \
  --workloads equal_total_few_large --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/equal_total_few_large/clickhouse \
  --engines clickhouse --layouts same_table,separate,full_core,asset_ref \
  --workloads equal_total_few_large --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/equal_total_many_medium/opengauss \
  --engines opengauss --layouts same_table,separate,full_core,asset_ref \
  --workloads equal_total_many_medium --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/equal_total_many_medium/clickhouse \
  --engines clickhouse --layouts same_table,separate,full_core,asset_ref \
  --workloads equal_total_many_medium --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/correctness_only/opengauss \
  --engines opengauss --layouts same_table,separate,full_core,asset_ref \
  --workloads correctness_only --measurements 30 --batch-measurements 5

$STAGE3_PY experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/matrix/correctness_only/clickhouse \
  --engines clickhouse --layouts same_table,separate,full_core,asset_ref \
  --workloads correctness_only --measurements 30 --batch-measurements 5
```

每条命令在 `<output>/<engine>/<layout>/run-manifest.json` 发布 target manifest，样本写入同目录的
`samples.jsonl`。`main`、`equal_total_few_large`、`equal_total_many_medium` 的样本进入性能统计，
`correctness_only` 只进入正确性门禁。

## ClickHouse part 状态控制

part 控制固定使用 ClickHouse 与 `main` 查询 catalog，每个 layout 一次调用，`samples_per_query=30`，
不暴露其他参数。

```bash
$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py part-states \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/part-states/same_table \
  --layout same_table

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py part-states \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/part-states/separate \
  --layout separate

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py part-states \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/part-states/full_core \
  --layout full_core

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py part-states \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/part-states/asset_ref \
  --layout asset_ref
```

一次调用按固定顺序执行四个状态：`fragmented`（停止 merge 后载入，要求至少两个 part）、`merging`
（恢复 merge 并在 merge 活动期间采样）、`stable`（连续三次轮询无活动 merge）、`single_part`（仅此状态执行
`OPTIMIZE FINAL`）。该序列是有序控制结果，没有 Latin round，禁止按位置或缓存状态与 main round 配对，
也不计算 main 与 part 的差值。

## 干扰控制

干扰控制固定使用 ClickHouse 与 `main` 查询 catalog，每个 layout 一次调用，五个 phase 依次为 `quiet`、
`detail_2m`、`trace_long`、`batch_loop`、`continuous_ingest`。每个 phase 预热 30 秒、测量 300 秒，单
layout 理论下限为 `5 × (30 + 300) = 1,650` 秒；四个 layout 合计下限 110 分钟，另有五次载入、drain、
`QueryFinish` 采集与 cleanup 时间。

```bash
$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py interference \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/interference/same_table \
  --layout same_table

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py interference \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/interference/separate \
  --layout separate

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py interference \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/interference/full_core \
  --layout full_core

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py interference \
  --input docs/temp/json-storage-stage3/runs/campaign-20260918/input \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/interference/asset_ref \
  --layout asset_ref
```

`continuous_ingest` 按 ingest 顺序循环冻结输入中 45 个窗口外完整 block，每个 block 的 digest、选择规则
与 `cyclic_replay=true` 写入运行级 manifest；找不到该集合或前台 truth 受影响时调用立即失败。p99 需要
至少 1,000 个成功样本，样本不足时保留原始样本并把统计量标为不可发布。

## Asset 故障控制

故障控制使用独立 fixture 目录，每个 engine 一次调用，覆盖六个固定 case：`missing`、`corrupt`、
`metadata_mismatch`、`upload_then_db_failure`、`publish_failure`、`delete_failure`。

```bash
$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py asset-failures \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/asset-failures/opengauss \
  --engine opengauss

$STAGE3_PY experiments/json-storage-stage3/runner/run_stage3.py asset-failures \
  --output docs/temp/json-storage-stage3/runs/campaign-20260918/asset-failures/clickhouse \
  --engine clickhouse
```

故障结果是证据控制：每个 case 记录注入点、真实状态转换、resolver 分类、orphan 检测、恢复动作与
cleanup，不参与时延排名。

## 汇总

`report/summarize.py` 当前只实现主矩阵汇总的库接口：`validate_run()` 接受
`agent-trace-json-storage-stage3-layout-matrix` 格式，`summarize(runs)` 对显式列出的 target 目录做
round-first、四轮中位数统计，`write_summary_atomic(output, summary)` 原子发布 `summary.json`。

该实现与本指南的主矩阵命令之间存在确切阻塞，正式汇总在 Task 7B 完成前无法执行：

- `_round_records()` 要求每个 target manifest 的 `workloads` 覆盖全部四个 workload，其中三个性能
  workload 各四轮、`correctness_only` 一轮；`validate_run()` 另外要求 target 级 `rounds_complete == 4`。
- 本指南按 engine 与 workload 分片执行，每个 shard 的 manifest 只含单个 workload，target 级轮次也只
  覆盖该 workload，因此当前 summarizer 会拒绝分片结果。
- 模块没有 CLI 入口（没有 `argparse` 或 `main()`），只能由 Python 代码显式传入 target 目录列表。
- 模块不校验 part-state、interference、Asset failure 三类控制 manifest，也不做跨运行 identity 检查。

Task 7B 补齐 summary CLI、三类控制 schema 与跨 shard 组装规则后，本节再给出最终 `summary.json` 与
阶段三报告的命令。Task 7B 完成前不做汇总，也不从单个 shard 目录生成比较结论。

## 生产门禁

正式运行在发布 complete 前验证以下证据，缺失任一项即把运行级 manifest 写为 `failed`：

- truth 与输入 identity：冻结源 manifest digest、`truth.json`、48,534 行与 190 block 水位、seed
  `20260907`、workload 的 payload 数量与原始字节。
- 完整 payload bytes 与 SHA-256：详情样本必须在应用可用计时内收到完整 payload 并核对长度与摘要，
  服务端摘要不能代替。
- access path/scan：每个成功样本的 plan、`query_details` 与实际扫描行列数必须与 `QueryFinish` 一致。
- `QueryFinish`：ClickHouse 每条测量请求必须有唯一且终态正常的 `QueryFinish` 记录。
- part 与 merge：主矩阵使用自然稳定 part，`optimize_final=false`；part 控制额外要求目标谓词与 merge
  恢复证据。
- 水位：写入完成与查询就绪分开计时，两张写表或 Asset 联合水位的每个目标都必须达到最终位置。
- 响应字节：`response_bytes` 分别记录数据库协议、Asset resolver 与校验后的 payload 字节，成功样本的
  响应字节总量必须大于零。
- cleanup：运行级 namespace、表、Assets 对象目录全部删除，且清理由独立读取的 manifest 证据确认。
- 无残留 namespace：cleanup 后目标 namespace 不再存在；中断或 `server_side_completion=unknown` 时保留
  namespace，先人工确认服务端请求结束，再用新 attempt 继续。

## 运行后审计

```bash
find docs/temp/json-storage-stage3/runs/campaign-20260918 -name run-manifest.json -print0 \
  | xargs -0 -n1 jq -r '[.format,.status,(.operation // "matrix")] | @tsv'

curl -sG 'http://127.0.0.1:18123/' \
  --data-urlencode "query=SELECT name FROM system.databases WHERE name LIKE 'jsons3%' FORMAT TSV"

$STAGE3_PY - <<'PY'
import subprocess
import psycopg

env = subprocess.run(
    ["docker", "inspect", "agent-trace-opengauss-v6", "--format",
     "{{range .Config.Env}}{{println .}}{{end}}"],
    capture_output=True, text=True, check=True,
).stdout
password = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("GS_PASSWORD="))
with psycopg.connect(host="127.0.0.1", port=15432, dbname="postgres", user="gaussdb", password=password) as conn:
    print(conn.execute("select nspname from pg_namespace where nspname like 'jsons3\\_%'").fetchall())
PY
```

两条残留检查都返回空结果时，当前 campaign 没有遗留 namespace。
