#!/usr/bin/env python3
"""执行阶段三四布局主矩阵并发布可审计的应用可用计时。"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from assets import LocalAssetStore
from common import (
    LAYOUTS, LayoutAdapter, QueryResult, QuerySpec, TruthCatalog,
    build_layout_catalog, canonical_digest, load_truth,
)


LOGICAL_FIELDS = (
    "event_id", "trace_id", "project_id", "start_time", "profile",
    "content_type", "encoding", "content_length", "preview", "sha256", "payload",
)
EVENT_FIELDS = (
    "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id", "project_id",
    "start_time", "end_time", "duration_ms", "span_type", "framework", "level",
    "cohort", "profile", "content_type", "encoding", "content_length", "preview",
    "sha256", "payload_path",
)
SMOKE_TRUTH_FORMAT = "agent-trace-json-storage-stage3-smoke-truth"
SMOKE_GENERATION_FORMAT = "agent-trace-json-storage-stage3-smoke-generation"
FORMAL_GENERATION_FORMAT = "agent-trace-json-storage-stage3-generation"
WORKLOADS = (
    "main", "equal_total_few_large", "equal_total_many_medium", "correctness_only",
)
PAYLOAD_FIELDS = (
    "cohort", "profile", "content_type", "encoding", "content_length", "preview",
    "sha256", "payload_path",
)
FORMAL_SOURCE_MANIFEST = {
    "bytes": 2_397,
    "sha256": "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437",
}
FORMAL_SOURCE_ARTIFACTS = {
    "dataset.jsonl": {
        "bytes": 302_518_948,
        "sha256": "8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405",
    },
    "truth-manifest.json": {
        "bytes": 18_073_179,
        "sha256": "b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04",
    },
}
FORMAL_QUERY_WINDOW = {
    "project_id": "Leoxx/whowhen_pro",
    "start_time": "2030-01-01T00:00:00.000Z",
    "end_time": "2030-01-01T00:52:08.500Z",
    "page_size": 256,
    "row_count": 27_561,
}


@dataclass(frozen=True)
class QueryTruth:
    """保存一次逻辑查询的独立预期行集及顺序。"""

    scenario: str
    rows: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class ValidationResult:
    """保存客户端对行集、顺序及完整 payload bytes 的校验结果。"""

    row_count: int = 0
    event_ids: tuple[str, ...] = ()
    validated_payload_bytes: int = 0
    sha256: str | None = None
    batch_sha256: str | None = None


@dataclass(frozen=True)
class QuerySample:
    """保存从请求提交到应用可用的完整样本及分项。"""

    scenario: str
    kind: str
    status: str
    query_id: str
    response_bytes: int
    database_response_bytes: int
    resolver_payload_bytes: int
    database_protocol_bytes: int | None
    resolver_requests: int
    resolver_read_ms: float
    request_count: int
    peak_memory_bytes: int
    query_complete_ms: float
    recovery_ms: float
    validation_ms: float
    application_ready_ms: float
    validation: ValidationResult
    error: str | None = None


@dataclass(frozen=True)
class RunConfig:
    """保存一个 engine-layout-round 的固定测量配置。"""

    input_root: Path
    output: Path
    engine: str
    layout: str
    round_index: int
    round_order: tuple[str, ...]
    measurements: int = 30
    batch_measurements: int = 5
    query_ready_timeout: int = 60
    input_identity: dict[str, object] = field(default_factory=dict)
    command: tuple[str, ...] = ()
    asset_root: Path | None = None
    verified_events: tuple[dict[str, object], ...] = ()
    workload: str = "main"

    def __post_init__(self):
        if self.layout not in LAYOUTS:
            raise ValueError(f"unsupported layout: {self.layout}")
        if tuple(sorted(self.round_order)) != tuple(sorted(LAYOUTS)):
            raise ValueError("round_order must contain every layout exactly once")
        if not 0 <= self.round_index < len(LAYOUTS):
            raise ValueError("round_index must be in [0, 3]")
        for name, value in (
            ("measurements", self.measurements),
            ("batch_measurements", self.batch_measurements),
            ("query_ready_timeout", self.query_ready_timeout),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.workload not in WORKLOADS:
            raise ValueError(f"unsupported workload: {self.workload}")


@dataclass(frozen=True)
class RunResult:
    """返回一个 round 的最终 manifest、正式样本和汇总。"""

    manifest: dict[str, object]
    samples: tuple[QuerySample, ...]
    summary: dict[str, object]


def _json_bytes(value):
    """返回确定性、保留 Unicode 的 JSON 文档 bytes。"""
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _write_bytes_atomic(path, content):
    """在目标目录内完成 fsync 和原子替换。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_manifest_atomic(output: Path, manifest: dict[str, object]) -> None:
    """原子发布 running、failed 或 complete run manifest。"""
    if not isinstance(manifest, dict) or manifest.get("status") not in {
        "running", "failed", "complete",
    }:
        raise ValueError("manifest status must be running, failed, or complete")
    _write_bytes_atomic(Path(output), _json_bytes(manifest))


def latin_square(layouts=LAYOUTS):
    """返回四阶循环 Latin square，每种布局覆盖每个运行位置一次。"""
    layouts = tuple(layouts)
    if len(layouts) != 4 or len(set(layouts)) != 4:
        raise ValueError("Latin square requires four distinct layouts")
    return tuple(layouts[offset:] + layouts[:offset] for offset in range(4))


def validate_formal_contract(generation, truth, measurements, batch_measurements):
    """强制正式矩阵使用冻结生成格式、来源、规模、窗口和 30/5 重复。"""
    if generation.get("format") != FORMAL_GENERATION_FORMAT or generation.get("format_version") != 1:
        raise ValueError("formal generation format mismatch")
    if (measurements, batch_measurements) != (30, 5):
        raise ValueError("formal performance workloads require 30/5 measurements")
    if not isinstance(truth, TruthCatalog):
        return
    expected_watermarks = tuple(list(range(256, 48_534, 256)) + [48_534])
    source = truth.source
    if (
        truth.seed != 20260907 or truth.record_count != 48_534
        or truth.block_size != 256 or truth.block_count != 190
        or truth.watermarks != expected_watermarks
        or truth.query_window != FORMAL_QUERY_WINDOW
        or source.get("manifest") != FORMAL_SOURCE_MANIFEST
        or source.get("artifacts") != FORMAL_SOURCE_ARTIFACTS
    ):
        raise ValueError("formal frozen source contract mismatch")


def _workload_accepts(row, workload):
    """判断事件 payload 是否属于当前隔离 workload。"""
    if workload == "main":
        return row["cohort"] == "main"
    if workload == "equal_total_few_large":
        return row["cohort"] == "equal_total_control" and row["profile"] == "text_2m"
    if workload == "equal_total_many_medium":
        return row["cohort"] == "equal_total_control" and row["profile"] == "text_64k"
    if workload == "correctness_only":
        return row["cohort"] == "correctness_only"
    raise ValueError(f"unsupported workload: {workload}")


def build_workload_events(events, workload):
    """保留全部 identity/block，并把非目标 payload 字段投影为 SQL NULL。"""
    if workload not in WORKLOADS:
        raise ValueError(f"unsupported workload: {workload}")
    projected = []
    for source in events:
        row = dict(source)
        if not _workload_accepts(source, workload):
            row.update({field_name: None for field_name in PAYLOAD_FIELDS})
        elif workload.startswith("equal_total_"):
            row["cohort"] = workload
        projected.append(row)
    return tuple(projected)


def validate_workload_contract(events, workload, input_kind):
    """核对隔离 workload 的 payload 数量和原始 bytes。"""
    selected = [row for row in events if row["payload_path"] is not None]
    actual = (len(selected), sum(row["content_length"] for row in selected))
    formal = {
        "main": (160, 128_450_560),
        "equal_total_few_large": (40, 83_886_080),
        "equal_total_many_medium": (1_280, 83_886_080),
        "correctness_only": (1, 1_024),
    }
    if input_kind == "formal" and actual != formal[workload]:
        raise ValueError(f"formal workload contract mismatch: {workload}")
    if input_kind == "smoke" and actual[0] < 1:
        raise ValueError(f"smoke workload is empty: {workload}")
    return {"payload_count": actual[0], "raw_payload_bytes": actual[1]}


def validate_watermark_keys(layout, watermarks, minimum):
    """要求水位 key 精确覆盖布局写目标且全部达到指定位置。"""
    expected = set(build_layout_catalog(layout).write_tables)
    if set(watermarks) != expected:
        raise RuntimeError("watermark keys do not match layout write targets")
    if any(not isinstance(value, int) or value < minimum for value in watermarks.values()):
        raise RuntimeError(f"joint watermark incomplete at {minimum}")


def _validate_query_rows(actual_rows, expected_rows):
    """在客户端逐字段核对行顺序、内容元数据和完整 payload bytes。"""
    if len(actual_rows) != len(expected_rows):
        raise ValueError(f"row set mismatch: expected {len(expected_rows)}, got {len(actual_rows)}")
    payload_bytes = 0
    payload_digests = []
    batch_digest = hashlib.sha256()
    event_ids = []
    for position, (actual, expected) in enumerate(zip(actual_rows, expected_rows)):
        if set(actual) != set(LOGICAL_FIELDS):
            raise ValueError(f"logical fields mismatch at row {position}")
        if actual["event_id"] != expected["event_id"]:
            raise ValueError(f"row order mismatch at row {position}")
        event_ids.append(actual["event_id"])
        for field_name in LOGICAL_FIELDS[:-1]:
            if actual[field_name] != expected[field_name]:
                raise ValueError(f"{field_name} mismatch: {actual['event_id']}")
        expected_payload = expected["payload"]
        actual_payload = actual["payload"]
        if expected_payload is None:
            if actual_payload is not None:
                raise ValueError(f"unexpected payload bytes: {actual['event_id']}")
            continue
        if not isinstance(actual_payload, bytes):
            raise ValueError(f"payload bytes missing: {actual['event_id']}")
        digest = hashlib.sha256(actual_payload).hexdigest()
        if len(actual_payload) != expected["content_length"] or digest != expected["sha256"]:
            raise ValueError(f"payload bytes mismatch: {actual['event_id']}")
        if actual_payload != expected_payload:
            raise ValueError(f"payload content mismatch: {actual['event_id']}")
        payload_bytes += len(actual_payload)
        payload_digests.append(digest)
        batch_digest.update(actual_payload)
    return ValidationResult(
        row_count=len(actual_rows), event_ids=tuple(event_ids),
        validated_payload_bytes=payload_bytes,
        sha256=payload_digests[0] if len(payload_digests) == 1 else None,
        batch_sha256=batch_digest.hexdigest() if payload_digests else None,
    )


def measure_query(adapter: LayoutAdapter, query: QuerySpec, truth: QueryTruth) -> QuerySample:
    """执行一次查询，在应用可用边界内完成客户端长度和 SHA-256 校验。"""
    started = time.perf_counter()
    result = None
    validation = ValidationResult()
    validation_ms = 0.0
    error = None
    try:
        tracemalloc.start()
        result = adapter.run_query(query)
        if not isinstance(result, QueryResult):
            raise ValueError("adapter returned an invalid QueryResult")
        if (
            result.response_bytes <= 0
            or result.database_response_bytes < 0
            or result.resolver_payload_bytes < 0
            or result.response_bytes
            != result.database_response_bytes + result.resolver_payload_bytes
        ):
            raise ValueError("response byte accounting mismatch")
        validation_started = time.perf_counter()
        try:
            validation = _validate_query_rows(result.rows, truth.rows)
        finally:
            validation_ms = (time.perf_counter() - validation_started) * 1000
        if result.response_bytes < validation.validated_payload_bytes:
            raise ValueError("response bytes exclude returned payload bytes")
    except Exception as exception:
        error = str(exception) or type(exception).__name__
    finally:
        _, peak_memory_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    application_ms = (time.perf_counter() - started) * 1000
    query_complete_ms = result.query_complete_ms if result is not None else 0.0
    recovery_ms = result.recovery_ms if result is not None else 0.0
    # 真实 adapter 的外层 wall time 必然覆盖分项；max 仅吸收测试替身和计时精度差异。
    application_ms = max(application_ms, query_complete_ms + recovery_ms + validation_ms)
    return QuerySample(
        scenario=truth.scenario,
        kind=query.kind,
        status="success" if error is None else "failed",
        query_id=result.query_id if result is not None else "",
        response_bytes=result.response_bytes if result is not None else 0,
        database_response_bytes=result.database_response_bytes if result is not None else 0,
        resolver_payload_bytes=result.resolver_payload_bytes if result is not None else 0,
        database_protocol_bytes=result.database_protocol_bytes if result is not None else None,
        resolver_requests=result.resolver_requests if result is not None else 0,
        resolver_read_ms=result.resolver_read_ms if result is not None else 0.0,
        request_count=(1 + result.resolver_requests) if result is not None else 1,
        peak_memory_bytes=peak_memory_bytes,
        query_complete_ms=query_complete_ms,
        recovery_ms=recovery_ms,
        validation_ms=validation_ms,
        application_ready_ms=application_ms,
        validation=validation,
        error=error,
    )


def _percentile(values, fraction):
    """以线性插值返回小样本也定义明确的百分位数。"""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires values")
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _distribution(values):
    """返回轮内中位数、p95 和范围。"""
    return {
        "minimum": min(values),
        "median": statistics.median(values),
        "p95": _percentile(values, 0.95),
        "maximum": max(values),
    }


def summarize_samples(samples):
    """保留失败计数，并仅以成功样本计算时延、bytes 和吞吐。"""
    grouped = {}
    for sample in samples:
        grouped.setdefault(sample.scenario, []).append(sample)
    summaries = {}
    for scenario, group in grouped.items():
        successful = [sample for sample in group if sample.status == "success"]
        summary = {
            "sample_count": len(group),
            "successful_samples": len(successful),
            "failed_samples": len(group) - len(successful),
            "latency_ms": _distribution(
                [sample.application_ready_ms for sample in successful]
            ) if successful else None,
            "query_complete_ms": _distribution(
                [sample.query_complete_ms for sample in successful]
            ) if successful else None,
            "recovery_ms": _distribution(
                [sample.recovery_ms for sample in successful]
            ) if successful else None,
            "validation_ms": _distribution(
                [sample.validation_ms for sample in successful]
            ) if successful else None,
            "response_bytes": {
                "database": sum(sample.database_response_bytes for sample in successful),
                "resolver_payload": sum(sample.resolver_payload_bytes for sample in successful),
                "total": sum(sample.response_bytes for sample in successful),
                "validated_payload": sum(
                    sample.validation.validated_payload_bytes for sample in successful
                ),
                "database_protocol": [
                    sample.database_protocol_bytes for sample in successful
                    if sample.database_protocol_bytes is not None
                ] or "unavailable",
            },
            "resolver": {
                "requests": sum(sample.resolver_requests for sample in successful),
                "read_ms": sum(sample.resolver_read_ms for sample in successful),
            },
            "request_count": sum(sample.request_count for sample in successful),
            "peak_memory_bytes": max(
                (sample.peak_memory_bytes for sample in successful), default=0,
            ),
        }
        if scenario.startswith("batch:"):
            rates = [
                sample.validation.validated_payload_bytes / (1024 * 1024)
                / (sample.application_ready_ms / 1000)
                for sample in successful
                if sample.application_ready_ms > 0
            ]
            summary["throughput_mib_s"] = _distribution(rates) if rates else None
        summaries[scenario] = summary
    return summaries


def _file_identity(path):
    """返回单个输入文件的 bytes 和 SHA-256。"""
    content = Path(path).read_bytes()
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _payload_record_from_dict(value):
    """将独立 smoke truth 记录转换为公共 PayloadRecord。"""
    from common import PayloadRecord

    return PayloadRecord(**value)


def _load_smoke_truth(path):
    """读取显式 smoke schema；正式 truth 始终交给严格 load_truth。"""
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid smoke truth") from error
    required = {
        "format", "format_version", "seed", "source", "record_count", "block_size",
        "block_count", "watermarks", "identity_sha256", "query_window", "payloads",
        "cohorts", "representative_traces", "detail_samples",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["format"] != SMOKE_TRUTH_FORMAT
        or value["format_version"] != 1
        or value["source"].get("fixture") != "stage3-layout-matrix-smoke"
    ):
        raise ValueError("smoke truth contract mismatch")
    payloads = tuple(_payload_record_from_dict(record) for record in value["payloads"])
    by_event = {record.event_id: record for record in payloads}
    detail = tuple(_payload_record_from_dict(record) for record in value["detail_samples"])
    if len(by_event) != 7 or any(by_event.get(record.event_id) != record for record in detail):
        raise ValueError("smoke payload contract mismatch")
    truth = TruthCatalog(
        seed=value["seed"], source=value["source"], record_count=value["record_count"],
        block_size=value["block_size"], block_count=value["block_count"],
        watermarks=tuple(value["watermarks"]), identity_sha256=value["identity_sha256"],
        query_window=value["query_window"], payloads=payloads, cohorts=value["cohorts"],
        representative_traces=value["representative_traces"], detail_samples=detail,
    )
    if (
        truth.record_count != 8 or truth.block_size != 2 or truth.block_count != 4
        or truth.watermarks != (2, 4, 6, 8)
        or tuple(record.profile for record in truth.detail_samples)
        != ("text_64k", "text_512k", "text_2m", "entropy_512k")
    ):
        raise ValueError("smoke truth contract mismatch")
    for record in payloads:
        payload = (Path(path).parent / record.payload_path).read_bytes()
        if (
            len(payload) != record.content_length
            or hashlib.sha256(payload).hexdigest() != record.sha256
            or payload.decode(record.encoding)[:200] != record.preview
        ):
            raise ValueError(f"smoke payload identity mismatch: {record.event_id}")
        json.loads(payload)
    return truth


def _load_events(path):
    """完整读取 events.jsonl，拒绝空行和非对象记录。"""
    rows = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            raise ValueError(f"empty event row: {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid event row: {line_number}") from error
        if not isinstance(row, dict) or set(row) != set(EVENT_FIELDS):
            raise ValueError(f"invalid event row: {line_number}")
        rows.append(row)
    return tuple(rows)


def _validate_input(truth, events, input_root, validate_payloads=True):
    """核对 events 顺序、block、水位、identity 及 payload 归属。"""
    if len(events) != truth.record_count:
        raise ValueError("event count mismatch")
    if tuple(row["ingest_seq"] for row in events) != tuple(range(len(events))):
        raise ValueError("ingest order mismatch")
    expected_watermarks = tuple(
        list(range(truth.block_size, truth.record_count, truth.block_size))
        + [truth.record_count]
    )
    if truth.watermarks != expected_watermarks or truth.block_count != len(expected_watermarks):
        raise ValueError("block boundary mismatch")
    identity = canonical_digest([
        [row["event_id"], row["trace_id"], row["project_id"], row["start_time"]]
        for row in events
    ])
    if identity != truth.identity_sha256:
        raise ValueError("event identity mismatch")
    by_event = {record.event_id: record for record in truth.payloads}
    if len(by_event) != len(truth.payloads):
        raise ValueError("duplicate payload truth")
    seen_payloads = set()
    for row in events:
        record = by_event.get(row["event_id"])
        expected = {
            field_name: getattr(record, field_name) if record is not None else None
            for field_name in (
                "cohort", "profile", "content_type", "encoding", "content_length",
                "preview", "sha256", "payload_path",
            )
        }
        if any(row[field_name] != value for field_name, value in expected.items()):
            raise ValueError(f"payload ownership mismatch: {row['event_id']}")
        if record is not None:
            seen_payloads.add(record.event_id)
    if seen_payloads != set(by_event):
        raise ValueError("payload truth contains unknown event")
    window = truth.query_window
    window_rows = [row for row in events if (
        row["project_id"] == window["project_id"]
        and window["start_time"] <= row["start_time"] < window["end_time"]
    )]
    if len(window_rows) != window["row_count"]:
        raise ValueError("query window row count mismatch")
    if not validate_payloads:
        return
    input_root = Path(input_root).resolve()
    for record in truth.payloads:
        path = (input_root / record.payload_path).resolve()
        try:
            path.relative_to(input_root)
        except ValueError as error:
            raise ValueError("payload path escapes input root") from error
        payload = path.read_bytes()
        if len(payload) != record.content_length or hashlib.sha256(payload).hexdigest() != record.sha256:
            raise ValueError(f"payload identity mismatch: {record.event_id}")


def load_run_input(input_root):
    """加载正式或显式 smoke 输入，并返回可写入 manifest 的独立身份。"""
    input_root = Path(input_root).resolve()
    truth_path = input_root / "truth.json"
    generation_path = input_root / "generation-manifest.json"
    try:
        generation = json.loads(generation_path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid generation manifest") from error
    if generation.get("status") != "complete":
        raise ValueError("input generation is incomplete")
    if generation.get("format") == SMOKE_GENERATION_FORMAT:
        truth = _load_smoke_truth(truth_path)
        input_kind = "smoke"
    elif generation.get("format") == FORMAL_GENERATION_FORMAT:
        truth = load_truth(truth_path)
        input_kind = "formal"
    else:
        raise ValueError("unknown generation format")
    events = _load_events(input_root / "events.jsonl")
    _validate_input(truth, events, input_root)
    generation_contract = {
        "seed": truth.seed,
        "record_count": truth.record_count,
        "block_size": truth.block_size,
        "block_count": truth.block_count,
        "watermarks": list(truth.watermarks),
    }
    if any(generation.get(name) != value for name, value in generation_contract.items()):
        raise ValueError("generation contract disagrees with truth")
    if generation.get("identity_sha256", truth.identity_sha256) != truth.identity_sha256:
        raise ValueError("generation contract disagrees with truth")
    identity = {
        "kind": input_kind,
        "generation_manifest": _file_identity(generation_path),
        "truth": _file_identity(truth_path),
        "events": _file_identity(input_root / "events.jsonl"),
        "identity_sha256": truth.identity_sha256,
    }
    artifacts = generation.get("artifacts", {})
    for name in ("truth.json", "events.jsonl"):
        if artifacts.get(name) != identity["truth" if name == "truth.json" else "events"]:
            raise ValueError(f"generation artifact identity mismatch: {name}")
    return truth, events, identity


def _smoke_payload(event_id, profile):
    """生成体积很小但包含 Unicode 且每条唯一的有效 JSON bytes。"""
    return _json_bytes({
        "event_id": event_id,
        "profile": profile,
        "content": f"smoke payload {event_id} 中文",
    }).rstrip(b"\n")


def build_smoke_input(output):
    """生成独立 smoke 输入；其格式身份不会被正式 load_truth 接受。"""
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("smoke output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    profiles = ("text_64k", "text_512k", "text_2m", "entropy_512k")
    payload_events = {
        0: ("main", profiles[0]), 1: ("main", profiles[1]),
        2: ("main", profiles[2]), 3: ("main", profiles[3]),
        4: ("equal_total_control", "text_2m"),
        5: ("equal_total_control", "text_64k"),
        6: ("correctness_only", "unicode_boundary"),
    }
    trace_ids = ("trace-short", "trace-mid", "trace-mid") + ("trace-long",) * 5
    events = []
    payload_records = []
    for index in range(8):
        event_id = f"smoke-event-{index:02d}"
        assignment = payload_events.get(index)
        cohort, profile = assignment if assignment else (None, None)
        payload = _smoke_payload(event_id, profile) if profile else None
        payload_record = None
        if payload is not None:
            digest = hashlib.sha256(payload).hexdigest()
            path = f"payloads/{digest}.json"
            _write_bytes_atomic(output / path, payload)
            payload_record = {
                "event_id": event_id, "trace_id": trace_ids[index],
                "project_id": "stage3-smoke", "start_time": f"2030-01-01T00:00:0{index}.000Z",
                "cohort": cohort, "profile": profile, "content_type": "application/json",
                "encoding": "utf-8", "content_length": len(payload),
                "preview": payload.decode("utf-8")[:200], "sha256": digest,
                "payload_path": path,
            }
            payload_records.append(payload_record)
        event = {
            "ingest_seq": index, "event_id": event_id, "trace_id": trace_ids[index],
            "span_id": f"smoke-span-{index:02d}",
            "parent_span_id": None if index in {0, 1, 3} else f"smoke-span-{index - 1:02d}",
            "project_id": "stage3-smoke", "start_time": f"2030-01-01T00:00:0{index}.000Z",
            "end_time": f"2030-01-01T00:00:0{index}.500Z", "duration_ms": 500,
            "span_type": "llm", "framework": "stage3-smoke", "level": "INFO",
            "cohort": payload_record["cohort"] if payload_record else None,
            "profile": payload_record["profile"] if payload_record else None,
            "content_type": payload_record["content_type"] if payload_record else None,
            "encoding": payload_record["encoding"] if payload_record else None,
            "content_length": payload_record["content_length"] if payload_record else None,
            "preview": payload_record["preview"] if payload_record else None,
            "sha256": payload_record["sha256"] if payload_record else None,
            "payload_path": payload_record["payload_path"] if payload_record else None,
        }
        events.append(event)
    identity_sha = canonical_digest([
        [row["event_id"], row["trace_id"], row["project_id"], row["start_time"]]
        for row in events
    ])
    by_trace = {
        "p25": ("trace-short", 1), "p50": ("trace-mid", 2), "p95": ("trace-long", 5),
    }
    representative = {}
    for label, (trace_id, span_count) in by_trace.items():
        selected = [record for record in payload_records if record["trace_id"] == trace_id]
        representative[label] = {
            "trace_id": trace_id, "span_count": span_count,
            "payload_count": len(selected),
            "profile_counts": {
                profile: sum(record["profile"] == profile for record in selected)
                for profile in profiles if any(record["profile"] == profile for record in selected)
            },
            "raw_payload_bytes": sum(record["content_length"] for record in selected),
        }
    truth = {
        "format": SMOKE_TRUTH_FORMAT, "format_version": 1, "seed": 20260907,
        "source": {"fixture": "stage3-layout-matrix-smoke", "identity": identity_sha},
        "record_count": 8, "block_size": 2, "block_count": 4,
        "watermarks": [2, 4, 6, 8], "identity_sha256": identity_sha,
        "query_window": {
            "project_id": "stage3-smoke", "start_time": "2030-01-01T00:00:00.000Z",
            "end_time": "2030-01-01T00:00:08.000Z", "page_size": 2, "row_count": 8,
        },
        "payloads": payload_records,
        "cohorts": {
            name: {
                "performance": name != "correctness_only",
                "payload_count": len(selected := [
                    record for record in payload_records if record["cohort"] == name
                ]),
                "profile_counts": {
                    profile: sum(record["profile"] == profile for record in selected)
                    for profile in {record["profile"] for record in selected}
                },
                "raw_payload_bytes": sum(record["content_length"] for record in selected),
            }
            for name in ("main", "equal_total_control", "correctness_only")
        },
        "representative_traces": representative,
        "detail_samples": payload_records[:4],
    }
    events_bytes = b"".join(_json_bytes(row) for row in events)
    truth_bytes = _json_bytes(truth)
    _write_bytes_atomic(output / "events.jsonl", events_bytes)
    _write_bytes_atomic(output / "truth.json", truth_bytes)
    generation = {
        "format": SMOKE_GENERATION_FORMAT, "format_version": 1, "status": "complete",
        "seed": 20260907, "record_count": 8, "block_size": 2, "block_count": 4,
        "watermarks": [2, 4, 6, 8], "identity_sha256": identity_sha,
        "artifacts": {
            "events.jsonl": {"bytes": len(events_bytes), "sha256": hashlib.sha256(events_bytes).hexdigest()},
            "truth.json": {"bytes": len(truth_bytes), "sha256": hashlib.sha256(truth_bytes).hexdigest()},
        },
    }
    write_manifest_atomic(output / "generation-manifest.json", generation)
    return generation


def _expected_row(row, input_root, kind):
    """从冻结事件独立构造 adapter 公共查询行的 truth 表示。"""
    result = {field_name: row[field_name] for field_name in LOGICAL_FIELDS[:-1]}
    if kind == "list":
        result["preview"] = None
    path = row["payload_path"]
    result["payload"] = (
        (Path(input_root) / path).read_bytes()
        if path is not None and kind in {"detail", "trace", "batch"}
        else None
    )
    return result


def _expected_target_audits(layout, events):
    """按物理目标列构造独立于 adapter 的有序审计预期。"""
    event_fields = (
        "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
        "project_id", "start_time", "end_time", "duration_ms", "span_type",
        "framework", "level", "cohort", "profile", "content_type", "encoding",
        "content_length", "preview", "sha256",
    )
    payload_fields = (
        "ingest_seq", "event_id", "trace_id", "project_id", "start_time",
        "profile", "content_type", "encoding", "content_length", "preview", "sha256",
    )

    def project(fields, rows):
        return tuple({field_name: row[field_name] for field_name in fields} for row in rows)

    all_events = project(event_fields, events)
    payload_events = tuple(row for row in events if row["sha256"] is not None)
    asset_rows = []
    asset_ids = set()
    for row in payload_events:
        if row["sha256"] in asset_ids:
            continue
        asset_ids.add(row["sha256"])
        asset_rows.append({
            "asset_id": row["sha256"], "sha256": row["sha256"],
            "content_type": row["content_type"], "encoding": row["encoding"],
            "content_length": row["content_length"], "status": "available",
        })
    if layout == "same_table":
        return {"events": all_events}
    if layout == "separate":
        return {
            "events_analytics": all_events,
            "event_payloads": project(payload_fields, events),
        }
    if layout == "full_core":
        return {"events_full": all_events, "events_core": all_events}
    return {
        "events_analytics": tuple(
            {**row, "asset_id": source["sha256"]}
            for row, source in zip(all_events, events)
        ),
        "assets": tuple(asset_rows),
    }


def _validate_dataset_audit(audit, events, input_root, layout):
    """对逻辑重建和每个物理写目标执行精确全量审计。"""
    if audit.duplicate_event_ids != 0 or len(audit.rows) != len(events):
        raise RuntimeError("dataset audit row count or duplicate mismatch")
    expected = tuple(
        {"ingest_seq": row["ingest_seq"], **_expected_row(row, input_root, "batch")}
        for row in events
    )
    if audit.rows != expected:
        raise RuntimeError("dataset audit identity/order/payload mismatch")
    if audit.logical_response_bytes <= 0:
        raise RuntimeError("dataset audit response bytes missing")
    expected_targets = _expected_target_audits(layout, events)
    if set(audit.target_audits) != set(expected_targets):
        raise RuntimeError("physical target audit target set mismatch")
    target_evidence = {}
    for target, expected_rows in expected_targets.items():
        actual = audit.target_audits[target]
        if actual.duplicate_identities != 0 or actual.rows != expected_rows:
            raise RuntimeError(f"physical target audit mismatch: {target}")
        expected_mappings = ()
        if target == "assets":
            expected_mappings = tuple({
                "ingest_seq": row["ingest_seq"], "event_id": row["event_id"],
                "asset_id": row["sha256"],
            } for row in events if row["sha256"] is not None)
            if actual.event_mappings != expected_mappings:
                raise RuntimeError("physical target audit asset mapping mismatch")
        target_evidence[target] = {
            "row_count": len(actual.rows),
            "duplicate_identities": actual.duplicate_identities,
            "identity_metadata_sha256": canonical_digest(actual.rows),
        }
        if target == "assets":
            target_evidence[target]["event_mapping_count"] = len(actual.event_mappings)
            target_evidence[target]["event_mapping_sha256"] = canonical_digest(actual.event_mappings)
    return {
        "row_count": len(audit.rows),
        "duplicate_event_ids": audit.duplicate_event_ids,
        "identity_sha256": canonical_digest([
            [row["event_id"], row["trace_id"], row["project_id"], row["start_time"]]
            for row in audit.rows
        ]),
        "payload_count": sum(row["payload"] is not None for row in audit.rows),
        "payload_bytes": sum(len(row["payload"] or b"") for row in audit.rows),
        "logical_response_bytes": audit.logical_response_bytes,
        "database_protocol_bytes": audit.database_protocol_bytes
        if audit.database_protocol_bytes is not None else "unavailable",
        "physical_targets": target_evidence,
    }


def _add_millisecond(timestamp):
    """将毫秒 UTC ISO 时间增加一毫秒，形成 Trace 查询开区间上界。"""
    value = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return (value + timedelta(milliseconds=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def workload_query_cases(events, truth, input_root, workload="main"):
    """构造固定首/中页、四详情、三 Trace 和主 cohort 批量 truth。"""
    window = truth.query_window
    window_rows = sorted([
        row for row in events if row["project_id"] == window["project_id"]
        and window["start_time"] <= row["start_time"] < window["end_time"]
    ], key=lambda row: (row["start_time"], row["event_id"]))
    page_size = window["page_size"]
    middle_offset = max(0, (len(window_rows) - page_size) // 2)
    page_definitions = (
        ("first", 0, "1900-01-01T00:00:00.000Z", ""),
        (
            "middle", middle_offset,
            window_rows[middle_offset - 1]["start_time"] if middle_offset else "1900-01-01T00:00:00.000Z",
            window_rows[middle_offset - 1]["event_id"] if middle_offset else "",
        ),
    )
    cases = []
    page_kinds = ("list", "preview") if workload == "main" else ("list",)
    for kind in page_kinds:
        for label, offset, cursor_time, cursor_id in page_definitions:
            selected = window_rows[offset:offset + page_size]
            query = QuerySpec(kind, {
                "project_id": window["project_id"], "start_time": window["start_time"],
                "end_time": window["end_time"], "cursor_time": cursor_time,
                "cursor_id": cursor_id, "page_size": page_size,
            })
            cases.append((query, QueryTruth(
                f"{kind}:{label}",
                tuple(_expected_row(row, input_root, kind) for row in selected),
            )))
    by_event = {row["event_id"]: row for row in events}
    if workload == "main":
        detail_records = truth.detail_samples
    else:
        detail_candidates = [row for row in events if row["payload_path"] is not None]
        detail_records = detail_candidates[:1]
    for record in detail_records:
        record_event_id = record.event_id if hasattr(record, "event_id") else record["event_id"]
        record_profile = record.profile if hasattr(record, "profile") else record["profile"]
        row = by_event[record_event_id]
        query = QuerySpec("detail", {
            "project_id": row["project_id"], "trace_id": row["trace_id"],
            "start_time": row["start_time"], "event_id": row["event_id"],
        })
        cases.append((query, QueryTruth(
            f"detail:{record_profile}", (_expected_row(row, input_root, "detail"),),
        )))
    for label in (("p25", "p50", "p95") if workload == "main" else ()):
        trace_id = truth.representative_traces[label]["trace_id"]
        selected = sorted(
            [row for row in events if row["trace_id"] == trace_id],
            key=lambda row: (row["start_time"], row["event_id"]),
        )
        if not selected or len(selected) != truth.representative_traces[label]["span_count"]:
            raise ValueError(f"representative Trace mismatch: {label}")
        projects = {row["project_id"] for row in selected}
        if len(projects) != 1:
            raise ValueError(f"representative Trace crosses projects: {label}")
        query = QuerySpec("trace", {
            "project_id": selected[0]["project_id"], "trace_id": trace_id,
            "start_time": selected[0]["start_time"],
            "end_time": _add_millisecond(selected[-1]["start_time"]),
        })
        cases.append((query, QueryTruth(
            f"trace:{label}", tuple(_expected_row(row, input_root, "trace") for row in selected),
        )))
    batch_cohort = workload if workload.startswith("equal_total_") else workload
    selected = sorted(
        [row for row in events if row["cohort"] == batch_cohort and row["sha256"] is not None],
        key=lambda row: (row["start_time"], row["event_id"]),
    )
    cases.append((QuerySpec("batch", {"cohort": batch_cohort}), QueryTruth(
        f"batch:{workload}", tuple(_expected_row(row, input_root, "batch") for row in selected),
    )))
    return tuple(cases)


def _query_cases(events, truth, input_root):
    """保留 Task 4 初始内部入口，等价于 main workload catalog。"""
    return workload_query_cases(events, truth, input_root, "main")


def _host_evidence():
    """返回不会修改宿主状态的最小资源身份。"""
    memory_kib = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                memory_kib = int(line.split()[1])
                break
    except (OSError, ValueError):
        pass
    return {
        "platform": platform.platform(), "machine": platform.machine(),
        "cpu_count": os.cpu_count(), "memory_total_kib": memory_kib,
    }


def _container_evidence(container_name):
    """读取容器镜像名和不可变 image ID，不读取凭据。"""
    completed = subprocess.run(
        ["docker", "inspect", container_name, "--format", "{{.Config.Image}}|{{.Image}}"],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0 or "|" not in completed.stdout:
        return {"container": container_name, "image": None, "image_id": None}
    image, image_id = completed.stdout.strip().split("|", 1)
    return {"container": container_name, "image": image, "image_id": image_id}


def _code_evidence(adapter):
    """记录 runner、公共契约、Asset 和当前 adapter 源文件身份。"""
    modules = {
        "runner": sys.modules[__name__],
        "common": sys.modules[TruthCatalog.__module__],
        "assets": sys.modules[LocalAssetStore.__module__],
        "adapter": sys.modules[type(adapter).__module__],
    }
    evidence = {}
    for name, module in modules.items():
        module_path = Path(module.__file__).resolve()
        evidence[name] = {"path": str(module_path), **_file_identity(module_path)}
    return evidence


def _engine_runtime(adapter, engine):
    """从目标服务读取版本；采集失败会阻止 complete manifest。"""
    if engine == "fake":
        return {"version": "test-double", "source": "unit-test"}
    connection = adapter.connect_worker()
    try:
        if engine == "opengauss":
            version = connection.execute("SELECT version()").fetchone()[0]
        elif engine == "clickhouse":
            rows = adapter._json_rows(adapter._request(
                connection, "SELECT version() AS version FORMAT JSONEachRow",
            ))
            version = rows[0]["version"] if rows else None
        else:
            raise ValueError(f"unsupported engine: {engine}")
    finally:
        connection.close()
    if not isinstance(version, str) or not version:
        raise RuntimeError("database version evidence is missing")
    return {"version": version, "source": "database-query"}


def _as_json(value):
    """递归将 dataclass 和 tuple 转为 JSON 可序列化对象。"""
    if hasattr(value, "__dataclass_fields__"):
        return {key: _as_json(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _as_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_as_json(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _write_samples(path, samples):
    """以 JSONL 保存每个正式样本，失败样本也完整保留。"""
    content = b"".join(_json_bytes(_as_json(sample)) for sample in samples)
    _write_bytes_atomic(path, content)


def _validate_access(engine, layout, input_kind, samples, access):
    """机检声明来源、payload 投影、实际 rows/bytes 和正式访问结构。"""
    sample_by_id = {sample.query_id: sample for sample in samples}
    if set(access.query_details) != set(sample_by_id):
        raise RuntimeError("access details do not cover every formal query")
    validation = {}
    for query_id, sample in sample_by_id.items():
        detail = access.query_details[query_id]
        declared = detail.get("declared_source")
        plan = access.plans[query_id]
        if (
            detail.get("kind") != sample.kind
            or not declared or declared.lower() not in detail.get("statement", "").lower()
            or detail.get("payload_selected") != (sample.kind in {"detail", "trace", "batch"})
            or sample.validation.row_count < 0 or sample.response_bytes <= 0
        ):
            raise RuntimeError(f"access contract mismatch: {query_id}")
        structure_observed = (
            "index" in plan.lower() or "mergetree" in plan.lower()
            or any(value > 0 for value in access.index_scans.values())
        )
        if input_kind == "formal" and not structure_observed:
            raise RuntimeError(f"declared access structure not observed: {query_id}")
        validation[query_id] = {
            "scenario": sample.scenario, "kind": sample.kind,
            "declared_source": declared, "payload_selected": detail["payload_selected"],
            "result_rows": sample.validation.row_count,
            "logical_response_bytes": sample.response_bytes,
            "scanned_rows": detail.get("scanned_rows"),
            "scanned_bytes": detail.get("scanned_bytes"),
            "scanned_bytes_status": detail.get("scanned_bytes_status", "unavailable"),
            "structure_observed": structure_observed,
            "mode": "formal" if input_kind == "formal" else "smoke-sequential-scan-allowed",
        }
    return validation


def _evidence_gate(engine, layout, input_kind, samples, access, storage, cleanup):
    """验证完成 manifest 所需的结果字节、访问、物理状态和清理证据。"""
    failed = [sample for sample in samples if sample.status != "success"]
    if failed:
        raise RuntimeError(f"{len(failed)} formal query samples failed validation")
    query_ids = [sample.query_id for sample in samples]
    if any(not query_id for query_id in query_ids) or set(access.plans) != set(query_ids):
        raise RuntimeError("access plans do not cover every formal query")
    if engine == "clickhouse" and set(access.query_finish) != set(query_ids):
        raise RuntimeError("QueryFinish does not cover every formal query")
    if engine == "opengauss" and not access.index_scans:
        raise RuntimeError("openGauss index scan evidence is missing")
    access_validation = _validate_access(engine, layout, input_kind, samples, access)
    expected_tables = set(build_layout_catalog(layout).write_tables)
    if set(storage.tables) != expected_tables:
        raise RuntimeError("storage evidence does not cover every write target")
    if layout == "asset_ref" and storage.asset_store is None:
        raise RuntimeError("Asset storage evidence is missing")
    if not cleanup.removed:
        raise RuntimeError("cleanup was not confirmed")
    return access_validation


def run_layout(adapter: LayoutAdapter, truth: TruthCatalog, config: RunConfig) -> RunResult:
    """载入、维护、测量并清理一个 engine-layout-round，原子发布最终状态。"""
    output = Path(config.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "run-manifest.json"
    run_id = f"jsons3-{config.engine}-{config.layout}-r{config.round_index + 1}-{uuid.uuid4().hex[:10]}"
    base_manifest = {
        "format": "agent-trace-json-storage-stage3-layout-run", "format_version": 1,
        "run_id": run_id, "status": "running", "engine": config.engine,
        "layout": config.layout, "round_index": config.round_index,
        "round_order": list(config.round_order), "seed": truth.seed,
        "measurements": config.measurements,
        "batch_measurements": config.batch_measurements,
        "cache_state": "warm-reused-connections-no-os-cache-drop",
        "command": list(config.command or ("programmatic:run_layout",)),
        "input": config.input_identity,
        "host": _host_evidence(),
    }
    write_manifest_atomic(manifest_path, base_manifest)
    samples = []
    manifest = dict(base_manifest)
    error = None
    cleanup = None
    asset_removed = config.asset_root is None
    preflight_started = time.perf_counter()
    try:
        if config.verified_events:
            source_events = config.verified_events
            # CLI 已完成一次全量文件校验；adapter 在每个 block 写入前再次校验实际 payload。
            _validate_input(truth, source_events, config.input_root, validate_payloads=False)
            loaded_identity = config.input_identity
        else:
            loaded_truth, source_events, loaded_identity = load_run_input(config.input_root)
            if (
                loaded_truth.identity_sha256 != truth.identity_sha256
                or loaded_truth.record_count != truth.record_count
            ):
                raise ValueError("provided truth differs from input truth")
            if config.input_identity and loaded_identity != config.input_identity:
                raise ValueError("input identity changed before execution")
        manifest["input"] = loaded_identity
        ddl_started = time.perf_counter()
        created = adapter.create()
        manifest["ddl_create_ms"] = (time.perf_counter() - ddl_started) * 1000
        manifest["layout_definition"] = created
        manifest["ddl_sha256"] = canonical_digest(created)
        manifest["code"] = _code_evidence(adapter)
        manifest["engine_runtime"] = _engine_runtime(adapter, config.engine)
        container = _container_evidence(getattr(adapter, "container_name", "test-double"))
        if config.engine in {"opengauss", "clickhouse"} and not container["image_id"]:
            raise RuntimeError("container image digest evidence is missing")
        manifest["container"] = container
        events = build_workload_events(source_events, config.workload)
        manifest["workload"] = config.workload
        manifest["preflight_ms"] = (time.perf_counter() - preflight_started) * 1000
        block_evidence = []
        previous = 0
        ingestion_started = time.perf_counter()
        for watermark in truth.watermarks:
            block = list(events[previous:watermark])
            block_result = adapter.ingest_block(block)
            if (
                block_result.rows != len(block)
                or block_result.watermark != watermark
            ):
                raise RuntimeError(f"block result mismatch at watermark {watermark}")
            validate_watermark_keys(config.layout, block_result.watermarks, watermark)
            visible = adapter.wait_write_complete(watermark)
            validate_watermark_keys(config.layout, visible.watermarks, watermark)
            if not visible.completed:
                raise RuntimeError(f"joint watermark incomplete at {watermark}")
            block_evidence.append({
                "ingest": _as_json(block_result), "visible": _as_json(visible),
            })
            previous = watermark
        write_wall_ms = (time.perf_counter() - ingestion_started) * 1000
        ready = adapter.wait_query_ready(config.query_ready_timeout)
        validate_watermark_keys(config.layout, ready.watermarks, truth.record_count)
        if not ready.completed:
            raise RuntimeError("query readiness or final joint watermark incomplete")
        ready_total_ms = (time.perf_counter() - ingestion_started) * 1000
        audit = adapter.audit_dataset()
        audit_evidence = _validate_dataset_audit(audit, events, config.input_root, config.layout)
        storage = adapter.collect_storage()
        payload_bytes = sum(row["content_length"] or 0 for row in events)
        block_times = [item["ingest"]["wall_ms"] for item in block_evidence]
        logical_target_bytes = {table: 0 for table in build_layout_catalog(config.layout).write_tables}
        protocol_target_bytes = {table: 0 for table in logical_target_bytes}
        protocol_available = {table: True for table in logical_target_bytes}
        for item in block_evidence:
            ingest = item["ingest"]
            for table, value in ingest.get("logical_target_row_bytes", {}).items():
                logical_target_bytes[table] = logical_target_bytes.get(table, 0) + value
            for table, value in ingest.get("database_protocol_body_bytes", {}).items():
                if value is None:
                    protocol_available[table] = False
                else:
                    protocol_target_bytes[table] = protocol_target_bytes.get(table, 0) + value
        protocol_manifest = {
            table: protocol_target_bytes[table] if protocol_available[table] else "unavailable"
            for table in logical_target_bytes
        }
        logical_submitted = sum(logical_target_bytes.values())
        asset_submitted = sum(item["ingest"].get("asset_raw_object_bytes", 0) for item in block_evidence)
        write_target_ms = {}
        for item in block_evidence:
            for target, duration in item["ingest"]["write_target_ms"].items():
                write_target_ms[target] = write_target_ms.get(target, 0.0) + duration
        manifest["write"] = {
            "row_count": truth.record_count, "block_count": truth.block_count,
            "final_watermark": truth.record_count, "wall_ms": write_wall_ms,
            "rows_per_second": truth.record_count / (write_wall_ms / 1000),
            "raw_payload_bytes": payload_bytes,
            "logical_target_row_bytes": logical_target_bytes,
            "logical_target_row_bytes_total": logical_submitted,
            "logical_target_row_bytes_to_raw_payload_ratio":
                logical_submitted / payload_bytes if payload_bytes else None,
            "database_protocol_request_body_bytes": protocol_manifest,
            "database_protocol_request_body_bytes_total": (
                sum(protocol_target_bytes.values()) if all(protocol_available.values()) else "unavailable"
            ),
            "asset_raw_object_bytes": asset_submitted,
            "asset_raw_object_bytes_to_raw_payload_ratio":
                asset_submitted / payload_bytes if payload_bytes else None,
            "write_target_ms": write_target_ms,
            "asset_publish_ms": sum(
                item["ingest"].get("asset_publish_ms", 0.0) for item in block_evidence
            ),
            "raw_payload_mib_per_second": payload_bytes / (1024 * 1024) / (write_wall_ms / 1000),
            "block_wall_ms": _distribution(block_times), "blocks": block_evidence,
        }
        manifest["maintenance"] = {
            **_as_json(ready), "load_to_query_ready_ms": ready_total_ms,
            "watermark_wait_ms": sum(
                item["visible"].get("watermark_wait_ms", item["visible"]["wall_ms"])
                for item in block_evidence
            ),
            "natural_stable_parts": config.engine == "clickhouse",
            "optimize_final": False,
        }
        manifest["dataset_audit"] = audit_evidence
        manifest["storage"] = _as_json(storage)
        cases = workload_query_cases(events, truth, config.input_root, config.workload)
        manifest["query_catalog_sha256"] = canonical_digest([
            {"scenario": query_truth.scenario, "kind": query.kind, "parameters": query.parameters}
            for query, query_truth in cases
        ])
        warmups = [measure_query(adapter, query, query_truth) for query, query_truth in cases]
        if any(sample.status != "success" for sample in warmups):
            raise RuntimeError("query warmup failed truth validation")
        for query, query_truth in cases:
            count = config.batch_measurements if query.kind == "batch" else config.measurements
            samples.extend(measure_query(adapter, query, query_truth) for _ in range(count))
        successful_ids = [sample.query_id for sample in samples if sample.status == "success"]
        access = adapter.collect_access_evidence(successful_ids)
        manifest["access"] = _as_json(access)
        manifest["access_validation"] = _validate_access(
            config.engine, config.layout, loaded_identity["kind"], samples, access,
        )
    except Exception as exception:
        error = exception
    finally:
        try:
            cleanup = adapter.cleanup()
        except Exception as cleanup_error:
            cleanup = {"namespace": getattr(adapter, "namespace", "unknown"), "removed": False,
                       "error": str(cleanup_error) or type(cleanup_error).__name__}
            if error is None:
                error = RuntimeError(f"cleanup failed: {cleanup_error}")
        if config.asset_root is not None:
            try:
                asset_root = Path(config.asset_root).resolve()
                shutil.rmtree(asset_root)
                asset_removed = not asset_root.exists()
            except OSError as asset_error:
                asset_removed = False
                if error is None:
                    error = RuntimeError(f"Asset cleanup failed: {asset_error}")
    manifest["cleanup"] = _as_json(cleanup)
    manifest["cleanup"]["asset_directory_removed"] = asset_removed
    summary = summarize_samples(tuple(samples))
    manifest["correctness"] = {
        "truth_identity": truth.identity_sha256,
        "formal_samples": len(samples),
        "successful_samples": sum(sample.status == "success" for sample in samples),
        "failed_samples": sum(sample.status != "success" for sample in samples),
        "response_bytes_validated": all(
            sample.status == "success" and sample.response_bytes > 0 for sample in samples
        ),
    }
    if error is None:
        try:
            manifest["access_validation"] = _evidence_gate(
                config.engine, config.layout, loaded_identity["kind"],
                samples, access, storage, cleanup,
            )
            if not asset_removed:
                raise RuntimeError("Asset directory cleanup was not confirmed")
        except Exception as gate_error:
            error = gate_error
    manifest["status"] = "complete" if error is None else "failed"
    if error is not None:
        manifest["error"] = str(error) or type(error).__name__
    _write_samples(output / "samples.jsonl", tuple(samples))
    _write_bytes_atomic(output / "result.json", _json_bytes({
        "run_id": run_id, "status": manifest["status"], "summary": summary,
        "correctness": manifest["correctness"],
    }))
    write_manifest_atomic(manifest_path, manifest)
    if error is not None:
        raise RuntimeError(str(error) or type(error).__name__) from error
    return RunResult(manifest, tuple(samples), summary)


def _adapter(engine, layout, namespace, input_root, asset_root, arguments):
    """按 CLI 目标构造数据库 adapter 及隔离的 Asset store。"""
    asset_store = LocalAssetStore(asset_root) if layout == "asset_ref" else None
    if engine == "opengauss":
        from opengauss import OpenGaussAdapter

        return OpenGaussAdapter(
            arguments.opengauss_host, arguments.opengauss_port,
            arguments.opengauss_container, namespace, layout, input_root, asset_store,
        )
    if engine == "clickhouse":
        from clickhouse import ClickHouseAdapter

        return ClickHouseAdapter(
            arguments.clickhouse_host, arguments.clickhouse_port,
            arguments.clickhouse_container, namespace, layout, input_root, asset_store,
        )
    raise ValueError(f"unsupported engine: {engine}")


def _parse_csv(value, allowed, name):
    """解析无重复的逗号分隔 CLI 目标。"""
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values or len(values) != len(set(values)) or any(item not in allowed for item in values):
        raise ValueError(f"invalid {name}")
    return values


def remove_empty_asset_workspace(root):
    """删除 runner 自有且仅含空目录的 Asset 工作树。"""
    root = Path(root)
    if not root.exists():
        return True
    if root.name != ".asset-work" or any(path.is_file() or path.is_symlink() for path in root.rglob("*")):
        return False
    shutil.rmtree(root)
    return not root.exists()


def failed_round_record(round_dir, position, output_root):
    """读取失败 round 的诊断 manifest，使聚合目标保留清理证据。"""
    path = Path(round_dir) / "run-manifest.json"
    try:
        manifest = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("failed round manifest is unavailable") from error
    if manifest.get("status") != "failed" or "cleanup" not in manifest:
        raise RuntimeError("failed round manifest lacks cleanup evidence")
    return {
        **manifest, "position": position,
        "artifact_directory": str(Path(round_dir).relative_to(output_root)),
    }


def run_matrix(arguments):
    """按 workload 隔离执行 Latin square，并在全局清理后发布八个目标。"""
    input_root = Path(arguments.input).resolve()
    output_root = Path(arguments.output).resolve()
    engines = _parse_csv(arguments.engines, {"opengauss", "clickhouse"}, "engines")
    layouts = _parse_csv(arguments.layouts, set(LAYOUTS), "layouts")
    workloads = _parse_csv(getattr(arguments, "workloads", ",".join(WORKLOADS)), set(WORKLOADS), "workloads")
    if set(layouts) != set(LAYOUTS):
        raise ValueError("main matrix requires all four layouts")
    schedule = latin_square(LAYOUTS)
    command = (sys.executable, *sys.argv)
    target_states = {}
    for engine in engines:
        for layout in LAYOUTS:
            target_dir = output_root / engine / layout
            target_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "format": "agent-trace-json-storage-stage3-layout-matrix", "format_version": 1,
                "run_id": f"jsons3-{engine}-{layout}-{uuid.uuid4().hex[:10]}",
                "status": "running", "engine": engine, "layout": layout,
                "latin_square": [list(row) for row in schedule],
                "measurements": arguments.measurements,
                "batch_measurements": arguments.batch_measurements,
                "command": list(command), "input": {
                    name: _file_identity(input_root / name)
                    for name in ("generation-manifest.json", "truth.json", "events.jsonl")
                    if (input_root / name).is_file()
                },
                "workloads": {workload: {"status": "running", "rounds": []} for workload in workloads},
                "rounds": [],
                "host": _host_evidence(),
                "container": _container_evidence(
                    arguments.opengauss_container if engine == "opengauss"
                    else arguments.clickhouse_container
                ),
            }
            write_manifest_atomic(target_dir / "run-manifest.json", state)
            target_states[(engine, layout)] = state
    try:
        truth, events, identity = load_run_input(input_root)
        generation = json.loads((input_root / "generation-manifest.json").read_bytes())
        if identity["kind"] == "formal":
            validate_formal_contract(
                generation, truth, arguments.measurements, arguments.batch_measurements,
            )
    except Exception as error:
        for (engine, layout), state in target_states.items():
            state.update({
                "status": "failed", "error_category": "input_validation",
                "error": str(error) or type(error).__name__,
            })
            write_manifest_atomic(output_root / engine / layout / "run-manifest.json", state)
        raise
    for state in target_states.values():
        state["seed"] = truth.seed
        state["input"] = identity
    failures = []
    aggregate_samples = {
        (engine, layout, workload): []
        for engine in engines for layout in LAYOUTS for workload in workloads
    }
    for workload in workloads:
        workload_events = build_workload_events(events, workload)
        contract = validate_workload_contract(workload_events, workload, identity["kind"])
        rounds = range(1) if workload == "correctness_only" else range(4)
        for state in target_states.values():
            state["workloads"][workload]["contract"] = contract
        for round_index in rounds:
            order = schedule[round_index]
            for position, layout in enumerate(order):
                for engine in engines:
                    state = target_states[(engine, layout)]
                    workload_state = state["workloads"][workload]
                    target_dir = output_root / engine / layout
                    round_dir = target_dir / "rounds" / workload / f"round-{round_index + 1}"
                    asset_root = output_root / ".asset-work" / engine / layout / workload / f"round-{round_index + 1}"
                    namespace = f"jsons3_{engine[:2]}{workload[:2]}{round_index + 1}{position + 1}_{uuid.uuid4().hex[:6]}"
                    adapter = _adapter(engine, layout, namespace, input_root, asset_root, arguments)
                    config = RunConfig(
                        input_root=input_root, output=round_dir, engine=engine, layout=layout,
                        round_index=round_index, round_order=order,
                        measurements=arguments.measurements,
                        batch_measurements=arguments.batch_measurements,
                        query_ready_timeout=arguments.query_ready_timeout,
                        input_identity=identity, command=command,
                        asset_root=asset_root if layout == "asset_ref" else None,
                        verified_events=events, workload=workload,
                    )
                    try:
                        result = run_layout(adapter, truth, config)
                        record = {
                            **result.manifest, "position": position,
                            "artifact_directory": str(round_dir.relative_to(output_root)),
                        }
                        workload_state["rounds"].append(record)
                        for evidence_key in ("code", "engine_runtime"):
                            state.setdefault(evidence_key, record[evidence_key])
                        if workload == "main":
                            state["rounds"].append(record)
                        aggregate_samples[(engine, layout, workload)].extend(
                            (round_index, position, sample) for sample in result.samples
                        )
                    except Exception as error:
                        workload_state.update({
                            "status": "failed", "error": str(error) or type(error).__name__,
                            "failed_round": round_index,
                        })
                        try:
                            workload_state["rounds"].append(
                                failed_round_record(round_dir, position, output_root)
                            )
                        except Exception as artifact_error:
                            workload_state["failed_artifact_error"] = str(artifact_error)
                        failures.append(f"{engine}/{layout}/{workload}: {workload_state['error']}")
                    write_manifest_atomic(target_dir / "run-manifest.json", state)
        expected_rounds = 1 if workload == "correctness_only" else 4
        for state in target_states.values():
            workload_state = state["workloads"][workload]
            if workload_state["status"] != "failed":
                complete = len(workload_state["rounds"]) == expected_rounds and all(
                    record["status"] == "complete" and record["cleanup"]["removed"]
                    and record["cleanup"]["asset_directory_removed"]
                    for record in workload_state["rounds"]
                )
                workload_state["status"] = "ready" if complete else "failed"
                if not complete:
                    workload_state["error"] = "workload rounds incomplete"
                    failures.append(f"{state['engine']}/{state['layout']}/{workload}: workload rounds incomplete")
    asset_work_clean = remove_empty_asset_workspace(output_root / ".asset-work")
    if not asset_work_clean:
        failures.append("matrix: Asset workspace contains residual objects")
    for engine in engines:
        for layout in LAYOUTS:
            target_dir = output_root / engine / layout
            state = target_states[(engine, layout)]
            decorated = []
            plain = []
            for workload in workloads:
                for round_index, position, sample in aggregate_samples[(engine, layout, workload)]:
                    value = _as_json(sample)
                    value.update({"round_index": round_index, "position": position, "workload": workload})
                    decorated.append(value)
                    plain.append(sample)
            _write_bytes_atomic(
                target_dir / "samples.jsonl",
                b"".join(_json_bytes(value) for value in decorated),
            )
            summary = summarize_samples(tuple(plain))
            complete = asset_work_clean and all(
                workload_state["status"] == "ready"
                for workload_state in state["workloads"].values()
            )
            state["status"] = "complete" if complete else "failed"
            state["global_cleanup"] = {
                "asset_workspace": str(output_root / ".asset-work"),
                "removed": asset_work_clean,
            }
            state["ddl_sha256"] = sorted({
                record["ddl_sha256"]
                for workload_state in state["workloads"].values()
                for record in workload_state["rounds"] if "ddl_sha256" in record
            })
            state["query_catalog_sha256"] = {
                workload: sorted({
                    record["query_catalog_sha256"] for record in workload_state["rounds"]
                    if "query_catalog_sha256" in record
                })
                for workload, workload_state in state["workloads"].items()
            }
            if complete:
                for workload_state in state["workloads"].values():
                    workload_state["status"] = "complete"
            if not complete and "error" not in state:
                state["error"] = "workload or global cleanup gate failed"
                failures.append(f"{engine}/{layout}: {state['error']}")
            state["correctness"] = {
                "rounds_complete": len(state["rounds"]),
                "formal_samples": len(plain),
                "successful_samples": sum(sample.status == "success" for sample in plain),
                "response_bytes_validated": bool(plain) and all(
                    sample.status == "success" and sample.response_bytes > 0 for sample in plain
                ),
            }
            state["response_bytes"] = {
                "database": sum(sample.database_response_bytes for sample in plain),
                "resolver_payload": sum(sample.resolver_payload_bytes for sample in plain),
                "total": sum(sample.response_bytes for sample in plain),
                "validated_payload": sum(
                    sample.validation.validated_payload_bytes for sample in plain
                ),
                "database_protocol": (
                    sum(sample.database_protocol_bytes for sample in plain)
                    if plain and all(sample.database_protocol_bytes is not None for sample in plain)
                    else "unavailable"
                ),
            }
            _write_bytes_atomic(target_dir / "result.json", _json_bytes({
                "run_id": state["run_id"], "status": state["status"], "summary": summary,
                "correctness": state["correctness"],
            }))
            write_manifest_atomic(target_dir / "run-manifest.json", state)
    if failures:
        raise RuntimeError("; ".join(failures))
    return target_states


def _argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-smoke-input", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--engines", default="opengauss,clickhouse")
    parser.add_argument("--layouts", default=",".join(LAYOUTS))
    parser.add_argument("--workloads", default=",".join(WORKLOADS))
    parser.add_argument("--measurements", type=int, default=30)
    parser.add_argument("--batch-measurements", type=int, default=5)
    parser.add_argument("--query-ready-timeout", type=int, default=60)
    parser.add_argument("--opengauss-host", default="127.0.0.1")
    parser.add_argument("--opengauss-port", type=int, default=15432)
    parser.add_argument("--opengauss-container", default="agent-trace-opengauss-v6")
    parser.add_argument("--clickhouse-host", default="127.0.0.1")
    parser.add_argument("--clickhouse-port", type=int, default=18123)
    parser.add_argument("--clickhouse-container", default="agent-trace-clickhouse-25-12")
    return parser


def main(argv=None):
    """解析 CLI；可生成显式 smoke 输入或执行主矩阵。"""
    parser = _argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.create_smoke_input is not None:
        if arguments.input is not None or arguments.output is not None:
            parser.error("--create-smoke-input cannot be combined with --input or --output")
        manifest = build_smoke_input(arguments.create_smoke_input)
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0
    if arguments.input is None or arguments.output is None:
        parser.error("--input and --output are required for a matrix run")
    run_matrix(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
