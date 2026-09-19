import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "report"))

import summarize as report


LAYOUTS = ("same_table", "separate", "full_core", "asset_ref")
PERFORMANCE_WORKLOADS = (
    "main", "equal_total_few_large", "equal_total_many_medium",
)
IDENTITY = "a" * 64
INPUT_IDENTITY = {
    "kind": "formal",
    "generation_manifest": {"bytes": 1, "sha256": "b" * 64},
    "truth": {"bytes": 1, "sha256": "c" * 64},
    "events": {"bytes": 1, "sha256": "d" * 64},
    "identity_sha256": IDENTITY,
}
LAYOUT_TARGETS = {
    "same_table": ("events",),
    "separate": ("events_analytics", "event_payloads"),
    "full_core": ("events_full", "events_core"),
    "asset_ref": ("events_analytics", "assets"),
}
CODE_EVIDENCE = {
    name: {"path": f"/source/{name}.py", "bytes": 100, "sha256": character * 64}
    for name, character in zip(("runner", "common", "assets", "adapter"), "ef01")
}
DDL_SHA256 = {
    key: character * 64
    for key, character in zip(
        (
            (workload, round_index)
            for workload in PERFORMANCE_WORKLOADS + ("correctness_only",)
            for round_index in range(4 if workload in PERFORMANCE_WORKLOADS else 1)
        ),
        "23456789abcde",
    )
}
QUERY_SHA256 = {
    workload: character * 64
    for workload, character in zip(PERFORMANCE_WORKLOADS + ("correctness_only",), "3456")
}


def sample(workload, round_index, position, scenario, number, latency):
    """构造一条已通过 truth 校验的正式原始样本。"""
    return {
        "workload": workload,
        "round_index": round_index,
        "position": position,
        "scenario": scenario,
        "kind": "batch" if scenario.startswith("batch:") else "list",
        "status": "success",
        "query_id": f"{workload}-{round_index}-{scenario}-{number}",
        "database_response_bytes": 10,
        "resolver_payload_bytes": 2,
        "response_bytes": 12,
        "database_protocol_bytes": 14,
        "request_count": 2,
        "query_complete_ms": latency,
        "recovery_ms": latency + 10,
        "validation_ms": latency + 20,
        "application_ready_ms": latency + 30,
        "validation": {"validated_payload_bytes": 1048576},
    }


def formal_target(root, engine="opengauss", layout="same_table"):
    """写入一个最小正式 target，其 round 证据覆盖所有原始样本。"""
    target = Path(root) / engine / layout
    target.mkdir(parents=True)
    workloads = {}
    all_samples = []
    for workload in PERFORMANCE_WORKLOADS + ("correctness_only",):
        count = 4 if workload != "correctness_only" else 1
        rounds = []
        for round_index in range(count):
            order = LAYOUTS[round_index:] + LAYOUTS[:round_index]
            position = order.index(layout)
            values = [round_index + 1] * 30
            round_samples = [
                sample(workload, round_index, position, "list:first", item, value)
                for item, value in enumerate(values)
            ]
            if workload != "correctness_only":
                round_samples.extend(
                    sample(workload, round_index, position, f"batch:{workload}", item, 20)
                    for item in range(5)
                )
            if engine == "opengauss":
                for item in round_samples:
                    item["database_protocol_bytes"] = None
            all_samples.extend(round_samples)
            ids = [item["query_id"] for item in round_samples]
            access = {
                "plans": {query_id: "observed scan" for query_id in ids},
                "query_details": {
                    query_id: {
                        "scanned_rows": 1,
                        "scanned_bytes": 1 if engine == "clickhouse" else None,
                        "scanned_bytes_status": (
                            "observed" if engine == "clickhouse" else "unavailable"
                        ),
                    }
                    for query_id in ids
                },
            }
            if engine == "clickhouse":
                access["query_finish"] = {
                    query_id: {"type": "QueryFinish", "read_rows": 1, "read_bytes": 1}
                    for query_id in ids
                }
            else:
                access["index_scans"] = {"events_list_idx": len(ids)}
            rounds.append({
                "format": "agent-trace-json-storage-stage3-layout-run",
                "format_version": 1,
                "status": "complete",
                "engine": engine,
                "layout": layout,
                "workload": workload,
                "round_index": round_index,
                "position": position,
                "round_order": list(order),
                "input": json.loads(json.dumps(INPUT_IDENTITY)),
                "code": json.loads(json.dumps(CODE_EVIDENCE)),
                "ddl_sha256": DDL_SHA256[(workload, round_index)],
                "query_catalog_sha256": QUERY_SHA256[workload],
                "correctness": {
                    "truth_identity": IDENTITY,
                    "formal_samples": len(round_samples),
                    "successful_samples": len(round_samples),
                    "failed_samples": 0,
                    "response_bytes_validated": True,
                },
                "access": access,
                "access_validation": {
                    item["query_id"]: {
                        "scenario": item["scenario"], "kind": item["kind"],
                        "scanned_rows": 1, "scanned_bytes": 1,
                        "access_structure": "observed", "mode": "formal",
                    }
                    for item in round_samples
                },
                "write": {"final_watermark": 10, "wall_ms": 1.0},
                "maintenance": {
                    "completed": True,
                    "watermarks": {name: 10 for name in LAYOUT_TARGETS[layout]},
                    "natural_stable_parts": engine == "clickhouse",
                    "optimize_final": False,
                },
                "storage": {
                    "tables": {
                        name: (
                            {
                                "part_count": 2, "rows": 10, "marks": 3,
                                "compressed_bytes": 100, "uncompressed_bytes": 200,
                                "columns": {},
                            }
                            if engine == "clickhouse"
                            else {
                                "heap_bytes": 40, "index_bytes": 20,
                                "toast_bytes": 40, "total_bytes": 100,
                            }
                        )
                        for name in LAYOUT_TARGETS[layout]
                    },
                    "merges": [],
                },
                "cleanup": {"removed": True, "asset_directory_removed": True},
            })
        workloads[workload] = {"status": "complete", "rounds": rounds}
    manifest = {
        "format": "agent-trace-json-storage-stage3-layout-matrix",
        "format_version": 1,
        "status": "complete",
        "engine": engine,
        "layout": layout,
        "latin_square": [list(LAYOUTS[index:] + LAYOUTS[:index]) for index in range(4)],
        "input": json.loads(json.dumps(INPUT_IDENTITY)),
        "code": json.loads(json.dumps(CODE_EVIDENCE)),
        "ddl_sha256": sorted(DDL_SHA256.values()),
        "query_catalog_sha256": {
            workload: [QUERY_SHA256[workload]]
            for workload in PERFORMANCE_WORKLOADS + ("correctness_only",)
        },
        "correctness": {
            "rounds_complete": 4,
            "formal_samples": len(all_samples),
            "successful_samples": len(all_samples),
            "response_bytes_validated": True,
        },
        "workloads": workloads,
        "summary": {"main": {"pooled": "must not be used"}},
        "global_cleanup": {"removed": True},
    }
    (target / "run-manifest.json").write_text(json.dumps(manifest))
    (target / "samples.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in all_samples)
    )
    return target, manifest, all_samples


def formal_shard(root, workload, engine="opengauss", layout="same_table"):
    """写入一个与生产分片一致的 single-workload 正式 shard。

    生产命令按 engine + workload 分目录发布，每个 manifest 只含一个 workload，
    且只把 main 的 round 计入 correctness.rounds_complete。
    """
    with tempfile.TemporaryDirectory() as scratch:
        _, manifest, samples = formal_target(scratch, engine, layout)
    rounds = manifest["workloads"][workload]["rounds"]
    shard_samples = [item for item in samples if item["workload"] == workload]
    manifest.pop("summary")
    manifest["workloads"] = {workload: manifest["workloads"][workload]}
    manifest["query_catalog_sha256"] = {workload: manifest["query_catalog_sha256"][workload]}
    manifest["ddl_sha256"] = sorted({record["ddl_sha256"] for record in rounds})
    manifest["correctness"] = {
        "rounds_complete": 4 if workload == "main" else 0,
        "formal_samples": len(shard_samples),
        "successful_samples": len(shard_samples),
        "response_bytes_validated": True,
    }
    target = Path(root) / f"{engine}-{layout}-{workload}"
    target.mkdir(parents=True)
    (target / "run-manifest.json").write_text(json.dumps(manifest))
    (target / "samples.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in shard_samples)
    )
    return target, manifest, shard_samples


def rewrite_manifest(target, mutate):
    """改写一个 target 或 shard 的 manifest，用于构造证据漂移。"""
    path = Path(target) / "run-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest))
    return manifest


def drift_code(manifest):
    """把 target 与每轮的 adapter code 证据同时改到另一 identity。"""
    manifest["code"]["adapter"]["sha256"] = "9" * 64
    for state in manifest["workloads"].values():
        for record in state["rounds"]:
            record["code"]["adapter"]["sha256"] = "9" * 64


def drift_input(manifest):
    """把正式 input identity 与每轮的引用同时改到另一 identity。"""
    manifest["input"]["identity_sha256"] = "5" * 64
    for state in manifest["workloads"].values():
        for record in state["rounds"]:
            record["input"]["identity_sha256"] = "5" * 64
            record["correctness"]["truth_identity"] = "5" * 64


PART_STATE_STATES = ("fragmented", "merging", "stable", "single_part")
PART_STATE_SCENARIOS = (
    "batch:main",
    "detail:entropy_512k",
    "detail:text_2m",
    "detail:text_512k",
    "detail:text_64k",
    "list:first",
    "list:middle",
    "preview:first",
    "preview:middle",
    "trace:p25",
    "trace:p50",
    "trace:p95",
)
PART_STATE_KINDS = {scenario: scenario.split(":", 1)[0] for scenario in PART_STATE_SCENARIOS}
PART_STATE_SAMPLES_PER_QUERY = 30
PART_STATE_CONTROLLED = {
    "same_table": "events",
    "separate": "events_analytics",
    "full_core": "events_core",
    "asset_ref": "events_analytics",
}
PART_STATE_CODE_ROLES = (
    "run_stage3", "production", "generator", "layout_runner",
    "part_state_runner", "common", "assets",
)


def part_state_sample(layout, state, scenario, number, latency=10):
    """构造一条已通过 truth 校验的 part-state 查询样本。"""
    return {
        "scenario": scenario,
        "kind": PART_STATE_KINDS[scenario],
        "status": "success",
        "error": None,
        "query_id": f"{layout}-{state}-{scenario}-{number}",
        "query_complete_ms": latency,
        "recovery_ms": latency + 1,
        "validation_ms": latency + 2,
        "application_ready_ms": latency + 3,
        "response_bytes": 100,
        "database_response_bytes": 100,
        "resolver_payload_bytes": 0,
        "resolver_requests": 0,
        "resolver_read_ms": 0.0,
        "database_protocol_bytes": 100,
        "request_count": 1,
        "validation": {"row_count": 1, "validated_payload_bytes": 100},
    }


def part_state_state(layout, name):
    """构造一个满足真实状态谓词与固定 scenario 覆盖的最小状态证据。"""
    targets = LAYOUT_TARGETS[layout]
    controlled = PART_STATE_CONTROLLED[layout]
    counts = {table: 190 for table in targets}
    if name == "stable":
        counts = {table: (3 if table == controlled else 7) for table in targets}
    elif name == "single_part":
        counts = {table: 1 for table in targets}
    observation = {"active_part_counts": dict(counts), "active_merges": []}
    samples = [
        part_state_sample(layout, name, scenario, number)
        for scenario in PART_STATE_SCENARIOS
        for number in range(PART_STATE_SAMPLES_PER_QUERY)
    ]
    query_ids = [item["query_id"] for item in samples]
    return {
        "name": name,
        "controlled_table": controlled,
        "predicate_proven": True,
        "tables": {
            table: {
                "part_count": counts[table],
                "marks": counts[table] * 2,
                "compressed_bytes": 100 * counts[table],
                "uncompressed_bytes": 200 * counts[table],
            }
            for table in targets
        },
        "active_merges": [{"table": controlled, "num_parts": 20}] if name == "merging" else [],
        "asset_store": (
            {
                "available_object_count": 160,
                "available_bytes": 128450560,
                "orphan_object_count": 0,
                "orphan_bytes": 0,
            }
            if layout == "asset_ref" else None
        ),
        "observations": [observation] * (3 if name == "stable" else 1),
        "query_samples": samples,
        "query_plans": {query_id: "observed scan" for query_id in query_ids},
        "query_details": {
            query_id: {
                "kind": sample["kind"],
                "statement": "SELECT 1",
                "declared_source": "events",
                "scanned_rows": 2,
                "scanned_bytes": 2,
            }
            for query_id, sample in zip(query_ids, samples)
        },
        "query_finish": {
            query_id: {"type": "QueryFinish", "exception_code": 0, "read_rows": 2, "read_bytes": 2}
            for query_id in query_ids
        },
        "successful_samples": len(samples),
        "failed_samples": 0,
        "query_finish_count": len(samples),
        "optimized_targets": list(targets) if name == "single_part" else [],
        "error": None,
    }


def part_state_child(layout):
    """构造一个与生产 child schema 一致的最小 part-state manifest。"""
    targets = LAYOUT_TARGETS[layout]
    namespace = f"jsons3_parts_{layout}_0123456789"
    return {
        "format": "agent-trace-json-storage-stage3-clickhouse-part-states",
        "format_version": 1,
        "run_id": f"jsons3-clickhouse-parts-{layout}",
        "status": "complete",
        "layout": layout,
        "physical_targets": list(targets),
        "controlled_table": PART_STATE_CONTROLLED[layout],
        "state_order": list(PART_STATE_STATES),
        "cache_limits": "ordered-control-warm-cache-no-os-cache-drop-not-main-matrix-paired",
        "samples_per_query": PART_STATE_SAMPLES_PER_QUERY,
        "states": [part_state_state(layout, name) for name in PART_STATE_STATES],
        "restoration": {
            "attempted": True,
            "restored": True,
            "targets": ([PART_STATE_CONTROLLED[layout]] if layout == "asset_ref" else list(targets)),
        },
        "cleanup": {"namespace": f"{namespace}_{layout}", "removed": True},
    }


def part_state_envelope(layout, content, child):
    """构造一个与生产 envelope schema 一致的最小 part-state 运行结果。"""
    namespace = f"jsons3_parts_{layout}_0123456789"
    return {
        "format": "agent-trace-json-storage-stage3-production-run",
        "format_version": 1,
        "run_id": f"jsons3-production-part-states-{layout}",
        "status": "complete",
        "operation": "part-states",
        "command": ["python3", "run_stage3.py", "part-states"],
        "namespace_policy": {
            "strategy": "unique-random-suffix",
            "prefix": f"jsons3_parts_{layout}_",
            "namespace": namespace,
            "reuse": False,
        },
        "code": {
            role: {"path": f"/source/{role}.py", "bytes": 10, "sha256": character * 64}
            for role, character in zip(PART_STATE_CODE_ROLES, "1234567")
        },
        "input": json.loads(json.dumps(INPUT_IDENTITY)),
        "truth": {
            "seed": 20260907,
            "identity_sha256": IDENTITY,
            "record_count": 48534,
            "block_size": 256,
            "block_count": 190,
        },
        "query_catalog_sha256": "f" * 64,
        "runtime": {
            "operation": "part-states",
            "engine": "clickhouse",
            "layout": layout,
            "endpoint": {"host": "127.0.0.1", "port": 18123},
            "container": {
                "container": "agent-trace-clickhouse-25-12",
                "image": "clickhouse/clickhouse-server:25.12",
                "image_id": "sha256:" + "0" * 64,
            },
            "engine_runtime": {"version": "25.12.11.4", "source": "database-query"},
            "host": {
                "platform": "Linux", "machine": "x86_64",
                "cpu_count": 8, "memory_total_kib": 16291948,
            },
        },
        "child": {
            "path": "child/run-manifest.json",
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "format": child["format"],
            "format_version": child["format_version"],
            "status": child["status"],
            "run_id": child["run_id"],
        },
        "cleanup": {
            "namespace": child["cleanup"]["namespace"],
            "removed": True,
            "asset_directory_removed": True,
            "asset_directory_applicable": layout == "asset_ref",
        },
    }


def write_part_state_run(root, layout, child=None):
    """写入一个最小 part-state production 目录并返回其路径。"""
    run = Path(root) / layout
    (run / "child").mkdir(parents=True)
    child = part_state_child(layout) if child is None else child
    content = json.dumps(child, separators=(",", ":")).encode("utf-8")
    (run / "child" / "run-manifest.json").write_bytes(content)
    envelope = part_state_envelope(layout, content, child)
    (run / "run-manifest.json").write_text(json.dumps(envelope), encoding="utf-8")
    return run


def rewrite_part_state_child(run, mutate):
    """改写 child 并同步 envelope 中由实际 bytes 形成的身份。"""
    path = Path(run) / "child" / "run-manifest.json"
    child = json.loads(path.read_text(encoding="utf-8"))
    mutate(child)
    content = json.dumps(child, separators=(",", ":")).encode("utf-8")
    path.write_bytes(content)
    envelope_path = Path(run) / "run-manifest.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["child"]["bytes"] = len(content)
    envelope["child"]["sha256"] = hashlib.sha256(content).hexdigest()
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    return child


def rewrite_part_state_envelope(run, mutate):
    """改写 part-state production envelope。"""
    path = Path(run) / "run-manifest.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return envelope


class StageThreeSummaryTest(unittest.TestCase):
    """验证正式矩阵汇总的证据门禁、分层统计和原子发布。"""

    def test_rejects_non_complete_target(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            manifest["status"] = "running"
            with self.assertRaisesRegex(ValueError, "status is not complete"):
                report.validate_run(manifest)

    def test_rejects_missing_required_evidence_closed(self):
        cases = (
            ("input", "truth/input identity"),
            ("correctness", "response-byte validation"),
            ("access", "access evidence"),
            ("access_validation", "access evidence"),
            ("write", "write evidence"),
            ("maintenance", "maintenance evidence"),
            ("storage", "storage evidence"),
            ("cleanup", "cleanup evidence"),
        )
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            for field, message in cases:
                candidate = json.loads(json.dumps(manifest))
                del candidate["workloads"]["main"]["rounds"][0][field]
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                    report.validate_run(candidate)

    def test_rejects_missing_or_mismatched_provenance(self):
        """正式 target 和每轮必须共享完整 code、DDL 与 workload query 身份。"""
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            for case in (
                "target_code", "round_code", "target_ddl", "round_ddl",
                "target_query", "round_query",
            ):
                candidate = json.loads(json.dumps(manifest))
                record = candidate["workloads"]["main"]["rounds"][0]
                if case == "target_code":
                    del candidate["code"]
                elif case == "round_code":
                    del record["code"]
                elif case == "target_ddl":
                    candidate["ddl_sha256"] = ["7" * 64]
                elif case == "round_ddl":
                    record["ddl_sha256"] = "7" * 64
                elif case == "target_query":
                    del candidate["query_catalog_sha256"]["main"]
                else:
                    record["query_catalog_sha256"] = "7" * 64
                with self.subTest(case=case), self.assertRaisesRegex(
                    ValueError, "provenance evidence"
                ):
                    report.validate_run(candidate)

    def test_rejects_shallow_access_evidence(self):
        """空 plan、缺 openGauss index scan 或缺正式 validation 标记均失败。"""
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            for case in ("index_scans", "plan", "mode", "access_structure"):
                candidate = json.loads(json.dumps(manifest))
                record = candidate["workloads"]["main"]["rounds"][0]
                query_id = next(iter(record["access"]["plans"]))
                if case == "index_scans":
                    del record["access"]["index_scans"]
                elif case == "plan":
                    record["access"]["plans"][query_id] = ""
                else:
                    del record["access_validation"][query_id][case]
                with self.subTest(case=case), self.assertRaisesRegex(
                    ValueError, "access evidence"
                ):
                    report.validate_run(candidate)

    def test_rejects_incomplete_identity_correctness_and_latin_schedule(self):
        """防止简化输入身份、计数矛盾或错位 Latin 顺序通过门禁。"""
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            del manifest["input"]["truth"]
            with self.assertRaisesRegex(ValueError, "truth/input identity"):
                report.validate_run(manifest)

            _, manifest, _ = formal_target(Path(directory) / "correctness")
            manifest["workloads"]["main"]["rounds"][0]["correctness"][
                "successful_samples"
            ] -= 1
            with self.assertRaisesRegex(ValueError, "correctness evidence"):
                report.validate_run(manifest)

            _, manifest, _ = formal_target(Path(directory) / "latin")
            manifest["workloads"]["main"]["rounds"][0]["round_order"].reverse()
            with self.assertRaisesRegex(ValueError, "Latin square order"):
                report.validate_run(manifest)

    def test_rejects_incomplete_clickhouse_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(Path(directory) / "second", engine="clickhouse")
            round_record = manifest["workloads"]["main"]["rounds"][0]
            del round_record["access"]["query_finish"]
            with self.assertRaisesRegex(ValueError, "QueryFinish"):
                report.validate_run(manifest)
            _, manifest, _ = formal_target(directory, engine="clickhouse")
            manifest["workloads"]["main"]["rounds"][0]["maintenance"]["optimize_final"] = True
            with self.assertRaisesRegex(ValueError, "optimize_final"):
                report.validate_run(manifest)
            _, manifest, _ = formal_target(Path(directory) / "malformed", engine="clickhouse")
            query_finish = manifest["workloads"]["main"]["rounds"][0]["access"]["query_finish"]
            next(iter(query_finish.values()))["type"] = "QueryStart"
            with self.assertRaisesRegex(ValueError, "QueryFinish"):
                report.validate_run(manifest)

    def test_rejects_invalid_round_sample_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            target, manifest, samples = formal_target(directory)
            samples[0]["status"] = "failed"
            (target / "samples.jsonl").write_text("".join(json.dumps(item) + "\n" for item in samples))
            with self.assertRaisesRegex(ValueError, "failed sample"):
                report.summarize([target])
            target, manifest, samples = formal_target(Path(directory) / "second")
            samples = [item for item in samples if item["query_id"] != "main-0-list:first-29"]
            (target / "samples.jsonl").write_text("".join(json.dumps(item) + "\n" for item in samples))
            with self.assertRaisesRegex(ValueError, "sample evidence"):
                report.summarize([target])

    def test_rejects_non_finite_metric(self):
        """防止 NaN 绕过非负数检查并污染 percentile 和 JSON 输出。"""
        with tempfile.TemporaryDirectory() as directory:
            target, _, samples = formal_target(directory)
            samples[0]["query_complete_ms"] = float("nan")
            (target / "samples.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in samples)
            )
            with self.assertRaisesRegex(ValueError, "query_complete_ms is invalid"):
                report.summarize([target])

    def test_rejects_schema_invalid_numeric_types_and_zero_response(self):
        """整数证据拒绝 bool/float，完整响应 bytes 必须为正数。"""
        with tempfile.TemporaryDirectory() as directory:
            for case in (
                "input_bool", "round_index_bool", "position_bool",
                "response_float", "request_count_float", "storage_float",
                "zero_response",
            ):
                target, manifest, samples = formal_target(Path(directory) / case)
                if case == "input_bool":
                    manifest["input"]["generation_manifest"]["bytes"] = True
                    for workload in manifest["workloads"].values():
                        for record in workload["rounds"]:
                            record["input"]["generation_manifest"]["bytes"] = True
                elif case == "round_index_bool":
                    manifest["workloads"]["main"]["rounds"][1]["round_index"] = True
                elif case == "position_bool":
                    manifest["workloads"]["main"]["rounds"][3]["position"] = True
                elif case == "response_float":
                    samples[0]["database_response_bytes"] = 10.5
                    samples[0]["response_bytes"] = 12.5
                elif case == "request_count_float":
                    samples[0]["request_count"] = 2.5
                elif case == "storage_float":
                    table = next(iter(manifest["workloads"]["main"]["rounds"][0]["storage"]["tables"].values()))
                    table["total_bytes"] = 100.5
                else:
                    samples[0]["database_response_bytes"] = 0
                    samples[0]["resolver_payload_bytes"] = 0
                    samples[0]["response_bytes"] = 0
                (target / "run-manifest.json").write_text(json.dumps(manifest))
                (target / "samples.jsonl").write_text(
                    "".join(json.dumps(item) + "\n" for item in samples)
                )
                with self.subTest(case=case), self.assertRaises(ValueError):
                    report.summarize([target])

    def test_rejects_zero_batch_application_ready_instead_of_dropping_sample(self):
        """batch 吞吐必须使用全部五个成功样本，零分母使整轮失败。"""
        with tempfile.TemporaryDirectory() as directory:
            target, _, samples = formal_target(directory)
            batch = next(item for item in samples if item["scenario"] == "batch:main")
            batch["application_ready_ms"] = 0
            (target / "samples.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in samples)
            )
            with self.assertRaisesRegex(ValueError, "application_ready_ms must be positive"):
                report.summarize([target])

    def test_rejects_repeated_round_index_and_missing_scan_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            manifest["workloads"]["main"]["rounds"][1]["round_index"] = 0
            with self.assertRaisesRegex(ValueError, "round index is duplicate"):
                report.validate_run(manifest)
            _, manifest, _ = formal_target(Path(directory) / "second")
            detail = next(iter(manifest["workloads"]["main"]["rounds"][0]["access"]["query_details"].values()))
            del detail["scanned_bytes"]
            with self.assertRaisesRegex(ValueError, "access evidence"):
                report.validate_run(manifest)

    def test_rejects_round_identity_watermark_and_clickhouse_merge_gaps(self):
        """防止错配 workload、水位或 ClickHouse merge 证据进入正式结果。"""
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_target(directory)
            manifest["workloads"]["main"]["rounds"][0]["workload"] = "correctness_only"
            with self.assertRaisesRegex(ValueError, "workload identity"):
                report.validate_run(manifest)

            _, manifest, _ = formal_target(Path(directory) / "watermark")
            manifest["workloads"]["main"]["rounds"][0]["maintenance"]["watermarks"]["events"] = 9
            with self.assertRaisesRegex(ValueError, "watermark evidence"):
                report.validate_run(manifest)

            _, manifest, _ = formal_target(Path(directory) / "clickhouse", engine="clickhouse")
            del manifest["workloads"]["main"]["rounds"][0]["storage"]["merges"]
            with self.assertRaisesRegex(ValueError, "part/merge storage evidence"):
                report.validate_run(manifest)

    def test_accepts_all_formal_engine_layout_target_schemas(self):
        """确认汇总接口接受 runner 会发布的八种正式 target schema。"""
        with tempfile.TemporaryDirectory() as directory:
            targets = [
                formal_target(Path(directory) / f"{engine}-{layout}", engine, layout)[0]
                for engine in ("opengauss", "clickhouse")
                for layout in LAYOUTS
            ]
            summary = report.summarize(targets)
            self.assertEqual(len(summary["matrix"]), 8)
            self.assertEqual(summary, report.summarize(list(reversed(targets))))

    def test_rejects_scenario_or_kind_not_explained_by_round_evidence(self):
        """防止 query ID 存在但对应的 scenario 或 kind 证据错配。"""
        with tempfile.TemporaryDirectory() as directory:
            target, manifest, samples = formal_target(directory)
            query_id = samples[0]["query_id"]
            manifest["workloads"]["main"]["rounds"][0]["access_validation"][query_id][
                "scenario"
            ] = "preview:first"
            (target / "run-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "scenario.*evidence"):
                report.summarize([target])

            target, manifest, samples = formal_target(Path(directory) / "kind")
            samples[0]["kind"] = "batch"
            query_id = samples[0]["query_id"]
            manifest["workloads"]["main"]["rounds"][0]["access_validation"][query_id][
                "kind"
            ] = "batch"
            (target / "run-manifest.json").write_text(json.dumps(manifest))
            (target / "samples.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in samples)
            )
            with self.assertRaisesRegex(ValueError, "scenario/kind evidence"):
                report.summarize([target])

    def test_rejects_wrong_measurement_count_after_evidence_removal(self):
        """确认计数门禁独立拒绝 29 个普通样本和 4 个 batch 样本。"""
        for scenario, query_id in (
            ("list:first", "main-0-list:first-29"),
            ("batch:main", "main-0-batch:main-4"),
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                target, manifest, samples = formal_target(directory)
                samples = [item for item in samples if item["query_id"] != query_id]
                record = manifest["workloads"]["main"]["rounds"][0]
                for evidence in ("plans", "query_details"):
                    del record["access"][evidence][query_id]
                del record["access_validation"][query_id]
                record["correctness"]["formal_samples"] -= 1
                record["correctness"]["successful_samples"] -= 1
                (target / "run-manifest.json").write_text(json.dumps(manifest))
                (target / "samples.jsonl").write_text(
                    "".join(json.dumps(item) + "\n" for item in samples)
                )
                with self.assertRaisesRegex(ValueError, "required successful samples"):
                    report.summarize([target])

    def test_rejects_missing_raw_scenario_even_when_other_samples_remain(self):
        """防止整类正式场景缺失后仍以剩余 raw samples 发布结果。"""
        with tempfile.TemporaryDirectory() as directory:
            target, _, samples = formal_target(directory)
            samples = [
                item for item in samples
                if item["workload"] not in PERFORMANCE_WORKLOADS
                or item["scenario"] != "list:first"
            ]
            (target / "samples.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in samples)
            )
            with self.assertRaisesRegex(ValueError, "sample evidence"):
                report.summarize([target])

    def test_uses_raw_samples_and_round_first_statistics(self):
        with tempfile.TemporaryDirectory() as directory:
            target, _, samples = formal_target(directory)
            for item in samples:
                if item["workload"] == "main" and item["scenario"] == "list:first":
                    base = item["round_index"]
                    item["application_ready_ms"] = (0, 0, 100, 100)[base]
                    if item["query_id"].endswith("-29"):
                        item["application_ready_ms"] += 100
            (target / "samples.jsonl").write_text("".join(json.dumps(item) + "\n" for item in samples))
            summary = report.summarize([target])
            result = summary["matrix"][0]["workloads"]["main"]["scenarios"]["list:first"]
            self.assertEqual([item["round_index"] for item in result["rounds"]], [0, 1, 2, 3])
            self.assertEqual(result["round_statistic_median"]["application_ready_ms"]["p95"], 50.0)
            self.assertNotEqual(result["round_statistic_median"]["application_ready_ms"]["p95"], 100.0)

    def test_keeps_metrics_and_workloads_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            target, _, samples = formal_target(directory)
            samples[0]["query_complete_ms"] = 10000
            (target / "samples.jsonl").write_text("".join(json.dumps(item) + "\n" for item in samples))
            summary = report.summarize([target])
            workloads = summary["matrix"][0]["workloads"]
            self.assertEqual(set(workloads), set(PERFORMANCE_WORKLOADS))
            scenario = workloads["main"]["scenarios"]["list:first"]["round_statistic_median"]
            self.assertEqual(scenario["recovery_ms"]["p50"], 12.5)
            self.assertEqual(scenario["validation_ms"]["p50"], 22.5)
            self.assertEqual(scenario["application_ready_ms"]["p50"], 32.5)

    def test_writes_complete_json_atomically_and_cleans_failed_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            fsync_targets = []
            publication_order = []
            link_paths = []
            original_link = os.link
            original_unlink = Path.unlink

            def observe_link(source, destination):
                link_paths.append((Path(source), Path(destination)))
                original_link(source, destination)

            def observe_unlink(path, *args, **kwargs):
                if path.name.endswith(".tmp"):
                    publication_order.append("temp unlink")
                return original_unlink(path, *args, **kwargs)

            def observe_fsync(fd):
                target = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                fsync_targets.append(target)
                publication_order.append(f"{target} fsync")

            with (
                patch.object(report.os, "fsync", side_effect=observe_fsync),
                patch.object(report.os, "link", side_effect=observe_link),
                patch.object(Path, "unlink", autospec=True, side_effect=observe_unlink),
            ):
                report.write_summary_atomic(output, {"status": "complete", "value": 1})
            self.assertEqual(json.loads(output.read_text()), {"status": "complete", "value": 1})
            self.assertEqual(fsync_targets, ["file", "directory"])
            self.assertEqual(publication_order, ["file fsync", "temp unlink", "directory fsync"])
            self.assertEqual(link_paths[0][0].parent, output.parent)
            self.assertEqual(link_paths[0][1], output)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])
            output.unlink()
            with patch.object(report.os, "link", side_effect=OSError("link failed")):
                with self.assertRaisesRegex(OSError, "link failed"):
                    report.write_summary_atomic(output, {"status": "complete"})
            self.assertFalse(output.exists())
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_directory_fsync_failure_preserves_complete_target_and_retry_fsyncs(self):
        """发布后持久化失败保留完整内容，相同内容 retry 收敛目录持久化。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            expected = b'{"status":"complete"}\n'
            calls = 0

            def fail_directory_fsync(_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")

            with patch.object(report.os, "fsync", side_effect=fail_directory_fsync):
                with self.assertRaisesRegex(OSError, "directory fsync failed"):
                    report.write_summary_atomic(output, {"status": "complete"})
            self.assertEqual(output.read_bytes(), expected)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

            retry_fsyncs = []
            with patch.object(
                report.os, "fsync",
                side_effect=lambda fd: retry_fsyncs.append(
                    "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                ),
            ):
                report.write_summary_atomic(output, {"status": "complete"})
            self.assertEqual(retry_fsyncs, ["directory"])
            self.assertEqual(output.read_bytes(), expected)

    def test_existing_summary_is_idempotent_and_never_clobbered(self):
        """既有有效结果只允许相同内容重放，不允许覆盖或失败后丢失。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            original = b'{"status":"complete","value":1}\n'
            output.write_bytes(original)

            with patch.object(report.os, "link") as link:
                report.write_summary_atomic(output, {"status": "complete", "value": 1})
            link.assert_not_called()
            self.assertEqual(output.read_bytes(), original)

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                report.write_summary_atomic(output, {"status": "complete", "value": 2})
            self.assertEqual(output.read_bytes(), original)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_publish_race_preserves_different_winner_and_cleans_temp(self):
        """发布边界出现不同内容时，必须以竞态胜出者为准。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            winner = b'{"status":"complete","value":2}\n'
            original_link = os.link
            calls = 0

            def publish_after_winner(source, destination):
                nonlocal calls
                calls += 1
                Path(destination).write_bytes(winner)
                original_link(source, destination)

            with patch.object(report.os, "link", side_effect=publish_after_winner):
                with self.assertRaisesRegex(FileExistsError, "already exists"):
                    report.write_summary_atomic(output, {"status": "complete", "value": 1})
            self.assertEqual(calls, 1)
            self.assertEqual(output.read_bytes(), winner)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_publish_race_accepts_identical_winner_and_cleans_temp(self):
        """发布边界出现相同内容时，按幂等成功处理。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            winner = b'{"status":"complete","value":1}\n'
            original_link = os.link
            calls = 0

            def publish_after_winner(source, destination):
                nonlocal calls
                calls += 1
                Path(destination).write_bytes(winner)
                original_link(source, destination)

            with patch.object(report.os, "link", side_effect=publish_after_winner):
                report.write_summary_atomic(output, {"status": "complete", "value": 1})
            self.assertEqual(calls, 1)
            self.assertEqual(output.read_bytes(), winner)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])

    def test_fsync_failure_does_not_unlink_at_the_stat_race_boundary(self):
        """目录 fsync 失败后不执行无法原子确认所有权的 output unlink。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "summary.json"
            competitor = Path(directory) / "competitor.json"
            published = b'{"status":"complete","value":1}\n'
            winner = b'{"status":"complete","value":2}\n'
            calls = 0
            output_unlinks = 0
            original_replace = os.replace
            original_unlink = Path.unlink

            def fail_directory_fsync(_):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("directory fsync failed")

            def replace_between_stat_and_unlink(path, *args, **kwargs):
                nonlocal output_unlinks
                if path == output:
                    output_unlinks += 1
                    competitor.write_bytes(winner)
                    original_replace(competitor, output)
                return original_unlink(path, *args, **kwargs)

            with (
                patch.object(report.os, "fsync", side_effect=fail_directory_fsync),
                patch.object(
                    Path, "unlink", autospec=True, side_effect=replace_between_stat_and_unlink,
                ),
            ):
                with self.assertRaisesRegex(OSError, "directory fsync failed"):
                    report.write_summary_atomic(output, {"status": "complete", "value": 1})
            self.assertEqual(output_unlinks, 0)
            self.assertEqual(output.read_bytes(), published)
            self.assertEqual(list(output.parent.glob(".*.tmp")), [])


class StageThreeMatrixShardTest(unittest.TestCase):
    """验证正式 single-workload shard 可组装为 engine/layout 逻辑 target。"""

    def shards(self, directory, engine="opengauss", layout="same_table"):
        return [
            formal_shard(directory, workload, engine, layout)[0]
            for workload in PERFORMANCE_WORKLOADS + ("correctness_only",)
        ]

    def test_assembles_single_workload_shards_into_engine_layout_target(self):
        """四个 workload shard 精确覆盖一个逻辑 target，且与输入顺序无关。"""
        with tempfile.TemporaryDirectory() as directory:
            shards = self.shards(directory)
            summary = report.summarize(shards)
            self.assertEqual(
                summary["statistics_boundary"], "round-first-four-round-median"
            )
            self.assertEqual(len(summary["matrix"]), 1)
            target = summary["matrix"][0]
            self.assertEqual(
                (target["engine"], target["layout"]), ("opengauss", "same_table")
            )
            self.assertEqual(set(target["workloads"]), set(PERFORMANCE_WORKLOADS))
            for workload in PERFORMANCE_WORKLOADS:
                scenario = target["workloads"][workload]["scenarios"]["list:first"]
                self.assertEqual(scenario["round_count"], 4)
                self.assertEqual(
                    [item["round_index"] for item in scenario["rounds"]],
                    [0, 1, 2, 3],
                )
            self.assertEqual(summary, report.summarize(list(reversed(shards))))

    def test_validates_samples_within_single_shard_workload_scope(self):
        """raw sample 验证只迭代 shard 实际声明的 workload。"""
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, samples = formal_shard(
                directory, "equal_total_few_large", layout="asset_ref"
            )
            try:
                grouped = report._validate_samples(manifest, samples)
            except KeyError as error:
                self.fail(f"single-workload sample validation escaped its scope: {error}")
            self.assertEqual(
                {workload for workload, _, _ in grouped},
                {"equal_total_few_large"},
            )

    def test_assembles_multiple_shard_targets_in_stable_order(self):
        """多组纯 shard 按 engine/layout 稳定排序，输入顺序不改变结果。"""
        with tempfile.TemporaryDirectory() as directory:
            opengauss = self.shards(
                Path(directory) / "opengauss", "opengauss", "asset_ref"
            )
            clickhouse = self.shards(
                Path(directory) / "clickhouse", "clickhouse", "separate"
            )
            runs = opengauss[1::2] + clickhouse[::2] + opengauss[::2] + clickhouse[1::2]
            summary = report.summarize(runs)
            self.assertEqual(
                [(item["engine"], item["layout"]) for item in summary["matrix"]],
                [("clickhouse", "separate"), ("opengauss", "asset_ref")],
            )
            self.assertEqual(summary, report.summarize(list(reversed(runs))))

    def test_mixes_aggregate_target_with_another_shard_target(self):
        """旧聚合 target 与另一 engine/layout 的四 shard 可共同汇总。"""
        with tempfile.TemporaryDirectory() as directory:
            aggregate = formal_target(
                Path(directory) / "aggregate", "opengauss", "same_table"
            )[0]
            shards = self.shards(
                Path(directory) / "shards", "clickhouse", "full_core"
            )
            runs = [shards[2], aggregate, shards[0], shards[3], shards[1]]
            summary = report.summarize(runs)
            self.assertEqual(
                [(item["engine"], item["layout"]) for item in summary["matrix"]],
                [("clickhouse", "full_core"), ("opengauss", "same_table")],
            )
            self.assertEqual(summary, report.summarize(list(reversed(runs))))

    def test_keeps_shard_workload_samples_separate(self):
        """组装按 workload 保留各自的四轮统计，不做跨 shard 池化。"""
        offsets = {
            "main": 100, "equal_total_few_large": 200, "equal_total_many_medium": 300,
        }
        with tempfile.TemporaryDirectory() as directory:
            shards = []
            for workload in PERFORMANCE_WORKLOADS + ("correctness_only",):
                target, _, samples = formal_shard(directory, workload)
                if workload in offsets:
                    for item in samples:
                        item["application_ready_ms"] = offsets[workload]
                    (target / "samples.jsonl").write_text(
                        "".join(json.dumps(item) + "\n" for item in samples)
                    )
                shards.append(target)
            workloads = report.summarize(shards)["matrix"][0]["workloads"]
            medians = {
                workload: workloads[workload]["scenarios"]["list:first"][
                    "round_statistic_median"
                ]["application_ready_ms"]["p50"]
                for workload in PERFORMANCE_WORKLOADS
            }
            self.assertEqual(
                medians, {name: float(value) for name, value in offsets.items()}
            )

    def test_rejects_incomplete_or_duplicated_shard_coverage(self):
        """缺少 workload 或同一 workload 出现两次都不得组装。"""
        with tempfile.TemporaryDirectory() as directory:
            shards = self.shards(directory)
            with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
                report.summarize(shards[:-1])
            with self.assertRaisesRegex(ValueError, "duplicate matrix shard"):
                report.summarize(shards + [shards[0]])

    def test_rejects_cross_shard_input_identity_and_code_drift(self):
        """同一 engine/layout 的 shard 必须共享正式 input 与 code 证据。"""
        with tempfile.TemporaryDirectory() as directory:
            shards = self.shards(directory)
            rewrite_manifest(shards[1], drift_code)
            with self.assertRaisesRegex(ValueError, "code evidence differs"):
                report.summarize(shards)
        with tempfile.TemporaryDirectory() as directory:
            shards = self.shards(directory)
            rewrite_manifest(shards[2], drift_input)
            with self.assertRaisesRegex(ValueError, "input identity differs"):
                report.summarize(shards)

    def test_gates_each_shard_workload_rounds_and_completion(self):
        """每个 shard 只对自己的 workload、round 数、rounds_complete 和计数负责。"""
        with tempfile.TemporaryDirectory() as directory:
            _, manifest, _ = formal_shard(directory, "main")
            manifest["workloads"]["main"]["status"] = "running"
            with self.assertRaisesRegex(ValueError, "workload is not complete"):
                report.validate_run(manifest)

            _, manifest, _ = formal_shard(Path(directory) / "rounds", "main")
            manifest["workloads"]["main"]["rounds"].pop()
            with self.assertRaisesRegex(ValueError, "must have 4 rounds"):
                report.validate_run(manifest)

            _, manifest, _ = formal_shard(Path(directory) / "main-rounds", "main")
            manifest["correctness"]["rounds_complete"] = 0
            with self.assertRaisesRegex(ValueError, "response-byte validation"):
                report.validate_run(manifest)

            _, manifest, _ = formal_shard(
                Path(directory) / "control-rounds", "equal_total_few_large"
            )
            manifest["correctness"]["rounds_complete"] = 4
            with self.assertRaisesRegex(ValueError, "response-byte validation"):
                report.validate_run(manifest)

            _, manifest, _ = formal_shard(Path(directory) / "samples", "main")
            manifest["correctness"]["successful_samples"] -= 1
            with self.assertRaisesRegex(ValueError, "response-byte validation"):
                report.validate_run(manifest)

    def test_rejects_shard_sample_count_that_contradicts_its_rounds(self):
        """shard 的原始样本数必须与自身 round 计数一致。"""
        with tempfile.TemporaryDirectory() as directory:
            shards = self.shards(directory)
            main = next(path for path in shards if path.name.endswith("-main"))
            samples = (main / "samples.jsonl").read_text(encoding="utf-8").splitlines()
            (main / "samples.jsonl").write_text(
                "".join(line + "\n" for line in samples[:-1])
            )
            with self.assertRaisesRegex(ValueError, "sample evidence"):
                report.summarize(shards)


class StageThreePartStateSummaryTest(unittest.TestCase):
    """验证四布局 part-state 控制的跨运行身份、状态门禁与独立统计。"""

    def runs(self, directory):
        """写入四个布局各一个最小正式 part-state 目录。"""
        return [write_part_state_run(directory, layout) for layout in LAYOUTS]

    def test_summarizes_four_layout_controls_in_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            summary = report.summarize_part_states(runs)
            self.assertEqual(
                summary["format"], "agent-trace-json-storage-stage3-part-states-summary"
            )
            self.assertEqual(summary["format_version"], 1)
            self.assertEqual(summary["statistics_boundary"], "raw-query-sample-distribution")
            self.assertEqual(summary["state_order"], list(PART_STATE_STATES))
            self.assertEqual(summary["scenarios"], list(PART_STATE_SCENARIOS))
            self.assertEqual(summary["input"], INPUT_IDENTITY)
            self.assertEqual([item["layout"] for item in summary["part_states"]], list(LAYOUTS))
            for target in summary["part_states"]:
                layout = target["layout"]
                self.assertEqual(target["physical_targets"], list(LAYOUT_TARGETS[layout]))
                self.assertEqual(target["controlled_table"], PART_STATE_CONTROLLED[layout])
                self.assertEqual(target["samples_per_query"], 30)
                self.assertEqual(
                    target["cache_limits"],
                    "ordered-control-warm-cache-no-os-cache-drop-not-main-matrix-paired",
                )
                self.assertEqual(target["runtime"]["operation"], "part-states")
                self.assertEqual(target["runtime"]["layout"], layout)
                self.assertEqual(
                    [state["name"] for state in target["states"]], list(PART_STATE_STATES)
                )
                for state in target["states"]:
                    self.assertEqual(list(state["scenarios"]), list(PART_STATE_SCENARIOS))
            self.assertEqual(summary, report.summarize_part_states(list(reversed(runs))))

    def test_rejects_missing_or_duplicated_layout(self):
        with self.assertRaisesRegex(ValueError, "at least one part-state run directory"):
            report.summarize_part_states([])
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
                report.summarize_part_states(runs[:-1])
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            runs.append(write_part_state_run(Path(directory) / "duplicate", "same_table"))
            with self.assertRaisesRegex(ValueError, "duplicate part-state layout"):
                report.summarize_part_states(runs)

    def test_rejects_formal_input_truth_and_query_catalog_drift(self):
        for case, message in (
            ("input", "input identity differs between runs"),
            ("truth", "truth identity is invalid"),
            ("query", "query catalog identity differs between runs"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                if case == "input":
                    rewrite_part_state_envelope(
                        runs[1],
                        lambda envelope: envelope["input"]["events"].__setitem__("sha256", "9" * 64),
                    )
                elif case == "truth":
                    rewrite_part_state_envelope(
                        runs[2], lambda envelope: envelope["truth"].__setitem__("record_count", 1)
                    )
                else:
                    rewrite_part_state_envelope(
                        runs[3],
                        lambda envelope: envelope.__setitem__("query_catalog_sha256", "8" * 64),
                    )
                with self.assertRaisesRegex(ValueError, message):
                    report.summarize_part_states(runs)

    def test_keeps_per_layout_code_provenance_and_accepts_divergent_code(self):
        """asset_ref 修复后的 runner 摘要与前三布局不同，结果树保留各自 code。"""
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_envelope(
                runs[3],
                lambda envelope: envelope["code"]["part_state_runner"].__setitem__(
                    "sha256", "9" * 64
                ),
            )
            summary = report.summarize_part_states(runs)
            codes = {item["layout"]: item["code"] for item in summary["part_states"]}
            self.assertEqual(codes["asset_ref"]["part_state_runner"]["sha256"], "9" * 64)
            self.assertEqual(codes["same_table"]["part_state_runner"]["sha256"], "5" * 64)

    def test_rejects_cross_layout_runtime_version_and_image_drift(self):
        """四布局必须来自同一 ClickHouse 版本和容器镜像 identity。"""
        for field, value in (("version", "25.12.12.1"), ("image_id", "sha256:" + "9" * 64)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                if field == "version":
                    rewrite_part_state_envelope(
                        runs[1],
                        lambda envelope: envelope["runtime"]["engine_runtime"].__setitem__(
                            field, value
                        ),
                    )
                else:
                    rewrite_part_state_envelope(
                        runs[2],
                        lambda envelope: envelope["runtime"]["container"].__setitem__(
                            field, value
                        ),
                    )
                with self.assertRaisesRegex(ValueError, "runtime identity differs between runs"):
                    report.summarize_part_states(runs)

    def test_rejects_non_formal_truth_dimensions_even_when_all_runs_match(self):
        """跨布局一致的缩减 truth 仍不得冒充固定正式输入。"""
        cases = (
            ("seed", 20260908),
            ("record_count", 1),
            ("block_size", 128),
            ("block_count", 1),
        )
        for field, value in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                for run in runs:
                    rewrite_part_state_envelope(
                        run,
                        lambda envelope, field=field, value=value: envelope["truth"].__setitem__(
                            field, value
                        ),
                    )
                with self.assertRaisesRegex(ValueError, "truth identity is invalid"):
                    report.summarize_part_states(runs)

    def test_rejects_envelope_identity_runtime_and_code_gaps(self):
        cases = (
            (lambda envelope: envelope.__setitem__("format", "other"), "format/version is invalid"),
            (lambda envelope: envelope.__setitem__("format_version", 2), "format/version is invalid"),
            (lambda envelope: envelope.__setitem__("status", "running"), "status is not complete"),
            (lambda envelope: envelope.__setitem__("operation", "candidate"), "operation is invalid"),
            (lambda envelope: envelope.__setitem__("run_id", ""), "run identity is missing"),
            (
                lambda envelope: envelope["runtime"].__setitem__("engine", "opengauss"),
                "runtime identity mismatch",
            ),
            (
                lambda envelope: envelope["runtime"].__setitem__("layout", "bogus"),
                "runtime identity mismatch",
            ),
            (
                lambda envelope: envelope["runtime"].__setitem__("operation", "candidate"),
                "runtime identity mismatch",
            ),
            (
                lambda envelope: envelope["runtime"].__setitem__("container", {}),
                "runtime evidence is invalid",
            ),
            (
                lambda envelope: envelope["runtime"]["engine_runtime"].__setitem__(
                    "source", "static"
                ),
                "runtime evidence is invalid",
            ),
            (
                lambda envelope: envelope["runtime"]["host"].__setitem__("cpu_count", True),
                "runtime evidence is invalid",
            ),
            (lambda envelope: envelope.pop("namespace_policy"), "namespace evidence is missing"),
            (lambda envelope: envelope["code"].pop("part_state_runner"), "code evidence is incomplete"),
            (
                lambda envelope: envelope["code"]["common"].__setitem__("sha256", "x" * 64),
                "code evidence is incomplete",
            ),
            (
                lambda envelope: envelope["code"]["assets"].__setitem__("bytes", 0),
                "code evidence is incomplete",
            ),
            (
                lambda envelope: envelope["input"].__setitem__("kind", "smoke"),
                "truth/input identity is missing",
            ),
            (lambda envelope: envelope["truth"].pop("block_count"), "truth identity is invalid"),
            (
                lambda envelope: envelope["truth"].__setitem__("identity_sha256", "7" * 64),
                "truth identity is invalid",
            ),
            (
                lambda envelope: envelope.__setitem__("query_catalog_sha256", "not-a-digest"),
                "query catalog identity is invalid",
            ),
            (
                lambda envelope: envelope["cleanup"].__setitem__("removed", False),
                "cleanup evidence is missing",
            ),
            (
                lambda envelope: envelope["cleanup"].__setitem__("asset_directory_removed", False),
                "cleanup evidence is missing",
            ),
            (
                lambda envelope: envelope["cleanup"].__setitem__(
                    "asset_directory_applicable", True
                ),
                "cleanup evidence is missing",
            ),
        )
        for index, (mutate, message) in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_envelope(runs[0], mutate)
                with self.assertRaisesRegex(ValueError, message):
                    report.summarize_part_states(runs)

    def test_rejects_child_identity_bytes_and_path_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_envelope(
                runs[0], lambda envelope: envelope["child"].__setitem__("sha256", "9" * 64)
            )
            with self.assertRaisesRegex(ValueError, "child identity mismatch"):
                report.summarize_part_states(runs)
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            path = runs[1] / "child" / "run-manifest.json"
            child = json.loads(path.read_text(encoding="utf-8"))
            child["run_id"] = "jsons3-clickhouse-parts-replaced"
            path.write_text(json.dumps(child), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "child identity mismatch"):
                report.summarize_part_states(runs)
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_envelope(
                runs[2],
                lambda envelope: envelope["child"].__setitem__(
                    "path", "../escape/run-manifest.json"
                ),
            )
            with self.assertRaisesRegex(ValueError, "child path escapes the run directory"):
                report.summarize_part_states(runs)
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            (runs[3] / "child" / "run-manifest.json").unlink()
            with self.assertRaisesRegex(ValueError, "child manifest is unavailable"):
                report.summarize_part_states(runs)
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_envelope(
                runs[0], lambda envelope: envelope["child"].__setitem__("status", "failed")
            )
            with self.assertRaisesRegex(ValueError, "child format/status mismatch"):
                report.summarize_part_states(runs)
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_envelope(runs[1], lambda envelope: envelope["child"].pop("bytes"))
            with self.assertRaisesRegex(ValueError, "child evidence is incomplete"):
                report.summarize_part_states(runs)


def _part_state_mutate_state(child, name):
    """取得 child 中指定名称的状态，供反例直接改写。"""
    return next(state for state in child["states"] if state["name"] == name)


def _mutate_first_sample(child, name, scenario, mutate):
    """改写一个状态内首个匹配 scenario 的样本。"""
    state = _part_state_mutate_state(child, name)
    sample = next(item for item in state["query_samples"] if item["scenario"] == scenario)
    mutate(sample)


def _mutate_named_samples(child, name, scenario, mutate):
    """改写一个状态内某个 scenario 的全部样本。"""
    state = _part_state_mutate_state(child, name)
    for sample in state["query_samples"]:
        if sample["scenario"] == scenario:
            mutate(sample)


class StageThreePartStateGateTest(unittest.TestCase):
    """验证 part-state child、scenario 与访问证据的反例全部 fail closed。"""

    def runs(self, directory):
        return [write_part_state_run(directory, layout) for layout in LAYOUTS]

    def test_rejects_child_physical_state_gaps(self):
        cases = (
            (
                lambda child: child.__setitem__("cache_limits", "warm-cache"),
                "cache limits mismatch",
            ),
            (lambda child: child["state_order"].reverse(), "state order mismatch"),
            (lambda child: child.__setitem__("samples_per_query", 29), "samples_per_query mismatch"),
            (lambda child: child.__setitem__("physical_targets", []), "physical targets mismatch"),
            (lambda child: child.__setitem__("controlled_table", "other"), "controlled table mismatch"),
            (lambda child: child.__setitem__("layout", "separate"), "format/status/layout mismatch"),
            (lambda child: child.__setitem__("errors", ["boom"]), "child contains errors"),
            (lambda child: child.__setitem__("states", child["states"][:3]), "states are incomplete"),
            (
                lambda child: _part_state_mutate_state(child, "stable").__setitem__(
                    "predicate_proven", False
                ),
                "predicate evidence is inconsistent",
            ),
            (
                lambda child: _part_state_mutate_state(child, "stable").__setitem__("error", "timeout"),
                "predicate evidence is inconsistent",
            ),
            (
                lambda child: _part_state_mutate_state(child, "fragmented").__setitem__(
                    "active_merges", [{"table": child["controlled_table"]}]
                ),
                "predicate evidence is inconsistent",
            ),
            (
                lambda child: _part_state_mutate_state(child, "merging").__setitem__(
                    "active_merges", [{"table": "other"}]
                ),
                "predicate evidence is inconsistent",
            ),
            (
                lambda child: _part_state_mutate_state(child, "stable")["observations"][-1][
                    "active_part_counts"
                ].__setitem__(child["controlled_table"], 999),
                "predicate evidence is inconsistent",
            ),
            (
                lambda child: _part_state_mutate_state(child, "single_part")["tables"][
                    child["controlled_table"]
                ].__setitem__("part_count", 2),
                "predicate evidence is inconsistent",
            ),
            (
                lambda child: child["restoration"].__setitem__("restored", False),
                "restoration evidence is invalid",
            ),
            (
                lambda child: child["restoration"].__setitem__("attempted", False),
                "restoration evidence is invalid",
            ),
            (
                lambda child: child["restoration"].__setitem__("targets", []),
                "restoration evidence is invalid",
            ),
            (
                lambda child: child["cleanup"].__setitem__("removed", False),
                "child cleanup evidence is invalid",
            ),
            (
                lambda child: child["cleanup"].__setitem__("namespace", ""),
                "child cleanup evidence is invalid",
            ),
            (
                lambda child: child["cleanup"].__setitem__("namespace", "other-namespace"),
                "child cleanup evidence is invalid",
            ),
        )
        for index, (mutate, message) in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(runs[3], mutate)
                with self.assertRaisesRegex(ValueError, message):
                    report.summarize_part_states(runs)

    def test_rejects_table_merge_and_observation_type_gaps(self):
        def first_table(state):
            return next(iter(state["tables"].values()))

        cases = (
            (
                lambda state: state["tables"].pop(next(iter(state["tables"]))),
                "table evidence is incomplete",
            ),
            (
                lambda state: first_table(state).__setitem__("part_count", True),
                "table metrics are invalid",
            ),
            (
                lambda state: first_table(state).__setitem__("marks", -1),
                "table metrics are invalid",
            ),
            (
                lambda state: first_table(state).__setitem__("compressed_bytes", 1.5),
                "table metrics are invalid",
            ),
            (lambda state: state.__setitem__("active_merges", {}), "merge evidence is invalid"),
            (
                lambda state: state.__setitem__("active_merges", [{"num_parts": 1}]),
                "merge evidence is invalid",
            ),
            (lambda state: state.__setitem__("observations", []), "observations evidence is invalid"),
            (
                lambda state: state.__setitem__("observations", [{"active_merges": []}]),
                "observations evidence is invalid",
            ),
            (
                lambda state: state["observations"][0].__setitem__(
                    "active_part_counts", {"unknown": 1}
                ),
                "observations evidence is invalid",
            ),
            (
                lambda state: state.__setitem__("optimized_targets", ["events"]),
                "optimized targets mismatch",
            ),
        )
        for index, (mutate, message) in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[0],
                    lambda child: mutate(_part_state_mutate_state(child, "fragmented")),
                )
                with self.assertRaisesRegex(ValueError, message):
                    report.summarize_part_states(runs)

    def test_rejects_scenario_and_sample_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)

            def drop_scenario(child):
                state = _part_state_mutate_state(child, "fragmented")
                state["query_samples"] = [
                    item for item in state["query_samples"] if item["scenario"] != "batch:main"
                ]

            rewrite_part_state_child(runs[0], drop_scenario)
            with self.assertRaisesRegex(ValueError, "query sample count mismatch"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[1],
                lambda child: _mutate_named_samples(
                    child, "stable", "trace:p95",
                    lambda sample: sample.__setitem__("scenario", "trace:p50"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "scenario coverage is incomplete"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[2],
                lambda child: _mutate_named_samples(
                    child, "merging", "list:first",
                    lambda sample: sample.__setitem__("scenario", "preview:first"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "scenario/kind evidence is inconsistent"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[3],
                lambda child: _mutate_named_samples(
                    child, "single_part", "list:middle",
                    lambda sample: sample.__setitem__("scenario", "bogus:scenario"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "scenario evidence is invalid"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)

            def duplicate_query_id(child):
                state = _part_state_mutate_state(child, "fragmented")
                state["query_samples"][1]["query_id"] = state["query_samples"][0]["query_id"]

            rewrite_part_state_child(runs[0], duplicate_query_id)
            with self.assertRaisesRegex(ValueError, "query IDs are duplicated"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[0],
                lambda child: _mutate_first_sample(
                    child, "fragmented", "list:first",
                    lambda sample: sample.__setitem__("query_id", ""),
                ),
            )
            with self.assertRaisesRegex(ValueError, "query ID evidence is invalid"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[1],
                lambda child: _mutate_first_sample(
                    child, "stable", "preview:middle",
                    lambda sample: sample.__setitem__("status", "failed"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "failed sample is present"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[2],
                lambda child: _mutate_first_sample(
                    child, "merging", "trace:p50",
                    lambda sample: sample.__setitem__("error", "connection reset"),
                ),
            )
            with self.assertRaisesRegex(ValueError, "failed sample is present"):
                report.summarize_part_states(runs)


class StageThreePartStateSampleGateTest(unittest.TestCase):
    """验证样本计数、数值类型、访问证据与 Asset 水位的 fail-closed 边界。"""

    def runs(self, directory):
        return [write_part_state_run(directory, layout) for layout in LAYOUTS]

    def test_rejects_sample_totals(self):
        cases = (
            ("successful_samples", 359),
            ("failed_samples", 1),
            ("query_finish_count", 1),
        )
        for field, value in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[0],
                    lambda child: _part_state_mutate_state(child, "stable").__setitem__(field, value),
                )
                with self.assertRaisesRegex(ValueError, "sample totals are invalid"):
                    report.summarize_part_states(runs)

    def test_rejects_non_negative_numeric_gates(self):
        cases = (
            (lambda sample: sample.__setitem__("recovery_ms", -1), "recovery_ms is invalid"),
            (
                lambda sample: sample.__setitem__("validation_ms", float("nan")),
                "child manifest",
            ),
            (
                lambda sample: sample.__setitem__("application_ready_ms", True),
                "application_ready_ms is invalid",
            ),
            (lambda sample: sample.__setitem__("response_bytes", 0), "response bytes are invalid"),
            (lambda sample: sample.__setitem__("response_bytes", 150), "response bytes are invalid"),
            (lambda sample: sample.__setitem__("request_count", 1.5), "request count is invalid"),
            (
                lambda sample: sample.__setitem__(
                    "validation", {"row_count": 1.5, "validated_payload_bytes": 1}
                ),
                "validation evidence is invalid",
            ),
            (lambda sample: sample.__setitem__("validation", None), "validation evidence is invalid"),
            (
                lambda sample: sample.__setitem__("database_protocol_bytes", 1.5),
                "response bytes are invalid",
            ),
            (
                lambda sample: sample.__setitem__("resolver_requests", True),
                "resolver_requests is invalid",
            ),
            (
                lambda sample: sample.__setitem__("resolver_requests", -1),
                "resolver_requests is invalid",
            ),
            (
                lambda sample: sample.__setitem__("resolver_read_ms", float("inf")),
                "child manifest",
            ),
            (
                lambda sample: sample.__setitem__("resolver_read_ms", -0.1),
                "resolver_read_ms is invalid",
            ),
        )
        for index, (mutate, message) in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[0],
                    lambda child: _mutate_first_sample(child, "fragmented", "batch:main", mutate),
                )
                with self.assertRaisesRegex(ValueError, message):
                    report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[1],
                lambda child: _mutate_first_sample(
                    child, "stable", "batch:main",
                    lambda sample: sample.__setitem__("application_ready_ms", 0),
                ),
            )
            with self.assertRaisesRegex(ValueError, "batch application_ready_ms must be positive"):
                report.summarize_part_states(runs)

    def test_rejects_access_evidence_gaps_and_inconsistencies(self):
        def mutate_access(child, name, scenario, mutate):
            state = _part_state_mutate_state(child, name)
            sample = next(item for item in state["query_samples"] if item["scenario"] == scenario)
            mutate(state, sample)

        cases = (
            (
                lambda state, sample: state["query_plans"].pop(sample["query_id"]),
                "access evidence is incomplete",
            ),
            (
                lambda state, sample: state["query_details"].pop(sample["query_id"]),
                "access evidence is incomplete",
            ),
            (
                lambda state, sample: state["query_finish"].pop(sample["query_id"]),
                "access evidence is incomplete",
            ),
            (
                lambda state, sample: state["query_plans"].__setitem__(sample["query_id"], "   "),
                "query plan is invalid",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "declared_source", ""
                ),
                "query detail evidence is invalid",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "statement", " "
                ),
                "query detail evidence is invalid",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "scanned_rows", -1
                ),
                "query detail evidence is invalid",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "scanned_bytes", True
                ),
                "query detail evidence is invalid",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "kind", "preview"
                ),
                "query evidence is inconsistent",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "scanned_rows", 3
                ),
                "query evidence is inconsistent",
            ),
            (
                lambda state, sample: state["query_details"][sample["query_id"]].__setitem__(
                    "scanned_bytes", 3
                ),
                "query evidence is inconsistent",
            ),
            (
                lambda state, sample: state["query_finish"][sample["query_id"]].__setitem__(
                    "read_rows", 0
                ),
                "query evidence is inconsistent",
            ),
            (
                lambda state, sample: state["query_finish"][sample["query_id"]].__setitem__(
                    "exception_code", 1
                ),
                "QueryFinish evidence is invalid",
            ),
            (
                lambda state, sample: state["query_finish"][sample["query_id"]].__setitem__(
                    "type", "QueryStart"
                ),
                "QueryFinish evidence is invalid",
            ),
            (
                lambda state, sample: state["query_finish"][sample["query_id"]].__setitem__(
                    "read_bytes", 1.5
                ),
                "QueryFinish evidence is invalid",
            ),
        )
        for index, (mutate, message) in enumerate(cases):
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[3],
                    lambda child: mutate_access(
                        child, "single_part", "detail:text_2m", mutate
                    ),
                )
                with self.assertRaisesRegex(ValueError, message):
                    report.summarize_part_states(runs)

    def test_rejects_asset_store_gaps_and_watermark_drift(self):
        for field in (
            "available_object_count", "available_bytes", "orphan_object_count", "orphan_bytes",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[3],
                    lambda child: _part_state_mutate_state(child, "fragmented")[
                        "asset_store"
                    ].pop(field),
                )
                with self.assertRaisesRegex(ValueError, "asset store evidence is invalid"):
                    report.summarize_part_states(runs)

        for field, value in (("orphan_object_count", 1), ("orphan_bytes", 5)):
            with self.subTest(orphan=field), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[3],
                    lambda child: _part_state_mutate_state(child, "merging")[
                        "asset_store"
                    ].__setitem__(field, value),
                )
                with self.assertRaisesRegex(ValueError, "asset store evidence is invalid"):
                    report.summarize_part_states(runs)

        for field, value in (("available_object_count", True), ("available_bytes", -1)):
            with self.subTest(type=field), tempfile.TemporaryDirectory() as directory:
                runs = self.runs(directory)
                rewrite_part_state_child(
                    runs[3],
                    lambda child: _part_state_mutate_state(child, "stable")[
                        "asset_store"
                    ].__setitem__(field, value),
                )
                with self.assertRaisesRegex(ValueError, "asset store evidence is invalid"):
                    report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[3],
                lambda child: _part_state_mutate_state(child, "stable").__setitem__(
                    "asset_store", None
                ),
            )
            with self.assertRaisesRegex(ValueError, "asset store evidence is invalid"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[3],
                lambda child: _part_state_mutate_state(child, "single_part")[
                    "asset_store"
                ].__setitem__("available_object_count", 159),
            )
            with self.assertRaisesRegex(ValueError, "asset store watermarks differ between states"):
                report.summarize_part_states(runs)

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[0],
                lambda child: _part_state_mutate_state(child, "fragmented").__setitem__(
                    "asset_store",
                    {
                        "available_object_count": 1, "available_bytes": 1,
                        "orphan_object_count": 0, "orphan_bytes": 0,
                    },
                ),
            )
            with self.assertRaisesRegex(ValueError, "asset store evidence is invalid"):
                report.summarize_part_states(runs)


class StageThreePartStateResultTest(unittest.TestCase):
    """验证控制结果直接来自 raw samples、保留机制证据且不含矩阵配对字段。"""

    def runs(self, directory):
        return [write_part_state_run(directory, layout) for layout in LAYOUTS]

    def test_recomputes_raw_distributions_without_mixing_state_or_scenario(self):
        def configure(child):
            state = _part_state_mutate_state(child, "stable")
            samples = [item for item in state["query_samples"] if item["scenario"] == "trace:p95"]
            for sample in samples:
                sample["application_ready_ms"] = 1000
                sample["database_response_bytes"] = 1000
                sample["resolver_payload_bytes"] = 500
                sample["response_bytes"] = 1500
                sample["request_count"] = 2
                sample["resolver_requests"] = 4
                sample["resolver_read_ms"] = 8.0
                sample["validation"]["validated_payload_bytes"] = 2048
            samples[0]["application_ready_ms"] = 2000
            samples[0]["resolver_requests"] = 10
            samples[0]["resolver_read_ms"] = 20.0

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_part_state_child(
                runs[0],
                lambda child: _mutate_named_samples(
                    child, "fragmented", "list:first",
                    lambda sample: sample.__setitem__("application_ready_ms", 7),
                ),
            )
            rewrite_part_state_child(runs[0], configure)
            summary = report.summarize_part_states(runs)
            target = next(item for item in summary["part_states"] if item["layout"] == "same_table")
            states = {state["name"]: state for state in target["states"]}
            stable = states["stable"]["scenarios"]["trace:p95"]
            self.assertEqual(stable["application_ready_ms"], {
                "minimum": 1000.0, "p50": 1000.0, "p95": 1000.0, "maximum": 2000.0,
            })
            self.assertEqual(stable["response_bytes"], {
                "database": 30000, "resolver_payload": 15000, "total": 45000,
                "validated_payload": 61440, "database_protocol": 3000, "request_count": 60,
            })
            self.assertEqual(stable["resolver_requests"], {
                "minimum": 4, "p50": 4.0, "p95": 4.0, "maximum": 10,
            })
            self.assertEqual(stable["resolver_read_ms"], {
                "minimum": 8.0, "p50": 8.0, "p95": 8.0, "maximum": 20.0,
            })
            self.assertEqual(
                states["stable"]["scenarios"]["list:first"]["application_ready_ms"]["p50"], 13.0
            )
            self.assertEqual(
                states["fragmented"]["scenarios"]["list:first"]["application_ready_ms"]["p50"], 7.0
            )
            self.assertEqual(
                states["stable"]["scenarios"]["list:first"]["response_bytes"]["total"], 3000
            )
            self.assertIn("throughput_mib_s", states["stable"]["scenarios"]["batch:main"])
            self.assertNotIn("throughput_mib_s", stable)
            separate = next(item for item in summary["part_states"] if item["layout"] == "separate")
            other = {state["name"]: state for state in separate["states"]}
            self.assertEqual(
                other["stable"]["scenarios"]["trace:p95"]["application_ready_ms"]["p50"], 13.0
            )

    def test_result_tree_keeps_state_mechanism_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = report.summarize_part_states(self.runs(directory))
            asset_ref = next(
                item for item in summary["part_states"] if item["layout"] == "asset_ref"
            )
            states = {state["name"]: state for state in asset_ref["states"]}
            self.assertEqual(
                states["fragmented"]["tables"]["events_analytics"]["part_count"], 190
            )
            self.assertEqual(states["stable"]["tables"]["events_analytics"]["part_count"], 3)
            self.assertEqual(states["merging"]["active_merge_count"], 1)
            self.assertEqual(states["fragmented"]["active_merge_count"], 0)
            self.assertEqual(
                states["single_part"]["optimized_targets"], list(LAYOUT_TARGETS["asset_ref"])
            )
            self.assertEqual(states["stable"]["asset_store"]["available_object_count"], 160)
            self.assertEqual(asset_ref["controlled_table"], "events_analytics")
            same_table = next(
                item for item in summary["part_states"] if item["layout"] == "same_table"
            )
            self.assertEqual(same_table["states"][0]["tables"]["events"]["part_count"], 190)
            self.assertIsNone(same_table["states"][0]["asset_store"])

    def test_summary_contains_no_main_matrix_pairing_fields(self):
        forbidden = {
            "rounds", "round_index", "round_statistic_median", "main_matrix", "delta",
            "paired", "pairing", "main_pair", "main_vs_part", "difference",
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = report.summarize_part_states(self.runs(directory))

        def collect(value):
            found = set()
            if isinstance(value, dict):
                for key, item in value.items():
                    found.add(key)
                    found |= collect(item)
            elif isinstance(value, list):
                for item in value:
                    found |= collect(item)
            return found

        self.assertEqual(collect(summary) & forbidden, set())
        self.assertEqual(summary["statistics_boundary"], "raw-query-sample-distribution")


if __name__ == "__main__":
    unittest.main()
