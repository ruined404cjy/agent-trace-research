import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "tools"))

import check_handback as check


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def sample(stream, sequence, status, duration=None, error=None):
    return {"stream": stream, "sequence": sequence, "status": status, "duration_ms": duration, "error": error}


class FakeCursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))

    def fetchall(self):
        return [(1,)]


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self.calls)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class CheckHandbackTest(unittest.TestCase):
    """补充核对只读已有结果：各项按固定目录取数，单项失败写成 NA 行而不中断其余项。"""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp)
        self.root = self.temp / "runs"
        self.feedback = self.temp / "feedback"

    def test_interference_separates_capacity_drops_from_stalls(self):
        # 连续丢弃长度区分集中卡顿与均匀的容量不足；丢弃原因原样计数，耗时只统计成功请求。
        phase = self.root / "xstore-interference-same_table" / "child" / "batch_loop"
        phase.mkdir(parents=True)
        rows = [sample("list", 0, "success", 10.0), sample("list", 1, "dropped", error="worker capacity unavailable"),
                sample("list", 2, "dropped", error="worker capacity unavailable"),
                sample("list", 3, "dropped", error="arrival deadline missed"), sample("list", 4, "success", 30.0)]
        (phase / "samples.jsonl").write_text("\n".join(json.dumps(row) for row in reversed(rows)) + "\n")
        (phase / "warmup-samples.jsonl").write_text(json.dumps(sample("list", 0, "success", 999.0)) + "\n")
        lines = check.interference_lines(self.root)
        measured = [line for line in lines if line.startswith("F2 xstore-interference-same_table ")]
        self.assertEqual(measured, [
            "F2 xstore-interference-same_table batch_loop list 5 status=dropped:3,success:2 "
            "dropped_by=arrival_deadline_missed:1,worker_capacity_unavailable:2 "
            "p50/p95/p99/max=30.0/30.0/30.0/30.0 max_consecutive_dropped=3"])
        self.assertIn("F2 NA ch-interference-asset_ref has no samples.jsonl", lines)

    def test_xstore_plan_lines_pair_server_runtime_with_client_medians(self):
        target = self.root / "xstore-main" / "xstore"
        for layout in check.pack_handback.LAYOUTS:
            write_json(target / layout / "result.json", {"summary": {"main": {
                scenario: {"latency_ms": {"median": 28.3}, "query_complete_ms": {"median": 27.9}}
                for scenario in check.SMALL_SCENARIOS}}})
        write_json(target / "same_table" / "rounds" / "main" / "round-1" / "run-manifest.json", {
            "access_validation": {"q1": {"scenario": "list:first"}, "q2": {"scenario": "batch:main"}},
            "access": {"plans": {"q1": "Limit (cost=0..1) (actual time=0.05..0.84 rows=256 loops=1)\n"
                                       "Total runtime: 1.02 ms", "q2": "Seq Scan"}}})
        lines = check.xstore_plan_lines(self.root)
        self.assertIn("F3 client same_table list:first latency_median 28.3 query_complete_median 27.9", lines)
        self.assertIn("F3 server same_table round-1 list:first total_runtime_ms 1.02 top_node_ms 0.84", lines)
        self.assertFalse(any("batch:main" in line for line in lines))

    def test_roundtrip_times_each_case_on_one_connection_and_closes_it(self):
        connection = FakeConnection()
        lines = check.roundtrip_lines(lambda: connection, repeat=5, warmup=1)
        self.assertEqual([line.split(" ")[1] for line in lines], [name for name, _, _ in check.ROUNDTRIP_CASES])
        self.assertEqual(len(connection.calls), 6 * len(check.ROUNDTRIP_CASES))
        self.assertIn(("SELECT %s::int", (1,)), connection.calls)
        self.assertTrue(connection.closed)

    def test_code_lines_group_non_head_files_by_run_directory(self):
        write_json(self.feedback / "feedback-manifest.json", {
            "code_runs": [
                {"run": "xstore-main/xstore/same_table/rounds/main/round-1/run-manifest.json", "all_head": False,
                 "code": [{"path": "runner/production.py", "status": "local"},
                          {"path": "runner/common.py", "status": "head"}]},
                {"run": "xstore-main/xstore/separate/run-manifest.json", "all_head": False,
                 "code": [{"path": "runner/production.py", "status": "local"}]},
                {"run": "xstore-asset-failures/run-manifest.json", "all_head": True, "code": []}],
            "code_files": [{"path": "runner/production.py", "sha256": "ab" * 32, "status": "local",
                            "copy": "code/local/abababababab/runner/production.py"}]})
        lines = check.code_lines(self.feedback)
        self.assertEqual(lines[0], "F1 runs 3 non_head 2")
        self.assertIn("F1 non_head xstore-main runner/production.py local 2", lines)
        self.assertIn("F1 head_run xstore-asset-failures/run-manifest.json", lines)
        self.assertIn("F1 local runner/production.py abababababab "
                      "code/local/abababababab/runner/production.py kept=False", lines)

    def test_one_failing_item_is_reported_and_the_rest_still_run(self):
        self.feedback.mkdir()
        (self.feedback / "feedback.txt").write_text("B2\nx\nB3\n1.0 2.0 0 0 heap\nNA 缺失\nB4\ny\n", encoding="utf-8")

        def refuse():
            raise ConnectionError("socket missing")

        lines = check.check(self.feedback, self.root, connect=refuse)
        self.assertTrue(lines[0].startswith("F1 NA FileNotFoundError"))
        self.assertIn("F4 NA ConnectionError: socket missing", lines)
        self.assertEqual([line for line in lines if line.startswith("F5")], ["F5 1.0 2.0 0 0 heap", "F5 NA 缺失"])
        self.assertEqual((self.feedback / "followup" / "checks.txt").read_text(encoding="utf-8"),
                         "\n".join(lines) + "\n")


if __name__ == "__main__":
    unittest.main()
