"""汇总已完成的 Stage 3 主矩阵 target。"""

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
WATERMARK_TARGETS = {
    "same_table": {"events"},
    "separate": {"events_analytics", "event_payloads"},
    "full_core": {"events_full", "events_core"},
    "asset_ref": {"events_analytics", "assets"},
}
PERFORMANCE_WORKLOADS = (
    "main", "equal_total_few_large", "equal_total_many_medium",
)
ALL_WORKLOADS = PERFORMANCE_WORKLOADS + ("correctness_only",)
METRICS = (
    "query_complete_ms", "recovery_ms", "validation_ms", "application_ready_ms",
)
STATISTICS = ("minimum", "p50", "p95", "maximum")


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


def _validate_provenance(manifest):
    """验证 target 汇总的 code、DDL 和 workload query 身份。"""
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
        or set(queries) != set(ALL_WORKLOADS)
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


def _round_records(manifest):
    """读取每个 workload 的固定 round 列表。"""
    workloads = _require(manifest.get("workloads"), "workload evidence is missing")
    if not isinstance(workloads, dict) or set(workloads) != set(ALL_WORKLOADS):
        raise ValueError("workload evidence is incomplete")
    records = {}
    for workload in ALL_WORKLOADS:
        state = workloads[workload]
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
        or set(watermarks) != WATERMARK_TARGETS[layout]
        or set(watermarks.values()) != {write["final_watermark"]}
    ):
        raise ValueError("watermark evidence is incomplete")
    storage = _require(record.get("storage"), "storage evidence is missing")
    if (
        not isinstance(storage, dict)
        or not isinstance(storage.get("tables"), dict)
        or set(storage["tables"]) != WATERMARK_TARGETS[layout]
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
    _validate_provenance(manifest)
    expected_schedule = [list(LAYOUTS[index:] + LAYOUTS[:index]) for index in range(4)]
    if manifest.get("latin_square") != expected_schedule:
        raise ValueError("Latin square order is invalid")
    correctness = _require(manifest.get("correctness"), "response-byte validation is missing")
    if not isinstance(correctness, dict) or not correctness.get("response_bytes_validated"):
        raise ValueError("response-byte validation is missing")
    _integer(correctness.get("rounds_complete"), "response-byte validation is missing", 1)
    formal_samples = _integer(
        correctness.get("formal_samples"), "response-byte validation is missing", 1,
    )
    successful_samples = _integer(
        correctness.get("successful_samples"), "response-byte validation is missing", 1,
    )
    if correctness["rounds_complete"] != 4 or successful_samples != formal_samples:
        raise ValueError("response-byte validation is missing")
    if not isinstance(manifest.get("global_cleanup"), dict) or not manifest["global_cleanup"].get("removed"):
        raise ValueError("cleanup evidence is missing")
    records_by_workload = _round_records(manifest)
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
    records = _round_records(manifest)
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
    for workload in ALL_WORKLOADS:
        for record in records[workload]:
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
    """从调用方显式选择的正式 target 目录汇总主矩阵结果。"""
    if not runs:
        raise ValueError("at least one target directory is required")
    targets = []
    input_identity = None
    seen = set()
    for run in runs:
        target = Path(run)
        try:
            manifest = json.loads((target / "run-manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("run-manifest.json is unavailable or invalid") from error
        validate_run(manifest)
        key = (manifest["engine"], manifest["layout"])
        if key in seen:
            raise ValueError("duplicate matrix target")
        seen.add(key)
        if input_identity is None:
            input_identity = manifest["input"]
        elif input_identity != manifest["input"]:
            raise ValueError("matrix input identity differs between targets")
        targets.append(_target_summary(manifest, _read_samples(target / "samples.jsonl")))
    return {
        "format": "agent-trace-json-storage-stage3-matrix-summary",
        "format_version": 1,
        "input": input_identity,
        "statistics_boundary": "round-first-four-round-median",
        "matrix": sorted(targets, key=lambda value: (value["engine"], value["layout"])),
    }


def write_summary_atomic(output: Path, summary: dict[str, object]) -> None:
    """以同目录临时文件发布新汇总，相同既有内容按幂等成功处理。"""
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
            return
        raise FileExistsError(f"summary output already exists: {output}")
    temporary = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        published = True
        directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        temporary = None
    except Exception:
        if published:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
