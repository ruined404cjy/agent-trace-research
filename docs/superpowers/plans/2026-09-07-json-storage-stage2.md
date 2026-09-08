# Agent Trace JSON 存储阶段二实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用真实 Agent Trace 数据和统一可比性契约，完成 openGauss 6.0.0 与 ClickHouse 25.12.11.4 的 residual 横向实验并产出独立报告。

**Architecture:** 审计器先输出真实 Trace 的路径、宽度、密度、类型、值长和窗口演化统计；生成器再把相同输入投影为两个引擎共用的 dataset、truth 和逐 block 水位结果。runner 通过公共契约模块与两个引擎 adapter 执行相同写入、并发查询、静态查询、正确性和原文恢复门禁，汇总器只消费完成 manifest 生成报告数据表。

**Tech Stack:** Python 3.11、Python 标准库、psycopg 3.3.5、openGauss 6.0.0、ClickHouse 25.12.11.4、Docker

**Spec:** `docs/json-storage-stage2-experiment-design.md`

## Global Constraints

- 输入固定为 `/home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl`，SHA-256 为 `3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683`。
- 上游 manifest SHA-256 固定为 `f46bbe843c5578faea9ddfb5e8eb3aac8b6dc4c2f4fb89beabab503043505e38`；输入规模固定为 48,534 spans、6,257 traces。
- exporter_demo 远端基线固定为 `81b55be6d6912d18c4e2ac7102fd7906e9dac3e8`；trace-synthesis 远端基线固定为 `ef3be141cc17415de9fb5a9d8003c16a4cd679ac`。
- 数据路径固定为 `independent_loader`，不修改 exporter_demo、trace-synthesis、数据库镜像和容器配置。
- 数据库容器固定为 `agent-trace-opengauss-v6` 和 `agent-trace-clickhouse-25-12`；端口固定为 15432 和 18123。
- 可比性契约固定为 `json-storage-cross-engine-v1`；INSERT block 固定为 256 行；正式查询每条预热 1 次、测量 100 次；每个布局运行 3 轮。
- 查询延迟只覆盖语句提交到结果完整读取；连接建立、规范化、hash 和正确性校验位于计时区间外。
- 请求等价速率固定为 `成功样本数 × 1000 / 成功样本延迟总和(ms)`；任一正式样本或正确性门禁失败的轮次不进入横向统计。
- 缓存状态固定为 `query_warmup_1_no_os_cache_drop`；运行期间不清理宿主机页缓存。
- 原始输入 bytes 单独保存在 raw 表；native JSON、Map 或 JSONB 的重建结果不承担字节级恢复契约。
- Full/Core、长 payload 和 asset reference 属于阶段三范围。
- 运行产物写入 gitignored 的 `docs/temp/json-storage-stage2/`；仓库只提交代码、测试、README 和最终报告。

## 文件结构

| 文件 | 职责 |
|---|---|
| `experiments/json-storage-stage2/audit/audit_real_traces.py` | 流式计算 `P/W/dᵢ/cᵢ/Tᵢ/Lᵢ/E`、payload 分布和分组覆盖 |
| `experiments/json-storage-stage2/generator/generate_cross_engine.py` | 生成统一 dataset、truth、查询参数和逐 block 水位结果 |
| `experiments/json-storage-stage2/runner/common.py` | canonical、hash、样本统计、可比性门禁和 manifest 公共逻辑 |
| `experiments/json-storage-stage2/runner/opengauss.py` | openGauss DDL、写入、查询、计划、空间和清理 |
| `experiments/json-storage-stage2/runner/clickhouse.py` | ClickHouse DDL、写入、查询、query log、part/merge、路径清单和清理 |
| `experiments/json-storage-stage2/runner/run_cross_engine.py` | 单引擎单轮编排、并发 worker、逐水位 truth 和完成 manifest |
| `experiments/json-storage-stage2/report/summarize_results.py` | 校验 6 个完成 run，生成跨轮汇总 JSON 和 Markdown 表 |
| `experiments/json-storage-stage2/tests/` | 审计、生成器、公共契约、adapter 和真实数据库集成测试 |
| `experiments/json-storage-stage2/README.md` | 复现命令、产物说明和运行门禁 |
| `docs/json-storage-stage2-cross-engine-report-2026-09-07.md` | 阶段二证据、横向结果、适用范围和建议 |

## Spec Coverage

| 设计内容 | 实施任务 |
|---|---|
| 两仓与环境基线 | Tasks 3、6、8 |
| 真实 Trace 分布审计 | Task 1 |
| 公共数据、truth、原文恢复 | Task 2 |
| 可比性、连接、计时、QPS | Tasks 3、6 |
| openGauss 三布局 | Task 4 |
| ClickHouse 三布局 | Task 5 |
| 持续写入、并发查询、后台维护 | Task 6 |
| 三轮正式运行与 manifest | Task 7 |
| 独立阶段二报告 | Task 8 |

---

### Task 1: 真实 Trace 分布审计

**Files:**
- Create: `experiments/json-storage-stage2/audit/audit_real_traces.py`
- Create: `experiments/json-storage-stage2/tests/test_audit_real_traces.py`

**Interfaces:**
- Consumes: UTF-8 JSONL，每行包含 `trace_id`、`span_id`、`start_time`、`attributes` 和 `schema_version`
- Produces: `audit.json`、`run-manifest.json`
- Public functions: `json_type(value) -> str`、`nearest_rank(values, percentile) -> int`、`audit_file(path, window_minutes) -> dict`、`write_audit(input_path, upstream_manifest, output_dir) -> None`

- [ ] **Step 1: 编写失败的审计单元测试**

  测试用三条记录覆盖稀疏键、点键、跨窗口新增键、类型冲突和 payload 长度：

  ```python
  report = audit.audit_file(input_path, window_minutes=15)
  self.assertEqual(report["global"]["path_count"], 4)
  self.assertEqual(report["global"]["width"]["max"], 3)
  self.assertEqual(report["paths"]["mixed"]["types"], {"integer": 1, "string": 1})
  self.assertEqual(report["paths"]["sparse"]["present_rows"], 1)
  self.assertEqual(report["epochs"][1]["added_paths"], ["late"])
  self.assertEqual(report["dimensions"]["tenant"], {"status": "unavailable"})
  ```

- [ ] **Step 2: 运行测试并确认缺少模块**

  Run:

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_audit_real_traces.py -v
  ```

  Expected: FAIL，原因是 `audit_real_traces.py` 尚不存在。

- [ ] **Step 3: 实现流式审计与原子 manifest**

  `audit_file` 按 Attribute 顶层键统计；点号属于键名。值使用对象键排序、紧凑分隔符、UTF-8 的 canonical bytes 计算 distinct 和长度。15 分钟窗口以 `start_time` 左闭右开分桶；`E` 比较相邻窗口的键集合与类型集合。`source_dataset`、`framework`、`span.type`、`schema_version` 各分组记录行数、`P`、`W` 和路径密度；tenant、project、instrumentation scope 固定记录为 `unavailable`。payload 统计固定覆盖完整原始行、attributes、`gen_ai.input.messages`、`gen_ai.output.messages`、`gen_ai.tool.call.arguments` 和 `gen_ai.tool.call.result`。

  `write_audit` 先删除旧 `run-manifest.json`，核对输入和上游 manifest hash，再写 `audit.json`，最后发布包含 artifact 字节数和 SHA-256 的完成 manifest。

- [ ] **Step 4: 运行单元测试与真实审计**

  Run:

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_audit_real_traces.py -v
  python3 experiments/json-storage-stage2/audit/audit_real_traces.py \
    --input /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl \
    --upstream-manifest /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/manifest.json \
    --output docs/temp/json-storage-stage2/real-trace-audit-20260907 \
    --window-minutes 15
  ```

  Expected: 单元测试通过；真实运行的 manifest 为 `complete`，行数 48,534、trace 数 6,257，输入 hash 与 Global Constraints 一致。

- [ ] **Step 5: 提交审计器**

  ```bash
  git add experiments/json-storage-stage2/audit/audit_real_traces.py experiments/json-storage-stage2/tests/test_audit_real_traces.py
  git commit -m "feat: audit real trace JSON distribution"
  ```

### Task 2: 统一数据与 Truth 生成器

**Files:**
- Create: `experiments/json-storage-stage2/generator/generate_cross_engine.py`
- Create: `experiments/json-storage-stage2/tests/test_generate_cross_engine.py`

**Interfaces:**
- Consumes: 固定输入 JSONL、Task 1 的 `audit.json`
- Produces: `dataset.jsonl`、`truth-manifest.json`、`run-manifest.json`
- Public functions: `project_attributes(attributes) -> dict`、`flatten_projected(value, key_map) -> dict[str, bytes]`、`build_record(source, raw_event, ingest_seq) -> dict`、`query_results(rows, watermark, parameters) -> dict`、`write_dataset(input_path, audit_path, output_dir, block_size) -> None`

- [ ] **Step 1: 编写失败的投影、原文和逐水位测试**

  ```python
  projected = generator.project_attributes({"a.b": 1, "a.c": [2, 1], "plain": None})
  self.assertEqual(projected, {"a": {"b": 1, "c": [2, 1]}, "plain": None})
  with self.assertRaisesRegex(ValueError, "prefix conflict"):
      generator.project_attributes({"a": 1, "a.b": 2})
  self.assertEqual(record["raw_event"].encode("utf-8"), original_line_without_newline)
  self.assertEqual(truth["watermarks"], [2, 4, 5])
  self.assertEqual(truth["queries"]["Q05"]["4"]["row_count"], 1)
  ```

- [ ] **Step 2: 运行测试并确认缺少模块**

  Run:

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_generate_cross_engine.py -v
  ```

  Expected: FAIL，原因是 `generate_cross_engine.py` 尚不存在。

- [ ] **Step 3: 实现统一记录和查询 truth**

  `build_record` 输出设计文档规定的 15 个字段。`event_id` 固定为 `<trace_id>:<span_id>`；`span_type` 读取 `attributes["span.type"]`；`framework` 读取 Attribute 后回退空字符串；`level` 由 `status.code == "STATUS_CODE_ERROR"` 映射为 `ERROR`，其余为 `DEFAULT`。`attributes_map` 的 value 是原始 Attribute 值的 canonical JSON 字符串。

  native JSON 路径预算取大于等于审计全局 `P` 的最小 2 的幂，上限为 128。Q01、Q03 的 50% 窗口固定为 `[min(start_time), min(start_time) + (max(start_time)-min(start_time))/2)`。Q04 按 `(span_count, trace_id)` 排序后选择下中位 trace。

  规范化结果固定为：Q01/Q02 的 `[[span_type,count],...]`，Q03 的 `[[span_type,count,sum_duration_ms],...]`，Q04 的 `[[start_time,event_id,attributes_map],...]`，Q05 的 `{row_count,identity_sha256}`。每个 256 行 block 水位和最终水位保存 Q01～Q05 的规范化结果 SHA-256、行数及固定参数；runner 不使用数据库结果生成期望值。

- [ ] **Step 4: 实现原子产物发布并运行真实生成**

  Run:

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_generate_cross_engine.py -v
  python3 experiments/json-storage-stage2/generator/generate_cross_engine.py \
    --input /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl \
    --audit docs/temp/json-storage-stage2/real-trace-audit-20260907/audit.json \
    --output docs/temp/json-storage-stage2/cross-engine-input-20260907 \
    --block-size 256
  ```

  Expected: 测试通过；manifest 为 `complete`，行数 48,534、block 数 190、最后水位 48,534；重新运行到第二目录后 `dataset.jsonl` 和 `truth-manifest.json` 的 SHA-256 完全一致。

- [ ] **Step 5: 提交生成器**

  ```bash
  git add experiments/json-storage-stage2/generator/generate_cross_engine.py experiments/json-storage-stage2/tests/test_generate_cross_engine.py
  git commit -m "feat: generate stage two cross-engine dataset"
  ```

### Task 3: 公共可比性与 Manifest 契约

**Files:**
- Create: `experiments/json-storage-stage2/runner/common.py`
- Create: `experiments/json-storage-stage2/tests/test_runner_common.py`

**Interfaces:**
- Consumes: 查询耗时与成功状态、输入 manifest、repo/container 身份
- Produces: 统一样本摘要、identity 审计、artifact identity 和 run manifest
- Public functions: `canonical_bytes(value) -> bytes`、`file_identity(path) -> dict`、`summarize_samples(samples) -> dict`、`audit_identities(actual, expected) -> dict`、`verify_input(input_dir) -> tuple[dict, dict]`、`write_manifest_last(output_dir, manifest, artifacts) -> None`

- [ ] **Step 1: 编写失败的统计和失败可见性测试**

  ```python
  summary = common.summarize_samples([{"ok": True, "latency_ms": 10.0}, {"ok": True, "latency_ms": 30.0}])
  self.assertEqual(summary["request_equivalent_qps"], 50.0)
  with self.assertRaisesRegex(ValueError, "failed sample"):
      common.summarize_samples([{"ok": True, "latency_ms": 10.0}, {"ok": False, "latency_ms": 1.0}])
  self.assertEqual(common.audit_identities(["a", "a", "x"], {"a", "b"})["duplicate_count"], 1)
  ```

- [ ] **Step 2: 运行测试并确认缺少模块**

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_runner_common.py -v
  ```

  Expected: FAIL，原因是 `common.py` 尚不存在。

- [ ] **Step 3: 实现公共契约**

  percentile 使用 nearest-rank；样本摘要只接受全部成功且耗时大于 0 的样本。`write_manifest_last` 在运行开始时删除旧完成 manifest，先写 artifact，再写 `status=complete`；失败路径写 `status=failed`、异常类型和消息，不复用旧 artifact 身份。

  manifest 固定记录 `comparability_contract_version=json-storage-cross-engine-v1`、延迟边界、QPS 公式、连接复用、阶段屏障、输入/DDL/查询/runner hash、三个仓的本地 HEAD 与远端 main、容器资源和清理结果。

- [ ] **Step 4: 运行测试并提交**

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_runner_common.py -v
  git add experiments/json-storage-stage2/runner/common.py experiments/json-storage-stage2/tests/test_runner_common.py
  git commit -m "feat: define cross-engine comparability contract"
  ```

### Task 4: openGauss 三布局 Adapter

**Files:**
- Create: `experiments/json-storage-stage2/runner/opengauss.py`
- Create: `experiments/json-storage-stage2/tests/test_opengauss_adapter.py`

**Interfaces:**
- Consumes: 统一记录、Q01～Q05、临时 schema 名、psycopg connection
- Produces: `og_jsonb`、`og_jsonb_hot`、`og_jsonb_gin` 的 DDL、查询、计划、空间和恢复结果
- Public functions: `create_layout_ddls(namespace, layout) -> str`、`query_sql(layout, query_id) -> str`
- Public class: `OpenGaussAdapter(host, port, container_name, namespace)`，实现 `connect_worker()`、`create_layout(layout, budget)`、`insert_block(layout, rows)`、`execute_query(connection, layout, query_id, params, watermark)`、`collect_plan(layout, query_id, params, watermark)`、`collect_storage(layout)`、`finish_maintenance(layout, timeout_seconds)`、`verify_raw(layout, truth)`、`cleanup(layout)`

- [ ] **Step 1: 编写失败的 DDL 与 SQL 单元测试**

  ```python
  ddls = adapter.create_layout_ddls("json_s2_test", "og_jsonb_hot")
  self.assertIn("attributes JSONB NOT NULL", ddls)
  self.assertIn("raw_event TEXT NOT NULL", ddls)
  self.assertIn("jsonb_object_field_text", ddls)
  self.assertNotIn("USING gin", ddls)
  self.assertIn("USING gin(attributes jsonb_ops)", adapter.create_layout_ddls("json_s2_test", "og_jsonb_gin"))
  self.assertIn("ingest_seq < %s", adapter.query_sql("og_jsonb", "Q05"))
  ```

- [ ] **Step 2: 运行测试并确认缺少模块**

  ```bash
  ../trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2/tests/test_opengauss_adapter.py -v
  ```

  Expected: FAIL，原因是 `opengauss.py` 尚不存在。

- [ ] **Step 3: 实现 DDL、COPY block、查询和空间采集**

  三个布局使用相同强类型列和独立 raw 表；表达式索引只覆盖 `gen_ai.operation.name`，GIN 使用 `jsonb_ops`。每个 block 在同一事务中写 analytics 与 raw 表并提交。Q04 返回完整 residual，计时结束后按生成器键映射恢复为原始 Attribute key 到 canonical value 的映射。`finish_maintenance` 执行 `ANALYZE`。adapter 从现有容器环境读取 `GS_PASSWORD` 并只保存在进程内存，不写入日志、结果或 manifest。

- [ ] **Step 4: 添加 512 行真实集成门禁**

  测试创建唯一 schema，使用 fixture dataset 逐 block 写入，验证三布局 Q01～Q05 digest、raw SHA-256、表达式/GIN 计划和清理。运行：

  ```bash
  RUN_OPENGAUSS_INTEGRATION=1 ../trace-synthesis/.venv/bin/python -m unittest \
    experiments/json-storage-stage2/tests/test_opengauss_adapter.py -v
  ```

  Expected: 单元与集成测试通过；临时 schema 数为 0。

- [ ] **Step 5: 提交 openGauss adapter**

  ```bash
  git add experiments/json-storage-stage2/runner/opengauss.py experiments/json-storage-stage2/tests/test_opengauss_adapter.py
  git commit -m "feat: add openGauss stage two layouts"
  ```

### Task 5: ClickHouse 三布局 Adapter

**Files:**
- Create: `experiments/json-storage-stage2/runner/clickhouse.py`
- Create: `experiments/json-storage-stage2/tests/test_clickhouse_adapter.py`

**Interfaces:**
- Consumes: 统一记录、Q01～Q05、临时 database 名、HTTP connection
- Produces: `ch_string`、`ch_map`、`ch_native` 的 DDL、查询、query log、part/merge、路径清单、空间和恢复结果
- Public functions: `create_layout_ddl(namespace, layout, budget) -> str`、`query_sql(layout, query_id) -> str`
- Public class: `ClickHouseAdapter(host, port, container_name, namespace)`，实现 `connect_worker()`、`create_layout(layout, budget)`、`insert_block(layout, rows)`、`execute_query(connection, layout, query_id, params, watermark)`、`collect_plan(layout, query_id, params, watermark)`、`collect_storage(layout)`、`finish_maintenance(layout, timeout_seconds)`、`verify_raw(layout, truth)`、`cleanup(layout)`

- [ ] **Step 1: 编写失败的 DDL 与 SQL 单元测试**

  ```python
  self.assertIn("attributes String CODEC(ZSTD(3))", adapter.create_layout_ddl("db", "ch_string", 32))
  self.assertIn("Map(String,String)", adapter.create_layout_ddl("db", "ch_map", 32))
  self.assertIn("JSON(max_dynamic_paths=32)", adapter.create_layout_ddl("db", "ch_native", 32))
  self.assertIn("ORDER BY (project_id,start_time,event_id)", adapter.create_layout_ddl("db", "ch_native", 32))
  self.assertIn("ingest_seq < {watermark:UInt64}", adapter.query_sql("ch_map", "Q05"))
  ```

- [ ] **Step 2: 运行测试并确认缺少模块**

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_clickhouse_adapter.py -v
  ```

  Expected: FAIL，原因是 `clickhouse.py` 尚不存在。

- [ ] **Step 3: 实现 HTTP 连接复用、JSONEachRow block 和查询**

  每个 worker 使用独立 `http.client.HTTPConnection` 并在阶段内复用。Map 查询使用原始 Attribute key 和 canonical JSON value；String 与 native 使用嵌套投影路径。每条正式查询设置唯一 query ID，完整读取响应后轮询 `system.query_log` 的 `QueryFinish`。

- [ ] **Step 4: 实现 merge、路径预算和 512 行集成门禁**

  `finish_maintenance` 等待 active merge backlog 连续三次为 0，最多 120 秒。`ch_native` 核对 dynamic/shared path 全集和预算；raw 表逐条核对原始 SHA-256。运行：

  ```bash
  RUN_CLICKHOUSE_INTEGRATION=1 python3 -m unittest \
    experiments/json-storage-stage2/tests/test_clickhouse_adapter.py -v
  ```

  Expected: 三布局 Q01～Q05、路径、raw 和清理门禁通过；临时 database 不存在。

- [ ] **Step 5: 提交 ClickHouse adapter**

  ```bash
  git add experiments/json-storage-stage2/runner/clickhouse.py experiments/json-storage-stage2/tests/test_clickhouse_adapter.py
  git commit -m "feat: add ClickHouse stage two layouts"
  ```

### Task 6: 单轮编排、并发查询与完成 Manifest

**Files:**
- Create: `experiments/json-storage-stage2/runner/run_cross_engine.py`
- Create: `experiments/json-storage-stage2/tests/test_run_cross_engine.py`
- Create: `experiments/json-storage-stage2/README.md`

**Interfaces:**
- Consumes: Task 2 输入目录、engine、round、layout order 和容器身份
- Produces: 每 layout 的 `result.json`、单轮 `run-manifest.json`
- CLI: `--input --output --engine --container-name --namespace --round --layout-order --measurements --block-size --query-workers --maintenance-timeout-seconds`
- Public functions: `parse_layout_order(value, engine) -> tuple[str, ...]`、`run_ingest_with_queries(adapter, blocks, truth, query_workers) -> dict`、`run_static_queries(adapter, layout, truth, measurements) -> dict`、`execute(args) -> None`

- [ ] **Step 1: 编写失败的轮换、屏障和水位测试**

  ```python
  self.assertEqual(runner.parse_layout_order("og_jsonb_hot,og_jsonb_gin,og_jsonb"), ("og_jsonb_hot", "og_jsonb_gin", "og_jsonb"))
  with self.assertRaises(ValueError):
      runner.parse_layout_order("og_jsonb,og_jsonb,og_jsonb_gin")
  blocks = [(watermark, [{"ingest_seq": watermark - 1}]) for watermark in (128, 256, 384, 512, 640, 768, 896, 1024)]
  truth = {str(watermark): {query_id: "digest-ok" for query_id in ("Q01", "Q02", "Q03", "Q05")} for watermark, _ in blocks}
  fake_adapter = FakeAdapter(returned_digest="digest-ok")
  result = runner.run_ingest_with_queries(fake_adapter, blocks, truth, query_workers=2)
  self.assertEqual(result["query_workers"], 2)
  self.assertTrue(all(sample["watermark"] in {640, 768, 896, 1024} for sample in result["samples"]))
  self.assertTrue(all(sample["matches_truth"] for sample in result["samples"]))
  ```

- [ ] **Step 2: 运行测试并确认缺少模块**

  ```bash
  ../trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2/tests/test_run_cross_engine.py -v
  ```

  Expected: FAIL，原因是 `run_cross_engine.py` 尚不存在。

- [ ] **Step 3: 实现写入期查询和静态测量**

  前五个 block 提交后建立两个 query worker 连接；worker 与协调线程通过 `threading.Barrier(3)` 进入测量。共享水位只在 analytics 与 raw 同一 block 成功后推进。每次查询在启动时复制水位，完整读取后在计时区间外与对应 truth digest 核对。

  写入结束后完成引擎维护；Q01～Q05 各预热一次，再正式执行 100 次。Q04 的 adapter 返回值在计时后统一展开为原始 Attribute key 到 canonical value 的映射。任一失败样本把本轮标记为 `failed`，保存诊断和清理结果，退出码为 1。

  顶层 manifest 固定包含 `status`、`engine`、`round`、`run_id`、`layout_order`、`comparability_contract`、`input`、`environment`、`gates` 和 `artifacts`；`gates` 固定包含 `correctness`、`raw_recovery`、`all_samples_successful` 和 `cleanup`。

- [ ] **Step 4: 运行双引擎小型统一门禁**

  ```bash
  RUN_OPENGAUSS_INTEGRATION=1 RUN_CLICKHOUSE_INTEGRATION=1 \
    ../trace-synthesis/.venv/bin/python -m unittest discover \
    -s experiments/json-storage-stage2/tests -v
  ```

  Expected: 全部单元和集成测试通过；两引擎使用相同 fixture、查询参数、block、返回规范化和 truth；没有临时 schema/database 残留。

- [ ] **Step 5: 编写 README 并提交编排器**

  README 记录依赖、输入、审计/生成/runner 命令、三轮布局顺序、manifest 解释、失败诊断和清理检查。

  ```bash
  git add experiments/json-storage-stage2/runner/run_cross_engine.py experiments/json-storage-stage2/tests/test_run_cross_engine.py experiments/json-storage-stage2/README.md
  git commit -m "feat: orchestrate stage two cross-engine runs"
  ```

### Task 7: 三轮正式横向运行

**Files:**
- Create: `docs/temp/json-storage-stage2/formal-20260907/`（gitignored 运行产物）

**Interfaces:**
- Consumes: Task 2 完成输入、两个运行中容器
- Produces: 6 个引擎轮次、18 个 layout 结果及完成 manifest

- [ ] **Step 1: 复核输入、上游和数据库身份**

  Run:

  ```bash
  sha256sum \
    /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl \
    /home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/manifest.json
  git -C /home/omm/work/agent-trace/exporter_demo ls-remote origin refs/heads/main
  git -C /home/omm/work/agent-trace/trace-synthesis ls-remote origin refs/heads/main
  docker inspect agent-trace-opengauss-v6 agent-trace-clickhouse-25-12 \
    --format '{{.Name}} {{.Config.Image}} memory={{.HostConfig.Memory}} nanocpus={{.HostConfig.NanoCpus}} running={{.State.Running}}'
  docker exec -u omm agent-trace-opengauss-v6 bash -lc \
    "/usr/local/opengauss/bin/gsql -d postgres -Atc 'SELECT version();'"
  curl -fsS 'http://127.0.0.1:18123/?query=SELECT%20version()'
  ```

  Expected: 两个输入 hash 和两个远端 main 与 Global Constraints 一致；两个容器处于 running，资源限制和数据库版本进入 run manifest。任一身份变化先更新设计边界，不启动正式轮次。

- [ ] **Step 2: 运行 openGauss 三轮**

  三轮 layout order 分别为：

  ```text
  og_jsonb,og_jsonb_hot,og_jsonb_gin
  og_jsonb_hot,og_jsonb_gin,og_jsonb
  og_jsonb_gin,og_jsonb,og_jsonb_hot
  ```

  使用精确轮次、namespace 和顺序运行：

  ```bash
  og_orders=(
    'og_jsonb,og_jsonb_hot,og_jsonb_gin'
    'og_jsonb_hot,og_jsonb_gin,og_jsonb'
    'og_jsonb_gin,og_jsonb,og_jsonb_hot'
  )
  for round in 1 2 3; do
    ../trace-synthesis/.venv/bin/python experiments/json-storage-stage2/runner/run_cross_engine.py \
      --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
      --output "docs/temp/json-storage-stage2/formal-20260907/opengauss-round-${round}" \
      --engine opengauss --container-name agent-trace-opengauss-v6 \
      --namespace "json_s2_og_r${round}" --round "$round" \
      --layout-order "${og_orders[$((round - 1))]}" \
      --measurements 100 --block-size 256 --query-workers 2 \
      --maintenance-timeout-seconds 120
  done
  ```

  Expected: 三个 manifest 均为 `complete`，每轮 48,534 行，全部临时 schema 已删除。

- [ ] **Step 3: 运行 ClickHouse 三轮**

  三轮 layout order 分别为：

  ```text
  ch_string,ch_map,ch_native
  ch_map,ch_native,ch_string
  ch_native,ch_string,ch_map
  ```

  使用精确轮次、namespace 和顺序运行：

  ```bash
  ch_orders=(
    'ch_string,ch_map,ch_native'
    'ch_map,ch_native,ch_string'
    'ch_native,ch_string,ch_map'
  )
  for round in 1 2 3; do
    ../trace-synthesis/.venv/bin/python experiments/json-storage-stage2/runner/run_cross_engine.py \
      --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
      --output "docs/temp/json-storage-stage2/formal-20260907/clickhouse-round-${round}" \
      --engine clickhouse --container-name agent-trace-clickhouse-25-12 \
      --namespace "json_s2_ch_r${round}" --round "$round" \
      --layout-order "${ch_orders[$((round - 1))]}" \
      --measurements 100 --block-size 256 --query-workers 2 \
      --maintenance-timeout-seconds 120
  done
  ```

  Expected: 三个 manifest 均为 `complete`；merge 等待结果、dynamic/shared paths 和 raw recovery 完整；全部临时 database 已删除。

- [ ] **Step 4: 核对全部运行门禁**

  ```bash
  find docs/temp/json-storage-stage2/formal-20260907 -name run-manifest.json -print0 \
    | xargs -0 -n1 jq -r '[.status,.engine,.round,.gates.correctness,.gates.raw_recovery,.gates.cleanup] | @tsv'
  ```

  Expected: 6 个顶层 run manifest、18 个 layout result；全部状态为 `complete/pass`，无失败样本或清理残留。

### Task 8: 汇总、报告与最终验证

**Files:**
- Create: `experiments/json-storage-stage2/report/summarize_results.py`
- Create: `experiments/json-storage-stage2/tests/test_summarize_results.py`
- Create: `docs/json-storage-stage2-cross-engine-report-2026-09-07.md`
- Modify: `README.md`
- Modify: `experiments/json-storage-stage2/README.md`

**Interfaces:**
- Consumes: Task 1 audit、Task 7 的 6 个完成 manifest 与 18 个 layout result
- Produces: `summary.json`、`summary.md`、独立阶段二报告

- [ ] **Step 1: 编写失败的完整性与汇总测试**

  ```python
  summary = report.summarize(run_dirs)
  self.assertEqual(set(summary["layouts"]), {"og_jsonb", "og_jsonb_hot", "og_jsonb_gin", "ch_string", "ch_map", "ch_native"})
  self.assertTrue(all(item["rounds"] == 3 for item in summary["layouts"].values()))
  with self.assertRaisesRegex(ValueError, "incomplete run"):
      report.summarize([failed_run])
  ```

- [ ] **Step 2: 实现汇总器并生成表格**

  汇总器拒绝 contract、input/truth hash、查询 catalog、block size、worker 数、测量次数或返回契约不同的 run。每个 layout 输出三轮 median、轮间 min/max、写入吞吐、block p95/p99、查询 median/p95/p99、请求等价速率、空间、read rows/bytes、维护时间和正确性状态。

  ```bash
  python3 -m unittest experiments/json-storage-stage2/tests/test_summarize_results.py -v
  python3 experiments/json-storage-stage2/report/summarize_results.py \
    --input docs/temp/json-storage-stage2/formal-20260907 \
    --output docs/temp/json-storage-stage2/formal-20260907/summary
  ```

  Expected: 测试通过；`summary.json` 和 `summary.md` 引用全部 6 个 run ID 和 18 个 layout result。

- [ ] **Step 3: 编写阶段二独立报告**

  报告只包含两仓/环境基线增量、真实 Trace 审计、统一契约与 run ID、residual 横向结果、正确性与原文恢复、异常、适用范围和 residual 建议。阶段一结果仅作为机制证据引用，不进入横向比例；Full/Core、长 payload 和 asset 不进入本报告。

- [ ] **Step 4: 执行完整验证**

  ```bash
  RUN_OPENGAUSS_INTEGRATION=1 RUN_CLICKHOUSE_INTEGRATION=1 \
    ../trace-synthesis/.venv/bin/python -m unittest discover \
    -s experiments/json-storage-stage2/tests -v
  git diff --check
  rg -n "run-manifest|json-storage-cross-engine-v1|independent_loader" \
    docs/json-storage-stage2-cross-engine-report-2026-09-07.md \
    experiments/json-storage-stage2/README.md
  ```

  Expected: 全部测试通过，diff 检查无错误，报告明确列出 contract、数据路径和 run manifest。

- [ ] **Step 5: 提交汇总器和报告**

  ```bash
  git add experiments/json-storage-stage2/report/summarize_results.py experiments/json-storage-stage2/tests/test_summarize_results.py experiments/json-storage-stage2/README.md docs/json-storage-stage2-cross-engine-report-2026-09-07.md README.md
  git commit -m "docs: report JSON storage stage two results"
  ```
