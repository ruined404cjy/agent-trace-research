import copy
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
    """验证阶段三 truth 的共享读取契约。"""

    def test_production_cohort_contract_matches_frozen_counts_and_bytes(self):
        """捕获生产 catalog 放宽主集合、控制组或 correctness-only 固定值。"""
        self.assertEqual(
            common.COHORT_CONTRACT,
            {
                "main": {
                    "performance": True,
                    "payload_count": 160,
                    "profile_counts": {
                        "text_64k": 40,
                        "text_512k": 40,
                        "text_2m": 40,
                        "entropy_512k": 40,
                    },
                    "raw_payload_bytes": 128_450_560,
                },
                "equal_total_control": {
                    "performance": True,
                    "payload_count": 1_320,
                    "profile_counts": {"text_2m": 40, "text_64k": 1_280},
                    "raw_payload_bytes": 167_772_160,
                    "variants": {
                        "few_large": {
                            "profile": "text_2m",
                            "payload_count": 40,
                            "raw_payload_bytes": 83_886_080,
                        },
                        "many_medium": {
                            "profile": "text_64k",
                            "payload_count": 1_280,
                            "raw_payload_bytes": 83_886_080,
                        },
                    },
                },
                "correctness_only": {
                    "performance": False,
                    "payload_count": 1,
                    "profile_counts": {"unicode_boundary": 1},
                    "raw_payload_bytes": 1_024,
                },
            },
        )

    def test_canonical_digest_sorts_keys_and_preserves_unicode(self):
        """捕获 canonical JSON 键顺序或 Unicode 转义改变 digest。"""
        self.assertEqual(
            common.canonical_digest({"b": 1, "a": "雪"}),
            "46b7a65cbee9e5a96cd669ceabac4f48e00eabcdb9259a39c45245098a72802f",
        )

    def test_logical_target_rows_use_each_layouts_actual_submitted_columns(self):
        """捕获提交统计把源行、payload_path 或另一目标列计入任何物理目标。"""
        row = {
            "ingest_seq": 0, "event_id": "event-a", "trace_id": "trace-a",
            "span_id": "span-a", "parent_span_id": None, "project_id": "project-a",
            "start_time": "2030-01-01T00:00:00.000Z", "end_time": "2030-01-01T00:00:01.000Z",
            "duration_ms": 1, "span_type": "llm", "framework": "fixture", "level": "INFO",
            "cohort": "main", "profile": "text_64k", "content_type": "application/json",
            "encoding": "utf-8", "content_length": 3, "preview": "abc",
            "sha256": "a" * 64, "payload_path": "payloads/a.json",
        }
        paths = {"a" * 64: "/objects/" + "a" * 64}

        targets = common.logical_target_rows("asset_ref", [row], [b"abc"], paths)

        self.assertEqual(set(targets), {"events_analytics", "assets"})
        self.assertEqual(
            targets["events_analytics"][0],
            {
                "ingest_seq": 0, "event_id": "event-a", "trace_id": "trace-a",
                "span_id": "span-a", "parent_span_id": None, "project_id": "project-a",
                "start_time": "2030-01-01T00:00:00.000Z", "end_time": "2030-01-01T00:00:01.000Z",
                "duration_ms": 1, "span_type": "llm", "framework": "fixture", "level": "INFO",
                "cohort": "main", "profile": "text_64k", "content_type": "application/json",
                "encoding": "utf-8", "content_length": 3, "preview": "abc", "sha256": "a" * 64,
                "asset_id": "a" * 64,
            },
        )
        self.assertEqual(
            targets["assets"][0],
            {
                "asset_id": "a" * 64, "sha256": "a" * 64,
                "content_type": "application/json", "encoding": "utf-8", "content_length": 3,
                "storage_path": "/objects/" + "a" * 64, "status": "pending",
                "error_category": None,
            },
        )
        self.assertNotIn("payload_path", targets["events_analytics"][0])
        self.assertEqual(
            common.logical_target_row_bytes("same_table", [row], [b"abc"], paths),
            {"events": 478},
        )
        self.assertEqual(
            common.logical_target_row_bytes("separate", [row], [b"abc"], paths),
            {"events_analytics": 475, "event_payloads": 312},
        )
        self.assertEqual(
            common.logical_target_row_bytes("full_core", [row], [b"abc"], paths),
            {"events_full": 478, "events_core": 475},
        )
        self.assertEqual(
            common.logical_target_row_bytes("asset_ref", [row], [b"abc"], paths),
            {"events_analytics": 553, "assets": 360},
        )

    def test_load_truth_returns_complete_payload_contract_and_checks_bytes(self):
        """捕获共享契约漏字段或 payload length、preview、digest 未校验。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            truth_path, truth, contract = self.write_complete_truth(root)

            with patch.object(common, "COHORT_CONTRACT", contract):
                catalog = common.load_truth(truth_path)

            self.assertEqual(catalog.record_count, 10)
            self.assertEqual(catalog.block_count, 1)
            self.assertEqual(
                catalog.cohort_counts,
                {"main": 4, "equal_total_control": 2, "correctness_only": 1},
            )
            self.assertEqual(catalog.query_window["page_size"], 256)
            self.assertEqual(
                catalog.payloads[-1],
                common.PayloadRecord(**truth["payloads"][-1]),
            )

            payload_path = root / truth["payloads"][-1]["payload_path"]
            payload_path.write_bytes(b'"tampered"')
            with patch.object(common, "COHORT_CONTRACT", contract):
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

    def test_load_truth_rejects_invalid_cohort_trace_and_detail_contracts(self):
        """捕获缺 cohort、错误性能标志、空代表 Trace 或详情样本错配。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            truth_path, valid_truth, contract = self.write_complete_truth(root)
            with patch.object(common, "COHORT_CONTRACT", contract, create=True):
                catalog = common.load_truth(truth_path)
                self.assertEqual(len(catalog.detail_samples), 4)

                missing_main = copy.deepcopy(valid_truth)
                missing_main["payloads"] = [
                    record for record in missing_main["payloads"] if record["cohort"] != "main"
                ]
                del missing_main["cohorts"]["main"]
                missing_main["detail_samples"] = []
                self.write_truth(truth_path, missing_main)
                with self.assertRaisesRegex(ValueError, "cohort contract mismatch"):
                    common.load_truth(truth_path)

                wrong_flag = copy.deepcopy(valid_truth)
                wrong_flag["cohorts"]["main"]["performance"] = False
                self.write_truth(truth_path, wrong_flag)
                with self.assertRaisesRegex(ValueError, "cohort performance mismatch"):
                    common.load_truth(truth_path)

                wrong_count = copy.deepcopy(valid_truth)
                wrong_count["cohorts"]["main"]["payload_count"] = 3
                self.write_truth(truth_path, wrong_count)
                with self.assertRaisesRegex(ValueError, "cohort summary mismatch"):
                    common.load_truth(truth_path)

                missing_profile = copy.deepcopy(valid_truth)
                removed = next(
                    record
                    for record in missing_profile["payloads"]
                    if record["cohort"] == "main" and record["profile"] == "text_64k"
                )
                missing_profile["payloads"].remove(removed)
                main_summary = missing_profile["cohorts"]["main"]
                main_summary["payload_count"] = 3
                del main_summary["profile_counts"]["text_64k"]
                main_summary["raw_payload_bytes"] -= removed["content_length"]
                missing_profile["detail_samples"] = missing_profile["detail_samples"][1:]
                self.write_truth(truth_path, missing_profile)
                with self.assertRaisesRegex(ValueError, "cohort contract mismatch"):
                    common.load_truth(truth_path)

                empty_traces = copy.deepcopy(valid_truth)
                empty_traces["representative_traces"] = {}
                self.write_truth(truth_path, empty_traces)
                with self.assertRaisesRegex(ValueError, "representative trace contract mismatch"):
                    common.load_truth(truth_path)

                bad_detail = copy.deepcopy(valid_truth)
                bad_detail["detail_samples"][0] = {
                    **bad_detail["detail_samples"][0],
                    "sha256": "0" * 64,
                }
                self.write_truth(truth_path, bad_detail)
                with self.assertRaisesRegex(ValueError, "detail sample mismatch"):
                    common.load_truth(truth_path)

    def test_load_truth_rejects_digest_consistent_invalid_json_payload(self):
        """捕获 length、preview、digest 均自洽的非法 application/json bytes。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            truth_path, truth, contract = self.write_complete_truth(root)
            record = truth["payloads"][0]
            invalid_bytes = b"x" * record["content_length"]
            (root / record["payload_path"]).write_bytes(invalid_bytes)
            replacement = {
                **record,
                "preview": invalid_bytes.decode("utf-8")[:200],
                "sha256": hashlib.sha256(invalid_bytes).hexdigest(),
            }
            truth["payloads"][0] = replacement
            truth["detail_samples"][0] = dict(replacement)
            self.write_truth(truth_path, truth)

            with patch.object(common, "COHORT_CONTRACT", contract, create=True):
                with self.assertRaisesRegex(ValueError, "invalid JSON payload"):
                    common.load_truth(truth_path)

    @classmethod
    def write_complete_truth(cls, root):
        """写入包含三个 cohort、代表 Trace 和详情样本的最小完整 truth。"""
        specifications = (
            ("main", "text_64k", True),
            ("main", "text_512k", True),
            ("main", "text_2m", True),
            ("main", "entropy_512k", True),
            ("equal_total_control", "text_64k", True),
            ("equal_total_control", "text_2m", True),
            ("correctness_only", "unicode_boundary", False),
        )
        payloads = []
        (root / "payloads").mkdir()
        for index, (cohort, profile, _) in enumerate(specifications):
            payload_bytes = json.dumps(
                {"index": index, "profile": profile}, separators=(",", ":")
            ).encode("utf-8")
            digest = hashlib.sha256(payload_bytes).hexdigest()
            relative_path = f"payloads/{index}.json"
            (root / relative_path).write_bytes(payload_bytes)
            payloads.append(
                {
                    "event_id": f"trace-a:span-{index}",
                    "trace_id": "trace-a",
                    "project_id": "Leoxx/whowhen_pro",
                    "start_time": f"2030-01-01T00:00:0{index}.000Z",
                    "cohort": cohort,
                    "profile": profile,
                    "content_type": "application/json",
                    "encoding": "utf-8",
                    "content_length": len(payload_bytes),
                    "preview": payload_bytes.decode("utf-8")[:200],
                    "sha256": digest,
                    "payload_path": relative_path,
                }
            )
        cohorts = {}
        contract = {}
        for cohort in ("main", "equal_total_control", "correctness_only"):
            records = [record for record in payloads if record["cohort"] == cohort]
            profile_counts = {
                profile: sum(record["profile"] == profile for record in records)
                for profile in sorted({record["profile"] for record in records})
            }
            summary = {
                "performance": cohort != "correctness_only",
                "payload_count": len(records),
                "profile_counts": profile_counts,
                "raw_payload_bytes": sum(record["content_length"] for record in records),
            }
            cohorts[cohort] = summary
            contract[cohort] = dict(summary)
        truth = {
            "format": "agent-trace-json-storage-stage3-truth",
            "format_version": 1,
            "seed": 20260907,
            "source": {"manifest": {"bytes": 10, "sha256": "1" * 64}},
            "record_count": 10,
            "block_size": 256,
            "block_count": 1,
            "watermarks": [10],
            "identity_sha256": "2" * 64,
            "query_window": {
                "project_id": "Leoxx/whowhen_pro",
                "start_time": "2030-01-01T00:00:00.000Z",
                "end_time": "2030-01-01T00:52:08.500Z",
                "page_size": 256,
                "row_count": 7,
            },
            "payloads": payloads,
            "cohorts": cohorts,
            "representative_traces": {
                label: {
                    "trace_id": "trace-a",
                    "span_count": 7,
                    "payload_count": 7,
                    "profile_counts": {
                        "entropy_512k": 1,
                        "text_2m": 2,
                        "text_512k": 1,
                        "text_64k": 2,
                        "unicode_boundary": 1,
                    },
                    "raw_payload_bytes": sum(record["content_length"] for record in payloads),
                }
                for label in ("p25", "p50", "p95")
            },
            "detail_samples": [
                record
                for profile in ("text_64k", "text_512k", "text_2m", "entropy_512k")
                for record in payloads
                if record["cohort"] == "main" and record["profile"] == profile
            ],
        }
        truth_path = root / "truth.json"
        cls.write_truth(truth_path, truth)
        return truth_path, truth, contract

    @staticmethod
    def write_truth(path, truth):
        """写入测试 truth。"""
        Path(path).write_text(json.dumps(truth, ensure_ascii=False), encoding="utf-8")

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
