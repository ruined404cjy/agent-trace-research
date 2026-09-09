import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "generator"))

import generate_supplement_truth as generator
import supplement_common as common


class SupplementTruthGeneratorTest(unittest.TestCase):
    """验证补充查询参数、结果及发布产物。"""

    def test_query_results_use_hand_derived_contracts(self):
        """捕获路径筛选、UTF-8 统计、trace 排序或分页边界偏差。"""
        rows = [
            self.row("event-3", "2030-01-01T00:00:03Z", "llm", "chat", "A.4", None),
            self.row("event-2", "2030-01-01T00:00:02Z", "llm", "execute_tool", "A.3", {"text": "你好"}),
            self.row("event-6", "2030-01-01T00:00:06Z", "tool", "execute_tool", "A.3", None),
            self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.4", ["汉"]),
            self.row("event-5", "2030-01-01T00:00:05Z", "llm", "chat", "A.4", None),
            self.row("event-4", "2030-01-01T00:00:04Z", "tool", "chat", "A.4", None),
        ]
        parameters = {
            "project_id": "project",
            "start_time": "2030-01-01T00:00:00Z",
            "end_time": "2030-01-01T00:00:04Z",
            "operation_name": "execute_tool",
            "failure_mistake_mode": "A.3",
            "attribute_key": "gen_ai.output.messages",
            "trace_id": "trace-a",
            "page_size": 256,
        }

        results = {
            query_id: generator.query_results(
                rows,
                query_id,
                {**parameters, "end_time": "2030-01-01T00:00:07Z"}
                if query_id == "S06"
                else parameters,
            )
            for query_id in generator.QUERY_IDS
        }

        self.assertEqual(results["S01"], [["llm", 2], ["tool", 1]])
        self.assertEqual(results["S02"], [["llm", 1], ["tool", 1]])
        self.assertEqual(
            results["S03"],
            {
                "identity_sha256": hashlib.sha256(
                    b'["event-2"]'
                ).hexdigest(),
                "row_count": 1,
            },
        )
        self.assertEqual(results["S04"], {"non_null_count": 2, "utf8_bytes": 24})
        self.assertEqual(
            results["S05"],
            [[
                "2030-01-01T00:00:01Z",
                "event-1",
                {
                    "failure": {"mistake_mode": "A.4"},
                    "gen_ai": {
                        "operation": {"name": "execute_tool"},
                        "output": {"messages": ["汉"]},
                    },
                },
            ]],
        )
        self.assertEqual(results["S06"]["row_count"], 6)
        self.assertEqual(results["S06"]["page_row_count"], 5)
        self.assertEqual(
            results["S06"]["rows"],
            [
                ["2030-01-01T00:00:02Z", "event-2", {"failure": {"mistake_mode": "A.3"}, "gen_ai": {"operation": {"name": "execute_tool"}, "output": {"messages": {"text": "你好"}}}}],
                ["2030-01-01T00:00:03Z", "event-3", {"failure": {"mistake_mode": "A.4"}, "gen_ai": {"operation": {"name": "chat"}, "output": {"messages": None}}}],
                ["2030-01-01T00:00:04Z", "event-4", {"failure": {"mistake_mode": "A.4"}, "gen_ai": {"operation": {"name": "chat"}, "output": {"messages": None}}}],
                ["2030-01-01T00:00:05Z", "event-5", {"failure": {"mistake_mode": "A.4"}, "gen_ai": {"operation": {"name": "chat"}, "output": {"messages": None}}}],
                ["2030-01-01T00:00:06Z", "event-6", {"failure": {"mistake_mode": "A.3"}, "gen_ai": {"operation": {"name": "execute_tool"}, "output": {"messages": None}}}],
            ],
        )
        self.assertEqual(
            results["S06"]["identity_sha256"],
            hashlib.sha256(b'["event-2","event-3","event-4","event-5","event-6"]').hexdigest(),
        )

    def test_query_parameters_map_each_supplement_query_to_its_fixed_source(self):
        """捕获 S01 至 S06 错取来源参数或漏写固定路径和页大小。"""
        source_truth = {
            "parameters": {
                "Q01": {"project_id": "p", "start_time": "t0", "end_time": "t1"},
                "Q02": {"project_id": "p", "start_time": "t0", "end_time": "t1", "operation_name": "execute_tool"},
                "Q04": {"project_id": "p", "start_time": "t0", "end_time": "t1", "trace_id": "trace"},
                "Q05": {"project_id": "p", "start_time": "t0", "end_time": "t1", "failure_mistake_mode": "A.3"},
            }
        }

        self.assertEqual(
            generator._query_parameters(source_truth),
            {
                "S01": {"project_id": "p", "start_time": "t0", "end_time": "t1"},
                "S02": {"project_id": "p", "start_time": "t0", "end_time": "t1", "operation_name": "execute_tool"},
                "S03": {"project_id": "p", "start_time": "t0", "end_time": "t1", "failure_mistake_mode": "A.3"},
                "S04": {"project_id": "p", "start_time": "t0", "end_time": "t1", "attribute_key": "gen_ai.output.messages"},
                "S05": {"project_id": "p", "start_time": "t0", "end_time": "t1", "trace_id": "trace"},
                "S06": {"project_id": "p", "start_time": "t0", "end_time": "t1", "page_size": 256},
            },
        )

    def test_write_truth_publishes_deterministic_catalog_truth_and_manifest(self):
        """捕获输入身份遗漏、非原子发布或相同输入得到不同 truth bytes。"""
        rows = [
            self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.3", ["汉"]),
            self.row("event-2", "2030-01-01T00:00:02Z", "llm", "chat", "A.4", {"text": "你好"}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            first_output = root / "first"
            second_output = root / "second"
            self.write_stage_two_input(input_dir, rows)

            with self.fixture_identity_patch(input_dir):
                generator.write_truth(input_dir, first_output, ["python3", "generator.py"])
                generator.write_truth(input_dir, second_output, ["python3", "generator.py"])

            catalog = json.loads((first_output / "query-catalog.json").read_bytes())
            truth = json.loads((first_output / "truth-manifest.json").read_bytes())
            manifest = json.loads((first_output / "run-manifest.json").read_bytes())
            self.assertEqual(catalog["query_ids"], list(generator.QUERY_IDS))
            self.assertEqual(truth["record_count"], 2)
            self.assertEqual(truth["block_count"], 1)
            self.assertEqual(truth["results"]["S06"]["row_count"], 2)
            self.assertEqual(manifest["status"], "complete")
            self.assertIn("artifact_write", manifest["timings_ns"])
            self.assertNotIn("artifact_serialization", manifest["timings_ns"])
            self.assertEqual(manifest["input"]["dataset_sha256"], manifest["source"]["artifacts"]["dataset.jsonl"]["sha256"])
            self.assertEqual(
                (first_output / "truth-manifest.json").read_bytes(),
                (second_output / "truth-manifest.json").read_bytes(),
            )

    def test_write_truth_reuses_stage_two_manifest_publisher_with_controlled_clock(self):
        """捕获生成器绕过阶段二发布器或未传递 artifact 写计时时钟。"""
        rows = [
            self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.3", None),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            output_dir = root / "output"
            self.write_stage_two_input(input_dir, rows)
            clock_values = iter((100, 170))
            clock_ns = lambda: next(clock_values)

            with self.fixture_identity_patch(input_dir):
                with patch.object(
                    generator,
                    "write_manifest_last",
                    wraps=common.write_manifest_last,
                ) as publish:
                    generator.write_truth(
                        input_dir,
                        output_dir,
                        ["python3", "generator.py"],
                        clock_ns=clock_ns,
                    )

            publish.assert_called_once()
            self.assertTrue(publish.call_args.kwargs["record_artifact_write_timing"])
            self.assertIs(publish.call_args.kwargs["clock_ns"], clock_ns)
            manifest = json.loads((output_dir / "run-manifest.json").read_bytes())
            self.assertEqual(manifest["timings_ns"]["artifact_write"], 70)

    def test_preprocess_input_rejects_dataset_identity_outside_frozen_contract(self):
        """捕获仅内部自洽、却替换了阶段二冻结 dataset 的输入。"""
        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory) / "input"
            self.write_stage_two_input(
                input_dir,
                [self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.3", None)],
            )

            with self.assertRaisesRegex(ValueError, "frozen dataset identity mismatch"):
                generator.preprocess_input(input_dir)

    def test_preprocess_input_rejects_frozen_source_input_sha256_mismatch(self):
        """捕获 dataset 保持一致但原始输入身份被替换的来源 manifest。"""
        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory) / "input"
            self.write_stage_two_input(
                input_dir,
                [self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.3", None)],
            )
            with self.fixture_identity_patch(input_dir, include_source_input=False):
                with self.assertRaisesRegex(ValueError, "frozen source input identity mismatch"):
                    generator.preprocess_input(input_dir)

    def test_write_truth_does_not_require_a_postpublication_manifest_write(self):
        """捕获 complete manifest 发布后再次写入计时字段的状态窗口。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            output_dir = root / "output"
            self.write_stage_two_input(
                input_dir,
                [self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.3", None)],
            )
            with self.fixture_identity_patch(input_dir):
                with patch.object(generator, "write_atomically", side_effect=OSError("unexpected second write"), create=True):
                    generator.write_truth(input_dir, output_dir, ["python3", "generator.py"])

            manifest = json.loads((output_dir / "run-manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "complete")

    def test_write_truth_publishes_failed_manifest_when_complete_publication_fails(self):
        """捕获最终 complete manifest 写入失败后仍遗留完成状态的错误。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            output_dir = root / "output"
            self.write_stage_two_input(
                input_dir,
                [self.row("event-1", "2030-01-01T00:00:01Z", "tool", "execute_tool", "A.3", None)],
            )
            original_write = common._stage_two_common.write_atomically

            def fail_complete_manifest(path, content):
                if Path(path).name == "run-manifest.json" and b'"status":"complete"' in content:
                    raise OSError("complete manifest unavailable")
                original_write(path, content)

            with self.fixture_identity_patch(input_dir):
                with patch.object(common._stage_two_common, "write_atomically", side_effect=fail_complete_manifest):
                    with self.assertRaisesRegex(OSError, "complete manifest unavailable"):
                        generator.write_truth(input_dir, output_dir, ["python3", "generator.py"])

            manifest = json.loads((output_dir / "run-manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "failed")

    @staticmethod
    def row(event_id, start_time, span_type, operation, failure_mode, output):
        """返回一个补充查询所需的完整手写记录。"""
        attributes = {
            "gen_ai": {
                "operation": {"name": operation},
                "output": {"messages": output},
            },
            "failure": {"mistake_mode": failure_mode},
        }
        attributes_map = {
            "failure.mistake_mode": json.dumps(failure_mode, ensure_ascii=False, separators=(",", ":")),
            "gen_ai.operation.name": json.dumps(operation, ensure_ascii=False, separators=(",", ":")),
        }
        if output is not None:
            attributes_map["gen_ai.output.messages"] = json.dumps(
                output, ensure_ascii=False, separators=(",", ":")
            )
        return {
            "attributes_analysis": attributes,
            "attributes_map": attributes_map,
            "event_id": event_id,
            "ingest_seq": int(event_id[-1]) - 1,
            "project_id": "project",
            "raw_event": f"raw-{event_id}",
            "span_type": span_type,
            "start_time": start_time,
            "trace_id": "trace-a" if event_id == "event-1" else "trace-b",
        }

    @staticmethod
    def write_stage_two_input(input_dir, rows):
        """写入通过阶段二输入验证的最小输入目录。"""
        input_dir.mkdir()
        dataset = b"".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for row in rows
        )
        source_truth = {
            "block_count": 1,
            "block_size": 256,
            "comparability_contract_version": "json-storage-cross-engine-v1",
            "input": {"sha256": "raw-input"},
            "parameters": {
                "Q01": {
                    "project_id": "project",
                    "start_time": "2030-01-01T00:00:00Z",
                    "end_time": "2030-01-01T00:00:04Z",
                },
                "Q02": {
                    "project_id": "project",
                    "start_time": "2030-01-01T00:00:00Z",
                    "end_time": "2030-01-01T00:00:04Z",
                    "operation_name": "execute_tool",
                },
                "Q04": {
                    "project_id": "project",
                    "start_time": "2030-01-01T00:00:00Z",
                    "end_time": "2030-01-01T00:00:04Z",
                    "trace_id": "trace-a",
                },
                "Q05": {
                    "project_id": "project",
                    "start_time": "2030-01-01T00:00:00Z",
                    "end_time": "2030-01-01T00:00:04Z",
                    "failure_mistake_mode": "A.3",
                },
            },
            "record_count": len(rows),
            "records": [
                {
                    "analysis_sha256": hashlib.sha256(
                        json.dumps(row["attributes_analysis"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                    "canonical_sha256": hashlib.sha256(
                        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                    "event_id": row["event_id"],
                    "raw_sha256": hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest(),
                }
                for row in rows
            ],
            "watermarks": [len(rows)],
        }
        truth = json.dumps(source_truth, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        input_dir.joinpath("dataset.jsonl").write_bytes(dataset)
        input_dir.joinpath("truth-manifest.json").write_bytes(truth)
        identity = lambda content: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        manifest = {
            "artifacts": {"dataset.jsonl": identity(dataset), "truth-manifest.json": identity(truth)},
            "block_count": 1,
            "block_size": 256,
            "comparability_contract_version": "json-storage-cross-engine-v1",
            "data_path": "independent_loader",
            "input": {"sha256": "raw-input"},
            "record_count": len(rows),
            "status": "complete",
            "watermarks": [len(rows)],
        }
        input_dir.joinpath("run-manifest.json").write_bytes(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )

    @staticmethod
    def fixture_identity_patch(input_dir, include_source_input=True):
        """为小型 fixture 绑定其实际来源身份。"""
        manifest = json.loads((input_dir / "run-manifest.json").read_bytes())
        overrides = {
            "EXPECTED_BLOCK_COUNT": manifest["block_count"],
            "EXPECTED_BLOCK_SIZE": manifest["block_size"],
            "EXPECTED_DATASET_SHA256": manifest["artifacts"]["dataset.jsonl"]["sha256"],
            "EXPECTED_RECORD_COUNT": manifest["record_count"],
        }
        if include_source_input:
            overrides["EXPECTED_SOURCE_INPUT_SHA256"] = manifest["input"]["sha256"]
        return patch.multiple(generator, create=True, **overrides)


if __name__ == "__main__":
    unittest.main()
