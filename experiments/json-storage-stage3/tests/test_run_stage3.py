import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import QuerySpec, TruthCatalog, canonical_digest
import production
import run_stage3


def file_identity(path):
    content = Path(path).read_bytes()
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def formal_fixture(root):
    truth = SimpleNamespace(
        seed=20260907, identity_sha256="a" * 64, record_count=48_534,
        block_size=256, block_count=190,
    )
    queries = (
        (QuerySpec("list", {"limit": 256}), SimpleNamespace(scenario="list:first")),
        (QuerySpec("batch", {"cohort": "main"}), SimpleNamespace(scenario="batch:main")),
    )
    return SimpleNamespace(
        root=Path(root).resolve(), truth=truth,
        identity={"kind": "formal", "identity_sha256": "b" * 64},
        main_queries=queries,
    )


def query_digest(formal):
    return canonical_digest([
        {"scenario": item.scenario, "kind": query.kind, "parameters": query.parameters}
        for query, item in formal.main_queries
    ])


def valid_child(formal, namespace="jsons3_candidate_0123456789"):
    query_ids = ("query-1", "query-2")
    return {
        "format": "agent-trace-json-storage-stage3-layout-run",
        "format_version": 1,
        "run_id": "jsons3-clickhouse-asset_ref-r1-0123456789",
        "status": "complete",
        "engine": "clickhouse",
        "layout": "asset_ref",
        "workload": "main",
        "round_index": 0,
        "round_order": ["same_table", "separate", "full_core", "asset_ref"],
        "seed": 20260907,
        "measurements": 30,
        "batch_measurements": 5,
        "input": dict(formal.identity),
        "query_catalog_sha256": query_digest(formal),
        "write": {
            "row_count": 48_534, "block_count": 190, "final_watermark": 48_534,
            "blocks": [
                {
                    "ingest": {"rows": 256 if index < 189 else 150,
                               "watermark": min((index + 1) * 256, 48_534),
                               "watermarks": {"assets": min((index + 1) * 256, 48_534),
                                                  "events_analytics": min((index + 1) * 256, 48_534)}},
                    "visible": {"completed": True,
                                "watermarks": {"assets": min((index + 1) * 256, 48_534),
                                                   "events_analytics": min((index + 1) * 256, 48_534)}},
                }
                for index in range(190)
            ],
        },
        "dataset_audit": {"row_count": 48_534},
        "storage": {"tables": {"assets": {}, "events_analytics": {}}, "asset_store": {}},
        "access": {
            "plans": {query_id: "plan" for query_id in query_ids},
            "query_finish": {query_id: {"read_rows": 1, "read_bytes": 1} for query_id in query_ids},
            "query_details": {query_id: {"kind": "list"} for query_id in query_ids},
        },
        "correctness": {
            "truth_identity": formal.truth.identity_sha256,
            "formal_samples": 2, "successful_samples": 2, "failed_samples": 0,
            "response_bytes_validated": True,
        },
        "maintenance": {
            "completed": True, "natural_stable_parts": True, "optimize_final": False,
            "watermarks": {"assets": 48_534, "events_analytics": 48_534},
        },
        "cleanup": {"namespace": namespace, "removed": True, "asset_directory_removed": True},
        "code": {"runner": {"path": "/runner.py", "bytes": 1, "sha256": "c" * 64}},
        "engine_runtime": {"version": "25.12", "source": "database-query"},
        "container": {"container": "agent-trace-clickhouse-25-12", "image": "clickhouse", "image_id": "sha256:id"},
        "host": {"platform": "test", "machine": "x86_64", "cpu_count": 1, "memory_total_kib": 1},
    }


def write_child(output, formal, mutate=None):
    child = output / "child"
    child.mkdir(parents=True)
    manifest = valid_child(formal)
    if mutate is not None:
        mutate(manifest)
    run_stage3.write_manifest_atomic(child / "run-manifest.json", manifest)
    samples = (
        {"status": "success", "query_id": "query-1"},
        {"status": "success", "query_id": "query-2"},
    )
    (child / "samples.jsonl").write_text("".join(json.dumps(row) + "\n" for row in samples))


class ParserAndLifecycleTests(unittest.TestCase):
    def test_parser_rejects_unpublished_commands_and_tunable_production_arguments(self):
        """捕获提前暴露 7G2 命令或允许调用方改写正式常量。"""
        parser = run_stage3.build_parser()
        for argv in (
            ["part-states", "--input", "i", "--output", "o"],
            ["candidate", "--input", "i", "--output", "o", "--seed", "1"],
            ["generate-input", "--source", "s", "--output", "o", "--engine", "clickhouse"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        self.assertEqual(parser.parse_args(["candidate", "--input", "i", "--output", "o"]).operation,
                         "candidate")

    def test_existing_output_stops_before_any_input_or_database_work(self):
        """捕获空目录复用及拒绝前读取输入、生成 payload 或创建 adapter。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "exists"
            output.mkdir()
            with patch.object(run_stage3, "load_formal_input") as loader, \
                 patch.object(run_stage3, "build_truth") as generator, \
                 patch.object(run_stage3, "create_adapter") as adapter:
                with self.assertRaises(FileExistsError):
                    run_stage3.main(["candidate", "--input", str(root / "input"),
                                     "--output", str(output)])
            loader.assert_not_called()
            generator.assert_not_called()
            adapter.assert_not_called()

    def test_running_is_published_before_work_and_failure_preserves_staging(self):
        """捕获工作先于 running 发布、异常被吞掉或 partial staging 被清理。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "attempt-1"

            def failing_generator(source, staging, seed):
                running = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(running["status"], "running")
                self.assertEqual(seed, 20260907)
                staging.mkdir()
                (staging / "partial").write_text("evidence")
                raise LookupError("generator exploded")

            with patch.object(run_stage3, "build_truth", side_effect=failing_generator):
                with self.assertRaisesRegex(LookupError, "generator exploded"):
                    run_stage3.main(["generate-input", "--source", str(root / "source"),
                                     "--output", str(output)])
            failed = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], {"type": "LookupError", "message": "generator exploded"})
            self.assertTrue((output / ".generating" / "partial").is_file())
            self.assertTrue(failed["cleanup"]["staging_exists"])


class GenerateInputTests(unittest.TestCase):
    def test_generation_stages_then_publishes_manifest_last_and_records_independent_identity(self):
        """捕获生成物直接写最终目录、manifest 提前发布或 child 身份自报。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "attempt-1"
            source = root / "source"
            source.mkdir()
            formal = formal_fixture(output)
            moves = []

            def fake_build(source_arg, staging, seed):
                self.assertEqual(source_arg, source.resolve())
                (staging / "payloads").mkdir(parents=True)
                (staging / "payloads" / "one.json").write_text("{}")
                (staging / "events.jsonl").write_text("{}\n")
                (staging / "truth.json").write_text("{}\n")
                run_stage3.write_manifest_atomic(staging / "generation-manifest.json", {
                    "format": "agent-trace-json-storage-stage3-generation",
                    "format_version": 1, "status": "complete",
                })

            def fake_load(path):
                self.assertEqual(path, output)
                self.assertTrue((path / "payloads" / "one.json").is_file())
                self.assertTrue((path / "generation-manifest.json").is_file())
                return formal

            original_move = run_stage3._move_new

            def recording_move(source_path, destination):
                moves.append(destination.name)
                return original_move(source_path, destination)

            with patch.object(run_stage3, "build_truth", side_effect=fake_build), \
                 patch.object(run_stage3, "load_formal_input", side_effect=fake_load), \
                 patch.object(run_stage3, "_move_new", side_effect=recording_move):
                self.assertEqual(run_stage3.main(["generate-input", "--source", str(source),
                                                  "--output", str(output)]), 0)
            manifest = json.loads((output / "run-manifest.json").read_text())
            child_path = output / manifest["child"]["path"]
            self.assertEqual(moves[-1], "generation-manifest.json")
            self.assertFalse((output / ".generating").exists())
            self.assertTrue(manifest["cleanup"]["staging_removed"])
            self.assertEqual({key: manifest["child"][key] for key in ("bytes", "sha256")},
                             file_identity(child_path))
            self.assertEqual(manifest["status"], "complete")

    def test_generation_final_verification_crosses_real_formal_loader_boundary(self):
        """捕获 generate 以替代校验绕过正式 input/truth/payload loader。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "attempt-1"
            source = root / "source"
            source.mkdir()
            truth = TruthCatalog(
                seed=20260907, source={}, record_count=48_534, block_size=256,
                block_count=190, watermarks=(), identity_sha256="a" * 64,
                query_window={}, payloads=(), cohorts={}, representative_traces={},
                detail_samples=(),
            )
            query_cases = ((QuerySpec("list", {"limit": 256}),
                            SimpleNamespace(scenario="list:first", rows=())),)

            def fake_build(source_arg, staging, seed):
                (staging / "payloads").mkdir(parents=True)
                (staging / "events.jsonl").write_text("")
                (staging / "truth.json").write_text("{}\n")
                run_stage3.write_manifest_atomic(staging / "generation-manifest.json", {
                    "format": "agent-trace-json-storage-stage3-generation",
                    "format_version": 1, "status": "complete",
                })

            with patch.object(run_stage3, "build_truth", side_effect=fake_build), \
                 patch.object(production, "load_run_input", return_value=(
                     truth, (), {"kind": "formal", "identity_sha256": "b" * 64},
                 )), \
                 patch.object(production, "validate_formal_contract"), \
                 patch.object(production, "build_workload_events", return_value=()), \
                 patch.object(production, "validate_workload_contract", return_value={}), \
                 patch.object(production, "_main_blocks", return_value=()), \
                 patch.object(production, "workload_query_cases", return_value=query_cases), \
                 patch.object(run_stage3, "load_formal_input",
                              wraps=production.load_formal_input) as real_loader:
                self.assertEqual(run_stage3.main([
                    "generate-input", "--source", str(source), "--output", str(output),
                ]), 0)
            real_loader.assert_called_once_with(output)


class CandidateTests(unittest.TestCase):
    def run_candidate(self, root, mutate=None, gate_patch=None):
        input_root = root / "input"
        input_root.mkdir()
        output = root / "attempt-1"
        formal = formal_fixture(input_root)
        adapter = SimpleNamespace(namespace="jsons3_candidate_0123456789")

        def fake_create(engine, layout, namespace, formal_arg, asset_root, endpoints):
            self.assertEqual((engine, layout), ("clickhouse", "asset_ref"))
            self.assertRegex(namespace, r"^jsons3_candidate_[0-9a-f]{10}$")
            self.assertEqual(asset_root, output / "assets")
            self.assertIsInstance(endpoints, run_stage3.EngineEndpoints)
            adapter.namespace = namespace
            return adapter

        def fake_config(formal_arg, child, assets, command):
            self.assertEqual(child, output / "child")
            self.assertEqual(assets, output / "assets")
            self.assertEqual(tuple(command[:2]), (sys.executable, str(run_stage3.SCRIPT_PATH)))
            return SimpleNamespace(
                engine="clickhouse", layout="asset_ref", round_index=0,
                round_order=("same_table", "separate", "full_core", "asset_ref"),
                measurements=30, batch_measurements=5, workload="main",
            )

        def fake_run(adapter_arg, truth, config):
            running = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(running["status"], "running")
            write_child(output, formal, mutate)

        patches = [
            patch.object(run_stage3, "load_formal_input", return_value=formal),
            patch.object(run_stage3, "create_adapter", side_effect=fake_create),
            patch.object(run_stage3, "candidate_config", side_effect=fake_config),
            patch.object(run_stage3, "run_layout", side_effect=fake_run),
            patch.object(run_stage3.uuid, "uuid4", return_value=SimpleNamespace(hex="0123456789abcdef")),
        ]
        if gate_patch is not None:
            patches.append(patch.object(run_stage3, "_gate_candidate", side_effect=gate_patch))
        for context in patches:
            context.start()
        try:
            result = run_stage3.main(["candidate", "--input", str(input_root), "--output", str(output)])
        finally:
            for context in reversed(patches):
                context.stop()
        return result, output, formal

    def test_candidate_wires_fixed_factory_config_and_publishes_gated_child(self):
        """捕获 candidate 的 engine/layout/path/endpoint 漂移或未发布 child 身份。"""
        with tempfile.TemporaryDirectory() as directory:
            result, output, formal = self.run_candidate(Path(directory))
            self.assertEqual(result, 0)
            envelope = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(envelope["status"], "complete")
            self.assertEqual(envelope["truth"]["identity_sha256"], formal.truth.identity_sha256)
            self.assertEqual(envelope["query_catalog_sha256"], query_digest(formal))
            self.assertEqual(envelope["runtime"]["endpoint"], {"host": "127.0.0.1", "port": 18123})
            self.assertEqual(envelope["child"]["path"], "child/run-manifest.json")

    def test_candidate_gate_fails_closed_for_each_evidence_family(self):
        """捕获缺失或矛盾的正式 child 证据仍升级为 complete。"""
        mutations = {
            "format": lambda m: m.update(format="unknown"),
            "version": lambda m: m.update(format_version=2),
            "status": lambda m: m.update(status="running"),
            "failed_status": lambda m: m.update(status="failed"),
            "input": lambda m: m.update(input={"kind": "formal", "identity_sha256": "x"}),
            "truth": lambda m: m["correctness"].update(truth_identity="x"),
            "query": lambda m: m.update(query_catalog_sha256="x"),
            "write": lambda m: m["write"].update(final_watermark=48_533),
            "blocks": lambda m: m["write"].update(blocks=m["write"]["blocks"][:-1]),
            "audit": lambda m: m.pop("dataset_audit"),
            "storage": lambda m: m.pop("storage"),
            "plans": lambda m: m["access"].update(plans={}),
            "query_finish": lambda m: m["access"]["query_finish"].pop("query-2"),
            "details": lambda m: m["access"].update(query_details={}),
            "response": lambda m: m["correctness"].update(response_bytes_validated=False),
            "maintenance": lambda m: m["maintenance"].update(natural_stable_parts=False),
            "optimize": lambda m: m["maintenance"].update(optimize_final=True),
            "watermark": lambda m: m["maintenance"]["watermarks"].update(assets=48_533),
            "cleanup": lambda m: m["cleanup"].update(removed=False),
            "asset_cleanup": lambda m: m["cleanup"].update(asset_directory_removed=False),
            "container": lambda m: m.pop("container"),
            "runtime": lambda m: m.pop("engine_runtime"),
            "code": lambda m: m.pop("code"),
            "host": lambda m: m.pop("host"),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises((ValueError, RuntimeError)):
                    self.run_candidate(Path(directory), mutation)
                output = Path(directory) / "attempt-1"
                envelope = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(envelope["status"], "failed")
                self.assertTrue((output / "child" / "run-manifest.json").is_file())

    def test_child_reader_rejects_escape_non_object_and_invalid_json(self):
        """捕获越界 child、非法 JSON 或非 object 文档进入 production gate。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            outside = output.parent / "outside.json"
            outside.write_text("{}")
            for path in (outside, output / "missing"):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    run_stage3._read_child(output, path)
            child = output / "child.json"
            for content in (b"[]", b"{"):
                child.write_bytes(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    run_stage3._read_child(output, child)

    def test_child_digest_change_after_gate_prevents_complete(self):
        """捕获 child 在门禁后被替换但 envelope 仍发布旧身份。"""
        original_gate = run_stage3._gate_candidate

        def mutate_after_gate(child, formal, namespace):
            original_gate(child, formal, namespace)
            path = Path(child["_path"])
            value = json.loads(path.read_text())
            value["tampered"] = True
            run_stage3.write_manifest_atomic(path, value)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "changed after gate"):
                self.run_candidate(Path(directory), gate_patch=mutate_after_gate)
            envelope = json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())
            self.assertEqual(envelope["status"], "failed")

    def test_code_identity_uses_current_file_bytes_and_json_rejects_nan(self):
        """捕获以 mtime/分支冒充代码身份或发布非有限 JSON 数值。"""
        evidence = run_stage3._code_evidence("candidate")
        for item in evidence.values():
            self.assertEqual({key: item[key] for key in ("bytes", "sha256")},
                             file_identity(item["path"]))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_stage3.write_manifest_atomic(Path(directory) / "manifest.json", {
                    "status": "failed", "value": math.nan,
                })


if __name__ == "__main__":
    unittest.main()
