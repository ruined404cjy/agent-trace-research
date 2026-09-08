import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import common


class RunnerCommonTest(unittest.TestCase):
    """验证跨引擎 runner 的公共可比性契约。"""

    def test_summarize_samples_uses_nearest_rank_and_request_equivalent_qps(self):
        """捕获分位数、QPS 口径或失败样本被静默放行。"""
        summary = common.summarize_samples(
            [
                {"ok": True, "latency_ms": 10.0},
                {"ok": True, "latency_ms": 20.0},
                {"ok": True, "latency_ms": 30.0},
                {"ok": True, "latency_ms": 40.0},
            ]
        )

        self.assertEqual(summary["sample_count"], 4)
        self.assertEqual(summary["latency_ms"], {"p50": 20.0, "p95": 40.0, "p99": 40.0, "max": 40.0})
        self.assertEqual(summary["request_equivalent_qps"], 40.0)
        self.assertEqual(
            common.summarize_samples(
                [{"ok": True, "latency_ms": 10.0}, {"ok": True, "latency_ms": 30.0}]
            )["request_equivalent_qps"],
            50.0,
        )
        with self.assertRaisesRegex(ValueError, "failed sample"):
            common.summarize_samples([{"ok": True, "latency_ms": 10.0}, {"ok": False, "latency_ms": 1.0}])
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            common.summarize_samples([])
        with self.assertRaisesRegex(ValueError, "must be positive"):
            common.summarize_samples([{"ok": True, "latency_ms": 0}])
        with self.assertRaisesRegex(ValueError, "must be positive"):
            common.summarize_samples([{"ok": True, "latency_ms": -1}])
        for latency in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(latency=latency):
                with self.assertRaisesRegex(ValueError, "must be positive"):
                    common.summarize_samples([{"ok": True, "latency_ms": latency}])

    def test_audit_identities_reports_sorted_missing_extra_and_duplicates(self):
        """捕获 identity 漏失、额外记录或重复记录未进入门禁。"""
        audit = common.audit_identities(["x", "a", "a"], {"a", "b"})

        self.assertEqual(audit["actual_count"], 3)
        self.assertEqual(audit["expected_count"], 2)
        self.assertEqual(audit["missing"], ["b"])
        self.assertEqual(audit["extra"], ["x"])
        self.assertEqual(audit["duplicates"], ["a"])
        self.assertEqual(audit["duplicate_count"], 1)

    def test_verify_input_checks_manifest_artifacts_and_dataset_truth_identity(self):
        """捕获不完整 manifest、artifact 篡改或 dataset/truth 身份失配。"""
        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory)
            rows, truth = self.write_valid_input(input_dir)

            actual_rows, actual_truth = common.verify_input(input_dir)

            self.assertEqual(actual_rows, rows)
            self.assertEqual(actual_truth, truth)
            manifest_path = input_dir / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "failed"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest status"):
                common.verify_input(input_dir)

            self.write_valid_input(input_dir)
            (input_dir / "dataset.jsonl").write_bytes(b"{}\n")
            with self.assertRaisesRegex(ValueError, "artifact identity mismatch: dataset.jsonl"):
                common.verify_input(input_dir)

            self.write_valid_input(input_dir)
            (input_dir / "truth-manifest.json").unlink()
            with self.assertRaisesRegex(ValueError, "artifact identity mismatch: truth-manifest.json"):
                common.verify_input(input_dir)

            self.write_valid_input(input_dir)
            truth_path = input_dir / "truth-manifest.json"
            broken_truth = json.loads(truth_path.read_text(encoding="utf-8"))
            broken_truth["records"][1]["event_id"] = "wrong"
            self.write_manifest(input_dir, rows, broken_truth)
            with self.assertRaisesRegex(ValueError, "dataset/truth event_id mismatch"):
                common.verify_input(input_dir)

            self.write_valid_input(input_dir)
            broken_truth = json.loads(truth_path.read_text(encoding="utf-8"))
            broken_truth["input"] = {"sha256": "different-input"}
            self.write_manifest(input_dir, rows, broken_truth)
            with self.assertRaisesRegex(ValueError, "manifest/truth input mismatch"):
                common.verify_input(input_dir)

            for field in ("record_count", "block_count", "block_size", "watermarks"):
                with self.subTest(field=field):
                    self.write_valid_input(input_dir)
                    broken_truth = json.loads(truth_path.read_text(encoding="utf-8"))
                    if field == "record_count":
                        broken_truth[field] = 3
                    elif field == "block_count":
                        broken_truth[field] = 2
                    elif field == "block_size":
                        broken_truth[field] = 128
                    else:
                        broken_truth[field] = [1, 2]
                    self.write_manifest(input_dir, rows, broken_truth)
                    with self.assertRaisesRegex(ValueError, f"truth {field.replace('_', ' ')} mismatch"):
                        common.verify_input(input_dir)

            for field in ("analysis_sha256", "canonical_sha256", "raw_sha256"):
                with self.subTest(field=field):
                    self.write_valid_input(input_dir)
                    broken_truth = json.loads(truth_path.read_text(encoding="utf-8"))
                    broken_truth["records"][0][field] = "0" * 64
                    self.write_manifest(input_dir, rows, broken_truth)
                    with self.assertRaisesRegex(ValueError, f"dataset/truth {field} mismatch"):
                        common.verify_input(input_dir)

            self.write_valid_input(input_dir)
            broken_truth = json.loads(truth_path.read_text(encoding="utf-8"))
            broken_truth["block_size"] = True
            self.write_manifest(input_dir, rows, broken_truth, block_size=True)
            with self.assertRaisesRegex(ValueError, "invalid block size"):
                common.verify_input(input_dir)

    def test_write_manifest_last_publishes_only_complete_artifacts_and_visible_failure(self):
        """捕获 manifest 抢先发布或失败路径继承旧 artifact identity。"""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "run-manifest.json").write_text("stale", encoding="utf-8")
            common.write_manifest_last(
                output_dir,
                {"run_id": "run-1", "status": "ignored"},
                {"metrics.json": b'{"ok":true}\n'},
            )
            manifest = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["artifacts"]["metrics.json"], common.file_identity(output_dir / "metrics.json"))
            self.assertEqual(manifest["comparability_contract_version"], "json-storage-cross-engine-v1")
            self.assertEqual(manifest["data_path"], "independent_loader")
            self.assertIn("comparability_contract", manifest)

            original_write = common.write_atomically
            calls = []

            def fail_artifact_once(path, content):
                calls.append(Path(path).name)
                if Path(path).name == "metrics.json":
                    raise OSError("disk full")
                original_write(path, content)

            with patch.object(common, "write_atomically", side_effect=fail_artifact_once):
                with self.assertRaisesRegex(OSError, "disk full"):
                    common.write_manifest_last(
                        output_dir,
                        {"run_id": "run-2"},
                        {"metrics.json": b"new"},
                    )
            failed = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(calls, ["metrics.json", "run-manifest.json"])
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["artifacts"], {})
            self.assertEqual(failed["error"], {"type": "OSError", "message": "disk full"})

    def test_write_manifest_last_rejects_reserved_artifact_names(self):
        """捕获 artifact 覆盖最终 manifest 或其原子临时文件。"""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            for name in ("run-manifest.json", ".run-manifest.json.tmp"):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "reserved artifact name"):
                        common.write_manifest_last(output_dir, {"run_id": "reserved"}, {name: b"x"})

    def test_write_manifest_last_keeps_artifacts_named_like_legacy_temp_files(self):
        """捕获一个 artifact 的临时文件覆盖另一个 artifact。"""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            common.write_manifest_last(
                output_dir,
                {"run_id": "temp-name"},
                {"metrics.json": b"one", ".metrics.json.tmp": b"two"},
            )

            manifest = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifacts"]["metrics.json"], common.file_identity(output_dir / "metrics.json"))
            self.assertEqual(
                manifest["artifacts"][".metrics.json.tmp"],
                common.file_identity(output_dir / ".metrics.json.tmp"),
            )

    @staticmethod
    def write_valid_input(input_dir):
        """写入满足生成器格式的最小输入目录。"""
        rows = [
            {"ingest_seq": 0, "event_id": "trace-1:span-1", "attributes_analysis": {"a": 1}, "raw_event": "one"},
            {"ingest_seq": 1, "event_id": "trace-1:span-2", "attributes_analysis": {"a": 2}, "raw_event": "two"},
        ]
        truth = {
            "block_count": 1,
            "block_size": 256,
            "comparability_contract_version": "json-storage-cross-engine-v1",
            "input": {"sha256": "input-sha"},
            "record_count": 2,
            "records": [
                {
                    "event_id": row["event_id"],
                    "canonical_sha256": hashlib.sha256(canonical_bytes(row)).hexdigest(),
                    "analysis_sha256": hashlib.sha256(canonical_bytes(row["attributes_analysis"])).hexdigest(),
                    "raw_sha256": hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest(),
                }
                for row in rows
            ],
            "watermarks": [2],
        }
        RunnerCommonTest.write_manifest(input_dir, rows, truth)
        return rows, truth

    @staticmethod
    def write_manifest(input_dir, rows, truth, **overrides):
        """按 artifact 实际 bytes 写入完成 manifest。"""
        dataset_content = b"".join(canonical_bytes(row) + b"\n" for row in rows)
        truth_content = canonical_bytes(truth) + b"\n"
        (input_dir / "dataset.jsonl").write_bytes(dataset_content)
        (input_dir / "truth-manifest.json").write_bytes(truth_content)
        manifest = {
            "artifacts": {
                "dataset.jsonl": identity(dataset_content),
                "truth-manifest.json": identity(truth_content),
            },
            "block_count": 1,
            "block_size": 256,
            "comparability_contract_version": "json-storage-cross-engine-v1",
            "data_path": "independent_loader",
            "input": {"sha256": "input-sha"},
            "record_count": 2,
            "status": "complete",
            "watermarks": [2],
        }
        manifest.update(overrides)
        (input_dir / "run-manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")


def canonical_bytes(value):
    """返回测试 fixture 所需的稳定 JSON bytes。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def identity(content):
    """返回测试 fixture 的 artifact identity。"""
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


if __name__ == "__main__":
    unittest.main()
