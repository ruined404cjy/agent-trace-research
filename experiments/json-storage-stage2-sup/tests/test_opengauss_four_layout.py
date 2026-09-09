import hashlib
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))
sys.path.insert(0, str(STAGE_DIR / "generator"))

import generate_supplement_truth as generator
import opengauss_four_layout as opengauss
import supplement_common as common


FIXTURE_ROWS = 512
BLOCK_ROWS = 256


def select_integration_fixture(rows, parameters):
    """从冻结输入确定性选择覆盖 S01 至 S06 的 512 行 fixture。"""
    window_rows = generator._window_rows(rows, parameters["S01"])
    required_ids = set()

    def include_first(query_id, predicate):
        """选择固定查询的首个窗口命中记录。"""
        row = next((item for item in window_rows if predicate(item)), None)
        if row is None:
            raise AssertionError(f"fixture requires a {query_id} match")
        required_ids.add(row["event_id"])

    include_first(
        "S02",
        lambda row: generator._value_at_path(
            row["attributes_analysis"], "gen_ai.operation.name"
        ) == parameters["S02"]["operation_name"],
    )
    include_first(
        "S03",
        lambda row: generator._value_at_path(
            row["attributes_analysis"], "failure.mistake_mode"
        ) == parameters["S03"]["failure_mistake_mode"],
    )
    include_first(
        "S04",
        lambda row: generator._value_at_path(
            row["attributes_analysis"], parameters["S04"]["attribute_key"]
        ) is not None,
    )
    required_ids.update(
        row["event_id"]
        for row in window_rows
        if row["trace_id"] == parameters["S05"]["trace_id"]
    )
    if not required_ids:
        raise AssertionError("fixture requires S05 matches")

    required_rows = [row for row in window_rows if row["event_id"] in required_ids]
    remaining_rows = [row for row in window_rows if row["event_id"] not in required_ids]
    fixture = required_rows + remaining_rows[:FIXTURE_ROWS - len(required_rows)]
    if len(fixture) != FIXTURE_ROWS:
        raise AssertionError("fixture requires 512 window rows")
    return fixture


class OpenGaussFourLayoutUnitTest(unittest.TestCase):
    """验证 openGauss JSON 与 openGauss JSONB 的基础比较契约。"""

    def setUp(self):
        """建立不依赖数据库的 adapter。"""
        self.adapter = opengauss.OpenGaussFourLayoutAdapter(
            "127.0.0.1", 15432, "unused", "jsons2sup_test"
        )

    def test_layout_ddl_only_changes_attributes_type_and_has_no_acceleration(self):
        """防止基础比较混入索引或改变稳定列定义。"""
        json_ddl = opengauss.create_layout_ddls("s2sup", "og_json")
        jsonb_ddl = opengauss.create_layout_ddls("s2sup", "og_jsonb")

        self.assertIn("attributes JSON NOT NULL", json_ddl)
        self.assertIn("attributes JSONB NOT NULL", jsonb_ddl)
        self.assertIn('CREATE TABLE s2sup_og_json."raw"', json_ddl)
        self.assertNotIn("CREATE INDEX", json_ddl)
        self.assertNotIn("CREATE INDEX", jsonb_ddl)
        self.assertNotIn("jsonb_hash_ops", jsonb_ddl)

    def test_queries_keep_shared_path_semantics_and_complete_document_order(self):
        """防止 JSON 与 JSONB 的路径谓词、分页或整文档恢复语义偏离。"""
        for layout in common.LAYOUTS[:2]:
            for query_id in common.QUERY_IDS:
                with self.subTest(layout=layout, query_id=query_id):
                    statement = opengauss.query_sql(layout, query_id)
                    self.assertIn("project_id = %s", statement)
                    self.assertIn("start_time >= %s", statement)
                    self.assertIn("start_time < %s", statement)
        self.assertIn("attributes #>> '{gen_ai,operation,name}'", opengauss.query_sql("og_json", "S02"))
        self.assertIn("attributes #>> '{failure,mistake_mode}'", opengauss.query_sql("og_jsonb", "S03"))
        self.assertIn("attributes #>> '{gen_ai,output,messages}'", opengauss.query_sql("og_json", "S04"))
        self.assertNotIn("@>", opengauss.query_sql("og_jsonb", "S03"))
        self.assertIn("attributes::text", opengauss.query_sql("og_jsonb", "S05"))
        self.assertIn("ORDER BY start_time, event_id", opengauss.query_sql("og_json", "S05"))
        self.assertIn("OFFSET", opengauss.query_sql("og_json", "S06"))
        self.assertIn("FLOOR((count(*) + 3) / 4.0) - 1", opengauss.query_sql("og_json", "S06"))
        self.assertIn(
            "FROM numbered ORDER BY start_time, event_id OFFSET",
            opengauss.query_sql("og_json", "S06"),
        )

    def test_query_parameters_follow_supplement_catalog(self):
        """防止位置参数漏写查询路径条件或 S06 页大小。"""
        parameters = {
            "project_id": "project", "start_time": "2030-01-01T00:00:00Z",
            "end_time": "2030-01-02T00:00:00Z", "operation_name": "execute_tool",
            "failure_mistake_mode": "A.3", "trace_id": "trace-a", "page_size": 256,
        }

        self.assertEqual(self.adapter._query_parameters("S01", parameters), (
            "project", "2030-01-01T00:00:00Z", "2030-01-02T00:00:00Z",
        ))
        self.assertEqual(self.adapter._query_parameters("S02", parameters)[-1], "execute_tool")
        self.assertEqual(self.adapter._query_parameters("S03", parameters)[-1], "A.3")
        self.assertEqual(self.adapter._query_parameters("S05", parameters)[-1], "trace-a")
        self.assertEqual(self.adapter._query_parameters("S06", parameters)[-1], 256)
        with self.assertRaisesRegex(ValueError, "page_size"):
            self.adapter._query_parameters("S06", {**parameters, "page_size": 0})

    def test_statement_substitution_preserves_json_path_braces(self):
        """防止替换分析表名时把 JSON 路径误作格式化字段。"""
        statement = self.adapter._statement("og_json", "S02")

        self.assertIn("jsons2sup_test_og_json.analytics", statement)
        self.assertIn("attributes #>> '{gen_ai,operation,name}'", statement)

    def test_normalization_matches_all_supplement_result_contracts(self):
        """防止响应读取后的恢复改变 S01 至 S06 的 truth 语义。"""
        timestamp = datetime(2030, 1, 1, 0, 0, 1, 234000, tzinfo=timezone.utc)
        document = {"gen_ai": {"output": {"messages": ["汉"]}}}

        self.assertEqual(self.adapter._normalize_result("S01", [("tool", 1), ("llm", 2)]), [["llm", 2], ["tool", 1]])
        self.assertEqual(self.adapter._normalize_result("S02", [("tool", 1)]), [["tool", 1]])
        self.assertEqual(
            self.adapter._normalize_result("S03", [("event-b",), ("event-a",)]),
            {"row_count": 2, "identity_sha256": hashlib.sha256(b'["event-a","event-b"]').hexdigest()},
        )
        self.assertEqual(
            self.adapter._normalize_result("S04", [('["汉"]',), (None,), ('{"a":1}',)]),
            {"non_null_count": 2, "utf8_bytes": len('["汉"]'.encode("utf-8")) + len(b'{"a":1}')},
        )
        self.assertEqual(
            self.adapter._normalize_result("S05", [(timestamp, "event-1", json.dumps(document))]),
            [["2030-01-01T00:00:01.234Z", "event-1", document]],
        )
        self.assertEqual(
            self.adapter._normalize_result("S06", [(3, timestamp, "event-1", json.dumps(document))]),
            {
                "identity_sha256": hashlib.sha256(b'["event-1"]').hexdigest(),
                "page_row_count": 1,
                "row_count": 3,
                "rows": [["2030-01-01T00:00:01.234Z", "event-1", document]],
            },
        )

    def test_storage_reports_heap_toast_index_and_total_bytes(self):
        """防止 openGauss 空间结果遗漏 TOAST 或混合统计对象。"""
        connection = mock.MagicMock()
        connection.execute.return_value.fetchall.return_value = [(10, 3, 20)]
        with mock.patch.object(self.adapter, "connect_worker", return_value=connection):
            storage = self.adapter.collect_storage("og_json")

        self.assertEqual(storage, {
            "analytics": {"heap_bytes": 10, "toast_bytes": 3, "index_bytes": 7, "total_bytes": 20},
            "raw": {"heap_bytes": 10, "toast_bytes": 3, "index_bytes": 7, "total_bytes": 20},
        })

    def test_cleanup_only_drops_schema_successfully_created_by_this_adapter(self):
        """防止异常处理删除同名的外部 schema。"""
        self.assertEqual(
            self.adapter.cleanup("og_json"),
            {"schema": "jsons2sup_test_og_json", "removed": False},
        )
        connection = mock.MagicMock()
        connection.transaction.return_value.__enter__.return_value = None
        connection.execute.return_value.fetchone.return_value = (False,)
        self.adapter._created_schemas.add("jsons2sup_test_og_json")
        with mock.patch.object(self.adapter, "connect_worker", return_value=connection):
            self.assertEqual(
                self.adapter.cleanup("og_json"),
                {"schema": "jsons2sup_test_og_json", "removed": True},
            )
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertIn("DROP SCHEMA IF EXISTS jsons2sup_test_og_json CASCADE", statements)
        self.assertNotIn("jsons2sup_test_og_json", self.adapter._created_schemas)

    def test_integration_fixture_is_deterministic_and_exercises_all_queries(self):
        """防止 512 行 fixture 漏掉固定 JSON 路径或整文档查询命中。"""
        rows, source_truth = common.verify_input(OpenGaussFourLayoutIntegrationTest.INPUT_DIR)
        parameters = generator._query_parameters(source_truth)
        first = select_integration_fixture(rows, parameters)
        second = select_integration_fixture(rows, parameters)

        self.assertEqual([row["event_id"] for row in first], [row["event_id"] for row in second])
        self.assertEqual(len(first), FIXTURE_ROWS)
        results = {
            query_id: generator.query_results(first, query_id, parameters[query_id])
            for query_id in common.QUERY_IDS
        }
        self.assertGreater(len(results["S01"]), 0)
        self.assertGreater(len(results["S02"]), 0)
        self.assertGreater(results["S03"]["row_count"], 0)
        self.assertGreater(results["S04"]["non_null_count"], 0)
        self.assertGreater(len(results["S05"]), 0)
        self.assertEqual(results["S06"]["page_row_count"], BLOCK_ROWS)


@unittest.skipUnless(os.environ.get("RUN_OPENGAUSS_INTEGRATION") == "1", "set RUN_OPENGAUSS_INTEGRATION=1")
class OpenGaussFourLayoutIntegrationTest(unittest.TestCase):
    """以覆盖固定查询命中的 512 行冻结输入验证 openGauss JSON 与 openGauss JSONB。"""

    INPUT_DIR = STAGE_DIR.parents[1] / "docs/temp/json-storage-stage2/cross-engine-input-20260907"

    def setUp(self):
        """创建独立 schema 并加载固定补充查询参数。"""
        import supplement_common

        rows, source_truth = supplement_common.verify_input(self.INPUT_DIR)
        self.parameters = generator._query_parameters(source_truth)
        self.rows = select_integration_fixture(rows, self.parameters)
        self.assertEqual(len(self.rows), FIXTURE_ROWS)
        self.namespace = f"jsons2sup_{uuid.uuid4().hex[:12]}"
        self.adapter = opengauss.OpenGaussFourLayoutAdapter(
            "127.0.0.1", 15432, "agent-trace-opengauss-v6", self.namespace
        )
        self.truth = {
            query_id: generator.query_results(self.rows, query_id, self.parameters[query_id])
            for query_id in common.QUERY_IDS
        }
        self.record_truth = {"records": [
            {
                "event_id": row["event_id"],
                "analysis_sha256": hashlib.sha256(common.canonical_bytes(row["attributes_analysis"])).hexdigest(),
                "raw_sha256": hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest(),
            }
            for row in self.rows
        ]}

    def tearDown(self):
        """删除本测试实例创建的 schema。"""
        for layout in common.LAYOUTS[:2]:
            self.adapter.cleanup(layout)
        self.assertTrue(all(not self.adapter.schema_exists(layout) for layout in common.LAYOUTS[:2]))

    def test_json_and_jsonb_match_truth_restore_raw_and_cleanup(self):
        """防止两种属性类型在载入、查询、恢复或清理阶段偏离。"""
        for layout in common.LAYOUTS[:2]:
            with self.subTest(layout=layout):
                self.adapter.create_layout(layout)
                first = self.adapter.insert_block(layout, self.rows[:BLOCK_ROWS])
                second = self.adapter.insert_block(layout, self.rows[BLOCK_ROWS:])
                self.assertEqual(first["rows"], BLOCK_ROWS)
                self.assertEqual(second["rows"], BLOCK_ROWS)
                self.assertTrue(self.adapter.finish_maintenance(layout)["analyzed"])
                connection = self.adapter.connect_worker()
                try:
                    for query_id in common.QUERY_IDS:
                        result = self.adapter.execute_query(
                            connection, layout, query_id, self.parameters[query_id]
                        )
                        self.assertEqual(result["result"], self.truth[query_id])
                        self.assertGreaterEqual(result["latency_ms"], 0.0)
                        self.assertGreaterEqual(result["recovery_ms"], 0.0)
                        if query_id in {"S01", "S02", "S05"}:
                            self.assertGreater(len(result["result"]), 0)
                        elif query_id == "S03":
                            self.assertGreater(result["result"]["row_count"], 0)
                        elif query_id == "S04":
                            self.assertGreater(result["result"]["non_null_count"], 0)
                        else:
                            self.assertEqual(result["result"]["page_row_count"], BLOCK_ROWS)
                finally:
                    connection.close()
                self.assertTrue(self.adapter.verify_analysis(layout, self.record_truth)["ok"])
                self.assertTrue(self.adapter.verify_raw(layout, self.record_truth)["ok"])
                storage = self.adapter.collect_storage(layout)
                self.assertEqual(set(storage), {"analytics", "raw"})
                self.assertEqual(set(storage["analytics"]), {"heap_bytes", "toast_bytes", "index_bytes", "total_bytes"})
                self.adapter.cleanup(layout)
                self.assertFalse(self.adapter.schema_exists(layout))


if __name__ == "__main__":
    unittest.main()
