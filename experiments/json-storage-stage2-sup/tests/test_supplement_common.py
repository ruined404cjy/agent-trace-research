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
        self.assertEqual(common.CONTRACT_VERSION, "json-storage-four-layout-v1")
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

if __name__ == "__main__":
    unittest.main()
