#!/usr/bin/env python3
"""执行 ClickHouse 四种有序 part 状态控制并发布物理与查询证据。"""

import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from common import (
    AccessEvidence, AssetStorageEvidence, CleanupResult, QuerySpec, StorageEvidence,
    build_layout_catalog,
)
from run_layout_matrix import QuerySample, QueryTruth, measure_query, write_manifest_atomic


STATE_ORDER = ("fragmented", "merging", "stable", "single_part")
TABLE_METRICS = ("part_count", "marks", "compressed_bytes", "uncompressed_bytes")


@dataclass(frozen=True)
class PartState:
    """保存一个已证明或失败的物理状态及其正式查询证据。"""

    name: str
    controlled_table: str
    predicate_proven: bool
    tables: dict[str, dict[str, object]]
    active_merges: tuple[dict[str, object], ...]
    asset_store: AssetStorageEvidence | None = None
    observations: tuple[dict[str, object], ...] = ()
    query_samples: tuple[QuerySample, ...] = ()
    query_plans: dict[str, str] = field(default_factory=dict)
    query_finish: dict[str, dict[str, object]] = field(default_factory=dict)
    successful_samples: int = 0
    failed_samples: int = 0
    query_finish_count: int = 0
    optimized_targets: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class PartStateRunResult:
    """返回最终 manifest 和固定顺序的四状态证据。"""

    manifest: dict[str, object]
    states: tuple[PartState, ...]


def capture_part_state(adapter, name="snapshot") -> PartState:
    """读取并校验当前布局全部物理写目标的 part、mark、bytes 和 merge。"""
    storage = adapter.collect_storage()
    if not isinstance(storage, StorageEvidence):
        raise ValueError("adapter returned invalid storage evidence")
    catalog = build_layout_catalog(adapter.layout)
    if set(storage.tables) != set(catalog.write_tables):
        raise RuntimeError("part state tables do not match layout write targets")
    tables = {}
    for table in catalog.write_tables:
        raw = storage.tables[table]
        if any(type(raw.get(metric)) is not int or raw[metric] < 0 for metric in TABLE_METRICS):
            raise RuntimeError(f"part state metrics are incomplete: {table}")
        tables[table] = dict(raw)
    merges = tuple(dict(item) for item in storage.merges)
    return PartState(
        name, catalog.list_source, False, tables, merges, asset_store=storage.asset_store,
    )


def _observation(state):
    """提取状态谓词和诊断所需的不可变观测。"""
    return {
        "active_part_counts": {
            table: values["part_count"] for table, values in state.tables.items()
        },
        "tables": {table: dict(values) for table, values in state.tables.items()},
        "active_merges": [dict(item) for item in state.active_merges],
    }


def _validate_wait(timeout_seconds, poll_interval):
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not isinstance(poll_interval, (int, float)) or isinstance(poll_interval, bool) or poll_interval <= 0:
        raise ValueError("poll_interval must be positive")


def _wait_for_state(adapter, name, predicate, timeout_seconds, clock, sleep, poll_interval):
    """轮询到状态谓词成立，超时仍返回完整失败观测。"""
    _validate_wait(timeout_seconds, poll_interval)
    started = clock()
    observations = []
    while True:
        state = capture_part_state(adapter, name)
        observations.append(_observation(state))
        if predicate(state):
            return replace(state, predicate_proven=True, observations=tuple(observations))
        if clock() - started >= timeout_seconds:
            return replace(state, observations=tuple(observations))
        sleep(poll_interval)


def wait_stable(adapter, consecutive_empty=3, timeout_seconds=60,
                clock=time.monotonic, sleep=time.sleep, poll_interval=0.1) -> PartState:
    """等待连续空 merge 且全部 active part 计数不变的自然稳定态。"""
    if not isinstance(consecutive_empty, int) or isinstance(consecutive_empty, bool) or consecutive_empty <= 0:
        raise ValueError("consecutive_empty must be positive")
    _validate_wait(timeout_seconds, poll_interval)
    started = clock()
    observations = []
    previous_counts = None
    stable_count = 0
    while True:
        state = capture_part_state(adapter, "stable")
        observation = _observation(state)
        observations.append(observation)
        counts = observation["active_part_counts"]
        if not state.active_merges:
            stable_count = stable_count + 1 if counts == previous_counts else 1
        else:
            stable_count = 0
        if stable_count >= consecutive_empty:
            return replace(state, predicate_proven=True, observations=tuple(observations))
        if clock() - started >= timeout_seconds:
            return replace(state, observations=tuple(observations))
        previous_counts = counts
        sleep(poll_interval)


def _sample_state(adapter, state, query_cases, samples_per_query):
    """仅在物理谓词成立后执行应用可用样本并核对 QueryFinish。"""
    if not state.predicate_proven:
        raise RuntimeError(f"{state.name} state was not proven")
    samples = tuple(
        measure_query(adapter, query, truth)
        for query, truth in query_cases
        for _ in range(samples_per_query)
    )
    successful_ids = [sample.query_id for sample in samples if sample.status == "success"]
    error = None
    access = AccessEvidence({}, query_finish={})
    try:
        collected = adapter.collect_access_evidence(successful_ids)
        if not isinstance(collected, AccessEvidence):
            raise RuntimeError("invalid QueryFinish evidence")
        access = collected
        if set(access.query_finish) != set(successful_ids) or len(successful_ids) != len(set(successful_ids)):
            raise RuntimeError("QueryFinish does not match successful samples")
        if set(access.plans) != set(successful_ids) or any(
            row.get("type") != "QueryFinish" or int(row.get("exception_code", -1)) != 0
            for row in access.query_finish.values()
        ):
            raise RuntimeError("QueryFinish evidence is invalid")
    except Exception as exception:
        error = str(exception) or type(exception).__name__
    successful = sum(sample.status == "success" for sample in samples)
    failed = len(samples) - successful
    if failed:
        error = f"query sampling failed: {failed} sample(s)"
    return replace(
        state, query_samples=samples, query_plans=dict(access.plans),
        query_finish=dict(access.query_finish), successful_samples=successful,
        failed_samples=failed, query_finish_count=len(access.query_finish), error=error,
    )


def _state_manifest(state):
    value = asdict(state)
    value["active_merges"] = list(state.active_merges)
    return value


def run_part_states(adapter, blocks, query_cases, output: Path, *, samples_per_query=1,
                    consecutive_empty=3, timeout_seconds=60,
                    clock=time.monotonic, sleep=time.sleep, poll_interval=0.1):
    """形成、证明并采样四状态，任何退出路径均恢复 merge 并清理 namespace。"""
    blocks = tuple(list(block) for block in blocks)
    query_cases = tuple(query_cases)
    if not blocks or any(not block for block in blocks):
        raise ValueError("blocks must contain non-empty write blocks")
    if not query_cases or any(
        not isinstance(query, QuerySpec) or not isinstance(truth, QueryTruth)
        for query, truth in query_cases
    ):
        raise ValueError("query_cases must contain QuerySpec and QueryTruth pairs")
    if not isinstance(samples_per_query, int) or isinstance(samples_per_query, bool) or samples_per_query <= 0:
        raise ValueError("samples_per_query must be positive")

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "run-manifest.json"
    catalog = build_layout_catalog(adapter.layout)
    targets = catalog.write_tables
    controlled = catalog.list_source
    manifest = {
        "format": "agent-trace-json-storage-stage3-clickhouse-part-states",
        "format_version": 1,
        "run_id": "jsons3-clickhouse-parts-" + uuid.uuid4().hex[:10],
        "status": "running",
        "layout": adapter.layout,
        "physical_targets": list(targets),
        "controlled_table": controlled,
        "state_order": list(STATE_ORDER),
        "cache_limits": "ordered-control-warm-cache-no-os-cache-drop-not-main-matrix-paired",
        "samples_per_query": samples_per_query,
        "states": [],
    }
    write_manifest_atomic(manifest_path, manifest)
    states = []
    errors = []
    restore_required = False
    restoration = {"attempted": False, "restored": False, "targets": list(targets)}
    cleanup = {"namespace": getattr(adapter, "database", "unknown"), "removed": False}

    def prove_and_sample(state):
        states.append(state)
        if not state.predicate_proven:
            raise RuntimeError(f"{state.name} state was not proven")
        sampled = _sample_state(adapter, state, query_cases, samples_per_query)
        states[-1] = sampled
        if sampled.error:
            raise RuntimeError(sampled.error)

    try:
        manifest["layout_definition"] = adapter.create()
        restore_required = True
        adapter.set_merges(False)
        for block in blocks:
            result = adapter.ingest_block(block)
            expected_watermark = int(block[-1]["ingest_seq"]) + 1
            if result.rows != len(block) or result.watermark != expected_watermark:
                raise RuntimeError("fragmented ingest result mismatch")
            if set(result.watermarks) != set(targets) or any(
                value < expected_watermark for value in result.watermarks.values()
            ):
                raise RuntimeError("fragmented joint watermark mismatch")
            visible = adapter.wait_write_complete(expected_watermark)
            if not visible.completed or set(visible.watermarks) != set(targets) or any(
                value < expected_watermark for value in visible.watermarks.values()
            ):
                raise RuntimeError("fragmented write visibility mismatch")

        prove_and_sample(_wait_for_state(
            adapter, "fragmented",
            lambda state: not state.active_merges and state.tables[controlled]["part_count"] >= 2,
            timeout_seconds, clock, sleep, poll_interval,
        ))

        adapter.set_merges(True)
        prove_and_sample(_wait_for_state(
            adapter, "merging",
            lambda state: any(merge.get("table") == controlled for merge in state.active_merges),
            timeout_seconds, clock, sleep, poll_interval,
        ))

        prove_and_sample(wait_stable(
            adapter, consecutive_empty, timeout_seconds, clock, sleep, poll_interval,
        ))

        optimized = tuple(adapter.force_single_part())
        if optimized != tuple(targets):
            raise RuntimeError("single-part control targets do not match layout write targets")
        single = _wait_for_state(
            adapter, "single_part",
            lambda state: (
                not state.active_merges
                and state.tables[controlled]["part_count"] == 1
                and all(values["part_count"] <= 1 for values in state.tables.values())
            ),
            timeout_seconds, clock, sleep, poll_interval,
        )
        prove_and_sample(replace(single, optimized_targets=optimized))
    except Exception as exception:
        errors.append(str(exception) or type(exception).__name__)
    finally:
        restoration["attempted"] = restore_required
        if restore_required:
            try:
                adapter.set_merges(True)
                restoration["restored"] = True
            except Exception as exception:
                restoration["error"] = str(exception) or type(exception).__name__
                errors.append("merge restoration failed: " + restoration["error"])
        try:
            cleanup_result = adapter.cleanup()
            if not isinstance(cleanup_result, CleanupResult):
                raise RuntimeError("adapter returned invalid cleanup evidence")
            cleanup = asdict(cleanup_result)
            if not cleanup_result.removed:
                errors.append("cleanup was not confirmed")
        except Exception as exception:
            cleanup = {
                "namespace": getattr(adapter, "database", "unknown"), "removed": False,
                "error": str(exception) or type(exception).__name__,
            }
            errors.append("cleanup failed: " + cleanup["error"])

    manifest["states"] = [_state_manifest(state) for state in states]
    manifest["restoration"] = restoration
    manifest["cleanup"] = cleanup
    manifest["status"] = "failed" if errors else "complete"
    if errors:
        manifest["errors"] = errors
    write_manifest_atomic(manifest_path, manifest)
    if errors:
        raise RuntimeError("; ".join(errors))
    return PartStateRunResult(manifest, tuple(states))
