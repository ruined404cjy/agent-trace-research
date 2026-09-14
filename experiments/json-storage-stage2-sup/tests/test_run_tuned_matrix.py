import sys
import unittest
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import run_tuned_matrix as runner


class TunedMatrixUnitTest(unittest.TestCase):
    """验证调优矩阵的目标顺序、专用查询和访问路径门禁。"""

    def test_default_order_runs_tuned_targets_before_baselines(self):
        self.assertEqual(
            runner.DEFAULT_TARGETS,
            (
                "og_jsonb_tuned",
                "ch_native_numeric_hint",
                "ch_native_trace_sort",
                "og_jsonb_baseline",
                "ch_native_baseline",
            ),
        )

    def test_optional_text_baselines_cover_json_and_string_json(self):
        """保证当前契约可补测两种文本 JSON 表示。"""
        self.assertEqual(runner.TARGETS["og_json_baseline"]["layout"], "og_json")
        self.assertEqual(runner.TARGETS["ch_string_baseline"]["layout"], "ch_string")
        self.assertEqual(
            runner.parse_targets("og_json_baseline,ch_string_baseline"),
            ("og_json_baseline", "ch_string_baseline"),
        )

    def test_gin_query_preserves_selective_filter_truth(self):
        statement = runner.opengauss_gin_sql()

        self.assertIn("attributes @> %s::jsonb", statement)
        self.assertIn("project_id = %s", statement)
        self.assertIn("start_time >= %s", statement)
        self.assertIn("ORDER BY event_id", statement)

    def test_access_path_gate_requires_each_declared_optimization(self):
        evidence = {
            "dense_forced_plan": "Bitmap Index Scan on analytics_operation_name_idx",
            "dense_forced_idx_scan_delta": 32,
            "dense_forced_expected_index_scans": 32,
            "query_stage_index_scans": {"analytics_operation_name_idx": 31},
            "query_stage_expected_index_scans": 31,
            "failure_btree_plan": "Index Scan using analytics_failure_mode_idx",
            "failure_gin_plan": "Bitmap Index Scan on analytics_attributes_gin_idx",
            "trace_plan": "Index Scan using analytics_trace_lookup_idx",
        }
        self.assertTrue(runner.opengauss_access_gate(evidence)["ok"])

        evidence["failure_btree_plan"] = "Seq Scan on analytics"
        gate = runner.opengauss_access_gate(evidence)
        self.assertFalse(gate["ok"])
        self.assertIn("expression_btree", gate["missing"])

        evidence["failure_btree_plan"] = "Index Scan using analytics_failure_mode_idx"
        evidence["dense_forced_plan"] = "Seq Scan on analytics"
        gate = runner.opengauss_access_gate(evidence)
        self.assertFalse(gate["ok"])
        self.assertIn("dense_expression_btree", gate["missing"])

        evidence["dense_forced_plan"] = "Bitmap Index Scan on analytics_operation_name_idx"
        evidence["dense_forced_idx_scan_delta"] = 1
        gate = runner.opengauss_access_gate(evidence)
        self.assertFalse(gate["ok"])
        self.assertIn("dense_expression_execution", gate["missing"])

        evidence["dense_forced_idx_scan_delta"] = 32
        evidence["query_stage_index_scans"]["analytics_operation_name_idx"] = 0
        gate = runner.opengauss_access_gate(evidence)
        self.assertFalse(gate["ok"])
        self.assertIn("dense_natural_execution", gate["missing"])

    def test_selectivity_probes_use_uniform_scalar_output_and_fixed_truth(self):
        """防止把不同返回量误当作高低选择率的影响。"""
        probes = runner.OPENGAUSS_SELECTIVITY_PROBES

        self.assertEqual(probes["dense_operation"]["expected_count"], 20155)
        self.assertEqual(probes["medium_operation"]["expected_count"], 4277)
        self.assertEqual(probes["sparse_failure"]["expected_count"], 741)
        for probe in probes.values():
            statement = runner.opengauss_selectivity_sql(probe["path"])
            self.assertTrue(statement.startswith("SELECT count(*)"))
            self.assertIn("attributes #>>", statement)
            self.assertNotIn("ORDER BY", statement)

    def test_force_index_sql_embeds_scan_hint_in_executed_statement(self):
        """防止强制计划只影响 EXPLAIN，而正式样本继续执行顺序扫描。"""
        statement = runner.opengauss_force_index_sql(
            "SELECT count(*) FROM example.analytics",
            "analytics_operation_name_idx",
        )

        self.assertEqual(
            statement,
            "SELECT /*+ indexscan(analytics analytics_operation_name_idx) */ count(*) FROM example.analytics",
        )
        with self.assertRaisesRegex(ValueError, "index name"):
            runner.opengauss_force_index_sql("SELECT count(*) FROM example.analytics", "bad-name")

    def test_index_scan_wait_polls_delayed_statistics(self):
        """容许统计收集器延迟发布刚完成的索引扫描。"""
        with (
            mock.patch.object(runner, "_og_index_scan_count", side_effect=[3, 5, 7]) as scan_count,
            mock.patch.object(runner.time, "sleep") as sleep,
        ):
            actual = runner._og_wait_index_scan_count(
                mock.sentinel.adapter,
                "analytics_operation_name_idx",
                minimum=7,
                attempts=4,
                interval_seconds=0.01,
            )

        self.assertEqual(actual, 7)
        self.assertEqual(scan_count.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_clickhouse_gate_requires_hint_primary_key_and_path_inventory(self):
        evidence = {
            "ddl": "`experiment.duration_ms` Int64 ORDER BY (project_id,trace_id,start_time,event_id)",
            "trace_plan": "Indexes:\n  PrimaryKey\n    Keys:\n      project_id\n      trace_id\n    Search Algorithm: binary search",
            "dynamic_paths": ["gen_ai.output.messages"],
            "path_parts": [{"part": "p", "dynamic_paths": ["gen_ai.output.messages"], "shared_paths": ["rare.path"]}],
            "shared_paths": ["rare.path"],
        }

        gate = runner.clickhouse_access_gate(evidence)

        self.assertTrue(gate["ok"])
        self.assertEqual(gate["numeric_path_storage"], "type_hint")
        self.assertEqual(gate["trace_access_path"], "primary_key")


if __name__ == "__main__":
    unittest.main()
