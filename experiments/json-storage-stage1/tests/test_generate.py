import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
GENERATOR = STAGE_DIR / "generator" / "generate.py"


class CorrectnessProfileGeneratorTest(unittest.TestCase):
    """验证正确性 profile 的公开文件契约。"""

    def run_generator(self, output_dir: Path, *, seed: int = 20260902):
        """运行真实 CLI，并返回输出文件内容。"""
        completed = subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--output",
                str(output_dir),
                "--count",
                "300",
                "--seed",
                str(seed),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        dataset_path = output_dir / "dataset.jsonl"
        truth_path = output_dir / "truth-manifest.json"
        duplicate_path = output_dir / "duplicate-key-probes.jsonl"
        run_path = output_dir / "run-manifest.json"
        return {
            "dataset_bytes": dataset_path.read_bytes(),
            "records": [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines()],
            "truth_bytes": truth_path.read_bytes(),
            "duplicate_bytes": duplicate_path.read_bytes(),
            "run_bytes": run_path.read_bytes(),
            "run": json.loads(run_path.read_text(encoding="utf-8")),
            "truth": json.loads(truth_path.read_text(encoding="utf-8")),
        }

    def test_same_seed_produces_identical_canonical_dataset_and_truth(self):
        """捕获随机状态泄漏、键顺序漂移和未记录的输入变化。"""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_result = self.run_generator(Path(first))
            second_result = self.run_generator(Path(second))

        self.assertEqual(first_result["dataset_bytes"], second_result["dataset_bytes"])
        self.assertEqual(first_result["truth_bytes"], second_result["truth_bytes"])
        self.assertEqual(first_result["duplicate_bytes"], second_result["duplicate_bytes"])
        self.assertEqual(first_result["run_bytes"], second_result["run_bytes"])

        lines = first_result["dataset_bytes"].decode("utf-8").splitlines()
        self.assertEqual(len(lines), 300)
        for line in lines:
            parsed = json.loads(line)
            expected = json.dumps(
                parsed,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            self.assertEqual(line, expected)

        run = first_result["run"]
        self.assertEqual(run["profile"], "correctness")
        self.assertEqual(run["record_count"], 300)
        self.assertEqual(run["seed"], 20260902)
        self.assertEqual(
            run["artifacts"]["dataset.jsonl"]["sha256"],
            hashlib.sha256(first_result["dataset_bytes"]).hexdigest(),
        )
        self.assertEqual(
            run["artifacts"]["truth-manifest.json"]["sha256"],
            hashlib.sha256(first_result["truth_bytes"]).hexdigest(),
        )
        self.assertIn("generator", run)
        self.assertEqual(run["generator"]["path"], "generator/generate.py")
        self.assertEqual(
            run["generator"]["sha256"],
            hashlib.sha256(GENERATOR.read_bytes()).hexdigest(),
        )
        artifact_content = {
            "dataset.jsonl": first_result["dataset_bytes"],
            "duplicate-key-probes.jsonl": first_result["duplicate_bytes"],
            "truth-manifest.json": first_result["truth_bytes"],
        }
        for name, content in artifact_content.items():
            self.assertEqual(run["artifacts"][name]["bytes"], len(content))
            self.assertEqual(run["artifacts"][name]["sha256"], hashlib.sha256(content).hexdigest())

    def test_truth_distinguishes_sql_null_missing_json_null_and_json_types(self):
        """捕获把三类空值或 JSON 数值类型合并的实现错误。"""
        with tempfile.TemporaryDirectory() as output:
            result = self.run_generator(Path(output))

        truth = result["truth"]
        rows = truth["rows"]
        cases = Counter(row["case"] for row in rows)
        self.assertEqual(
            cases,
            Counter(
                {
                    "metadata_sql_null": 25,
                    "metadata_path_missing": 25,
                    "metadata_json_null": 25,
                    "target_boolean": 25,
                    "target_integer": 25,
                    "target_number": 25,
                    "conflict_string": 25,
                    "conflict_integer": 25,
                    "conflict_object": 25,
                    "conflict_array": 25,
                    "escaped_path": 25,
                    "array_order": 25,
                }
            ),
        )

        by_case = {row["case"]: row for row in rows}
        self.assertEqual(by_case["metadata_sql_null"]["columns"]["metadata"], {"state": "sql_null"})
        self.assertEqual(
            by_case["metadata_path_missing"]["paths"]["/metadata/target"],
            {"state": "missing"},
        )
        self.assertEqual(
            by_case["metadata_json_null"]["paths"]["/metadata/target"],
            {"state": "value", "type": "null", "value": None},
        )
        self.assertEqual(by_case["target_boolean"]["paths"]["/metadata/target"]["type"], "boolean")
        self.assertEqual(by_case["target_integer"]["paths"]["/metadata/target"]["type"], "integer")
        self.assertEqual(by_case["target_number"]["paths"]["/metadata/target"]["type"], "number")
        self.assertEqual(by_case["conflict_string"]["paths"]["/metadata/conflict"]["type"], "string")
        self.assertEqual(by_case["conflict_integer"]["paths"]["/metadata/conflict"]["type"], "integer")
        self.assertEqual(by_case["conflict_object"]["paths"]["/metadata/conflict"]["type"], "object")
        self.assertEqual(by_case["conflict_array"]["paths"]["/metadata/conflict"]["type"], "array")

    def test_truth_preserves_array_order_and_uses_json_pointer_escaping(self):
        """捕获数组排序或包含斜线、波浪号的路径寻址错误。"""
        with tempfile.TemporaryDirectory() as output:
            result = self.run_generator(Path(output))

        by_case = {row["case"]: row for row in result["truth"]["rows"]}
        self.assertEqual(
            by_case["escaped_path"]["paths"]["/metadata/a~1b/tilde~0key"],
            {"state": "value", "type": "string", "value": "escaped"},
        )
        self.assertEqual(
            by_case["array_order"]["paths"]["/metadata/ordered"],
            {"state": "value", "type": "array", "value": [3, 2, 1]},
        )

    def test_duplicate_key_probe_keeps_raw_occurrences_outside_canonical_dataset(self):
        """捕获 JSON 解析器在实验前静默吞掉重复键的错误。"""
        with tempfile.TemporaryDirectory() as output:
            result = self.run_generator(Path(output))

        self.assertEqual(
            result["duplicate_bytes"].decode("utf-8"),
            '{"probe_id":"duplicate-key-001","metadata":{"dup":1,"dup":2}}\n',
        )
        self.assertEqual(
            result["truth"]["duplicate_key_probes"],
            [
                {
                    "duplicate_path": "/metadata/dup",
                    "occurrences": [1, 2],
                    "probe_id": "duplicate-key-001",
                    "required_observation": "record_acceptance_and_retained_value",
                }
            ],
        )
        self.assertEqual(result["truth"]["canonicalization"]["duplicate_keys"], "raw_probe_only")

    def test_different_seed_changes_identity_but_not_case_distribution(self):
        """捕获 seed 被忽略或 seed 意外改变覆盖结构的错误。"""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_result = self.run_generator(Path(first), seed=1)
            second_result = self.run_generator(Path(second), seed=2)

        first_rows = first_result["truth"]["rows"]
        second_rows = second_result["truth"]["rows"]
        self.assertEqual(
            Counter(row["case"] for row in first_rows),
            Counter(row["case"] for row in second_rows),
        )
        identity_fields = {"event_id", "trace_id", "span_id", "parent_span_id"}
        for first_record, second_record in zip(
            first_result["records"],
            second_result["records"],
            strict=True,
        ):
            self.assertEqual(
                {key: value for key, value in first_record.items() if key not in identity_fields},
                {key: value for key, value in second_record.items() if key not in identity_fields},
            )
            for field in ("event_id", "trace_id", "span_id"):
                self.assertNotEqual(first_record[field], second_record[field])
            if first_record["parent_span_id"] is None:
                self.assertIsNone(second_record["parent_span_id"])
            else:
                self.assertNotEqual(
                    first_record["parent_span_id"],
                    second_record["parent_span_id"],
                )

        for first_truth, second_truth in zip(first_rows, second_rows, strict=True):
            self.assertEqual(
                {
                    key: value
                    for key, value in first_truth.items()
                    if key not in {"canonical_sha256", "event_id"}
                },
                {
                    key: value
                    for key, value in second_truth.items()
                    if key not in {"canonical_sha256", "event_id"}
                },
            )
            self.assertNotEqual(first_truth["canonical_sha256"], second_truth["canonical_sha256"])
            self.assertNotEqual(first_truth["event_id"], second_truth["event_id"])

        for first_query, second_query in zip(
            first_result["truth"]["queries"],
            second_result["truth"]["queries"],
            strict=True,
        ):
            self.assertEqual(first_query["query_id"], second_query["query_id"])
            self.assertEqual(first_query["parameters"], second_query["parameters"])
            self.assertEqual(first_query["expected_row_count"], second_query["expected_row_count"])
            self.assertNotEqual(first_query["expected_event_ids"], second_query["expected_event_ids"])

    def test_truth_exposes_query_parameters_and_expected_event_ids(self):
        """捕获 loader 只能读数据、无法按统一参数校验查询结果的缺口。"""
        with tempfile.TemporaryDirectory() as output:
            result = self.run_generator(Path(output))

        self.assertIn("queries", result["truth"])
        queries = {query["query_id"]: query for query in result["truth"]["queries"]}
        self.assertEqual(
            set(queries),
            {
                "metadata_column_is_sql_null",
                "metadata_target_is_missing",
                "metadata_target_is_json_null",
                "metadata_target_integer_equals_one",
                "metadata_conflict_is_object",
                "escaped_path_equals_escaped",
                "array_order_equals_3_2_1",
            },
        )
        expected_counts = {query_id: 25 for query_id in queries}
        expected_counts["metadata_target_is_missing"] = 175
        for query_id, query in queries.items():
            self.assertEqual(query["expected_row_count"], expected_counts[query_id])
            self.assertEqual(len(query["expected_event_ids"]), expected_counts[query_id])
            self.assertEqual(query["expected_event_ids"], sorted(query["expected_event_ids"]))

        self.assertEqual(
            queries["metadata_target_integer_equals_one"]["parameters"],
            {"pointer": "/metadata/target", "type": "integer", "value": 1},
        )
        self.assertEqual(
            queries["escaped_path_equals_escaped"]["parameters"],
            {"pointer": "/metadata/a~1b/tilde~0key", "type": "string", "value": "escaped"},
        )

    def test_dataset_independently_reconstructs_row_truth_and_query_matches(self):
        """捕获 truth 中不存在的 ID、错误 hash、错误路径事实和错误查询命中集。"""
        with tempfile.TemporaryDirectory() as output:
            result = self.run_generator(Path(output))

        records = result["records"]
        truth_rows = {row["event_id"]: row for row in result["truth"]["rows"]}
        self.assertEqual(set(truth_rows), {record["event_id"] for record in records})

        for record in records:
            row_truth = truth_rows[record["event_id"]]
            canonical = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(row_truth["canonical_sha256"], hashlib.sha256(canonical).hexdigest())
            self.assertEqual(row_truth["case"], record["case_id"])

            if "metadata" not in record:
                self.assertEqual(row_truth["columns"]["metadata"], {"state": "sql_null"})
                self.assertEqual(
                    row_truth["paths"],
                    {"/metadata/target": {"state": "column_sql_null"}},
                )
                continue

            metadata = record["metadata"]
            metadata_bytes = json.dumps(
                metadata,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                row_truth["columns"]["metadata"],
                {"canonical_sha256": hashlib.sha256(metadata_bytes).hexdigest(), "state": "json"},
            )

            if record["case_id"] == "metadata_path_missing":
                expected_paths = {"/metadata/target": {"state": "missing"}}
            elif record["case_id"] in {
                "metadata_json_null",
                "target_boolean",
                "target_integer",
                "target_number",
            }:
                expected_paths = {
                    "/metadata/target": self.value_fact(metadata["target"]),
                }
            elif record["case_id"].startswith("conflict_"):
                expected_paths = {
                    "/metadata/conflict": self.value_fact(metadata["conflict"]),
                }
            elif record["case_id"] == "escaped_path":
                expected_paths = {
                    "/metadata/a~1b/tilde~0key": self.value_fact(metadata["a/b"]["tilde~key"]),
                }
            else:
                expected_paths = {
                    "/metadata/ordered": self.value_fact(metadata["ordered"]),
                }
            self.assertEqual(row_truth["paths"], expected_paths)

        query_predicates = {
            "metadata_column_is_sql_null": lambda row: "metadata" not in row,
            "metadata_target_is_missing": lambda row: (
                "metadata" in row and "target" not in row["metadata"]
            ),
            "metadata_target_is_json_null": lambda row: (
                "metadata" in row
                and "target" in row["metadata"]
                and row["metadata"]["target"] is None
            ),
            "metadata_target_integer_equals_one": lambda row: (
                "metadata" in row
                and type(row["metadata"].get("target")) is int
                and row["metadata"]["target"] == 1
            ),
            "metadata_conflict_is_object": lambda row: (
                "metadata" in row and isinstance(row["metadata"].get("conflict"), dict)
            ),
            "escaped_path_equals_escaped": lambda row: (
                "metadata" in row
                and row["metadata"].get("a/b", {}).get("tilde~key") == "escaped"
            ),
            "array_order_equals_3_2_1": lambda row: (
                "metadata" in row and row["metadata"].get("ordered") == [3, 2, 1]
            ),
        }
        for query in result["truth"]["queries"]:
            expected_ids = sorted(
                record["event_id"]
                for record in records
                if query_predicates[query["query_id"]](record)
            )
            self.assertEqual(query["expected_event_ids"], expected_ids)
            self.assertEqual(query["expected_row_count"], len(expected_ids))

    @staticmethod
    def value_fact(value):
        """从已解析 dataset 值独立构造 truth fact。"""
        if value is None:
            value_type = "null"
        elif isinstance(value, bool):
            value_type = "boolean"
        elif isinstance(value, int):
            value_type = "integer"
        elif isinstance(value, float):
            value_type = "number"
        elif isinstance(value, str):
            value_type = "string"
        elif isinstance(value, list):
            value_type = "array"
        else:
            value_type = "object"
        return {"state": "value", "type": value_type, "value": value}

    def test_unbalanced_count_is_rejected_before_writing_output(self):
        """捕获不平衡 case 分布被误当成有效正确性 profile 的错误。"""
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent) / "run"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    str(output),
                    "--count",
                    "13",
                    "--seed",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("count must be a positive multiple of 12", completed.stderr)
            self.assertFalse(output.exists())

    def test_failed_rerun_removes_stale_complete_manifest(self):
        """捕获覆盖失败后旧 manifest 仍把混合产物标记为完成的错误。"""
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent) / "run"
            output.mkdir()
            (output / "run-manifest.json").write_text(
                '{"status":"complete"}\n',
                encoding="utf-8",
            )
            (output / "dataset.jsonl").mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    str(output),
                    "--count",
                    "12",
                    "--seed",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((output / "run-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
