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
WATERMARK_TARGETS = {
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
                    "watermarks": {name: 10 for name in WATERMARK_TARGETS[layout]},
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
                        for name in WATERMARK_TARGETS[layout]
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


if __name__ == "__main__":
    unittest.main()
