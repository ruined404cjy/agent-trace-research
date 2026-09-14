import sys
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import supplement_common as common


class SupplementCommonTest(unittest.TestCase):
    """验证补充实验共享的固定执行契约。"""

    def test_layouts_and_round_orders_cover_each_layout_once_per_round(self):
        """捕获遗漏布局、布局重复或顺序偏离固定 Latin square 的错误。"""
        self.assertEqual(common.CONTRACT_VERSION, "json-storage-tuned-matrix-v2")
        self.assertEqual(common.LAYOUTS, ("og_json", "og_jsonb", "ch_string", "ch_native"))
        self.assertEqual(len(common.ROUND_ORDERS), 4)
        self.assertEqual(set(common.ROUND_ORDERS[0]), set(common.LAYOUTS))
        self.assertEqual(
            common.ROUND_ORDERS,
            (
                ("og_json", "og_jsonb", "ch_string", "ch_native"),
                ("og_jsonb", "ch_native", "og_json", "ch_string"),
                ("ch_string", "og_json", "ch_native", "og_jsonb"),
                ("ch_native", "ch_string", "og_jsonb", "og_json"),
            ),
        )
        for order in common.ROUND_ORDERS:
            self.assertEqual(set(order), set(common.LAYOUTS))

    def test_fixed_query_results_are_bound_to_reviewed_truth(self):
        """捕获汇总用的固定查询摘要或返回行数漂移。"""
        self.assertEqual(set(common.EXPECTED_RESULT_SHA256), set(common.QUERY_IDS))
        self.assertTrue(all(len(value) == 64 for value in common.EXPECTED_RESULT_SHA256.values()))
        self.assertEqual(common.QUERY_IDS, ("S01", "S02", "S03", "S04", "S05", "S06", "S07"))
        self.assertEqual(common.EXPECTED_ROW_COUNTS,
                         {"S01": 3, "S02": 1, "S03": 741, "S04": 4277, "S05": 6, "S06": 256, "S07": 1})
        self.assertEqual(common.FOUR_LAYOUT_V1_CONTRACT_VERSION, "json-storage-four-layout-v1")
        self.assertEqual(common.FOUR_LAYOUT_V1_QUERY_IDS, common.QUERY_IDS[:6])

    def test_derived_attributes_add_numeric_control_without_mutating_input(self):
        """保证实验数值路径由独立列确定性派生，并保持冻结输入对象不变。"""
        original = {"gen_ai": {"operation": {"name": "chat"}}}

        derived = common.derived_attributes({"attributes_analysis": original, "duration_ms": 17})

        self.assertEqual(derived["experiment"]["duration_ms"], 17)
        self.assertNotIn("experiment", original)

if __name__ == "__main__":
    unittest.main()
