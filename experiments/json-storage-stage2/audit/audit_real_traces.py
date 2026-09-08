#!/usr/bin/env python3
"""审计真实 Trace JSONL 的 Attribute 分布。"""

import argparse
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


FORMAT_VERSION = 1
DATA_PATH = "independent_loader"
EXPECTED_INPUT_SHA256 = "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683"
EXPECTED_UPSTREAM_MANIFEST_SHA256 = "f46bbe843c5578faea9ddfb5e8eb3aac8b6dc4c2f4fb89beabab503043505e38"
PAYLOAD_PATHS = (
    "raw_event",
    "attributes",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
)


def canonical_bytes(value):
    """返回对象键排序且保留数组顺序的 JSON UTF-8 bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def json_type(value):
    """返回 JSON 值的规范类型名称。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def nearest_rank(values, percentile):
    """返回 values 的 nearest-rank 分位数整数值。"""
    if not values:
        raise ValueError("values must not be empty")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * percentile / 100) - 1]


def percentile_summary(values):
    """返回整数值的 p50、p95、p99 和最大值。"""
    if not values:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    return {
        "p50": nearest_rank(values, 50),
        "p95": nearest_rank(values, 95),
        "p99": nearest_rank(values, 99),
        "max": max(values),
    }


def parse_start_time(value, line_number):
    """解析 ISO 8601 时间并统一转换为 UTC。"""
    if not isinstance(value, str):
        raise ValueError(f"line {line_number}: start_time must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"line {line_number}: invalid start_time") from error
    if parsed.tzinfo is None:
        raise ValueError(f"line {line_number}: start_time must include timezone")
    return parsed.astimezone(timezone.utc)


def epoch_start(value, window_minutes):
    """返回 value 所在左闭右开窗口的 UTC 起点。"""
    seconds = window_minutes * 60
    timestamp = value.timestamp()
    return int(math.floor(timestamp / seconds) * seconds)


def iso_utc(timestamp):
    """把 UTC epoch 秒转为稳定的 ISO 8601 字符串。"""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def new_path_state():
    """创建单个 Attribute 路径的流式统计状态。"""
    return {"present_rows": 0, "values": set(), "types": Counter(), "lengths": []}


def update_path_state(state, value):
    """将一个 Attribute 值计入路径统计状态。"""
    encoded = canonical_bytes(value)
    state["present_rows"] += 1
    state["values"].add(encoded)
    state["types"][json_type(value)] += 1
    state["lengths"].append(len(encoded))


def render_path_state(state, row_count):
    """把路径统计状态转换为可写入审计文件的字典。"""
    type_counts = dict(sorted(state["types"].items()))
    present_rows = state["present_rows"]
    distinct_values = len(state["values"])
    return {
        "density": present_rows / row_count if row_count else 0,
        "distinct_ratio": distinct_values / present_rows if present_rows else 0,
        "distinct_values": distinct_values,
        "length": percentile_summary(state["lengths"]),
        "present_rows": present_rows,
        "type_conflict_ratio": (
            (present_rows - max(type_counts.values())) / present_rows if present_rows else 0
        ),
        "types": type_counts,
    }


def add_payload(payloads, name, value):
    """记录一个存在的 payload 的 canonical 字节长度。"""
    payloads[name].append(len(canonical_bytes(value)))


def dimension_value(record, attributes, name):
    """返回指定维度的原始分组值。"""
    if name == "schema_version":
        return record["schema_version"]
    return attributes.get(name, "")


def render_dimension(groups):
    """返回一个维度下各分组的行数、P、W 和路径密度。"""
    rendered = {}
    for value, state in sorted(groups.items()):
        row_count = state["row_count"]
        rendered[value] = {
            "path_count": len(state["paths"]),
            "path_density": {
                path: count / row_count for path, count in sorted(state["path_rows"].items())
            },
            "row_count": row_count,
            "width": percentile_summary(state["widths"]),
        }
    return rendered


def audit_file(path, window_minutes):
    """流式审计 JSONL 文件，返回全局、路径、窗口和维度统计。"""
    if window_minutes <= 0:
        raise ValueError("window_minutes must be positive")

    path_states = defaultdict(new_path_state)
    epoch_states = {}
    dimension_names = ("source_dataset", "framework", "span.type", "schema_version")
    dimensions = {name: {} for name in dimension_names}
    payloads = {name: [] for name in PAYLOAD_PATHS}
    widths = []
    trace_ids = set()
    row_count = 0

    with Path(path).open("rb") as input_file:
        for line_number, raw_line in enumerate(input_file, start=1):
            if not raw_line.strip():
                raise ValueError(f"line {line_number}: empty JSONL row")
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {line_number}: invalid JSON") from error
            required = ("trace_id", "span_id", "start_time", "attributes", "schema_version")
            missing = [name for name in required if name not in record]
            if missing:
                raise ValueError(f"line {line_number}: missing {', '.join(missing)}")
            attributes = record["attributes"]
            if not isinstance(attributes, dict):
                raise ValueError(f"line {line_number}: attributes must be an object")

            timestamp = parse_start_time(record["start_time"], line_number)
            bucket = epoch_start(timestamp, window_minutes)
            epoch = epoch_states.setdefault(
                bucket,
                {"path_types": defaultdict(set), "paths": set(), "row_count": 0, "widths": []},
            )
            row_count += 1
            trace_ids.add(record["trace_id"])
            widths.append(len(attributes))
            epoch["row_count"] += 1
            epoch["widths"].append(len(attributes))

            payloads["raw_event"].append(len(raw_line.rstrip(b"\r\n")))
            add_payload(payloads, "attributes", attributes)
            for payload_name in PAYLOAD_PATHS[2:]:
                if payload_name in attributes:
                    add_payload(payloads, payload_name, attributes[payload_name])

            for attribute_path, value in attributes.items():
                update_path_state(path_states[attribute_path], value)
                epoch["paths"].add(attribute_path)
                epoch["path_types"][attribute_path].add(json_type(value))

            for name in dimension_names:
                value = dimension_value(record, attributes, name)
                group_key = str(value)
                state = dimensions[name].setdefault(
                    group_key,
                    {"path_rows": Counter(), "paths": set(), "row_count": 0, "widths": []},
                )
                state["row_count"] += 1
                state["widths"].append(len(attributes))
                state["paths"].update(attributes)
                state["path_rows"].update(attributes.keys())

    epochs = []
    previous_paths = set()
    previous_types = {}
    for bucket, state in sorted(epoch_states.items()):
        current_paths = state["paths"]
        current_types = state["path_types"]
        type_changes = []
        for attribute_path in sorted(current_paths & previous_paths):
            added_types = sorted(current_types[attribute_path] - previous_types[attribute_path])
            removed_types = sorted(previous_types[attribute_path] - current_types[attribute_path])
            if added_types or removed_types:
                type_changes.append(
                    {
                        "added_types": added_types,
                        "path": attribute_path,
                        "removed_types": removed_types,
                    }
                )
        epochs.append(
            {
                "added_paths": sorted(current_paths - previous_paths),
                "end_time": iso_utc(bucket + window_minutes * 60),
                "path_count": len(current_paths),
                "removed_paths": sorted(previous_paths - current_paths),
                "row_count": state["row_count"],
                "stable_paths": sorted(current_paths & previous_paths),
                "start_time": iso_utc(bucket),
                "type_changes": type_changes,
                "width": percentile_summary(state["widths"]),
            }
        )
        previous_paths = current_paths
        previous_types = current_types

    return {
        "data_path": DATA_PATH,
        "dimensions": {
            "framework": render_dimension(dimensions["framework"]),
            "instrumentation_scope": {"status": "unavailable"},
            "project": {"status": "unavailable"},
            "schema_version": render_dimension(dimensions["schema_version"]),
            "source_dataset": render_dimension(dimensions["source_dataset"]),
            "span.type": render_dimension(dimensions["span.type"]),
            "tenant": {"status": "unavailable"},
        },
        "epochs": epochs,
        "format": "agent-trace-real-audit",
        "format_version": FORMAT_VERSION,
        "global": {
            "path_count": len(path_states),
            "row_count": row_count,
            "trace_count": len(trace_ids),
            "width": percentile_summary(widths),
        },
        "paths": {
            attribute_path: render_path_state(state, row_count)
            for attribute_path, state in sorted(path_states.items())
        },
        "payloads": {
            name: {"length": percentile_summary(lengths), "present_rows": len(lengths)}
            for name, lengths in payloads.items()
        },
        "window_minutes": window_minutes,
    }


def sha256_file(path):
    """返回文件内容的 SHA-256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomically(path, content):
    """在目标目录写入临时文件后原子替换目标文件。"""
    temporary_path = Path(path).with_name(f".{Path(path).name}.tmp")
    temporary_path.write_bytes(content)
    os.replace(temporary_path, path)


def write_audit(input_path, upstream_manifest, output_dir, window_minutes=15):
    """校验固定输入后写入审计产物，并最后发布完成 manifest。"""
    input_path = Path(input_path)
    upstream_manifest = Path(upstream_manifest)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "run-manifest.json"
    manifest_path.unlink(missing_ok=True)

    input_sha256 = sha256_file(input_path)
    upstream_manifest_sha256 = sha256_file(upstream_manifest)
    if input_sha256 != EXPECTED_INPUT_SHA256:
        raise ValueError(f"input SHA-256 mismatch: {input_sha256}")
    if upstream_manifest_sha256 != EXPECTED_UPSTREAM_MANIFEST_SHA256:
        raise ValueError(f"upstream manifest SHA-256 mismatch: {upstream_manifest_sha256}")

    report = audit_file(input_path, window_minutes)
    report["input"] = {
        "path": str(input_path),
        "sha256": input_sha256,
        "upstream_manifest": {"path": str(upstream_manifest), "sha256": upstream_manifest_sha256},
    }
    audit_path = output_dir / "audit.json"
    audit_content = canonical_bytes(report) + b"\n"
    write_atomically(audit_path, audit_content)

    manifest = {
        "artifacts": {
            "audit.json": {"bytes": len(audit_content), "sha256": hashlib.sha256(audit_content).hexdigest()}
        },
        "audit": {"path": "audit/audit_real_traces.py", "sha256": sha256_file(__file__)},
        "data_path": DATA_PATH,
        "format": "agent-trace-json-storage-run",
        "format_version": FORMAT_VERSION,
        "input": report["input"],
        "record_count": report["global"]["row_count"],
        "status": "complete",
        "trace_count": report["global"]["trace_count"],
        "window_minutes": window_minutes,
    }
    write_atomically(manifest_path, canonical_bytes(manifest) + b"\n")


def parse_args():
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="输入 JSONL")
    parser.add_argument("--upstream-manifest", required=True, type=Path, help="上游 manifest")
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    parser.add_argument("--window-minutes", default=15, type=int, help="窗口分钟数")
    return parser.parse_args()


def main():
    """执行审计并输出完成 manifest 路径。"""
    args = parse_args()
    try:
        write_audit(args.input, args.upstream_manifest, args.output, args.window_minutes)
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(str(error)) from error
    print(args.output / "run-manifest.json")


if __name__ == "__main__":
    main()
