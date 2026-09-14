import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import common


class RunnerCommonTest(unittest.TestCase):
    """验证阶段三 truth 的共享读取契约。"""

    def test_canonical_digest_sorts_keys_and_preserves_unicode(self):
        """捕获 canonical JSON 键顺序或 Unicode 转义改变 digest。"""
        self.assertEqual(
            common.canonical_digest({"b": 1, "a": "雪"}),
            "46b7a65cbee9e5a96cd669ceabac4f48e00eabcdb9259a39c45245098a72802f",
        )

    def test_load_truth_returns_complete_payload_contract_and_checks_bytes(self):
        """捕获共享契约漏字段或 payload length、preview、digest 未校验。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = '"' + "a" * 198 + "雪尾部" + '"'
            payload_bytes = payload.encode("utf-8")
            payload_path = root / "payloads" / "sample.json"
            payload_path.parent.mkdir()
            payload_path.write_bytes(payload_bytes)
            truth = self.truth_fixture(payload_bytes, payload[:200])
            truth_path = root / "truth.json"
            truth_path.write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")

            catalog = common.load_truth(truth_path)

            self.assertEqual(catalog.record_count, 2)
            self.assertEqual(catalog.block_count, 1)
            self.assertEqual(catalog.profile_counts, {"unicode_boundary": 1})
            self.assertEqual(catalog.cohort_counts, {"correctness_only": 1})
            self.assertEqual(catalog.raw_payload_bytes, len(payload_bytes))
            self.assertEqual(catalog.query_window["page_size"], 256)
            self.assertEqual(
                catalog.payloads[0],
                common.PayloadRecord(
                    event_id="trace-a:span-1",
                    trace_id="trace-a",
                    project_id="Leoxx/whowhen_pro",
                    start_time="2030-01-01T00:00:00.000Z",
                    cohort="correctness_only",
                    profile="unicode_boundary",
                    content_type="application/json",
                    encoding="utf-8",
                    content_length=len(payload_bytes),
                    preview=payload[:200],
                    sha256=hashlib.sha256(payload_bytes).hexdigest(),
                    payload_path="payloads/sample.json",
                ),
            )

            payload_path.write_bytes(b'"tampered"')
            with self.assertRaisesRegex(ValueError, "payload identity mismatch"):
                common.load_truth(truth_path)

    def test_load_truth_rejects_missing_shared_contract_field(self):
        """捕获下游需依赖隐藏状态补齐 project 或时间字段。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload_bytes = b'"payload"'
            payload_path = root / "payloads" / "sample.json"
            payload_path.parent.mkdir()
            payload_path.write_bytes(payload_bytes)
            truth = self.truth_fixture(payload_bytes, '"payload"')
            del truth["payloads"][0]["project_id"]
            truth_path = root / "truth.json"
            truth_path.write_text(json.dumps(truth), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid payload record"):
                common.load_truth(truth_path)

    @staticmethod
    def truth_fixture(payload_bytes, preview):
        """返回带独立字面量身份的最小 truth。"""
        digest = hashlib.sha256(payload_bytes).hexdigest()
        return {
            "format": "agent-trace-json-storage-stage3-truth",
            "format_version": 1,
            "seed": 20260907,
            "source": {"manifest": {"bytes": 10, "sha256": "1" * 64}},
            "record_count": 2,
            "block_size": 256,
            "block_count": 1,
            "watermarks": [2],
            "identity_sha256": "2" * 64,
            "query_window": {
                "project_id": "Leoxx/whowhen_pro",
                "start_time": "2030-01-01T00:00:00.000Z",
                "end_time": "2030-01-01T00:52:08.500Z",
                "page_size": 256,
                "row_count": 1,
            },
            "payloads": [
                {
                    "event_id": "trace-a:span-1",
                    "trace_id": "trace-a",
                    "project_id": "Leoxx/whowhen_pro",
                    "start_time": "2030-01-01T00:00:00.000Z",
                    "cohort": "correctness_only",
                    "profile": "unicode_boundary",
                    "content_type": "application/json",
                    "encoding": "utf-8",
                    "content_length": len(payload_bytes),
                    "preview": preview,
                    "sha256": digest,
                    "payload_path": "payloads/sample.json",
                }
            ],
            "cohorts": {
                "correctness_only": {
                    "performance": False,
                    "payload_count": 1,
                    "profile_counts": {"unicode_boundary": 1},
                    "raw_payload_bytes": len(payload_bytes),
                }
            },
            "representative_traces": {},
        }


if __name__ == "__main__":
    unittest.main()
