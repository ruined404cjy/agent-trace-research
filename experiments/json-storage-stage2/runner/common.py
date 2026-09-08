"""跨引擎 runner 的公共可比性与 manifest 契约。"""

import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path


COMPARABILITY_CONTRACT_VERSION = "json-storage-cross-engine-v1"
DATA_PATH = "independent_loader"
RESERVED_ARTIFACT_NAMES = {"run-manifest.json", ".run-manifest.json.tmp"}
COMPARABILITY_CONTRACT = {
    "connection_reuse": "each worker reuses one independent connection within a stage",
    "latency_boundary": "statement submission through complete result read",
    "request_equivalent_qps_formula": "success_count * 1000 / sum(success latency_ms)",
    "stage_barrier": "all connected workers enter each warmup or measurement stage together",
    "success_gate": "all formal samples and correctness gates must succeed",
}


def canonical_bytes(value):
    """返回对象键排序且保留数组顺序的 JSON UTF-8 bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def file_identity(path):
    """返回文件实际字节数和 SHA-256。"""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def nearest_rank(values, percentile):
    """返回数值序列的 nearest-rank 分位数。"""
    return sorted(values)[math.ceil(len(values) * percentile / 100) - 1]


def summarize_samples(samples):
    """汇总全成功样本的延迟分位数和请求等价速率。"""
    if not samples:
        raise ValueError("samples must not be empty")
    latencies = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("sample must be an object")
        if sample.get("ok") is not True:
            raise ValueError("failed sample")
        latency = sample.get("latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or not math.isfinite(latency) or latency <= 0:
            raise ValueError("latency_ms must be positive")
        latencies.append(latency)
    total_latency_ms = sum(latencies)
    return {
        "latency_ms": {
            "p50": nearest_rank(latencies, 50),
            "p95": nearest_rank(latencies, 95),
            "p99": nearest_rank(latencies, 99),
            "max": max(latencies),
        },
        "request_equivalent_qps": len(latencies) * 1000 / total_latency_ms,
        "sample_count": len(latencies),
        "total_latency_ms": total_latency_ms,
    }


def audit_identities(actual, expected):
    """返回实际 identity 相对期望集合的缺失、额外和重复明细。"""
    actual_values = list(actual)
    expected_values = set(expected)
    counts = Counter(actual_values)
    actual_set = set(actual_values)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    return {
        "actual_count": len(actual_values),
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "expected_count": len(expected_values),
        "extra": sorted(actual_set - expected_values),
        "missing": sorted(expected_values - actual_set),
    }


def read_json(path, name):
    """读取 JSON 文件，并将格式错误转换为输入验证错误。"""
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"invalid {name}")
    return value


def require_equal(actual, expected, message):
    """在值不相等时以稳定消息中断输入验证。"""
    if actual != expected:
        raise ValueError(message)


def verify_input(input_dir):
    """验证完成输入目录并返回 dataset 行列表和 truth manifest。"""
    input_dir = Path(input_dir)
    manifest = read_json(input_dir / "run-manifest.json", "run manifest")
    if manifest.get("status") != "complete":
        raise ValueError("manifest status must be complete")
    if manifest.get("comparability_contract_version") != COMPARABILITY_CONTRACT_VERSION:
        raise ValueError("manifest contract version mismatch")
    if manifest.get("data_path") != DATA_PATH:
        raise ValueError("manifest data path mismatch")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("manifest artifacts missing")
    for name in ("dataset.jsonl", "truth-manifest.json"):
        try:
            actual_identity = file_identity(input_dir / name)
        except OSError as error:
            raise ValueError(f"artifact identity mismatch: {name}") from error
        if artifacts.get(name) != actual_identity:
            raise ValueError(f"artifact identity mismatch: {name}")

    rows = []
    try:
        with (input_dir / "dataset.jsonl").open("rb") as dataset_file:
            for line_number, line in enumerate(dataset_file, start=1):
                if not line.strip():
                    raise ValueError(f"invalid dataset row: {line_number}")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"invalid dataset row: {line_number}")
                rows.append(row)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid dataset.jsonl") from error
    truth = read_json(input_dir / "truth-manifest.json", "truth manifest")

    record_count = len(rows)
    if record_count == 0:
        raise ValueError("dataset must not be empty")
    block_size = manifest.get("block_size")
    if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size <= 0:
        raise ValueError("invalid block size")
    expected_watermarks = list(range(block_size, record_count, block_size)) + [record_count]
    for source, name in ((manifest, "manifest"), (truth, "truth")):
        require_equal(source.get("comparability_contract_version"), COMPARABILITY_CONTRACT_VERSION, f"{name} contract version mismatch")
        require_equal(source.get("record_count"), record_count, f"{name} record count mismatch")
        require_equal(source.get("block_size"), block_size, f"{name} block size mismatch")
        require_equal(source.get("block_count"), len(expected_watermarks), f"{name} block count mismatch")
        require_equal(source.get("watermarks"), expected_watermarks, f"{name} watermarks mismatch")
    require_equal(truth.get("input"), manifest.get("input"), "manifest/truth input mismatch")

    records = truth.get("records")
    if not isinstance(records, list) or len(records) != record_count:
        raise ValueError("truth record count mismatch")
    try:
        row_ids = [row["event_id"] for row in rows]
        truth_ids = [record["event_id"] for record in records]
    except (KeyError, TypeError) as error:
        raise ValueError("dataset/truth event_id missing") from error
    if row_ids != truth_ids or len(set(row_ids)) != record_count:
        raise ValueError("dataset/truth event_id mismatch")
    for row, record in zip(rows, records):
        try:
            expected_record = {
                "analysis_sha256": hashlib.sha256(canonical_bytes(row["attributes_analysis"])).hexdigest(),
                "canonical_sha256": hashlib.sha256(canonical_bytes(row)).hexdigest(),
                "raw_sha256": hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest(),
            }
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError("invalid dataset/truth record") from error
        for field, expected_hash in expected_record.items():
            if record.get(field) != expected_hash:
                raise ValueError(f"dataset/truth {field} mismatch")
    return rows, truth


def write_atomically(path, content):
    """在目标目录写入临时文件后原子替换目标文件。"""
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def contract_fields(manifest):
    """返回附带固定可比性语义的 run manifest 内容。"""
    result = dict(manifest)
    result["comparability_contract_version"] = COMPARABILITY_CONTRACT_VERSION
    result["comparability_contract"] = dict(COMPARABILITY_CONTRACT)
    result["data_path"] = DATA_PATH
    return result


def write_manifest_last(output_dir, manifest, artifacts):
    """原子发布 bytes artifact 和最终 run manifest；失败时发布诊断 manifest。"""
    output_dir = Path(output_dir)
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be a mapping of relative names to bytes")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "run-manifest.json"
    manifest_path.unlink(missing_ok=True)
    base_manifest = contract_fields(manifest)
    try:
        artifact_identities = {}
        for name, content in sorted(artifacts.items()):
            if not isinstance(name, str) or name in RESERVED_ARTIFACT_NAMES:
                raise ValueError("reserved artifact name")
            path = Path(name)
            if path.name != name or path.is_absolute() or not isinstance(content, bytes):
                raise ValueError("artifacts must map relative file names to bytes")
            artifact_path = output_dir / path
            write_atomically(artifact_path, content)
            artifact_identities[name] = file_identity(artifact_path)
        complete_manifest = dict(base_manifest)
        complete_manifest["artifacts"] = artifact_identities
        complete_manifest["status"] = "complete"
        complete_manifest.pop("error", None)
        write_atomically(manifest_path, canonical_bytes(complete_manifest) + b"\n")
    except Exception as error:
        failed_manifest = dict(base_manifest)
        failed_manifest["artifacts"] = {}
        failed_manifest["error"] = {"message": str(error), "type": type(error).__name__}
        failed_manifest["status"] = "failed"
        write_atomically(manifest_path, canonical_bytes(failed_manifest) + b"\n")
        raise
