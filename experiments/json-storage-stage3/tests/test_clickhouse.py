import hashlib
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
from test_opengauss import CONTROL_PAYLOAD, EXPECTED_KEYS, PAYLOAD, fixture


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
                                detail.database_response_bytes, len(event_body.encode("utf-8")),
                            )
                            self.assertEqual(detail.response_bytes,
                                             detail.database_response_bytes + len(PAYLOAD))
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
