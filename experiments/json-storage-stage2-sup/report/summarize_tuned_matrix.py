#!/usr/bin/env python3
"""确定性汇总阶段二调优与基础矩阵结果。"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


QUERY_NAMES = {
    "S01": "ordinary_column_grouping_control",
    "S02": "dense_json_path_filter_grouping",
    "S03": "sparse_json_path_filter",
    "S04": "large_json_path_projection",
    "S05": "complete_trace_read",
    "S06": "complete_document_page",
    "S07": "numeric_json_path_aggregation",
}


def _median(values):
    """返回一组数值的中位数。"""
    if not values:
        raise ValueError("cannot summarize an empty value set")
    return statistics.median(values)


def _storage_bytes(result):
    """返回分析表与原文表当前物理占用。"""
    storage = result["storage"]
    if result["engine"] == "opengauss":
        analytics = int(storage["analytics"]["total_bytes"])
        raw = int(storage["raw"]["total_bytes"])
    else:
        analytics = int(storage["tables"]["analytics"]["compressed_bytes"])
        raw = int(storage["tables"]["raw"]["compressed_bytes"])
    return {"analysis_bytes": analytics, "raw_bytes": raw, "solution_bytes": analytics + raw}


def _maintenance_ms(result):
    """把不同引擎的查询就绪维护时间统一为毫秒。"""
    maintenance = result["maintenance"]
    if result["engine"] == "opengauss":
        analyze_ms = float(maintenance["analyze_ms"])
        return {"analyze_ms": analyze_ms, "wall_ms": analyze_ms}
    before_wait_ms = float(maintenance["before_optimize"]["waited_seconds"]) * 1000
    optimize_final_ms = sum(float(value) for value in maintenance["optimize_final_ms"].values())
    after_wait_ms = float(maintenance["waited_seconds"]) * 1000
    return {
        "after_optimize_wait_ms": after_wait_ms,
        "before_optimize_wait_ms": before_wait_ms,
        "optimize_final_ms": optimize_final_ms,
        "wall_ms": before_wait_ms + optimize_final_ms + after_wait_ms,
    }


def _round_summary(result):
    """先在单轮内部汇总写入、空间与查询样本。"""
    blocks = result["ingest"]["blocks"]
    rows = sum(int(block["rows"]) for block in blocks)
    wall_ms = sum(float(block["block_wall_ms"]) for block in blocks)
    maintenance = _maintenance_ms(result)
    samples = defaultdict(list)
    recovery = defaultdict(list)
    read_rows = defaultdict(list)
    read_bytes = defaultdict(list)
    application_ready = defaultdict(list)
    for sample in result["query_stage"]["samples"]:
        if sample.get("phase") != "measurement":
            continue
        query_id = sample["query_id"]
        samples[query_id].append(float(sample["latency_ms"]))
        recovery[query_id].append(float(sample["recovery_ms"]))
        application_ready[query_id].append(float(sample["latency_ms"]) + float(sample["recovery_ms"]))
        if "query_log" in sample:
            read_rows[query_id].append(int(sample["query_log"]["read_rows"]))
            read_bytes[query_id].append(int(sample["query_log"]["read_bytes"]))
    gin_samples = result.get("access_evidence", {}).get("gin_samples", [])
    gin_latency = [float(sample["latency_ms"]) for sample in gin_samples if sample.get("phase") == "measurement"]
    dense_samples = result.get("access_evidence", {}).get("dense_forced_samples", [])
    dense_latency = [float(sample["latency_ms"]) for sample in dense_samples if sample.get("phase") == "measurement"]
    special_latency = {}
    special_access = {}
    if gin_latency:
        special_latency["sparse_json_containment_gin"] = _median(gin_latency)
    if dense_latency:
        special_latency["dense_json_expression_btree_forced"] = _median(dense_latency)
        evidence = result["access_evidence"]
        expected_scans = int(evidence["dense_forced_expected_index_scans"])
        scan_delta = int(evidence["dense_forced_idx_scan_delta"])
        special_access["dense_json_expression_btree_forced"] = {
            "expected_index_scans": expected_scans,
            "idx_scan_delta": scan_delta,
            "plan_uses_index": "analytics_operation_name_idx" in evidence["dense_forced_plan"],
        }
    for name, probe in result.get("access_evidence", {}).get("selectivity_probes", {}).items():
        for mode in ("natural", "forced"):
            mode_result = probe.get(mode, {})
            probe_latency = [
                float(sample["latency_ms"])
                for sample in mode_result.get("samples", [])
                if sample.get("phase") == "measurement"
            ]
            if probe_latency:
                key = f"selectivity_{name}_{mode}"
                special_latency[key] = _median(probe_latency)
                expected_scans = int(mode_result.get("expected_index_scans", 0))
                scan_delta = int(mode_result.get("idx_scan_delta", 0))
                special_access[key] = {
                    "expected_index_scans": expected_scans,
                    "idx_scan_delta": scan_delta,
                    "plan_uses_index": expected_scans > 0 and scan_delta >= expected_scans,
                }
    return {
        "application_ready_ms": {query_id: _median(values) for query_id, values in application_ready.items()},
        "ingest": {"rows": rows, "wall_ms": wall_ms, "rows_per_second": rows * 1000 / wall_ms},
        "maintenance": maintenance,
        "query_ready": {
            "wall_ms": wall_ms + maintenance["wall_ms"],
            "rows_per_second": rows * 1000 / (wall_ms + maintenance["wall_ms"]),
        },
        "query_latency_ms": {query_id: _median(values) for query_id, values in samples.items()},
        "recovery_ms": {query_id: _median(values) for query_id, values in recovery.items()},
        "read_rows": {query_id: _median(values) for query_id, values in read_rows.items()},
        "read_bytes": {query_id: _median(values) for query_id, values in read_bytes.items()},
        "storage": _storage_bytes(result),
        "special_access": special_access,
        "special_latency_ms": special_latency,
    }


def summarize_results(results):
    """按目标先算轮内中位数，再算轮间中位数。"""
    grouped = defaultdict(list)
    for result in results:
        if result.get("status") != "complete" or result.get("cleanup", {}).get("removed") is not True:
            raise ValueError(f"incomplete result: {result.get('target')}")
        if result.get("access_gate", {}).get("ok") is not True:
            raise ValueError(f"access gate failed: {result.get('target')}")
        grouped[result["target"]].append((result, _round_summary(result)))
    targets = {}
    for target, rounds in sorted(grouped.items()):
        raw = [item[0] for item in rounds]
        values = [item[1] for item in rounds]
        query_ids = sorted(set().union(*(item["query_latency_ms"] for item in values)))
        targets[target] = {
            "engine": raw[0]["engine"],
            "profile": raw[0]["profile"],
            "rounds": len(rounds),
            "ingest": {
                "rows_per_second": _median([item["ingest"]["rows_per_second"] for item in values]),
                "wall_ms": _median([item["ingest"]["wall_ms"] for item in values]),
            },
            "maintenance": {
                key: _median([item["maintenance"][key] for item in values])
                for key in sorted(set().union(*(item["maintenance"] for item in values)))
            },
            "query_ready": {
                "rows_per_second": _median([item["query_ready"]["rows_per_second"] for item in values]),
                "wall_ms": _median([item["query_ready"]["wall_ms"] for item in values]),
            },
            "query_latency_ms": {
                query_id: {"name": QUERY_NAMES[query_id], "p50": _median([item["query_latency_ms"][query_id] for item in values])}
                for query_id in query_ids
            },
            "application_ready_ms": {
                query_id: {"p50": _median([item["application_ready_ms"][query_id] for item in values])}
                for query_id in query_ids
            },
            "recovery_ms": {
                query_id: {"p50": _median([item["recovery_ms"][query_id] for item in values])}
                for query_id in query_ids
            },
            "read_rows": {
                query_id: {"p50": _median([item["read_rows"][query_id] for item in values if query_id in item["read_rows"]])}
                for query_id in query_ids if any(query_id in item["read_rows"] for item in values)
            },
            "read_bytes": {
                query_id: {"p50": _median([item["read_bytes"][query_id] for item in values if query_id in item["read_bytes"]])}
                for query_id in query_ids if any(query_id in item["read_bytes"] for item in values)
            },
            "storage": {
                key: _median([item["storage"][key] for item in values])
                for key in ("analysis_bytes", "raw_bytes", "solution_bytes")
            },
            "special_latency_ms": {
                name: {"p50": _median([item["special_latency_ms"][name] for item in values if name in item["special_latency_ms"]])}
                for name in sorted(set().union(*(item["special_latency_ms"] for item in values)))
            },
            "special_access": {
                name: {
                    "all_plans_use_index": all(
                        item["special_access"][name]["plan_uses_index"]
                        for item in values if name in item["special_access"]
                    ),
                    "expected_index_scans": _median([
                        item["special_access"][name]["expected_index_scans"]
                        for item in values if name in item["special_access"]
                    ]),
                    "idx_scan_delta": _median([
                        item["special_access"][name]["idx_scan_delta"]
                        for item in values if name in item["special_access"]
                    ]),
                }
                for name in sorted(set().union(*(item["special_access"] for item in values)))
            },
        }
    return {"format": "agent-trace-json-storage-tuned-matrix-summary", "format_version": 1, "targets": targets}


def load_results(inputs, selected_targets=None):
    """读取完成的轮次目录，并可筛选进入公开汇总的目标。"""
    results = []
    sources = []
    found_targets = set()
    for directory in inputs:
        manifest_path = Path(directory) / "run-manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("status") != "complete":
            raise ValueError(f"incomplete run: {directory}")
        sources.append(str(directory))
        for target in manifest["targets"]:
            if selected_targets is not None and target not in selected_targets:
                continue
            results.append(json.loads((Path(directory) / f"result-{target}.json").read_bytes()))
            found_targets.add(target)
    if selected_targets is not None and found_targets != selected_targets:
        missing = sorted(selected_targets - found_targets)
        raise ValueError(f"selected targets not found: {missing}")
    return results, sources


def main():
    """汇总指定轮次并写入格式稳定的 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--target", action="append", dest="targets")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    selected_targets = set(args.targets) if args.targets else None
    results, sources = load_results(args.input, selected_targets=selected_targets)
    summary = summarize_results(results)
    summary["sources"] = sources
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
