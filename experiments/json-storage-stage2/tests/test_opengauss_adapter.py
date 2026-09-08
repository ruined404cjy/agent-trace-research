import hashlib
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import opengauss


class OpenGaussAdapterUnitTest(unittest.TestCase):
    """验证 openGauss 三种 residual 布局的固定 DDL 与查询契约。"""

    def test_layout_ddl_limits_indexes_to_the_requested_layout(self):
        """捕获热路径或 GIN 索引错误进入其他布局的回归。"""
        hot = opengauss.create_layout_ddls("json_s2_test", "og_jsonb_hot")
        plain = opengauss.create_layout_ddls("json_s2_test", "og_jsonb")
        gin = opengauss.create_layout_ddls("json_s2_test", "og_jsonb_gin")

        self.assertIn("attributes JSONB NOT NULL", hot)
        self.assertIn("raw_event TEXT NOT NULL", hot)
        self.assertIn("jsonb_object_field_text", hot)
        self.assertNotIn("USING gin", hot)
        self.assertNotIn("CREATE INDEX", plain)
        self.assertIn("USING gin(attributes jsonb_ops)", gin)
        self.assertNotIn("jsonb_object_field_text", gin)

    def test_queries_keep_all_visibility_predicates_and_reject_invalid_inputs(self):
        """捕获查询遗漏 project、时间或水位可见性边界的错误。"""
        for layout in opengauss.LAYOUTS:
            for query_id in opengauss.QUERY_IDS:
                with self.subTest(layout=layout, query_id=query_id):
                    statement = opengauss.query_sql(layout, query_id)
                    self.assertIn("project_id = %s", statement)
                    self.assertIn("start_time >= %s", statement)
                    self.assertIn("start_time < %s", statement)
                    self.assertIn("ingest_seq < %s", statement)
        self.assertIn("ingest_seq < %s", opengauss.query_sql("og_jsonb", "Q05"))
        for namespace, layout, query_id in (
            ("bad-name", "og_jsonb", "Q01"),
            ("json_s2", "invalid", "Q01"),
            ("json_s2", "og_jsonb", "Q99"),
        ):
            with self.subTest(namespace=namespace, layout=layout, query_id=query_id):
                with self.assertRaises(ValueError):
                    if query_id == "Q99":
                        opengauss.query_sql(layout, query_id)
                    else:
                        opengauss.create_layout_ddls(namespace, layout)

    def test_normalization_restores_non_empty_q04_residual_and_hashes_result(self):
        """捕获 Q04 遗漏 truth 键映射或 canonical residual 值的回归。"""
        adapter = opengauss.OpenGaussAdapter("127.0.0.1", 15432, "unused", "json_s2_test")
        attributes = {
            "gen_ai": {"operation": {"name": "execute_tool"}},
            "failure": {"mistake_mode": "A.3"},
            "plain": None,
        }
        rows = [
            (
                datetime(2030, 1, 1, 0, 0, 1, 234000, tzinfo=timezone.utc),
                "trace-1:span-1",
                attributes,
            )
        ]
        params = {
            "key_map": {
                "gen_ai.operation.name": "gen_ai.operation.name",
                "failure.mistake_mode": "failure.mistake_mode",
                "plain": "plain",
            }
        }

        result = adapter._normalize_result("Q04", rows, params)
        digest = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(
            result,
            [[
                "2030-01-01T00:00:01.234Z",
                "trace-1:span-1",
                {
                    "failure.mistake_mode": '"A.3"',
                    "gen_ai.operation.name": '"execute_tool"',
                    "plain": "null",
                },
            ]],
        )
        self.assertEqual(digest, "d94d851ed76fd92742f26ac1fc2c1ba1f920374e449446c712538829fae57ce2")

    def test_normalization_sorts_non_empty_q05_identities_and_hashes_result(self):
        """捕获 Q05 未排序 identity 或摘要覆盖空结果的回归。"""
        adapter = opengauss.OpenGaussAdapter("127.0.0.1", 15432, "unused", "json_s2_test")

        result = adapter._normalize_result(
            "Q05", [("event-c",), ("event-a",), ("event-b",)], {}
        )
        digest = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        self.assertEqual(
            result,
            {
                "row_count": 3,
                "identity_sha256": "0a1968429bbd9bb1fc3ecd7c1b260afc63db22a2c9ec5d797e67bb6c15e31b73",
            },
        )
        self.assertEqual(digest, "b9a3b17777f2e118900318c8d37975e8331b23fb7177fcf5500295b76d199c45")

    def test_constructor_rejects_namespace_that_cannot_form_all_layout_schemas(self):
        """捕获 schema 后缀令构造期可接受 namespace 失效的回归。"""
        with self.assertRaisesRegex(ValueError, "schema name"):
            opengauss.OpenGaussAdapter("127.0.0.1", 15432, "unused", "a" * 63)


@unittest.skipUnless(os.environ.get("RUN_OPENGAUSS_INTEGRATION") == "1", "set RUN_OPENGAUSS_INTEGRATION=1")
class OpenGaussAdapterIntegrationTest(unittest.TestCase):
    """以正式输入前 512 行验证三布局的 openGauss 边界。"""

    INPUT_DIR = STAGE_DIR.parents[1] / "docs/temp/json-storage-stage2/cross-engine-input-20260907"

    def setUp(self):
        """创建独立 adapter，并读取正式输入的受限 fixture。"""
        import common

        rows, truth = common.verify_input(self.INPUT_DIR)
        self.rows = rows[:512]
        self.assertEqual(len(self.rows), 512)
        self.truth = self._fixture_truth(rows, truth, len(self.rows))
        self.namespace = f"jsons2_{uuid.uuid4().hex[:12]}"
        self.adapter = opengauss.OpenGaussAdapter(
            host="127.0.0.1",
            port=15432,
            container_name="agent-trace-opengauss-v6",
            namespace=self.namespace,
        )

    def tearDown(self):
        """无论门禁结果如何都删除本测试创建的全部 schema。"""
        for layout in opengauss.LAYOUTS:
            self.adapter.cleanup(layout)
        self.assertTrue(all(not self.adapter.schema_exists(layout) for layout in opengauss.LAYOUTS))

    def test_three_layouts_match_truth_restore_raw_and_cleanup(self):
        """捕获三布局的查询、raw、索引计划、维护或清理偏差。"""
        for layout in opengauss.LAYOUTS:
            with self.subTest(layout=layout):
                self.adapter.create_layout(layout, budget=32)
                self.adapter.insert_block(layout, self.rows[:256])
                self.adapter.insert_block(layout, self.rows[256:])
                maintenance = self.adapter.finish_maintenance(layout, timeout_seconds=30)
                self.assertTrue(maintenance["analyzed"])

                connection = self.adapter.connect_worker()
                try:
                    for query_id in opengauss.QUERY_IDS:
                        params = dict(self.truth["parameters"][query_id])
                        params["key_map"] = self.truth["key_map"]
                        actual = self.adapter.execute_query(
                            connection, layout, query_id, params, watermark=512
                        )
                        expected = self.truth["queries"][query_id]["512"]
                        self.assertEqual(actual["result_sha256"], expected["result_sha256"])
                        self.assertEqual(actual["row_count"], expected["row_count"])
                        self.assertGreaterEqual(actual["latency_ms"], 0.0)

                    index_query = "Q02" if layout == "og_jsonb_hot" else "Q05"
                    plan = self.adapter.collect_plan(
                        layout,
                        index_query,
                        {**self.truth["parameters"][index_query], "key_map": self.truth["key_map"]},
                        watermark=512,
                    )
                    self.assertTrue(plan["natural"])
                    self.assertTrue(plan["forced"])
                    if layout == "og_jsonb_hot":
                        self.assertIn("analytics_hot_operation_idx", plan["forced"])
                    if layout == "og_jsonb_gin":
                        self.assertIn("analytics_gin_attributes_idx", plan["forced"])
                finally:
                    connection.close()

                raw = self.adapter.verify_raw(layout, self.truth)
                self.assertTrue(raw["ok"])
                storage = self.adapter.collect_storage(layout)
                self.assertEqual(set(storage), {"analytics", "raw"})
                self.assertGreater(storage["analytics"]["total_bytes"], 0)
                self.adapter.cleanup(layout)
                self.assertFalse(self.adapter.schema_exists(layout))

    def test_insert_block_rolls_back_analytics_when_raw_copy_fails(self):
        """捕获 raw COPY 失败后 analytics block 被部分提交的回归。"""
        layout = "og_jsonb"
        row = self.rows[0]
        try:
            self.adapter.create_layout(layout, budget=32)
            connection = self.adapter.connect_worker()
            try:
                with connection.transaction():
                    connection.execute(
                        "INSERT INTO " + self.adapter._raw(layout) + "(event_id,raw_event) "
                        "VALUES (%s, %s)",
                        (row["event_id"], row["raw_event"]),
                    )
            finally:
                connection.close()

            with self.assertRaises(Exception):
                self.adapter.insert_block(layout, [row])

            connection = self.adapter.connect_worker()
            try:
                count = connection.execute(
                    "SELECT count(*) FROM " + self.adapter._analytics(layout)
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)
        finally:
            self.adapter.cleanup(layout)
            self.assertFalse(self.adapter.schema_exists(layout))

    @staticmethod
    def _fixture_truth(rows, truth, watermark):
        """从正式 truth 复制前缀水位所需的不可变字段。"""
        query_results = {
            query_id: {str(watermark): truth["queries"][query_id][str(watermark)]}
            for query_id in opengauss.QUERY_IDS
        }
        return {
            "key_map": truth["key_map"],
            "parameters": truth["parameters"],
            "queries": query_results,
            "records": truth["records"][:watermark],
        }


if __name__ == "__main__":
    unittest.main()
