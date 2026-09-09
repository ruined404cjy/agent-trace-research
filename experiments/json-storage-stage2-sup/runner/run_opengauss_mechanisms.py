#!/usr/bin/env python3
"""观察 openGauss JSON/JSONB 自然读写与索引机制。"""

import argparse
import json
import hashlib
import re
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

from opengauss_four_layout import OpenGaussFourLayoutAdapter, validate_identifier
from supplement_common import (
    QUERY_IDS, canonical_bytes, file_identity, write_failed_manifest,
    write_manifest_last,
)
from run_four_layouts import _container_identity, _require_host_port, load_inputs


LAYOUTS = ("og_json", "og_json_hot", "og_jsonb", "og_jsonb_hot", "og_jsonb_gin")
HOT_PATH = "{gen_ai,operation,name}"


def opengauss_mechanism_ddls(schema: str) -> dict[str, str]:
    """返回五种 JSON/JSONB 机制表 DDL；schema 必须是安全 identifier。"""
    validate_identifier(schema, "schema")
    definitions = {}
    for layout in LAYOUTS:
        value_type = "JSON" if layout.startswith("og_json_") or layout == "og_json" else "JSONB"
        table = f"{schema}.{layout}"
        statements = [
            f"""CREATE TABLE {table} (
    ingest_seq BIGINT NOT NULL,
    event_id TEXT NOT NULL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    project_id TEXT NOT NULL,
    start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    duration_ms BIGINT NOT NULL,
    span_type TEXT NOT NULL,
    framework TEXT NOT NULL,
    level TEXT NOT NULL,
    attributes {value_type} NOT NULL
)""",
        ]
        if layout.endswith("_hot"):
            statements.append(
                f"CREATE INDEX {layout}_operation_idx ON {table} "
                f"((attributes #>> '{HOT_PATH}'))"
            )
        elif layout == "og_jsonb_gin":
            statements.append(
                f"CREATE INDEX {layout}_attributes_idx ON {table} "
                "USING gin(attributes jsonb_hash_ops)"
            )
        definitions[layout] = ";\n".join(statements) + ";"
    return definitions


def path_query_sql(layout: str) -> str:
    """返回热点路径自然查询；GIN 组使用 JSONB containment。"""
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported mechanism layout: {layout}")
    table = "{table}"
    if layout == "og_jsonb_gin":
        return f"SELECT event_id FROM {table} WHERE attributes @> %s::jsonb ORDER BY event_id"
    return (
        f"SELECT event_id FROM {table} WHERE attributes #>> '{HOT_PATH}' = %s "
        "ORDER BY event_id"
    )


def _execute_ddl(connection, ddl):
    """执行由本模块生成的分号分隔 DDL。"""
    for statement in (part.strip() for part in ddl.split(";") if part.strip()):
        connection.execute(statement)


def _storage(connection, table):
    """返回目标表的 heap、TOAST、index 与合计空间。"""
    row = connection.execute(
        "SELECT pg_relation_size(%s::regclass), GREATEST("
        "pg_total_relation_size(%s::regclass)-pg_relation_size(%s::regclass)-"
        "pg_indexes_size(%s::regclass),0), pg_indexes_size(%s::regclass), "
        "pg_total_relation_size(%s::regclass)",
        (table, table, table, table, table, table),
    ).fetchone()
    names = ("heap_bytes", "toast_bytes", "index_bytes", "total_bytes")
    return {name: int(value) for name, value in zip(names, row)}


def validate_document_rows(rows, expected_hashes):
    """按 event_id 校验服务端完整文档 canonical 摘要与字节。"""
    if not isinstance(rows, list) or not isinstance(expected_hashes, dict):
        raise ValueError("rows and expected_hashes must be collections")
    actual_ids = [row[0] for row in rows]
    mismatches = []
    canonical_documents = []
    for event_id, value in rows:
        if isinstance(value, str):
            value = json.loads(value)
        document = canonical_bytes(value)
        canonical_documents.append([event_id, value])
        if event_id in expected_hashes and hashlib.sha256(document).hexdigest() != expected_hashes[event_id]:
            mismatches.append(event_id)
    actual = set(actual_ids)
    expected = set(expected_hashes)
    duplicates = sorted(
        event_id for event_id, count in Counter(actual_ids).items() if count > 1
    )
    return {
        "canonical_bytes": sum(len(canonical_bytes(value)) for _, value in canonical_documents),
        "canonical_sha256": hashlib.sha256(canonical_bytes(canonical_documents)).hexdigest(),
        "duplicates": duplicates,
        "extra": sorted(actual - expected),
        "mismatches": sorted(mismatches),
        "missing": sorted(expected - actual),
        "ok": not any((duplicates, actual - expected, expected - actual, mismatches)) and len(rows) == len(expected),
        "row_count": len(rows),
    }


def validate_truth_contract(catalog, truth):
    """要求 catalog/truth 精确覆盖 S01 至 S06。"""
    parameters = catalog.get("parameters") if isinstance(catalog, dict) else None
    results = truth.get("results") if isinstance(truth, dict) else None
    expected = set(QUERY_IDS)
    if not isinstance(parameters, dict) or set(parameters) != expected \
            or not isinstance(results, dict) or set(results) != expected:
        raise ValueError("catalog and truth must contain exactly S01-S06")
    return parameters, results


def parse_server_timing(plan_text):
    """从 openGauss EXPLAIN ANALYZE 提取服务端 Total runtime。"""
    if not isinstance(plan_text, str):
        raise ValueError("plan_text must be a string")
    match = re.search(r"Total runtime: ([0-9.]+) ms", plan_text)
    if match is None:
        raise ValueError("missing openGauss Total runtime")
    return float(match.group(1))


def attributes_to_jsonb_probe(layout, plan_text, client_observed_ms, scanned_rows, returned_rows):
    """标注 attributes::jsonb 复合扫描探针，不拆分其中各服务端操作。"""
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported mechanism layout: {layout}")
    source_type = "JSON" if layout.startswith("og_json_") or layout == "og_json" else "JSONB"
    execution_ms = parse_server_timing(plan_text)
    return {
        "client_observed_overhead_ms": max(client_observed_ms - execution_ms, 0.0),
        "client_observed_wall_ms": client_observed_ms,
        "execution_ms": execution_ms,
        "expression": "attributes::jsonb",
        "measurement_scope": "scan_filter_expression_result",
        "plan": plan_text,
        "returned_rows": int(returned_rows),
        "scanned_rows": int(scanned_rows),
        "source_type": source_type,
    }


def cleanup_owned_schema(connection, schema, result):
    """删除本次创建的 schema，并把不存在确认纳入完成门禁。"""
    validate_identifier(schema, "schema")
    connection.rollback()
    connection.execute(f"DROP SCHEMA {schema} CASCADE")
    connection.commit()
    result["cleanup_confirmed"] = not connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,)
    ).fetchone()[0]
    if not result["cleanup_confirmed"]:
        result["status"] = "failed"
        raise RuntimeError(f"schema cleanup not confirmed: {schema}")


def run_opengauss_mechanisms(input_dir, truth_dir, host, port, container_name, schema):
    """校验 Task 1 冻结输入/truth 后执行五种 openGauss 机制结构。"""
    loaded = load_inputs(input_dir, truth_dir)
    return _run_loaded(*loaded, host, port, container_name, schema)


def collect_environment_identity(host, port, container_name, namespace):
    """核对固定 openGauss 服务端与容器身份。"""
    validate_identifier(namespace, "namespace")
    container = _container_identity(container_name)
    _require_host_port(container, "5432/tcp", port, "openGauss")
    adapter = OpenGaussFourLayoutAdapter(host, port, container_name, "s2sup_identity")
    version = adapter.database_version()
    if not isinstance(version, str) or not version.startswith("(openGauss 6.0.0 "):
        raise RuntimeError("invalid openGauss database version")
    return {
        "container": {**container, "database_version": version},
        "endpoint": {"host": host, "port": port},
    }


def _mechanism_identity(namespace):
    """记录机制程序、生成 DDL 与 S01-S06 查询身份。"""
    queries = {
        layout: {
            query_id: OpenGaussFourLayoutAdapter.query_sql(
                "og_json" if layout.startswith("og_json_") or layout == "og_json" else "og_jsonb",
                query_id,
            )
            for query_id in QUERY_IDS
        }
        for layout in LAYOUTS
    }
    runner_dir = Path(__file__).resolve().parent
    return {
        "ddl_sha256": hashlib.sha256(canonical_bytes(opengauss_mechanism_ddls(namespace))).hexdigest(),
        "files": {
            "runner/run_opengauss_mechanisms.py": file_identity(__file__),
            "runner/opengauss_four_layout.py": file_identity(runner_dir / "opengauss_four_layout.py"),
            "runner/run_four_layouts.py": file_identity(runner_dir / "run_four_layouts.py"),
            "runner/supplement_common.py": file_identity(runner_dir / "supplement_common.py"),
        },
        "query_ids": list(QUERY_IDS),
        "queries_sha256": hashlib.sha256(canonical_bytes(queries)).hexdigest(),
    }


def _correctness_summary(result):
    """从完整机制结果提取 truth 与清理门禁摘要。"""
    layouts = result.get("layouts", {}) if isinstance(result, dict) else {}
    truth_ok = set(layouts) == set(LAYOUTS) and all(
        item.get("queries_ok") is True and item.get("documents", {}).get("ok") is True
        for item in layouts.values()
    )
    return {
        "cleanup_confirmed": result.get("cleanup_confirmed") is True,
        "layout_count": len(layouts),
        "truth_ok": truth_ok,
    }


def execute(args):
    """执行机制链，并以公共状态机最后发布 manifest。"""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "run-manifest.json").unlink(missing_ok=True)
    (output / "mechanism-result.json").unlink(missing_ok=True)
    manifest = {
        "command": list(getattr(args, "command", [Path(sys.executable).name, *sys.argv])),
        "engine": "opengauss",
        "format": "agent-trace-opengauss-json-mechanism-run",
        "format_version": 1,
        "namespace": args.namespace,
        "run_id": f"opengauss-mechanism-{args.namespace}-{uuid.uuid4().hex}",
    }
    artifacts = {}
    try:
        manifest["environment"] = collect_environment_identity(
            args.host, args.port, args.container_name, args.namespace
        )
        manifest["mechanism"] = _mechanism_identity(args.namespace)
        result = run_opengauss_mechanisms(
            args.input, args.truth, args.host, args.port, args.container_name, args.namespace
        )
        artifacts["mechanism-result.json"] = canonical_bytes(result) + b"\n"
        manifest["input"] = result.get("input")
        manifest["correctness"] = _correctness_summary(result)
        if (result.get("status") != "complete" or result.get("cleanup_confirmed") is not True
                or manifest["correctness"]["truth_ok"] is not True):
            raise RuntimeError("openGauss mechanism run incomplete, incorrect, or cleanup unconfirmed")
    except Exception as error:
        write_failed_manifest(output, manifest, artifacts, error)
        raise
    write_manifest_last(output, manifest, artifacts)
    return result


def parse_args(argv=None):
    """解析 openGauss 机制观察程序参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=15432, type=int)
    parser.add_argument("--container-name", default="agent-trace-opengauss-v6")
    return parser.parse_args(argv)


def _run_loaded(rows, source_truth, catalog, truth, identity, host, port, container_name, schema):
    """对已通过 Task 4 身份门禁的输入执行 openGauss 机制流程。"""
    validate_identifier(schema, "schema")
    parameters, expected_results = validate_truth_contract(catalog, truth)
    adapter = OpenGaussFourLayoutAdapter(host, port, container_name, "s2sup_probe")
    block_size = source_truth["block_size"]
    blocks = [rows[index:index + block_size] for index in range(0, len(rows), block_size)]
    expected_hashes = {
        record["event_id"]: record["analysis_sha256"] for record in source_truth["records"]
    }
    operation_name = parameters["S02"]["operation_name"]
    expected_path_ids = sorted(
        row["event_id"] for row in rows
        if row.get("attributes_analysis", {}).get("gen_ai", {}).get("operation", {}).get("name")
        == operation_name
    )
    preprocess_started = time.perf_counter()
    preprocessed_bytes = sum(len(canonical_bytes(row["attributes_analysis"])) for row in rows)
    client_preprocess_ms = (time.perf_counter() - preprocess_started) * 1000
    connection = adapter.connect_worker()
    owned = False
    result = {
        "cleanup_confirmed": False,
        "client_preprocess": {"canonical_bytes": preprocessed_bytes, "elapsed_ms": client_preprocess_ms},
        "input": identity,
        "layouts": {},
        "status": "failed",
    }
    try:
        exists = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)", (schema,)
        ).fetchone()[0]
        if exists:
            raise ValueError(f"schema already exists: {schema}")
        connection.execute(f"CREATE SCHEMA {schema}")
        connection.commit()
        owned = True
        for ddl in opengauss_mechanism_ddls(schema).values():
            _execute_ddl(connection, ddl)
        connection.commit()
        for layout in LAYOUTS:
            table = f"{schema}.{layout}"
            copy_started = time.perf_counter()
            for block in blocks:
                with connection.cursor() as cursor:
                    adapter._copy_analytics(cursor, table, block)
                connection.commit()
            copy_ms = (time.perf_counter() - copy_started) * 1000
            analyze_started = time.perf_counter()
            connection.autocommit = True
            connection.execute(f"ANALYZE {table}")
            connection.autocommit = False
            analyze_ms = (time.perf_counter() - analyze_started) * 1000

            query_results = {}
            query_timings = {}
            base_layout = "og_json" if layout.startswith("og_json_") or layout == "og_json" else "og_jsonb"
            for query_id in expected_results:
                statement = adapter.query_sql(base_layout, query_id).replace("{analytics}", table)
                started = time.perf_counter()
                raw_rows = connection.execute(
                    statement, adapter._query_parameters(query_id, parameters[query_id])
                ).fetchall()
                query_timings[query_id] = (time.perf_counter() - started) * 1000
                normalized = adapter._normalize_result(query_id, raw_rows)
                if normalized != expected_results[query_id]:
                    raise RuntimeError(f"truth mismatch: {layout} {query_id}")
                query_results[query_id] = normalized

            path_parameter = operation_name
            if layout == "og_jsonb_gin":
                path_parameter = canonical_bytes(
                    {"gen_ai": {"operation": {"name": operation_name}}}
                ).decode("utf-8")
            path_statement = path_query_sql(layout).replace("{table}", table)
            natural_plan = "\n".join(
                row[0] for row in connection.execute("EXPLAIN " + path_statement, (path_parameter,))
            )
            actual_path_ids = [
                row[0] for row in connection.execute(path_statement, (path_parameter,)).fetchall()
            ]
            if actual_path_ids != expected_path_ids:
                raise RuntimeError(f"path truth mismatch: {layout}")

            probe_wall_started = time.perf_counter()
            plan_rows = connection.execute(
                f"EXPLAIN ANALYZE SELECT attributes::jsonb FROM {table} WHERE ingest_seq < %s",
                (block_size,),
            ).fetchall()
            probe_wall_ms = (time.perf_counter() - probe_wall_started) * 1000
            plan_text = "\n".join(row[0] for row in plan_rows)

            document_started = time.perf_counter()
            document_rows = connection.execute(
                f"SELECT event_id,attributes::text FROM {table} ORDER BY event_id"
            ).fetchall()
            server_return_wall_ms = (time.perf_counter() - document_started) * 1000
            recovery_started = time.perf_counter()
            documents = validate_document_rows(document_rows, expected_hashes)
            documents["recovery_ms"] = (time.perf_counter() - recovery_started) * 1000
            documents["server_return_wall_ms"] = server_return_wall_ms
            if not documents["ok"]:
                raise RuntimeError(f"document truth mismatch: {layout}")
            row_count = int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            probe_returned_rows = int(connection.execute(
                f"SELECT count(*) FROM {table} WHERE ingest_seq < %s", (block_size,)
            ).fetchone()[0])
            try:
                probe = attributes_to_jsonb_probe(
                    layout, plan_text, probe_wall_ms, row_count, probe_returned_rows
                )
            except ValueError as error:
                raise RuntimeError(f"invalid attributes_to_jsonb probe: {layout}") from error
            result["layouts"][layout] = {
                "analyze_ms": analyze_ms,
                "attributes_to_jsonb_probe": probe,
                "copy_and_index_maintenance_ms": copy_ms,
                "documents": documents,
                "natural_plan": natural_plan,
                "path_identity_sha256": hashlib.sha256(canonical_bytes(actual_path_ids)).hexdigest(),
                "queries": query_results,
                "queries_ok": True,
                "query_timings_ms": query_timings,
                "row_count": row_count,
                "storage": _storage(connection, table),
            }
    finally:
        try:
            if owned:
                cleanup_owned_schema(connection, schema, result)
        finally:
            connection.close()
    result["status"] = "complete"
    return result


if __name__ == "__main__":
    arguments = parse_args()
    arguments.command = [Path(sys.executable).name, *sys.argv]
    execute(arguments)
