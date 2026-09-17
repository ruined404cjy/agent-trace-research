#!/usr/bin/env python3
"""发布阶段三冻结输入与 candidate 的不可复用生产运行 envelope。"""

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
import uuid
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

import assets
import common
import production
import clickhouse
import run_asset_failures
import run_interference
import run_layout_matrix
from common import canonical_digest
from production import (
    EngineEndpoints, candidate_config, create_adapter, load_formal_input, part_state_inputs,
)
import run_clickhouse_part_states
from run_clickhouse_part_states import run_part_states
from run_layout_matrix import run_layout, write_manifest_atomic


STAGE_DIR = Path(__file__).resolve().parents[1]
GENERATOR_DIR = STAGE_DIR / "generator"
if str(GENERATOR_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATOR_DIR))
import generate_payloads
from generate_payloads import build_truth


SCRIPT_PATH = Path(__file__).resolve()
FORMAT = "agent-trace-json-storage-stage3-production-run"
FORMAT_VERSION = 1
SEED = 20260907
CHILD_FORMAT = "agent-trace-json-storage-stage3-layout-run"
LATIN_FIRST = ("same_table", "separate", "full_core", "asset_ref")
WATERMARK_KEYS = {"assets", "events_analytics"}


def build_parser():
    """构造仅暴露固定正式参数的命令行解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    generate = commands.add_parser("generate-input")
    generate.add_argument("--source", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--input", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    part_states = commands.add_parser("part-states")
    part_states.add_argument("--input", type=Path, required=True)
    part_states.add_argument("--output", type=Path, required=True)
    part_states.add_argument("--layout", choices=("same_table", "separate", "full_core", "asset_ref"),
                             required=True)
    interference = commands.add_parser("interference")
    interference.add_argument("--input", type=Path, required=True)
    interference.add_argument("--output", type=Path, required=True)
    interference.add_argument("--layout", choices=("same_table", "separate", "full_core", "asset_ref"),
                              required=True)
    asset_failures = commands.add_parser("asset-failures")
    asset_failures.add_argument("--output", type=Path, required=True)
    asset_failures.add_argument("--engine", choices=("opengauss", "clickhouse"), required=True)
    return parser


def _json_value(value):
    """复制 MappingProxy 等只读容器，形成隔离的 JSON 值。"""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _strict_json_snapshot(value, label):
    """形成拒绝非有限数值和非 JSON 对象的隔离副本。"""
    try:
        return json.loads(json.dumps(
            _json_value(value), ensure_ascii=False, allow_nan=False,
        ))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} is not JSON-safe") from error


def _file_identity(path):
    """读取一次文件并返回不可变 bytes 身份。"""
    path = Path(path).resolve()
    content = path.read_bytes()
    return {"path": str(path), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def _code_evidence(operation):
    """记录 CLI、公共边界和直接执行模块的实际文件 bytes。"""
    modules = {
        "run_stage3": sys.modules[__name__],
        "production": production,
        "generator": generate_payloads,
        "layout_runner": run_layout_matrix,
        "common": common,
        "assets": assets,
    }
    if operation == "part-states":
        modules["part_state_runner"] = run_clickhouse_part_states
    if operation == "interference":
        modules["interference_runner"] = run_interference
    if operation == "asset-failures":
        modules["asset_failure_runner"] = run_asset_failures
    return {role: _file_identity(module.__file__) for role, module in modules.items()}


def _query_catalog_sha256(formal):
    """按 main catalog 原顺序计算与矩阵一致的 canonical digest。"""
    catalog = [
        {
            "scenario": truth.scenario,
            "kind": query.kind,
            "parameters": _json_value(query.parameters),
        }
        for query, truth in formal.main_queries
    ]
    return canonical_digest(catalog)


def _truth_evidence(formal):
    """提取 production envelope 的固定 truth 身份。"""
    truth = formal.truth
    return {
        "seed": truth.seed,
        "identity_sha256": truth.identity_sha256,
        "record_count": truth.record_count,
        "block_size": truth.block_size,
        "block_count": truth.block_count,
    }


def _record_formal_evidence(envelope, formal):
    """在正式 loader 返回后立即保留失败路径仍可发布的输入身份。"""
    envelope.update({
        "input": _json_value(formal.identity),
        "truth": _truth_evidence(formal),
        "query_catalog_sha256": _query_catalog_sha256(formal),
    })


def _load_json_object(content, label):
    """解析有限 JSON object，拒绝 NaN、Infinity 和非 object 文档。"""
    def reject_constant(value):
        raise ValueError(f"{label} contains non-finite JSON value: {value}")

    try:
        value = json.loads(content, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {label}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _read_child(output, path):
    """从 output 内重新读取 child bytes、解析 object 并计算独立身份。"""
    output = Path(output).resolve()
    path = Path(path).resolve()
    try:
        relative = path.relative_to(output)
    except ValueError as error:
        raise ValueError("child path escapes output") from error
    if not path.is_file():
        raise ValueError("child manifest is missing")
    content = path.read_bytes()
    manifest = _load_json_object(content, "child manifest")
    return {
        "_path": str(path),
        "_relative": relative.as_posix(),
        "_bytes": content,
        "_identity": {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()},
        "_manifest": manifest,
    }


def _child_evidence(child):
    """将内部 child 快照转换为 envelope 可发布身份。"""
    manifest = child["_manifest"]
    evidence = {"path": child["_relative"], **child["_identity"]}
    for key in ("format", "format_version", "status", "run_id"):
        if key in manifest:
            evidence[key] = manifest[key]
    return evidence


def _read_samples(child):
    """读取 candidate 正式样本，返回成功 query_id 的精确序列。"""
    path = Path(child["_path"]).parent / "samples.jsonl"
    if not path.is_file():
        raise RuntimeError("candidate samples are missing")
    samples = []
    for line_number, line in enumerate(path.read_bytes().splitlines(), 1):
        if not line:
            raise ValueError(f"empty candidate sample line: {line_number}")
        samples.append(_load_json_object(line, f"candidate sample line {line_number}"))
    if not samples:
        raise RuntimeError("candidate samples are empty")
    if any(sample.get("status") != "success" for sample in samples):
        raise RuntimeError("candidate samples contain failures")
    query_ids = [sample.get("query_id") for sample in samples]
    if any(not isinstance(query_id, str) or not query_id for query_id in query_ids):
        raise RuntimeError("candidate sample query_id is missing")
    if len(set(query_ids)) != len(query_ids):
        raise RuntimeError("candidate sample query_id is not unique")
    return query_ids


def _require_object(manifest, key):
    value = manifest.get(key)
    if not isinstance(value, dict) or not value:
        raise RuntimeError(f"candidate {key} evidence is missing")
    return value


def _is_int(value, minimum=0):
    """判断 JSON 值为排除 bool 的有界整数。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_sha256(value):
    """判断值为小写 SHA-256 十六进制字符串。"""
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _finite_number(value, minimum=0):
    return (type(value) in {int, float} and math.isfinite(value) and value >= minimum)


def _matches_fixed_json(value, expected):
    """按 JSON 节点类型和值核对固定结构。"""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(
            _matches_fixed_json(value[key], item) for key, item in expected.items()
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _matches_fixed_json(item, wanted) for item, wanted in zip(value, expected)
        )
    if isinstance(expected, float) and not math.isfinite(value):
        return False
    return value == expected


ASSET_FAILURE_CASES = tuple(run_asset_failures.FAILURE_CASE_NAMES)
ASSET_FAILURE_RULES = {
    "missing": ("remove_published_object", "missing", False,
                "restore_missing_object", "available", "available", True),
    "corrupt": ("modify_published_bytes", "corrupt", True,
                "replace_corrupt_object", "available", "available", True),
    "metadata_mismatch": ("replace_catalog_metadata", "metadata_mismatch", True,
                          "restore_catalog_metadata", "available", "available", True),
    "upload_then_db_failure": ("fail_after_object_upload", "missing", True,
                               "remove_orphan_object", "absent", "absent", False),
    "publish_failure": ("fail_pending_publication", "failed", False,
                        "confirm_failed_publication", "failed", "failed", False),
}


def _asset_failure_resolver(error, payload, digest, visible):
    """构造固定 resolver 结果，用于严格 JSON 结构比较。"""
    if visible:
        return {
            "error": None, "content_visible": True, "content_length": len(payload),
            "sha256": digest, "preview": payload.decode("utf-8"),
        }
    return {
        "error": error, "content_visible": False, "content_length": None,
        "sha256": None, "preview": None,
    }


def _expected_asset_failure_result(case, namespace, output, delete_status=None):
    """按固定六故障语义生成单项只读比较契约。"""
    if case == "delete_failure":
        if delete_status not in {"deleting", "failed"}:
            raise RuntimeError("asset failure delete status is invalid")
        rule = (
            "fail_deleting_object_removal", delete_status, True,
            "confirm_delete_failure_state", delete_status, delete_status, False,
        )
    else:
        rule = ASSET_FAILURE_RULES[case]
    injection, error, object_exists, recovery, injected_status, final_status, recovered = rule
    payload = ('{"content":"asset failure ' + case + '"}').encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    object_directory = (
        Path(output).resolve() / "child" / "asset-failure-cases" / case / "objects"
    )
    object_path = object_directory / digest[:2] / digest
    prepared_status = "pending" if case == "publish_failure" else (
        "absent" if case == "upload_then_db_failure" else "available"
    )
    attempts = []
    if case == "upload_then_db_failure":
        attempts = [{"asset_id": digest, "error": None, "final_object_exists": True}]
    elif case == "publish_failure":
        attempts = [{"asset_id": digest, "error": "failed", "final_object_exists": False}]
    return {
        "case": case, "namespace": namespace, "asset_id": digest, "sha256": digest,
        "injection_point": injection,
        "catalog_transitions": [
            {"phase": "injection", "asset_id": digest, "sha256": digest,
             "before": prepared_status, "after": injected_status},
            {"phase": "recovery", "asset_id": digest, "sha256": digest,
             "before": injected_status, "after": final_status},
        ],
        "resolver": _asset_failure_resolver(error, payload, digest, False),
        "event_visible": case != "upload_then_db_failure",
        "reconcile": {
            "orphan_count": 1 if case == "upload_then_db_failure" else 0,
            "orphan_paths": [str(object_path)] if case == "upload_then_db_failure" else [],
        },
        "store_observation": {
            "object_path": str(object_path), "object_exists": object_exists,
            "publish_attempts": attempts,
        },
        "recovery_actions": [recovery],
        "recovery_resolver": _asset_failure_resolver(error, payload, digest, recovered),
        "reconcile_after_recovery": {"orphan_count": 0, "orphan_paths": []},
        "final_status": final_status,
        "cleanup": {
            "namespace": namespace, "adapter_cleanup_target": namespace + "_asset_ref",
            "namespace_removed": True, "object_directory": str(object_directory),
            "object_directory_removed": True, "errors": [],
        },
        "validation_errors": [], "execution_error": None,
    }


def _gate_asset_failures(child, output, engine):
    """从 child JSON bytes 独立门禁两引擎固定六故障证据。"""
    if not isinstance(engine, str) or engine not in {"opengauss", "clickhouse"}:
        raise ValueError("unsupported asset failure engine")
    output = Path(output).resolve()
    expected_path = (output / "child" / "run-manifest.json").resolve()
    if (
        not isinstance(child, dict)
        or child.get("_relative") != "child/run-manifest.json"
        or not isinstance(child.get("_path"), str)
        or Path(child["_path"]).resolve() != expected_path
    ):
        raise RuntimeError("asset failure child manifest path is invalid")
    content = child.get("_bytes")
    identity = child.get("_identity")
    if (
        not isinstance(content, bytes) or not isinstance(identity, dict)
        or set(identity) != {"bytes", "sha256"}
        or not _is_int(identity.get("bytes")) or identity["bytes"] != len(content)
        or not _is_sha256(identity.get("sha256"))
        or identity["sha256"] != hashlib.sha256(content).hexdigest()
    ):
        raise RuntimeError("asset failure child bytes identity is invalid")
    manifest = _load_json_object(content, "asset failure child manifest")
    root_fields = {"format", "format_version", "run_id", "status", "case_order", "results"}
    if (
        set(manifest) != root_fields
        or manifest.get("format") != "agent-trace-json-storage-stage3-asset-failure-run"
        or not _is_int(manifest.get("format_version")) or manifest["format_version"] != 1
        or not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]
        or manifest.get("status") != "complete"
        or not _matches_fixed_json(manifest.get("case_order"), list(ASSET_FAILURE_CASES))
        or not isinstance(manifest.get("results"), list)
        or len(manifest["results"]) != len(ASSET_FAILURE_CASES)
    ):
        raise RuntimeError("asset failure child root evidence is invalid")
    namespaces = []
    gated_results = []
    for case, result in zip(ASSET_FAILURE_CASES, manifest["results"]):
        if not isinstance(result, dict):
            raise RuntimeError("asset failure result is invalid")
        namespace = result.get("namespace")
        pattern = r"jsons3_af_" + re.escape(case) + r"_[0-9a-f]{10}"
        if not isinstance(namespace, str) or re.fullmatch(pattern, namespace) is None:
            raise RuntimeError("asset failure namespace is invalid")
        delete_status = result.get("final_status") if case == "delete_failure" else None
        expected = _expected_asset_failure_result(case, namespace, output, delete_status)
        if not _matches_fixed_json(result, expected):
            raise RuntimeError(f"asset failure {case} evidence is invalid")
        namespaces.append(namespace)
        gated_results.append(result)
    if len(set(namespaces)) != len(namespaces):
        raise RuntimeError("asset failure namespaces are not unique")
    return _strict_json_snapshot({
        "engine": engine, "case_order": list(ASSET_FAILURE_CASES),
        "namespaces": namespaces, "results": gated_results,
        "cleanup": {"namespaces_removed": True, "object_directories_removed": True},
    }, "asset failure evidence")


def _artifact_identity(output, child_root, path):
    """读取 child 内 artifact，并返回相对 production output 的 bytes 身份。"""
    output, child_root, path = map(lambda item: Path(item).resolve(), (output, child_root, path))
    try:
        path.relative_to(child_root)
        relative = path.relative_to(output)
    except ValueError as error:
        raise RuntimeError("interference artifact escapes child root") from error
    if not path.is_file():
        raise RuntimeError("interference artifact is missing")
    content = path.read_bytes()
    return {"path": relative.as_posix(), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}


def _parse_interference_raw(path, schedules, query_scenarios=None, write_tables=None, *,
                            continuous_block_rows=None, continuous_watermarks=None):
    """从完整 raw JSONL 重算各 stream 计数和成功目标证据。"""
    path = Path(path)
    if not path.is_file():
        raise RuntimeError("interference raw file is missing")
    lines = path.read_bytes().splitlines()
    if not lines or any(not line for line in lines):
        raise RuntimeError("interference raw file is empty or contains an empty line")
    if not isinstance(schedules, dict) or not schedules:
        raise RuntimeError("interference schedules are invalid")
    records = {stream: [] for stream in schedules}
    fields = {"stream", "sequence", "scheduled_offset_seconds", "started_offset_seconds",
              "completed_offset_seconds", "duration_ms", "application_ready_ms", "status",
              "late_by_ms", "sample", "error"}
    for number, line in enumerate(lines, 1):
        row = _load_json_object(line, f"interference raw line {number}")
        if set(row) != fields or row.get("stream") not in records:
            raise RuntimeError("interference raw fields or stream are invalid")
        records[row["stream"]].append(row)
    result = {}
    query_ids = set()
    for stream, rows in records.items():
        if not rows:
            raise RuntimeError("interference raw stream is missing")
        schedule = schedules[stream]
        sequences = [row.get("sequence") for row in rows]
        if any(type(value) is not int or value < 0 for value in sequences) or sequences != list(
            range(len(rows))
        ):
            raise RuntimeError("interference raw sequence is not contiguous")
        counts = {name: 0 for name in ("started", "completed", "success", "failed",
                                       "timed_out", "dropped", "late")}
        successful_queries = []
        for row in rows:
            status = row.get("status")
            if status not in {"success", "failed", "timed_out", "dropped"}:
                raise RuntimeError("interference raw status is invalid")
            for field in ("scheduled_offset_seconds", "late_by_ms"):
                if not _finite_number(row.get(field)):
                    raise RuntimeError("interference raw timing is invalid")
            started, completed = row.get("started_offset_seconds"), row.get("completed_offset_seconds")
            if status == "dropped":
                if started is not None or completed is not None or row.get("sample") is not None:
                    raise RuntimeError("interference dropped sample is inconsistent")
            elif not _finite_number(started) or (
                status in {"success", "failed"} and not _finite_number(completed)
            ) or completed is not None and (
                not _finite_number(completed) or completed < started
            ):
                raise RuntimeError("interference started/completed evidence is invalid")
            duration, application_ready = row.get("duration_ms"), row.get("application_ready_ms")
            if completed is None:
                if duration is not None or application_ready is not None:
                    raise RuntimeError("interference incomplete timing is inconsistent")
            elif not _finite_number(duration) or not _finite_number(application_ready):
                raise RuntimeError("interference request duration is invalid")
            if status == "success" and row.get("error") is not None:
                raise RuntimeError("interference successful sample contains error")
            if status != "success" and (
                not isinstance(row.get("error"), str) or not row["error"]
            ):
                raise RuntimeError("interference failed sample lacks error")
            counts[status] += 1
            counts["started"] += started is not None
            counts["completed"] += completed is not None
            counts["late"] += row["late_by_ms"] > schedule["late_tolerance_seconds"] * 1000
            if status != "success":
                continue
            sample = row.get("sample")
            if stream == "continuous_ingest":
                if (not isinstance(sample, dict)
                        or not _is_int(continuous_block_rows, 1)
                        or not isinstance(continuous_watermarks, (set, frozenset))
                        or not continuous_watermarks
                        or sample.get("rows") != continuous_block_rows
                        or not _is_int(sample.get("rows"), 1)
                        or not _is_int(sample.get("watermark"), 1)
                        or sample["watermark"] not in continuous_watermarks
                ) or not isinstance(sample.get("watermarks"), dict) or any(
                    not _is_int(value) for value in sample["watermarks"].values()
                ) or write_tables is not None and (
                    set(sample["watermarks"]) != set(write_tables)
                    or any(value != sample["watermark"] for value in sample["watermarks"].values())
                ):
                    raise RuntimeError("continuous_ingest success lacks BlockResult")
            else:
                expected = (query_scenarios or {
                    "list": ("list", "list:first"), "preview": ("preview", "preview:first"),
                    "detail_2m": ("detail", "detail:text_2m"),
                    "trace_long": ("trace", "trace:p95"),
                    "batch_loop": ("batch", "batch:main"),
                }).get(stream)
                validation = sample.get("validation") if isinstance(sample, dict) else None
                if (expected is None or sample.get("kind") != expected[0]
                        or sample.get("scenario") != expected[1]
                        or sample.get("status") != "success"
                        or not isinstance(sample.get("query_id"), str) or not sample["query_id"]
                        or sample["query_id"] in query_ids or not _is_int(sample.get("response_bytes"), 1)
                        or not isinstance(validation, dict)
                        or not _is_int(validation.get("row_count"))
                        or not _is_int(validation.get("validated_payload_bytes"))):
                    raise RuntimeError("interference successful query evidence is invalid")
                query_ids.add(sample["query_id"])
                successful_queries.append(sample)
        scheduled = len(rows)
        if schedule.get("mode") == "fixed" and scheduled != math.ceil(
            schedule["duration_seconds"] * schedule["rate_per_second"]
        ):
            raise RuntimeError("interference fixed scheduled count is invalid")
        result[stream] = {"counts": {
            "scheduled_requests": scheduled, "started_requests": counts["started"],
            "completed_requests": counts["completed"], "successful_requests": counts["success"],
            "failed_requests": counts["failed"], "timed_out_requests": counts["timed_out"],
            "dropped_requests": counts["dropped"], "late_requests": counts["late"],
        }, "successful_queries": successful_queries}
        if stream != "continuous_ingest" and len(successful_queries) != counts["success"]:
            raise RuntimeError("interference successful query count mismatch")
    return result


def _gate_interference_summary(summary, parsed, schedules):
    """交叉核对 raw 重算计数与发布 summary。"""
    if not isinstance(summary, dict) or set(summary) != set(schedules):
        raise RuntimeError("interference summary streams mismatch")
    for stream, schedule in schedules.items():
        item = summary.get(stream)
        counts = parsed[stream]["counts"]
        if not isinstance(item, dict) or any(
            not _is_int(item.get(key)) or item[key] != value for key, value in counts.items()
        ):
            raise RuntimeError("interference raw and summary counts mismatch")
        wall = item.get("phase_wall_seconds")
        offered = item.get("offered_rate_requests_s")
        expected_rate = schedule.get("rate_per_second")
        latency = item.get("latency_ms")
        throughput = item.get("completed_throughput_requests_s")
        success_count = counts["successful_requests"]
        latency_values = ("minimum", "p50", "p95", "maximum")
        if ((expected_rate is None and offered is not None)
                or (expected_rate is not None and (
                    not _finite_number(offered) or offered != expected_rate))
                or not _finite_number(wall, 0) or wall <= 0
                or not _finite_number(throughput)
                or not math.isclose(throughput, success_count / wall, rel_tol=1e-9, abs_tol=1e-12)
                or not isinstance(latency, dict)
                or not _is_int(latency.get("p99_minimum_successes"))
                or latency["p99_minimum_successes"] != run_interference.P99_MINIMUM_SUCCESSES
                or latency.get("p99_status") != (
                    "publishable" if success_count >= run_interference.P99_MINIMUM_SUCCESSES
                    else "unavailable_insufficient_successes")
                or (success_count >= run_interference.P99_MINIMUM_SUCCESSES
                    and not _finite_number(latency.get("p99")))
                or (success_count < run_interference.P99_MINIMUM_SUCCESSES
                    and latency.get("p99") is not None)
                or (success_count == 0 and any(latency.get(key) is not None for key in latency_values))
                or (success_count > 0 and any(
                    not _finite_number(latency.get(key)) for key in latency_values))):
            raise RuntimeError("interference summary publication evidence is invalid")


def _gate_interference_access(manifest, queries):
    """核对成功 query 与 plan/detail/QueryFinish 的一一对应。"""
    if not queries or any(not values for values in queries.values()):
        raise RuntimeError("interference query stream lacks a success")
    flattened = [sample for values in queries.values() for sample in values]
    samples = {sample["query_id"]: sample for sample in flattened}
    if len(samples) != len(flattened):
        raise RuntimeError("interference query IDs are duplicated across segments")
    evidence = manifest.get("query_evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError("interference query evidence is missing")
    expected = set(samples)
    for key in ("plans", "query_details", "query_finish"):
        if not isinstance(evidence.get(key), dict) or set(evidence[key]) != expected:
            raise RuntimeError("interference query evidence IDs mismatch")
    for query_id, sample in samples.items():
        plan, detail, finish = (evidence[key][query_id]
                                for key in ("plans", "query_details", "query_finish"))
        validation = sample["validation"]
        if (not isinstance(plan, str) or not plan or not isinstance(detail, dict)
                or detail.get("kind") != sample["kind"]
                or not isinstance(detail.get("statement"), str) or not detail["statement"]
                or not isinstance(detail.get("declared_source"), str) or not detail["declared_source"]
                or not _is_int(detail.get("scanned_rows")) or not _is_int(detail.get("scanned_bytes"))
                or not isinstance(finish, dict) or finish.get("type") != "QueryFinish"
                or not _is_int(finish.get("exception_code")) or finish["exception_code"] != 0
                or not _is_int(finish.get("read_rows"))
                or not _is_int(finish.get("read_bytes"))
                or detail["scanned_rows"] != finish["read_rows"]
                or detail["scanned_bytes"] != finish["read_bytes"]
                or finish["read_rows"] < validation["row_count"]):
            raise RuntimeError("interference detail and QueryFinish evidence conflict")


def _verify_interference_artifacts(output, artifacts):
    """重读已门禁 artifact，拒绝发布前 bytes 被替换。"""
    output = Path(output).resolve()
    if not isinstance(artifacts, list) or not artifacts:
        raise RuntimeError("interference artifact evidence is missing")
    for expected in artifacts:
        path = (output / expected.get("path", "")).resolve()
        try:
            path.relative_to(output / "child")
        except ValueError as error:
            raise RuntimeError("interference artifact escapes child root") from error
        if not path.is_file():
            raise RuntimeError("interference artifact changed after gate")
        content = path.read_bytes()
        if expected != {"path": path.relative_to(output).as_posix(), "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest()}:
            raise RuntimeError("interference artifact changed after gate")


def _gate_interference(child, formal, layout):
    """从 child bytes 独立门禁五阶段 formal interference 证据。"""
    root_path = Path(child["_path"]).resolve()
    output = root_path.parents[1]
    if (child.get("_relative") != "child/run-manifest.json"
            or root_path != output / "child" / "run-manifest.json"):
        raise RuntimeError("interference child manifest path is invalid")
    manifest = child["_manifest"]
    phases = tuple(run_interference.FIXED_PHASES)
    names = [phase.name for phase in phases]
    fixed = {"format": "agent-trace-json-storage-stage3-interference-run",
             "format_version": 1, "status": "complete", "seed": SEED,
             "execution_scope": "formal", "classification": "formal_complete",
             "phase_order": names}
    if any(manifest.get(key) != value for key, value in fixed.items()) or any(
        not _is_int(manifest.get(key)) for key in ("format_version", "seed")
    ) or any(key in manifest for key in ("error", "errors", "execution_resolution")):
        raise RuntimeError("interference root manifest is not formal complete")
    required_scenarios = {"list:first": "list", "preview:first": "preview",
                          "detail:text_2m": "detail", "trace:p95": "trace",
                          "batch:main": "batch"}
    formal_catalog = {(truth.scenario, query.kind) for query, truth in formal.main_queries}
    if any((scenario, kind) not in formal_catalog for scenario, kind in required_scenarios.items()):
        raise RuntimeError("interference formal query catalog is incomplete")
    query_scenarios = {
        "list": ("list", "list:first"), "preview": ("preview", "preview:first"),
        "detail_2m": ("detail", "detail:text_2m"), "trace_long": ("trace", "trace:p95"),
        "batch_loop": ("batch", "batch:main"),
    }
    embedded = manifest.get("phases")
    if not isinstance(embedded, list) or len(embedded) != len(phases):
        raise RuntimeError("interference root phases are incomplete")
    child_root = root_path.parent
    artifacts = [{"path": child["_relative"], **child["_identity"]}]
    phase_evidence, namespaces = [], []
    catalog = common.build_layout_catalog(layout)
    continuous_blocks = production._continuous_blocks(formal)
    continuous_watermarks = frozenset(
        int(block[-1]["ingest_seq"]) + 1 for _, block in continuous_blocks
    )
    for phase, root_phase in zip(phases, embedded):
        phase_path = child_root / phase.name / "run-manifest.json"
        identity = _artifact_identity(output, child_root, phase_path)
        phase_manifest = _load_json_object(phase_path.read_bytes(), "interference phase manifest")
        if phase_manifest != root_phase:
            raise RuntimeError("interference phase file and root value mismatch")
        expected = {"format": "agent-trace-json-storage-stage3-interference-phase",
                    "format_version": 1, "status": "complete", "phase": phase.name,
                    "seed": SEED, "execution_scope": "formal",
                    "classification": "formal_complete", "layout": layout,
                    "warmup_seconds": 30.0, "measurement_seconds": 300.0}
        if any(phase_manifest.get(key) != value for key, value in expected.items()) or any(
            not _is_int(phase_manifest.get(key)) for key in ("format_version", "seed")
        ) or any(
            key in phase_manifest for key in ("error", "errors", "execution_resolution")
        ):
            raise RuntimeError("interference phase manifest mismatch")
        namespace = phase_manifest.get("namespace")
        if not isinstance(namespace, str) or not namespace or namespace in namespaces:
            raise RuntimeError("interference namespaces are invalid or duplicated")
        namespaces.append(namespace)
        cleanup = phase_manifest.get("cleanup")
        if not isinstance(cleanup, dict) or cleanup.get("removed") is not True or cleanup.get(
            "namespace") != clickhouse.database_name(namespace, layout):
            raise RuntimeError("interference cleanup evidence is invalid")
        schedules = {segment: run_interference._json_value(
            run_interference.fixed_phase_schedules(phase, measurement=segment == "measurement"))
            for segment in ("warmup", "measurement")}
        if not _matches_fixed_json(phase_manifest.get("schedules"), schedules):
            raise RuntimeError("interference schedules mismatch")
        coverage = phase_manifest.get("execution_coverage")
        if not isinstance(coverage, dict) or not _finite_number(
            coverage.get("warmup_actual_seconds"), 30
        ) or not _finite_number(coverage.get("measurement_actual_seconds"), 300):
            raise RuntimeError("interference formal coverage is short")
        snapshots = phase_manifest.get("snapshots")
        if not isinstance(snapshots, list) or [item.get("name") for item in snapshots] != [
            "before_warmup", "before_measurement", "after_measurement"]:
            raise RuntimeError("interference snapshots are incomplete")
        for snapshot in snapshots:
            run_interference.validate_resource_snapshot(snapshot.get("resources"))
            if not _finite_number(snapshot.get("captured_offset_seconds")):
                raise RuntimeError("interference snapshot offset is invalid")
            storage = snapshot.get("storage")
            tables = storage.get("tables") if isinstance(storage, dict) else None
            merges = storage.get("merges") if isinstance(storage, dict) else None
            if not isinstance(tables, dict) or set(tables) != set(catalog.write_tables) or not isinstance(merges, list):
                raise RuntimeError("interference storage evidence is incomplete")
            for table in tables.values():
                if not isinstance(table, dict) or any(not _is_int(table.get(key)) for key in (
                    "part_count", "marks", "compressed_bytes", "uncompressed_bytes")):
                    raise RuntimeError("interference storage metrics are invalid")
            backlog = sum(max(0, table["part_count"] - 1) for table in tables.values())
            if (not _is_int(snapshot.get("active_part_backlog"))
                    or snapshot["active_part_backlog"] != backlog
                    or not _is_int(snapshot.get("active_merge_count"))
                    or snapshot["active_merge_count"] != len(merges)):
                raise RuntimeError("interference storage totals mismatch")
            if layout == "asset_ref" and (not isinstance(storage.get("asset_store"), dict) or any(
                not _is_int(storage["asset_store"].get(key)) for key in (
                    "available_object_count", "available_bytes", "orphan_object_count", "orphan_bytes"))):
                raise RuntimeError("interference asset store evidence is incomplete")
        queries = {}
        raw_evidence = []
        for segment, filename, summary_key in (("warmup", "warmup-samples.jsonl", "warmup"),
                                                ("measurement", "samples.jsonl", "statistics")):
            raw_path = phase_path.parent / filename
            raw_evidence.append(_artifact_identity(output, child_root, raw_path))
            parsed = _parse_interference_raw(
                raw_path, schedules[segment], query_scenarios, catalog.write_tables,
                continuous_block_rows=formal.truth.block_size,
                continuous_watermarks=continuous_watermarks,
            )
            _gate_interference_summary(phase_manifest.get(summary_key), parsed, schedules[segment])
            for stream, item in parsed.items():
                if stream != "continuous_ingest":
                    queries.setdefault(stream, []).extend(item["successful_queries"])
        _gate_interference_access(phase_manifest, queries)
        artifacts.extend([identity, *raw_evidence])
        phase_evidence.append({"phase": phase.name, "namespace": namespace,
                               "manifest": identity, "raw": raw_evidence})
    return {"namespaces": namespaces, "phases": phase_evidence, "artifacts": artifacts}


def _gate_blocks(write, formal):
    """逐块核对 candidate 的行数、水位和联合可见证据。"""
    blocks = write.get("blocks")
    if not isinstance(blocks, list) or len(blocks) != formal.truth.block_count:
        raise RuntimeError("candidate block evidence is incomplete")
    previous = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise RuntimeError("candidate block evidence is invalid")
        ingest = block.get("ingest")
        visible = block.get("visible")
        watermark = min((index + 1) * formal.truth.block_size, formal.truth.record_count)
        rows = watermark - previous
        if not isinstance(ingest, dict) or (
            ingest.get("rows"), ingest.get("watermark")
        ) != (rows, watermark) or not all(
            _is_int(ingest.get(key)) for key in ("rows", "watermark")
        ):
            raise RuntimeError("candidate ingest block evidence mismatch")
        if set(ingest.get("watermarks", {})) != WATERMARK_KEYS or any(
            not _is_int(value) or value != watermark
            for value in ingest["watermarks"].values()
        ):
            raise RuntimeError("candidate ingest joint watermark mismatch")
        if not isinstance(visible, dict) or visible.get("completed") is not True:
            raise RuntimeError("candidate visible block evidence is incomplete")
        if set(visible.get("watermarks", {})) != WATERMARK_KEYS or any(
            not _is_int(value) or value != watermark
            for value in visible["watermarks"].values()
        ):
            raise RuntimeError("candidate visible joint watermark mismatch")
        previous = watermark


def _gate_candidate(child, formal, namespace):
    """独立门禁 candidate child，不信任 runner 返回值。"""
    manifest = child["_manifest"]
    fixed = {
        "format": CHILD_FORMAT, "format_version": 1, "status": "complete",
        "engine": "clickhouse", "layout": "asset_ref", "workload": "main",
        "round_index": 0, "round_order": list(LATIN_FIRST), "seed": SEED,
        "measurements": 30, "batch_measurements": 5,
    }
    for key, expected in fixed.items():
        value = manifest.get(key)
        if value != expected or (
            isinstance(expected, int) and not isinstance(expected, bool) and not _is_int(value)
        ):
            raise RuntimeError(f"candidate {key} mismatch")
    if manifest.get("input") != _json_value(formal.identity):
        raise RuntimeError("candidate input identity mismatch")
    expected_query_digest = _query_catalog_sha256(formal)
    if manifest.get("query_catalog_sha256") != expected_query_digest:
        raise RuntimeError("candidate query catalog identity mismatch")

    write = _require_object(manifest, "write")
    expected_write = {
        "row_count": formal.truth.record_count,
        "block_count": formal.truth.block_count,
        "final_watermark": formal.truth.record_count,
    }
    if any(
        not _is_int(write.get(key)) or write.get(key) != value
        for key, value in expected_write.items()
    ):
        raise RuntimeError("candidate write evidence mismatch")
    _gate_blocks(write, formal)
    dataset = _require_object(manifest, "dataset_audit")
    dataset_counts = {
        "row_count": formal.truth.record_count,
        "duplicate_event_ids": 0,
        "payload_count": 160,
        "payload_bytes": 128_450_560,
    }
    if (
        any(not _is_int(dataset.get(key)) or dataset.get(key) != value
            for key, value in dataset_counts.items())
        or dataset.get("identity_sha256") != formal.truth.identity_sha256
        or not _is_int(dataset.get("logical_response_bytes"), 1)
        or not _is_int(dataset.get("database_protocol_bytes"), 0)
    ):
        raise RuntimeError("candidate dataset audit evidence mismatch")
    targets = dataset.get("physical_targets")
    if not isinstance(targets, dict) or set(targets) != WATERMARK_KEYS:
        raise RuntimeError("candidate dataset target evidence is incomplete")
    for target, expected_rows in (("events_analytics", formal.truth.record_count), ("assets", 160)):
        evidence = targets.get(target)
        if (
            not isinstance(evidence, dict) or not evidence
            or not _is_int(evidence.get("row_count"))
            or evidence["row_count"] != expected_rows
            or not _is_int(evidence.get("duplicate_identities"))
            or evidence["duplicate_identities"] != 0
            or not _is_sha256(evidence.get("identity_metadata_sha256"))
        ):
            raise RuntimeError("candidate dataset target evidence mismatch")
    asset_target = targets["assets"]
    if (
        not _is_int(asset_target.get("event_mapping_count"))
        or asset_target["event_mapping_count"] != 160
        or not _is_sha256(asset_target.get("event_mapping_sha256"))
    ):
        raise RuntimeError("candidate Asset mapping audit evidence mismatch")

    storage = _require_object(manifest, "storage")
    tables = storage.get("tables")
    if not isinstance(tables, dict) or set(tables) != WATERMARK_KEYS:
        raise RuntimeError("candidate storage evidence is incomplete")
    for table, expected_rows in (("events_analytics", formal.truth.record_count), ("assets", 160)):
        evidence = tables.get(table)
        if not isinstance(evidence, dict) or not evidence:
            raise RuntimeError("candidate storage table evidence is empty")
        for key in ("part_count", "rows", "marks", "compressed_bytes", "uncompressed_bytes"):
            if not _is_int(evidence.get(key)):
                raise RuntimeError("candidate storage table evidence is invalid")
        if evidence["part_count"] <= 0 or evidence["rows"] != expected_rows:
            raise RuntimeError("candidate storage table row evidence mismatch")
        if not isinstance(evidence.get("columns"), dict):
            raise RuntimeError("candidate storage column evidence is missing")
    if not isinstance(storage.get("merges"), list):
        raise RuntimeError("candidate merge evidence is missing")
    asset_store = storage.get("asset_store")
    expected_asset_store = {
        "available_object_count": 160, "available_bytes": 128_450_560,
        "orphan_object_count": 0, "orphan_bytes": 0,
    }
    if not isinstance(asset_store, dict) or any(
        not _is_int(asset_store.get(key)) or asset_store.get(key) != value
        for key, value in expected_asset_store.items()
    ):
        raise RuntimeError("candidate Asset storage evidence mismatch")

    query_ids = _read_samples(child)
    access = _require_object(manifest, "access")
    for key in ("plans", "query_finish", "query_details"):
        evidence = access.get(key)
        if not isinstance(evidence, dict) or set(evidence) != set(query_ids):
            raise RuntimeError(f"candidate {key} does not cover successful samples")
    for query_id in query_ids:
        plan = access["plans"][query_id]
        finish = access["query_finish"][query_id]
        detail = access["query_details"][query_id]
        if not isinstance(plan, str) or not plan.strip():
            raise RuntimeError("candidate query plan is empty")
        if (
            not isinstance(finish, dict) or finish.get("type") != "QueryFinish"
            or not _is_int(finish.get("exception_code")) or finish["exception_code"] != 0
            or not _is_int(finish.get("read_rows")) or not _is_int(finish.get("read_bytes"))
        ):
            raise RuntimeError("candidate QueryFinish evidence is invalid")
        if (
            not isinstance(detail, dict) or detail.get("kind") not in {
                "list", "preview", "detail", "trace", "batch",
            }
            or not isinstance(detail.get("statement"), str) or not detail["statement"].strip()
            or not isinstance(detail.get("payload_selected"), bool)
            or not isinstance(detail.get("declared_source"), str)
            or not detail["declared_source"]
            or not _is_int(detail.get("scanned_rows"))
            or not _is_int(detail.get("scanned_bytes"))
            or detail.get("scanned_bytes_status") != "observed"
            or detail["scanned_rows"] != finish["read_rows"]
            or detail["scanned_bytes"] != finish["read_bytes"]
        ):
            raise RuntimeError("candidate query detail evidence is invalid")
    correctness = _require_object(manifest, "correctness")
    if correctness.get("truth_identity") != formal.truth.identity_sha256:
        raise RuntimeError("candidate truth identity mismatch")
    if (
        not _is_int(correctness.get("failed_samples"))
        or correctness["failed_samples"] != 0
        or not _is_int(correctness.get("formal_samples"), 1)
        or correctness["formal_samples"] != len(query_ids)
        or not _is_int(correctness.get("successful_samples"), 1)
        or correctness["successful_samples"] != len(query_ids)
        or not query_ids
        or correctness.get("response_bytes_validated") is not True
    ):
        raise RuntimeError("candidate correctness evidence mismatch")

    maintenance = _require_object(manifest, "maintenance")
    if (
        maintenance.get("completed") is not True
        or maintenance.get("natural_stable_parts") is not True
        or maintenance.get("optimize_final") is not False
        or set(maintenance.get("watermarks", {})) != WATERMARK_KEYS
        or any(
            not _is_int(value) or value != formal.truth.record_count
            for value in maintenance["watermarks"].values()
        )
    ):
        raise RuntimeError("candidate maintenance evidence mismatch")
    cleanup = _require_object(manifest, "cleanup")
    if (
        cleanup.get("namespace") != clickhouse.database_name(namespace, "asset_ref")
        or cleanup.get("removed") is not True
        or cleanup.get("asset_directory_removed") is not True
    ):
        raise RuntimeError("candidate cleanup evidence mismatch")
    code = _require_object(manifest, "code")
    if not {"runner", "common", "assets", "adapter"}.issubset(code):
        raise RuntimeError("candidate code evidence roles are incomplete")
    for evidence in code.values():
        if (
            not isinstance(evidence, dict) or not evidence
            or not isinstance(evidence.get("path"), str) or not evidence["path"]
            or not _is_int(evidence.get("bytes"), 1)
            or not _is_sha256(evidence.get("sha256"))
        ):
            raise RuntimeError("candidate code evidence is invalid")
    runtime = _require_object(manifest, "engine_runtime")
    if (
        not isinstance(runtime.get("version"), str) or not runtime["version"]
        or runtime.get("source") != "database-query"
    ):
        raise RuntimeError("candidate engine runtime evidence is invalid")
    container = _require_object(manifest, "container")
    if container.get("container") != EngineEndpoints().clickhouse_container:
        raise RuntimeError("candidate container identity mismatch")
    if (
        not isinstance(container.get("image"), str) or not container["image"]
        or not isinstance(container.get("image_id"), str) or not container["image_id"]
    ):
        raise RuntimeError("candidate container image identity is missing")
    host = _require_object(manifest, "host")
    if (
        not isinstance(host.get("platform"), str) or not host["platform"]
        or not isinstance(host.get("machine"), str) or not host["machine"]
        or not _is_int(host.get("cpu_count"), 1)
        or not _is_int(host.get("memory_total_kib"), 1)
    ):
        raise RuntimeError("candidate host evidence is invalid")


def _require_part_object(value, name):
    """取得非空 part-state object，缺失时拒绝发布 complete。"""
    if not isinstance(value, dict) or not value:
        raise RuntimeError(f"part-state {name} evidence is missing")
    return value


def _gate_part_state_samples(state, query_catalog, samples_per_query):
    """核对一个物理状态的成功样本、访问路径和 QueryFinish 证据。"""
    expected_queries = Counter(
        (truth.scenario, query.kind)
        for query, truth in query_catalog
        for _ in range(samples_per_query)
    )
    expected_count = len(query_catalog) * samples_per_query
    samples = state.get("query_samples")
    if not isinstance(samples, list) or len(samples) != expected_count:
        raise RuntimeError("part-state query sample count mismatch")
    query_ids = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise RuntimeError("part-state query sample is invalid")
        query_id = sample.get("query_id")
        validation = sample.get("validation")
        if (
            sample.get("status") != "success" or sample.get("error") is not None
            or not isinstance(query_id, str) or not query_id
            or not _is_int(sample.get("response_bytes"), 1)
            or not isinstance(validation, dict)
            or not _is_int(validation.get("row_count"))
            or not _is_int(validation.get("validated_payload_bytes"))
        ):
            raise RuntimeError("part-state query sample evidence is invalid")
        query_ids.append(query_id)
    if Counter((sample.get("scenario"), sample.get("kind")) for sample in samples) != expected_queries:
        raise RuntimeError("part-state formal query samples mismatch")
    if len(set(query_ids)) != len(query_ids):
        raise RuntimeError("part-state query IDs are duplicated")
    samples_by_id = {sample["query_id"]: sample for sample in samples}
    expected_ids = set(query_ids)
    evidence = {
        "query_plans": state.get("query_plans"),
        "query_details": state.get("query_details"),
        "query_finish": state.get("query_finish"),
    }
    if any(not isinstance(value, dict) or set(value) != expected_ids for value in evidence.values()):
        raise RuntimeError("part-state access evidence does not cover successful samples")
    for query_id in query_ids:
        plan = evidence["query_plans"][query_id]
        detail = evidence["query_details"][query_id]
        finish = evidence["query_finish"][query_id]
        if not isinstance(plan, str) or not plan.strip():
            raise RuntimeError("part-state query plan is empty")
        if (
            not isinstance(detail, dict)
            or not isinstance(detail.get("kind"), str) or not detail["kind"]
            or not isinstance(detail.get("statement"), str) or not detail["statement"].strip()
            or not isinstance(detail.get("declared_source"), str) or not detail["declared_source"]
            or not _is_int(detail.get("scanned_rows"))
            or not _is_int(detail.get("scanned_bytes"))
        ):
            raise RuntimeError("part-state query detail evidence is invalid")
        if (
            not isinstance(finish, dict) or finish.get("type") != "QueryFinish"
            or not _is_int(finish.get("exception_code")) or finish["exception_code"] != 0
            or not _is_int(finish.get("read_rows")) or not _is_int(finish.get("read_bytes"))
        ):
            raise RuntimeError("part-state QueryFinish evidence is invalid")
        sample = samples_by_id[query_id]
        if (
            detail["kind"] != sample.get("kind")
            or detail["scanned_rows"] != finish["read_rows"]
            or detail["scanned_bytes"] != finish["read_bytes"]
            or finish["read_rows"] < sample["validation"]["row_count"]
        ):
            raise RuntimeError("part-state query evidence is inconsistent")
    if (
        not _is_int(state.get("successful_samples"), 1)
        or state["successful_samples"] != expected_count
        or not _is_int(state.get("failed_samples")) or state["failed_samples"] != 0
        or not _is_int(state.get("query_finish_count"), 1)
        or state["query_finish_count"] != expected_count
    ):
        raise RuntimeError("part-state sample totals are invalid")


def _gate_part_states(child, formal, layout, database):
    """独立门禁 part-state child，不信任 runner 的返回对象。"""
    manifest = child["_manifest"]
    catalog = common.build_layout_catalog(layout)
    fixed = {
        "format": "agent-trace-json-storage-stage3-clickhouse-part-states",
        "format_version": 1,
        "status": "complete",
        "layout": layout,
        "physical_targets": list(catalog.write_tables),
        "controlled_table": catalog.list_source,
        "state_order": ["fragmented", "merging", "stable", "single_part"],
        "samples_per_query": 30,
    }
    for key, expected in fixed.items():
        value = manifest.get(key)
        if value != expected or (
            isinstance(expected, int) and not isinstance(expected, bool) and not _is_int(value)
        ):
            raise RuntimeError(f"part-state {key} mismatch")
    if "errors" in manifest:
        raise RuntimeError("part-state child contains errors")
    states = manifest.get("states")
    if not isinstance(states, list) or len(states) != 4:
        raise RuntimeError("part-state states are incomplete")
    for expected_name, state in zip(fixed["state_order"], states):
        if not isinstance(state, dict) or state.get("name") != expected_name:
            raise RuntimeError("part-state state order mismatch")
        if state.get("controlled_table") != catalog.list_source:
            raise RuntimeError("part-state controlled table mismatch")
        if state.get("predicate_proven") is not True or state.get("error") is not None:
            raise RuntimeError("part-state predicate evidence is invalid")
        tables = state.get("tables")
        if not isinstance(tables, dict) or set(tables) != set(catalog.write_tables):
            raise RuntimeError("part-state table evidence is incomplete")
        for table in catalog.write_tables:
            values = tables[table]
            if not isinstance(values, dict) or any(
                not _is_int(values.get(metric))
                for metric in ("part_count", "marks", "compressed_bytes", "uncompressed_bytes")
            ):
                raise RuntimeError("part-state table metrics are invalid")
        if not isinstance(state.get("active_merges"), list) or not isinstance(state.get("observations"), list):
            raise RuntimeError("part-state observations are invalid")
        if layout == "asset_ref":
            asset_store = _require_part_object(state.get("asset_store"), "asset store")
            if any(not _is_int(asset_store.get(field)) for field in (
                "available_object_count", "available_bytes", "orphan_object_count", "orphan_bytes",
            )):
                raise RuntimeError("part-state asset store evidence is invalid")
        expected_optimized = list(catalog.write_tables) if expected_name == "single_part" else []
        if state.get("optimized_targets") != expected_optimized:
            raise RuntimeError("part-state optimized targets mismatch")
        _gate_part_state_samples(state, formal.main_queries, fixed["samples_per_query"])
    restoration = _require_part_object(manifest.get("restoration"), "restoration")
    if (
        restoration.get("attempted") is not True or restoration.get("restored") is not True
        or restoration.get("targets") != list(catalog.write_tables)
    ):
        raise RuntimeError("part-state restoration evidence is invalid")
    cleanup = _require_part_object(manifest.get("cleanup"), "cleanup")
    if cleanup.get("namespace") != database or cleanup.get("removed") is not True:
        raise RuntimeError("part-state cleanup evidence is invalid")


def _database_runtime_evidence(adapter, engine, endpoints):
    """在 namespace 创建前采集并门禁固定数据库运行身份。"""
    if engine == "opengauss":
        container_name = endpoints.opengauss_container
    elif engine == "clickhouse":
        container_name = endpoints.clickhouse_container
    else:
        raise ValueError(f"unsupported runtime engine: {engine}")
    runtime = run_layout_matrix._engine_runtime(adapter, engine)
    container = run_layout_matrix._container_evidence(container_name)
    host = run_layout_matrix._host_evidence()
    if (
        not isinstance(runtime, dict) or set(runtime) != {"version", "source"}
        or not isinstance(runtime.get("version"), str)
        or not runtime["version"] or runtime.get("source") != "database-query"
    ):
        raise RuntimeError("database runtime evidence is invalid")
    if (
        not isinstance(container, dict) or set(container) != {"container", "image", "image_id"}
        or container.get("container") != container_name
        or not isinstance(container.get("image"), str) or not container["image"]
        or not isinstance(container.get("image_id"), str) or not container["image_id"]
    ):
        raise RuntimeError("database container evidence is invalid")
    if (
        not isinstance(host, dict)
        or set(host) != {"platform", "machine", "cpu_count", "memory_total_kib"}
        or not isinstance(host.get("platform"), str) or not host["platform"]
        or not isinstance(host.get("machine"), str) or not host["machine"]
        or not _is_int(host.get("cpu_count"), 1) or not _is_int(host.get("memory_total_kib"), 1)
    ):
        raise RuntimeError("database host evidence is invalid")
    return (
        _strict_json_snapshot(runtime, "database runtime evidence"),
        _strict_json_snapshot(container, "database container evidence"),
        _strict_json_snapshot(host, "database host evidence"),
    )


def _remove_asset_directory(asset_root):
    """删除本命令独占的 Asset 目录，并确认路径已不存在。"""
    asset_root = Path(asset_root)
    if asset_root.exists():
        shutil.rmtree(asset_root)
    if asset_root.exists():
        raise RuntimeError("asset directory remains after cleanup")


def _gate_interference_metadata(metadata, formal):
    """复制并门禁固定 factory metadata，拒绝类型或冻结输入漂移。"""
    snapshot = _strict_json_snapshot(metadata, "interference factory metadata")
    eligible = production._continuous_blocks(formal)
    expected = {
        "selection_rules": {
            "full_block_rows": formal.truth.block_size,
            "outside_query_window": _json_value(formal.truth.query_window),
            "requires_main_payload": True,
            "query_scenarios": {
                "list": "list:first", "preview": "preview:first",
                "detail_2m": "detail:text_2m", "trace_long": "trace:p95",
                "batch_loop": "batch:main",
            },
        },
        "eligible_block_count": len(eligible),
        "eligible_block_indices": [index for index, _ in eligible],
        "eligible_block_sha256": [
            canonical_digest([_json_value(row) for row in block]) for _, block in eligible
        ],
        "cyclic_replay": True,
        "main_query_catalog_sha256": _query_catalog_sha256(formal),
        "preload_block_count": len(formal.main_blocks),
        "block_size": formal.truth.block_size,
        "final_watermark": formal.truth.record_count,
        "seed": formal.truth.seed,
    }
    if not _matches_fixed_json(snapshot, expected):
        raise RuntimeError("interference factory metadata is invalid")
    if snapshot["seed"] != SEED or snapshot["final_watermark"] != 48_534:
        raise RuntimeError("interference factory metadata violates formal constants")
    return snapshot


def _interference_partial_namespaces(output):
    """尽力从 child root 保留失败前已经发布的 phase namespace。"""
    path = Path(output) / "child" / "run-manifest.json"
    try:
        manifest = _load_json_object(path.read_bytes(), "interference child manifest")
    except (OSError, ValueError):
        return []
    phases = manifest.get("phases")
    if not isinstance(phases, list):
        return []
    namespaces = []
    for phase in phases:
        namespace = phase.get("namespace") if isinstance(phase, dict) else None
        if isinstance(namespace, str) and namespace and namespace not in namespaces:
            namespaces.append(namespace)
    return namespaces


def _run_interference(arguments, envelope):
    """执行固定 ClickHouse interference，并发布门禁后的运行证据。"""
    output = arguments.output.resolve()
    layout = arguments.layout
    formal = load_formal_input(arguments.input.resolve())
    _record_formal_evidence(envelope, formal)
    asset_root = output / "assets" if layout == "asset_ref" else None
    envelope["namespace_policy"] = {
        "strategy": "runner-fixed-phase-unique", "reuse": False,
        "phase_order": [phase.name for phase in run_interference.FIXED_PHASES],
        "namespace_prefix": "jsons3_if_<phase>_", "namespaces": [],
    }
    envelope["cleanup"] = {
        "namespaces": [], "namespaces_removed": False,
        "asset_directory_applicable": layout == "asset_ref",
        "asset_directory_removed": layout != "asset_ref",
    }
    endpoints = EngineEndpoints()
    probe = create_adapter(
        "clickhouse", layout, "jsons3_runtime_probe", formal, asset_root, endpoints,
    )
    runtime, container, host = _database_runtime_evidence(probe, "clickhouse", endpoints)
    envelope["runtime"] = {
        "operation": "interference", "engine": "clickhouse", "layout": layout,
        "endpoint": {"host": endpoints.clickhouse_host, "port": endpoints.clickhouse_port},
        "container": _json_value(container), "engine_runtime": _json_value(runtime),
        "host": _json_value(host),
    }
    adapter_factory, targets_factory, metadata = production.interference_factories(
        formal, layout, asset_root, endpoints,
    )
    try:
        metadata = _strict_json_snapshot(metadata, "interference factory metadata")
    except RuntimeError as error:
        envelope["interference_snapshot"] = {
            "status": "failed",
            "error": {"type": type(error).__name__, "message": str(error)},
        }
        raise
    envelope["interference"] = metadata
    _gate_interference_metadata(metadata, formal)
    child_root = output / "child"
    run_interference.run_interference(
        adapter_factory, targets_factory, child_root, scope="formal",
    )
    child = _read_child(output, child_root / "run-manifest.json")
    evidence = _gate_interference(child, formal, layout)
    cleanup = {
        "namespaces": list(evidence["namespaces"]), "namespaces_removed": True,
        "asset_directory_applicable": layout == "asset_ref",
        "asset_directory_removed": layout != "asset_ref",
    }
    envelope["namespace_policy"]["namespaces"] = list(evidence["namespaces"])
    envelope["cleanup"] = cleanup
    if asset_root is not None:
        _remove_asset_directory(asset_root)
        cleanup["asset_directory_removed"] = True
    _verify_interference_artifacts(output, evidence["artifacts"])
    confirmed = _read_child(output, child_root / "run-manifest.json")
    if confirmed["_identity"] != child["_identity"]:
        raise RuntimeError("interference child manifest changed after gate")
    envelope.update({
        "artifacts": evidence["artifacts"],
        "child": _child_evidence(confirmed),
        "cleanup": cleanup,
    })


def _asset_failure_partial_namespaces(output):
    """尽力从 child root 保留失败前已经发布的 case namespace。"""
    path = Path(output) / "child" / "run-manifest.json"
    try:
        manifest = _load_json_object(path.read_bytes(), "asset failure child manifest")
    except (OSError, ValueError):
        return []
    results = manifest.get("results")
    if not isinstance(results, list):
        return []
    namespaces = []
    for result in results:
        if not isinstance(result, dict):
            continue
        case, namespace = result.get("case"), result.get("namespace")
        if (
            case in ASSET_FAILURE_CASES
            and isinstance(namespace, str)
            and re.fullmatch(r"jsons3_af_" + re.escape(case) + r"_[0-9a-f]{10}", namespace)
            and namespace not in namespaces
        ):
            namespaces.append(namespace)
    return namespaces


def _run_asset_failures(arguments, envelope):
    """执行固定两引擎 Asset 六故障，并发布门禁后的运行证据。"""
    output = arguments.output.resolve()
    engine = arguments.engine
    probe_directory = output / "runtime-probe-assets"
    envelope["namespace_policy"] = {
        "strategy": "runner-fixed-case-unique", "reuse": False,
        "case_order": list(ASSET_FAILURE_CASES),
        "namespace_prefix": "jsons3_af_<case>_", "namespaces": [],
    }
    cleanup = {
        "namespaces": [], "namespaces_removed": False,
        "object_directories_removed": False,
        "runtime_probe_directory": str(probe_directory),
        "runtime_probe_directory_removed": False,
    }
    envelope["cleanup"] = cleanup
    endpoints = EngineEndpoints()
    adapter_factory, catalog_factory, fault_injector = production.asset_failure_factories(
        engine, endpoints,
    )
    probe_control = adapter_factory("jsons3_asset_runtime_probe", probe_directory)
    adapter = getattr(probe_control, "adapter", None)
    runtime, container, host = _database_runtime_evidence(adapter, engine, endpoints)
    if engine == "opengauss":
        endpoint = {"host": endpoints.opengauss_host, "port": endpoints.opengauss_port}
    else:
        endpoint = {"host": endpoints.clickhouse_host, "port": endpoints.clickhouse_port}
    envelope["runtime"] = {
        "operation": "asset-failures", "engine": engine, "layout": "asset_ref",
        "endpoint": endpoint, "container": _json_value(container),
        "engine_runtime": _json_value(runtime), "host": _json_value(host),
    }
    _remove_asset_directory(probe_directory)
    cleanup["runtime_probe_directory_removed"] = True

    generated_cases = set()
    generated_namespaces = set()

    def namespace_factory(case):
        """为每个固定 case 生成一次独占且受长度限制的 namespace。"""
        name = getattr(case, "name", None)
        if name not in ASSET_FAILURE_CASES or name in generated_cases:
            raise ValueError("asset failure namespace case is invalid or duplicated")
        while True:
            namespace = f"jsons3_af_{name}_{uuid.uuid4().hex[:10]}"
            if namespace not in generated_namespaces:
                break
        generated_cases.add(name)
        generated_namespaces.add(namespace)
        return namespace

    child_root = output / "child"
    run_asset_failures.run_failure_catalog(
        child_root,
        adapter_factory=adapter_factory,
        catalog_factory=catalog_factory,
        fault_injector=fault_injector,
        namespace_factory=namespace_factory,
    )
    child = _read_child(output, child_root / "run-manifest.json")
    evidence = _gate_asset_failures(child, output, engine)
    namespaces = list(evidence["namespaces"])
    envelope["namespace_policy"]["namespaces"] = namespaces
    cleanup.update({
        "namespaces": namespaces,
        "namespaces_removed": evidence["cleanup"]["namespaces_removed"],
        "object_directories_removed": evidence["cleanup"]["object_directories_removed"],
    })
    envelope["asset_failures"] = evidence
    confirmed = _read_child(output, child_root / "run-manifest.json")
    if confirmed["_identity"] != child["_identity"]:
        raise RuntimeError("asset failure child manifest changed after gate")
    envelope["child"] = _child_evidence(confirmed)


def _run_part_states(arguments, envelope):
    """执行固定 ClickHouse part-state control，并门禁 child 证据。"""
    output = arguments.output.resolve()
    layout = arguments.layout
    formal = load_formal_input(arguments.input.resolve())
    _record_formal_evidence(envelope, formal)
    namespace = f"jsons3_parts_{layout}_{uuid.uuid4().hex[:10]}"
    envelope["namespace_policy"] = {
        "strategy": "unique-random-suffix", "prefix": f"jsons3_parts_{layout}_",
        "namespace": namespace, "reuse": False,
    }
    asset_root = output / "assets" if layout == "asset_ref" else None
    child_root = output / "child"
    endpoints = EngineEndpoints()
    adapter = create_adapter("clickhouse", layout, namespace, formal, asset_root, endpoints)
    database = getattr(adapter, "database", None)
    if not isinstance(database, str) or not database:
        raise RuntimeError("part-state adapter database identity is invalid")
    runtime, container, host = _database_runtime_evidence(adapter, "clickhouse", endpoints)
    blocks, query_cases = part_state_inputs(formal)
    run_part_states(adapter, blocks, query_cases, child_root, samples_per_query=30)
    child = _read_child(output, child_root / "run-manifest.json")
    _gate_part_states(child, formal, layout, database)
    cleanup = _json_value(child["_manifest"]["cleanup"])
    cleanup.update({
        "asset_directory_removed": layout != "asset_ref",
        "asset_directory_applicable": layout == "asset_ref",
    })
    envelope["cleanup"] = cleanup
    if asset_root is not None:
        _remove_asset_directory(asset_root)
        cleanup["asset_directory_removed"] = True
    confirmed = _read_child(output, child_root / "run-manifest.json")
    if confirmed["_identity"] != child["_identity"]:
        raise RuntimeError("part-state child manifest changed after gate")
    envelope.update({
        "runtime": {
            "operation": "part-states", "engine": "clickhouse", "layout": layout,
            "endpoint": {"host": endpoints.clickhouse_host, "port": endpoints.clickhouse_port},
            "container": _json_value(container), "engine_runtime": _json_value(runtime),
            "host": _json_value(host),
        },
        "child": _child_evidence(confirmed),
        "cleanup": cleanup,
    })


def _move_new(source, destination):
    """将一个 staging artifact 移至不存在的最终路径。"""
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if not source.exists():
        raise FileNotFoundError(f"generated artifact is missing: {source}")
    source.replace(destination)


def _gate_generation_child(child, formal):
    """核对最终 generation child 与正式 loader 已验证的同一 bytes 身份。"""
    manifest = child["_manifest"]
    if (
        manifest.get("format") != "agent-trace-json-storage-stage3-generation"
        or not _is_int(manifest.get("format_version"))
        or manifest["format_version"] != 1
        or manifest.get("status") != "complete"
    ):
        raise RuntimeError("generation child format or status mismatch")
    expected = formal.identity.get("generation_manifest")
    if not isinstance(expected, Mapping) or dict(expected) != child["_identity"]:
        raise RuntimeError("generation child identity changed after formal validation")


def _run_generate(arguments, envelope):
    """在 staging 生成冻结输入，最终复验后完成 envelope。"""
    output = arguments.output.resolve()
    source = arguments.source.resolve()
    staging = output / ".generating"
    build_truth(source, staging, SEED)
    for name in ("payloads", "events.jsonl", "truth.json", "generation-manifest.json"):
        _move_new(staging / name, output / name)
    formal = load_formal_input(output)
    _record_formal_evidence(envelope, formal)
    staging.rmdir()
    child = _read_child(output, output / "generation-manifest.json")
    _gate_generation_child(child, formal)
    envelope.update({
        "runtime": {"operation": "generate-input", "source": str(source)},
        "child": _child_evidence(child),
        "cleanup": {"staging_removed": True, "staging_exists": False},
    })


def _run_candidate(arguments, envelope):
    """执行固定 ClickHouse asset_ref candidate 并门禁 child 证据。"""
    output = arguments.output.resolve()
    formal = load_formal_input(arguments.input.resolve())
    _record_formal_evidence(envelope, formal)
    namespace = f"jsons3_candidate_{uuid.uuid4().hex[:10]}"
    envelope["namespace_policy"] = {
        "strategy": "unique-random-suffix", "prefix": "jsons3_candidate_",
        "namespace": namespace, "reuse": False,
    }
    assets_root = output / "assets"
    child_root = output / "child"
    endpoints = EngineEndpoints()
    adapter = create_adapter("clickhouse", "asset_ref", namespace, formal, assets_root, endpoints)
    config = candidate_config(formal, child_root, assets_root, tuple(envelope["command"]))
    run_layout(adapter, formal.truth, config)
    child = _read_child(output, child_root / "run-manifest.json")
    _gate_candidate(child, formal, namespace)
    confirmed = _read_child(output, child_root / "run-manifest.json")
    if confirmed["_identity"] != child["_identity"]:
        raise RuntimeError("child manifest changed after gate")
    manifest = confirmed["_manifest"]
    envelope.update({
        "runtime": {
            "operation": "candidate", "engine": "clickhouse", "layout": "asset_ref",
            "endpoint": {"host": endpoints.clickhouse_host, "port": endpoints.clickhouse_port},
            "container": _json_value(manifest["container"]),
            "engine_runtime": _json_value(manifest["engine_runtime"]),
            "host": _json_value(manifest["host"]),
        },
        "child": _child_evidence(confirmed),
        "cleanup": _json_value(manifest["cleanup"]),
    })


def _failed_child(output, operation):
    """失败时尽力记录已存在 child，不掩盖原始异常。"""
    relative = "generation-manifest.json" if operation == "generate-input" else "child/run-manifest.json"
    path = output / relative
    try:
        if not path.is_file():
            return None, None
        content = path.read_bytes()
    except (OSError, RuntimeError) as error:
        return None, error
    evidence = {"path": relative, "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest()}
    try:
        manifest = _load_json_object(content, "child manifest")
    except ValueError as error:
        return evidence, error
    for key in ("format", "format_version", "status", "run_id"):
        if key in manifest:
            evidence[key] = manifest[key]
    return evidence, None


def main(argv=None):
    """执行一个 production operation；失败发布 envelope 后重新抛出原异常。"""
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    arguments = build_parser().parse_args(actual_argv)
    output = arguments.output.resolve()
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.mkdir(parents=True)
    command = [sys.executable, str(SCRIPT_PATH), *actual_argv]
    envelope = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "run_id": f"jsons3-production-{arguments.operation}-{uuid.uuid4().hex[:10]}",
        "status": "running",
        "operation": arguments.operation,
        "command": command,
        "namespace_policy": {"strategy": "not-applicable", "reuse": False},
        "code": _code_evidence(arguments.operation),
        "cleanup": {},
    }
    manifest_path = output / "run-manifest.json"
    write_manifest_atomic(manifest_path, envelope)
    try:
        if arguments.operation == "generate-input":
            _run_generate(arguments, envelope)
        elif arguments.operation == "candidate":
            _run_candidate(arguments, envelope)
        elif arguments.operation == "part-states":
            _run_part_states(arguments, envelope)
        elif arguments.operation == "interference":
            _run_interference(arguments, envelope)
        elif arguments.operation == "asset-failures":
            _run_asset_failures(arguments, envelope)
        else:
            raise RuntimeError(f"unsupported production operation: {arguments.operation}")
        envelope["status"] = "complete"
        write_manifest_atomic(manifest_path, envelope)
    except Exception as error:
        envelope["status"] = "failed"
        envelope["error"] = {"type": type(error).__name__, "message": str(error)}
        child, child_error = _failed_child(output, arguments.operation)
        if child is not None:
            envelope["child"] = child
        if arguments.operation == "interference":
            namespaces = _interference_partial_namespaces(output)
            if namespaces:
                envelope["namespace_policy"]["namespaces"] = namespaces
                envelope["cleanup"]["namespaces"] = namespaces
        if arguments.operation == "asset-failures":
            namespaces = _asset_failure_partial_namespaces(output)
            if namespaces:
                envelope["namespace_policy"]["namespaces"] = namespaces
                envelope["cleanup"]["namespaces"] = namespaces
        if child_error is not None:
            error.add_note(
                f"child snapshot failed: {type(child_error).__name__}: {child_error}"
            )
        if arguments.operation == "generate-input":
            staging = output / ".generating"
            envelope["cleanup"] = {
                "staging_removed": False,
                "staging_exists": staging.exists(),
            }
        try:
            write_manifest_atomic(manifest_path, envelope)
        except Exception as publication_error:
            error.add_note(
                "failed envelope publication failed: "
                f"{type(publication_error).__name__}: {publication_error}"
            )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
