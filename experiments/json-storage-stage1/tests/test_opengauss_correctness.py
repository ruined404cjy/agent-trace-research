import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
GENERATOR = STAGE_DIR / "generator" / "generate.py"
RUNNER = STAGE_DIR / "opengauss" / "run_correctness.py"
CONTAINER = "agent-trace-opengauss-v6"
GSQL = "/usr/local/opengauss/bin/gsql"


@unittest.skipUnless(
    os.environ.get("RUN_OPENGAUSS_INTEGRATION") == "1",
    "设置 RUN_OPENGAUSS_INTEGRATION=1 后运行真实 openGauss 集成测试",
)
class OpenGaussCorrectnessIntegrationTest(unittest.TestCase):
    """验证标准 openGauss 6.0.0 上 exporter JSON schema 的正确性探针。"""

    def test_json_roundtrip_and_path_semantics_match_truth_manifest(self):
        """捕获 JSON DDL、路径查询、回读校验或临时 schema 清理偏离契约的错误。"""
        schema_name = f"json_stage1_test_{os.getpid()}"
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
                    "--count",
                    "12",
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
                    "--schema-name",
                    schema_name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

            result = json.loads((output_dir / "correctness.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "pass")
        self.assertIn("openGauss 6.0.0", result["database"]["server_version"])
        self.assertTrue(
            result["database"]["repo_digest"].startswith("enmotech/opengauss@sha256:")
        )
        self.assertEqual(result["storage"], {"json_type": "JSON", "profile": "row"})
        self.assertTrue(result["runtime_capabilities"]["jsonb_type_available"])
        self.assertEqual(
            result["summary"],
            {
                "array_order_count": 1,
                "conflict_array_count": 1,
                "conflict_integer_count": 1,
                "conflict_object_count": 1,
                "conflict_string_count": 1,
                "escaped_path_count": 1,
                "json_null_count": 1,
                "row_count": 12,
                "sql_null_count": 1,
                "target_boolean_count": 1,
                "target_integer_count": 1,
                "target_missing_count": 7,
                "target_number_count": 1,
            },
        )
        self.assertEqual(result["roundtrip"], {"checked": 12, "hash_mismatches": []})
        self.assertEqual(
            result["duplicate_key_probe"],
            {"accepted": True, "retained_type": "number", "retained_value": 2},
        )
        self.assertTrue(all(query["matches_truth"] for query in result["queries"]))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(
            manifest["baselines"]["benchmark"],
            "9529c8f389673132757f4da9a96878926f22b94f",
        )
        self.assertEqual(
            manifest["baselines"]["exporter"],
            "54ca553a7ed09ad1751c82adab3aa52c6e9357b1",
        )
        self.assertEqual(manifest["data_path"], "independent_loader")
        self.assertEqual(len(manifest["ddl_sha256"]), 64)

        cleanup_check = subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "omm",
                CONTAINER,
                "bash",
                "-lc",
                f"{GSQL} -d postgres -tAc \"SELECT count(*) FROM pg_namespace "
                f"WHERE nspname='{schema_name}'\"",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(cleanup_check.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
