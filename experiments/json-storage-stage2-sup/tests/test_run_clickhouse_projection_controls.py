import sys
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import run_clickhouse_projection_controls as runner


class ProjectionControlRunnerTest(unittest.TestCase):
    def test_schedule_uses_abba_order_with_equal_counts(self):
        """防止时间漂移与固定布局先后顺序混在一起。"""
        schedule = runner.control_schedule(4)

        self.assertEqual(schedule, [
            "ch_string", "ch_native", "ch_native", "ch_string",
            "ch_native", "ch_string", "ch_string", "ch_native",
        ])
        self.assertEqual(schedule.count("ch_string"), 4)
        self.assertEqual(schedule.count("ch_native"), 4)

    def test_summary_reports_client_and_server_components(self):
        """防止汇总只保留客户端 wall，无法分辨服务端执行与结果传输。"""
        samples = [{
            "layout": "ch_string",
            "latency_ms": value,
            "query_log": {"query_duration_ms": value - 1, "read_bytes": 10, "result_bytes": 5},
            "recovery_ms": 0.5,
            "response_bytes": 8,
            "response_read_ms": 1.0,
        } for value in (4.0, 2.0, 3.0)]

        self.assertEqual(runner.summarize_samples(samples), {
            "ch_string": {
                "latency_ms": {"p50": 3.0, "p95": 4.0},
                "query_duration_ms": {"p50": 2.0, "p95": 3.0},
                "read_bytes": {"p50": 10, "p95": 10},
                "recovery_ms": {"p50": 0.5, "p95": 0.5},
                "response_bytes": {"p50": 8, "p95": 8},
                "response_read_ms": {"p50": 1.0, "p95": 1.0},
                "result_bytes": {"p50": 5, "p95": 5},
                "samples": 3,
            }
        })

    def test_aggregate_gates_count_but_retains_layout_serialization_bytes(self):
        """防止 Native JSON 文本编码长度差异被误判为路径值错误。"""
        class Adapter:
            def execute_projection_control(self, *args):
                return {
                    "result": {"non_null_count": 2, "utf8_bytes": 9},
                    "result_sha256": "digest",
                }

        sample = runner._sample(
            Adapter(), object(), "ch_native", "aggregate", {},
            {"non_null_count": 2, "utf8_bytes": 7}, "measurement",
        )

        self.assertTrue(sample["matches_truth"])
        self.assertEqual(sample["result"]["utf8_bytes"], 9)


if __name__ == "__main__":
    unittest.main()
