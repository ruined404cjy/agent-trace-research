# JSON 存储阶段二调优矩阵

本目录使用固定 Agent Trace 输入比较 openGauss JSON/JSONB、ClickHouse String/Native JSON，并验证 openGauss 索引、Native JSON 数值 type hint、ClickHouse 排序键和 merge 状态。设计和结果分别见[实验设计](../../docs/json-storage/json-storage-stage2-sup-experiment-design-2026-09-10.md)与[阶段二报告](../../docs/json-storage/json-storage-stage2-report-2026-09-10.md)。

## 环境与输入

运行环境为 Python 3.11、openGauss 6.0.0 和 ClickHouse 25.12.11.4。数据库容器与端口固定为：

- `agent-trace-opengauss-v6`，`127.0.0.1:15432`；
- `agent-trace-clickhouse-25-12`，`127.0.0.1:18123`。

冻结数据位于 `docs/temp/json-storage-stage2/cross-engine-input-20260907/`。新版查询 truth 位于 `docs/temp/json-storage-stage2-sup/input-20260911-v2/`，由以下命令生成：

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python

"$PYTHON" experiments/json-storage-stage2-sup/generator/generate_supplement_truth.py \
  --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
  --output docs/temp/json-storage-stage2-sup/input-20260911-v2
```

生成器保持冻结 dataset 不变，在载入视图中为每行增加 `experiment.duration_ms`，并发布派生后分析文档摘要、原文摘要和七个查询的 truth。

## 验证

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python

"$PYTHON" -m compileall -q experiments/json-storage-stage2-sup
"$PYTHON" -m unittest discover -s experiments/json-storage-stage2-sup/tests -v

RUN_OPENGAUSS_INTEGRATION=1 \
  "$PYTHON" -m unittest experiments/json-storage-stage2-sup/tests/test_opengauss_four_layout.py -v
RUN_CLICKHOUSE_INTEGRATION=1 \
  "$PYTHON" -m unittest experiments/json-storage-stage2-sup/tests/test_clickhouse_four_layout.py -v
```

集成测试在唯一临时 schema 或 database 中运行并验证清理。正式运行前确认不存在同名残留对象和暂停的 merge。

## 正式运行

调优 runner 接受逗号分隔的目标列表，并要求优化候选位于基础对照之前。主矩阵中的 ClickHouse 目标在查询前对分析表和原文表执行 `OPTIMIZE FINAL`，以构造单 part 对照；常规查询就绪不要求该操作。

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python
INPUT=docs/temp/json-storage-stage2/cross-engine-input-20260907
TRUTH=docs/temp/json-storage-stage2-sup/input-20260911-v2

"$PYTHON" experiments/json-storage-stage2-sup/runner/run_tuned_matrix.py \
  --input "$INPUT" \
  --truth "$TRUTH" \
  --output docs/temp/json-storage-stage2-sup/final-matrix-20260911/round-1 \
  --namespace js2fm_r1 \
  --targets og_jsonb_tuned,og_jsonb_baseline,ch_native_baseline \
  --measurements 30 \
  --document-page-measurements 10
```

其余正式目标为：

- `ch_native_numeric_hint,ch_native_trace_sort`：分离数值 type hint 与 Trace 排序键；
- `og_json_baseline,ch_string_baseline`：补齐文本 JSON 对照。

每组执行三轮并交替目标顺序。输出目录使用独立 namespace；失败运行保留诊断文件，重跑写入新目录。

## 汇总

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python

"$PYTHON" experiments/json-storage-stage2-sup/report/summarize_tuned_matrix.py \
  --input docs/temp/json-storage-stage2-sup/final-matrix-20260911/round-1 \
  --input docs/temp/json-storage-stage2-sup/final-matrix-20260911/round-2 \
  --input docs/temp/json-storage-stage2-sup/final-matrix-20260911/round-3 \
  --input docs/temp/json-storage-stage2-sup/isolation-matrix-20260911/round-1 \
  --input docs/temp/json-storage-stage2-sup/isolation-matrix-20260911/round-2 \
  --input docs/temp/json-storage-stage2-sup/isolation-matrix-20260911/round-3 \
  --input docs/temp/json-storage-stage2-sup/text-baseline-matrix-20260911/round-1 \
  --input docs/temp/json-storage-stage2-sup/text-baseline-matrix-20260911/round-2 \
  --input docs/temp/json-storage-stage2-sup/text-baseline-matrix-20260911/round-3 \
  --target og_json_baseline \
  --target og_jsonb_baseline \
  --target og_jsonb_tuned \
  --target ch_string_baseline \
  --target ch_native_baseline \
  --target ch_native_numeric_hint \
  --target ch_native_trace_sort \
  --output docs/temp/json-storage-stage2-sup/final-matrix-20260911/final-summary.json
```

汇总器先计算轮内中位数，再计算三轮中位数。输入运行必须完成 truth、访问路径、恢复和清理门禁。

## 索引与 merge 补测

openGauss 索引补测应将调优和基线写入独立目录。调优目标创建两个复合表达式 B-tree、Trace B-tree 和 GIN，并以正式参数化执行、扫描 Hint 诊断及 `idx_scan` 增量核对实际访问路径。

ClickHouse merge runner 每轮执行 String/Native JSON 的后台 merge 开启与暂停目标。暂停目标依次测量 190 part、后台 merge 并发、稳定少量 part 和单 part；所有查询均通过 truth 和 `QueryFinish` 门禁。

```bash
PYTHON=/home/omm/work/agent-trace/trace-synthesis/.venv/bin/python

"$PYTHON" experiments/json-storage-stage2-sup/runner/run_clickhouse_merge_performance.py \
  --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
  --truth docs/temp/json-storage-stage2-sup/input-20260911-v2 \
  --output docs/temp/json-storage-stage2-sup/review-merge-matrix-20260914/round-1 \
  --namespace js2rm_r1 \
  --round 1 \
  --measurements 10 \
  --document-page-measurements 5
```

三轮结果使用 `report/summarize_clickhouse_merge_performance.py` 汇总。每轮必须使用唯一 namespace 和输出目录。

## 其他实验入口

`run_clickhouse_projection_controls.py` 保留 ClickHouse 大值路径的自然输出、统一文本输出和聚合摘要控制。`run_opengauss_mechanisms.py` 与 `run_clickhouse_mechanisms.py` 保留结构保真、路径迁移和 FINAL 机制观察。这些结果作为辅助证据，不与性能矩阵混合汇总。

运行产物写入 gitignored 的 `docs/temp/json-storage-stage2-sup/`。提交前执行 `git diff --check` 和 `git status --short`，只纳入正式代码与文档。
