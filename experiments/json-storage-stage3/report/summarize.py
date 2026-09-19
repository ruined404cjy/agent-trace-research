"""汇总已完成的 Stage 3 主矩阵 target 与 part-state 控制。"""

import hashlib
import json
import math
import os
import statistics
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
    if engine == "opengauss":
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
    storage_fields = (
        ("part_count", "rows", "marks", "compressed_bytes", "uncompressed_bytes")
        if engine == "clickhouse"
        else ("heap_bytes", "index_bytes", "toast_bytes", "total_bytes")
    )
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
    if manifest.get("engine") not in {"opengauss", "clickhouse"}:
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


def _target_summary(manifest, samples):
    """构造单 target 的三组性能 workload 结果。"""
    grouped = _validate_samples(manifest, samples)
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
        workloads[workload] = {"scenarios": scenarios}
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
    if not isinstance(container, dict) or any(
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
        or any(truth.get(field) != value for field, value in FORMAL_TRUTH.items())
        or not _sha256(truth.get("identity_sha256"))
        or truth["identity_sha256"] != envelope["input"]["identity_sha256"]
    ):
        raise ValueError("part-state truth identity is invalid")
    for field in ("record_count", "block_size", "block_count"):
        _integer(truth.get(field), "part-state truth identity is invalid", 1)
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
    runtime_identity = (
        first["runtime"]["engine_runtime"]["version"],
        first["runtime"]["container"]["image_id"],
    )
    if any(
        (
            envelopes[layout][1]["runtime"]["engine_runtime"]["version"],
            envelopes[layout][1]["runtime"]["container"]["image_id"],
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
