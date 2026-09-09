import hashlib
import json
import sys
import tempfile
import threading
import unittest
from subprocess import CompletedProcess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import run_four_layouts as runner
import supplement_common as common


EXPECTED_BYTES = {
    "dataset": 302518948,
    "truth": 138577,
    "query_catalog": 1074,
    "input_run_manifest": 2397,
    "input_truth_manifest": 18073179,
}
EXPECTED_INPUT_RUN_SHA256 = "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437"
EXPECTED_INPUT_TRUTH_SHA256 = "b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04"


class Connection:
    def close(self):
        pass


class FakeAdapter:
    """返回完整结果结构并记录编排调用。"""

    def __init__(self, fail_layout=None, cleanup_fail=False, clickhouse=False, cleanup_removed=True, calls=None):
        self.calls = calls if calls is not None else []
        self.fail_layout = fail_layout
        self.cleanup_fail = cleanup_fail
        self.clickhouse = clickhouse
        self.cleanup_removed = cleanup_removed
        self.query_number = 0
        self.lock = threading.Lock()

    def create_layout(self, layout, *args):
        self.calls.append(("create", layout))
        return {"ddl": f"CREATE {layout}", "schema": layout}

    def insert_block(self, layout, rows):
        self.calls.append(("insert", layout))
        if layout == self.fail_layout:
            raise RuntimeError("insert failed")
        return {"rows": len(rows), "block_wall_ms": 1.0}

    def finish_maintenance(self, layout, *args, **kwargs):
        self.calls.append(("maintenance", layout))
        return {"completed": True, "analyzed": True}

    def connect_worker(self):
        return Connection()

    def execute_query(self, connection, layout, query_id, params):
        result = params["expected"]
        envelope = {"latency_ms": 1.0, "recovery_ms": 0.1, "result": result,
                    "result_sha256": hashlib.sha256(common.canonical_bytes(result)).hexdigest(),
                    "row_count": 1}
        if self.clickhouse:
            with self.lock:
                self.query_number += 1
                number = self.query_number
            envelope["query_log_id"] = f"{query_id}-{number}"
        return envelope

    def collect_query_logs(self, ids):
        return {value: dict.fromkeys(runner.QUERY_LOG_METRICS, 1) for value in ids}

    def collect_plan(self, layout, query_id, params):
        return {"natural": query_id}

    def verify_analysis(self, layout, truth):
        return {"ok": True, "actual_count": len(truth["records"]), "expected_count": len(truth["records"])}

    def verify_raw(self, layout, truth):
        return {"ok": True, "actual_count": len(truth["records"]), "expected_count": len(truth["records"])}

    def collect_storage(self, layout):
        return {"analytics": {"total_bytes": 10}, "raw": {"total_bytes": 20}}

    def cleanup(self, layout):
        self.calls.append(("cleanup", layout))
        if self.cleanup_fail:
            raise RuntimeError("cleanup failed")
        return {"removed": self.cleanup_removed}

    @staticmethod
    def query_sql(layout, query_id):
        return f"SELECT {layout} {query_id}"


class RunnerTests(unittest.TestCase):
    def test_query_stage_uses_two_workers_exact_counts_and_queryfinish(self):
        adapter = FakeAdapter(clickhouse=True)
        catalog, truth = self.query_fixture()
        result = runner.run_query_stage(adapter, "ch_native", catalog, truth, 2, 1, workers=2)
        counts = {query: sum(s["query_id"] == query for s in result["samples"]) for query in common.QUERY_IDS}
        self.assertEqual(counts, {**dict.fromkeys(common.QUERY_IDS[:5], 2), "S06": 1})
        self.assertEqual(result["worker_sample_counts"], {"0": 6, "1": 5})
        self.assertTrue(all(set(s["query_log"]) == set(runner.QUERY_LOG_METRICS) for s in result["samples"]))
        self.assertEqual(len({s["query_log_id"] for s in result["samples"]}), len(result["samples"]))

    def test_query_stage_rejects_truth_mismatch(self):
        adapter = FakeAdapter()
        catalog, truth = self.query_fixture()
        catalog["parameters"]["S01"]["expected"] = {"wrong": True}
        with self.assertRaisesRegex(RuntimeError, "truth mismatch"):
            runner.run_query_stage(adapter, "og_json", catalog, truth, 1, 1, workers=2)

    def test_query_stage_rejects_invalid_timing_envelope(self):
        """捕获无效查询延迟进入完成结果。"""
        adapter = FakeAdapter()
        catalog, truth = self.query_fixture()
        original = adapter.execute_query

        def invalid(*args):
            result = original(*args)
            result["latency_ms"] = 0
            return result

        adapter.execute_query = invalid
        with self.assertRaisesRegex(RuntimeError, "query timing"):
            runner.run_query_stage(adapter, "og_json", catalog, truth, 1, 1, workers=2)

    def test_execute_orders_layouts_publishes_complete_last_and_cleans(self):
        calls = []
        adapter = FakeAdapter(calls=calls)
        clickhouse_adapter = FakeAdapter(clickhouse=True, calls=calls)
        rows, source_truth, catalog, truth, args = self.execution_fixture()
        with tempfile.TemporaryDirectory() as directory:
            args.output = Path(directory)
            with mock.patch.object(runner, "load_inputs", return_value=(rows, source_truth, catalog, truth, {"dataset": {"sha256": "d"}, "truth": {"sha256": "t"}})), mock.patch.object(
                runner, "make_adapters", return_value=(adapter, clickhouse_adapter)
            ), mock.patch.object(runner, "collect_environment", return_value={"containers": {}}):
                runner.execute(args)
            manifest = json.loads((args.output / "run-manifest.json").read_bytes())
            result = json.loads((args.output / "result-og_json.json").read_bytes())
        self.assertEqual([layout for action, layout in calls if action == "create"], list(common.ROUND_ORDERS[0]))
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(set(manifest["artifacts"]), {f"result-{layout}.json" for layout in common.LAYOUTS})
        self.assertTrue(all("bytes" in value and "sha256" in value for value in manifest["artifacts"].values()))
        self.assertEqual(set(manifest["runner"]["files"]), {
            "runner/run_four_layouts.py", "runner/supplement_common.py",
            "runner/opengauss_four_layout.py", "runner/clickhouse_four_layout.py",
        })
        self.assertEqual(manifest["measurements"], {"S01-S05": 100, "S06": 20, "query_workers": 2})
        self.assertEqual(manifest["cache_state"], "query_warmup_1_no_os_cache_drop")
        self.assertEqual(result["runner"], manifest["runner"])
        self.assertEqual(result["run_id"], manifest["run_id"])

    def test_layout_order_is_exact_for_each_round(self):
        """捕获只校验布局集合、未校验固定轮次顺序。"""
        self.assertEqual(runner.parse_layout_order(",".join(common.ROUND_ORDERS[2]), 3), common.ROUND_ORDERS[2])
        with self.assertRaisesRegex(ValueError, "fixed round order"):
            runner.parse_layout_order(",".join(reversed(common.ROUND_ORDERS[2])), 3)

    def test_argument_failure_replaces_stale_complete_manifest(self):
        """捕获启动校验失败时保留旧 complete manifest。"""
        _, _, _, _, args = self.execution_fixture()
        args.layout_order = ",".join(reversed(common.ROUND_ORDERS[0]))
        with tempfile.TemporaryDirectory() as directory:
            args.output = Path(directory)
            (args.output / "run-manifest.json").write_text('{"status":"complete"}\n')
            with self.assertRaisesRegex(ValueError, "fixed round order"):
                runner.execute(args)
            manifest = json.loads((args.output / "run-manifest.json").read_bytes())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["artifacts"], {})

    def test_container_identity_uses_safe_fields_and_records_repo_digest(self):
        """捕获容器检查读取密码环境或遗漏镜像仓库摘要。"""
        ports = '{"5432/tcp":[{"HostIp":"","HostPort":"15432"}]}'
        inspect = CompletedProcess([], 0, f"image:tag|sha256:image|{ports}|0|0|true\n", "")
        image = CompletedProcess([], 0, '["repo/image@sha256:digest"]\n', "")
        with mock.patch.object(runner.subprocess, "run", side_effect=[inspect, image]) as run:
            identity = runner._container_identity("container")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertFalse(any(".Config.Env" in part for command in commands for part in command))
        self.assertEqual(identity["repo_digests"], ["repo/image@sha256:digest"])
        self.assertEqual(identity["ports"], {"5432/tcp": [{"HostIp": "", "HostPort": "15432"}]})

    def test_container_identity_rejects_missing_or_malformed_fields(self):
        """捕获 Docker 身份缺少镜像字段或端口 JSON 仍进入环境清单。"""
        image = CompletedProcess([], 0, '["repo/image@sha256:digest"]\n', "")
        for name, output in (
            ("empty name", "image:tag|sha256:image|{}|0|0|true\n"),
            ("empty image", "|sha256:image|{}|0|0|true\n"),
            ("empty image ID", "image:tag||{}|0|0|true\n"),
            ("malformed ports", "image:tag|sha256:image|not-json|0|0|true\n"),
        ):
            with self.subTest(name=name), mock.patch.object(
                runner.subprocess, "run",
                side_effect=[CompletedProcess([], 0, output, ""), image],
            ):
                with self.assertRaisesRegex(RuntimeError, "container identity"):
                    runner._container_identity("" if name == "empty name" else "container")

    def test_collect_environment_rejects_wrong_ports_and_database_versions(self):
        """捕获固定容器的端口或数据库版本漂移。"""
        identities = {
            "agent-trace-opengauss-v6": {
                "name": "agent-trace-opengauss-v6", "image": "og:6", "image_id": "sha256:og",
                "ports": {"5432/tcp": [{"HostIp": "", "HostPort": "15432"}]},
                "memory_bytes": 0, "cpu_nanocpus": 0, "running": True,
                "repo_digests": ["og@sha256:digest"],
            },
            "agent-trace-clickhouse-25-12": {
                "name": "agent-trace-clickhouse-25-12", "image": "ch:25.12", "image_id": "sha256:ch",
                "ports": {"8123/tcp": [{"HostIp": "", "HostPort": "18123"}]},
                "memory_bytes": 0, "cpu_nanocpus": 0, "running": True,
                "repo_digests": ["ch@sha256:digest"],
            },
        }
        args = self.execution_fixture()[-1]
        args.opengauss_container = "agent-trace-opengauss-v6"
        args.clickhouse_container = "agent-trace-clickhouse-25-12"
        versions = SimpleNamespace(database_version=lambda: "(openGauss 6.0.0 build 1)"), SimpleNamespace(
            database_version=lambda: "25.12.11.4"
        )
        for mutation, message in (
            (lambda: identities["agent-trace-opengauss-v6"]["ports"]["5432/tcp"][0].update(HostPort="5432"), "port"),
            (lambda: setattr(versions[0], "database_version", lambda: "wrong-version"), "version"),
            (lambda: setattr(versions[1], "database_version", lambda: "25.12.0.0"), "version"),
        ):
            identities["agent-trace-opengauss-v6"]["ports"]["5432/tcp"][0]["HostPort"] = "15432"
            versions[0].database_version = lambda: "(openGauss 6.0.0 build 1)"
            versions[1].database_version = lambda: "25.12.11.4"
            mutation()
            with self.subTest(message=message), mock.patch.object(
                runner, "_container_identity", side_effect=lambda name: identities[name]
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    runner.collect_environment(args, versions)

    def test_execute_rejects_nonformal_measurements_before_adapter_creation(self):
        """捕获非固定样本规模创建数据库对象并发布完成清单。"""
        for field, value, message in (("measurements", 1, "measurements must be 100"),
                                      ("document_page_measurements", 1, "document_page_measurements must be 20"),
                                      ("query_workers", 1, "query_workers must be 2"),
                                      ("host", "localhost", "host must be 127.0.0.1"),
                                      ("opengauss_container", "og", "opengauss_container"),
                                      ("opengauss_port", 5432, "opengauss_port"),
                                      ("clickhouse_container", "ch", "clickhouse_container"),
                                      ("clickhouse_port", 8123, "clickhouse_port")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                _, _, _, _, args = self.execution_fixture()
                setattr(args, field, value)
                args.output = Path(directory)
                with mock.patch.object(runner, "make_adapters") as make:
                    with self.assertRaisesRegex(ValueError, message):
                        runner.execute(args)
                manifest = json.loads((args.output / "run-manifest.json").read_bytes())
                make.assert_not_called()
                self.assertEqual(manifest["status"], "failed")

    def test_load_inputs_rejects_resigned_truth_outside_reviewed_identity(self):
        """捕获伪造查询结果并重签 truth manifest 后仍被接受。"""
        input_dir, truth_dir = Path("input"), Path("truth")
        input_manifest = {"input": {"path": "/source/traces.jsonl", "sha256": runner.EXPECTED_SOURCE_SHA256}}
        catalog = {"contract_version": common.CONTRACT_VERSION, "query_ids": list(common.QUERY_IDS)}
        input_truth_identity = {"bytes": EXPECTED_BYTES["input_truth_manifest"],
                                "sha256": EXPECTED_INPUT_TRUTH_SHA256}
        input_run_identity = {"bytes": EXPECTED_BYTES["input_run_manifest"],
                              "sha256": EXPECTED_INPUT_RUN_SHA256}
        dataset_identity = {"bytes": EXPECTED_BYTES["dataset"],
                            "sha256": runner.EXPECTED_DATASET_SHA256}
        truth = {"contract_version": common.CONTRACT_VERSION, "query_ids": list(common.QUERY_IDS),
                 "query_catalog_sha256": hashlib.sha256(common.canonical_bytes(catalog)).hexdigest(),
                 "input": {"dataset_sha256": runner.EXPECTED_DATASET_SHA256,
                           "source_input": input_manifest["input"]},
                 "record_count": 48534, "block_count": 190, "block_size": 256,
                 "source": {"run_manifest_sha256": input_run_identity["sha256"],
                            "truth_manifest_sha256": input_truth_identity["sha256"],
                            "artifacts": {"dataset.jsonl": dataset_identity,
                                          "truth-manifest.json": input_truth_identity},
                            "input": input_manifest["input"]}}
        supplement_manifest = {"status": "complete", "artifacts": {
            "query-catalog.json": {"bytes": EXPECTED_BYTES["query_catalog"],
                                   "sha256": runner.EXPECTED_CATALOG_SHA256},
            "truth-manifest.json": {"bytes": EXPECTED_BYTES["truth"], "sha256": "0" * 64}}}

        def read(path, _name):
            if path == input_dir / "run-manifest.json": return input_manifest
            if path == truth_dir / "run-manifest.json": return supplement_manifest
            if path.name == "query-catalog.json": return catalog
            return truth

        def identity(path):
            if path == input_dir / "dataset.jsonl": return dataset_identity
            if path == input_dir / "run-manifest.json": return input_run_identity
            if path == input_dir / "truth-manifest.json": return input_truth_identity
            return supplement_manifest["artifacts"][path.name]

        source_truth = {"block_count": 190, "block_size": 256}
        with mock.patch.object(runner, "verify_input", return_value=([{}] * 48534, source_truth)), mock.patch.object(
            runner, "read_json", side_effect=read
        ), mock.patch.object(runner, "file_identity", side_effect=identity):
            with self.assertRaisesRegex(ValueError, "supplement truth artifact"):
                runner.load_inputs(input_dir, truth_dir)

        supplement_manifest["artifacts"]["truth-manifest.json"]["sha256"] = runner.EXPECTED_TRUTH_SHA256
        supplement_manifest["source"] = truth["source"]
        supplement_manifest["input"] = truth["input"]
        truth["source"] = {**truth["source"], "run_manifest_sha256": "f" * 64}
        with mock.patch.object(runner, "verify_input", return_value=([{}] * 48534, source_truth)), mock.patch.object(
            runner, "read_json", side_effect=read
        ), mock.patch.object(runner, "file_identity", side_effect=identity):
            with self.assertRaisesRegex(ValueError, "source identity"):
                runner.load_inputs(input_dir, truth_dir)

        truth["source"] = {**truth["source"], "run_manifest_sha256": input_run_identity["sha256"]}
        supplement_manifest["source"] = truth["source"]
        dataset_identity["bytes"] += 1
        with mock.patch.object(runner, "verify_input", return_value=([{}] * 48534, source_truth)), mock.patch.object(
            runner, "read_json", side_effect=read
        ), mock.patch.object(runner, "file_identity", side_effect=identity):
            with self.assertRaisesRegex(ValueError, "fixed input identity"):
                runner.load_inputs(input_dir, truth_dir)

        dataset_identity["bytes"] = EXPECTED_BYTES["dataset"]
        input_manifest["input"]["unexpected"] = True
        truth["source"]["input"] = input_manifest["input"]
        truth["input"]["source_input"] = input_manifest["input"]
        supplement_manifest["source"] = truth["source"]
        supplement_manifest["input"] = truth["input"]
        with mock.patch.object(runner, "verify_input", return_value=([{}] * 48534, source_truth)), mock.patch.object(
            runner, "read_json", side_effect=read
        ), mock.patch.object(runner, "file_identity", side_effect=identity):
            with self.assertRaisesRegex(ValueError, "source input identity"):
                runner.load_inputs(input_dir, truth_dir)

    def test_execute_failure_publishes_diagnostics_and_cleanup(self):
        adapter = FakeAdapter(fail_layout="og_jsonb")
        rows, source_truth, catalog, truth, args = self.execution_fixture()
        with tempfile.TemporaryDirectory() as directory:
            args.output = Path(directory)
            with mock.patch.object(runner, "load_inputs", return_value=(rows, source_truth, catalog, truth, {})), mock.patch.object(
                runner, "make_adapters", return_value=(adapter, adapter)
            ), mock.patch.object(runner, "collect_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "insert failed"):
                    runner.execute(args)
            manifest = json.loads((args.output / "run-manifest.json").read_bytes())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error"]["message"], "insert failed")
        self.assertIn(("cleanup", "og_jsonb"), adapter.calls)
        self.assertNotIn(("create", "ch_string"), adapter.calls)

    def test_execute_cleanup_failure_blocks_complete_manifest(self):
        """捕获清理失败后仍发布 complete manifest。"""
        adapter = FakeAdapter(cleanup_fail=True)
        rows, source_truth, catalog, truth, args = self.execution_fixture()
        with tempfile.TemporaryDirectory() as directory:
            args.output = Path(directory)
            with mock.patch.object(runner, "load_inputs", return_value=(rows, source_truth, catalog, truth, {})), mock.patch.object(
                runner, "make_adapters", return_value=(adapter, adapter)
            ), mock.patch.object(runner, "collect_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                    runner.execute(args)
            manifest = json.loads((args.output / "run-manifest.json").read_bytes())
            result = json.loads((args.output / "result-og_json.json").read_bytes())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup"]["removed"])

    def test_cleanup_false_marks_result_failed_before_publication(self):
        """捕获 cleanup 返回 removed=false 时结果仍写成 complete。"""
        adapter = FakeAdapter(cleanup_removed=False)
        rows, source_truth, catalog, truth, args = self.execution_fixture()
        with tempfile.TemporaryDirectory() as directory:
            args.output = Path(directory)
            with mock.patch.object(runner, "load_inputs", return_value=(rows, source_truth, catalog, truth, {})), mock.patch.object(
                runner, "make_adapters", return_value=(adapter, adapter)
            ), mock.patch.object(runner, "collect_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "layout cleanup failed"):
                    runner.execute(args)
            result = json.loads((args.output / "result-og_json.json").read_bytes())
        self.assertEqual(result["status"], "failed")

    @staticmethod
    def query_fixture():
        results = {query: {"value": query} for query in common.QUERY_IDS}
        catalog = {"parameters": {query: {"expected": results[query]} for query in common.QUERY_IDS}}
        return catalog, {"results": results}

    @classmethod
    def execution_fixture(cls):
        catalog, truth = cls.query_fixture()
        rows = [{"raw_event": "x", "event_id": "e"}]
        source_truth = {"block_size": 1, "block_count": 1, "records": [{"event_id": "e"}]}
        args = SimpleNamespace(input=Path("input"), truth=Path("truth"), output=None,
            namespace="s2sup", round=1, layout_order=",".join(common.ROUND_ORDERS[0]),
            opengauss_container="agent-trace-opengauss-v6", opengauss_port=15432,
            clickhouse_container="agent-trace-clickhouse-25-12",
            clickhouse_port=18123, host="127.0.0.1", measurements=100,
            document_page_measurements=20, query_workers=2, maintenance_timeout_seconds=1)
        return rows, source_truth, catalog, truth, args


if __name__ == "__main__":
    unittest.main()
