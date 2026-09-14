import json
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "report"))

import summarize_tuned_matrix as summary


class TunedMatrixSummaryTest(unittest.TestCase):
    """验证调优矩阵采用轮内中位数与轮间中位数。"""

    def test_summarize_uses_round_level_statistics(self):
        rounds = [
            self.result("og_jsonb_tuned", [1, 3, 100], 1000, 10, 200),
            self.result("og_jsonb_tuned", [5, 7, 9], 1000, 20, 220),
            self.result("og_jsonb_tuned", [11, 13, 15], 1000, 25, 240),
        ]

        result = summary.summarize_results(rounds)

        target = result["targets"]["og_jsonb_tuned"]
        self.assertEqual(target["query_latency_ms"]["S07"]["p50"], 7)
        self.assertAlmostEqual(target["application_ready_ms"]["S07"]["p50"], 7.1)
        self.assertEqual(target["ingest"]["rows_per_second"], 50000)
        self.assertEqual(target["storage"]["solution_bytes"], 220)

    def test_load_results_can_select_public_targets(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "run-manifest.json").write_text(
                json.dumps({"status": "complete", "targets": ["public", "diagnostic"]}),
                encoding="utf-8",
            )
            for target in ("public", "diagnostic"):
                (directory / f"result-{target}.json").write_text(
                    json.dumps({"target": target}), encoding="utf-8",
                )

            results, _ = summary.load_results([directory], selected_targets={"public"})

        self.assertEqual([result["target"] for result in results], ["public"])

    def test_query_ready_throughput_includes_engine_specific_maintenance(self):
        opengauss = self.result(
            "og_jsonb_tuned", [1, 2, 3], 1000, 100, 200,
            maintenance={"analyze_ms": 50, "analyzed": True},
        )
        clickhouse = self.result(
            "ch_native_baseline", [1, 2, 3], 1000, 100, 200,
            engine="clickhouse",
            maintenance={
                "before_optimize": {"waited_seconds": 0.2},
                "optimize_final_ms": {"analytics": 100, "raw": 50},
                "waited_seconds": 0.1,
            },
        )

        result = summary.summarize_results([opengauss, clickhouse])

        og_target = result["targets"]["og_jsonb_tuned"]
        self.assertEqual(og_target["maintenance"]["wall_ms"], 50)
        self.assertEqual(og_target["query_ready"]["wall_ms"], 150)
        self.assertAlmostEqual(og_target["query_ready"]["rows_per_second"], 6666.666666666667)
        ch_target = result["targets"]["ch_native_baseline"]
        self.assertEqual(ch_target["maintenance"]["wall_ms"], 450)
        self.assertEqual(ch_target["maintenance"]["before_optimize_wait_ms"], 200)
        self.assertEqual(ch_target["maintenance"]["optimize_final_ms"], 150)
        self.assertEqual(ch_target["maintenance"]["after_optimize_wait_ms"], 100)
        self.assertEqual(ch_target["query_ready"]["wall_ms"], 550)
        self.assertAlmostEqual(ch_target["query_ready"]["rows_per_second"], 1818.1818181818182)

    def test_special_index_probes_only_summarize_measurement_samples(self):
        result = self.result("og_jsonb_tuned", [1, 2, 3], 1000, 100, 200)
        result["access_evidence"] = {
            "dense_forced_samples": [
                {"phase": "warmup", "latency_ms": 900},
                {"phase": "measurement", "latency_ms": 30},
                {"phase": "measurement", "latency_ms": 10},
            ],
            "dense_forced_plan": "Bitmap Index Scan on analytics_operation_name_idx",
            "dense_forced_idx_scan_delta": 4,
            "dense_forced_expected_index_scans": 4,
            "gin_samples": [
                {"phase": "warmup", "latency_ms": 800},
                {"phase": "measurement", "latency_ms": 20},
                {"phase": "measurement", "latency_ms": 4},
            ],
            "selectivity_probes": {
                "dense_operation": {
                    "natural": {
                        "samples": [{"phase": "measurement", "latency_ms": 8}],
                        "plan": "Seq Scan on analytics",
                        "idx_scan_delta": 0,
                        "expected_index_scans": 0,
                    },
                    "forced": {
                        "samples": [{"phase": "measurement", "latency_ms": 18}],
                        "plan": "Index Scan using analytics_operation_name_idx",
                        "idx_scan_delta": 2,
                        "expected_index_scans": 2,
                    },
                }
            },
        }

        target = summary.summarize_results([result])["targets"]["og_jsonb_tuned"]

        self.assertEqual(target["special_latency_ms"]["dense_json_expression_btree_forced"]["p50"], 20)
        self.assertEqual(target["special_latency_ms"]["sparse_json_containment_gin"]["p50"], 12)
        self.assertEqual(target["special_latency_ms"]["selectivity_dense_operation_natural"]["p50"], 8)
        self.assertEqual(target["special_latency_ms"]["selectivity_dense_operation_forced"]["p50"], 18)
        self.assertEqual(target["special_access"]["dense_json_expression_btree_forced"]["idx_scan_delta"], 4)
        self.assertFalse(target["special_access"]["selectivity_dense_operation_natural"]["all_plans_use_index"])
        self.assertTrue(target["special_access"]["selectivity_dense_operation_forced"]["all_plans_use_index"])

    @staticmethod
    def result(target, latencies, rows, ingest_ms, storage_bytes, engine="opengauss", maintenance=None):
        storage = (
            {
                "analytics": {"total_bytes": storage_bytes - 20},
                "raw": {"total_bytes": 20},
            }
            if engine == "opengauss" else
            {
                "tables": {
                    "analytics": {"compressed_bytes": storage_bytes - 20},
                    "raw": {"compressed_bytes": 20},
                }
            }
        )
        return {
            "access_gate": {"ok": True},
            "cleanup": {"removed": True},
            "engine": engine,
            "ingest": {"blocks": [{"rows": rows, "block_wall_ms": ingest_ms}]},
            "maintenance": maintenance or {"analyze_ms": 0, "analyzed": True},
            "profile": "tuned",
            "query_stage": {
                "samples": [
                    {"query_id": "S07", "phase": "measurement", "latency_ms": value, "recovery_ms": 0.1}
                    for value in latencies
                ]
            },
            "status": "complete",
            "storage": storage,
            "target": target,
        }


if __name__ == "__main__":
    unittest.main()
