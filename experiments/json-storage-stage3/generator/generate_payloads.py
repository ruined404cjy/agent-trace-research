#!/usr/bin/env python3
"""生成阶段三长 payload 冻结输入与 truth。"""

import base64
import hashlib
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path


SEED = 20260907
MAIN_PROFILES = {
    "text_64k": {"count": 40, "content_length": 65_536},
    "text_512k": {"count": 40, "content_length": 524_288},
    "text_2m": {"count": 40, "content_length": 2_097_152},
    "entropy_512k": {"count": 40, "content_length": 524_288},
}
CONTROL_PROFILES = {
    "text_2m": {"count": 40, "content_length": 2_097_152},
    "text_64k": {"count": 1_280, "content_length": 65_536},
}
UNICODE_BOUNDARY_LENGTH = 1_024
EXPECTED_SOURCE_MANIFEST = {
    "bytes": 2_397,
    "sha256": "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437",
}
EXPECTED_SOURCE_ARTIFACTS = {
    "dataset.jsonl": {
        "bytes": 302_518_948,
        "sha256": "8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405",
    },
    "truth-manifest.json": {
        "bytes": 18_073_179,
        "sha256": "b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04",
    },
}
EXPECTED_RECORD_COUNT = 48_534
BLOCK_SIZE = 256
EXPECTED_BLOCK_COUNT = 190
PROJECT_ID = "Leoxx/whowhen_pro"
QUERY_START_TIME = "2030-01-01T00:00:00.000Z"
QUERY_END_TIME = "2030-01-01T00:52:08.500Z"
QUERY_PAGE_SIZE = 256
EXPECTED_QUERY_ROW_COUNT = 27_561


def _json_payload(profile, event_id, content):
    """以稳定字段顺序组装只含安全 ASCII content 的 JSON bytes。"""
    prefix = b'{"content":"'
    suffix = (
        b'","event_id":'
        + json.dumps(event_id, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b',"profile":'
        + json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        + b"}"
    )
    return prefix + content + suffix


def _text_content(length, seed, event_id):
    """生成固定词表结构的可压缩 ASCII 内容。"""
    marker = hashlib.sha256(f"{seed}:{event_id}".encode("utf-8")).hexdigest().encode("ascii")
    phrase = b" agent trace tool result observation reasoning step "
    block = marker + phrase
    return (block * (length // len(block) + 1))[:length]


def _entropy_content(length, seed, event_id):
    """使用 SHA-256 counter 扩展生成高熵 URL-safe ASCII 内容。"""
    content = bytearray()
    counter = 0
    identity = f"{seed}:{event_id}:".encode("utf-8")
    while len(content) < length:
        digest = hashlib.sha256(identity + str(counter).encode("ascii")).digest()
        content.extend(base64.urlsafe_b64encode(digest).rstrip(b"="))
        counter += 1
    return bytes(content[:length])


def _unicode_boundary_payload(event_id, seed):
    """生成第 200 个 code point 为多字节字符的有效 JSON 文本。"""
    marker = hashlib.sha256(f"{seed}:{event_id}".encode("utf-8")).hexdigest().encode("ascii")
    prefix = b'"' + b"a" * 198 + "雪".encode("utf-8") + marker
    padding_length = UNICODE_BOUNDARY_LENGTH - len(prefix) - 1
    if padding_length < 0:
        raise ValueError("unicode boundary payload length is too small")
    return prefix + b"u" * padding_length + b'"'


def generate_payload_bytes(profile: str, event_id: str, seed: int) -> bytes:
    """按 profile、事件身份和 seed 生成精确长度的 JSON UTF-8 bytes。"""
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if profile == "unicode_boundary":
        return _unicode_boundary_payload(event_id, seed)
    try:
        target_length = MAIN_PROFILES[profile]["content_length"]
    except (KeyError, TypeError):
        raise ValueError(f"unknown payload profile: {profile}") from None

    empty_payload = _json_payload(profile, event_id, b"")
    content_length = target_length - len(empty_payload)
    if content_length < 0:
        raise ValueError(f"payload metadata exceeds target length: {profile}")
    if profile == "entropy_512k":
        content = _entropy_content(content_length, seed, event_id)
    else:
        content = _text_content(content_length, seed, event_id)
    return _json_payload(profile, event_id, content)


def _artifact_identity(path):
    """返回文件的实际 bytes 和 SHA-256。"""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return {"bytes": size, "sha256": digest.hexdigest()}


def _canonical_bytes(value):
    """返回稳定、保留 Unicode 的 canonical JSON bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _bytes_identity(content):
    """返回内存 bytes 的长度和 SHA-256。"""
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def _write_atomically(path, content):
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


def _read_json_object(path, name):
    """读取 JSON object，并把格式错误转换为稳定输入错误。"""
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {name}") from error
    if not isinstance(value, dict):
        raise ValueError(f"invalid {name}")
    return value


def _read_frozen_source(source_dir):
    """验证阶段二冻结来源身份，并返回生成所需的普通列。"""
    source_dir = Path(source_dir)
    manifest_path = source_dir / "run-manifest.json"
    try:
        manifest_identity = _artifact_identity(manifest_path)
    except OSError as error:
        raise ValueError("source manifest identity mismatch") from error
    if manifest_identity != EXPECTED_SOURCE_MANIFEST:
        raise ValueError("source manifest identity mismatch")
    manifest = _read_json_object(manifest_path, "source run-manifest.json")
    if manifest.get("status") != "complete":
        raise ValueError("source manifest status must be complete")
    if manifest.get("artifacts") != EXPECTED_SOURCE_ARTIFACTS:
        raise ValueError("source artifact declaration mismatch")

    actual_artifacts = {}
    for name, expected in EXPECTED_SOURCE_ARTIFACTS.items():
        try:
            actual_artifacts[name] = _artifact_identity(source_dir / name)
        except OSError as error:
            raise ValueError(f"source artifact identity mismatch: {name}") from error
        if actual_artifacts[name] != expected:
            raise ValueError(f"source artifact identity mismatch: {name}")

    rows = []
    event_ids = set()
    dataset_path = source_dir / "dataset.jsonl"
    try:
        with dataset_path.open("rb") as dataset_file:
            for line_number, line in enumerate(dataset_file, start=1):
                if not line.strip():
                    raise ValueError(f"invalid source row: {line_number}")
                row = json.loads(line)
                projected = {
                    "ingest_seq": row["ingest_seq"],
                    "event_id": row["event_id"],
                    "trace_id": row["trace_id"],
                    "project_id": row["project_id"],
                    "start_time": row["start_time"],
                }
                if (
                    projected["ingest_seq"] != line_number - 1
                    or any(
                        not isinstance(projected[field], str) or not projected[field]
                        for field in ("event_id", "trace_id", "project_id", "start_time")
                    )
                    or projected["event_id"] in event_ids
                ):
                    raise ValueError(f"invalid source row: {line_number}")
                event_ids.add(projected["event_id"])
                rows.append(projected)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("invalid source dataset.jsonl") from error

    expected_watermarks = list(range(BLOCK_SIZE, EXPECTED_RECORD_COUNT, BLOCK_SIZE)) + [
        EXPECTED_RECORD_COUNT
    ]
    if len(rows) != EXPECTED_RECORD_COUNT:
        raise ValueError("source record count mismatch")
    for field, expected in (
        ("record_count", EXPECTED_RECORD_COUNT),
        ("block_size", BLOCK_SIZE),
        ("block_count", EXPECTED_BLOCK_COUNT),
        ("watermarks", expected_watermarks),
    ):
        if manifest.get(field) != expected:
            raise ValueError(f"source manifest {field} mismatch")

    source_truth = _read_json_object(source_dir / "truth-manifest.json", "source truth-manifest.json")
    truth_records = source_truth.get("records")
    if (
        source_truth.get("record_count") != EXPECTED_RECORD_COUNT
        or source_truth.get("block_size") != BLOCK_SIZE
        or source_truth.get("block_count") != EXPECTED_BLOCK_COUNT
        or source_truth.get("watermarks") != expected_watermarks
        or not isinstance(truth_records, list)
        or [record.get("event_id") for record in truth_records]
        != [row["event_id"] for row in rows]
    ):
        raise ValueError("source dataset/truth identity mismatch")

    source = {
        "manifest": manifest_identity,
        "artifacts": actual_artifacts,
        "input": manifest.get("input"),
        "record_count": EXPECTED_RECORD_COUNT,
        "block_size": BLOCK_SIZE,
        "block_count": EXPECTED_BLOCK_COUNT,
        "watermarks": expected_watermarks,
    }
    return rows, source


def _representative_trace_ids(rows):
    """按 Span 数量 nearest-rank 与 trace_id 并列顺序选择三个 Trace。"""
    trace_counts = Counter(row["trace_id"] for row in rows)
    ordered = sorted(trace_counts.items(), key=lambda item: (item[1], item[0]))
    selected = {}
    for label, percentile in (("p25", 25), ("p50", 50), ("p95", 95)):
        index = math.ceil(len(ordered) * percentile / 100) - 1
        trace_id, span_count = ordered[index]
        selected[label] = {"trace_id": trace_id, "span_count": span_count}
    return selected


def _ranked_rows(rows, seed, label, excluded):
    """按 seed、cohort 与事件身份稳定排序未占用记录。"""
    available = (row for row in rows if row["event_id"] not in excluded)
    return sorted(
        available,
        key=lambda row: (
            hashlib.sha256(f"{seed}:{label}:{row['event_id']}".encode("utf-8")).digest(),
            row["ingest_seq"],
        ),
    )


def _profile_sequence(specifications):
    """交错返回 profile，使各类 payload 分散到确定性选中记录。"""
    maximum = max(specification["count"] for specification in specifications.values())
    return [
        profile
        for index in range(maximum)
        for profile, specification in specifications.items()
        if index < specification["count"]
    ]


def _assign_cohort(rows, seed, cohort, specifications, excluded, reserved=()):
    """为一个 cohort 选择互异事件并返回事件到 profile 的映射。"""
    profiles = _profile_sequence(specifications)
    reserved_rows = []
    reserved_ids = set()
    for row in reserved:
        if row["event_id"] not in excluded and row["event_id"] not in reserved_ids:
            reserved_rows.append(row)
            reserved_ids.add(row["event_id"])
    ranked = [
        row
        for row in _ranked_rows(rows, seed, cohort, excluded)
        if row["event_id"] not in reserved_ids
    ]
    selected = (reserved_rows + ranked)[: len(profiles)]
    if len(selected) != len(profiles):
        raise ValueError(f"insufficient source records for cohort: {cohort}")
    assignments = {row["event_id"]: profile for row, profile in zip(selected, profiles)}
    excluded.update(assignments)
    return assignments


def _cohort_summary(payloads, performance, variants=None):
    """返回 cohort 的计数、profile 和原始 bytes 汇总。"""
    summary = {
        "performance": performance,
        "payload_count": len(payloads),
        "profile_counts": dict(Counter(payload["profile"] for payload in payloads)),
        "raw_payload_bytes": sum(payload["content_length"] for payload in payloads),
    }
    if variants is not None:
        summary["variants"] = variants
    return summary


def _payload_record(row, cohort, profile, payload_bytes):
    """构造包含完整身份与内容元数据的 payload truth 记录。"""
    digest = hashlib.sha256(payload_bytes).hexdigest()
    return {
        "event_id": row["event_id"],
        "trace_id": row["trace_id"],
        "project_id": row["project_id"],
        "start_time": row["start_time"],
        "cohort": cohort,
        "profile": profile,
        "content_type": "application/json",
        "encoding": "utf-8",
        "content_length": len(payload_bytes),
        "preview": payload_bytes.decode("utf-8")[:200],
        "sha256": digest,
        "payload_path": f"payloads/{digest}.json",
    }


def _event_record(row, payload):
    """构造保留基础顺序的事件输入；缺失 payload 字段使用 JSON null。"""
    result = dict(row)
    for field in (
        "cohort",
        "profile",
        "content_type",
        "encoding",
        "content_length",
        "preview",
        "sha256",
        "payload_path",
    ):
        result[field] = payload[field] if payload is not None else None
    return result


def build_truth(source_dir: Path, output_dir: Path, seed: int) -> dict[str, object]:
    """从阶段二冻结输入生成事件、payload、truth 和最终 generation manifest。"""
    if seed != SEED:
        raise ValueError(f"seed must be {SEED}")
    rows, source = _read_frozen_source(source_dir)
    query_row_count = sum(
        row["project_id"] == PROJECT_ID
        and QUERY_START_TIME <= row["start_time"] < QUERY_END_TIME
        for row in rows
    )
    if query_row_count != EXPECTED_QUERY_ROW_COUNT:
        raise ValueError("source query window row count mismatch")
    query_window = {
        "project_id": PROJECT_ID,
        "start_time": QUERY_START_TIME,
        "end_time": QUERY_END_TIME,
        "page_size": QUERY_PAGE_SIZE,
        "row_count": query_row_count,
    }
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    payload_dir = output_dir / "payloads"
    payload_dir.mkdir()

    representative = _representative_trace_ids(rows)
    row_by_event = {row["event_id"]: row for row in rows}
    reserved = [
        next(row for row in rows if row["trace_id"] == selection["trace_id"])
        for selection in representative.values()
    ]
    excluded = set()
    assignments = {
        "main": _assign_cohort(
            rows,
            seed,
            "main",
            MAIN_PROFILES,
            excluded,
            reserved=reserved,
        ),
        "equal_total_control": _assign_cohort(
            rows,
            seed,
            "equal_total_control",
            CONTROL_PROFILES,
            excluded,
        ),
        "correctness_only": _assign_cohort(
            rows,
            seed,
            "correctness_only",
            {"unicode_boundary": {"count": 1, "content_length": UNICODE_BOUNDARY_LENGTH}},
            excluded,
        ),
    }

    payload_artifacts = {}
    payloads_by_cohort = {name: [] for name in assignments}
    payload_by_event = {}
    for cohort, cohort_assignments in assignments.items():
        for event_id, profile in cohort_assignments.items():
            row = row_by_event[event_id]
            payload_bytes = generate_payload_bytes(profile, event_id, seed)
            payload = _payload_record(row, cohort, profile, payload_bytes)
            payload_path = output_dir / payload["payload_path"]
            _write_atomically(payload_path, payload_bytes)
            payload_artifacts[payload["payload_path"]] = _bytes_identity(payload_bytes)
            payloads_by_cohort[cohort].append(payload)
            payload_by_event[event_id] = payload

    expected_control_lengths = {
        profile: MAIN_PROFILES[profile]["content_length"] for profile in CONTROL_PROFILES
    }
    actual_control_lengths = {
        profile: specification["content_length"]
        for profile, specification in CONTROL_PROFILES.items()
    }
    if actual_control_lengths != expected_control_lengths:
        raise ValueError("control profile length mismatch")
    control_variants = {
        "few_large": {
            "profile": "text_2m",
            "payload_count": CONTROL_PROFILES["text_2m"]["count"],
            "raw_payload_bytes": (
                CONTROL_PROFILES["text_2m"]["count"]
                * CONTROL_PROFILES["text_2m"]["content_length"]
            ),
        },
        "many_medium": {
            "profile": "text_64k",
            "payload_count": CONTROL_PROFILES["text_64k"]["count"],
            "raw_payload_bytes": (
                CONTROL_PROFILES["text_64k"]["count"]
                * CONTROL_PROFILES["text_64k"]["content_length"]
            ),
        },
    }
    if len({variant["raw_payload_bytes"] for variant in control_variants.values()}) != 1:
        raise ValueError("equal-total control bytes mismatch")
    cohorts = {
        "main": _cohort_summary(payloads_by_cohort["main"], True),
        "equal_total_control": _cohort_summary(
            payloads_by_cohort["equal_total_control"], True, control_variants
        ),
        "correctness_only": _cohort_summary(payloads_by_cohort["correctness_only"], False),
    }
    all_payloads = sorted(
        payload_by_event.values(),
        key=lambda payload: row_by_event[payload["event_id"]]["ingest_seq"],
    )
    for selection in representative.values():
        trace_payloads = [
            payload for payload in all_payloads if payload["trace_id"] == selection["trace_id"]
        ]
        selection.update(
            {
                "payload_count": len(trace_payloads),
                "profile_counts": dict(Counter(payload["profile"] for payload in trace_payloads)),
                "raw_payload_bytes": sum(payload["content_length"] for payload in trace_payloads),
            }
        )

    identity_values = [
        [row["event_id"], row["trace_id"], row["project_id"], row["start_time"]]
        for row in rows
    ]
    truth = {
        "format": "agent-trace-json-storage-stage3-truth",
        "format_version": 1,
        "seed": seed,
        "source": source,
        "record_count": len(rows),
        "block_size": source["block_size"],
        "block_count": source["block_count"],
        "watermarks": source["watermarks"],
        "identity_sha256": hashlib.sha256(_canonical_bytes(identity_values)).hexdigest(),
        "query_window": query_window,
        "payloads": all_payloads,
        "cohorts": cohorts,
        "representative_traces": representative,
    }
    events_content = b"".join(
        _canonical_bytes(_event_record(row, payload_by_event.get(row["event_id"]))) + b"\n"
        for row in rows
    )
    truth_content = _canonical_bytes(truth) + b"\n"
    _write_atomically(output_dir / "events.jsonl", events_content)
    _write_atomically(output_dir / "truth.json", truth_content)
    manifest = {
        "format": "agent-trace-json-storage-stage3-generation",
        "format_version": 1,
        "status": "complete",
        "seed": seed,
        "source": source,
        "record_count": len(rows),
        "block_size": source["block_size"],
        "block_count": source["block_count"],
        "watermarks": source["watermarks"],
        "query_window": query_window,
        "cohorts": cohorts,
        "artifacts": {
            "events.jsonl": _bytes_identity(events_content),
            "truth.json": _bytes_identity(truth_content),
            "payloads": payload_artifacts,
        },
    }
    _write_atomically(output_dir / "generation-manifest.json", _canonical_bytes(manifest) + b"\n")
    return truth
