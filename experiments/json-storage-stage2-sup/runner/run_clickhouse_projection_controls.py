#!/usr/bin/env python3
"""在同一批数据上执行 ClickHouse S04 输出控制实验。"""

import argparse
import hashlib
import math
import statistics
import uuid
from collections import defaultdict
from pathlib import Path

from clickhouse_four_layout import (
    CLICKHOUSE_LAYOUTS,
    PROJECTION_CONTROL_MODES,
    ClickHouseFourLayoutAdapter,
)
from supplement_common import canonical_bytes, nearest_rank, write_failed_manifest, write_manifest_last


def control_schedule(measurements):
    """返回交替起始布局的 ABBA 采样顺序。"""
    if type(measurements) is not int or measurements <= 0 or measurements % 2:
        raise ValueError("measurements must be a positive even integer")
    schedule = []
    for pair in range(measurements // 2):
        schedule.extend(
            ("ch_string", "ch_native", "ch_native", "ch_string")
            if pair % 2 == 0 else
            ("ch_native", "ch_string", "ch_string", "ch_native")
        )
    return schedule


def _metric(values):
    """返回一组控制实验指标的中位数和 nearest-rank p95。"""
    if not values or any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("invalid projection control metric")
    return {"p50": statistics.median(values), "p95": nearest_rank(values, 95)}


def summarize_samples(samples):
    """按布局汇总客户端、服务端、读取量和响应体指标。"""
    groups = defaultdict(list)
    for sample in samples:
        groups[sample["layout"]].append(sample)
    result = {}
    for layout, selected in sorted(groups.items()):
        result[layout] = {
            "latency_ms": _metric([item["latency_ms"] for item in selected]),
            "query_duration_ms": _metric([item["query_log"]["query_duration_ms"] for item in selected]),
            "read_bytes": _metric([item["query_log"]["read_bytes"] for item in selected]),
            "recovery_ms": _metric([item["recovery_ms"] for item in selected]),
            "response_bytes": _metric([item["response_bytes"] for item in selected]),
            "response_read_ms": _metric([item["response_read_ms"] for item in selected]),
            "result_bytes": _metric([item["query_log"]["result_bytes"] for item in selected]),
            "samples": len(selected),
        }
    return result


def _sample(adapter, connection, layout, mode, parameters, expected, phase):
    """执行并核对一个 S04 控制样本。"""
    sample = adapter.execute_projection_control(connection, layout, mode, parameters)
    # Native JSON 的 toJSONString 文本长度可与 canonical 输入不同；值语义由
    # natural 和 uniform_text 两组完整结果校验，aggregate 只校验非空计数。
    matches = (sample["result"].get("non_null_count") == expected.get("non_null_count")
               if mode == "aggregate" else sample["result"] == expected)
    if not matches:
        raise RuntimeError(
            f"projection truth mismatch: {layout} {mode}; "
            f"actual={sample['result']!r}; expected={expected!r}"
        )
    return {**sample, "layout": layout, "matches_truth": True, "mode": mode, "phase": phase}


def execute(args):
    """载入两个 ClickHouse 布局，交替采样并在清理后发布结果。"""
    # 延迟加载避免纯汇总和单元测试依赖 openGauss 客户端。
    from run_four_layouts import load_inputs

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "run-manifest.json").unlink(missing_ok=True)
    manifest = {
        "format": "agent-trace-clickhouse-projection-controls",
        "format_version": 1,
        "gates": dict.fromkeys(("truth", "analysis", "raw", "cleanup", "samples"), False),
        "measurements_per_layout_mode": args.measurements,
        "namespace": args.namespace,
        "run_id": f"projection-controls-{uuid.uuid4().hex}",
    }
    artifacts = {}
    adapter = ClickHouseFourLayoutAdapter(
        args.host, args.clickhouse_port, args.clickhouse_container, args.namespace
    )
    owned = []
    cleanup = {}
    try:
        rows, source_truth, catalog, truth, identity = load_inputs(args.input, args.truth)
        expected = truth["results"]["S04"]
        manifest["input"] = identity
        manifest["database_version"] = adapter.database_version()
        blocks = [rows[index:index + source_truth["block_size"]]
                  for index in range(0, len(rows), source_truth["block_size"])]
        ingest = dict.fromkeys(CLICKHOUSE_LAYOUTS)
        for layout in CLICKHOUSE_LAYOUTS:
            adapter.create_layout(layout, 32)
            owned.append(layout)
            ingest[layout] = []
        for index, block in enumerate(blocks):
            order = CLICKHOUSE_LAYOUTS if index % 2 == 0 else tuple(reversed(CLICKHOUSE_LAYOUTS))
            for layout in order:
                ingest[layout].append(adapter.insert_block(layout, block))
        maintenance = {layout: adapter.finish_maintenance(layout, args.maintenance_timeout_seconds)
                       for layout in CLICKHOUSE_LAYOUTS}
        if not all(value.get("completed") for value in maintenance.values()):
            raise RuntimeError("ClickHouse maintenance did not complete")
        analysis = {layout: adapter.verify_analysis(layout, source_truth) for layout in CLICKHOUSE_LAYOUTS}
        raw = {layout: adapter.verify_raw(layout, source_truth) for layout in CLICKHOUSE_LAYOUTS}
        if not all(value.get("ok") for value in (*analysis.values(), *raw.values())):
            raise RuntimeError("loaded data recovery failed")

        connections = {layout: adapter.connect_worker() for layout in CLICKHOUSE_LAYOUTS}
        warmups, samples = [], []
        try:
            for mode in PROJECTION_CONTROL_MODES:
                for layout in CLICKHOUSE_LAYOUTS:
                    warmups.append(_sample(adapter, connections[layout], layout, mode,
                                           catalog["parameters"]["S04"], expected, "warmup"))
                for layout in control_schedule(args.measurements):
                    samples.append(_sample(adapter, connections[layout], layout, mode,
                                           catalog["parameters"]["S04"], expected, "measurement"))
        finally:
            for connection in connections.values():
                connection.close()
        query_logs = adapter.collect_query_logs(
            [sample["query_log_id"] for sample in warmups + samples]
        )
        for sample in warmups + samples:
            sample["query_log"] = query_logs[sample["query_log_id"]]
        summaries = {
            mode: summarize_samples([sample for sample in samples if sample["mode"] == mode])
            for mode in PROJECTION_CONTROL_MODES
        }
        result = {
            "analysis_recovery": analysis,
            "expected": expected,
            "ingest": ingest,
            "maintenance": maintenance,
            "samples": samples,
            "storage": {layout: adapter.collect_storage(layout) for layout in CLICKHOUSE_LAYOUTS},
            "summaries": summaries,
            "warmups": warmups,
        }
        manifest["gates"].update({"truth": True, "analysis": True, "raw": True, "samples": True})
    except Exception as error:
        write_failed_manifest(output, manifest, artifacts, error)
        raise
    finally:
        for layout in reversed(owned):
            try:
                cleanup[layout] = adapter.cleanup(layout)
            except Exception as error:
                cleanup[layout] = {"removed": False, "error": str(error)}

    if set(cleanup) != set(CLICKHOUSE_LAYOUTS) or not all(value.get("removed") for value in cleanup.values()):
        error = RuntimeError("projection control cleanup failed")
        write_failed_manifest(output, manifest, artifacts, error)
        raise error
    result["cleanup"] = cleanup
    result["raw_recovery"] = raw
    manifest["gates"]["cleanup"] = True
    content = canonical_bytes(result) + b"\n"
    artifacts["result.json"] = content
    manifest["result_sha256"] = hashlib.sha256(content).hexdigest()
    write_manifest_last(output, manifest, artifacts)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--clickhouse-container", default="agent-trace-clickhouse-25-12")
    parser.add_argument("--clickhouse-port", default=18123, type=int)
    parser.add_argument("--measurements", default=40, type=int)
    parser.add_argument("--maintenance-timeout-seconds", default=120, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    execute(parse_args())
