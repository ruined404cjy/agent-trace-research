"""组装阶段三正式输入及后续运行可复用的基础工厂。"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from assets import LocalAssetStore
from common import LAYOUTS, QuerySpec, TruthCatalog
from run_layout_matrix import (
    QueryTruth,
    RunConfig,
    build_workload_events,
    latin_square,
    load_run_input,
    validate_formal_contract,
    validate_workload_contract,
    workload_query_cases,
)


_FORMAL_INPUT_SEAL = object()


class _FrozenDict(dict):
    """保留 dict 消费接口的递归只读映射。"""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("formal input is read-only")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze(value):
    """递归复制可变容器，保留现有 dict 和 tuple 读取接口。"""
    if isinstance(value, _FrozenDict):
        return value
    if isinstance(value, dict):
        return _FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
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
    return tuple(
        (
            QuerySpec(query.kind, _freeze(query.parameters)),
            QueryTruth(query_truth.scenario, _freeze(query_truth.rows)),
        )
        for query, query_truth in query_cases
    )


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
    identity: dict[str, object]
    generation: dict[str, object]
    main_events: tuple[dict[str, object], ...]
    main_blocks: tuple[tuple[dict[str, object], ...], ...]
    main_queries: tuple[tuple[QuerySpec, QueryTruth], ...]
    main_contract: dict[str, object]
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
        batch_measurements=5, input_identity=formal.identity, command=tuple(command),
        asset_root=Path(asset_root).resolve(), verified_events=formal.main_events, workload="main",
    )


def part_state_inputs(formal: FormalInput):
    """返回 ClickHouse part-state 运行所需的同一 main blocks 与 queries。"""
    formal = _require_formal_input(formal)
    return formal.main_blocks, formal.main_queries
