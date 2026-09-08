"""正式汇总的完整性拒绝门禁与可复现统计。"""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "report/summarize_results.py"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def fixture(root):
    """创建六轮完整文件，吞吐与分位数采用可手算输入。"""
    watermarks = list(range(256, 48534, 256)) + [48534]
    for engine, layouts in {"opengauss": ["og_jsonb", "og_jsonb_hot", "og_jsonb_gin"],
                            "clickhouse": ["ch_string", "ch_map", "ch_native"]}.items():
        for round_no in (1, 2, 3):
            directory = root / f"{engine}-round-{round_no}"
            manifest = {
                "status": "complete", "run_id": f"{engine}-{round_no}", "engine": engine,
                "round": round_no, "layout_order": layouts, "artifacts": {},
                "gates": dict.fromkeys(["correctness", "raw_recovery", "cleanup", "all_samples_successful"], True),
                "comparability_contract_version": "json-storage-cross-engine-v1",
                "comparability_contract": {"connection_reuse": "each worker reuses one independent connection within a stage", "latency_boundary": "statement submission through complete result read", "request_equivalent_qps_formula": "success_count * 1000 / sum(success latency_ms)", "stage_barrier": "all connected workers enter each warmup or measurement stage together", "success_gate": "all formal samples and correctness gates must succeed"},
                "data_path": "independent_loader", "cache_state": "query_warmup_1_no_os_cache_drop",
                "runner": {"path": "runner/run_cross_engine.py", "sha256": "r" * 64},
                "input": {"record_count": 48534, "block_count": 190, "block_size": 256, "seed": 42,
                          "watermarks": watermarks, **{key: {"path": key, "sha256": key * 2, "bytes": 1}
                          for key in ["source_input", "upstream_manifest", "audit", "dataset", "truth"]}},
                "environment": {"repositories": {"research": {"head": "h", "remote_main": "m"}},
                                "host": {"cpu_count": 8, "memory_bytes": 123, "disk_total_bytes": 456},
                                "container": {"image_id": engine, "repo_digests": [engine], "database_version": "1", "running": True}}}
            for layout in layouts:
                object_type = "schema" if engine == "opengauss" else "database"
                namespace = f"json_s2_r{round_no}_{layout}"
                ddl = f"CREATE {object_type.upper()} {namespace};\nCREATE TABLE {namespace}.analytics (value Int64);"
                checks = {"ok": True, "actual_count": 48534, "expected_count": 48534,
                          "duplicate_count": 0, "duplicates": [], "extra": [], "missing": []}
                samples = []
                for query in ["Q01", "Q02", "Q03", "Q04", "Q05"]:
                    for index in range(1, 101):
                        sample = {"query_id": query, "phase": "static", "watermark": 48534,
                                  "ok": True, "matches_truth": True, "latency_ms": index * round_no}
                        if engine == "clickhouse":
                            sample["query_log"] = dict.fromkeys(["memory_usage", "query_duration_ms", "read_bytes", "read_rows", "result_bytes", "result_rows", "selected_bytes", "selected_rows"], 10)
                        samples.append(sample)
                concurrent = [dict(sample, phase="concurrent", watermark=1280) for sample in samples
                              if sample["query_id"] != "Q04"]
                result = {"status": "complete", "layout": layout, "runner": manifest["runner"],
                          "ddl": {"identity": {object_type: namespace, "ddl": ddl},
                                  "sha256": hashlib.sha256(ddl.encode()).hexdigest()},
                          "queries": {"sha256": engine}, "analysis_correctness": dict(checks, analysis_sha256_mismatches=[]),
                          "raw_recovery": dict(checks, raw_sha256_mismatches=[]), "cleanup": {"removed": True},
                          "ingest": {"query_workers": 2, "samples": concurrent,
                                     "blocks": [{"rows": end - (watermarks[i - 1] if i else 0), "watermark": end,
                                                 "wall_time_ms": 10 * round_no, "input_bytes": 1048576,
                                                 "visible_after_commit": True} for i, end in enumerate(watermarks)]},
                          "static_queries": {"samples": samples, "watermark": 48534},
                          "maintenance": {"analyzed": True} if engine == "opengauss" else {"completed": True, "timed_out": False, "waited_seconds": 0.2},
                          "storage": {"analytics": {"total_bytes": 100}, "raw": {"total_bytes": 200}} if engine == "opengauss" else
                          {"merge_backlog": 0, "tables": {"analytics": {"compressed_bytes": 10, "rows": 48534}, "raw": {"compressed_bytes": 20, "rows": 48534}}, "paths": {"dynamic_paths": ["a"], "shared_paths": []} if layout == "ch_native" else None}}
                manifest["artifacts"][f"{layout}/result.json"] = write_json(directory / layout / "result.json", result)
            write_json(directory / "run-manifest.json", manifest)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), "summarizer is not implemented")
        spec = importlib.util.spec_from_file_location("summary_report", MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture(self.root)

    def mutate(self, callback, result=False):
        directory = self.root / "clickhouse-round-1"
        path = directory / ("ch_native/result.json" if result else "run-manifest.json")
        value = json.loads(path.read_text())
        callback(value)
        identity = write_json(path, value)
        if result:
            manifest_path = directory / "run-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["artifacts"]["ch_native/result.json"] = identity
            write_json(manifest_path, manifest)

    def test_complete_statistics_and_deterministic_publication(self):
        summary = self.module.summarize(self.root)
        self.assertEqual(len(summary["runs"]), 6)
        self.assertEqual(len(summary["artifacts"]), 18)
        self.assertEqual(len(summary["ddl_identities"]), 6)
        self.assertEqual(summary["ddl_identities"]["ch_native"]["normalized_ddl"],
                         "CREATE DATABASE __namespace__;\nCREATE TABLE __namespace__.analytics (value Int64);")
        stats = summary["layouts"]["og_jsonb"]["aggregate"]
        self.assertEqual(stats["static"]["Q01"]["latency_ms"]["p95"], {"median": 190, "min": 95, "max": 285})
        self.assertAlmostEqual(stats["ingest"]["rows_per_second"]["median"], 48534 / 3.8)
        self.assertAlmostEqual(stats["static"]["Q01"]["request_equivalent_qps"]["median"], 1000 / 101)
        output = self.root / "summary"
        self.module.publish(summary, output)
        first = (output / "summary.json").read_bytes()
        self.module.publish(self.module.summarize(self.root), output)
        self.assertEqual(first, (output / "summary.json").read_bytes())

    def test_rejects_failed_runs(self):
        self.mutate(lambda value: value.update(status="failed"))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.module.summarize(self.root)

    def test_rejects_artifact_tampering(self):
        path = self.root / "clickhouse-round-1/ch_native/result.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "artifact"):
            self.module.summarize(self.root)

    def test_rejects_identity_drift(self):
        for key in ["input", "runner", "cache_state", "comparability_contract", "environment"]:
            with self.subTest(key=key):
                fixture(self.root)
                self.mutate(lambda value: value.update({key: {"changed": True}}))
                with self.assertRaises(ValueError):
                    self.module.summarize(self.root)

    def test_rejects_missing_round_and_layout(self):
        self.mutate(lambda value: value.update(round=2))
        with self.assertRaisesRegex(ValueError, "round"):
            self.module.summarize(self.root)
        fixture(self.root)
        self.mutate(lambda value: value["artifacts"].pop("ch_native/result.json"))
        with self.assertRaisesRegex(ValueError, "layout"):
            self.module.summarize(self.root)

    def test_rejects_failed_truth_and_missing_samples(self):
        self.mutate(lambda value: value["static_queries"]["samples"][0].update(matches_truth=False), result=True)
        with self.assertRaisesRegex(ValueError, "sample"):
            self.module.summarize(self.root)
        fixture(self.root)
        self.mutate(lambda value: value["static_queries"]["samples"].pop(), result=True)
        with self.assertRaisesRegex(ValueError, "sample"):
            self.module.summarize(self.root)

    def test_rejects_missing_query_log_metrics(self):
        self.mutate(lambda value: value["static_queries"]["samples"][0]["query_log"].pop("read_bytes"), result=True)
        with self.assertRaisesRegex(ValueError, "query_log"):
            self.module.summarize(self.root)

    def test_rejects_invalid_completion_and_layout_contracts(self):
        mutations = [
            lambda value: value["raw_recovery"].update(ok=False),
            lambda value: value["cleanup"].update(removed=False),
            lambda value: value["ingest"].update(query_workers=1),
            lambda value: value["ingest"]["blocks"][0].update(rows=255),
            lambda value: value["ingest"]["blocks"][0].update(input_bytes=1),
            lambda value: value["queries"].update(sha256="different"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                fixture(self.root)
                self.mutate(mutation, result=True)
                with self.assertRaises(ValueError):
                    self.module.summarize(self.root)

    def test_rejects_duplicate_id_and_missing_run(self):
        self.mutate(lambda value: value.update(run_id="clickhouse-2"))
        with self.assertRaisesRegex(ValueError, "run ID"):
            self.module.summarize(self.root)
        fixture(self.root)
        (self.root / "clickhouse-round-1/run-manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "six"):
            self.module.summarize(self.root)

    def test_rejects_undeclared_results_outside_summary_directory(self):
        """阻止忽略额外布局，但允许 summary 目录保存派生输出。"""
        write_json(self.root / "summary/result.json", {})
        self.module.summarize(self.root)
        write_json(self.root / "clickhouse-round-1/rogue/result.json", {})
        with self.assertRaisesRegex(ValueError, "artifact"):
            self.module.summarize(self.root)

    def test_rejects_ddl_drift_with_consistent_artifact_and_ddl_hashes(self):
        """改变列类型且重算所有摘要时仍须拒绝同布局跨轮差异。"""
        def change_ddl(value):
            ddl = value["ddl"]["identity"]["ddl"].replace("Int64", "String")
            value["ddl"]["identity"]["ddl"] = ddl
            value["ddl"]["sha256"] = hashlib.sha256(ddl.encode()).hexdigest()

        self.mutate(change_ddl, result=True)
        with self.assertRaisesRegex(ValueError, "DDL"):
            self.module.summarize(self.root)

    def test_rejects_ddl_hash_and_namespace_identity_corruption(self):
        """阻止伪造 DDL 摘要、缺结构或把无关名字当作 namespace。"""
        mutations = [
            lambda value: value["ddl"].update(sha256="0" * 64),
            lambda value: value["ddl"]["identity"].update(database="wrong_namespace"),
            lambda value: value["ddl"]["identity"].update(database="json_s2_r1"),
            lambda value: value["ddl"].update(identity={}),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                fixture(self.root)
                self.mutate(mutation, result=True)
                with self.assertRaisesRegex(ValueError, "DDL"):
                    self.module.summarize(self.root)


if __name__ == "__main__":
    unittest.main()
