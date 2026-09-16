#!/usr/bin/env python3
"""发布阶段三冻结输入与 candidate 的不可复用生产运行 envelope。"""

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path

import assets
import common
import production
import run_layout_matrix
from common import canonical_digest
from production import EngineEndpoints, candidate_config, create_adapter, load_formal_input
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
    """构造仅包含本切片两个固定操作的命令行解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    generate = commands.add_parser("generate-input")
    generate.add_argument("--source", type=Path, required=True)
    generate.add_argument("--output", type=Path, required=True)
    candidate = commands.add_parser("candidate")
    candidate.add_argument("--input", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    return parser


def _json_value(value):
    """复制 MappingProxy 等只读容器，形成隔离的 JSON 值。"""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


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
        cleanup.get("namespace") != namespace
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
        else:
            _run_candidate(arguments, envelope)
        envelope["status"] = "complete"
        write_manifest_atomic(manifest_path, envelope)
    except Exception as error:
        envelope["status"] = "failed"
        envelope["error"] = {"type": type(error).__name__, "message": str(error)}
        child, child_error = _failed_child(output, arguments.operation)
        if child is not None:
            envelope["child"] = child
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
