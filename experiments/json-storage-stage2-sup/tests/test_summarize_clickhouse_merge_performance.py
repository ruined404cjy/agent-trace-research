import sys
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "report"))

import summarize_clickhouse_merge_performance as summary


class ClickHouseMergeSummaryTest(unittest.TestCase):
    """验证 merge 探针按目标和阶段独立汇总。"""

    def test_summary_keeps_ingest_and_query_stages_separate(self):
        result = {
            "target": "ch_native_paused",
            "layout": "ch_native",
            "mode": "paused",
            "status": "complete",
            "cleanup": {"removed": True},
            "ingest": {"blocks": [{"rows": 256, "block_wall_ms": 40.0}]},
            "ingest_merge_observer": {"max_active_merges": 0},
            "stages": {
                "fragmented": self.stage(190, 8.0, 1000),
                "background_merging": self.stage(120, 9.0, 900),
                "background_stable": self.stage(4, 5.0, 500),
                "single_part": self.stage(1, 4.0, 400),
            },
            "maintenance": {
                "background_wait_ms": 100.0,
                "optimize_final_ms": 50.0,
            },
        }

        target = summary.summarize_results([result])["targets"]["ch_native_paused"]

        self.assertEqual(target["ingest"]["rows_per_second"], 6400.0)
        self.assertEqual(target["ingest"]["max_active_merges"], 0)
        self.assertEqual(target["stages"]["fragmented"]["part_count"]["analytics"], 190)
        self.assertEqual(target["stages"]["fragmented"]["part_count_before"]["analytics"], 191)
        self.assertEqual(target["stages"]["background_merging"]["max_active_merges"], 3)
        self.assertEqual(target["stages"]["single_part"]["query_latency_ms"]["S02"]["p50"], 4.0)
        self.assertEqual(target["stages"]["background_stable"]["read_bytes"]["S02"]["p50"], 500)

    def test_summary_rejects_incomplete_cleanup(self):
        result = {
            "target": "ch_string_active", "status": "complete",
            "cleanup": {"removed": False},
        }
        with self.assertRaisesRegex(ValueError, "cleanup"):
            summary.summarize_results([result])

    @staticmethod
    def stage(parts, latency, read_bytes):
        return {
            "merge_observer": {"max_active_merges": 3},
            "storage_before": {
                "tables": {
                    "analytics": {"part_count": parts + 1},
                    "raw": {"part_count": parts + 1},
                }
            },
            "storage": {
                "tables": {
                    "analytics": {"part_count": parts},
                    "raw": {"part_count": parts},
                }
            },
            "query_stage": {
                "samples": [{
                    "phase": "measurement",
                    "query_id": "S02",
                    "latency_ms": latency,
                    "recovery_ms": 1.0,
                    "query_log": {"read_rows": 10, "read_bytes": read_bytes},
                }]
            },
        }


if __name__ == "__main__":
    unittest.main()
