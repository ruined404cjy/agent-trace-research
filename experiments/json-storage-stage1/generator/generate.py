#!/usr/bin/env python3
"""生成 JSON 存储第一阶段的确定性正确性数据。"""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


FORMAT_VERSION = 1
CASES = (
    "metadata_sql_null",
    "metadata_path_missing",
    "metadata_json_null",
    "target_boolean",
    "target_integer",
    "target_number",
    "conflict_string",
    "conflict_integer",
    "conflict_object",
    "conflict_array",
    "escaped_path",
    "array_order",
)
DUPLICATE_PROBE = b'{"probe_id":"duplicate-key-001","metadata":{"dup":1,"dup":2}}\n'
QUERY_SPECS = (
    (
        "metadata_column_is_sql_null",
        ("metadata_sql_null",),
        {"column": "metadata", "state": "sql_null"},
    ),
    (
        "metadata_target_is_missing",
        (
            "metadata_path_missing",
            "conflict_string",
            "conflict_integer",
            "conflict_object",
            "conflict_array",
            "escaped_path",
            "array_order",
        ),
        {"pointer": "/metadata/target", "state": "missing"},
    ),
    (
        "metadata_target_is_json_null",
        ("metadata_json_null",),
        {"pointer": "/metadata/target", "type": "null", "value": None},
    ),
    (
        "metadata_target_integer_equals_one",
        ("target_integer",),
        {"pointer": "/metadata/target", "type": "integer", "value": 1},
    ),
    (
        "metadata_conflict_is_object",
        ("conflict_object",),
        {"pointer": "/metadata/conflict", "type": "object", "value": {"value": 1}},
    ),
    (
        "escaped_path_equals_escaped",
        ("escaped_path",),
        {"pointer": "/metadata/a~1b/tilde~0key", "type": "string", "value": "escaped"},
    ),
    (
        "array_order_equals_3_2_1",
        ("array_order",),
        {"pointer": "/metadata/ordered", "type": "array", "value": [3, 2, 1]},
    ),
)


def canonical_bytes(value, *, newline=False):
    """返回 canonical JSON UTF-8 bytes；对象键排序，数组顺序保持。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return encoded + (b"\n" if newline else b"")


def sha256_hex(content):
    """计算输入 bytes 的十六进制 SHA-256。"""
    return hashlib.sha256(content).hexdigest()


def stable_id(kind, seed, index, length):
    """按类型、seed 和序号生成固定长度的十六进制标识。"""
    source = f"{kind}:{seed}:{index}".encode("utf-8")
    return sha256_hex(source)[:length]


def json_type(value):
    """返回 truth manifest 使用的 JSON 类型名称。"""
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


def metadata_for_case(case):
    """返回 case 对应的 metadata；None 表示整列 SQL NULL。"""
    values = {
        "metadata_sql_null": None,
        "metadata_path_missing": {"present": "value"},
        "metadata_json_null": {"target": None},
        "target_boolean": {"target": True},
        "target_integer": {"target": 1},
        "target_number": {"target": 1.5},
        "conflict_string": {"conflict": "1"},
        "conflict_integer": {"conflict": 1},
        "conflict_object": {"conflict": {"value": 1}},
        "conflict_array": {"conflict": [1, 2]},
        "escaped_path": {"a/b": {"tilde~key": "escaped"}},
        "array_order": {"ordered": [3, 2, 1]},
    }
    return values[case]


def path_truth(case, metadata):
    """返回 case 的关键 JSON Pointer 及其预期状态。"""
    if case == "metadata_sql_null":
        return {"/metadata/target": {"state": "column_sql_null"}}
    if case == "metadata_path_missing":
        return {"/metadata/target": {"state": "missing"}}

    if case in {"metadata_json_null", "target_boolean", "target_integer", "target_number"}:
        pointer = "/metadata/target"
        value = metadata["target"]
    elif case.startswith("conflict_"):
        pointer = "/metadata/conflict"
        value = metadata["conflict"]
    elif case == "escaped_path":
        pointer = "/metadata/a~1b/tilde~0key"
        value = metadata["a/b"]["tilde~key"]
    else:
        pointer = "/metadata/ordered"
        value = metadata["ordered"]

    return {
        pointer: {
            "state": "value",
            "type": json_type(value),
            "value": value,
        }
    }


def build_record(index, seed):
    """生成一条逻辑记录及其 truth；输入为序号和 seed。"""
    case = CASES[index % len(CASES)]
    trace_index = index // 5
    span_position = index % 5
    span_id = stable_id("span", seed, index, 16)
    record = {
        "case_id": case,
        "end_time": (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=index + 1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event_id": stable_id("event", seed, index, 32),
        "level": "ERROR" if index % 17 == 0 else "DEFAULT",
        "model": f"model-{index % 3}",
        "parent_span_id": None if span_position == 0 else stable_id("span", seed, index - 1, 16),
        "service_name": f"service-{index % 4}",
        "span_id": span_id,
        "start_time": (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=index)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "trace_id": stable_id("trace", seed, trace_index, 32),
        "type": "generation" if index % 3 == 0 else "span",
    }
    metadata = metadata_for_case(case)
    if metadata is not None:
        record["metadata"] = metadata

    record_bytes = canonical_bytes(record)
    columns = {"metadata": {"state": "sql_null"}}
    if metadata is not None:
        columns = {
            "metadata": {
                "canonical_sha256": sha256_hex(canonical_bytes(metadata)),
                "state": "json",
            }
        }
    truth = {
        "canonical_sha256": sha256_hex(record_bytes),
        "case": case,
        "columns": columns,
        "event_id": record["event_id"],
        "paths": path_truth(case, metadata),
    }
    return record, truth


def build_profile(count, seed):
    """生成指定规模的 dataset bytes 与 truth manifest。"""
    if count <= 0 or count % len(CASES) != 0:
        raise ValueError(f"count must be a positive multiple of {len(CASES)}")

    records = []
    rows = []
    for index in range(count):
        record, truth = build_record(index, seed)
        records.append(record)
        rows.append(truth)

    dataset = b"".join(canonical_bytes(record, newline=True) for record in records)
    case_counts = Counter(row["case"] for row in rows)
    queries = []
    for query_id, cases, parameters in QUERY_SPECS:
        event_ids = sorted(row["event_id"] for row in rows if row["case"] in cases)
        queries.append(
            {
                "expected_event_ids": event_ids,
                "expected_row_count": len(event_ids),
                "parameters": parameters,
                "query_id": query_id,
            }
        )
    truth_manifest = {
        "canonicalization": {
            "array_order": "preserved",
            "duplicate_keys": "raw_probe_only",
            "object_key_order": "ignored",
            "sql_null": "top_level_column_omitted",
        },
        "case_counts": dict(sorted(case_counts.items())),
        "duplicate_key_probes": [
            {
                "duplicate_path": "/metadata/dup",
                "occurrences": [1, 2],
                "probe_id": "duplicate-key-001",
                "required_observation": "record_acceptance_and_retained_value",
            }
        ],
        "format": "agent-trace-json-correctness-truth",
        "format_version": FORMAT_VERSION,
        "profile": "correctness",
        "queries": queries,
        "record_count": count,
        "rows": rows,
        "seed": seed,
    }
    return dataset, canonical_bytes(truth_manifest, newline=True)


def write_profile(output_dir, count, seed):
    """把 profile 写入输出目录，并在最后写入完整 run manifest。"""
    dataset, truth = build_profile(count, seed)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_manifest_path = output_dir / "run-manifest.json"
    run_manifest_path.unlink(missing_ok=True)

    artifacts = {
        "dataset.jsonl": dataset,
        "duplicate-key-probes.jsonl": DUPLICATE_PROBE,
        "truth-manifest.json": truth,
    }
    for name, content in artifacts.items():
        (output_dir / name).write_bytes(content)

    run_manifest = {
        "artifacts": {
            name: {"bytes": len(content), "sha256": sha256_hex(content)}
            for name, content in sorted(artifacts.items())
        },
        "format": "agent-trace-json-storage-run",
        "format_version": FORMAT_VERSION,
        "generator": {
            "path": "generator/generate.py",
            "sha256": sha256_hex(Path(__file__).read_bytes()),
        },
        "profile": "correctness",
        "record_count": count,
        "seed": seed,
        "status": "complete",
    }
    run_manifest_path.write_bytes(canonical_bytes(run_manifest, newline=True))


def parse_args():
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    parser.add_argument("--count", default=300, type=int, help="记录数，必须是 12 的正整数倍")
    parser.add_argument("--seed", default=20260902, type=int, help="确定性 seed")
    return parser.parse_args()


def main():
    """执行生成器；成功时输出 run manifest 路径。"""
    args = parse_args()
    try:
        write_profile(args.output, args.count, args.seed)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(args.output / "run-manifest.json")


if __name__ == "__main__":
    main()
