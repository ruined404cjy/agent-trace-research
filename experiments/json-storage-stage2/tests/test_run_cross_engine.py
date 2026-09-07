import importlib.util
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
RUNNER_PATH = STAGE_DIR / "runner" / "run_cross_engine.py"
sys.path.insert(0, str(STAGE_DIR / "runner"))
RUNNER_SPEC = importlib.util.spec_from_file_location("run_cross_engine", RUNNER_PATH)
runner = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = runner
RUNNER_SPEC.loader.exec_module(runner)


class FakeAdapter:
    """模拟单个 layout session，保留并发和失败门禁所需的可观察状态。"""

    def __init__(self, digest="digest-ok", fail_insert_at=None, fail_connect=False, require_params=False):
        self.digest = digest
        self.fail_insert_at = fail_insert_at
        self.fail_connect = fail_connect
        self.require_params = require_params
        self.inserted = []
        self.connections = []
        self.query_calls = []
        self.query_started = threading.Event()

    def insert_block(self, rows):
        watermark = rows[-1]["ingest_seq"] + 1
        if watermark == self.fail_insert_at:
            raise RuntimeError("raw insert failed")
        self.inserted.append(watermark)
        if watermark == 768:
            self.query_started.wait(timeout=1)
        return {"rows": len(rows)}

    def connect_worker(self):
        if self.fail_connect:
            raise RuntimeError("worker connection failed")
        connection = object()
        self.connections.append(connection)
        return connection

    def execute_query(self, connection, query_id, params, watermark):
        if self.require_params and not params:
            raise RuntimeError("missing query params")
        self.query_started.set()
        self.query_calls.append((id(connection), query_id, params, watermark))
        return {"latency_ms": 1.0, "result_sha256": self.digest, "row_count": 1}

    def collect_plan(self, query_id, _params, _watermark):
        return {"query_id": query_id}


class FakeEngineAdapter:
    """模拟真实 adapter 的 layout 绑定接口，用于 manifest 与清理门禁。"""

    def __init__(self, fail_insert=False, fail_connect=False):
        self.fail_insert = fail_insert
        self.fail_connect = fail_connect
        self.cleaned = []

    def create_layout(self, _layout, _budget):
        return {"ddl": "CREATE TEST"}

    def insert_block(self, _layout, rows):
        if self.fail_insert:
            raise RuntimeError("raw insert failed")
        return {"rows": len(rows)}

    def connect_worker(self):
        if self.fail_connect:
            raise RuntimeError("worker connection failed")
        return object()

    def execute_query(self, _connection, _layout, _query_id, _params, _watermark):
        return {"latency_ms": 1.0, "result_sha256": "digest-ok", "row_count": 1}

    def collect_plan(self, _layout, query_id, _params, _watermark):
        return {"query_id": query_id}

    def finish_maintenance(self, _layout, _timeout):
        return {"completed": True}

    def verify_analysis(self, _layout, _truth):
        return {"ok": True}

    def verify_raw(self, _layout, _truth):
        return {"ok": True}

    def collect_storage(self, _layout):
        return {"bytes": 1}

    def cleanup(self, layout):
        self.cleaned.append(layout)
        return {"removed": True}

    @staticmethod
    def query_sql(_layout, query_id):
        return query_id


class DeferredLogAdapter(FakeAdapter):
    """模拟仅在阶段末提供服务端指标的 adapter。"""

    def __init__(self, missing=False):
        super().__init__()
        self.log_ids = []
        self.batches = []
        self.missing = missing

    def execute_query(self, connection, query_id, params, watermark):
        result = super().execute_query(connection, query_id, params, watermark)
        log_id = str(id(result)) + "_" + str(threading.get_ident()) + "_" + str(len(self.log_ids))
        self.log_ids.append(log_id)
        return {**result, "query_log_id": log_id}

    def collect_query_logs(self, query_ids):
        self.batches.append(list(query_ids))
        return {} if self.missing else {query_id: {"read_rows": 3} for query_id in query_ids}


class CrossEngineRunnerTest(unittest.TestCase):
    """验证写入期并发、静态查询与失败可见性契约。"""

    QUERY_IDS = ("Q01", "Q02", "Q03", "Q05")

    @staticmethod
    def blocks():
        return [
            (watermark, [{"ingest_seq": watermark - 1, "raw_event": "x"}])
            for watermark in (128, 256, 384, 512, 640, 768, 896, 1024)
        ]

    @classmethod
    def concurrent_truth(cls, digest="digest-ok"):
        truth = {
            str(watermark): {query_id: digest for query_id in cls.QUERY_IDS}
            for watermark, _rows in cls.blocks()
        }
        truth["parameters"] = {
            query_id: {"project_id": "project", "query_id": query_id}
            for query_id in cls.QUERY_IDS
        }
        return truth

    def test_parse_layout_order_requires_each_engine_layout_once(self):
        """阻止重复、遗漏或跨引擎 layout 进入可比较轮次。"""
        self.assertEqual(
            runner.parse_layout_order("og_jsonb_hot,og_jsonb_gin,og_jsonb", "opengauss"),
            ("og_jsonb_hot", "og_jsonb_gin", "og_jsonb"),
        )
        with self.assertRaises(ValueError):
            runner.parse_layout_order("og_jsonb,og_jsonb,og_jsonb_gin", "opengauss")
        with self.assertRaises(ValueError):
            runner.parse_layout_order("ch_string,ch_map,ch_native", "opengauss")

    def test_build_blocks_preserves_short_final_block_boundary(self):
        """阻止最后不足 block size 的输入重复前一 block 行。"""
        rows = [{"ingest_seq": index, "raw_event": "x"} for index in range(5)]

        blocks = runner._build_blocks(rows, [2, 4, 5])

        self.assertEqual([[row["ingest_seq"] for row in block] for _watermark, block in blocks], [[0, 1], [2, 3], [4]])

    def test_ingest_queries_start_after_five_blocks_and_reuse_two_connections(self):
        """阻止 worker 过早查询、共享连接或读取未成功提交的水位。"""
        adapter = FakeAdapter()

        result = runner.run_ingest_with_queries(
            adapter, self.blocks(), self.concurrent_truth(), query_workers=2
        )

        self.assertEqual(result["query_workers"], 2)
        self.assertEqual(adapter.inserted, [128, 256, 384, 512, 640, 768, 896, 1024])
        self.assertEqual(len(adapter.connections), 2)
        self.assertEqual(len({connection_id for connection_id, _query_id, _params, _watermark in adapter.query_calls}), 2)
        self.assertTrue(adapter.query_calls)
        self.assertTrue(all(watermark in {640, 768, 896, 1024} for _connection, _query, _params, watermark in adapter.query_calls))
        self.assertTrue(all(sample["matches_truth"] for sample in result["samples"]))
        self.assertEqual(set(result["summary"]), set(self.QUERY_IDS))

    def test_ingest_passes_truth_query_parameters_to_every_concurrent_query(self):
        """阻止并发查询丢失 project、时间等固定 truth 参数。"""
        adapter = FakeAdapter(require_params=True)
        truth = self.concurrent_truth()

        runner.run_ingest_with_queries(adapter, self.blocks(), truth, query_workers=2)

        self.assertTrue(adapter.query_calls)
        for _connection, query_id, params, _watermark in adapter.query_calls:
            self.assertEqual(params, truth["parameters"][query_id])

    def test_worker_connection_failure_aborts_barrier_without_live_threads(self):
        """阻止一个 worker 连接失败后协调线程永久等待 Barrier。"""
        adapter = FakeAdapter(fail_connect=True)
        errors = []
        thread = threading.Thread(
            target=lambda: self._capture_error(
                errors,
                runner.run_ingest_with_queries,
                adapter,
                self.blocks(),
                self.concurrent_truth(),
                2,
            ),
            daemon=True,
        )

        thread.start()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)

    def test_ingest_mismatch_and_partial_failure_do_not_advance_watermark(self):
        """阻止错误查询样本或 raw 失败被记录为可比较的成功写入。"""
        with self.assertRaisesRegex(RuntimeError, "truth mismatch"):
            runner.run_ingest_with_queries(
                FakeAdapter(digest="wrong"), self.blocks(), self.concurrent_truth(), query_workers=2
            )

        failed = FakeAdapter(fail_insert_at=768)
        with self.assertRaisesRegex(RuntimeError, "raw insert failed"):
            runner.run_ingest_with_queries(
                failed, self.blocks(), self.concurrent_truth(), query_workers=2
            )
        self.assertEqual(failed.inserted, [128, 256, 384, 512, 640])

    def test_static_queries_warm_once_then_measure_and_collect_plans(self):
        """阻止预热计入正式样本或遗漏每条查询的计划证据。"""
        adapter = FakeAdapter()
        truth = {
            "parameters": {query_id: {"key_map": {}} for query_id in ("Q01", "Q02", "Q03", "Q04", "Q05")},
            "queries": {
                query_id: {"1024": {"result_sha256": "digest-ok", "row_count": 1}}
                for query_id in ("Q01", "Q02", "Q03", "Q04", "Q05")
            },
        }

        result = runner.run_static_queries(adapter, "ignored", truth, measurements=2, watermark=1024)

        self.assertEqual(set(result["summary"]), {"Q01", "Q02", "Q03", "Q04", "Q05"})
        self.assertEqual(len(result["samples"]), 10)
        self.assertEqual(len(adapter.query_calls), 15)
        self.assertEqual(set(result["plans"]), {"Q01", "Q02", "Q03", "Q04", "Q05"})

    def test_concurrent_and_static_samples_are_backfilled_once_before_summary(self):
        """阻止阶段指标未回填、预热日志漏验或内部 ID 泄入最终样本。"""
        adapter = DeferredLogAdapter()
        concurrent = runner.run_ingest_with_queries(adapter, self.blocks(), self.concurrent_truth(), 2)
        self.assertEqual(len(adapter.batches), 1)
        self.assertEqual(set(adapter.batches[0]), set(adapter.log_ids))
        truth = {"parameters": {query_id: {} for query_id in runner.QUERY_IDS},
                 "queries": {query_id: {"1024": {"result_sha256": "digest-ok"}} for query_id in runner.QUERY_IDS}}
        static = runner.run_static_queries(adapter, "ignored", truth, 2, 1024)
        self.assertEqual(len(adapter.batches), 2)
        self.assertEqual(len(adapter.batches[1]), 15)
        for sample in concurrent["samples"] + static["samples"]:
            self.assertEqual(sample["query_log"], {"read_rows": 3})
            self.assertNotIn("query_log_id", sample)

    def test_missing_batch_metrics_stop_phase_before_summary(self):
        """阻止 adapter 返回缺失指标时仍发布成功阶段摘要。"""
        with self.assertRaises(RuntimeError):
            runner.run_ingest_with_queries(DeferredLogAdapter(missing=True), self.blocks(), self.concurrent_truth(), 2)

    def test_execute_publishes_failed_manifest_after_cleanup(self):
        """阻止 raw 部分失败留下 complete manifest 或跳过已创建 layout 清理。"""
        rows = [
            {"ingest_seq": index, "raw_event": "x", "event_id": f"event-{index}"}
            for index in range(5)
        ]
        truth = {
            "block_size": 1,
            "native_json": {"path_budget": 1},
            "key_map": {},
            "parameters": {query_id: {} for query_id in ("Q01", "Q02", "Q03", "Q04", "Q05")},
            "queries": {
                query_id: {
                    str(watermark): {"result_sha256": "digest-ok", "row_count": 1}
                    for watermark in range(1, 6)
                }
                for query_id in ("Q01", "Q02", "Q03", "Q04", "Q05")
            },
            "records": [],
            "watermarks": list(range(1, 6)),
        }
        args = SimpleNamespace(
            input="unused", output=None, engine="opengauss", container_name="unused",
            namespace="json_s2_test", round=1, layout_order="og_jsonb,og_jsonb_hot,og_jsonb_gin",
            measurements=1, block_size=1, query_workers=1, maintenance_timeout_seconds=1,
            host="127.0.0.1", port=15432,
        )
        adapter = FakeEngineAdapter(fail_insert=True)
        with tempfile.TemporaryDirectory() as temporary:
            input_dir = Path(temporary) / "input"
            input_dir.mkdir()
            (input_dir / "dataset.jsonl").write_text("{}\n", encoding="utf-8")
            (input_dir / "truth-manifest.json").write_text("{}\n", encoding="utf-8")
            args.input = input_dir
            args.output = temporary
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)), mock.patch.object(
                runner, "_adapter_for", return_value=adapter
            ), mock.patch.object(runner, "_input_lineage", return_value={"seed": 42}
            ), mock.patch.object(runner, "_collect_environment", return_value={}):
                with self.assertRaisesRegex(RuntimeError, "raw insert failed"):
                    runner.execute(args)
            manifest = json.loads((Path(temporary) / "run-manifest.json").read_bytes())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(adapter.cleaned, ["og_jsonb"])

    def test_execute_connection_failure_publishes_failed_manifest_after_cleanup(self):
        """阻止 worker 连接失败后遗留 layout 或缺失失败 manifest。"""
        rows, truth, args = self._execution_fixture()
        adapter = FakeEngineAdapter(fail_connect=True)
        with tempfile.TemporaryDirectory() as temporary:
            self._write_input_fixture(temporary, args)
            args.output = temporary
            errors = []
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)), mock.patch.object(
                runner, "_adapter_for", return_value=adapter
            ), mock.patch.object(runner, "_input_lineage", return_value={"seed": 42}
            ), mock.patch.object(runner, "_collect_environment", return_value={}):
                thread = threading.Thread(
                    target=lambda: self._capture_error(errors, runner.execute, args), daemon=True
                )
                thread.start()
                thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            manifest = json.loads((Path(temporary) / "run-manifest.json").read_bytes())
        self.assertEqual(len(errors), 1)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(adapter.cleaned, ["og_jsonb"])

    def test_execute_replaces_stale_manifest_when_validation_or_environment_fails(self):
        """阻止参数或环境失败保留旧 complete manifest 或遗漏失败诊断。"""
        rows, truth, args = self._execution_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_input_fixture(temporary, args)
            args.output = temporary
            manifest_path = Path(temporary) / "run-manifest.json"
            manifest_path.write_text('{"status":"complete"}\n', encoding="utf-8")
            args.round = 0
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)):
                with self.assertRaisesRegex(ValueError, "round must be positive"):
                    runner.execute(args)
            validation_manifest = json.loads(manifest_path.read_bytes())
            self.assertEqual(validation_manifest["status"], "failed")
            self.assertEqual(validation_manifest["error"]["type"], "ValueError")

            args.round = 1
            manifest_path.write_text('{"status":"complete"}\n', encoding="utf-8")
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)), mock.patch.object(
                runner, "_input_lineage", return_value={"seed": 42}
            ), mock.patch.object(
                runner, "_collect_environment", side_effect=RuntimeError("environment unavailable")
            ):
                with self.assertRaisesRegex(RuntimeError, "environment unavailable"):
                    runner.execute(args)
            environment_manifest = json.loads(manifest_path.read_bytes())
        self.assertEqual(environment_manifest["status"], "failed")
        self.assertEqual(environment_manifest["error"]["message"], "environment unavailable")

    def test_execute_records_complete_input_lineage_and_cache_state(self):
        """阻止完成 manifest 遗漏可复核的数据 lineage 或固定缓存状态。"""
        rows, truth, args = self._execution_fixture()
        lineage = {
            "audit": {"path": "/audit/audit.json", "sha256": "audit-sha"},
            "block_count": 5,
            "block_size": 1,
            "dataset": {"bytes": 2, "sha256": "dataset-sha"},
            "record_count": 5,
            "seed": 42,
            "source_input": {"path": "/source.jsonl", "sha256": "source-sha"},
            "truth": {"bytes": 2, "sha256": "truth-sha"},
            "watermarks": [1, 2, 3, 4, 5],
        }
        with tempfile.TemporaryDirectory() as temporary:
            self._write_input_fixture(temporary, args)
            args.output = temporary
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)), mock.patch.object(
                runner, "_input_lineage", return_value=lineage, create=True
            ), mock.patch.object(runner, "_collect_environment", return_value={}), mock.patch.object(
                runner, "_adapter_for", return_value=FakeEngineAdapter()
            ):
                runner.execute(args)
            manifest = json.loads((Path(temporary) / "run-manifest.json").read_bytes())
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["cache_state"], "query_warmup_1_no_os_cache_drop")
        self.assertEqual(manifest["input"], lineage)

    def test_lineage_mismatch_publishes_failed_preflight_manifest(self):
        """阻止 audit、source 或上游 identity 失配后继续正式轮次。"""
        rows, truth, args = self._execution_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            self._write_input_fixture(temporary, args)
            args.output = temporary
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)), mock.patch.object(
                runner, "_input_lineage", side_effect=ValueError("audit artifact identity mismatch"), create=True
            ):
                with self.assertRaisesRegex(ValueError, "audit artifact identity mismatch"):
                    runner.execute(args)
            manifest = json.loads((Path(temporary) / "run-manifest.json").read_bytes())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error"]["message"], "audit artifact identity mismatch")

    def test_input_lineage_validates_source_audit_and_upstream_seed(self):
        """阻止无来源 seed、审计篡改或 source SHA 偏差进入顶层 manifest。"""
        with tempfile.TemporaryDirectory() as temporary:
            input_dir, rows, truth, source_path, upstream_path = self._lineage_fixture(temporary)

            with mock.patch.object(runner, "FROZEN_SOURCE_PATH", source_path.resolve(), create=True), mock.patch.object(
                runner, "FROZEN_SOURCE_SHA256", runner.common.file_identity(source_path)["sha256"], create=True
            ), mock.patch.object(
                runner, "FROZEN_UPSTREAM_MANIFEST_SHA256", runner.common.file_identity(upstream_path)["sha256"], create=True
            ):
                lineage = runner._input_lineage(input_dir, rows, truth)
            self.assertEqual(lineage["seed"], 42)
            self.assertEqual(lineage["record_count"], 2)
            self.assertEqual(lineage["watermarks"], [1, 2])

            manifest_path = input_dir / "run-manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["audit"]["sha256"] = "wrong"
            manifest_path.write_bytes(runner.common.canonical_bytes(manifest) + b"\n")
            with mock.patch.object(runner, "FROZEN_SOURCE_PATH", source_path.resolve(), create=True), mock.patch.object(
                runner, "FROZEN_SOURCE_SHA256", runner.common.file_identity(source_path)["sha256"], create=True
            ), mock.patch.object(
                runner, "FROZEN_UPSTREAM_MANIFEST_SHA256", runner.common.file_identity(upstream_path)["sha256"], create=True
            ):
                with self.assertRaisesRegex(ValueError, "audit artifact identity mismatch"):
                    runner._input_lineage(input_dir, rows, truth)

    def test_self_consistent_unfrozen_lineage_publishes_failed_manifest(self):
        """阻止伪造 source、upstream 与 seed=42 的自洽链绕过冻结输入锚点。"""
        with tempfile.TemporaryDirectory() as temporary:
            input_dir, rows, truth, _source_path, _upstream_path = self._lineage_fixture(temporary)
            _rows, _expected_truth, args = self._execution_fixture()
            args.input = input_dir
            args.output = Path(temporary) / "result"
            with mock.patch.object(runner.common, "verify_input", return_value=(rows, truth)):
                with self.assertRaisesRegex(ValueError, "frozen source input mismatch"):
                    runner.execute(args)
            manifest = json.loads((args.output / "run-manifest.json").read_bytes())
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["error"]["type"], "ValueError")

    def test_container_identity_records_repo_digest_without_environment_reads(self):
        """阻止容器身份只有 image ID 而缺少可发布的仓库 digest。"""
        inspect = CompletedProcess([], 0, 'image:tag|sha256:image|{}|0|0|true\n', "")
        image = CompletedProcess([], 0, '["registry/image@sha256:digest"]\n', "")
        with mock.patch.object(runner.subprocess, "run", side_effect=[inspect, image]):
            identity = runner._safe_container_identity("container")
        self.assertEqual(identity["repo_digests"], ["registry/image@sha256:digest"])

    def test_collect_environment_uses_workspace_root_from_linked_worktree(self):
        """阻止 linked worktree 将 .worktrees 误判为上游仓目录。"""
        args = SimpleNamespace(
            engine="opengauss", container_name="agent-trace-opengauss-v6", host="127.0.0.1", port=15432
        )
        environment = runner._collect_environment(args)

        repositories = environment["repositories"]
        self.assertTrue(Path(repositories["research"]["path"]).is_dir())
        self.assertTrue(Path(repositories["exporter_demo"]["path"]).is_dir())
        self.assertTrue(Path(repositories["trace_synthesis"]["path"]).is_dir())
        self.assertNotIn(".worktrees", repositories["exporter_demo"]["path"])
        self.assertNotEqual(repositories["research"]["head"], "unavailable")

    @staticmethod
    def _capture_error(errors, function, *args):
        """在线程中保存预期失败，供有界 join 断言使用。"""
        try:
            function(*args)
        except Exception as error:
            errors.append(error)

    @staticmethod
    def _write_input_fixture(temporary, args):
        """写入 execute 在 manifest identity 中读取的最小输入文件。"""
        input_dir = Path(temporary) / "input"
        input_dir.mkdir(exist_ok=True)
        (input_dir / "dataset.jsonl").write_text("{}\n", encoding="utf-8")
        (input_dir / "truth-manifest.json").write_text("{}\n", encoding="utf-8")
        args.input = input_dir

    @staticmethod
    def _execution_fixture():
        """构造满足五个 block 启动条件的最小 execute 输入。"""
        rows = [
            {"ingest_seq": index, "raw_event": "x", "event_id": f"event-{index}"}
            for index in range(5)
        ]
        truth = {
            "block_size": 1,
            "native_json": {"path_budget": 1},
            "key_map": {},
            "parameters": {query_id: {"project_id": "project"} for query_id in ("Q01", "Q02", "Q03", "Q04", "Q05")},
            "queries": {
                query_id: {
                    str(watermark): {"result_sha256": "digest-ok", "row_count": 1}
                    for watermark in range(1, 6)
                }
                for query_id in ("Q01", "Q02", "Q03", "Q04", "Q05")
            },
            "records": [],
            "watermarks": list(range(1, 6)),
        }
        args = SimpleNamespace(
            input="unused", output=None, engine="opengauss", container_name="unused",
            namespace="json_s2_test", round=1, layout_order="og_jsonb,og_jsonb_hot,og_jsonb_gin",
            measurements=1, block_size=1, query_workers=2, maintenance_timeout_seconds=1,
            host="127.0.0.1", port=15432,
        )
        return rows, truth, args

    @staticmethod
    def _lineage_fixture(temporary):
        """创建独立的 generator、audit 与上游 lineage 文件。"""
        root = Path(temporary)
        input_dir = root / "input"
        audit_dir = root / "audit"
        source_path = root / "source.jsonl"
        upstream_path = root / "upstream-manifest.json"
        input_dir.mkdir()
        audit_dir.mkdir()
        source_path.write_bytes(b'{"trace_id":"t"}\n')
        upstream = {
            "seed": 42,
            "shards": [{"file": source_path.name, "sha256": runner.common.file_identity(source_path)["sha256"], "span_count": 2}],
            "status": "complete",
        }
        upstream_path.write_bytes(runner.common.canonical_bytes(upstream) + b"\n")
        audit_path = audit_dir / "audit.json"
        audit_path.write_bytes(b"{}\n")
        audit_manifest = {
            "artifacts": {"audit.json": runner.common.file_identity(audit_path)},
            "data_path": runner.common.DATA_PATH,
            "input": {
                "path": str(source_path),
                "sha256": runner.common.file_identity(source_path)["sha256"],
                "upstream_manifest": {"path": str(upstream_path), "sha256": runner.common.file_identity(upstream_path)["sha256"]},
            },
            "status": "complete",
        }
        (audit_dir / "run-manifest.json").write_bytes(runner.common.canonical_bytes(audit_manifest) + b"\n")
        dataset_path = input_dir / "dataset.jsonl"
        truth_path = input_dir / "truth-manifest.json"
        dataset_path.write_bytes(b"{}\n")
        truth_path.write_bytes(b"{}\n")
        rows = [{"event_id": "a"}, {"event_id": "b"}]
        truth = {"block_count": 2, "block_size": 1, "record_count": 2, "watermarks": [1, 2]}
        generator_manifest = {
            "artifacts": {"dataset.jsonl": runner.common.file_identity(dataset_path), "truth-manifest.json": runner.common.file_identity(truth_path)},
            "audit": {"path": str(audit_path), "sha256": runner.common.file_identity(audit_path)["sha256"]},
            "block_count": 2,
            "block_size": 1,
            "data_path": runner.common.DATA_PATH,
            "input": {"path": str(source_path), "sha256": runner.common.file_identity(source_path)["sha256"]},
            "record_count": 2,
            "status": "complete",
            "watermarks": [1, 2],
        }
        (input_dir / "run-manifest.json").write_bytes(runner.common.canonical_bytes(generator_manifest) + b"\n")
        return input_dir, rows, truth, source_path, upstream_path


if __name__ == "__main__":
    unittest.main()
