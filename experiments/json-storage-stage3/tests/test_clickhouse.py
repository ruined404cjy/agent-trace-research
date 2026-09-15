import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))
sys.path.insert(0, str(STAGE_DIR / "tests"))

import clickhouse
from assets import AssetError, AssetReference, AssetResolver, LocalAssetStore
from common import QuerySpec, build_layout_catalog
from test_opengauss import (
    CONTROL_PAYLOAD, EXPECTED_KEYS, PAYLOAD, FailAfterPublishStore, fixture,
)


class RecordingConnection:
    """记录实际交给 HTTP client 的 body，并按请求序号返回状态。"""

    def __init__(self, requests, statuses=()):
        self.requests = requests
        self.statuses = statuses

    def request(self, method, path, body, headers):
        self.requests.append({
            "method": method, "path": path, "body": body, "headers": headers,
        })

    def getresponse(self):
        index = len(self.requests) - 1
        status = self.statuses[index] if index < len(self.statuses) else 200

        class Response:
            def __init__(self, response_status):
                self.status = response_status

            def read(self):
                return b"" if self.status == 200 else b"injected failure"

        return Response(status)

    def close(self):
        return None


class ClickHouseAdapterUnitTest(unittest.TestCase):
    """验证 ClickHouse 四布局的 DDL、查询日志和物理证据 SQL。"""

    def test_ddls_use_fixed_codec_order_keys_and_database_asset_catalog(self):
        for layout in clickhouse.LAYOUTS:
            ddl = clickhouse.create_layout_ddl("jsons3_test", layout)
            self.assertIn("CREATE DATABASE jsons3_test_" + layout, ddl)
            for table in build_layout_catalog(layout).write_tables:
                self.assertIn("CREATE TABLE jsons3_test_" + layout + "." + table, ddl)
            self.assertIn("ORDER BY (project_id,start_time,event_id)", ddl)
            if layout != "asset_ref":
                self.assertIn("payload String CODEC(ZSTD(3))", ddl)
        asset = clickhouse.create_layout_ddl("jsons3_test", "asset_ref")
        self.assertIn("CREATE TABLE jsons3_test_asset_ref.assets", asset)
        for status in ("pending", "available", "failed", "deleting"):
            self.assertIn("'" + status + "'", asset)
        separate = clickhouse.create_layout_ddl("jsons3_test", "separate")
        payload_table = separate.split("CREATE TABLE jsons3_test_separate.event_payloads", 1)[1].split("ENGINE=MergeTree", 1)[0]
        self.assertIn("trace_id String", payload_table)
        self.assertIn("payload String CODEC(ZSTD(3))", payload_table)
        self.assertNotIn("span_type", payload_table)
        self.assertNotIn("duration_ms", payload_table)

    def test_evidence_sql_covers_parts_marks_columns_merges_and_query_finish(self):
        statements = "\n".join(clickhouse.storage_statements("jsons3_test_same_table"))
        self.assertIn("system.parts", statements)
        self.assertIn("marks", statements)
        self.assertIn("system.parts_columns", statements)
        self.assertIn("system.merges", statements)
        logs = clickhouse.query_finish_sql()
        self.assertIn("system.query_log", logs)
        self.assertIn("QueryFinish", logs)
        self.assertIn("query_id", logs)

    def test_readiness_sql_never_forces_final_merge(self):
        source = "\n".join(clickhouse.storage_statements("jsons3_test_same_table"))
        self.assertNotIn("OPTIMIZE", source.upper())
        self.assertNotIn("FINAL", source.upper())

    def test_batch_sql_binds_cohort_and_excludes_payloadless_rows(self):
        adapter = clickhouse.ClickHouseAdapter(
            "127.0.0.1", 18123, "unused", "jsons3_test", "same_table", Path("."),
        )
        statement, values = adapter._query_statement(QuerySpec("batch", {"cohort": "main"}))
        self.assertIn("cohort={cohort:String}", statement)
        self.assertIn("sha256 IS NOT NULL", statement)
        self.assertEqual(values, {"cohort": "main"})

    def test_ingest_counts_complete_post_bodies_and_asset_mutations_once(self):
        """捕获只统计 JSONEachRow 或漏计 Asset 状态 mutation 的传输 bytes。"""
        class Response:
            status = 200

            @staticmethod
            def read():
                return b""

        class Connection:
            def __init__(self, bodies):
                self.bodies = bodies

            def request(self, method, path, body, headers):
                self.bodies.append(body)

            @staticmethod
            def getresponse():
                return Response()

            @staticmethod
            def close():
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "unused", "jsons3_test", "asset_ref", root,
                LocalAssetStore(root / "assets"),
            )
            request_bodies = []
            adapter.connect_worker = lambda: Connection(request_bodies)

            block = adapter.ingest_block(fixture(root))

            asset_requests = [body for body in request_bodies if b".assets" in body]
            event_requests = [body for body in request_bodies if b".events_analytics" in body]
            self.assertEqual(len(asset_requests), 3)
            self.assertEqual(len(event_requests), 1)
            self.assertIn(b'"error_category":null', asset_requests[0])
            self.assertEqual(
                block.database_ingest_request_body_bytes["assets"],
                sum(len(body) for body in asset_requests),
            )
            self.assertEqual(
                block.database_ingest_request_body_bytes["events_analytics"], len(event_requests[0]),
            )
            self.assertTrue(all(
                body.startswith(b"INSERT INTO") or body.startswith(b"ALTER TABLE")
                for body in request_bodies
            ))

    def test_publish_failure_exposes_pending_and_failed_mutation_bodies(self):
        """捕获 failed mutation 成功发送后随原发布异常丢失的请求 bytes。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = []
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "unused", "jsons3_test", "asset_ref", root,
                FailAfterPublishStore(root / "assets"),
            )
            adapter.connect_worker = lambda: RecordingConnection(requests)

            with self.assertRaisesRegex(AssetError, "^failed$"):
                adapter.ingest_block(fixture(root)[:1])

            bodies = [request["body"] for request in requests]
            self.assertEqual(len(bodies), 2)
            self.assertTrue(bodies[0].startswith(b"INSERT INTO"))
            self.assertTrue(bodies[1].startswith(b"ALTER TABLE"))
            evidence = getattr(adapter, "ingest_failure_evidence", None)
            self.assertIsNotNone(evidence)
            self.assertEqual(
                evidence(), {"assets": sum(map(len, bodies))},
            )

    def test_available_mutation_failure_exposes_every_handed_asset_body(self):
        """捕获 available mutation 失败时只保留 pending INSERT bytes。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = []
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "unused", "jsons3_test", "asset_ref", root,
                LocalAssetStore(root / "assets"),
            )
            adapter.connect_worker = lambda: RecordingConnection(requests, (200, 500))

            with self.assertRaisesRegex(RuntimeError, "ClickHouse request failed"):
                adapter.ingest_block(fixture(root)[:1])

            bodies = [request["body"] for request in requests]
            self.assertEqual(len(bodies), 2)
            evidence = getattr(adapter, "ingest_failure_evidence", None)
            self.assertIsNotNone(evidence)
            self.assertEqual(
                evidence(), {"assets": sum(map(len, bodies))},
            )

    def test_status_connection_failure_does_not_count_an_unhanded_body(self):
        """捕获 status 连接建立失败时把未交给 HTTP client 的 body 计入证据。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = []
            connection_count = 0

            def connect_worker():
                nonlocal connection_count
                connection_count += 1
                if connection_count == 2:
                    raise OSError("injected connection failure")
                return RecordingConnection(requests)

            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "unused", "jsons3_test", "asset_ref", root,
                LocalAssetStore(root / "assets"),
            )
            adapter.connect_worker = connect_worker

            with self.assertRaisesRegex(OSError, "injected connection failure"):
                adapter.ingest_block(fixture(root)[:1])

            bodies = [request["body"] for request in requests]
            self.assertEqual(len(bodies), 1)
            evidence = getattr(adapter, "ingest_failure_evidence", None)
            self.assertIsNotNone(evidence)
            self.assertEqual(
                evidence(), {"assets": len(bodies[0])},
            )


@unittest.skipUnless(os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1",
                     "set RUN_CLICKHOUSE_INTEGRATION=1")
class ClickHouseAdapterIntegrationTest(unittest.TestCase):
    """以唯一 database 验证四布局真实写入、QueryFinish 和清理。"""

    def test_four_layouts_return_full_bytes_real_states_evidence_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)
            for layout in clickhouse.LAYOUTS:
                with self.subTest(layout=layout):
                    namespace = "jsons3_" + uuid.uuid4().hex[:10]
                    store = LocalAssetStore(root / (layout + "_assets"))
                    adapter = clickhouse.ClickHouseAdapter(
                        "127.0.0.1", 18123, "agent-trace-clickhouse-25-12",
                        namespace, layout, root, store,
                    )
                    self.addCleanup(adapter.cleanup)
                    adapter.create()
                    try:
                        block = adapter.ingest_block(rows)
                        self.assertEqual(block.watermark, 3)
                        self.assertEqual(
                            set(block.logical_target_row_bytes),
                            set(build_layout_catalog(layout).write_tables),
                        )
                        self.assertTrue(all(
                            value is not None for value in block.database_ingest_request_body_bytes.values()
                        ))
                        if layout == "same_table":
                            data = "".join(
                                json.dumps(adapter._event_row(row, adapter._payload(row), True),
                                           ensure_ascii=False, separators=(",", ":")) + "\n"
                                for row in rows
                            )
                            expected_body = (
                                f"INSERT INTO {adapter.database}.events FORMAT JSONEachRow\n"
                                + data
                            ).encode("utf-8")
                            self.assertEqual(
                                block.database_ingest_request_body_bytes["events"],
                                len(expected_body),
                            )
                        ready = adapter.wait_write_complete(3)
                        self.assertTrue(ready.completed)
                        self.assertEqual(set(ready.watermarks), set(build_layout_catalog(layout).write_tables))
                        maintained = adapter.wait_query_ready(timeout_seconds=30)
                        self.assertTrue(maintained.completed)
                        self.assertNotIn("optimize", str(maintained).lower())

                        listing = adapter.run_query(QuerySpec("list", {
                            "project_id": "project-a", "start_time": "2030-01-01T00:00:00.000Z",
                            "end_time": "2030-01-02T00:00:00.000Z", "page_size": 10,
                        }))
                        self.assertEqual([row["event_id"] for row in listing.rows], ["event-a", "event-b", "event-c"])
                        self.assertEqual(set(listing.rows[0]), EXPECTED_KEYS)
                        self.assertTrue(all(row["preview"] is None and row["payload"] is None
                                            for row in listing.rows))
                        preview = adapter.run_query(QuerySpec("preview", {
                            "project_id": "project-a", "start_time": "2030-01-01T00:00:00.000Z",
                            "end_time": "2030-01-02T00:00:00.000Z", "page_size": 10,
                        }))
                        self.assertEqual(preview.rows[0]["preview"], PAYLOAD.decode())
                        self.assertTrue(all(row["payload"] is None for row in preview.rows))

                        detail = adapter.run_query(QuerySpec("detail", {
                            "project_id": "project-a", "trace_id": "trace-a",
                            "start_time": "2030-01-01T00:00:00.000Z", "event_id": "event-a",
                        }))
                        self.assertEqual(detail.rows[0]["payload"], PAYLOAD)
                        self.assertEqual(set(detail.rows[0]), EXPECTED_KEYS)
                        trace = adapter.run_query(QuerySpec("trace", {
                            "project_id": "project-a", "trace_id": "trace-a",
                            "start_time": "2030-01-01T00:00:00.000Z",
                            "end_time": "2030-01-02T00:00:00.000Z",
                        }))
                        self.assertEqual([row["event_id"] for row in trace.rows], ["event-a", "event-b"])
                        self.assertEqual(trace.rows[0]["payload"], PAYLOAD)
                        self.assertIsNone(trace.rows[1]["payload"])
                        self.assertGreaterEqual(trace.response_bytes, len(PAYLOAD))
                        batch = adapter.run_query(QuerySpec("batch", {"cohort": "main"}))
                        self.assertEqual([row["event_id"] for row in batch.rows], ["event-a"])
                        self.assertEqual(batch.rows[0]["payload"], PAYLOAD)
                        self.assertEqual(set(batch.rows[0]), EXPECTED_KEYS)
                        self.assertEqual(batch.response_bytes,
                                         batch.database_response_bytes + batch.resolver_payload_bytes)

                        evidence = adapter.collect_access_evidence([detail.query_id])
                        self.assertIn(detail.query_id, evidence.query_finish)
                        self.assertEqual(evidence.query_finish[detail.query_id]["type"], "QueryFinish")
                        self.assertTrue(evidence.plans[detail.query_id])
                        storage = adapter.collect_storage()
                        self.assertEqual(set(storage.tables), set(build_layout_catalog(layout).write_tables))
                        self.assertTrue(all("marks" in value for value in storage.tables.values()))
                        audit = adapter.audit_dataset()
                        self.assertEqual(set(audit.target_audits), set(build_layout_catalog(layout).write_tables))
                        self.assertTrue(all(
                            target.duplicate_identities == 0 for target in audit.target_audits.values()
                        ))
                        if layout == "separate":
                            self.assertEqual(len(audit.target_audits["event_payloads"].rows), 3)
                        if layout == "full_core":
                            self.assertEqual(len(audit.target_audits["events_core"].rows), 3)

                        if layout == "asset_ref":
                            record = adapter.get_available(rows[0]["sha256"])
                            self.assertEqual(record.status, "available")
                            self.assertIsNotNone(storage.asset_store)
                            self.assertEqual(storage.asset_store.available_object_count, 2)
                            self.assertEqual(storage.asset_store.available_bytes,
                                             len(PAYLOAD) + len(CONTROL_PAYLOAD))
                            self.assertEqual(storage.asset_store.orphan_object_count, 0)
                            self.assertEqual(detail.resolver_payload_bytes, len(PAYLOAD))
                            self.assertGreater(detail.database_response_bytes, 0)
                            statement, parameters = adapter._query_statement(QuerySpec("detail", {
                                "project_id": "project-a", "trace_id": "trace-a",
                                "start_time": "2030-01-01T00:00:00.000Z", "event_id": "event-a",
                            }))
                            connection = adapter.connect_worker()
                            try:
                                event_body = adapter._request(
                                    connection, statement, parameters=parameters,
                                )
                            finally:
                                connection.close()
                            self.assertGreater(
                                detail.database_protocol_bytes, len(event_body.encode("utf-8")),
                            )
                            self.assertEqual(detail.response_bytes,
                                             detail.database_response_bytes + len(PAYLOAD))
                            self.assertEqual(len(audit.target_audits["assets"].rows), 2)
                            self.assertEqual(len(audit.target_audits["assets"].event_mappings), 2)
                            for status in ("pending", "failed", "deleting", "available"):
                                adapter.set_asset_status(record.asset_id, status)
                                actual = adapter.get_available(record.asset_id)
                                self.assertEqual(actual.status, status)
                                if status != "available":
                                    self.assertFalse(adapter.wait_write_complete(3).completed)
                                    self.assertFalse(adapter.wait_query_ready(timeout_seconds=30).completed)
                                    with self.assertRaisesRegex(AssetError, "^" + status + "$"):
                                        AssetResolver(adapter, store).resolve(
                                            AssetReference(ref="asset:sha256:" + record.asset_id,
                                                content_type=record.content_type,
                                                encoding=record.encoding,
                                                content_length=record.content_length,
                                                preview=rows[0]["preview"])
                                        )
                                else:
                                    self.assertTrue(adapter.wait_write_complete(3).completed)
                                    self.assertTrue(adapter.wait_query_ready(timeout_seconds=30).completed)
                    finally:
                        cleanup = adapter.cleanup()
                    self.assertTrue(cleanup.removed)
                    self.assertFalse(adapter.namespace_exists())

    def test_asset_catalog_deduplicates_digest_without_losing_event_mappings(self):
        """以真实 MergeTree 目录捕获 digest 去重后第二个事件映射丢失。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)[:2]
            rows[1].update({
                "cohort": "main", "profile": rows[0]["profile"],
                "content_type": rows[0]["content_type"], "encoding": rows[0]["encoding"],
                "content_length": rows[0]["content_length"], "preview": rows[0]["preview"],
                "sha256": rows[0]["sha256"], "payload_path": rows[0]["payload_path"],
            })
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "agent-trace-clickhouse-25-12",
                "jsons3_" + uuid.uuid4().hex[:10], "asset_ref", root,
                LocalAssetStore(root / "assets"),
            )
            adapter.create()
            try:
                adapter.ingest_block(rows)
                audit = adapter.audit_dataset().target_audits["assets"]
                self.assertEqual(len(audit.rows), 1)
                self.assertEqual([row["event_id"] for row in audit.event_mappings], ["event-a", "event-b"])
            finally:
                self.assertTrue(adapter.cleanup().removed)

    def test_failed_ingest_still_allows_confirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)
            rows[0]["sha256"] = "0" * 64
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "agent-trace-clickhouse-25-12",
                "jsons3_" + uuid.uuid4().hex[:10], "same_table", root,
            )
            adapter.create()
            try:
                with self.assertRaisesRegex(ValueError, "payload identity mismatch"):
                    adapter.ingest_block(rows)
            finally:
                cleanup = adapter.cleanup()
            self.assertTrue(cleanup.removed)
            self.assertFalse(adapter.namespace_exists())

    def test_missing_asset_catalog_row_breaks_joint_watermark_and_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)[:1]
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "agent-trace-clickhouse-25-12",
                "jsons3_" + uuid.uuid4().hex[:10], "asset_ref", root,
                LocalAssetStore(root / "missing_catalog_assets"),
            )
            adapter.create()
            try:
                adapter.ingest_block(rows)
                connection = adapter.connect_worker()
                try:
                    adapter._request(
                        connection,
                        f"ALTER TABLE {adapter.database}.assets DELETE WHERE asset_id={{asset_id:String}} SETTINGS mutations_sync=2",
                        parameters={"asset_id": rows[0]["sha256"]},
                    )
                finally:
                    connection.close()
                self.assertFalse(adapter.wait_write_complete(1).completed)
                self.assertFalse(adapter.wait_query_ready(timeout_seconds=30).completed)
            finally:
                cleanup = adapter.cleanup()
            self.assertTrue(cleanup.removed)

    def test_cleanup_restores_stopped_merges_before_drop(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = clickhouse.ClickHouseAdapter(
                "127.0.0.1", 18123, "agent-trace-clickhouse-25-12",
                "jsons3_" + uuid.uuid4().hex[:10], "same_table", Path(directory),
            )
            adapter.create()
            with mock.patch.object(adapter, "_request", wraps=adapter._request) as request:
                adapter.set_merges(False)
                cleanup = adapter.cleanup()
            statements = [call.args[1] for call in request.call_args_list]
            stop = next(index for index, value in enumerate(statements) if value.startswith("SYSTEM STOP MERGES"))
            start = next(index for index, value in enumerate(statements) if value.startswith("SYSTEM START MERGES"))
            drop = next(index for index, value in enumerate(statements) if value.startswith("DROP DATABASE"))
            self.assertLess(stop, start)
            self.assertLess(start, drop)
            self.assertTrue(cleanup.removed)
            self.assertFalse(adapter.namespace_exists())


if __name__ == "__main__":
    unittest.main()
