#!/usr/bin/env python3
"""比较 ClickHouse 后台 merge 对载入与查询阶段的影响。"""

import argparse
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from clickhouse_four_layout import ClickHouseFourLayoutAdapter
from run_four_layouts import load_inputs, run_query_stage
from supplement_common import canonical_bytes, write_failed_manifest, write_manifest_last


TARGETS = {
    "ch_string_active": ("ch_string", "active"),
    "ch_native_active": ("ch_native", "active"),
    "ch_string_paused": ("ch_string", "paused"),
    "ch_native_paused": ("ch_native", "paused"),
}
ROUND_ORDERS = (
    ("ch_string_active", "ch_native_active", "ch_string_paused", "ch_native_paused"),
    ("ch_native_paused", "ch_string_paused", "ch_native_active", "ch_string_active"),
    ("ch_string_paused", "ch_native_paused", "ch_string_active", "ch_native_active"),
)


@contextmanager
def paused_merges(adapter, layout):
    """在作用域内暂停当前布局两张表的 merge，并保证退出时恢复。"""
    adapter.set_merges(layout, False)
    try:
        yield
    finally:
        adapter.set_merges(layout, True)


def fragmented_gate(storage, block_count):
    """要求暂停 merge 后每次 INSERT 在两张表各留下一个 active part。"""
    actual = {
        table: int(storage["tables"][table]["part_count"])
        for table in ("analytics", "raw")
    }
    return {
        "actual": actual,
        "expected": block_count,
        "ok": all(value == block_count for value in actual.values()),
    }


def _observe_merges(adapter, layout, operation, interval_seconds=0.01):
    """并行轮询 active merge 数并执行给定操作。"""
    stop = threading.Event()
    observations = []

    def observe():
        while not stop.is_set():
            observations.append({
                "active_merges": adapter._merge_backlog(layout),
                "elapsed_ms": (time.perf_counter() - started) * 1000,
            })
            stop.wait(interval_seconds)

    started = time.perf_counter()
    thread = threading.Thread(target=observe, name="json-s2sup-merge-observer")
    thread.start()
    try:
        value = operation()
    finally:
        stop.set()
        thread.join()
    observations.append({
        "active_merges": adapter._merge_backlog(layout),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
    })
    return value, {
        "max_active_merges": max(item["active_merges"] for item in observations),
        "observations": observations,
        "wall_ms": (time.perf_counter() - started) * 1000,
    }


def _insert_all(adapter, layout, blocks):
    """按冻结顺序载入全部 block。"""
    return {"blocks": [adapter.insert_block(layout, block) for block in blocks]}


def _query_stage(adapter, layout, catalog, truth, measurements, document_measurements):
    """在当前 part 状态执行一组带 truth 与 QueryFinish 门禁的查询。"""
    storage_before = adapter.collect_storage(layout)
    query = run_query_stage(
        adapter, layout, catalog, truth, measurements, document_measurements, workers=2,
    )
    return {
        "query_stage": query,
        "storage_before": storage_before,
        "storage": adapter.collect_storage(layout),
    }


def _maintenance_wall_ms(maintenance):
    """汇总一次 finish_maintenance 的实际等待与 OPTIMIZE 时间。"""
    before = maintenance.get("before_optimize", {})
    return (
        float(before.get("waited_seconds", 0.0)) * 1000
        + sum(float(value) for value in maintenance.get("optimize_final_ms", {}).values())
        + float(maintenance.get("waited_seconds", 0.0)) * 1000
    )


def _run_target(args, target, rows, source_truth, catalog, truth):
    """运行单个 layout/merge-mode 目标并清理自有 database。"""
    layout, mode = TARGETS[target]
    adapter = ClickHouseFourLayoutAdapter(
        args.host,
        args.clickhouse_port,
        args.clickhouse_container,
        f"{args.namespace}_{'str' if layout == 'ch_string' else 'nat'}_{mode}",
    )
    result = {"layout": layout, "mode": mode, "status": "failed", "target": target}
    owned = False
    try:
        result["ddl"] = adapter.create_layout(layout, 32)
        owned = True
        blocks = [
            rows[index:index + source_truth["block_size"]]
            for index in range(0, len(rows), source_truth["block_size"])
        ]
        if mode == "paused":
            with paused_merges(adapter, layout):
                result["ingest"] = _insert_all(adapter, layout, blocks)
                fragmented = adapter.collect_storage(layout)
                result["fragmented_gate"] = fragmented_gate(fragmented, source_truth["block_count"])
                if not result["fragmented_gate"]["ok"]:
                    raise RuntimeError("fragmented part topology mismatch")
                result["stages"] = {
                    "fragmented": _query_stage(
                        adapter, layout, catalog, truth,
                        args.measurements, args.document_page_measurements,
                    )
                }
            background_stage, background_observer = _observe_merges(
                adapter,
                layout,
                lambda: _query_stage(
                    adapter, layout, catalog, truth,
                    args.measurements, args.document_page_measurements,
                ),
            )
            background_stage["merge_observer"] = background_observer
            result["stages"]["background_merging"] = background_stage
            stable_maintenance = adapter.finish_maintenance(
                layout, args.maintenance_timeout_seconds, optimize_final=False,
            )
            if not stable_maintenance.get("completed"):
                raise RuntimeError("background merge did not become idle")
            result["stages"]["background_stable"] = _query_stage(
                adapter, layout, catalog, truth,
                args.measurements, args.document_page_measurements,
            )
            final_maintenance = adapter.finish_maintenance(
                layout, args.maintenance_timeout_seconds, optimize_final=True,
            )
            if not final_maintenance.get("completed"):
                raise RuntimeError("OPTIMIZE FINAL did not complete")
            result["stages"]["single_part"] = _query_stage(
                adapter, layout, catalog, truth,
                args.measurements, args.document_page_measurements,
            )
            result["maintenance"] = {
                "background": stable_maintenance,
                "background_wait_ms": _maintenance_wall_ms(stable_maintenance),
                "final": final_maintenance,
                "optimize_final_ms": _maintenance_wall_ms(final_maintenance),
            }
        else:
            result["ingest"], result["ingest_merge_observer"] = _observe_merges(
                adapter, layout, lambda: _insert_all(adapter, layout, blocks),
            )
            result["storage_after_ingest"] = adapter.collect_storage(layout)
            stable_maintenance = adapter.finish_maintenance(
                layout, args.maintenance_timeout_seconds, optimize_final=False,
            )
            if not stable_maintenance.get("completed"):
                raise RuntimeError("active merge did not become idle")
            result["maintenance"] = {
                "background": stable_maintenance,
                "background_wait_ms": _maintenance_wall_ms(stable_maintenance),
                "optimize_final_ms": 0.0,
            }
            result["stages"] = {
                "background_stable": _query_stage(
                    adapter, layout, catalog, truth,
                    args.measurements, args.document_page_measurements,
                )
            }
        result["analysis_recovery"] = adapter.verify_analysis(layout, truth)
        result["raw_recovery"] = adapter.verify_raw(layout, truth)
        if not result["analysis_recovery"]["ok"] or not result["raw_recovery"]["ok"]:
            raise RuntimeError("document recovery gate failed")
        result["status"] = "complete"
        return result
    finally:
        result["cleanup"] = adapter.cleanup(layout) if owned else {"removed": False, "skipped": True}
        if owned and result["cleanup"].get("removed") is not True:
            result["status"] = "failed"


def execute(args):
    """按轮次顺序执行四个目标，并在全部门禁通过后发布 manifest。"""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "run-manifest.json").unlink(missing_ok=True)
    if args.round not in range(1, len(ROUND_ORDERS) + 1):
        raise ValueError("round must be 1, 2, or 3")
    order = ROUND_ORDERS[args.round - 1]
    manifest = {
        "format": "agent-trace-clickhouse-merge-performance",
        "format_version": 1,
        "round": args.round,
        "run_id": f"merge-performance-{uuid.uuid4().hex}",
        "targets": list(order),
    }
    artifacts = {}
    try:
        rows, source_truth, catalog, truth, identity = load_inputs(args.input, args.truth)
        manifest["input"] = identity
        for target in order:
            result = _run_target(args, target, rows, source_truth, catalog, truth)
            content = canonical_bytes(result) + b"\n"
            artifacts[f"result-{target}.json"] = content
            if result["status"] != "complete" or result["cleanup"].get("removed") is not True:
                raise RuntimeError(f"merge target failed: {target}")
        manifest["gates"] = {"cleanup": True, "truth": True, "topology": True}
        write_manifest_last(output, manifest, artifacts)
    except Exception as error:
        write_failed_manifest(output, manifest, artifacts, error)
        raise


def parse_args():
    """解析 ClickHouse merge 性能探针参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--clickhouse-container", default="agent-trace-clickhouse-25-12")
    parser.add_argument("--clickhouse-port", default=18123, type=int)
    parser.add_argument("--measurements", default=10, type=int)
    parser.add_argument("--document-page-measurements", default=5, type=int)
    parser.add_argument("--maintenance-timeout-seconds", default=120, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    arguments.command = [Path(sys.executable).name, *sys.argv]
    execute(arguments)
