import hashlib
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))
sys.path.insert(0, str(STAGE_DIR / "generator"))

import clickhouse_four_layout as clickhouse
import generate_supplement_truth as generator
import supplement_common as common


FIXTURE_ROWS = 512
BLOCK_ROWS = 256


def select_integration_fixture(rows, parameters):
    """从冻结输入确定性选择覆盖 S01 至 S06 的 512 行 fixture。"""
    window_rows = generator._window_rows(rows, parameters["S01"])
    required_ids = set()

    def include_first(query_id, predicate):
        """选择固定查询的首个窗口命中记录。"""
        row = next((item for item in window_rows if predicate(item)), None)
        if row is None:
            raise AssertionError(f"fixture requires a {query_id} match")
        required_ids.add(row["event_id"])

    include_first("S02", lambda row: generator._value_at_path(
        row["attributes_analysis"], "gen_ai.operation.name"
    ) == parameters["S02"]["operation_name"])
    include_first("S03", lambda row: generator._value_at_path(
        row["attributes_analysis"], "failure.mistake_mode"
    ) == parameters["S03"]["failure_mistake_mode"])
    include_first("S04", lambda row: generator._value_at_path(
        row["attributes_analysis"], parameters["S04"]["attribute_key"]
    ) is not None)
    required_ids.update(row["event_id"] for row in window_rows if row["trace_id"] == parameters["S05"]["trace_id"])
    required_rows = [row for row in window_rows if row["event_id"] in required_ids]
    remaining_rows = [row for row in window_rows if row["event_id"] not in required_ids]
    fixture = required_rows + remaining_rows[:FIXTURE_ROWS - len(required_rows)]
    if len(fixture) != FIXTURE_ROWS:
        raise AssertionError("fixture requires 512 window rows")
    return fixture


class ClickHouseFourLayoutUnitTest(unittest.TestCase):
    """验证 ClickHouse String JSON 与 Native JSON 的基础比较契约。"""

    def setUp(self):
        """建立不依赖数据库的 adapter。"""
        self.adapter = clickhouse.ClickHouseFourLayoutAdapter(
            "127.0.0.1", 18123, "unused", "jsons2sup_test"
        )

    def test_database_version_uses_safe_http_query(self):
        """捕获环境身份遗漏 ClickHouse 服务端版本。"""
        connection = mock.MagicMock()
        with mock.patch.object(self.adapter, "connect_worker", return_value=connection), mock.patch.object(
            self.adapter, "_request", return_value="25.12.11.4\n"
        ) as request:
            self.assertEqual(self.adapter.database_version(), "25.12.11.4")
        request.assert_called_once_with(connection, "SELECT version()")

    def test_analytics_row_can_preserve_v1_attributes_without_derived_path(self):
        """旧机制契约写入原分析属性，不注入调优矩阵的数值路径。"""
        row = {
            "ingest_seq": 1, "event_id": "event-v1", "trace_id": "trace-v1",
            "span_id": "span-v1", "parent_span_id": None, "project_id": "project-v1",
            "start_time": "2030-01-01T00:00:00.000Z",
            "end_time": "2030-01-01T00:00:01.000Z", "duration_ms": 1000,
            "span_type": "tool", "framework": "fixture", "level": "INFO",
            "attributes_analysis": {"plain": "kept"},
        }

        actual = self.adapter._analytics_row("ch_native", row, include_derived=False)

        self.assertEqual(actual["attributes"], {"plain": "kept"})
        self.assertEqual(actual["fidelity_values"], {})

    def test_layout_ddl_only_adds_native_sidecar(self):
        """防止基础布局混入索引、hint 或额外加速结构。"""
        string_ddl = clickhouse.create_layout_ddl("s2sup", "ch_string", 32)
        native_ddl = clickhouse.create_layout_ddl("s2sup", "ch_native", 32)

        self.assertIn("attributes String CODEC(ZSTD(3))", string_ddl)
        self.assertNotIn("fidelity_values", string_ddl)
        self.assertIn("attributes JSON(max_dynamic_paths=32)", native_ddl)
        self.assertIn("fidelity_values Map(String,String)", native_ddl)
        self.assertNotIn("CREATE PROJECTION", native_ddl)
        self.assertNotIn("CREATE INDEX", native_ddl)
        self.assertNotIn("TYPE", native_ddl)

    def test_tuned_native_ddl_declares_numeric_hint_and_trace_sort_key(self):
        """保证调优结构固定数值路径类型，并按 Trace 定位字段排序。"""
        ddl = clickhouse.create_layout_ddl("s2sup", "ch_native", 32, profile="tuned")

        self.assertIn("`experiment.duration_ms` Int64", ddl)
        self.assertNotIn("`gen_ai.operation.name` String", ddl)
        self.assertNotIn("`failure.mistake_mode` String", ddl)
        self.assertNotIn("PROJECTION trace_lookup", ddl)
        self.assertIn("ORDER BY (project_id,trace_id,start_time,event_id)", ddl)
        self.assertIn(
            "attributes.gen_ai.operation.name.:String = {operation_name:String}",
            clickhouse.query_sql("ch_native", "S02", profile="tuned"),
        )
        self.assertIn(".:String", clickhouse.query_sql("ch_native", "S03", profile="tuned"))
        self.assertIn(
            "sum(attributes.experiment.duration_ms)",
            clickhouse.query_sql("ch_native", "S07", profile="tuned"),
        )

    def test_native_profiles_isolate_hint_from_trace_sort_key(self):
        """保证 type hint 与 Trace 排序键使用独立候选，避免归因混淆。"""
        hinted = clickhouse.create_layout_ddl("s2sup", "ch_native", 32, profile="numeric_hint")
        trace_sorted = clickhouse.create_layout_ddl("s2sup", "ch_native", 32, profile="trace_sort")

        self.assertIn("`experiment.duration_ms` Int64", hinted)
        self.assertIn("ORDER BY (project_id,start_time,event_id)", hinted)
        self.assertNotIn("`experiment.duration_ms` Int64", trace_sorted)
        self.assertIn("ORDER BY (project_id,trace_id,start_time,event_id)", trace_sorted)
        self.assertIn(".:Int64", clickhouse.query_sql("ch_native", "S07", profile="trace_sort"))

    def test_native_budget_is_fixed_to_comparison_contract(self):
        """防止适配器接受非 32 的预算而改变基础比较存储结构。"""
        self.assertEqual(clickhouse.validate_budget(32), 32)
        for budget in (1, 31, 33, 128, 32.0, True):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(ValueError, "must be 32"):
                    clickhouse.validate_budget(budget)
                with self.assertRaisesRegex(ValueError, "must be 32"):
                    clickhouse.create_layout_ddl("s2sup", "ch_native", budget)

    def test_queries_match_s01_to_s06_contract(self):
        """防止两种 JSON 表达式、分页和整文档读取偏离公共语义。"""
        for layout in common.LAYOUTS[2:]:
            for query_id in common.QUERY_IDS:
                with self.subTest(layout=layout, query_id=query_id):
                    statement = clickhouse.query_sql(layout, query_id)
                    self.assertIn("project_id = {project_id:String}", statement)
                    self.assertIn("start_time >= {start_time:DateTime64(3, 'UTC')}", statement)
                    self.assertIn("start_time < {end_time:DateTime64(3, 'UTC')}", statement)
        self.assertIn("JSONExtractString(attributes, 'gen_ai', 'operation', 'name')", clickhouse.query_sql("ch_string", "S02"))
        self.assertIn("attributes.gen_ai.operation.name.:String", clickhouse.query_sql("ch_native", "S02"))
        self.assertIn("JSONExtractString(attributes, 'failure', 'mistake_mode')", clickhouse.query_sql("ch_string", "S03"))
        self.assertIn("attributes.failure.mistake_mode.:String", clickhouse.query_sql("ch_native", "S03"))
        self.assertIn("JSONExtractRaw(attributes, 'gen_ai', 'output', 'messages')", clickhouse.query_sql("ch_string", "S04"))
        self.assertIn("attributes.gen_ai.output.messages", clickhouse.query_sql("ch_native", "S04"))
        self.assertIn("fidelity_values", clickhouse.query_sql("ch_native", "S05"))
        self.assertIn("ORDER BY start_time, event_id", clickhouse.query_sql("ch_native", "S06"))
        self.assertIn("OFFSET", clickhouse.query_sql("ch_string", "S06"))
        self.assertIn("sum(attributes.experiment.duration_ms.:Int64)", clickhouse.query_sql("ch_native", "S07"))
        self.assertIn("JSONExtractInt(attributes, 'experiment', 'duration_ms')", clickhouse.query_sql("ch_string", "S07"))

    def test_projection_controls_hold_filter_and_output_contract_constant(self):
        """防止 S04 控制实验混用筛选条件，或继续比较不同的输出类型。"""
        for layout in common.LAYOUTS[2:]:
            for mode in ("natural", "uniform_text", "aggregate"):
                with self.subTest(layout=layout, mode=mode):
                    statement = clickhouse.projection_control_sql(layout, mode)
                    self.assertIn("project_id = {project_id:String}", statement)
                    self.assertIn("start_time >= {start_time:DateTime64(3, 'UTC')}", statement)
                    self.assertIn("start_time < {end_time:DateTime64(3, 'UTC')}", statement)
                    self.assertTrue(statement.endswith("FORMAT JSONEachRow"))

        self.assertIn(
            "JSONExtractRaw(attributes, 'gen_ai', 'output', 'messages') AS value",
            clickhouse.projection_control_sql("ch_string", "natural"),
        )
        self.assertIn(
            "attributes.gen_ai.output.messages AS value",
            clickhouse.projection_control_sql("ch_native", "natural"),
        )
        for layout in common.LAYOUTS[2:]:
            uniform = clickhouse.projection_control_sql(layout, "uniform_text")
            aggregate = clickhouse.projection_control_sql(layout, "aggregate")
            self.assertIn("value_text", uniform)
            self.assertIn("isNotNull(value_text)", uniform)
            self.assertIn("non_null_count", aggregate)
            self.assertIn("utf8_bytes", aggregate)
        self.assertIn("JSONExtractRaw", clickhouse.projection_control_sql("ch_string", "uniform_text"))
        self.assertIn("toJSONString", clickhouse.projection_control_sql("ch_native", "uniform_text"))

        with self.assertRaisesRegex(ValueError, "unsupported projection control"):
            clickhouse.projection_control_sql("ch_native", "unknown")

    def test_projection_control_normalization_enforces_same_logical_result(self):
        """防止控制实验只统一列名，却未统一 S04 的逻辑结果。"""
        expected = {"non_null_count": 2, "utf8_bytes": len(b'[1,2]') + len('{"x":"汉"}'.encode("utf-8"))}
        self.assertEqual(
            self.adapter._normalize_projection_control(
                "uniform_text", [{"value_text": "[1,2]"}, {"value_text": '{"x":"汉"}'}]
            ),
            expected,
        )
        self.assertEqual(
            self.adapter._normalize_projection_control(
                "aggregate", [{"non_null_count": "2", "utf8_bytes": str(expected["utf8_bytes"])}]
            ),
            expected,
        )
        with self.assertRaisesRegex(ValueError, "one result row"):
            self.adapter._normalize_projection_control("aggregate", [])

    def test_set_merges_updates_both_owned_tables(self):
        """防止 merge 探针只暂停分析表而遗漏同步写入的原文表。"""
        connection = mock.MagicMock()
        with (
            mock.patch.object(self.adapter, "connect_worker", return_value=connection),
            mock.patch.object(self.adapter, "_request", return_value="") as request,
        ):
            self.adapter.set_merges("ch_native", False)

        statements = [call.args[1] for call in request.call_args_list]
        self.assertEqual(statements, [
            "SYSTEM STOP MERGES jsons2sup_test_ch_native.analytics",
            "SYSTEM STOP MERGES jsons2sup_test_ch_native.raw",
        ])
        connection.close.assert_called_once_with()

    def test_execute_projection_control_records_wall_read_and_response_size(self):
        """防止控制实验遗漏响应读取耗时，或把客户端恢复计入查询 wall。"""
        connection = mock.MagicMock()
        parameters = {
            "project_id": "project-a",
            "start_time": "2030-01-01T00:00:00.000Z",
            "end_time": "2030-01-01T00:01:00.000Z",
        }

        def respond(_connection, statement, **kwargs):
            self.assertIn("jsons2sup_test_ch_string.analytics", statement)
            kwargs["timing"]["response_read_ms"] = 1.25
            return '{"non_null_count":2,"utf8_bytes":7}\n'

        with mock.patch.object(self.adapter, "_request", side_effect=respond):
            result = self.adapter.execute_projection_control(
                connection, "ch_string", "aggregate", parameters
            )

        self.assertEqual(result["result"], {"non_null_count": 2, "utf8_bytes": 7})
        self.assertEqual(result["response_read_ms"], 1.25)
        self.assertEqual(result["response_bytes"], len(b'{"non_null_count":2,"utf8_bytes":7}\n'))
        self.assertGreaterEqual(result["latency_ms"], 0.0)
        self.assertGreaterEqual(result["recovery_ms"], 0.0)
        self.assertRegex(result["query_log_id"], r"^json_s2sup_s04_aggregate_[0-9a-f]+$")

    def test_normalization_matches_all_supplement_result_contracts(self):
        """防止 JSONEachRow 响应恢复改变 S01 至 S06 的 truth 语义。"""
        document = {"gen_ai": {"output": {"messages": ["汉"]}}}
        params = {"page_size": 256}

        self.assertEqual(self.adapter._normalize_result("S01", [{"span_type": "tool", "count": 1}]), [["tool", 1]])
        self.assertEqual(self.adapter._normalize_result("S02", [{"span_type": "llm", "count": 2}]), [["llm", 2]])
        self.assertEqual(
            self.adapter._normalize_result("S03", [{"event_id": "event-b"}, {"event_id": "event-a"}]),
            {"row_count": 2, "identity_sha256": hashlib.sha256(b'["event-a","event-b"]').hexdigest()},
        )
        self.assertEqual(
            self.adapter._normalize_result("S04", [{"value": '["汉"]'}, {"value": None}, {"value": '{"a":1}'}]),
            {"non_null_count": 2, "utf8_bytes": len('["汉"]'.encode("utf-8")) + len(b'{"a":1}')},
        )
        expected_row = [["2030-01-01T00:00:01.234Z", "event-1", document]]
        self.assertEqual(
            self.adapter._normalize_result("S05", [{"start_time": "2030-01-01 00:00:01.234", "event_id": "event-1", "attributes": json.dumps(document)}], params, "ch_string"),
            expected_row,
        )
        self.assertEqual(
            self.adapter._normalize_result("S06", [{"total_rows": 3, "start_time": "2030-01-01 00:00:01.234", "event_id": "event-1", "attributes": document, "fidelity_values": {}}], params, "ch_native"),
            {"identity_sha256": hashlib.sha256(b'["event-1"]').hexdigest(), "page_row_count": 1, "row_count": 3, "rows": expected_row},
        )
        self.assertEqual(
            self.adapter._normalize_result("S07", [{"non_null_count": "2", "sum": "37"}], params, "ch_native"),
            {"non_null_count": 2, "sum": 37},
        )

    def test_sidecar_preserves_complete_affected_branches(self):
        """防止点号键和数字对象键在 Native JSON 回读后改变原始结构。"""
        original = {
            "branch": {"null": None, "empty": {}, "items": [None, [], {"value": "kept"}]},
            "nested": {"a.b": {"keep": 1}},
            "holder": {"0": None},
            "plain": "kept",
        }
        sidecar = clickhouse.fidelity_values(original)
        native_read = {
            "branch": {"items": [{}, {}, {"value": "kept"}]},
            "nested": {"a": {"b": {"keep": 1}}},
            "holder": {},
            "plain": "kept",
        }

        self.assertEqual(sidecar, {
            "/branch": '{"empty":{},"items":[null,[],{"value":"kept"}],"null":null}',
            "/holder": '{"0":null}',
            "/nested": '{"a.b":{"keep":1}}',
        })
        self.assertEqual(clickhouse.merge_fidelity(native_read, sidecar), original)

    def test_sidecar_uses_root_for_top_level_dot_key_and_rejects_invalid_values(self):
        """防止顶层点号键残留改写结构，或非 canonical 值和深路径进入恢复。"""
        original = {"top.level": {"keep": 1}, "plain": "kept"}
        sidecar = clickhouse.fidelity_values(original)

        self.assertEqual(sidecar, {"": '{"plain":"kept","top.level":{"keep":1}}'})
        self.assertEqual(clickhouse.merge_fidelity({"top": {"level": {"keep": 1}}}, sidecar), original)
        with self.assertRaisesRegex(ValueError, "canonical"):
            clickhouse.merge_fidelity({}, {"/branch": '{"a": 1}'})
        with self.assertRaisesRegex(ValueError, "top-level"):
            clickhouse.merge_fidelity({}, {"/branch/nested": "null"})

    def test_sidecar_round_trips_empty_slash_and_tilde_top_level_keys(self):
        """防止 JSON Pointer 的空键、~0 或 ~1 转义不能闭环恢复。"""
        original = {"": None, "a/b": {}, "a~b": []}
        sidecar = clickhouse.fidelity_values(original)

        self.assertEqual(sidecar, {"/": "null", "/a~1b": "{}", "/a~0b": "[]"})
        self.assertEqual(clickhouse.merge_fidelity({}, sidecar), original)

    def test_sidecar_rejects_invalid_pointer_escapes_and_mixed_root_value(self):
        """防止非 RFC 6901 转义或根值混入其他覆盖路径。"""
        for pointer in ("/bad~2key", "/bad~"):
            with self.subTest(pointer=pointer):
                with self.assertRaisesRegex(ValueError, "JSON Pointer"):
                    clickhouse.merge_fidelity({}, {pointer: "null"})
        with self.assertRaisesRegex(ValueError, "root fidelity"):
            clickhouse.merge_fidelity({}, {"": "{}", "/branch": "null"})

    def test_query_finish_storage_and_cleanup_boundaries(self):
        """防止 QueryFinish、part 路径空间字段或自有 database 清理被遗漏。"""
        connection = mock.MagicMock()
        with mock.patch.object(self.adapter, "connect_worker", return_value=connection), mock.patch.object(
            self.adapter, "_request", side_effect=[
                "", '{"query_id":"query-a","type":"QueryFinish","exception_code":0,"query_duration_ms":1,"read_rows":2,"read_bytes":3,"memory_usage":4,"result_rows":5,"result_bytes":6,"selected_rows":7,"selected_bytes":8}\n',
            ]
        ):
            metrics = self.adapter.collect_query_logs(["query-a"], attempts=1)
        self.assertEqual(metrics["query-a"]["read_bytes"], 3)
        self.assertEqual(set(metrics["query-a"]), set(clickhouse.QUERY_LOG_METRICS))

        self.assertEqual(self.adapter.cleanup("ch_native"), {"database": "jsons2sup_test_ch_native", "removed": False})

    def test_finish_maintenance_can_force_both_tables_to_one_part(self):
        """保证受控比较显式记录分析表和原文表的 OPTIMIZE FINAL。"""
        connection = mock.MagicMock()
        with mock.patch.object(self.adapter, "_merge_backlog", side_effect=[0, 0, 0, 0, 0, 0]), mock.patch.object(
            self.adapter, "connect_worker", return_value=connection
        ), mock.patch.object(self.adapter, "_request", return_value="") as request:
            result = self.adapter.finish_maintenance("ch_native", 30, optimize_final=True)

        self.assertTrue(result["completed"])
        self.assertEqual(set(result["optimize_final_ms"]), {"analytics", "raw"})
        statements = [call.args[1] for call in request.call_args_list]
        self.assertIn("OPTIMIZE TABLE jsons2sup_test_ch_native.analytics FINAL", statements)
        self.assertIn("OPTIMIZE TABLE jsons2sup_test_ch_native.raw FINAL", statements)

    def test_collect_plan_uses_bound_formal_query_and_returns_natural_plan(self):
        """防止正式 runner 收集 ClickHouse 自然计划时缺少 adapter 接口。"""
        connection = mock.MagicMock()
        parameters = {
            "project_id": "project-a",
            "start_time": "2030-01-01T00:00:00.000Z",
            "end_time": "2030-01-01T00:01:00.000Z",
            "operation_name": "chat",
        }
        with mock.patch.object(self.adapter, "connect_worker", return_value=connection), mock.patch.object(
            self.adapter, "_request", return_value="Expression (Projection + Before ORDER BY)\n"
        ) as request:
            result = self.adapter.collect_plan("ch_string", "S02", parameters)

        self.assertEqual(result, {"natural": "Expression (Projection + Before ORDER BY)"})
        statement = request.call_args.args[1]
        self.assertTrue(statement.startswith("EXPLAIN SELECT span_type, count() AS count FROM jsons2sup_test_ch_string.analytics"))
        self.assertIn("JSONExtractString(attributes, 'gen_ai', 'operation', 'name')", statement)
        self.assertEqual(request.call_args.kwargs["parameters"], {
            "project_id": "project-a",
            "start_time": "2030-01-01 00:00:00.000",
            "end_time": "2030-01-01 00:01:00.000",
            "operation_name": "chat",
        })
        connection.close.assert_called_once()


@unittest.skipUnless(os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1", "set RUN_CLICKHOUSE_INTEGRATION=1")
class ClickHouseFourLayoutIntegrationTest(unittest.TestCase):
    """以两组 256 行冻结输入验证 ClickHouse 两种 JSON 存储布局。"""

    INPUT_DIR = STAGE_DIR.parents[1] / "docs/temp/json-storage-stage2/cross-engine-input-20260907"

    def setUp(self):
        """加载确定性 fixture、独立 truth 和临时 database 前缀。"""
        rows, source_truth = common.verify_input(self.INPUT_DIR)
        self.parameters = generator._query_parameters(source_truth)
        self.rows = select_integration_fixture(rows, self.parameters)
        self.assertEqual(len(self.rows), FIXTURE_ROWS)
        self.truth = {
            query_id: generator.query_results(self.rows, query_id, self.parameters[query_id])
            for query_id in common.QUERY_IDS
        }
        self.record_truth = {"records": [{
            "event_id": row["event_id"],
            "analysis_sha256": hashlib.sha256(common.canonical_bytes(common.derived_attributes(row))).hexdigest(),
            "raw_sha256": hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest(),
        } for row in self.rows]}
        self.namespace = f"jsons2sup_{uuid.uuid4().hex[:12]}"
        self.adapter = clickhouse.ClickHouseFourLayoutAdapter(
            "127.0.0.1", 18123, "agent-trace-clickhouse-25-12", self.namespace
        )

    def tearDown(self):
        """始终清理由测试创建的两个临时 database。"""
        for layout in common.LAYOUTS[2:]:
            self.adapter.cleanup(layout)
        self.assertTrue(all(not self.adapter.database_exists(layout) for layout in common.LAYOUTS[2:]))

    def test_string_and_native_json_match_truth_restore_raw_log_and_cleanup(self):
        """捕获查询、Sidecar、摘要、原文、QueryFinish 或清理的布局偏差。"""
        for layout in common.LAYOUTS[2:]:
            with self.subTest(layout=layout):
                self.adapter.create_layout(layout, 32)
                self.assertEqual(self.adapter.insert_block(layout, self.rows[:BLOCK_ROWS])["rows"], BLOCK_ROWS)
                self.assertEqual(self.adapter.insert_block(layout, self.rows[BLOCK_ROWS:])["rows"], BLOCK_ROWS)
                self.assertTrue(self.adapter.finish_maintenance(layout, timeout_seconds=30)["completed"])
                connection = self.adapter.connect_worker()
                query_ids = []
                try:
                    for query_id in common.QUERY_IDS:
                        result = self.adapter.execute_query(connection, layout, query_id, self.parameters[query_id])
                        self.assertEqual(result["result"], self.truth[query_id])
                        self.assertGreaterEqual(result["latency_ms"], 0.0)
                        self.assertGreaterEqual(result["recovery_ms"], 0.0)
                        query_ids.append(result["query_log_id"])
                finally:
                    connection.close()
                self.assertEqual(set(self.adapter.collect_query_logs(query_ids)), set(query_ids))
                self.assertTrue(self.adapter.verify_analysis(layout, self.record_truth)["ok"])
                self.assertTrue(self.adapter.verify_raw(layout, self.record_truth)["ok"])
                storage = self.adapter.collect_storage(layout)
                self.assertEqual(set(storage["tables"]), {"analytics", "raw"})
                self.assertGreater(storage["tables"]["analytics"]["compressed_bytes"], 0)
                if layout == "ch_native":
                    self.assertEqual(
                        sum(bool(clickhouse.fidelity_values(row["attributes_analysis"])) for row in self.rows),
                        19,
                    )
                    self.assertIsInstance(storage["paths"]["dynamic_paths"], list)
                    self.assertIsInstance(storage["paths"]["shared_paths"], list)
                    self.assertIsInstance(storage["paths"]["parts"], list)
                    self.assertTrue(all(set(item) == {"part", "dynamic_paths", "shared_paths"} for item in storage["paths"]["parts"]))
                else:
                    self.assertIsNone(storage["paths"])
                self.adapter.cleanup(layout)
                self.assertFalse(self.adapter.database_exists(layout))

    def test_native_capability_restores_special_json_structures(self):
        """验证真实 Native JSON 回读可由 Sidecar 恢复特殊 JSON 结构。"""
        documents = {
            "capability-branch": {
                "branch": {"null": None, "empty_object": {}, "empty_array": [], "items": [None, {}, []]},
                "nested": {"dot.key": {"keep": 1}},
                "holder": {"0": None},
                "plain": "kept",
            },
            "capability-root": {"top.level": {"keep": 1}, "plain": "kept"},
        }
        rows = [self._capability_row(index, event_id, attributes) for index, (event_id, attributes) in enumerate(documents.items(), 1)]
        self.adapter.create_layout("ch_native", 32)
        self.adapter.insert_block("ch_native", rows)
        connection = self.adapter.connect_worker()
        try:
            actual = self.adapter._json_rows(self.adapter._request(
                connection,
                "SELECT event_id, attributes, fidelity_values FROM "
                + self.adapter._analytics("ch_native") + " ORDER BY event_id FORMAT JSONEachRow",
            ))
        finally:
            connection.close()

        self.assertEqual(len(actual), len(documents))
        actual_by_id = {row["event_id"]: row for row in actual}
        self.assertEqual(actual_by_id["capability-branch"]["fidelity_values"], {
            "/branch": '{"empty_array":[],"empty_object":{},"items":[null,{},[]],"null":null}',
            "/holder": '{"0":null}',
            "/nested": '{"dot.key":{"keep":1}}',
        })
        self.assertEqual(actual_by_id["capability-root"]["fidelity_values"], {
            "": '{"experiment":{"duration_ms":1000},"plain":"kept","top.level":{"keep":1}}',
        })
        for event_id, expected in documents.items():
            expected = common.derived_attributes(self._capability_row(1, event_id, expected))
            native_value = self.adapter._load_attributes(actual_by_id[event_id]["attributes"])
            self.assertNotEqual(native_value, expected)
            self.assertEqual(clickhouse.merge_fidelity(native_value, actual_by_id[event_id]["fidelity_values"]), expected)

    def test_collect_plan_accepts_clickhouse_25_bound_query_syntax(self):
        """验证固定 ClickHouse 25.12 对正式绑定查询的 EXPLAIN 语法。"""
        self.adapter.create_layout("ch_string", 32)

        plan = self.adapter.collect_plan("ch_string", "S02", self.parameters["S02"])

        self.assertIsInstance(plan["natural"], str)
        self.assertTrue(plan["natural"].strip())

    def test_projection_controls_match_s04_semantics_for_both_layouts(self):
        """验证三种输出控制在 ClickHouse 25.12 上保持 S04 值语义。"""
        for layout in common.LAYOUTS[2:]:
            with self.subTest(layout=layout):
                self.adapter.create_layout(layout, 32)
                self.adapter.insert_block(layout, self.rows)
                connection = self.adapter.connect_worker()
                try:
                    for mode in clickhouse.PROJECTION_CONTROL_MODES:
                        result = self.adapter.execute_projection_control(
                            connection, layout, mode, self.parameters["S04"]
                        )
                        if mode == "aggregate":
                            self.assertEqual(
                                result["result"]["non_null_count"],
                                self.truth["S04"]["non_null_count"],
                            )
                        else:
                            self.assertEqual(result["result"], self.truth["S04"])
                        self.assertGreater(result["response_bytes"], 0)
                finally:
                    connection.close()
                self.adapter.cleanup(layout)

    @staticmethod
    def _capability_row(ingest_seq, event_id, attributes):
        """构造仅供隔离 Native JSON 能力测试使用的最小输入记录。"""
        raw_event = common.canonical_bytes({"event_id": event_id, "attributes": attributes}).decode("utf-8")
        return {
            "ingest_seq": ingest_seq,
            "event_id": event_id,
            "trace_id": "capability-trace",
            "span_id": event_id,
            "parent_span_id": None,
            "project_id": "capability-project",
            "start_time": "2030-01-01T00:00:00.000Z",
            "end_time": "2030-01-01T00:00:01.000Z",
            "duration_ms": 1000,
            "span_type": "tool",
            "framework": "capability",
            "level": "INFO",
            "attributes_analysis": attributes,
            "raw_event": raw_event,
        }


if __name__ == "__main__":
    unittest.main()
