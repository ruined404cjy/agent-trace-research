import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
GENERATOR = STAGE_DIR / "generator" / "generate_path_profile.py"
RUNNER = STAGE_DIR / "opengauss" / "run_path_organization.py"
CONTAINER = "agent-trace-opengauss-v6"
GSQL = "/usr/local/opengauss/bin/gsql"


@unittest.skipUnless(
    os.environ.get("RUN_OPENGAUSS_INTEGRATION") == "1",
    "设置 RUN_OPENGAUSS_INTEGRATION=1 后运行真实 openGauss 集成测试",
)
class OpenGaussPathOrganizationIntegrationTest(unittest.TestCase):
    """验证 openGauss JSONB 三种路径索引布局的实验门禁。"""

    def test_jsonb_layouts_match_truth_and_expected_index_plans(self):
        """捕获 JSONB 查询错误、目标索引不可用或临时 schema 未清理的错误。"""
        schema_name = f"json_path_test_{os.getpid()}"
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
                    "--port",
                    "15432",
                    "--schema-name",
                    schema_name,
                    "--measurements",
                    "5",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["storage"], {"json_type": "JSONB", "profile": "row"})
        self.assertEqual(result["roundtrip_scope"], "canonical_record")
        self.assertIn("openGauss 6.0.0", result["database"]["server_version"])
        self.assertEqual(
            set(result["layouts"]),
            {"no_index", "gin", "hot_expression"},
        )
        for layout in result["layouts"].values():
            self.assertTrue(all(query["matches_truth"] for query in layout["queries"]))
            self.assertEqual(layout["roundtrip"], {"checked": 200, "hash_mismatches": []})

        query_families = {
            layout_name: {
                query["query_id"]: query["predicate_family"]
                for query in layout["queries"]
            }
            for layout_name, layout in result["layouts"].items()
        }
        self.assertEqual(
            query_families,
            {
                "no_index": {
                    "hot_tenant_equals": "containment",
                    "cold_path_equals": "containment",
                },
                "gin": {
                    "hot_tenant_equals": "containment",
                    "cold_path_equals": "containment",
                },
                "hot_expression": {
                    "hot_tenant_equals": "expression",
                    "cold_path_equals": "containment",
                },
            },
        )

        self.assertIn(
            "events_gin_metadata_idx",
            result["layouts"]["gin"]["plans"]["cold_path_equals"]["forced"],
        )
        self.assertIn(
            "events_hot_tenant_idx",
            result["layouts"]["hot_expression"]["plans"]["hot_tenant_equals"]["forced"],
        )
        self.assertIn(
            "Seq Scan",
            result["layouts"]["no_index"]["plans"]["cold_path_equals"]["natural"],
        )
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["data_path"], "independent_loader")
        self.assertEqual(manifest["input"]["path_count"], 50)
        self.assertEqual(manifest["input"]["density_percent"], 20)
        self.assertEqual(manifest["measurements"], 5)
        self.assertEqual(len(manifest["ddl_sha256"]), 64)
        self.assertEqual(manifest["connection"]["published_container_port"], 5432)
        self.assertEqual(manifest["connection"]["server_version"], result["database"]["server_version"])

        cleanup_check = subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "omm",
                CONTAINER,
                "bash",
                "-lc",
                'exec "$0" "$@"',
                GSQL,
                "-X",
                "-d",
                "postgres",
                "-Atc",
                f"SELECT count(*) FROM pg_namespace WHERE nspname='{schema_name}';",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(cleanup_check.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
