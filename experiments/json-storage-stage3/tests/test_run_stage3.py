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

from common import QuerySpec, TruthCatalog, build_layout_catalog, canonical_digest
import production
import run_interference
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
        identity={
            "kind": "formal", "identity_sha256": "b" * 64,
            "generation_manifest": {"bytes": 1, "sha256": "b" * 64},
        },
        main_queries=queries,
        main_blocks=(({"ingest_seq": 0},),),
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
        "dataset_audit": {
            "row_count": 48_534, "duplicate_event_ids": 0,
            "identity_sha256": formal.truth.identity_sha256,
            "payload_count": 160, "payload_bytes": 128_450_560,
            "logical_response_bytes": 1, "database_protocol_bytes": 1,
            "physical_targets": {
                "events_analytics": {
                    "row_count": 48_534, "duplicate_identities": 0,
                    "identity_metadata_sha256": "d" * 64,
                },
                "assets": {
                    "row_count": 160, "duplicate_identities": 0,
                    "identity_metadata_sha256": "e" * 64,
                    "event_mapping_count": 160, "event_mapping_sha256": "f" * 64,
                },
            },
        },
        "storage": {
            "tables": {
                "assets": {"part_count": 1, "rows": 160, "marks": 1,
                           "compressed_bytes": 1, "uncompressed_bytes": 1, "columns": {}},
                "events_analytics": {"part_count": 1, "rows": 48_534, "marks": 1,
                                     "compressed_bytes": 1, "uncompressed_bytes": 1,
                                     "columns": {}},
            },
            "merges": [],
            "asset_store": {"available_object_count": 160, "available_bytes": 128_450_560,
                            "orphan_object_count": 0, "orphan_bytes": 0},
        },
        "access": {
            "plans": {query_id: "plan" for query_id in query_ids},
            "query_finish": {
                query_id: {"type": "QueryFinish", "exception_code": 0,
                           "read_rows": 1, "read_bytes": 1}
                for query_id in query_ids
            },
            "query_details": {
                query_id: {"kind": "list", "statement": "SELECT event_id FROM events_analytics",
                           "payload_selected": False, "declared_source": "events_analytics",
                           "scanned_rows": 1, "scanned_bytes": 1,
                           "scanned_bytes_status": "observed"}
                for query_id in query_ids
            },
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
        "code": {
            role: {"path": f"/{role}.py", "bytes": 1, "sha256": "c" * 64}
            for role in ("runner", "common", "assets", "adapter")
        },
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


def valid_part_child(formal, layout, namespace):
    """构造通过 production gate 的 part-state child 证据。"""
    catalog = build_layout_catalog(layout)
    query_cases = tuple(formal.main_queries)
    states = []
    for state_name in ("fragmented", "merging", "stable", "single_part"):
        samples = []
        for query, truth in query_cases:
            for index in range(30):
                query_id = f"{state_name}-{truth.scenario}-{index}"
                samples.append({
                    "scenario": truth.scenario, "kind": query.kind, "status": "success",
                    "query_id": query_id, "response_bytes": 1,
                    "validation": {"row_count": 1, "validated_payload_bytes": 1},
                    "error": None,
                })
        query_ids = [sample["query_id"] for sample in samples]
        state = {
            "name": state_name, "controlled_table": catalog.list_source,
            "predicate_proven": True,
            "tables": {
                table: {"part_count": 1, "marks": 0, "compressed_bytes": 0,
                        "uncompressed_bytes": 0}
                for table in catalog.write_tables
            },
            "active_merges": [], "observations": [], "query_samples": samples,
            "query_plans": {query_id: "ReadFromMergeTree" for query_id in query_ids},
            "query_details": {
                query_id: {"kind": sample["kind"], "statement": "SELECT 1",
                           "declared_source": catalog.list_source, "scanned_rows": 1,
                           "scanned_bytes": 1}
                for query_id, sample in zip(query_ids, samples)
            },
            "query_finish": {
                query_id: {"type": "QueryFinish", "exception_code": 0,
                           "read_rows": 1, "read_bytes": 1}
                for query_id in query_ids
            },
            "successful_samples": len(samples), "failed_samples": 0,
            "query_finish_count": len(samples), "optimized_targets": (
                list(catalog.write_tables) if state_name == "single_part" else []
            ),
            "error": None,
        }
        if layout == "asset_ref":
            state["asset_store"] = {
                "available_object_count": 0, "available_bytes": 0,
                "orphan_object_count": 0, "orphan_bytes": 0,
            }
        states.append(state)
    return {
        "format": "agent-trace-json-storage-stage3-clickhouse-part-states",
        "format_version": 1, "run_id": "jsons3-clickhouse-parts-0123456789",
        "status": "complete", "layout": layout,
        "physical_targets": list(catalog.write_tables),
        "controlled_table": catalog.list_source,
        "state_order": ["fragmented", "merging", "stable", "single_part"],
        "samples_per_query": 30, "states": states,
        "restoration": {"attempted": True, "restored": True,
                        "targets": list(catalog.write_tables)},
        "cleanup": {"namespace": namespace, "removed": True},
    }


def write_part_child(output, formal, layout, namespace, mutate=None):
    """写入 part-state runner 的 child manifest。"""
    child = output / "child"
    child.mkdir(parents=True)
    manifest = valid_part_child(formal, layout, namespace)
    if mutate is not None:
        mutate(manifest)
    run_stage3.write_manifest_atomic(child / "run-manifest.json", manifest)


class ParserAndLifecycleTests(unittest.TestCase):
    def test_parser_rejects_unpublished_commands_and_tunable_production_arguments(self):
        """捕获 part-state 参数漂移或其他未发布操作进入 CLI。"""
        parser = run_stage3.build_parser()
        for argv in (
            ["part-states", "--input", "i", "--output", "o", "--samples", "1"],
            ["candidate", "--input", "i", "--output", "o", "--seed", "1"],
            ["generate-input", "--source", "s", "--output", "o", "--engine", "clickhouse"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        self.assertEqual(parser.parse_args(["candidate", "--input", "i", "--output", "o"]).operation,
                         "candidate")
        arguments = parser.parse_args([
            "part-states", "--input", "i", "--output", "o", "--layout", "same_table",
        ])
        self.assertEqual(arguments.operation, "part-states")
        self.assertEqual(arguments.layout, "same_table")

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
                formal.identity["generation_manifest"] = file_identity(
                    path / "generation-manifest.json",
                )
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
                 patch.object(production, "load_run_input", side_effect=lambda root: (
                     truth, (), {
                         "kind": "formal", "identity_sha256": "b" * 64,
                         "generation_manifest": file_identity(
                             output / "generation-manifest.json",
                         ),
                     },
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

    def test_generation_rejects_child_replaced_after_formal_loader_returns(self):
        """捕获 loader 验证后 generation child 状态或 bytes 被替换仍发布 complete。"""
        replacements = {
            "failed_status": {
                "format": "agent-trace-json-storage-stage3-generation",
                "format_version": 1, "status": "failed",
            },
            "changed_bytes": {
                "format": "agent-trace-json-storage-stage3-generation",
                "format_version": 1, "status": "complete", "tampered": True,
            },
        }
        for name, replacement in replacements.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "attempt-1"
                source = root / "source"
                source.mkdir()

                def fake_build(source_arg, staging, seed):
                    (staging / "payloads").mkdir(parents=True)
                    (staging / "events.jsonl").write_text("")
                    (staging / "truth.json").write_text("{}\n")
                    run_stage3.write_manifest_atomic(staging / "generation-manifest.json", {
                        "format": "agent-trace-json-storage-stage3-generation",
                        "format_version": 1, "status": "complete",
                    })

                def load_then_replace(path):
                    formal = formal_fixture(path)
                    formal.identity["generation_manifest"] = file_identity(
                        path / "generation-manifest.json",
                    )
                    run_stage3.write_manifest_atomic(path / "generation-manifest.json", replacement)
                    return formal

                with patch.object(run_stage3, "build_truth", side_effect=fake_build), \
                     patch.object(run_stage3, "load_formal_input", side_effect=load_then_replace):
                    with self.assertRaisesRegex(RuntimeError, "generation child"):
                        run_stage3.main([
                            "generate-input", "--source", str(source), "--output", str(output),
                        ])
                envelope = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(envelope["status"], "failed")

    def test_generation_failure_after_loader_preserves_available_identities(self):
        """捕获 loader 成功后的 staging/child 故障丢失 formal 身份与 query digest。"""
        for failure in ("rmdir", "child"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "attempt-1"
                source = root / "source"
                source.mkdir()
                formal = formal_fixture(output)

                def fake_build(source_arg, staging, seed):
                    (staging / "payloads").mkdir(parents=True)
                    (staging / "events.jsonl").write_text("")
                    (staging / "truth.json").write_text("{}\n")
                    run_stage3.write_manifest_atomic(staging / "generation-manifest.json", {
                        "format": "agent-trace-json-storage-stage3-generation",
                        "format_version": 1, "status": "complete",
                    })

                def fake_load(path):
                    formal.identity["generation_manifest"] = file_identity(
                        path / "generation-manifest.json",
                    )
                    return formal

                original_rmdir = Path.rmdir

                def failing_rmdir(path):
                    if failure == "rmdir" and path == output / ".generating":
                        raise OSError("staging rmdir failed")
                    return original_rmdir(path)

                read_effect = RuntimeError("child read failed") if failure == "child" else None
                contexts = [
                    patch.object(run_stage3, "build_truth", side_effect=fake_build),
                    patch.object(run_stage3, "load_formal_input", side_effect=fake_load),
                    patch.object(Path, "rmdir", failing_rmdir),
                ]
                if read_effect is not None:
                    contexts.append(patch.object(run_stage3, "_read_child", side_effect=read_effect))
                for context in contexts:
                    context.start()
                try:
                    with self.assertRaises((OSError, RuntimeError)):
                        run_stage3.main([
                            "generate-input", "--source", str(source), "--output", str(output),
                        ])
                finally:
                    for context in reversed(contexts):
                        context.stop()
                envelope = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(envelope["input"], formal.identity)
                self.assertEqual(envelope["truth"]["identity_sha256"],
                                 formal.truth.identity_sha256)
                self.assertEqual(envelope["query_catalog_sha256"], query_digest(formal))


class CandidateTests(unittest.TestCase):
    def run_candidate(self, root, mutate=None, gate_patch=None, run_error=None,
                      create_error=None):
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
            if create_error is not None:
                raise create_error
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
            if run_error is not None:
                raise run_error

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

    def test_candidate_failure_preserves_identities_and_namespace_when_available(self):
        """捕获 create/run/gate 故障丢失已验证 formal 身份或本次 namespace。"""
        failures = {
            "create": {"create_error": RuntimeError("create failed")},
            "run": {"run_error": RuntimeError("run failed")},
            "gate": {"gate_patch": RuntimeError("gate failed")},
        }
        for name, options in failures.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                with self.assertRaisesRegex(RuntimeError, f"{name} failed"):
                    self.run_candidate(root, **options)
                envelope = json.loads((root / "attempt-1" / "run-manifest.json").read_text())
                formal = formal_fixture(root / "input")
                self.assertEqual(envelope["input"], formal.identity)
                self.assertEqual(envelope["truth"]["identity_sha256"],
                                 formal.truth.identity_sha256)
                self.assertEqual(envelope["query_catalog_sha256"], query_digest(formal))
                self.assertEqual(envelope["namespace_policy"], {
                    "strategy": "unique-random-suffix", "prefix": "jsons3_candidate_",
                    "namespace": "jsons3_candidate_0123456789", "reuse": False,
                })

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

    def test_candidate_gate_rejects_bool_float_and_non_integer_counts(self):
        """捕获 bool/float 借助 Python 数值相等规则通过固定整数门禁。"""
        mutations = {
            "bool_version": lambda m: m.update(format_version=True),
            "bool_round": lambda m: m.update(round_index=False),
            "float_measurements": lambda m: m.update(measurements=30.0),
            "float_write": lambda m: m["write"].update(row_count=48_534.0),
            "float_block_rows": lambda m: m["write"]["blocks"][0]["ingest"].update(rows=256.0),
            "float_block_watermark": lambda m: m["write"]["blocks"][0]["visible"][
                "watermarks"
            ].update(assets=256.0),
            "bool_failed_samples": lambda m: m["correctness"].update(failed_samples=False),
            "float_formal_samples": lambda m: m["correctness"].update(formal_samples=2.0),
            "float_final_watermark": lambda m: m["maintenance"]["watermarks"].update(
                assets=48_534.0,
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "candidate"):
                    self.run_candidate(Path(directory), mutation)

    def test_candidate_gate_rejects_empty_or_malformed_nested_evidence(self):
        """捕获空 plan/QueryFinish/detail 与空审计、存储和运行身份进入 complete。"""
        mutations = {
            "empty_plan": lambda m: m["access"]["plans"].update(**{"query-1": ""}),
            "empty_query_finish": lambda m: m["access"]["query_finish"].update(
                **{"query-1": {}},
            ),
            "wrong_query_finish_type": lambda m: m["access"]["query_finish"][
                "query-1"
            ].update(type="QueryStart"),
            "negative_query_finish": lambda m: m["access"]["query_finish"][
                "query-1"
            ].update(read_bytes=-1),
            "empty_query_detail": lambda m: m["access"]["query_details"].update(
                **{"query-1": {}},
            ),
            "empty_dataset_target": lambda m: m["dataset_audit"]["physical_targets"].update(
                assets={},
            ),
            "empty_storage_table": lambda m: m["storage"]["tables"].update(assets={}),
            "empty_asset_storage": lambda m: m["storage"].update(asset_store={}),
            "empty_code_entry": lambda m: m["code"].update(runner={}),
            "empty_runtime_version": lambda m: m["engine_runtime"].update(version=""),
            "empty_host_platform": lambda m: m["host"].update(platform=""),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "candidate"):
                    self.run_candidate(Path(directory), mutation)

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

    def test_failed_child_read_race_preserves_original_business_exception(self):
        """捕获 child snapshot 的竞态 OSError 掩盖数据库 runner 原异常。"""
        business_error = LookupError("database operation failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_path = root / "attempt-1" / "child" / "run-manifest.json"
            original_read = Path.read_bytes

            def racing_read(path):
                if path == child_path:
                    raise FileNotFoundError("child vanished")
                return original_read(path)

            with patch.object(Path, "read_bytes", racing_read):
                with self.assertRaises(LookupError) as raised:
                    self.run_candidate(root, run_error=business_error)
            self.assertIs(raised.exception, business_error)
            self.assertIn("child snapshot", " ".join(business_error.__notes__))
            failed = json.loads((root / "attempt-1" / "run-manifest.json").read_text())
            self.assertEqual(failed["error"], {
                "type": "LookupError", "message": "database operation failed",
            })

    def test_failed_child_snapshot_keeps_identity_when_json_is_invalid(self):
        """捕获 child bytes 已读取后因 JSON 非法丢失可用 SHA/bytes 身份。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "attempt-1"
            child = output / "child" / "run-manifest.json"
            child.parent.mkdir(parents=True)
            child.write_bytes(b"{")
            evidence, error = run_stage3._failed_child(output, "candidate")
            self.assertEqual(evidence, {
                "path": "child/run-manifest.json",
                "bytes": 1,
                "sha256": hashlib.sha256(b"{").hexdigest(),
            })
            self.assertIsInstance(error, ValueError)

    def test_failed_manifest_write_error_is_not_the_primary_exception(self):
        """捕获 failed envelope 写失败替换原业务异常对象与消息。"""
        business_error = LookupError("database operation failed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envelope_path = root / "attempt-1" / "run-manifest.json"
            original_writer = run_stage3.write_manifest_atomic

            def failing_writer(path, manifest):
                if Path(path) == envelope_path and manifest.get("status") == "failed":
                    raise OSError("failed publication unavailable")
                return original_writer(path, manifest)

            with patch.object(run_stage3, "write_manifest_atomic", side_effect=failing_writer):
                with self.assertRaises(LookupError) as raised:
                    self.run_candidate(root, run_error=business_error)
            self.assertIs(raised.exception, business_error)
            self.assertEqual(str(raised.exception), "database operation failed")
            self.assertIn("failed envelope publication", " ".join(business_error.__notes__))

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


class PartStateProductionTests(unittest.TestCase):
    """验证 part-state CLI 的固定生产接线与独立 child gate。"""

    def run_part_states_cli(self, root, layout="same_table", mutate=None,
                            runtime=None, runner_error=None, gate_patch=None):
        """以真实 CLI envelope 运行受控的 part-state child 替身。"""
        output = root / "attempt-1"
        input_root = root / "input"
        formal = formal_fixture(input_root)
        namespace = f"jsons3_parts_{layout}_0123456789"
        database = f"{namespace}_{layout}"
        calls = []

        def fake_create(engine, layout_arg, namespace_arg, formal_arg, asset_root, endpoints):
            self.assertEqual((engine, layout_arg, namespace_arg), ("clickhouse", layout, namespace))
            self.assertIs(formal_arg, formal)
            self.assertIsInstance(endpoints, production.EngineEndpoints)
            self.assertEqual(asset_root, output / "assets" if layout == "asset_ref" else None)
            return SimpleNamespace(database=database)

        def fake_runner(adapter, blocks, query_cases, child_root, **kwargs):
            running = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(running["status"], "running")
            self.assertEqual(blocks, formal.main_blocks)
            self.assertEqual(query_cases, formal.main_queries)
            self.assertEqual(kwargs, {"samples_per_query": 30})
            self.assertEqual(child_root, output / "child")
            if layout == "asset_ref":
                (output / "assets").mkdir()
            write_part_child(output, formal, layout, database, mutate)
            calls.append("runner")
            if runner_error is not None:
                raise runner_error

        runtime_value = runtime or {"version": "25.12", "source": "database-query"}
        patches = [
            patch.object(run_stage3, "load_formal_input", return_value=formal),
            patch.object(run_stage3, "create_adapter", side_effect=fake_create),
            patch.object(run_stage3, "part_state_inputs", return_value=(
                formal.main_blocks, formal.main_queries,
            )),
            patch.object(run_stage3, "run_part_states", side_effect=fake_runner, create=True),
            patch.object(run_stage3.run_layout_matrix, "_engine_runtime", return_value=runtime_value),
            patch.object(run_stage3.run_layout_matrix, "_container_evidence", return_value={
                "container": "agent-trace-clickhouse-25-12", "image": "clickhouse", "image_id": "sha256:id",
            }),
            patch.object(run_stage3.run_layout_matrix, "_host_evidence", return_value={
                "platform": "test", "machine": "x86_64", "cpu_count": 1, "memory_total_kib": 1,
            }),
            patch.object(run_stage3.uuid, "uuid4", return_value=SimpleNamespace(hex="0123456789abcdef")),
        ]
        if gate_patch is not None:
            patches.append(patch.object(run_stage3, "_gate_part_states", side_effect=gate_patch,
                                        create=True))
        for context in patches:
            context.start()
        try:
            result = run_stage3.main([
                "part-states", "--input", str(input_root), "--output", str(output), "--layout", layout,
            ])
        finally:
            for context in reversed(patches):
                context.stop()
        return result, output, formal, calls

    def test_part_states_wires_all_layouts_with_only_fixed_runner_argument(self):
        """捕获四布局 asset 路径、namespace 或 runner 可调参数漂移。"""
        for layout in ("same_table", "separate", "full_core", "asset_ref"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                result, output, formal, calls = self.run_part_states_cli(Path(directory), layout)
                envelope = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(result, 0)
                self.assertEqual(calls, ["runner"])
                self.assertEqual(envelope["status"], "complete")
                self.assertEqual(envelope["namespace_policy"], {
                    "strategy": "unique-random-suffix", "prefix": f"jsons3_parts_{layout}_",
                    "namespace": f"jsons3_parts_{layout}_0123456789", "reuse": False,
                })
                self.assertEqual(envelope["runtime"]["engine"], "clickhouse")
                self.assertEqual(envelope["runtime"]["layout"], layout)
                self.assertEqual(envelope["child"]["path"], "child/run-manifest.json")
                self.assertEqual(envelope["cleanup"]["namespace"],
                                 f"jsons3_parts_{layout}_0123456789_{layout}")
                self.assertEqual(envelope["truth"]["identity_sha256"], formal.truth.identity_sha256)
                self.assertTrue(envelope["cleanup"]["asset_directory_removed"])
                self.assertEqual(envelope["cleanup"]["asset_directory_applicable"], layout == "asset_ref")
                self.assertIn("part_state_runner", envelope["code"])

    def test_part_states_formal_and_namespace_failures_publish_available_evidence(self):
        """捕获 formal 或 namespace 故障吞掉 running/输入身份/namespace policy。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "formal"
            with patch.object(run_stage3, "load_formal_input", side_effect=ValueError("bad input")):
                with self.assertRaisesRegex(ValueError, "bad input"):
                    run_stage3.main(["part-states", "--input", str(root / "input"),
                                     "--output", str(output), "--layout", "same_table"])
            self.assertEqual(json.loads((output / "run-manifest.json").read_text())["status"], "failed")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(run_stage3, "create_adapter", side_effect=RuntimeError("create failed")), \
                 patch.object(run_stage3, "load_formal_input", return_value=formal_fixture(root / "input")), \
                 patch.object(run_stage3.uuid, "uuid4", return_value=SimpleNamespace(hex="0123456789abcdef")):
                with self.assertRaisesRegex(RuntimeError, "create failed"):
                    run_stage3.main(["part-states", "--input", str(root / "input"),
                                     "--output", str(root / "namespace"), "--layout", "same_table"])
            failed = json.loads((root / "namespace" / "run-manifest.json").read_text())
            self.assertEqual(failed["input"]["kind"], "formal")
            self.assertEqual(failed["namespace_policy"]["namespace"], "jsons3_parts_same_table_0123456789")

    def test_part_state_gate_rejects_invalid_control_table_state_order_ids_and_metrics(self):
        """捕获 child 控制表、状态顺序、样本身份或 bool 指标伪装为完整证据。"""
        mutations = {
            "controlled_table": lambda manifest: manifest["states"][0].update(controlled_table="wrong"),
            "state_order": lambda manifest: manifest["states"].reverse(),
            "duplicate_id": lambda manifest: manifest["states"][0]["query_samples"][1].update(
                query_id=manifest["states"][0]["query_samples"][0]["query_id"],
            ),
            "bool_metric": lambda manifest: manifest["states"][0]["tables"]["events"].update(
                marks=True,
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "part-state"):
                    self.run_part_states_cli(Path(directory), mutate=mutation)

    def test_part_state_gate_requires_every_formal_query_in_each_state(self):
        """捕获每个 state 以重复 list 样本替代缺失 batch 后仍满足总数。"""
        def replace_batch_with_list(manifest):
            for state in manifest["states"]:
                for sample in state["query_samples"]:
                    if sample["kind"] == "batch":
                        sample.update(scenario="list:first", kind="list")
                        state["query_details"][sample["query_id"]]["kind"] = "list"

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "part-state"):
                self.run_part_states_cli(Path(directory), mutate=replace_batch_with_list)

    def test_part_state_gate_cross_checks_sample_detail_and_query_finish(self):
        """捕获同一 query 的 kind、扫描计数和结果行数证据相互冲突。"""
        def first_query(manifest):
            state = manifest["states"][0]
            sample = state["query_samples"][0]
            return (
                sample,
                state["query_details"][sample["query_id"]],
                state["query_finish"][sample["query_id"]],
            )

        mutations = {
            "detail_kind": lambda manifest: first_query(manifest)[1].update(kind="batch"),
            "scanned_rows": lambda manifest: first_query(manifest)[1].update(scanned_rows=7),
            "scanned_bytes": lambda manifest: first_query(manifest)[1].update(scanned_bytes=9),
            "read_rows_below_result": lambda manifest: first_query(manifest)[0]["validation"].update(
                row_count=2,
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "part-state"):
                    self.run_part_states_cli(Path(directory), mutate=mutation)

    def test_part_states_missing_runtime_and_asset_cleanup_failure_prevent_complete(self):
        """捕获运行身份缺失或独占 Asset 目录未删除仍发布 complete。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "runtime"):
                self.run_part_states_cli(Path(directory), runtime={"version": "", "source": "database-query"})
            self.assertEqual(json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())["status"],
                             "failed")

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(run_stage3, "_remove_asset_directory", side_effect=OSError("unlink failed"),
                              create=True):
                with self.assertRaisesRegex(OSError, "unlink failed"):
                    self.run_part_states_cli(Path(directory), layout="asset_ref")
            failed = json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())
            self.assertEqual(failed["status"], "failed")
            self.assertFalse(failed["cleanup"]["asset_directory_removed"])

    def test_part_states_child_replacement_and_runner_failure_keep_child_identity(self):
        """捕获 gate 后 child 替换或 runner 失败时丢失固定 child 身份。"""
        original_gate = getattr(run_stage3, "_gate_part_states", None)

        def replace_after_gate(child, formal, layout, namespace):
            original_gate(child, formal, layout, namespace)
            value = json.loads(Path(child["_path"]).read_text())
            value["tampered"] = True
            run_stage3.write_manifest_atomic(Path(child["_path"]), value)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "changed after gate"):
                self.run_part_states_cli(Path(directory), gate_patch=replace_after_gate)
            self.assertEqual(json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())["status"],
                             "failed")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "runner failed"):
                self.run_part_states_cli(Path(directory), runner_error=RuntimeError("runner failed"))
            failed = json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())
            self.assertEqual(failed["child"]["path"], "child/run-manifest.json")


def interference_raw_sample(stream, sequence, query_id=None):
    """构造一条可独立门禁的成功 raw 请求。"""
    nested = None
    if stream == "continuous_ingest":
        nested = {"rows": 256, "watermark": 48_790,
                  "watermarks": {"events_analytics": 48_790}, "wall_ms": 1.0,
                  "write_target_ms": {}, "asset_publish_ms": 0.0,
                  "logical_target_row_bytes": {}, "database_ingest_request_body_bytes": {},
                  "asset_raw_object_bytes": 0}
    else:
        kind, scenario = run_interference.QUERY_STREAMS[stream]
        nested = {"scenario": scenario or f"{stream}:first", "kind": kind,
                  "status": "success", "query_id": query_id,
                  "response_bytes": 1, "validation": {
                      "row_count": 1, "validated_payload_bytes": 0,
                  }}
    return {"stream": stream, "sequence": sequence,
            "scheduled_offset_seconds": float(sequence), "started_offset_seconds": float(sequence),
            "completed_offset_seconds": float(sequence) + 0.1, "duration_ms": 100.0,
            "application_ready_ms": 100.0, "status": "success", "late_by_ms": 0.0,
            "sample": nested, "error": None}


def valid_interference_tree(output, layout="same_table"):
    """写入小型 raw fixture；高层测试单独替换完整 raw parser。"""
    child_root = output / "child"
    child_root.mkdir(parents=True)
    root = {"format": "agent-trace-json-storage-stage3-interference-run",
            "format_version": 1, "status": "complete", "seed": 20260907,
            "execution_scope": "formal", "classification": "formal_complete",
            "phase_order": [phase.name for phase in run_interference.FIXED_PHASES], "phases": []}
    parsed = {}
    query_counter = 0
    for phase in run_interference.FIXED_PHASES:
        schedules = {
            segment: run_interference._json_value(run_interference.fixed_phase_schedules(
                phase, measurement=segment == "measurement"))
            for segment in ("warmup", "measurement")
        }
        records = {}
        all_queries = []
        summaries = {}
        for segment, filename in (("warmup", "warmup-samples.jsonl"),
                                  ("measurement", "samples.jsonl")):
            segment_records = []
            streams = {}
            for stream, schedule in schedules[segment].items():
                query_counter += 1
                row = interference_raw_sample(stream, 0, f"query-{query_counter}")
                segment_records.append(row)
                scheduled = (math.ceil(schedule["duration_seconds"] * schedule["rate_per_second"])
                             if schedule["mode"] == "fixed" else 1)
                counts = {"scheduled_requests": scheduled, "started_requests": scheduled,
                          "completed_requests": scheduled, "successful_requests": scheduled,
                          "failed_requests": 0, "timed_out_requests": 0,
                          "dropped_requests": 0, "late_requests": 0}
                streams[stream] = {"counts": counts, "successful_queries": (
                    [] if stream == "continuous_ingest" else [row["sample"]]
                )}
                if stream != "continuous_ingest":
                    all_queries.append(row["sample"])
            records[filename] = segment_records
            summaries[segment] = streams
        query_ids = [sample["query_id"] for sample in all_queries]
        catalog = build_layout_catalog(layout)
        snapshot = {"captured_offset_seconds": 0.0,
                    "resources": {"cpu": {"status": "available", "ticks": [1]},
                                    "memory": {"status": "available", "total_kib": 2,
                                               "available_kib": 1},
                                    "io": {"status": "available", "devices": 1,
                                           "read_sectors": 0, "written_sectors": 0}},
                    "storage": {"tables": {name: {"part_count": 1, "marks": 0,
                                                     "compressed_bytes": 0,
                                                     "uncompressed_bytes": 0}
                                           for name in catalog.write_tables}, "merges": []},
                    "active_part_backlog": 0, "active_merge_count": 0}
        manifest = {"format": "agent-trace-json-storage-stage3-interference-phase",
                    "format_version": 1, "status": "complete", "phase": phase.name,
                    "seed": 20260907, "execution_scope": "formal",
                    "classification": "formal_complete", "layout": layout,
                    "namespace": f"jsons3_if_{phase.name}", "warmup_seconds": 30.0,
                    "measurement_seconds": 300.0, "schedules": schedules,
                    "execution_coverage": {"warmup_actual_seconds": 30.0,
                                           "measurement_actual_seconds": 300.0},
                    "snapshots": [dict(snapshot, name=name) for name in (
                        "before_warmup", "before_measurement", "after_measurement")],
                    "warmup": {}, "statistics": {},
                    "query_evidence": {
                        "plans": {qid: "plan" for qid in query_ids},
                        "query_details": {sample["query_id"]: {
                            "kind": sample["kind"], "statement": "SELECT 1",
                            "declared_source": catalog.list_source,
                            "scanned_rows": 1, "scanned_bytes": 1,
                        } for sample in all_queries},
                        "query_finish": {qid: {"type": "QueryFinish", "exception_code": 0,
                                               "read_rows": 1, "read_bytes": 1}
                                         for qid in query_ids}},
                    "cleanup": {"namespace": f"jsons3_if_{phase.name}_{layout}",
                                "removed": True}}
        for segment, streams in summaries.items():
            target = manifest["warmup" if segment == "warmup" else "statistics"]
            for stream, info in streams.items():
                successful = info["counts"]["successful_requests"]
                target[stream] = {"offered_rate_requests_s": schedules[segment][stream]["rate_per_second"],
                                  "phase_wall_seconds": 30.0 if segment == "warmup" else 300.0,
                                  **info["counts"], "completed_throughput_requests_s": (
                                      successful / (30.0 if segment == "warmup" else 300.0)),
                                  "latency_ms": {"p99_status": (
                                      "publishable" if successful >= 1000
                                      else "unavailable_insufficient_successes"),
                                                 "p99_minimum_successes": 1000,
                                                 "p99": 100.0 if successful >= 1000 else None,
                                                 "minimum": 100.0, "p50": 100.0,
                                                 "p95": 100.0, "maximum": 100.0}}
        phase_root = child_root / phase.name
        phase_root.mkdir()
        run_stage3.write_manifest_atomic(phase_root / "run-manifest.json", manifest)
        for filename, rows in records.items():
            (phase_root / filename).write_text("".join(json.dumps(row) + "\n" for row in rows))
        root["phases"].append(manifest)
        parsed[phase.name] = summaries
    run_stage3.write_manifest_atomic(child_root / "run-manifest.json", root)
    return run_stage3._read_child(output, child_root / "run-manifest.json"), parsed


class InterferenceProductionGateTests(unittest.TestCase):
    def run_gate(self, mutate=None):
        directory = tempfile.TemporaryDirectory()
        output = Path(directory.name)
        child, parsed = valid_interference_tree(output)
        if mutate:
            mutate(output, child["_manifest"])
            child = run_stage3._read_child(output, output / "child" / "run-manifest.json")
        parser = patch.object(run_stage3, "_parse_interference_raw", side_effect=lambda path, schedules, *_: (
            parsed[Path(path).parent.name]["warmup" if Path(path).name.startswith("warmup")
                                            else "measurement"]
        ))
        parser.start()
        self.addCleanup(parser.stop)
        self.addCleanup(directory.cleanup)
        self.formal = SimpleNamespace(main_queries=tuple(
            (QuerySpec(kind, {"cohort": "main"} if kind == "batch" else {}),
             SimpleNamespace(scenario=scenario))
            for scenario, kind in (("list:first", "list"), ("preview:first", "preview"),
                                   ("detail:text_2m", "detail"), ("trace:p95", "trace"),
                                   ("batch:main", "batch"))
        ))
        return output, child

    def test_gate_accepts_exact_five_phase_formal_evidence_and_rechecks_artifacts(self):
        output, child = self.run_gate()
        evidence = run_stage3._gate_interference(child, self.formal, "same_table")
        self.assertEqual(len(evidence["namespaces"]), 5)
        self.assertEqual(len(evidence["phases"]), 5)
        self.assertEqual(len(evidence["artifacts"]), 16)
        run_stage3._verify_interference_artifacts(output, evidence["artifacts"])
        (output / evidence["artifacts"][-1]["path"]).write_text("replaced")
        with self.assertRaisesRegex(RuntimeError, "artifact.*changed"):
            run_stage3._verify_interference_artifacts(output, evidence["artifacts"])

    def test_gate_rejects_root_phase_namespace_coverage_and_storage_mutations(self):
        def cases(name):
            def mutate(output, root):
                phase = root["phases"][0]
                if name == "non_formal": root["execution_scope"] = "diagnostic"
                elif name == "bool_version": root["format_version"] = True
                elif name == "missing_phase": root["phases"].pop()
                elif name == "embedded_mismatch": phase["status"] = "failed"
                elif name == "duplicate_namespace": root["phases"][1]["namespace"] = phase["namespace"]
                elif name == "bool_phase_version": phase["format_version"] = True
                elif name == "short_coverage": phase["execution_coverage"]["warmup_actual_seconds"] = 29.9
                elif name == "missing_storage": phase["snapshots"][0]["storage"]["tables"] = {}
                elif name == "bool_snapshot_total": phase["snapshots"][0]["active_merge_count"] = False
                if name not in {"non_formal", "bool_version", "missing_phase", "embedded_mismatch"}:
                    changed = root["phases"][1] if name == "duplicate_namespace" else phase
                    run_stage3.write_manifest_atomic(
                        output / "child" / changed["phase"] / "run-manifest.json", changed,
                    )
                run_stage3.write_manifest_atomic(output / "child" / "run-manifest.json", root)
            return mutate
        for name in ("non_formal", "bool_version", "missing_phase", "embedded_mismatch",
                     "duplicate_namespace", "bool_phase_version", "short_coverage",
                     "missing_storage", "bool_snapshot_total"):
            with self.subTest(name=name):
                output, child = self.run_gate(cases(name))
                with self.assertRaises(RuntimeError):
                    run_stage3._gate_interference(child, self.formal, "same_table")

    def test_raw_parser_rejects_missing_empty_invalid_sequence_and_summary_count(self):
        schedule = run_interference._json_value(run_interference.fixed_phase_schedules(
            run_interference.FIXED_PHASES[0], measurement=False))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            with self.assertRaises(RuntimeError): run_stage3._parse_interference_raw(path, schedule)
            path.write_bytes(b"")
            with self.assertRaises(RuntimeError): run_stage3._parse_interference_raw(path, schedule)
            path.write_bytes(b"{\n")
            with self.assertRaises(ValueError): run_stage3._parse_interference_raw(path, schedule)
            rows = [interference_raw_sample("list", 1, "q1"),
                    interference_raw_sample("preview", 0, "q2")]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.assertRaises(RuntimeError): run_stage3._parse_interference_raw(path, schedule)
            one = {"list": dict(schedule["list"], rate_per_second=2.0,
                                duration_seconds=1.0)}
            path.write_text(json.dumps(interference_raw_sample("list", 0, "q1")) + "\n")
            with self.assertRaisesRegex(RuntimeError, "scheduled count"):
                run_stage3._parse_interference_raw(path, one)

    def test_raw_parser_rejects_query_mapping_and_missing_continuous_block_result(self):
        fixed = {"list": {"name": "list", "rate_per_second": 1.0,
                           "duration_seconds": 1.0, "workers": 1,
                           "timeout_seconds": 1.0, "late_tolerance_seconds": 0.05,
                           "mode": "fixed"}}
        continuous = {"continuous_ingest": {"name": "continuous_ingest",
                                              "rate_per_second": None,
                                              "duration_seconds": 1.0, "workers": 1,
                                              "timeout_seconds": 1.0,
                                              "late_tolerance_seconds": 0.05,
                                              "mode": "continuous"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            wrong = interference_raw_sample("list", 0, "q1")
            wrong["sample"].update(kind="trace", scenario="trace:p95")
            path.write_text(json.dumps(wrong) + "\n")
            with self.assertRaisesRegex(RuntimeError, "query evidence"):
                run_stage3._parse_interference_raw(path, fixed)
            missing = interference_raw_sample("continuous_ingest", 0)
            missing["sample"] = None
            path.write_text(json.dumps(missing) + "\n")
            with self.assertRaisesRegex(RuntimeError, "BlockResult"):
                run_stage3._parse_interference_raw(path, continuous)

    def test_raw_parser_accepts_unfinished_timeout_and_checks_ingest_watermarks(self):
        fixed = {"list": {"name": "list", "rate_per_second": 1.0,
                           "duration_seconds": 1.0, "workers": 1,
                           "timeout_seconds": 1.0, "late_tolerance_seconds": 0.05,
                           "mode": "fixed"}}
        continuous = {"continuous_ingest": {"name": "continuous_ingest",
                                              "rate_per_second": None,
                                              "duration_seconds": 1.0, "workers": 1,
                                              "timeout_seconds": 1.0,
                                              "late_tolerance_seconds": 0.05,
                                              "mode": "continuous"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            timeout = interference_raw_sample("list", 0, "unused")
            timeout.update(status="timed_out", completed_offset_seconds=None,
                           duration_ms=None, application_ready_ms=None, sample=None,
                           error="request timed out")
            path.write_text(json.dumps(timeout) + "\n")
            parsed = run_stage3._parse_interference_raw(path, fixed)
            self.assertEqual(parsed["list"]["counts"]["timed_out_requests"], 1)
            block = interference_raw_sample("continuous_ingest", 0)
            block["sample"]["watermarks"] = {"wrong_table": block["sample"]["watermark"]}
            path.write_text(json.dumps(block) + "\n")
            with self.assertRaisesRegex(RuntimeError, "BlockResult"):
                run_stage3._parse_interference_raw(
                    path, continuous, {}, ("events",),
                )

    def test_access_gate_rejects_query_ids_reused_between_segments(self):
        sample = interference_raw_sample("list", 0, "duplicate")["sample"]
        with self.assertRaisesRegex(RuntimeError, "duplicated across segments"):
            run_stage3._gate_interference_access({}, {"list": [sample, dict(sample)]})

    def test_gate_rejects_summary_query_access_and_ingest_conflicts(self):
        def cases(name):
            def mutate(output, root):
                phase = root["phases"][4 if name == "missing_block" else 0]
                if name == "summary": phase["warmup"]["list"]["scheduled_requests"] = 2
                elif name == "bool_count": phase["warmup"]["list"]["failed_requests"] = False
                elif name == "throughput": phase["warmup"]["list"]["completed_throughput_requests_s"] = 1.0
                elif name == "p99": phase["statistics"]["list"]["latency_ms"].update(
                    p99_status="unavailable_insufficient_successes", p99=None,
                )
                elif name == "latency": phase["warmup"]["list"]["latency_ms"]["p50"] = "100"
                elif name == "bool_rate":
                    phase = root["phases"][1]
                    phase["warmup"]["detail_2m"]["offered_rate_requests_s"] = True
                elif name == "missing_query_id": phase["query_evidence"]["plans"].pop(next(iter(phase["query_evidence"]["plans"])))
                elif name == "finish_conflict": next(iter(phase["query_evidence"]["query_finish"].values()))["read_bytes"] = 2
                elif name == "bool_exception": next(iter(
                    phase["query_evidence"]["query_finish"].values()
                ))["exception_code"] = False
                run_stage3.write_manifest_atomic(
                    output / "child" / phase["phase"] / "run-manifest.json", phase,
                )
                run_stage3.write_manifest_atomic(output / "child" / "run-manifest.json", root)
            return mutate
        for name in ("summary", "bool_count", "throughput", "p99", "latency", "bool_rate",
                     "missing_query_id", "finish_conflict", "bool_exception"):
            with self.subTest(name=name):
                output, child = self.run_gate(cases(name))
                with self.assertRaises(RuntimeError):
                    run_stage3._gate_interference(child, self.formal, "same_table")


if __name__ == "__main__":
    unittest.main()
