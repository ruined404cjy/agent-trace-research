import hashlib
import json
import os
import sys
import unittest
import urllib.parse
import uuid
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import clickhouse


class FakeHTTPConnection:
    """记录真实 adapter 发出的 HTTP 请求，并提供明确的服务端响应。"""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def request(self, method, path, body, headers):
        self.requests.append((path, body.decode("utf-8")))

    def getresponse(self):
        response = mock.Mock(status=200)
        response.read.return_value = next(self.responses).encode("utf-8")
        return response

    def close(self):
        pass


def query_log_row(query_id, **overrides):
    """返回完整 QueryFinish fixture，覆盖指标缺失或异常测试。"""
    return {
        "query_id": query_id, "type": "QueryFinish", "exception_code": 0,
        "query_duration_ms": 7, "read_rows": 3, "read_bytes": 30,
        "memory_usage": 100, "result_rows": 1, "result_bytes": 10,
        "selected_rows": 3, "selected_bytes": 30, **overrides,
    }


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
        """阻止 worker 共享连接或把首次 TCP 建连纳入查询计时。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        connections = [mock.Mock(), mock.Mock()]
        with mock.patch.object(clickhouse.http.client, "HTTPConnection", side_effect=connections) as constructor:
            first = adapter.connect_worker()
            second = adapter.connect_worker()

        self.assertIsNot(first, second)
        self.assertEqual(constructor.call_count, 2)
        for connection in connections:
            connection.connect.assert_called_once_with()
        params = {"project_id": "project", "start_time": "2030-01-01T00:00:00.000Z", "end_time": "2030-01-02T00:00:00.000Z"}
        ticks = iter((1.0, 1.001, 2.0, 2.001))

        def query_clock():
            first.connect.assert_called_once_with()
            return next(ticks)

        with mock.patch.object(adapter, "_request", return_value=""), mock.patch.object(
            clickhouse.time, "perf_counter", side_effect=query_clock
        ):
            for _ in range(2):
                sample = adapter.execute_query(first, "ch_string", "Q01", params, 512)
                self.assertAlmostEqual(sample["latency_ms"], 1.0)
        first.connect.assert_called_once_with()

    def test_failed_preconnect_closes_connection_and_preserves_error(self):
        """阻止连接失败的 worker 带着未关闭 socket 进入阶段。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        connection = mock.Mock()
        failure = OSError("connect failed")
        connection.connect.side_effect = failure
        with mock.patch.object(clickhouse.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(OSError) as raised:
                adapter.connect_worker()
        self.assertIs(raised.exception, failure)
        connection.close.assert_called_once_with()

    def test_partial_ddl_cleans_only_database_created_by_current_call(self):
        """阻止建表失败遗留自有 database，或 CREATE DATABASE 失败后误删他人对象。"""
        for failed_statement, cleanup_fails in ((0, False), (1, False), (2, False), (1, True)):
            with self.subTest(failed_statement=failed_statement, cleanup_fails=cleanup_fails):
                adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
                state = {"exists": False, "creates": 0, "drops": 0}
                failure = RuntimeError("DDL failed")

                def request(_connection, statement):
                    if statement.startswith("DROP DATABASE"):
                        state["drops"] += 1
                        if cleanup_fails:
                            raise RuntimeError("DROP failed")
                        state["exists"] = False
                    else:
                        index = state["creates"]
                        state["creates"] += 1
                        state["exists"] = True
                        if index == failed_statement:
                            raise failure
                    return ""

                with mock.patch.object(adapter, "database_exists", side_effect=lambda _layout: state["exists"]), mock.patch.object(
                    adapter, "connect_worker", return_value=mock.Mock()
                ), mock.patch.object(adapter, "_request", side_effect=request):
                    with self.assertRaises(RuntimeError) as raised:
                        adapter.create_layout("ch_string", 32)
                self.assertIs(raised.exception, failure)
                self.assertEqual(state["drops"], 0 if failed_statement == 0 else 1)
                self.assertEqual(state["exists"], failed_statement == 0 or cleanup_fails)
                if cleanup_fails:
                    self.assertIn("DROP failed", " ".join(getattr(failure, "__notes__", [])))

    def test_measurement_defers_logs_and_management_requests_disable_observer_logs(self):
        """阻止测量逐条 flush 或管理请求继续放大 system logs。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        connection = FakeHTTPConnection(['{"span_type":"tool","count":3}\n', "", '{"query_duration_ms":7,"read_rows":3}\n', ""])
        params = {"project_id": "project", "start_time": "2030-01-01T00:00:00.000Z", "end_time": "2030-01-02T00:00:00.000Z"}
        actual = adapter.execute_query(connection, "ch_string", "Q01", params, 512)
        self.assertEqual(actual["result"], [["tool", 3]])
        self.assertIn("query_log_id", actual)
        self.assertNotIn("query_log", actual)
        self.assertEqual(len(connection.requests), 1)
        adapter._request(connection, "SELECT 1")
        for index, (path, statement) in enumerate(connection.requests):
            settings = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            self.assertEqual(settings["log_queries"], ["1" if index == 0 else "0"])
            for setting in ("log_processors_profiles", "memory_profiler_step", "log_query_settings"):
                self.assertEqual(settings[setting], ["0"])
            self.assertNotIn("SETTINGS", statement)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(connection.requests[0][0]).query)["param_project_id"], ["project"])

    def test_batch_query_logs_flush_once_and_poll_missing_finish(self):
        """阻止批量两样本按查询或按轮询重复 flush。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        connection = FakeHTTPConnection(["", "", "\n".join(json.dumps(query_log_row(query_id)) for query_id in ("first", "second"))])
        self.assertTrue(callable(getattr(adapter, "collect_query_logs", None)))
        with mock.patch.object(adapter, "connect_worker", return_value=connection), mock.patch.object(clickhouse.time, "sleep"):
            actual = adapter.collect_query_logs(["first", "second"], attempts=2)
        self.assertEqual(set(actual), {"first", "second"})
        self.assertEqual(actual["first"]["read_rows"], 3)
        self.assertEqual([statement for _path, statement in connection.requests if statement.startswith("SYSTEM")], ["SYSTEM FLUSH LOGS query_log"])
        self.assertTrue(all(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["log_queries"] == ["0"] for path, _statement in connection.requests))

    def test_batch_query_logs_reject_missing_duplicate_and_invalid_finish(self):
        """阻止缺失、重复、异常结束或坏指标静默进入统计。"""
        cases = [[], [query_log_row("first"), query_log_row("first")],
                 [query_log_row("first", type="ExceptionWhileProcessing")],
                 [query_log_row("first", exception_code=241)],
                 [query_log_row("first", read_rows=-1)],
                 [query_log_row("first", memory_usage="bad")],
                 [query_log_row("first", selected_bytes=None)],
                 [query_log_row("first", result_rows=True)],
                 [query_log_row("first", result_rows=1.5)],
                 [{key: value for key, value in query_log_row("first").items() if key != "read_bytes"}]]
        for rows in cases:
            with self.subTest(rows=rows):
                adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
                self.assertTrue(callable(getattr(adapter, "collect_query_logs", None)))
                connection = FakeHTTPConnection(["", "\n".join(json.dumps(row) for row in rows)])
                with mock.patch.object(adapter, "connect_worker", return_value=connection):
                    with self.assertRaises(RuntimeError):
                        adapter.collect_query_logs(["first"], attempts=1)

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

    def test_verify_analysis_restores_map_values_and_reports_tampering(self):
        """阻止 Map analytics 篡改或 identity 偏差绕过 canonical analysis 门禁。"""
        adapter = clickhouse.ClickHouseAdapter("127.0.0.1", 18123, "unused", "json_s2_test")
        expected = {"a": {"b": 1}}
        truth = {
            "key_map": {"a.b": "a.b"},
            "records": [{
                "event_id": "event-1",
                "analysis_sha256": hashlib.sha256(clickhouse.canonical_bytes(expected)).hexdigest(),
            }],
        }
        connection = mock.MagicMock()
        with mock.patch.object(adapter, "connect_worker", return_value=connection), mock.patch.object(
            adapter, "_request", return_value='{"event_id":"event-1","attributes":{"a.b":"1"}}\n'
        ):
            passing = adapter.verify_analysis("ch_map", truth)
        self.assertTrue(passing["ok"])
        self.assertEqual(passing["analysis_sha256_mismatches"], [])

        with mock.patch.object(adapter, "connect_worker", return_value=connection), mock.patch.object(
            adapter, "_request", return_value='{"event_id":"event-1","attributes":{"a.b":"2"}}\n{"event_id":"extra","attributes":{"a.b":"1"}}\n'
        ):
            tampered = adapter.verify_analysis("ch_map", truth)
        self.assertFalse(tampered["ok"])
        self.assertEqual(tampered["extra"], ["extra"])
        self.assertEqual(tampered["analysis_sha256_mismatches"], ["event-1"])


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
                query_ids = []
                try:
                    for query_id in clickhouse.QUERY_IDS:
                        params = {**self.truth["parameters"][query_id], "key_map": self.truth["key_map"]}
                        actual = self.adapter.execute_query(connection, layout, query_id, params, 512)
                        expected = self.truth["queries"][query_id]["512"]
                        self.assertEqual(actual["result_sha256"], expected["result_sha256"])
                        self.assertEqual(actual["row_count"], expected["row_count"])
                        self.assertGreaterEqual(actual["latency_ms"], 0.0)
                        query_ids.append(actual["query_log_id"])
                finally:
                    connection.close()
                metrics = self.adapter.collect_query_logs(query_ids)
                self.assertEqual(set(metrics), set(query_ids))
                self.assertTrue(all(set(value) == set(clickhouse.QUERY_LOG_METRICS) for value in metrics.values()))

                self.assertTrue(self.adapter.verify_raw(layout, self.truth)["ok"])
                self.assertTrue(self.adapter.verify_analysis(layout, self.truth)["ok"])
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

    def test_live_static_stress_collects_all_105_query_logs_in_one_batch(self):
        """阻止 100 次正式查询扩大为逐请求日志 flush，并验证真实 QueryFinish 完整性。"""
        import run_cross_engine

        layout = "ch_string"
        before = self._system_log_snapshot()
        self.adapter.create_layout(layout, 32)
        self.adapter.insert_block(layout, self.rows)
        with mock.patch.object(self.adapter, "_request", wraps=self.adapter._request) as request, mock.patch.object(
            self.adapter, "collect_query_logs", wraps=self.adapter.collect_query_logs
        ) as collect:
            result = run_cross_engine.run_static_queries(
                run_cross_engine._LayoutSession(self.adapter, layout), layout,
                self.truth, measurements=20, watermark=512,
            )
        self.assertEqual(len(result["samples"]), 100)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(len(collect.call_args.args[0]), 105)
        flushes = [call.args[1] for call in request.call_args_list if call.args[1].startswith("SYSTEM")]
        self.assertEqual(flushes, ["SYSTEM FLUSH LOGS query_log"])
        self.assertTrue(all(sample["ok"] and set(sample["query_log"]) == set(clickhouse.QUERY_LOG_METRICS) for sample in result["samples"]))
        self.adapter.cleanup(layout)
        self.assertFalse(self.adapter.database_exists(layout))
        after = self._system_log_snapshot()
        print("STAGE2_QUERY_LOG_STRESS " + json.dumps({
            "before": before, "after": after, "measurement_count": 100,
            "query_finish_count": 105, "targeted_flush_count": len(flushes),
            "cleanup": True,
        }, sort_keys=True), flush=True)

    def _system_log_snapshot(self):
        """读取压力测试前后的 system log active part、字节与 merge/memory 状态。"""
        connection = self.adapter.connect_worker()
        try:
            statements = {
                "parts": "SELECT table, count() AS parts, sum(bytes_on_disk) AS bytes FROM system.parts WHERE active AND database = 'system' GROUP BY table ORDER BY table FORMAT JSONEachRow",
                "metrics": "SELECT metric, value FROM system.metrics WHERE metric IN ('QueriesMemoryUsage','MergesMutationsMemoryTracking','Merge') ORDER BY metric FORMAT JSONEachRow",
                "memory": "SELECT metric, value FROM system.asynchronous_metrics WHERE metric IN ('MemoryResident','MemoryTracking') ORDER BY metric FORMAT JSONEachRow",
            }
            return {name: self.adapter._json_rows(self.adapter._request(connection, statement)) for name, statement in statements.items()}
        finally:
            connection.close()

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
