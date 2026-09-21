import contextlib
import hashlib
import io
import json
import math
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


def write_evidence(round_index, engine, layout):
    """构造一轮写入完成证据，各项随 round 变化以便检验四轮中位数。"""
    scale = round_index + 1
    return {
        "final_watermark": 10,
        "wall_ms": 10.0 * scale,
        "rows_per_second": 100.0 * scale,
        "raw_payload_mib_per_second": 2.0 * scale,
        "asset_publish_ms": 5.0 * scale if layout == "asset_ref" else 0.0,
        "asset_raw_object_bytes": 1024 * scale if layout == "asset_ref" else 0,
        "block_count": 190,
        "database_ingest_request_body_bytes_total": (
            1000 * scale if engine == "clickhouse" else "unavailable"
        ),
        "block_wall_ms": {
            "minimum": 1.0 * scale, "median": 2.0 * scale,
            "p95": 3.0 * scale, "maximum": 4.0 * scale,
        },
    }


def storage_evidence(round_index, engine, layout):
    """构造一轮分表空间证据，各表取值不同以便检验分表口径。"""
    scale = round_index + 1
    tables = {}
    for index, name in enumerate(LAYOUT_TARGETS[layout]):
        factor = index + 1
        tables[name] = (
            {
                "part_count": 2 * scale, "rows": 10 * factor, "marks": 3 * scale,
                "compressed_bytes": 100 * scale * factor,
                "uncompressed_bytes": 200 * scale * factor,
                "columns": {},
            }
            if engine == "clickhouse"
            else {
                "heap_bytes": 40 * scale * factor, "index_bytes": 20 * scale * factor,
                "toast_bytes": 40 * scale * factor, "total_bytes": 100 * scale * factor,
            }
        )
    return {
        "tables": tables,
        "asset_store": (
            {
                "available_bytes": 4096 * scale, "available_object_count": 160,
                "orphan_bytes": 0, "orphan_object_count": 0,
            }
            if layout == "asset_ref"
            else None
        ),
        "merges": [],
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
                "write": write_evidence(round_index, engine, layout),
                "maintenance": {
                    "completed": True,
                    "watermarks": {name: 10 for name in LAYOUT_TARGETS[layout]},
                    "natural_stable_parts": engine == "clickhouse",
                    "optimize_final": False,
                },
                "storage": storage_evidence(round_index, engine, layout),
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

    def test_publishes_write_and_space_round_statistic_median(self):
        """写入与空间证据按 round 取中位数发布，不 pool 四个 round。"""
        with tempfile.TemporaryDirectory() as directory:
            target, _, _ = formal_target(Path(directory) / "asset", "clickhouse", "asset_ref")
            workload = report.summarize([target])["matrix"][0]["workloads"]["main"]
            self.assertEqual(workload["write"]["round_count"], 4)
            write = workload["write"]["round_statistic_median"]
            self.assertEqual(write["wall_ms"], 25.0)
            self.assertEqual(write["rows_per_second"], 250.0)
            self.assertEqual(write["raw_payload_mib_per_second"], 5.0)
            self.assertEqual(write["asset_publish_ms"], 12.5)
            self.assertEqual(write["asset_raw_object_bytes"], 2560.0)
            self.assertEqual(write["block_count"], 190)
            self.assertEqual(write["final_watermark"], 10)
            self.assertEqual(write["database_ingest_request_body_bytes_total"], 2500.0)
            self.assertEqual(
                write["block_wall_ms"],
                {"minimum": 2.5, "median": 5.0, "p95": 7.5, "maximum": 10.0},
            )
            self.assertEqual(workload["storage"]["round_count"], 4)
            storage = workload["storage"]["round_statistic_median"]
            self.assertEqual(storage["tables"]["events_analytics"]["compressed_bytes"], 250.0)
            self.assertEqual(storage["tables"]["assets"]["compressed_bytes"], 500.0)
            self.assertEqual(storage["tables"]["events_analytics"]["part_count"], 5.0)
            self.assertEqual(storage["asset_store"], {
                "available_bytes": 10240.0, "available_object_count": 160,
                "orphan_bytes": 0, "orphan_object_count": 0,
            })

    def test_keeps_engine_space_fields_and_null_asset_store_faithful(self):
        """两引擎空间字段各自保留，非 asset_ref layout 的对象存储发布为 null。"""
        with tempfile.TemporaryDirectory() as directory:
            targets = [
                formal_target(Path(directory) / engine, engine, "full_core")[0]
                for engine in ("clickhouse", "opengauss")
            ]
            summary = report.summarize(targets)
            results = {item["engine"]: item["workloads"]["main"] for item in summary["matrix"]}
            clickhouse = results["clickhouse"]["storage"]["round_statistic_median"]
            opengauss = results["opengauss"]["storage"]["round_statistic_median"]
            self.assertEqual(set(clickhouse["tables"]), {"events_full", "events_core"})
            self.assertEqual(
                set(clickhouse["tables"]["events_full"]),
                {"compressed_bytes", "uncompressed_bytes", "part_count", "marks", "rows"},
            )
            self.assertEqual(
                set(opengauss["tables"]["events_full"]),
                {"total_bytes", "heap_bytes", "index_bytes", "toast_bytes"},
            )
            # 分表口径：两张表的空间不得被合并成单一 target 数值。
            self.assertEqual(opengauss["tables"]["events_full"]["total_bytes"], 250.0)
            self.assertEqual(opengauss["tables"]["events_core"]["total_bytes"], 500.0)
            self.assertIsNone(clickhouse["asset_store"])
            self.assertIsNone(opengauss["asset_store"])
            self.assertEqual(
                results["opengauss"]["write"]["round_statistic_median"][
                    "database_ingest_request_body_bytes_total"
                ],
                "unavailable",
            )

    def test_rejects_write_and_space_evidence_gaps(self):
        """缺项、类型错误或与 layout 矛盾的写入/空间证据必须整轮失败。"""
        cases = {
            "missing_wall": ("write wall_ms is invalid", lambda record: record["write"].pop("wall_ms")),
            "bool_block_count": (
                "write block_count is invalid",
                lambda record: record["write"].__setitem__("block_count", True),
            ),
            "negative_rows_per_second": (
                "write rows_per_second is invalid",
                lambda record: record["write"].__setitem__("rows_per_second", -1.0),
            ),
            "text_rate": (
                "write raw_payload_mib_per_second is invalid",
                lambda record: record["write"].__setitem__("raw_payload_mib_per_second", "fast"),
            ),
            "missing_block_statistic": (
                "write block_wall_ms is invalid",
                lambda record: record["write"]["block_wall_ms"].pop("p95"),
            ),
            "null_client_bytes": (
                "client submitted bytes are invalid",
                lambda record: record["write"].__setitem__(
                    "database_ingest_request_body_bytes_total", None
                ),
            ),
            "missing_asset_store": (
                "asset store evidence is missing",
                lambda record: record["storage"].pop("asset_store"),
            ),
            "present_asset_store": (
                "asset store evidence must be absent",
                lambda record: record["storage"].__setitem__(
                    "asset_store",
                    {
                        "available_bytes": 0, "available_object_count": 0,
                        "orphan_bytes": 0, "orphan_object_count": 0,
                    },
                ),
            ),
        }
        asset_cases = {
            "null_asset_store": (
                "asset store evidence is missing",
                lambda record: record["storage"].__setitem__("asset_store", None),
            ),
            "bool_orphan_bytes": (
                "asset store orphan_bytes is invalid",
                lambda record: record["storage"]["asset_store"].__setitem__("orphan_bytes", True),
            ),
            "negative_object_count": (
                "asset store available_object_count is invalid",
                lambda record: record["storage"]["asset_store"].__setitem__(
                    "available_object_count", -1
                ),
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            for layout, group in (("same_table", cases), ("asset_ref", asset_cases)):
                for case, (message, mutate) in group.items():
                    target, _, _ = formal_target(
                        Path(directory) / case, "clickhouse", layout,
                    )
                    rewrite_manifest(
                        target,
                        lambda manifest: mutate(manifest["workloads"]["main"]["rounds"][2]),
                    )
                    with self.subTest(case=case), self.assertRaisesRegex(ValueError, message):
                        report.summarize([target])

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
            ("seed", 20260907.0),
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


# ---------------------------------------------------------------------------
# interference 控制 fixture
# ---------------------------------------------------------------------------

INTERFERENCE_PHASES = ("quiet", "detail_2m", "trace_long", "batch_loop", "continuous_ingest")
INTERFERENCE_SEGMENT_FILES = {"warmup": "warmup-samples.jsonl", "measurement": "samples.jsonl"}
INTERFERENCE_SEGMENT_SECONDS = {"warmup": 0.6, "measurement": 1.2}
INTERFERENCE_PHASE_STREAM = {
    "quiet": None,
    "detail_2m": "detail_2m",
    "trace_long": "trace_long",
    "batch_loop": "batch_loop",
    "continuous_ingest": "continuous_ingest",
}
INTERFERENCE_STREAM_RATES = {
    "list": 5.0, "preview": 5.0, "detail_2m": 5.0,
    "trace_long": 5.0, "batch_loop": None, "continuous_ingest": 5.0,
}
INTERFERENCE_STREAM_WORKERS = {
    "list": 2, "preview": 2, "detail_2m": 1,
    "trace_long": 1, "batch_loop": 1, "continuous_ingest": 1,
}
INTERFERENCE_QUERY_SCENARIOS = {
    "list": ("list", "list:first"),
    "preview": ("preview", "preview:first"),
    "detail_2m": ("detail", "detail:text_2m"),
    "trace_long": ("trace", "trace:p95"),
    "batch_loop": ("batch", "batch:main"),
}
INTERFERENCE_CONTINUOUS_ROWS = {"warmup": 2, "measurement": 4}
INTERFERENCE_ELIGIBLE_INDICES = tuple(range(108, 153))
INTERFERENCE_BLOCK_SIZE = 256
INTERFERENCE_FORMAL_SECONDS = {"warmup": 30.0, "measurement": 300.0}
INTERFERENCE_CODE_ROLES = (
    "assets", "common", "generator", "interference_runner",
    "layout_runner", "production", "run_stage3",
)
INTERFERENCE_ASSET_STORE = {
    "available_object_count": 160,
    "available_bytes": 128450560,
    "orphan_object_count": 0,
    "orphan_bytes": 0,
}


def interference_schedules():
    """按 runner 固定 factory 结构生成小规模 phase/segment schedule。"""
    schedules = {}
    for phase in INTERFERENCE_PHASES:
        streams = ["list", "preview"]
        if INTERFERENCE_PHASE_STREAM[phase] is not None:
            streams.append(INTERFERENCE_PHASE_STREAM[phase])
        schedules[phase] = {
            segment: {
                stream: {
                    "name": stream,
                    "rate_per_second": INTERFERENCE_STREAM_RATES[stream],
                    "duration_seconds": INTERFERENCE_SEGMENT_SECONDS[segment],
                    "workers": INTERFERENCE_STREAM_WORKERS[stream],
                    "timeout_seconds": 30.0,
                    "late_tolerance_seconds": 0.05,
                    "mode": "continuous" if stream == "batch_loop" else "fixed",
                }
                for stream in streams
            }
            for segment in ("warmup", "measurement")
        }
    return schedules


def interference_json_bytes(value):
    """按 runner 原子写 manifest 的字节形式序列化固定结构。"""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def interference_row(stream, sequence, layout, phase, segment, **overrides):
    """构造一条满足固定 schedule 与 stream 场景契约的 raw 请求行。"""
    watermark = (
        INTERFERENCE_ELIGIBLE_INDICES[sequence % len(INTERFERENCE_ELIGIBLE_INDICES)] + 1
    ) * INTERFERENCE_BLOCK_SIZE
    if stream == "continuous_ingest":
        sample = {
            "rows": INTERFERENCE_BLOCK_SIZE,
            "watermark": watermark,
            "watermarks": {table: watermark for table in LAYOUT_TARGETS[layout]},
            "wall_ms": 1.0,
            "write_target_ms": {},
            "asset_publish_ms": 0.0,
            "logical_target_row_bytes": {},
            "database_ingest_request_body_bytes": {},
            "asset_raw_object_bytes": 0,
        }
    else:
        kind, scenario = INTERFERENCE_QUERY_SCENARIOS[stream]
        sample = {
            "scenario": scenario,
            "kind": kind,
            "status": "success",
            "query_id": f"{layout}-{phase}-{segment}-{stream}-{sequence}",
            "query_complete_ms": 10.0,
            "recovery_ms": 11.0,
            "validation_ms": 12.0,
            "application_ready_ms": 100.0,
            "response_bytes": 100,
            "database_response_bytes": 100,
            "database_protocol_bytes": 100,
            "resolver_payload_bytes": 0,
            "resolver_requests": 0,
            "resolver_read_ms": 0.0,
            "request_count": 1,
            "error": None,
            "validation": {"row_count": 1, "validated_payload_bytes": 100},
        }
    offset = sequence / (INTERFERENCE_STREAM_RATES[stream] or 5.0)
    row = {
        "stream": stream,
        "sequence": sequence,
        "scheduled_offset_seconds": offset,
        "started_offset_seconds": offset,
        "completed_offset_seconds": offset + 0.1,
        "duration_ms": 100.0,
        "application_ready_ms": 100.0,
        "status": "success",
        "late_by_ms": 0.0,
        "sample": sample,
        "error": None,
    }
    row.update(overrides)
    if row["status"] == "dropped":
        row.update({
            "started_offset_seconds": None, "completed_offset_seconds": None,
            "duration_ms": None, "application_ready_ms": None, "sample": None,
        })
        if row["error"] is None:
            row["error"] = "scheduler dropped the request"
    elif row["status"] in {"failed", "timed_out"}:
        row["sample"] = None
        if row["error"] is None:
            row["error"] = "request failed"
        if row["completed_offset_seconds"] is None:
            row.update({"duration_ms": None, "application_ready_ms": None})
    return row


def interference_rows(layout, phase, segment, schedules):
    """按固定 schedule 生成一个 phase/segment 的全部 raw 行。"""
    records = {}
    for stream, schedule in schedules[phase][segment].items():
        count = (
            math.ceil(schedule["duration_seconds"] * schedule["rate_per_second"])
            if schedule["mode"] == "fixed" else INTERFERENCE_CONTINUOUS_ROWS[segment]
        )
        records[stream] = [
            interference_row(stream, sequence, layout, phase, segment)
            for sequence in range(count)
        ]
    return records


def interference_published(rows, schedule, wall_seconds):
    """按 runner 发布结构独立重算一个 stream 的 fixture 期望 summary。"""
    successful = [row["application_ready_ms"] for row in rows if row["status"] == "success"]
    publish_p99 = len(successful) >= 1000
    counts = {
        "scheduled_requests": len(rows),
        "started_requests": sum(row["started_offset_seconds"] is not None for row in rows),
        "completed_requests": sum(row["completed_offset_seconds"] is not None for row in rows),
        "successful_requests": sum(row["status"] == "success" for row in rows),
        "failed_requests": sum(row["status"] == "failed" for row in rows),
        "timed_out_requests": sum(row["status"] == "timed_out" for row in rows),
        "dropped_requests": sum(row["status"] == "dropped" for row in rows),
        "late_requests": sum(
            row["late_by_ms"] > schedule["late_tolerance_seconds"] * 1000 for row in rows
        ),
    }
    # fixture 使用常量时延，期望值可独立于百分位实现直接给出。
    return {
        "offered_rate_requests_s": schedule["rate_per_second"],
        "phase_wall_seconds": wall_seconds,
        **counts,
        "completed_throughput_requests_s": counts["successful_requests"] / wall_seconds,
        "latency_ms": {
            "minimum": min(successful) if successful else None,
            "p50": min(successful) if successful else None,
            "p95": min(successful) if successful else None,
            "p99": min(successful) if publish_p99 else None,
            "maximum": max(successful) if successful else None,
            "p99_status": (
                "publishable" if publish_p99 else "unavailable_insufficient_successes"
            ),
            "p99_minimum_successes": 1000,
        },
    }


def percentile_oracle(values, fraction):
    """以独立实现的线性插值百分位作为发布值的 oracle。"""
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    floor, ceiling = math.floor(position), math.ceil(position)
    if floor == ceiling:
        return ordered[floor]
    return ordered[floor] + (ordered[ceiling] - ordered[floor]) * (position - floor)


def interference_query_evidence(records, layout):
    """按全部成功 query 生成 plans/details/QueryFinish 证据。"""
    plans, details, finishes = {}, {}, {}
    for segment in ("warmup", "measurement"):
        for stream, rows in records[segment].items():
            if stream == "continuous_ingest":
                continue
            for row in rows:
                if row["status"] != "success":
                    continue
                sample = row["sample"]
                query_id = sample["query_id"]
                plans[query_id] = "observed scan"
                details[query_id] = {
                    "kind": sample["kind"], "statement": "SELECT 1",
                    "declared_source": LAYOUT_TARGETS[layout][0],
                    "scanned_rows": 2, "scanned_bytes": 2, "payload_selected": False,
                }
                finishes[query_id] = {
                    "type": "QueryFinish", "exception_code": 0,
                    "read_rows": 2, "read_bytes": 2, "query_duration_ms": 1,
                }
    return {
        "index_scans": {}, "plans": plans,
        "query_details": details, "query_finish": finishes,
    }


def interference_snapshots(layout):
    """构造顺序固定的三个资源/存储 snapshot。"""
    tables = {
        table: {
            "columns": {}, "part_count": 1, "rows": 48534, "marks": 1,
            "compressed_bytes": 0, "uncompressed_bytes": 0,
        }
        for table in LAYOUT_TARGETS[layout]
    }
    snapshot = {
        "captured_offset_seconds": 0.0,
        "resources": {
            "cpu": {"status": "available", "ticks": [1, 2, 3]},
            "memory": {"status": "available", "total_kib": 2, "available_kib": 1},
            "io": {
                "status": "available", "devices": 1,
                "read_sectors": 0, "written_sectors": 0,
            },
        },
        "storage": {
            "tables": tables, "merges": [],
            "asset_store": dict(INTERFERENCE_ASSET_STORE) if layout == "asset_ref" else None,
        },
        "active_part_backlog": 0,
        "active_merge_count": 0,
    }
    return [
        dict(snapshot, name=name, captured_offset_seconds=offset)
        for name, offset in zip(
            ("before_warmup", "before_measurement", "after_measurement"), (0.0, 10.0, 20.0)
        )
    ]


def interference_spec(layout, *, schedules=None, segment_seconds=None,
                      row_mutate=None, phase_mutate=None):
    """构造一个布局的 phase、raw、child 与 envelope 内存证据。"""
    schedules = interference_schedules() if schedules is None else schedules
    seconds = INTERFERENCE_SEGMENT_SECONDS if segment_seconds is None else segment_seconds
    child_run_id = f"jsons3-interference-fixture-{layout}"
    wall = {segment: seconds[segment] + 0.001 for segment in ("warmup", "measurement")}
    phases, records = [], {}
    for phase in INTERFERENCE_PHASES:
        rows = {
            segment: interference_rows(layout, phase, segment, schedules)
            for segment in ("warmup", "measurement")
        }
        if row_mutate is not None:
            row_mutate(phase, rows)
        namespace = f"jsons3_if_{phase}_0123abcd"
        manifest = {
            "format": "agent-trace-json-storage-stage3-interference-phase",
            "format_version": 1,
            "run_id": f"{child_run_id}-{phase}",
            "status": "complete",
            "phase": phase,
            "seed": 20260907,
            "execution_scope": "formal",
            "classification": "formal_complete",
            "layout": layout,
            "namespace": namespace,
            "cache_state": "warm-fixed-offered-load-no-os-cache-drop",
            "child_pid": 4242,
            "warmup_seconds": seconds["warmup"],
            "measurement_seconds": seconds["measurement"],
            "schedules": schedules[phase],
            "execution_coverage": {
                "warmup_actual_seconds": wall["warmup"],
                "measurement_actual_seconds": wall["measurement"],
            },
            "snapshots": interference_snapshots(layout),
            "warmup": {
                stream: interference_published(items, schedules[phase]["warmup"][stream],
                                               wall["warmup"])
                for stream, items in rows["warmup"].items()
            },
            "statistics": {
                stream: interference_published(items, schedules[phase]["measurement"][stream],
                                               wall["measurement"])
                for stream, items in rows["measurement"].items()
            },
            "query_evidence": interference_query_evidence(rows, layout),
            "layout_definition": {
                "database": f"{namespace}_{layout}", "ddl": "CREATE DATABASE fixture;",
            },
            "cleanup": {"namespace": f"{namespace}_{layout}", "removed": True},
        }
        if phase_mutate is not None:
            phase_mutate(phase, manifest, rows)
        phases.append(manifest)
        records[phase] = rows
    return {
        "layout": layout,
        "phases": phases,
        "records": records,
        "schedules": schedules,
        "segment_seconds": seconds,
        "child": {
            "format": "agent-trace-json-storage-stage3-interference-run",
            "format_version": 1,
            "run_id": child_run_id,
            "status": "complete",
            "seed": 20260907,
            "execution_scope": "formal",
            "classification": "formal_complete",
            "phase_order": list(INTERFERENCE_PHASES),
            "phases": phases,
            "statistics_boundary": "raw-samples-retained-p99-requires-1000-successes",
        },
    }


def interference_artifact_paths():
    """返回 child 内固定的十六项 artifact 相对路径。"""
    paths = ["child/run-manifest.json"]
    for phase in INTERFERENCE_PHASES:
        paths.append(f"child/{phase}/run-manifest.json")
        paths.append(f"child/{phase}/warmup-samples.jsonl")
        paths.append(f"child/{phase}/samples.jsonl")
    return paths


def interference_artifact_evidence(run):
    """按当前文件重算十六项 artifact 身份。"""
    run = Path(run)
    return [
        {
            "path": relative,
            "bytes": (run / relative).stat().st_size,
            "sha256": hashlib.sha256((run / relative).read_bytes()).hexdigest(),
        }
        for relative in interference_artifact_paths()
    ]


def interference_metadata():
    """构造固定 interference factory metadata。"""
    return {
        "block_size": INTERFERENCE_BLOCK_SIZE,
        "cyclic_replay": True,
        "eligible_block_count": len(INTERFERENCE_ELIGIBLE_INDICES),
        "eligible_block_indices": list(INTERFERENCE_ELIGIBLE_INDICES),
        "eligible_block_sha256": [f"{index:064x}" for index in INTERFERENCE_ELIGIBLE_INDICES],
        "final_watermark": 48534,
        "main_query_catalog_sha256": "f" * 64,
        "preload_block_count": 190,
        "seed": 20260907,
        "selection_rules": {
            "full_block_rows": INTERFERENCE_BLOCK_SIZE,
            "outside_query_window": {
                "project_id": "outside-query-window",
                "start_time": "2026-01-01T00:00:00.000Z",
                "end_time": "2026-01-02T00:00:00.000Z",
            },
            "query_scenarios": {
                "list": "list:first", "preview": "preview:first",
                "detail_2m": "detail:text_2m", "trace_long": "trace:p95",
                "batch_loop": "batch:main",
            },
            "requires_main_payload": True,
        },
    }


def interference_envelope(spec, run):
    """构造与生产 envelope schema 一致的最小 interference 运行结果。"""
    child_root = Path(run) / "child" / "run-manifest.json"
    content = child_root.read_bytes()
    namespaces = [f"jsons3_if_{phase}_0123abcd" for phase in INTERFERENCE_PHASES]
    return {
        "format": "agent-trace-json-storage-stage3-production-run",
        "format_version": 1,
        "run_id": f"jsons3-production-interference-{spec['layout']}",
        "status": "complete",
        "operation": "interference",
        "command": ["python3", "run_stage3.py", "interference"],
        "input": json.loads(json.dumps(INPUT_IDENTITY)),
        "truth": {
            "seed": 20260907, "identity_sha256": IDENTITY, "record_count": 48534,
            "block_size": INTERFERENCE_BLOCK_SIZE, "block_count": 190,
        },
        "query_catalog_sha256": "f" * 64,
        "interference": interference_metadata(),
        "namespace_policy": {
            "strategy": "runner-fixed-phase-unique", "reuse": False,
            "phase_order": list(INTERFERENCE_PHASES),
            "namespace_prefix": "jsons3_if_<phase>_", "namespaces": namespaces,
        },
        "cleanup": {
            "namespaces": namespaces, "namespaces_removed": True,
            "asset_directory_applicable": spec["layout"] == "asset_ref",
            "asset_directory_removed": True,
        },
        "runtime": {
            "operation": "interference", "engine": "clickhouse", "layout": spec["layout"],
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
        "code": {
            role: {"path": f"/source/{role}.py", "bytes": 10, "sha256": character * 64}
            for role, character in zip(INTERFERENCE_CODE_ROLES, "0123456")
        },
        "child": {
            "path": "child/run-manifest.json", "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "format": spec["child"]["format"], "format_version": spec["child"]["format_version"],
            "status": spec["child"]["status"], "run_id": spec["child"]["run_id"],
        },
        "artifacts": interference_artifact_evidence(run),
    }


def write_interference_run(root, layout, spec=None):
    """写入一个最小 interference production 目录并返回路径。"""
    spec = interference_spec(layout) if spec is None else spec
    run = Path(root) / layout
    child_root = run / "child"
    child_root.mkdir(parents=True)
    for phase, manifest in zip(INTERFERENCE_PHASES, spec["phases"]):
        phase_root = child_root / phase
        phase_root.mkdir()
        (phase_root / "run-manifest.json").write_bytes(interference_json_bytes(manifest))
        for segment, filename in INTERFERENCE_SEGMENT_FILES.items():
            streams = spec["records"][phase][segment]
            (phase_root / filename).write_bytes(b"".join(
                interference_json_bytes(row) for rows in streams.values() for row in rows
            ))
    (child_root / "run-manifest.json").write_bytes(interference_json_bytes(spec["child"]))
    envelope = interference_envelope(spec, run)
    (run / "run-manifest.json").write_text(json.dumps(envelope), encoding="utf-8")
    return run


def sync_interference_artifact(run, relative):
    """按当前文件 bytes 同步 envelope 中的 artifact 与 child 身份。"""
    run = Path(run)
    content = (run / relative).read_bytes()
    envelope_path = run / "run-manifest.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    for item in envelope["artifacts"]:
        if item["path"] == relative:
            item["bytes"] = len(content)
            item["sha256"] = hashlib.sha256(content).hexdigest()
    if relative == "child/run-manifest.json":
        envelope["child"]["bytes"] = len(content)
        envelope["child"]["sha256"] = hashlib.sha256(content).hexdigest()
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")


def rewrite_interference_envelope(run, mutate):
    """改写 interference production envelope。"""
    path = Path(run) / "run-manifest.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return envelope


def rewrite_interference_phase(run, phase, mutate):
    """改写独立 phase 文件并同步其 artifact 身份。"""
    path = Path(run) / "child" / phase / "run-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    mutate(manifest)
    path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    sync_interference_artifact(run, f"child/{phase}/run-manifest.json")
    return manifest


def rewrite_interference_child(run, mutate):
    """改写 child root manifest 并同步 child 身份。"""
    path = Path(run) / "child" / "run-manifest.json"
    child = json.loads(path.read_text(encoding="utf-8"))
    mutate(child)
    path.write_text(json.dumps(child, separators=(",", ":")), encoding="utf-8")
    sync_interference_artifact(run, "child/run-manifest.json")
    return child


def rewrite_interference_phase_both(run, phase, mutate):
    """同时改写 phase 文件与 child 内嵌副本，保留二者相等。"""
    manifest = rewrite_interference_phase(run, phase, mutate)

    def replace(child):
        index = [item["phase"] for item in child["phases"]].index(phase)
        child["phases"][index] = json.loads(json.dumps(manifest))

    rewrite_interference_child(run, replace)
    return manifest


def rewrite_interference_raw(run, phase, segment, mutate):
    """按行改写 raw JSONL 并同步其 artifact 身份。"""
    relative = f"child/{phase}/{INTERFERENCE_SEGMENT_FILES[segment]}"
    path = Path(run) / relative
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    mutate(rows)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8",
    )
    sync_interference_artifact(run, relative)
    return rows


class StageThreeInterferenceSummaryTest(unittest.TestCase):
    """验证四布局 interference 控制的结果树、stream 隔离与 drops 语义。"""

    def runs(self, directory, spec_factory=None):
        """写入四个布局各一个最小 interference 正式目录。"""
        spec_factory = interference_spec if spec_factory is None else spec_factory
        return [
            write_interference_run(directory, layout, spec_factory(layout))
            for layout in LAYOUTS
        ]

    def summarize(self, runs):
        return report._summarize_interference(
            runs, schedules=interference_schedules(),
            segment_seconds=dict(INTERFERENCE_SEGMENT_SECONDS),
        )

    def test_summarizes_four_layout_controls_in_stable_order(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            summary = self.summarize(runs)
            self.assertEqual(
                summary["format"], "agent-trace-json-storage-stage3-interference-summary"
            )
            self.assertEqual(summary["format_version"], 1)
            self.assertEqual(summary["phase_order"], list(INTERFERENCE_PHASES))
            self.assertEqual(
                summary["statistics_boundary"],
                "raw-samples-retained-p99-requires-1000-successes",
            )
            self.assertEqual(summary["input"], INPUT_IDENTITY)
            self.assertEqual(summary["interference"], interference_metadata())
            self.assertEqual([item["layout"] for item in summary["layouts"]], list(LAYOUTS))
            for item in summary["layouts"]:
                layout = item["layout"]
                self.assertEqual(item["runtime"]["operation"], "interference")
                self.assertEqual(item["runtime"]["layout"], layout)
                self.assertEqual(
                    [artifact["path"] for artifact in item["artifacts"]],
                    interference_artifact_paths(),
                )
                self.assertEqual(
                    [phase["phase"] for phase in item["phases"]], list(INTERFERENCE_PHASES)
                )
                for phase in item["phases"]:
                    name = phase["phase"]
                    self.assertEqual(
                        [snapshot["name"] for snapshot in phase["snapshots"]],
                        ["before_warmup", "before_measurement", "after_measurement"],
                    )
                    self.assertNotIn("warmup", phase)
                    self.assertEqual(
                        set(phase["execution_coverage"]),
                        {"warmup_actual_seconds", "measurement_actual_seconds"},
                    )
                    streams = phase["streams"]
                    self.assertEqual(
                        set(streams), set(interference_schedules()[name]["measurement"])
                    )
                    listing = streams["list"]
                    self.assertEqual(listing["counts"]["scheduled_requests"], 6)
                    self.assertEqual(listing["counts"]["successful_requests"], 6)
                    self.assertEqual(listing["counts"]["dropped_requests"], 0)
                    self.assertEqual(listing["counts"]["failed_requests"], 0)
                    self.assertEqual(listing["counts"]["late_requests"], 0)
                    self.assertEqual(listing["latency_ms"]["p50"], 100.0)
                    self.assertEqual(listing["latency_ms"]["p99"], None)
                    self.assertEqual(
                        listing["latency_ms"]["p99_status"],
                        "unavailable_insufficient_successes",
                    )
                    self.assertEqual(listing["metrics"]["query_complete_ms"]["p50"], 10.0)
                    self.assertEqual(listing["response_bytes"]["total"], 600)
                    self.assertEqual(listing["resolver_read_ms"]["maximum"], 0.0)
                    if name == "quiet":
                        self.assertEqual(set(streams), {"list", "preview"})
                    else:
                        stream = INTERFERENCE_PHASE_STREAM[name]
                        self.assertIn(stream, streams)
                        if stream == "batch_loop":
                            self.assertIsNone(streams[stream]["offered_rate_requests_s"])
                            self.assertEqual(
                                streams[stream]["counts"]["scheduled_requests"],
                                INTERFERENCE_CONTINUOUS_ROWS["measurement"],
                            )
                        if stream == "continuous_ingest":
                            # 写入流只发布计数、吞吐与时延，不附查询时延指标。
                            self.assertNotIn("metrics", streams[stream])
                            self.assertNotIn("resolver_requests", streams[stream])
            self.assertEqual(summary, self.summarize(list(reversed(runs))))

    def test_publishes_drops_without_counting_them_as_failures(self):
        def mutate(phase, rows):
            if phase != "quiet":
                return
            for segment in ("warmup", "measurement"):
                rows[segment]["list"][0] = interference_row(
                    "list", 0, "same_table", phase, segment, status="dropped"
                )
                rows[segment]["preview"][1] = interference_row(
                    "preview", 1, "same_table", phase, segment, status="failed"
                )

        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory, lambda layout: interference_spec(
                layout, row_mutate=mutate if layout == "same_table" else None
            ))
            summary = self.summarize(runs)
            quiet = next(
                phase for item in summary["layouts"] if item["layout"] == "same_table"
                for phase in item["phases"] if phase["phase"] == "quiet"
            )
            self.assertEqual(quiet["streams"]["list"]["counts"], {
                "scheduled_requests": 6, "started_requests": 5, "completed_requests": 5,
                "successful_requests": 5, "failed_requests": 0, "timed_out_requests": 0,
                "dropped_requests": 1, "late_requests": 0,
            })
            self.assertEqual(quiet["streams"]["preview"]["counts"], {
                "scheduled_requests": 6, "started_requests": 6, "completed_requests": 6,
                "successful_requests": 5, "failed_requests": 1, "timed_out_requests": 0,
                "dropped_requests": 0, "late_requests": 0,
            })
            self.assertEqual(quiet["streams"]["list"]["latency_ms"]["p50"], 100.0)

    def test_summary_contains_no_main_matrix_pairing_fields(self):
        forbidden = {
            "rounds", "round_index", "round_statistic_median", "main_matrix", "delta",
            "paired", "pairing", "main_pair", "main_vs_part", "difference", "latin_square",
        }
        with tempfile.TemporaryDirectory() as directory:
            summary = self.summarize(self.runs(directory))

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
        self.assertEqual(
            set(summary),
            {
                "format", "format_version", "input", "truth", "query_catalog_sha256",
                "interference", "statistics_boundary", "phase_order", "layouts",
            },
        )

    def test_recomputes_raw_evidence_without_read_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            with patch.object(
                Path, "read_bytes", side_effect=AssertionError("read_bytes is not allowed")
            ):
                summary = self.summarize(runs)
            self.assertEqual(len(summary["layouts"]), 4)


class StageThreeInterferenceRawTest(unittest.TestCase):
    """验证 raw 重算核心的计数、时延发布与 stream 边界。"""

    def parse(self, directory, rows, schedule):
        """按注入的 schedule 解析一个最小 raw JSONL。"""
        path = Path(directory) / "samples.jsonl"
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )
        return report._parse_interference_raw(
            path, {"list": schedule}, query_scenarios=INTERFERENCE_QUERY_SCENARIOS,
            write_tables=LAYOUT_TARGETS["same_table"], block_size=INTERFERENCE_BLOCK_SIZE,
            watermarks={27904}, horizon=schedule["duration_seconds"] + 1.0,
        )

    def schedule(self, count):
        return {
            "name": "list", "rate_per_second": 5.0, "duration_seconds": count / 5.0,
            "workers": 1, "timeout_seconds": 30.0, "late_tolerance_seconds": 0.05,
            "mode": "fixed",
        }

    def test_publishes_p99_only_from_one_thousand_successes(self):
        for count, publishable in ((999, False), (1000, True)):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                schedule = self.schedule(count)
                rows = [
                    interference_row("list", sequence, "same_table", "quiet", "measurement")
                    for sequence in range(count)
                ]
                parsed = self.parse(directory, rows, schedule)
                result = report._interference_stream_statistics(
                    parsed["list"], schedule, float(count)
                )
                self.assertEqual(result["counts"]["successful_requests"], count)
                self.assertEqual(
                    result["latency_ms"]["p99_status"],
                    "publishable" if publishable else "unavailable_insufficient_successes",
                )
                self.assertEqual(result["latency_ms"]["p99"] is None, not publishable)
                self.assertEqual(result["latency_ms"]["minimum"], 100.0)

    def test_publishes_percentiles_from_an_independent_oracle(self):
        """以非常量时延和独立百分位实现校验发布的 p50/p95/p99。"""
        count = 1000
        latencies = [1.0 + (sequence * 7 % count) * 0.25 for sequence in range(count)]
        with tempfile.TemporaryDirectory() as directory:
            schedule = self.schedule(count)
            rows = [
                interference_row(
                    "list", sequence, "same_table", "quiet", "measurement",
                    application_ready_ms=latencies[sequence],
                )
                for sequence in range(count)
            ]
            parsed = self.parse(directory, rows, schedule)
            result = report._interference_stream_statistics(
                parsed["list"], schedule, float(count)
            )
            latency = result["latency_ms"]
            for field, fraction in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
                self.assertAlmostEqual(
                    latency[field], percentile_oracle(latencies, fraction), places=9
                )
            self.assertEqual(latency["minimum"], min(latencies))
            self.assertEqual(latency["maximum"], max(latencies))
            self.assertNotEqual(latency["p50"], latency["p95"])

    def test_keeps_failed_timed_out_and_dropped_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            schedule = self.schedule(4)
            rows = [
                interference_row("list", 0, "same_table", "quiet", "measurement"),
                interference_row("list", 1, "same_table", "quiet", "measurement",
                                 status="dropped"),
                interference_row("list", 2, "same_table", "quiet", "measurement",
                                 status="failed"),
                interference_row("list", 3, "same_table", "quiet", "measurement",
                                 status="timed_out", completed_offset_seconds=None),
            ]
            parsed = self.parse(directory, rows, schedule)
            self.assertEqual(parsed["list"]["counts"], {
                "scheduled_requests": 4, "started_requests": 3, "completed_requests": 2,
                "successful_requests": 1, "failed_requests": 1, "timed_out_requests": 1,
                "dropped_requests": 1, "late_requests": 0,
            })

    def test_binds_scheduled_offsets_tighter_than_one_inter_arrival_interval(self):
        """20 请求/秒下到达间隔与 late_tolerance 相等，半槽整体平移必须被拒绝。"""
        count = 10
        schedule = dict(
            self.schedule(count), rate_per_second=20.0, duration_seconds=count / 20.0,
        )
        rows = []
        for sequence in range(count):
            offset = sequence / 20.0 + 0.025
            rows.append(interference_row(
                "list", sequence, "same_table", "quiet", "measurement",
                scheduled_offset_seconds=offset, started_offset_seconds=offset,
                completed_offset_seconds=offset + 0.1,
            ))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError, "raw scheduled offset is off the published rate"
            ):
                self.parse(directory, rows, schedule)

    def test_rejects_started_offsets_beyond_the_published_wall(self):
        """timed_out 行没有完成时刻，起始时刻同样受 horizon 约束。"""
        rows = [
            interference_row("list", 0, "same_table", "quiet", "measurement"),
            interference_row(
                "list", 1, "same_table", "quiet", "measurement", status="timed_out",
                started_offset_seconds=1e9, completed_offset_seconds=None,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "raw start exceeds the published wall"):
                self.parse(directory, rows, self.schedule(2))

    def test_rejects_continuous_scheduled_offsets_beyond_the_published_wall(self):
        """continuous stream 的调度偏移不由速率定位，仍需受 horizon 上界约束。"""
        schedule = dict(self.schedule(2), rate_per_second=None, mode="continuous")
        rows = [
            interference_row("list", 0, "same_table", "quiet", "measurement"),
            interference_row(
                "list", 1, "same_table", "quiet", "measurement",
                scheduled_offset_seconds=1e9,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError, "raw scheduled offset exceeds the published wall"
            ):
                self.parse(directory, rows, schedule)

    def test_schedules_mirror_the_runner_fixed_factory(self):
        sys.path.insert(0, str(STAGE_DIR / "runner"))
        import run_interference

        formal = report._interference_schedules()
        for phase in run_interference.FIXED_PHASES:
            for measurement in (False, True):
                segment = "measurement" if measurement else "warmup"
                expected = run_interference._json_value(
                    run_interference.fixed_phase_schedules(phase, measurement=measurement)
                )
                for schedule in expected.values():
                    self.assertEqual(
                        schedule["duration_seconds"], INTERFERENCE_FORMAL_SECONDS[segment]
                    )
                self.assertEqual(formal[phase.name][segment], expected)

    def test_rejects_raw_streams_without_rows(self):
        preview = {
            "name": "preview", "rate_per_second": 5.0, "duration_seconds": 0.4,
            "workers": 1, "timeout_seconds": 30.0, "late_tolerance_seconds": 0.05,
            "mode": "fixed",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.jsonl"
            path.write_text("".join(
                json.dumps(interference_row(
                    "preview", sequence, "same_table", "quiet", "measurement"
                )) + "\n"
                for sequence in range(2)
            ), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "interference raw stream is missing"):
                report._parse_interference_raw(
                    path, {"list": self.schedule(2), "preview": preview},
                    query_scenarios=INTERFERENCE_QUERY_SCENARIOS,
                    write_tables=LAYOUT_TARGETS["same_table"],
                    block_size=INTERFERENCE_BLOCK_SIZE, watermarks={27904}, horizon=2.0,
                )


class StageThreeInterferenceGateTest(unittest.TestCase):
    """验证 interference 控制的身份、artifact、phase 与发布反例全部 fail closed。"""

    def summarize(self, runs):
        return report._summarize_interference(
            runs, schedules=interference_schedules(),
            segment_seconds=dict(INTERFERENCE_SEGMENT_SECONDS),
        )

    def runs(self, directory):
        return [write_interference_run(directory, layout) for layout in LAYOUTS]

    def reject(self, mutate, message, spec_factory=None):
        """写入四布局 fixture，施加指定的反例后要求门禁拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            spec_factory = interference_spec if spec_factory is None else spec_factory
            runs = [
                write_interference_run(directory, layout, spec_factory(layout))
                for layout in LAYOUTS
            ]
            mutate(runs)
            with self.assertRaisesRegex(ValueError, message):
                self.summarize(runs)

    def test_rejects_missing_or_duplicated_layout(self):
        with self.assertRaisesRegex(ValueError, "at least one interference run directory"):
            report.summarize_interference([])
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
                report.summarize_interference(runs[:-1])
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            runs.append(write_interference_run(Path(directory) / "duplicate", "same_table"))
            with self.assertRaisesRegex(
                ValueError, "duplicate interference layout: same_table"
            ):
                report.summarize_interference(runs)

    def test_public_entry_requires_the_formal_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "phase manifest mismatch"):
                report.summarize_interference(self.runs(directory))

    def test_rejects_cross_run_identity_drift(self):
        cases = (
            (
                lambda runs: rewrite_interference_envelope(
                    runs[1],
                    lambda envelope: envelope["input"]["events"].__setitem__("sha256", "9" * 64),
                ),
                "interference input identity differs between runs",
            ),
            (
                lambda runs: rewrite_interference_envelope(
                    runs[2], lambda envelope: envelope["truth"].__setitem__("record_count", 1)
                ),
                "interference truth identity is invalid",
            ),
            (
                lambda runs: rewrite_interference_envelope(
                    runs[3],
                    lambda envelope: (
                        envelope.__setitem__("query_catalog_sha256", "9" * 64),
                        envelope["interference"].__setitem__(
                            "main_query_catalog_sha256", "9" * 64
                        ),
                    ),
                ),
                "interference query catalog identity differs between runs",
            ),
            (
                lambda runs: rewrite_interference_envelope(
                    runs[1],
                    lambda envelope: envelope["interference"][
                        "eligible_block_indices"
                    ].__setitem__(-1, 153),
                ),
                "interference metadata differs between runs",
            ),
            (
                lambda runs: rewrite_interference_envelope(
                    runs[1],
                    lambda envelope: envelope["code"]["production"].__setitem__(
                        "sha256", "9" * 64
                    ),
                ),
                "interference code evidence differs between runs",
            ),
            (
                lambda runs: rewrite_interference_envelope(
                    runs[1],
                    lambda envelope: envelope["runtime"]["container"].__setitem__(
                        "image_id", "sha256:" + "1" * 64
                    ),
                ),
                "interference runtime identity differs between runs",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(mutate, message)

    def test_rejects_envelope_schema_gaps(self):
        cases = (
            (lambda e: e.__setitem__("format", "other"), "format/version is invalid"),
            (lambda e: e.__setitem__("format_version", True), "format/version is invalid"),
            (lambda e: e.__setitem__("status", "running"), "status is not complete"),
            (lambda e: e.__setitem__("operation", "part-states"), "operation is invalid"),
            (lambda e: e.pop("run_id"), "run identity is missing"),
            (lambda e: e["truth"].__setitem__("record_count", 1), "truth identity is invalid"),
            (
                lambda e: e["truth"].__setitem__("identity_sha256", "9" * 64),
                "truth identity is invalid",
            ),
            (lambda e: e["truth"].__setitem__("seed", True), "truth identity is invalid"),
            (
                lambda e: e.__setitem__("query_catalog_sha256", "x"),
                "query catalog identity is invalid",
            ),
            (lambda e: e["code"].pop("production"), "code evidence is incomplete"),
            (
                lambda e: e["code"]["production"].__setitem__("bytes", 0),
                "code evidence is incomplete",
            ),
            (lambda e: e["interference"].pop("cyclic_replay"), "metadata is invalid"),
            (
                lambda e: e["interference"].__setitem__("cyclic_replay", 1),
                "metadata is invalid",
            ),
            (
                lambda e: e["interference"].__setitem__("eligible_block_count", 44),
                "metadata is invalid",
            ),
            (
                lambda e: e["interference"]["eligible_block_indices"].__setitem__(0, 200),
                "metadata is invalid",
            ),
            (
                lambda e: e["interference"]["eligible_block_sha256"].__setitem__(0, "0" * 63),
                "metadata is invalid",
            ),
            (
                lambda e: e["interference"]["selection_rules"].pop("requires_main_payload"),
                "metadata is invalid",
            ),
            (
                lambda e: e["interference"]["selection_rules"][
                    "outside_query_window"
                ].__setitem__("page_size", 256),
                "metadata is invalid",
            ),
            (
                lambda e: e["interference"].__setitem__(
                    "main_query_catalog_sha256", "9" * 64
                ),
                "metadata is invalid",
            ),
            (lambda e: e["namespace_policy"].__setitem__("reuse", True),
             "namespace policy is invalid"),
            (
                lambda e: e["namespace_policy"].__setitem__("strategy", "other"),
                "namespace policy is invalid",
            ),
            (
                lambda e: e["namespace_policy"]["namespaces"].__setitem__(0, "other"),
                "cleanup evidence is missing",
            ),
            (lambda e: e["cleanup"].__setitem__("namespaces_removed", False),
             "cleanup evidence is missing"),
            (
                lambda e: e["cleanup"]["namespaces"].pop(),
                "cleanup evidence is missing",
            ),
            (
                lambda e: e["cleanup"].__setitem__("asset_directory_applicable", True),
                "cleanup evidence is missing",
            ),
            (lambda e: e["child"].__setitem__("bytes", 1), "child identity mismatch"),
            (lambda e: e["child"].__setitem__("status", "running"), "child evidence is missing"),
            (
                lambda e: e["child"].__setitem__("path", "child/other.json"),
                "child evidence is missing",
            ),
            (
                lambda e: e["artifacts"].append(dict(e["artifacts"][0])),
                "artifacts are incomplete",
            ),
            (lambda e: e["artifacts"].pop(), "artifacts are incomplete"),
            (
                lambda e: e["artifacts"][0].__setitem__("bytes", 1),
                "artifact identity mismatch",
            ),
            (
                lambda e: e["artifacts"][0].__setitem__("sha256", "9" * 64),
                "artifact identity mismatch",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_envelope(runs[0], mutate),
                    message,
                )

    def test_rejects_artifact_file_and_path_escapes(self):
        def append_byte(runs):
            path = runs[0] / "child" / "quiet" / "samples.jsonl"
            path.write_bytes(path.read_bytes() + b"{}")

        def symlink_escape(runs):
            path = runs[0] / "child" / "quiet" / "samples.jsonl"
            path.unlink()
            os.symlink(runs[0] / "run-manifest.json", path)

        def remove_file(runs):
            (runs[0] / "child" / "quiet" / "warmup-samples.jsonl").unlink()

        for mutate, message in (
            (append_byte, "artifact identity mismatch"),
            (symlink_escape, "artifact path escapes the child directory"),
            (remove_file, "artifact is unavailable"),
        ):
            with self.subTest(message=message):
                self.reject(mutate, message)

    def test_rejects_phase_schedule_coverage_and_root_gaps(self):
        cases = (
            (
                lambda run: rewrite_interference_phase(
                    run, "quiet",
                    lambda m: m["schedules"]["warmup"]["list"].__setitem__(
                        "rate_per_second", 6.0
                    ),
                ),
                "phase file and root value mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet",
                    lambda m: m["schedules"]["warmup"]["list"].__setitem__(
                        "rate_per_second", 6.0
                    ),
                ),
                "interference schedules mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet",
                    lambda m: m["schedules"]["measurement"]["list"].__setitem__(
                        "late_tolerance_seconds", 0.5
                    ),
                ),
                "interference schedules mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "trace_long",
                    lambda m: m.__setitem__("measurement_seconds", 301.0),
                ),
                "phase manifest mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet", lambda m: m.__setitem__("status", "failed")
                ),
                "phase manifest mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet", lambda m: m.__setitem__("execution_scope", "smoke")
                ),
                "phase manifest mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet", lambda m: m.__setitem__("layout", "separate")
                ),
                "phase manifest mismatch",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet", lambda m: m.__setitem__("error", "boom")
                ),
                "phase manifest is not formal complete",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet",
                    lambda m: m["execution_coverage"].__setitem__(
                        "measurement_actual_seconds", 1.0
                    ),
                ),
                "formal coverage is short",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet",
                    lambda m: m["execution_coverage"].__setitem__(
                        "measurement_actual_seconds", 301.0
                    ),
                ),
                "formal coverage is inconsistent",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet", lambda m: m["cleanup"].__setitem__("removed", False)
                ),
                "cleanup evidence is invalid",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet", lambda m: m["cleanup"].__setitem__("namespace", "other")
                ),
                "cleanup evidence is invalid",
            ),
            (
                lambda run: rewrite_interference_phase_both(
                    run, "quiet",
                    lambda m: m["layout_definition"].__setitem__("database", "other"),
                ),
                "layout definition is invalid",
            ),
            (
                lambda run: rewrite_interference_child(
                    run, lambda child: child.__setitem__("seed", 1)
                ),
                "root manifest is not formal complete",
            ),
            (
                lambda run: rewrite_interference_child(
                    run, lambda child: child.__setitem__("status", "failed")
                ),
                "root manifest is not formal complete",
            ),
            (
                lambda run: rewrite_interference_child(
                    run, lambda child: child.__setitem__("error", "boom")
                ),
                "root manifest is not formal complete",
            ),
            (
                lambda run: rewrite_interference_child(
                    run, lambda child: child["phase_order"].reverse()
                ),
                "root manifest is not formal complete",
            ),
            (
                lambda run: rewrite_interference_child(
                    run, lambda child: child.__setitem__("phases", child["phases"][:4])
                ),
                "root phases are incomplete",
            ),
            (
                lambda run: rewrite_interference_child(
                    run, lambda child: child["phases"][0].__setitem__("status", "failed")
                ),
                "phase file and root value mismatch",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: mutate(runs[0]),
                    message,
                )

    def test_rejects_duplicated_phase_namespace(self):
        def mutate(manifest):
            manifest["namespace"] = "jsons3_if_detail_2m_0123abcd"
            manifest["cleanup"]["namespace"] = "jsons3_if_detail_2m_0123abcd_same_table"
            manifest["layout_definition"]["database"] = (
                "jsons3_if_detail_2m_0123abcd_same_table"
            )

        self.reject(
            lambda runs: rewrite_interference_phase_both(runs[0], "quiet", mutate),
            "namespaces are invalid or duplicated",
        )

    def test_rejects_snapshot_and_storage_gaps(self):
        cases = (
            (lambda m: m["snapshots"].reverse(), "snapshots are incomplete"),
            (
                lambda m: m["snapshots"][1]["resources"]["memory"].__setitem__(
                    "status", "unavailable"
                ),
                "resource snapshot is invalid",
            ),
            (
                lambda m: m["snapshots"][0]["resources"]["io"].__setitem__("devices", 0),
                "resource snapshot is invalid",
            ),
            (lambda m: m["snapshots"][0].__setitem__("active_part_backlog", 1),
             "storage totals mismatch"),
            (lambda m: m["snapshots"][0].__setitem__("active_merge_count", 1),
             "storage totals mismatch"),
            (
                lambda m: m["snapshots"][0]["storage"].__setitem__(
                    "merges", [{"table": "events", "num_parts": 2}]
                ),
                "storage totals mismatch",
            ),
            (
                lambda m: m["snapshots"][2]["storage"]["tables"].pop("events"),
                "storage evidence is incomplete",
            ),
            (
                lambda m: m["snapshots"][0]["storage"]["tables"]["events"].__setitem__(
                    "part_count", 1.0
                ),
                "storage metrics are invalid",
            ),
            (
                lambda m: m["snapshots"][0]["storage"].__setitem__(
                    "asset_store", dict(INTERFERENCE_ASSET_STORE)
                ),
                "asset store evidence is invalid",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_phase_both(
                        runs[0], "quiet", mutate
                    ),
                    message,
                )

    def test_rejects_asset_ref_store_watermark_drift(self):
        cases = (
            lambda m: m["snapshots"][0]["storage"]["asset_store"].__setitem__(
                "available_object_count", 159
            ),
            lambda m: m["snapshots"][2]["storage"]["asset_store"].__setitem__(
                "available_bytes", 128450559
            ),
            lambda m: m["snapshots"][1]["storage"]["asset_store"].__setitem__(
                "orphan_object_count", 1
            ),
            lambda m: m["snapshots"][0]["storage"]["asset_store"].__setitem__(
                "orphan_bytes", 1
            ),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_phase_both(
                        runs[3], "quiet", mutate
                    ),
                    "asset store evidence is invalid",
                )
        self.reject(
            lambda runs: rewrite_interference_phase_both(
                runs[3], "quiet",
                lambda m: m["snapshots"][0]["storage"].__setitem__("asset_store", None),
            ),
            "asset store evidence is invalid",
        )

    def test_rejects_raw_contract_gaps(self):
        cases = (
            (lambda rows: rows[1].__setitem__("sequence", 5), "sequence is not contiguous"),
            (lambda rows: rows.pop(), "fixed scheduled count is invalid"),
            (lambda rows: rows[0].__setitem__("extra", 1), "fields or stream are invalid"),
            (lambda rows: rows[0].__setitem__("stream", "other"), "fields or stream are invalid"),
            (lambda rows: rows[0].__setitem__("status", "unknown"), "raw status is invalid"),
            (lambda rows: rows[0].__setitem__("late_by_ms", "0"), "raw timing is invalid"),
            (lambda rows: rows[0].update({"status": "dropped"}),
             "raw dropped sample is inconsistent"),
            (lambda rows: rows[0].__setitem__("error", "boom"),
             "raw successful sample contains error"),
            (lambda rows: rows[0].update({"status": "failed", "sample": None, "error": None}),
             "raw failed sample lacks error"),
            (
                lambda rows: rows[0].update(
                    {"status": "timed_out", "completed_offset_seconds": None}
                ),
                "raw incomplete timing is inconsistent",
            ),
            (
                lambda rows: rows[0].__setitem__("completed_offset_seconds", "0.1"),
                "raw started/completed evidence is invalid",
            ),
            (
                lambda rows: rows[0]["sample"].__setitem__("scenario", "list:middle"),
                "successful query evidence is invalid",
            ),
            (
                lambda rows: rows[0]["sample"].__setitem__("query_id", ""),
                "successful query evidence is invalid",
            ),
            (
                lambda rows: rows[0]["sample"].__setitem__("validation", {"row_count": 1}),
                "successful query evidence is invalid",
            ),
            (
                lambda rows: rows[0]["sample"].__setitem__("response_bytes", 7),
                "successful query evidence is invalid",
            ),
            (
                lambda rows: rows[0]["sample"].__setitem__("resolver_read_ms", "1"),
                "successful query evidence is invalid",
            ),
            (
                lambda rows: rows[0]["sample"].__setitem__("query_complete_ms", None),
                "successful query evidence is invalid",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_raw(
                        runs[0], "quiet", "measurement", mutate
                    ),
                    message,
                )

    def test_rejects_empty_and_unavailable_raw_files(self):
        def empty_raw(runs):
            path = runs[0] / "child" / "quiet" / "samples.jsonl"
            path.write_bytes(b"")
            sync_interference_artifact(runs[0], "child/quiet/samples.jsonl")

        def blank_line(runs):
            path = runs[0] / "child" / "quiet" / "samples.jsonl"
            path.write_bytes(path.read_bytes() + b"\n")
            sync_interference_artifact(runs[0], "child/quiet/samples.jsonl")

        def missing_raw(runs):
            (runs[0] / "child" / "quiet" / "samples.jsonl").unlink()

        for mutate, message in (
            (empty_raw, "artifacts are incomplete"),
            (blank_line, "empty or contains an empty line"),
            (missing_raw, "artifact is unavailable"),
        ):
            with self.subTest(message=message):
                self.reject(mutate, message)

    def test_rejects_query_evidence_gaps(self):
        cases = (
            (
                lambda m: m["query_evidence"]["plans"].pop(
                    next(iter(m["query_evidence"]["plans"]))
                ),
                "query evidence IDs mismatch",
            ),
            (
                lambda m: m["query_evidence"]["query_details"].pop(
                    next(iter(m["query_evidence"]["query_details"]))
                ),
                "query evidence IDs mismatch",
            ),
            (
                lambda m: m["query_evidence"]["query_finish"].pop(
                    next(iter(m["query_evidence"]["query_finish"]))
                ),
                "query evidence IDs mismatch",
            ),
            (
                lambda m: next(
                    iter(m["query_evidence"]["query_details"].values())
                ).__setitem__("kind", "batch"),
                "query detail evidence is invalid",
            ),
            (
                lambda m: next(
                    iter(m["query_evidence"]["query_details"].values())
                ).__setitem__("scanned_rows", 3),
                "query evidence is inconsistent",
            ),
            (
                lambda m: next(
                    iter(m["query_evidence"]["query_finish"].values())
                ).__setitem__("exception_code", 1),
                "QueryFinish evidence is invalid",
            ),
            (
                lambda m: next(
                    iter(m["query_evidence"]["query_finish"].values())
                ).__setitem__("read_rows", 0),
                "query evidence is inconsistent",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_phase_both(
                        runs[0], "quiet", mutate
                    ),
                    message,
                )
        self.reject(
            lambda runs: rewrite_interference_raw(
                runs[0], "quiet", "measurement",
                lambda rows: rows[0]["sample"].__setitem__(
                    "query_id", "same_table-quiet-warmup-list-0"
                ),
            ),
            "query IDs are duplicated across segments",
        )

    def test_rejects_continuous_ingest_block_gaps(self):
        def continuous_sample(rows):
            return next(row for row in rows if row["stream"] == "continuous_ingest")["sample"]

        cases = (
            lambda rows: continuous_sample(rows).__setitem__("watermark", 27905),
            lambda rows: continuous_sample(rows).__setitem__("rows", 255),
            lambda rows: continuous_sample(rows).__setitem__("watermarks", {"events": 27905}),
            lambda rows: continuous_sample(rows).__setitem__("watermarks", {"other": 27904}),
            lambda rows: continuous_sample(rows).__setitem__("watermarks", {}),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_raw(
                        runs[0], "continuous_ingest", "measurement", mutate
                    ),
                    "continuous_ingest success lacks BlockResult",
                )

    def test_rejects_publication_gaps(self):
        cases = (
            (
                lambda m: m["statistics"]["list"].__setitem__("successful_requests", 7),
                "raw and summary counts mismatch",
            ),
            (
                lambda m: m["warmup"]["list"].__setitem__("dropped_requests", 1),
                "raw and summary counts mismatch",
            ),
            (
                lambda m: m["statistics"]["list"].__setitem__("late_requests", True),
                "raw and summary counts mismatch",
            ),
            (
                lambda m: m["statistics"]["list"].__setitem__(
                    "completed_throughput_requests_s", 1.0
                ),
                "summary publication evidence is invalid",
            ),
            (
                lambda m: m["statistics"]["list"]["latency_ms"].update(
                    {"p99": 100.0, "p99_status": "publishable"}
                ),
                "summary publication evidence is invalid",
            ),
            (
                lambda m: m["statistics"]["list"]["latency_ms"].__setitem__("p50", 99.0),
                "summary publication evidence is invalid",
            ),
            (
                lambda m: m["statistics"]["list"].__setitem__("offered_rate_requests_s", 99.0),
                "summary publication evidence is invalid",
            ),
            (
                lambda m: m["statistics"]["list"].__setitem__("phase_wall_seconds", 0.0),
                "summary publication evidence is invalid",
            ),
            (
                lambda m: m["statistics"]["list"].pop("latency_ms"),
                "summary publication evidence is invalid",
            ),
            (lambda m: m["statistics"].pop("preview"), "summary streams mismatch"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_phase_both(
                        runs[0], "quiet", mutate
                    ),
                    message,
                )

    def test_rejects_dropped_requests_published_as_failures(self):
        def drop_first_list_row(phase, rows):
            if phase == "quiet":
                rows["measurement"]["list"][0] = interference_row(
                    "list", 0, "same_table", "quiet", "measurement", status="dropped"
                )

        self.reject(
            lambda runs: rewrite_interference_phase_both(
                runs[0], "quiet",
                lambda m: m["statistics"]["list"].update(
                    {"dropped_requests": 0, "failed_requests": 1}
                ),
            ),
            "raw and summary counts mismatch",
            spec_factory=lambda layout: interference_spec(
                layout, row_mutate=drop_first_list_row if layout == "same_table" else None
            ),
        )

    def test_rejects_raw_offsets_outside_the_published_schedule(self):
        cases = (
            (
                lambda rows: rows[0].__setitem__("scheduled_offset_seconds", 9.0),
                "raw scheduled offset is off the published rate",
            ),
            (
                lambda rows: rows[0].__setitem__("completed_offset_seconds", 9.0),
                "raw completion exceeds the published wall",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_raw(
                        runs[0], "quiet", "measurement", mutate
                    ),
                    message,
                )

    def test_rejects_coverage_beyond_the_published_wall(self):
        def overrun(phase, manifest, rows):
            if phase != "quiet":
                return
            manifest["execution_coverage"]["measurement_actual_seconds"] = 40.0
            for item in manifest["statistics"].values():
                item["phase_wall_seconds"] = 40.0
                item["completed_throughput_requests_s"] = item["successful_requests"] / 40.0

        self.reject(
            lambda runs: None,
            "formal coverage is inconsistent",
            spec_factory=lambda layout: interference_spec(
                layout, phase_mutate=overrun if layout == "same_table" else None
            ),
        )

    def test_rejects_coverage_beyond_the_allowed_segment_overrun(self):
        """coverage 的上界是固定的 segment 超出余量，而不是一个请求超时。"""
        def overrun(phase, manifest, rows):
            if phase != "quiet":
                return
            actual = INTERFERENCE_SEGMENT_SECONDS["measurement"] + 8.0
            manifest["execution_coverage"]["measurement_actual_seconds"] = actual
            for item in manifest["statistics"].values():
                item["phase_wall_seconds"] = actual
                item["completed_throughput_requests_s"] = (
                    item["successful_requests"] / actual
                )

        self.reject(
            lambda runs: None,
            "formal coverage is inconsistent",
            spec_factory=lambda layout: interference_spec(
                layout, phase_mutate=overrun if layout == "same_table" else None
            ),
        )

    def test_binds_index_scan_counts_instead_of_query_ids(self):
        """index_scans 按索引名聚合，门禁只约束其计数值域。"""
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            rewrite_interference_phase_both(
                runs[0], "quiet",
                lambda m: m["query_evidence"].__setitem__(
                    "index_scans", {"events_list_idx": 12}
                ),
            )
            self.assertEqual(len(self.summarize(runs)["layouts"]), len(LAYOUTS))
        for value in ("garbage", -1, True):
            with self.subTest(value=value):
                self.reject(
                    lambda runs, value=value: rewrite_interference_phase_both(
                        runs[0], "quiet",
                        lambda m: m["query_evidence"].__setitem__(
                            "index_scans",
                            {next(iter(m["query_evidence"]["plans"])): value},
                        ),
                    ),
                    "query evidence IDs mismatch",
                )

    def test_rejects_phase_namespace_outside_the_phase_prefix(self):
        def mutate(namespace):
            def apply(manifest):
                manifest["namespace"] = namespace
                manifest["cleanup"]["namespace"] = f"{namespace}_same_table"
                manifest["layout_definition"]["database"] = f"{namespace}_same_table"

            return apply

        for namespace in ("jsons3_if_quiet_", "jsons3_other_quiet_0123abcd"):
            with self.subTest(namespace=namespace):
                self.reject(
                    lambda runs, namespace=namespace: rewrite_interference_phase_both(
                        runs[0], "quiet", mutate(namespace)
                    ),
                    "namespaces are invalid or duplicated",
                )

    def test_rejects_eligible_indices_beyond_the_final_watermark(self):
        self.reject(
            lambda runs: rewrite_interference_envelope(
                runs[0],
                lambda e: e["interference"].__setitem__(
                    "eligible_block_indices", list(range(145, 190))
                ),
            ),
            "metadata is invalid",
        )

    def test_rejects_incomplete_phase_and_root_evidence(self):
        phase_cases = (
            (lambda m: m.__setitem__("cache_state", "cold"), "phase manifest mismatch"),
            (lambda m: m.pop("cache_state"), "phase manifest mismatch"),
            (lambda m: m.pop("child_pid"), "phase manifest mismatch"),
            (lambda m: m.__setitem__("child_pid", 0), "phase manifest mismatch"),
            (
                lambda m: m["query_evidence"].__setitem__("index_scans", []),
                "query evidence IDs mismatch",
            ),
            (
                lambda m: m["query_evidence"].__setitem__("index_scans", {"other": []}),
                "query evidence IDs mismatch",
            ),
        )
        for mutate, message in phase_cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_interference_phase_both(
                        runs[0], "quiet", mutate
                    ),
                    message,
                )
        self.reject(
            lambda runs: rewrite_interference_child(
                runs[0], lambda child: child.__setitem__("statistics_boundary", "other")
            ),
            "root manifest is not formal complete",
        )

    def test_rejects_run_identity_shared_between_layouts(self):
        def share_envelope_run_id(runs):
            envelope = json.loads((runs[0] / "run-manifest.json").read_text(encoding="utf-8"))
            rewrite_interference_envelope(
                runs[1], lambda other: other.__setitem__("run_id", envelope["run_id"])
            )

        def share_child_run_id(runs):
            child = json.loads(
                (runs[0] / "child" / "run-manifest.json").read_text(encoding="utf-8")
            )
            rewrite_interference_child(
                runs[1], lambda other: other.__setitem__("run_id", child["run_id"])
            )
            rewrite_interference_envelope(
                runs[1], lambda e: e["child"].__setitem__("run_id", child["run_id"])
            )

        for mutate in (share_envelope_run_id, share_child_run_id):
            with self.subTest(mutate=mutate):
                self.reject(mutate, "run identity is duplicated between runs")

    def test_rejects_symlinked_child_directory(self):
        def symlink_child(runs):
            child = runs[0] / "child"
            child.rename(runs[0] / "child-store")
            os.symlink(runs[0] / "child-store", child)

        self.reject(symlink_child, "child directory is a symlink")


ASSET_FAILURE_ENGINES = ("opengauss", "clickhouse")
ASSET_FAILURE_CASES = (
    "missing", "corrupt", "metadata_mismatch",
    "upload_then_db_failure", "publish_failure", "delete_failure",
)
ASSET_FAILURE_CODE_ROLES = (
    "asset_failure_runner", "assets", "common", "generator",
    "layout_runner", "production", "run_stage3",
)
ASSET_FAILURE_SPECS = {
    "missing": {
        "injection_point": "remove_published_object",
        "recovery_action": "restore_missing_object",
        "resolver_error": "missing", "recovery_error": None,
        "before": "available", "final_status": "available",
        "event_visible": True, "orphan_count": 0,
        "object_exists": False, "publish_attempt": False, "publish_error": None,
    },
    "corrupt": {
        "injection_point": "modify_published_bytes",
        "recovery_action": "replace_corrupt_object",
        "resolver_error": "corrupt", "recovery_error": None,
        "before": "available", "final_status": "available",
        "event_visible": True, "orphan_count": 0,
        "object_exists": True, "publish_attempt": False, "publish_error": None,
    },
    "metadata_mismatch": {
        "injection_point": "replace_catalog_metadata",
        "recovery_action": "restore_catalog_metadata",
        "resolver_error": "metadata_mismatch", "recovery_error": None,
        "before": "available", "final_status": "available",
        "event_visible": True, "orphan_count": 0,
        "object_exists": True, "publish_attempt": False, "publish_error": None,
    },
    "upload_then_db_failure": {
        "injection_point": "fail_after_object_upload",
        "recovery_action": "remove_orphan_object",
        "resolver_error": "missing", "recovery_error": "missing",
        "before": "absent", "final_status": "absent",
        "event_visible": False, "orphan_count": 1,
        "object_exists": True, "publish_attempt": True, "publish_error": None,
    },
    "publish_failure": {
        "injection_point": "fail_pending_publication",
        "recovery_action": "confirm_failed_publication",
        "resolver_error": "failed", "recovery_error": "failed",
        "before": "pending", "final_status": "failed",
        "event_visible": True, "orphan_count": 0,
        "object_exists": False, "publish_attempt": True, "publish_error": "failed",
    },
    "delete_failure": {
        "injection_point": "fail_deleting_object_removal",
        "recovery_action": "confirm_delete_failure_state",
        "resolver_error": "deleting", "recovery_error": "deleting",
        "before": "available", "final_status": "deleting",
        "event_visible": True, "orphan_count": 0,
        "object_exists": True, "publish_attempt": False, "publish_error": None,
    },
}
ASSET_FAILURE_SUMMARY_FIELDS = frozenset({
    "case_order", "engines", "format", "format_version", "statistics_boundary",
})
ASSET_FAILURE_ENGINE_FIELDS = frozenset({
    "cases", "child", "cleanup", "code", "engine", "namespace_policy", "run_id", "runtime",
})
ASSET_FAILURE_CASE_FIELDS = frozenset({
    "asset_id", "case", "catalog_transitions", "cleanup", "event_visible", "execution_error",
    "final_status", "injection_point", "namespace", "orphan_count",
    "orphan_count_after_recovery", "recovery_actions", "recovery_resolver", "reconcile",
    "reconcile_after_recovery", "resolver", "sha256", "store_observation", "validation_errors",
})
ASSET_FAILURE_RUNTIME_FIELDS = frozenset({
    "container", "endpoint", "engine", "engine_runtime", "host", "layout", "operation",
})
ASSET_FAILURE_POLICY_FIELDS = frozenset({
    "case_order", "namespace_prefix", "namespaces", "reuse", "strategy",
})
ASSET_FAILURE_RUN_CLEANUP_FIELDS = frozenset({
    "namespaces", "namespaces_removed", "object_directories_removed",
    "runtime_probe_directory", "runtime_probe_directory_removed",
})
ASSET_FAILURE_RUNTIMES = {
    "opengauss": {
        "endpoint": {"host": "127.0.0.1", "port": 15432},
        "container": {
            "container": "agent-trace-opengauss-v6",
            "image": "enmotech/opengauss:6.0.0",
            "image_id": "sha256:" + "6" * 64,
        },
        "engine_runtime": {"source": "database-query", "version": "openGauss 6.0.0"},
    },
    "clickhouse": {
        "endpoint": {"host": "127.0.0.1", "port": 18123},
        "container": {
            "container": "agent-trace-clickhouse-25-12",
            "image": "clickhouse/clickhouse-server:25.12",
            "image_id": "sha256:" + "0" * 64,
        },
        "engine_runtime": {"source": "database-query", "version": "25.12.11.4"},
    },
}


def asset_failure_resolver(error, asset_id, content):
    """构造 resolver / recovery_resolver 证据，按可见性决定内容字段。"""
    if error is None:
        return {
            "content_length": len(content), "content_visible": True,
            "error": None, "preview": content, "sha256": asset_id,
        }
    return {
        "content_length": None, "content_visible": False,
        "error": error, "preview": None, "sha256": None,
    }


def asset_failure_result(engine, case):
    """构造一个与生产 child schema 一致的最小 asset-failure case 结果。"""
    spec = ASSET_FAILURE_SPECS[case]
    asset_id = hashlib.sha256(f"{engine}:{case}".encode("utf-8")).hexdigest()
    namespace = f"jsons3_af_{case}_{asset_id[:10]}"
    root = f"/runs/{engine}/child/asset-failure-cases/{case}"
    object_path = f"{root}/objects/{asset_id[:2]}/{asset_id}"
    content = '{"content":"asset failure %s"}' % case
    return {
        "asset_id": asset_id,
        "case": case,
        "catalog_transitions": [
            {
                "after": spec["final_status"], "asset_id": asset_id,
                "before": spec["before"], "phase": "injection", "sha256": asset_id,
            },
            {
                "after": spec["final_status"], "asset_id": asset_id,
                "before": spec["final_status"], "phase": "recovery", "sha256": asset_id,
            },
        ],
        "cleanup": {
            "adapter_cleanup_target": f"{namespace}_asset_ref",
            "errors": [],
            "namespace": namespace,
            "namespace_removed": True,
            "object_directory": f"{root}/objects",
            "object_directory_removed": True,
        },
        "event_visible": spec["event_visible"],
        "execution_error": None,
        "final_status": spec["final_status"],
        "injection_point": spec["injection_point"],
        "namespace": namespace,
        "reconcile": {
            "orphan_count": spec["orphan_count"],
            "orphan_paths": [object_path] if spec["orphan_count"] else [],
        },
        "reconcile_after_recovery": {"orphan_count": 0, "orphan_paths": []},
        "recovery_actions": [spec["recovery_action"]],
        "recovery_resolver": asset_failure_resolver(spec["recovery_error"], asset_id, content),
        "resolver": asset_failure_resolver(spec["resolver_error"], asset_id, content),
        "sha256": asset_id,
        "store_observation": {
            "object_exists": spec["object_exists"],
            "object_path": object_path,
            "publish_attempts": [
                {
                    "asset_id": asset_id, "error": spec["publish_error"],
                    "final_object_exists": spec["object_exists"],
                }
            ] if spec["publish_attempt"] else [],
        },
        "validation_errors": [],
    }


def asset_failure_child(engine):
    """构造一个与生产 child schema 一致的最小 asset-failure manifest。"""
    return {
        "case_order": list(ASSET_FAILURE_CASES),
        "format": "agent-trace-json-storage-stage3-asset-failure-run",
        "format_version": 1,
        "results": [asset_failure_result(engine, case) for case in ASSET_FAILURE_CASES],
        "run_id": f"jsons3-asset-failures-{engine}",
        "status": "complete",
    }


def asset_failure_envelope(engine, content, child):
    """构造一个与生产 envelope schema 一致的最小 asset-failure 运行结果。"""
    namespaces = [result["namespace"] for result in child["results"]]
    return {
        "asset_failures": {
            "case_order": list(child["case_order"]),
            "cleanup": {"namespaces_removed": True, "object_directories_removed": True},
            "engine": engine,
            "namespaces": namespaces,
            "results": json.loads(json.dumps(child["results"])),
        },
        "child": {
            "bytes": len(content),
            "format": child["format"],
            "format_version": child["format_version"],
            "path": "child/run-manifest.json",
            "run_id": child["run_id"],
            "sha256": hashlib.sha256(content).hexdigest(),
            "status": child["status"],
        },
        "cleanup": {
            "namespaces": namespaces,
            "namespaces_removed": True,
            "object_directories_removed": True,
            "runtime_probe_directory": f"/runs/{engine}/runtime-probe-assets",
            "runtime_probe_directory_removed": True,
        },
        "code": {
            role: {"path": f"/source/{role}.py", "bytes": 10, "sha256": character * 64}
            for role, character in zip(ASSET_FAILURE_CODE_ROLES, "1234567")
        },
        "command": ["python3", "run_stage3.py", "asset-failures", "--engine", engine],
        "format": "agent-trace-json-storage-stage3-production-run",
        "format_version": 1,
        "namespace_policy": {
            "case_order": list(child["case_order"]),
            "namespace_prefix": "jsons3_af_<case>_",
            "namespaces": namespaces,
            "reuse": False,
            "strategy": "runner-fixed-case-unique",
        },
        "operation": "asset-failures",
        "run_id": f"jsons3-production-asset-failures-{engine}",
        "runtime": {
            "operation": "asset-failures",
            "engine": engine,
            "layout": "asset_ref",
            "host": {
                "platform": "Linux", "machine": "x86_64",
                "cpu_count": 8, "memory_total_kib": 16291948,
            },
            **json.loads(json.dumps(ASSET_FAILURE_RUNTIMES[engine])),
        },
        "status": "complete",
    }


def write_asset_failure_run(root, engine, child=None):
    """写入一个最小 asset-failure production 目录并返回其路径。"""
    run = Path(root) / engine
    (run / "child").mkdir(parents=True)
    child = asset_failure_child(engine) if child is None else child
    content = json.dumps(child, separators=(",", ":")).encode("utf-8")
    (run / "child" / "run-manifest.json").write_bytes(content)
    envelope = asset_failure_envelope(engine, content, child)
    (run / "run-manifest.json").write_text(json.dumps(envelope), encoding="utf-8")
    return run


def rewrite_asset_failure_child(run, mutate):
    """改写 child 并同步 envelope 中由实际 bytes 与结果形成的镜像证据。"""
    path = Path(run) / "child" / "run-manifest.json"
    child = json.loads(path.read_text(encoding="utf-8"))
    mutate(child)
    content = json.dumps(child, separators=(",", ":")).encode("utf-8")
    path.write_bytes(content)
    envelope_path = Path(run) / "run-manifest.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["child"]["bytes"] = len(content)
    envelope["child"]["sha256"] = hashlib.sha256(content).hexdigest()
    for key in ("format", "format_version", "status", "run_id"):
        if key in child:
            envelope["child"][key] = child[key]
    envelope["asset_failures"]["results"] = json.loads(json.dumps(child["results"]))
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    return child


def rewrite_asset_failure_envelope(run, mutate):
    """改写 asset-failure production envelope。"""
    path = Path(run) / "run-manifest.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return envelope


class StageThreeAssetFailureSummaryTest(unittest.TestCase):
    """验证双引擎 asset-failure 控制只汇总分类与证据，不产出任何时延统计。"""

    def runs(self, directory):
        """写入两个引擎各一个最小正式 asset-failure 目录。"""
        return [write_asset_failure_run(directory, engine) for engine in ASSET_FAILURE_ENGINES]

    def test_summarizes_two_engine_controls_in_fixed_case_order(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            summary = report.summarize_asset_failures(runs)
            self.assertEqual(
                summary["format"], "agent-trace-json-storage-stage3-asset-failures-summary"
            )
            self.assertEqual(summary["format_version"], 1)
            self.assertEqual(summary["case_order"], list(ASSET_FAILURE_CASES))
            self.assertEqual(
                [item["engine"] for item in summary["engines"]], list(ASSET_FAILURE_ENGINES)
            )
            for item in summary["engines"]:
                self.assertEqual(
                    [case["case"] for case in item["cases"]], list(ASSET_FAILURE_CASES)
                )
                self.assertEqual(item["runtime"]["layout"], "asset_ref")
                self.assertEqual(item["runtime"]["engine"], item["engine"])
            self.assertEqual(summary, report.summarize_asset_failures(list(reversed(runs))))

    def test_carries_classification_and_recovery_evidence_per_case(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = report.summarize_asset_failures(self.runs(directory))
            for item in summary["engines"]:
                cases = {case["case"]: case for case in item["cases"]}
                for name, spec in ASSET_FAILURE_SPECS.items():
                    case = cases[name]
                    self.assertEqual(case["resolver"]["error"], spec["resolver_error"])
                    self.assertEqual(case["final_status"], spec["final_status"])
                    self.assertEqual(case["event_visible"], spec["event_visible"])
                    self.assertEqual(case["orphan_count"], spec["orphan_count"])
                    self.assertEqual(case["orphan_count_after_recovery"], 0)
                    self.assertEqual(case["injection_point"], spec["injection_point"])
                    self.assertEqual(case["recovery_actions"], [spec["recovery_action"]])
                    self.assertEqual(
                        case["recovery_resolver"]["error"], spec["recovery_error"]
                    )
                    self.assertEqual(
                        case["store_observation"]["object_exists"], spec["object_exists"]
                    )
                    self.assertEqual(case["validation_errors"], [])
                    self.assertIsNone(case["execution_error"])
                    self.assertTrue(case["cleanup"]["namespace_removed"])
                    self.assertEqual(len(case["catalog_transitions"]), 2)

    def test_publishes_exactly_the_contracted_keys_at_every_level(self):
        """该控制的设计边界禁止时延统计与引擎排名：逐层键集须与契约完全一致。"""
        with tempfile.TemporaryDirectory() as directory:
            summary = report.summarize_asset_failures(self.runs(directory))
            self.assertEqual(set(summary), set(ASSET_FAILURE_SUMMARY_FIELDS))
            for item in summary["engines"]:
                self.assertEqual(set(item), set(ASSET_FAILURE_ENGINE_FIELDS))
                self.assertEqual(set(item["runtime"]), set(ASSET_FAILURE_RUNTIME_FIELDS))
                self.assertEqual(set(item["namespace_policy"]), set(ASSET_FAILURE_POLICY_FIELDS))
                self.assertEqual(set(item["cleanup"]), set(ASSET_FAILURE_RUN_CLEANUP_FIELDS))
                self.assertEqual(
                    set(item["child"]),
                    {"bytes", "format", "format_version", "path", "run_id", "sha256", "status"},
                )
                self.assertEqual(set(item["code"]), set(ASSET_FAILURE_CODE_ROLES))
                for evidence in item["code"].values():
                    self.assertEqual(set(evidence), {"bytes", "path", "sha256"})
                for case in item["cases"]:
                    self.assertEqual(set(case), set(ASSET_FAILURE_CASE_FIELDS))
                    for name in ("resolver", "recovery_resolver"):
                        self.assertEqual(
                            set(case[name]),
                            {"content_length", "content_visible", "error", "preview", "sha256"},
                        )
                    for name in ("reconcile", "reconcile_after_recovery"):
                        self.assertEqual(set(case[name]), {"orphan_count", "orphan_paths"})
                    for transition in case["catalog_transitions"]:
                        self.assertEqual(
                            set(transition), {"after", "asset_id", "before", "phase", "sha256"}
                        )
                    observation = case["store_observation"]
                    self.assertEqual(
                        set(observation), {"object_exists", "object_path", "publish_attempts"}
                    )
                    for attempt in observation["publish_attempts"]:
                        self.assertEqual(
                            set(attempt), {"asset_id", "error", "final_object_exists"}
                        )
                    self.assertEqual(
                        set(case["cleanup"]),
                        {
                            "adapter_cleanup_target", "errors", "namespace", "namespace_removed",
                            "object_directory", "object_directory_removed",
                        },
                    )

    def test_carries_optional_input_and_truth_identity_when_published(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            for run in runs:
                rewrite_asset_failure_envelope(
                    run,
                    lambda envelope: envelope.update({
                        "input": json.loads(json.dumps(INPUT_IDENTITY)),
                        "truth": {
                            "seed": 20260907, "identity_sha256": IDENTITY,
                            "record_count": 48534, "block_size": 256, "block_count": 190,
                        },
                    }),
                )
            summary = report.summarize_asset_failures(runs)
            self.assertEqual(summary["input"], INPUT_IDENTITY)
            self.assertEqual(summary["truth"]["record_count"], 48534)

    def test_rejects_input_identity_drift_between_engines(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            for index, run in enumerate(runs):
                rewrite_asset_failure_envelope(
                    run,
                    lambda envelope, index=index: envelope.__setitem__(
                        "input", json.loads(json.dumps(INPUT_IDENTITY))
                    ),
                )
            rewrite_asset_failure_envelope(
                runs[1],
                lambda envelope: envelope["input"]["events"].__setitem__("sha256", "9" * 64),
            )
            with self.assertRaisesRegex(ValueError, "input identity differs between runs"):
                report.summarize_asset_failures(runs)


class StageThreeAssetFailureGateTest(unittest.TestCase):
    """验证 asset-failure 控制的覆盖、身份、分类与证据门禁全部 fail closed。"""

    def runs(self, directory):
        return [write_asset_failure_run(directory, engine) for engine in ASSET_FAILURE_ENGINES]

    def reject(self, mutate, message):
        """写入双引擎 fixture，施加指定的反例后要求门禁拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            mutate(runs)
            with self.assertRaisesRegex(ValueError, message):
                report.summarize_asset_failures(runs)

    def test_rejects_missing_duplicated_or_extra_engine(self):
        with self.assertRaisesRegex(ValueError, "at least one asset-failure run directory"):
            report.summarize_asset_failures([])
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            with self.assertRaisesRegex(ValueError, "engine coverage is incomplete"):
                report.summarize_asset_failures(runs[:1])
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            runs.append(write_asset_failure_run(Path(directory) / "duplicate", "opengauss"))
            with self.assertRaisesRegex(
                ValueError, "duplicate asset-failure engine: opengauss"
            ):
                report.summarize_asset_failures(runs)
        with tempfile.TemporaryDirectory() as directory:
            runs = self.runs(directory)
            extra = write_asset_failure_run(Path(directory) / "extra", "opengauss")
            rewrite_asset_failure_envelope(
                extra, lambda envelope: envelope["runtime"].__setitem__("engine", "duckdb")
            )
            runs.append(extra)
            with self.assertRaisesRegex(ValueError, "runtime identity mismatch"):
                report.summarize_asset_failures(runs)

    def test_rejects_envelope_format_status_and_operation_drift(self):
        cases = (
            ("format", "layout-matrix", "envelope format/version is invalid"),
            ("format_version", 2, "envelope format/version is invalid"),
            ("format_version", True, "envelope format/version is invalid"),
            ("status", "partial", "envelope status is not complete"),
            ("operation", "interference", "asset-failure operation is invalid"),
            ("run_id", "", "asset-failure run identity is missing"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                self.reject(
                    lambda runs, field=field, value=value: rewrite_asset_failure_envelope(
                        runs[1], lambda envelope: envelope.__setitem__(field, value)
                    ),
                    message,
                )

    def test_rejects_child_identity_and_format_mismatch(self):
        def rewrite_bytes_only(runs):
            path = runs[0] / "child" / "run-manifest.json"
            path.write_bytes(path.read_bytes() + b" ")

        def drop_child(runs):
            (runs[1] / "child" / "run-manifest.json").unlink()

        def escape_child(runs):
            rewrite_asset_failure_envelope(
                runs[0], lambda envelope: envelope["child"].__setitem__(
                    "path", "../../escape.json"
                ),
            )

        def declared_status(runs):
            rewrite_asset_failure_envelope(
                runs[1], lambda envelope: envelope["child"].__setitem__("status", "partial")
            )

        for mutate, message in (
            (rewrite_bytes_only, "child identity mismatch"),
            (drop_child, "child manifest is unavailable"),
            (escape_child, "child path escapes the run directory"),
            (declared_status, "child format/status mismatch"),
        ):
            with self.subTest(mutate=mutate.__name__):
                self.reject(mutate, message)

    def test_rejects_child_format_version_and_status_drift(self):
        cases = (
            ("format", "agent-trace-json-storage-stage3-interference-run"),
            ("format_version", 2),
            ("status", "partial"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self.reject(
                    lambda runs, field=field, value=value: rewrite_asset_failure_child(
                        runs[0], lambda child: child.__setitem__(field, value)
                    ),
                    "asset-failure child format/version/status is invalid",
                )

    def test_rejects_missing_duplicated_extra_or_reordered_case(self):
        def drop_case(child):
            child["case_order"] = child["case_order"][:-1]
            child["results"] = child["results"][:-1]

        def duplicate_case(child):
            child["case_order"][1] = child["case_order"][0]
            child["results"][1] = json.loads(json.dumps(child["results"][0]))

        def extra_case(child):
            child["case_order"].append("unknown")
            child["results"].append(json.loads(json.dumps(child["results"][0])))

        def reorder_case(child):
            child["case_order"][0], child["case_order"][1] = (
                child["case_order"][1], child["case_order"][0]
            )
            child["results"][0], child["results"][1] = (
                child["results"][1], child["results"][0]
            )

        def empty_results(child):
            child["results"] = []

        for mutate in (drop_case, duplicate_case, extra_case, reorder_case, empty_results):
            with self.subTest(mutate=mutate.__name__):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    "asset-failure case order mismatch",
                )

    def test_rejects_result_order_that_contradicts_the_declared_case_order(self):
        def swap_results(child):
            child["results"][0], child["results"][1] = (
                child["results"][1], child["results"][0]
            )

        self.reject(
            lambda runs: rewrite_asset_failure_child(runs[1], swap_results),
            "asset-failure case identity mismatch",
        )

    def test_rejects_namespace_not_bound_to_the_case_prefix(self):
        def rename(child):
            child["results"][2]["namespace"] = "jsons3_af_missing_0123456789"

        def rename_cleanup(child):
            child["results"][3]["cleanup"]["namespace"] = "jsons3_if_quiet_0123456789"

        for mutate in (rename, rename_cleanup):
            with self.subTest(mutate=mutate.__name__):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    "asset-failure namespace evidence is invalid",
                )

    def test_rejects_resolver_classification_drift(self):
        cases = (
            (0, "corrupt"), (1, "missing"), (2, None),
            (3, "corrupt"), (4, "deleting"), (5, "failed"),
        )
        for index, value in cases:
            with self.subTest(index=index, value=value):
                self.reject(
                    lambda runs, index=index, value=value: rewrite_asset_failure_child(
                        runs[index % 2],
                        lambda child: child["results"][index]["resolver"].__setitem__(
                            "error", value
                        ),
                    ),
                    "asset-failure resolver classification mismatch",
                )

    def test_rejects_final_status_event_visibility_and_orphan_drift(self):
        cases = (
            (
                lambda child: child["results"][0].__setitem__("final_status", "failed"),
                "asset-failure final status mismatch",
            ),
            (
                lambda child: child["results"][3].__setitem__("event_visible", True),
                "asset-failure event visibility mismatch",
            ),
            (
                lambda child: child["results"][0].__setitem__("event_visible", False),
                "asset-failure event visibility mismatch",
            ),
            (
                lambda child: child["results"][3].__setitem__(
                    "reconcile", {"orphan_count": 0, "orphan_paths": []}
                ),
                "asset-failure orphan evidence mismatch",
            ),
            (
                lambda child: child["results"][0].__setitem__(
                    "reconcile", {"orphan_count": 1, "orphan_paths": ["/objects/aa"]}
                ),
                "asset-failure orphan evidence mismatch",
            ),
            (
                lambda child: child["results"][3].__setitem__(
                    "reconcile_after_recovery",
                    {"orphan_count": 1, "orphan_paths": ["/objects/aa"]},
                ),
                "asset-failure orphan evidence mismatch",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    message,
                )

    def test_rejects_orphan_count_that_contradicts_the_listed_paths(self):
        def forge_count(child):
            child["results"][3]["reconcile"]["orphan_paths"] = []

        self.reject(
            lambda runs: rewrite_asset_failure_child(runs[1], forge_count),
            "asset-failure reconcile evidence is invalid",
        )

    def test_rejects_boolean_posing_as_orphan_count(self):
        def forge_bool(child):
            child["results"][3]["reconcile"]["orphan_count"] = True

        self.reject(
            lambda runs: rewrite_asset_failure_child(runs[0], forge_bool),
            "asset-failure reconcile evidence is invalid",
        )

    def test_rejects_non_boolean_event_visibility(self):
        def forge_visibility(child):
            child["results"][0]["event_visible"] = 1

        self.reject(
            lambda runs: rewrite_asset_failure_child(runs[0], forge_visibility),
            "asset-failure result evidence is incomplete",
        )

    def test_rejects_missing_result_keys_and_wrong_types(self):
        cases = (
            (lambda child: child["results"][0].pop("injection_point"), "result evidence is incomplete"),
            (lambda child: child["results"][0].__setitem__("asset_id", 1), "result evidence is incomplete"),
            (lambda child: child["results"][0].__setitem__("extra", 1), "result evidence is incomplete"),
            (lambda child: child["results"][0].__setitem__("resolver", []), "resolver evidence is invalid"),
            (lambda child: child["results"][0]["resolver"].pop("preview"), "resolver evidence is invalid"),
            (lambda child: child["results"][0]["store_observation"].pop("object_exists"), "store observation evidence is invalid"),
            (lambda child: child["results"][0]["cleanup"].pop("errors"), "case cleanup evidence is invalid"),
            (lambda child: child["results"][0].__setitem__("catalog_transitions", []), "catalog transition evidence is invalid"),
            (lambda child: child["results"][0].__setitem__("recovery_actions", []), "recovery evidence is invalid"),
            (lambda child: child["results"][0].__setitem__("recovery_actions", ["", "x"]), "recovery evidence is invalid"),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    message,
                )

    def test_rejects_resolver_content_that_contradicts_the_classification(self):
        cases = (
            lambda child: child["results"][0]["resolver"].__setitem__("content_visible", True),
            lambda child: child["results"][0]["resolver"].__setitem__("sha256", "9" * 64),
            lambda child: child["results"][0]["recovery_resolver"].__setitem__(
                "content_visible", False
            ),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    "asset-failure resolver evidence is invalid",
                )

    def test_rejects_catalog_transitions_that_do_not_chain_to_the_final_status(self):
        cases = (
            lambda child: child["results"][0]["catalog_transitions"][1].__setitem__(
                "before", "failed"
            ),
            lambda child: child["results"][0]["catalog_transitions"][1].__setitem__(
                "after", "deleting"
            ),
            lambda child: child["results"][0]["catalog_transitions"][0].__setitem__(
                "phase", "recovery"
            ),
            lambda child: child["results"][0]["catalog_transitions"][0].__setitem__(
                "asset_id", "9" * 64
            ),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    "asset-failure catalog transition evidence is invalid",
                )

    def test_rejects_validation_errors_and_execution_errors(self):
        cases = (
            (
                lambda child: child["results"][2].__setitem__(
                    "validation_errors", ["sha256 mismatch"]
                ),
                "asset-failure validation errors are present",
            ),
            (
                lambda child: child["results"][2].__setitem__("execution_error", "timeout"),
                "asset-failure execution error is present",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    message,
                )

    def test_rejects_incomplete_cleanup_evidence(self):
        cases = (
            (
                lambda runs: rewrite_asset_failure_child(
                    runs[0],
                    lambda child: child["results"][4]["cleanup"].__setitem__(
                        "namespace_removed", False
                    ),
                ),
                "asset-failure case cleanup evidence is invalid",
            ),
            (
                lambda runs: rewrite_asset_failure_child(
                    runs[0],
                    lambda child: child["results"][4]["cleanup"].__setitem__(
                        "errors", ["drop failed"]
                    ),
                ),
                "asset-failure case cleanup evidence is invalid",
            ),
            (
                lambda runs: rewrite_asset_failure_envelope(
                    runs[1],
                    lambda envelope: envelope["cleanup"].__setitem__(
                        "namespaces_removed", False
                    ),
                ),
                "asset-failure cleanup evidence is invalid",
            ),
            (
                lambda runs: rewrite_asset_failure_envelope(
                    runs[1],
                    lambda envelope: envelope["cleanup"].__setitem__("namespaces", []),
                ),
                "asset-failure cleanup evidence is invalid",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(mutate, message)

    def test_rejects_namespace_policy_drift(self):
        cases = (
            lambda envelope: envelope["namespace_policy"].__setitem__(
                "namespace_prefix", "jsons3_if_<phase>_"
            ),
            lambda envelope: envelope["namespace_policy"].__setitem__("reuse", True),
            lambda envelope: envelope["namespace_policy"].__setitem__(
                "strategy", "unique-random-suffix"
            ),
            lambda envelope: envelope["namespace_policy"].__setitem__(
                "namespaces", envelope["namespace_policy"]["namespaces"][:-1]
            ),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_envelope(runs[0], mutate),
                    "asset-failure namespace policy evidence is invalid",
                )

    def test_rejects_envelope_results_that_differ_from_the_child(self):
        cases = (
            lambda envelope: envelope["asset_failures"]["results"][0].__setitem__(
                "final_status", "failed"
            ),
            lambda envelope: envelope["asset_failures"].__setitem__("results", []),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_envelope(runs[0], mutate),
                    "asset-failure envelope and child evidence differ",
                )

    def test_rejects_envelope_asset_failure_block_drift(self):
        cases = (
            lambda envelope: envelope["asset_failures"].__setitem__("engine", "clickhouse"),
            lambda envelope: envelope["asset_failures"].pop("namespaces"),
            lambda envelope: envelope["asset_failures"]["cleanup"].__setitem__(
                "namespaces_removed", False
            ),
            lambda envelope: envelope["asset_failures"]["case_order"].reverse(),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_envelope(runs[0], mutate),
                    "asset-failure envelope evidence is invalid",
                )

    def test_rejects_incomplete_code_provenance(self):
        cases = (
            lambda envelope: envelope["code"].pop("asset_failure_runner"),
            lambda envelope: envelope["code"]["assets"].__setitem__("sha256", "zz"),
            lambda envelope: envelope["code"]["common"].__setitem__("bytes", 0),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_envelope(runs[0], mutate),
                    "asset-failure code evidence is incomplete",
                )

    def test_rejects_duplicated_run_identity_between_engines(self):
        def share_envelope_run_id(runs):
            envelope = json.loads((runs[0] / "run-manifest.json").read_text(encoding="utf-8"))
            rewrite_asset_failure_envelope(
                runs[1], lambda other: other.__setitem__("run_id", envelope["run_id"])
            )

        def share_child_run_id(runs):
            child = json.loads(
                (runs[0] / "child" / "run-manifest.json").read_text(encoding="utf-8")
            )
            rewrite_asset_failure_child(
                runs[1], lambda other: other.__setitem__("run_id", child["run_id"])
            )
            rewrite_asset_failure_envelope(
                runs[1], lambda envelope: envelope["child"].__setitem__(
                    "run_id", child["run_id"]
                ),
            )

        for mutate in (share_envelope_run_id, share_child_run_id):
            with self.subTest(mutate=mutate.__name__):
                self.reject(mutate, "run identity is duplicated between runs")

    def test_rejects_recovery_evidence_that_contradicts_the_case(self):
        cases = (
            (
                lambda child: child["results"][0].__setitem__(
                    "recovery_resolver",
                    {
                        "content_length": None, "content_visible": False,
                        "error": "missing", "preview": None, "sha256": None,
                    },
                ),
                "asset-failure recovery classification mismatch",
            ),
            (
                lambda child: child["results"][3]["recovery_resolver"].__setitem__(
                    "error", "corrupt"
                ),
                "asset-failure recovery classification mismatch",
            ),
            (
                lambda child: child["results"][0].__setitem__("injection_point", "whatever"),
                "asset-failure injection point mismatch",
            ),
            (
                lambda child: child["results"][0].__setitem__(
                    "injection_point", "modify_published_bytes"
                ),
                "asset-failure injection point mismatch",
            ),
            (
                lambda child: child["results"][0].__setitem__(
                    "recovery_actions", ["did_nothing"]
                ),
                "asset-failure recovery action mismatch",
            ),
            (
                lambda child: child["results"][4].__setitem__(
                    "recovery_actions",
                    ["confirm_failed_publication", "confirm_failed_publication"],
                ),
                "asset-failure recovery action mismatch",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    message,
                )

    def test_rejects_store_observation_that_contradicts_the_case(self):
        cases = (
            lambda child: child["results"][0]["store_observation"].__setitem__(
                "object_exists", True
            ),
            lambda child: child["results"][1]["store_observation"].__setitem__(
                "object_exists", False
            ),
            lambda child: child["results"][4]["store_observation"].__setitem__(
                "publish_attempts", []
            ),
            lambda child: child["results"][0]["store_observation"]["publish_attempts"].append(
                {
                    "asset_id": child["results"][0]["asset_id"],
                    "error": None, "final_object_exists": False,
                }
            ),
        )
        for index, mutate in enumerate(cases):
            with self.subTest(index=index):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_child(runs[0], mutate),
                    "asset-failure store observation evidence is invalid",
                )

    def test_rejects_injection_starting_state_that_contradicts_the_case(self):
        cases = ((0, "deleting"), (3, "available"), (4, "available"))
        for index, value in cases:
            with self.subTest(index=index, value=value):
                self.reject(
                    lambda runs, index=index, value=value: rewrite_asset_failure_child(
                        runs[0],
                        lambda child: child["results"][index]["catalog_transitions"][0].__setitem__(
                            "before", value
                        ),
                    ),
                    "asset-failure catalog transition evidence is invalid",
                )

    def test_rejects_namespaces_reused_between_engines(self):
        def reuse_namespaces(runs):
            source = json.loads(
                (runs[0] / "child" / "run-manifest.json").read_text(encoding="utf-8")
            )
            names = [result["namespace"] for result in source["results"]]

            def rename(child):
                for result, namespace in zip(child["results"], names):
                    result["namespace"] = namespace
                    result["cleanup"]["namespace"] = namespace
                    result["cleanup"]["adapter_cleanup_target"] = f"{namespace}_asset_ref"

            rewrite_asset_failure_child(runs[1], rename)
            rewrite_asset_failure_envelope(runs[1], lambda envelope: [
                envelope["cleanup"].__setitem__("namespaces", list(names)),
                envelope["namespace_policy"].__setitem__("namespaces", list(names)),
                envelope["asset_failures"].__setitem__("namespaces", list(names)),
            ])

        self.reject(reuse_namespaces, "asset-failure namespaces are reused between runs")

    def test_rejects_unknown_fields_in_envelope_code_and_namespace_policy(self):
        cases = (
            (
                lambda envelope: envelope["code"].__setitem__(
                    "bogus",
                    {
                        "path": "/source/bogus.py", "bytes": 10,
                        "sha256": "8" * 64, "p99_ms": 12.5,
                    },
                ),
                "asset-failure code evidence is incomplete",
            ),
            (
                lambda envelope: envelope["code"]["assets"].__setitem__("p99_ms", 12.5),
                "asset-failure code evidence is incomplete",
            ),
            (
                lambda envelope: envelope["namespace_policy"].__setitem__(
                    "query_complete_p99", 12.5
                ),
                "asset-failure namespace policy evidence is invalid",
            ),
        )
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(
                    lambda runs, mutate=mutate: rewrite_asset_failure_envelope(runs[0], mutate),
                    message,
                )


COMBINED_FAMILY_OPTIONS = ("--matrix", "--part-state", "--interference", "--asset-failure")
COMBINED_TRUTH = {
    "seed": 20260907, "identity_sha256": IDENTITY,
    "record_count": 48534, "block_size": 256, "block_count": 190,
}


def combined_interference_summary(**overrides):
    """构造 CLI 装配所需的最小 interference 汇总子树。"""
    summary = {
        "format": "agent-trace-json-storage-stage3-interference-summary",
        "format_version": 1,
        "input": json.loads(json.dumps(INPUT_IDENTITY)),
        "truth": json.loads(json.dumps(COMBINED_TRUTH)),
        "query_catalog_sha256": "f" * 64,
        "statistics_boundary": "raw-samples-retained-p99-requires-1000-successes",
        "phase_order": list(INTERFERENCE_PHASES),
        "layouts": [],
    }
    summary.update(overrides)
    return summary


def combined_run_tree(directory):
    """写入四族各自的最小正式运行目录，返回 CLI 需要的显式目录清单。"""
    root = Path(directory)
    target, _, _ = formal_target(root / "matrix")
    interference = root / "interference"
    for layout in LAYOUTS:
        (interference / layout).mkdir(parents=True)
    return {
        "matrix": [target],
        "part-state": [
            write_part_state_run(root / "part-states", layout) for layout in LAYOUTS
        ],
        "interference": [interference / layout for layout in LAYOUTS],
        "asset-failure": [
            write_asset_failure_run(root / "asset-failures", engine)
            for engine in ASSET_FAILURE_ENGINES
        ],
    }


class StageThreeCombinedSummaryCliTest(unittest.TestCase):
    """验证组合汇总 CLI 的族覆盖、目录显式性、身份绑定与发布边界。"""

    def argv(self, output, families, skip=None):
        """按显式目录清单构造一次 CLI 调用参数。"""
        argv = ["--output", str(output)]
        for option, runs in families.items():
            if option == skip:
                continue
            argv.extend([f"--{option}", *(str(run) for run in runs)])
        return argv

    def run_cli(self, argv, interference=None):
        """执行 CLI，返回退出码与 stderr 文本；interference 族按需注入子树。"""
        stderr = io.StringIO()
        summary = combined_interference_summary() if interference is None else interference
        seen = []

        def stub(runs):
            seen.append([Path(run) for run in runs])
            return json.loads(json.dumps(summary))

        with patch.object(report, "summarize_interference", side_effect=stub), \
             contextlib.redirect_stderr(stderr):
            try:
                code = report.main(argv)
            except SystemExit as error:
                code = error.code
        return code, stderr.getvalue(), seen

    def test_publishes_four_family_trees_bound_by_the_identity_each_family_carries(self):
        """组合结果必须按族分树发布，并只绑定各族实际携带的身份字段。"""
        with tempfile.TemporaryDirectory() as directory:
            families = combined_run_tree(directory)
            output = Path(directory) / "combined-summary.json"
            code, stderr, seen = self.run_cli(self.argv(output, families))
            self.assertEqual((code, stderr), (0, ""))
            summary = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(
                set(summary),
                {
                    "format", "format_version", "input", "truth", "query_catalog_sha256",
                    "identity_binding", "matrix", "part_states", "interference",
                    "asset_failures",
                },
            )
            self.assertEqual(
                summary["format"], "agent-trace-json-storage-stage3-combined-summary"
            )
            self.assertEqual(summary["format_version"], 1)
            self.assertEqual(summary["input"], INPUT_IDENTITY)
            self.assertEqual(summary["truth"], COMBINED_TRUTH)
            self.assertEqual(summary["query_catalog_sha256"], "f" * 64)
            self.assertEqual(
                summary["matrix"]["format"],
                "agent-trace-json-storage-stage3-matrix-summary",
            )
            self.assertEqual(
                summary["part_states"]["format"],
                "agent-trace-json-storage-stage3-part-states-summary",
            )
            self.assertEqual(
                summary["interference"]["format"],
                "agent-trace-json-storage-stage3-interference-summary",
            )
            self.assertEqual(
                summary["asset_failures"]["format"],
                "agent-trace-json-storage-stage3-asset-failures-summary",
            )
            # interference 族只接收调用方列出的目录，CLI 不得自行发现运行目录。
            self.assertEqual(seen, [families["interference"]])

    def test_declares_the_asset_failure_tree_unbound_with_its_reason(self):
        """正式 asset-failure envelope 不发布输入身份，组合结果须明示该豁免。"""
        with tempfile.TemporaryDirectory() as directory:
            families = combined_run_tree(directory)
            output = Path(directory) / "combined-summary.json"
            self.assertEqual(self.run_cli(self.argv(output, families))[0], 0)
            summary = json.loads(output.read_text(encoding="utf-8"))
            binding = summary["identity_binding"]
            self.assertEqual(binding["input"], ["matrix", "part_states", "interference"])
            self.assertEqual(binding["truth"], ["part_states", "interference"])
            self.assertEqual(
                binding["query_catalog_sha256"], ["part_states", "interference"]
            )
            self.assertEqual(set(binding["unbound"]), {"asset_failures"})
            self.assertIn("publish", binding["unbound"]["asset_failures"])
            for field in ("input", "truth", "query_catalog_sha256"):
                self.assertNotIn(field, summary["asset_failures"])

    def test_rejects_a_missing_family_and_publishes_nothing(self):
        """缺任一族即失败，且必须指明缺失的族。"""
        for option in ("matrix", "part-state", "interference", "asset-failure"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as directory:
                families = combined_run_tree(directory)
                output = Path(directory) / "combined-summary.json"
                code, stderr, _ = self.run_cli(self.argv(output, families, skip=option))
                self.assertNotEqual(code, 0)
                self.assertIn(f"--{option}", stderr)
                self.assertFalse(output.exists())

    def test_rejects_a_directory_that_is_not_a_run(self):
        with tempfile.TemporaryDirectory() as directory:
            families = combined_run_tree(directory)
            output = Path(directory) / "combined-summary.json"
            empty = Path(directory) / "empty"
            empty.mkdir()
            families["matrix"] = [empty]
            code, stderr, _ = self.run_cli(self.argv(output, families))
            self.assertNotEqual(code, 0)
            self.assertIn("run-manifest.json", stderr)
            self.assertFalse(output.exists())

    def test_refuses_to_scan_a_parent_directory_for_attempts(self):
        """失败与被取代的尝试与有效运行并列存放，父目录只能被拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            families = combined_run_tree(directory)
            output = Path(directory) / "combined-summary.json"
            parents = {
                "matrix": Path(directory) / "matrix",
                "part-state": Path(directory) / "part-states",
                "asset-failure": Path(directory) / "asset-failures",
            }
            for option, parent in parents.items():
                with self.subTest(option=option):
                    selected = dict(families, **{option: [parent]})
                    code, stderr, _ = self.run_cli(self.argv(output, selected))
                    self.assertNotEqual(code, 0)
                    self.assertNotEqual(stderr, "")
                    self.assertFalse(output.exists())

    def test_rejects_input_identity_drift_between_bound_families(self):
        with tempfile.TemporaryDirectory() as directory:
            families = combined_run_tree(directory)
            output = Path(directory) / "combined-summary.json"
            for run in families["part-state"]:
                rewrite_part_state_envelope(
                    run,
                    lambda envelope: envelope["input"]["events"].__setitem__(
                        "sha256", "9" * 64
                    ),
                )
            code, stderr, _ = self.run_cli(self.argv(output, families))
            self.assertNotEqual(code, 0)
            self.assertIn("input", stderr)
            self.assertFalse(output.exists())

    def test_rejects_truth_and_query_catalog_drift_between_bound_families(self):
        """truth 与 query catalog 只在 part-state 和 interference 之间绑定。"""
        cases = (
            ("truth", {"truth": dict(COMBINED_TRUTH, block_count=189)}),
            ("query_catalog_sha256", {"query_catalog_sha256": "9" * 64}),
        )
        for field, override in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                families = combined_run_tree(directory)
                output = Path(directory) / "combined-summary.json"
                code, stderr, _ = self.run_cli(
                    self.argv(output, families),
                    interference=combined_interference_summary(**override),
                )
                self.assertNotEqual(code, 0)
                self.assertIn(field, stderr)
                self.assertFalse(output.exists())

    def test_refuses_to_overwrite_an_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            families = combined_run_tree(directory)
            output = Path(directory) / "combined-summary.json"
            output.write_text("{}", encoding="utf-8")
            code, stderr, seen = self.run_cli(self.argv(output, families))
            self.assertNotEqual(code, 0)
            self.assertIn("already exists", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "{}")
            self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
