# JSON 存储阶段二补充实验

本目录在固定真实 Trace 输入上比较 openGauss JSON、openGauss JSONB、ClickHouse String JSON 和 ClickHouse Native JSON 四种基础存储结构。数据路径为 `independent_loader`；实验不经过 Collector、exporter、benchmark 或产品服务。

基础四结构的定义、查询语义、计时边界和证据范围见[阶段二补充实验设计](../../docs/json-storage-stage2-sup-experiment-design-2026-09-08.md)。正式结果由四轮矩阵的原始清单汇总产生；机制观察单独记录，不进入四结构性能排名。

## 依赖和冻结输入

运行环境为 Python 3.11、`psycopg`、openGauss 6.0.0 与 ClickHouse 25.12.11.4。容器必须分别命名为 `agent-trace-opengauss-v6`、`agent-trace-clickhouse-25-12`，并把服务发布到 `127.0.0.1:15432` 和 `127.0.0.1:18123`。

固定输入由两个目录组成：

| 目录 | 内容 |
| --- | --- |
| `docs/temp/json-storage-stage2/cross-engine-input-20260907` | 48,534 行 dataset、190 个 256 行 block、阶段二 truth 与来源清单 |
| `docs/temp/json-storage-stage2-sup/input-20260908` | 补充实验 S01--S06 truth、查询 catalog 与来源身份 |

四结构 runner 会逐项核对 dataset、两个 truth、query catalog 与来源清单的字节数和 SHA-256。不要复制、改写或重新生成上述冻结目录。

从仓库根目录执行以下预检。`--help` 的参数定义是复现命令的唯一 CLI 来源。

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python

docker inspect agent-trace-opengauss-v6 agent-trace-clickhouse-25-12
"$PYTHON" experiments/json-storage-stage2-sup/runner/run_four_layouts.py --help
"$PYTHON" experiments/json-storage-stage2-sup/report/summarize_results.py --help

"$PYTHON" -m compileall -q experiments/json-storage-stage2-sup
"$PYTHON" -m unittest discover -s experiments/json-storage-stage1/tests -v
"$PYTHON" -m unittest discover -s experiments/json-storage-stage2/tests -v
"$PYTHON" -m unittest discover -s experiments/json-storage-stage2-sup/tests -v

RUN_OPENGAUSS_INTEGRATION=1 RUN_CLICKHOUSE_INTEGRATION=1 \
  "$PYTHON" -m unittest discover -s experiments/json-storage-stage2-sup/tests -v
```

集成测试在唯一临时 schema 或 database 中运行，完成后验证清理。开始正式矩阵前，检查不存在本实验残留的 schema 或 database，且 ClickHouse 不存在暂停 merge 或 active merge。

## 四轮正式矩阵

每轮严格使用 100 次 S01--S05 测量、20 次 S06 测量和两个已建立连接的查询 worker。运行器拒绝改变这些参数。下列命令使用实际运行日期 `20260909`，每轮使用独立目录与命名空间。

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python
INPUT=docs/temp/json-storage-stage2/cross-engine-input-20260907
TRUTH=docs/temp/json-storage-stage2-sup/input-20260908
OUTPUT=docs/temp/json-storage-stage2-sup/formal-20260909

orders=(
  'og_json,og_jsonb,ch_string,ch_native'
  'og_jsonb,ch_native,og_json,ch_string'
  'ch_string,og_json,ch_native,og_jsonb'
  'ch_native,ch_string,og_jsonb,og_json'
)

for round in 1 2 3 4; do
  "$PYTHON" experiments/json-storage-stage2-sup/runner/run_four_layouts.py \
    --input "$INPUT" \
    --truth "$TRUTH" \
    --output "$OUTPUT/round-$round" \
    --namespace "json_s2sup_r$round" \
    --round "$round" \
    --layout-order "${orders[$((round - 1))]}" \
    --opengauss-container agent-trace-opengauss-v6 \
    --opengauss-port 15432 \
    --clickhouse-container agent-trace-clickhouse-25-12 \
    --clickhouse-port 18123 \
    --measurements 100 \
    --document-page-measurements 20 \
    --query-workers 2 \
    --maintenance-timeout-seconds 120
done
```

每个 `round-*` 目录仅在四种结构均完成、truth/分析恢复/原文恢复/样本/清理门禁均通过后写入 `run-manifest.json` 的 `status=complete`。汇总只接受四个完整轮次、共 16 个结果。

失败运行保留原目录、`run-manifest.json` 与已写入的诊断结果。使用新的正式结果目录和新的 namespace 重跑整轮，例如 `formal-20260909-retry-1/` 与 `json_s2sup_retry1_r1`；不覆盖失败目录，也不把其结果加入汇总输入。

## 机制观察和汇总

openGauss 机制观察覆盖 JSON/JSONB、热点表达式索引和 JSONB GIN；`attributes::jsonb` 观察的计时范围是 scan/filter/expression/result 复合探针，不能解释为纯 JSON 到 JSONB 转换时间。ClickHouse 机制观察覆盖无 Sidecar、稀疏 Sidecar、完整 Sidecar 和 type hint 稀疏 Sidecar，并记录 merge 与 `OPTIMIZE ... FINAL`。

两个机制程序均以 complete-last manifest 发布结果。使用独立输出目录与 namespace 执行；每个目录包含 `mechanism-result.json` 和仅在正确性、环境身份及清理全部通过后发布的 `run-manifest.json`。

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python
INPUT=docs/temp/json-storage-stage2/cross-engine-input-20260907
TRUTH=docs/temp/json-storage-stage2-sup/input-20260908

"$PYTHON" experiments/json-storage-stage2-sup/runner/run_opengauss_mechanisms.py \
  --input "$INPUT" --truth "$TRUTH" \
  --output docs/temp/json-storage-stage2-sup/opengauss-mechanisms-20260909 \
  --namespace json_s2sup_og_mech_0909 \
  --host 127.0.0.1 --port 15432 \
  --container-name agent-trace-opengauss-v6

"$PYTHON" experiments/json-storage-stage2-sup/runner/run_clickhouse_mechanisms.py \
  --input "$INPUT" --truth "$TRUTH" \
  --output docs/temp/json-storage-stage2-sup/clickhouse-mechanisms-20260909 \
  --namespace json_s2sup_ch_mech_0909 \
  --host 127.0.0.1 --port 18123 \
  --container-name agent-trace-clickhouse-25-12
```

本轮无 Sidecar Native JSON 观测到 JSON null 与空对象信息缺失（分别 5,446/10；共 5,456），未观测到空数组缺失；稀疏 Sidecar 规则仍覆盖递归空数组。这些信息缺失必须记录，无 Sidecar 结果不构成完整文档保真通过。稀疏与完整 Sidecar 的完整恢复门禁必须通过。dynamic/shared path inventory 在 merge 前后可变化，只记录物理组织变化，逻辑恢复由完整文档和查询 truth 判定。

正式结果目录 `formal-20260909/round-1` 保留首次失败运行，不进入有效汇总。以硬链接构建独立的汇总输入：`round-1-retry-1` 映射为 `round-1`，`round-2`至 `round-4` 保持同名。每轮只链接 `run-manifest.json` 和四个 `result-*.json` 的明确文件名。目标目录已存在时命令清晰失败；需要保留多组汇总时，将 `SUMMARY_INPUT` 和 `SUMMARY_OUTPUT` 改为新的显式目录。

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python
RESULT_ROOT=docs/temp/json-storage-stage2-sup/formal-20260909
SUMMARY_INPUT=docs/temp/json-storage-stage2-sup/formal-20260909-summary-input
SUMMARY_OUTPUT=docs/temp/json-storage-stage2-sup/formal-20260909-summary

if [ -e "$SUMMARY_INPUT" ] || [ -e "$SUMMARY_OUTPUT" ]; then
  echo "summary target already exists; choose new explicit directories" >&2
  exit 1
fi

mkdir "$SUMMARY_INPUT"

for mapping in \
  'round-1-retry-1 round-1' \
  'round-2 round-2' \
  'round-3 round-3' \
  'round-4 round-4'
do
  set -- $mapping
  source_round=$1
  summary_round=$2
  mkdir "$SUMMARY_INPUT/$summary_round"
  for file in \
    run-manifest.json \
    result-og_json.json \
    result-og_jsonb.json \
    result-ch_string.json \
    result-ch_native.json
  do
    ln "$RESULT_ROOT/$source_round/$file" "$SUMMARY_INPUT/$summary_round/$file"
  done
done

"$PYTHON" experiments/json-storage-stage2-sup/report/summarize_results.py \
  --input "$SUMMARY_INPUT" --output "$SUMMARY_OUTPUT/summary-a"
"$PYTHON" experiments/json-storage-stage2-sup/report/summarize_results.py \
  --input "$SUMMARY_INPUT" --output "$SUMMARY_OUTPUT/summary-b"
cmp "$SUMMARY_OUTPUT/summary-a/summary.json" \
  "$SUMMARY_OUTPUT/summary-b/summary.json"
```

汇总器重新验证 16 个结果的输入身份、布局与轮次、DDL/查询身份、190 个写入 block、S01--S06 样本、ClickHouse QueryFinish、analysis 与 raw recovery、存储指标和清理状态。汇总 JSON 不写入当前时间，因此同一输入的两次 `summary.json` 必须字节一致。

## 结果目录和清理

运行产物一律写入 gitignored 的 `docs/temp/json-storage-stage2-sup/`。清单保存完整命令、容器与镜像身份、文件摘要、内部运行编号、结果和清理状态；README 不保存这些运行时身份。

正式汇总前后执行：

```bash
git diff --check
git status --short
```

确认只有预期的文档改动进入 Git，`docs/temp/` 产物保持忽略。完成后核对 openGauss 中不存在 `json_s2sup%` schema，ClickHouse 中不存在 `json_s2sup%` database，且目标表没有暂停 merge 或 active merge。
