"""验证六轮正式产物，确定性汇总 residual 横向实验。"""

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from common import (COMPARABILITY_CONTRACT, COMPARABILITY_CONTRACT_VERSION,
                    DATA_PATH, canonical_bytes, file_identity, nearest_rank,
                    read_json, summarize_samples)


LAYOUTS = {"opengauss": {"og_jsonb", "og_jsonb_hot", "og_jsonb_gin"},
           "clickhouse": {"ch_string", "ch_map", "ch_native"}}
QUERIES = {"Q01", "Q02", "Q03", "Q04", "Q05"}
LOG_METRICS = {"memory_usage", "query_duration_ms", "read_bytes", "read_rows",
               "result_bytes", "result_rows", "selected_bytes", "selected_rows"}
WATERMARKS = list(range(256, 48534, 256)) + [48534]


def require(condition, message):
    """验证必要条件，失败时停止发布。"""
    if not condition:
        raise ValueError(message)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def normalized_ddl_identity(result, engine):
    """校验 DDL 原文摘要和命名空间，返回可跨轮比较的规范化身份。"""
    record = result.get("ddl")
    require(isinstance(record, dict), "missing DDL identity")
    identity = record.get("identity")
    object_type = "schema" if engine == "opengauss" else "database"
    require(isinstance(identity, dict) and set(identity) == {object_type, "ddl"}, "invalid DDL identity structure")
    namespace, ddl = identity[object_type], identity["ddl"]
    require(isinstance(namespace, str) and re.fullmatch(r"[a-z][a-z0-9_]*", namespace), "invalid DDL namespace")
    require(isinstance(ddl, str) and ddl, "invalid DDL text")
    require(record.get("sha256") == hashlib.sha256(ddl.encode("utf-8")).hexdigest(), "DDL hash mismatch")
    declaration = f"CREATE {object_type.upper()} {namespace};"
    require(ddl.startswith(declaration) and re.search(rf"(?<![A-Za-z0-9_]){re.escape(namespace)}\.", ddl),
            "DDL namespace does not match declared objects")
    # 替换对象声明和限定名，列类型、索引与存储设置继续参与跨轮比较。
    normalized = f"CREATE {object_type.upper()} __namespace__;" + ddl[len(declaration):]
    normalized = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(namespace)}(?=\.)", "__namespace__", normalized)
    return {"normalized_ddl": normalized,
            "normalized_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest()}


def aggregate(values):
    """递归计算三轮数值的 median/min/max，保留同值状态。"""
    first = values[0]
    if isinstance(first, dict):
        return {key: aggregate([value[key] for value in values]) for key in sorted(first)}
    if type(first) in (int, float):
        return {"median": statistics.median(values), "min": min(values), "max": max(values)}
    return first if all(value == first for value in values) else values


def query_summary(samples, engine, phase):
    """验证阶段样本和日志，按查询重算延迟、速率与读放大指标。"""
    grouped = defaultdict(list)
    for sample in samples:
        require(sample.get("ok") is True and sample.get("matches_truth") is True,
                "failed sample or truth")
        require(sample.get("phase") == phase, "sample phase mismatch")
        watermark = sample.get("watermark")
        require(watermark == 48534 if phase == "static" else watermark in WATERMARKS[4:],
                "sample watermark mismatch")
        if engine == "clickhouse":
            log = sample.get("query_log", {})
            require(set(log) == LOG_METRICS and all(type(value) is int and value >= 0 for value in log.values()),
                    "invalid query_log metrics")
        grouped[sample.get("query_id")].append(sample)
    require(set(grouped) == (QUERIES if phase == "static" else QUERIES - {"Q04"}), "sample query catalog mismatch")
    summaries = {}
    for query, rows in sorted(grouped.items()):
        require(phase != "static" or len(rows) == 100, "static sample count mismatch")
        summary = summarize_samples(rows)
        summary["watermark"] = {"min": min(row["watermark"] for row in rows),
                                "max": max(row["watermark"] for row in rows)}
        if engine == "clickhouse":
            summary["query_log"] = {
                metric: {"p50": nearest_rank([row["query_log"][metric] for row in rows], 50),
                         "p95": nearest_rank([row["query_log"][metric] for row in rows], 95),
                         "max": max(row["query_log"][metric] for row in rows)}
                for metric in sorted(LOG_METRICS)}
        summaries[query] = summary
    return summaries


def summarize_result(result, manifest):
    """从单布局原始 block、样本与空间事实生成一轮派生值。"""
    require(result.get("status") == "complete", "incomplete result")
    require(result.get("runner") == manifest["runner"], "result runner identity mismatch")
    for field, mismatch in [("analysis_correctness", "analysis_sha256_mismatches"),
                            ("raw_recovery", "raw_sha256_mismatches")]:
        check = result[field]
        require(check.get("ok") is True and check.get("actual_count") == 48534
                and check.get("expected_count") == 48534 and check.get("duplicate_count") == 0
                and all(check.get(key) == [] for key in ["duplicates", "extra", "missing", mismatch]),
                f"invalid {field}")
    require(result["cleanup"].get("removed") is True, "cleanup failed")
    ingest = result["ingest"]
    require(ingest.get("query_workers") == 2, "query worker mismatch")
    blocks = ingest["blocks"]
    require(len(blocks) == 190 and [block["watermark"] for block in blocks] == WATERMARKS,
            "block count or watermark mismatch")
    previous = 0
    for block in blocks:
        require(block["rows"] == block["watermark"] - previous
                and block.get("visible_after_commit") is True
                and positive(block["wall_time_ms"]) and positive(block["input_bytes"]), "invalid block")
        previous = block["watermark"]
    seconds = sum(block["wall_time_ms"] for block in blocks) / 1000
    block_stats = summarize_samples([{"ok": True, "latency_ms": block["wall_time_ms"]} for block in blocks])
    engine = manifest["engine"]
    storage = result["storage"]
    maintenance = result["maintenance"]
    if engine == "opengauss":
        require(maintenance.get("analyzed") is True, "maintenance failed")
        sizes = {name: storage[name]["total_bytes"] for name in ["analytics", "raw"]}
        maintenance_stats = {"completed": True, "waited_seconds": None}
    else:
        require(maintenance.get("completed") is True and maintenance.get("timed_out") is False
                and storage.get("merge_backlog") == 0, "maintenance failed")
        require(all(storage["tables"][name]["rows"] == 48534 for name in ["analytics", "raw"]), "storage row mismatch")
        sizes = {name: storage["tables"][name]["compressed_bytes"] for name in ["analytics", "raw"]}
        maintenance_stats = {"completed": True, "waited_seconds": maintenance["waited_seconds"]}
    require(all(type(value) is int and value >= 0 for value in sizes.values()), "invalid storage")
    sizes["total"] = sum(sizes.values())
    require(result["static_queries"]["watermark"] == 48534, "static watermark mismatch")
    derived = {
        "ingest": {"rows": 48534, "blocks": 190, "seconds": seconds,
                   "input_bytes": sum(block["input_bytes"] for block in blocks),
                   "rows_per_second": 48534 / seconds,
                   "mib_per_second": sum(block["input_bytes"] for block in blocks) / 1048576 / seconds,
                   "block_latency_ms": block_stats["latency_ms"]},
        "concurrent": query_summary(ingest["samples"], engine, "concurrent"),
        "static": query_summary(result["static_queries"]["samples"], engine, "static"),
        "space_bytes": sizes, "maintenance": maintenance_stats,
        "correctness": {"analysis": True, "raw_recovery": True, "cleanup": True}}
    if result["layout"] == "ch_native":
        paths = storage["paths"]
        derived["native_paths"] = {"dynamic": len(paths["dynamic_paths"]), "shared": len(paths["shared_paths"])}
    return derived


def summarize(root):
    """验证一个完整正式根，返回包含 provenance 与三轮统计的 JSON 对象。"""
    root = Path(root)
    manifests = sorted(root.glob("*/run-manifest.json"))
    require(len(manifests) == 6, "expected six complete engine/round runs")
    runs, artifacts, layouts, identities, engine_identities, query_identities = [], [], defaultdict(list), [], {}, {}
    input_blocks = None
    ddl_identities = {}
    declared_artifacts = set()
    seen_rounds, seen_ids = set(), set()
    for path in manifests:
        manifest = read_json(path, "run manifest")
        require(manifest.get("status") == "complete", "incomplete run")
        require(manifest.get("gates") == dict.fromkeys(["correctness", "raw_recovery", "cleanup", "all_samples_successful"], True), "incomplete run gates")
        engine, round_no, run_id = manifest["engine"], manifest["round"], manifest["run_id"]
        require(engine in LAYOUTS and round_no in (1, 2, 3) and (engine, round_no) not in seen_rounds, "duplicate or invalid engine/round")
        require(isinstance(run_id, str) and run_id and run_id not in seen_ids, "duplicate run ID")
        seen_rounds.add((engine, round_no))
        seen_ids.add(run_id)
        require(set(manifest["layout_order"]) == LAYOUTS[engine] and len(manifest["layout_order"]) == 3
                and set(manifest["artifacts"]) == {f"{layout}/result.json" for layout in LAYOUTS[engine]}, "layout set mismatch")
        require(manifest["comparability_contract_version"] == COMPARABILITY_CONTRACT_VERSION
                and manifest["comparability_contract"] == COMPARABILITY_CONTRACT
                and manifest["data_path"] == DATA_PATH
                and manifest["cache_state"] == "query_warmup_1_no_os_cache_drop", "contract identity mismatch")
        source = manifest["input"]
        require(all(source.get(key) == value for key, value in {"record_count": 48534, "block_count": 190, "block_size": 256, "seed": 42, "watermarks": WATERMARKS}.items()), "input identity mismatch")
        require(all(isinstance(source.get(key), dict) and source[key].get("sha256")
                    for key in ["source_input", "upstream_manifest", "audit", "dataset", "truth"]), "missing input identity")
        environment = manifest["environment"]
        require(all(isinstance(environment.get(key), dict) for key in ["repositories", "host", "container"]),
                "missing environment identity")
        identity = {key: manifest[key] for key in ["input", "runner", "cache_state", "comparability_contract_version", "comparability_contract", "data_path"]}
        identity["repositories"] = environment["repositories"]
        identity["host"] = {key: environment["host"][key] for key in ["cpu_count", "memory_bytes", "disk_total_bytes"]}
        require(not identities or identity == identities[0], "cross-run identity mismatch")
        identities.append(identity)
        container = environment["container"]
        require(container.get("running") is True and container.get("image_id") and container.get("repo_digests") and container.get("database_version"), "missing container identity")
        require(engine not in engine_identities or container == engine_identities[engine], "container identity mismatch")
        engine_identities[engine] = container
        runs.append({"run_id": run_id, "engine": engine, "round": round_no, "layout_order": manifest["layout_order"],
                     "manifest": str(path.relative_to(root)), **file_identity(path)})
        for name, expected in sorted(manifest["artifacts"].items()):
            artifact = path.parent / name
            declared_artifacts.add(artifact.relative_to(root))
            require(artifact.is_file(), f"missing artifact: {artifact}")
            require(file_identity(artifact) == expected, f"artifact hash mismatch: {artifact}")
            result = read_json(artifact, "layout result")
            layout = name.split("/")[0]
            require(result.get("layout") == layout, "result layout mismatch")
            ddl_identity = normalized_ddl_identity(result, engine)
            require(layout not in ddl_identities or ddl_identity == ddl_identities[layout], "cross-round DDL identity mismatch")
            ddl_identities[layout] = ddl_identity
            require(result.get("queries", {}).get("sha256"), "missing query identity")
            require(layout not in query_identities or result["queries"] == query_identities[layout], "query identity mismatch")
            query_identities[layout] = result["queries"]
            block_identity = [{key: block[key] for key in ["rows", "watermark", "input_bytes"]}
                              for block in result["ingest"]["blocks"]]
            require(input_blocks is None or block_identity == input_blocks, "block input identity mismatch")
            input_blocks = block_identity
            try:
                derived = summarize_result(result, manifest)
            except (KeyError, TypeError) as error:
                raise ValueError(f"incomplete result: {artifact}") from error
            layouts[layout].append({"round": round_no, "run_id": run_id, "metrics": derived})
            artifacts.append({"path": str(artifact.relative_to(root)), "run_id": run_id, "layout": layout, **expected})
    require(seen_rounds == {(engine, round_no) for engine in LAYOUTS for round_no in (1, 2, 3)}, "missing engine/round")
    actual_artifacts = {path.relative_to(root) for path in root.rglob("result.json")
                        if path.relative_to(root).parts[0] != "summary"}
    require(actual_artifacts == declared_artifacts, "declared artifact set differs from result files")
    return {"format_version": 1, "source_root": str(root), "identity": identities[0],
            "containers": engine_identities, "query_identities": query_identities, "ddl_identities": ddl_identities,
            "runs": runs, "artifacts": artifacts,
            "statistics": {"percentile": "nearest-rank within each round", "three_rounds": "median/min/max of three per-round derived values", "space": {"opengauss": "pg_total_relation_size allocated heap/TOAST/index bytes", "clickhouse": "active part compressed bytes"}},
            "layouts": {layout: {"rounds": 3, "per_round": sorted(values, key=lambda value: value["round"]),
                                 "aggregate": aggregate([value["metrics"] for value in values])}
                        for layout, values in sorted(layouts.items())}}


def publish(summary, output):
    """写入确定性 JSON 和供检查的完整 Markdown 汇总。"""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    preview = "# 阶段二机器汇总\n\n统计定义、六轮 manifest、十八个 result 身份与全部派生值：\n\n```json\n"
    (output / "summary.md").write_text(preview + json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n```\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    publish(summarize(args.input), args.output)
