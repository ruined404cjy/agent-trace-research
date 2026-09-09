#!/usr/bin/env python3
"""观察 ClickHouse Native JSON 路径、Sidecar、merge 与 FINAL 机制。"""

import argparse
import hashlib
import importlib.util
import json
import sys
import time
import types
import uuid
from pathlib import Path

from clickhouse_four_layout import (
    ClickHouseFourLayoutAdapter,
    merge_fidelity,
    validate_identifier,
)
from supplement_common import (
    QUERY_IDS, canonical_bytes, file_identity, write_failed_manifest,
    write_manifest_last,
)


LAYOUTS = (
    "ch_native_auto32_none",
    "ch_native_auto32_sparse",
    "ch_native_auto32_full",
    "ch_native_hinted32_sparse",
)
STABLE_COLUMNS = (
    "ingest_seq UInt64", "event_id String", "trace_id String", "span_id String",
    "parent_span_id String", "project_id String", "start_time DateTime64(3, 'UTC')",
    "end_time DateTime64(3, 'UTC')", "duration_ms Int64", "span_type String",
    "framework String", "level String",
)
TABLE_SETTINGS = (
    "min_bytes_for_wide_part=0,min_rows_for_wide_part=0,"
    "object_shared_data_serialization_version='advanced',"
    "object_shared_data_serialization_version_for_zero_level_parts='map_with_buckets'"
)
HINT_PATHS = ("gen_ai.operation.name", "failure.mistake_mode")
HINT_MISSING_PREFIX = "@missing:"


def _task4_runner():
    """惰性加载 Task 4 runner，保持 ClickHouse-only 导入可用。"""
    if importlib.util.find_spec("psycopg") is not None:
        import run_four_layouts
        return run_four_layouts
    stub = types.ModuleType("psycopg")
    sys.modules["psycopg"] = stub
    try:
        import run_four_layouts
        return run_four_layouts
    finally:
        sys.modules.pop("psycopg", None)


def load_inputs(input_dir, truth_dir):
    """惰性复用 Task 4 输入门禁，避免 ClickHouse-only 导入 openGauss 驱动。"""
    return _task4_runner().load_inputs(input_dir, truth_dir)


def _truth_generator():
    """加载 Task 1 生成器，用于首 block 和空表阶段的同契约 truth。"""
    path = Path(__file__).resolve().parents[1] / "generator" / "generate_supplement_truth.py"
    specification = importlib.util.spec_from_file_location("s2sup_truth_generator_mechanisms", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def mechanism_ddls(database: str) -> dict[str, str]:
    """返回预算固定为 32 的四种 Native JSON 机制表 DDL。"""
    validate_identifier(database, "database")
    definitions = {}
    for layout in LAYOUTS:
        hinted = layout == "ch_native_hinted32_sparse"
        parameters = "max_dynamic_paths=32"
        if hinted:
            parameters += ", gen_ai.operation.name String, failure.mistake_mode String"
        columns = [*STABLE_COLUMNS, f"attributes JSON({parameters})"]
        if layout.endswith("_sparse"):
            columns.append("fidelity_values Map(String,String)")
        elif layout.endswith("_full"):
            columns.append("attributes_raw String CODEC(ZSTD(3))")
        definitions[layout] = (
            f"CREATE TABLE {database}.{layout} (" + ", ".join(columns) + ") "
            "ENGINE=MergeTree ORDER BY (project_id,start_time,event_id) SETTINGS " + TABLE_SETTINGS
        )
    return definitions


def mechanism_query_sql(layout: str, query_id: str) -> str:
    """返回指定机制结构实际执行的 S01 至 S06 SQL 模板。"""
    if layout not in LAYOUTS:
        raise ValueError("invalid mechanism layout")
    if query_id not in QUERY_IDS:
        raise ValueError("invalid query id")
    statement = ClickHouseFourLayoutAdapter.query_sql("ch_native", query_id)
    if layout == "ch_native_auto32_none":
        return statement.replace(", fidelity_values", "")
    if layout == "ch_native_auto32_full":
        return statement.replace(", fidelity_values", ", attributes_raw")
    if layout == "ch_native_hinted32_sparse":
        return statement.replace(
            "attributes.gen_ai.operation.name.:String", "attributes.gen_ai.operation.name"
        ).replace(
            "attributes.failure.mistake_mode.:String", "attributes.failure.mistake_mode"
        )
    return statement


def validate_truth_contract(catalog, truth, digests=None):
    """要求 catalog/truth 精确覆盖 S01 至 S06，并生成或核对摘要。"""
    expected = set(QUERY_IDS)
    parameters = catalog.get("parameters") if isinstance(catalog, dict) else None
    results = truth.get("results") if isinstance(truth, dict) else None
    if not isinstance(parameters, dict) or set(parameters) != expected:
        raise ValueError("catalog must contain exactly S01-S06")
    if not isinstance(results, dict) or set(results) != expected:
        raise ValueError("truth must contain exactly S01-S06")
    actual_digests = {
        query_id: hashlib.sha256(canonical_bytes(results[query_id])).hexdigest()
        for query_id in QUERY_IDS
    }
    if digests is not None:
        if not isinstance(digests, dict) or set(digests) != expected or any(
            not isinstance(value, str) or not value for value in digests.values()
        ):
            raise ValueError("truth digest set must contain non-empty S01-S06 digests")
        if digests != actual_digests:
            raise ValueError("truth digest mismatch")
    return {"digests": actual_digests, "parameters": parameters, "results": results}


def _path_sets(state):
    """校验并返回一次阶段观察的 dynamic/shared 路径集合。"""
    if not isinstance(state, dict):
        raise ValueError("path state must be an object")
    dynamic = state.get("dynamic_paths")
    shared = state.get("shared_paths")
    if not isinstance(dynamic, list) or not isinstance(shared, list):
        raise ValueError("path state must contain path lists")
    if any(not isinstance(path, str) or not path for path in dynamic + shared):
        raise ValueError("paths must be non-empty strings")
    return set(dynamic), set(shared)


def _logical_paths_are_valid(state, dynamic, shared, expected):
    """以 JSONAllPathsWithTypes 为逻辑全集，并要求物理位置互斥。"""
    hinted = state.get("hinted_paths", [])
    if not isinstance(hinted, list) or any(not isinstance(path, str) or not path for path in hinted):
        raise ValueError("hinted_paths must be a path list")
    hinted = set(hinted)
    path_types = state.get("path_types")
    if path_types is not None:
        if not isinstance(path_types, dict) or any(
            not isinstance(path, str) or not path for path in path_types
        ):
            raise ValueError("path_types must be keyed by non-empty paths")
        logical = set(path_types)
    else:
        logical = dynamic.union(shared, hinted)
    return (
        not dynamic.intersection(shared)
        and not hinted.intersection(dynamic.union(shared))
        and logical == expected
    )


def validate_path_transition(before: dict, after: dict, expected: set[str]) -> dict:
    """校验 merge 前后逻辑路径全集与固定 truth 摘要，并报告物理迁移。"""
    if not isinstance(expected, set) or any(not isinstance(path, str) or not path for path in expected):
        raise ValueError("expected must be a set of non-empty paths")
    before_dynamic, before_shared = _path_sets(before)
    after_dynamic, after_shared = _path_sets(after)
    before_inventory_valid = _logical_paths_are_valid(before, before_dynamic, before_shared, expected)
    after_inventory_valid = _logical_paths_are_valid(after, after_dynamic, after_shared, expected)
    before_digest = before.get("query_results_sha256")
    after_digest = after.get("query_results_sha256")
    if not isinstance(before_digest, str) or not before_digest or not isinstance(after_digest, str) or not after_digest:
        raise ValueError("query_results_sha256 must be a non-empty string on both stages")
    before_analysis = before.get("analysis_results_sha256")
    after_analysis = after.get("analysis_results_sha256")
    if not isinstance(before_analysis, str) or not before_analysis or not isinstance(after_analysis, str) or not after_analysis:
        raise ValueError("analysis_results_sha256 must be a non-empty string on both stages")
    query_results_preserved = before_digest == after_digest
    before_hinted = set(before.get("hinted_paths", []))
    after_hinted = set(after.get("hinted_paths", []))
    before_logical = set(before.get("path_types", {})) or before_dynamic.union(before_shared, before_hinted)
    after_logical = set(after.get("path_types", {})) or after_dynamic.union(after_shared, after_hinted)
    before_documents = before.get("native_documents")
    after_documents = after.get("native_documents")
    if before_documents is None and after_documents is None:
        logical_paths_preserved = before_inventory_valid and after_inventory_valid
        before_document_digest = None
        after_document_digest = None
    elif not isinstance(before_documents, dict) or not isinstance(after_documents, dict):
        raise ValueError("native_documents must exist on both transition stages")
    else:
        before_document_digest = before_documents.get("actual_sha256")
        after_document_digest = after_documents.get("actual_sha256")
        if not isinstance(before_document_digest, str) or not before_document_digest \
                or not isinstance(after_document_digest, str) or not after_document_digest:
            raise ValueError("native document digest must be non-empty on both stages")
        logical_paths_preserved = (
            before_documents.get("ordinary_content_ok") is True
            and after_documents.get("ordinary_content_ok") is True
        )
    return {
        "entered_dynamic": sorted(after_dynamic - before_dynamic),
        "entered_shared": sorted(after_shared - before_shared),
        "exited_dynamic": sorted(before_dynamic - after_dynamic),
        "exited_shared": sorted(before_shared - after_shared),
        "inventory_paths_preserved": before_inventory_valid and after_inventory_valid,
        "logical_paths_preserved": logical_paths_preserved,
        "analysis_results_preserved": before_analysis == after_analysis,
        "query_results_preserved": query_results_preserved,
        "before_query_results_sha256": before_digest,
        "after_query_results_sha256": after_digest,
        "before_native_documents_sha256": before_document_digest,
        "after_native_documents_sha256": after_document_digest,
        "before_unclassified_paths": sorted(
            before_logical - before_dynamic - before_shared - before_hinted
        ),
        "after_unclassified_paths": sorted(
            after_logical - after_dynamic - after_shared - after_hinted
        ),
    }


_MISSING = object()


def _loss_counts(value):
    """统计仅由 JSON null、空对象和空数组构成的值；其他内容返回 None。"""
    if value is None:
        return {"json_null": 1, "empty_object": 0, "empty_array": 0}
    if value == {}:
        return {"json_null": 0, "empty_object": 1, "empty_array": 0}
    if value == []:
        return {"json_null": 0, "empty_object": 0, "empty_array": 1}
    if isinstance(value, dict) and value:
        nested = [_loss_counts(item) for item in value.values()]
    elif isinstance(value, list) and value:
        nested = [_loss_counts(item) for item in value]
    else:
        return None
    if any(item is None for item in nested):
        return None
    return {
        name: sum(item[name] for item in nested)
        for name in ("json_null", "empty_object", "empty_array")
    }


def _compare_native_value(expected, actual, losses, reasons):
    """递归验证差异是否完全由 Native JSON 的三类已知信息缺失组成。"""
    if actual is not _MISSING and canonical_bytes(expected) == canonical_bytes(actual):
        return
    allowed = _loss_counts(expected)
    if allowed is not None and (actual is _MISSING or actual == {}):
        for name, count in allowed.items():
            losses[name] += count
        return
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(actual) - set(expected):
            reasons.append("unexpected_key")
        for key, value in expected.items():
            _compare_native_value(value, actual.get(key, _MISSING), losses, reasons)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            reasons.append("array_length_mismatch")
            return
        for expected_item, actual_item in zip(expected, actual):
            _compare_native_value(expected_item, actual_item, losses, reasons)
        return
    reasons.append("ordinary_value_mismatch")


def diagnose_native_query_difference(query_id, expected, actual):
    """诊断 S05/S06 文档差异，并拒绝三类已知缺失以外的内容变化。"""
    if query_id not in {"S05", "S06"}:
        raise ValueError("native document diagnostic only supports S05/S06")
    losses = {"json_null": 0, "empty_object": 0, "empty_array": 0}
    reasons = []
    expected_rows = expected
    actual_rows = actual
    if query_id == "S06":
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            reasons.append("result_shape_mismatch")
            expected_rows, actual_rows = [], []
        else:
            for key in ("identity_sha256", "page_row_count", "row_count"):
                if expected.get(key) != actual.get(key):
                    reasons.append(f"{key}_mismatch")
            expected_rows = expected.get("rows")
            actual_rows = actual.get("rows")
    if not isinstance(expected_rows, list) or not isinstance(actual_rows, list):
        reasons.append("result_shape_mismatch")
    elif len(expected_rows) != len(actual_rows):
        reasons.append("document_row_count_mismatch")
    else:
        for expected_row, actual_row in zip(expected_rows, actual_rows):
            if not isinstance(expected_row, list) or not isinstance(actual_row, list) \
                    or len(expected_row) != 3 or len(actual_row) != 3:
                reasons.append("document_row_shape_mismatch")
                continue
            if expected_row[:2] != actual_row[:2]:
                reasons.append("document_identity_mismatch")
                continue
            _compare_native_value(expected_row[2], actual_row[2], losses, reasons)
    reasons = sorted(set(reasons))
    return {
        "actual_sha256": hashlib.sha256(canonical_bytes(actual)).hexdigest(),
        "known_loss_only": not reasons,
        "loss_count": sum(losses.values()),
        "losses": losses,
        "reasons": reasons,
        "truth_sha256": hashlib.sha256(canonical_bytes(expected)).hexdigest(),
    }


def _path_exists(value, dotted_path):
    """返回对象是否显式包含点分路径。"""
    for part in dotted_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def hint_presence_markers(attributes):
    """为两条声明路径生成缺失标记；auto/hinted sparse 使用相同输入。"""
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    return {
        HINT_MISSING_PREFIX + path: "null"
        for path in HINT_PATHS if not _path_exists(attributes, path)
    }


def _remove_path(value, dotted_path):
    """移除声明路径注入的默认值，并清理因此产生的空父对象。"""
    parts = dotted_path.split(".")
    parents = []
    current = value
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        parents.append((current, part))
        current = current[part]
    if not isinstance(current, dict):
        return
    current.pop(parts[-1], None)
    for parent, part in reversed(parents):
        if parent.get(part) == {}:
            parent.pop(part)


def restore_sparse_row(row):
    """消费 sparse 缺失标记并恢复 typed-path 缺失状态。"""
    restored = dict(row)
    attributes = restored.get("attributes")
    if isinstance(attributes, str):
        attributes = json.loads(attributes)
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    attributes = json.loads(canonical_bytes(attributes))
    values = restored.get("fidelity_values", {})
    if not isinstance(values, dict):
        raise ValueError("fidelity_values must be an object")
    values = dict(values)
    for key in list(values):
        if key.startswith(HINT_MISSING_PREFIX):
            path = key.removeprefix(HINT_MISSING_PREFIX)
            if path not in HINT_PATHS or values[key] != "null":
                raise ValueError("invalid hinted path presence marker")
            _remove_path(attributes, path)
            values.pop(key)
    restored["attributes"] = attributes
    restored["fidelity_values"] = values
    return restored


def native_document_observation(originals, stored_rows):
    """逐行校验完整 Native 文档，普通内容必须与冻结输入完全一致。"""
    expected = {
        row["event_id"]: row["attributes_analysis"] for row in originals
    }
    stored = {}
    for row in stored_rows:
        attributes = row.get("attributes")
        if isinstance(attributes, str):
            attributes = json.loads(attributes)
        stored[row.get("event_id")] = attributes
    reasons = []
    losses = {"json_null": 0, "empty_object": 0, "empty_array": 0}
    if len(expected) != len(originals) or len(stored) != len(stored_rows):
        reasons.append("duplicate_event_id")
    missing = sorted(set(expected) - set(stored))
    extra = sorted(set(stored) - set(expected))
    if missing:
        reasons.append("missing_event_id")
    if extra:
        reasons.append("extra_event_id")
    for event_id in sorted(set(expected).intersection(stored)):
        _compare_native_value(expected[event_id], stored[event_id], losses, reasons)
    canonical_rows = [[event_id, stored[event_id]] for event_id in sorted(stored)]
    reasons = sorted(set(reasons))
    return {
        "actual_sha256": hashlib.sha256(canonical_bytes(canonical_rows)).hexdigest(),
        "extra_event_ids": extra,
        "loss_count": sum(losses.values()),
        "losses": losses,
        "missing_event_ids": missing,
        "ordinary_content_ok": not reasons,
        "reasons": reasons,
        "row_count": len(stored_rows),
    }


def sidecar_observation(layout, originals, stored_rows):
    """恢复 Native JSON 行并记录 Sidecar 条目、字节和差异。"""
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported mechanism layout: {layout}")
    if not isinstance(originals, list) or not isinstance(stored_rows, list):
        raise ValueError("originals and stored_rows must be lists")
    try:
        expected = {row["event_id"]: row["attributes"] for row in originals}
        stored = {row["event_id"]: row for row in stored_rows}
    except (KeyError, TypeError) as error:
        raise ValueError("invalid sidecar observation rows") from error
    if len(expected) != len(originals) or len(stored) != len(stored_rows) or set(expected) != set(stored):
        raise ValueError("sidecar observation identities differ")

    sidecar_entries = 0
    sidecar_bytes = 0
    marker_entries = 0
    marker_bytes = 0
    recovered = {}
    started = time.perf_counter()
    for event_id in sorted(expected):
        row = stored[event_id]
        attributes = row.get("attributes")
        if isinstance(attributes, str):
            attributes = json.loads(attributes)
        if layout.endswith("_sparse"):
            values = row.get("fidelity_values", {})
            if not isinstance(values, dict):
                raise ValueError("fidelity_values must be an object")
            if any(not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()):
                raise ValueError("fidelity_values keys and values must be strings")
            sidecar_entries += len(values)
            sidecar_bytes += sum(
                len(key.encode("utf-8")) + len(value.encode("utf-8"))
                for key, value in values.items()
            )
            markers = {
                key: value for key, value in values.items()
                if key.startswith(HINT_MISSING_PREFIX)
            }
            marker_entries += len(markers)
            marker_bytes += sum(
                len(key.encode("utf-8")) + len(value.encode("utf-8"))
                for key, value in markers.items()
            )
            restored = restore_sparse_row({"attributes": attributes, "fidelity_values": values})
            attributes = merge_fidelity(restored["attributes"], restored["fidelity_values"])
        elif layout.endswith("_full"):
            raw = row.get("attributes_raw")
            if not isinstance(raw, str):
                raise ValueError("attributes_raw must be a string")
            parsed = json.loads(raw)
            if raw != canonical_bytes(parsed).decode("utf-8"):
                raise ValueError("attributes_raw must be canonical JSON")
            sidecar_bytes += len(raw.encode("utf-8"))
            attributes = parsed
        recovered[event_id] = attributes
    recovery_ms = (time.perf_counter() - started) * 1000
    mismatches = [
        event_id for event_id in sorted(expected)
        if canonical_bytes(recovered[event_id]) != canonical_bytes(expected[event_id])
    ]
    return {
        "mismatch_count": len(mismatches),
        "mismatch_event_ids": mismatches,
        "marker_bytes": marker_bytes,
        "marker_entries": marker_entries,
        "marker_rule": {
            "missing_prefix": HINT_MISSING_PREFIX,
            "paths": list(HINT_PATHS),
        },
        "recovery_ms": recovery_ms,
        "row_count": len(expected),
        "sidecar_bytes": sidecar_bytes,
        "sidecar_entries": sidecar_entries,
    }


def cleanup_owned_database(adapter, database, result):
    """删除本次创建的 database，并把不存在确认纳入完成门禁。"""
    validate_identifier(database, "database")
    _request(adapter, f"DROP DATABASE IF EXISTS {database} SYNC")
    remaining = _json_rows(
        adapter,
        "SELECT count() AS count FROM system.databases WHERE name={database:String} FORMAT JSONEachRow",
        parameters={"database": database},
    )
    result["cleanup_confirmed"] = not remaining or int(remaining[0]["count"]) == 0
    if not result["cleanup_confirmed"]:
        result["status"] = "failed"
        raise RuntimeError(f"database cleanup not confirmed: {database}")


def _request(adapter, statement, *, parameters=None):
    """通过 Task 3 adapter 的 HTTP 边界执行一次完整请求。"""
    connection = adapter.connect_worker()
    try:
        return adapter._request(connection, statement, parameters=parameters)
    finally:
        connection.close()


def _json_rows(adapter, statement, *, parameters=None):
    """执行 JSONEachRow 查询并复用 Task 3 响应恢复函数。"""
    return adapter._json_rows(_request(adapter, statement, parameters=parameters))


def _owned_table(table, owned_tables):
    """返回已验证的 database/layout，并要求本次运行已取得所有权。"""
    parts = table.split(".") if isinstance(table, str) else []
    if not isinstance(owned_tables, set) or table not in owned_tables:
        raise ValueError("table must be an owned mechanism table")
    if len(parts) != 2 or parts[1] not in LAYOUTS:
        raise ValueError("invalid owned mechanism table")
    validate_identifier(parts[0], "database")
    return parts


def _execute_truth_queries(adapter, table, layout, catalog, expected_results):
    """执行 S01-S06，复用 Task 3 SQL、参数和规范化恢复，并逐项比较 truth。"""
    contract = validate_truth_contract(catalog, {"results": expected_results})
    actual = {}
    matches = {}
    diagnostics = {}
    for query_id in QUERY_IDS:
        statement = mechanism_query_sql(layout, query_id).replace("{analytics}", table)
        body = _request(
            adapter, statement,
            parameters=adapter._query_parameters(query_id, contract["parameters"][query_id]),
        )
        rows = adapter._json_rows(body)
        if layout.endswith("_sparse") and query_id in {"S05", "S06"}:
            rows = [restore_sparse_row(row) for row in rows]
        if layout == "ch_native_auto32_full" and query_id in {"S05", "S06"}:
            rows = [{**row, "attributes": row["attributes_raw"]} for row in rows]
        result = adapter._normalize_result(
            query_id, rows, contract["parameters"][query_id], "ch_native"
        )
        actual[query_id] = result
        matches[query_id] = result == contract["results"][query_id]
        diagnostics[query_id] = {
            "actual_sha256": hashlib.sha256(canonical_bytes(result)).hexdigest(),
            "match": matches[query_id],
            "truth_sha256": contract["digests"][query_id],
        }
        if query_id in {"S05", "S06"}:
            diagnostics[query_id].update(
                diagnose_native_query_difference(
                    query_id, contract["results"][query_id], result
                )
            )
    analysis = {query_id: actual[query_id] for query_id in QUERY_IDS[:4]}
    return {
        "actual": actual,
        "analysis_truth_ok": all(matches[query_id] for query_id in ("S01", "S02", "S03", "S04")),
        "analysis_digest": hashlib.sha256(canonical_bytes(analysis)).hexdigest(),
        "diagnostics": diagnostics,
        "digest": hashlib.sha256(canonical_bytes(actual)).hexdigest(),
        "matches": matches,
        "truth_digest": hashlib.sha256(canonical_bytes(contract["results"])).hexdigest(),
        "truth_ok": all(matches.values()),
    }


def collect_native_stage(adapter, table, owned_tables, catalog, expected_results, originals=None):
    """采集 active part、压缩空间、路径位置/类型与固定 truth 结果。"""
    database, layout = _owned_table(table, owned_tables)
    validate_truth_contract(catalog, {"results": expected_results})
    part_row = _json_rows(
        adapter,
        "SELECT count() AS part_count,sum(rows) AS rows,"
        "sum(data_compressed_bytes) AS compressed_bytes,"
        "sum(data_uncompressed_bytes) AS uncompressed_bytes FROM system.parts "
        "WHERE active AND database={database:String} AND table={table:String} FORMAT JSONEachRow",
        parameters={"database": database, "table": layout},
    )[0]
    parts = {
        field: int(part_row.get(field) or 0)
        for field in ("part_count", "rows", "compressed_bytes", "uncompressed_bytes")
    }
    paths = _json_rows(
        adapter,
        "SELECT arraySort(arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(attributes))))) "
        "AS dynamic_paths,arraySort(arrayDistinct(arrayFlatten(groupArray("
        "JSONSharedDataPaths(attributes))))) AS shared_paths FROM " + table + " FORMAT JSONEachRow",
    )[0]
    type_rows = _json_rows(
        adapter,
        "SELECT tupleElement(item,1) AS path,arraySort(groupUniqArray(tupleElement(item,2))) "
        "AS types FROM (SELECT arrayJoin(arrayZip(mapKeys(path_types),mapValues(path_types))) "
        "AS item FROM (SELECT JSONAllPathsWithTypes(attributes) AS path_types FROM " + table
        + ")) GROUP BY path ORDER BY path FORMAT JSONEachRow",
    )
    truth = _execute_truth_queries(adapter, table, layout, catalog, expected_results)
    known_loss_only = all(
        truth["diagnostics"][query_id]["match"]
        or truth["diagnostics"][query_id]["known_loss_only"]
        for query_id in ("S05", "S06")
    )
    if not truth["analysis_truth_ok"]:
        raise RuntimeError(f"analysis truth mismatch: {layout}")
    if layout != "ch_native_auto32_none" and not truth["truth_ok"]:
        raise RuntimeError(f"document truth mismatch: {layout}")
    if layout == "ch_native_auto32_none" and not known_loss_only:
        raise RuntimeError(f"unexpected Native JSON document mismatch: {layout}")
    native_documents = None
    if originals is not None:
        document_fields = "event_id,attributes"
        if layout.endswith("_sparse"):
            document_fields += ",fidelity_values"
        elif layout.endswith("_full"):
            document_fields += ",attributes_raw"
        stored_documents = _json_rows(
            adapter,
            f"SELECT {document_fields} FROM {table} ORDER BY event_id FORMAT JSONEachRow",
        )
        if layout.endswith("_sparse"):
            recovered_documents = []
            for row in stored_documents:
                restored = restore_sparse_row(row)
                recovered_documents.append({
                    "event_id": row["event_id"],
                    "attributes": merge_fidelity(
                        restored["attributes"], restored["fidelity_values"]
                    ),
                })
            stored_documents = recovered_documents
        elif layout.endswith("_full"):
            stored_documents = [
                {"event_id": row["event_id"], "attributes": row["attributes_raw"]}
                for row in stored_documents
            ]
        native_documents = native_document_observation(originals, stored_documents)
        if not native_documents["ordinary_content_ok"]:
            raise RuntimeError(f"unexpected complete Native JSON content mismatch: {layout}")
    hinted_paths = (
        ["failure.mistake_mode", "gen_ai.operation.name"]
        if layout == "ch_native_hinted32_sparse" else []
    )
    return {
        "dynamic_paths": paths["dynamic_paths"],
        "hinted_paths": hinted_paths,
        "parts": parts,
        "path_types": {row["path"]: row["types"] for row in type_rows},
        "analysis_truth_ok": truth["analysis_truth_ok"],
        "analysis_results_sha256": truth["analysis_digest"],
        "query_matches": truth["matches"],
        "query_diagnostics": truth["diagnostics"],
        "query_results_sha256": truth["digest"],
        "truth_results_sha256": truth["truth_digest"],
        "known_native_loss_only": known_loss_only,
        "native_documents": native_documents,
        "shared_paths": paths["shared_paths"],
        "truth_ok": truth["truth_ok"],
        "truth_results": truth["actual"],
    }


def _wait_for_merges(adapter, table, owned_tables, timeout_seconds=30):
    """等待目标表 merge 空闲且 active part 数连续十次稳定。"""
    database, layout = _owned_table(table, owned_tables)
    started = time.monotonic()
    observations = []
    stable_streak = 0
    previous_part_count = None
    while True:
        rows = _json_rows(
            adapter,
            "SELECT (SELECT count() FROM system.merges WHERE "
            "database={database:String} AND table={table:String}) AS merge_count,"
            "(SELECT count() FROM system.parts WHERE active AND "
            "database={database:String} AND table={table:String}) AS part_count "
            "FORMAT JSONEachRow",
            parameters={"database": database, "table": layout},
        )
        observation = {
            "merge_count": int(rows[0]["merge_count"]),
            "part_count": int(rows[0]["part_count"]),
        }
        observations.append(observation)
        stable = observation["merge_count"] == 0 and observation["part_count"] == previous_part_count
        stable_streak = stable_streak + 1 if stable else 1 if observation["merge_count"] == 0 else 0
        previous_part_count = observation["part_count"]
        if stable_streak == 10:
            return {"observations": observations, "stable": True}
        if time.monotonic() - started >= timeout_seconds:
            raise TimeoutError(f"merge did not stabilize for {table}")
        time.sleep(0.2)


def run_final_probe(host, port, container_name, database):
    """验证 ReplacingMergeTree 的普通读、FINAL 与 OPTIMIZE FINAL 语义。"""
    validate_identifier(database, "database")
    adapter = ClickHouseFourLayoutAdapter(host, port, container_name, "s2sup_probe")
    created = False
    result = {}
    try:
        existing = _json_rows(
            adapter,
            "SELECT count() AS count FROM system.databases WHERE name={database:String} FORMAT JSONEachRow",
            parameters={"database": database},
        )
        if existing and int(existing[0]["count"]):
            raise ValueError(f"database already exists: {database}")
        _request(adapter, f"CREATE DATABASE {database}")
        created = True
        table = f"{database}.final_probe"
        _request(
            adapter,
            f"CREATE TABLE {table} (event_id String, version UInt64, value String) "
            "ENGINE=ReplacingMergeTree(version) ORDER BY event_id",
        )

        def versions(final=False):
            suffix = " FINAL" if final else ""
            rows = _json_rows(
                adapter,
                f"SELECT version FROM {table}{suffix} ORDER BY version FORMAT JSONEachRow",
            )
            return [int(row["version"]) for row in rows]

        def active_parts():
            rows = _json_rows(
                adapter,
                "SELECT count() AS count FROM system.parts WHERE active AND "
                "database={database:String} AND table='final_probe' FORMAT JSONEachRow",
                parameters={"database": database},
            )
            return int(rows[0]["count"])

        _request(adapter, f"SYSTEM STOP MERGES {table}")
        try:
            # 两次独立 INSERT 固定产生两个 data part。
            _request(adapter, f"INSERT INTO {table} VALUES ('event-1',1,'old')")
            _request(adapter, f"INSERT INTO {table} VALUES ('event-1',2,'new')")
            result["before_optimize"] = {
                "active_parts": active_parts(),
                "final_versions": versions(final=True),
                "ordinary_versions": versions(),
            }
        finally:
            _request(adapter, f"SYSTEM START MERGES {table}")
        optimize_started = time.perf_counter()
        _request(adapter, f"OPTIMIZE TABLE {table} FINAL")
        result["optimize_ms"] = (time.perf_counter() - optimize_started) * 1000
        result["after_optimize"] = {
            "active_parts": active_parts(),
            "final_versions": versions(final=True),
            "ordinary_versions": versions(),
        }
        result["cleanup_confirmed"] = False
        return result
    finally:
        if created:
            _request(adapter, f"DROP DATABASE IF EXISTS {database} SYNC")
            remaining = _json_rows(
                adapter,
                "SELECT count() AS count FROM system.databases WHERE name={database:String} FORMAT JSONEachRow",
                parameters={"database": database},
            )
            result["cleanup_confirmed"] = not remaining or int(remaining[0]["count"]) == 0


def observe_merge_stages(adapter, table, owned_tables, insert_statements, collect_stage):
    """暂停自有表 merge，记录固定阶段，并保证恢复后台 merge。"""
    if not isinstance(insert_statements, list) or not insert_statements:
        raise ValueError("insert_statements must be a non-empty list")
    _owned_table(table, owned_tables)
    stages = {"ddl": collect_stage("ddl")}
    _request(adapter, f"SYSTEM STOP MERGES {table}")
    try:
        _request(adapter, insert_statements[0])
        stages["first_insert"] = collect_stage("first_insert")
        for statement in insert_statements[1:]:
            _request(adapter, statement)
        stages["all_inserts"] = collect_stage("all_inserts")
    finally:
        _request(adapter, f"SYSTEM START MERGES {table}")
    stages["merge_wait"] = _wait_for_merges(adapter, table, owned_tables)
    stages["merges_stable"] = collect_stage("merges_stable")
    _request(adapter, f"OPTIMIZE TABLE {table} FINAL")
    stages["optimize_final"] = collect_stage("optimize_final")
    return stages


def run_clickhouse_mechanisms(input_dir, truth_dir, host, port, container_name, database):
    """校验 Task 1 冻结输入/truth 后执行四种 Native JSON 机制结构。"""
    loaded = load_inputs(input_dir, truth_dir)
    return _run_loaded(*loaded, host, port, container_name, database)


def collect_environment_identity(host, port, container_name, namespace):
    """核对固定 ClickHouse 服务端与容器身份。"""
    validate_identifier(namespace, "namespace")
    task4 = _task4_runner()
    container = task4._container_identity(container_name)
    task4._require_host_port(container, "8123/tcp", port, "ClickHouse")
    adapter = ClickHouseFourLayoutAdapter(host, port, container_name, "s2sup_identity")
    version = adapter.database_version()
    if version != "25.12.11.4":
        raise RuntimeError("invalid ClickHouse database version")
    return {
        "container": {**container, "database_version": version},
        "endpoint": {"host": host, "port": port},
    }


def _mechanism_identity(database):
    """记录机制程序、生成 DDL 与 S01-S06 查询身份。"""
    queries = {
        layout: {
            query_id: mechanism_query_sql(layout, query_id)
            for query_id in QUERY_IDS
        }
        for layout in LAYOUTS
    }
    runner_dir = Path(__file__).resolve().parent
    return {
        "ddl_sha256": hashlib.sha256(canonical_bytes(mechanism_ddls(database))).hexdigest(),
        "files": {
            "runner/run_clickhouse_mechanisms.py": file_identity(__file__),
            "runner/clickhouse_four_layout.py": file_identity(runner_dir / "clickhouse_four_layout.py"),
            "runner/run_four_layouts.py": file_identity(runner_dir / "run_four_layouts.py"),
            "runner/supplement_common.py": file_identity(runner_dir / "supplement_common.py"),
            "generator/generate_supplement_truth.py": file_identity(
                runner_dir.parent / "generator" / "generate_supplement_truth.py"
            ),
        },
        "query_ids": list(QUERY_IDS),
        "queries_sha256": hashlib.sha256(canonical_bytes(queries)).hexdigest(),
    }


def _correctness_summary(result):
    """从完整机制结果提取 truth、FINAL 与清理门禁摘要。"""
    layouts = result.get("layouts", {}) if isinstance(result, dict) else {}
    sidecars = result.get("sidecars", {}) if isinstance(result, dict) else {}
    sidecar_ok = set(sidecars) == set(LAYOUTS) and all(
        layout == "ch_native_auto32_none" or item.get("mismatch_count") == 0
        for layout, item in sidecars.items()
    )
    stage_results = [
        stage
        for layout in layouts.values()
        for name, stage in layout.get("stages", {}).items()
        if name != "merge_wait"
    ]
    return {
        "analysis_truth_ok": bool(stage_results) and all(
            stage.get("analysis_truth_ok") is True for stage in stage_results
        ),
        "cleanup_confirmed": result.get("cleanup_confirmed") is True,
        "final_cleanup_confirmed": result.get("final_probe", {}).get("cleanup_confirmed") is True,
        "layout_count": len(layouts),
        "layouts_complete": set(layouts) == set(LAYOUTS),
        "sidecar_ok": sidecar_ok,
    }


def execute(args):
    """执行机制链，并以公共状态机最后发布 manifest。"""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "run-manifest.json").unlink(missing_ok=True)
    (output / "mechanism-result.json").unlink(missing_ok=True)
    manifest = {
        "command": list(getattr(args, "command", [Path(sys.executable).name, *sys.argv])),
        "engine": "clickhouse",
        "format": "agent-trace-clickhouse-native-json-mechanism-run",
        "format_version": 1,
        "namespace": args.namespace,
        "run_id": f"clickhouse-mechanism-{args.namespace}-{uuid.uuid4().hex}",
    }
    artifacts = {}
    try:
        manifest["environment"] = collect_environment_identity(
            args.host, args.port, args.container_name, args.namespace
        )
        manifest["mechanism"] = _mechanism_identity(args.namespace)
        result = run_clickhouse_mechanisms(
            args.input, args.truth, args.host, args.port, args.container_name, args.namespace
        )
        artifacts["mechanism-result.json"] = canonical_bytes(result) + b"\n"
        manifest["input"] = result.get("input")
        manifest["correctness"] = _correctness_summary(result)
        if (result.get("status") != "complete" or result.get("cleanup_confirmed") is not True
                or not all(manifest["correctness"].get(name) is True for name in (
                    "analysis_truth_ok", "final_cleanup_confirmed", "layouts_complete", "sidecar_ok",
                ))):
            raise RuntimeError("ClickHouse mechanism run incomplete, incorrect, or cleanup unconfirmed")
    except Exception as error:
        write_failed_manifest(output, manifest, artifacts, error)
        raise
    write_manifest_last(output, manifest, artifacts)
    return result


def parse_args(argv=None):
    """解析 ClickHouse 机制观察程序参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18123, type=int)
    parser.add_argument("--container-name", default="agent-trace-clickhouse-25-12")
    return parser.parse_args(argv)


def _run_loaded(rows, source_truth, catalog, truth, identity, host, port, container_name, database):
    """对已通过 Task 4 身份门禁的输入执行 ClickHouse 机制流程。"""
    validate_identifier(database, "database")
    contract = validate_truth_contract(catalog, truth)
    adapter = ClickHouseFourLayoutAdapter(host, port, container_name, "s2sup_probe")
    generator = _truth_generator()
    block_size = source_truth["block_size"]
    blocks = [rows[index:index + block_size] for index in range(0, len(rows), block_size)]
    partial_truth = {
        "ddl": {
            query_id: generator.query_results([], query_id, contract["parameters"][query_id])
            for query_id in QUERY_IDS
        },
        "first_insert": {
            query_id: generator.query_results(blocks[0], query_id, contract["parameters"][query_id])
            for query_id in QUERY_IDS
        },
    }
    partial_rows = {"ddl": [], "first_insert": blocks[0]}
    created_database = False
    owned_tables = set()
    result = {
        "cleanup_confirmed": False,
        "input": identity,
        "layouts": {},
        "sidecars": {},
        "status": "failed",
    }

    def database_exists():
        rows_found = _json_rows(
            adapter,
            "SELECT count() AS count FROM system.databases WHERE name={database:String} FORMAT JSONEachRow",
            parameters={"database": database},
        )
        return bool(rows_found and int(rows_found[0]["count"]))

    try:
        if database_exists():
            raise ValueError(f"database already exists: {database}")
        _request(adapter, f"CREATE DATABASE {database}")
        created_database = True
        for layout, ddl in mechanism_ddls(database).items():
            _request(adapter, ddl)
            owned_tables.add(f"{database}.{layout}")

        for layout in LAYOUTS:
            table = f"{database}.{layout}"
            insert_statements = []
            for block in blocks:
                prepared = []
                for row in block:
                    item = adapter._analytics_row("ch_native", row)
                    if layout == "ch_native_auto32_none":
                        item.pop("fidelity_values")
                    elif layout == "ch_native_auto32_full":
                        item.pop("fidelity_values")
                        item["attributes_raw"] = canonical_bytes(row["attributes_analysis"]).decode("utf-8")
                    elif layout.endswith("_sparse"):
                        item["fidelity_values"].update(
                            hint_presence_markers(row["attributes_analysis"])
                        )
                    prepared.append(item)
                body = "".join(canonical_bytes(item).decode("utf-8") + "\n" for item in prepared)
                insert_statements.append(f"INSERT INTO {table} FORMAT JSONEachRow\n{body}")

            def collect(phase):
                expected = partial_truth.get(phase, contract["results"])
                expected_rows = partial_rows.get(phase, rows)
                return collect_native_stage(
                    adapter, table, owned_tables, catalog, expected, expected_rows
                )

            stages = observe_merge_stages(
                adapter, table, owned_tables, insert_statements, collect
            )
            final_states = [stages[name] for name in ("all_inserts", "merges_stable", "optimize_final")]
            expected_paths = set(final_states[0]["path_types"])
            transitions = [
                validate_path_transition(before, after, expected_paths)
                for before, after in zip(final_states, final_states[1:])
            ]
            required_preserved = all(
                transition["logical_paths_preserved"]
                and transition["analysis_results_preserved"]
                and (layout == "ch_native_auto32_none" or transition["query_results_preserved"])
                for transition in transitions
            )
            if not required_preserved:
                raise RuntimeError(
                    f"path transition mismatch: {layout}: "
                    + canonical_bytes(transitions).decode("utf-8")
                )

            fields = "event_id,attributes"
            if layout.endswith("_sparse"):
                fields += ",fidelity_values"
            elif layout.endswith("_full"):
                fields += ",attributes_raw"
            stored = _json_rows(
                adapter, f"SELECT {fields} FROM {table} ORDER BY event_id FORMAT JSONEachRow"
            )
            originals = [
                {"event_id": row["event_id"], "attributes": row["attributes_analysis"]}
                for row in rows
            ]
            result["sidecars"][layout] = sidecar_observation(layout, originals, stored)
            result["layouts"][layout] = {
                "full_result_changed_across_transition": any(
                    not item["query_results_preserved"] for item in transitions
                ),
                "stages": stages,
                "transitions": transitions,
            }

        final_database = validate_identifier(database + "_final", "final database")
        result["final_probe"] = run_final_probe(host, port, container_name, final_database)
        if not result["final_probe"]["cleanup_confirmed"]:
            raise RuntimeError("FINAL probe cleanup failed")
    finally:
        if created_database:
            try:
                cleanup_owned_database(adapter, database, result)
            finally:
                owned_tables.clear()
    result["status"] = "complete"
    return result


if __name__ == "__main__":
    arguments = parse_args()
    arguments.command = [Path(sys.executable).name, *sys.argv]
    execute(arguments)
