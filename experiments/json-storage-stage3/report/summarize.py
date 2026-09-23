"""汇总已完成的 Stage 3 主矩阵 target、part-state 与 interference 控制。"""

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path


FORMAT = "agent-trace-json-storage-stage3-layout-matrix"
FORMAT_VERSION = 1
LAYOUTS = ("same_table", "separate", "full_core", "asset_ref")
LAYOUT_TARGETS = {
    "same_table": ("events",),
    "separate": ("events_analytics", "event_payloads"),
    "full_core": ("events_full", "events_core"),
    "asset_ref": ("events_analytics", "assets"),
}
PERFORMANCE_WORKLOADS = (
    "main", "equal_total_few_large", "equal_total_many_medium",
)
ALL_WORKLOADS = PERFORMANCE_WORKLOADS + ("correctness_only",)
METRICS = (
    "query_complete_ms", "recovery_ms", "validation_ms", "application_ready_ms",
)
STATISTICS = ("minimum", "p50", "p95", "maximum")
WRITE_RATE_FIELDS = (
    "wall_ms", "rows_per_second", "raw_payload_mib_per_second", "asset_publish_ms",
)
WRITE_COUNT_FIELDS = ("asset_raw_object_bytes", "block_count", "final_watermark")
WRITE_CLIENT_BYTES_FIELD = "database_ingest_request_body_bytes_total"
WRITE_BLOCK_STATISTICS = ("minimum", "median", "p95", "maximum")
STORAGE_TABLE_FIELDS = {
    "clickhouse": ("part_count", "rows", "marks", "compressed_bytes", "uncompressed_bytes"),
    "opengauss": ("heap_bytes", "index_bytes", "toast_bytes", "total_bytes"),
    "xstore": ("heap_bytes", "index_bytes", "toast_bytes", "total_bytes"),
}
STORAGE_ASSET_FIELDS = (
    "available_bytes", "available_object_count", "orphan_bytes", "orphan_object_count",
)

PRODUCTION_FORMAT = "agent-trace-json-storage-stage3-production-run"
PART_STATE_FORMAT = "agent-trace-json-storage-stage3-clickhouse-part-states"
PART_STATE_SUMMARY_FORMAT = "agent-trace-json-storage-stage3-part-states-summary"
PART_STATE_STATE_ORDER = ("fragmented", "merging", "stable", "single_part")
PART_STATE_SCENARIOS = (
    "batch:main",
    "detail:entropy_512k",
    "detail:text_2m",
    "detail:text_512k",
    "detail:text_64k",
    "list:first",
    "list:middle",
    "preview:first",
    "preview:middle",
    "trace:p25",
    "trace:p50",
    "trace:p95",
)
PART_STATE_SCENARIO_KINDS = {
    scenario: scenario.split(":", 1)[0] for scenario in PART_STATE_SCENARIOS
}
PART_STATE_SAMPLES_PER_QUERY = 30
PART_STATE_TABLE_METRICS = ("part_count", "marks", "compressed_bytes", "uncompressed_bytes")
PART_STATE_CONTROLLED_TABLES = {
    "same_table": "events",
    "separate": "events_analytics",
    "full_core": "events_core",
    "asset_ref": "events_analytics",
}
PART_STATE_CODE_ROLES = (
    "run_stage3", "production", "generator", "layout_runner",
    "part_state_runner", "common", "assets",
)
PART_STATE_ASSET_FIELDS = (
    "available_object_count", "available_bytes", "orphan_object_count", "orphan_bytes",
)
PART_STATE_STATISTICS_BOUNDARY = "raw-query-sample-distribution"
PART_STATE_CACHE_LIMITS = "ordered-control-warm-cache-no-os-cache-drop-not-main-matrix-paired"
FORMAL_TRUTH = {
    "seed": 20260907,
    "record_count": 48534,
    "block_size": 256,
    "block_count": 190,
}

INTERFERENCE_FORMAT = "agent-trace-json-storage-stage3-interference-run"
INTERFERENCE_PHASE_FORMAT = "agent-trace-json-storage-stage3-interference-phase"
INTERFERENCE_SUMMARY_FORMAT = "agent-trace-json-storage-stage3-interference-summary"
INTERFERENCE_PHASE_ORDER = (
    "quiet", "detail_2m", "trace_long", "batch_loop", "continuous_ingest",
)
INTERFERENCE_SEGMENTS = ("warmup", "measurement")
INTERFERENCE_SEGMENT_SECONDS = {"warmup": 30.0, "measurement": 300.0}
INTERFERENCE_SEGMENT_FILES = {
    "warmup": "warmup-samples.jsonl", "measurement": "samples.jsonl",
}
INTERFERENCE_SNAPSHOT_NAMES = (
    "before_warmup", "before_measurement", "after_measurement",
)
INTERFERENCE_STATISTICS_BOUNDARY = "raw-samples-retained-p99-requires-1000-successes"
INTERFERENCE_P99_MINIMUM_SUCCESSES = 1000
INTERFERENCE_SCOPE = "formal"
INTERFERENCE_CLASSIFICATION = "formal_complete"
INTERFERENCE_ELIGIBLE_BLOCK_COUNT = 45
INTERFERENCE_ASSET_OBJECTS = 160
INTERFERENCE_ASSET_BYTES = 128450560
INTERFERENCE_PHASE_STREAMS = {
    "quiet": None,
    "detail_2m": "detail_2m",
    "trace_long": "trace_long",
    "batch_loop": "batch_loop",
    "continuous_ingest": "continuous_ingest",
}
INTERFERENCE_STREAM_RATES = {
    "list": 20.0, "preview": 20.0, "detail_2m": 1.0,
    "trace_long": 0.2, "batch_loop": None, "continuous_ingest": 1.0,
}
INTERFERENCE_STREAM_WORKERS = {
    "list": 2, "preview": 2, "detail_2m": 1,
    "trace_long": 1, "batch_loop": 1, "continuous_ingest": 1,
}
INTERFERENCE_STREAM_MODES = {"batch_loop": "continuous"}
INTERFERENCE_TIMEOUT_SECONDS = 30.0
INTERFERENCE_LATE_TOLERANCE_SECONDS = 0.05
INTERFERENCE_COVERAGE_OVERRUN_SECONDS = 3.0
INTERFERENCE_OFFSET_TOLERANCE_SECONDS = 1e-6
INTERFERENCE_QUERY_SCENARIOS = {
    "list": ("list", "list:first"),
    "preview": ("preview", "preview:first"),
    "detail_2m": ("detail", "detail:text_2m"),
    "trace_long": ("trace", "trace:p95"),
    "batch_loop": ("batch", "batch:main"),
}
INTERFERENCE_CODE_ROLES = frozenset({
    "assets", "common", "generator", "interference_runner",
    "layout_runner", "production", "run_stage3",
})
INTERFERENCE_METADATA_FIELDS = frozenset({
    "block_size", "cyclic_replay", "eligible_block_count", "eligible_block_indices",
    "eligible_block_sha256", "final_watermark", "main_query_catalog_sha256",
    "preload_block_count", "seed", "selection_rules",
})
INTERFERENCE_SELECTION_FIELDS = frozenset({
    "full_block_rows", "outside_query_window", "query_scenarios", "requires_main_payload",
})
INTERFERENCE_QUERY_WINDOW_FIELDS = frozenset({"project_id", "start_time", "end_time"})
INTERFERENCE_CLEANUP_FIELDS = frozenset({
    "namespaces", "namespaces_removed", "asset_directory_applicable",
    "asset_directory_removed",
})
INTERFERENCE_NAMESPACE_POLICY_FIELDS = frozenset({
    "strategy", "reuse", "phase_order", "namespace_prefix", "namespaces",
})
INTERFERENCE_CACHE_STATE = "warm-fixed-offered-load-no-os-cache-drop"
INTERFERENCE_NAMESPACE_STRATEGY = "runner-fixed-phase-unique"
INTERFERENCE_NAMESPACE_PREFIX = "jsons3_if_<phase>_"
INTERFERENCE_CHILD_FIELDS = frozenset({
    "path", "bytes", "sha256", "format", "format_version", "status", "run_id",
})
INTERFERENCE_ARTIFACT_FIELDS = frozenset({"path", "bytes", "sha256"})
INTERFERENCE_ARTIFACT_PATHS = tuple(
    ["child/run-manifest.json"]
    + [
        path
        for phase in INTERFERENCE_PHASE_ORDER
        for path in (
            f"child/{phase}/run-manifest.json",
            f"child/{phase}/warmup-samples.jsonl",
            f"child/{phase}/samples.jsonl",
        )
    ]
)
INTERFERENCE_SNAPSHOT_TABLE_METRICS = (
    "part_count", "marks", "compressed_bytes", "uncompressed_bytes",
)
INTERFERENCE_QUERY_EVIDENCE_FIELDS = frozenset({
    "index_scans", "plans", "query_details", "query_finish",
})
INTERFERENCE_COUNT_FIELDS = (
    "scheduled_requests", "started_requests", "completed_requests", "successful_requests",
    "failed_requests", "timed_out_requests", "dropped_requests", "late_requests",
)
INTERFERENCE_PUBLICATION_FIELDS = frozenset(
    set(INTERFERENCE_COUNT_FIELDS)
    | {
        "offered_rate_requests_s", "phase_wall_seconds",
        "completed_throughput_requests_s", "latency_ms",
    }
)
INTERFERENCE_LATENCY_FIELDS = frozenset({
    "minimum", "p50", "p95", "p99", "maximum", "p99_status", "p99_minimum_successes",
})
INTERFERENCE_STATUS_FIELDS = {
    "success": "successful_requests",
    "failed": "failed_requests",
    "timed_out": "timed_out_requests",
    "dropped": "dropped_requests",
}
INTERFERENCE_RAW_FIELDS = frozenset({
    "stream", "sequence", "scheduled_offset_seconds", "started_offset_seconds",
    "completed_offset_seconds", "duration_ms", "application_ready_ms", "status",
    "late_by_ms", "sample", "error",
})

ASSET_FAILURE_FORMAT = "agent-trace-json-storage-stage3-asset-failure-run"
ASSET_FAILURE_SUMMARY_FORMAT = "agent-trace-json-storage-stage3-asset-failures-summary"
ASSET_FAILURE_ENGINES = ("opengauss", "clickhouse", "xstore")
ASSET_FAILURE_MINIMUM_ENGINES = 2
ASSET_FAILURE_LAYOUT = "asset_ref"
ASSET_FAILURE_CASE_ORDER = (
    "missing", "corrupt", "metadata_mismatch",
    "upload_then_db_failure", "publish_failure", "delete_failure",
)
ASSET_FAILURE_STATISTICS_BOUNDARY = "classification-and-evidence-only-no-timing-statistic"
ASSET_FAILURE_NAMESPACE_STRATEGY = "runner-fixed-case-unique"
ASSET_FAILURE_NAMESPACE_PREFIX = "jsons3_af_<case>_"
ASSET_FAILURE_CODE_ROLES = (
    "asset_failure_runner", "assets", "common", "generator",
    "layout_runner", "production", "run_stage3",
)
ASSET_FAILURE_NAMESPACE_POLICY_FIELDS = frozenset({
    "strategy", "reuse", "case_order", "namespace_prefix", "namespaces",
})
# 六个案例的固定契约：注入点与注入前目录状态、解析器分类、终态、事件可见性、注入后孤儿
# 对象数、恢复动作与恢复解析结果、对象存在性与发布尝试次数；恢复后孤儿数固定为 0。
ASSET_FAILURE_CLASSIFICATION = {
    "missing": {
        "injection_point": "remove_published_object", "injection_before": "available",
        "resolver_error": "missing", "final_status": "available",
        "event_visible": True, "orphan_count": 0,
        "recovery_actions": ["restore_missing_object"],
        "recovery_error": None, "recovery_visible": True,
        "object_exists": False, "publish_attempts": 0,
    },
    "corrupt": {
        "injection_point": "modify_published_bytes", "injection_before": "available",
        "resolver_error": "corrupt", "final_status": "available",
        "event_visible": True, "orphan_count": 0,
        "recovery_actions": ["replace_corrupt_object"],
        "recovery_error": None, "recovery_visible": True,
        "object_exists": True, "publish_attempts": 0,
    },
    "metadata_mismatch": {
        "injection_point": "replace_catalog_metadata", "injection_before": "available",
        "resolver_error": "metadata_mismatch", "final_status": "available",
        "event_visible": True, "orphan_count": 0,
        "recovery_actions": ["restore_catalog_metadata"],
        "recovery_error": None, "recovery_visible": True,
        "object_exists": True, "publish_attempts": 0,
    },
    "upload_then_db_failure": {
        "injection_point": "fail_after_object_upload", "injection_before": "absent",
        "resolver_error": "missing", "final_status": "absent",
        "event_visible": False, "orphan_count": 1,
        "recovery_actions": ["remove_orphan_object"],
        "recovery_error": "missing", "recovery_visible": False,
        "object_exists": True, "publish_attempts": 1,
    },
    "publish_failure": {
        "injection_point": "fail_pending_publication", "injection_before": "pending",
        "resolver_error": "failed", "final_status": "failed",
        "event_visible": True, "orphan_count": 0,
        "recovery_actions": ["confirm_failed_publication"],
        "recovery_error": "failed", "recovery_visible": False,
        "object_exists": False, "publish_attempts": 1,
    },
    "delete_failure": {
        "injection_point": "fail_deleting_object_removal", "injection_before": "available",
        "resolver_error": "deleting", "final_status": "deleting",
        "event_visible": True, "orphan_count": 0,
        "recovery_actions": ["confirm_delete_failure_state"],
        "recovery_error": "deleting", "recovery_visible": False,
        "object_exists": True, "publish_attempts": 0,
    },
}
ASSET_FAILURE_RESULT_FIELDS = frozenset({
    "asset_id", "case", "catalog_transitions", "cleanup", "event_visible",
    "execution_error", "final_status", "injection_point", "namespace", "reconcile",
    "reconcile_after_recovery", "recovery_actions", "recovery_resolver", "resolver",
    "sha256", "store_observation", "validation_errors",
})
ASSET_FAILURE_RESOLVER_FIELDS = frozenset({
    "content_length", "content_visible", "error", "preview", "sha256",
})
ASSET_FAILURE_RECONCILE_FIELDS = frozenset({"orphan_count", "orphan_paths"})
ASSET_FAILURE_TRANSITION_FIELDS = frozenset({
    "after", "asset_id", "before", "phase", "sha256",
})
ASSET_FAILURE_TRANSITION_PHASES = ("injection", "recovery")
ASSET_FAILURE_STORE_FIELDS = frozenset({
    "object_exists", "object_path", "publish_attempts",
})
ASSET_FAILURE_ATTEMPT_FIELDS = frozenset({"asset_id", "error", "final_object_exists"})
ASSET_FAILURE_CASE_CLEANUP_FIELDS = frozenset({
    "adapter_cleanup_target", "errors", "namespace", "namespace_removed",
    "object_directory", "object_directory_removed",
})
ASSET_FAILURE_BLOCK_FIELDS = frozenset({
    "case_order", "cleanup", "engine", "namespaces", "results",
})
ASSET_FAILURE_CHILD_FIELDS = frozenset({
    "bytes", "format", "format_version", "path", "run_id", "sha256", "status",
})
ASSET_FAILURE_CLEANUP_FIELDS = frozenset({
    "namespaces", "namespaces_removed", "object_directories_removed",
    "runtime_probe_directory", "runtime_probe_directory_removed",
})

COMBINED_FORMAT = "agent-trace-json-storage-stage3-combined-summary"
COMBINED_INPUT_FAMILIES = ("matrix", "part_states", "interference")
COMBINED_TRUTH_FAMILIES = ("part_states", "interference")
COMBINED_ASSET_FAILURE_BINDING = (
    "not bound to the formal input identity: the asset-failure production envelopes "
    "publish no input, truth or query catalog identity on either engine"
)


def _require(value, message):
    """在缺少正式证据时抛出可定位的门禁错误。"""
    if not value:
        raise ValueError(message)
    return value


def _number(value, message):
    """验证非布尔的非负数测量值。"""
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(message)
    return value


def _integer(value, message, minimum=0):
    """验证非布尔的整数计数或字节值。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(message)
    return value


def _sha256(value):
    """判断值是否为 runner 写入的小写 SHA-256。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_provenance(manifest, workloads):
    """验证 manifest 汇总的 code、DDL 和 workload query 身份。"""
    code = manifest.get("code")
    if not isinstance(code, dict) or set(code) != {"runner", "common", "assets", "adapter"}:
        raise ValueError("provenance evidence is incomplete")
    for evidence in code.values():
        if (
            not isinstance(evidence, dict)
            or not isinstance(evidence.get("path"), str)
            or not evidence["path"]
            or not _sha256(evidence.get("sha256"))
        ):
            raise ValueError("provenance evidence is incomplete")
        _integer(evidence.get("bytes"), "provenance evidence is incomplete", 1)
    ddl = manifest.get("ddl_sha256")
    queries = manifest.get("query_catalog_sha256")
    if (
        not isinstance(ddl, list)
        or not ddl
        or any(not _sha256(value) for value in ddl)
        or ddl != sorted(set(ddl))
        or not isinstance(queries, dict)
        or set(queries) != set(workloads)
        or any(
            not isinstance(values, list)
            or not values
            or any(not _sha256(value) for value in values)
            or values != sorted(set(values))
            for values in queries.values()
        )
    ):
        raise ValueError("provenance evidence is incomplete")


def _percentile(values, fraction):
    """以线性插值计算固定定义的百分位数。"""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] * (1 - position + lower) + ordered[upper] * (position - lower)


def _distribution(values):
    """返回一个 round 内的四项延迟分布。"""
    if not values:
        raise ValueError("successful samples are missing")
    return {
        "minimum": min(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "maximum": max(values),
    }


def _identity(value):
    """验证正式冻结输入身份并返回可比较对象。"""
    if (
        not isinstance(value, dict)
        or value.get("kind") != "formal"
        or not _sha256(value.get("identity_sha256"))
    ):
        raise ValueError("formal truth/input identity is missing")
    digests = ("generation_manifest", "truth", "events")
    for name in digests:
        evidence = value.get(name)
        if (
            not isinstance(evidence, dict)
            or not _sha256(evidence.get("sha256"))
        ):
            raise ValueError("formal truth/input identity is missing")
        _integer(evidence.get("bytes"), "formal truth/input identity is missing", 1)
    return value


def _workload_scope(manifest):
    """读取 manifest 声明的 workload 范围，兼容聚合 target 与单 workload shard。"""
    states = _require(manifest.get("workloads"), "workload evidence is missing")
    if (
        not isinstance(states, dict)
        or not states
        or not set(states) <= set(ALL_WORKLOADS)
    ):
        raise ValueError("workload evidence is incomplete")
    return tuple(workload for workload in ALL_WORKLOADS if workload in states)


def _round_records(manifest, workloads):
    """读取给定 workload 范围内每个 workload 的固定 round 列表。"""
    states = _require(manifest.get("workloads"), "workload evidence is missing")
    if not isinstance(states, dict):
        raise ValueError("workload evidence is incomplete")
    records = {}
    for workload in workloads:
        state = states[workload]
        if not isinstance(state, dict) or state.get("status") != "complete":
            raise ValueError(f"{workload} workload is not complete")
        rounds = state.get("rounds")
        expected = 4 if workload in PERFORMANCE_WORKLOADS else 1
        if not isinstance(rounds, list) or len(rounds) != expected:
            raise ValueError(f"{workload} performance workload must have {expected} rounds")
        records[workload] = rounds
    return records


def _validate_round(manifest, workload, record, seen_positions):
    """验证单个 round 的身份、证据和 ClickHouse 物理状态。"""
    engine, layout = manifest["engine"], manifest["layout"]
    if not isinstance(record, dict) or record.get("status") != "complete":
        raise ValueError("round status is not complete")
    if (
        record.get("format") != "agent-trace-json-storage-stage3-layout-run"
        or record.get("format_version") != 1
    ):
        raise ValueError("round format/version is invalid")
    if record.get("engine") != engine or record.get("layout") != layout:
        raise ValueError("round engine/layout identity mismatch")
    if record.get("workload") != workload:
        raise ValueError("round workload identity mismatch")
    if (
        record.get("code") != manifest["code"]
        or record.get("ddl_sha256") not in manifest["ddl_sha256"]
        or record.get("query_catalog_sha256")
        not in manifest["query_catalog_sha256"][workload]
    ):
        raise ValueError("provenance evidence is incomplete")
    if (
        not isinstance(record.get("round_index"), int)
        or isinstance(record["round_index"], bool)
        or not 0 <= record["round_index"] < 4
    ):
        raise ValueError("round index is invalid")
    order = record.get("round_order")
    schedule = manifest.get("latin_square")
    if (
        not isinstance(order, list)
        or sorted(order) != sorted(LAYOUTS)
        or not isinstance(schedule, list)
        or record["round_index"] >= len(schedule)
        or order != schedule[record["round_index"]]
    ):
        raise ValueError("Latin square order is invalid")
    position = record.get("position")
    if (
        not isinstance(position, int)
        or isinstance(position, bool)
        or not 0 <= position < len(LAYOUTS)
    ):
        raise ValueError("Latin position is invalid")
    if order[position] != layout or position in seen_positions:
        raise ValueError("Latin position is duplicate")
    seen_positions.add(position)
    if _identity(record.get("input")) != manifest["input"]:
        raise ValueError("round truth/input identity mismatch")
    correctness = _require(record.get("correctness"), "response-byte validation is missing")
    if not isinstance(correctness, dict) or not correctness.get("response_bytes_validated"):
        raise ValueError("response-byte validation is missing")
    if correctness.get("truth_identity") != manifest["input"]["identity_sha256"]:
        raise ValueError("truth evidence is missing")
    formal_samples = _integer(
        correctness.get("formal_samples"), "round correctness evidence is incomplete", 1,
    )
    successful_samples = _integer(
        correctness.get("successful_samples"), "round correctness evidence is incomplete", 1,
    )
    failed_samples = _integer(
        correctness.get("failed_samples"), "round correctness evidence is incomplete",
    )
    if successful_samples != formal_samples or failed_samples != 0:
        raise ValueError("round correctness evidence is incomplete")
    access = _require(record.get("access"), "access evidence is missing")
    if not isinstance(access, dict) or not access.get("plans") or not access.get("query_details"):
        raise ValueError("access evidence is missing")
    if any(not isinstance(plan, str) or not plan.strip() for plan in access["plans"].values()):
        raise ValueError("access evidence is incomplete")
    if engine in ("opengauss", "xstore"):
        index_scans = access.get("index_scans")
        if not isinstance(index_scans, dict) or not index_scans:
            raise ValueError("access evidence is incomplete")
        for count in index_scans.values():
            _integer(count, "access evidence is incomplete")
    for detail in access["query_details"].values():
        if not isinstance(detail, dict):
            raise ValueError("access evidence is missing")
        _integer(detail.get("scanned_rows"), "access evidence is missing")
        if engine == "clickhouse":
            _integer(detail.get("scanned_bytes"), "access evidence is missing")
        elif not (
            "scanned_bytes" in detail
            and detail.get("scanned_bytes") is None
            and detail.get("scanned_bytes_status") == "unavailable"
        ):
            raise ValueError("access evidence is missing")
    access_validation = _require(record.get("access_validation"), "access evidence is missing")
    if not isinstance(access_validation, dict) or not (
        set(access["plans"]) == set(access["query_details"]) == set(access_validation)
    ):
        raise ValueError("access evidence is incomplete")
    if len(access["plans"]) != correctness["successful_samples"]:
        raise ValueError("round correctness evidence is incomplete")
    if any(
        not isinstance(evidence, dict)
        or evidence.get("mode") != "formal"
        or not isinstance(evidence.get("access_structure"), str)
        or not evidence["access_structure"].strip()
        or evidence["access_structure"] == "sequential-or-full-scan-diagnostic"
        for evidence in access_validation.values()
    ):
        raise ValueError("access evidence is incomplete")
    write = _require(record.get("write"), "write evidence is missing")
    if not isinstance(write, dict):
        raise ValueError("write evidence is missing")
    _integer(write.get("final_watermark"), "write evidence is missing", 1)
    maintenance = _require(record.get("maintenance"), "maintenance evidence is missing")
    if not isinstance(maintenance, dict) or not maintenance.get("completed") or not maintenance.get("watermarks"):
        raise ValueError("maintenance evidence is missing")
    watermarks = maintenance["watermarks"]
    if isinstance(watermarks, dict):
        for value in watermarks.values():
            _integer(value, "watermark evidence is incomplete", 1)
    if (
        not isinstance(watermarks, dict)
        or set(watermarks) != set(LAYOUT_TARGETS[layout])
        or set(watermarks.values()) != {write["final_watermark"]}
    ):
        raise ValueError("watermark evidence is incomplete")
    storage = _require(record.get("storage"), "storage evidence is missing")
    if (
        not isinstance(storage, dict)
        or not isinstance(storage.get("tables"), dict)
        or set(storage["tables"]) != set(LAYOUT_TARGETS[layout])
    ):
        raise ValueError("storage evidence is missing")
    storage_fields = STORAGE_TABLE_FIELDS[engine]
    for table in storage["tables"].values():
        if not isinstance(table, dict):
            raise ValueError("storage evidence is missing")
        for field in storage_fields:
            _integer(table.get(field), "storage evidence is missing")
    cleanup = _require(record.get("cleanup"), "cleanup evidence is missing")
    if not isinstance(cleanup, dict) or not cleanup.get("removed") or not cleanup.get("asset_directory_removed"):
        raise ValueError("cleanup evidence is missing")
    if engine == "clickhouse":
        if not isinstance(access.get("query_finish"), dict) or set(access["query_finish"]) != set(access["plans"]):
            raise ValueError("QueryFinish is missing")
        if any(
            not isinstance(finish, dict)
            or finish.get("type") != "QueryFinish"
            or any(
                not isinstance(finish.get(field), int)
                or isinstance(finish[field], bool)
                or finish[field] < 0
                for field in ("read_rows", "read_bytes")
            )
            for finish in access["query_finish"].values()
        ):
            raise ValueError("QueryFinish is invalid")
        if maintenance.get("natural_stable_parts") is not True:
            raise ValueError("natural stable part evidence is missing")
        if maintenance.get("optimize_final") is not False:
            raise ValueError("optimize_final must be false")
        if not isinstance(storage.get("merges"), list):
            raise ValueError("ClickHouse part/merge storage evidence is missing")


def validate_run(manifest: dict[str, object]) -> None:
    """验证一个 target manifest 已完成且拥有正式矩阵证据。"""
    if not isinstance(manifest, dict):
        raise ValueError("target manifest is invalid")
    if manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("target format/version is invalid")
    if manifest.get("status") != "complete":
        raise ValueError("status is not complete")
    if manifest.get("engine") not in {"opengauss", "clickhouse", "xstore"}:
        raise ValueError("target engine is invalid")
    if manifest.get("layout") not in LAYOUTS:
        raise ValueError("target layout is invalid")
    _identity(manifest.get("input"))
    workloads = _workload_scope(manifest)
    _validate_provenance(manifest, workloads)
    expected_schedule = [list(LAYOUTS[index:] + LAYOUTS[:index]) for index in range(4)]
    if manifest.get("latin_square") != expected_schedule:
        raise ValueError("Latin square order is invalid")
    records_by_workload = _round_records(manifest, workloads)
    correctness = _require(manifest.get("correctness"), "response-byte validation is missing")
    if not isinstance(correctness, dict) or not correctness.get("response_bytes_validated"):
        raise ValueError("response-byte validation is missing")
    _integer(correctness.get("rounds_complete"), "response-byte validation is missing")
    formal_samples = _integer(
        correctness.get("formal_samples"), "response-byte validation is missing", 1,
    )
    successful_samples = _integer(
        correctness.get("successful_samples"), "response-byte validation is missing", 1,
    )
    # 生产按 engine + workload 分片发布，只有 main 的 round 计入 rounds_complete。
    if (
        correctness["rounds_complete"] != len(records_by_workload.get("main", ()))
        or successful_samples != formal_samples
    ):
        raise ValueError("response-byte validation is missing")
    if not isinstance(manifest.get("global_cleanup"), dict) or not manifest["global_cleanup"].get("removed"):
        raise ValueError("cleanup evidence is missing")
    for workload, records in records_by_workload.items():
        positions = set()
        round_indexes = set()
        for record in records:
            round_index = record.get("round_index") if isinstance(record, dict) else None
            if round_index in round_indexes:
                raise ValueError("round index is duplicate")
            _validate_round(manifest, workload, record, positions)
            round_indexes.add(round_index)
        if workload in PERFORMANCE_WORKLOADS and positions != set(range(4)):
            raise ValueError("Latin position is incomplete")
        if workload in PERFORMANCE_WORKLOADS and round_indexes != set(range(4)):
            raise ValueError("round index is incomplete")
    observed_ddl = sorted({
        record["ddl_sha256"]
        for records in records_by_workload.values()
        for record in records
    })
    observed_queries = {
        workload: sorted({record["query_catalog_sha256"] for record in records})
        for workload, records in records_by_workload.items()
    }
    if (
        manifest["ddl_sha256"] != observed_ddl
        or manifest["query_catalog_sha256"] != observed_queries
    ):
        raise ValueError("provenance evidence is incomplete")


def _read_samples(path):
    """读取 target 的原始 JSONL 样本，不读取 manifest 的 pooled summary。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("samples.jsonl is unavailable") from error
    if not lines:
        raise ValueError("samples.jsonl is empty")
    try:
        values = [json.loads(line) for line in lines]
    except json.JSONDecodeError as error:
        raise ValueError("samples.jsonl is invalid") from error
    if not all(isinstance(value, dict) for value in values):
        raise ValueError("samples.jsonl records must be objects")
    return values


def _round_index(records):
    """建立 workload/round 到 manifest round evidence 的查找表。"""
    return {
        (workload, record["round_index"]): record
        for workload, rounds in records.items() for record in rounds
    }


def _validate_samples(manifest, samples):
    """验证 raw sample 可由相应 round 的访问和 truth 证据解释。"""
    records = _round_records(manifest, _workload_scope(manifest))
    index = _round_index(records)
    grouped = defaultdict(list)
    seen_ids = set()
    for sample in samples:
        workload = sample.get("workload")
        round_index = sample.get("round_index")
        _integer(round_index, "sample round evidence is missing")
        key = (workload, round_index)
        record = index.get(key)
        if record is None:
            raise ValueError("sample round evidence is missing")
        if sample.get("status") != "success":
            raise ValueError("failed sample is present")
        position = _integer(sample.get("position"), "sample Latin position mismatch")
        if position != record["position"]:
            raise ValueError("sample Latin position mismatch")
        scenario = sample.get("scenario")
        query_id = sample.get("query_id")
        if not isinstance(scenario, str) or not scenario or not isinstance(query_id, str) or not query_id:
            raise ValueError("sample scenario or query ID is invalid")
        if query_id in seen_ids:
            raise ValueError("sample query ID is duplicate")
        seen_ids.add(query_id)
        access = record["access"]
        if query_id not in access.get("plans", {}) or query_id not in access.get("query_details", {}):
            raise ValueError("sample access evidence is missing")
        if query_id not in record["access_validation"]:
            raise ValueError("sample access validation is missing")
        validated_access = record["access_validation"][query_id]
        if (
            not isinstance(validated_access, dict)
            or validated_access.get("scenario") != scenario
            or validated_access.get("kind") != sample.get("kind")
            or scenario.startswith("batch:") != (sample.get("kind") == "batch")
        ):
            raise ValueError("sample scenario/kind evidence is inconsistent")
        if manifest["engine"] == "clickhouse":
            finish = access.get("query_finish", {}).get(query_id)
            if not isinstance(finish, dict) or finish.get("type") != "QueryFinish":
                raise ValueError("QueryFinish is missing")
        for metric in METRICS:
            _number(sample.get(metric), f"{metric} is invalid")
        if scenario.startswith("batch:") and sample["application_ready_ms"] <= 0:
            raise ValueError("batch application_ready_ms must be positive")
        for field in ("database_response_bytes", "resolver_payload_bytes"):
            _integer(sample.get(field), f"{field} is invalid")
        _integer(sample.get("response_bytes"), "response_bytes is invalid", 1)
        _integer(sample.get("request_count"), "request_count is invalid", 1)
        if sample.get("database_protocol_bytes") is not None:
            _integer(sample["database_protocol_bytes"], "database_protocol_bytes is invalid")
        if sample["response_bytes"] != sample["database_response_bytes"] + sample["resolver_payload_bytes"]:
            raise ValueError("sample response bytes are inconsistent")
        validation = sample.get("validation")
        if not isinstance(validation, dict):
            raise ValueError("sample validation evidence is missing")
        _integer(validation.get("validated_payload_bytes"), "validated payload bytes are invalid")
        grouped[(workload, round_index, scenario)].append(sample)
    for workload, workload_records in records.items():
        for record in workload_records:
            round_groups = [items for (name, index_value, _), items in grouped.items()
                            if name == workload and index_value == record["round_index"]]
            if not round_groups:
                raise ValueError("successful samples are missing")
            sample_ids = {item["query_id"] for items in round_groups for item in items}
            if sample_ids != set(record["access"]["plans"]):
                raise ValueError("raw sample evidence does not match round access evidence")
            if workload not in PERFORMANCE_WORKLOADS:
                continue
            for items in round_groups:
                expected = 5 if items[0]["scenario"].startswith("batch:") else 30
                if len(items) != expected:
                    raise ValueError("round does not contain required successful samples")
    if len(samples) != manifest["correctness"]["formal_samples"]:
        raise ValueError("raw sample evidence does not match target correctness evidence")
    return grouped


def _round_summary(samples):
    """计算单一 scenario/round 的原始样本统计和独立 bytes 总量。"""
    summary = {metric: _distribution([sample[metric] for sample in samples]) for metric in METRICS}
    summary["response_bytes"] = {
        "database": sum(sample["database_response_bytes"] for sample in samples),
        "resolver_payload": sum(sample["resolver_payload_bytes"] for sample in samples),
        "total": sum(sample["response_bytes"] for sample in samples),
        "validated_payload": sum(sample["validation"]["validated_payload_bytes"] for sample in samples),
        "database_protocol": (
            sum(sample["database_protocol_bytes"] for sample in samples)
            if all(isinstance(sample.get("database_protocol_bytes"), int) for sample in samples)
            else "unavailable"
        ),
        "request_count": sum(sample["request_count"] for sample in samples),
    }
    if samples[0]["scenario"].startswith("batch:"):
        summary["throughput_mib_s"] = _distribution([
            sample["validation"]["validated_payload_bytes"] / (1024 * 1024)
            / (sample["application_ready_ms"] / 1000)
            for sample in samples
        ])
    return summary


def _median_round_summaries(rounds):
    """对四个 round 的同名统计量取中位数，保留 bytes 字段边界。"""
    result = {
        metric: {statistic: statistics.median(round_[metric][statistic] for round_ in rounds)
                 for statistic in STATISTICS}
        for metric in METRICS
    }
    response = {}
    for field in ("database", "resolver_payload", "total", "validated_payload", "request_count"):
        response[field] = statistics.median(round_["response_bytes"][field] for round_ in rounds)
    protocols = [round_["response_bytes"]["database_protocol"] for round_ in rounds]
    response["database_protocol"] = (
        statistics.median(protocols) if all(isinstance(value, int) for value in protocols) else "unavailable"
    )
    result["response_bytes"] = response
    if "throughput_mib_s" in rounds[0]:
        result["throughput_mib_s"] = {
            statistic: statistics.median(round_["throughput_mib_s"][statistic] for round_ in rounds)
            for statistic in STATISTICS
        }
    return result


def _write_summary(record):
    """提取单个 round 的写入完成证据，缺项或类型不符即失败。"""
    write = record.get("write")
    if not isinstance(write, dict):
        raise ValueError("write evidence is missing")
    summary = {
        field: _number(write.get(field), f"write {field} is invalid")
        for field in WRITE_RATE_FIELDS
    }
    for field in WRITE_COUNT_FIELDS:
        summary[field] = _integer(write.get(field), f"write {field} is invalid")
    # openGauss 适配器无法读取协议层提交字节，runner 写入 "unavailable"，不折算为 0。
    client_bytes = write.get(WRITE_CLIENT_BYTES_FIELD)
    if client_bytes != "unavailable":
        _integer(client_bytes, "write client submitted bytes are invalid")
    summary[WRITE_CLIENT_BYTES_FIELD] = client_bytes
    block_wall = write.get("block_wall_ms")
    if not isinstance(block_wall, dict):
        raise ValueError("write block_wall_ms is invalid")
    summary["block_wall_ms"] = {
        statistic: _number(block_wall.get(statistic), "write block_wall_ms is invalid")
        for statistic in WRITE_BLOCK_STATISTICS
    }
    return summary


def _storage_summary(record, engine, layout):
    """提取单个 round 的分表空间与 Asset 对象存储证据，两引擎字段各自保留。"""
    storage = record.get("storage")
    if (
        not isinstance(storage, dict)
        or not isinstance(storage.get("tables"), dict)
        or set(storage["tables"]) != set(LAYOUT_TARGETS[layout])
    ):
        raise ValueError("storage evidence is missing")
    tables = {}
    for name, table in storage["tables"].items():
        if not isinstance(table, dict):
            raise ValueError("storage evidence is missing")
        tables[name] = {
            field: _integer(table.get(field), f"storage {field} is invalid")
            for field in STORAGE_TABLE_FIELDS[engine]
        }
    asset_store = storage.get("asset_store")
    # 只有 asset_ref 有对象存储；其余 layout 的 null 表示没有存储，与空存储不同。
    if layout != "asset_ref":
        if "asset_store" not in storage:
            raise ValueError("asset store evidence is missing")
        if asset_store is not None:
            raise ValueError("asset store evidence must be absent")
    elif not isinstance(asset_store, dict):
        raise ValueError("asset store evidence is missing")
    else:
        asset_store = {
            field: _integer(asset_store.get(field), f"asset store {field} is invalid")
            for field in STORAGE_ASSET_FIELDS
        }
    return {"tables": tables, "asset_store": asset_store}


def _median_write_summaries(rounds):
    """对四个 round 的写入测量取中位数，保留 unavailable 边界。"""
    result = {
        field: statistics.median(round_[field] for round_ in rounds)
        for field in WRITE_RATE_FIELDS + WRITE_COUNT_FIELDS
    }
    client_bytes = [round_[WRITE_CLIENT_BYTES_FIELD] for round_ in rounds]
    result[WRITE_CLIENT_BYTES_FIELD] = (
        statistics.median(client_bytes)
        if all(isinstance(value, int) for value in client_bytes)
        else "unavailable"
    )
    result["block_wall_ms"] = {
        statistic: statistics.median(round_["block_wall_ms"][statistic] for round_ in rounds)
        for statistic in WRITE_BLOCK_STATISTICS
    }
    return result


def _median_storage_summaries(rounds):
    """对四个 round 的空间测量按表和字段取中位数，null 对象存储保持为 null。"""
    tables = {
        name: {
            field: statistics.median(round_["tables"][name][field] for round_ in rounds)
            for field in fields
        }
        for name, fields in rounds[0]["tables"].items()
    }
    asset_store = None
    if rounds[0]["asset_store"] is not None:
        asset_store = {
            field: statistics.median(round_["asset_store"][field] for round_ in rounds)
            for field in STORAGE_ASSET_FIELDS
        }
    return {"tables": tables, "asset_store": asset_store}


def _target_summary(manifest, samples):
    """构造单 target 的三组性能 workload 结果。"""
    grouped = _validate_samples(manifest, samples)
    records = _round_records(manifest, _workload_scope(manifest))
    engine, layout = manifest["engine"], manifest["layout"]
    workloads = {}
    for workload in PERFORMANCE_WORKLOADS:
        scenarios = {}
        names = sorted({scenario for name, _, scenario in grouped if name == workload})
        for scenario in names:
            round_summaries = [
                _round_summary(grouped[(workload, round_index, scenario)])
                for round_index in range(4)
            ]
            scenarios[scenario] = {
                "round_count": 4,
                "rounds": [
                    {"round_index": round_index, **round_summary}
                    for round_index, round_summary in enumerate(round_summaries)
                ],
                "round_statistic_median": _median_round_summaries(round_summaries),
            }
        write_rounds = [_write_summary(record) for record in records[workload]]
        storage_rounds = [
            _storage_summary(record, engine, layout) for record in records[workload]
        ]
        workloads[workload] = {
            "scenarios": scenarios,
            "write": {
                "round_count": 4,
                "round_statistic_median": _median_write_summaries(write_rounds),
            },
            "storage": {
                "round_count": 4,
                "round_statistic_median": _median_storage_summaries(storage_rounds),
            },
        }
    return {"engine": manifest["engine"], "layout": manifest["layout"], "workloads": workloads}


def summarize(runs: list[Path]) -> dict[str, object]:
    """从调用方显式选择的正式 target 或 single-workload shard 目录汇总主矩阵结果。"""
    if not runs:
        raise ValueError("at least one target directory is required")
    groups = {}
    input_identity = None
    for run in runs:
        target = Path(run)
        try:
            manifest = json.loads((target / "run-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("run-manifest.json is unavailable or invalid") from error
        validate_run(manifest)
        if input_identity is None:
            input_identity = manifest["input"]
        elif input_identity != manifest["input"]:
            raise ValueError("matrix input identity differs between targets")
        scope = _workload_scope(manifest)
        group = groups.get((manifest["engine"], manifest["layout"]))
        if group is None:
            group = groups[(manifest["engine"], manifest["layout"])] = {
                "code": manifest["code"], "workloads": {}, "samples": [],
            }
        elif manifest["code"] != group["code"]:
            raise ValueError("code evidence differs between matrix shards")
        if set(group["workloads"]) & set(scope):
            raise ValueError("duplicate matrix shard")
        for workload in scope:
            group["workloads"][workload] = manifest["workloads"][workload]
        group["samples"].extend(_read_samples(target / "samples.jsonl"))
    targets = []
    for (engine, layout), group in groups.items():
        # 每个 engine/layout 必须由精确覆盖四个 workload 的 shard 组装而成。
        if set(group["workloads"]) != set(ALL_WORKLOADS):
            raise ValueError("matrix shard coverage is incomplete")
        logical = {
            "engine": engine,
            "layout": layout,
            "workloads": group["workloads"],
            "correctness": {
                "formal_samples": sum(
                    record["correctness"]["formal_samples"]
                    for state in group["workloads"].values()
                    for record in state["rounds"]
                ),
            },
        }
        targets.append(_target_summary(logical, group["samples"]))
    return {
        "format": "agent-trace-json-storage-stage3-matrix-summary",
        "format_version": 1,
        "input": input_identity,
        "statistics_boundary": "round-first-four-round-median",
        "matrix": sorted(targets, key=lambda value: (value["engine"], value["layout"])),
    }


def _read_json_object(path, label):
    """读取并严格解析 JSON object，拒绝 NaN、Infinity 和非 object 文档。"""
    try:
        content = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error

    def reject_constant(value):
        raise ValueError(f"{label} contains non-finite JSON value: {value}")

    try:
        value = json.loads(content, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _part_state_runtime(envelope):
    """验证 production envelope 的固定引擎、操作与布局身份。"""
    runtime = envelope.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("operation") != "part-states"
        or runtime.get("engine") != "clickhouse"
        or runtime.get("layout") not in LAYOUTS
    ):
        raise ValueError("part-state runtime identity mismatch")
    endpoint = runtime.get("endpoint")
    if (
        not isinstance(endpoint, dict)
        or not isinstance(endpoint.get("host"), str) or not endpoint["host"]
        or not _integer(endpoint.get("port"), "part-state runtime evidence is invalid", 1)
    ):
        raise ValueError("part-state runtime evidence is invalid")
    container = runtime.get("container")
    if not isinstance(container, dict):
        raise ValueError("part-state runtime evidence is invalid")
    if container.get("mode") == "native-package":
        for field in ("engine_version", "package_checksums", "binary_sha256"):
            if not isinstance(container.get(field), str) or not container[field]:
                raise ValueError("part-state runtime evidence is invalid")
    elif any(
        not isinstance(container.get(field), str) or not container[field]
        for field in ("container", "image", "image_id")
    ):
        raise ValueError("part-state runtime evidence is invalid")
    engine_runtime = runtime.get("engine_runtime")
    if (
        not isinstance(engine_runtime, dict)
        or not isinstance(engine_runtime.get("version"), str) or not engine_runtime["version"]
        or engine_runtime.get("source") != "database-query"
    ):
        raise ValueError("part-state runtime evidence is invalid")
    host = runtime.get("host")
    if (
        not isinstance(host, dict)
        or not isinstance(host.get("platform"), str) or not host["platform"]
        or not isinstance(host.get("machine"), str) or not host["machine"]
    ):
        raise ValueError("part-state runtime evidence is invalid")
    for field in ("cpu_count", "memory_total_kib"):
        _integer(host.get(field), "part-state runtime evidence is invalid", 1)
    return runtime["layout"]


def _validate_part_state_envelope(envelope):
    """验证一个 part-state production envelope 的格式、身份和清理证据。"""
    if envelope.get("format") != PRODUCTION_FORMAT or envelope.get("format_version") != FORMAT_VERSION:
        raise ValueError("part-state production envelope format/version is invalid")
    if envelope.get("status") != "complete":
        raise ValueError("part-state production envelope status is not complete")
    if envelope.get("operation") != "part-states":
        raise ValueError("part-state operation is invalid")
    if not isinstance(envelope.get("run_id"), str) or not envelope["run_id"]:
        raise ValueError("part-state run identity is missing")
    layout = _part_state_runtime(envelope)
    _identity(envelope.get("input"))
    truth = envelope.get("truth")
    if (
        not isinstance(truth, dict)
        or set(truth) != {"seed", "identity_sha256", "record_count", "block_size", "block_count"}
        or not _sha256(truth.get("identity_sha256"))
        or truth["identity_sha256"] != envelope["input"]["identity_sha256"]
    ):
        raise ValueError("part-state truth identity is invalid")
    for field, expected in FORMAL_TRUTH.items():
        if _integer(truth.get(field), "part-state truth identity is invalid", 1) != expected:
            raise ValueError("part-state truth identity is invalid")
    if not _sha256(envelope.get("query_catalog_sha256")):
        raise ValueError("part-state query catalog identity is invalid")
    code = envelope.get("code")
    if not isinstance(code, dict) or not set(PART_STATE_CODE_ROLES) <= set(code):
        raise ValueError("part-state code evidence is incomplete")
    for evidence in code.values():
        if (
            not isinstance(evidence, dict)
            or not isinstance(evidence.get("path"), str)
            or not evidence["path"]
            or not _sha256(evidence.get("sha256"))
        ):
            raise ValueError("part-state code evidence is incomplete")
        _integer(evidence.get("bytes"), "part-state code evidence is incomplete", 1)
    policy = envelope.get("namespace_policy")
    if (
        not isinstance(policy, dict)
        or not isinstance(policy.get("namespace"), str)
        or not policy["namespace"]
    ):
        raise ValueError("part-state namespace evidence is missing")
    cleanup = envelope.get("cleanup")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("removed") is not True
        or cleanup.get("asset_directory_removed") is not True
        or not isinstance(cleanup.get("asset_directory_applicable"), bool)
        or cleanup["asset_directory_applicable"] != (layout == "asset_ref")
        or not isinstance(cleanup.get("namespace"), str)
        or not cleanup["namespace"]
    ):
        raise ValueError("part-state cleanup evidence is missing")
    return layout


def _read_part_state_child(run, envelope):
    """从运行目录重新读取 child bytes，并核对 envelope 记录的实际身份。"""
    evidence = envelope.get("child")
    relative = evidence.get("path") if isinstance(evidence, dict) else None
    if not isinstance(relative, str) or not relative:
        raise ValueError("part-state child evidence is missing")
    root = Path(run).resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("part-state child path escapes the run directory") from error
    _integer(evidence.get("bytes"), "part-state child evidence is incomplete", 1)
    if not _sha256(evidence.get("sha256")):
        raise ValueError("part-state child evidence is incomplete")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError("part-state child manifest is unavailable") from error
    if evidence["bytes"] != len(content) or evidence["sha256"] != hashlib.sha256(content).hexdigest():
        raise ValueError("part-state child identity mismatch")
    manifest = _read_json_object(path, "part-state child manifest")
    for key in ("format", "format_version", "status", "run_id"):
        if evidence.get(key) != manifest.get(key):
            raise ValueError("part-state child format/status mismatch")
    return manifest


def _part_state_tables(state, targets):
    """核对四个物理写目标的 part、mark 与 bytes 证据。"""
    tables = state.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(targets):
        raise ValueError("part-state table evidence is incomplete")
    result = {}
    for table in targets:
        values = tables[table]
        if not isinstance(values, dict):
            raise ValueError("part-state table metrics are invalid")
        result[table] = {
            metric: _integer(values.get(metric), "part-state table metrics are invalid")
            for metric in PART_STATE_TABLE_METRICS
        }
    return result


def _part_state_merges(value):
    """核对 active merge 列表的固定结构。"""
    if not isinstance(value, list):
        raise ValueError("part-state merge evidence is invalid")
    for merge in value:
        if not isinstance(merge, dict) or not isinstance(merge.get("table"), str) or not merge["table"]:
            raise ValueError("part-state merge evidence is invalid")
    return value


def _part_state_observations(value, targets):
    """核对状态观测的 part 计数与 merge 快照。"""
    if not isinstance(value, list) or not value:
        raise ValueError("part-state observations evidence is invalid")
    observations = []
    for observation in value:
        counts = observation.get("active_part_counts") if isinstance(observation, dict) else None
        if (
            not isinstance(counts, dict)
            or set(counts) != set(targets)
            or not isinstance(observation.get("active_merges"), list)
        ):
            raise ValueError("part-state observations evidence is invalid")
        observations.append({
            "active_part_counts": {
                table: _integer(counts[table], "part-state observations evidence is invalid")
                for table in targets
            },
            "active_merges": observation["active_merges"],
        })
    return observations


def _part_state_predicate(name, tables, merges, observations, controlled):
    """复现 runner 的四种状态谓词，拒绝 manifest 自报的 predicate_proven。"""
    counts = {table: values["part_count"] for table, values in tables.items()}
    if name == "fragmented":
        valid = not merges and counts[controlled] >= 2
    elif name == "merging":
        valid = any(merge["table"] == controlled for merge in merges)
    elif name == "stable":
        tail = observations[-3:]
        valid = (
            not merges
            and len(tail) == 3
            and all(not observation["active_merges"] for observation in tail)
            and all(observation["active_part_counts"] == counts for observation in tail)
        )
    else:
        valid = (
            not merges
            and counts[controlled] == 1
            and all(count <= 1 for count in counts.values())
        )
    if not valid:
        raise ValueError("part-state predicate evidence is inconsistent")


def _part_state_asset_store(state, layout):
    """要求 asset_ref 记录对象水位与零孤立对象，其它布局不携带 Asset 证据。"""
    value = state.get("asset_store")
    if layout != "asset_ref":
        if value is not None:
            raise ValueError("part-state asset store evidence is invalid")
        return None
    if not isinstance(value, dict):
        raise ValueError("part-state asset store evidence is invalid")
    for field in PART_STATE_ASSET_FIELDS:
        _integer(value.get(field), "part-state asset store evidence is invalid")
    if value["orphan_object_count"] != 0 or value["orphan_bytes"] != 0:
        raise ValueError("part-state asset store evidence is invalid")
    return {field: value[field] for field in PART_STATE_ASSET_FIELDS}


def _part_state_sample_numbers(sample, scenario):
    """核对单条成功样本的 bytes、validation 与四类时延。"""
    for metric in METRICS:
        _number(sample.get(metric), f"part-state {metric} is invalid")
    if scenario.startswith("batch:") and sample["application_ready_ms"] <= 0:
        raise ValueError("part-state batch application_ready_ms must be positive")
    response = _integer(sample.get("response_bytes"), "part-state response bytes are invalid", 1)
    database = _integer(
        sample.get("database_response_bytes"), "part-state response bytes are invalid"
    )
    resolver = _integer(
        sample.get("resolver_payload_bytes"), "part-state response bytes are invalid"
    )
    if response != database + resolver:
        raise ValueError("part-state response bytes are invalid")
    _integer(sample.get("request_count"), "part-state request count is invalid", 1)
    if sample.get("database_protocol_bytes") is not None:
        _integer(sample["database_protocol_bytes"], "part-state response bytes are invalid")
    _integer(sample.get("resolver_requests"), "part-state resolver_requests is invalid")
    _number(sample.get("resolver_read_ms"), "part-state resolver_read_ms is invalid")
    validation = sample.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("part-state validation evidence is invalid")
    _integer(validation.get("row_count"), "part-state validation evidence is invalid")
    _integer(validation.get("validated_payload_bytes"), "part-state validation evidence is invalid")


def _part_state_access(state, samples_by_id):
    """核对 plans、details 与 QueryFinish 对成功样本的精确覆盖和一致性。"""
    evidence = {
        name: state.get(name) for name in ("query_plans", "query_details", "query_finish")
    }
    if any(
        not isinstance(value, dict) or set(value) != set(samples_by_id)
        for value in evidence.values()
    ):
        raise ValueError("part-state access evidence is incomplete")
    for query_id, sample in samples_by_id.items():
        plan = evidence["query_plans"][query_id]
        detail = evidence["query_details"][query_id]
        finish = evidence["query_finish"][query_id]
        if not isinstance(plan, str) or not plan.strip():
            raise ValueError("part-state query plan is invalid")
        if (
            not isinstance(detail, dict)
            or not isinstance(detail.get("kind"), str) or not detail["kind"]
            or not isinstance(detail.get("statement"), str) or not detail["statement"].strip()
            or not isinstance(detail.get("declared_source"), str) or not detail["declared_source"]
        ):
            raise ValueError("part-state query detail evidence is invalid")
        scanned_rows = _integer(
            detail.get("scanned_rows"), "part-state query detail evidence is invalid"
        )
        scanned_bytes = _integer(
            detail.get("scanned_bytes"), "part-state query detail evidence is invalid"
        )
        if (
            not isinstance(finish, dict)
            or finish.get("type") != "QueryFinish"
            or not isinstance(finish.get("exception_code"), int)
            or isinstance(finish.get("exception_code"), bool)
            or finish["exception_code"] != 0
        ):
            raise ValueError("part-state QueryFinish evidence is invalid")
        read_rows = _integer(finish.get("read_rows"), "part-state QueryFinish evidence is invalid")
        read_bytes = _integer(finish.get("read_bytes"), "part-state QueryFinish evidence is invalid")
        if (
            detail["kind"] != sample["kind"]
            or scanned_rows != read_rows
            or scanned_bytes != read_bytes
            or read_rows < sample["validation"]["row_count"]
        ):
            raise ValueError("part-state query evidence is inconsistent")


def _part_state_scenarios(state):
    """从 raw query_samples 重算每个固定 scenario 的分布与 bytes 总量。"""
    samples = state.get("query_samples")
    expected_count = len(PART_STATE_SCENARIOS) * PART_STATE_SAMPLES_PER_QUERY
    if not isinstance(samples, list) or len(samples) != expected_count:
        raise ValueError("part-state query sample count mismatch")
    grouped = defaultdict(list)
    query_ids = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("part-state query sample evidence is invalid")
        if sample.get("status") != "success" or sample.get("error") is not None:
            raise ValueError("part-state failed sample is present")
        scenario = sample.get("scenario")
        if scenario not in PART_STATE_SCENARIO_KINDS:
            raise ValueError("part-state scenario evidence is invalid")
        if sample.get("kind") != PART_STATE_SCENARIO_KINDS[scenario]:
            raise ValueError("part-state scenario/kind evidence is inconsistent")
        query_id = sample.get("query_id")
        if not isinstance(query_id, str) or not query_id:
            raise ValueError("part-state query ID evidence is invalid")
        _part_state_sample_numbers(sample, scenario)
        grouped[scenario].append(sample)
        query_ids.append(query_id)
    # 先拒绝重复 ID，再核对 scenario 覆盖与访问证据，避免重复样本掩盖缺失场景。
    if len(set(query_ids)) != len(query_ids):
        raise ValueError("part-state query IDs are duplicated")
    if any(
        len(grouped[scenario]) != PART_STATE_SAMPLES_PER_QUERY
        for scenario in PART_STATE_SCENARIOS
    ):
        raise ValueError("part-state scenario coverage is incomplete")
    _part_state_access(state, {sample["query_id"]: sample for sample in samples})
    if (
        state.get("successful_samples") != expected_count
        or state.get("failed_samples") != 0
        or state.get("query_finish_count") != expected_count
    ):
        raise ValueError("part-state sample totals are invalid")
    summaries = {}
    for scenario in PART_STATE_SCENARIOS:
        values = grouped[scenario]
        summary = _round_summary(values)
        summary["resolver_requests"] = _distribution([
            sample["resolver_requests"] for sample in values
        ])
        summary["resolver_read_ms"] = _distribution([
            sample["resolver_read_ms"] for sample in values
        ])
        summaries[scenario] = summary
    return summaries


def _validate_part_state_child(manifest, layout):
    """按真实 production 门禁等价核对 child 的物理、恢复与查询证据。"""
    targets = LAYOUT_TARGETS[layout]
    controlled = PART_STATE_CONTROLLED_TABLES[layout]
    if (
        manifest.get("format") != PART_STATE_FORMAT
        or manifest.get("format_version") != FORMAT_VERSION
        or manifest.get("status") != "complete"
        or manifest.get("layout") != layout
    ):
        raise ValueError("part-state child format/status/layout mismatch")
    if manifest.get("physical_targets") != list(targets):
        raise ValueError("part-state child physical targets mismatch")
    if manifest.get("controlled_table") != controlled:
        raise ValueError("part-state child controlled table mismatch")
    if manifest.get("state_order") != list(PART_STATE_STATE_ORDER):
        raise ValueError("part-state child state order mismatch")
    if manifest.get("samples_per_query") != PART_STATE_SAMPLES_PER_QUERY:
        raise ValueError("part-state samples_per_query mismatch")
    if manifest.get("cache_limits") != PART_STATE_CACHE_LIMITS:
        raise ValueError("part-state cache limits mismatch")
    if "errors" in manifest:
        raise ValueError("part-state child contains errors")
    states = manifest.get("states")
    if not isinstance(states, list) or len(states) != len(PART_STATE_STATE_ORDER):
        raise ValueError("part-state states are incomplete")
    results = []
    asset_watermarks = set()
    for name, state in zip(PART_STATE_STATE_ORDER, states):
        if not isinstance(state, dict) or state.get("name") != name:
            raise ValueError("part-state state name mismatch")
        if state.get("controlled_table") != controlled:
            raise ValueError("part-state child controlled table mismatch")
        if state.get("predicate_proven") is not True or state.get("error") is not None:
            raise ValueError("part-state predicate evidence is inconsistent")
        tables = _part_state_tables(state, targets)
        merges = _part_state_merges(state.get("active_merges"))
        observations = _part_state_observations(state.get("observations"), targets)
        _part_state_predicate(name, tables, merges, observations, controlled)
        asset_store = _part_state_asset_store(state, layout)
        if asset_store is not None:
            asset_watermarks.add((asset_store["available_object_count"], asset_store["available_bytes"]))
        optimized = list(targets) if name == "single_part" else []
        if state.get("optimized_targets") != optimized:
            raise ValueError("part-state optimized targets mismatch")
        results.append({
            "name": name,
            "tables": tables,
            "active_merge_count": len(merges),
            "optimized_targets": optimized,
            "asset_store": asset_store,
            "scenarios": _part_state_scenarios(state),
        })
    if layout == "asset_ref" and len(asset_watermarks) != 1:
        raise ValueError("part-state asset store watermarks differ between states")
    restoration = manifest.get("restoration")
    merge_targets = [controlled] if layout == "asset_ref" else list(targets)
    if (
        not isinstance(restoration, dict)
        or restoration.get("attempted") is not True
        or restoration.get("restored") is not True
        or restoration.get("targets") != merge_targets
    ):
        raise ValueError("part-state restoration evidence is invalid")
    cleanup = manifest.get("cleanup")
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("removed") is not True
        or not isinstance(cleanup.get("namespace"), str)
        or not cleanup["namespace"]
    ):
        raise ValueError("part-state child cleanup evidence is invalid")
    return results, cleanup["namespace"]


def summarize_part_states(runs: list[Path]) -> dict[str, object]:
    """汇总四个布局各一个正式 part-state 控制，不做任何 main 矩阵配对。"""
    if not runs:
        raise ValueError("at least one part-state run directory is required")
    envelopes = {}
    for run in runs:
        envelope = _read_json_object(
            Path(run) / "run-manifest.json", "part-state production envelope"
        )
        layout = _validate_part_state_envelope(envelope)
        if layout in envelopes:
            raise ValueError(f"duplicate part-state layout: {layout}")
        envelopes[layout] = (Path(run), envelope)
    if set(envelopes) != set(LAYOUTS):
        raise ValueError("part-state layout coverage is incomplete")
    first = envelopes[LAYOUTS[0]][1]
    for name, field in (
        ("input", "input"), ("truth", "truth"), ("query catalog", "query_catalog_sha256"),
    ):
        if any(envelopes[layout][1][field] != first[field] for layout in LAYOUTS):
            raise ValueError(f"part-state {name} identity differs between runs")
    def _container_identity(container):
        if container.get("mode") == "native-package":
            return (container.get("binary_sha256"), container.get("package_checksums"))
        return (container.get("image"), container.get("image_id"))
    runtime_identity = (
        first["runtime"]["engine_runtime"]["version"],
        _container_identity(first["runtime"]["container"]),
    )
    if any(
        (
            envelopes[layout][1]["runtime"]["engine_runtime"]["version"],
            _container_identity(envelopes[layout][1]["runtime"]["container"]),
        ) != runtime_identity
        for layout in LAYOUTS
    ):
        raise ValueError("part-state runtime identity differs between runs")
    targets = []
    for layout in LAYOUTS:
        run, envelope = envelopes[layout]
        manifest = _read_part_state_child(run, envelope)
        states, namespace = _validate_part_state_child(manifest, layout)
        if namespace != envelope["cleanup"]["namespace"]:
            raise ValueError("part-state child cleanup evidence is invalid")
        targets.append({
            "layout": layout,
            "run_id": envelope["run_id"],
            "code": envelope["code"],
            "child": envelope["child"],
            "runtime": envelope["runtime"],
            "physical_targets": list(LAYOUT_TARGETS[layout]),
            "controlled_table": PART_STATE_CONTROLLED_TABLES[layout],
            "cache_limits": PART_STATE_CACHE_LIMITS,
            "samples_per_query": PART_STATE_SAMPLES_PER_QUERY,
            "cleanup": envelope["cleanup"],
            "states": states,
        })
    return {
        "format": PART_STATE_SUMMARY_FORMAT,
        "format_version": FORMAT_VERSION,
        "input": first["input"],
        "truth": first["truth"],
        "query_catalog_sha256": first["query_catalog_sha256"],
        "statistics_boundary": PART_STATE_STATISTICS_BOUNDARY,
        "state_order": list(PART_STATE_STATE_ORDER),
        "scenarios": list(PART_STATE_SCENARIOS),
        "part_states": targets,
    }


def _is_number(value, minimum=0):
    """判断 JSON 值是否为排除 bool 的有界有限数值。"""
    return type(value) in {int, float} and math.isfinite(value) and value >= minimum


def _is_integer(value, minimum=0):
    """判断 JSON 值是否为排除 bool 的有界整数。"""
    return type(value) is int and value >= minimum


def _matches_contract(value, expected):
    """按 JSON 节点类型和值精确核对固定契约。"""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _matches_contract(value[key], item) for key, item in expected.items()
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _matches_contract(item, wanted) for item, wanted in zip(value, expected)
        )
    return value == expected


def _matches_json_measurement(value, expected):
    """比较重算测量值与已发布值，浮点数使用固定容差。"""
    if expected is None or isinstance(expected, (bool, str)):
        return type(value) is type(expected) and value == expected
    if isinstance(expected, int):
        return type(value) is int and value == expected
    if isinstance(expected, float):
        return type(value) is float and math.isfinite(value) and math.isclose(
            value, expected, rel_tol=1e-9, abs_tol=1e-9,
        )
    if isinstance(expected, dict):
        return isinstance(value, dict) and set(value) == set(expected) and all(
            _matches_json_measurement(value[key], item) for key, item in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(value, list) and len(value) == len(expected) and all(
            _matches_json_measurement(item, wanted) for item, wanted in zip(value, expected)
        )
    return value == expected


def _nullable_distribution(values):
    """返回允许空样本的 minimum/p50/p95/maximum 分布。"""
    if not values:
        return {"minimum": None, "p50": None, "p95": None, "maximum": None}
    return _distribution(values)


def _load_json_line(raw, label):
    """解析单行 JSON object，拒绝非有限常量和非 object 记录。"""
    def reject_constant(value):
        raise ValueError(f"{label} contains non-finite JSON value: {value}")

    try:
        value = json.loads(raw, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _load_json_document(path, label):
    """流式读取 JSON object 文档，拒绝非有限常量与非 object 文档。"""
    def reject_constant(value):
        raise ValueError(f"{label} contains non-finite JSON value: {value}")

    try:
        with open(path, "rb") as handle:
            value = json.load(handle, parse_constant=reject_constant)
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _stream_identity(path):
    """流式重算文件 bytes 与 SHA-256，不把大文件整体读入内存。"""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _interference_schedules():
    """按 runner 固定 factory 生成 30/300 秒的 phase schedule。"""
    schedules = {}
    for phase in INTERFERENCE_PHASE_ORDER:
        streams = ["list", "preview"]
        interference_stream = INTERFERENCE_PHASE_STREAMS[phase]
        if interference_stream is not None:
            streams.append(interference_stream)
        schedules[phase] = {
            segment: {
                stream: {
                    "name": stream,
                    "rate_per_second": INTERFERENCE_STREAM_RATES[stream],
                    "duration_seconds": INTERFERENCE_SEGMENT_SECONDS[segment],
                    "workers": INTERFERENCE_STREAM_WORKERS[stream],
                    "timeout_seconds": INTERFERENCE_TIMEOUT_SECONDS,
                    "late_tolerance_seconds": INTERFERENCE_LATE_TOLERANCE_SECONDS,
                    "mode": INTERFERENCE_STREAM_MODES.get(stream, "fixed"),
                }
                for stream in streams
            }
            for segment in INTERFERENCE_SEGMENTS
        }
    return schedules


INTERFERENCE_SCHEDULES = _interference_schedules()


def _interference_runtime(envelope):
    """验证 interference runtime 的固定引擎与布局身份。"""
    runtime = envelope.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("operation") != "interference"
        or runtime.get("engine") != "clickhouse"
        or runtime.get("layout") not in LAYOUTS
    ):
        raise ValueError("interference runtime identity mismatch")
    endpoint = runtime.get("endpoint")
    if (
        not isinstance(endpoint, dict)
        or not isinstance(endpoint.get("host"), str) or not endpoint["host"]
        or not _integer(endpoint.get("port"), "interference runtime evidence is invalid", 1)
    ):
        raise ValueError("interference runtime evidence is invalid")
    container = runtime.get("container")
    if not isinstance(container, dict):
        raise ValueError("interference runtime evidence is invalid")
    if container.get("mode") == "native-package":
        for field in ("engine_version", "package_checksums", "binary_sha256"):
            if not isinstance(container.get(field), str) or not container[field]:
                raise ValueError("interference runtime evidence is invalid")
    elif any(
        not isinstance(container.get(field), str) or not container[field]
        for field in ("container", "image", "image_id")
    ):
        raise ValueError("interference runtime evidence is invalid")
    engine_runtime = runtime.get("engine_runtime")
    if (
        not isinstance(engine_runtime, dict)
        or not isinstance(engine_runtime.get("version"), str) or not engine_runtime["version"]
        or engine_runtime.get("source") != "database-query"
    ):
        raise ValueError("interference runtime evidence is invalid")
    host = runtime.get("host")
    if (
        not isinstance(host, dict)
        or not isinstance(host.get("platform"), str) or not host["platform"]
        or not isinstance(host.get("machine"), str) or not host["machine"]
    ):
        raise ValueError("interference runtime evidence is invalid")
    for field in ("cpu_count", "memory_total_kib"):
        _integer(host.get(field), "interference runtime evidence is invalid", 1)
    return runtime["layout"]


def _interference_runtime_identity(envelope):
    """返回跨运行比较的引擎版本与镜像身份。"""
    runtime = envelope["runtime"]
    container = runtime["container"]
    if container.get("mode") == "native-package":
        return (
            runtime["engine_runtime"]["version"],
            container.get("binary_sha256"), container.get("package_checksums"),
        )
    return (
        runtime["engine_runtime"]["version"], container["image"], container["image_id"],
    )


def _interference_code(code):
    """验证正式 code map 的固定角色与身份。"""
    if not isinstance(code, dict) or set(code) != INTERFERENCE_CODE_ROLES:
        raise ValueError("interference code evidence is incomplete")
    for evidence in code.values():
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"path", "bytes", "sha256"}
            or not isinstance(evidence.get("path"), str) or not evidence["path"]
            or not _sha256(evidence.get("sha256"))
        ):
            raise ValueError("interference code evidence is incomplete")
        _integer(evidence.get("bytes"), "interference code evidence is incomplete", 1)


def _interference_metadata(metadata, truth, query_digest):
    """验证固定 interference factory metadata，包含 45-block 选择与 digest。"""
    invalid = "interference metadata is invalid"
    if not isinstance(metadata, dict) or set(metadata) != INTERFERENCE_METADATA_FIELDS:
        raise ValueError(invalid)
    for field, expected in (
        ("seed", FORMAL_TRUTH["seed"]),
        ("final_watermark", FORMAL_TRUTH["record_count"]),
        ("block_size", truth["block_size"]),
        ("preload_block_count", truth["block_count"]),
    ):
        if _integer(metadata.get(field), invalid, 1) != expected:
            raise ValueError(invalid)
    if metadata.get("cyclic_replay") is not True:
        raise ValueError(invalid)
    indices = metadata.get("eligible_block_indices")
    if (
        not isinstance(indices, list)
        or any(not _is_integer(value) for value in indices)
        or indices != sorted(set(indices))
        or len(indices) != INTERFERENCE_ELIGIBLE_BLOCK_COUNT
        or (indices[-1] + 1) * truth["block_size"] > metadata["final_watermark"]
    ):
        raise ValueError(invalid)
    digests = metadata.get("eligible_block_sha256")
    if (
        not isinstance(digests, list)
        or len(digests) != len(indices)
        or any(not _sha256(value) for value in digests)
        or len(set(digests)) != len(digests)
        or _integer(metadata.get("eligible_block_count"), invalid, 1) != len(indices)
        or metadata.get("main_query_catalog_sha256") != query_digest
    ):
        raise ValueError(invalid)
    rules = metadata.get("selection_rules")
    window = rules.get("outside_query_window") if isinstance(rules, dict) else None
    if (
        not isinstance(rules, dict)
        or set(rules) != INTERFERENCE_SELECTION_FIELDS
        or _integer(rules.get("full_block_rows"), invalid, 1) != truth["block_size"]
        or rules.get("requires_main_payload") is not True
        or not _matches_contract(
            rules.get("query_scenarios"),
            {stream: scenario for stream, (_, scenario) in INTERFERENCE_QUERY_SCENARIOS.items()},
        )
        or not isinstance(window, dict)
        or set(window) != INTERFERENCE_QUERY_WINDOW_FIELDS
        or any(not isinstance(window.get(field), str) or not window[field]
               for field in INTERFERENCE_QUERY_WINDOW_FIELDS)
    ):
        raise ValueError(invalid)


def _interference_namespace_policy(policy):
    """验证 phase 独占 namespace 策略。"""
    invalid = "interference namespace policy is invalid"
    namespaces = policy.get("namespaces") if isinstance(policy, dict) else None
    if (
        not isinstance(policy, dict)
        or set(policy) != INTERFERENCE_NAMESPACE_POLICY_FIELDS
        or policy.get("strategy") != INTERFERENCE_NAMESPACE_STRATEGY
        or policy.get("reuse") is not False
        or policy.get("namespace_prefix") != INTERFERENCE_NAMESPACE_PREFIX
        or not _matches_contract(policy.get("phase_order"), list(INTERFERENCE_PHASE_ORDER))
        or not isinstance(namespaces, list)
        or len(namespaces) != len(INTERFERENCE_PHASE_ORDER)
        or any(not isinstance(namespace, str) or not namespace for namespace in namespaces)
        or len(set(namespaces)) != len(namespaces)
    ):
        raise ValueError(invalid)


def _interference_cleanup(cleanup, layout):
    """验证运行级 cleanup 与 Asset 目录证据。"""
    invalid = "interference cleanup evidence is missing"
    namespaces = cleanup.get("namespaces") if isinstance(cleanup, dict) else None
    if (
        not isinstance(cleanup, dict)
        or set(cleanup) != INTERFERENCE_CLEANUP_FIELDS
        or cleanup.get("namespaces_removed") is not True
        or cleanup.get("asset_directory_removed") is not True
        or cleanup.get("asset_directory_applicable") is not (layout == "asset_ref")
        or not isinstance(namespaces, list)
        or len(namespaces) != len(INTERFERENCE_PHASE_ORDER)
        or any(not isinstance(namespace, str) or not namespace for namespace in namespaces)
        or len(set(namespaces)) != len(namespaces)
    ):
        raise ValueError(invalid)


def _interference_child_evidence(evidence):
    """验证 envelope 记录的 child manifest 身份结构。"""
    invalid = "interference child evidence is missing"
    if (
        not isinstance(evidence, dict)
        or set(evidence) != INTERFERENCE_CHILD_FIELDS
        or evidence.get("path") != "child/run-manifest.json"
        or not _integer(evidence.get("bytes"), invalid, 1)
        or not _sha256(evidence.get("sha256"))
        or not _matches_contract(evidence.get("format"), INTERFERENCE_FORMAT)
        or not _matches_contract(evidence.get("format_version"), FORMAT_VERSION)
        or not _matches_contract(evidence.get("status"), "complete")
        or not isinstance(evidence.get("run_id"), str) or not evidence["run_id"]
    ):
        raise ValueError(invalid)


def _interference_artifacts(artifacts):
    """验证十六项 child artifact 的固定路径集合。"""
    invalid = "interference artifacts are incomplete"
    if not isinstance(artifacts, list) or len(artifacts) != len(INTERFERENCE_ARTIFACT_PATHS):
        raise ValueError(invalid)
    paths = []
    for item in artifacts:
        if (
            not isinstance(item, dict)
            or set(item) != INTERFERENCE_ARTIFACT_FIELDS
            or not isinstance(item.get("path"), str)
            or not _integer(item.get("bytes"), invalid, 1)
            or not _sha256(item.get("sha256"))
        ):
            raise ValueError(invalid)
        paths.append(item["path"])
    if len(set(paths)) != len(paths) or set(paths) != set(INTERFERENCE_ARTIFACT_PATHS):
        raise ValueError(invalid)


def _validate_interference_envelope(envelope):
    """验证一个 interference production envelope 的格式、身份与固定 metadata。"""
    if (
        not _matches_contract(envelope.get("format"), PRODUCTION_FORMAT)
        or not _matches_contract(envelope.get("format_version"), FORMAT_VERSION)
    ):
        raise ValueError("interference production envelope format/version is invalid")
    if not _matches_contract(envelope.get("status"), "complete"):
        raise ValueError("interference production envelope status is not complete")
    if not _matches_contract(envelope.get("operation"), "interference"):
        raise ValueError("interference operation is invalid")
    if not isinstance(envelope.get("run_id"), str) or not envelope["run_id"]:
        raise ValueError("interference run identity is missing")
    layout = _interference_runtime(envelope)
    _identity(envelope.get("input"))
    truth = envelope.get("truth")
    if (
        not isinstance(truth, dict)
        or set(truth) != {"seed", "identity_sha256", "record_count", "block_size", "block_count"}
        or not _sha256(truth.get("identity_sha256"))
        or truth["identity_sha256"] != envelope["input"]["identity_sha256"]
    ):
        raise ValueError("interference truth identity is invalid")
    for field, expected in FORMAL_TRUTH.items():
        if _integer(truth.get(field), "interference truth identity is invalid", 1) != expected:
            raise ValueError("interference truth identity is invalid")
    if not _sha256(envelope.get("query_catalog_sha256")):
        raise ValueError("interference query catalog identity is invalid")
    _interference_code(envelope.get("code"))
    _interference_metadata(envelope.get("interference"), truth, envelope["query_catalog_sha256"])
    _interference_namespace_policy(envelope.get("namespace_policy"))
    _interference_cleanup(envelope.get("cleanup"), layout)
    _interference_artifacts(envelope.get("artifacts"))
    _interference_child_evidence(envelope.get("child"))
    return layout


def _recompute_interference_artifacts(run):
    """流式重算十六项 child artifact 的 bytes/SHA，拒绝路径逃逸。"""
    root = Path(run).resolve()
    if (root / "child").is_symlink():
        raise ValueError("interference child directory is a symlink")
    child_root = (root / "child").resolve()
    identities = {}
    for relative in INTERFERENCE_ARTIFACT_PATHS:
        path = (root / relative).resolve()
        try:
            path.relative_to(child_root)
        except ValueError as error:
            raise ValueError("interference artifact path escapes the child directory") from error
        try:
            identities[relative] = {"path": relative, **_stream_identity(path)}
        except OSError as error:
            raise ValueError("interference artifact is unavailable") from error
    return identities


def _verify_interference_artifacts(artifacts, recomputed):
    """核对 envelope 记录的 artifact 身份与流式重算结果。"""
    declared = {item["path"]: item for item in artifacts}
    for relative, identity in recomputed.items():
        item = declared[relative]
        if item["bytes"] != identity["bytes"] or item["sha256"] != identity["sha256"]:
            raise ValueError("interference artifact identity mismatch")


def _read_interference_child(run, envelope):
    """读取 child root manifest，并核对 envelope 记录的实际 bytes/SHA。"""
    evidence = envelope["child"]
    root = Path(run).resolve()
    path = (root / evidence["path"]).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("interference child path escapes the run directory") from error
    try:
        identity = _stream_identity(path)
    except OSError as error:
        raise ValueError("interference child manifest is unavailable") from error
    if evidence["bytes"] != identity["bytes"] or evidence["sha256"] != identity["sha256"]:
        raise ValueError("interference child identity mismatch")
    manifest = _load_json_document(path, "interference child manifest")
    return manifest


def _verify_interference_child_evidence(evidence, manifest):
    """核对 envelope child 记录与 child manifest 的格式、状态和运行身份。"""
    for key in ("format", "format_version", "status", "run_id"):
        if evidence.get(key) != manifest.get(key):
            raise ValueError("interference child format/status mismatch")


def _validate_interference_root(manifest):
    """验证 child root 的固定正式身份并返回内嵌 phase 列表。"""
    fixed = {
        "format": INTERFERENCE_FORMAT,
        "format_version": FORMAT_VERSION,
        "status": "complete",
        "seed": FORMAL_TRUTH["seed"],
        "execution_scope": INTERFERENCE_SCOPE,
        "classification": INTERFERENCE_CLASSIFICATION,
        "phase_order": list(INTERFERENCE_PHASE_ORDER),
        "statistics_boundary": INTERFERENCE_STATISTICS_BOUNDARY,
    }
    if (
        any(not _matches_contract(manifest.get(key), value) for key, value in fixed.items())
        or any(key in manifest for key in ("error", "errors", "execution_resolution"))
        or not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]
    ):
        raise ValueError("interference root manifest is not formal complete")
    phases = manifest.get("phases")
    if (
        not isinstance(phases, list)
        or len(phases) != len(INTERFERENCE_PHASE_ORDER)
        or any(not isinstance(phase, dict) for phase in phases)
    ):
        raise ValueError("interference root phases are incomplete")
    return phases


def _interference_resources(resources):
    """验证正式 resource snapshot 的 CPU、内存与 I/O 观测。"""
    invalid = "interference resource snapshot is invalid"
    if not isinstance(resources, dict):
        raise ValueError(invalid)
    cpu = resources.get("cpu")
    ticks = cpu.get("ticks") if isinstance(cpu, dict) else None
    if (
        not isinstance(cpu, dict) or cpu.get("status") != "available"
        or not isinstance(ticks, list) or not ticks
        or any(not _is_integer(value) for value in ticks)
    ):
        raise ValueError(invalid)
    memory = resources.get("memory")
    if (
        not isinstance(memory, dict) or memory.get("status") != "available"
        or any(not _is_integer(memory.get(field)) for field in ("total_kib", "available_kib"))
        or memory["available_kib"] > memory["total_kib"]
    ):
        raise ValueError(invalid)
    io = resources.get("io")
    if (
        not isinstance(io, dict) or io.get("status") != "available"
        or any(
            not _is_integer(io.get(field), minimum)
            for field, minimum in (("devices", 1), ("read_sectors", 0), ("written_sectors", 0))
        )
    ):
        raise ValueError(invalid)
    return resources


def _interference_asset_store(storage, layout):
    """要求 asset_ref 记录 160 个对象水位，其它布局不携带 Asset 证据。"""
    value = storage.get("asset_store")
    invalid = "interference asset store evidence is invalid"
    if layout != "asset_ref":
        if value is not None:
            raise ValueError(invalid)
        return None
    if not isinstance(value, dict):
        raise ValueError(invalid)
    for field in PART_STATE_ASSET_FIELDS:
        _integer(value.get(field), invalid)
    if (
        value["available_object_count"] != INTERFERENCE_ASSET_OBJECTS
        or value["available_bytes"] != INTERFERENCE_ASSET_BYTES
        or value["orphan_object_count"] != 0
        or value["orphan_bytes"] != 0
    ):
        raise ValueError(invalid)
    return {field: value[field] for field in PART_STATE_ASSET_FIELDS}


def _interference_snapshots(manifest, layout):
    """验证三个资源/存储 snapshot 的顺序、物理计数与 Asset 水位。"""
    snapshots = manifest.get("snapshots")
    names = [
        item.get("name") if isinstance(item, dict) else None for item in snapshots
    ] if isinstance(snapshots, list) else None
    if names != list(INTERFERENCE_SNAPSHOT_NAMES):
        raise ValueError("interference snapshots are incomplete")
    targets = LAYOUT_TARGETS[layout]
    results = []
    for snapshot in snapshots:
        _interference_resources(snapshot.get("resources"))
        _number(snapshot.get("captured_offset_seconds"), "interference snapshot offset is invalid")
        storage = snapshot.get("storage")
        tables = storage.get("tables") if isinstance(storage, dict) else None
        merges = storage.get("merges") if isinstance(storage, dict) else None
        if (
            not isinstance(tables, dict) or set(tables) != set(targets)
            or not isinstance(merges, list)
            or any(
                not isinstance(merge, dict) or not isinstance(merge.get("table"), str)
                or not merge["table"] for merge in merges
            )
        ):
            raise ValueError("interference storage evidence is incomplete")
        normalized = {}
        for table, values in tables.items():
            if not isinstance(values, dict):
                raise ValueError("interference storage metrics are invalid")
            normalized[table] = {
                metric: _integer(values.get(metric), "interference storage metrics are invalid")
                for metric in INTERFERENCE_SNAPSHOT_TABLE_METRICS
            }
        backlog = sum(max(0, values["part_count"] - 1) for values in normalized.values())
        if (
            _integer(snapshot.get("active_part_backlog"), "interference storage totals mismatch")
            != backlog
            or _integer(snapshot.get("active_merge_count"), "interference storage totals mismatch")
            != len(merges)
        ):
            raise ValueError("interference storage totals mismatch")
        results.append({
            "name": snapshot["name"],
            "captured_offset_seconds": snapshot["captured_offset_seconds"],
            "resources": snapshot["resources"],
            "tables": normalized,
            "merges": merges,
            "active_part_backlog": backlog,
            "active_merge_count": len(merges),
            "asset_store": _interference_asset_store(storage, layout),
        })
    return results


def _interference_accumulator(stream):
    """建立一个 stream 的流式重算累加器。"""
    return {
        "stream": stream,
        "continuous": stream == "continuous_ingest",
        "counts": {field: 0 for field in INTERFERENCE_COUNT_FIELDS},
        "latency_values": [],
        "metrics_values": {metric: [] for metric in METRICS},
        "resolver_requests": [],
        "resolver_read_ms": [],
        "response_bytes": {
            "database": 0, "resolver_payload": 0, "total": 0, "validated_payload": 0,
            "request_count": 0, "database_protocol": 0, "protocol_complete": True,
        },
        "queries": {},
    }


def _interference_raw_evidence(row):
    """按 runner 契约验证一条 raw 的状态、时序与错误证据。"""
    status = row.get("status")
    if status not in INTERFERENCE_STATUS_FIELDS:
        raise ValueError("interference raw status is invalid")
    for field in ("scheduled_offset_seconds", "late_by_ms"):
        _number(row.get(field), "interference raw timing is invalid")
    started = row.get("started_offset_seconds")
    completed = row.get("completed_offset_seconds")
    if status == "dropped":
        if started is not None or completed is not None or row.get("sample") is not None:
            raise ValueError("interference raw dropped sample is inconsistent")
    elif (
        not _is_number(started)
        or (status in {"success", "failed"} and not _is_number(completed))
        or (completed is not None and (not _is_number(completed) or completed < started))
    ):
        raise ValueError("interference raw started/completed evidence is invalid")
    duration, application_ready = row.get("duration_ms"), row.get("application_ready_ms")
    if completed is None:
        if duration is not None or application_ready is not None:
            raise ValueError("interference raw incomplete timing is inconsistent")
    elif not _is_number(duration) or not _is_number(application_ready):
        raise ValueError("interference raw request duration is invalid")
    if status == "success" and row.get("error") is not None:
        raise ValueError("interference raw successful sample contains error")
    if status != "success" and (not isinstance(row.get("error"), str) or not row["error"]):
        raise ValueError("interference raw failed sample lacks error")
    return status


def _interference_success_evidence(row, accumulator, query_scenarios, write_tables,
                                   block_size, watermarks):
    """验证成功 raw 的目标证据并累加 bytes、时延与 resolver 分布。"""
    invalid = "interference successful query evidence is invalid"
    sample = row.get("sample")
    if accumulator["continuous"]:
        if (
            not isinstance(sample, dict)
            or not _is_integer(sample.get("rows"), 1) or sample["rows"] != block_size
            or not _is_integer(sample.get("watermark"), 1)
            or sample["watermark"] not in watermarks
            or not isinstance(sample.get("watermarks"), dict)
            or set(sample["watermarks"]) != set(write_tables)
            or any(not _is_integer(value) for value in sample["watermarks"].values())
            or any(value != sample["watermark"] for value in sample["watermarks"].values())
        ):
            raise ValueError("interference continuous_ingest success lacks BlockResult")
        return
    expected = query_scenarios.get(accumulator["stream"])
    validation = sample.get("validation") if isinstance(sample, dict) else None
    if (
        expected is None or not isinstance(sample, dict)
        or sample.get("kind") != expected[0] or sample.get("scenario") != expected[1]
        or sample.get("status") != "success"
        or not isinstance(sample.get("query_id"), str) or not sample["query_id"]
        or sample["query_id"] in accumulator["queries"]
        or not isinstance(validation, dict)
    ):
        raise ValueError(invalid)
    response = _integer(sample.get("response_bytes"), invalid, 1)
    database = _integer(sample.get("database_response_bytes"), invalid)
    resolver_payload = _integer(sample.get("resolver_payload_bytes"), invalid)
    request_count = _integer(sample.get("request_count"), invalid, 1)
    resolver_requests = _integer(sample.get("resolver_requests"), invalid)
    resolver_read_ms = _number(sample.get("resolver_read_ms"), invalid)
    protocol = sample.get("database_protocol_bytes")
    if protocol is not None:
        _integer(protocol, invalid)
    rows = _integer(validation.get("row_count"), invalid)
    validated = _integer(validation.get("validated_payload_bytes"), invalid)
    for metric in METRICS:
        _number(sample.get(metric), invalid)
    if response != database + resolver_payload:
        raise ValueError(invalid)
    if sample["application_ready_ms"] <= 0 and sample["scenario"].startswith("batch:"):
        raise ValueError(invalid)
    accumulator["queries"][sample["query_id"]] = {"kind": sample["kind"], "row_count": rows}
    for metric in METRICS:
        accumulator["metrics_values"][metric].append(sample[metric])
    accumulator["resolver_requests"].append(resolver_requests)
    accumulator["resolver_read_ms"].append(resolver_read_ms)
    totals = accumulator["response_bytes"]
    totals["database"] += database
    totals["resolver_payload"] += resolver_payload
    totals["total"] += response
    totals["validated_payload"] += validated
    totals["request_count"] += request_count
    if protocol is None:
        totals["protocol_complete"] = False
    else:
        totals["database_protocol"] += protocol


def _parse_interference_raw(path, schedules, *, query_scenarios, write_tables,
                            block_size, watermarks, horizon):
    """流式重算一个 raw JSONL 的 stream 计数、时延与成功 query 证据，horizon 为完成时刻上界。"""
    path = Path(path)
    if not path.is_file():
        raise ValueError("interference raw file is unavailable")
    if not isinstance(schedules, dict) or not schedules:
        raise ValueError("interference schedules mismatch")
    accumulators = {stream: _interference_accumulator(stream) for stream in schedules}
    sequences = {stream: [] for stream in schedules}
    offsets = {stream: [] for stream in schedules}
    with open(path, "rb") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError("interference raw file is empty or contains an empty line")
            row = _load_json_line(line, f"interference raw line {number}")
            if set(row) != INTERFERENCE_RAW_FIELDS or row.get("stream") not in accumulators:
                raise ValueError("interference raw fields or stream are invalid")
            stream = row["stream"]
            schedule = schedules[stream]
            status = _interference_raw_evidence(row)
            if (
                row["completed_offset_seconds"] is not None
                and row["completed_offset_seconds"] > horizon
            ):
                raise ValueError("interference raw completion exceeds the published wall")
            if (
                row["started_offset_seconds"] is not None
                and row["started_offset_seconds"] > horizon
            ):
                raise ValueError("interference raw start exceeds the published wall")
            tolerance = _number(
                schedule.get("late_tolerance_seconds"), "interference schedules mismatch"
            )
            counts = accumulators[stream]["counts"]
            counts[INTERFERENCE_STATUS_FIELDS[status]] += 1
            counts["started_requests"] += row["started_offset_seconds"] is not None
            counts["completed_requests"] += row["completed_offset_seconds"] is not None
            counts["late_requests"] += row["late_by_ms"] > tolerance * 1000
            sequences[stream].append(row["sequence"])
            offsets[stream].append(row["scheduled_offset_seconds"])
            if status == "success":
                _interference_success_evidence(
                    row, accumulators[stream], query_scenarios, write_tables,
                    block_size, watermarks,
                )
                accumulators[stream]["latency_values"].append(row["application_ready_ms"])
    results = {}
    for stream, accumulator in accumulators.items():
        rows = sequences[stream]
        if not rows:
            raise ValueError("interference raw stream is missing")
        if (
            any(not _is_integer(value) for value in rows)
            or rows != list(range(len(rows)))
        ):
            raise ValueError("interference raw sequence is not contiguous")
        accumulator["counts"]["scheduled_requests"] = len(rows)
        if schedules[stream].get("mode") == "fixed":
            mismatch = "interference schedules mismatch"
            duration = _number(schedules[stream].get("duration_seconds"), mismatch)
            rate = _number(schedules[stream].get("rate_per_second"), mismatch)
            if len(rows) != math.ceil(duration * rate):
                raise ValueError("interference raw fixed scheduled count is invalid")
            # 偏移必须唯一定位到自身 sequence，容差远小于一个到达间隔。
            if any(
                not math.isclose(
                    offset, sequence / rate,
                    rel_tol=0.0, abs_tol=INTERFERENCE_OFFSET_TOLERANCE_SECONDS,
                )
                for sequence, offset in zip(rows, offsets[stream])
            ):
                raise ValueError("interference raw scheduled offset is off the published rate")
        elif any(offset > horizon for offset in offsets[stream]):
            raise ValueError("interference raw scheduled offset exceeds the published wall")
        results[stream] = accumulator
    return results


def _interference_latency(values):
    """按固定阈值发布 application-ready 时延，p99 至少需要 1,000 个成功样本。"""
    publishable = len(values) >= INTERFERENCE_P99_MINIMUM_SUCCESSES
    return {
        "minimum": min(values) if values else None,
        "p50": _percentile(values, 0.50) if values else None,
        "p95": _percentile(values, 0.95) if values else None,
        "p99": _percentile(values, 0.99) if publishable else None,
        "maximum": max(values) if values else None,
        "p99_status": (
            "publishable" if publishable else "unavailable_insufficient_successes"
        ),
        "p99_minimum_successes": INTERFERENCE_P99_MINIMUM_SUCCESSES,
    }


def _interference_stream_statistics(parsed, schedule, phase_wall_seconds):
    """按固定定义重算一个 stream 的计数、吞吐、时延与 bytes 分布。"""
    invalid = "interference summary publication evidence is invalid"
    wall = _number(phase_wall_seconds, invalid)
    if wall <= 0:
        raise ValueError(invalid)
    counts = parsed["counts"]
    result = {
        "counts": {field: counts[field] for field in INTERFERENCE_COUNT_FIELDS},
        "offered_rate_requests_s": schedule.get("rate_per_second"),
        "phase_wall_seconds": wall,
        "completed_throughput_requests_s": counts["successful_requests"] / wall,
        "latency_ms": _interference_latency(parsed["latency_values"]),
    }
    if parsed["continuous"]:
        return result
    totals = parsed["response_bytes"]
    result["metrics"] = {
        metric: _nullable_distribution(parsed["metrics_values"][metric]) for metric in METRICS
    }
    result["response_bytes"] = {
        "database": totals["database"],
        "resolver_payload": totals["resolver_payload"],
        "total": totals["total"],
        "validated_payload": totals["validated_payload"],
        "request_count": totals["request_count"],
        "database_protocol": (
            totals["database_protocol"] if totals["protocol_complete"] else "unavailable"
        ),
    }
    result["resolver_requests"] = _nullable_distribution(parsed["resolver_requests"])
    result["resolver_read_ms"] = _nullable_distribution(parsed["resolver_read_ms"])
    return result


def _gate_interference_summary(published, statistics, schedules):
    """交叉核对 raw 重算计数、吞吐与时延发布证据。"""
    if not isinstance(published, dict) or set(published) != set(schedules):
        raise ValueError("interference summary streams mismatch")
    for stream, schedule in schedules.items():
        item = published.get(stream)
        result = statistics[stream]
        if (
            not isinstance(item, dict)
            or set(item) != INTERFERENCE_PUBLICATION_FIELDS
            or not isinstance(item.get("latency_ms"), dict)
            or set(item["latency_ms"]) != INTERFERENCE_LATENCY_FIELDS
        ):
            raise ValueError("interference summary publication evidence is invalid")
        for field, value in result["counts"].items():
            if not _is_integer(item.get(field)) or item[field] != value:
                raise ValueError("interference raw and summary counts mismatch")
        if (
            not _matches_json_measurement(
                item.get("offered_rate_requests_s"), schedule["rate_per_second"]
            )
            or not _matches_json_measurement(
                item.get("phase_wall_seconds"), result["phase_wall_seconds"]
            )
            or not _matches_json_measurement(
                item.get("completed_throughput_requests_s"),
                result["completed_throughput_requests_s"],
            )
            or not _matches_json_measurement(item.get("latency_ms"), result["latency_ms"])
        ):
            raise ValueError("interference summary publication evidence is invalid")


def _gate_interference_access(manifest, queries):
    """核对成功 query 与 plan/detail/QueryFinish 的一一对应。"""
    grouped = defaultdict(dict)
    for query_id, (stream, evidence) in queries.items():
        grouped[stream][query_id] = evidence
    if not grouped or any(not values for values in grouped.values()):
        raise ValueError("interference query stream lacks a success")
    evidence = manifest.get("query_evidence")
    if (
        not isinstance(evidence, dict)
        or set(evidence) != INTERFERENCE_QUERY_EVIDENCE_FIELDS
    ):
        raise ValueError("interference query evidence is missing")
    expected = set(queries)
    for key in ("plans", "query_details", "query_finish"):
        if not isinstance(evidence.get(key), dict) or set(evidence[key]) != expected:
            raise ValueError("interference query evidence IDs mismatch")
    # index_scans 按索引名聚合，与 query ID 无关，只核对计数值域。
    scans = evidence["index_scans"]
    if not isinstance(scans, dict):
        raise ValueError("interference query evidence IDs mismatch")
    for count in scans.values():
        _integer(count, "interference query evidence IDs mismatch")
    for query_id, (_, planned) in queries.items():
        plan = evidence["plans"][query_id]
        detail = evidence["query_details"][query_id]
        finish = evidence["query_finish"][query_id]
        if not isinstance(plan, str) or not plan.strip():
            raise ValueError("interference query plan is invalid")
        if (
            not isinstance(detail, dict) or detail.get("kind") != planned["kind"]
            or not isinstance(detail.get("statement"), str) or not detail["statement"].strip()
            or not isinstance(detail.get("declared_source"), str)
            or not detail["declared_source"]
        ):
            raise ValueError("interference query detail evidence is invalid")
        invalid = "interference query detail evidence is invalid"
        scanned_rows = _integer(detail.get("scanned_rows"), invalid)
        scanned_bytes = _integer(detail.get("scanned_bytes"), invalid)
        if (
            not isinstance(finish, dict) or finish.get("type") != "QueryFinish"
            or not _is_integer(finish.get("exception_code")) or finish["exception_code"] != 0
        ):
            raise ValueError("interference QueryFinish evidence is invalid")
        invalid = "interference QueryFinish evidence is invalid"
        read_rows = _integer(finish.get("read_rows"), invalid)
        read_bytes = _integer(finish.get("read_bytes"), invalid)
        if (
            scanned_rows != read_rows or scanned_bytes != read_bytes
            or read_rows < planned["row_count"]
        ):
            raise ValueError("interference query evidence is inconsistent")


def _validate_interference_phase(run, phase, manifest, layout, metadata, schedules,
                                 segment_seconds):
    """验证单个 phase 的固定 manifest、snapshot、raw 与访问证据。"""
    expected = {
        "format": INTERFERENCE_PHASE_FORMAT,
        "format_version": FORMAT_VERSION,
        "status": "complete",
        "phase": phase,
        "seed": FORMAL_TRUTH["seed"],
        "execution_scope": INTERFERENCE_SCOPE,
        "classification": INTERFERENCE_CLASSIFICATION,
        "layout": layout,
        "cache_state": INTERFERENCE_CACHE_STATE,
        "warmup_seconds": segment_seconds["warmup"],
        "measurement_seconds": segment_seconds["measurement"],
    }
    if any(not _matches_contract(manifest.get(key), value) for key, value in expected.items()):
        raise ValueError("interference phase manifest mismatch")
    if any(key in manifest for key in ("error", "errors", "execution_resolution")):
        raise ValueError("interference phase manifest is not formal complete")
    _integer(manifest.get("child_pid"), "interference phase manifest mismatch", 1)
    namespace = manifest.get("namespace")
    prefix = INTERFERENCE_NAMESPACE_PREFIX.replace("<phase>", phase)
    if (
        not isinstance(namespace, str)
        or not namespace.startswith(prefix)
        or len(namespace) <= len(prefix)
    ):
        raise ValueError("interference phase namespaces are invalid or duplicated")
    database = f"{namespace}_{layout}"
    cleanup = manifest.get("cleanup")
    if (
        not isinstance(cleanup, dict) or set(cleanup) != {"namespace", "removed"}
        or cleanup.get("removed") is not True or cleanup.get("namespace") != database
    ):
        raise ValueError("interference phase cleanup evidence is invalid")
    definition = manifest.get("layout_definition")
    if (
        not isinstance(definition, dict) or definition.get("database") != database
        or not isinstance(definition.get("ddl"), str) or not definition["ddl"].strip()
    ):
        raise ValueError("interference phase layout definition is invalid")
    if not _matches_contract(manifest.get("schedules"), schedules[phase]):
        raise ValueError("interference schedules mismatch")
    coverage = manifest.get("execution_coverage")
    if (
        not isinstance(coverage, dict)
        or set(coverage) != {f"{segment}_actual_seconds" for segment in INTERFERENCE_SEGMENTS}
    ):
        raise ValueError("interference formal coverage is short")
    for segment in INTERFERENCE_SEGMENTS:
        actual = _number(
            coverage.get(f"{segment}_actual_seconds"), "interference formal coverage is short"
        )
        if actual < segment_seconds[segment]:
            raise ValueError("interference formal coverage is short")
    snapshots = _interference_snapshots(manifest, layout)
    watermarks = frozenset(
        (index + 1) * metadata["block_size"] for index in metadata["eligible_block_indices"]
    )
    streams, queries, walls = {}, {}, {}
    for segment in INTERFERENCE_SEGMENTS:
        parsed = _parse_interference_raw(
            Path(run) / "child" / phase / INTERFERENCE_SEGMENT_FILES[segment],
            schedules[phase][segment], query_scenarios=INTERFERENCE_QUERY_SCENARIOS,
            write_tables=LAYOUT_TARGETS[layout], block_size=metadata["block_size"],
            watermarks=watermarks, horizon=coverage[f"{segment}_actual_seconds"],
        )
        published = manifest.get("warmup" if segment == "warmup" else "statistics")
        if not isinstance(published, dict) or set(published) != set(schedules[phase][segment]):
            raise ValueError("interference summary streams mismatch")
        statistics = {}
        walls[segment] = {}
        for stream in parsed:
            item = published[stream]
            wall = _number(
                item.get("phase_wall_seconds") if isinstance(item, dict) else None,
                "interference summary publication evidence is invalid",
            )
            statistics[stream] = _interference_stream_statistics(
                parsed[stream], schedules[phase][segment][stream], wall
            )
            walls[segment][stream] = wall
        _gate_interference_summary(published, statistics, schedules[phase][segment])
        for stream, item in parsed.items():
            if item["continuous"]:
                continue
            for query_id, evidence in item["queries"].items():
                if query_id in queries:
                    raise ValueError("interference query IDs are duplicated across segments")
                queries[query_id] = (stream, evidence)
        if segment == "measurement":
            streams = statistics
    # 连续 stream 的末次迭代只能把 wall 拖出固定的余量。
    for segment in INTERFERENCE_SEGMENTS:
        if (
            not math.isclose(
                coverage[f"{segment}_actual_seconds"], min(walls[segment].values()),
                rel_tol=1e-9, abs_tol=1e-9,
            )
            or coverage[f"{segment}_actual_seconds"]
            > segment_seconds[segment] + INTERFERENCE_COVERAGE_OVERRUN_SECONDS
        ):
            raise ValueError("interference formal coverage is inconsistent")
    _gate_interference_access(manifest, queries)
    return {
        "phase": phase,
        "namespace": namespace,
        "execution_coverage": {
            f"{segment}_actual_seconds": coverage[f"{segment}_actual_seconds"]
            for segment in INTERFERENCE_SEGMENTS
        },
        "schedules": manifest["schedules"],
        "snapshots": snapshots,
        "streams": streams,
    }


def summarize_interference(runs):
    """汇总四布局各一个正式 interference 控制，不生成 main 矩阵配对结果。"""
    return _summarize_interference(
        runs, schedules=INTERFERENCE_SCHEDULES,
        segment_seconds=dict(INTERFERENCE_SEGMENT_SECONDS),
    )


def _summarize_interference(runs, *, schedules, segment_seconds):
    """按注入的固定 schedule 契约汇总 interference 控制；上层固定 30/300 秒。"""
    if not runs:
        raise ValueError("at least one interference run directory is required")
    envelopes = {}
    for run in runs:
        envelope = _load_json_document(
            Path(run) / "run-manifest.json", "interference production envelope"
        )
        layout = _validate_interference_envelope(envelope)
        if layout in envelopes:
            raise ValueError(f"duplicate interference layout: {layout}")
        envelopes[layout] = (Path(run), envelope)
    if set(envelopes) != set(LAYOUTS):
        raise ValueError("interference layout coverage is incomplete")
    first = envelopes[LAYOUTS[0]][1]
    for name, field in (
        ("input identity", "input"), ("truth identity", "truth"),
        ("query catalog identity", "query_catalog_sha256"),
        ("metadata", "interference"), ("code evidence", "code"),
    ):
        if any(envelopes[layout][1][field] != first[field] for layout in LAYOUTS):
            raise ValueError(f"interference {name} differs between runs")
    identities = [
        (envelopes[layout][1]["run_id"], envelopes[layout][1]["child"]["run_id"])
        for layout in LAYOUTS
    ]
    if any(len({item[index] for item in identities}) != len(identities) for index in (0, 1)):
        raise ValueError("interference run identity is duplicated between runs")
    runtime_identity = _interference_runtime_identity(first)
    if any(
        _interference_runtime_identity(envelopes[layout][1]) != runtime_identity
        for layout in LAYOUTS
    ):
        raise ValueError("interference runtime identity differs between runs")
    layouts = []
    for layout in LAYOUTS:
        run, envelope = envelopes[layout]
        recomputed = _recompute_interference_artifacts(run)
        _verify_interference_artifacts(envelope["artifacts"], recomputed)
        manifest = _read_interference_child(run, envelope)
        embedded = _validate_interference_root(manifest)
        _verify_interference_child_evidence(envelope["child"], manifest)
        manifest = None
        namespaces, phases = set(), []
        for index, phase in enumerate(INTERFERENCE_PHASE_ORDER):
            phase_manifest = _load_json_document(
                run / "child" / phase / "run-manifest.json", "interference phase manifest"
            )
            if phase_manifest != embedded[index]:
                raise ValueError("interference phase file and root value mismatch")
            result = _validate_interference_phase(
                run, phase, phase_manifest, layout, envelope["interference"],
                schedules, segment_seconds,
            )
            if result["namespace"] in namespaces:
                raise ValueError("interference phase namespaces are invalid or duplicated")
            namespaces.add(result["namespace"])
            embedded[index] = None
            phases.append(result)
        if (
            namespaces != set(envelope["cleanup"]["namespaces"])
            or namespaces != set(envelope["namespace_policy"]["namespaces"])
        ):
            raise ValueError("interference cleanup evidence is missing")
        layouts.append({
            "layout": layout,
            "run_id": envelope["run_id"],
            "code": envelope["code"],
            "runtime": envelope["runtime"],
            "child": envelope["child"],
            "namespace_policy": envelope["namespace_policy"],
            "cleanup": envelope["cleanup"],
            "artifacts": [recomputed[relative] for relative in INTERFERENCE_ARTIFACT_PATHS],
            "phases": phases,
        })
        embedded = None
    return {
        "format": INTERFERENCE_SUMMARY_FORMAT,
        "format_version": FORMAT_VERSION,
        "input": first["input"],
        "truth": first["truth"],
        "query_catalog_sha256": first["query_catalog_sha256"],
        "interference": first["interference"],
        "statistics_boundary": INTERFERENCE_STATISTICS_BOUNDARY,
        "phase_order": list(INTERFERENCE_PHASE_ORDER),
        "layouts": layouts,
    }


def _asset_failure_runtime(envelope):
    """验证 asset-failure envelope 的固定操作、引擎与 asset_ref 布局身份。"""
    runtime = envelope.get("runtime")
    if (
        not isinstance(runtime, dict)
        or runtime.get("operation") != "asset-failures"
        or runtime.get("engine") not in ASSET_FAILURE_ENGINES
        or runtime.get("layout") != ASSET_FAILURE_LAYOUT
    ):
        raise ValueError("asset-failure runtime identity mismatch")
    endpoint = runtime.get("endpoint")
    if (
        not isinstance(endpoint, dict)
        or not isinstance(endpoint.get("host"), str) or not endpoint["host"]
    ):
        raise ValueError("asset-failure runtime evidence is invalid")
    _integer(endpoint.get("port"), "asset-failure runtime evidence is invalid", 1)
    container = runtime.get("container")
    if not isinstance(container, dict):
        raise ValueError("asset-failure runtime evidence is invalid")
    if container.get("mode") == "native-package":
        for field in ("engine_version", "package_checksums", "binary_sha256"):
            if not isinstance(container.get(field), str) or not container[field]:
                raise ValueError("asset-failure runtime evidence is invalid")
    elif any(
        not isinstance(container.get(field), str) or not container[field]
        for field in ("container", "image", "image_id")
    ):
        raise ValueError("asset-failure runtime evidence is invalid")
    engine_runtime = runtime.get("engine_runtime")
    if (
        not isinstance(engine_runtime, dict)
        or not isinstance(engine_runtime.get("version"), str) or not engine_runtime["version"]
        or engine_runtime.get("source") != "database-query"
    ):
        raise ValueError("asset-failure runtime evidence is invalid")
    host = runtime.get("host")
    if (
        not isinstance(host, dict)
        or not isinstance(host.get("platform"), str) or not host["platform"]
        or not isinstance(host.get("machine"), str) or not host["machine"]
    ):
        raise ValueError("asset-failure runtime evidence is invalid")
    for field in ("cpu_count", "memory_total_kib"):
        _integer(host.get(field), "asset-failure runtime evidence is invalid", 1)
    return runtime["engine"]


def _asset_failure_code(code):
    """核对 asset-failure runner 及其依赖的代码身份证据。"""
    if not isinstance(code, dict) or set(code) != set(ASSET_FAILURE_CODE_ROLES):
        raise ValueError("asset-failure code evidence is incomplete")
    for evidence in code.values():
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"path", "bytes", "sha256"}
            or not isinstance(evidence.get("path"), str)
            or not evidence["path"]
            or not _sha256(evidence.get("sha256"))
        ):
            raise ValueError("asset-failure code evidence is incomplete")
        _integer(evidence.get("bytes"), "asset-failure code evidence is incomplete", 1)


def _asset_failure_namespaces(value, message):
    """核对按案例数量给出的互不相同的命名空间列表。"""
    if (
        not isinstance(value, list)
        or len(value) != len(ASSET_FAILURE_CASE_ORDER)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(message)
    return list(value)


def _asset_failure_cleanup(cleanup):
    """核对 envelope 级命名空间、对象目录与运行探针目录的清理证据。"""
    if not isinstance(cleanup, dict) or set(cleanup) != ASSET_FAILURE_CLEANUP_FIELDS:
        raise ValueError("asset-failure cleanup evidence is invalid")
    if (
        cleanup["namespaces_removed"] is not True
        or cleanup["object_directories_removed"] is not True
        or cleanup["runtime_probe_directory_removed"] is not True
        or not isinstance(cleanup["runtime_probe_directory"], str)
        or not cleanup["runtime_probe_directory"]
    ):
        raise ValueError("asset-failure cleanup evidence is invalid")
    return _asset_failure_namespaces(
        cleanup["namespaces"], "asset-failure cleanup evidence is invalid"
    )


def _asset_failure_policy(policy):
    """核对 runner 固定的按案例唯一命名空间策略。"""
    if (
        not isinstance(policy, dict)
        or set(policy) != ASSET_FAILURE_NAMESPACE_POLICY_FIELDS
        or not _matches_contract(policy.get("strategy"), ASSET_FAILURE_NAMESPACE_STRATEGY)
        or not _matches_contract(
            policy.get("namespace_prefix"), ASSET_FAILURE_NAMESPACE_PREFIX
        )
        or not _matches_contract(policy.get("reuse"), False)
        or not _matches_contract(policy.get("case_order"), list(ASSET_FAILURE_CASE_ORDER))
    ):
        raise ValueError("asset-failure namespace policy evidence is invalid")
    return _asset_failure_namespaces(
        policy.get("namespaces"), "asset-failure namespace policy evidence is invalid"
    )


def _asset_failure_block(envelope, engine, namespaces):
    """核对 envelope 内嵌的 asset_failures 汇总块与运行身份一致。"""
    block = envelope.get("asset_failures")
    if (
        not isinstance(block, dict)
        or set(block) != ASSET_FAILURE_BLOCK_FIELDS
        or block["engine"] != engine
        or not _matches_contract(block["case_order"], list(ASSET_FAILURE_CASE_ORDER))
        or not _matches_contract(
            block["cleanup"],
            {"namespaces_removed": True, "object_directories_removed": True},
        )
        or block["namespaces"] != namespaces
    ):
        raise ValueError("asset-failure envelope evidence is invalid")
    return block


def _asset_failure_truth(truth, input_identity):
    """在 envelope 发布正式 truth 时核对其固定维度与输入身份。"""
    if (
        not isinstance(truth, dict)
        or set(truth) != {"seed", "identity_sha256", "record_count", "block_size", "block_count"}
        or not _sha256(truth.get("identity_sha256"))
        or (
            isinstance(input_identity, dict)
            and truth["identity_sha256"] != input_identity.get("identity_sha256")
        )
    ):
        raise ValueError("asset-failure truth identity is invalid")
    for field, expected in FORMAL_TRUTH.items():
        if _integer(truth.get(field), "asset-failure truth identity is invalid", 1) != expected:
            raise ValueError("asset-failure truth identity is invalid")


def _asset_failure_child_evidence(evidence):
    """核对 envelope 记录的 child 身份字段齐备。"""
    if not isinstance(evidence, dict) or set(evidence) != ASSET_FAILURE_CHILD_FIELDS:
        raise ValueError("asset-failure child evidence is incomplete")
    if (
        not isinstance(evidence["path"], str) or not evidence["path"]
        or not _sha256(evidence["sha256"])
    ):
        raise ValueError("asset-failure child evidence is incomplete")
    _integer(evidence["bytes"], "asset-failure child evidence is incomplete", 1)


def _validate_asset_failure_envelope(envelope):
    """验证一个 asset-failure production envelope 的格式、身份、策略与清理证据。"""
    if (
        not _matches_contract(envelope.get("format"), PRODUCTION_FORMAT)
        or not _matches_contract(envelope.get("format_version"), FORMAT_VERSION)
    ):
        raise ValueError("asset-failure production envelope format/version is invalid")
    if not _matches_contract(envelope.get("status"), "complete"):
        raise ValueError("asset-failure production envelope status is not complete")
    if not _matches_contract(envelope.get("operation"), "asset-failures"):
        raise ValueError("asset-failure operation is invalid")
    if not isinstance(envelope.get("run_id"), str) or not envelope["run_id"]:
        raise ValueError("asset-failure run identity is missing")
    engine = _asset_failure_runtime(envelope)
    _asset_failure_code(envelope.get("code"))
    namespaces = _asset_failure_cleanup(envelope.get("cleanup"))
    if _asset_failure_policy(envelope.get("namespace_policy")) != namespaces:
        raise ValueError("asset-failure namespace policy evidence is invalid")
    _asset_failure_block(envelope, engine, namespaces)
    _asset_failure_child_evidence(envelope.get("child"))
    # 该控制不依赖正式输入身份；envelope 发布时才按固定维度核对。
    if "input" in envelope:
        _identity(envelope["input"])
    if "truth" in envelope:
        _asset_failure_truth(envelope["truth"], envelope.get("input"))
    return engine


def _read_asset_failure_child(run, envelope):
    """从运行目录重算 child 的 bytes 与 SHA-256，并核对 envelope 记录的身份。"""
    evidence = envelope["child"]
    root = Path(run).resolve()
    path = (root / evidence["path"]).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("asset-failure child path escapes the run directory") from error
    try:
        content = path.read_bytes()
    except OSError as error:
        raise ValueError("asset-failure child manifest is unavailable") from error
    if (
        evidence["bytes"] != len(content)
        or evidence["sha256"] != hashlib.sha256(content).hexdigest()
    ):
        raise ValueError("asset-failure child identity mismatch")
    manifest = _read_json_object(path, "asset-failure child manifest")
    for key in ("format", "format_version", "status", "run_id"):
        if evidence[key] != manifest.get(key):
            raise ValueError("asset-failure child format/status mismatch")
    return manifest


def _asset_failure_namespace(result, case):
    """把案例命名空间绑定到 jsons3_af_<case>_ 前缀和该案例的清理记录。"""
    namespace = result["namespace"]
    prefix = f"jsons3_af_{case}_"
    cleanup = result["cleanup"]
    suffix = namespace[len(prefix):] if isinstance(namespace, str) else None
    if (
        not isinstance(namespace, str)
        or not namespace.startswith(prefix)
        or len(suffix) != 10
        or any(character not in "0123456789abcdef" for character in suffix)
        or not isinstance(cleanup, dict)
        or cleanup.get("namespace") != namespace
    ):
        raise ValueError("asset-failure namespace evidence is invalid")
    return namespace


def _asset_failure_resolver(value):
    """核对 resolver 或 recovery_resolver 的字段类型，不在此判定分类取值。"""
    if not isinstance(value, dict) or set(value) != ASSET_FAILURE_RESOLVER_FIELDS:
        raise ValueError("asset-failure resolver evidence is invalid")
    error, preview = value["error"], value["preview"]
    if (
        not isinstance(value["content_visible"], bool)
        or not (error is None or (isinstance(error, str) and error))
        or not (preview is None or isinstance(preview, str))
        or not (value["sha256"] is None or _sha256(value["sha256"]))
        or not (value["content_length"] is None or _is_integer(value["content_length"]))
    ):
        raise ValueError("asset-failure resolver evidence is invalid")
    return value


def _asset_failure_resolver_content(value, asset_id):
    """核对内容可见性与分类一致：可见须给出内容身份，不可见须全部为空。"""
    if value["content_visible"] != (value["error"] is None):
        raise ValueError("asset-failure resolver evidence is invalid")
    if value["content_visible"]:
        if (
            value["sha256"] != asset_id
            or not _is_integer(value["content_length"], 1)
            or not isinstance(value["preview"], str)
        ):
            raise ValueError("asset-failure resolver evidence is invalid")
    elif any(
        value[field] is not None for field in ("content_length", "preview", "sha256")
    ):
        raise ValueError("asset-failure resolver evidence is invalid")
    return value


def _asset_failure_reconcile(value, expected):
    """核对孤儿对象计数与路径证据自洽，并匹配该案例的固定孤儿数。"""
    if not isinstance(value, dict) or set(value) != ASSET_FAILURE_RECONCILE_FIELDS:
        raise ValueError("asset-failure reconcile evidence is invalid")
    count, paths = value["orphan_count"], value["orphan_paths"]
    if (
        not _is_integer(count)
        or not isinstance(paths, list)
        or count != len(paths)
        or any(not isinstance(path, str) or not path for path in paths)
    ):
        raise ValueError("asset-failure reconcile evidence is invalid")
    if count != expected:
        raise ValueError("asset-failure orphan evidence mismatch")
    return {"orphan_count": count, "orphan_paths": list(paths)}


def _asset_failure_transitions(value, result, expected):
    """核对注入与恢复两次目录状态跃迁自该案例的注入前状态首尾相接并落在其终态。"""
    if not isinstance(value, list) or len(value) != len(ASSET_FAILURE_TRANSITION_PHASES):
        raise ValueError("asset-failure catalog transition evidence is invalid")
    previous = None
    for phase, transition in zip(ASSET_FAILURE_TRANSITION_PHASES, value):
        if (
            not isinstance(transition, dict)
            or set(transition) != ASSET_FAILURE_TRANSITION_FIELDS
            or transition["phase"] != phase
            or transition["asset_id"] != result["asset_id"]
            or transition["sha256"] != result["sha256"]
            or not isinstance(transition["before"], str) or not transition["before"]
            or not isinstance(transition["after"], str) or not transition["after"]
            or transition["before"] != (
                expected["injection_before"] if previous is None else previous
            )
        ):
            raise ValueError("asset-failure catalog transition evidence is invalid")
        previous = transition["after"]
    if previous != result["final_status"]:
        raise ValueError("asset-failure catalog transition evidence is invalid")
    return list(value)


def _asset_failure_store_observation(value, asset_id, expected):
    """核对对象存在性观测与发布尝试证据，并匹配该案例的固定取值与尝试次数。"""
    if not isinstance(value, dict) or set(value) != ASSET_FAILURE_STORE_FIELDS:
        raise ValueError("asset-failure store observation evidence is invalid")
    attempts = value["publish_attempts"]
    if (
        not isinstance(value["object_exists"], bool)
        or value["object_exists"] is not expected["object_exists"]
        or not isinstance(value["object_path"], str) or not value["object_path"]
        or not isinstance(attempts, list)
        or len(attempts) != expected["publish_attempts"]
    ):
        raise ValueError("asset-failure store observation evidence is invalid")
    for attempt in attempts:
        error = attempt.get("error") if isinstance(attempt, dict) else None
        if (
            not isinstance(attempt, dict)
            or set(attempt) != ASSET_FAILURE_ATTEMPT_FIELDS
            or attempt["asset_id"] != asset_id
            or not isinstance(attempt["final_object_exists"], bool)
            or not (error is None or (isinstance(error, str) and error))
        ):
            raise ValueError("asset-failure store observation evidence is invalid")
    return value


def _asset_failure_case_cleanup(value, namespace):
    """核对单个案例的命名空间与对象目录清理证据。"""
    if set(value) != ASSET_FAILURE_CASE_CLEANUP_FIELDS:
        raise ValueError("asset-failure case cleanup evidence is invalid")
    if (
        value["namespace_removed"] is not True
        or value["object_directory_removed"] is not True
        or value["errors"] != []
        or not isinstance(value["adapter_cleanup_target"], str)
        or not value["adapter_cleanup_target"].startswith(namespace)
        or not isinstance(value["object_directory"], str) or not value["object_directory"]
    ):
        raise ValueError("asset-failure case cleanup evidence is invalid")
    return value


def _asset_failure_result(result, case):
    """按固定分类契约核对单个案例的分类与证据，只汇总分类与证据。"""
    if not isinstance(result, dict) or set(result) != ASSET_FAILURE_RESULT_FIELDS:
        raise ValueError("asset-failure result evidence is incomplete")
    if result["case"] != case:
        raise ValueError("asset-failure case identity mismatch")
    if (
        not _sha256(result["asset_id"])
        or result["sha256"] != result["asset_id"]
        or not isinstance(result["event_visible"], bool)
        or not isinstance(result["final_status"], str) or not result["final_status"]
        or not isinstance(result["injection_point"], str) or not result["injection_point"]
        or not isinstance(result["validation_errors"], list)
    ):
        raise ValueError("asset-failure result evidence is incomplete")
    if result["validation_errors"]:
        raise ValueError("asset-failure validation errors are present")
    if result["execution_error"] is not None:
        raise ValueError("asset-failure execution error is present")
    namespace = _asset_failure_namespace(result, case)
    expected = ASSET_FAILURE_CLASSIFICATION[case]
    resolver = _asset_failure_resolver(result["resolver"])
    recovery_resolver = _asset_failure_resolver(result["recovery_resolver"])
    if resolver["error"] != expected["resolver_error"]:
        raise ValueError("asset-failure resolver classification mismatch")
    if result["final_status"] != expected["final_status"]:
        raise ValueError("asset-failure final status mismatch")
    if result["event_visible"] is not expected["event_visible"]:
        raise ValueError("asset-failure event visibility mismatch")
    _asset_failure_resolver_content(resolver, result["asset_id"])
    _asset_failure_resolver_content(recovery_resolver, result["asset_id"])
    # 恢复后的解析结果必须落在该案例的预期终态，否则失败的恢复会被当作成功发布。
    if (
        recovery_resolver["error"] != expected["recovery_error"]
        or recovery_resolver["content_visible"] is not expected["recovery_visible"]
    ):
        raise ValueError("asset-failure recovery classification mismatch")
    if result["injection_point"] != expected["injection_point"]:
        raise ValueError("asset-failure injection point mismatch")
    reconcile = _asset_failure_reconcile(result["reconcile"], expected["orphan_count"])
    reconcile_after = _asset_failure_reconcile(result["reconcile_after_recovery"], 0)
    actions = result["recovery_actions"]
    if not isinstance(actions, list) or not actions or any(
        not isinstance(action, str) or not action for action in actions
    ):
        raise ValueError("asset-failure recovery evidence is invalid")
    if actions != expected["recovery_actions"]:
        raise ValueError("asset-failure recovery action mismatch")
    return {
        "case": case,
        "namespace": namespace,
        "asset_id": result["asset_id"],
        "sha256": result["sha256"],
        "injection_point": result["injection_point"],
        "resolver": resolver,
        "final_status": result["final_status"],
        "event_visible": result["event_visible"],
        "catalog_transitions": _asset_failure_transitions(
            result["catalog_transitions"], result, expected
        ),
        "orphan_count": reconcile["orphan_count"],
        "orphan_count_after_recovery": reconcile_after["orphan_count"],
        "reconcile": reconcile,
        "reconcile_after_recovery": reconcile_after,
        "recovery_actions": list(actions),
        "recovery_resolver": recovery_resolver,
        "store_observation": _asset_failure_store_observation(
            result["store_observation"], result["asset_id"], expected
        ),
        "cleanup": _asset_failure_case_cleanup(result["cleanup"], namespace),
        "validation_errors": list(result["validation_errors"]),
        "execution_error": result["execution_error"],
    }


def _validate_asset_failure_child(manifest):
    """核对 child 的固定格式、六案例定序覆盖与逐案例分类证据。"""
    if (
        not _matches_contract(manifest.get("format"), ASSET_FAILURE_FORMAT)
        or not _matches_contract(manifest.get("format_version"), FORMAT_VERSION)
        or not _matches_contract(manifest.get("status"), "complete")
    ):
        raise ValueError("asset-failure child format/version/status is invalid")
    if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
        raise ValueError("asset-failure child run identity is missing")
    results = manifest.get("results")
    if (
        not _matches_contract(manifest.get("case_order"), list(ASSET_FAILURE_CASE_ORDER))
        or not isinstance(results, list)
        or len(results) != len(ASSET_FAILURE_CASE_ORDER)
    ):
        raise ValueError("asset-failure case order mismatch")
    return [
        _asset_failure_result(result, case)
        for case, result in zip(ASSET_FAILURE_CASE_ORDER, results)
    ]


def summarize_asset_failures(runs: list[Path]) -> dict[str, object]:
    """汇总两个引擎各一个正式 asset-failure 控制的分类与证据，不产出时延统计。"""
    if not runs:
        raise ValueError("at least one asset-failure run directory is required")
    envelopes = {}
    for run in runs:
        envelope = _read_json_object(
            Path(run) / "run-manifest.json", "asset-failure production envelope"
        )
        engine = _validate_asset_failure_envelope(envelope)
        if engine in envelopes:
            raise ValueError(f"duplicate asset-failure engine: {engine}")
        envelopes[engine] = (Path(run), envelope)
    # 覆盖哪些引擎由调用方传入的运行目录决定，并作为事实写入汇总；
    # 少于两个引擎无法构成对比，直接拒绝。
    covered = tuple(engine for engine in ASSET_FAILURE_ENGINES if engine in envelopes)
    if len(covered) != len(envelopes) or len(covered) < ASSET_FAILURE_MINIMUM_ENGINES:
        raise ValueError("asset-failure engine coverage is incomplete")
    first = envelopes[covered[0]][1]
    for name, field in (
        ("input identity", "input"), ("truth identity", "truth"),
        ("query catalog identity", "query_catalog_sha256"),
    ):
        if any(
            envelopes[engine][1].get(field) != first.get(field)
            for engine in covered
        ):
            raise ValueError(f"asset-failure {name} differs between runs")
    identities = [
        (envelopes[engine][1]["run_id"], envelopes[engine][1]["child"]["run_id"])
        for engine in covered
    ]
    if any(len({item[index] for item in identities}) != len(identities) for index in (0, 1)):
        raise ValueError("asset-failure run identity is duplicated between runs")
    engines = []
    for engine in covered:
        run, envelope = envelopes[engine]
        manifest = _read_asset_failure_child(run, envelope)
        cases = _validate_asset_failure_child(manifest)
        if (
            envelope["asset_failures"]["results"] != manifest["results"]
            or envelope["cleanup"]["namespaces"] != [case["namespace"] for case in cases]
        ):
            raise ValueError("asset-failure envelope and child evidence differ")
        engines.append({
            "engine": engine,
            "run_id": envelope["run_id"],
            "code": envelope["code"],
            "child": envelope["child"],
            "runtime": envelope["runtime"],
            "namespace_policy": envelope["namespace_policy"],
            "cleanup": envelope["cleanup"],
            "cases": cases,
        })
    namespaces = [case["namespace"] for item in engines for case in item["cases"]]
    if len(set(namespaces)) != len(namespaces):
        raise ValueError("asset-failure namespaces are reused between runs")
    summary = {
        "format": ASSET_FAILURE_SUMMARY_FORMAT,
        "format_version": FORMAT_VERSION,
        "statistics_boundary": ASSET_FAILURE_STATISTICS_BOUNDARY,
        "case_order": list(ASSET_FAILURE_CASE_ORDER),
        "engines": engines,
    }
    # 该控制的 envelope 不强制发布正式输入身份，发布时才携带进汇总。
    for field in ("input", "truth", "query_catalog_sha256"):
        if field in first:
            summary[field] = first[field]
    return summary


def _fsync_directory(directory: Path) -> None:
    """持久化目录项的创建和删除。"""
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_summary_atomic(output: Path, summary: dict[str, object]) -> None:
    """以同目录临时文件原子发布新汇总，绝不覆盖既有结果。"""
    output = Path(output)
    content = json.dumps(
        summary, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    try:
        existing = output.read_bytes()
    except FileNotFoundError:
        pass
    else:
        if existing == content:
            _fsync_directory(output.parent)
            return
        raise FileExistsError(f"summary output already exists: {output}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, output)
        except FileExistsError:
            existing = output.read_bytes()
            temporary.unlink()
            temporary = None
            if existing == content:
                _fsync_directory(output.parent)
                return
            raise FileExistsError(f"summary output already exists: {output}") from None
        temporary.unlink()
        temporary = None
        _fsync_directory(output.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _bind_identity(trees, field, families):
    """在实际携带该身份字段的族之间取共享值，不一致即失败。"""
    first = trees[families[0]][field]
    if any(trees[family][field] != first for family in families):
        raise ValueError(
            f"combined {field} identity differs between " + " and ".join(families)
        )
    return first


def _combine_summaries(matrix, part_states, interference, asset_failures):
    """装配四族各自独立的结果树，只绑定各族实际携带的身份字段。"""
    trees = {
        "matrix": matrix, "part_states": part_states,
        "interference": interference, "asset_failures": asset_failures,
    }
    return {
        "format": COMBINED_FORMAT,
        "format_version": FORMAT_VERSION,
        "input": _bind_identity(trees, "input", COMBINED_INPUT_FAMILIES),
        "truth": _bind_identity(trees, "truth", COMBINED_TRUTH_FAMILIES),
        "query_catalog_sha256": _bind_identity(
            trees, "query_catalog_sha256", COMBINED_TRUTH_FAMILIES
        ),
        "identity_binding": {
            "input": list(COMBINED_INPUT_FAMILIES),
            "truth": list(COMBINED_TRUTH_FAMILIES),
            "query_catalog_sha256": list(COMBINED_TRUTH_FAMILIES),
            "unbound": {"asset_failures": COMBINED_ASSET_FAILURE_BINDING},
        },
        **trees,
    }


def build_parser():
    """构造只接受调用方显式列出的运行目录的汇总解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    # 四族目录逐个列出：失败与被取代的尝试与有效运行并列存放，不得扫描父目录选取。
    for option in ("matrix", "part-state", "interference", "asset-failure"):
        parser.add_argument(f"--{option}", type=Path, nargs="+", required=True, metavar="DIR")
    return parser


def main(argv=None):
    """汇总四族控制并原子发布组合结果；校验失败时在 stderr 说明原因并返回非零。"""
    arguments = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    output = arguments.output.resolve()
    try:
        if output.exists():
            raise FileExistsError(f"summary output already exists: {output}")
        summary = _combine_summaries(
            summarize(arguments.matrix),
            summarize_part_states(arguments.part_state),
            summarize_interference(arguments.interference),
            summarize_asset_failures(arguments.asset_failure),
        )
        write_summary_atomic(output, summary)
    except (OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
