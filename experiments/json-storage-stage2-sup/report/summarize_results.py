#!/usr/bin/env python3
"""严格校验四轮结果并生成确定性汇总。"""

import argparse
import hashlib
import importlib.util
import json
import statistics
import math
import re
from collections import defaultdict
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("s2sup_common", STAGE_DIR / "runner/supplement_common.py")
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
LAYOUTS, QUERY_IDS, ROUND_ORDERS = common.LAYOUTS, common.QUERY_IDS, common.ROUND_ORDERS
QUERY_LOG_METRICS = {"query_duration_ms", "read_rows", "read_bytes", "memory_usage",
                     "result_rows", "result_bytes", "selected_rows", "selected_bytes"}
EXPECTED_INPUT = {
    "dataset": {"bytes": 302518948, "sha256": "8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405"},
    "truth": {"bytes": 138577, "sha256": "929d79b729c5b7ad5fa51150ee00f55249647c91365271e02ffc345e6760bb28"},
    "query_catalog": {"bytes": 1074, "sha256": "554f7ead33fc17aada0432997ff314f84b6f44e7945d8355544818f5d868c4a8"},
    "input_run_manifest": {"bytes": 2397, "sha256": "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437"},
    "input_truth_manifest": {"bytes": 18073179, "sha256": "b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04"},
}
EXPECTED_SOURCE_SHA256 = "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683"
EXPECTED_CODE_FILES = {"runner/run_four_layouts.py", "runner/supplement_common.py",
                       "runner/opengauss_four_layout.py", "runner/clickhouse_four_layout.py"}
CONTAINER_FIELDS = {"name", "image", "image_id", "ports", "memory_bytes", "cpu_nanocpus",
                    "repo_digests", "running", "database_version"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _round_stats(samples, query_id):
    """按 nearest-rank 计算单轮查询延迟统计。"""
    values = [sample["latency_ms"] for sample in samples if sample.get("query_id") == query_id]
    expected = 20 if query_id == "S06" else 100
    require(len(values) == expected, f"invalid sample count: {query_id}")
    require(all(type(value) in (int, float) and math.isfinite(value) and value > 0 for value in values),
            f"invalid latency: {query_id}")
    result = {"p50": common.nearest_rank(values, 50), "p95": common.nearest_rank(values, 95)}
    if query_id != "S06":
        result["p99"] = common.nearest_rank(values, 99)
    return result


def _aggregate(values):
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def _normalized_ddl(result):
    """验证 DDL 摘要，并将本轮 schema/database 名规范化。"""
    try:
        identity = result["ddl"]["identity"]
        ddl = identity["ddl"]
        namespace = identity.get("schema", identity.get("database"))
    except (KeyError, TypeError) as error:
        raise ValueError("invalid DDL identity") from error
    require(isinstance(ddl, str) and isinstance(namespace, str) and namespace, "invalid DDL identity")
    expected_namespace = f"{result.get('namespace')}_{result.get('layout')}"
    object_field = "schema" if result.get("layout", "").startswith("og_") else "database"
    require(identity.get(object_field) == expected_namespace and namespace == expected_namespace,
            "invalid DDL namespace")
    require(hashlib.sha256(ddl.encode()).hexdigest() == result["ddl"].get("sha256"), "DDL hash mismatch")
    return ddl.replace(namespace, "__namespace__")


def _valid_queryfinish(sample):
    values = sample.get("query_log")
    return (isinstance(values, dict) and set(values) == QUERY_LOG_METRICS
            and all(type(value) is int and value >= 0 for value in values.values()))


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_runner(value):
    require(isinstance(value, dict) and value.get("path") == "runner/run_four_layouts.py", "invalid runner identity")
    require(_sha(value.get("sha256")) and value.get("sha256") == value.get("files", {}).get("runner/run_four_layouts.py"), "invalid runner SHA")
    require(set(value.get("files", {})) == EXPECTED_CODE_FILES
            and all(_sha(item) for item in value["files"].values()), "invalid runner code files")


def _validate_environment(value):
    require(isinstance(value, dict), "invalid environment identity")
    host = value.get("host", {})
    require(type(host.get("cpu_count")) is int and host["cpu_count"] > 0
            and type(host.get("memory_bytes")) is int and host["memory_bytes"] > 0
            and type(host.get("disk_total_bytes")) is int and host["disk_total_bytes"] > 0
            and all(isinstance(host.get(field), str) and host[field] for field in ("platform", "kernel")),
            "invalid host environment")
    containers = value.get("containers", {})
    require(set(containers) == {"opengauss", "clickhouse"}, "invalid container environment")
    expected = {
        "opengauss": ("agent-trace-opengauss-v6", "5432/tcp", "15432"),
        "clickhouse": ("agent-trace-clickhouse-25-12", "8123/tcp", "18123"),
    }
    for engine, container in containers.items():
        require(isinstance(container, dict) and set(container) == CONTAINER_FIELDS,
                "invalid container environment")
        require(container.get("running") is True
                and all(isinstance(container.get(field), str) and container[field]
                        for field in ("name", "image", "image_id", "database_version"))
                and type(container.get("memory_bytes")) is int and container["memory_bytes"] >= 0
                and type(container.get("cpu_nanocpus")) is int and container["cpu_nanocpus"] >= 0
                and isinstance(container.get("ports"), dict) and container["ports"]
                and isinstance(container.get("repo_digests"), list) and container["repo_digests"]
                and all(isinstance(item, str) and item for item in container["repo_digests"]),
                "invalid container environment")
        name, container_port, host_port = expected[engine]
        require(container["name"] == name, "invalid fixed container name")
        bindings = container.get("ports", {}).get(container_port)
        require(isinstance(bindings, list) and bindings
                and any(isinstance(item, dict) and item.get("HostPort") == host_port for item in bindings),
                "invalid fixed container port")
    require(containers["opengauss"]["database_version"].startswith("(openGauss 6.0.0 "),
            "invalid openGauss database version")
    require(containers["clickhouse"]["database_version"] == "25.12.11.4",
            "invalid ClickHouse database version")


def _validate_input(value):
    require(isinstance(value, dict), "invalid input identity")
    require(set(value) == {*EXPECTED_INPUT, "source_input"}
            and all(value.get(name) == identity for name, identity in EXPECTED_INPUT.items()),
            "invalid fixed input identity")
    source = value.get("source_input")
    require(isinstance(source, dict) and set(source) == {"path", "sha256"}
            and isinstance(source.get("path"), str) and source["path"]
            and source.get("sha256") == EXPECTED_SOURCE_SHA256, "invalid source input identity")


def _validate_storage(layout, storage):
    require(isinstance(storage, dict), "invalid storage result")
    if layout.startswith("og_"):
        require(set(storage) == {"analytics", "raw"}, "invalid openGauss storage")
        for table in storage.values():
            require(set(table) == {"heap_bytes", "toast_bytes", "index_bytes", "total_bytes"}
                    and all(type(value) is int and value >= 0 for value in table.values()), "invalid openGauss storage")
            require(table["total_bytes"] == table["heap_bytes"] + table["toast_bytes"] + table["index_bytes"],
                    "invalid openGauss storage total")
            require(table["heap_bytes"] > 0 and table["total_bytes"] > 0, "invalid openGauss storage")
        return
    require(set(storage) == {"merge_backlog", "tables", "paths"}
            and type(storage.get("merge_backlog")) is int and storage["merge_backlog"] == 0
            and set(storage.get("tables", {})) == {"analytics", "raw"},
            "invalid ClickHouse storage")
    for table in storage["tables"].values():
        require(set(table) == {"part_count", "rows", "compressed_bytes", "uncompressed_bytes"}
                and table.get("rows") == 48534
                and all(type(table.get(field)) is int and table[field] > 0
                        for field in ("part_count", "compressed_bytes", "uncompressed_bytes")),
                "invalid ClickHouse storage")
    paths = storage.get("paths")
    require((layout == "ch_native" and isinstance(paths, dict) and set(paths) == {"dynamic_paths", "shared_paths"}
             and all(isinstance(paths[name], list)
                     and all(isinstance(item, str) for item in paths[name]) for name in paths))
            or (layout == "ch_string" and paths is None),
            "invalid Native JSON paths")


def _validate_recovery(value, mismatch_field, label):
    """验证 48,534 行恢复结果的计数、集合和摘要明细。"""
    fields = {"actual_count", "duplicate_count", "duplicates", "expected_count",
              "extra", "missing", mismatch_field, "ok"}
    require(isinstance(value, dict) and set(value) == fields
            and type(value.get("actual_count")) is int and value["actual_count"] == 48534
            and type(value.get("expected_count")) is int and value["expected_count"] == 48534
            and type(value.get("duplicate_count")) is int and value["duplicate_count"] == 0
            and value.get("ok") is True
            and all(value.get(field) == [] for field in ("duplicates", "extra", "missing", mismatch_field)),
            f"{label} recovery failed")


def _validate_cleanup(layout, namespace, value):
    """验证已删除对象就是当前布局的独占 schema 或 database。"""
    object_field = "schema" if layout.startswith("og_") else "database"
    require(isinstance(value, dict) and set(value) == {object_field, "removed"}
            and value.get(object_field) == f"{namespace}_{layout}" and value.get("removed") is True,
            "cleanup failed")


def _validate_samples(layout, stage, query_log_ids):
    samples, warmups = stage.get("samples"), stage.get("warmups")
    require(stage.get("workers") == 2 and stage.get("worker_sample_counts") == {"0": 260, "1": 260},
            "invalid worker distribution")
    require(isinstance(samples, list) and len(samples) == 520
            and set(sample.get("query_id") for sample in samples) == set(QUERY_IDS), "invalid query ID or sample total")
    require(isinstance(warmups, list) and sorted(sample.get("query_id") for sample in warmups) == list(QUERY_IDS),
            "warmup sample contract failed")
    for query in QUERY_IDS:
        selected = [sample for sample in samples if sample["query_id"] == query]
        count = 20 if query == "S06" else 100
        require(len(selected) == count and {worker: sum(sample.get("worker") == worker for sample in selected)
                                            for worker in (0, 1)} == {0: count // 2, 1: count // 2},
                "invalid worker distribution")
    for sample in samples + warmups:
        query = sample.get("query_id")
        require(sample.get("ok") is True and sample.get("matches_truth") is True, "truth sample failed")
        require(sample.get("result_sha256") == common.EXPECTED_RESULT_SHA256.get(query), "invalid result SHA")
        require(sample.get("row_count") == common.EXPECTED_ROW_COUNTS.get(query), "invalid row count")
        require(type(sample.get("latency_ms")) in (int, float) and math.isfinite(sample["latency_ms"])
                and sample["latency_ms"] > 0, "invalid latency")
        require(type(sample.get("recovery_ms")) in (int, float) and math.isfinite(sample["recovery_ms"])
                and sample["recovery_ms"] >= 0, "invalid recovery")
    require(all(sample.get("phase") == "measurement" for sample in samples)
            and all(sample.get("phase") == "warmup" for sample in warmups), "invalid sample phase")
    if layout.startswith("ch_"):
        for sample in samples:
            query_id = sample.get("query_log_id")
            require(isinstance(query_id, str) and query_id and query_id not in query_log_ids,
                    "invalid or duplicate query log ID")
            require(_valid_queryfinish(sample), "ClickHouse QueryFinish missing")
            query_log_ids.add(query_id)
        for sample in warmups:
            if "query_log" in sample:
                require(isinstance(sample.get("query_log_id"), str) and sample["query_log_id"]
                        and _valid_queryfinish(sample), "invalid warmup QueryFinish")
    return samples, warmups


def summarize(root):
    """校验四轮 16 个结果，并汇总每轮与跨轮统计。"""
    root = Path(root)
    paths = sorted(root.glob("*/run-manifest.json"))
    require(len(paths) == 4, "expected four rounds")
    layouts = defaultdict(list)
    seen_rounds, seen_run_ids, seen_namespaces = set(), set(), set()
    shared_input = shared_environment = None
    result_count = 0
    declared_artifacts = set()
    query_log_ids = set()
    for path in paths:
        manifest = common.read_json(path, "run manifest")
        require(manifest.get("status") == "complete", "incomplete run")
        require(manifest.get("format") == "agent-trace-json-storage-four-layout-run"
                and manifest.get("format_version") == 1, "invalid manifest format")
        require(manifest.get("supplement_contract_version") == common.CONTRACT_VERSION, "invalid supplement contract")
        stage_common = common._stage_two_common
        require(manifest.get("comparability_contract_version") == stage_common.COMPARABILITY_CONTRACT_VERSION
                and manifest.get("comparability_contract") == stage_common.COMPARABILITY_CONTRACT
                and manifest.get("data_path") == stage_common.DATA_PATH, "invalid comparability contract")
        require(manifest.get("cache_state") == "query_warmup_1_no_os_cache_drop", "invalid cache state")
        require(manifest.get("measurements") == {"S01-S05": 100, "S06": 20, "query_workers": 2}, "invalid measurement contract")
        require(isinstance(manifest.get("command"), list) and manifest["command"]
                and all(isinstance(item, str) for item in manifest["command"]), "invalid command")
        round_no = manifest.get("round")
        require(round_no in range(1, 5) and round_no not in seen_rounds, "duplicate or invalid round")
        seen_rounds.add(round_no)
        require(isinstance(manifest.get("namespace"), str) and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", manifest["namespace"]), "invalid namespace")
        require(isinstance(manifest.get("run_id"), str) and re.fullmatch(rf"four-layout-r{round_no}-[0-9a-f]{{32}}", manifest["run_id"]), "invalid run ID")
        require(manifest["namespace"] not in seen_namespaces and manifest["run_id"] not in seen_run_ids,
                "duplicate namespace or run ID")
        seen_namespaces.add(manifest["namespace"])
        seen_run_ids.add(manifest["run_id"])
        _validate_runner(manifest.get("runner"))
        require(tuple(manifest.get("layout_order", ())) == ROUND_ORDERS[round_no - 1], "wrong layout order")
        require(manifest.get("gates") == dict.fromkeys(("truth", "analysis", "raw", "cleanup", "samples"), True), "run gate failed")
        require(set(manifest.get("artifacts", {})) == {f"result-{layout}.json" for layout in LAYOUTS}, "duplicate or missing layout")
        current_input, current_environment = manifest.get("input"), manifest.get("environment")
        _validate_input(current_input)
        _validate_environment(current_environment)
        require(shared_input is None or current_input == shared_input, "input or truth identity mismatch")
        require(shared_environment is None or current_environment == shared_environment, "environment identity mismatch")
        shared_input, shared_environment = current_input, current_environment
        for name, identity in sorted(manifest["artifacts"].items()):
            artifact = path.parent / name
            declared_artifacts.add(artifact.relative_to(root))
            require(artifact.is_file() and common.file_identity(artifact) == identity, "artifact identity mismatch")
            result = common.read_json(artifact, "layout result")
            layout = name.removeprefix("result-").removesuffix(".json")
            require(result.get("layout") == layout and result.get("round") == round_no and result.get("status") == "complete", "result identity mismatch")
            require(result.get("layout_order") == manifest["layout_order"], "result order mismatch")
            require(result.get("input") == current_input and result.get("environment") == current_environment, "result identity drift")
            for field in ("runner", "run_id", "namespace", "command", "cache_state", "measurements"):
                require(result.get(field) == manifest.get(field), f"result/manifest {field} mismatch")
            _validate_runner(result.get("runner"))
            require(isinstance(result.get("queries"), dict) and _sha(result["queries"].get("sha256")), "invalid queries identity")
            _validate_recovery(result.get("analysis_recovery"), "analysis_sha256_mismatches", "analysis")
            _validate_recovery(result.get("raw_recovery"), "raw_sha256_mismatches", "raw")
            _validate_cleanup(layout, result["namespace"], result.get("cleanup"))
            blocks = result.get("ingest", {}).get("blocks")
            require(isinstance(blocks, list) and len(blocks) == 190, "invalid block count")
            require([block.get("rows") for block in blocks] == [256] * 189 + [150], "invalid block rows")
            timing_fields = ({"analysis_copy_ms", "raw_copy_ms", "commit_ms", "block_wall_ms"}
                             if layout.startswith("og_") else
                             {"analytics_insert_ms", "raw_insert_ms", "client_row_build_ms",
                              "response_read_ms", "block_wall_ms"})
            require(all(all(type(block.get(field)) in (int, float) and math.isfinite(block[field])
                                and block[field] >= 0 for field in timing_fields) for block in blocks),
                    "invalid block timing")
            maintenance = result.get("maintenance", {})
            require(maintenance.get("analyzed") is True if layout.startswith("og_")
                    else maintenance.get("completed") is True, "maintenance failed")
            stage = result.get("query_stage", {})
            require(set(stage.get("plans", {})) == set(QUERY_IDS)
                    and all(isinstance(value, dict) and value for value in stage["plans"].values()),
                    "invalid query plans")
            samples, _ = _validate_samples(layout, stage, query_log_ids)
            _validate_storage(layout, result.get("storage"))
            layouts[layout].append({"round": round_no, "queries": {query: _round_stats(samples, query) for query in QUERY_IDS},
                                    "storage": result.get("storage"), "ddl": _normalized_ddl(result),
                                    "queries_identity": result.get("queries"), "runner": result.get("runner")})
            result_count += 1
    require(seen_rounds == {1, 2, 3, 4} and result_count == 16, "missing round or result")
    actual_artifacts = {path.relative_to(root) for path in root.rglob("result-*.json")
                        if "summary" not in path.relative_to(root).parts}
    require(actual_artifacts == declared_artifacts, "declared artifact set differs from result files")
    output_layouts = {}
    for layout in LAYOUTS:
        rounds = sorted(layouts[layout], key=lambda item: item["round"])
        require(len(rounds) == 4, "duplicate or missing layout")
        for field in ("ddl", "queries_identity", "runner"):
            require(all(item[field] == rounds[0][field] for item in rounds), f"{field} identity mismatch")
        output_layouts[layout] = {"rounds": rounds, "queries": {
            query: {percentile: _aggregate([item["queries"][query][percentile] for item in rounds])
                    for percentile in (("p50", "p95") if query == "S06" else ("p50", "p95", "p99"))}
            for query in QUERY_IDS}}
    return {"format": "agent-trace-json-storage-four-layout-summary", "format_version": 1,
            "result_count": result_count, "rounds": [1, 2, 3, 4], "input": shared_input,
            "environment": shared_environment, "statistics": {"within_round": "nearest-rank",
            "across_rounds": "median/min/max", "storage": "engine-specific bytes are not combined"},
            "layouts": output_layouts}


def render_tables(summary):
    """只根据最终 summary 渲染 Markdown 查询表。"""
    names = {"og_json": "openGauss JSON", "og_jsonb": "openGauss JSONB",
             "ch_string": "ClickHouse String JSON", "ch_native": "ClickHouse Native JSON"}
    lines = ["# 四种 JSON 存储结构汇总", "", "| 存储结构 | 查询 | p50 中位数 | p95 中位数 | p99 中位数 |", "|---|---:|---:|---:|---:|"]
    for layout in LAYOUTS:
        for query in QUERY_IDS:
            stats = summary["layouts"][layout]["queries"][query]
            lines.append(f"| {names[layout]} | {query} | {stats['p50']['median']} | {stats['p95']['median']} | {stats.get('p99', {}).get('median', '-')} |")
    return "\n".join(lines) + "\n"


def publish(summary, output):
    """原子写出确定性 JSON 和从该对象渲染的 Markdown。"""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    common.write_atomically(output / "summary.json", common.canonical_bytes(summary) + b"\n")
    common.write_atomically(output / "tables.md", render_tables(summary).encode("utf-8"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    publish(summarize(args.input), args.output)
