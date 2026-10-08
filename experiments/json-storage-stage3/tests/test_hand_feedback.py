import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "report"))

import hand_feedback as hand


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def scenario(app, query_complete=None):
    stats = {"application_ready_ms": {"p50": app}}
    if query_complete is not None:
        stats["query_complete_ms"] = {"p50": query_complete}
    return {"round_statistic_median": stats}


def stream(p50, p95, dropped, successful=6000):
    return {"latency_ms": {"p50": p50, "p95": p95},
            "counts": {"dropped_requests": dropped, "successful_requests": successful}}


class FormatTest(unittest.TestCase):
    """数值规则决定转述的字符数与精度，接收方按同一规则还原。"""

    def test_number_rules(self):
        self.assertEqual([hand.number(v) for v in (28.25, 28.0, 99.96, 124.7, 1541.9, 7, None)],
                         ["28.2", "28", "100", "125", "1542", "7", "NA"])
        self.assertEqual([hand.fine(v) for v in (0.64, 0.871, 3.30, 0.0, 47.6)], ["0.64", "0.87", "3.3", "0", "47.6"])
        self.assertEqual(hand.megabytes(219_824_128), "220")
        self.assertEqual(hand.megabytes(44_564_480), "44.6")


class VerifyTest(unittest.TestCase):
    """校验码让接收方定位抄错的节，而不是整份重抄。"""

    def test_typo_is_located_to_its_section(self):
        lines = ["V2 161 2026-10-08", f"XE {hand.crc(['518 2037'])}", "518 2037",
                 f"XW {hand.crc(['9493 220 0'])}", "9493 220 0", "XA ERR", "KeyError: 'cases'", "N", "N merge ok"]
        results = dict(hand.verify_lines(lines))
        self.assertEqual((results["XE"], results["XW"], results["XA"], results["XM"]), ("ok", "ok", "err", "missing"))
        lines[4] = "9439 220 0"
        self.assertEqual(dict(hand.verify_lines(lines))["XW"], "mismatch")


class HandLinesTest(unittest.TestCase):
    """手敲版从 pack 与 check 的产物取数；单节失败不影响其余各节。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        main = {name: scenario(28.3) for name in hand.MATRIX_SCENARIOS}
        main["list:first"] = scenario(28.6, 18.2)
        main["list:middle"] = scenario(29.1, 18.9)
        write_json(self.dir / "summary" / "matrix-xstore.json", {"matrix": [
            {"engine": "xstore", "layout": "same_table", "workloads": {"main": {"scenarios": main}}}]})
        checks = [
            "F3 server same_table round-1 list:first total_runtime_ms 0.64 top_node_ms 0.55 removed 0",
            "F3 server same_table round-1 list:middle total_runtime_ms 0.87 top_node_ms 0.71 removed 6",
            "F3 server same_table round-2 list:middle total_runtime_ms 0.91 top_node_ms 0.75 removed 6",
            "F2 ch-interference-same_table batch_loop list 6000 status=dropped:20 "
            "dropped_by=arrival_deadline_missed:3,worker_capacity_unavailable:17 p50/p95/p99/max=1/2/3/4",
            "F4 exec_select_1 p50 0.16 p95 0.25 max 0.52",
            "F4 NA ConnectionError: refused",
            "F6 list_first rows 256 cols 11 fetchall_p50_ms 3.300 fetchall_p95_ms 3.5 getvalue_floor_p50_ms 1.900",
        ]
        (self.dir / "followup").mkdir()
        (self.dir / "followup" / "checks.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")
        phases = [{"phase": "quiet", "streams": {"list": stream(30.0, 40.0, 1)}},
                  {"phase": "batch_loop", "streams": {"list": stream(31.0, 60.0, 20),
                                                      "batch_loop": stream(2100.0, 2400.0, 0, 140)}}]
        write_json(self.dir / "summary" / "interference-clickhouse.json",
                   {"layouts": [{"layout": "same_table", "phases": phases}]})
        write_json(self.dir / "summary" / "asset-failures.json", {"engines": [
            {"engine": "xstore", "cases": [{"case": name, "final_status": status} for name, status in zip(
                hand.ASSET_CASES, ("available", "available", "available", "absent", "failed", "deleting"))]},
            {"engine": "clickhouse", "cases": "malformed"}]})

    def section(self, lines, code):
        start = next(index for index, line in enumerate(lines) if line.split(" ")[0] == code)
        end = next(index for index in range(start + 1, len(lines))
                   if lines[index].split(" ")[0] in {name for name, _ in hand.SECTIONS} | {"N"})
        return lines[start], lines[start + 1:end]

    def test_sections_follow_the_fixed_layout_and_field_order(self):
        lines = hand.hand_lines(self.dir, "161", "2026-10-08")
        self.assertEqual(lines[0], "V2 161 2026-10-08")
        self.assertEqual(lines[-1], "N")
        _, xm = self.section(lines, "XM")
        self.assertEqual(xm[0], "28.6 29.1 28.3 28.3 28.3 28.3 28.3 28.3")
        self.assertEqual(xm[1], " ".join(["NA"] * 8))
        header, xq = self.section(lines, "XQ")
        # 中间页的服务端时间取各轮中位数，removed 证明游标下界生效。
        self.assertEqual(xq[0], "18.2 0.64 18.9 0.89 6 NA")
        self.assertEqual(header, f"XQ {hand.crc(xq)}")
        _, xa = self.section(lines, "XA")
        self.assertEqual(xa, ["AAAXFD NA NA NA NA"])
        _, xd = self.section(lines, "XD")
        self.assertEqual(xd, ["0.16 NA NA 3.3 NA NA 1.9"])
        _, rows = self.section(lines, "I")
        self.assertEqual(rows[4], "30 40 1|NA NA|31 60 20 3|NA NA NA|2100 140 NA")

    def test_a_failing_section_is_reported_and_the_rest_continue(self):
        lines = hand.hand_lines(self.dir, "161", "2026-10-08")
        index = lines.index("CA ERR")
        self.assertTrue(lines[index + 1].startswith("TypeError"))
        self.assertIn("N", lines[index + 2:])
        results = dict(hand.verify_lines(lines))
        self.assertEqual(results["CA"], "err")
        self.assertEqual(results["XQ"], "ok")


if __name__ == "__main__":
    unittest.main()
