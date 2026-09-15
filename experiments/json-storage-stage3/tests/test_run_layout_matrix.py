import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import (
    AccessEvidence, BlockResult, CleanupResult, DatasetAudit, MaintenanceResult,
    PhysicalTargetAudit, QueryResult, QuerySpec, StorageEvidence, logical_response_bytes,
)
import run_layout_matrix as runner
from run_layout_matrix import (
    QueryTruth, RunConfig, build_smoke_input, build_workload_events, latin_square,
    _validate_dataset_audit, load_run_input, measure_query, run_layout, summarize_samples,
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


def access_statement(payload_expression, preview_expression="preview", source="jsons3_test.events"):
    """构造与固定 adapter 投影一致、仅 payload 列可变的 SQL。"""
    fields = (
        "event_id", "trace_id", "project_id", "start_time", "profile", "content_type",
        "encoding", "content_length", "preview", "sha256",
    )
    projection = [
        (preview_expression + " AS preview") if field == "preview" else field + " AS " + field
        for field in fields
    ]
    projection.append(payload_expression + " AS payload_value")
    return "SELECT " + ",".join(projection) + " FROM " + source + " WHERE project_id=%s"


def clickhouse_access_statement(payload_expression, preview_expression,
                                source="jsons3_test.events"):
    """构造 ClickHouse 固定别名投影，供 SQL 门禁反例使用。"""
    fields = (
        "event_id", "trace_id", "project_id", "start_time", "profile", "content_type",
        "encoding", "content_length", "preview", "sha256",
    )
    projection = [
        (preview_expression + " AS preview") if field == "preview" else field + " AS " + field
        for field in fields
    ]
    projection.append(payload_expression + " AS payload_value")
    return "SELECT " + ",".join(projection) + " FROM " + source + " WHERE project_id={project_id:String}"


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
        self.query_statements = {}
        self.ingested = []

    def create(self):
        return {"schema": "jsons3_fake_same_table", "ddl": "CREATE TABLE events"}

    def ingest_block(self, block):
        self.ingested.extend(block)
        watermark = block[-1]["ingest_seq"] + 1
        return BlockResult(
            len(block), watermark, {"events": watermark}, 0.5,
            write_target_ms={"events": 0.4},
            logical_target_row_bytes={"events": 17},
            database_ingest_request_body_bytes={"events": None},
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
        fields = [
            "event_id", "trace_id", "project_id", "start_time", "profile",
            "content_type", "encoding", "content_length",
            "NULL AS preview" if query.kind == "list" else "preview", "sha256",
            "NULL AS payload_value" if query.kind in {"list", "preview"} else "payload AS payload_value",
        ]
        self.query_statements[query_id] = "SELECT " + ",".join(fields) + " FROM events WHERE project_id=%s"
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
                    "statement": self.query_statements[query_id],
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
        fields = (
            "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
            "project_id", "start_time", "end_time", "duration_ms", "span_type",
            "framework", "level", "cohort", "profile", "content_type", "encoding",
            "content_length", "preview", "sha256",
        )
        return DatasetAudit(
            rows, 0, logical_response_bytes(rows), target_audits={
                "events": PhysicalTargetAudit(
                    tuple({field: row[field] for field in fields} for row in self.ingested), 0,
                ),
            },
        )

    def cleanup(self):
        return CleanupResult("jsons3_fake_same_table", self.cleanup_removed)


class FailingIngestEvidenceAdapter(SmokeAdapter):
    """第二个 block 失败，并发布实际已发送的 transport bytes。"""

    def __init__(self, events, input_root):
        super().__init__(events, input_root)
        self.ingest_calls = 0
        self.failure_evidence = {}

    def ingest_block(self, block):
        self.ingest_calls += 1
        if self.ingest_calls == 2:
            self.failure_evidence = {"events": 13}
            raise RuntimeError("injected ingest failure")
        self.ingested.extend(block)
        watermark = block[-1]["ingest_seq"] + 1
        return BlockResult(
            len(block), watermark, {"events": watermark}, 0.5,
            write_target_ms={"events": 0.4},
            logical_target_row_bytes={"events": 17},
            database_ingest_request_body_bytes={"events": 11},
        )

    def ingest_failure_evidence(self):
        return dict(self.failure_evidence)


class FailingWarmupAdapter(SmokeAdapter):
    """在完整写入证据形成后注入查询失败。"""

    def run_query(self, query):
        raise RuntimeError("injected warmup failure")


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

    def test_formal_access_rejects_adapter_claim_when_list_sql_selects_payload(self):
        """捕获 adapter 自报 list 无 payload、实际 SQL 却选择 payload 的情况。"""
        sample = SimpleNamespace(
            query_id="list-1", kind="list", scenario="list:first", response_bytes=9,
            validation=SimpleNamespace(row_count=1),
        )
        access = AccessEvidence(
            {"list-1": "Index Scan using events_list_idx (actual time=0.1..0.2 rows=1 loops=1)\nBuffers: shared hit=1"},
            {"events_list_idx": 1},
            query_details={"list-1": {
                "kind": "list", "statement": access_statement("payload", "NULL::text"),
                "payload_selected": False, "declared_source": "events",
            }},
        )
        with self.assertRaisesRegex(RuntimeError, "projection"):
            runner._validate_access(
                "opengauss", "same_table", "formal", (sample,), access, "jsons3_test",
            )

    def test_formal_access_rejects_index_seen_by_another_query(self):
        """捕获累计 idx_scan 非零却不能证明当前 SQL 使用索引的情况。"""
        sample = SimpleNamespace(
            query_id="detail-1", kind="detail", scenario="detail:text_64k", response_bytes=9,
            validation=SimpleNamespace(row_count=1),
        )
        access = AccessEvidence(
            {"detail-1": "Seq Scan on events (actual time=0.1..0.2 rows=1 loops=1)\nBuffers: shared hit=1"},
            {"events_list_idx": 99},
            query_details={"detail-1": {
                "kind": "detail", "statement": access_statement("payload"),
                "payload_selected": True, "declared_source": "events",
            }},
        )
        with self.assertRaisesRegex(RuntimeError, "access structure"):
            runner._validate_access(
                "opengauss", "same_table", "formal", (sample,), access, "jsons3_test",
            )

    def test_formal_clickhouse_access_rejects_mergetree_full_scan(self):
        """捕获仅出现 MergeTree 字样、没有 primary-key/mark 裁剪的全扫计划。"""
        sample = SimpleNamespace(
            query_id="trace-1", kind="trace", scenario="trace:p50", response_bytes=9,
            validation=SimpleNamespace(row_count=1),
        )
        access = AccessEvidence(
            {"trace-1": "ReadFromMergeTree (events)"},
            query_finish={"trace-1": {"read_rows": 100, "read_bytes": 1000}},
            query_details={"trace-1": {
                "kind": "trace", "statement": clickhouse_access_statement("payload", "preview"),
                "payload_selected": True, "declared_source": "events",
            }},
        )
        with self.assertRaisesRegex(RuntimeError, "access structure"):
            runner._validate_access(
                "clickhouse", "same_table", "formal", (sample,), access, "jsons3_test",
            )

    def test_access_rejects_opengauss_composite_null_projection(self):
        """捕获 COALESCE 形式的 NULL 投影仍实际读取 payload。"""
        statement = access_statement(
            "COALESCE(payload,NULL::text)", "NULL::text", "jsons3_actual.events",
        )
        with self.assertRaisesRegex(RuntimeError, "projection"):
            runner._validate_sql_contract(
                "opengauss", "same_table", "list", statement, "jsons3_actual",
            )

    def test_access_rejects_clickhouse_composite_null_projection(self):
        """捕获 if 形式的 NULL 投影仍实际读取 payload。"""
        statement = clickhouse_access_statement(
            "if(payload != '',payload,CAST(NULL AS Nullable(String)))",
            "CAST(NULL AS Nullable(String))", "jsons3_actual.events",
        )
        with self.assertRaisesRegex(RuntimeError, "projection"):
            runner._validate_sql_contract(
                "clickhouse", "same_table", "list", statement, "jsons3_actual",
            )

    def test_access_rejects_opengauss_wrong_qualified_namespace(self):
        """捕获同名 events 表位于错误 schema 时的来源混淆。"""
        statement = access_statement("NULL::text", "NULL::text", "wrong_schema.events")
        with self.assertRaisesRegex(RuntimeError, "source"):
            runner._validate_sql_contract(
                "opengauss", "same_table", "list", statement, "jsons3_actual",
            )

    def test_access_rejects_clickhouse_wrong_qualified_namespace(self):
        """捕获同名 events 表位于错误 database 时的来源混淆。"""
        statement = clickhouse_access_statement(
            "CAST(NULL AS Nullable(String))", "CAST(NULL AS Nullable(String))",
            "wrong_database.events",
        )
        with self.assertRaisesRegex(RuntimeError, "source"):
            runner._validate_sql_contract(
                "clickhouse", "same_table", "list", statement, "jsons3_actual",
            )

    def test_access_accepts_separate_payload_projection_from_payload_alias(self):
        """捕获 separate 查询把 p.payload 错当作 analytics 表字段而拒绝。"""
        fields = (
            "event_id", "trace_id", "project_id", "start_time", "profile", "content_type",
            "encoding", "content_length", "preview", "sha256",
        )
        statement = (
            "SELECT " + ",".join("a." + field + " AS " + field for field in fields)
            + " ,p.payload AS payload_value FROM jsons3_actual.events_analytics a "
            "LEFT JOIN jsons3_actual.event_payloads p ON p.event_id=a.event_id "
            "WHERE a.project_id=%s"
        )
        runner._validate_sql_contract(
            "opengauss", "separate", "detail", statement, "jsons3_actual",
        )

    def test_summary_keeps_each_workload_scenario_independent(self):
        """捕获 main 与控制 workload 的同名 list scenario 被汇总到同一计数。"""
        expected = logical_row()
        sample = measure_query(
            OneResultAdapter(expected), QuerySpec("list", {}), QueryTruth("list:first", (expected,)),
        )
        summary = runner.summarize_workload_samples({
            "main": (sample, sample, sample, sample),
            "equal_total_few_large": (sample, sample, sample, sample),
            "equal_total_many_medium": (sample, sample, sample, sample),
            "correctness_only": (sample,),
        })
        self.assertEqual(summary["main"]["list:first"]["sample_count"], 4)
        self.assertEqual(summary["equal_total_few_large"]["list:first"]["sample_count"], 4)
        self.assertEqual(summary["equal_total_many_medium"]["list:first"]["sample_count"], 4)
        self.assertEqual(summary["correctness_only"]["list:first"]["sample_count"], 1)

    def test_measure_query_does_not_start_allocator_tracing(self):
        """捕获正式 application-ready 样本启用 tracemalloc 的计时干扰。"""
        expected = logical_row()
        calls = []
        original = runner.tracemalloc
        runner.tracemalloc = SimpleNamespace(
            start=lambda: calls.append("start"), stop=lambda: calls.append("stop"),
            get_traced_memory=lambda: (0, 123),
        )
        try:
            sample = measure_query(
                OneResultAdapter(expected), QuerySpec("detail", {}), QueryTruth("detail:text_64k", (expected,)),
            )
        finally:
            runner.tracemalloc = original
        self.assertEqual(sample.status, "success")
        self.assertEqual(calls, [])

    def test_batch_memory_diagnostic_validates_truth_and_labels_allocator_peak(self):
        """捕获未做 truth 门禁或将 tracemalloc 峰值写成 RSS 的诊断记录。"""
        expected = logical_row()
        tracer = SimpleNamespace(
            start=lambda: None, stop=lambda: None, get_traced_memory=lambda: (3, 123),
        )
        diagnostic = runner.measure_batch_memory_diagnostic(
            OneResultAdapter(expected), QuerySpec("batch", {"cohort": "main"}),
            QueryTruth("batch:main", (expected,)), tracer=tracer, clock=lambda: 1.0,
        )
        self.assertEqual(diagnostic["status"], "success")
        self.assertEqual(diagnostic["peak_memory_bytes"], 123)
        self.assertEqual(diagnostic["observation_method"], "python-tracemalloc-allocator-peak")
        failed = runner.measure_batch_memory_diagnostic(
            OneResultAdapter(logical_row(payload=b"bad")), QuerySpec("batch", {"cohort": "main"}),
            QueryTruth("batch:main", (expected,)), tracer=tracer, clock=lambda: 1.0,
        )
        self.assertEqual(failed["status"], "failed")

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

    def test_ingest_failure_manifest_preserves_all_handed_request_bodies(self):
        """捕获 failed manifest 遗漏成功 block 与失败 block 的已发送 bytes。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output = root / "input", root / "output"
            build_smoke_input(input_root)
            truth, events, identity = load_run_input(input_root)
            adapter = FailingIngestEvidenceAdapter(events, input_root)

            with self.assertRaisesRegex(RuntimeError, "injected ingest failure"):
                run_layout(adapter, truth, RunConfig(
                    input_root=input_root, output=output, engine="fake",
                    layout="same_table", round_index=0,
                    round_order=("same_table", "separate", "full_core", "asset_ref"),
                    measurements=1, batch_measurements=1, input_identity=identity,
                ))

            manifest = json.loads((output / "run-manifest.json").read_text())
            write = manifest.get("write", {})
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(
                write.get("database_ingest_request_body_bytes"), {"events": 24},
            )
            self.assertEqual(write.get("database_ingest_request_body_bytes_total"), 24)

    def test_post_ingest_failure_does_not_replace_complete_write_evidence(self):
        """捕获查询失败分支用部分 transport 统计覆盖完整 write 证据。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output = root / "input", root / "output"
            build_smoke_input(input_root)
            truth, events, identity = load_run_input(input_root)

            with self.assertRaisesRegex(RuntimeError, "query warmup failed"):
                run_layout(FailingWarmupAdapter(events, input_root), truth, RunConfig(
                    input_root=input_root, output=output, engine="fake",
                    layout="same_table", round_index=0,
                    round_order=("same_table", "separate", "full_core", "asset_ref"),
                    measurements=1, batch_measurements=1, input_identity=identity,
                ))

            write = json.loads((output / "run-manifest.json").read_text())["write"]
            self.assertEqual(write.get("logical_target_row_bytes"), {"events": 68})
            self.assertEqual(write.get("block_count"), 4)

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

    def test_dataset_audit_rejects_missing_middle_full_core_replica_row(self):
        """捕获 Core 中间行缺失但最大 ingest_seq 水位仍完整。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            build_smoke_input(root)
            _, events, _ = load_run_input(root)
            adapter = SmokeAdapter(events, root)
            adapter.ingested = list(events)
            logical = adapter.audit_dataset()
            fields = (
                "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
                "project_id", "start_time", "end_time", "duration_ms", "span_type",
                "framework", "level", "cohort", "profile", "content_type", "encoding",
                "content_length", "preview", "sha256",
            )
            complete = tuple({field: row[field] for field in fields} for row in events)
            missing_middle = complete[:3] + complete[4:]
            self.assertEqual(max(row["ingest_seq"] for row in missing_middle) + 1, len(events))
            audit = SimpleNamespace(
                rows=logical.rows,
                duplicate_event_ids=logical.duplicate_event_ids,
                logical_response_bytes=logical.logical_response_bytes,
                database_protocol_bytes=logical.database_protocol_bytes,
                target_audits={
                    "events_full": SimpleNamespace(rows=complete, duplicate_identities=0),
                    "events_core": SimpleNamespace(rows=missing_middle, duplicate_identities=0),
                },
            )

            with self.assertRaisesRegex(RuntimeError, "physical target audit"):
                _validate_dataset_audit(audit, events, root, "full_core")

    def test_dataset_audit_requires_unique_asset_catalog_and_all_event_mappings(self):
        """捕获内容寻址目录唯一却漏掉使用同一 digest 的事件映射。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            build_smoke_input(root)
            _, events, _ = load_run_input(root)
            duplicate = dict(events[1])
            duplicate.update({
                "sha256": events[0]["sha256"], "content_length": events[0]["content_length"],
                "content_type": events[0]["content_type"], "encoding": events[0]["encoding"],
                "preview": events[0]["preview"], "payload_path": events[0]["payload_path"],
            })
            events = [events[0], duplicate]
            adapter = SmokeAdapter(events, root)
            adapter.ingested = list(events)
            logical = adapter.audit_dataset()
            asset_id = events[0]["sha256"]
            audit = SimpleNamespace(
                rows=logical.rows,
                duplicate_event_ids=logical.duplicate_event_ids,
                logical_response_bytes=logical.logical_response_bytes,
                database_protocol_bytes=logical.database_protocol_bytes,
                target_audits={
                    "events_analytics": SimpleNamespace(
                        rows=tuple({
                            **{field: row[field] for field in (
                                "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
                                "project_id", "start_time", "end_time", "duration_ms", "span_type",
                                "framework", "level", "cohort", "profile", "content_type", "encoding",
                                "content_length", "preview", "sha256",
                            )},
                            "asset_id": row["sha256"],
                        } for row in events),
                        duplicate_identities=0,
                    ),
                    "assets": SimpleNamespace(
                        rows=({
                            "asset_id": asset_id, "sha256": asset_id,
                            "content_type": events[0]["content_type"], "encoding": events[0]["encoding"],
                            "content_length": events[0]["content_length"], "status": "available",
                        },),
                        duplicate_identities=0,
                        event_mappings=tuple({
                            "ingest_seq": row["ingest_seq"], "event_id": row["event_id"], "asset_id": asset_id,
                        } for row in events),
                    ),
                },
            )

            evidence = _validate_dataset_audit(audit, events, root, "asset_ref")
            self.assertEqual(evidence["physical_targets"]["assets"]["event_mapping_count"], 2)

    def test_dataset_audit_accepts_asset_catalog_sorted_by_two_unordered_digests(self):
        """捕获预期按事件顺序而 adapter 按 asset_id 返回时的假失败。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            build_smoke_input(root)
            _, source_events, _ = load_run_input(root)
            events = sorted(
                (row for row in source_events if row["sha256"] is not None),
                key=lambda row: row["sha256"], reverse=True,
            )[:2]
            self.assertEqual(len(events), 2)
            self.assertNotEqual(
                [row["sha256"] for row in events],
                sorted(row["sha256"] for row in events),
            )
            logical_rows = tuple({
                "ingest_seq": row["ingest_seq"],
                **{field: row[field] for field in (
                    "event_id", "trace_id", "project_id", "start_time", "profile",
                    "content_type", "encoding", "content_length", "preview", "sha256",
                )},
                "payload": (root / row["payload_path"]).read_bytes(),
            } for row in events)
            event_fields = (
                "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
                "project_id", "start_time", "end_time", "duration_ms", "span_type",
                "framework", "level", "cohort", "profile", "content_type", "encoding",
                "content_length", "preview", "sha256",
            )
            asset_rows = tuple(sorted(({
                "asset_id": row["sha256"], "sha256": row["sha256"],
                "content_type": row["content_type"], "encoding": row["encoding"],
                "content_length": row["content_length"], "status": "available",
            } for row in events), key=lambda row: row["asset_id"]))
            audit = SimpleNamespace(
                rows=logical_rows, duplicate_event_ids=0,
                logical_response_bytes=logical_response_bytes(logical_rows),
                database_protocol_bytes=None,
                target_audits={
                    "events_analytics": SimpleNamespace(
                        rows=tuple({
                            **{field: row[field] for field in event_fields},
                            "asset_id": row["sha256"],
                        } for row in events), duplicate_identities=0,
                    ),
                    "assets": SimpleNamespace(
                        rows=asset_rows, duplicate_identities=0,
                        event_mappings=tuple({
                            "ingest_seq": row["ingest_seq"], "event_id": row["event_id"],
                            "asset_id": row["sha256"],
                        } for row in events),
                    ),
                },
            )

            _validate_dataset_audit(audit, events, root, "asset_ref")

    def test_write_manifest_separates_logical_targets_and_unavailable_ingest_body_bytes(self):
        """捕获将 COPY 传输开销伪报为可观测 bytes，或把目标行统计合并到单一计数。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output = root / "input", root / "output"
            build_smoke_input(input_root)
            truth, events, identity = load_run_input(input_root)
            run_layout(SmokeAdapter(events, input_root), truth, RunConfig(
                input_root=input_root, output=output, engine="fake", layout="same_table",
                round_index=0, round_order=("same_table", "separate", "full_core", "asset_ref"),
                measurements=1, batch_measurements=1, input_identity=identity,
            ))
            write = json.loads((output / "run-manifest.json").read_text())["write"]
            self.assertEqual(write["logical_target_row_bytes"], {"events": 68})
            self.assertEqual(write["logical_target_row_bytes_total"], 68)
            self.assertEqual(write["database_ingest_request_body_bytes"], {"events": "unavailable"})
            self.assertEqual(write["database_ingest_request_body_bytes_total"], "unavailable")
            self.assertEqual(write["asset_raw_object_bytes"], 0)

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
