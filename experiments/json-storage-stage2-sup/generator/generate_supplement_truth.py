#!/usr/bin/env python3
"""从阶段二冻结数据生成四结构补充查询 truth。"""

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from supplement_common import (
    CONTRACT_VERSION,
    QUERY_IDS,
    canonical_bytes,
    file_identity,
    read_json,
    verify_input,
    write_manifest_last,
)


FORMAT_VERSION = 1
PAGE_SIZE = 256
EXPECTED_BLOCK_COUNT = 190
EXPECTED_BLOCK_SIZE = 256
EXPECTED_DATASET_SHA256 = "8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405"
EXPECTED_RECORD_COUNT = 48534
EXPECTED_SOURCE_INPUT_SHA256 = "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683"


def preprocess_input(input_dir: Path) -> dict:
    """验证阶段二输入目录并返回补充 truth 所需的来源信息。"""
    input_dir = Path(input_dir)
    rows, source_truth = verify_input(input_dir)
    source_manifest = read_json(input_dir / "run-manifest.json", "run manifest")
    expected_identity = {
        "block_count": EXPECTED_BLOCK_COUNT,
        "block_size": EXPECTED_BLOCK_SIZE,
        "dataset_sha256": EXPECTED_DATASET_SHA256,
        "record_count": EXPECTED_RECORD_COUNT,
    }
    actual_identity = {
        "block_count": source_manifest.get("block_count"),
        "block_size": source_manifest.get("block_size"),
        "dataset_sha256": source_manifest.get("artifacts", {}).get("dataset.jsonl", {}).get("sha256"),
        "record_count": source_manifest.get("record_count"),
    }
    if actual_identity != expected_identity:
        raise ValueError("frozen dataset identity mismatch")
    if source_manifest.get("input", {}).get("sha256") != EXPECTED_SOURCE_INPUT_SHA256:
        raise ValueError("frozen source input identity mismatch")
    return {
        "input_dir": input_dir,
        "rows": rows,
        "source_manifest": source_manifest,
        "source_truth": source_truth,
    }


def _value_at_path(value, dotted_path):
    """返回嵌套 JSON 对象中点分隔路径的值，缺失时返回 None。"""
    current = value
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _window_rows(rows, parameters):
    """按固定 project 与左闭右开时间窗口筛选记录。"""
    try:
        project_id = parameters["project_id"]
        start_time = parameters["start_time"]
        end_time = parameters["end_time"]
    except KeyError as error:
        raise ValueError(f"missing query parameter: {error.args[0]}") from None
    return [
        row
        for row in rows
        if row.get("project_id") == project_id
        and start_time <= row.get("start_time", "") < end_time
    ]


def _aggregate_span_types(rows):
    """返回按 span_type 排序的分组计数。"""
    counts = Counter(row["span_type"] for row in rows)
    return [[span_type, count] for span_type, count in sorted(counts.items())]


def _identity_result(rows):
    """返回记录数及按 event_id 排序的身份摘要。"""
    event_ids = sorted(row["event_id"] for row in rows)
    return {
        "identity_sha256": hashlib.sha256(canonical_bytes(event_ids)).hexdigest(),
        "row_count": len(event_ids),
    }


def _document_rows(rows):
    """把完整分析文档规范化为固定可比较的返回行。"""
    return [
        [row["start_time"], row["event_id"], row["attributes_analysis"]]
        for row in sorted(rows, key=lambda row: (row["start_time"], row["event_id"]))
    ]


def query_results(rows: list[dict], query_id: str, parameters: dict) -> object:
    """计算单个补充查询的规范化最终水位结果。"""
    if query_id not in QUERY_IDS:
        raise ValueError(f"unknown query id: {query_id}")
    window_rows = _window_rows(rows, parameters)
    if query_id == "S01":
        return _aggregate_span_types(window_rows)
    if query_id == "S02":
        try:
            operation_name = parameters["operation_name"]
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None
        return _aggregate_span_types(
            row
            for row in window_rows
            if _value_at_path(row["attributes_analysis"], "gen_ai.operation.name") == operation_name
        )
    if query_id == "S03":
        try:
            failure_mode = parameters["failure_mistake_mode"]
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None
        return _identity_result(
            row
            for row in window_rows
            if _value_at_path(row["attributes_analysis"], "failure.mistake_mode") == failure_mode
        )
    if query_id == "S04":
        try:
            attribute_key = parameters["attribute_key"]
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None
        values = [
            _value_at_path(row["attributes_analysis"], attribute_key)
            for row in window_rows
        ]
        present_values = [value for value in values if value is not None]
        return {
            "non_null_count": len(present_values),
            "utf8_bytes": sum(len(canonical_bytes(value)) for value in present_values),
        }
    if query_id == "S05":
        try:
            trace_id = parameters["trace_id"]
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None
        return _document_rows(row for row in window_rows if row["trace_id"] == trace_id)
    try:
        page_size = parameters["page_size"]
    except KeyError as error:
        raise ValueError(f"missing query parameter: {error.args[0]}") from None
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
        raise ValueError("page_size must be positive")
    documents = _document_rows(_window_rows(rows, parameters))
    start_index = (len(documents) + 3) // 4 - 1
    page = documents[max(start_index, 0):max(start_index, 0) + page_size]
    return {
        "identity_sha256": hashlib.sha256(canonical_bytes([row[1] for row in page])).hexdigest(),
        "page_row_count": len(page),
        "row_count": len(documents),
        "rows": page,
    }


def _query_parameters(source_truth):
    """从阶段二固定参数派生补充查询的固定参数。"""
    source_parameters = source_truth.get("parameters")
    if not isinstance(source_parameters, dict):
        raise ValueError("source truth parameters missing")
    try:
        return {
            "S01": dict(source_parameters["Q01"]),
            "S02": dict(source_parameters["Q02"]),
            "S03": dict(source_parameters["Q05"]),
            "S04": {**source_parameters["Q01"], "attribute_key": "gen_ai.output.messages"},
            "S05": dict(source_parameters["Q04"]),
            "S06": {**source_parameters["Q01"], "page_size": PAGE_SIZE},
        }
    except KeyError as error:
        raise ValueError(f"source truth parameter missing: {error.args[0]}") from None


def write_truth(input_dir: Path, output_dir: Path, command: list[str], clock_ns=None) -> None:
    """生成 catalog 与 S01 至 S06 truth；clock_ns 控制 artifact 写入计时。"""
    if not isinstance(command, list) or not all(isinstance(value, str) for value in command):
        raise ValueError("command must be a list of strings")
    started = time.perf_counter_ns()
    input_dir = Path(input_dir)
    raw_dataset = (input_dir / "dataset.jsonl").read_bytes()
    read_elapsed_ns = time.perf_counter_ns() - started

    parsed_started = time.perf_counter_ns()
    try:
        parsed_rows = [json.loads(line) for line in raw_dataset.splitlines()]
    except json.JSONDecodeError as error:
        raise ValueError("invalid dataset.jsonl") from error
    if not parsed_rows or not all(isinstance(row, dict) for row in parsed_rows):
        raise ValueError("invalid dataset.jsonl")
    parse_elapsed_ns = time.perf_counter_ns() - parsed_started

    validated = preprocess_input(input_dir)
    if parsed_rows != validated["rows"]:
        raise ValueError("dataset validation mismatch")
    parameters_started = time.perf_counter_ns()
    parameters = _query_parameters(validated["source_truth"])
    parameters_elapsed_ns = time.perf_counter_ns() - parameters_started

    truth_started = time.perf_counter_ns()
    results = {
        query_id: query_results(parsed_rows, query_id, parameters[query_id])
        for query_id in QUERY_IDS
    }
    truth_elapsed_ns = time.perf_counter_ns() - truth_started
    source_manifest = validated["source_manifest"]
    dataset_identity = file_identity(input_dir / "dataset.jsonl")
    source = {
        "artifacts": source_manifest["artifacts"],
        "input": source_manifest["input"],
        "path": str(input_dir),
        "run_manifest_sha256": file_identity(input_dir / "run-manifest.json")["sha256"],
        "truth_manifest_sha256": file_identity(input_dir / "truth-manifest.json")["sha256"],
    }
    catalog = {
        "contract_version": CONTRACT_VERSION,
        "format": "agent-trace-json-storage-supplement-query-catalog",
        "format_version": FORMAT_VERSION,
        "parameters": parameters,
        "query_ids": list(QUERY_IDS),
    }
    truth = {
        "block_count": source_manifest["block_count"],
        "block_size": source_manifest["block_size"],
        "contract_version": CONTRACT_VERSION,
        "format": "agent-trace-json-storage-supplement-truth",
        "format_version": FORMAT_VERSION,
        "input": {"dataset_sha256": dataset_identity["sha256"], "source_input": source_manifest["input"]},
        "query_catalog_sha256": hashlib.sha256(canonical_bytes(catalog)).hexdigest(),
        "query_ids": list(QUERY_IDS),
        "record_count": len(parsed_rows),
        "results": results,
        "source": source,
    }
    artifacts = {
        "query-catalog.json": canonical_bytes(catalog) + b"\n",
        "truth-manifest.json": canonical_bytes(truth) + b"\n",
    }
    manifest = {
        "block_count": source_manifest["block_count"],
        "block_size": source_manifest["block_size"],
        "command": command,
        "format": "agent-trace-json-storage-supplement-run",
        "format_version": FORMAT_VERSION,
        "generator": {"path": "generator/generate_supplement_truth.py", "sha256": file_identity(__file__)["sha256"]},
        "input": {"dataset_sha256": dataset_identity["sha256"], "source_input": source_manifest["input"]},
        "record_count": len(parsed_rows),
        "source": source,
        "supplement_contract_version": CONTRACT_VERSION,
        "timings_ns": {
            "json_parse": parse_elapsed_ns,
            "parameter_selection": parameters_elapsed_ns,
            "read": read_elapsed_ns,
            "truth_calculation": truth_elapsed_ns,
        },
    }
    write_manifest_last(
        output_dir,
        manifest,
        artifacts,
        record_artifact_write_timing=True,
        clock_ns=clock_ns,
    )


def parse_args():
    """解析 truth 生成命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="阶段二输入目录")
    parser.add_argument("--output", required=True, type=Path, help="补充 truth 输出目录")
    return parser.parse_args()


def main():
    """执行 truth 生成并输出完成 manifest 路径。"""
    args = parse_args()
    try:
        write_truth(args.input, args.output, [Path(sys.executable).name, *sys.argv])
    except (OSError, ValueError, TypeError) as error:
        raise SystemExit(str(error)) from error
    print(args.output / "run-manifest.json")


if __name__ == "__main__":
    main()
