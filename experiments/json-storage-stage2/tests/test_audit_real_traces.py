import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "audit"))

import audit_real_traces as audit


class RealTraceAuditTest(unittest.TestCase):
    """验证真实 Trace 审计的公开统计契约。"""

    def test_audit_preserves_top_level_dot_keys_and_window_changes(self):
        """捕获点键拆分、稀疏度遗漏、类型冲突和窗口边界错误。"""
        rows = [
            {
                "schema_version": "1.0",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "start_time": "2030-01-01T00:00:00Z",
                "attributes": {"sparse": "only-once", "mixed": 1, "point.key": ["a", "b"]},
            },
            {
                "schema_version": "1.0",
                "trace_id": "trace-1",
                "span_id": "span-2",
                "start_time": "2030-01-01T00:14:59Z",
                "attributes": {"mixed": "one"},
            },
            {
                "schema_version": "1.0",
                "trace_id": "trace-2",
                "span_id": "span-3",
                "start_time": "2030-01-01T00:15:00Z",
                "attributes": {"late": "新增"},
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "fixture.jsonl"
            input_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            report = audit.audit_file(input_path, window_minutes=15)

        self.assertEqual(report["global"]["path_count"], 4)
        self.assertEqual(report["global"]["width"]["max"], 3)
        self.assertEqual(report["paths"]["mixed"]["types"], {"integer": 1, "string": 1})
        self.assertEqual(report["paths"]["mixed"]["distinct_values"], 2)
        self.assertEqual(report["paths"]["mixed"]["distinct_ratio"], 1.0)
        self.assertEqual(report["paths"]["sparse"]["present_rows"], 1)
        self.assertIn("point.key", report["paths"])
        self.assertEqual(report["epochs"][1]["added_paths"], ["late"])
        self.assertEqual(report["dimensions"]["tenant"], {"status": "unavailable"})
        self.assertEqual(report["dimensions"]["project"], {"status": "unavailable"})
        self.assertEqual(report["dimensions"]["instrumentation_scope"], {"status": "unavailable"})
        self.assertEqual(report["payloads"]["attributes"]["length"]["max"], 54)
        self.assertEqual(report["payloads"]["raw_event"]["present_rows"], 3)

    def test_audit_tracks_type_changes_payloads_and_dimensions(self):
        """捕获窗口类型遗漏、非 ASCII canonical 和分组统计缺失。"""
        rows = [
            {
                "schema_version": "1.0",
                "trace_id": "trace-1",
                "span_id": "span-1",
                "start_time": "2030-01-01T00:00:00Z",
                "attributes": {
                    "source_dataset": "dataset-a",
                    "framework": "framework-a",
                    "span.type": "span-a",
                    "canonical.object": {"b": "é", "a": 1},
                    "changing": 1,
                    "gen_ai.input.messages": [{"content": "你好", "role": "user"}],
                    "gen_ai.output.messages": ["完成"],
                    "gen_ai.tool.call.arguments": {"a": "é"},
                    "gen_ai.tool.call.result": "结果",
                },
            },
            {
                "schema_version": "1.0",
                "trace_id": "trace-1",
                "span_id": "span-2",
                "start_time": "2030-01-01T00:14:59Z",
                "attributes": {
                    "source_dataset": "dataset-a",
                    "framework": "framework-a",
                    "span.type": "span-a",
                    "canonical.object": {"a": 1, "b": "é"},
                    "changing": 1,
                    "gen_ai.input.messages": [{"role": "user", "content": "你好"}],
                    "gen_ai.output.messages": ["完成"],
                    "gen_ai.tool.call.arguments": {"a": "é"},
                    "gen_ai.tool.call.result": "结果",
                },
            },
            {
                "schema_version": "1.0",
                "trace_id": "trace-2",
                "span_id": "span-3",
                "start_time": "2030-01-01T00:15:00Z",
                "attributes": {
                    "source_dataset": "dataset-a",
                    "framework": "framework-a",
                    "span.type": "span-a",
                    "canonical.object": {"a": 1, "b": "é"},
                    "changing": "one",
                    "gen_ai.input.messages": [{"content": "你好", "role": "user"}],
                    "gen_ai.output.messages": ["完成"],
                    "gen_ai.tool.call.arguments": {"a": "é"},
                    "gen_ai.tool.call.result": "结果",
                },
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "fixture.jsonl"
            input_path.write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )
            report = audit.audit_file(input_path, window_minutes=15)

        self.assertEqual(report["paths"]["canonical.object"]["distinct_values"], 1)
        self.assertEqual(report["paths"]["canonical.object"]["length"]["max"], 16)
        self.assertEqual(
            report["epochs"][1]["type_changes"],
            [{"added_types": ["string"], "path": "changing", "removed_types": ["integer"]}],
        )
        for name, length in {
            "gen_ai.input.messages": 36,
            "gen_ai.output.messages": 10,
            "gen_ai.tool.call.arguments": 10,
            "gen_ai.tool.call.result": 8,
        }.items():
            self.assertEqual(report["payloads"][name]["present_rows"], 3)
            self.assertEqual(report["payloads"][name]["length"]["max"], length)
        for name, value in {
            "source_dataset": "dataset-a",
            "framework": "framework-a",
            "span.type": "span-a",
            "schema_version": "1.0",
        }.items():
            group = report["dimensions"][name][value]
            self.assertEqual(group["row_count"], 3)
            self.assertEqual(group["path_count"], 9)
            self.assertEqual(group["width"], {"p50": 9, "p95": 9, "p99": 9, "max": 9})
            self.assertEqual(group["path_density"]["canonical.object"], 1.0)

    def test_write_audit_publishes_manifest_after_verified_artifact(self):
        """捕获输入门禁、旧 manifest 残留和 manifest 先发布错误。"""
        row = {
            "schema_version": "1.0",
            "trace_id": "trace-1",
            "span_id": "span-1",
            "start_time": "2030-01-01T00:00:00Z",
            "attributes": {"span.type": "span"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.jsonl"
            upstream_manifest = root / "upstream.json"
            output_dir = root / "output"
            input_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            upstream_manifest.write_text("{}\n", encoding="utf-8")
            input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
            upstream_sha256 = hashlib.sha256(upstream_manifest.read_bytes()).hexdigest()
            output_dir.mkdir()
            (output_dir / "run-manifest.json").write_text("stale", encoding="utf-8")

            writes = []
            write_atomically = audit.write_atomically

            def record_write(path, content):
                writes.append(Path(path).name)
                write_atomically(path, content)

            with (
                patch.object(audit, "EXPECTED_INPUT_SHA256", input_sha256),
                patch.object(audit, "EXPECTED_UPSTREAM_MANIFEST_SHA256", upstream_sha256),
                patch.object(audit, "write_atomically", side_effect=record_write),
            ):
                audit.write_audit(input_path, upstream_manifest, output_dir)

            audit_bytes = (output_dir / "audit.json").read_bytes()
            manifest = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(writes, ["audit.json", "run-manifest.json"])
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["input"]["sha256"], input_sha256)
            self.assertEqual(manifest["input"]["upstream_manifest"]["sha256"], upstream_sha256)
            self.assertEqual(manifest["artifacts"]["audit.json"]["bytes"], len(audit_bytes))
            self.assertEqual(
                manifest["artifacts"]["audit.json"]["sha256"],
                hashlib.sha256(audit_bytes).hexdigest(),
            )

            for expected_input, expected_upstream in (("invalid", upstream_sha256), (input_sha256, "invalid")):
                (output_dir / "run-manifest.json").write_text("stale", encoding="utf-8")
                with (
                    patch.object(audit, "EXPECTED_INPUT_SHA256", expected_input),
                    patch.object(audit, "EXPECTED_UPSTREAM_MANIFEST_SHA256", expected_upstream),
                ):
                    with self.assertRaises(ValueError):
                        audit.write_audit(input_path, upstream_manifest, output_dir)
                self.assertFalse((output_dir / "run-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
