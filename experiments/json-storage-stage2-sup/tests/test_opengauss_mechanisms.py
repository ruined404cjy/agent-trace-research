import os
import sys
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
