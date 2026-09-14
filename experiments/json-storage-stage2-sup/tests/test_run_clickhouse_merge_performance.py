import sys
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import run_clickhouse_merge_performance as runner


class ClickHouseMergePerformanceTest(unittest.TestCase):
    """验证 merge 性能探针的状态切换和拓扑门禁。"""

    def test_round_orders_cover_each_target_once(self):
        expected = {
            "ch_string_active", "ch_native_active",
            "ch_string_paused", "ch_native_paused",
        }
        for order in runner.ROUND_ORDERS:
            self.assertEqual(set(order), expected)
            self.assertEqual(len(order), len(expected))

    def test_paused_merges_restores_both_owned_tables_after_error(self):
        class Adapter:
            def __init__(self):
                self.calls = []

            def set_merges(self, layout, enabled):
                self.calls.append((layout, enabled))

        adapter = Adapter()
        with self.assertRaisesRegex(RuntimeError, "probe failed"):
            with runner.paused_merges(adapter, "ch_native"):
                raise RuntimeError("probe failed")

        self.assertEqual(adapter.calls, [("ch_native", False), ("ch_native", True)])

    def test_fragmented_gate_requires_one_part_per_inserted_block(self):
        storage = {
            "tables": {
                "analytics": {"part_count": 190},
                "raw": {"part_count": 190},
            }
        }
        self.assertTrue(runner.fragmented_gate(storage, 190)["ok"])

        storage["tables"]["raw"]["part_count"] = 189
        gate = runner.fragmented_gate(storage, 190)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["actual"], {"analytics": 190, "raw": 189})


if __name__ == "__main__":
    unittest.main()
