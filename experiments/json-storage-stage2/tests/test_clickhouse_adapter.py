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

import clickhouse


class ClickHouseAdapterUnitTest(unittest.TestCase):
    """验证 ClickHouse 三种 residual 布局的固定接口契约。"""

    def test_layout_ddl_uses_distinct_databases_and_expected_residual_types(self):
        """捕获残差列类型或排序键偏离跨引擎记录契约的回归。"""
        string = clickhouse.create_layout_ddl("json_s2_test", "ch_string", 32)
        mapped = clickhouse.create_layout_ddl("json_s2_test", "ch_map", 32)
        native = clickhouse.create_layout_ddl("json_s2_test", "ch_native", 32)

        self.assertIn("CREATE DATABASE json_s2_test_ch_string", string)
        self.assertIn("attributes String CODEC(ZSTD(3))", string)
        self.assertIn("Map(String,String)", mapped)
        self.assertIn("JSON(max_dynamic_paths=32)", native)
        self.assertIn("fidelity_values Map(String,String)", native)
        self.assertNotIn("fidelity_values", string)
        self.assertNotIn("fidelity_values", mapped)
        for ddl in (string, mapped, native):
            self.assertIn("ORDER BY (project_id,start_time,event_id)", ddl)
            self.assertIn("raw_event String CODEC(ZSTD(3))", ddl)

    def test_queries_keep_visibility_and_layout_specific_residual_expressions(self):
        """捕获任一 Q01-Q05 遗漏统一可见性或混用 residual 表达式的回归。"""
        for layout in clickhouse.LAYOUTS:
            for query_id in clickhouse.QUERY_IDS:
                with self.subTest(layout=layout, query_id=query_id):
                    statement = clickhouse.query_sql(layout, query_id)
                    self.assertIn("project_id = {project_id:String}", statement)
                    self.assertIn("start_time >= {start_time:DateTime64(3, 'UTC')}", statement)
                    self.assertIn("start_time < {end_time:DateTime64(3, 'UTC')}", statement)
                    self.assertIn("ingest_seq < {watermark:UInt64}", statement)
        self.assertIn("JSONExtractString(attributes, 'gen_ai', 'operation', 'name')", clickhouse.query_sql("ch_string", "Q02"))
        self.assertIn("attributes['gen_ai.operation.name']", clickhouse.query_sql("ch_map", "Q02"))
        self.assertIn("attributes.gen_ai.operation.name.:String", clickhouse.query_sql("ch_native", "Q02"))
        self.assertIn("JSONExtractString(attributes, 'failure', 'mistake_mode')", clickhouse.query_sql("ch_string", "Q05"))
        self.assertIn("attributes['failure.mistake_mode']", clickhouse.query_sql("ch_map", "Q05"))

    def test_invalid_identifiers_layouts_queries_and_budgets_fail_visibly(self):
        """捕获未经校验的 DDL identifier 或不支持的固定参数回归。"""
        for namespace, layout, budget in (
            ("bad-name", "ch_string", 32),
            ("json_s2", "invalid", 32),
            ("json_s2", "ch_native", 0),
            ("json_s2", "ch_native", 129),
        ):
            with self.subTest(namespace=namespace, layout=layout, budget=budget):
                with self.assertRaises(ValueError):
                    clickhouse.create_layout_ddl(namespace, layout, budget)
        with self.assertRaises(ValueError):
            clickhouse.query_sql("ch_map", "Q99")
        with self.assertRaisesRegex(ValueError, "database name"):
            clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "a" * 63)

    def test_connect_worker_returns_independent_connections_for_workers(self):
        """捕获多个查询 worker 意外共享 HTTPConnection 对象的回归。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        with mock.patch.object(clickhouse.http.client, "HTTPConnection", side_effect=[object(), object()]) as constructor:
            first = adapter.connect_worker()
            second = adapter.connect_worker()

        self.assertIsNot(first, second)
        self.assertEqual(constructor.call_count, 2)

    def test_query_log_collection_polls_until_query_finish_after_complete_body_read(self):
        """捕获 query log 异步发布时将空结果误当成最终指标的回归。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        responses = [
            "",
            "",
            "",
            '{"query_duration_ms":"7","read_rows":"3"}\n',
        ]
        with mock.patch.object(adapter, "_request", side_effect=responses), mock.patch.object(
            clickhouse.time, "sleep"
        ) as sleep:
            metrics = adapter._collect_query_log(object(), "stage2_delayed", attempts=2)

        self.assertEqual(metrics, {"query_duration_ms": 7, "read_rows": 3})
        sleep.assert_called_once()

    def test_finish_maintenance_requires_three_consecutive_zero_backlogs(self):
        """捕获中间出现 merge 后仍把零 backlog 计为连续完成的回归。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        with mock.patch.object(adapter, "_merge_backlog", side_effect=[0, 0, 1, 0, 0, 0]), mock.patch.object(
            clickhouse.time, "sleep"
        ), mock.patch.object(clickhouse.time, "monotonic", side_effect=range(20)):
            result = adapter.finish_maintenance("ch_string", timeout_seconds=20)

        self.assertTrue(result["completed"])
        self.assertEqual(result["observations"], [0, 0, 1, 0, 0, 0])
        self.assertEqual(result["zero_streak"], 3)

    def test_normalization_restores_non_empty_q04_and_sorts_q05_identities(self):
        """捕获 Q04 未恢复原始键、Q05 未排序或摘要不稳定的回归。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        params = {
            "key_map": {
                "failure.mistake_mode": "failure.mistake_mode",
                "gen_ai.operation.name": "gen_ai.operation.name",
                "plain": "plain",
            }
        }
        q04 = adapter._normalize_result("Q04", [{
            "start_time": "2030-01-01 00:00:01.234",
            "event_id": "trace-1:span-1",
            "attributes": {
                "gen_ai": {"operation": {"name": "execute_tool"}},
                "failure": {"mistake_mode": "A.3"},
                "plain": None,
            },
        }], params)
        q05 = adapter._normalize_result("Q05", [{"event_id": "event-c"}, {"event_id": "event-a"}, {"event_id": "event-b"}], {})

        self.assertEqual(q04, [["2030-01-01T00:00:01.234Z", "trace-1:span-1", {
            "failure.mistake_mode": '"A.3"',
            "gen_ai.operation.name": '"execute_tool"',
            "plain": "null",
        }]])
        self.assertEqual(q05, {
            "row_count": 3,
            "identity_sha256": "0a1968429bbd9bb1fc3ecd7c1b260afc63db22a2c9ec5d797e67bb6c15e31b73",
        })
        self.assertEqual(
            hashlib.sha256(json.dumps(q04, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "d94d851ed76fd92742f26ac1fc2c1ba1f920374e449446c712538829fae57ce2",
        )

    def test_map_q04_preserves_already_canonical_values_and_rejects_unknown_keys(self):
        """捕获 Map residual 再次 JSON 序列化或接受非 truth 键的回归。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        params = {"key_map": {"failure.mistake_agent": "failure.mistake_agent", "span.type": "span.type"}}
        rows = [{
            "start_time": "2030-01-01 00:00:01.234",
            "event_id": "trace-1:span-1",
            "attributes": {"failure.mistake_agent": "null", "span.type": '"trace"'},
        }]

        result = adapter._normalize_result("Q04", rows, params, layout="ch_map")

        self.assertEqual(result[0][2], {"failure.mistake_agent": "null", "span.type": '"trace"'})
        with self.assertRaisesRegex(ValueError, "unknown Map attribute key"):
            adapter._normalize_result("Q04", [{**rows[0], "attributes": {"unknown": "null"}}], params, layout="ch_map")

    def test_native_special_companion_preserves_recursive_null_and_empty_containers(self):
        """捕获 native JSON 丢失嵌套 null、空对象或空数组后 Q04 无法恢复的回归。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        source = {
            "plain": "safe",
            "null_value": None,
            "empty_object": {},
            "empty_array": [],
            "nested": {"kept": 1, "lost": {}},
        }
        row = {
            "attributes_analysis": source,
            "attributes_map": {key: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for key, value in source.items()},
        }
        special = adapter._native_special_values(row)
        params = {"key_map": {key: key for key in source}}
        native_rows = [{
            "start_time": "2030-01-01 00:00:01.234",
            "event_id": "trace-1:span-1",
            "attributes": {"plain": "safe", "nested": {"kept": 1}},
            "fidelity_values": special,
        }]

        result = adapter._normalize_result("Q04", native_rows, params, layout="ch_native")

        self.assertEqual(set(special), {"null_value", "empty_object", "empty_array", "nested"})
        self.assertEqual(result[0][2], {
            "empty_array": "[]",
            "empty_object": "{}",
            "nested": '{"kept":1,"lost":{}}',
            "null_value": "null",
            "plain": '"safe"',
        })


@unittest.skipUnless(os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1", "set RUN_CLICKHOUSE_INTEGRATION=1")
class ClickHouseAdapterIntegrationTest(unittest.TestCase):
    """以正式输入前 512 行验证 ClickHouse adapter 的三布局边界。"""

    INPUT_DIR = STAGE_DIR.parents[1] / "docs/temp/json-storage-stage2/cross-engine-input-20260907"

    def setUp(self):
        """读取正式 fixture 前缀，并建立带随机前缀的 adapter。"""
        import common

        rows, truth = common.verify_input(self.INPUT_DIR)
        self.rows = rows[:512]
        self.assertEqual(len(self.rows), 512)
        self.truth = self._fixture_truth(truth, 512)
        self.namespace = f"jsons2_{uuid.uuid4().hex[:12]}"
        self.adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "agent-trace-clickhouse-25-12", self.namespace)

    def tearDown(self):
        """无论测试成功或失败都清理每个 layout database 并验证无残留。"""
        for layout in clickhouse.LAYOUTS:
            self.adapter.cleanup(layout)
        self.assertTrue(all(not self.adapter.database_exists(layout) for layout in clickhouse.LAYOUTS))

    def test_three_layouts_match_truth_raw_query_log_paths_maintenance_and_cleanup(self):
        """捕获查询、原文、query log、路径、维护或清理的任一布局偏差。"""
        budget = self.truth["native_json"]["path_budget"]
        for layout in clickhouse.LAYOUTS:
            with self.subTest(layout=layout):
                self.adapter.create_layout(layout, budget)
                self.adapter.insert_block(layout, self.rows[:256])
                self.adapter.insert_block(layout, self.rows[256:])
                maintenance = self.adapter.finish_maintenance(layout, timeout_seconds=30)
                self.assertTrue(maintenance["completed"])

                connection = self.adapter.connect_worker()
                try:
                    for query_id in clickhouse.QUERY_IDS:
                        params = {**self.truth["parameters"][query_id], "key_map": self.truth["key_map"]}
                        actual = self.adapter.execute_query(connection, layout, query_id, params, 512)
                        expected = self.truth["queries"][query_id]["512"]
                        self.assertEqual(actual["result_sha256"], expected["result_sha256"])
                        self.assertEqual(actual["row_count"], expected["row_count"])
                        self.assertGreaterEqual(actual["latency_ms"], 0.0)
                        self.assertIn("query_log", actual)
                finally:
                    connection.close()

                self.assertTrue(self.adapter.verify_raw(layout, self.truth)["ok"])
                storage = self.adapter.collect_storage(layout)
                self.assertEqual(set(storage["tables"]), {"analytics", "raw"})
                self.assertGreater(storage["tables"]["analytics"]["compressed_bytes"], 0)
                if layout == "ch_native":
                    paths = storage["paths"]
                    self.assertEqual(budget, 32)
                    candidates = self._native_path_candidates(self.rows)
                    special_paths = set().union(*(self.adapter._native_special_values(row) for row in self.rows))
                    self.assertEqual(len(candidates), 23)
                    self.assertEqual(set(paths["dynamic_paths"]).union(paths["shared_paths"]), candidates - special_paths)
                    self.assertLessEqual(len(paths["dynamic_paths"]), budget)
                    self.assertFalse(set(paths["dynamic_paths"]).intersection(paths["shared_paths"]))
                else:
                    self.assertIsNone(storage["paths"])
                self.adapter.cleanup(layout)
                self.assertFalse(self.adapter.database_exists(layout))

    def test_raw_failure_leaves_visible_partial_write_and_raises(self):
        """捕获 raw 写入失败时 adapter 误报整块成功或掩盖部分写入的回归。"""
        layout = "ch_string"
        row = self.rows[256]
        try:
            self.adapter.create_layout(layout, 32)
            connection = self.adapter.connect_worker()
            try:
                self.adapter._request(connection, "ALTER TABLE " + self.adapter._raw(layout) + " MODIFY COLUMN raw_event FixedString(1)")
            finally:
                connection.close()
            with self.assertRaises(RuntimeError):
                self.adapter.insert_block(layout, [row])
            connection = self.adapter.connect_worker()
            try:
                rows = self.adapter._json_rows(self.adapter._request(connection, "SELECT count() AS rows FROM " + self.adapter._analytics(layout) + " FORMAT JSONEachRow"))
            finally:
                connection.close()
            self.assertEqual(rows[0]["rows"], 1)
        finally:
            self.adapter.cleanup(layout)
            self.assertFalse(self.adapter.database_exists(layout))

    @staticmethod
    def _fixture_truth(truth, watermark):
        """复制 fixture 前缀需要的不可变 truth 字段。"""
        return {
            "key_map": truth["key_map"],
            "native_json": truth["native_json"],
            "parameters": truth["parameters"],
            "queries": {query_id: {str(watermark): truth["queries"][query_id][str(watermark)]} for query_id in clickhouse.QUERY_IDS},
            "records": truth["records"][:watermark],
        }

    @staticmethod
    def _native_path_candidates(rows):
        """从冻结 rows 的 nested analysis 推导 native JSON 的非容器候选叶路径。"""
        paths = set()

        def visit(value, prefix):
            if isinstance(value, dict):
                for key, nested in value.items():
                    visit(nested, prefix + [key])
            else:
                paths.add(".".join(prefix))

        for row in rows:
            visit(row["attributes_analysis"], [])
        return paths


@unittest.skipUnless(os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1", "set RUN_CLICKHOUSE_INTEGRATION=1")
class ClickHouseAdapterRepresentativeTraceIntegrationTest(unittest.TestCase):
    """验证正式代表 trace 的非空 Q04 和非空 Q05 恢复契约。"""

    INPUT_DIR = STAGE_DIR.parents[1] / "docs/temp/json-storage-stage2/cross-engine-input-20260907"

    def setUp(self):
        """仅流式保留正式 Q04 代表 trace 的六行及 Q05 命中行。"""
        self.truth = json.loads((self.INPUT_DIR / "truth-manifest.json").read_bytes())
        q04 = self.truth["parameters"]["Q04"]
        q05 = self.truth["parameters"]["Q05"]
        self.q04_rows = []
        self.q05_rows = []
        self.special_counts = {"null": 0, "empty_array": 0, "empty_object": 0, "nested_empty_object": 0}
        q05_value = json.dumps(q05["failure_mistake_mode"], ensure_ascii=False, separators=(",", ":"))
        with (self.INPUT_DIR / "dataset.jsonl").open(encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if row["trace_id"] == q04["trace_id"]:
                    self.q04_rows.append(row)
                for value in row["attributes_map"].values():
                    attribute = json.loads(value)
                    if attribute is None:
                        self.special_counts["null"] += 1
                    elif attribute == {}:
                        self.special_counts["empty_object"] += 1
                    elif attribute == []:
                        self.special_counts["empty_array"] += 1
                    self.special_counts["nested_empty_object"] += self._nested_empty_objects(attribute)
                if (
                    row["project_id"] == q05["project_id"]
                    and q05["start_time"] <= row["start_time"] < q05["end_time"]
                    and row["attributes_map"].get("failure.mistake_mode") == q05_value
                ):
                    self.q05_rows.append(row)
        self.assertEqual(len(self.q04_rows), 6)
        self.assertTrue(self.q05_rows)
        self.namespace = f"jsons2_{uuid.uuid4().hex[:12]}"
        self.adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "agent-trace-clickhouse-25-12", self.namespace)

    def tearDown(self):
        """删除代表 trace 测试创建的全部 database。"""
        for layout in clickhouse.LAYOUTS:
            self.adapter.cleanup(layout)
        self.assertTrue(all(not self.adapter.database_exists(layout) for layout in clickhouse.LAYOUTS))

    def test_three_layouts_match_final_non_empty_q04_and_q05_truth(self):
        """捕获只有正式非空 Q04/Q05 才可见的 residual 恢复偏差。"""
        watermark = self.truth["record_count"]
        for layout in clickhouse.LAYOUTS:
            with self.subTest(layout=layout):
                self.adapter.create_layout(layout, self.truth["native_json"]["path_budget"])
                self.adapter.insert_block(layout, self.q04_rows)
                connection = self.adapter.connect_worker()
                try:
                    q04 = self.adapter.execute_query(connection, layout, "Q04", {**self.truth["parameters"]["Q04"], "key_map": self.truth["key_map"]}, watermark)
                finally:
                    connection.close()
                self.assertEqual(q04["result_sha256"], self.truth["queries"]["Q04"][str(watermark)]["result_sha256"])
                self.adapter.cleanup(layout)

                self.adapter.create_layout(layout, self.truth["native_json"]["path_budget"])
                self.adapter.insert_block(layout, self.q05_rows)
                connection = self.adapter.connect_worker()
                try:
                    q05 = self.adapter.execute_query(connection, layout, "Q05", {**self.truth["parameters"]["Q05"], "key_map": self.truth["key_map"]}, watermark)
                finally:
                    connection.close()
                self.assertEqual(q05["result_sha256"], self.truth["queries"]["Q05"][str(watermark)]["result_sha256"])

    def test_frozen_dataset_special_values_cover_native_fidelity_companion(self):
        """捕获正式输入特殊值变化后 native fidelity_values 门禁失效的回归。"""
        self.assertEqual(self.special_counts, {
            "null": 5446,
            "empty_object": 9,
            "empty_array": 12623,
            "nested_empty_object": 1,
        })

    @staticmethod
    def _nested_empty_objects(value):
        """统计 Attribute 值内部的空对象，顶层空对象由调用方单独计数。"""
        if isinstance(value, dict):
            return sum(
                (1 if nested == {} else 0) + ClickHouseAdapterRepresentativeTraceIntegrationTest._nested_empty_objects(nested)
                for nested in value.values()
            )
        if isinstance(value, list):
            return sum(ClickHouseAdapterRepresentativeTraceIntegrationTest._nested_empty_objects(item) for item in value)
        return 0


if __name__ == "__main__":
    unittest.main()
