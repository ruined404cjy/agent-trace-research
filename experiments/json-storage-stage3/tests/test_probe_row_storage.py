import json
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))
sys.path.insert(0, str(STAGE_DIR / "tools"))

from common import BlockResult, CleanupResult, MaintenanceResult, QuerySpec, StorageEvidence
import probe_row_storage as probe


PROFILES = ("entropy_512k", "text_2m", "text_512k", "text_64k")
EXPANDED_CURSOR = "AND (start_time > %s OR (start_time = %s AND event_id > %s))"
# XStore adapter 实际发出的中间页游标：蕴含下界加展开式。
BOUNDED_CURSOR = "AND start_time >= %s " + EXPANDED_CURSOR
MIDDLE_QUERY = QuerySpec("list", {
    "project_id": "project-a", "start_time": "2030-01-01T00:00:00.000Z",
    "end_time": "2030-01-01T01:00:00.000Z", "cursor_time": "2030-01-01T00:30:00.000Z",
    "cursor_id": "event-mid", "page_size": 256,
})


class FakeConnection:
    """按语句类别返回固定结果，并记录全部执行的 SQL 与绑定。"""

    def __init__(self, adapter):
        self.adapter = adapter

    def execute(self, statement, values=()):
        self.adapter.executed.append((statement, tuple(values)))
        if "pg_column_size" in statement:
            rows = [(profile, self.adapter.profile_rows.get(profile, 40), 100, 104)
                    for profile in PROFILES]
        elif "FROM pg_class" in statement:
            rows = [("events", "r", 0, None), ("events_pkey", "i", 0, None)]
        elif statement.startswith("EXPLAIN"):
            rows = [("Limit  (actual rows=256 loops=1)",), ("  ->  Index Scan using events_list_idx",)]
        else:
            raise AssertionError(f"unexpected statement: {statement}")
        return FakeCursor(rows)

    def close(self):
        self.adapter.closed_connections += 1


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)


class FakeRowAdapter:
    """替换数据库边界，记录探针对 adapter 公共接口的调用顺序。"""

    container_name = ""

    def __init__(self, layout, fail_at_block=None):
        self.layout = layout
        self.namespace = f"jsons3_probe_{layout}"
        self.schema = self.namespace + "_" + layout
        self.fail_at_block = fail_at_block
        self.calls = []
        self.executed = []
        self.profile_rows = {}
        self.closed_connections = 0

    def create(self):
        self.calls.append("create")
        return {"schema": self.schema}

    def ingest_block(self, block):
        index = sum(call.startswith("ingest") for call in self.calls)
        self.calls.append(f"ingest:{index}")
        if index == self.fail_at_block:
            raise RuntimeError("ingest failed")
        watermark = block[-1]["ingest_seq"] + 1
        return BlockResult(len(block), watermark, {"events": watermark}, 1.0)

    def wait_write_complete(self, watermark):
        self.calls.append(f"visible:{watermark}")
        return MaintenanceResult(True, {"events": watermark})

    def wait_query_ready(self, timeout_seconds):
        self.calls.append("ready")
        return MaintenanceResult(True, {"events": 4})

    def connect_worker(self):
        return FakeConnection(self)

    def collect_storage(self):
        self.calls.append("storage")
        return StorageEvidence({"events": {"heap_bytes": 1, "index_bytes": 2,
                                           "toast_bytes": 0, "total_bytes": 3}})

    def _query_statement(self, query):
        statement = ("SELECT event_id FROM " + self.schema + ".events WHERE project_id=%s "
                     "AND start_time>=%s AND start_time<%s " + BOUNDED_CURSOR
                     + " ORDER BY start_time,event_id LIMIT %s")
        params = query.parameters
        return statement, (params["project_id"], params["start_time"], params["end_time"],
                           params["cursor_time"], params["cursor_time"], params["cursor_time"],
                           params["cursor_id"], params["page_size"])

    def cleanup(self):
        self.calls.append("cleanup")
        return CleanupResult(self.namespace, True)


def blocks():
    """两个 block 覆盖四个事件，第二块的末行决定最终水位。"""
    rows = [{"ingest_seq": index, "event_id": f"event-{index}"} for index in range(4)]
    return (tuple(rows[:2]), tuple(rows[2:]))


class RowStorageProbeTest(unittest.TestCase):
    """验证探针复用主矩阵载入路径、在清理前取证并且总是清理。"""

    def test_same_table_probe_records_payload_relations_and_both_cursor_forms(self):
        adapter = FakeRowAdapter("same_table")
        record = probe.probe_layout(adapter, "xstore", blocks(), MIDDLE_QUERY, ready_timeout=5)

        self.assertEqual(adapter.calls, [
            "create", "ingest:0", "visible:2", "ingest:1", "visible:4", "ready", "storage", "cleanup",
        ])
        payload_sql = next(sql for sql, _ in adapter.executed if "pg_column_size" in sql)
        self.assertIn(f"FROM {adapter.schema}.events WHERE payload IS NOT NULL", payload_sql)
        self.assertEqual([item["profile"] for item in record["payload_profiles"]], list(PROFILES))
        self.assertEqual(record["payload_profiles"][0],
                         {"profile": "entropy_512k", "rows": 40, "logical_bytes": 100, "stored_bytes": 104})
        self.assertEqual(record["relations"][0],
                         {"relname": "events", "relkind": "r", "reltoastrelid": 0, "reloptions": None})
        forms = record["cursor_probe"]
        self.assertEqual(set(forms), {"expanded", "expanded_with_lower_bound"})
        # 两种形式只差一条被 OR 条件蕴含的下界及其绑定的 cursor_time；形式一由探针
        # 去掉 adapter 语句中的下界得到，计划对照因此仍能说明下界的作用。
        cursor_time = "2030-01-01T00:30:00.000Z"
        expanded, bounded = forms["expanded"], forms["expanded_with_lower_bound"]
        self.assertIn(BOUNDED_CURSOR, bounded["statement"])
        self.assertEqual(bounded["values"][3:7], [cursor_time, cursor_time, cursor_time, "event-mid"])
        self.assertIn(EXPANDED_CURSOR, expanded["statement"])
        self.assertNotIn("start_time >= %s", expanded["statement"])
        self.assertEqual(expanded["values"][3:6], [cursor_time, cursor_time, "event-mid"])
        for form in (expanded, bounded):
            self.assertEqual(form["statement"].count("%s"), len(form["values"]))
        self.assertTrue(forms["expanded"]["plan"].startswith("Limit"))
        explained = [sql for sql, _ in adapter.executed if sql.startswith("EXPLAIN (ANALYZE, BUFFERS)")]
        self.assertEqual(len(explained), 2)
        self.assertEqual(record["cleanup"], {"namespace": adapter.namespace, "removed": True})
        self.assertEqual(adapter.closed_connections, 1)

    def test_split_layouts_measure_their_own_payload_table_without_cursor_probe(self):
        for layout, table in (("separate", "event_payloads"), ("full_core", "events_full")):
            with self.subTest(layout=layout):
                adapter = FakeRowAdapter(layout)
                record = probe.probe_layout(adapter, "xstore", blocks(), MIDDLE_QUERY, ready_timeout=5)
                payload_sql = next(sql for sql, _ in adapter.executed if "pg_column_size" in sql)
                self.assertIn(f"FROM {adapter.schema}.{table} WHERE", payload_sql)
                self.assertNotIn("cursor_probe", record)

    def test_cleanup_runs_when_loading_fails(self):
        adapter = FakeRowAdapter("same_table", fail_at_block=1)
        with self.assertRaisesRegex(RuntimeError, "ingest failed"):
            probe.probe_layout(adapter, "xstore", blocks(), MIDDLE_QUERY, ready_timeout=5)
        self.assertEqual(adapter.calls[-1], "cleanup")

    def test_payload_profile_counts_must_match_the_main_workload(self):
        adapter = FakeRowAdapter("same_table")
        adapter.profile_rows = {"text_2m": 39}
        with self.assertRaisesRegex(ValueError, "payload profile rows"):
            probe.probe_layout(adapter, "xstore", blocks(), MIDDLE_QUERY, ready_timeout=5)
        self.assertEqual(adapter.calls[-1], "cleanup")

    def test_cursor_forms_reject_a_statement_without_the_bounded_cursor(self):
        """adapter 语句形态变化时探针报错，避免两种形式静默退化为同一条语句。"""
        for statement, values in (
            ("SELECT 1 WHERE (start_time,event_id)>(%s,%s)", ("a", "b")),
            ("SELECT 1 WHERE project_id=%s AND start_time>=%s AND start_time<%s " + EXPANDED_CURSOR,
             ("p", "s", "e", "c", "c", "i")),
        ):
            with self.subTest(statement=statement):
                with self.assertRaisesRegex(ValueError, "bounded cursor"):
                    probe.cursor_forms(statement, values)
        # 三处 cursor_time 绑定不一致时两种形式的结果集不再相同，对照失去意义。
        with self.assertRaisesRegex(ValueError, "cursor_time"):
            probe.cursor_forms("SELECT 1 WHERE project_id=%s AND start_time>=%s AND start_time<%s "
                               + BOUNDED_CURSOR + " LIMIT %s", ("p", "s", "e", "c", "c", "d", "i", 256))

    def test_cursor_forms_follow_the_xstore_adapter_statement(self):
        """探针对照的形式二就是 adapter 发出的语句，形式一只去掉下界。"""
        import xstore
        from assets import LocalAssetStore

        with tempfile.TemporaryDirectory() as root:
            adapter = xstore.XStoreAdapter(
                "127.0.0.1", 29000, "", "jsons3_probe", "same_table",
                Path(root), LocalAssetStore(Path(root) / "assets"), user="runner",
            )
            statement, values = adapter._query_statement(MIDDLE_QUERY)
        forms = probe.cursor_forms(statement, values)
        self.assertEqual(forms["expanded_with_lower_bound"], (statement, tuple(values)))
        expanded, expanded_values = forms["expanded"]
        self.assertEqual(expanded, statement.replace(BOUNDED_CURSOR, EXPANDED_CURSOR))
        self.assertEqual(expanded_values, tuple(values[:3]) + tuple(values[4:]))

    def test_row_constructor_engine_records_cursor_probe_as_not_applicable(self):
        adapter = FakeRowAdapter("same_table")
        record = probe.probe_layout(adapter, "opengauss", blocks(), MIDDLE_QUERY, ready_timeout=5)
        self.assertEqual(record["cursor_probe"]["status"], "not-applicable")
        self.assertFalse(any(sql.startswith("EXPLAIN") for sql, _ in adapter.executed))

    def test_cli_refuses_to_overwrite_an_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "probe.json"
            output.write_text(json.dumps({"status": "complete"}), encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                probe.main(["--input", directory, "--output", str(output), "--engine", "xstore"])


if __name__ == "__main__":
    unittest.main()
