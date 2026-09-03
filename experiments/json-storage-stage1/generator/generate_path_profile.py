#!/usr/bin/env python3
"""生成 JSON 路径数量与密度实验的确定性数据。"""

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


FORMAT_VERSION = 1
SUPPORTED_PATH_COUNTS = (50, 500, 5000)
SUPPORTED_DENSITIES = (1, 20, 95)
DENSITY_PERIOD = 100
DENSITY_STEP = 37
DEFAULT_TARGET_BYTES = 128 * 1024 * 1024


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


def file_identity(path):
    """流式返回文件长度与 SHA-256，避免重新物化正式产物。"""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def stable_id(kind, seed, index, length):
    """按类型、seed 和序号生成固定长度的十六进制标识。"""
    source = f"{kind}:{seed}:{index}".encode("utf-8")
    return sha256_hex(source)[:length]


def path_is_present(row_index, path_index, density_percent, seed):
    """按 100 行周期决定路径是否存在，使每条路径达到指定总体密度。"""
    position = path_index + row_index * DENSITY_STEP + seed % DENSITY_PERIOD
    return position % DENSITY_PERIOD < density_percent


def build_record(index, path_count, density_percent, seed):
    """生成一条包含固定热点和稀疏动态路径的逻辑记录。"""
    trace_index = index // 5
    span_position = index % 5
    metadata_paths = {
        f"p{path_index:05d}": f"v{(index + path_index) % 17:02d}"
        for path_index in range(path_count)
        if path_is_present(index, path_index, density_percent, seed)
    }
    record = {
        "end_time": (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=index + 1)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event_id": stable_id("event", seed, index, 32),
        "level": "ERROR" if index % 17 == 0 else "DEFAULT",
        "metadata": {
            "hot": {
                "region": f"region-{index % 4:02d}",
                "tenant": f"tenant-{index % 8:02d}",
            },
            "paths": metadata_paths,
        },
        "model": f"model-{index % 3}",
        "parent_span_id": (
            None
            if span_position == 0
            else stable_id("span", seed, index - 1, 16)
        ),
        "service_name": f"service-{index % 4}",
        "span_id": stable_id("span", seed, index, 16),
        "start_time": (
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=index)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "trace_id": stable_id("trace", seed, trace_index, 32),
        "type": "generation" if index % 3 == 0 else "span",
    }
    return record


def build_profile(count, path_count, density_percent, seed):
    """生成指定规模的 dataset bytes 与 truth manifest。"""
    if count <= 0 or count % DENSITY_PERIOD != 0:
        raise ValueError(f"count must be a positive multiple of {DENSITY_PERIOD}")
    if path_count not in SUPPORTED_PATH_COUNTS:
        raise ValueError(f"path-count must be one of {SUPPORTED_PATH_COUNTS}")
    if density_percent not in SUPPORTED_DENSITIES:
        raise ValueError(f"density-percent must be one of {SUPPORTED_DENSITIES}")

    records = [
        build_record(index, path_count, density_percent, seed)
        for index in range(count)
    ]
    dataset = b"".join(canonical_bytes(record, newline=True) for record in records)
    rows = [
        {
            "canonical_sha256": sha256_hex(canonical_bytes(record)),
            "event_id": record["event_id"],
            "metadata_canonical_sha256": sha256_hex(
                canonical_bytes(record["metadata"])
            ),
        }
        for record in records
    ]

    cold_path = f"p{path_count - 1:05d}"
    query_specs = (
        (
            "hot_tenant_equals",
            {"path": "hot.tenant", "value": "tenant-03"},
            lambda record: record["metadata"]["hot"]["tenant"] == "tenant-03",
        ),
        (
            "cold_path_equals",
            {"path": cold_path, "value": "v00"},
            lambda record: record["metadata"]["paths"].get(cold_path) == "v00",
        ),
    )
    queries = []
    for query_id, parameters, predicate in query_specs:
        event_ids = sorted(record["event_id"] for record in records if predicate(record))
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
            "object_key_order": "ignored",
        },
        "density_definition": {
            "period_rows": DENSITY_PERIOD,
            "scope": "dynamic_paths",
        },
        "density_percent": density_percent,
        "format": "agent-trace-json-path-truth",
        "format_version": FORMAT_VERSION,
        "path_count": path_count,
        "profile": "path-organization",
        "queries": queries,
        "record_count": count,
        "rows": rows,
        "seed": seed,
    }
    return dataset, canonical_bytes(truth_manifest, newline=True)


def select_record_count(target_bytes, path_count, density_percent, seed):
    """按一个密度周期的平均行宽估算最接近目标大小的记录数。"""
    if target_bytes <= 0:
        raise ValueError("target-bytes must be positive")
    calibration_bytes = sum(
        len(
            canonical_bytes(
                build_record(index, path_count, density_percent, seed),
                newline=True,
            )
        )
        for index in range(DENSITY_PERIOD)
    )
    period_count = max(1, round(target_bytes / calibration_bytes))
    return period_count * DENSITY_PERIOD


def write_profile(
    output_dir,
    count,
    path_count,
    density_percent,
    seed,
    *,
    target_bytes=None,
):
    """流式写入 profile，并在所有数据完成后原子发布 run manifest。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    run_manifest_path = output_dir / "run-manifest.json"
    run_manifest_path.unlink(missing_ok=True)
    if count is None:
        count = select_record_count(target_bytes, path_count, density_percent, seed)
    if count <= 0 or count % DENSITY_PERIOD != 0:
        raise ValueError(f"count must be a positive multiple of {DENSITY_PERIOD}")

    dataset_path = output_dir / "dataset.jsonl"
    truth_path = output_dir / "truth-manifest.json"
    dataset_temp = output_dir / "dataset.jsonl.tmp"
    rows_temp = output_dir / "truth-rows.jsonl.tmp"
    truth_temp = output_dir / "truth-manifest.json.tmp"
    for path in (dataset_temp, rows_temp, truth_temp):
        path.unlink(missing_ok=True)

    query_ids = {"hot_tenant_equals": [], "cold_path_equals": []}
    cold_path = f"p{path_count - 1:05d}"
    try:
        with dataset_temp.open("wb") as dataset_file, rows_temp.open("wb") as rows_file:
            for index in range(count):
                record = build_record(index, path_count, density_percent, seed)
                record_bytes = canonical_bytes(record)
                dataset_file.write(record_bytes + b"\n")
                row = {
                    "canonical_sha256": sha256_hex(record_bytes),
                    "event_id": record["event_id"],
                    "metadata_canonical_sha256": sha256_hex(
                        canonical_bytes(record["metadata"])
                    ),
                }
                rows_file.write(canonical_bytes(row, newline=True))
                if record["metadata"]["hot"]["tenant"] == "tenant-03":
                    query_ids["hot_tenant_equals"].append(record["event_id"])
                if record["metadata"]["paths"].get(cold_path) == "v00":
                    query_ids["cold_path_equals"].append(record["event_id"])

        queries = []
        for query_id, parameters in (
            ("hot_tenant_equals", {"path": "hot.tenant", "value": "tenant-03"}),
            ("cold_path_equals", {"path": cold_path, "value": "v00"}),
        ):
            event_ids = sorted(query_ids[query_id])
            queries.append(
                {
                    "expected_event_ids": event_ids,
                    "expected_row_count": len(event_ids),
                    "parameters": parameters,
                    "query_id": query_id,
                }
            )

        truth_prefix = {
            "canonicalization": {
                "array_order": "preserved",
                "object_key_order": "ignored",
            },
            "density_definition": {
                "period_rows": DENSITY_PERIOD,
                "scope": "dynamic_paths",
            },
            "density_percent": density_percent,
            "format": "agent-trace-json-path-truth",
            "format_version": FORMAT_VERSION,
            "path_count": path_count,
            "profile": "path-organization",
            "queries": queries,
            "record_count": count,
        }
        with truth_temp.open("wb") as truth_file:
            truth_file.write(b"{")
            for key, value in truth_prefix.items():
                truth_file.write(canonical_bytes(key) + b":" + canonical_bytes(value) + b",")
            truth_file.write(b'"rows":[')
            with rows_temp.open("rb") as rows_file:
                for index, line in enumerate(rows_file):
                    if index:
                        truth_file.write(b",")
                    truth_file.write(line.rstrip(b"\n"))
            truth_file.write(b'],"seed":' + canonical_bytes(seed) + b"}\n")

        dataset_temp.replace(dataset_path)
        truth_temp.replace(truth_path)
    finally:
        for path in (dataset_temp, rows_temp, truth_temp):
            path.unlink(missing_ok=True)

    artifacts = {
        "dataset.jsonl": file_identity(dataset_path),
        "truth-manifest.json": file_identity(truth_path),
    }

    run_manifest = {
        "artifacts": dict(sorted(artifacts.items())),
        "density_percent": density_percent,
        "format": "agent-trace-json-storage-run",
        "format_version": FORMAT_VERSION,
        "generator": {
            "path": "generator/generate_path_profile.py",
            "sha256": sha256_hex(Path(__file__).read_bytes()),
        },
        "path_count": path_count,
        "profile": "path-organization",
        "record_count": count,
        "seed": seed,
        "status": "complete",
    }
    if target_bytes is not None:
        run_manifest["target_input_bytes"] = target_bytes
    run_manifest_path.write_bytes(canonical_bytes(run_manifest, newline=True))


def parse_args():
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    size = parser.add_mutually_exclusive_group(required=True)
    size.add_argument("--count", type=int, help="记录数，必须是 100 的正整数倍")
    size.add_argument(
        "--target-bytes",
        type=int,
        help=f"未压缩 JSONL 目标大小；性能 profile 使用 {DEFAULT_TARGET_BYTES}",
    )
    parser.add_argument(
        "--path-count",
        required=True,
        type=int,
        choices=SUPPORTED_PATH_COUNTS,
        help="动态路径全集大小",
    )
    parser.add_argument(
        "--density-percent",
        required=True,
        type=int,
        choices=SUPPORTED_DENSITIES,
        help="每个动态路径在记录中出现的百分比",
    )
    parser.add_argument("--seed", default=20260902, type=int, help="确定性 seed")
    return parser.parse_args()


def main():
    """执行生成器；成功时输出 run manifest 路径。"""
    args = parse_args()
    try:
        write_profile(
            args.output,
            args.count,
            args.path_count,
            args.density_percent,
            args.seed,
            target_bytes=args.target_bytes,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(args.output / "run-manifest.json")


if __name__ == "__main__":
    main()
