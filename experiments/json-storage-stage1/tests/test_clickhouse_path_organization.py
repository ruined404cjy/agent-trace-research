import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
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

        self.assertIn("String CODEC(ZSTD(3))", ddls["string"])
        self.assertIn("JSON(max_dynamic_paths=100)", ddls["native_limited"])
        self.assertIn("max_dynamic_paths=1000", ddls["native_hinted"])
        self.assertIn("hot.tenant String", ddls["native_hinted"])
        self.assertIn("hot.region String", ddls["native_hinted"])
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
        self.assertIn("getSubcolumn(metadata, 'hot.tenant')::String", runner.query_spec("native_limited", hot))
        self.assertIn("getSubcolumn(metadata, 'paths.p00499')::String", runner.query_spec("native_hinted", cold))

    def test_path_inventory_deduplicates_paths_across_rows(self):
        """路径数量统计应计算全集，不能重复累计每行返回的数组。"""
        runner = load_runner()
        statement = runner.path_inventory_statement("json_stage1_test.events_native_limited")

        self.assertIn("arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(metadata))))", statement)
        self.assertIn("arrayDistinct(arrayFlatten(groupArray(JSONSharedDataPaths(metadata))))", statement)


@unittest.skipUnless(
    os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1",
    "设置 RUN_CLICKHOUSE_INTEGRATION=1 后运行真实 ClickHouse 集成测试",
)
class ClickHousePathOrganizationIntegrationTest(unittest.TestCase):
    """验证 ClickHouse String、动态子列和 shared data 的实验门禁。"""

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
        self.assertEqual(set(result["layouts"]), {"string", "native_limited", "native_hinted"})
        for layout in result["layouts"].values():
            self.assertTrue(all(query["matches_truth"] for query in layout["queries"]))
            self.assertEqual(layout["roundtrip"], {"checked": 200, "hash_mismatches": []})
            self.assertGreaterEqual(layout["parts_before_merge"]["part_count"], 2)
            self.assertEqual(layout["parts_after_merge"]["part_count"], 1)

        limited = result["layouts"]["native_limited"]
        hinted = result["layouts"]["native_hinted"]
        self.assertGreater(limited["paths_after_merge"]["shared"], 0)
        self.assertEqual(hinted["paths_after_merge"]["shared"], 0)
        self.assertEqual(hinted["hot_path_types"], {"hot.region": "String", "hot.tenant": "String"})
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["data_path"], "independent_loader")
        self.assertEqual(manifest["input"]["path_count"], 500)
        self.assertEqual(manifest["dynamic_path_budgets"], {"native_hinted": 1000, "native_limited": 100})

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
