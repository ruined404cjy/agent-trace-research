import contextlib
import io
import os
import sys
import tempfile
import unittest
import uuid
import hashlib
import json
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import run_opengauss_mechanisms as mechanisms


class OpenGaussMechanismUnitTest(unittest.TestCase):
    """验证 JSON/JSONB 自然路径与索引能力的 DDL 边界。"""

    def test_mechanism_ddls_keep_types_and_index_kinds_distinct(self):
        """捕获热点索引或 JSONB containment 索引落到错误结构。"""
        ddls = mechanisms.opengauss_mechanism_ddls("s2sup_og_mech")

        self.assertEqual(set(ddls), {
            "og_json", "og_json_hot", "og_jsonb", "og_jsonb_hot", "og_jsonb_gin",
        })
        self.assertIn("attributes JSON NOT NULL", ddls["og_json_hot"])
        self.assertIn("attributes JSONB NOT NULL", ddls["og_jsonb_hot"])
        self.assertIn("CREATE INDEX", ddls["og_json_hot"])
        self.assertIn("CREATE INDEX", ddls["og_jsonb_hot"])
        self.assertIn("jsonb_hash_ops", ddls["og_jsonb_gin"])
        self.assertIn("project_id TEXT NOT NULL", ddls["og_json"])
        self.assertIn("start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL", ddls["og_jsonb"])
        self.assertIn("@>", mechanisms.path_query_sql("og_jsonb_gin"))
        self.assertNotIn("@>", mechanisms.path_query_sql("og_json"))

    def test_schema_identifier_is_validated_before_sql_generation(self):
        """捕获未验证 identifier 进入机制 DDL。"""
        for value in ("Bad", "bad-name", "x" * 64, ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mechanisms.opengauss_mechanism_ddls(value)

    def test_document_evidence_rejects_same_count_with_wrong_content(self):
        """捕获只检查完整文档行数而漏过内容错误。"""
        expected = {
            "event-a": hashlib.sha256(b'{"value":1}').hexdigest(),
            "event-b": hashlib.sha256(b'{"value":2}').hexdigest(),
        }
        rows = [("event-a", '{"value":1}'), ("event-b", '{"value":3}')]

        result = mechanisms.validate_document_rows(rows, expected)

        self.assertFalse(result["ok"])
        self.assertEqual(result["mismatches"], ["event-b"])
        self.assertEqual(result["row_count"], 2)

    def test_truth_contract_requires_exact_s01_to_s06(self):
        """缺失或额外查询不能进入 openGauss 机制运行。"""
        catalog = {"parameters": {query_id: {} for query_id in mechanisms.QUERY_IDS}}
        truth = {"results": {query_id: {} for query_id in mechanisms.QUERY_IDS}}

        self.assertEqual(set(mechanisms.validate_truth_contract(catalog, truth)[0]), set(mechanisms.QUERY_IDS))
        catalog["parameters"]["S07"] = {}
        with self.assertRaisesRegex(ValueError, "S01-S06"):
            mechanisms.validate_truth_contract(catalog, truth)

    def test_server_probe_timing_accepts_opengauss_total_runtime_label(self):
        """服务端复合探针计时使用 openGauss 原生 Total runtime 标签。"""
        self.assertEqual(
            mechanisms.parse_server_timing("Seq Scan\nTotal runtime: 0.106 ms"), 0.106
        )

    def test_attributes_to_jsonb_probe_labels_composite_query_evidence(self):
        """复合扫描探针不得标成纯 JSON/JSONB 转换或独立网络计时。"""
        probe = mechanisms.attributes_to_jsonb_probe(
            "og_json", "Seq Scan\nTotal runtime: 0.106 ms", 0.200, 48534, 256,
        )

        self.assertEqual(probe["source_type"], "JSON")
        self.assertEqual(probe["plan"], "Seq Scan\nTotal runtime: 0.106 ms")
        self.assertEqual(probe["scanned_rows"], 48534)
        self.assertEqual(probe["returned_rows"], 256)
        self.assertEqual(probe["execution_ms"], 0.106)
        self.assertAlmostEqual(probe["client_observed_overhead_ms"], 0.094)
        self.assertNotIn("network_and_plan_ms", probe)

    def test_cleanup_rejects_drop_when_schema_still_exists(self):
        """DROP 返回后 schema 仍存在时必须标记 failed 并抛错。"""
        connection = mock.MagicMock()
        connection.execute.return_value.fetchone.return_value = (True,)
        result = {"status": "running", "cleanup_confirmed": False}

        with self.assertRaisesRegex(RuntimeError, "schema cleanup not confirmed"):
            mechanisms.cleanup_owned_schema(connection, "s2sup_cleanup", result)

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup_confirmed"])

    def test_run_entry_loads_frozen_inputs_before_database_work(self):
        """捕获 openGauss 入口绕过 Task 4 冻结身份门禁。"""
        loaded = ([{"event_id": "event-a"}], {"block_size": 256}, {"parameters": {}}, {"results": {}}, {"dataset": {}})
        with mock.patch.object(mechanisms, "load_inputs", return_value=loaded) as load, \
                mock.patch.object(mechanisms, "_run_loaded", return_value={"status": "complete"}) as run:
            result = mechanisms.run_opengauss_mechanisms(
                Path("input"), Path("truth"), "127.0.0.1", 15432, "container", "s2sup_og"
            )
        load.assert_called_once_with(Path("input"), Path("truth"))
        run.assert_called_once()
        self.assertEqual(result["status"], "complete")

    def test_cli_parses_required_paths_namespace_and_fixed_defaults(self):
        """捕获机制程序缺少可复现入口或默认固定容器端点漂移。"""
        args = mechanisms.parse_args([
            "--input", "input", "--truth", "truth", "--output", "output",
            "--namespace", "s2sup_cli",
        ])

        self.assertEqual(args.input, Path("input"))
        self.assertEqual(args.truth, Path("truth"))
        self.assertEqual(args.output, Path("output"))
        self.assertEqual(args.namespace, "s2sup_cli")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 15432)
        self.assertEqual(args.container_name, "agent-trace-opengauss-v6")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            mechanisms.parse_args(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("--input", output.getvalue())
        self.assertIn("--container-name", output.getvalue())

    def test_cli_publishes_result_before_complete_manifest_and_forwards_arguments(self):
        """捕获 CLI 未透传参数、未记录 artifact 身份或覆盖输出目录其他文件。"""
        result = {
            "status": "complete", "cleanup_confirmed": True,
            "input": {"dataset": {"bytes": 3, "sha256": "input"}},
            "layouts": {
                layout: {"queries_ok": True, "documents": {"ok": True}}
                for layout in mechanisms.LAYOUTS
            },
        }
        environment = {
            "endpoint": {"host": "db.example", "port": 15433},
            "container": {"name": "og-container", "database_version": "(openGauss 6.0.0 build abc)"},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            call_order = []
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            (output / "run-manifest.json").write_text('{"status":"complete","stale":true}', encoding="utf-8")
            args = mechanisms.parse_args([
                "--input", "input", "--truth", "truth", "--output", str(output),
                "--namespace", "s2sup_cli", "--host", "db.example", "--port", "15433",
                "--container-name", "og-container",
            ])
            args.command = ["python", "run_opengauss_mechanisms.py", "--namespace", "s2sup_cli"]
            with mock.patch.object(
                    mechanisms, "collect_environment_identity",
                    side_effect=lambda *unused: call_order.append("environment") or environment,
            ), mock.patch.object(
                    mechanisms, "run_opengauss_mechanisms",
                    side_effect=lambda *unused: call_order.append("run") or result,
            ) as run:
                mechanisms.execute(args)

            manifest = json.loads((output / "run-manifest.json").read_bytes())
            artifact = output / "mechanism-result.json"
            expected_bytes = mechanisms.canonical_bytes(result) + b"\n"
            self.assertEqual(artifact.read_bytes(), expected_bytes)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["engine"], "opengauss")
            self.assertEqual(manifest["command"], args.command)
            self.assertEqual(manifest["input"], result["input"])
            self.assertEqual(manifest["environment"], environment)
            self.assertTrue(manifest["correctness"]["truth_ok"])
            self.assertTrue(manifest["correctness"]["cleanup_confirmed"])
            self.assertIn("s2sup_cli", manifest["run_id"])
            self.assertEqual(manifest["mechanism"]["query_ids"], list(mechanisms.QUERY_IDS))
            self.assertIn("runner/run_opengauss_mechanisms.py", manifest["mechanism"]["files"])
            self.assertEqual(manifest["artifacts"]["mechanism-result.json"], {
                "bytes": len(expected_bytes), "sha256": hashlib.sha256(expected_bytes).hexdigest(),
            })
            self.assertEqual((output / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(call_order, ["environment", "run"])
            run.assert_called_once_with(
                Path("input"), Path("truth"), "db.example", 15433, "og-container", "s2sup_cli",
            )

    def test_cli_publishes_failed_manifest_without_running_when_environment_check_fails(self):
        """捕获环境身份失败后仍创建 schema、写入或遗留旧 complete artifact。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "run-manifest.json").write_text('{"status":"complete"}', encoding="utf-8")
            (output / "mechanism-result.json").write_text('{"status":"complete"}', encoding="utf-8")
            args = mechanisms.parse_args([
                "--input", "input", "--truth", "truth", "--output", str(output),
                "--namespace", "s2sup_cli",
            ])
            with mock.patch.object(
                    mechanisms, "collect_environment_identity",
                    side_effect=RuntimeError("invalid environment"),
            ), mock.patch.object(
                    mechanisms,
                    "run_opengauss_mechanisms",
                    return_value={
                        "status": "complete", "cleanup_confirmed": True, "input": {},
                        "layouts": {
                            layout: {"queries_ok": True, "documents": {"ok": True}}
                            for layout in mechanisms.LAYOUTS
                        },
                    },
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "invalid environment"):
                    mechanisms.execute(args)

            manifest = json.loads((output / "run-manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse((output / "mechanism-result.json").exists())
            run.assert_not_called()

    def test_cli_replaces_stale_complete_with_failed_manifest_for_run_or_cleanup_failure(self):
        """捕获执行异常或未确认清理后遗留 complete manifest。"""
        environment = {
            "endpoint": {"host": "127.0.0.1", "port": 15432},
            "container": {"name": "agent-trace-opengauss-v6", "database_version": "(openGauss 6.0.0 build abc)"},
        }
        failures = (
            RuntimeError("database unavailable"),
            {"status": "complete", "cleanup_confirmed": False, "input": {}, "layouts": {}},
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                (output / "run-manifest.json").write_text('{"status":"complete"}', encoding="utf-8")
                args = mechanisms.parse_args([
                    "--input", "input", "--truth", "truth", "--output", str(output),
                    "--namespace", "s2sup_cli",
                ])
                patch_value = ({"side_effect": failure} if isinstance(failure, Exception)
                               else {"return_value": failure})
                with mock.patch.object(mechanisms, "collect_environment_identity", return_value=environment), \
                        mock.patch.object(mechanisms, "run_opengauss_mechanisms", **patch_value):
                    with self.assertRaises(RuntimeError):
                        mechanisms.execute(args)
                manifest = json.loads((output / "run-manifest.json").read_bytes())
                self.assertEqual(manifest["status"], "failed")
                self.assertNotIn("stale", manifest)
                if isinstance(failure, Exception):
                    self.assertFalse((output / "mechanism-result.json").exists())
                else:
                    self.assertIn("mechanism-result.json", manifest["artifacts"])

    def test_environment_identity_rejects_wrong_server_version(self):
        """捕获容器身份存在但服务端版本不属于固定 openGauss 6.0.0。"""
        identity = {"name": "og", "ports": {"5432/tcp": [{"HostPort": "15432"}]}}
        adapter = mock.MagicMock()
        adapter.database_version.return_value = "(openGauss 7.0.0 build future)"
        with mock.patch.object(mechanisms, "_container_identity", return_value=identity), \
                mock.patch.object(mechanisms, "OpenGaussFourLayoutAdapter", return_value=adapter):
            with self.assertRaisesRegex(RuntimeError, "database version"):
                mechanisms.collect_environment_identity(
                    "127.0.0.1", 15432, "og", "s2sup_cli"
                )


@unittest.skipUnless(os.environ.get("RUN_OPENGAUSS_INTEGRATION") == "1", "set RUN_OPENGAUSS_INTEGRATION=1")
class OpenGaussMechanismIntegrationTest(unittest.TestCase):
    """以 Task 1 冻结输入/truth 验证五种 openGauss 机制结构。"""

    DATA_ROOT = Path(os.environ.get("MECHANISM_DATA_ROOT", STAGE_DIR.parents[1]))
    INPUT_DIR = DATA_ROOT / "docs/temp/json-storage-stage2/cross-engine-input-20260907"
    TRUTH_DIR = DATA_ROOT / "docs/temp/json-storage-stage2-sup/input-20260908"

    def test_frozen_input_mechanism_chain_matches_truth_and_cleans_schema(self):
        namespace = f"s2sup_mech_{uuid.uuid4().hex[:10]}"
        result = mechanisms.run_opengauss_mechanisms(
            self.INPUT_DIR, self.TRUTH_DIR, "127.0.0.1", 15432,
            "agent-trace-opengauss-v6", namespace,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(set(result["layouts"]), set(mechanisms.LAYOUTS))
        self.assertTrue(all(item["queries_ok"] for item in result["layouts"].values()))
        self.assertTrue(all(item["documents"]["ok"] for item in result["layouts"].values()))
        for layout, item in result["layouts"].items():
            probe = item["attributes_to_jsonb_probe"]
            self.assertEqual(probe["source_type"], "JSON" if layout.startswith("og_json_") or layout == "og_json" else "JSONB")
            self.assertIsInstance(probe["execution_ms"], float)
            self.assertEqual(probe["scanned_rows"], 48534)
            self.assertEqual(probe["returned_rows"], 256)
            self.assertTrue(probe["plan"])
            self.assertIn("client_observed_overhead_ms", probe)
            self.assertNotIn("server_conversion", item)
        self.assertTrue(all(
            item["documents"]["canonical_sha256"] and item["documents"]["canonical_bytes"] > 0
            for item in result["layouts"].values()
        ))
        self.assertTrue(all(item["row_count"] == 48534 for item in result["layouts"].values()))
        self.assertTrue(result["cleanup_confirmed"])


if __name__ == "__main__":
    unittest.main()
