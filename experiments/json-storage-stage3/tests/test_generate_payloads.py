import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "generator"))

import generate_payloads as generator

sys.path.insert(0, str(STAGE_DIR / "runner"))
import common


class GeneratePayloadsTest(unittest.TestCase):
    """验证阶段三冻结 payload 的生成契约。"""

    def test_main_profiles_have_exact_lengths_unique_digests_and_unicode_preview(self):
        """捕获 profile 数量、精确 bytes、内容唯一性或 Unicode preview 回退。"""
        catalog = self.build_fixture_catalog(seed=20260907)

        self.assertEqual(
            catalog.profile_counts,
            {
                "text_64k": 40,
                "text_512k": 40,
                "text_2m": 40,
                "entropy_512k": 40,
            },
        )
        self.assertEqual(len({item.sha256 for item in catalog.payloads}), 160)
        self.assertTrue(
            all(len(item.payload_bytes) == item.content_length for item in catalog.payloads)
        )
        self.assertEqual(sum(item.content_length for item in catalog.payloads), 128_450_560)
        self.assertEqual(
            Counter(generator._profile_sequence(generator.CONTROL_PROFILES)),
            {"text_2m": 40, "text_64k": 1_280},
        )
        self.assertEqual(40 * 2_097_152, 1_280 * 65_536)
        self.assertEqual(
            catalog.unicode_boundary.preview,
            catalog.unicode_boundary.text[:200],
        )
        self.assertGreater(
            len(catalog.unicode_boundary.preview.encode("utf-8")),
            len(catalog.unicode_boundary.preview),
        )

    def test_frozen_stage_two_source_has_exact_identity_rows_and_blocks(self):
        """捕获内部自洽 manifest 放行被替换的阶段二输入。"""
        source_dir = (
            STAGE_DIR.parents[1]
            / "docs"
            / "temp"
            / "json-storage-stage2"
            / "cross-engine-input-20260907"
        )

        rows, source = generator._read_frozen_source(source_dir)

        self.assertEqual(len(rows), 48_534)
        self.assertEqual(source["record_count"], 48_534)
        self.assertEqual(source["block_size"], 256)
        self.assertEqual(source["block_count"], 190)
        self.assertEqual(source["watermarks"][-2:], [48_384, 48_534])
        self.assertEqual(
            source["manifest"],
            {
                "bytes": 2_397,
                "sha256": "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437",
            },
        )

    def test_build_truth_publishes_events_payloads_truth_and_manifest_last(self):
        """捕获 cohort 混合、基础行丢失、空 payload 物化或 manifest 提前发布。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            output_dir = root / "output"
            expected = self.write_source_fixture(source_dir)
            writes = []
            original_write = generator._write_atomically

            def record_write(path, content):
                writes.append(Path(path).relative_to(output_dir).as_posix())
                original_write(path, content)

            with (
                patch.object(generator, "EXPECTED_SOURCE_MANIFEST", expected["manifest"]),
                patch.object(generator, "EXPECTED_SOURCE_ARTIFACTS", expected["artifacts"]),
                patch.object(generator, "EXPECTED_RECORD_COUNT", 10),
                patch.object(generator, "BLOCK_SIZE", 4),
                patch.object(generator, "EXPECTED_BLOCK_COUNT", 3),
                patch.object(generator, "EXPECTED_QUERY_ROW_COUNT", 10, create=True),
                patch.object(
                    generator,
                    "MAIN_PROFILES",
                    {
                        "text_64k": {"count": 1, "content_length": 256},
                        "text_512k": {"count": 1, "content_length": 384},
                        "text_2m": {"count": 1, "content_length": 512},
                        "entropy_512k": {"count": 1, "content_length": 384},
                    },
                ),
                patch.object(
                    generator,
                    "CONTROL_PROFILES",
                    {
                        "text_2m": {"count": 1, "content_length": 512},
                        "text_64k": {"count": 2, "content_length": 256},
                    },
                ),
                patch.object(generator, "UNICODE_BOUNDARY_LENGTH", 384),
                patch.object(generator, "_write_atomically", side_effect=record_write),
            ):
                truth = generator.build_truth(source_dir, output_dir, seed=20260907)

            catalog = common.load_truth(output_dir / "truth.json")
            events = [json.loads(line) for line in (output_dir / "events.jsonl").read_text().splitlines()]
            manifest = json.loads((output_dir / "generation-manifest.json").read_bytes())

            self.assertEqual(truth["record_count"], 10)
            self.assertEqual(truth["block_count"], 3)
            self.assertEqual(truth["watermarks"], [4, 8, 10])
            self.assertEqual(
                truth["query_window"],
                {
                    "project_id": "Leoxx/whowhen_pro",
                    "start_time": "2030-01-01T00:00:00.000Z",
                    "end_time": "2030-01-01T00:52:08.500Z",
                    "page_size": 256,
                    "row_count": 10,
                },
            )
            self.assertEqual(
                catalog.cohort_counts,
                {"main": 4, "equal_total_control": 3, "correctness_only": 1},
            )
            self.assertEqual(
                catalog.profile_counts,
                {
                    "text_64k": 3,
                    "text_512k": 1,
                    "text_2m": 2,
                    "entropy_512k": 1,
                    "unicode_boundary": 1,
                },
            )
            self.assertEqual(len(events), 10)
            self.assertEqual([event["ingest_seq"] for event in events], list(range(10)))
            self.assertEqual(sum(event["payload_path"] is None for event in events), 2)
            self.assertTrue(
                all(
                    event["profile"] is None
                    and event["content_length"] is None
                    and event["sha256"] is None
                    for event in events
                    if event["payload_path"] is None
                )
            )
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["seed"], 20260907)
            self.assertEqual(manifest["cohorts"], truth["cohorts"])
            self.assertFalse(truth["cohorts"]["correctness_only"]["performance"])
            self.assertTrue(truth["cohorts"]["main"]["performance"])
            self.assertEqual(writes[-3:], ["events.jsonl", "truth.json", "generation-manifest.json"])
            self.assertEqual(
                manifest["artifacts"]["truth.json"],
                self.file_identity(output_dir / "truth.json"),
            )
            self.assertEqual(
                set(truth["representative_traces"]),
                {"p25", "p50", "p95"},
            )

    def test_source_validation_rejects_self_consistent_replacement(self):
        """捕获替换输入重算自身 artifact 与 manifest 后被错误接受。"""
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory) / "source"
            expected = self.write_source_fixture(source_dir)
            dataset_path = source_dir / "dataset.jsonl"
            dataset_path.write_bytes(dataset_path.read_bytes().replace(b"trace-a", b"trace-x"))
            truth_path = source_dir / "truth-manifest.json"
            truth = json.loads(truth_path.read_bytes())
            for record in truth["records"]:
                record["event_id"] = record["event_id"].replace("trace-a", "trace-x")
            truth_path.write_bytes(self.canonical_bytes(truth) + b"\n")
            manifest_path = source_dir / "run-manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["artifacts"] = {
                "dataset.jsonl": self.file_identity(dataset_path),
                "truth-manifest.json": self.file_identity(truth_path),
            }
            manifest_path.write_bytes(self.canonical_bytes(manifest) + b"\n")

            with (
                patch.object(generator, "EXPECTED_SOURCE_MANIFEST", expected["manifest"]),
                patch.object(generator, "EXPECTED_SOURCE_ARTIFACTS", expected["artifacts"]),
                patch.object(generator, "EXPECTED_RECORD_COUNT", 10),
                patch.object(generator, "BLOCK_SIZE", 4),
                patch.object(generator, "EXPECTED_BLOCK_COUNT", 3),
            ):
                with self.assertRaisesRegex(ValueError, "source manifest identity mismatch"):
                    generator._read_frozen_source(source_dir)

    @classmethod
    def write_source_fixture(cls, source_dir):
        """写入具有独立 artifact identity 的最小阶段二目录。"""
        source_dir.mkdir()
        trace_ids = ["trace-a"] * 4 + ["trace-b"] * 3 + ["trace-c"] * 2 + ["trace-d"]
        rows = [
            {
                "ingest_seq": index,
                "event_id": f"{trace_id}:span-{index}",
                "trace_id": trace_id,
                "project_id": "Leoxx/whowhen_pro",
                "start_time": f"2030-01-01T00:00:{index:02d}.000Z",
            }
            for index, trace_id in enumerate(trace_ids)
        ]
        dataset = b"".join(cls.canonical_bytes(row) + b"\n" for row in rows)
        source_truth = {
            "record_count": 10,
            "block_size": 4,
            "block_count": 3,
            "watermarks": [4, 8, 10],
            "records": [{"event_id": row["event_id"]} for row in rows],
        }
        truth_bytes = cls.canonical_bytes(source_truth) + b"\n"
        (source_dir / "dataset.jsonl").write_bytes(dataset)
        (source_dir / "truth-manifest.json").write_bytes(truth_bytes)
        artifacts = {
            "dataset.jsonl": cls.bytes_identity(dataset),
            "truth-manifest.json": cls.bytes_identity(truth_bytes),
        }
        manifest = {
            "status": "complete",
            "record_count": 10,
            "block_size": 4,
            "block_count": 3,
            "watermarks": [4, 8, 10],
            "artifacts": artifacts,
            "input": {"sha256": "3" * 64},
        }
        manifest_bytes = cls.canonical_bytes(manifest) + b"\n"
        (source_dir / "run-manifest.json").write_bytes(manifest_bytes)
        return {"manifest": cls.bytes_identity(manifest_bytes), "artifacts": artifacts}

    @staticmethod
    def canonical_bytes(value):
        """返回测试独立构造的 canonical JSON bytes。"""
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def bytes_identity(content):
        """返回测试 fixture bytes 的 identity。"""
        return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

    @staticmethod
    def file_identity(path):
        """返回测试独立读取的文件 identity。"""
        return GeneratePayloadsTest.bytes_identity(Path(path).read_bytes())

    @staticmethod
    def build_fixture_catalog(seed):
        """通过真实生成接口构造仅供测试断言的 payload catalog。"""
        profiles = {
            "text_64k": (40, 65_536),
            "text_512k": (40, 524_288),
            "text_2m": (40, 2_097_152),
            "entropy_512k": (40, 524_288),
        }
        payloads = []
        for profile, (count, content_length) in profiles.items():
            for index in range(count):
                event_id = f"fixture-trace:{profile}-{index:03d}"
                payload_bytes = generator.generate_payload_bytes(profile, event_id, seed)
                payloads.append(
                    SimpleNamespace(
                        content_length=content_length,
                        payload_bytes=payload_bytes,
                        sha256=hashlib.sha256(payload_bytes).hexdigest(),
                    )
                )
        unicode_bytes = generator.generate_payload_bytes(
            "unicode_boundary",
            "fixture-trace:unicode-boundary",
            seed,
        )
        unicode_text = unicode_bytes.decode("utf-8")
        return SimpleNamespace(
            payloads=payloads,
            profile_counts=Counter(profile for profile, (count, _) in profiles.items() for _ in range(count)),
            unicode_boundary=SimpleNamespace(
                preview=unicode_text[:200],
                text=unicode_text,
            ),
        )


if __name__ == "__main__":
    unittest.main()
