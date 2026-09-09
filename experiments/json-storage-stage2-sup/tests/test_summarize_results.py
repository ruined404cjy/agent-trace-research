import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
MODULE = STAGE_DIR / "report/summarize_results.py"
HASHES = {
    "S01": "11b66e4cd5d0b2fd8310af52da1cdb9c76aeacada8d8fdbf821433aa2d1ac47c",
    "S02": "868ef72164c999ac75e4334700aeb1d3e1c12a7bd1e4ff9e4472a4ec1bb1c37e",
    "S03": "db0e00e0373f7d7a4fc16b3908824f6aa6be797369e2b8e0c040af9e8d40c69c",
    "S04": "ae94332c2d8a203e55c5d00f6baeda1202ff893d815923ab61f6fd40adc631a9",
    "S05": "cdd66eba4ca33847acf97d3cd89e65d42f3f488c8cb16cc46c926b444b65b167",
    "S06": "a1fc89802da0ccf1d5b7bc349b4b7b69b39bce93ec08325bc8ecbadd28388b3d",
}
ROWS = {"S01": 3, "S02": 1, "S03": 741, "S04": 4277, "S05": 6, "S06": 256}
INPUT_IDENTITIES = {
    "dataset": {"bytes": 302518948, "sha256": "8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405"},
    "truth": {"bytes": 138577, "sha256": "929d79b729c5b7ad5fa51150ee00f55249647c91365271e02ffc345e6760bb28"},
    "query_catalog": {"bytes": 1074, "sha256": "554f7ead33fc17aada0432997ff314f84b6f44e7945d8355544818f5d868c4a8"},
    "input_run_manifest": {"bytes": 2397, "sha256": "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437"},
    "input_truth_manifest": {"bytes": 18073179, "sha256": "b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04"},
}


def recovery(field):
    """返回 48,534 行恢复门禁的完整成功结构。"""
    return {
        "actual_count": 48534, "duplicate_count": 0, "duplicates": [],
        "expected_count": 48534, "extra": [], "missing": [],
        f"{field}_mismatches": [], "ok": True,
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    path.write_bytes(content)
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def fixture(root):
    layouts = ("og_json", "og_jsonb", "ch_string", "ch_native")
    orders = (layouts, ("og_jsonb", "ch_native", "og_json", "ch_string"),
              ("ch_string", "og_json", "ch_native", "og_jsonb"),
              ("ch_native", "ch_string", "og_jsonb", "og_json"))
    for round_no, order in enumerate(orders, 1):
        directory = root / f"round-{round_no}"
        runner = {"path": "runner/run_four_layouts.py", "sha256": "a" * 64,
                  "files": {name: ("a" if name == "runner/run_four_layouts.py" else "b") * 64
                            for name in ("runner/run_four_layouts.py", "runner/supplement_common.py", "runner/opengauss_four_layout.py", "runner/clickhouse_four_layout.py")}}
        environment = {"host": {"cpu_count": 8, "memory_bytes": 123, "disk_total_bytes": 456,
                                "platform": "Linux-x", "kernel": "6.0"},
                       "containers": {
                           "opengauss": {"name": "agent-trace-opengauss-v6", "image": "og:6",
                               "image_id": "sha256:" + "a" * 64,
                               "ports": {"5432/tcp": [{"HostIp": "", "HostPort": "15432"}]},
                               "memory_bytes": 0, "cpu_nanocpus": 0, "running": True,
                               "repo_digests": ["og@sha256:" + "b" * 64],
                               "database_version": "(openGauss 6.0.0 build 1)"},
                           "clickhouse": {"name": "agent-trace-clickhouse-25-12", "image": "ch:25.12",
                               "image_id": "sha256:" + "c" * 64,
                               "ports": {"8123/tcp": [{"HostIp": "", "HostPort": "18123"}]},
                               "memory_bytes": 0, "cpu_nanocpus": 0, "running": True,
                               "repo_digests": ["ch@sha256:" + "d" * 64],
                               "database_version": "25.12.11.4"}}}
        input_identity = {**INPUT_IDENTITIES,
                          "source_input": {"sha256": "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683", "path": "/source/traces.jsonl"}}
        namespace = f"s2sup_r{round_no}"
        manifest = {"status": "complete", "round": round_no, "layout_order": list(order),
                    "format": "agent-trace-json-storage-four-layout-run", "format_version": 1,
                    "supplement_contract_version": "json-storage-four-layout-v1",
                    "comparability_contract_version": "json-storage-cross-engine-v1",
                    "comparability_contract": {"connection_reuse": "each worker reuses one independent connection within a stage", "latency_boundary": "statement submission through complete result read", "request_equivalent_qps_formula": "success_count * 1000 / sum(success latency_ms)", "stage_barrier": "all connected workers enter each warmup or measurement stage together", "success_gate": "all formal samples and correctness gates must succeed"},
                    "data_path": "independent_loader", "cache_state": "query_warmup_1_no_os_cache_drop",
                    "run_id": f"four-layout-r{round_no}-{'a' * 32}", "namespace": namespace,
                    "command": ["python", "run_four_layouts.py"],
                    "measurements": {"S01-S05": 100, "S06": 20, "query_workers": 2},
                    "runner": runner, "input": input_identity, "environment": environment,
                    "artifacts": {}, "gates": dict.fromkeys(("truth", "analysis", "raw", "cleanup", "samples"), True)}
        for layout in layouts:
            samples = []
            warmups = []
            for query in ("S01", "S02", "S03", "S04", "S05", "S06"):
                count = 20 if query == "S06" else 100
                for value in range(1, count + 1):
                    sample = {"query_id": query, "latency_ms": value * round_no, "recovery_ms": 0.1,
                              "matches_truth": True, "ok": True, "phase": "measurement",
                              "worker": (value - 1) % 2, "result_sha256": HASHES[query],
                              "row_count": ROWS[query]}
                    if layout.startswith("ch_"):
                        sample["query_log"] = dict.fromkeys(("query_duration_ms", "read_rows", "read_bytes", "memory_usage", "result_rows", "result_bytes", "selected_rows", "selected_bytes"), 1)
                        sample["query_log_id"] = f"r{round_no}-{layout}-{query}-{value}"
                    samples.append(sample)
                warmup = {"query_id": query, "latency_ms": 1, "recovery_ms": 0.1,
                          "matches_truth": True, "ok": True, "phase": "warmup",
                          "worker": 0, "result_sha256": HASHES[query], "row_count": ROWS[query]}
                if layout.startswith("ch_"):
                    warmup["query_log"] = dict.fromkeys(("query_duration_ms", "read_rows", "read_bytes", "memory_usage", "result_rows", "result_bytes", "selected_rows", "selected_bytes"), 1)
                    warmup["query_log_id"] = f"warmup-r{round_no}-{layout}-{query}"
                warmups.append(warmup)
            object_name = f"{namespace}_{layout}"
            ddl = f"CREATE SCHEMA {object_name}; CREATE TABLE {object_name}.analytics(value JSON);"
            storage = ({name: {"heap_bytes": 1, "toast_bytes": 2, "index_bytes": 3, "total_bytes": 6}
                        for name in ("analytics", "raw")} if layout.startswith("og_") else
                       {"merge_backlog": 0, "tables": {name: {"part_count": 1, "rows": 48534,
                        "compressed_bytes": 10, "uncompressed_bytes": 20} for name in ("analytics", "raw")},
                        "paths": {"dynamic_paths": ["a"], "shared_paths": []} if layout == "ch_native" else None})
            result = {"status": "complete", "layout": layout, "round": round_no,
                      "layout_order": list(order), "input": input_identity, "environment": environment,
                      "run_id": manifest["run_id"], "namespace": namespace, "command": manifest["command"],
                      "cache_state": manifest["cache_state"], "measurements": manifest["measurements"],
                      "ddl": {"identity": {"ddl": ddl, ("schema" if layout.startswith("og_") else "database"): object_name}, "sha256": hashlib.sha256(ddl.encode()).hexdigest()},
                      "queries": {"sha256": hashlib.sha256(layout.encode()).hexdigest()}, "runner": runner,
                      "query_stage": {"workers": 2, "worker_sample_counts": {"0": 260, "1": 260},
                                      "plans": {query: {"natural": query} for query in HASHES},
                                      "samples": samples, "warmups": warmups},
                      "ingest": {"blocks": [{"rows": rows, "block_wall_ms": 1.0, **(
                          {"analysis_copy_ms": 0.2, "raw_copy_ms": 0.2, "commit_ms": 0.1}
                          if layout.startswith("og_") else
                          {"analytics_insert_ms": 0.2, "raw_insert_ms": 0.2,
                           "client_row_build_ms": 0.1, "response_read_ms": 0.1})}
                          for rows in ([256] * 189 + [150])]},
                      "maintenance": {"analyzed": True} if layout.startswith("og_") else {"completed": True},
                      "analysis_recovery": recovery("analysis_sha256"),
                      "raw_recovery": recovery("raw_sha256"),
                      "cleanup": {("schema" if layout.startswith("og_") else "database"): object_name,
                                  "removed": True}, "storage": storage}
            manifest["artifacts"][f"result-{layout}.json"] = write_json(directory / f"result-{layout}.json", result)
        write_json(directory / "run-manifest.json", manifest)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), "summarizer missing")
        spec = importlib.util.spec_from_file_location("s2sup_summary", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)

    def mutate(self, callback, result=False, layout="ch_native"):
        directory = self.root / "round-1"
        path = directory / (f"result-{layout}.json" if result else "run-manifest.json")
        value = json.loads(path.read_bytes())
        callback(value)
        identity = write_json(path, value)
        if result:
            manifest = json.loads((directory / "run-manifest.json").read_bytes())
            manifest["artifacts"][path.name] = identity
            write_json(directory / "run-manifest.json", manifest)

    def test_summarizes_four_rounds_and_is_deterministic(self):
        self.mutate(lambda r: r["query_stage"]["warmups"][0].pop("query_log"), result=True)
        summary = self.module.summarize(self.root)
        self.assertEqual(summary["result_count"], 16)
        self.assertEqual(summary["rounds"], [1, 2, 3, 4])
        self.assertEqual(summary["layouts"]["og_json"]["queries"]["S01"]["p95"], {"median": 237.5, "min": 95, "max": 380})
        self.assertNotIn("p99", summary["layouts"]["og_json"]["queries"]["S06"])
        output = self.root / "summary"
        self.module.publish(summary, output)
        first = (output / "summary.json").read_bytes()
        self.module.publish(self.module.summarize(self.root), output)
        self.assertEqual(first, (output / "summary.json").read_bytes())
        self.assertTrue((output / "tables.md").is_file())

    def test_rejects_missing_round_duplicate_layout_and_wrong_order(self):
        (self.root / "round-4/run-manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "four rounds"):
            self.module.summarize(self.root)
        fixture(self.root)
        self.mutate(lambda value: value["layout_order"].__setitem__(1, "og_json"))
        with self.assertRaisesRegex(ValueError, "order"):
            self.module.summarize(self.root)

    def test_rejects_tamper_truth_recovery_samples_and_queryfinish(self):
        path = self.root / "round-1/result-ch_native.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "artifact"):
            self.module.summarize(self.root)
        for mutation, message in (
            (lambda r: r["query_stage"]["samples"][0].update(matches_truth=False), "truth"),
            (lambda r: r["analysis_recovery"].update(ok=False), "analysis"),
            (lambda r: r["raw_recovery"].update(ok=False), "raw"),
            (lambda r: r["cleanup"].update(removed=False), "cleanup"),
            (lambda r: r["query_stage"]["samples"].pop(), "sample"),
            (lambda r: r["query_stage"]["samples"].append(dict(r["query_stage"]["samples"][0])), "sample"),
            (lambda r: r["query_stage"]["samples"].append({**r["query_stage"]["samples"][0], "query_id": "UNKNOWN"}), "query ID"),
            (lambda r: [sample.update(worker=0) for sample in r["query_stage"]["samples"]], "worker"),
            (lambda r: r["query_stage"].update(worker_sample_counts={"0": 520, "1": 0}), "worker"),
            (lambda r: r["query_stage"]["samples"][0].update(latency_ms=0), "latency"),
            (lambda r: r["query_stage"]["samples"][0].update(recovery_ms=-1), "recovery"),
            (lambda r: r["query_stage"]["samples"][0].update(result_sha256="tampered"), "result SHA"),
            (lambda r: r["query_stage"]["samples"][0].update(row_count=-1), "row count"),
            (lambda r: r["query_stage"]["samples"][1].update(query_log_id=r["query_stage"]["samples"][0]["query_log_id"]), "query log ID"),
            (lambda r: r["query_stage"]["warmups"][0].update(matches_truth=False), "truth"),
            (lambda r: r["query_stage"]["samples"][0]["query_log"].pop("read_bytes"), "QueryFinish"),
            (lambda r: r["query_stage"]["samples"][0]["query_log"].update(read_bytes=-1), "QueryFinish"),
            (lambda r: r["ingest"]["blocks"].pop(), "block"),
            (lambda r: r["maintenance"].update(completed=False), "maintenance"),
        ):
            fixture(self.root)
            self.mutate(mutation, result=True)
            with self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)

    def test_rejects_undeclared_result_and_ddl_corruption(self):
        """捕获未声明结果、DDL 摘要损坏或同布局结构漂移。"""
        write_json(self.root / "round-1/result-rogue.json", {})
        with self.assertRaisesRegex(ValueError, "artifact set"):
            self.module.summarize(self.root)
        fixture(self.root)
        self.mutate(lambda result: result["ddl"].update(sha256="0" * 64), result=True)
        with self.assertRaisesRegex(ValueError, "DDL"):
            self.module.summarize(self.root)

    def test_rejects_missing_result_and_manifest_contract_fields(self):
        """捕获四轮共同缺少身份、环境、查询或空间字段。"""
        for mutation, message in (
            (lambda r: r.pop("queries"), "queries"),
            (lambda r: r.pop("runner"), "runner"),
            (lambda r: r.pop("storage"), "storage"),
        ):
            fixture(self.root)
            self.mutate(mutation, result=True)
            with self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)
        for mutation, message in (
            (lambda m: m.pop("format"), "format"),
            (lambda m: m.update(cache_state="changed"), "cache"),
            (lambda m: m["measurements"].update({"S06": 21}), "measurement"),
            (lambda m: m["input"]["truth"].update(sha256="0" * 64), "input"),
        ):
            fixture(self.root)
            self.mutate(mutation)
            with self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)

    def test_rejects_wrong_fixed_environment_and_missing_container_fields(self):
        """捕获固定版本、容器名、端口或容器身份字段漂移。"""
        mutations = (
            (lambda m: m["environment"]["containers"]["opengauss"].update(database_version="wrong-version"), "version"),
            (lambda m: m["environment"]["containers"]["clickhouse"].update(database_version="25.12.0.0"), "version"),
            (lambda m: m["environment"]["containers"]["opengauss"].update(name="opengauss"), "container"),
            (lambda m: m["environment"]["containers"]["clickhouse"]["ports"]["8123/tcp"][0].update(HostPort="8123"), "port"),
        )
        for mutation, message in mutations:
            fixture(self.root)
            self.mutate(mutation)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)
        for field in ("name", "image", "image_id", "ports", "repo_digests", "running",
                      "memory_bytes", "cpu_nanocpus"):
            fixture(self.root)
            self.mutate(lambda m, field=field: m["environment"]["containers"]["opengauss"].pop(field))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "container"):
                self.module.summarize(self.root)

    def test_rejects_incomplete_recovery_and_wrong_cleanup_namespace(self):
        """捕获恢复布尔值与明细矛盾，或清理对象不是布局命名空间。"""
        for mutation, message in (
            (lambda r: r["analysis_recovery"].update(actual_count=0), "analysis recovery"),
            (lambda r: r["analysis_recovery"].update(missing=["event"]), "analysis recovery"),
            (lambda r: r["raw_recovery"].pop("raw_sha256_mismatches"), "raw recovery"),
            (lambda r: r["raw_recovery"].update(duplicates=["event"], duplicate_count=1), "raw recovery"),
            (lambda r: r["cleanup"].update(database="wrong_database"), "cleanup"),
        ):
            fixture(self.root)
            self.mutate(mutation, result=True)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)

    def test_rejects_zero_storage_and_invalid_native_paths(self):
        """捕获已载入表的全零空间、错误行数及非字符串 Native JSON 路径。"""
        for layout, mutation, message in (
            ("og_json", lambda r: [table.update(heap_bytes=0, toast_bytes=0, index_bytes=0, total_bytes=0)
                                    for table in r["storage"].values()], "openGauss storage"),
            ("ch_native", lambda r: [table.update(part_count=0, rows=0, compressed_bytes=0, uncompressed_bytes=0)
                                      for table in r["storage"]["tables"].values()], "ClickHouse storage"),
            ("ch_native", lambda r: r["storage"]["paths"]["dynamic_paths"].append(1), "Native JSON paths"),
        ):
            fixture(self.root)
            self.mutate(mutation, result=True, layout=layout)
            with self.subTest(layout=layout), self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)

    def test_rejects_wrong_file_bytes_and_source_input_structure(self):
        """捕获固定 SHA 正确但 bytes 错误，或 source input 结构不完整。"""
        for name in ("dataset", "truth", "query_catalog", "input_run_manifest", "input_truth_manifest"):
            fixture(self.root)
            self.mutate(lambda m, name=name: m["input"][name].pop("bytes"))
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "input"):
                self.module.summarize(self.root)
        for mutation in (
            lambda m: m["input"]["source_input"].pop("path"),
            lambda m: m["input"]["source_input"].update(bytes=1),
        ):
            fixture(self.root)
            self.mutate(mutation)
            with self.assertRaisesRegex(ValueError, "input"):
                self.module.summarize(self.root)

    def test_rejects_boolean_counts_and_malformed_environment_types(self):
        """捕获 JSON 布尔值冒充计数，或容器版本、端口使用错误类型。"""
        for mutation, message in (
            (lambda r: r["analysis_recovery"].update(duplicate_count=False), "analysis recovery"),
            (lambda r: r["storage"].update(merge_backlog=False), "ClickHouse storage"),
        ):
            fixture(self.root)
            self.mutate(mutation, result=True)
            with self.assertRaisesRegex(ValueError, message):
                self.module.summarize(self.root)
        for mutation in (
            lambda m: m["environment"]["containers"]["opengauss"].update(database_version=6),
            lambda m: m["environment"]["containers"]["opengauss"].update(ports=[]),
        ):
            fixture(self.root)
            self.mutate(mutation)
            with self.assertRaisesRegex(ValueError, "container"):
                self.module.summarize(self.root)


if __name__ == "__main__":
    unittest.main()
