#!/usr/bin/env python3
"""运行阶段二 JSONB 与 Native JSON 的调优优先矩阵。"""

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path

from clickhouse_four_layout import ClickHouseFourLayoutAdapter
from opengauss_four_layout import OpenGaussFourLayoutAdapter
from run_four_layouts import collect_environment, load_inputs, run_query_stage
from supplement_common import canonical_bytes, write_failed_manifest, write_manifest_last


DEFAULT_TARGETS = (
    "og_jsonb_tuned",
    "ch_native_numeric_hint",
    "ch_native_trace_sort",
    "og_jsonb_baseline",
    "ch_native_baseline",
)
TARGETS = {
    "og_json_baseline": {"engine": "opengauss", "layout": "og_json", "profile": "baseline", "suffix": "ogj"},
    "og_jsonb_tuned": {"engine": "opengauss", "layout": "og_jsonb", "profile": "tuned", "suffix": "ogt"},
    "ch_native_tuned": {"engine": "clickhouse", "layout": "ch_native", "profile": "tuned", "suffix": "cht"},
    "ch_native_numeric_hint": {"engine": "clickhouse", "layout": "ch_native", "profile": "numeric_hint", "suffix": "chh"},
    "ch_native_trace_sort": {"engine": "clickhouse", "layout": "ch_native", "profile": "trace_sort", "suffix": "chs"},
    "og_jsonb_baseline": {"engine": "opengauss", "layout": "og_jsonb", "profile": "baseline", "suffix": "ogb"},
    "ch_native_baseline": {"engine": "clickhouse", "layout": "ch_native", "profile": "baseline", "suffix": "chb"},
    "ch_string_baseline": {"engine": "clickhouse", "layout": "ch_string", "profile": "baseline", "suffix": "chj"},
}
OPENGAUSS_SELECTIVITY_PROBES = {
    "dense_operation": {
        "expected_count": 20155,
        "index_name": "analytics_operation_name_idx",
        "path": "{gen_ai,operation,name}",
        "value": "execute_tool",
    },
    "medium_operation": {
        "expected_count": 4277,
        "index_name": "analytics_operation_name_idx",
        "path": "{gen_ai,operation,name}",
        "value": "chat",
    },
    "sparse_failure": {
        "expected_count": 741,
        "index_name": "analytics_failure_mode_idx",
        "path": "{failure,mistake_mode}",
        "value": "A.3",
    },
}


def parse_targets(value):
    """解析目标列表并保持用户给定顺序。"""
    targets = tuple(item.strip() for item in value.split(",") if item.strip())
    if not targets or len(set(targets)) != len(targets) or any(item not in TARGETS for item in targets):
        raise ValueError("targets must contain unique known target names")
    optimized = tuple(item for item in targets if TARGETS[item]["profile"] != "baseline")
    baselines = tuple(item for item in targets if TARGETS[item]["profile"] == "baseline")
    if targets != optimized + baselines:
        raise ValueError("optimized targets must precede baseline targets")
    return targets


def opengauss_gin_sql():
    """返回与低密度字段过滤 truth 等价的 JSONB containment 查询。"""
    return (
        "SELECT event_id FROM {analytics} WHERE project_id = %s AND start_time >= %s "
        "AND start_time < %s AND attributes @> %s::jsonb ORDER BY event_id"
    )


def opengauss_selectivity_sql(path):
    """返回统一标量输出的 JSONB 路径选择率查询。"""
    allowed = {probe["path"] for probe in OPENGAUSS_SELECTIVITY_PROBES.values()}
    if path not in allowed:
        raise ValueError("unsupported selectivity path")
    return (
        "SELECT count(*) FROM {analytics} WHERE project_id = %s AND start_time >= %s "
        f"AND start_time < %s AND attributes #>> '{path}' = %s"
    )


def opengauss_force_index_sql(statement, index_name):
    """在实际执行的 SELECT 中嵌入 openGauss 扫描 Hint。"""
    if not isinstance(index_name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", index_name):
        raise ValueError("index name must be a valid identifier")
    if not statement.startswith("SELECT "):
        raise ValueError("statement must start with SELECT")
    return statement.replace(
        "SELECT ", f"SELECT /*+ indexscan(analytics {index_name}) */ ", 1,
    )


def opengauss_access_gate(evidence):
    """核对四项 openGauss 调优访问结构均出现在实际计划中。"""
    checks = {
        "dense_natural_execution": (
            evidence.get("query_stage_index_scans", {}).get("analytics_operation_name_idx", 0)
            >= evidence.get("query_stage_expected_index_scans", 1)
        ),
        "dense_expression_btree": "analytics_operation_name_idx" in evidence.get("dense_forced_plan", ""),
        "dense_expression_execution": (
            evidence.get("dense_forced_idx_scan_delta", 0)
            >= evidence.get("dense_forced_expected_index_scans", 1)
        ),
        "expression_btree": "analytics_failure_mode_idx" in evidence.get("failure_btree_plan", ""),
        "gin": "analytics_attributes_gin_idx" in evidence.get("failure_gin_plan", ""),
        "trace_btree": "analytics_trace_lookup_idx" in evidence.get("trace_plan", ""),
    }
    return {"checks": checks, "missing": [name for name, ok in checks.items() if not ok], "ok": all(checks.values())}


def clickhouse_access_gate(evidence, profile="tuned"):
    """核对 Native JSON 数值 type hint、Trace 主键访问与路径库存。"""
    ddl = evidence.get("ddl", "")
    dynamic = evidence.get("dynamic_paths", [])
    shared = evidence.get("shared_paths", [])
    path_parts = evidence.get("path_parts", [])
    numeric_path = "experiment.duration_ms"
    hinted = f"`{numeric_path}` Int64" in ddl and numeric_path not in dynamic and numeric_path not in shared
    trace_plan = evidence.get("trace_plan", "")
    primary_key = all(value in trace_plan for value in ("PrimaryKey", "trace_id", "binary search"))
    checks = {
        "path_inventory": isinstance(dynamic, list) and isinstance(shared, list) and bool(path_parts),
    }
    if profile in {"numeric_hint", "tuned"}:
        checks["numeric_type_hint"] = hinted
    if profile in {"trace_sort", "tuned"}:
        checks["trace_primary_key"] = primary_key
    return {
        "checks": checks,
        "missing": [name for name, ok in checks.items() if not ok],
        "numeric_path_storage": "type_hint" if hinted else "unverified",
        "ok": all(checks.values()),
        "trace_access_path": "primary_key" if primary_key else "unverified",
    }


def _og_plan(adapter, layout, query_id, params):
    """使用正式绑定参数执行 EXPLAIN ANALYZE 并返回文本计划。"""
    connection = adapter.connect_worker()
    try:
        rows = connection.execute(
            "EXPLAIN ANALYZE " + adapter._statement(layout, query_id),
            adapter._query_parameters(query_id, params),
        ).fetchall()
        return "\n".join(row[0] for row in rows)
    finally:
        connection.close()


def _og_gin_probe(adapter, params, expected, measurements):
    """执行 JSONB GIN 等价查询并核对与字段过滤相同的 identity。"""
    statement = opengauss_gin_sql().replace("{analytics}", adapter._analytics("og_jsonb"))
    values = (
        params["project_id"], params["start_time"], params["end_time"],
        json.dumps({"failure": {"mistake_mode": params["failure_mistake_mode"]}}, separators=(",", ":")),
    )
    connection = adapter.connect_worker()
    try:
        plan_rows = connection.execute("EXPLAIN ANALYZE " + statement, values).fetchall()
        plan = "\n".join(row[0] for row in plan_rows)
        samples = []
        for phase, count in (("warmup", 1), ("measurement", measurements)):
            for _ in range(count):
                started = time.perf_counter()
                event_ids = [row[0] for row in connection.execute(statement, values).fetchall()]
                latency_ms = (time.perf_counter() - started) * 1000
                actual = {
                    "identity_sha256": hashlib.sha256(canonical_bytes(sorted(event_ids))).hexdigest(),
                    "row_count": len(event_ids),
                }
                if actual != expected:
                    raise RuntimeError("GIN query truth mismatch")
                samples.append({"latency_ms": latency_ms, "phase": phase})
        return {"plan": plan, "samples": samples}
    finally:
        connection.close()


def _og_dense_forced_probe(adapter, params, expected, measurements):
    """通过扫描 Hint 验证高密度表达式索引的实际可用性与代价。"""
    statement = opengauss_force_index_sql(
        adapter._statement("og_jsonb", "S02"), "analytics_operation_name_idx",
    )
    values = adapter._query_parameters("S02", params)
    before_scans = _og_index_scan_count(adapter, "analytics_operation_name_idx")
    connection = adapter.connect_worker()
    try:
        connection.prepare_threshold = None
        plan_rows = connection.execute("EXPLAIN ANALYZE " + statement, values).fetchall()
        plan = "\n".join(row[0] for row in plan_rows)
        samples = []
        for phase, count in (("warmup", 1), ("measurement", measurements)):
            for _ in range(count):
                started = time.perf_counter()
                rows = connection.execute(statement, values).fetchall()
                latency_ms = (time.perf_counter() - started) * 1000
                if adapter._normalize_result("S02", rows) != expected:
                    raise RuntimeError("dense expression index query truth mismatch")
                samples.append({"latency_ms": latency_ms, "phase": phase})
    finally:
        connection.close()
    expected_index_scans = measurements + 2
    after_scans = _og_wait_index_scan_count(
        adapter,
        "analytics_operation_name_idx",
        before_scans + expected_index_scans,
    )
    return {
        "expected_index_scans": expected_index_scans,
        "idx_scan_delta": after_scans - before_scans,
        "plan": plan,
        "samples": samples,
    }


def _og_selectivity_probes(adapter, catalog, measurements):
    """用统一 count 输出比较三种命中密度的自然与强制计划。"""
    window = catalog["parameters"]["S01"]
    values_prefix = (window["project_id"], window["start_time"], window["end_time"])
    results = {}
    for name, probe in OPENGAUSS_SELECTIVITY_PROBES.items():
        statement = opengauss_selectivity_sql(probe["path"]).replace(
            "{analytics}", adapter._analytics("og_jsonb")
        )
        values = (*values_prefix, probe["value"])
        natural = _og_selectivity_mode(
            adapter, statement, values, probe["expected_count"], measurements,
            probe["index_name"],
        )
        forced = _og_selectivity_mode(
            adapter,
            opengauss_force_index_sql(statement, probe["index_name"]),
            values,
            probe["expected_count"],
            measurements,
            probe["index_name"],
        )
        results[name] = {
            "expected_count": probe["expected_count"],
            "forced": forced,
            "natural": natural,
        }
    return results


def _og_selectivity_mode(adapter, statement, values, expected_count, measurements, index_name):
    """以定制计划测量一种选择率模式，并记录实际索引扫描增量。"""
    before_scans = _og_index_scan_count(adapter, index_name)
    connection = adapter.connect_worker()
    try:
        connection.prepare_threshold = None
        connection.execute("SET plan_cache_mode = force_custom_plan")
        plan = "\n".join(
            row[0] for row in connection.execute("EXPLAIN ANALYZE " + statement, values).fetchall()
        )
        samples = _og_count_samples(connection, statement, values, expected_count, measurements)
    finally:
        connection.close()
    expected_index_scans = measurements + 2 if index_name in plan else 0
    after_scans = (
        _og_wait_index_scan_count(adapter, index_name, before_scans + expected_index_scans)
        if expected_index_scans else _og_index_scan_count(adapter, index_name)
    )
    return {
        "expected_index_scans": expected_index_scans,
        "idx_scan_delta": after_scans - before_scans,
        "plan": plan,
        "samples": samples,
    }


def _og_count_samples(connection, statement, values, expected_count, measurements):
    """测量 count 查询并在每个样本上核对固定结果。"""
    samples = []
    for phase, count in (("warmup", 1), ("measurement", measurements)):
        for _ in range(count):
            started = time.perf_counter()
            actual = int(connection.execute(statement, values).fetchone()[0])
            latency_ms = (time.perf_counter() - started) * 1000
            if actual != expected_count:
                raise RuntimeError("selectivity probe truth mismatch")
            samples.append({"latency_ms": latency_ms, "phase": phase})
    return samples


def _og_index_stats(adapter):
    """采集调优 schema 中每个索引的扫描次数和定义。"""
    connection = adapter.connect_worker()
    try:
        rows = connection.execute(
            "SELECT indexrelname,idx_scan FROM pg_stat_user_indexes WHERE schemaname=%s ORDER BY indexrelname",
            (adapter._schema("og_jsonb"),),
        ).fetchall()
        definitions = connection.execute(
            "SELECT indexname,indexdef FROM pg_indexes WHERE schemaname=%s ORDER BY indexname",
            (adapter._schema("og_jsonb"),),
        ).fetchall()
        return {
            "definitions": {name: definition for name, definition in definitions},
            "idx_scan": {name: int(count) for name, count in rows},
        }
    finally:
        connection.close()


def _og_index_scan_count(adapter, index_name):
    """返回指定调优索引已完成的扫描次数。"""
    connection = adapter.connect_worker()
    try:
        row = connection.execute(
            "SELECT idx_scan FROM pg_stat_user_indexes WHERE schemaname=%s AND indexrelname=%s",
            (adapter._schema("og_jsonb"), index_name),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"missing index statistics: {index_name}")
        return int(row[0])
    finally:
        connection.close()


def _og_wait_index_scan_count(adapter, index_name, minimum, attempts=40, interval_seconds=0.05):
    """等待统计收集器发布至少 minimum 次索引扫描。"""
    latest = None
    for attempt in range(attempts):
        latest = _og_index_scan_count(adapter, index_name)
        if latest >= minimum:
            return latest
        if attempt + 1 < attempts:
            time.sleep(interval_seconds)
    return latest


def _ch_trace_plan(adapter, params):
    """返回包含 MergeTree 索引选择信息的 Trace 查询计划。"""
    connection = adapter.connect_worker()
    try:
        return adapter._request(
            connection,
            "EXPLAIN indexes=1 " + adapter._statement("ch_native", "S05"),
            parameters=adapter._query_parameters("S05", params),
        ).strip()
    finally:
        connection.close()


def _adapter(args, target):
    """为单个目标创建独占 namespace 的引擎适配器。"""
    spec = TARGETS[target]
    namespace = f"{args.namespace}_{spec['suffix']}"
    if spec["engine"] == "opengauss":
        return OpenGaussFourLayoutAdapter(args.host, args.opengauss_port, args.opengauss_container, namespace)
    return ClickHouseFourLayoutAdapter(args.host, args.clickhouse_port, args.clickhouse_container, namespace)


def _run_target(args, target, rows, source_truth, catalog, truth):
    """运行一个调优或基础目标，完成正确性、访问路径与清理门禁。"""
    spec = TARGETS[target]
    adapter = _adapter(args, target)
    layout = spec["layout"]
    result = {"target": target, "profile": spec["profile"], "engine": spec["engine"], "status": "failed"}
    owned = False
    try:
        created = (
            adapter.create_layout(layout, 32, profile=spec["profile"])
            if spec["engine"] == "clickhouse"
            else adapter.create_layout(layout, profile=spec["profile"])
        )
        owned = True
        result["ddl"] = created
        blocks = [rows[index:index + source_truth["block_size"]] for index in range(0, len(rows), source_truth["block_size"])]
        result["ingest"] = {"blocks": [adapter.insert_block(layout, block) for block in blocks]}
        result["maintenance"] = (
            adapter.finish_maintenance(layout, args.maintenance_timeout_seconds, optimize_final=True)
            if spec["engine"] == "clickhouse" else adapter.finish_maintenance(layout)
        )
        if result["maintenance"].get("completed") is False:
            raise RuntimeError("maintenance did not complete")
        query_index_scans_before = (
            _og_index_stats(adapter)["idx_scan"]
            if spec["engine"] == "opengauss" and spec["profile"] == "tuned" else None
        )
        result["query_stage"] = run_query_stage(
            adapter, layout, catalog, truth, args.measurements,
            args.document_page_measurements, workers=2,
        )
        if query_index_scans_before is not None:
            expected_dense_scans = args.measurements + 1
            _og_wait_index_scan_count(
                adapter,
                "analytics_operation_name_idx",
                query_index_scans_before.get("analytics_operation_name_idx", 0) + expected_dense_scans,
            )
            query_index_scans_after = _og_index_stats(adapter)["idx_scan"]
            result["query_stage_index_scans"] = {
                name: query_index_scans_after.get(name, 0) - query_index_scans_before.get(name, 0)
                for name in query_index_scans_after
            }
        result["analysis_recovery"] = adapter.verify_analysis(layout, truth)
        result["raw_recovery"] = adapter.verify_raw(layout, truth)
        if not result["analysis_recovery"]["ok"] or not result["raw_recovery"]["ok"]:
            raise RuntimeError("document recovery gate failed")
        result["storage"] = adapter.collect_storage(layout)
        if spec["engine"] == "opengauss":
            if layout == "og_jsonb":
                evidence = {
                    "dense_natural_plan": _og_plan(adapter, layout, "S02", catalog["parameters"]["S02"]),
                    "failure_btree_plan": _og_plan(adapter, layout, "S03", catalog["parameters"]["S03"]),
                    "trace_plan": _og_plan(adapter, layout, "S05", catalog["parameters"]["S05"]),
                }
                gin = _og_gin_probe(
                    adapter, catalog["parameters"]["S03"], truth["results"]["S03"], args.measurements,
                )
                evidence["failure_gin_plan"] = gin["plan"]
                evidence["gin_samples"] = gin["samples"]
                if spec["profile"] == "tuned":
                    evidence["query_stage_expected_index_scans"] = args.measurements + 1
                    evidence["query_stage_index_scans"] = result["query_stage_index_scans"]
                    dense = _og_dense_forced_probe(
                        adapter, catalog["parameters"]["S02"], truth["results"]["S02"], args.measurements,
                    )
                    evidence["dense_forced_plan"] = dense["plan"]
                    evidence["dense_forced_samples"] = dense["samples"]
                    evidence["dense_forced_idx_scan_delta"] = dense["idx_scan_delta"]
                    evidence["dense_forced_expected_index_scans"] = dense["expected_index_scans"]
                    evidence["selectivity_probes"] = _og_selectivity_probes(
                        adapter, catalog, args.measurements,
                    )
                evidence["indexes"] = _og_index_stats(adapter)
                result["access_evidence"] = evidence
                result["access_gate"] = opengauss_access_gate(evidence) if spec["profile"] == "tuned" else {"ok": True, "scope": "baseline"}
            else:
                result["access_gate"] = {"ok": True, "scope": "text_json_baseline"}
        else:
            if layout == "ch_native":
                evidence = {
                    "ddl": created["ddl"],
                    "dynamic_paths": result["storage"]["paths"]["dynamic_paths"],
                    "path_parts": result["storage"]["paths"]["parts"],
                    "shared_paths": result["storage"]["paths"]["shared_paths"],
                    "trace_plan": _ch_trace_plan(adapter, catalog["parameters"]["S05"]),
                }
                result["access_evidence"] = evidence
                result["access_gate"] = clickhouse_access_gate(evidence, spec["profile"]) if spec["profile"] != "baseline" else {"ok": True, "scope": "baseline"}
            else:
                result["access_gate"] = {"ok": True, "scope": "string_json_baseline"}
        if not result["access_gate"]["ok"]:
            raise RuntimeError(f"access path gate failed: {target}")
        result["status"] = "complete"
        return result
    finally:
        result["cleanup"] = adapter.cleanup(layout) if owned else {"removed": False, "skipped": True}
        if owned and result["cleanup"].get("removed") is not True:
            result["status"] = "failed"


def execute(args):
    """按调优优先顺序运行目标并最后发布 manifest。"""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    targets = parse_targets(args.targets)
    manifest = {
        "format": "agent-trace-json-storage-tuned-matrix-run",
        "format_version": 1,
        "run_id": f"tuned-matrix-{uuid.uuid4().hex}",
        "targets": list(targets),
        "measurements": args.measurements,
        "document_page_measurements": args.document_page_measurements,
        "command": list(getattr(args, "command", [Path(sys.executable).name, *sys.argv])),
    }
    artifacts = {}
    try:
        rows, source_truth, catalog, truth, identity = load_inputs(args.input, args.truth)
        manifest["input"] = identity
        identity_adapters = (_adapter(args, "og_jsonb_tuned"), _adapter(args, "ch_native_tuned"))
        manifest["environment"] = collect_environment(args, identity_adapters)
        for target in targets:
            result = _run_target(args, target, rows, source_truth, catalog, truth)
            artifacts[f"result-{target}.json"] = canonical_bytes(result) + b"\n"
            if result["status"] != "complete" or result["cleanup"].get("removed") is not True:
                raise RuntimeError(f"target failed: {target}")
        manifest["gates"] = {"cleanup": True, "truth": True, "access_paths": True}
        write_manifest_last(output, manifest, artifacts)
    except Exception as error:
        write_failed_manifest(output, manifest, artifacts, error)
        raise


def parse_args():
    """解析正式调优矩阵参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--targets", default=",".join(DEFAULT_TARGETS))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--opengauss-container", default="agent-trace-opengauss-v6")
    parser.add_argument("--opengauss-port", default=15432, type=int)
    parser.add_argument("--clickhouse-container", default="agent-trace-clickhouse-25-12")
    parser.add_argument("--clickhouse-port", default=18123, type=int)
    parser.add_argument("--measurements", default=30, type=int)
    parser.add_argument("--document-page-measurements", default=10, type=int)
    parser.add_argument("--maintenance-timeout-seconds", default=120, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    arguments.command = [Path(sys.executable).name, *sys.argv]
    execute(arguments)
