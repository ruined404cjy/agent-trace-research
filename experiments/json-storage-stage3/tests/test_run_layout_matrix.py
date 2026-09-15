import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import (
    AccessEvidence, BlockResult, CleanupResult, DatasetAudit, MaintenanceResult,
    QueryResult, QuerySpec, StorageEvidence, logical_response_bytes,
)
from run_layout_matrix import (
    QueryTruth, RunConfig, build_smoke_input, build_workload_events, latin_square,
    load_run_input, measure_query, run_layout, summarize_samples,
    validate_formal_contract, validate_watermark_keys, workload_query_cases,
    write_manifest_atomic,
)


SHA_ABC = hashlib.sha256(b"abc").hexdigest()


def logical_row(payload=b"abc"):
    """返回 adapter 公共查询契约的一条完整逻辑行。"""
    return {
        "event_id": "event-a",
        "trace_id": "trace-a",
        "project_id": "project-a",
        "start_time": "2030-01-01T00:00:00.000Z",
        "profile": "text_64k",
        "content_type": "application/json",
        "encoding": "utf-8",
        "content_length": 3,
        "preview": "abc",
        "sha256": SHA_ABC,
        "payload": payload,
    }


class OneResultAdapter:
    """只替换数据库边界，返回调用方指定的完整 QueryResult。"""

    def __init__(self, row, response_bytes=19):
        self.row = row
        self.response_bytes = response_bytes

    def run_query(self, query):
        return QueryResult(
            "query-detail-1", (self.row,), self.response_bytes,
            self.response_bytes, 0, 0.25, 0.10,
        )


class SmokeAdapter:
    """按 QuerySpec 执行内存过滤，用于验证 runner 编排而非数据库驱动。"""

    layout = "same_table"

    def __init__(self, events, input_root, cleanup_removed=True):
        self.events = events
        self.input_root = input_root
        self.cleanup_removed = cleanup_removed
        self.query_ids = []
        self.query_kinds = {}
        self.ingested = []

    def create(self):
        return {"schema": "jsons3_fake_same_table", "ddl": "CREATE TABLE events"}

    def ingest_block(self, block):
        self.ingested.extend(block)
        watermark = block[-1]["ingest_seq"] + 1
        return BlockResult(
            len(block), watermark, {"events": watermark}, 0.5,
            database_submitted_bytes=128, write_target_ms={"events": 0.4},
        )

    def wait_write_complete(self, watermark):
        return MaintenanceResult(True, {"events": watermark})

    def wait_query_ready(self, timeout_seconds):
        return MaintenanceResult(True, {"events": len(self.events)}, ({"analyzed": True},), 1.0)

    def _payload(self, row):
        path = row.get("payload_path")
        return None if path is None else (self.input_root / path).read_bytes()

    def run_query(self, query):
        rows = list(self.ingested)
        params = query.parameters
        if query.kind in {"list", "preview"}:
            rows = [row for row in rows if (
                row["project_id"] == params["project_id"]
                and params["start_time"] <= row["start_time"] < params["end_time"]
                and (row["start_time"], row["event_id"])
                > (params["cursor_time"], params["cursor_id"])
            )]
            rows = sorted(rows, key=lambda row: (row["start_time"], row["event_id"]))[
                :params["page_size"]
            ]
        elif query.kind == "detail":
            rows = [row for row in rows if all(row[key] == params[key] for key in (
                "project_id", "trace_id", "start_time", "event_id",
            ))]
        elif query.kind == "trace":
            rows = [row for row in rows if (
                row["project_id"] == params["project_id"]
                and row["trace_id"] == params["trace_id"]
                and params["start_time"] <= row["start_time"] < params["end_time"]
            )]
            rows.sort(key=lambda row: (row["start_time"], row["event_id"]))
        else:
            rows = [row for row in rows if row["cohort"] == params["cohort"]]
            rows.sort(key=lambda row: (row["start_time"], row["event_id"]))
        projected = []
        for row in rows:
            item = {key: row[key] for key in (
                "event_id", "trace_id", "project_id", "start_time", "profile",
                "content_type", "encoding", "content_length", "preview", "sha256",
            )}
            if query.kind == "list":
                item["preview"] = None
            item["payload"] = (
                self._payload(row) if query.kind in {"detail", "trace", "batch"} else None
            )
            projected.append(item)
        query_id = f"query-{len(self.query_ids)}"
        self.query_ids.append(query_id)
        self.query_kinds[query_id] = query.kind
        payload_bytes = sum(len(row["payload"] or b"") for row in projected)
        database_bytes = max(1, len(json.dumps(
            projected, default=lambda value: value.decode("utf-8"),
        ).encode("utf-8")))
        return QueryResult(
            query_id, tuple(projected), database_bytes, database_bytes, 0, 0.2, 0.1,
        )

    def collect_storage(self):
        return StorageEvidence({"events": {"total_bytes": 4096}})

    def collect_access_evidence(self, query_ids):
        return AccessEvidence(
            {query_id: "Index Scan using events_list_idx" for query_id in query_ids},
            {"events_list_idx": len(query_ids)},
            query_details={
                query_id: {
                    "kind": self.query_kinds[query_id],
                    "statement": "SELECT payload FROM events",
                    "payload_selected": self.query_kinds[query_id] in {"detail", "trace", "batch"},
                    "declared_source": "events",
                }
                for query_id in query_ids
            },
        )

    def audit_dataset(self):
        rows = tuple({
            "ingest_seq": row["ingest_seq"],
            **{key: row[key] for key in (
                "event_id", "trace_id", "project_id", "start_time", "profile",
                "content_type", "encoding", "content_length", "preview", "sha256",
            )},
            "payload": self._payload(row),
        } for row in self.ingested)
        return DatasetAudit(rows, 0, logical_response_bytes(rows))

    def cleanup(self):
        return CleanupResult("jsons3_fake_same_table", self.cleanup_removed)


class LayoutMatrixUnitTest(unittest.TestCase):
    """验证主矩阵的顺序、客户端校验、汇总和发布门禁。"""

    def test_latin_square_places_every_layout_once_in_every_position(self):
        rows = latin_square(("same_table", "separate", "full_core", "asset_ref"))
        self.assertEqual(rows[0], ("same_table", "separate", "full_core", "asset_ref"))
        self.assertEqual(rows[3], ("asset_ref", "same_table", "separate", "full_core"))
        self.assertEqual(len(rows), 4)
        for position in range(4):
            self.assertEqual({row[position] for row in rows}, {
                "same_table", "separate", "full_core", "asset_ref",
            })

    def test_detail_sample_requires_received_bytes_and_application_validation(self):
        expected = logical_row()
        sample = measure_query(
            OneResultAdapter(expected), QuerySpec("detail", {}),
            QueryTruth("detail:text_64k", (expected,)),
        )
        self.assertEqual(sample.response_bytes, 19)
        self.assertLessEqual(sample.query_complete_ms, sample.application_ready_ms)
        self.assertEqual(sample.validation.sha256, SHA_ABC)
        self.assertEqual(sample.validation.validated_payload_bytes, 3)
        self.assertEqual(sample.status, "success")

    def test_server_digest_cannot_replace_payload_bytes(self):
        expected = logical_row()
        sample = measure_query(
            OneResultAdapter(logical_row(payload=None)), QuerySpec("detail", {}),
            QueryTruth("detail:text_64k", (expected,)),
        )
        self.assertEqual(sample.status, "failed")
        self.assertIn("payload bytes", sample.error)
        self.assertEqual(sample.validation.validated_payload_bytes, 0)

    def test_failed_samples_remain_counted_but_are_excluded_from_statistics(self):
        expected = logical_row()
        successful = measure_query(
            OneResultAdapter(expected), QuerySpec("detail", {}),
            QueryTruth("detail:text_64k", (expected,)),
        )
        failed = measure_query(
            OneResultAdapter(logical_row(payload=b"bad")), QuerySpec("detail", {}),
            QueryTruth("detail:text_64k", (expected,)),
        )
        summary = summarize_samples((successful, failed))
        self.assertEqual(summary["detail:text_64k"]["sample_count"], 2)
        self.assertEqual(summary["detail:text_64k"]["successful_samples"], 1)
        self.assertEqual(summary["detail:text_64k"]["failed_samples"], 1)
        self.assertEqual(summary["detail:text_64k"]["latency_ms"]["minimum"],
                         successful.application_ready_ms)

    def test_batch_throughput_uses_validated_client_payload_bytes(self):
        first = logical_row(b"abc")
        second = {**logical_row(b"defg"), "event_id": "event-b", "content_length": 4,
                  "preview": "defg", "sha256": hashlib.sha256(b"defg").hexdigest()}
        result = QueryResult("query-batch-1", (first, second), 100, 100, 0, 0.3, 0.1)
        adapter = OneResultAdapter(first)
        adapter.run_query = lambda query: result
        sample = measure_query(
            adapter, QuerySpec("batch", {"cohort": "main"}),
            QueryTruth("batch:main", (first, second)),
        )
        summary = summarize_samples((sample,))["batch:main"]
        self.assertEqual(sample.validation.validated_payload_bytes, 7)
        expected = 7 / (1024 * 1024) / (sample.application_ready_ms / 1000)
        self.assertAlmostEqual(summary["throughput_mib_s"]["median"], expected)

    def test_atomic_manifest_replaces_running_with_complete_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-manifest.json"
            write_manifest_atomic(path, {"status": "running", "run_id": "one"})
            self.assertEqual(json.loads(path.read_text())["status"], "running")
            write_manifest_atomic(path, {"status": "complete", "run_id": "one"})
            self.assertEqual(json.loads(path.read_text()), {
                "run_id": "one", "status": "complete",
            })
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_input_loader_rejects_generation_contract_that_disagrees_with_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            build_smoke_input(root)
            path = root / "generation-manifest.json"
            manifest = json.loads(path.read_text())
            manifest["record_count"] = 9
            write_manifest_atomic(path, manifest)
            with self.assertRaisesRegex(ValueError, "generation contract"):
                load_run_input(root)

    def test_run_layout_gates_every_block_and_publishes_all_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            output = root / "output"
            build_smoke_input(input_root)
            truth, events, identity = load_run_input(input_root)
            adapter = SmokeAdapter(events, input_root)
            result = run_layout(adapter, truth, RunConfig(
                input_root=input_root, output=output, engine="fake",
                layout="same_table", round_index=0,
                round_order=("same_table", "separate", "full_core", "asset_ref"),
                measurements=1, batch_measurements=1, input_identity=identity,
            ))
            manifest = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["write"]["block_count"], truth.block_count)
            self.assertEqual(manifest["write"]["final_watermark"], truth.record_count)
            self.assertGreaterEqual(manifest["ddl_create_ms"], 0)
            self.assertEqual(set(manifest["write"]["write_target_ms"]), {"events"})
            self.assertEqual(manifest["write"]["asset_publish_ms"], 0)
            self.assertIn("watermark_wait_ms", manifest["maintenance"])
            self.assertTrue(manifest["maintenance"]["completed"])
            self.assertTrue(manifest["cleanup"]["removed"])
            self.assertEqual(set(manifest["storage"]["tables"]), {"events"})
            self.assertEqual(len(manifest["code"]["runner"]["sha256"]), 64)
            self.assertTrue(manifest["engine_runtime"]["version"])
            self.assertEqual(len(manifest["access"]["plans"]), len(result.samples))
            self.assertTrue(all(sample.status == "success" for sample in result.samples))
            self.assertTrue(all(sample.response_bytes > 0 for sample in result.samples))
            self.assertTrue((output / "samples.jsonl").is_file())
            self.assertTrue((output / "result.json").is_file())

    def test_cleanup_failure_keeps_manifest_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root = root / "input"
            output = root / "output"
            build_smoke_input(input_root)
            truth, events, identity = load_run_input(input_root)
            adapter = SmokeAdapter(events, input_root, cleanup_removed=False)
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                run_layout(adapter, truth, RunConfig(
                    input_root=input_root, output=output, engine="fake",
                    layout="same_table", round_index=0,
                    round_order=("same_table", "separate", "full_core", "asset_ref"),
                    measurements=1, batch_measurements=1, input_identity=identity,
                ))
            manifest = json.loads((output / "run-manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(manifest["cleanup"]["removed"])

    def test_empty_asset_workspace_parent_tree_is_removed(self):
        from run_layout_matrix import remove_empty_asset_workspace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".asset-work"
            (root / "opengauss" / "asset_ref").mkdir(parents=True)
            remove_empty_asset_workspace(root)
            self.assertFalse(root.exists())

    def test_failed_round_record_preserves_cleanup_evidence(self):
        from run_layout_matrix import failed_round_record

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            round_dir = root / "engine" / "layout" / "rounds" / "round-1"
            write_manifest_atomic(round_dir / "run-manifest.json", {
                "status": "failed", "run_id": "failed-one",
                "cleanup": {"namespace": "jsons3_failed", "removed": True},
            })
            record = failed_round_record(round_dir, 2, root)
            self.assertEqual(record["status"], "failed")
            self.assertTrue(record["cleanup"]["removed"])
            self.assertEqual(record["position"], 2)

    def test_unknown_formal_format_and_reduced_measurements_are_rejected(self):
        frozen = {
            "format": "agent-trace-json-storage-stage3-generation",
            "format_version": 1, "seed": 20260907, "record_count": 48534,
            "block_size": 256, "block_count": 190,
        }
        with self.assertRaisesRegex(ValueError, "format"):
            validate_formal_contract({**frozen, "format": "unknown"}, object(), 30, 5)
        with self.assertRaisesRegex(ValueError, "30/5"):
            validate_formal_contract(frozen, object(), 2, 1)

    def test_workloads_project_non_target_payloads_to_null_and_select_scenarios(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            build_smoke_input(root)
            truth, events, _ = load_run_input(root)
            main = build_workload_events(events, "main")
            few = build_workload_events(events, "equal_total_few_large")
            many = build_workload_events(events, "equal_total_many_medium")
            self.assertEqual(sum(row["payload_path"] is not None for row in main), 4)
            self.assertEqual(sum(row["payload_path"] is not None for row in few), 1)
            self.assertEqual(sum(row["payload_path"] is not None for row in many), 1)
            self.assertTrue(all(
                row["preview"] is None and row["sha256"] is None
                for source, row in zip(events, few)
                if source["profile"] != "text_2m" or source["cohort"] != "equal_total_control"
            ))
            main_scenarios = {item[1].scenario for item in workload_query_cases(
                main, truth, root, "main",
            )}
            control_scenarios = {item[1].scenario for item in workload_query_cases(
                few, truth, root, "equal_total_few_large",
            )}
            self.assertTrue(any(name.startswith("preview:") for name in main_scenarios))
            self.assertTrue(any(name.startswith("trace:") for name in main_scenarios))
            self.assertFalse(any(name.startswith("preview:") for name in control_scenarios))
            self.assertFalse(any(name.startswith("trace:") for name in control_scenarios))
            self.assertEqual(sum(name.startswith("detail:") for name in control_scenarios), 1)

    def test_watermark_gate_requires_exact_layout_targets(self):
        with self.assertRaisesRegex(RuntimeError, "watermark keys"):
            validate_watermark_keys("separate", {"events_analytics": 8}, 8)

    def test_logical_response_bytes_do_not_depend_on_engine_protocol_encoding(self):
        from common import logical_response_bytes

        rows = (logical_row(),)
        logical = logical_response_bytes(rows, include_payload=True)
        self.assertEqual(logical, logical_response_bytes(rows, include_payload=True))
        self.assertGreater(logical, len(b"abc"))

    def test_smoke_input_validation_failure_publishes_failed_target_manifests(self):
        from argparse import Namespace
        from run_layout_matrix import run_matrix

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output = root / "input", root / "output"
            build_smoke_input(input_root)
            (input_root / "truth.json").write_text("{}")
            arguments = Namespace(
                input=input_root, output=output, engines="opengauss,clickhouse",
                layouts="same_table,separate,full_core,asset_ref", workloads="main",
                measurements=2, batch_measurements=1, query_ready_timeout=30,
                opengauss_host="127.0.0.1", opengauss_port=15432,
                opengauss_container="agent-trace-opengauss-v6",
                clickhouse_host="127.0.0.1", clickhouse_port=18123,
                clickhouse_container="agent-trace-clickhouse-25-12",
            )
            with self.assertRaises(ValueError):
                run_matrix(arguments)
            for engine in ("opengauss", "clickhouse"):
                for layout in ("same_table", "separate", "full_core", "asset_ref"):
                    manifest = json.loads((output / engine / layout / "run-manifest.json").read_text())
                    self.assertEqual(manifest["status"], "failed")
                    self.assertEqual(manifest["error_category"], "input_validation")


if __name__ == "__main__":
    unittest.main()
