# Trace 摄入 Demo：蓝区复现与黄区迁移指南

> 文档状态：历史固定版本复现指南。本文固定的 exporter `ebff8fd...` 使用
> traces/observations/scores 三表模型。当前 exporter `4cc3bf2...` 已采用
> events/ingest_batches/scores 单宽表模型，不能与本文命令和旧 benchmark ref
> 混用。当前 schema 与 JSON 设计见
> [JSON 存储设计调研](json-storage-design-survey.md)。

本文说明如何将固定的 Agent Trace 样本通过 OpenTelemetry Collector 写入标准 openGauss 6.0.0，并用发送确认清单与数据库记录做精确对账。蓝区步骤已于 2026-08-26 实测。黄区章节给出切换到 xstore 的参数、权限和验收项。

## 1. 目标、边界与成功定义

核心 Demo 验证一条 trace 摄入链路：

```text
Toucan fixture
  -> trace-synthesis 生成 flat JSONL
  -> replay 转换并发送 OTLP/HTTP JSON
  -> Collector: OTLP Receiver -> Batch Processor -> opengauss Exporter
  -> openGauss 协议、事务和 SQL
  -> openGauss 6.0.0 的 otel.traces / observations / scores
  -> confirm manifest 与数据库集合对账
```

核心成功条件为：10 个 trace、69 个 span 全部收到 OTLP HTTP 成功响应；数据库存在 10 行 `traces`、69 行 `observations`、0 行 `scores`；observation 类型为 27 个 `generation` 和 42 个 `span`；发送清单和数据库的 trace/span 集合差集为空。

本流程分为三层：

| 层次 | 作用 | 是否影响核心结论 |
|---|---|---|
| 隔离冒烟 | 用 `exporter_demo/scripts/loadgen` 单独验证 Collector、exporter、驱动和数据库 | 前置诊断 |
| 标准摄入 | 用 `trace-synthesis` 固定 fixture 执行 10 traces / 69 spans 摄入和对账 | 核心硬门槛 |
| 可选读侧验证 | 用 benchmark ref 的 query driver 检查 psycopg 连接和 v1 schema | 扩展证据 |

以下内容不属于本 Demo：`agent-trace-graph-test` 的图查询、Langfuse 服务、metrics/logs 信号、完整性能 benchmark、Collector 上游源码构建，以及用标准 openGauss 验证 xstore 的 dstore 列存能力。

## 2. 背景和术语

### 2.1 Trace、span 与 Agent Trace

trace 表示一次请求或任务的完整调用过程，span 表示其中一个有起止时间的操作。多个 span 通过 `trace_id`、`span_id` 和 `parent_span_id` 组成调用树。Agent Trace 常包含模型调用、工具调用和编排步骤；模型、token、输入输出等信息保存在 span attributes 或 events 中。

### 2.2 OpenTelemetry、OTLP 与 Collector

OpenTelemetry（OTel）提供可观测数据的公共数据模型、SDK、协议和采集组件。采用 OTel 后，数据生产端和存储端通过稳定协议解耦，数据库无需理解每一种 Agent SDK 的私有格式。

OTLP 是 OTel 数据的传输协议。本 Demo 使用 `OTLP/HTTP JSON`：replay 向 `http://127.0.0.1:4318/v1/traces` 发送 JSON。Collector 还监听 `4317` 的 OTLP/gRPC；gRPC 使用 protobuf 和长连接，适合 SDK 的高效持续发送，本 Demo 保留该兼容入口但不使用它。HTTP 与 gRPC 是 OTLP 的两种传输方式，传递的 trace 数据模型相同。

Collector 是可配置的遥测数据管道。当前管道包含：

| 组件 | 当前职责 |
|---|---|
| OTLP Receiver | 在 4317/4318 接收并解码 OTLP traces |
| Batch Processor | 将收到的 spans 组批，减少下游事务次数 |
| `opengauss` Exporter | 将 OTel 数据映射为三表记录，在一个事务中写入数据库 |

官方 Collector release 不包含团队的 `opengauss` exporter。本 Demo 使用 OpenTelemetry Collector Builder（OCB）按 `exporter_demo/builder-config.yaml` 组装定制二进制。运行链路不依赖本地 `opentelemetry-collector` 源码仓，也不需要把该仓从浅克隆改为完整克隆。

### 2.3 GenAI 语义约定

GenAI semantic conventions 是 OTel 对模型、Agent 和工具调用字段的命名约定，例如 `gen_ai.*`。统一命名使 exporter 能识别模型调用，并把对应 observation 映射为 `generation`，同时提取 model、token、input 和 output。它是字段语义约定，不是新的传输协议，也不要求单独的 GenAI 服务。

### 2.4 三表与 storage profile

`exporter_demo` 使用以下模型：

| 表 | 含义 | 当前写入行为 |
|---|---|---|
| `traces` | trace 级概要 | 按 `trace_id` 首次写入 |
| `observations` | span、generation 和 span event | 追加写入，核心明细表 |
| `scores` | 预留评分数据 | 自动建表，本流程不写入 |

`storage_profile: row` 为标准 openGauss 联调模式，三表均使用标准行存 DDL。`storage_profile: xstore` 为黄区模式，`observations` 使用 `storage_type=dstore, orientation=column`，`traces` 和 `scores` 保持行存。标准 openGauss 6.0.0 不支持 dstore，因此蓝区结果只证明协议、认证、行存 DDL、写入和查询兼容性。

## 3. 仓库、组件关系与固定版本

| 路径 | 固定 ref 或版本 | 在摄入 MVP 中的职责 |
|---|---|---|
| `trace-synthesis/` | main `f93dbb0a5d18f7c808b9211acdd1aa22eb9ab6cc` | 生成固定数据、转换为 OTLP、replay、记录确认清单 |
| `trace-synthesis/` benchmark ref | `e0199aca92b2c596fc7d6c6313805fcecef159fb` | 可选的数据库读侧和 schema 检查 |
| `exporter_demo/` | main `ebff8fd6c65910f284aac5342e6b9549c572a90d` | OCB 清单、自定义 exporter、loadgen、reconcile |
| `opentelemetry-collector/` | 本地参考快照 `b6918236de851afd9fbf560d43c44909a0fbf4e8` | 阅读 Receiver、Processor、Exporter 框架；运行时不读取该工作树 |
| `agent-trace-graph-test/` | `5f021357de5d17172c1d56f57e4cec2526c780b1` | 图查询实验；不进入摄入流程 |
| `openGauss-server/` | tag `v6.0.0`，`798b1578c7502888ecbfc349bb1abee8d49bc5ab` | 蓝区数据库版本证据；本流程不编译源码 |
| OCB / Collector 组件 | `v0.158.0` | 构建并运行定制 Collector |
| Collector stable modules | `v1.64.0` | pdata、component 和 exporter API |
| openGauss Go connector | `v1.0.8` | exporter 和 reconcile 的数据库连接 |
| openGauss 容器 | `enmotech/opengauss:6.0.0` | 蓝区行存数据库 |

实测工具版本为 Go 1.25.0、Python 3.11.6、Docker Engine 25.0.3。运行端口为：

| 端口 | 用途 |
|---|---|
| 15432 | 蓝区 openGauss 宿主端口 |
| 4317 | Collector OTLP/gRPC |
| 4318 | Collector OTLP/HTTP，本流程实际入口 |
| 8888 | Collector Prometheus metrics |

## 4. 蓝区前置检查

在同一个终端执行后续环境变量、数据库和 Collector 命令：

```bash
cd /home/omm/work/agent-trace

go version
python --version
docker version --format '{{.Server.Version}}'
docker info >/dev/null

for repo in trace-synthesis exporter_demo opentelemetry-collector \
  agent-trace-graph-test openGauss-server; do
  test -d "$repo/.git" || { echo "missing repo: $repo"; exit 1; }
done

git -C trace-synthesis rev-parse HEAD
git -C exporter_demo rev-parse HEAD
git -C openGauss-server rev-parse HEAD

for port in 15432 4317 4318 8888; do
  if ss -ltn "sport = :$port" | tail -n +2 | grep -q .; then
    echo "port in use: $port"
    exit 1
  fi
done

mkdir -p /home/omm/.local/state/agent-trace
mkdir -p /home/omm/.local/share/agent-trace/opengauss-v6-row
```

工作目录中的仓库已存在时只核对 ref。需要重新获取时使用仓库表中的 ref 固定版本。`opentelemetry-collector` 仅用于阅读，代理对大仓 clone 返回 HTTP 403 时可以跳过该仓；OCB 会按 Go module 版本取得构建依赖。需要源码证据时可使用固定 ref 的浅克隆或 GitHub source archive，无需下载完整历史。

## 5. 构建定制 Collector

从 `exporter_demo` 根目录运行 OCB。相对路径用于把当前仓的 exporter module 加入二进制，因此工作目录不能改变。

```bash
cd /home/omm/work/agent-trace/exporter_demo

go run go.opentelemetry.io/collector/cmd/builder@v0.158.0 \
  --config=builder-config.yaml

test -x dist/otelcol-opengauss
./dist/otelcol-opengauss --help >/dev/null
go version -m ./dist/otelcol-opengauss \
  | grep -E 'exporter_demo|collector/(component|pdata)|otlpreceiver'
```

定制的最小发行版没有 `--version` 选项。`--help` 验证可执行性，`go version -m` 验证编入的 module 版本。

黄区无法访问外部 Go module 时，在相同 OS/CPU 架构的联网环境构建二进制，同时传递以下工件及其 SHA-256：

```text
exporter_demo/dist/otelcol-opengauss
exporter_demo/otelcol.yaml
trace-synthesis 源码固定快照
exporter_demo/scripts/reconcile 源码固定快照
```

## 6. 启动蓝区 openGauss 6.0.0

### 6.1 凭据与状态隔离

本流程使用独立容器和数据目录：

```text
container: agent-trace-opengauss-v6
data:      /home/omm/.local/share/agent-trace/opengauss-v6-row
logs:      /home/omm/.local/state/agent-trace
database:  postgres
user:      otel
schema:    otel
```

生成 28 字符测试密码。`@` 满足镜像的特殊字符检查，percent encoding 后可安全放入 DSN；openGauss 密码上限为 32 字符。

```bash
export TRACE_DB_PASSWORD="Aa1@$(openssl rand -hex 12)"
export TRACE_DB_PASSWORD_ENC="${TRACE_DB_PASSWORD//@/%40}"

set -o pipefail
GS_PASSWORD="$TRACE_DB_PASSWORD" \
OTEL_PASSWORD="$TRACE_DB_PASSWORD" \
PGDATA_HOST=/home/omm/.local/share/agent-trace/opengauss-v6-row \
BENCH_PORT=15432 \
CONTAINER_NAME=agent-trace-opengauss-v6 \
  scripts/perf/start-opengauss.sh \
  | sed -E '/export OPENGAUSS_DSN=/d; / -W /d'

export TRACE_DSN_SCHEME='postgres://otel:'
export OPENGAUSS_DSN="${TRACE_DSN_SCHEME}${TRACE_DB_PASSWORD_ENC}@127.0.0.1:15432/postgres?sslmode=disable"
```

启动脚本调用 Docker 时会通过容器环境传递初始化密码。执行期间不要运行会打印完整进程参数的诊断命令。脚本末尾会打印 DSN 和带密码的验证命令，上述 `sed` 会过滤这两行。不要将脚本原始输出上传到日志系统。

验证容器和服务端版本：

```bash
docker ps --filter name=agent-trace-opengauss-v6 \
  --format '{{.Names}} {{.Image}} {{.Status}} {{.Ports}}'

docker exec -u omm agent-trace-opengauss-v6 bash -lc \
  "/usr/local/opengauss/bin/gsql -d postgres -tAc 'SELECT version()'"
```

预期版本文本包含 `openGauss 6.0.0`。`OPENGAUSS_DSN` 只保存在当前进程环境，不写入 YAML、日志或文档。

### 6.2 启动 Collector

```bash
cd /home/omm/work/agent-trace/exporter_demo

./dist/otelcol-opengauss --config scripts/perf/otelcol-row.yaml \
  > /home/omm/.local/state/agent-trace/collector-smoke.log 2>&1 &
export COLLECTOR_PID=$!

code=000
for attempt in $(seq 1 30); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' -d '{"resourceSpans":[]}' \
    http://127.0.0.1:4318/v1/traces || true)
  [ "$code" = 200 ] && break
  sleep 1
done
test "$code" = 200

grep 'opengauss exporter started' \
  /home/omm/.local/state/agent-trace/collector-smoke.log
grep '"storage_profile": "row"' \
  /home/omm/.local/state/agent-trace/collector-smoke.log
```

Collector 启动时连接数据库，并以 `otel` 用户的默认 schema 幂等创建三表。Collector 能返回 HTTP 200 说明 Receiver 已就绪；启动日志同时证明 exporter 已完成连接和建表。

## 7. 隔离冒烟

隔离冒烟使用 exporter 仓内的生成器，先验证 Collector 到数据库的边界：

```bash
cd /home/omm/work/agent-trace/exporter_demo

go run ./scripts/loadgen \
  -spans 100 \
  -manifest /home/omm/.local/state/agent-trace/smoke-manifest.csv

count=0
for attempt in $(seq 1 30); do
  count=$(docker exec -u omm agent-trace-opengauss-v6 bash -lc \
    "/usr/local/opengauss/bin/gsql -d postgres -tAc \
    \"SELECT count(*) FROM otel.observations WHERE position('#' in observation_id)=0\"" \
    | tr -d '[:space:]')
  [ "$count" = 100 ] && break
  sleep 1
done
test "$count" = 100

go run ./scripts/reconcile \
  -manifest /home/omm/.local/state/agent-trace/smoke-manifest.csv
```

本次实测结果为：100 个主 observations、85 个 event observations、15 个 traces；主 observations 和 traces 的 MISSING、UNEXPECTED 均为 0。loadgen 会给部分 span 添加 event，exporter 将每个 event 写成独立 observation，其 ID 包含 `#`。因此物理总行数为 185，不能用物理总行数判断 100 个发送 span 是否完成。

标准摄入需要从空表开始。停止 Collector，只删除本独立容器中的三张表，再重启：

```bash
kill "$COLLECTOR_PID"
wait "$COLLECTOR_PID" || true

drop_output=$(docker exec -u omm agent-trace-opengauss-v6 bash -lc \
  "/usr/local/opengauss/bin/gsql -d postgres -c \
  \"DROP TABLE IF EXISTS otel.observations;
    DROP TABLE IF EXISTS otel.traces;
    DROP TABLE IF EXISTS otel.scores;\"" 2>&1)
printf '%s\n' "$drop_output"
if printf '%s\n' "$drop_output" | grep -q '^ERROR: '; then
  exit 1
fi

./dist/otelcol-opengauss --config scripts/perf/otelcol-row.yaml \
  > /home/omm/.local/state/agent-trace/collector-ingestion.log 2>&1 &
export COLLECTOR_PID=$!

for attempt in $(seq 1 30); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' -d '{"resourceSpans":[]}' \
    http://127.0.0.1:4318/v1/traces || true)
  [ "$code" = 200 ] && break
  sleep 1
done
test "$code" = 200

docker exec -u omm agent-trace-opengauss-v6 bash -lc \
  "/usr/local/opengauss/bin/gsql -d postgres -tAc \
  'SELECT (SELECT count(*) FROM otel.traces),
          (SELECT count(*) FROM otel.observations),
          (SELECT count(*) FROM otel.scores)'"
```

预期最后一条查询输出 `0|0|0`。openGauss 6.0.0 的 `gsql` 在部分 SQL 错误时仍返回 0，涉及 DDL 的自动化必须同时扫描 `ERROR:`。

## 8. 标准 Trace 摄入与对账

### 8.1 生成固定数据

```bash
cd /home/omm/work/agent-trace/trace-synthesis

python -m demo.convert_toucan \
  --input tests/fixtures/toucan_sft_first_10.jsonl \
  --output /home/omm/.local/state/agent-trace/toucan-traces.jsonl

python -c 'import json,pathlib; p=pathlib.Path("/home/omm/.local/state/agent-trace/toucan-traces.jsonl"); rows=[json.loads(x) for x in p.read_text().splitlines()]; assert len(rows)==69; assert len({x["trace_id"] for x in rows})==10; print("traces=10 spans=69")'
```

### 8.2 OTLP/HTTP replay

```bash
python tools/replay_traces.py \
  --input /home/omm/.local/state/agent-trace/toucan-traces.jsonl \
  --sink otlp \
  --endpoint http://127.0.0.1:4318 \
  --rate 0 \
  --speed 0 \
  --confirm-manifest /home/omm/.local/state/agent-trace/toucan-confirmed.csv
```

预期输出：

```text
replayed 10 traces, 69 spans sent, 0 failed posts (0 spans dropped)
```

`--confirm-manifest` 只记录已取得 HTTP 成功响应的 span。它是本次对账的发送侧事实源。

### 8.3 等待可见并精确对账

```bash
count=0
for attempt in $(seq 1 30); do
  count=$(docker exec -u omm agent-trace-opengauss-v6 bash -lc \
    "/usr/local/opengauss/bin/gsql -d postgres -tAc \
    'SELECT count(*) FROM otel.observations'" \
    | tr -d '[:space:]')
  [ "$count" = 69 ] && break
  sleep 1
done
test "$count" = 69

cd /home/omm/work/agent-trace/exporter_demo
go run ./scripts/reconcile \
  -manifest /home/omm/.local/state/agent-trace/toucan-confirmed.csv \
  | tee /home/omm/.local/state/agent-trace/reconcile.txt
```

预期 reconcile 输出：

```text
manifest: 69 unique spans, 10 unique traces
expected: 69, actual: 69, event rows: 0
MISSING (sent but absent in db): 0
UNEXPECTED (in db but never sent): 0
expected: 10, actual: 10
MISSING traces: 0
UNEXPECTED traces: 0
RECONCILE OK: diff empty
```

### 8.4 查询存储结果

```bash
docker exec -u omm agent-trace-opengauss-v6 bash -lc \
  "/usr/local/opengauss/bin/gsql -d postgres -A -F ',' -c \
  \"SELECT count(*) AS traces FROM otel.traces;
    SELECT type,count(*) FROM otel.observations GROUP BY type ORDER BY type;
    SELECT count(*) AS scores FROM otel.scores;\"" \
  | tee /home/omm/.local/state/agent-trace/db-summary.txt
```

预期结果为 `traces=10`、`generation=27`、`span=42`、`scores=0`。

### 8.5 Collector 指标和日志

```bash
curl -fsS http://127.0.0.1:8888/metrics \
  | grep -E 'otelcol_exporter_(sent|send_failed)_spans|otelcol_exporter_queue_(size|capacity)|otelcol_receiver_(accepted|failed|refused)_spans'

if grep -Eni 'level=(error|fatal)|panic|postgres://otel:' \
  /home/omm/.local/state/agent-trace/collector-ingestion.log; then
  exit 1
fi
```

本次实测关键指标为：receiver accepted=69、failed=0、refused=0；exporter sent=69；queue size=0、capacity=1000。零失败时 `otelcol_exporter_send_failed_spans` 时间序列可能尚未生成，Receiver 的失败指标、日志和精确对账共同覆盖失败判断。

## 9. 验收表

| 验收项 | 蓝区实测 | 判据 |
|---|---:|---|
| replay | 10 traces / 69 spans / 0 failed | 退出码 0，全部 batch 为 HTTP 2xx |
| confirm manifest | 69 spans / 10 traces | ID 均唯一 |
| `otel.traces` | 10 | 等于 manifest 唯一 trace 数 |
| `otel.observations` | 69 | 等于 manifest 唯一 span 数 |
| observation 类型 | generation 27 / span 42 | 合计 69 |
| `otel.scores` | 0 | 表存在 |
| observations 差集 | 0 / 0 | MISSING / UNEXPECTED |
| traces 差集 | 0 / 0 | MISSING / UNEXPECTED |
| Collector | accepted 69 / sent 69 / queue 0 | failed、refused 和日志错误为 0 |

以上项目全部满足时，蓝区的 OTLP/HTTP → Collector → exporter → openGauss row 摄入链路跑通。

## 10. 可选 benchmark 读侧验证

该步骤检查标准 psycopg 客户端和版本化 schema，不参与核心摄入判定。

```bash
cd /home/omm/work/agent-trace
mkdir -p .worktrees
git -C trace-synthesis worktree add --detach \
  /home/omm/work/agent-trace/.worktrees/trace-synthesis-benchmark \
  e0199aca92b2c596fc7d6c6313805fcecef159fb

python -m venv /home/omm/.local/state/agent-trace/benchmark-venv
/home/omm/.local/state/agent-trace/benchmark-venv/bin/pip \
  install 'psycopg[binary]>=3.1'

set +e
/home/omm/.local/state/agent-trace/benchmark-venv/bin/python \
  /home/omm/work/agent-trace/.worktrees/trace-synthesis-benchmark/benchmark/query_driver.py \
  --schema-version v1 \
  --stage database \
  --results /home/omm/.local/state/agent-trace/benchmark-query.json \
  > /home/omm/.local/state/agent-trace/benchmark-query.txt 2>&1
query_driver_exit=$?
set -e
echo "query-driver exit: $query_driver_exit"
```

本次实测 `connect=ok`、schema check passed，共检查 31 项。标准摄入数据仍在表中，`scope_empty` 门禁报告 10/69/0 并按设计退出 1；这表示 benchmark 专用运行范围不为空，不表示查询连接失败。

## 11. 停止、重启与精确清理

停止进程并保留数据：

```bash
kill "$COLLECTOR_PID"
wait "$COLLECTOR_PID" || true
docker stop agent-trace-opengauss-v6
```

恢复服务：

```bash
docker start agent-trace-opengauss-v6
cd /home/omm/work/agent-trace/exporter_demo
./dist/otelcol-opengauss --config scripts/perf/otelcol-row.yaml \
  > /home/omm/.local/state/agent-trace/collector-ingestion.log 2>&1 &
export COLLECTOR_PID=$!
```

重新执行 Demo 时，使用第 7 节的三条全限定 `DROP TABLE`；Collector 重启后会重建空表。该命令不影响其他 schema。

完全移除独立容器并保留可恢复的数据目录：

```bash
docker rm -f agent-trace-opengauss-v6
archive=/home/omm/.local/share/agent-trace/opengauss-v6-row.saved.$(date +%Y%m%d%H%M%S)
mv /home/omm/.local/share/agent-trace/opengauss-v6-row "$archive"
echo "database files preserved at: $archive"
```

## 12. 实测故障与定位结论

| 现象 | 根因 | 处理与验证 |
|---|---|---|
| Collector 仓 clone 返回 HTTP 403 | 代理对该大仓的 Git HTTP 请求有限制 | 运行链路改从 OCB/Go modules 构建；Collector 源码只作阅读，可用浅克隆或 source archive |
| openGauss 镜像拒绝含 `_` 的初始密码 | 镜像的特殊字符规则未把该字符计入有效组合 | 使用 28 字符 `Aa1@` 加随机十六进制串 |
| `ALTER USER` 拒绝密码 | openGauss 密码长度上限为 32 | 将随机密码总长限制为 28 |
| 诊断输出出现密码或完整 DSN | Docker 启动参数和脚本末尾说明包含凭据 | 立即轮换已暴露密码；过滤 DSN 和 `-W` 行；不打印完整进程参数 |
| 定制 Collector 执行 `--version` 失败 | 最小 OCB 发行版未注册该选项 | 使用 `--help` 和 `go version -m` 验证 |
| 发送 100 spans 后 observations 总数为 185 | 85 个 span event 被映射为独立 observation | 完成条件使用不含 `#` 的主 observation ID；reconcile 单列 event 行 |
| SQL 报错但 shell 返回成功 | openGauss 6.0.0 的 `gsql` 部分错误仍返回 0 | DDL 自动化同时扫描输出中的 `ERROR:` |
| 用 `omm` 查询时找不到表 | exporter 以 `otel` 用户建表，表位于 `otel` schema | 使用 `otel.traces` 等全限定表名 |
| exporter 零失败但看不到 `send_failed` 指标 | counter 尚未产生零值时间序列 | 联合 receiver failed/refused、日志、sent 和数据库对账判断 |
| query driver 退出 1 | database stage 要求 benchmark 范围在 replay 前为空 | 检查报告中的 `connect`、`schema_check` 和 `scope_empty`，不以总退出码代替分项判断 |

故障定位顺序固定为：生成数据规模 → replay HTTP 与确认清单 → Receiver → exporter 启动和指标 → 数据库 schema/表/行数 → reconcile/query。每次只改变一个边界条件，并在原失败点重新验证。

## 13. 黄区 xstore 入口

黄区使用相同的数据生产、OTLP、Collector 和三表逻辑契约，将数据库连接替换为 GV openGauss 兼容入口，并把 `storage_profile` 设为 `xstore`。Docker 和标准 openGauss 不进入黄区流程。

定制 `otelcol-opengauss` 是从 `exporter_demo` 构建出的 Collector 可执行文件，不是额外仓库；已克隆的上游 Collector 源码只用于阅读。完整的仓库覆盖关系、Go 换源边界、GV 权限、构建、配置、摄入和 dstore 验收步骤见 [Trace 摄入 Demo：黄区 GV xstore 复现指南](trace-ingestion-demo-yellow-zone-guide.md)。

## 14. 参考入口

- [OTel、OTLP、Collector、GenAI 与 Langfuse 学习指南](otel-langfuse-study-guide.md)
- [`trace-synthesis` 说明](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/README.md)
- [`exporter_demo` 使用说明](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/USAGE.md)
- [`exporter_demo` 连接指南](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/CONNECTION.md)
- [`exporter_demo` 三表数据字典](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/SCHEMA.md)
- [`exporter_demo` xstore 验证说明](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/XSTORE_VERIFICATION.md)
- [OpenTelemetry Collector Architecture](https://opentelemetry.io/docs/collector/architecture/)
- [OTLP Specification](https://opentelemetry.io/docs/specs/otlp/)
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
