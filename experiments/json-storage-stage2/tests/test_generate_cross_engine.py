import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "generator"))

import generate_cross_engine as generator


class CrossEngineGeneratorTest(unittest.TestCase):
    """验证统一数据与查询 truth 的公开契约。"""

    def test_project_attributes_preserves_values_and_rejects_prefix_conflicts(self):
        """捕获点键投影丢值及标量/对象前缀冲突。"""
        attributes = {"a.b": 1, "a.c": [2, 1], "plain": None, "object": {"x": "值"}}

        projected = generator.project_attributes(attributes)

        self.assertEqual(
            projected,
            {"a": {"b": 1, "c": [2, 1]}, "plain": None, "object": {"x": "值"}},
        )
        key_map = generator.project_key_map(attributes)
        self.assertEqual(
            generator.flatten_projected(projected, key_map),
            {name: generator.canonical_bytes(value) for name, value in attributes.items()},
        )
        with self.assertRaisesRegex(ValueError, "prefix conflict"):
            generator.project_attributes({"a": 1, "a.b": 2})
        with self.assertRaisesRegex(ValueError, "prefix conflict"):
            generator.project_attributes({"a": None, "a.b": 2})

    def test_build_record_keeps_raw_bytes_and_normalizes_attributes(self):
        """捕获原文 bytes、字段投影和状态等级映射偏差。"""
        source = {
            "trace_id": "trace-1",
            "span_id": "span-1",
            "parent_span_id": None,
            "start_time": "2030-01-01T00:00:00Z",
            "end_time": "2030-01-01T00:00:01Z",
            "duration_ms": 1000,
            "status": {"code": "STATUS_CODE_ERROR"},
            "attributes": {"span.type": "tool", "nested.value": ["é", 1]},
        }
        original_line_without_newline = json.dumps(source, ensure_ascii=False).encode("utf-8")

        record = generator.build_record(source, original_line_without_newline, 0)

        self.assertEqual(record["raw_event"].encode("utf-8"), original_line_without_newline)
        self.assertEqual(record["event_id"], "trace-1:span-1")
        self.assertEqual(record["project_id"], "Leoxx/whowhen_pro")
        self.assertEqual(record["span_type"], "tool")
        self.assertEqual(record["framework"], "")
        self.assertEqual(record["level"], "ERROR")
        self.assertEqual(record["attributes_analysis"], {"span": {"type": "tool"}, "nested": {"value": ["é", 1]}})
        self.assertEqual(record["attributes_map"]["nested.value"], '["é",1]')
        self.assertEqual(len(record), 15)

    def test_write_dataset_publishes_block_truth_and_manifest_last(self):
        """捕获逐水位 truth、代表 trace 和 manifest 提前发布错误。"""
        rows = [
            self.fixture_row("trace-a", "span-1", 0, "tool", "execute_tool", "A.3"),
            self.fixture_row("trace-a", "span-2", 1, "llm", "chat", "A.4"),
            self.fixture_row("trace-b", "span-3", 2, "tool", "execute_tool", "A.4"),
            self.fixture_row("trace-c", "span-4", 3, "tool", "execute_tool", "A.4"),
            self.fixture_row("trace-c", "span-5", 4, "llm", "chat", "A.3"),
            self.fixture_row("trace-a", "span-6", 4, "tool", "execute_tool", "A.3"),
            self.fixture_row("trace-c", "span-7", 4, "tool", "execute_tool", "A.4"),
        ]
        raw_lines = [json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8") for row in rows]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.jsonl"
            audit_path = root / "audit.json"
            output_dir = root / "output"
            input_path.write_bytes(b"\n".join(raw_lines) + b"\n")
            input_sha256 = hashlib.sha256(input_path.read_bytes()).hexdigest()
            audit_path.write_text(
                json.dumps({"global": {"path_count": 3}, "input": {"sha256": input_sha256}}),
                encoding="utf-8",
            )
            output_dir.mkdir()
            (output_dir / "run-manifest.json").write_text("stale", encoding="utf-8")

            writes = []
            write_atomically = generator.write_atomically

            def record_write(path, content):
                writes.append(Path(path).name)
                write_atomically(path, content)

            with (
                patch.object(generator, "EXPECTED_INPUT_SHA256", input_sha256),
                patch.object(generator, "write_atomically", side_effect=record_write),
            ):
                generator.write_dataset(
                    input_path,
                    audit_path,
                    output_dir,
                    block_size=2,
                    command="python3 generate_cross_engine.py --fixture",
                )

            dataset = [json.loads(line) for line in (output_dir / "dataset.jsonl").read_text(encoding="utf-8").splitlines()]
            truth = json.loads((output_dir / "truth-manifest.json").read_text(encoding="utf-8"))
            manifest = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(writes, ["dataset.jsonl", "truth-manifest.json", "run-manifest.json"])
            self.assertEqual(dataset[0]["raw_event"].encode("utf-8"), raw_lines[0])
            self.assertEqual(truth["watermarks"], [2, 4, 6, 7])
            self.assertEqual(truth["queries"]["Q05"]["4"]["row_count"], 1)
            self.assertEqual(truth["parameters"]["Q04"]["trace_id"], "trace-a")
            for parameters in truth["parameters"].values():
                self.assertIn("start_time", parameters)
                self.assertIn("end_time", parameters)
            self.assertEqual(truth["queries"]["Q02"]["7"]["result"], [["tool", 1]])
            self.assertEqual(
                truth["queries"]["Q04"]["7"]["result"],
                [
                    ["2030-01-01T00:00:00Z", "trace-a:span-1", dataset[0]["attributes_map"]],
                    ["2030-01-01T00:00:01Z", "trace-a:span-2", dataset[1]["attributes_map"]],
                ],
            )
            self.assertEqual(truth["queries"]["Q05"]["7"]["row_count"], 1)
            self.assertEqual(truth["native_json"]["path_budget"], 4)
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["record_count"], 7)
            self.assertEqual(manifest["block_count"], 4)
            self.assertEqual(manifest["data_path"], "independent_loader")
            self.assertEqual(manifest["command"], "python3 generate_cross_engine.py --fixture")
            self.assertEqual(manifest["artifacts"]["dataset.jsonl"]["bytes"], len((output_dir / "dataset.jsonl").read_bytes()))

    def test_write_dataset_rejects_missing_or_mismatched_audit_input_identity(self):
        """捕获审计输入身份缺失或与固定输入不一致的错误放行。"""
        row = self.fixture_row("trace-a", "span-1", 0, "tool", "execute_tool", "A.3")
        raw_line = json.dumps(row, separators=(",", ":")).encode("utf-8") + b"\n"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "fixture.jsonl"
            audit_path = root / "audit.json"
            output_dir = root / "output"
            input_path.write_bytes(raw_line)
            output_dir.mkdir()
            input_sha256 = hashlib.sha256(raw_line).hexdigest()

            for audit_content, message in (
                ({"global": {"path_count": 1}}, "audit input SHA-256 missing"),
                ({"global": {"path_count": 1}, "input": {"sha256": "wrong"}}, "audit input SHA-256 mismatch"),
            ):
                audit_path.write_text(json.dumps(audit_content), encoding="utf-8")
                (output_dir / "run-manifest.json").write_text("stale", encoding="utf-8")
                with patch.object(generator, "EXPECTED_INPUT_SHA256", input_sha256):
                    with self.assertRaisesRegex(ValueError, message):
                        generator.write_dataset(input_path, audit_path, output_dir, block_size=2)
                self.assertFalse((output_dir / "run-manifest.json").exists())

    @staticmethod
    def fixture_row(trace_id, span_id, second, span_type, operation, failure_mode):
        """返回用于查询 truth 的最小 Span 输入。"""
        return {
            "schema_version": "1.0",
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": None,
            "start_time": f"2030-01-01T00:00:0{second}Z",
            "end_time": f"2030-01-01T00:00:1{second}Z",
            "duration_ms": 1000 + second,
            "status": {"code": "STATUS_CODE_OK"},
            "attributes": {
                "span.type": span_type,
                "framework": "fixture",
                "gen_ai.operation.name": operation,
                "failure.mistake_mode": failure_mode,
            },
        }


if __name__ == "__main__":
    unittest.main()
