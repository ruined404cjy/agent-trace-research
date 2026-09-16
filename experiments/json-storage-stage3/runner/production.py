"""组装阶段三正式输入及后续运行可复用的基础工厂。"""

import json
from dataclasses import dataclass
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
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("opengauss_port", "clickhouse_port"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
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
    events = tuple(events)
    main_events = build_workload_events(events, "main")
    main_contract = validate_workload_contract(main_events, "main", "formal")
    main_blocks = _main_blocks(main_events, truth.watermarks)
    main_queries = workload_query_cases(main_events, truth, root, "main")
    return FormalInput(
        root, truth, events, dict(identity), dict(generation), main_events, main_blocks,
        main_queries, main_contract,
    )


def _require_formal_input(formal: FormalInput) -> FormalInput:
    """确保工厂只接受正式 loader 产出的聚合输入对象。"""
    if not isinstance(formal, FormalInput):
        raise ValueError("formal must be a FormalInput")
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
        batch_measurements=5, input_identity=dict(formal.identity), command=tuple(command),
        asset_root=Path(asset_root).resolve(), verified_events=formal.main_events, workload="main",
    )


def part_state_inputs(formal: FormalInput):
    """返回 ClickHouse part-state 运行所需的同一 main blocks 与 queries。"""
    formal = _require_formal_input(formal)
    return formal.main_blocks, formal.main_queries
