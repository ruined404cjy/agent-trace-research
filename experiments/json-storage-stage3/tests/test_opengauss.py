import datetime
import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from dataclasses import fields
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import opengauss
from assets import AssetError, AssetReference, AssetResolver, LocalAssetStore
from common import QueryResult, QuerySpec, StorageEvidence, build_layout_catalog


PAYLOAD = b'{"content":"\xe4\xb8\xad\xe6\x96\x87 payload"}'
CONTROL_PAYLOAD = b'{"content":"control payload"}'
EXPECTED_KEYS = {
    "event_id", "trace_id", "project_id", "start_time", "profile",
    "content_type", "encoding", "content_length", "preview", "sha256", "payload",
}


class FailAfterPublishStore(LocalAssetStore):
    """发布最终对象后注入故障，模拟 catalog 更新前的失败。"""

    def publish_bytes(self, asset_id, payload):
        super().publish_bytes(asset_id, payload)
        raise AssetError("failed")


def fixture(root):
    """创建包含 payload、空 payload 与 Trace 顺序的最小事件 block。"""
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    path = root / "payloads" / "one.json"
    path.parent.mkdir()
    path.write_bytes(PAYLOAD)
    control_digest = hashlib.sha256(CONTROL_PAYLOAD).hexdigest()
    control_path = root / "payloads" / "control.json"
    control_path.write_bytes(CONTROL_PAYLOAD)
    common = {
        "trace_id": "trace-a", "project_id": "project-a",
        "end_time": "2030-01-01T00:00:01.000Z", "duration_ms": 1,
        "span_type": "llm", "framework": "fixture", "level": "INFO",
    }
    return [
        {**common, "ingest_seq": 0, "event_id": "event-a", "span_id": "span-a",
         "parent_span_id": None, "start_time": "2030-01-01T00:00:00.000Z",
         "cohort": "main", "profile": "text_64k", "content_type": "application/json",
         "encoding": "utf-8", "content_length": len(PAYLOAD),
         "preview": PAYLOAD.decode()[:200], "sha256": digest,
         "payload_path": "payloads/one.json"},
        {**common, "ingest_seq": 1, "event_id": "event-b", "span_id": "span-b",
         "parent_span_id": "span-a", "start_time": "2030-01-01T00:00:00.500Z",
         "cohort": None, "profile": None, "content_type": None, "encoding": None,
         "content_length": None, "preview": None, "sha256": None, "payload_path": None},
        {**common, "ingest_seq": 2, "event_id": "event-c", "trace_id": "trace-b",
         "span_id": "span-c", "parent_span_id": None,
         "start_time": "2030-01-01T00:00:00.750Z", "cohort": "equal_total_control",
         "profile": "text_64k", "content_type": "application/json", "encoding": "utf-8",
         "content_length": len(CONTROL_PAYLOAD), "preview": CONTROL_PAYLOAD.decode(),
         "sha256": control_digest, "payload_path": "payloads/control.json"},
    ]


ASSET_START_TIME = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)


class RecordingResult:
    """提供 psycopg 风格 fetchone/fetchall 的固定结果。"""

    def __init__(self, rows):
        self._rows = tuple(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class CatalogReuseConnection:
    """按语句分派主查询与 catalog 读取，并统计自身关闭次数。"""

    def __init__(self, requests, query_rows, catalog_rows, failing_asset_id=None):
        self.requests = requests
        self.query_rows = query_rows
        self.catalog_rows = catalog_rows
        self.failing_asset_id = failing_asset_id
        self.close_count = 0

    def execute(self, statement, parameters=None):
        self.requests.append((self, statement, parameters))
        if "assets WHERE asset_id=" in statement:
            if parameters[0] == self.failing_asset_id:
                raise RuntimeError("injected catalog failure")
            row = self.catalog_rows.get(parameters[0])
            return RecordingResult(() if row is None else (row,))
        return RecordingResult(self.query_rows)

    def close(self):
        self.close_count += 1


def asset_query_fixture(root, payloads):
    """发布 payload 对象，并返回 store、catalog 行和 asset_ref 主查询行。"""

    store = LocalAssetStore(root / "assets")
    catalog_rows = {}
    rows = []
    for index, payload in enumerate(payloads):
        if payload is None:
            rows.append((f"event-{index}", "trace-a", "project-a", ASSET_START_TIME,
                         None, None, None, None, None, None, None))
            continue
        asset_id = hashlib.sha256(payload).hexdigest()
        store.publish_bytes(asset_id, payload)
        catalog_rows[asset_id] = (
            asset_id, asset_id, "text/plain", "utf-8", len(payload),
            str(store.object_path(asset_id)), "available", ASSET_START_TIME, None,
        )
        rows.append((f"event-{index}", "trace-a", "project-a", ASSET_START_TIME,
                     "text_64k", "text/plain", "utf-8", len(payload),
                     payload.decode("utf-8")[:200], asset_id, asset_id))
    return store, catalog_rows, tuple(rows)


def catalog_response_bytes(row):
    """按 adapter 的客户端表示规则计算 catalog 行长度。"""
    return len(json.dumps(
        row, ensure_ascii=False,
        default=lambda value: value.isoformat() if hasattr(value, "isoformat") else str(value),
    ).encode("utf-8"))


class OpenGaussAdapterUnitTest(unittest.TestCase):
    """验证 openGauss 四布局的 SQL、状态和公共协议。"""

    def test_ingest_failure_has_no_observable_request_body_bytes(self):
        adapter = opengauss.OpenGaussAdapter(
            "127.0.0.1", 15432, "unused", "jsons3_test", "same_table", Path("."),
        )
        evidence = getattr(adapter, "ingest_failure_evidence", None)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence(), {})

    def test_asset_pending_insert_projects_every_logical_catalog_column(self):
        """捕获 pending INSERT 遗漏 logical Asset row 的显式 NULL 列。"""
        class RecordingConnection:
            def __init__(self):
                self.executions = []

            def transaction(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, exception_type, exception, traceback):
                return False

            def execute(self, statement, parameters):
                self.executions.append((statement, parameters))

            def close(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)[:1]
            connection = RecordingConnection()
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root,
                FailAfterPublishStore(root / "assets"),
            )
            adapter.connect_worker = lambda: connection
            adapter.set_asset_status = lambda *arguments: None

            with self.assertRaisesRegex(AssetError, "^failed$"):
                adapter._ingest_assets(rows, [adapter._payload(rows[0])])

            statement, parameters = connection.executions[0]
            columns = statement.split("assets(", 1)[1].split(")", 1)[0].split(",")
            self.assertEqual(columns, [
                "asset_id", "sha256", "content_type", "encoding", "content_length",
                "storage_path", "status", "error_category",
            ])
            self.assertTrue(statement.endswith(
                "VALUES (%s,%s,%s,%s,%s,%s,'pending',NULL)"
            ))
            self.assertEqual(parameters, (
                rows[0]["sha256"], rows[0]["sha256"], rows[0]["content_type"],
                rows[0]["encoding"], rows[0]["content_length"],
                str(adapter.asset_store.object_path(rows[0]["sha256"])),
            ))

    def test_layout_catalog_has_distinct_tables_and_completion_watermarks(self):
        catalog = build_layout_catalog("full_core")
        self.assertEqual(catalog.write_tables, ("events_full", "events_core"))
        self.assertEqual(catalog.list_source, "events_core")
        self.assertEqual(catalog.detail_source, "events_full")
        self.assertTrue(catalog.requires_joint_watermark)
        self.assertEqual(build_layout_catalog("separate").write_tables,
                         ("events_analytics", "event_payloads"))
        self.assertEqual(build_layout_catalog("asset_ref").write_tables,
                         ("events_analytics", "assets"))

    def test_ddls_use_text_and_cover_list_trace_asset_and_toast_paths(self):
        for layout in opengauss.LAYOUTS:
            ddl = opengauss.create_layout_ddls("jsons3_test", layout)
            self.assertIn("CREATE SCHEMA jsons3_test_" + layout, ddl)
            self.assertIn("(project_id,start_time,event_id)", ddl)
            self.assertIn("(project_id,trace_id,start_time,event_id)", ddl)
            if layout != "asset_ref":
                self.assertIn("payload TEXT", ddl)
        asset = opengauss.create_layout_ddls("jsons3_test", "asset_ref")
        self.assertIn("CREATE TABLE jsons3_test_asset_ref.assets", asset)
        for status in ("pending", "available", "failed", "deleting"):
            self.assertIn("'" + status + "'", asset)
        storage = opengauss.storage_sql("jsons3_test_same_table", "events")
        self.assertIn("pg_relation_size", storage)
        self.assertIn("reltoastrelid", storage)
        evidence = opengauss.access_evidence_sql("jsons3_test_same_table")
        self.assertIn("idx_scan", evidence)
        self.assertIn("pg_stat_user_indexes", evidence)
        self.assertTrue(opengauss.explain_sql("SELECT 1").startswith("EXPLAIN (ANALYZE, BUFFERS) "))
        separate = opengauss.create_layout_ddls("jsons3_test", "separate")
        payload_table = separate.split("CREATE TABLE jsons3_test_separate.event_payloads", 1)[1].split("CREATE INDEX", 1)[0]
        self.assertIn("trace_id TEXT", payload_table)
        self.assertIn("payload TEXT", payload_table)
        self.assertNotIn("span_type", payload_table)
        self.assertNotIn("duration_ms", payload_table)

    def test_invalid_namespace_and_layout_fail_before_sql(self):
        with self.assertRaises(ValueError):
            opengauss.create_layout_ddls("bad-name", "same_table")
        with self.assertRaises(ValueError):
            build_layout_catalog("view")
        with self.assertRaisesRegex(ValueError, "batch query requires cohort"):
            QuerySpec("batch")

    def test_batch_sql_and_evidence_types_expose_fixed_contracts(self):
        adapter = opengauss.OpenGaussAdapter(
            "127.0.0.1", 15432, "unused", "jsons3_test", "same_table", Path("."),
        )
        statement, values = adapter._query_statement(QuerySpec("batch", {"cohort": "main"}))
        self.assertIn("cohort=%s", statement)
        self.assertIn("sha256 IS NOT NULL", statement)
        self.assertEqual(values, ("main",))
        self.assertIn("database_response_bytes", {item.name for item in fields(QueryResult)})
        self.assertIn("resolver_payload_bytes", {item.name for item in fields(QueryResult)})
        self.assertIn("asset_store", {item.name for item in fields(StorageEvidence)})
        result_fields = {item.name for item in fields(QueryResult)}
        self.assertIn("database_protocol_bytes", result_fields)
        self.assertIn("resolver_requests", result_fields)
        self.assertIn("resolver_read_ms", result_fields)

    @staticmethod
    def _catalog_connection_factory(requests, connections, query_rows, catalog_rows,
                                    failing_asset_id=None):
        """返回记录新建连接的 connect_worker 替身。"""
        def connect_worker():
            connection = CatalogReuseConnection(
                requests, query_rows, catalog_rows, failing_asset_id,
            )
            connections.append(connection)
            return connection
        return connect_worker

    @staticmethod
    def _catalog_statements(requests):
        return [entry for entry in requests if "assets WHERE asset_id=" in entry[1]]

    def test_asset_normalization_reuses_one_lazy_catalog_connection_per_query(self):
        """捕获每个 Asset 引用新建 catalog 连接并耗尽客户端连接。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = b"first asset payload", b"second asset payload"
            store, catalog_rows, rows = asset_query_fixture(root, [first, second, first])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
            )

            normalized, catalog_bytes, resolver_requests, _ = adapter._normalize_rows(rows)

            catalog_statements = self._catalog_statements(requests)
            first_id = hashlib.sha256(first).hexdigest()
            second_id = hashlib.sha256(second).hexdigest()
            self.assertEqual(resolver_requests, 3)
            self.assertEqual([row["payload"] for row in normalized], [first, second, first])
            self.assertEqual(len(catalog_statements), 3)
            self.assertEqual(len({id(entry[0]) for entry in catalog_statements}), 1)
            self.assertEqual(len(connections), 1)
            self.assertEqual(connections[0].close_count, 1)
            self.assertEqual(catalog_bytes, (
                2 * catalog_response_bytes(catalog_rows[first_id])
                + catalog_response_bytes(catalog_rows[second_id])
            ))

    def test_asset_query_owns_query_and_catalog_connections_separately(self):
        """捕获查询作用域 catalog 连接与主查询连接混用或未关闭。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = b"first asset payload", b"second asset payload"
            store, catalog_rows, rows = asset_query_fixture(root, [first, second, first])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
            )

            result = adapter.run_query(QuerySpec("batch", {"cohort": "main"}))

            self.assertEqual([row["payload"] for row in result.rows], [first, second, first])
            self.assertEqual(result.resolver_requests, 3)
            self.assertEqual(len(connections), 2)
            self.assertEqual([connection.close_count for connection in connections], [1, 1])
            catalog_connections = {id(entry[0]) for entry in self._catalog_statements(requests)}
            self.assertEqual(catalog_connections, {id(connections[1])})

    def test_asset_query_without_references_opens_no_catalog_connection(self):
        """捕获无 Asset 引用的查询仍为 catalog 读取新建闲置连接。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, catalog_rows, rows = asset_query_fixture(root, [None])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
            )

            result = adapter.run_query(QuerySpec("batch", {"cohort": "main"}))

            self.assertEqual(result.resolver_requests, 0)
            self.assertEqual([row["payload"] for row in result.rows], [None])
            self.assertEqual(self._catalog_statements(requests), [])
            self.assertEqual(len(connections), 1)
            self.assertEqual(connections[0].close_count, 1)

    def test_public_asset_lookup_owns_and_closes_its_catalog_connection(self):
        """捕获公开 catalog 读取复用查询连接或遗留未关闭连接。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = b"public lookup payload"
            store, catalog_rows, rows = asset_query_fixture(root, [payload])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
            )

            adapter.run_query(QuerySpec("batch", {"cohort": "main"}))
            query_connections = tuple(connections)
            record = adapter.get_available(hashlib.sha256(payload).hexdigest())

            self.assertEqual(record.status, "available")
            self.assertEqual(len(connections), len(query_connections) + 1)
            self.assertNotIn(connections[-1], query_connections)
            self.assertEqual(connections[-1].close_count, 1)

    def test_asset_normalization_closes_catalog_connection_when_resolution_fails(self):
        """捕获 resolver 异常路径遗留查询作用域 catalog 连接。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = b"first asset payload", b"second asset payload"
            store, catalog_rows, rows = asset_query_fixture(root, [first, second])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
            )
            store.object_path(hashlib.sha256(second).hexdigest()).unlink()

            with self.assertRaisesRegex(AssetError, "^missing$"):
                adapter._normalize_rows(rows)

            self.assertEqual(len(connections), 1)
            self.assertEqual(connections[0].close_count, 1)

    def test_two_asset_queries_do_not_share_a_closed_catalog_connection(self):
        """捕获跨查询复用上一条查询已关闭的 catalog 连接。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = b"first asset payload", b"second asset payload"
            store, catalog_rows, rows = asset_query_fixture(root, [first, second, first])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
            )

            first_result = adapter.run_query(QuerySpec("batch", {"cohort": "main"}))
            second_result = adapter.run_query(QuerySpec("batch", {"cohort": "main"}))

            catalog_statements = self._catalog_statements(requests)
            catalog_connections = [id(entry[0]) for entry in catalog_statements]
            self.assertEqual([row["payload"] for row in first_result.rows], [first, second, first])
            self.assertEqual([row["payload"] for row in second_result.rows], [first, second, first])
            self.assertEqual(len(connections), 4)
            self.assertEqual(catalog_connections.count(id(connections[1])), 3)
            self.assertEqual(catalog_connections.count(id(connections[3])), 3)
            self.assertEqual(len(set(catalog_connections)), 2)
            self.assertEqual([connection.close_count for connection in connections], [1, 1, 1, 1])

    def test_catalog_select_failure_propagates_and_closes_the_query_connection(self):
        """捕获 catalog SELECT 抛异常时未关闭查询作用域连接或吞掉异常。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = b"first asset payload", b"second asset payload"
            store, catalog_rows, rows = asset_query_fixture(root, [first, second, first])
            requests, connections = [], []
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "unused", "jsons3_test", "asset_ref", root, store,
            )
            adapter.connect_worker = self._catalog_connection_factory(
                requests, connections, rows, catalog_rows,
                failing_asset_id=hashlib.sha256(second).hexdigest(),
            )

            with self.assertRaisesRegex(RuntimeError, "^injected catalog failure$"):
                adapter.run_query(QuerySpec("batch", {"cohort": "main"}))

            self.assertEqual(len(connections), 2)
            self.assertEqual([connection.close_count for connection in connections], [1, 1])


@unittest.skipUnless(os.environ.get("RUN_OPENGAUSS_INTEGRATION") == "1",
                     "set RUN_OPENGAUSS_INTEGRATION=1")
class OpenGaussAdapterIntegrationTest(unittest.TestCase):
    """以唯一 schema 验证四布局真实写入、查询、状态和清理。"""

    def test_four_layouts_return_full_bytes_real_states_evidence_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)
            for layout in opengauss.LAYOUTS:
                with self.subTest(layout=layout):
                    namespace = "jsons3_" + uuid.uuid4().hex[:10]
                    store = LocalAssetStore(root / (layout + "_assets"))
                    adapter = opengauss.OpenGaussAdapter(
                        "127.0.0.1", 15432, "agent-trace-opengauss-v6",
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
                            value is None for value in block.database_ingest_request_body_bytes.values()
                        ))
                        ready = adapter.wait_write_complete(3)
                        self.assertTrue(ready.completed)
                        self.assertEqual(set(ready.watermarks), set(build_layout_catalog(layout).write_tables))
                        self.assertTrue(adapter.wait_query_ready(timeout_seconds=30).completed)

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
                        self.assertEqual(detail.rows[0]["sha256"], hashlib.sha256(PAYLOAD).hexdigest())
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
                        self.assertIn("EXPLAIN ANALYZE", evidence.plans[detail.query_id])
                        self.assertTrue(evidence.index_scans)
                        storage = adapter.collect_storage()
                        self.assertEqual(set(storage.tables), set(build_layout_catalog(layout).write_tables))
                        self.assertTrue(all("toast_bytes" in value for value in storage.tables.values()))
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
        """以真实 schema 捕获目录按 digest 去重时遗漏第二个事件引用。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)[:2]
            rows[1].update({
                "cohort": "main", "profile": rows[0]["profile"],
                "content_type": rows[0]["content_type"], "encoding": rows[0]["encoding"],
                "content_length": rows[0]["content_length"], "preview": rows[0]["preview"],
                "sha256": rows[0]["sha256"], "payload_path": rows[0]["payload_path"],
            })
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "agent-trace-opengauss-v6",
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
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "agent-trace-opengauss-v6",
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

    def test_asset_publish_failure_persists_failed_catalog_without_event_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = fixture(root)[:1]
            store = FailAfterPublishStore(root / "failed_assets")
            adapter = opengauss.OpenGaussAdapter(
                "127.0.0.1", 15432, "agent-trace-opengauss-v6",
                "jsons3_" + uuid.uuid4().hex[:10], "asset_ref", root, store,
            )
            adapter.create()
            try:
                with self.assertRaisesRegex(AssetError, "^failed$"):
                    adapter.ingest_block(rows)
                record = adapter.get_available(rows[0]["sha256"])
                self.assertEqual(record.status, "failed")
                self.assertEqual(record.error_category, "failed")
                connection = adapter.connect_worker()
                try:
                    count = connection.execute(
                        f"SELECT count(*) FROM {adapter.schema}.events_analytics"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(count, 0)
                self.assertFalse(adapter.wait_write_complete(1).completed)
                storage = adapter.collect_storage()
                self.assertEqual(storage.asset_store.available_object_count, 0)
                self.assertEqual(storage.asset_store.orphan_object_count, 1)
                self.assertEqual(storage.asset_store.orphan_bytes, len(PAYLOAD))
            finally:
                cleanup = adapter.cleanup()
            self.assertTrue(cleanup.removed)


if __name__ == "__main__":
    unittest.main()
