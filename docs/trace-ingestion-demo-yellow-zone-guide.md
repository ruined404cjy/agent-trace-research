# Trace 摄入 Demo：黄区 GV xstore 复现指南

> 文档状态：历史固定版本复现指南。本文固定的 exporter `ebff8fd...` 与
> trace-synthesis `f93dbb0...` 用于当时的三表摄入验证。当前 exporter
> `4cc3bf2...` 已采用 events 单宽表，benchmark v4 database catalog 仍与其存在
> 28/26 列差异。执行前应按
> [JSON 存储设计调研](json-storage-design-survey.md)和
> [穿刺实验设计](json-storage-spike-experiment-design.md)重新固定匹配版本。

本文面向已经能够启动 GV xstore 实例、已经配置 Go 环境与 Go module 换源，并已克隆 `trace-synthesis`、`exporter_demo`、`opentelemetry-collector` 的黄区操作者和 Agent。目标是在实际 xstore 上完成固定 Agent Trace 数据的 OTLP/HTTP 摄入、三表写入、dstore 物理形态确认和集合级精确对账。

蓝区已使用标准 openGauss 6.0.0 行存验证共享链路：固定 Toucan fixture 生成 10 traces / 69 spans，Collector 接收和 exporter 写出均为 69，数据库为 10 traces / 69 observations / 0 scores，reconcile 差集为空。黄区需要验证蓝区无法覆盖的 xstore DDL、认证、事务和 dstore 存储行为。

## 1. 黄区目标和边界

黄区运行拓扑如下：

```text
trace-synthesis 内置 Toucan fixture
  -> flat JSONL（10 traces / 69 spans）
  -> replay_traces.py
  -> OTLP/HTTP JSON :4318/v1/traces
  -> 定制 otelcol-opengauss
       OTLP Receiver -> Batch Processor -> opengauss Exporter
  -> GV openGauss 兼容协议与 SQL
  -> traces（行存）+ observations（dstore 列存）+ scores（行存）
  -> confirm manifest 与数据库集合对账
```

核心验收包含：

1. `exporter_demo` 的 xstore E2E 门禁通过。
2. 定制 Collector 使用 `storage_profile: xstore` 启动并自动建表。
3. 固定数据的 69 spans 全部获得 OTLP HTTP 2xx。
4. 数据库存在 10 traces、69 observations、0 scores。
5. observations 类型为 27 generation、42 span。
6. traces 和 observations 的 MISSING、UNEXPECTED 均为 0。
7. 数据库元数据证明 observations 实际使用 dstore 列存。

Docker、标准 openGauss、Langfuse、图查询和完整性能 benchmark 均不属于黄区核心流程。

## 2. “定制 otelcol-opengauss”的含义

OpenTelemetry Collector 是按组件装配的 Go 程序。官方 Collector release 不包含团队开发的 `opengauss` exporter，也不能在运行时直接加载该 Go module。`otelcol-opengauss` 是使用 OpenTelemetry Collector Builder（OCB）编译出的独立可执行文件，内含：

| 组件 | 来源 | 职责 |
|---|---|---|
| OTLP Receiver | OTel 官方 module | 接收 OTLP/gRPC 和 OTLP/HTTP traces |
| Batch Processor | OTel 官方 module | 将 spans 组批 |
| Debug Exporter | OTel 官方 module | 可选调试输出 |
| `opengauss` Exporter | `exporter_demo` 本地 module | 映射三表、建表建索引、事务写 GV |

构建关系为：

```text
exporter_demo/builder-config.yaml
  + OTel module v0.158.0 / v1.64.0
  + exporter_demo 当前源码（path: ./）
  -> exporter_demo/dist/otelcol-opengauss
```

因此，定制 Collector 是已有 `exporter_demo` 仓库的构建产物，不是额外仓库。已克隆的 `opentelemetry-collector` 源码仓只用于阅读框架实现；当前 OCB 命令不会读取该工作树，也不会因该仓已经完整克隆而停止下载 Go modules。

## 3. 已有仓库覆盖范围

| 能力或工件 | 由哪个仓库覆盖 | 黄区还需执行的动作 |
|---|---|---|
| 固定 Toucan fixture | `trace-synthesis` | 核对 ref，直接使用仓内文件 |
| fixture 转 flat JSONL | `trace-synthesis` | 运行 Python module |
| flat JSONL 转 OTLP/HTTP 并 replay | `trace-synthesis` | 设置 Collector endpoint |
| HTTP 2xx 确认清单 | `trace-synthesis` | 指定 `--confirm-manifest` |
| `opengauss` exporter 源码 | `exporter_demo` | 核对 ref，编译进入 Collector |
| OCB 构建清单 | `exporter_demo/builder-config.yaml` | 执行 OCB 构建 |
| xstore Collector 配置基础 | `exporter_demo/otelcol.yaml` | 设置 DSN；按需生成带 metrics 的运行配置 |
| xstore 兼容性 E2E | `exporter_demo/scripts/run-e2e.sh` | 对实际 GV 执行 |
| Collector→GV 冒烟数据 | `exporter_demo/scripts/loadgen` | 发送 100 spans |
| manifest→数据库对账 | `exporter_demo/scripts/reconcile` | 使用相同 DSN 和 search path |
| 离线开发包 | `exporter_demo/scripts/build-offline-bundle.sh` | Go 换源不完整时在联网区出包 |
| Collector 框架源码 | `opentelemetry-collector` | 仅阅读；不参与当前构建和运行 |
| GV 实例、账号、权限、网络 | 不在源码仓 | 由黄区环境提供并验证 |
| `otelcol-opengauss` 二进制 | 构建产物 | 在黄区构建或从同架构环境传入 |

`agent-trace-graph-test`、`openGauss-server` 源码和官方 Collector 二进制均不是本流程依赖。

## 4. 固定版本

| 项目 | 固定版本或 ref |
|---|---|
| `trace-synthesis` main | `f93dbb0a5d18f7c808b9211acdd1aa22eb9ab6cc` |
| `exporter_demo` main | `ebff8fd6c65910f284aac5342e6b9549c572a90d` |
| OCB、OTLP Receiver、Batch Processor | `v0.158.0` |
| Collector stable modules | `v1.64.0` |
| openGauss Go connector | `v1.0.8` |
| Go | `1.25.0`；仓库 `go.mod` 的构建版本 |
| Python | `3.8` 及以上；蓝区实测为 3.11.6 |

`opentelemetry-collector` 本地工作树 ref 不决定运行版本。运行版本由 `builder-config.yaml` 和生成的 `dist/go.mod` 决定。

## 5. 黄区仍需环境提供的条件

### 5.1 GV 数据库条件

向 GV 环境维护方确认：

1. GV 构建版本和 openGauss 兼容协议版本。
2. Collector 节点可以访问 GV 主机和端口，pg_hba 已放行。
3. 账号可以连接 `postgres` 数据库，认证方式为 connector 支持的 sha256、SM3 或 md5。
4. 账号的默认 search path 指向专用且可写的 Demo schema。
5. 账号可以执行 `CREATE TABLE IF NOT EXISTS`、`CREATE INDEX IF NOT EXISTS`、`INSERT`、`SELECT`、`DROP TABLE`、`VACUUM`、`ANALYZE` 和事务提交。
6. observations 支持 `WITH (storage_type=dstore, orientation=column)` 和 trace_id、ts、session_id 上的 psort 索引。
7. traces 行存支持主键及 `ON DUPLICATE KEY UPDATE NOTHING`。
8. JSON、VARCHAR、TIMESTAMP(3)、BIGINT、DOUBLE PRECISION 与当前三表 DDL 兼容。
9. 一个事务可以同时写行存 traces 和 dstore observations。
10. 当前 CancelRequest 风险是否已修复；未确认修复时保留 `timeout: 7200s`。

建议使用专用数据库用户和专用 schema。reconcile 按整表做集合比较，不支持 run ID 过滤；共享历史表会产生 UNEXPECTED。exporter 和 reconcile 都使用未限定 schema 的表名，因此两者必须使用相同用户和默认 search path。

### 5.2 本机命令和网络

核心流程需要：

```text
bash、git、Go 1.25.0、Python >= 3.8、curl、基础 CA 证书
```

建议安装与 GV 匹配的 `gsql`，用于清理 Demo 表、查询计数和确认 dstore 物理形态。缺少 `gsql` 时，由数据库维护方执行本文的 SQL 并保留输出。

若 replay、Collector 和 GV 经过代理或位于不同主机，需要：

- 将 `127.0.0.1:4318` 改为适当监听地址，并配置访问控制。
- 放行 replay→Collector 的 4318 和 Collector→GV 的数据库端口。
- 把 `127.0.0.1`、`localhost` 和 GV 主机加入 `NO_PROXY`。

## 6. 工作目录和凭据

将三仓放在同一工作区；路径可以不同，后续命令通过变量引用：

```bash
export TRACE_WORKSPACE=/path/to/agent-trace
export TRACE_STATE="${XDG_STATE_HOME:-$HOME/.local/state}/agent-trace-yellow"
mkdir -p "$TRACE_STATE"

cd "$TRACE_WORKSPACE"
test -d trace-synthesis/.git
test -d exporter_demo/.git
test -d opentelemetry-collector/.git
```

核对核心仓 ref：

```bash
test "$(git -C trace-synthesis rev-parse HEAD)" = \
  f93dbb0a5d18f7c808b9211acdd1aa22eb9ab6cc
test "$(git -C exporter_demo rev-parse HEAD)" = \
  ebff8fd6c65910f284aac5342e6b9549c572a90d
test -z "$(git -C trace-synthesis status --porcelain)"
test -z "$(git -C exporter_demo status --porcelain)"
```

凭据只进入当前进程环境。以下示例中的非密码字段应替换为黄区实际值：

```bash
export GV_DB_USER=otel_demo
export GV_DB_HOST=gv-xstore.example.internal
export GV_DB_PORT=5432
export GV_DB_NAME=postgres

read -rsp 'GV database password: ' GV_DB_PASSWORD
echo
export GV_DB_PASSWORD
export GV_DB_PASSWORD_ENC="$(python -c \
  'import os,urllib.parse; print(urllib.parse.quote(os.environ["GV_DB_PASSWORD"], safe=""))')"

export TRACE_DSN_SCHEME='postgres://'
export OPENGAUSS_DSN="${TRACE_DSN_SCHEME}${GV_DB_USER}:${GV_DB_PASSWORD_ENC}@${GV_DB_HOST}:${GV_DB_PORT}/${GV_DB_NAME}?sslmode=disable"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,${GV_DB_HOST}"
```

不要启用 `set -x`，不要打印 `OPENGAUSS_DSN`，不要把 DSN 写入 YAML、日志或 shell history。密码含 URI 保留字符时必须 percent encode。

检查基础环境和数据库端口：

```bash
go version
go env GOPROXY GOSUMDB GOMODCACHE GOCACHE
python -c 'import sys; assert sys.version_info >= (3,8); print(sys.version)'
timeout 3 bash -c ': >/dev/tcp/"$GV_DB_HOST"/"$GV_DB_PORT"'
```

最后一条只证明 TCP 可达，数据库认证和 SQL 兼容性由后续 E2E 验证。

## 7. Go 换源和自动下载边界

黄区已经配置 Go 环境和换源后，仍需确认换源覆盖完整依赖图：

```bash
cd "$TRACE_WORKSPACE/exporter_demo"
go env GOPROXY GOSUMDB
go mod download
go test ./...
```

以下命令会主动或自动访问 Go module 源：

| 命令 | 下载或加载内容 |
|---|---|
| `go mod download` | `exporter_demo/go.mod` 的 connector、Collector API 和测试依赖 |
| `go test ./...` | 编译缓存和测试依赖 |
| `go run .../builder@v0.158.0` | OCB 本身，以及生成 Collector 所需的 Receiver、Processor、Exporter 和服务 modules |
| `go run ./scripts/loadgen` | exporter 根 module 及编译缓存；已有缓存时不再下载 |
| `go run ./scripts/reconcile` | connector 和 exporter 根 module 的相关依赖 |

内部 Go proxy 至少需要覆盖：

```text
go.opentelemetry.io/collector/cmd/builder v0.158.0
go.opentelemetry.io/collector/receiver/otlpreceiver v0.158.0
go.opentelemetry.io/collector/processor/batchprocessor v0.158.0
go.opentelemetry.io/collector/exporter/debugexporter v0.158.0
go.opentelemetry.io/collector stable modules v1.64.0
gitcode.com/opengauss/openGauss-connector-go-pq v1.0.8
```

Go proxy 可达但公共 sumdb 不可达时，按黄区安全策略配置内部 sumdb。长期关闭 checksum 校验会失去依赖完整性保护。

核心 Python 转换和 replay 只使用标准库与仓内 module，不执行 `pip install`，也不下载 Toucan/Hugging Face 全量数据。`pyarrow`、`huggingface-hub`、OpenTelemetry Python SDK 和 `psycopg` 均不是核心依赖。

换源缺包或环境完全离线时，在可联网的同架构环境执行：

```bash
cd exporter_demo
go run go.opentelemetry.io/collector/cmd/builder@v0.158.0 \
  --config=builder-config.yaml
scripts/build-offline-bundle.sh
```

生成的 bundle 包含固定 HEAD 源码、根 module 与 dist module 的双 vendor，以及 Linux amd64 Go 工具链。只需运行现成二进制时，可以传递 `otelcol-opengauss`、预编译的 loadgen/reconcile、配置和 SHA-256 清单，省去黄区编译依赖。

## 8. 先验证 exporter 与 GV xstore 契约

`scripts/run-e2e.sh` 使用随机后缀表名，结束时删除自身表，不接触默认 `traces`、`observations`、`scores`。它直接验证 connector 认证、xstore DDL、索引、upsert、多值 INSERT、事务和 OTLP 映射。

```bash
cd "$TRACE_WORKSPACE/exporter_demo"

OTEL_E2E_DSN="$OPENGAUSS_DSN" \
OTEL_E2E_PROFILE=xstore \
  scripts/run-e2e.sh | tee "$TRACE_STATE/xstore-e2e.log"
```

预期所有 E2E tests 通过，日志和数据库端均无 `ERROR:`。该门禁失败时先解决 GV 适配问题，不进入 Collector 摄入。

## 9. 构建定制 Collector

必须从 `exporter_demo` 根目录执行，`builder-config.yaml` 中的 `path: ./` 才会指向当前 exporter module：

```bash
cd "$TRACE_WORKSPACE/exporter_demo"

go run go.opentelemetry.io/collector/cmd/builder@v0.158.0 \
  --config=builder-config.yaml

test -x dist/otelcol-opengauss
dist/otelcol-opengauss --help >/dev/null
dist/otelcol-opengauss components | grep -E 'opengauss|otlp|batch'
go version -m dist/otelcol-opengauss \
  | grep -E 'exporter_demo|otlpreceiver|batchprocessor|openGauss-connector'
sha256sum dist/otelcol-opengauss | tee "$TRACE_STATE/collector.sha256"
```

最小 OCB 二进制不提供 `--version`；使用 `components` 和 `go version -m` 验证组件及 module 版本。

## 10. 生成黄区运行配置

仓库根 `otelcol.yaml` 已使用 xstore，但不含蓝区压测配置中的 Prometheus metrics reader。为保留队列和收发指标，在仓外从现有 row 配置生成运行配置，只改变 storage profile：

```bash
cd "$TRACE_WORKSPACE/exporter_demo"
cp scripts/perf/otelcol-row.yaml "$TRACE_STATE/otelcol-xstore.yaml"
sed -i -E \
  's/^([[:space:]]*)storage_profile: row/\1storage_profile: xstore/' \
  "$TRACE_STATE/otelcol-xstore.yaml"

test "$(grep -c 'storage_profile: xstore' "$TRACE_STATE/otelcol-xstore.yaml")" = 1
if grep -Eq 'storage_profile: row' "$TRACE_STATE/otelcol-xstore.yaml"; then
  exit 1
fi

dist/otelcol-opengauss validate \
  --config "$TRACE_STATE/otelcol-xstore.yaml"
```

该文件保留以下关键参数：

```yaml
opengauss:
  dsn: ${env:OPENGAUSS_DSN}
  timeout: 7200s
  storage_profile: xstore
```

Receiver 默认只监听 `127.0.0.1:4317` 和 `127.0.0.1:4318`。replay 与 Collector 同机时保持该配置。跨主机接入时在仓外运行配置中调整监听地址，同时配置防火墙和鉴权。

## 11. 启动 Collector

首次标准运行前，专用 Demo schema 中不能存在其他跑次的数据。已有默认三表时，先由数据库维护方确认其归属，再使用第 13 节的精确清理命令。

```bash
cd "$TRACE_WORKSPACE/exporter_demo"

dist/otelcol-opengauss \
  --config "$TRACE_STATE/otelcol-xstore.yaml" \
  > "$TRACE_STATE/collector-smoke.log" 2>&1 &
export COLLECTOR_PID=$!

code=000
for attempt in $(seq 1 30); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d '{"resourceSpans":[]}' \
    http://127.0.0.1:4318/v1/traces || true)
  [ "$code" = 200 ] && break
  sleep 1
done
test "$code" = 200

grep 'opengauss exporter started' "$TRACE_STATE/collector-smoke.log"
grep '"storage_profile": "xstore"' "$TRACE_STATE/collector-smoke.log"
if grep -Eni 'level=(error|fatal)|panic|postgres://' \
  "$TRACE_STATE/collector-smoke.log"; then
  exit 1
fi
```

Collector 启动会执行数据库 Ping、三表 DDL 和索引 DDL。HTTP 200 只能证明 Receiver 就绪；必须同时检查 exporter 启动日志和数据库物理表。

## 12. Collector→GV 隔离冒烟

```bash
cd "$TRACE_WORKSPACE/exporter_demo"

go run ./scripts/loadgen \
  -spans 100 \
  -manifest "$TRACE_STATE/smoke-manifest.csv"

reconciled=0
for attempt in $(seq 1 60); do
  if go run ./scripts/reconcile \
      -manifest "$TRACE_STATE/smoke-manifest.csv" \
      > "$TRACE_STATE/smoke-reconcile.txt" 2>&1; then
    reconciled=1
    break
  fi
  sleep 1
done
cat "$TRACE_STATE/smoke-reconcile.txt"
test "$reconciled" = 1
grep 'RECONCILE OK: diff empty' "$TRACE_STATE/smoke-reconcile.txt"
```

固定 loadgen 实测口径为 100 个主 observations、85 个 event observations、15 个 traces。event observation ID 含 `#`，reconcile 会单独计数，不把它们误判为额外 span。

冒烟完成后必须清空默认三表，再执行标准 fixture。停止 Collector：

```bash
kill "$COLLECTOR_PID"
wait "$COLLECTOR_PID" || true
```

使用同一 Demo 用户执行精确清理。`PGPASSWORD` 仅进入子进程环境，不出现在参数中：

```bash
drop_output=$(PGPASSWORD="$GV_DB_PASSWORD" gsql \
  -h "$GV_DB_HOST" -p "$GV_DB_PORT" \
  -U "$GV_DB_USER" -d "$GV_DB_NAME" \
  -c 'DROP TABLE IF EXISTS observations;
      DROP TABLE IF EXISTS traces;
      DROP TABLE IF EXISTS scores;' 2>&1)
printf '%s\n' "$drop_output"
if printf '%s\n' "$drop_output" | grep -q '^ERROR: '; then
  exit 1
fi
```

禁止使用 `TRUNCATE`；当前 xstore 运维纪律要求 DROP 后由 Collector 重新 CREATE。没有 `gsql` 时由数据库维护方执行同样的三条目标 SQL，并检查服务端错误。

重新启动 Collector，日志改为标准摄入日志：

```bash
dist/otelcol-opengauss \
  --config "$TRACE_STATE/otelcol-xstore.yaml" \
  > "$TRACE_STATE/collector-ingestion.log" 2>&1 &
export COLLECTOR_PID=$!

for attempt in $(seq 1 30); do
  code=$(curl -sS -o /dev/null -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d '{"resourceSpans":[]}' \
    http://127.0.0.1:4318/v1/traces || true)
  [ "$code" = 200 ] && break
  sleep 1
done
test "$code" = 200
```

## 13. 标准 Trace 摄入

### 13.1 生成固定 10 traces / 69 spans

```bash
cd "$TRACE_WORKSPACE/trace-synthesis"

python -m demo.convert_toucan \
  --input tests/fixtures/toucan_sft_first_10.jsonl \
  --output "$TRACE_STATE/toucan-traces.jsonl"

TRACE_STATE="$TRACE_STATE" python -c \
  'import json,os,pathlib; p=pathlib.Path(os.environ["TRACE_STATE"])/"toucan-traces.jsonl"; rows=[json.loads(x) for x in p.read_text().splitlines()]; assert len(rows)==69; assert len({x["trace_id"] for x in rows})==10; print("traces=10 spans=69")'
```

该 fixture 位于仓库内，不触发 Hugging Face、Toucan 全量数据或 pyarrow 下载。

### 13.2 发送 OTLP/HTTP 并生成确认清单

```bash
python tools/replay_traces.py \
  --input "$TRACE_STATE/toucan-traces.jsonl" \
  --sink otlp \
  --endpoint http://127.0.0.1:4318 \
  --rate 0 \
  --speed 0 \
  --confirm-manifest "$TRACE_STATE/toucan-confirmed.csv"
```

预期输出包含：

```text
replayed 10 traces, 69 spans sent, 0 failed posts (0 spans dropped)
```

确认清单只记录获得 HTTP 2xx 的 span，是发送侧对账事实源。

### 13.3 等待数据库可见并执行 reconcile

```bash
cd "$TRACE_WORKSPACE/exporter_demo"

reconciled=0
for attempt in $(seq 1 60); do
  if go run ./scripts/reconcile \
      -manifest "$TRACE_STATE/toucan-confirmed.csv" \
      > "$TRACE_STATE/reconcile.txt" 2>&1; then
    reconciled=1
    break
  fi
  sleep 1
done
cat "$TRACE_STATE/reconcile.txt"
test "$reconciled" = 1
grep 'RECONCILE OK: diff empty' "$TRACE_STATE/reconcile.txt"
```

成功输出必须同时包含：

```text
expected: 69, actual: 69, event rows: 0
MISSING (sent but absent in db): 0
UNEXPECTED (in db but never sent): 0
expected: 10, actual: 10
MISSING traces: 0
UNEXPECTED traces: 0
RECONCILE OK: diff empty
```

## 14. 数据库和 Collector 验收

使用 `gsql` 查询逻辑结果：

```bash
PGPASSWORD="$GV_DB_PASSWORD" gsql \
  -h "$GV_DB_HOST" -p "$GV_DB_PORT" \
  -U "$GV_DB_USER" -d "$GV_DB_NAME" \
  -A -F ',' -c '
    SELECT count(*) AS traces FROM traces;
    SELECT type,count(*) FROM observations GROUP BY type ORDER BY type;
    SELECT count(*) AS scores FROM scores;'
```

预期为 10 traces、27 generation、42 span、0 scores。`gsql` 在部分服务端错误下仍可能返回 0，自动化必须扫描输出中的 `ERROR:`。

使用 GV 当前版本认可的系统表查询或管理命令保留以下物理证据：

- observations 为 `storage_type=dstore, orientation=column`。
- observations 没有主键或唯一索引。
- observations 在 trace_id、ts、session_id 上存在 psort 非唯一索引。
- traces 和 scores 为行存且具有各自主键。

不同 GV 构建的系统目录接口可能不同，由环境维护方提供对应查询。Collector 日志中的 `storage_profile: xstore` 只能证明选择了 xstore DDL 分支，不能代替数据库物理形态证据。

检查 Collector 指标和日志：

```bash
curl -fsS http://127.0.0.1:8888/metrics \
  | grep -E 'otelcol_exporter_(sent|send_failed)_spans|otelcol_exporter_queue_(size|capacity)|otelcol_receiver_(accepted|failed|refused)_spans'

if grep -Eni 'level=(error|fatal)|panic|postgres://' \
  "$TRACE_STATE/collector-ingestion.log"; then
  exit 1
fi
```

预期 receiver accepted=69、failed=0、refused=0，exporter sent=69，queue size=0。零失败时 exporter 的 `send_failed_spans` 时间序列可能尚未产生。

灌入结束后按 GV 运维纪律执行 observations 的 `VACUUM` 和 `ANALYZE`，并记录 delta 累积与 flush 状态。该操作不改变 10/69/0 逻辑验收。

## 15. 成功判据

| 验收项 | 必须结果 |
|---|---|
| exporter xstore E2E | 全部 tests 通过 |
| Collector 启动 | 日志显示 `storage_profile: xstore`，无认证或 DDL 错误 |
| OTLP replay | 10 traces、69 spans、0 failed posts |
| confirm manifest | 69 个唯一 span、10 个唯一 trace |
| traces | 10 行 |
| observations | 69 行 |
| 类型分布 | generation=27、span=42 |
| scores | 0 行 |
| reconcile observations | MISSING=0、UNEXPECTED=0 |
| reconcile traces | MISSING=0、UNEXPECTED=0 |
| Collector | accepted=69、sent=69、failed/refused=0、queue=0 |
| xstore 物理证据 | observations 为 dstore 列存，索引符合当前 DDL |

全部满足后，才能形成“黄区 GV xstore Trace 摄入 Demo 跑通”的结论。仅有 HTTP 2xx、配置文本或数据库行数均不足以单独证明跑通。

## 16. 常见阻断

| 现象 | 边界和处理 |
|---|---|
| 已克隆 Collector 但没有 `otelcol-opengauss` | Collector 源码仓不含自定义 exporter 产物；从 `exporter_demo` 根目录执行 OCB |
| OCB 仍访问外网 | 本地 Collector clone 不参与 module 解析；检查 GOPROXY、GOSUMDB 及内部镜像覆盖 |
| OCB 下载某个 `go.opentelemetry.io` module 失败 | 内部 Go proxy 缺少锁定 module；补镜像或使用 offline bundle |
| `gitcode.com` connector 下载失败 | 内部源缺少 `openGauss-connector-go-pq v1.0.8` |
| Collector validate 报 `dsn is required` | 启动 shell 未设置 `OPENGAUSS_DSN`；env provider 没有默认值 |
| Collector 启动时报 dstore DDL 错误 | GV 构建或账号权限不满足 xstore profile；先修复第 8 节 E2E |
| Collector HTTP 200 但数据库无数据 | Receiver 已就绪，exporter 可能仍在重试；检查日志、sent/failed 和 queue |
| reconcile 报 UNEXPECTED | 目标表含历史跑次数据；使用专用 schema 并执行 DROP+重建 |
| 查询不到表 | Collector 和查询客户端的默认 search path 不一致 |
| `TRUNCATE` 后数据仍存在 | xstore 当前纪律禁止 TRUNCATE；使用 DROP，由 Collector 重建 |
| gsql 显示 ERROR 但 shell 成功 | 同时扫描 `ERROR:`，不单独依赖退出码 |
| Python 要求安装 OTel SDK | 当前固定 replay 使用标准库 OTLP/HTTP JSON 路径，无需 OTel Python SDK |

定位顺序固定为：Go module 与构建 → connector/xstore E2E → Collector 配置和启动 → OTLP HTTP → exporter 指标 → GV 表与物理形态 → reconcile。每次只改变一个边界条件，并在原失败点重新验证。

## 17. 参考入口

- [蓝区 openGauss 复现指南](trace-ingestion-demo-blue-zone-guide.md)
- [OTel、OTLP、Collector 与 GenAI 学习指南](otel-langfuse-study-guide.md)
- [`exporter_demo` xstore 验证 Runbook](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/XSTORE_VERIFICATION.md)
- [`exporter_demo` 连接指南](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/CONNECTION.md)
- [`exporter_demo` 三表数据字典](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/docs/SCHEMA.md)
- [`exporter_demo` OCB 构建清单](https://github.com/labmemW/exporter_demo/blob/4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b/builder-config.yaml)
- [`trace-synthesis` 说明](https://github.com/zfwang2021/trace-synthesis/blob/e0b9c83e3bd8bd7bb78d68225f29df0753f5432e/README.md)
