"""组装阶段三正式输入及后续运行可复用的基础工厂。"""

import hashlib
import json
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from assets import AssetError, AssetRecord, AssetReference, LocalAssetStore
from clickhouse import ClickHouseAdapter, clickhouse_timestamp
from common import BlockResult, LAYOUTS, MaintenanceResult, QuerySpec, TruthCatalog, canonical_digest
from opengauss import OpenGaussAdapter
from run_asset_failures import FailureFixture
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
    """保存数据库 engine 的固定本地连接端点。"""

    opengauss_host: str = "127.0.0.1"
    opengauss_port: int = 15432
    opengauss_container: str = "agent-trace-opengauss-v6"
    clickhouse_host: str = "127.0.0.1"
    clickhouse_port: int = 18123
    clickhouse_container: str = "agent-trace-clickhouse-25-12"
    xstore_host: str = "127.0.0.1"
    xstore_port: int = 29000
    xstore_container: str = ""
    deployment: str = "container"

    def __post_init__(self):
        """拒绝空连接身份、非正端口和未知部署模式，避免工厂产生歧义配置。"""
        if self.deployment not in {"container", "native"}:
            raise ValueError("deployment must be container or native")
        required = ["opengauss_host", "opengauss_container", "clickhouse_host", "xstore_host"]
        # 原生包部署没有容器名，容器身份由 run 侧的原生包证据替代。
        if self.deployment == "container":
            required.append("clickhouse_container")
        for name in required:
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
        for name in ("opengauss_port", "clickhouse_port", "xstore_port"):
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
    if engine not in {"opengauss", "clickhouse", "xstore"}:
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
    if engine == "xstore":
        from xstore import XStoreAdapter
        return XStoreAdapter(
            endpoints.xstore_host, endpoints.xstore_port,
            endpoints.xstore_container, namespace, layout, formal.root, asset_store,
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
        asset_root=Path(asset_root).resolve(), verified_events=_thaw(formal.events), workload="main",
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
    engine: str = "clickhouse",
) -> tuple[Callable, Callable, dict[str, object]]:
    """构造 fixed interference factories 与静态输入 metadata。"""
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
        """为一个 fixed phase 构造尚未 create 的 adapter。"""
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
            engine, layout, namespace, formal, namespace_asset_root, endpoints,
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
            issued = 0

            def continuous_target(deadline, cancellation):
                nonlocal position, issued
                if cancellation.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError("continuous ingest deadline expired before execution")
                _, block = eligible[position]
                # 预载已写入这些 block；行存以 event_id 为主键，重放行按轮次改用新标识，
                # 其余列与水位所用的 ingest_seq 保持原值，前台窗口外的行不影响前台 truth。
                cycle = issued // len(eligible) + 1
                submitted = [{**_thaw(row), "event_id": f"{row['event_id']}#replay-{cycle}"}
                             for row in block]
                watermark = int(block[-1]["ingest_seq"]) + 1
                result = adapter.ingest_block(submitted)
                _validate_block_result(result, layout, len(block), watermark)
                position = (position + 1) % len(eligible)
                issued += 1
                return result

            targets[phase.name] = DeadlineTarget(continuous_target)
        if set(targets) != set(fixed_phase_schedules(phase, measurement=True)):
            raise RuntimeError("phase targets do not match fixed schedules")
        return targets

    return adapter_factory, targets_factory, metadata


class OpenGaussAssetFaultControl:
    """封装真实 openGauss asset_ref adapter 的故障控制与只读观测面。"""

    def __init__(self, adapter: OpenGaussAdapter):
        if (
            not isinstance(adapter, OpenGaussAdapter)
            or adapter.layout != "asset_ref"
            or not isinstance(adapter.asset_store, LocalAssetStore)
        ):
            raise ValueError("control requires an openGauss asset_ref adapter")
        self.adapter = adapter
        self.store = adapter.asset_store

    def create(self) -> dict[str, object]:
        """创建 control 持有的物理 schema。"""
        return self.adapter.create()

    def cleanup(self):
        """清理 control 持有的物理 schema。"""
        return self.adapter.cleanup()

    def cleanup_targets(self) -> tuple[str, ...]:
        """返回 runner 可核对的物理 schema cleanup identity。"""
        return (self.adapter.schema,)

    def get_available(self, asset_id: str) -> AssetRecord | None:
        """读取指定 asset 的真实 catalog 状态。"""
        return self.adapter.get_available(asset_id)

    def event_visible(self, reference: AssetReference) -> bool:
        """从事件表核对完整 Asset 引用是否可见。"""
        if not isinstance(reference, AssetReference):
            raise ValueError("reference must be AssetReference")
        connection = self.adapter.connect_worker()
        try:
            row = connection.execute(
                f"SELECT EXISTS(SELECT 1 FROM {self.adapter.schema}.events_analytics "
                "WHERE asset_id=%s AND content_type=%s AND encoding=%s "
                "AND content_length=%s AND preview=%s AND sha256=%s)",
                (
                    reference.asset_id, reference.content_type, reference.encoding,
                    reference.content_length, reference.preview, reference.asset_id,
                ),
            ).fetchone()
            return bool(row[0])
        finally:
            connection.close()

    def reachable_paths(self) -> set[Path]:
        """通过 event 与 catalog 的真实 JOIN 返回可达对象路径。"""
        connection = self.adapter.connect_worker()
        try:
            rows = connection.execute(
                f"SELECT DISTINCT a.storage_path FROM {self.adapter.schema}.events_analytics e "
                f"JOIN {self.adapter.schema}.assets a ON a.asset_id=e.asset_id"
            ).fetchall()
            return {Path(row[0]) for row in rows}
        finally:
            connection.close()

    @staticmethod
    def _event_values(fixture: FailureFixture) -> tuple[object, ...]:
        """将故障 fixture 转为固定且完整的 event row。"""
        record = fixture.record
        return (
            0, record.event_id, record.trace_id, record.event_id + ":span", None,
            record.project_id, record.start_time, "2030-01-01T00:00:00.001Z", 1,
            "llm", "asset_failure", "ERROR", record.cohort, record.profile,
            record.content_type, record.encoding, record.content_length,
            record.preview, record.sha256, fixture.reference.asset_id,
        )

    def _insert_fixture(self, fixture: FailureFixture, status: str) -> None:
        """在单个事务中插入 catalog 行和对应 event 引用。"""
        asset_id = fixture.reference.asset_id
        path = self.store.object_path(asset_id)
        connection = self.adapter.connect_worker()
        try:
            try:
                with connection.transaction():
                    asset_cursor = connection.execute(
                        f"INSERT INTO {self.adapter.schema}.assets("
                        "asset_id,sha256,content_type,encoding,content_length,storage_path,"
                        "status,error_category) VALUES (%s,%s,%s,%s,%s,%s,%s,NULL)",
                        (
                            asset_id, asset_id, fixture.record.content_type,
                            fixture.record.encoding, fixture.record.content_length, str(path), status,
                        ),
                    )
                    event_cursor = connection.execute(
                        f"INSERT INTO {self.adapter.schema}.events_analytics("
                        "ingest_seq,event_id,trace_id,span_id,parent_span_id,project_id,"
                        "start_time,end_time,duration_ms,span_type,framework,level,cohort,profile,"
                        "content_type,encoding,content_length,preview,sha256,asset_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        self._event_values(fixture),
                    )
                    if asset_cursor.rowcount != 1 or event_cursor.rowcount != 1:
                        raise RuntimeError("fixture insert did not affect exactly one row")
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def prepare_available(self, fixture: FailureFixture) -> None:
        """先发布 fixture bytes，再原子插入 available catalog 与 event。"""
        stored = self.store.publish_bytes(fixture.reference.asset_id, fixture.payload)
        if stored.path != self.store.object_path(fixture.reference.asset_id):
            raise RuntimeError("published object path mismatch")
        self._insert_fixture(fixture, "available")

    def prepare_pending(self, fixture: FailureFixture) -> None:
        """原子插入 pending catalog 与 event，不创建最终对象。"""
        path = self.store.object_path(fixture.reference.asset_id)
        if path.exists():
            raise RuntimeError("pending fixture final object already exists")
        self._insert_fixture(fixture, "pending")

    def set_status(self, asset_id: str, status: str,
                   error_category: str | None = None) -> None:
        """复用 adapter 的显式 catalog 状态转换。"""
        self.adapter.set_asset_status(asset_id, status, error_category)

    def replace_metadata(self, fixture: FailureFixture, *, mismatched: bool) -> None:
        """精确更新 catalog content_length，以注入或恢复 metadata。"""
        content_length = len(fixture.payload) + 1 if mismatched else len(fixture.payload)
        connection = self.adapter.connect_worker()
        try:
            try:
                with connection.transaction():
                    cursor = connection.execute(
                        f"UPDATE {self.adapter.schema}.assets SET content_length=%s,"
                        "updated_at=CURRENT_TIMESTAMP WHERE asset_id=%s",
                        (content_length, fixture.reference.asset_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("metadata update did not affect exactly one asset")
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()


class ClickHouseAssetFaultControl:
    """封装真实 ClickHouse asset_ref adapter 的故障控制与只读观测面。"""

    def __init__(self, adapter: ClickHouseAdapter):
        if (
            not isinstance(adapter, ClickHouseAdapter)
            or adapter.layout != "asset_ref"
            or not isinstance(adapter.asset_store, LocalAssetStore)
        ):
            raise ValueError("control requires a ClickHouse asset_ref adapter")
        self.adapter = adapter
        self.store = adapter.asset_store

    def create(self) -> dict[str, object]:
        """创建 control 持有的物理 database。"""
        return self.adapter.create()

    def cleanup(self):
        """清理 control 持有的物理 database。"""
        return self.adapter.cleanup()

    def cleanup_targets(self) -> tuple[str, ...]:
        """返回 runner 可核对的物理 database cleanup identity。"""
        return (self.adapter.database,)

    def get_available(self, asset_id: str) -> AssetRecord | None:
        """读取指定 asset 的真实 catalog 状态。"""
        return self.adapter.get_available(asset_id)

    def event_visible(self, reference: AssetReference) -> bool:
        """从事件表精确核对完整 Asset 引用是否唯一可见。"""
        if not isinstance(reference, AssetReference):
            raise ValueError("reference must be AssetReference")
        connection = self.adapter.connect_worker()
        try:
            body = self.adapter._request(
                connection,
                f"SELECT count() AS count FROM {self.adapter.database}.events_analytics "
                "WHERE asset_id={asset_id:String} AND content_type={content_type:String} "
                "AND encoding={encoding:String} AND content_length={content_length:UInt64} "
                "AND preview={preview:String} AND sha256={sha256:String} FORMAT JSONEachRow",
                parameters={
                    "asset_id": reference.asset_id, "content_type": reference.content_type,
                    "encoding": reference.encoding,
                    "content_length": reference.content_length,
                    "preview": reference.preview, "sha256": reference.asset_id,
                },
            )
            rows = self.adapter._json_rows(body)
        finally:
            connection.close()
        if (
            len(rows) != 1 or not isinstance(rows[0], dict)
            or set(rows[0]) != {"count"}
            or isinstance(rows[0]["count"], bool)
        ):
            raise RuntimeError("invalid ClickHouse event visibility response")
        count = int(rows[0]["count"])
        if count not in (0, 1):
            raise RuntimeError("invalid ClickHouse event visibility response")
        return count == 1

    def reachable_paths(self) -> set[Path]:
        """通过 event 与 catalog 的真实 JOIN 返回可达对象路径。"""
        connection = self.adapter.connect_worker()
        try:
            body = self.adapter._request(
                connection,
                f"SELECT DISTINCT a.storage_path AS storage_path "
                f"FROM {self.adapter.database}.events_analytics e "
                f"INNER JOIN {self.adapter.database}.assets a ON a.asset_id=e.asset_id "
                "FORMAT JSONEachRow",
            )
            rows = self.adapter._json_rows(body)
        finally:
            connection.close()
        paths = []
        for row in rows:
            if (
                not isinstance(row, dict) or set(row) != {"storage_path"}
                or not isinstance(row["storage_path"], str) or not row["storage_path"]
            ):
                raise RuntimeError("invalid ClickHouse reachable-path response")
            paths.append(Path(row["storage_path"]))
        if len(paths) != len(set(paths)):
            raise RuntimeError("duplicate ClickHouse reachable-path response")
        return set(paths)

    @staticmethod
    def _event_row(fixture: FailureFixture) -> dict[str, object]:
        """将故障 fixture 转为 ClickHouse DDL 对应的完整 event row。"""
        record = fixture.record
        return {
            "ingest_seq": 0, "event_id": record.event_id, "trace_id": record.trace_id,
            "span_id": record.event_id + ":span", "parent_span_id": None,
            "project_id": record.project_id, "start_time": clickhouse_timestamp(record.start_time),
            "end_time": "2030-01-01 00:00:00.001", "duration_ms": 1,
            "span_type": "llm", "framework": "asset_failure", "level": "ERROR",
            "cohort": record.cohort, "profile": record.profile,
            "content_type": record.content_type, "encoding": record.encoding,
            "content_length": record.content_length, "preview": record.preview,
            "sha256": record.sha256, "asset_id": fixture.reference.asset_id,
        }

    def _insert_fixture(self, fixture: FailureFixture, status: str) -> None:
        """依次写入 catalog 与 event；任一 ClickHouse 写失败均向上传播。"""
        asset_id = fixture.reference.asset_id
        rows = (
            ("assets", {
                "asset_id": asset_id, "sha256": asset_id,
                "content_type": fixture.record.content_type,
                "encoding": fixture.record.encoding,
                "content_length": fixture.record.content_length,
                "storage_path": str(self.store.object_path(asset_id)),
                "status": status, "error_category": None,
            }),
            ("events_analytics", self._event_row(fixture)),
        )
        connection = self.adapter.connect_worker()
        try:
            for table, row in rows:
                body = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                self.adapter._request(
                    connection,
                    f"INSERT INTO {self.adapter.database}.{table} FORMAT JSONEachRow",
                    body,
                )
        finally:
            connection.close()

    def prepare_available(self, fixture: FailureFixture) -> None:
        """先发布 fixture bytes，再插入 available catalog 与 event。"""
        stored = self.store.publish_bytes(fixture.reference.asset_id, fixture.payload)
        if stored.path != self.store.object_path(fixture.reference.asset_id):
            raise RuntimeError("published object path mismatch")
        self._insert_fixture(fixture, "available")

    def prepare_pending(self, fixture: FailureFixture) -> None:
        """插入 pending catalog 与 event，不创建最终对象。"""
        path = self.store.object_path(fixture.reference.asset_id)
        if path.exists():
            raise RuntimeError("pending fixture final object already exists")
        self._insert_fixture(fixture, "pending")

    def set_status(self, asset_id: str, status: str,
                   error_category: str | None = None) -> None:
        """复用 adapter 的同步 catalog 状态转换。"""
        self.adapter.set_asset_status(asset_id, status, error_category)

    def replace_metadata(self, fixture: FailureFixture, *, mismatched: bool) -> None:
        """同步更新 content_length，并立即核对目标 identity 与精确值。"""
        content_length = len(fixture.payload) + 1 if mismatched else len(fixture.payload)
        asset_id = fixture.reference.asset_id
        connection = self.adapter.connect_worker()
        try:
            self.adapter._request(
                connection,
                f"ALTER TABLE {self.adapter.database}.assets "
                "UPDATE content_length={content_length:UInt64} "
                "WHERE asset_id={asset_id:String} SETTINGS mutations_sync=2",
                parameters={"content_length": content_length, "asset_id": asset_id},
            )
        finally:
            connection.close()
        connection = self.adapter.connect_worker()
        try:
            body = self.adapter._request(
                connection,
                f"SELECT asset_id,content_length FROM {self.adapter.database}.assets "
                "WHERE asset_id={asset_id:String} FORMAT JSONEachRow",
                parameters={"asset_id": asset_id},
            )
            rows = self.adapter._json_rows(body)
        finally:
            connection.close()
        if (
            len(rows) != 1 or not isinstance(rows[0], dict)
            or set(rows[0]) != {"asset_id", "content_length"}
            or rows[0]["asset_id"] != asset_id
            or isinstance(rows[0]["content_length"], bool)
        ):
            raise RuntimeError("ClickHouse metadata mutation readback mismatch")
        if int(rows[0]["content_length"]) != content_length:
            raise RuntimeError("ClickHouse metadata mutation readback mismatch")


class DatabaseFaultInjector:
    """通过 control 与受观测 store 操作面实施六种固定数据库 Asset 故障。"""

    _CASES = frozenset({
        "missing", "corrupt", "metadata_mismatch", "upload_then_db_failure",
        "publish_failure", "delete_failure",
    })

    @staticmethod
    def _control(case, fixture, context):
        """验证 case、fixture、control identity 与 canonical object path。"""
        if case.name not in DatabaseFaultInjector._CASES:
            raise ValueError("unsupported asset failure case: " + str(case.name))
        if context.adapter is not context.catalog:
            raise ValueError("adapter and catalog must be the same fault control")
        control = context.adapter
        required = (
            "prepare_available", "prepare_pending", "set_status", "replace_metadata",
        )
        if not all(callable(getattr(control, name, None)) for name in required):
            raise ValueError("fault control is missing required capabilities")
        asset_id = fixture.reference.asset_id
        if (
            fixture.record.sha256 != asset_id
            or fixture.record.content_length != len(fixture.payload)
            or hashlib.sha256(fixture.payload).hexdigest() != asset_id
            or context.store.object_path(asset_id) != control.store.object_path(asset_id)
        ):
            raise ValueError("fixture or object path mismatch")
        return control

    def prepare(self, case, fixture, *, context) -> None:
        """建立各 case 的固定注入前状态。"""
        control = self._control(case, fixture, context)
        if case.name == "upload_then_db_failure":
            return
        if case.name == "publish_failure":
            control.prepare_pending(fixture)
            return
        control.prepare_available(fixture)

    @staticmethod
    def _restore_mode(path: Path, original_mode: int) -> None:
        """恢复并核对故障前目录 mode。"""
        path.chmod(original_mode)
        if stat.S_IMODE(path.stat().st_mode) != original_mode:
            raise RuntimeError("asset shard mode was not restored")

    def _inject_publish_failure(self, fixture, context, control) -> None:
        """用 shard 写权限制造一次受 runner 观测的发布失败。"""
        asset_id = fixture.reference.asset_id
        final_path = context.store.object_path(asset_id)
        shard = final_path.parent
        shard.mkdir(parents=True, exist_ok=True)
        original_mode = stat.S_IMODE(shard.stat().st_mode)
        failed = False
        try:
            shard.chmod(original_mode & ~0o222)
            try:
                context.store.publish_bytes(asset_id, fixture.payload)
            except AssetError as error:
                if error.category != "failed":
                    raise RuntimeError("publish failure had an unexpected category") from error
                failed = True
        finally:
            self._restore_mode(shard, original_mode)
        if not failed:
            raise RuntimeError("asset publish did not fail")
        if final_path.exists():
            raise RuntimeError("failed asset publish left a final object")
        control.set_status(asset_id, "failed", "failed")

    def _inject_delete_failure(self, fixture, context, control) -> None:
        """用 shard 写权限制造一次真实最终对象删除失败。"""
        asset_id = fixture.reference.asset_id
        final_path = context.store.object_path(asset_id)
        if not final_path.is_file():
            raise RuntimeError("delete failure requires a final object")
        control.set_status(asset_id, "deleting")
        shard = final_path.parent
        original_mode = stat.S_IMODE(shard.stat().st_mode)
        failed = False
        try:
            shard.chmod(original_mode & ~0o222)
            try:
                final_path.unlink()
            except OSError:
                failed = True
        finally:
            self._restore_mode(shard, original_mode)
        if not failed:
            raise RuntimeError("asset deletion did not fail")
        if not final_path.is_file():
            raise RuntimeError("failed asset deletion removed the final object")

    def inject(self, case, fixture, *, context) -> None:
        """执行单个固定故障动作，不返回或生成 evidence。"""
        control = self._control(case, fixture, context)
        asset_id = fixture.reference.asset_id
        path = context.store.object_path(asset_id)
        if case.name == "missing":
            path.unlink()
        elif case.name == "corrupt":
            corrupt = bytes([fixture.payload[0] ^ 1]) + fixture.payload[1:]
            if len(corrupt) != len(fixture.payload) or hashlib.sha256(corrupt).hexdigest() == asset_id:
                raise RuntimeError("unable to construct corrupt payload")
            path.write_bytes(corrupt)
        elif case.name == "metadata_mismatch":
            control.replace_metadata(fixture, mismatched=True)
        elif case.name == "upload_then_db_failure":
            context.store.publish_bytes(asset_id, fixture.payload)
        elif case.name == "publish_failure":
            self._inject_publish_failure(fixture, context, control)
        elif case.name == "delete_failure":
            self._inject_delete_failure(fixture, context, control)

    def recover(self, case, fixture, *, context) -> None:
        """恢复可恢复故障，并保留 publish/delete 的失败后置状态。"""
        control = self._control(case, fixture, context)
        asset_id = fixture.reference.asset_id
        path = context.store.object_path(asset_id)
        if case.name == "missing":
            context.store.publish_bytes(asset_id, fixture.payload)
        elif case.name == "corrupt":
            path.unlink()
            context.store.publish_bytes(asset_id, fixture.payload)
        elif case.name == "metadata_mismatch":
            control.replace_metadata(fixture, mismatched=False)
        elif case.name == "upload_then_db_failure":
            path.unlink()


def opengauss_asset_failure_factories(
    endpoints: EngineEndpoints,
) -> tuple[Callable, Callable, DatabaseFaultInjector]:
    """构造可直接交给六故障 runner 的 openGauss factories 与 injector。"""
    if not isinstance(endpoints, EngineEndpoints):
        raise ValueError("endpoints must be EngineEndpoints")
    controls = set()

    def adapter_factory(namespace, object_directory):
        """构造独立 asset_ref control，不连接数据库或创建 schema。"""
        store = LocalAssetStore(Path(object_directory).resolve())
        adapter = OpenGaussAdapter(
            endpoints.opengauss_host, endpoints.opengauss_port,
            endpoints.opengauss_container, namespace, "asset_ref", Path.cwd(), store,
        )
        control = OpenGaussAssetFaultControl(adapter)
        controls.add(control)
        return control

    def catalog_factory(control):
        """仅向对应 factory 创建的同一 control 返回只读观测面。"""
        if not isinstance(control, OpenGaussAssetFaultControl) or control not in controls:
            raise ValueError("catalog factory requires its corresponding fault control")
        return control

    return adapter_factory, catalog_factory, DatabaseFaultInjector()


def xstore_asset_failure_factories(
    endpoints: EngineEndpoints,
) -> tuple[Callable, Callable, DatabaseFaultInjector]:
    """构造可直接交给六故障 runner 的 XStore factories 与 injector。"""
    if not isinstance(endpoints, EngineEndpoints):
        raise ValueError("endpoints must be EngineEndpoints")
    controls = set()

    def adapter_factory(namespace, object_directory):
        """构造独立 asset_ref control，不连接数据库或创建 schema。"""
        from xstore import XStoreAdapter
        store = LocalAssetStore(Path(object_directory).resolve())
        adapter = XStoreAdapter(
            endpoints.xstore_host, endpoints.xstore_port,
            endpoints.xstore_container, namespace, "asset_ref", Path.cwd(), store,
        )
        control = OpenGaussAssetFaultControl(adapter)
        controls.add(control)
        return control

    def catalog_factory(control):
        """仅向对应 factory 创建的同一 control 返回只读观测面。"""
        if not isinstance(control, OpenGaussAssetFaultControl) or control not in controls:
            raise ValueError("catalog factory requires its corresponding fault control")
        return control

    return adapter_factory, catalog_factory, DatabaseFaultInjector()


def clickhouse_asset_failure_factories(
    endpoints: EngineEndpoints,
) -> tuple[Callable, Callable, DatabaseFaultInjector]:
    """构造可直接交给六故障 runner 的 ClickHouse factories 与 injector。"""
    if not isinstance(endpoints, EngineEndpoints):
        raise ValueError("endpoints must be EngineEndpoints")
    controls = set()

    def adapter_factory(namespace, object_directory):
        """构造独立 asset_ref control，不连接数据库或创建 database。"""
        store = LocalAssetStore(Path(object_directory).resolve())
        adapter = ClickHouseAdapter(
            endpoints.clickhouse_host, endpoints.clickhouse_port,
            endpoints.clickhouse_container, namespace, "asset_ref", Path.cwd(), store,
        )
        control = ClickHouseAssetFaultControl(adapter)
        controls.add(control)
        return control

    def catalog_factory(control):
        """仅向对应 factory 创建的同一 control 返回只读观测面。"""
        if not isinstance(control, ClickHouseAssetFaultControl) or control not in controls:
            raise ValueError("catalog factory requires its corresponding fault control")
        return control

    return adapter_factory, catalog_factory, DatabaseFaultInjector()


def asset_failure_factories(
    engine: str,
    endpoints: EngineEndpoints,
) -> tuple[Callable, Callable, DatabaseFaultInjector]:
    """按固定 engine 名选择 Asset 六故障生产 factories。"""
    if not isinstance(endpoints, EngineEndpoints):
        raise ValueError("endpoints must be EngineEndpoints")
    if engine == "opengauss":
        return opengauss_asset_failure_factories(endpoints)
    if engine == "xstore":
        return xstore_asset_failure_factories(endpoints)
    if engine == "clickhouse":
        return clickhouse_asset_failure_factories(endpoints)
    raise ValueError(f"unsupported engine: {engine}")
