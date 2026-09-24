import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "report"))

import pack_handback as pack


MODULE = "experiments/json-storage-stage3/runner/mod.py"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def git(repo, *arguments):
    subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def round_manifest(code_path, code_text, round_index):
    """一个 ClickHouse 矩阵轮次清单：两 block 写入、末轮空间、一条列表访问证据。"""
    return {
        "status": "complete", "engine": "clickhouse", "workload": "main", "round_index": round_index,
        "code": {"runner": {"path": code_path, "bytes": len(code_text), "sha256": sha(code_text)}},
        "engine_runtime": {"version": "23.3.10.5", "source": "database-query"},
        "write": {"wall_ms": 30.0, "block_wall_ms": {"median": 5.0},
                  "blocks": [{"ingest": {"wall_ms": 4.0}}, {"ingest": {"wall_ms": 6.0}}]},
        "storage": {"tables": {"events": {"part_count": 6, "rows": 4, "marks": 3, "compressed_bytes": 10,
                                          "uncompressed_bytes": 20,
                                          "columns": {"payload": {"compressed_bytes": 7,
                                                                  "uncompressed_bytes": 15}}}},
                    "asset_store": None},
        "access_validation": {"q1": {"scenario": "list:first", "kind": "list", "scanned_rows": 12,
                                     "scanned_bytes": 900, "access_structure": "primary-key-mark-pruning",
                                     "expected_sources": ["jsons3_x_same_table.events"]}},
        "access": {"plans": {"q1": '{"explain":"ReadFromMergeTree (jsons3_x_same_table.events)"}\n'
                                   '{"explain":"        Parts: 1/6"}'},
                   "query_details": {"q1": {"payload_selected": False}},
                   "query_finish": {"q1": {"read_rows": 12, "read_bytes": 900}},
                   "index_scans": {}},
    }


class PackHandbackTest(unittest.TestCase):
    """验证打包保留全部内层清单、标注代码身份，并把缺失与失败写成可读原因。"""

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.temp)])
        self.repo = self.temp / "repo"
        (self.repo / MODULE).parent.mkdir(parents=True)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        for text in ("old version\n", "head version\n"):
            (self.repo / MODULE).write_text(text)
            git(self.repo, "add", ".")
            git(self.repo, "commit", "-q", "-m", text.strip())
        # 黄区运行时的检出路径与打包时不同，只有 experiments/ 之后的相对路径一致。
        self.run_checkout = self.temp / "yellow" / "agent-trace-research"
        local = self.run_checkout / MODULE
        local.parent.mkdir(parents=True)
        local.write_text("local edit\n")
        self.root = self.temp / "runs"
        target = self.root / "clickhouse-main" / "clickhouse" / "same_table"
        recorded = str(self.run_checkout / MODULE)
        write_json(target / "run-manifest.json", {"status": "complete"})
        write_json(target / "rounds" / "main" / "round-1" / "run-manifest.json",
                   round_manifest(recorded, "head version\n", 0))
        write_json(target / "rounds" / "main" / "round-2" / "run-manifest.json",
                   round_manifest(recorded, "old version\n", 1))
        write_json(target / "rounds" / "main" / "round-3" / "run-manifest.json",
                   round_manifest(recorded, "local edit\n", 2))
        write_json(target / "result.json", {"status": "complete"})
        for layout in ("separate", "full_core", "asset_ref"):
            write_json(self.root / "clickhouse-main" / "clickhouse" / layout / "run-manifest.json",
                       {"status": "complete"})
        (target / "samples.jsonl").write_text('{"sample": 1}\n' * 50)
        write_json(self.root / "ch-asset-failures" / "run-manifest.json", {
            "status": "complete", "runtime": {"engine": "clickhouse"},
            "asset_failures": {"results": [{
                "case": "missing", "injection_point": "remove_published_object",
                "resolver": {"error": "missing"}, "final_status": "available", "event_visible": True,
                "reconcile": {"orphan_count": 0}, "recovery_actions": ["restore_missing_object"],
            }]},
        })
        (self.root / "facts").mkdir()
        (self.root / "facts" / "host-checks.txt").write_text("load average: 0.10\n")
        self.destination = self.temp / "feedback"

    def run_pack(self, part_bytes=400):
        matrix = {"matrix": [{"engine": "clickhouse", "layout": "same_table", "workloads": {"main": {
            "scenarios": {"list:first": {"round_statistic_median": {"application_ready_ms": {"p50": 21.3}}}},
        }}}]}
        with patch.object(pack.summarize, "summarize", return_value=matrix), \
                patch.object(pack.summarize, "summarize_part_states", side_effect=ValueError("bad part")), \
                patch.object(pack.summarize, "summarize_interference", return_value={"layouts": []}), \
                patch.object(pack.summarize, "summarize_asset_failures",
                             side_effect=ValueError("two engines required")):
            return pack.pack(self.root, self.repo, "161", "2026-09-25", self.destination, part_bytes)

    def test_code_identity_marks_head_published_and_local_and_keeps_the_local_source(self):
        document = self.run_pack()
        statuses = {item["sha256"]: item for item in document["code_files"]}
        self.assertEqual(statuses[sha("head version\n")]["status"], "head")
        self.assertTrue(statuses[sha("old version\n")]["status"].startswith("published:"))
        local = statuses[sha("local edit\n")]
        self.assertEqual(local["status"], "local")
        self.assertEqual((self.destination / local["copy"]).read_text(), "local edit\n")
        self.assertIn("1 code files differ from every published commit", document["anomalies"])

    def test_every_inner_manifest_is_archived_in_parts_and_raw_samples_stay_local(self):
        document = self.run_pack(part_bytes=400)
        parts = sorted((self.destination / "raw").glob("manifests.tar.gz.part-*"))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part.stat().st_size <= 400 for part in parts))
        content = b"".join(part.read_bytes() for part in parts)
        self.assertEqual(hashlib.sha256(content).hexdigest(), document["archive"]["archive_sha256"])
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            names = set(archive.getnames())
        prefix = "clickhouse-main/clickhouse/same_table/"
        self.assertIn(prefix + "rounds/main/round-3/run-manifest.json", names)
        self.assertIn(prefix + "result.json", names)
        self.assertIn("ch-asset-failures/run-manifest.json", names)
        self.assertNotIn(prefix + "samples.jsonl", names)
        self.assertEqual(document["local_only_samples"], [{"path": prefix + "samples.jsonl", "bytes": 700}])

    def test_feedback_block_reports_values_missing_runs_and_summary_failures(self):
        document = self.run_pack()
        lines = (self.destination / "feedback.txt").read_text(encoding="utf-8").splitlines()
        a1 = lines[lines.index("A1") + 1:lines.index("A2")]
        self.assertEqual(a1[0], "NA 缺失 NA 缺失 NA 缺失 NA 缺失")
        # 目录齐全时取汇总器的值；汇总结果里没有的场景写“汇总失败”，不回退到其他口径。
        self.assertEqual(a1[4], "21.3 NA 汇总失败 NA 汇总失败 NA 汇总失败")
        a5 = lines[lines.index("A5") + 1:lines.index("A6")]
        # 写入合计为每轮 block ingest wall 之和取三轮中位数；单块 p50 取各轮中位数的中位数。
        self.assertEqual(a5[4], "10.0 5.0")
        b4 = lines[lines.index("B4") + 1:lines.index("C4")]
        self.assertEqual(b4[6], "remove_published_object missing available True 0 restore_missing_object")
        self.assertFalse(document["presence"]["matrix/xstore/same_table"])
        errors = {item.get("summary"): item["error"] for item in document["summary_errors"]}
        self.assertEqual(errors["part-states"], "run directories missing")
        self.assertEqual(errors["matrix-xstore"], "run directories missing")
        self.assertTrue((self.destination / "facts" / "host-checks.txt").is_file())
        self.assertTrue((self.destination / "results" / "clickhouse-same_table.json").is_file())

    def test_matrix_evidence_keeps_access_and_last_round_storage(self):
        self.run_pack()
        evidence = json.loads((self.destination / "evidence" / "matrix-clickhouse-same_table.json").read_text())
        group = evidence["rounds"][0]["scenarios"]["list:first"]
        self.assertEqual(group["plans"], {"ReadFromMergeTree (events) > Parts: 1/6": 1})
        self.assertEqual(group["read_rows"], {"minimum": 12, "maximum": 12})
        self.assertEqual(evidence["rounds"][-1]["storage"]["tables"]["events"]["compressed_bytes"], 10)

    def test_existing_destination_is_refused(self):
        self.destination.mkdir()
        with self.assertRaisesRegex(FileExistsError, "already exists"):
            self.run_pack()

    def test_row_plan_signature_drops_costs_predicates_and_namespaces(self):
        plan = ("EXPLAIN ANALYZE\nLimit  (cost=0.00..1.00 rows=1 width=8) (actual time=0.1..0.2 rows=256 loops=1)\n"
                "  ->  Index Scan using events_list_idx on jsons3_ab_same_table.events  (cost=0..1 rows=1)\n"
                "        Index Cond: (project_id = 'p'::text)\n        Rows Removed by Filter: 48115\n"
                "        (Buffers: shared hit=4)\nTotal runtime: 0.3 ms")
        self.assertEqual(pack.plan_signature(plan),
                         "Limit > Index Scan using events_list_idx on events > Rows Removed by Filter: 48115")


if __name__ == "__main__":
    unittest.main()
