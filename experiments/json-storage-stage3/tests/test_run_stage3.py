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
        "cleanup": {
            "namespace": namespace + "_asset_ref",
            "removed": True,
            "asset_directory_removed": True,
        },
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
            "active_merges": (
                [{"table": catalog.list_source}] if state_name == "merging" else []
            ), "observations": [], "query_samples": samples,
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
        if state_name == "fragmented":
            state["tables"][catalog.list_source]["part_count"] = 2
        observation = {
            "active_part_counts": {
                table: values["part_count"] for table, values in state["tables"].items()
            },
            "active_merges": list(state["active_merges"]),
        }
        state["observations"] = [
            {
                "active_part_counts": dict(observation["active_part_counts"]),
                "active_merges": list(observation["active_merges"]),
            }
            for _ in range(3 if state_name == "stable" else 1)
        ]
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
        "restoration": {"attempted": True, "restored": True, "targets": (
            [catalog.list_source] if layout == "asset_ref" else list(catalog.write_tables)
        )},
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

    def test_candidate_gate_accepts_physical_cleanup_database(self):
        """捕获 candidate gate 把 base namespace 当作 ClickHouse 物理数据库。"""
        with tempfile.TemporaryDirectory() as directory:
            try:
                result, output, _ = self.run_candidate(Path(directory))
            except RuntimeError as error:
                self.fail(f"valid physical cleanup database was rejected: {error}")
            envelope = json.loads((output / "run-manifest.json").read_text())

        self.assertEqual(result, 0)
        self.assertEqual(
            envelope["namespace_policy"]["namespace"], "jsons3_candidate_0123456789",
        )
        self.assertEqual(
            envelope["cleanup"]["namespace"], "jsons3_candidate_0123456789_asset_ref",
        )

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

    def test_part_state_gate_recomputes_physical_state_predicates(self):
        """捕获 child 自报 predicate_proven 掩盖不成立的物理状态。"""
        mutations = {
            "fragmented_part_count": lambda manifest: manifest["states"][0]["tables"][
                "events"
            ].update(part_count=1),
            "fragmented_active_merge": lambda manifest: manifest["states"][0][
                "active_merges"
            ].append({"table": "events"}),
            "merging_without_controlled_table": lambda manifest: manifest["states"][1].update(
                active_merges=[{"table": "wrong"}],
            ),
            "unstable_observations": lambda manifest: manifest["states"][2]["observations"][
                -1
            ]["active_part_counts"].update(events=2),
            "single_part_count": lambda manifest: manifest["states"][3]["tables"][
                "events"
            ].update(part_count=2),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "part-state predicate"):
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
        nested = {"rows": 256, "watermark": 27_904,
                  "watermarks": {"events_analytics": 27_904}, "wall_ms": 1.0,
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


def interference_formal_fixture():
    """构造能通过真实 continuous block 选择器的最小正式输入。"""
    blocks = []
    for index in range(190):
        row = {
            "project_id": "outside-query-window",
            "start_time": "2026-01-01T00:00:00.000Z",
            "payload_path": "payload.bin" if 108 <= index < 153 else None,
            "ingest_seq": (index + 1) * 256 - 1,
        }
        blocks.append((row,) * 256)
    return SimpleNamespace(
        truth=SimpleNamespace(
            seed=20260907, identity_sha256="a" * 64, record_count=48_534,
            block_size=256, block_count=190,
            query_window={
                "project_id": "query-project",
                "start_time": "2026-02-01T00:00:00.000Z",
                "end_time": "2026-03-01T00:00:00.000Z",
                "page_size": 256,
                "row_count": 27_561,
            },
        ),
        main_blocks=tuple(blocks),
        main_queries=tuple(
            (QuerySpec(kind, {"cohort": "main"} if kind == "batch" else {}),
             SimpleNamespace(scenario=scenario))
            for scenario, kind in (("list:first", "list"), ("preview:first", "preview"),
                                   ("detail:text_2m", "detail"), ("trace:p95", "trace"),
                                   ("batch:main", "batch"))
        ),
    )


def interference_factory_metadata(formal):
    """按 production.interference_factories 的字段构造固定 interference metadata。"""
    eligible = production._continuous_blocks(formal)
    window = formal.truth.query_window
    return {
        "selection_rules": {
            "full_block_rows": formal.truth.block_size,
            "outside_query_window": {
                "project_id": window["project_id"],
                "start_time": window["start_time"],
                "end_time": window["end_time"],
            },
            "requires_main_payload": True,
            "query_scenarios": {
                "list": "list:first", "preview": "preview:first",
                "detail_2m": "detail:text_2m", "trace_long": "trace:p95",
                "batch_loop": "batch:main",
            },
        },
        "eligible_block_count": len(eligible),
        "eligible_block_indices": [index for index, _ in eligible],
        "eligible_block_sha256": [
            canonical_digest([dict(row) for row in block]) for _, block in eligible
        ],
        "cyclic_replay": True,
        "main_query_catalog_sha256": query_digest(formal),
        "preload_block_count": len(formal.main_blocks),
        "block_size": formal.truth.block_size,
        "final_watermark": formal.truth.record_count,
        "seed": formal.truth.seed,
    }


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
        if layout == "asset_ref":
            snapshot["storage"]["asset_store"] = {
                "available_object_count": 0, "available_bytes": 0,
                "orphan_object_count": 0, "orphan_bytes": 0,
            }
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
        parser = patch.object(run_stage3, "_parse_interference_raw", side_effect=lambda path, schedules, *_, **__: (
            parsed[Path(path).parent.name]["warmup" if Path(path).name.startswith("warmup")
                                            else "measurement"]
        ))
        parser.start()
        self.addCleanup(parser.stop)
        self.addCleanup(directory.cleanup)
        self.formal = interference_formal_fixture()
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

    def test_raw_parser_requires_nonnegative_integer_sequences(self):
        """捕获 bool、float 或负数 sequence 冒充从零连续的整数序列。"""
        schedule = {"list": {"name": "list", "rate_per_second": 1.0,
                             "duration_seconds": 1.0, "workers": 1,
                             "timeout_seconds": 1.0, "late_tolerance_seconds": 0.05,
                             "mode": "fixed"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            for sequence in (False, 0.0, -1):
                with self.subTest(sequence=sequence):
                    path.write_text(json.dumps(interference_raw_sample(
                        "list", sequence, "q1",
                    )) + "\n")
                    with self.assertRaisesRegex(RuntimeError, "sequence"):
                        run_stage3._parse_interference_raw(path, schedule)

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

    def test_raw_parser_binds_continuous_results_to_formal_blocks_and_layout_targets(self):
        """捕获短块、未知水位或布局写目标漂移仍被认定为正式持续写入。"""
        continuous = {"continuous_ingest": {"name": "continuous_ingest",
                                              "rate_per_second": None,
                                              "duration_seconds": 1.0, "workers": 1,
                                              "timeout_seconds": 1.0,
                                              "late_tolerance_seconds": 0.05,
                                              "mode": "continuous"}}
        eligible_watermarks = {27_904, 28_160}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            for layout in ("same_table", "separate", "full_core", "asset_ref"):
                with self.subTest(layout=layout):
                    tables = build_layout_catalog(layout).write_tables
                    row = interference_raw_sample("continuous_ingest", 0)
                    row["sample"]["watermarks"] = {table: 27_904 for table in tables}
                    path.write_text(json.dumps(row) + "\n")
                    parsed = run_stage3._parse_interference_raw(
                        path, continuous, {}, tables,
                        continuous_block_rows=256,
                        continuous_watermarks=eligible_watermarks,
                    )
                    self.assertEqual(
                        parsed["continuous_ingest"]["counts"]["successful_requests"], 1,
                    )
            tables = build_layout_catalog("same_table").write_tables
            for name, rows, watermark in (("short_rows", 1, 27_904),
                                          ("zero_watermark", 256, 0),
                                          ("unknown_watermark", 256, 99_999)):
                with self.subTest(name=name):
                    row = interference_raw_sample("continuous_ingest", 0)
                    row["sample"].update(
                        rows=rows, watermark=watermark,
                        watermarks={table: watermark for table in tables},
                    )
                    path.write_text(json.dumps(row) + "\n")
                    with self.assertRaisesRegex(RuntimeError, "BlockResult"):
                        run_stage3._parse_interference_raw(
                            path, continuous, {}, tables,
                            continuous_block_rows=256,
                            continuous_watermarks=eligible_watermarks,
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

    def test_gate_uses_formal_continuous_selection_as_single_source(self):
        """捕获 gate 绕过 production 正式 block 选择器并复制选择算法。"""
        _, child = self.run_gate()
        with patch.object(
            production, "_continuous_blocks",
            side_effect=RuntimeError("formal continuous block selection failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "formal continuous block selection failed"):
                run_stage3._gate_interference(child, self.formal, "same_table")

    def test_gate_rejects_type_coerced_schedules_and_p99_minimum(self):
        """捕获同步篡改 root/phase 后 bool、float 仍通过固定类型契约。"""
        def mutate_schedule(output, root, field, value):
            phase = root["phases"][1]
            phase["schedules"]["warmup"]["detail_2m"][field] = value
            run_stage3.write_manifest_atomic(
                output / "child" / phase["phase"] / "run-manifest.json", phase,
            )
            run_stage3.write_manifest_atomic(output / "child" / "run-manifest.json", root)

        for field, value in (("rate_per_second", True), ("workers", 1.0)):
            with self.subTest(field=field):
                output, child = self.run_gate(
                    lambda output, root: mutate_schedule(output, root, field, value),
                )
                with self.assertRaisesRegex(RuntimeError, "schedules"):
                    run_stage3._gate_interference(child, self.formal, "same_table")

        def mutate_p99(output, root):
            phase = root["phases"][0]
            phase["warmup"]["list"]["latency_ms"]["p99_minimum_successes"] = 1000.0
            run_stage3.write_manifest_atomic(
                output / "child" / phase["phase"] / "run-manifest.json", phase,
            )
            run_stage3.write_manifest_atomic(output / "child" / "run-manifest.json", root)

        output, child = self.run_gate(mutate_p99)
        with self.assertRaisesRegex(RuntimeError, "publication evidence"):
            run_stage3._gate_interference(child, self.formal, "same_table")

    def test_gate_requires_fixed_child_manifest_path(self):
        """捕获 rogue child 树通过 gate 后又被 artifact verifier 拒绝。"""
        output, _ = self.run_gate()
        (output / "child").rename(output / "rogue")
        child = run_stage3._read_child(output, output / "rogue" / "run-manifest.json")
        with self.assertRaisesRegex(RuntimeError, "child manifest path"):
            run_stage3._gate_interference(child, self.formal, "same_table")


class InterferenceFactoryMetadataGateTests(unittest.TestCase):
    """验证 interference factory metadata 按 production 的三字段窗口核对。"""

    def test_gate_accepts_three_field_window_when_truth_window_has_more_fields(self):
        """正式 truth 的 query_window 含 page_size/row_count 时三字段 metadata 仍通过。"""
        formal = interference_formal_fixture()
        self.assertEqual(set(formal.truth.query_window), {
            "project_id", "start_time", "end_time", "page_size", "row_count",
        })
        snapshot = run_stage3._gate_interference_metadata(
            interference_factory_metadata(formal), formal,
        )
        self.assertEqual(snapshot["selection_rules"]["outside_query_window"], {
            "project_id": formal.truth.query_window["project_id"],
            "start_time": formal.truth.query_window["start_time"],
            "end_time": formal.truth.query_window["end_time"],
        })

    def test_gate_rejects_three_field_window_drift(self):
        """捕获 outside_query_window 三字段任一漂移仍被接受。"""
        for field in ("project_id", "start_time", "end_time"):
            with self.subTest(field=field):
                formal = interference_formal_fixture()
                metadata = interference_factory_metadata(formal)
                metadata["selection_rules"]["outside_query_window"][field] = "drifted"
                with self.assertRaisesRegex(RuntimeError, "factory metadata is invalid"):
                    run_stage3._gate_interference_metadata(metadata, formal)


class InterferenceCliTests(unittest.TestCase):
    """验证 interference CLI 的固定 production 编排和失败证据。"""

    def run_interference_cli(self, root, layout="same_table", *, factory_mutate=None,
                             runner_error=None, gate_error=None, runtime=None,
                             remove_error=None, replace_artifact=False):
        """以受控 child runner 执行 CLI，并返回 output 与实际调用记录。"""
        output = root / "attempt-1"
        input_root = root / "input"
        formal = interference_formal_fixture()
        formal.root = input_root.resolve()
        formal.identity = {"kind": "formal", "identity_sha256": "b" * 64}
        calls = []
        parsed = {}

        def metadata():
            return interference_factory_metadata(formal)

        def fake_create(engine, layout_arg, namespace, formal_arg, asset_root, endpoints):
            self.assertEqual((engine, layout_arg, namespace), (
                "clickhouse", layout, "jsons3_runtime_probe",
            ))
            self.assertIs(formal_arg, formal)
            self.assertIsInstance(endpoints, production.EngineEndpoints)
            self.assertEqual(asset_root, output / "assets" if layout == "asset_ref" else None)
            calls.append("probe")
            return SimpleNamespace()

        def fake_factories(formal_arg, layout_arg, asset_root, endpoints):
            self.assertEqual(json.loads((output / "run-manifest.json").read_text())["status"], "running")
            self.assertIs(formal_arg, formal)
            self.assertEqual(layout_arg, layout)
            self.assertEqual(asset_root, output / "assets" if layout == "asset_ref" else None)
            self.assertIsInstance(endpoints, production.EngineEndpoints)
            value = metadata()
            if factory_mutate is not None:
                factory_mutate(value)
            calls.append("factories")
            return object(), object(), value

        def fake_runner(adapter_factory, targets_factory, child_root, **kwargs):
            self.assertEqual(kwargs, {"scope": "formal"})
            self.assertEqual(child_root, output / "child")
            self.assertIsNotNone(adapter_factory)
            self.assertIsNotNone(targets_factory)
            if layout == "asset_ref":
                (output / "assets").mkdir()
            child, values = valid_interference_tree(output, layout)
            parsed.update(values)
            calls.append("runner")
            if runner_error is not None:
                raise runner_error
            return child

        runtime_value = runtime or {"version": "25.12", "source": "database-query"}
        patches = [
            patch.object(run_stage3, "load_formal_input", return_value=formal),
            patch.object(run_stage3, "create_adapter", side_effect=fake_create),
            patch.object(run_stage3.production, "interference_factories", side_effect=fake_factories),
            patch.object(run_stage3.run_interference, "run_interference", side_effect=fake_runner),
            patch.object(run_stage3, "_parse_interference_raw", side_effect=lambda path, *_args, **_kwargs: (
                parsed[Path(path).parent.name]["warmup" if Path(path).name.startswith("warmup")
                                               else "measurement"]
            )),
            patch.object(run_stage3.run_layout_matrix, "_engine_runtime", return_value=runtime_value),
            patch.object(run_stage3.run_layout_matrix, "_container_evidence", return_value={
                "container": "agent-trace-clickhouse-25-12", "image": "clickhouse",
                "image_id": "sha256:id",
            }),
            patch.object(run_stage3.run_layout_matrix, "_host_evidence", return_value={
                "platform": "test", "machine": "x86_64", "cpu_count": 1,
                "memory_total_kib": 1,
            }),
        ]
        if gate_error is not None:
            patches.append(patch.object(run_stage3, "_gate_interference", side_effect=gate_error))
        if remove_error is not None:
            patches.append(patch.object(run_stage3, "_remove_asset_directory", side_effect=remove_error))
        if replace_artifact:
            original_verify = run_stage3._verify_interference_artifacts

            def replace_then_verify(output_arg, artifacts):
                (Path(output_arg) / artifacts[-1]["path"]).write_text("replaced")
                original_verify(output_arg, artifacts)

            patches.append(patch.object(run_stage3, "_verify_interference_artifacts",
                                        side_effect=replace_then_verify))
        for context in patches:
            context.start()
        try:
            result = run_stage3.main([
                "interference", "--input", str(input_root), "--output", str(output),
                "--layout", layout,
            ])
        finally:
            for context in reversed(patches):
                context.stop()
        return result, output, formal, calls

    def test_parser_and_four_layouts_keep_the_interference_surface_fixed(self):
        """捕获 CLI 暴露调度参数或 factory/probe 偏离四布局固定调用。"""
        parser = run_stage3.build_parser()
        for option in ("--seed", "--phase", "--warmup", "--measurement", "--scope",
                       "--runner", "--endpoint", "--namespace", "--process-policy"):
            with self.subTest(option=option):
                with self.assertRaises(SystemExit):
                    parser.parse_args(["interference", "--input", "in", "--output", "out",
                                       "--layout", "same_table", option, "x"])
        for layout in ("same_table", "separate", "full_core", "asset_ref"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                result, output, formal, calls = self.run_interference_cli(Path(directory), layout)
                envelope = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(result, 0)
                self.assertEqual(calls, ["probe", "factories", "runner"])
                self.assertEqual(envelope["status"], "complete")
                self.assertEqual(envelope["namespace_policy"]["strategy"],
                                 "runner-fixed-phase-unique")
                self.assertEqual(envelope["namespace_policy"]["namespaces"], [
                    f"jsons3_if_{phase.name}" for phase in run_interference.FIXED_PHASES
                ])
                self.assertEqual(envelope["runtime"]["operation"], "interference")
                self.assertEqual(envelope["runtime"]["engine"], "clickhouse")
                self.assertEqual(envelope["runtime"]["layout"], layout)
                self.assertIn("interference_runner", envelope["code"])
                self.assertEqual(envelope["interference"]["main_query_catalog_sha256"],
                                 envelope["query_catalog_sha256"])
                self.assertEqual(len(envelope["artifacts"]), 16)
                self.assertEqual(envelope["child"]["path"], "child/run-manifest.json")
                self.assertTrue(envelope["cleanup"]["namespaces_removed"])
                self.assertEqual(envelope["cleanup"]["asset_directory_applicable"], layout == "asset_ref")
                self.assertTrue(envelope["cleanup"]["asset_directory_removed"])
                self.assertFalse((output / "assets").exists())

    def test_metadata_drift_and_post_gate_failures_publish_failed_envelopes(self):
        """捕获 factory metadata 漂移、gate 后替换或 Asset 清理失败仍发布 complete。"""
        mutations = {
            "bool_count": lambda value: value.update(eligible_block_count=True),
            "block_count": lambda value: value.update(eligible_block_count=44),
            "digest": lambda value: value.update(main_query_catalog_sha256="0" * 64),
            "eligible": lambda value: value["eligible_block_indices"].pop(),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    self.run_interference_cli(Path(directory), factory_mutate=mutate)
                failed = json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["truth"]["record_count"], 48_534)
                self.assertIn("query_catalog_sha256", failed)
                if name == "block_count":
                    self.assertEqual(failed["interference"]["eligible_block_count"], 44)

        for name, options in (
            ("runtime", {"runtime": {"version": "", "source": "database-query"}}),
            ("factory", {"factory_mutate": lambda _value: (_ for _ in ()).throw(
                RuntimeError("factory failed"))}),
            ("gate", {"gate_error": RuntimeError("gate failed")}),
            ("asset_cleanup", {"layout": "asset_ref", "remove_error": OSError("unlink failed")}),
            ("artifact", {"layout": "asset_ref", "replace_artifact": True}),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(Exception):
                    self.run_interference_cli(Path(directory), **options)
                failed = json.loads((Path(directory) / "attempt-1" / "run-manifest.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["input"]["kind"], "formal")
                self.assertEqual(failed["truth"]["record_count"], 48_534)
                if name not in {"runtime", "factory"}:
                    self.assertEqual(failed["child"]["path"], "child/run-manifest.json")
                if name == "artifact":
                    self.assertTrue(failed["cleanup"]["asset_directory_removed"])
                    self.assertFalse((Path(directory) / "attempt-1" / "assets").exists())

    def test_non_json_metadata_publishes_a_safe_snapshot_failure(self):
        """捕获 NaN 或对象让 failed envelope 发布失败并掩盖 metadata 门禁异常。"""
        mutations = {
            "nan": lambda value: value.update(eligible_block_count=math.nan),
            "object": lambda value: value.update(eligible_block_count=object()),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(
                    RuntimeError, "interference factory metadata is not JSON-safe",
                ):
                    self.run_interference_cli(Path(directory), factory_mutate=mutate)
                failed = json.loads(
                    (Path(directory) / "attempt-1" / "run-manifest.json").read_text()
                )
                self.assertEqual(failed["status"], "failed")
                self.assertNotIn("interference", failed)
                self.assertEqual(failed["interference_snapshot"], {
                    "status": "failed",
                    "error": {
                        "type": "RuntimeError",
                        "message": "interference factory metadata is not JSON-safe",
                    },
                })
                self.assertEqual(failed["error"], failed["interference_snapshot"]["error"])

    def test_runner_failure_records_partial_namespaces_and_preserves_assets(self):
        """捕获 child 已发布后 runner 失败丢失 phase namespace 或提前删除 Asset 证据。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "unknown server-side completion"):
                self.run_interference_cli(
                    Path(directory), layout="asset_ref",
                    runner_error=RuntimeError("unknown server-side completion"),
                )
            output = Path(directory) / "attempt-1"
            failed = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["namespace_policy"]["namespaces"], [
                f"jsons3_if_{phase.name}" for phase in run_interference.FIXED_PHASES
            ])
            self.assertTrue((output / "assets").is_dir())


ASSET_FAILURE_CASES = (
    "missing", "corrupt", "metadata_mismatch", "upload_then_db_failure",
    "publish_failure", "delete_failure",
)


def valid_asset_failure_child(output):
    """构造独立于故障 runner helper 的固定六故障 child。"""
    rules = {
        "missing": ("remove_published_object", "missing", False,
                    "restore_missing_object", "available", "available", True),
        "corrupt": ("modify_published_bytes", "corrupt", True,
                    "replace_corrupt_object", "available", "available", True),
        "metadata_mismatch": ("replace_catalog_metadata", "metadata_mismatch", True,
                              "restore_catalog_metadata", "available", "available", True),
        "upload_then_db_failure": ("fail_after_object_upload", "missing", True,
                                   "remove_orphan_object", "absent", "absent", False),
        "publish_failure": ("fail_pending_publication", "failed", False,
                            "confirm_failed_publication", "failed", "failed", False),
        "delete_failure": ("fail_deleting_object_removal", "deleting", True,
                           "confirm_delete_failure_state", "deleting", "deleting", False),
    }
    results = []
    for index, case in enumerate(ASSET_FAILURE_CASES):
        injection, error, object_exists, recovery, injected_status, final_status, recovered = rules[case]
        payload = ('{"content":"asset failure ' + case + '"}').encode()
        digest = hashlib.sha256(payload).hexdigest()
        namespace = f"jsons3_af_{case}_{index:010x}"
        objects = (output / "child" / "asset-failure-cases" / case / "objects").resolve()
        object_path = objects / digest[:2] / digest
        prepared_status = "pending" if case == "publish_failure" else (
            "absent" if case == "upload_then_db_failure" else "available"
        )
        resolver = {
            "error": error, "content_visible": False, "content_length": None,
            "sha256": None, "preview": None,
        }
        recovery_resolver = dict(resolver)
        if recovered:
            recovery_resolver = {
                "error": None, "content_visible": True, "content_length": len(payload),
                "sha256": digest, "preview": payload.decode(),
            }
        attempts = []
        if case == "upload_then_db_failure":
            attempts = [{"asset_id": digest, "error": None, "final_object_exists": True}]
        elif case == "publish_failure":
            attempts = [{"asset_id": digest, "error": "failed", "final_object_exists": False}]
        results.append({
            "case": case, "namespace": namespace, "asset_id": digest, "sha256": digest,
            "injection_point": injection,
            "catalog_transitions": [
                {"phase": "injection", "asset_id": digest, "sha256": digest,
                 "before": prepared_status, "after": injected_status},
                {"phase": "recovery", "asset_id": digest, "sha256": digest,
                 "before": injected_status, "after": final_status},
            ],
            "resolver": resolver,
            "event_visible": case != "upload_then_db_failure",
            "reconcile": {
                "orphan_count": 1 if case == "upload_then_db_failure" else 0,
                "orphan_paths": [str(object_path)] if case == "upload_then_db_failure" else [],
            },
            "store_observation": {
                "object_path": str(object_path), "object_exists": object_exists,
                "publish_attempts": attempts,
            },
            "recovery_actions": [recovery], "recovery_resolver": recovery_resolver,
            "reconcile_after_recovery": {"orphan_count": 0, "orphan_paths": []},
            "final_status": final_status,
            "cleanup": {
                "namespace": namespace, "adapter_cleanup_target": namespace + "_asset_ref",
                "namespace_removed": True, "object_directory": str(objects),
                "object_directory_removed": True, "errors": [],
            },
            "validation_errors": [], "execution_error": None,
        })
    return {
        "format": "agent-trace-json-storage-stage3-asset-failure-run",
        "format_version": 1, "run_id": "jsons3-asset-failures-0123456789",
        "status": "complete", "case_order": list(ASSET_FAILURE_CASES), "results": results,
    }


class AssetFailureProductionGateTests(unittest.TestCase):
    """验证六故障 child bytes 的独立 production 门禁。"""

    def gate(self, root, mutate=None, engine="opengauss"):
        output = root / "attempt-1"
        child_root = output / "child"
        child_root.mkdir(parents=True)
        manifest = valid_asset_failure_child(output)
        if mutate is not None:
            mutate(manifest)
        run_stage3.write_manifest_atomic(child_root / "run-manifest.json", manifest)
        child = run_stage3._read_child(output, child_root / "run-manifest.json")
        return run_stage3._gate_asset_failures(child, output, engine), child

    def assert_rejected(self, mutate=None, engine="opengauss"):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises((RuntimeError, ValueError)):
                self.gate(Path(directory), mutate, engine)

    def test_gate_accepts_both_engines_and_returns_isolated_complete_evidence(self):
        """捕获门禁遗漏 engine、namespace、cleanup 或返回 child 可变对象。"""
        for engine in ("opengauss", "clickhouse"):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                evidence, child = self.gate(Path(directory), engine=engine)
                self.assertEqual(evidence["engine"], engine)
                self.assertEqual(evidence["case_order"], list(ASSET_FAILURE_CASES))
                self.assertEqual(len(set(evidence["namespaces"])), 6)
                self.assertEqual(evidence["cleanup"], {
                    "namespaces_removed": True, "object_directories_removed": True,
                })
                child["_manifest"]["results"][0]["case"] = "forged"
                self.assertEqual(evidence["results"][0]["case"], "missing")
                evidence["results"][0]["case"] = "changed"
                fresh, _ = self.gate(Path(directory) / "fresh", engine=engine)
                self.assertEqual(fresh["results"][0]["case"], "missing")

    def test_gate_rejects_root_path_order_fields_and_engine_drift(self):
        """捕获 root 身份、case 覆盖或固定 child 相对路径漂移。"""
        mutations = (
            lambda value: value.update(format="wrong"),
            lambda value: value.update(format_version=True),
            lambda value: value.update(status="running"),
            lambda value: value.update(run_id=""),
            lambda value: value["case_order"].reverse(),
            lambda value: value["results"].__setitem__(1, dict(value["results"][0])),
            lambda value: value.update(extra=True),
            lambda value: value["results"][0].update(extra=True),
        )
        for mutation in mutations:
            self.assert_rejected(mutation)
        self.assert_rejected(engine="sqlite")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "attempt-1"
            rogue = output / "rogue"
            rogue.mkdir(parents=True)
            run_stage3.write_manifest_atomic(rogue / "run-manifest.json", valid_asset_failure_child(output))
            child = run_stage3._read_child(output, rogue / "run-manifest.json")
            with self.assertRaisesRegex(RuntimeError, "child manifest path"):
                run_stage3._gate_asset_failures(child, output, "opengauss")

    def test_gate_rejects_types_identity_namespace_and_cleanup_binding(self):
        """捕获 bool/float、摘要、namespace 与物理 cleanup target 冒充证据。"""
        mutations = (
            lambda value: value["results"][0]["reconcile"].update(orphan_count=False),
            lambda value: value["results"][0]["store_observation"].update(object_exists=1),
            lambda value: value["results"][0].update(event_visible=1.0),
            lambda value: value["results"][0].update(asset_id="0" * 64, sha256="0" * 64),
            lambda value: value["results"][0].update(namespace=""),
            lambda value: value["results"][0]["cleanup"].update(adapter_cleanup_target="wrong"),
            lambda value: value["results"][0]["cleanup"].update(namespace_removed=1),
            lambda value: value["results"][0].update(execution_error=""),
        )
        for mutation in mutations:
            self.assert_rejected(mutation)

    def test_gate_rejects_legacy_namespace_prefix(self):
        """捕获会使最长物理 schema/database 超过 63 字符的旧前缀。"""
        def use_legacy_prefix(value):
            for index, result in enumerate(value["results"]):
                namespace = f"jsons3_asset_failure_{result['case']}_{index:010x}"
                result["namespace"] = namespace
                result["cleanup"]["namespace"] = namespace
                result["cleanup"]["adapter_cleanup_target"] = namespace + "_asset_ref"

        self.assert_rejected(use_legacy_prefix)

    def test_production_factories_construct_bounded_longest_case_without_database(self):
        """捕获最长 case 派生出的生产 schema/database 超过 63 字符或构造时连接数据库。"""
        namespace = "jsons3_af_upload_then_db_failure_0123456789"
        endpoints = production.EngineEndpoints()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            production.OpenGaussAdapter, "connect_worker",
            side_effect=AssertionError("factory must not connect to openGauss"),
        ), patch.object(
            production.ClickHouseAdapter, "connect_worker",
            side_effect=AssertionError("factory must not connect to ClickHouse"),
        ):
            for engine, attribute in (("opengauss", "schema"), ("clickhouse", "database")):
                with self.subTest(engine=engine):
                    adapter_factory, _, _ = production.asset_failure_factories(engine, endpoints)
                    control = adapter_factory(namespace, Path(directory) / engine / "objects")
                    physical = getattr(control.adapter, attribute)
                    self.assertEqual(physical, namespace + "_asset_ref")
                    self.assertLessEqual(len(physical), 63)

    def test_gate_rejects_each_fixed_failure_semantic_family(self):
        """捕获状态链、resolver、orphan、对象、publish 与恢复语义漂移。"""
        mutations = (
            lambda value: value["results"][0].update(injection_point="wrong"),
            lambda value: value["results"][1]["catalog_transitions"][0].update(after="failed"),
            lambda value: value["results"][2]["resolver"].update(error="corrupt"),
            lambda value: value["results"][3].update(event_visible=True),
            lambda value: value["results"][3]["reconcile"].update(orphan_count=0, orphan_paths=[]),
            lambda value: value["results"][0]["store_observation"].update(object_exists=True),
            lambda value: value["results"][4]["store_observation"].update(publish_attempts=[]),
            lambda value: value["results"][0].update(recovery_actions=["wrong"]),
            lambda value: value["results"][0]["recovery_resolver"].update(content_length=1),
            lambda value: value["results"][5].update(final_status="available"),
        )
        for mutation in mutations:
            self.assert_rejected(mutation)

    def test_gate_rejects_output_escape_and_cross_case_object_paths(self):
        """捕获 object/reconcile 路径逃逸 output 或串用另一 case 的对象。"""
        def escape(value):
            value["results"][0]["store_observation"]["object_path"] = "/tmp/escape"

        def cross_case(value):
            source = value["results"][0]["store_observation"]["object_path"]
            value["results"][3]["store_observation"]["object_path"] = source
            value["results"][3]["reconcile"]["orphan_paths"] = [source]

        def cleanup_escape(value):
            value["results"][0]["cleanup"]["object_directory"] = "/tmp"

        for mutation in (escape, cross_case, cleanup_escape):
            self.assert_rejected(mutation)


class AssetFailureCliTests(unittest.TestCase):
    """验证 Asset 六故障 CLI 的固定生产接线和失败证据。"""

    def run_asset_failure_cli(self, root, engine="opengauss", *, factory_error=None,
                              runtime=None, container=None, host=None,
                              probe_cleanup_error=None, runner_error=None,
                              gate_error=None, replace_after_gate=False,
                              invalid_partial_child=False):
        """以受控 production 依赖执行 CLI，并返回 envelope 接线记录。"""
        output = root / "attempt-1"
        calls = []
        create_calls = []
        adapter = SimpleNamespace(engine=engine)
        control = SimpleNamespace(
            adapter=adapter,
            create=lambda: create_calls.append("create"),
        )
        catalog_factory = object()
        fault_injector = object()

        def adapter_factory(namespace, object_directory):
            self.assertEqual(namespace, "jsons3_asset_runtime_probe")
            self.assertEqual(object_directory, output / "runtime-probe-assets")
            object_directory.mkdir(parents=True)
            calls.append("probe")
            return control

        def fake_factories(engine_arg, endpoints):
            self.assertEqual(
                json.loads((output / "run-manifest.json").read_text())["status"],
                "running",
            )
            self.assertEqual(engine_arg, engine)
            self.assertIsInstance(endpoints, production.EngineEndpoints)
            calls.append("factories")
            if factory_error is not None:
                raise factory_error
            return adapter_factory, catalog_factory, fault_injector

        def fake_runtime(adapter_arg, engine_arg):
            self.assertIs(adapter_arg, adapter)
            self.assertEqual(engine_arg, engine)
            calls.append("runtime")
            return runtime or {"version": "test-db", "source": "database-query"}

        expected_container = (
            "agent-trace-opengauss-v6" if engine == "opengauss"
            else "agent-trace-clickhouse-25-12"
        )

        def fake_container(container_name):
            self.assertEqual(container_name, expected_container)
            return container or {
                "container": expected_container,
                "image": engine + "-image",
                "image_id": "sha256:id",
            }

        def fake_runner(child_root, **kwargs):
            self.assertEqual(child_root, output / "child")
            self.assertEqual(set(kwargs), {
                "adapter_factory", "catalog_factory", "fault_injector", "namespace_factory",
            })
            self.assertIs(kwargs["adapter_factory"], adapter_factory)
            self.assertIs(kwargs["catalog_factory"], catalog_factory)
            self.assertIs(kwargs["fault_injector"], fault_injector)
            manifest = valid_asset_failure_child(output)
            namespaces = []
            for case, result in zip(
                run_stage3.run_asset_failures.FAILURE_CASES, manifest["results"],
            ):
                namespace = kwargs["namespace_factory"](case)
                self.assertRegex(namespace, rf"^jsons3_af_{case.name}_[0-9a-f]{{10}}$")
                result["namespace"] = namespace
                result["cleanup"]["namespace"] = namespace
                result["cleanup"]["adapter_cleanup_target"] = namespace + "_asset_ref"
                namespaces.append(namespace)
            self.assertEqual(len(set(namespaces)), 6)
            child_root.mkdir(parents=True)
            if runner_error is not None:
                manifest["status"] = "failed"
                manifest["results"] = manifest["results"][:2]
            if invalid_partial_child:
                (child_root / "run-manifest.json").write_bytes(b"{invalid")
            else:
                run_stage3.write_manifest_atomic(child_root / "run-manifest.json", manifest)
            calls.append("runner")
            if runner_error is not None:
                raise runner_error

        actual_gate = run_stage3._gate_asset_failures

        def gate(child, output_arg, engine_arg):
            if gate_error is not None:
                raise gate_error
            evidence = actual_gate(child, output_arg, engine_arg)
            if replace_after_gate:
                manifest = json.loads(Path(child["_path"]).read_text())
                manifest["replaced"] = True
                run_stage3.write_manifest_atomic(Path(child["_path"]), manifest)
            return evidence

        patches = [
            patch.object(run_stage3.production, "asset_failure_factories",
                         side_effect=fake_factories),
            patch.object(run_stage3.run_asset_failures, "run_failure_catalog",
                         side_effect=fake_runner),
            patch.object(run_stage3.run_layout_matrix, "_engine_runtime",
                         side_effect=fake_runtime),
            patch.object(run_stage3.run_layout_matrix, "_container_evidence",
                         side_effect=fake_container),
            patch.object(run_stage3.run_layout_matrix, "_host_evidence", return_value=host or {
                "platform": "test", "machine": "x86_64", "cpu_count": 1,
                "memory_total_kib": 1,
            }),
            patch.object(run_stage3, "_gate_asset_failures", side_effect=gate),
        ]
        if probe_cleanup_error is not None:
            patches.append(patch.object(
                run_stage3, "_remove_asset_directory", side_effect=probe_cleanup_error,
            ))
        for context in patches:
            context.start()
        try:
            result = run_stage3.main([
                "asset-failures", "--output", str(output), "--engine", engine,
            ])
        finally:
            for context in reversed(patches):
                context.stop()
        return result, output, calls, create_calls

    def test_parser_and_both_engines_use_fixed_production_dependencies(self):
        """捕获 CLI 暴露可调依赖、串用 endpoint/container 或创建 probe namespace。"""
        parser = run_stage3.build_parser()
        for option in (
            "--input", "--layout", "--case", "--fixture", "--namespace", "--store",
            "--writer", "--endpoint", "--factory", "--fault-injector",
        ):
            with self.subTest(option=option), self.assertRaises(SystemExit):
                parser.parse_args([
                    "asset-failures", "--output", "out", "--engine", "opengauss", option, "x",
                ])
        for engine, endpoint in (
            ("opengauss", {"host": "127.0.0.1", "port": 15432}),
            ("clickhouse", {"host": "127.0.0.1", "port": 18123}),
        ):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                result, output, calls, create_calls = self.run_asset_failure_cli(
                    Path(directory), engine,
                )
                envelope = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(result, 0)
                self.assertEqual(calls, ["factories", "probe", "runtime", "runner"])
                self.assertEqual(create_calls, [])
                self.assertEqual(envelope["status"], "complete")
                self.assertEqual(envelope["runtime"]["endpoint"], endpoint)
                self.assertEqual(envelope["runtime"]["engine"], engine)
                self.assertEqual(envelope["runtime"]["layout"], "asset_ref")
                self.assertNotIn("input", envelope)
                self.assertNotIn("truth", envelope)
                self.assertNotIn("query_catalog_sha256", envelope)
                self.assertEqual(envelope["namespace_policy"]["case_order"],
                                 list(ASSET_FAILURE_CASES))
                self.assertEqual(len(set(envelope["namespace_policy"]["namespaces"])), 6)
                self.assertEqual(envelope["asset_failures"]["engine"], engine)
                self.assertEqual(envelope["child"]["path"], "child/run-manifest.json")
                self.assertIn("asset_failure_runner", envelope["code"])
                self.assertTrue(envelope["cleanup"]["namespaces_removed"])
                self.assertTrue(envelope["cleanup"]["object_directories_removed"])
                self.assertTrue(envelope["cleanup"]["runtime_probe_directory_removed"])
                self.assertEqual(envelope["cleanup"]["runtime_probe_directory"],
                                 str(output / "runtime-probe-assets"))
                self.assertFalse((output / "runtime-probe-assets").exists())

    def test_factory_runtime_and_probe_cleanup_fail_closed_with_available_evidence(self):
        """捕获前置失败伪报 cleanup、删除 probe 现场或丢失已采集 runtime。"""
        scenarios = (
            ("factory", {"factory_error": RuntimeError("factory failed")}),
            ("runtime", {"runtime": {"version": "", "source": "database-query"}}),
            ("cleanup", {"probe_cleanup_error": OSError("probe unlink failed")}),
        )
        for name, options in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(Exception):
                    self.run_asset_failure_cli(Path(directory), **options)
                output = Path(directory) / "attempt-1"
                failed = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["namespace_policy"]["namespaces"], [])
                self.assertFalse(failed["cleanup"]["namespaces_removed"])
                self.assertFalse(failed["cleanup"]["object_directories_removed"])
                self.assertFalse(failed["cleanup"]["runtime_probe_directory_removed"])
                if name == "factory":
                    self.assertNotIn("runtime", failed)
                    self.assertFalse((output / "runtime-probe-assets").exists())
                else:
                    self.assertTrue((output / "runtime-probe-assets").is_dir())
                if name == "cleanup":
                    self.assertEqual(failed["runtime"]["engine_runtime"]["version"], "test-db")

    def test_runner_partial_gate_and_post_gate_failures_preserve_exact_state(self):
        """捕获 partial namespace/child 丢失，或 gate 后替换仍发布 complete。"""
        scenarios = (
            ("runner", {"runner_error": RuntimeError("runner failed")}, 2, False),
            ("gate", {"gate_error": RuntimeError("gate failed")}, 6, False),
            ("post_gate", {"replace_after_gate": True}, 6, True),
        )
        for name, options, namespace_count, cleanup_complete in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    self.run_asset_failure_cli(Path(directory), **options)
                output = Path(directory) / "attempt-1"
                failed = json.loads((output / "run-manifest.json").read_text())
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(failed["child"]["path"], "child/run-manifest.json")
                self.assertEqual(len(failed["namespace_policy"]["namespaces"]), namespace_count)
                self.assertEqual(failed["cleanup"]["namespaces_removed"], cleanup_complete)
                self.assertEqual(failed["cleanup"]["object_directories_removed"], cleanup_complete)
                self.assertTrue(failed["cleanup"]["runtime_probe_directory_removed"])
                self.assertFalse((output / "runtime-probe-assets").exists())
                if name == "post_gate":
                    self.assertIn("asset_failures", failed)

    def test_invalid_partial_child_does_not_mask_the_runner_exception(self):
        """捕获 partial child 解析失败替换原始数据库运行异常。"""
        with tempfile.TemporaryDirectory() as directory:
            original = RuntimeError("database operation failed")
            with self.assertRaises(RuntimeError) as raised:
                self.run_asset_failure_cli(
                    Path(directory), runner_error=original, invalid_partial_child=True,
                )
            self.assertIs(raised.exception, original)
            failed = json.loads(
                (Path(directory) / "attempt-1" / "run-manifest.json").read_text()
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["child"]["bytes"], len(b"{invalid"))
            self.assertEqual(failed["namespace_policy"]["namespaces"], [])

    def test_runtime_rejects_wrong_source_container_and_host_resources(self):
        """捕获不完整 runtime/container/host 身份进入两引擎 complete envelope。"""
        invalid = (
            {"runtime": {"version": "test", "source": "self-report"}},
            {"runtime": {
                "version": "test", "source": "database-query", "self_reported": True,
            }},
            {"container": {
                "container": "wrong", "image": "image", "image_id": "sha256:id",
            }},
            {"container": {
                "container": "agent-trace-opengauss-v6", "image": "", "image_id": "sha256:id",
            }},
            {"container": {
                "container": "agent-trace-opengauss-v6", "image": "image", "image_id": "",
            }},
            {"host": {
                "platform": "", "machine": "x86_64", "cpu_count": 1,
                "memory_total_kib": 1,
            }},
            {"host": {
                "platform": "test", "machine": "x86_64", "cpu_count": True,
                "memory_total_kib": 1,
            }},
        )
        for options in invalid:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    self.run_asset_failure_cli(Path(directory), **options)
                failed = json.loads(
                    (Path(directory) / "attempt-1" / "run-manifest.json").read_text()
                )
                self.assertEqual(failed["status"], "failed")
                self.assertTrue((Path(directory) / "attempt-1" / "runtime-probe-assets").is_dir())


if __name__ == "__main__":
    unittest.main()
