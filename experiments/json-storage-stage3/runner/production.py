"""组装阶段三正式输入及后续运行可复用的基础工厂。"""

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from assets import LocalAssetStore
from common import BlockResult, LAYOUTS, MaintenanceResult, QuerySpec, TruthCatalog, canonical_digest
from run_layout_matrix import (
    QueryTruth,
    RunConfig,
    build_workload_events,
    latin_square,
    load_run_input,
    validate_formal_contract,
    validate_watermark_keys,
    validate_workload_contract,
    workload_query_cases,
)
from run_interference import (
    DeadlineTarget,
    FIXED_PHASES,
    build_query_target,
    fixed_phase_schedules,
)


_FORMAL_INPUT_SEAL = object()


def _freeze(value):
    """递归复制可变容器并返回不继承 dict 的只读映射。"""
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    """为既有 runner 的可变配置边界建立隔离的普通容器副本。"""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_thaw(item) for item in value)
    return value


def _freeze_truth(truth: TruthCatalog) -> TruthCatalog:
    """复制 TruthCatalog 内全部可变映射，保持公共 dataclass 类型。"""
    return TruthCatalog(
        seed=truth.seed, source=_freeze(truth.source), record_count=truth.record_count,
        block_size=truth.block_size, block_count=truth.block_count,
        watermarks=_freeze(truth.watermarks), identity_sha256=truth.identity_sha256,
        query_window=_freeze(truth.query_window), payloads=_freeze(truth.payloads),
        cohorts=_freeze(truth.cohorts), representative_traces=_freeze(truth.representative_traces),
        detail_samples=_freeze(truth.detail_samples),
    )


def _freeze_queries(query_cases):
    """冻结现有 query helper 返回的参数与独立 truth 行集。"""
    frozen_cases = []
    for query, query_truth in query_cases:
        frozen_query = QuerySpec(query.kind, dict(query.parameters))
        object.__setattr__(frozen_query, "parameters", _freeze(query.parameters))
        frozen_cases.append((
            frozen_query,
            QueryTruth(query_truth.scenario, _freeze(query_truth.rows)),
        ))
    return tuple(frozen_cases)


@dataclass(frozen=True)
class EngineEndpoints:
    """保存两个数据库 engine 的固定本地连接端点。"""

    opengauss_host: str = "127.0.0.1"
    opengauss_port: int = 15432
    opengauss_container: str = "agent-trace-opengauss-v6"
    clickhouse_host: str = "127.0.0.1"
    clickhouse_port: int = 18123
    clickhouse_container: str = "agent-trace-clickhouse-25-12"

    def __post_init__(self):
        """拒绝空连接身份和非正端口，避免工厂产生歧义配置。"""
        for name in (
            "opengauss_host", "opengauss_container",
            "clickhouse_host", "clickhouse_container",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or not value.isprintable()
                or value.strip() != value
                or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                       for character in value)
            ):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("opengauss_port", "clickhouse_port"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65_535:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class FormalInput:
    """保存正式冻结输入、主 workload 投影及派生的只读运行数据。"""

    root: Path
    truth: TruthCatalog
    events: tuple[dict[str, object], ...]
    identity: Mapping[str, object]
    generation: Mapping[str, object]
    main_events: tuple[dict[str, object], ...]
    main_blocks: tuple[tuple[dict[str, object], ...], ...]
    main_queries: tuple[tuple[QuerySpec, QueryTruth], ...]
    main_contract: Mapping[str, object]
    _validation_seal: object | None = field(default=None, init=False, repr=False, compare=False)


def _load_generation(root: Path) -> dict[str, object]:
    """读取完整 generation manifest object，保留既有正式契约验证。"""
    try:
        generation = json.loads((root / "generation-manifest.json").read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid generation manifest") from error
    if not isinstance(generation, dict):
        raise ValueError("generation manifest must be an object")
    if generation.get("status") != "complete":
        raise ValueError("input generation is incomplete")
    return generation


def _main_blocks(events, watermarks):
    """按 truth 水位切分主 workload，逐块核对边界和 ingest 顺序。"""
    blocks = []
    previous = 0
    for watermark in watermarks:
        if (
            not isinstance(watermark, int)
            or isinstance(watermark, bool)
            or watermark <= previous
            or watermark > len(events)
        ):
            raise ValueError("formal watermarks must be strictly increasing")
        block = tuple(events[previous:watermark])
        if not block or int(block[-1]["ingest_seq"]) + 1 != watermark:
            raise ValueError("formal watermark block boundary mismatch")
        blocks.append(block)
        previous = watermark
    if previous != len(events):
        raise ValueError("formal watermarks do not cover events")
    return tuple(blocks)


def load_formal_input(root: Path) -> FormalInput:
    """加载并门禁正式输入，返回 main workload 的不可变派生数据。"""
    root = Path(root).resolve()
    generation = _load_generation(root)
    truth, events, identity = load_run_input(root)
    if identity.get("kind") != "formal":
        raise ValueError("formal input identity is required")
    validate_formal_contract(generation, truth, 30, 5)
    truth = _freeze_truth(truth)
    events = _freeze(tuple(events))
    main_events = build_workload_events(events, "main")
    main_contract = validate_workload_contract(main_events, "main", "formal")
    main_events = _freeze(main_events)
    main_blocks = _main_blocks(main_events, truth.watermarks)
    main_queries = _freeze_queries(workload_query_cases(main_events, truth, root, "main"))
    formal = FormalInput(
        root, truth, events, _freeze(identity), _freeze(generation), main_events, main_blocks,
        main_queries, _freeze(main_contract),
    )
    object.__setattr__(formal, "_validation_seal", _FORMAL_INPUT_SEAL)
    return formal


def _require_formal_input(formal: FormalInput) -> FormalInput:
    """确保工厂只接受正式 loader 产出的聚合输入对象。"""
    if not isinstance(formal, FormalInput) or formal._validation_seal is not _FORMAL_INPUT_SEAL:
        raise ValueError("formal must be a validated FormalInput")
    return formal


def create_adapter(engine: str, layout: str, namespace: str, formal: FormalInput,
                   asset_root: Path | None, endpoints: EngineEndpoints):
    """构造一个真实 adapter，但不连接数据库或创建 namespace。"""
    formal = _require_formal_input(formal)
    if not isinstance(endpoints, EngineEndpoints):
        raise ValueError("endpoints must be EngineEndpoints")
    if engine not in {"opengauss", "clickhouse"}:
        raise ValueError(f"unsupported engine: {engine}")
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")

    asset_store = None
    if layout == "asset_ref":
        if asset_root is None:
            raise ValueError("asset_ref requires asset_root")
        canonical_asset_root = Path(asset_root).resolve()
        if canonical_asset_root.exists() and (
            not canonical_asset_root.is_dir() or any(canonical_asset_root.iterdir())
        ):
            raise ValueError("asset_root must be empty")
        asset_store = LocalAssetStore(canonical_asset_root)

    if engine == "opengauss":
        from opengauss import OpenGaussAdapter
        return OpenGaussAdapter(
            endpoints.opengauss_host, endpoints.opengauss_port,
            endpoints.opengauss_container, namespace, layout, formal.root, asset_store,
        )
    from clickhouse import ClickHouseAdapter
    return ClickHouseAdapter(
        endpoints.clickhouse_host, endpoints.clickhouse_port,
        endpoints.clickhouse_container, namespace, layout, formal.root, asset_store,
    )


def candidate_config(formal: FormalInput, output: Path, asset_root: Path,
                     command: tuple[str, ...]) -> RunConfig:
    """组装固定首轮 ClickHouse asset_ref 正式候选配置。"""
    formal = _require_formal_input(formal)
    return RunConfig(
        input_root=formal.root, output=Path(output), engine="clickhouse", layout="asset_ref",
        round_index=0, round_order=latin_square()[0], measurements=30,
        batch_measurements=5, input_identity=_thaw(formal.identity), command=tuple(command),
        asset_root=Path(asset_root).resolve(), verified_events=_thaw(formal.main_events), workload="main",
    )


def part_state_inputs(formal: FormalInput):
    """返回 ClickHouse part-state 运行所需的同一 main blocks 与 queries。"""
    formal = _require_formal_input(formal)
    return formal.main_blocks, formal.main_queries


def _interference_queries(formal: FormalInput):
    """选择并验证固定 interference query catalog。"""
    expected_kinds = {
        "list:first": "list",
        "preview:first": "preview",
        "detail:text_2m": "detail",
        "trace:p95": "trace",
        "batch:main": "batch",
    }
    selected = {}
    catalog = []
    for case in formal.main_queries:
        if (
            not isinstance(case, tuple) or len(case) != 2
            or not isinstance(case[0], QuerySpec) or not isinstance(case[1], QueryTruth)
        ):
            raise ValueError("main query catalog contains an invalid query case")
        query, truth = case
        catalog.append({
            "scenario": truth.scenario,
            "kind": query.kind,
            "parameters": _thaw(query.parameters),
        })
        if truth.scenario not in expected_kinds:
            continue
        if truth.scenario in selected:
            raise ValueError(f"duplicate fixed query scenario: {truth.scenario}")
        if query.kind != expected_kinds[truth.scenario]:
            raise ValueError(f"fixed query kind mismatch: {truth.scenario}")
        selected[truth.scenario] = case
    if set(selected) != set(expected_kinds):
        raise ValueError("fixed query scenarios are incomplete")
    return selected, canonical_digest(catalog)


def _continuous_blocks(formal: FormalInput):
    """按冻结窗口规则选择 continuous ingest 的完整 main blocks。"""
    window = formal.truth.query_window
    selected = []
    for index, block in enumerate(formal.main_blocks):
        outside_window = all(not (
            row["project_id"] == window["project_id"]
            and window["start_time"] <= row["start_time"] < window["end_time"]
        ) for row in block)
        if (
            len(block) == formal.truth.block_size
            and outside_window
            and any(row["payload_path"] is not None for row in block)
        ):
            selected.append((index, block))
    if len(selected) != 45 or selected[0][0] != 108:
        raise ValueError("continuous ingest block selection does not match formal input")
    return tuple(selected)


def _phase_seed(phase, seed):
    """验证 fixed interference runner 回传的 phase 与 seed。"""
    if phase not in FIXED_PHASES:
        raise ValueError("phase must be one of FIXED_PHASES")
    if seed != 20260907:
        raise ValueError("interference seed must be 20260907")


def _validate_block_result(result, layout, rows, watermark):
    """核对 adapter 单块写入返回的真实联合水位证据。"""
    if not isinstance(result, BlockResult):
        raise RuntimeError("adapter ingest did not return BlockResult")
    if result.rows != rows or result.watermark != watermark:
        raise RuntimeError(f"block result mismatch at watermark {watermark}")
    validate_watermark_keys(layout, result.watermarks, watermark)
    if any(value != watermark for value in result.watermarks.values()):
        raise RuntimeError(f"joint watermark mismatch at {watermark}")
    return result


def interference_factories(
    formal: FormalInput,
    layout: str,
    asset_root: Path | None,
    endpoints: EngineEndpoints,
) -> tuple[Callable, Callable, dict[str, object]]:
    """构造 ClickHouse fixed interference factories 与静态输入 metadata。"""
    formal = _require_formal_input(formal)
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported layout: {layout}")
    if not isinstance(endpoints, EngineEndpoints):
        raise ValueError("endpoints must be EngineEndpoints")
    if layout == "asset_ref" and asset_root is None:
        raise ValueError("asset_ref requires asset_root")

    canonical_asset_root = Path(asset_root).resolve() if layout == "asset_ref" else None
    queries, query_catalog_digest = _interference_queries(formal)
    eligible = _continuous_blocks(formal)
    eligible_indices = [index for index, _ in eligible]
    eligible_digests = [
        canonical_digest([_thaw(row) for row in block]) for _, block in eligible
    ]
    query_scenarios = {
        "list": "list:first",
        "preview": "preview:first",
        "detail_2m": "detail:text_2m",
        "trace_long": "trace:p95",
        "batch_loop": "batch:main",
    }
    metadata = {
        "selection_rules": {
            "full_block_rows": formal.truth.block_size,
            "outside_query_window": {
                "project_id": formal.truth.query_window["project_id"],
                "start_time": formal.truth.query_window["start_time"],
                "end_time": formal.truth.query_window["end_time"],
            },
            "requires_main_payload": True,
            "query_scenarios": dict(query_scenarios),
        },
        "eligible_block_count": len(eligible),
        "eligible_block_indices": eligible_indices,
        "eligible_block_sha256": eligible_digests,
        "cyclic_replay": True,
        "main_query_catalog_sha256": query_catalog_digest,
        "preload_block_count": len(formal.main_blocks),
        "block_size": formal.truth.block_size,
        "final_watermark": formal.truth.record_count,
        "seed": formal.truth.seed,
    }
    used_namespaces = set()

    def adapter_factory(namespace, phase, seed):
        """为一个 fixed phase 构造尚未 create 的 ClickHouse adapter。"""
        _phase_seed(phase, seed)
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace must be a non-empty string")
        if namespace in used_namespaces:
            raise ValueError("namespace cannot be reused")
        namespace_asset_root = (
            canonical_asset_root / namespace if canonical_asset_root is not None else None
        )
        if namespace_asset_root is not None and namespace_asset_root.exists():
            raise ValueError("asset namespace must be fresh")
        used_namespaces.add(namespace)
        return create_adapter(
            "clickhouse", layout, namespace, formal, namespace_asset_root, endpoints,
        )

    def targets_factory(adapter, phase, seed):
        """完成一次 phase 预载后返回固定查询与 continuous targets。"""
        _phase_seed(phase, seed)
        if getattr(adapter, "layout", None) != layout:
            raise ValueError("adapter layout does not match interference factory")
        for block, watermark in zip(formal.main_blocks, formal.truth.watermarks):
            submitted = [_thaw(row) for row in block]
            result = adapter.ingest_block(submitted)
            _validate_block_result(result, layout, len(block), watermark)
            visible = adapter.wait_write_complete(watermark)
            if not isinstance(visible, MaintenanceResult):
                raise RuntimeError("write completion did not return MaintenanceResult")
            validate_watermark_keys(layout, visible.watermarks, watermark)
            if any(value != watermark for value in visible.watermarks.values()):
                raise RuntimeError(f"joint watermark mismatch at {watermark}")
            if not visible.completed:
                raise RuntimeError(f"joint watermark incomplete at {watermark}")
        ready = adapter.wait_query_ready(60)
        if not isinstance(ready, MaintenanceResult):
            raise RuntimeError("query readiness did not return MaintenanceResult")
        validate_watermark_keys(layout, ready.watermarks, formal.truth.record_count)
        if any(value != formal.truth.record_count for value in ready.watermarks.values()):
            raise RuntimeError("final joint watermark mismatch")
        if not ready.completed:
            raise RuntimeError("query readiness or final joint watermark incomplete")

        targets = {}
        for stream in ("list", "preview"):
            query, truth = queries[query_scenarios[stream]]
            targets[stream] = build_query_target(adapter, query, truth)
        if phase.name in ("detail_2m", "trace_long", "batch_loop"):
            query, truth = queries[query_scenarios[phase.name]]
            targets[phase.name] = build_query_target(adapter, query, truth)
        elif phase.name == "continuous_ingest":
            position = 0

            def continuous_target(deadline, cancellation):
                nonlocal position
                if cancellation.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("continuous ingest deadline expired before execution")
                _, block = eligible[position]
                submitted = [_thaw(row) for row in block]
                watermark = int(block[-1]["ingest_seq"]) + 1
                result = adapter.ingest_block(submitted)
                _validate_block_result(result, layout, len(block), watermark)
                position = (position + 1) % len(eligible)
                return result

            targets[phase.name] = DeadlineTarget(continuous_target)
        if set(targets) != set(fixed_phase_schedules(phase, measurement=True)):
            raise RuntimeError("phase targets do not match fixed schedules")
        return targets

    return adapter_factory, targets_factory, metadata
