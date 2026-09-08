#!/usr/bin/env python3
"""生成阶段二跨引擎共用数据集与查询 truth。"""

import argparse
import hashlib
import json
import os
import shlex
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


FORMAT_VERSION = 1
CONTRACT_VERSION = "json-storage-cross-engine-v1"
DATA_PATH = "independent_loader"
PROJECT_ID = "Leoxx/whowhen_pro"
EXPECTED_INPUT_SHA256 = "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683"
QUERY_IDS = ("Q01", "Q02", "Q03", "Q04", "Q05")


def canonical_bytes(value):
    """返回对象键排序且保留数组顺序的 JSON UTF-8 bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value):
    """返回 bytes 的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    """返回文件内容的 SHA-256。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_attributes(attributes):
    """将点分隔 Attribute 键投影为嵌套 JSON，并拒绝前缀冲突。"""
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    projected = {}
    for original_key, value in attributes.items():
        if not isinstance(original_key, str) or not original_key:
            raise ValueError("attribute key must be a non-empty string")
        current = projected
        parts = original_key.split(".")
        for index, part in enumerate(parts):
            if not part:
                raise ValueError(f"invalid attribute key: {original_key}")
            is_leaf = index == len(parts) - 1
            if is_leaf:
                if part in current:
                    raise ValueError(f"prefix conflict: {original_key}")
                current[part] = value
            else:
                if part not in current:
                    current[part] = {}
                    current = current[part]
                elif isinstance(current[part], dict):
                    current = current[part]
                else:
                    raise ValueError(f"prefix conflict: {original_key}")
    return projected


def project_key_map(attributes):
    """返回投影路径到原始 Attribute 键的可逆映射。"""
    project_attributes(attributes)
    return {key: key for key in sorted(attributes)}


def flatten_projected(value, key_map):
    """按键映射把嵌套投影还原为原始键及 canonical bytes。"""
    flattened = {}

    def visit(current, path):
        joined = ".".join(path)
        if joined in key_map:
            flattened[key_map[joined]] = canonical_bytes(current)
            return
        if not isinstance(current, dict):
            raise ValueError(f"missing key map entry: {joined}")
        for key, nested_value in current.items():
            visit(nested_value, path + [key])

    if not isinstance(value, dict):
        raise ValueError("projected value must be an object")
    visit(value, [])
    return flattened


def build_record(source, raw_event, ingest_seq):
    """把单条原始 Span 及原文 bytes 转为 15 字段统一记录。"""
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    if not isinstance(raw_event, bytes):
        raise ValueError("raw_event must be bytes")
    required = (
        "trace_id",
        "span_id",
        "parent_span_id",
        "start_time",
        "end_time",
        "duration_ms",
        "attributes",
    )
    missing = [name for name in required if name not in source]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    attributes = source["attributes"]
    projected = project_attributes(attributes)
    key_map = project_key_map(attributes)
    restored = flatten_projected(projected, key_map)
    expected = {key: canonical_bytes(value) for key, value in attributes.items()}
    if restored != expected:
        raise ValueError("attribute projection round trip failed")
    try:
        raw_text = raw_event.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("raw_event must be UTF-8") from error
    status = source.get("status", {})
    status_code = status.get("code") if isinstance(status, dict) else None
    return {
        "ingest_seq": ingest_seq,
        "event_id": f"{source['trace_id']}:{source['span_id']}",
        "trace_id": source["trace_id"],
        "span_id": source["span_id"],
        "parent_span_id": source["parent_span_id"],
        "project_id": PROJECT_ID,
        "start_time": source["start_time"],
        "end_time": source["end_time"],
        "duration_ms": source["duration_ms"],
        "span_type": attributes.get("span.type", ""),
        "framework": attributes.get("framework", ""),
        "level": "ERROR" if status_code == "STATUS_CODE_ERROR" else "DEFAULT",
        "attributes_analysis": projected,
        "attributes_map": {key: restored[key].decode("utf-8") for key in sorted(restored)},
        "raw_event": raw_text,
    }


def parse_time(value):
    """把 ISO 8601 时间转换为 UTC datetime。"""
    if not isinstance(value, str):
        raise ValueError("start_time must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid start_time: {value}") from error
    if parsed.tzinfo is None:
        raise ValueError(f"start_time must include timezone: {value}")
    return parsed.astimezone(timezone.utc)


def format_time(value):
    """返回稳定的 UTC ISO 8601 时间字符串。"""
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def trace_parameters(rows):
    """从完整记录集派生固定查询参数。"""
    if not rows:
        raise ValueError("dataset must not be empty")
    start_times = [parse_time(row["start_time"]) for row in rows]
    minimum = min(start_times)
    maximum = max(start_times)
    midpoint = minimum + (maximum - minimum) / 2
    window = {
        "start_time": format_time(minimum),
        "end_time": format_time(midpoint),
    }
    trace_counts = Counter(row["trace_id"] for row in rows)
    ordered_traces = sorted((count, trace_id) for trace_id, count in trace_counts.items())
    representative = ordered_traces[(len(ordered_traces) - 1) // 2][1]
    return {
        "Q01": {
            "project_id": PROJECT_ID,
            **window,
        },
        "Q02": {"project_id": PROJECT_ID, "operation_name": "execute_tool", **window},
        "Q03": {
            "project_id": PROJECT_ID,
            **window,
        },
        "Q04": {"project_id": PROJECT_ID, "trace_id": representative, **window},
        "Q05": {"project_id": PROJECT_ID, "failure_mistake_mode": "A.3", **window},
    }


def query_results(rows, watermark, parameters):
    """计算一个水位下 Q01 至 Q05 的规范化结果和摘要。"""
    if watermark < 0:
        raise ValueError("watermark must not be negative")
    visible = [
        row
        for row in rows
        if row["ingest_seq"] < watermark and row["project_id"] == PROJECT_ID
    ]

    def aggregate(query_rows, include_duration=False):
        grouped = {}
        for row in query_rows:
            key = row["span_type"]
            count, duration = grouped.get(key, (0, 0))
            grouped[key] = (count + 1, duration + row["duration_ms"])
        if include_duration:
            return [[key, count, duration] for key, (count, duration) in sorted(grouped.items())]
        return [[key, count] for key, (count, _duration) in sorted(grouped.items())]

    window_start = parse_time(parameters["Q01"]["start_time"])
    window_end = parse_time(parameters["Q01"]["end_time"])
    window_rows = [
        row
        for row in visible
        if window_start <= parse_time(row["start_time"]) < window_end
    ]
    operation = canonical_bytes(parameters["Q02"]["operation_name"]).decode("utf-8")
    operation_rows = [
        row
        for row in window_rows
        if row["attributes_map"].get("gen_ai.operation.name") == operation
    ]
    trace_rows = sorted(
        (row for row in window_rows if row["trace_id"] == parameters["Q04"]["trace_id"]),
        key=lambda row: (row["start_time"], row["event_id"]),
    )
    failure_mode = canonical_bytes(parameters["Q05"]["failure_mistake_mode"]).decode("utf-8")
    failure_rows = [
        row
        for row in window_rows
        if row["attributes_map"].get("failure.mistake_mode") == failure_mode
    ]
    normalized = {
        "Q01": aggregate(window_rows),
        "Q02": aggregate(operation_rows),
        "Q03": aggregate(window_rows, include_duration=True),
        "Q04": [[row["start_time"], row["event_id"], row["attributes_map"]] for row in trace_rows],
        "Q05": {
            "row_count": len(failure_rows),
            "identity_sha256": sha256_bytes(
                canonical_bytes(sorted(row["event_id"] for row in failure_rows))
            ),
        },
    }
    return {
        query_id: {
            "result": normalized[query_id],
            "result_sha256": sha256_bytes(canonical_bytes(normalized[query_id])),
            "row_count": len(normalized[query_id]) if query_id != "Q05" else normalized[query_id]["row_count"],
        }
        for query_id in QUERY_IDS
    }


def native_json_budget(path_count):
    """返回覆盖审计路径数的最小 2 的幂 native JSON 路径预算。"""
    if not isinstance(path_count, int) or path_count <= 0:
        raise ValueError("audit global path_count must be positive")
    budget = 1
    while budget < path_count:
        budget *= 2
    if budget > 128:
        raise ValueError(f"native JSON path budget exceeds 128: {budget}")
    return budget


def write_atomically(path, content):
    """在目标目录写入临时文件后原子替换目标文件。"""
    path = Path(path)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_bytes(content)
    os.replace(temporary_path, path)


def artifact_identity(content):
    """返回内存产物的字节数与 SHA-256。"""
    return {"bytes": len(content), "sha256": sha256_bytes(content)}


def write_dataset(input_path, audit_path, output_dir, block_size, command=None):
    """校验固定输入与审计后，原子发布 dataset、truth 和完成 manifest。"""
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    input_path = Path(input_path)
    audit_path = Path(audit_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "run-manifest.json"
    manifest_path.unlink(missing_ok=True)

    input_sha256 = sha256_file(input_path)
    if input_sha256 != EXPECTED_INPUT_SHA256:
        raise ValueError(f"input SHA-256 mismatch: {input_sha256}")
    try:
        audit = json.loads(audit_path.read_bytes())
        path_count = audit["global"]["path_count"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("invalid audit.json") from error
    try:
        audit_input = audit["input"]["sha256"]
    except (KeyError, TypeError):
        raise ValueError("audit input SHA-256 missing") from None
    if audit_input != input_sha256:
        raise ValueError(f"audit input SHA-256 mismatch: {audit_input}")

    rows = []
    event_ids = set()
    with input_path.open("rb") as input_file:
        for ingest_seq, line in enumerate(input_file):
            raw_event = line.rstrip(b"\r\n")
            if not raw_event:
                raise ValueError(f"line {ingest_seq + 1}: empty JSONL row")
            try:
                source = json.loads(raw_event)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {ingest_seq + 1}: invalid JSON") from error
            record = build_record(source, raw_event, ingest_seq)
            if record["event_id"] in event_ids:
                raise ValueError(f"duplicate event_id: {record['event_id']}")
            event_ids.add(record["event_id"])
            rows.append(record)
    if not rows:
        raise ValueError("input must not be empty")

    parameters = trace_parameters(rows)
    watermarks = list(range(block_size, len(rows), block_size)) + [len(rows)]
    key_map = project_key_map({key: None for row in rows for key in row["attributes_map"]})
    truth = {
        "block_count": len(watermarks),
        "block_size": block_size,
        "comparability_contract_version": CONTRACT_VERSION,
        "format": "agent-trace-cross-engine-truth",
        "format_version": FORMAT_VERSION,
        "input": {"path": str(input_path), "sha256": input_sha256},
        "key_map": key_map,
        "native_json": {
            "budget_formula": "smallest power of two greater than or equal to global path_count, capped at 128",
            "path_budget": native_json_budget(path_count),
            "path_count": path_count,
        },
        "parameters": parameters,
        "record_count": len(rows),
        "records": [
            {
                "analysis_sha256": sha256_bytes(canonical_bytes(row["attributes_analysis"])),
                "canonical_sha256": sha256_bytes(canonical_bytes(row)),
                "event_id": row["event_id"],
                "raw_sha256": sha256_bytes(row["raw_event"].encode("utf-8")),
            }
            for row in rows
        ],
        "watermarks": watermarks,
        "queries": {query_id: {} for query_id in QUERY_IDS},
    }
    for watermark in watermarks:
        for query_id, result in query_results(rows, watermark, parameters).items():
            truth["queries"][query_id][str(watermark)] = result

    dataset_content = b"".join(canonical_bytes(row) + b"\n" for row in rows)
    truth_content = canonical_bytes(truth) + b"\n"
    dataset_path = output_dir / "dataset.jsonl"
    truth_path = output_dir / "truth-manifest.json"
    write_atomically(dataset_path, dataset_content)
    write_atomically(truth_path, truth_content)

    manifest = {
        "artifacts": {
            "dataset.jsonl": artifact_identity(dataset_content),
            "truth-manifest.json": artifact_identity(truth_content),
        },
        "audit": {"path": str(audit_path), "sha256": sha256_file(audit_path)},
        "block_count": len(watermarks),
        "block_size": block_size,
        "comparability_contract_version": CONTRACT_VERSION,
        "command": command,
        "data_path": DATA_PATH,
        "format": "agent-trace-json-storage-run",
        "format_version": FORMAT_VERSION,
        "generator": {"path": "generator/generate_cross_engine.py", "sha256": sha256_file(__file__)},
        "input": truth["input"],
        "record_count": len(rows),
        "status": "complete",
        "watermarks": watermarks,
    }
    write_atomically(manifest_path, canonical_bytes(manifest) + b"\n")


def parse_args():
    """解析生成器 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="输入 JSONL")
    parser.add_argument("--audit", required=True, type=Path, help="审计结果")
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    parser.add_argument("--block-size", default=256, type=int, help="INSERT block 行数")
    return parser.parse_args()


def main():
    """执行生成并输出完成 manifest 路径。"""
    args = parse_args()
    try:
        command = shlex.join([Path(sys.executable).name, *sys.argv])
        write_dataset(args.input, args.audit, args.output, args.block_size, command)
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(str(error)) from error
    print(args.output / "run-manifest.json")


if __name__ == "__main__":
    main()
