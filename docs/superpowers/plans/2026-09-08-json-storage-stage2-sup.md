# Agent Trace JSON 存储阶段二补充实验实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在统一实验条件下比较 openGauss JSON、openGauss JSONB、ClickHouse String JSON 和 ClickHouse Native JSON，并形成围绕 JSONB 与 Native JSON 自然处理流程的阶段二报告和组内汇报辅助材料。

**Architecture:** 复用阶段二冻结的 48,534 行数据和已经验证的正确性、原文恢复、统计及连接逻辑，在独立的 `json-storage-stage2-sup` 目录增加查询 truth、四结构数据库适配、顺序平衡运行和结果汇总。ClickHouse 机制观察使用独立小型运行说明 type hint、dynamic path、shared data、data part、merge、`OPTIMIZE TABLE ... FINAL`、`SELECT ... FINAL` 与 Sidecar；它不参与四结构性能排名。

**Tech Stack:** Python 3.11、Python 标准库、psycopg 3.3.5、openGauss 6.0.0、ClickHouse 25.12.11.4、Docker、Markdown、SVG

**Spec:** `docs/json-storage-stage2-sup-experiment-design-2026-09-08.md`

## Global Constraints

- 输入固定为 `docs/temp/json-storage-stage2/cross-engine-input-20260907/dataset.jsonl`，行数为 48,534，SHA-256 为 `8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405`。
- 原始输入 SHA-256 固定为 `3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683`；INSERT block 固定为 256 行，共 190 个。
- 四结构固定为 openGauss JSON、openGauss JSONB、ClickHouse String JSON、ClickHouse Native JSON；Native JSON 的 `max_dynamic_paths` 固定为 32，并保存稀疏保真 Sidecar。
- 四结构各运行四轮，顺序固定为设计中的 4×4 Latin square；任一时刻只有一个存储布局执行载入或查询。
- S01～S05 每轮预热 1 次、正式测量 100 次；S06 每轮预热 1 次、正式测量 20 次。
- 查询延迟覆盖已建立连接上的语句提交到响应完整读取。结果规范化、Sidecar 合并、canonical 序列化、hash 和 truth 核对位于该区间外，并单独记录客户端恢复耗时。
- 每个布局保存独立 raw 表，并分别通过 analysis canonical hash 与 raw UTF-8 SHA-256 门禁。
- 基础四结构不创建 GIN、表达式索引或 type hint。openGauss 索引和 ClickHouse type hint、Sidecar、merge、FINAL 进入独立机制观察。
- 阶段二已有实验目录 `docs/temp/json-storage-stage2/formal-20260907-retry-3/` 及其内容保持不变；补充产物写入 `docs/temp/json-storage-stage2-sup/`。
- 正文使用具体的数据库类型、存储对象和操作名称。首次出现 dynamic path、shared data、data part、merge 和 Sidecar 时说明含义。布局 ID 只用于命令、DDL、manifest 和结果定位。
- 不在正式实验设计、报告或汇报辅助材料中设置术语表。内部称呼检查清单位于 `docs/temp/json-storage-stage2-sup/terminology-guide.md`。
- 文档语言朴素严谨。使用“纳入统计的实验目录”“程序生成的汇总文件”等明确表述，不使用“正式根”“机器结果”等简写。

## 文件结构

| 文件 | 职责 |
|---|---|
| `experiments/json-storage-stage2-sup/generator/generate_supplement_truth.py` | 从阶段二数据生成 S01～S06 参数、最终水位结果和预处理指标 |
| `experiments/json-storage-stage2-sup/runner/supplement_common.py` | 加载阶段二公共函数，固定补充实验契约、布局顺序和样本统计 |
| `experiments/json-storage-stage2-sup/runner/opengauss_four_layout.py` | openGauss JSON/JSONB DDL、载入、查询、恢复、空间和清理 |
| `experiments/json-storage-stage2-sup/runner/clickhouse_four_layout.py` | ClickHouse String JSON/Native JSON DDL、载入、查询、Sidecar、part、merge 和清理 |
| `experiments/json-storage-stage2-sup/runner/run_four_layouts.py` | 单轮四结构顺序执行、环境采集、失败处理和 manifest 发布 |
| `experiments/json-storage-stage2-sup/runner/run_opengauss_mechanisms.py` | JSON/JSONB 热点表达式索引配对及 JSONB GIN 能力观察 |
| `experiments/json-storage-stage2-sup/runner/run_clickhouse_mechanisms.py` | type hint、Sidecar、merge、`OPTIMIZE TABLE ... FINAL` 与 `SELECT ... FINAL` 观察 |
| `experiments/json-storage-stage2-sup/report/summarize_results.py` | 校验四轮 16 个结果并生成确定性 JSON/Markdown 汇总 |
| `experiments/json-storage-stage2-sup/tests/` | truth、适配、运行、汇总和数据库集成测试 |
| `experiments/json-storage-stage2-sup/README.md` | 环境要求、命令、输出和复现说明 |
| `docs/json-storage-stage2-report-2026-09-08.md` | 阶段二完整报告，按 JSONB、Native JSON 和四结构场景重组 |
| `docs/json-storage-stage2-sup-pre-2026-09-08.md` | 组内进展汇报辅助材料 |
| `docs/assets/json-storage-*.svg` | JSONB、Native JSON 与 Sidecar 流程图 |

---

### Task 1: 公共契约与补充查询 Truth

**Files:**
- Create: `experiments/json-storage-stage2-sup/generator/generate_supplement_truth.py`
- Create: `experiments/json-storage-stage2-sup/runner/supplement_common.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_generate_supplement_truth.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_supplement_common.py`

**Interfaces:**
- Consumes: 阶段二 `dataset.jsonl`、`truth-manifest.json` 和 `run-manifest.json`
- Produces: `query-catalog.json`、`truth-manifest.json`、`run-manifest.json`
- Public functions: `preprocess_input(input_dir: Path) -> dict`、`query_results(rows: list[dict], query_id: str, parameters: dict) -> object`、`write_truth(input_dir: Path, output_dir: Path, command: list[str]) -> None`
- Public constants: `CONTRACT_VERSION`、`LAYOUTS`、`ROUND_ORDERS`、`QUERY_IDS`

- [ ] **Step 1: 编写失败的查询与契约测试**

  使用六条手写记录，独立写出预期结果：

  ```python
  self.assertEqual(common.LAYOUTS, ("og_json", "og_jsonb", "ch_string", "ch_native"))
  self.assertEqual(len(common.ROUND_ORDERS), 4)
  self.assertEqual(set(common.ROUND_ORDERS[0]), set(common.LAYOUTS))
  self.assertEqual(results["S01"], [["llm", 2], ["tool", 1]])
  self.assertEqual(results["S04"], {"non_null_count": 2, "utf8_bytes": 24})
  self.assertEqual(results["S06"]["row_count"], 6)
  ```

- [ ] **Step 2: 运行测试并确认因功能缺失而失败**

  Run:

  ```bash
  python3 -m unittest discover -s experiments/json-storage-stage2-sup/tests -p 'test_*truth.py' -v
  python3 -m unittest experiments/json-storage-stage2-sup/tests/test_supplement_common.py -v
  ```

  Expected: FAIL，缺少补充实验模块。

- [ ] **Step 3: 实现最小公共契约和 truth 生成器**

  `supplement_common.py` 通过明确的文件路径加载阶段二 `runner/common.py`，复用 canonical、SHA-256、nearest-rank、输入校验和原子 manifest 发布，不复制其实现。`ROUND_ORDERS` 使用设计中固定的四种顺序。

  truth 生成器逐行读取阶段二 dataset，生成 S01～S06 的固定参数和最终结果。S04 选择 `gen_ai.output.messages`，返回非空值数量和 canonical value 的 UTF-8 总字节数；S06 固定为按 `(start_time,event_id)` 排序后从下四分位点开始的 256 行。生成器记录读取、JSON 解析、参数选择、truth 计算和写出耗时。

- [ ] **Step 4: 验证 GREEN、确定性和输入身份**

  Run:

  ```bash
  python3 -m unittest experiments/json-storage-stage2-sup/tests/test_generate_supplement_truth.py experiments/json-storage-stage2-sup/tests/test_supplement_common.py -v
  python3 experiments/json-storage-stage2-sup/generator/generate_supplement_truth.py \
    --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
    --output docs/temp/json-storage-stage2-sup/input-20260908
  sha256sum docs/temp/json-storage-stage2-sup/input-20260908/truth-manifest.json
  ```

  Expected: 测试通过；输入行数、block 数与固定 SHA-256 一致；重复生成的 truth bytes 一致。

- [ ] **Step 5: 提交公共契约与 truth**

  ```bash
  git add experiments/json-storage-stage2-sup
  git commit -m "feat: define stage two supplement workload"
  ```

### Task 2: openGauss JSON 与 JSONB 适配

**Files:**
- Create: `experiments/json-storage-stage2-sup/runner/opengauss_four_layout.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_opengauss_four_layout.py`

**Interfaces:**
- Consumes: Task 1 的布局、查询和 truth；阶段二 `OpenGaussAdapter` 的连接与容器身份规则
- Produces: openGauss JSON/JSONB 的 DDL、分块载入、S01～S06、计划、空间、analysis/raw 恢复和清理结果
- Public class: `OpenGaussFourLayoutAdapter(host, port, container_name, namespace)`

- [ ] **Step 1: 编写失败的 DDL、查询和规范化测试**

  ```python
  self.assertIn("attributes JSON NOT NULL", create_layout_ddls("s2sup", "og_json"))
  self.assertIn("attributes JSONB NOT NULL", create_layout_ddls("s2sup", "og_jsonb"))
  self.assertIn("attributes #>> '{gen_ai,operation,name}'", query_sql("og_json", "S02"))
  self.assertNotIn("@>", query_sql("og_jsonb", "S03"))
  self.assertIn("ORDER BY start_time, event_id", query_sql("og_json", "S05"))
  ```

- [ ] **Step 2: 运行测试并确认因适配器缺失而失败**

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
    experiments/json-storage-stage2-sup/tests/test_opengauss_four_layout.py -v
  ```

  Expected: FAIL，缺少 `opengauss_four_layout.py`。

- [ ] **Step 3: 实现 JSON/JSONB 基础流程**

  两个布局只改变 `attributes` 类型。每个 block 在同一事务内写 analytics 和 raw 表；载入结果分别记录 analysis COPY、raw COPY、commit 和完整 block wall time。S02/S03/S04 使用等价 `#>>` 路径读取；S05/S06 完整读取 `attributes::text`。响应完整读取结束后再解析 JSON、规范化并核对 truth，同时记录客户端恢复耗时。

  空表创建不计入载入时间；`ANALYZE` 单独计时。空间记录 heap、TOAST、index 和 total bytes。异常路径只清理当前实例成功创建的 schema，并确认 schema 已删除。

- [ ] **Step 4: 运行单元测试和数据库能力测试**

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
    experiments/json-storage-stage2-sup/tests/test_opengauss_four_layout.py -v
  RUN_OPENGAUSS_INTEGRATION=1 /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
    experiments/json-storage-stage2-sup/tests/test_opengauss_four_layout.py -v
  ```

  Expected: 单元测试通过；Docker 可用时，512 行 fixture 的 S01～S06、canonical/raw 恢复和清理通过。Docker 不可用时集成测试明确跳过。

- [ ] **Step 5: 提交 openGauss 适配器**

  ```bash
  git add experiments/json-storage-stage2-sup/runner/opengauss_four_layout.py experiments/json-storage-stage2-sup/tests/test_opengauss_four_layout.py
  git commit -m "feat: compare openGauss JSON and JSONB"
  ```

### Task 3: ClickHouse String JSON 与 Native JSON 适配

**Files:**
- Create: `experiments/json-storage-stage2-sup/runner/clickhouse_four_layout.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_clickhouse_four_layout.py`

**Interfaces:**
- Consumes: Task 1 的布局、查询和 truth；阶段二 ClickHouse HTTP 与 QueryFinish 规则
- Produces: String JSON/Native JSON 的 DDL、分块载入、S01～S06、Sidecar 恢复、QueryFinish、part、路径、空间和清理结果
- Public class: `ClickHouseFourLayoutAdapter(host, port, container_name, namespace)`

- [ ] **Step 1: 编写失败的 DDL、查询和 Sidecar 测试**

  ```python
  self.assertIn("attributes String CODEC(ZSTD(3))", create_layout_ddl("s2sup", "ch_string", 32))
  native = create_layout_ddl("s2sup", "ch_native", 32)
  self.assertIn("attributes JSON(max_dynamic_paths=32)", native)
  self.assertIn("fidelity_values Map(String,String)", native)
  self.assertIn("attributes.gen_ai.operation.name.:String", query_sql("ch_native", "S02"))
  self.assertEqual(merge_fidelity({"a": 1}, {"empty": "{}"}), {"a": 1, "empty": {}})
  ```

- [ ] **Step 2: 运行测试并确认因适配器缺失而失败**

  ```bash
  python3 -m unittest experiments/json-storage-stage2-sup/tests/test_clickhouse_four_layout.py -v
  ```

  Expected: FAIL，缺少 `clickhouse_four_layout.py`。

- [ ] **Step 3: 实现 String JSON/Native JSON 基础流程**

  String JSON 直接写入 canonical analysis JSON。Native JSON 写入解析后的对象，并只为递归包含 JSON null、空对象或空数组的原始 Attribute 写入 `fidelity_values`。载入结果分别记录客户端行构造、analytics INSERT、raw INSERT、响应读取和完整 block wall time。

  S02/S03/S04 分别使用 JSON 提取函数和 Native JSON 直接子列；S05/S06 的 Native JSON 查询同时返回 `fidelity_values`。客户端在计时区间外恢复点键、覆盖 Sidecar 值并 canonical 化。每个正式查询关联唯一 query ID，阶段结束后批量读取 QueryFinish 指标。

- [ ] **Step 4: 运行单元测试和数据库能力测试**

  ```bash
  python3 -m unittest experiments/json-storage-stage2-sup/tests/test_clickhouse_four_layout.py -v
  RUN_CLICKHOUSE_INTEGRATION=1 python3 -m unittest \
    experiments/json-storage-stage2-sup/tests/test_clickhouse_four_layout.py -v
  ```

  Expected: 单元测试通过；Docker 可用时，512 行 fixture 的 S01～S06、Sidecar、canonical/raw 恢复、QueryFinish 和清理通过。Docker 不可用时集成测试明确跳过。

- [ ] **Step 5: 提交 ClickHouse 适配器**

  ```bash
  git add experiments/json-storage-stage2-sup/runner/clickhouse_four_layout.py experiments/json-storage-stage2-sup/tests/test_clickhouse_four_layout.py
  git commit -m "feat: compare ClickHouse String and Native JSON"
  ```

### Task 4: 四结构运行与确定性汇总

**Files:**
- Create: `experiments/json-storage-stage2-sup/runner/run_four_layouts.py`
- Create: `experiments/json-storage-stage2-sup/report/summarize_results.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_run_four_layouts.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_summarize_results.py`

**Interfaces:**
- Consumes: Tasks 1～3 的 truth、公共契约和两个适配器
- Produces: 每轮 `run-manifest.json`、四个 `result-<layout>.json`、`summary.json`、`tables.md`
- CLI: `run_four_layouts.py --input ... --truth ... --output ... --round 1 --layout-order ... --opengauss-container ... --clickhouse-container ...`
- CLI: `summarize_results.py --input ... --output ...`

- [ ] **Step 1: 编写失败的编排、失败发布和汇总测试**

  Fake adapter 使用完整结构的返回值验证：严格按给定布局顺序执行；单个布局失败后清理已经创建的对象并发布失败 manifest；完成 manifest 包含四个结果的字节数和 SHA-256；汇总拒绝缺轮、重复布局、错误顺序、truth 失败、恢复失败和产物篡改。

  ```python
  self.assertEqual(fake.calls[:4], ["og_json", "og_jsonb", "ch_string", "ch_native"])
  self.assertEqual(manifest["status"], "complete")
  self.assertEqual(summary["result_count"], 16)
  self.assertEqual(summary["rounds"], [1, 2, 3, 4])
  ```

- [ ] **Step 2: 运行测试并确认因编排和汇总缺失而失败**

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2-sup/tests/test_run_four_layouts.py \
    experiments/json-storage-stage2-sup/tests/test_summarize_results.py -v
  ```

  Expected: FAIL，缺少运行和汇总模块。

- [ ] **Step 3: 实现单轮编排**

  runner 启动时核对两容器、输入 SHA-256、truth、round 与布局顺序。每个布局创建独立 schema/database，完成 190 个 block 载入、维护、预热、正式查询、analysis/raw 恢复、空间采集和清理后写 result。四个 result 全部成功且对象全部清理后发布完成 manifest；失败 manifest 保留已产生的诊断和 cleanup 结果。

- [ ] **Step 4: 实现确定性汇总**

  汇总器核对四轮、16 个 result、DDL/查询/代码摘要、环境、样本数、truth、恢复和清理。每轮使用 nearest-rank 计算 p50/p95/p99，再报告四轮中位数及 min/max；S06 不计算 p99。`summary.json` 不写当前时间或输出路径，重复生成必须得到相同 bytes。`tables.md` 只从 `summary.json` 渲染。

- [ ] **Step 5: 运行完整单元测试并提交**

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover -s experiments/json-storage-stage2-sup/tests -v
  git add experiments/json-storage-stage2-sup/runner/run_four_layouts.py \
    experiments/json-storage-stage2-sup/report/summarize_results.py \
    experiments/json-storage-stage2-sup/tests
  git commit -m "feat: run four JSON storage layouts"
  ```

### Task 5: openGauss JSONB 与 ClickHouse Native JSON 机制观察

**Files:**
- Create: `experiments/json-storage-stage2-sup/runner/run_opengauss_mechanisms.py`
- Create: `experiments/json-storage-stage2-sup/runner/run_clickhouse_mechanisms.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_opengauss_mechanisms.py`
- Create: `experiments/json-storage-stage2-sup/tests/test_clickhouse_mechanisms.py`

**Interfaces:**
- Consumes: Task 1 输入与 truth、Task 3 ClickHouse HTTP/恢复函数
- Produces: openGauss JSON/JSONB 索引观察，四种 Native JSON 变体的阶段观察、路径变化、Sidecar 结果和 FINAL 正确性结果
- Public functions: `opengauss_mechanism_ddls(schema: str) -> dict[str, str]`、`mechanism_ddls(database: str) -> dict[str, str]`、`validate_path_transition(before: dict, after: dict, expected: set[str]) -> dict`、`run_final_probe(...) -> dict`

- [ ] **Step 1: 编写失败的 openGauss 索引机制测试**

  ```python
  ddls = opengauss_mechanism_ddls("s2sup_og_mech")
  self.assertIn("attributes JSON NOT NULL", ddls["og_json_hot"])
  self.assertIn("attributes JSONB NOT NULL", ddls["og_jsonb_hot"])
  self.assertIn("CREATE INDEX", ddls["og_json_hot"])
  self.assertIn("CREATE INDEX", ddls["og_jsonb_hot"])
  self.assertIn("jsonb_hash_ops", ddls["og_jsonb_gin"])
  ```

- [ ] **Step 2: 编写失败的 ClickHouse 机制 DDL 与状态转换测试**

  ```python
  ddls = mechanism_ddls("s2sup_mech")
  self.assertIn("max_dynamic_paths=32", ddls["ch_native_hinted32_sparse"])
  self.assertIn("gen_ai.operation.name String", ddls["ch_native_hinted32_sparse"])
  self.assertNotIn("fidelity_values", ddls["ch_native_auto32_none"])
  self.assertIn("attributes_raw String CODEC(ZSTD(3))", ddls["ch_native_auto32_full"])
  self.assertTrue(validate_path_transition(before, after, expected_paths)["logical_paths_preserved"])
  ```

- [ ] **Step 3: 运行测试并确认因机制程序缺失而失败**

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2-sup/tests/test_opengauss_mechanisms.py -v
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2-sup/tests/test_clickhouse_mechanisms.py -v
  ```

  Expected: FAIL，缺少两个机制实验程序。

- [ ] **Step 4: 实现 openGauss JSONB 和 ClickHouse Native JSON 的机制流程**

  openGauss 使用相同输入比较 JSON/JSONB 无索引与热点表达式索引，记录 JSON 文本解析、JSONB 转换、COPY、索引维护、`ANALYZE`、自然执行计划、路径查询、完整 JSON 返回和 heap/TOAST/index 空间。`og_jsonb_gin` 使用 `jsonb_hash_ops` 和 containment，只说明 JSONB 特有查询能力，不与 JSON 计算性能比例。

  runner 只暂停自己创建表的 merge，并在 `finally` 恢复。依次记录 DDL、首个 INSERT 后、全部 INSERT 后、恢复后台 merge 并稳定后、`OPTIMIZE TABLE ... FINAL` 后的 data part、压缩空间、dynamic/shared paths 和路径类型。每个阶段执行固定 truth 查询；路径物理位置改变时仍要求逻辑路径全集和结果不变。

  Sidecar 组记录无 Sidecar 的 Native JSON 回读差异、稀疏保真 Sidecar 的条目数/字节/恢复耗时、完整文档 Sidecar 的字节/恢复耗时。type hint 组与自动路径组保持相同预算和 Sidecar，只改变两个热点路径的声明类型。

- [ ] **Step 5: 实现 FINAL 正确性探针并验证**

  建立独立 `ReplacingMergeTree(version)` 小表，跨两个 data part 写同一主键的两个版本。普通查询返回两个物理版本；`SELECT ... FINAL` 返回最新逻辑版本；`OPTIMIZE TABLE ... FINAL` 后普通查询返回最新版本且 active part 为一个。该探针只记录正确性和 part 变化。

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2-sup/tests/test_opengauss_mechanisms.py -v
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest experiments/json-storage-stage2-sup/tests/test_clickhouse_mechanisms.py -v
  RUN_OPENGAUSS_INTEGRATION=1 /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
    experiments/json-storage-stage2-sup/tests/test_opengauss_mechanisms.py -v
  RUN_CLICKHOUSE_INTEGRATION=1 python3 -m unittest \
    experiments/json-storage-stage2-sup/tests/test_clickhouse_mechanisms.py -v
  git add experiments/json-storage-stage2-sup/runner/run_opengauss_mechanisms.py \
    experiments/json-storage-stage2-sup/runner/run_clickhouse_mechanisms.py \
    experiments/json-storage-stage2-sup/tests/test_opengauss_mechanisms.py \
    experiments/json-storage-stage2-sup/tests/test_clickhouse_mechanisms.py
  git commit -m "feat: observe JSON storage engine mechanisms"
  ```

### Task 6: 复现说明、正式运行与结果校验

**Files:**
- Create: `experiments/json-storage-stage2-sup/README.md`
- Modify: `README.md`
- Produce in ignored directory: `docs/temp/json-storage-stage2-sup/formal-20260908*/`
- Produce in ignored directory: `docs/temp/json-storage-stage2-sup/clickhouse-mechanisms-20260908*/`

**Interfaces:**
- Consumes: Tasks 1～5 的程序和固定 Docker 容器
- Produces: 四轮 16 个完成结果、机制观察结果、两次一致的汇总文件

- [ ] **Step 1: 编写复现说明并运行静态检查**

  README 说明两个容器、端口、输入、四轮命令、机制命令、汇总命令、失败目录排除规则和清理核对。运行：

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m compileall -q experiments/json-storage-stage2-sup
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover -s experiments/json-storage-stage1/tests -v
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover -s experiments/json-storage-stage2/tests -v
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest discover -s experiments/json-storage-stage2-sup/tests -v
  ```

- [ ] **Step 2: 启动或确认固定容器并完成 smoke run**

  ```bash
  docker inspect agent-trace-opengauss-v6 agent-trace-clickhouse-25-12
  RUN_OPENGAUSS_INTEGRATION=1 RUN_CLICKHOUSE_INTEGRATION=1 \
    /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python -m unittest \
    discover -s experiments/json-storage-stage2-sup/tests -v
  ```

  Expected: 版本、镜像 digest 和端口符合设计；集成测试通过；临时 schema/database 数为零。

- [ ] **Step 3: 按四种固定顺序运行四轮**

  每轮使用新的输出目录和命名空间：

  ```bash
  orders=(
    'og_json,og_jsonb,ch_string,ch_native'
    'og_jsonb,ch_native,og_json,ch_string'
    'ch_string,og_json,ch_native,og_jsonb'
    'ch_native,ch_string,og_jsonb,og_json'
  )
  for round in 1 2 3 4; do
    /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python experiments/json-storage-stage2-sup/runner/run_four_layouts.py \
      --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
      --truth docs/temp/json-storage-stage2-sup/input-20260908 \
      --output "docs/temp/json-storage-stage2-sup/formal-20260908/round-${round}" \
      --namespace "json_s2sup_r${round}" \
      --round "${round}" \
      --layout-order "${orders[$((round - 1))]}" \
      --opengauss-container agent-trace-opengauss-v6 \
      --opengauss-port 15432 \
      --clickhouse-container agent-trace-clickhouse-25-12 \
      --clickhouse-port 18123 \
      --measurements 100 \
      --document-page-measurements 20
  done
  ```

  Expected: 四个完成的运行清单共声明 16 个完成结果；失败批次保留诊断并使用新的输出目录完整重跑。

- [ ] **Step 4: 运行 openGauss 与 ClickHouse 机制观察和确定性汇总**

  ```bash
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python experiments/json-storage-stage2-sup/runner/run_opengauss_mechanisms.py \
    --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
    --truth docs/temp/json-storage-stage2-sup/input-20260908 \
    --output docs/temp/json-storage-stage2-sup/opengauss-mechanisms-20260908 \
    --container-name agent-trace-opengauss-v6 \
    --port 15432
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python experiments/json-storage-stage2-sup/runner/run_clickhouse_mechanisms.py \
    --input docs/temp/json-storage-stage2/cross-engine-input-20260907 \
    --truth docs/temp/json-storage-stage2-sup/input-20260908 \
    --output docs/temp/json-storage-stage2-sup/clickhouse-mechanisms-20260908 \
    --container-name agent-trace-clickhouse-25-12 \
    --port 18123
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python experiments/json-storage-stage2-sup/report/summarize_results.py \
    --input docs/temp/json-storage-stage2-sup/formal-20260908 \
    --output docs/temp/json-storage-stage2-sup/formal-20260908/summary-a
  /home/omm/work/agent-trace/trace-synthesis/.venv/bin/python experiments/json-storage-stage2-sup/report/summarize_results.py \
    --input docs/temp/json-storage-stage2-sup/formal-20260908 \
    --output docs/temp/json-storage-stage2-sup/formal-20260908/summary-b
  cmp docs/temp/json-storage-stage2-sup/formal-20260908/summary-a/summary.json \
    docs/temp/json-storage-stage2-sup/formal-20260908/summary-b/summary.json
  ```

- [ ] **Step 5: 清理核对并提交复现说明**

  核对两个引擎中没有本实验命名空间的 schema/database、暂停 merge 的目标表或 active merge。提交代码和 README，不提交 `docs/temp` 结果。

  ```bash
  git add README.md experiments/json-storage-stage2-sup/README.md
  git commit -m "docs: document stage two supplement reproduction"
  ```

### Task 7: 阶段二报告、流程图与汇报辅助材料

**Files:**
- Modify: `docs/json-storage-stage2-report-2026-09-08.md`
- Modify: `docs/json-storage-stage2-experiment-design-2026-09-08.md`
- Modify: `docs/json-storage-stage1-report-2026-09-08.md`
- Modify: `docs/json-storage-stage1-experiment-design-2026-09-08.md`
- Modify: `docs/json-storage-design-survey-2026-09-08.md`
- Modify: `README.md`
- Modify: `experiments/json-storage-stage1/README.md`
- Modify: `experiments/json-storage-stage2/README.md`
- Create: `docs/json-storage-stage2-sup-pre-2026-09-08.md`
- Create: `docs/assets/json-storage-opengauss-jsonb-flow.svg`
- Create: `docs/assets/json-storage-clickhouse-native-json-flow.svg`
- Create: `docs/assets/json-storage-native-json-sidecar-flow.svg`

**Interfaces:**
- Consumes: 阶段一/二已有结果、Task 6 的 `summary.json` 与 ClickHouse 机制 manifest、固定版本官方资料
- Produces: 可独立阅读的阶段二报告、组内汇报辅助材料、三张处理流程图和用语一致的相关文档

- [ ] **Step 1: 核对来源并建立事实清单**

  对每个数字记录来源文件和 JSON 路径。核对 openGauss 6.0.0 JSON/JSONB 官方资料与源码、ClickHouse 25.12 Native JSON、dynamic paths、shared data、data parts、merge、`OPTIMIZE TABLE ... FINAL` 和 `SELECT ... FINAL` 官方资料。内部事实清单保存到 `docs/temp/json-storage-stage2-sup/report-evidence.md`。

- [ ] **Step 2: 重组阶段二报告**

  报告依次说明 openGauss JSONB 的输入解析、JSONB 转换、存储、索引维护、路径读取、完整 JSON 返回；ClickHouse Native JSON 的输入解析、类型识别、dynamic/shared 分配、data part 写入、可查询、后台 merge、强制合并和读取；Sidecar 补足的信息、写入变化和恢复流程；四结构按基础载入、稳定列、热点/低密度路径、单路径投影、完整 Trace、完整文档页和特殊负载横比。

  原阶段二六布局结果和补充四结构结果分别注明实验目录、查询和计时范围。跨引擎空间只并列口径。报告压缩两仓状态、命令和诊断细节，通过链接指向实验 README 与 manifest。

- [ ] **Step 3: 绘制三张 SVG 流程图**

  openGauss 图按客户端 JSON 文本→SQL/COPY 输入→JSON 解析→JSONB 二进制值→heap/TOAST 与索引→路径/整文档查询排列。ClickHouse 图按 JSONEachRow→类型识别→type hint/dynamic/shared 分配→data part→可查询→后台 merge→`OPTIMIZE ... FINAL`→子列/整文档读取排列。Sidecar 图对比 Native JSON 丢失的 JSON null、空对象、空数组状态，以及稀疏 Sidecar 合并恢复与 raw bytes 恢复。

- [ ] **Step 4: 编写汇报辅助材料并统一相关文档语言**

  `pre` 控制为汇报提纲和图表材料：每节先给一句结论，再给流程图或结果表，最后给适用边界。相关文档把 `residual` 改为“动态属性”或具体列名，把 `layout` 改为“存储布局”，把“正式根/机器结果”改为明确的实验目录/汇总文件表述；英文机制词首次出现时解释，后续称呼一致。不在正式文档设置术语表。

- [ ] **Step 5: 验证引用、数据和文档后提交**

  ```bash
  python3 -m unittest discover -s experiments/json-storage-stage2-sup/tests -v
  git diff --check
  rg -n '正式根|机器结果|正式统计根' README.md docs experiments --glob '*.md' --glob '!docs/temp/**'
  ```

  Expected: 测试通过；差异无空白错误；不再出现列出的模糊简写；所有本地链接存在；报告与 `pre` 的数字均可追溯到事实清单。

  ```bash
  git add README.md docs experiments/json-storage-stage1/README.md experiments/json-storage-stage2/README.md
  git commit -m "docs: explain JSON storage processing and results"
  ```
