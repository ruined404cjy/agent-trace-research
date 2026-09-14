#!/usr/bin/env python3
"""汇总 ClickHouse merge 性能探针的三轮结果。"""

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path


def _median(values):
    """返回非空指标集合的中位数。"""
    if not values:
        raise ValueError("cannot summarize empty values")
    return statistics.median(values)


def _stage(stage):
    """汇总一个 part 状态下的正式查询样本。"""
    samples = defaultdict(lambda: defaultdict(list))
    for sample in stage["query_stage"]["samples"]:
        if sample.get("phase") != "measurement":
            continue
        query_id = sample["query_id"]
        samples[query_id]["latency_ms"].append(float(sample["latency_ms"]))
        samples[query_id]["recovery_ms"].append(float(sample["recovery_ms"]))
        samples[query_id]["read_rows"].append(int(sample["query_log"]["read_rows"]))
        samples[query_id]["read_bytes"].append(int(sample["query_log"]["read_bytes"]))
    return {
        "max_active_merges": int(stage.get("merge_observer", {}).get("max_active_merges", 0)),
        "part_count_before": {
            table: int(stage.get("storage_before", stage["storage"])["tables"][table]["part_count"])
            for table in ("analytics", "raw")
        },
        "part_count": {
            table: int(stage["storage"]["tables"][table]["part_count"])
            for table in ("analytics", "raw")
        },
        "query_latency_ms": {
            query_id: _median(values["latency_ms"])
            for query_id, values in samples.items()
        },
        "recovery_ms": {
            query_id: _median(values["recovery_ms"])
            for query_id, values in samples.items()
        },
        "read_rows": {
            query_id: _median(values["read_rows"])
            for query_id, values in samples.items()
        },
        "read_bytes": {
            query_id: _median(values["read_bytes"])
            for query_id, values in samples.items()
        },
    }


def _round(result):
    """先在单轮目标内部汇总载入、维护与查询阶段。"""
    blocks = result["ingest"]["blocks"]
    rows = sum(int(block["rows"]) for block in blocks)
    wall_ms = sum(float(block["block_wall_ms"]) for block in blocks)
    return {
        "ingest": {
            "max_active_merges": int(result.get("ingest_merge_observer", {}).get("max_active_merges", 0)),
            "rows_per_second": rows * 1000 / wall_ms,
            "wall_ms": wall_ms,
        },
        "maintenance": {
            "background_wait_ms": float(result["maintenance"]["background_wait_ms"]),
            "optimize_final_ms": float(result["maintenance"]["optimize_final_ms"]),
        },
        "stages": {name: _stage(stage) for name, stage in result["stages"].items()},
    }


def summarize_results(results):
    """按目标汇总轮内中位数，再计算三轮中位数。"""
    grouped = defaultdict(list)
    for result in results:
        if result.get("status") != "complete":
            raise ValueError(f"incomplete target: {result.get('target')}")
        if result.get("cleanup", {}).get("removed") is not True:
            raise ValueError(f"cleanup failed: {result.get('target')}")
        grouped[result["target"]].append((result, _round(result)))
    targets = {}
    for target, rounds in sorted(grouped.items()):
        raw = [item[0] for item in rounds]
        values = [item[1] for item in rounds]
        stage_names = sorted(set().union(*(item["stages"] for item in values)))
        targets[target] = {
            "layout": raw[0]["layout"],
            "mode": raw[0]["mode"],
            "rounds": len(rounds),
            "ingest": {
                key: _median([item["ingest"][key] for item in values])
                for key in ("max_active_merges", "rows_per_second", "wall_ms")
            },
            "maintenance": {
                key: _median([item["maintenance"][key] for item in values])
                for key in ("background_wait_ms", "optimize_final_ms")
            },
            "stages": {},
        }
        for stage_name in stage_names:
            selected = [item["stages"][stage_name] for item in values if stage_name in item["stages"]]
            query_ids = sorted(set().union(*(item["query_latency_ms"] for item in selected)))
            targets[target]["stages"][stage_name] = {
                "part_count": {
                    table: _median([item["part_count"][table] for item in selected])
                    for table in ("analytics", "raw")
                },
                "part_count_before": {
                    table: _median([item["part_count_before"][table] for item in selected])
                    for table in ("analytics", "raw")
                },
                "max_active_merges": _median([item["max_active_merges"] for item in selected]),
                **{
                    metric: {
                        query_id: {"p50": _median([item[metric][query_id] for item in selected])}
                        for query_id in query_ids
                    }
                    for metric in ("query_latency_ms", "recovery_ms", "read_rows", "read_bytes")
                },
            }
    return {
        "format": "agent-trace-clickhouse-merge-performance-summary",
        "format_version": 1,
        "targets": targets,
    }


def main():
    """读取完成轮次，汇总并写出确定性 JSON。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    results = []
    sources = []
    for directory in args.input:
        manifest = json.loads((directory / "run-manifest.json").read_bytes())
        if manifest.get("status") != "complete":
            raise ValueError(f"incomplete run: {directory}")
        sources.append(str(directory))
        for target in manifest["targets"]:
            results.append(json.loads((directory / f"result-{target}.json").read_bytes()))
    result = summarize_results(results)
    result["sources"] = sources
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
