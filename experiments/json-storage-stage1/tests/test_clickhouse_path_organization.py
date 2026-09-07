import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
GENERATOR = STAGE_DIR / "generator" / "generate_path_profile.py"
RUNNER = STAGE_DIR / "clickhouse" / "run_path_organization.py"
CONTAINER = "agent-trace-clickhouse-25-12"


def load_runner():
    """从脚本路径加载 runner，供单元测试校验 SQL 契约。"""
    spec = importlib.util.spec_from_file_location("clickhouse_path_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClickHousePathOrganizationUnitTest(unittest.TestCase):
    """固定三种布局与两类查询的机制差异。"""

    def test_layouts_fix_dynamic_path_budgets_and_hot_hints(self):
        runner = load_runner()
        ddls = runner.create_table_ddls("json_stage1_test")

        self.assertIn("start_time DateTime64(3, 'UTC')", ddls["string"])
        self.assertIn("ORDER BY (start_time,event_id)", ddls["string"])
        self.assertIn("String CODEC(ZSTD(3))", ddls["string"])
        self.assertIn("JSON(max_dynamic_paths=100)", ddls["native_limited"])
        self.assertIn("max_dynamic_paths=1000", ddls["native_hinted"])
        self.assertIn("hot.tenant String", ddls["native_hinted"])
        self.assertIn("hot.region String", ddls["native_hinted"])
        self.assertNotIn("metadata_raw", ddls["string"])
        self.assertIn("metadata_raw String CODEC(ZSTD(3))", ddls["native_limited"])
        self.assertIn("metadata_raw String CODEC(ZSTD(3))", ddls["native_hinted"])
        for ddl in ddls.values():
            self.assertIn("object_shared_data_serialization_version='advanced'", ddl)
            self.assertIn(
                "object_shared_data_serialization_version_for_zero_level_parts='map_with_buckets'",
                ddl,
            )

    def test_query_specs_keep_string_parsing_and_native_subcolumns_distinct(self):
        runner = load_runner()
        hot = {
            "query_id": "hot_tenant_equals",
            "parameters": {"path": "hot.tenant", "value": "tenant-03"},
        }
        cold = {
            "query_id": "cold_path_equals",
            "parameters": {"path": "p00499", "value": "v00"},
        }

        self.assertIn("JSONExtractString", runner.query_spec("string", hot))
        self.assertIn("JSONExtractString", runner.query_spec("string", cold))
        self.assertEqual(
            runner.query_spec("native_limited", hot),
            "metadata.hot.tenant.:String = 'tenant-03'",
        )
        self.assertEqual(
            runner.query_spec("native_hinted", hot),
            "metadata.hot.tenant = 'tenant-03'",
        )
        self.assertEqual(
            runner.query_spec("native_hinted", cold),
            "metadata.paths.p00499.:String = 'v00'",
        )

    def test_path_inventory_deduplicates_paths_across_rows(self):
        """路径数量统计应计算全集，不能重复累计每行返回的数组。"""
        runner = load_runner()
        statement = runner.path_inventory_statement("json_stage1_test.events_native_limited")

        self.assertIn("arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(metadata))))", statement)
        self.assertIn("arrayDistinct(arrayFlatten(groupArray(JSONSharedDataPaths(metadata))))", statement)

    def test_layout_order_requires_each_layout_exactly_once(self):
        """捕获重复或缺失布局导致跨轮顺序控制失效的错误。"""
        runner = load_runner()

        self.assertEqual(
            runner.parse_layout_order("native_limited,native_hinted,string"),
            ("native_limited", "native_hinted", "string"),
        )
        with self.assertRaises(ValueError):
            runner.parse_layout_order("string,native_limited,native_limited")

    def test_performance_query_is_time_scoped_and_returns_small_aggregation(self):
        """捕获性能计时仍包含完整 ID 排序与传输的错误。"""
        runner = load_runner()
        query = {
            "query_id": "hot_tenant_equals",
            "parameters": {"path": "hot.tenant", "value": "tenant-03"},
        }
        window = {
            "start_inclusive": "2026-01-01T00:00:00.250Z",
            "end_exclusive": "2026-01-01T00:00:00.750Z",
        }

        statement = runner.performance_statement(
            "native_limited",
            "json_stage1_test.events_native_limited",
            query,
            window,
        )

        self.assertIn("start_time >= toDateTime64('2026-01-01 00:00:00.250', 3, 'UTC')", statement)
        self.assertIn("start_time < toDateTime64('2026-01-01 00:00:00.750', 3, 'UTC')", statement)
        self.assertIn("count() AS rows", statement)
        self.assertIn("GROUP BY group_key", statement)
        self.assertNotIn("SELECT event_id", statement)
        self.assertNotIn("ORDER BY event_id", statement)

    def test_full_object_query_forces_content_read_for_string_rebuild_and_sidecar(self):
        """捕获 String 仅读取 offset、与 native 对象重建工作量不等的错误。"""
        runner = load_runner()
        window = {
            "start_inclusive": "2026-01-01T00:00:00.250Z",
            "end_exclusive": "2026-01-01T00:00:00.750Z",
        }

        string_sql = runner.full_object_statement(
            "string",
            "json_stage1_test.events_string",
            window,
            "storage",
        )
        rebuild_sql = runner.full_object_statement(
            "native_limited",
            "json_stage1_test.events_native_limited",
            window,
            "storage",
        )
        sidecar_sql = runner.full_object_statement(
            "native_limited",
            "json_stage1_test.events_native_limited",
            window,
            "fidelity",
        )

        for statement in (string_sql, rebuild_sql, sidecar_sql):
            self.assertIn("sum(cityHash64(", statement)
            self.assertIn("start_time >=", statement)
            self.assertNotIn("sum(length(", statement)
        self.assertIn("cityHash64(metadata)", string_sql)
        self.assertIn("cityHash64(toJSONString(metadata))", rebuild_sql)
        self.assertIn("cityHash64(metadata_raw)", sidecar_sql)

    def test_query_log_statement_reads_completed_server_metrics(self):
        """捕获继续使用响应开始阶段的不完整 HTTP summary 的错误。"""
        runner = load_runner()
        statement = runner.query_log_statement("stage1_0123456789abcdef")

        self.assertIn("type = 'QueryFinish'", statement)
        self.assertIn("query_duration_ms", statement)
        self.assertIn("read_rows", statement)
        self.assertIn("read_bytes", statement)
        self.assertIn("memory_usage", statement)
        self.assertIn("ProfileEvents['SelectedRows']", statement)

    def test_query_log_collection_waits_for_eventual_query_finish(self):
        """捕获查询已完成但日志异步发布导致的偶发空结果。"""
        runner = load_runner()
        responses = [
            ("", None, 0.0),
            ("", None, 0.0),
            ("", None, 0.0),
            ('{"query_duration_ms":"7"}\n', None, 0.0),
        ]

        with mock.patch.object(runner, "http_query", side_effect=responses), mock.patch.object(
            runner.time,
            "sleep",
        ) as sleep:
            metrics = runner.collect_query_log("127.0.0.1", 18123, "stage1_delayed")

        self.assertEqual(metrics, {"query_duration_ms": 7})
        sleep.assert_called_once()

    def test_inventory_gate_catches_first_path_over_budget_and_wrong_priority(self):
        """捕获 99+2 边界未入 shared 或低密度路径挤占子列的错误。"""
        runner = load_runner()
        occurrences = {"hot.region": 100, "hot.tenant": 100}
        occurrences.update({f"paths.p{index:05d}": 20 for index in range(98)})
        occurrences["paths.p00098"] = 1
        valid = {
            "dynamic": 100,
            "dynamic_paths": ["hot.region", "hot.tenant"]
            + [f"paths.p{index:05d}" for index in range(98)],
            "shared": 1,
            "shared_paths": ["paths.p00098"],
        }

        self.assertTrue(
            runner.validate_path_inventory("native_limited", valid, occurrences)["passed"]
        )
        no_shared = dict(
            valid,
            dynamic=101,
            dynamic_paths=valid["dynamic_paths"] + ["paths.p00098"],
            shared=0,
            shared_paths=[],
        )
        self.assertFalse(
            runner.validate_path_inventory("native_limited", no_shared, occurrences)["passed"]
        )
        wrong_priority = dict(
            valid,
            dynamic_paths=valid["dynamic_paths"][:-1] + ["paths.p00098"],
            shared_paths=["paths.p00097"],
        )
        self.assertFalse(
            runner.validate_path_inventory("native_limited", wrong_priority, occurrences)["passed"]
        )

    def test_roundtrip_identity_audit_rejects_missing_extra_and_duplicate_ids(self):
        """捕获合法重复行抵消缺行后保真门禁仍通过的错误。"""
        runner = load_runner()

        audit = runner.audit_identities(
            ["event-a", "event-a", "event-extra"],
            {"event-a", "event-b"},
        )

        self.assertEqual(audit["duplicate_count"], 1)
        self.assertEqual(audit["duplicate_sample"], ["event-a"])
        self.assertEqual(audit["extra_count"], 1)
        self.assertEqual(audit["extra_sample"], ["event-extra"])
        self.assertEqual(audit["missing_count"], 1)
        self.assertEqual(audit["missing_sample"], ["event-b"])

    def test_inventory_gate_records_strict_density_uplift_from_first_seen_paths(self):
        """捕获合并结果仍可由首次出现路径占满预算解释的错误。"""
        runner = load_runner()
        low_paths = [f"paths.low{index:03d}" for index in range(99)]
        occurrences = {"hot.region": 100, "hot.tenant": 100, "paths.high": 95}
        occurrences.update({path: 1 for path in low_paths})
        first_seen = ["hot.region", "hot.tenant", *low_paths, "paths.high"]
        inventory = {
            "dynamic": 100,
            "dynamic_paths": [
                "hot.region",
                "hot.tenant",
                "paths.high",
                *low_paths[:97],
            ],
            "shared": 2,
            "shared_paths": low_paths[97:],
        }

        validation = runner.validate_path_inventory(
            "native_limited",
            inventory,
            occurrences,
            first_seen_order=first_seen,
            require_strict_uplift=True,
        )

        self.assertTrue(validation["passed"])
        self.assertEqual(validation["entered_dynamic_count"], 1)
        self.assertEqual(validation["exited_dynamic_count"], 1)
        self.assertEqual(validation["min_entered_occurrences"], 95)
        self.assertEqual(validation["max_exited_occurrences"], 1)
        self.assertTrue(validation["strict_uplift_passed"])

    def test_failed_rerun_removes_stale_complete_manifest_before_input_checks(self):
        """捕获输入校验失败后旧完成标记仍留在输出目录的错误。"""
        with tempfile.TemporaryDirectory() as parent:
            parent_path = Path(parent)
            output = parent_path / "output"
            output.mkdir()
            manifest = output / "manifest.json"
            manifest.write_text('{"status":"complete"}\n', encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--input",
                    str(parent_path / "missing-input"),
                    "--output",
                    str(output),
                    "--container-name",
                    CONTAINER,
                    "--database-name",
                    "json_stale_manifest_test",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(manifest.exists())


@unittest.skipUnless(
    os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1",
    "设置 RUN_CLICKHOUSE_INTEGRATION=1 后运行真实 ClickHouse 集成测试",
)
class ClickHousePathOrganizationIntegrationTest(unittest.TestCase):
    """验证 ClickHouse String、动态子列和 shared data 的实验门禁。"""

    def test_raw_sidecar_preserves_empty_objects_without_blocking_path_analysis(self):
        """native JSON 可继续分析，canonical sidecar 必须保持完整文档。"""
        database = f"json_empty_object_test_{os.getpid()}"
        with tempfile.TemporaryDirectory() as parent:
            parent_path = Path(parent)
            input_dir = parent_path / "input"
            output_dir = parent_path / "output"
            subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    str(input_dir),
                    "--path-count",
                    "50",
                    "--density-percent",
                    "20",
                    "--count",
                    "100",
                    "--seed",
                    "20260902",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir),
                    "--container-name",
                    CONTAINER,
                    "--database-name",
                    database,
                    "--http-port",
                    "18123",
                    "--measurements",
                    "5",
                    "--insert-chunks",
                    "2",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["gates"],
            {"analysis_equivalence": "pass", "document_fidelity": "pass"},
        )
        self.assertEqual(manifest["status"], "complete")
        for layout_name in ("native_limited", "native_hinted"):
            layout = result["layouts"][layout_name]
            self.assertEqual(layout["storage_roundtrip"]["hash_mismatch_count"], 31)
            self.assertEqual(layout["fidelity_roundtrip"]["hash_mismatch_count"], 0)
            self.assertEqual(layout["fidelity_roundtrip"]["source_column"], "metadata_raw")

        string_layout = result["layouts"]["string"]
        self.assertEqual(string_layout["storage_roundtrip"]["hash_mismatch_count"], 0)
        self.assertEqual(string_layout["fidelity_roundtrip"]["hash_mismatch_count"], 0)
        self.assertEqual(string_layout["fidelity_roundtrip"]["source_column"], "metadata")

    def test_three_layouts_match_truth_and_cleanup_database(self):
        database = f"json_path_test_{os.getpid()}"
        with tempfile.TemporaryDirectory() as parent:
            parent_path = Path(parent)
            input_dir = parent_path / "input"
            output_dir = parent_path / "output"
            subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    str(input_dir),
                    "--path-count",
                    "500",
                    "--density-percent",
                    "20",
                    "--count",
                    "200",
                    "--seed",
                    "20260902",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output_dir),
                    "--container-name",
                    CONTAINER,
                    "--database-name",
                    database,
                    "--http-port",
                    "18123",
                    "--measurements",
                    "5",
                    "--insert-chunks",
                    "2",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["storage"], {"engine": "MergeTree", "json_type": "JSON"})
        self.assertIn("25.12.", result["database"]["server_version"])
        self.assertEqual(
            result["gates"],
            {"analysis_equivalence": "pass", "document_fidelity": "pass"},
        )
        self.assertEqual(set(result["layouts"]), {"string", "native_limited", "native_hinted"})
        for layout in result["layouts"].values():
            self.assertTrue(
                all(
                    query["correctness"]["matches_truth"]
                    and query["performance"]["matches_truth"]
                    for query in layout["queries"]
                )
            )
            for query in layout["queries"]:
                metrics = query["performance"]["server_metrics"]
                self.assertGreaterEqual(metrics["query_duration_ms"]["median"], 0)
                self.assertGreater(metrics["read_rows"]["median"], 0)
                self.assertGreater(metrics["read_bytes"]["median"], 0)
            full_object = layout["full_object_read"]
            self.assertTrue(full_object["storage"]["stable_result"])
            self.assertGreater(
                full_object["storage"]["server_metrics"]["read_bytes"]["median"],
                0,
            )
            if full_object["fidelity"] is not None:
                self.assertTrue(full_object["fidelity"]["stable_result"])
                self.assertTrue(full_object["fidelity"]["matches_string_storage"])
            self.assertEqual(layout["storage_roundtrip"]["checked"], 200)
            self.assertEqual(layout["storage_roundtrip"]["hash_mismatch_count"], 0)
            self.assertEqual(layout["fidelity_roundtrip"]["checked"], 200)
            self.assertEqual(layout["fidelity_roundtrip"]["hash_mismatch_count"], 0)
            for roundtrip_name in ("storage_roundtrip", "fidelity_roundtrip"):
                roundtrip = layout[roundtrip_name]
                self.assertEqual(roundtrip["duplicate_count"], 0)
                self.assertEqual(roundtrip["extra_count"], 0)
                self.assertEqual(roundtrip["missing_count"], 0)
            self.assertGreaterEqual(layout["parts_before_merge"]["part_count"], 2)
            self.assertEqual(layout["parts_after_merge"]["part_count"], 1)
            self.assertEqual(layout["parts_after_merge"]["rows"], 200)

        limited = result["layouts"]["native_limited"]
        hinted = result["layouts"]["native_hinted"]
        self.assertGreater(limited["paths_after_merge"]["shared"], 0)
        self.assertEqual(hinted["paths_after_merge"]["shared"], 0)
        self.assertTrue(limited["path_inventory_validation"]["passed"])
        self.assertTrue(hinted["path_inventory_validation"]["passed"])
        self.assertEqual(hinted["hot_path_types"], {"hot.region": "String", "hot.tenant": "String"})
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["data_path"], "independent_loader")
        self.assertEqual(manifest["input"]["path_count"], 500)
        self.assertEqual(manifest["dynamic_path_budgets"], {"native_hinted": 1000, "native_limited": 100})
        self.assertEqual(result["workload"]["time_fraction"], 0.5)
        self.assertEqual(
            manifest["layout_order"],
            ["string", "native_limited", "native_hinted"],
        )

        cleanup = subprocess.run(
            [
                "docker",
                "exec",
                CONTAINER,
                "clickhouse-client",
                "--query",
                f"SELECT count() FROM system.databases WHERE name='{database}'",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(cleanup.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
