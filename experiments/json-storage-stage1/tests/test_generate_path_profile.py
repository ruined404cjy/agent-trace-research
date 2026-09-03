import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
GENERATOR = STAGE_DIR / "generator" / "generate_path_profile.py"


def load_generator_module():
    """加载生成器函数，用于验证全部受支持参数组合。"""
    spec = importlib.util.spec_from_file_location("path_profile_generator", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PathProfileGeneratorTest(unittest.TestCase):
    """验证路径数量与密度 profile 的公开文件契约。"""

    def run_generator(self, output_dir: Path, *, seed: int = 20260902):
        """运行小规模真实 CLI，并返回全部生成产物。"""
        completed = subprocess.run(
            [
                sys.executable,
                str(GENERATOR),
                "--output",
                str(output_dir),
                "--path-count",
                "50",
                "--density-percent",
                "20",
                "--count",
                "200",
                "--seed",
                str(seed),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        dataset_bytes = (output_dir / "dataset.jsonl").read_bytes()
        truth_bytes = (output_dir / "truth-manifest.json").read_bytes()
        run_bytes = (output_dir / "run-manifest.json").read_bytes()
        return {
            "dataset_bytes": dataset_bytes,
            "records": [json.loads(line) for line in dataset_bytes.splitlines()],
            "truth_bytes": truth_bytes,
            "truth": json.loads(truth_bytes),
            "run_bytes": run_bytes,
            "run": json.loads(run_bytes),
        }

    def test_profile_is_deterministic_and_truth_matches_dynamic_paths(self):
        """捕获密度、canonical hash 或查询 truth 与 JSONL 不一致的错误。"""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_result = self.run_generator(Path(first))
            second_result = self.run_generator(Path(second))

        self.assertEqual(first_result["dataset_bytes"], second_result["dataset_bytes"])
        self.assertEqual(first_result["truth_bytes"], second_result["truth_bytes"])
        self.assertEqual(first_result["run_bytes"], second_result["run_bytes"])

        records = first_result["records"]
        self.assertEqual(len(records), 200)
        path_occurrences = Counter(
            path
            for record in records
            for path in record["metadata"]["paths"]
        )
        self.assertEqual(set(path_occurrences), {f"p{index:05d}" for index in range(50)})
        self.assertEqual(set(path_occurrences.values()), {40})

        truth_rows = {
            row["event_id"]: row for row in first_result["truth"]["rows"]
        }
        self.assertEqual(set(truth_rows), {record["event_id"] for record in records})
        for record in records:
            line = json.dumps(
                record,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            metadata = json.dumps(
                record["metadata"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.assertEqual(
                truth_rows[record["event_id"]],
                {
                    "canonical_sha256": hashlib.sha256(line).hexdigest(),
                    "event_id": record["event_id"],
                    "metadata_canonical_sha256": hashlib.sha256(metadata).hexdigest(),
                },
            )

        queries = {
            query["query_id"]: query for query in first_result["truth"]["queries"]
        }
        predicates = {
            "hot_tenant_equals": lambda record, parameters: (
                record["metadata"]["hot"]["tenant"] == parameters["value"]
            ),
            "cold_path_equals": lambda record, parameters: (
                record["metadata"]["paths"].get(parameters["path"])
                == parameters["value"]
            ),
        }
        self.assertEqual(set(queries), set(predicates))
        for query_id, query in queries.items():
            expected_ids = sorted(
                record["event_id"]
                for record in records
                if predicates[query_id](record, query["parameters"])
            )
            self.assertEqual(query["expected_event_ids"], expected_ids)
            self.assertEqual(query["expected_row_count"], len(expected_ids))

        run = first_result["run"]
        self.assertEqual(run["profile"], "path-organization")
        self.assertEqual(run["path_count"], 50)
        self.assertEqual(run["density_percent"], 20)
        self.assertEqual(run["record_count"], 200)
        self.assertEqual(run["seed"], 20260902)
        self.assertEqual(
            run["artifacts"]["dataset.jsonl"],
            {
                "bytes": len(first_result["dataset_bytes"]),
                "sha256": hashlib.sha256(first_result["dataset_bytes"]).hexdigest(),
            },
        )

    def test_target_bytes_selects_balanced_record_count(self):
        """捕获不同路径密度沿用固定行数、导致输入规模失衡的错误。"""
        target_bytes = 1024 * 1024
        with tempfile.TemporaryDirectory() as output:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    output,
                    "--path-count",
                    "50",
                    "--density-percent",
                    "1",
                    "--target-bytes",
                    str(target_bytes),
                    "--seed",
                    "20260902",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output_dir = Path(output)
            run = json.loads((output_dir / "run-manifest.json").read_text(encoding="utf-8"))
            dataset_bytes = (output_dir / "dataset.jsonl").stat().st_size

        self.assertEqual(run["target_input_bytes"], target_bytes)
        self.assertEqual(run["record_count"] % 100, 0)
        self.assertEqual(run["artifacts"]["dataset.jsonl"]["bytes"], dataset_bytes)
        self.assertGreaterEqual(dataset_bytes, target_bytes * 0.95)
        self.assertLessEqual(dataset_bytes, target_bytes * 1.05)

    def test_invalid_target_removes_stale_complete_manifest(self):
        """捕获昂贵生成开始前旧完成标记仍保持有效的错误。"""
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent) / "run"
            output.mkdir()
            manifest = output / "run-manifest.json"
            manifest.write_text('{"status":"complete"}\n', encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--output",
                    str(output),
                    "--path-count",
                    "50",
                    "--density-percent",
                    "1",
                    "--target-bytes",
                    "-1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(manifest.exists())

    def test_every_supported_path_has_exact_density_per_period(self):
        """捕获仅部分路径数或密度组合满足总体密度的错误。"""
        generator = load_generator_module()
        for path_count in generator.SUPPORTED_PATH_COUNTS:
            for density in generator.SUPPORTED_DENSITIES:
                occurrences = Counter(
                    path
                    for row_index in range(generator.DENSITY_PERIOD)
                    for path in generator.build_record(
                        row_index,
                        path_count,
                        density,
                        20260902,
                    )["metadata"]["paths"]
                )
                self.assertEqual(len(occurrences), path_count)
                self.assertEqual(set(occurrences.values()), {density})


if __name__ == "__main__":
    unittest.main()
