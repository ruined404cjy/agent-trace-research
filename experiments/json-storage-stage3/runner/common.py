"""阶段三 runner 共用的 payload 与 truth 契约。"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol


TRUTH_FORMAT = "agent-trace-json-storage-stage3-truth"
FORMAT_VERSION = 1
COHORT_CONTRACT = {
    "main": {
        "performance": True,
        "payload_count": 160,
        "profile_counts": {
            "text_64k": 40,
            "text_512k": 40,
            "text_2m": 40,
            "entropy_512k": 40,
        },
        "raw_payload_bytes": 128_450_560,
    },
    "equal_total_control": {
        "performance": True,
        "payload_count": 1_320,
        "profile_counts": {"text_2m": 40, "text_64k": 1_280},
        "raw_payload_bytes": 167_772_160,
        "variants": {
            "few_large": {
                "profile": "text_2m",
                "payload_count": 40,
                "raw_payload_bytes": 83_886_080,
            },
            "many_medium": {
                "profile": "text_64k",
                "payload_count": 1_280,
                "raw_payload_bytes": 83_886_080,
            },
        },
    },
    "correctness_only": {
        "performance": False,
        "payload_count": 1,
        "profile_counts": {"unicode_boundary": 1},
        "raw_payload_bytes": 1_024,
    },
}
DETAIL_PROFILES = ("text_64k", "text_512k", "text_2m", "entropy_512k")
LAYOUTS = ("same_table", "separate", "full_core", "asset_ref")


@dataclass(frozen=True)
class LayoutCatalog:
    """描述一种布局的写目标、列表入口、详情入口和水位要求。"""

    name: str
    write_tables: tuple[str, ...]
    list_source: str
    detail_source: str
    requires_joint_watermark: bool


def build_layout_catalog(layout: str) -> LayoutCatalog:
    """返回阶段三四种固定布局的物理入口契约。"""
    catalogs = {
        "same_table": LayoutCatalog("same_table", ("events",), "events", "events", False),
        "separate": LayoutCatalog(
            "separate", ("events_analytics", "event_payloads"),
            "events_analytics", "event_payloads", True,
        ),
        "full_core": LayoutCatalog(
            "full_core", ("events_full", "events_core"),
            "events_core", "events_full", True,
        ),
        "asset_ref": LayoutCatalog(
            "asset_ref", ("events_analytics", "assets"),
            "events_analytics", "assets", True,
        ),
    }
    try:
        return catalogs[layout]
    except (KeyError, TypeError) as error:
        raise ValueError(f"unsupported layout: {layout}") from error


@dataclass(frozen=True)
class QuerySpec:
    """保存统一逻辑查询类型及其绑定参数。"""

    kind: str
    parameters: dict[str, object] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in {"list", "preview", "detail", "trace", "batch"}:
            raise ValueError(f"unsupported query kind: {self.kind}")
        if not isinstance(self.parameters, dict):
            raise ValueError("query parameters must be an object")
        if self.kind == "batch" and (
            not isinstance(self.parameters.get("cohort"), str)
            or not self.parameters["cohort"]
        ):
            raise ValueError("batch query requires cohort")


@dataclass(frozen=True)
class BlockResult:
    """描述一个 block 完成协议写入后的联合水位。"""

    rows: int
    watermark: int
    watermarks: dict[str, int]
    wall_ms: float
    write_target_ms: dict[str, float] = field(default_factory=dict)
    asset_publish_ms: float = 0.0
    logical_target_row_bytes: dict[str, int] = field(default_factory=dict)
    database_ingest_request_body_bytes: dict[str, int | None] = field(default_factory=dict)
    asset_raw_object_bytes: int = 0


@dataclass(frozen=True)
class MaintenanceResult:
    """描述写入可见或查询维护的完成状态和观测值。"""

    completed: bool
    watermarks: dict[str, int] = field(default_factory=dict)
    observations: tuple[dict[str, object], ...] = ()
    wall_ms: float = 0.0
    analyze_ms: float | None = None
    merge_wait_ms: float | None = None
    watermark_wait_ms: float = 0.0


@dataclass(frozen=True)
class QueryResult:
    """保存完整读取到客户端的逻辑行及查询证据身份。"""

    query_id: str
    rows: tuple[dict[str, object], ...]
    response_bytes: int
    database_response_bytes: int
    resolver_payload_bytes: int
    query_complete_ms: float
    recovery_ms: float
    database_protocol_bytes: int | None = None
    resolver_requests: int = 0
    resolver_read_ms: float = 0.0


@dataclass(frozen=True)
class AssetStorageEvidence:
    """保存事件可达和 orphan 内容对象的数量与已验证 bytes。"""

    available_object_count: int
    available_bytes: int
    orphan_object_count: int
    orphan_bytes: int


@dataclass(frozen=True)
class StorageEvidence:
    """保存按写目标分项的引擎空间和物理状态。"""

    tables: dict[str, dict[str, object]]
    merges: tuple[dict[str, object], ...] = ()
    asset_store: AssetStorageEvidence | None = None


@dataclass(frozen=True)
class AccessEvidence:
    """保存查询计划、索引扫描或 QueryFinish 证据。"""

    plans: dict[str, str]
    index_scans: dict[str, int] = field(default_factory=dict)
    query_finish: dict[str, dict[str, object]] = field(default_factory=dict)
    query_details: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class CleanupResult:
    """描述 adapter 独占 namespace 的清理确认。"""

    namespace: str
    removed: bool


@dataclass(frozen=True)
class PhysicalTargetAudit:
    """保存单个物理写目标的有序 identity 与 metadata 审计行。"""

    rows: tuple[dict[str, object], ...]
    duplicate_identities: int
    event_mappings: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class DatasetAudit:
    """保存数据库全量 identity、顺序、payload 和重复项审计结果。"""

    rows: tuple[dict[str, object], ...]
    duplicate_event_ids: int
    logical_response_bytes: int
    database_protocol_bytes: int | None = None
    target_audits: dict[str, PhysicalTargetAudit] = field(default_factory=dict)


class LayoutAdapter(Protocol):
    """定义主矩阵、part 状态和故障实验共用的 adapter 边界。"""

    def create(self) -> dict[str, object]: ...
    def ingest_block(self, block: list[dict[str, object]]) -> BlockResult: ...
    def wait_write_complete(self, watermark: int) -> MaintenanceResult: ...
    def wait_query_ready(self, timeout_seconds: int) -> MaintenanceResult: ...
    def get_available(self, asset_id: str): ...
    def run_query(self, query: QuerySpec) -> QueryResult: ...
    def collect_storage(self) -> StorageEvidence: ...
    def collect_access_evidence(self, query_ids: list[str]) -> AccessEvidence: ...
    def audit_dataset(self) -> DatasetAudit: ...
    def cleanup(self) -> CleanupResult: ...


def logical_response_bytes(rows, include_payload=True):
    """按跨引擎统一的 canonical metadata 加原始 payload 计算逻辑响应 bytes。"""
    total = 0
    for row in rows:
        projected = dict(row)
        payload = projected.pop("payload", None)
        total += len(_canonical_bytes(projected)) + 1
        if include_payload and payload is not None:
            if not isinstance(payload, bytes):
                raise ValueError("logical response payload must be bytes")
            total += len(payload)
    return total


def logical_submission_bytes(rows, include_payload):
    """按客户端实际构造的 logical rows 计算可复现提交 bytes。"""
    total = 0
    for row in rows:
        projected = dict(row)
        payload = projected.pop("_payload_bytes", None)
        total += len(_canonical_bytes(projected)) + 1
        if include_payload and payload is not None:
            total += len(payload)
    return total


EVENT_TARGET_FIELDS = (
    "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
    "project_id", "start_time", "end_time", "duration_ms", "span_type",
    "framework", "level", "cohort", "profile", "content_type", "encoding",
    "content_length", "preview", "sha256",
)
PAYLOAD_TARGET_FIELDS = (
    "ingest_seq", "event_id", "trace_id", "project_id", "start_time",
    "profile", "content_type", "encoding", "content_length", "preview", "sha256",
)


def logical_target_rows(layout, rows, payloads, asset_paths=None, submitted_asset_ids=None):
    """按布局实际 INSERT 列构造跨引擎一致的确定性逻辑目标行。"""
    if len(rows) != len(payloads):
        raise ValueError("rows and payloads must have the same length")
    catalog = build_layout_catalog(layout)

    def project(fields, row):
        return {field_name: row[field_name] for field_name in fields}

    event_rows = tuple(project(EVENT_TARGET_FIELDS, row) for row in rows)
    full_rows = tuple(
        {**project(EVENT_TARGET_FIELDS, row), "_payload_bytes": payload}
        for row, payload in zip(rows, payloads)
    )
    payload_rows = tuple(
        {**project(PAYLOAD_TARGET_FIELDS, row), "_payload_bytes": payload}
        for row, payload in zip(rows, payloads)
    )
    if catalog.name == "same_table":
        return {"events": full_rows}
    if catalog.name == "separate":
        return {"events_analytics": event_rows, "event_payloads": payload_rows}
    if catalog.name == "full_core":
        return {"events_full": full_rows, "events_core": event_rows}
    if asset_paths is None:
        raise ValueError("asset_ref logical rows require asset paths")
    analytics_rows = tuple(
        {**project(EVENT_TARGET_FIELDS, row), "asset_id": row["sha256"]}
        for row in rows
    )
    assets = []
    seen = set()
    for row, payload in zip(rows, payloads):
        if payload is None:
            continue
        asset_id = row["sha256"]
        if asset_id in seen or (submitted_asset_ids is not None and asset_id not in submitted_asset_ids):
            continue
        seen.add(asset_id)
        assets.append({
            "asset_id": asset_id, "sha256": asset_id,
            "content_type": row["content_type"], "encoding": row["encoding"],
            "content_length": row["content_length"], "storage_path": asset_paths[asset_id],
            "status": "pending", "error_category": None,
        })
    return {"events_analytics": analytics_rows, "assets": tuple(assets)}


def logical_target_row_bytes(layout, rows, payloads, asset_paths=None, submitted_asset_ids=None):
    """返回每个物理 INSERT 目标的 deterministic logical target-row bytes。"""
    return {
        target: logical_submission_bytes(
            list(target_rows), any("_payload_bytes" in row for row in target_rows),
        )
        for target, target_rows in logical_target_rows(
            layout, rows, payloads, asset_paths, submitted_asset_ids,
        ).items()
    }


def _canonical_bytes(value):
    """返回键排序、数组保序且保留 Unicode 的 canonical JSON bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_digest(value):
    """返回 canonical JSON UTF-8 bytes 的 SHA-256。"""
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class PayloadRecord:
    """描述 payload 的完整逻辑身份、内容元数据与冻结文件位置。"""

    event_id: str
    trace_id: str
    project_id: str
    start_time: str
    cohort: str
    profile: str
    content_type: str
    encoding: str
    content_length: int
    preview: str
    sha256: str
    payload_path: str


@dataclass(frozen=True)
class TruthCatalog:
    """保存阶段三冻结输入的公共 truth 视图。"""

    seed: int
    source: dict[str, object]
    record_count: int
    block_size: int
    block_count: int
    watermarks: tuple[int, ...]
    identity_sha256: str
    query_window: dict[str, object]
    payloads: tuple[PayloadRecord, ...]
    cohorts: dict[str, dict[str, object]]
    representative_traces: dict[str, object]
    detail_samples: tuple[PayloadRecord, ...]

    @property
    def profile_counts(self):
        """返回全部 payload 的 profile 计数。"""
        return dict(Counter(record.profile for record in self.payloads))

    @property
    def cohort_counts(self):
        """返回全部 payload 的 cohort 计数。"""
        return dict(Counter(record.cohort for record in self.payloads))

    @property
    def raw_payload_bytes(self):
        """返回全部 payload 的原始 UTF-8 bytes 总量。"""
        return sum(record.content_length for record in self.payloads)


def _is_sha256(value):
    """判断字符串是否为小写 SHA-256 十六进制摘要。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_int(value):
    """判断值是否为排除 bool 的正整数。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _payload_record(value):
    """校验并转换单条 payload truth。"""
    if not isinstance(value, dict):
        raise ValueError("invalid payload record")
    fields = (
        "event_id",
        "trace_id",
        "project_id",
        "start_time",
        "cohort",
        "profile",
        "content_type",
        "encoding",
        "content_length",
        "preview",
        "sha256",
        "payload_path",
    )
    if set(value) != set(fields):
        raise ValueError("invalid payload record")
    string_fields = tuple(field for field in fields if field != "content_length")
    if any(not isinstance(value[field], str) or not value[field] for field in string_fields):
        raise ValueError("invalid payload record")
    if not _positive_int(value["content_length"]):
        raise ValueError("invalid payload record")
    if value["content_type"] != "application/json" or value["encoding"] != "utf-8":
        raise ValueError("invalid payload record")
    if len(value["preview"]) > 200 or not _is_sha256(value["sha256"]):
        raise ValueError("invalid payload record")
    path = PurePosixPath(value["payload_path"])
    if path.is_absolute() or not path.parts or path.parts[0] != "payloads" or ".." in path.parts:
        raise ValueError("invalid payload record")
    return PayloadRecord(**value)


def _validate_payload_file(root, record):
    """核对 payload location、bytes、UTF-8 preview 与摘要。"""
    root = root.resolve()
    payload_path = (root / record.payload_path).resolve()
    try:
        payload_path.relative_to(root)
        payload_bytes = payload_path.read_bytes()
        payload_text = payload_bytes.decode(record.encoding)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"payload identity mismatch: {record.event_id}") from error
    if (
        len(payload_bytes) != record.content_length
        or hashlib.sha256(payload_bytes).hexdigest() != record.sha256
        or payload_text[:200] != record.preview
    ):
        raise ValueError(f"payload identity mismatch: {record.event_id}")
    try:
        json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSON payload: {record.event_id}") from error


def _validate_cohorts(cohorts, payloads):
    """核对 cohort 与 profile 的计数和原始 bytes 汇总。"""
    if not isinstance(cohorts, dict) or set(cohorts) != set(COHORT_CONTRACT):
        raise ValueError("cohort contract mismatch")
    grouped = {name: [] for name in cohorts}
    for record in payloads:
        if record.cohort not in grouped:
            raise ValueError("payload cohort missing")
        grouped[record.cohort].append(record)
    for name, summary in cohorts.items():
        records = grouped[name]
        expected_contract = COHORT_CONTRACT[name]
        if not isinstance(summary, dict) or not isinstance(summary.get("performance"), bool):
            raise ValueError(f"invalid cohort: {name}")
        if summary["performance"] != expected_contract["performance"]:
            raise ValueError(f"cohort performance mismatch: {name}")
        expected = {
            "payload_count": len(records),
            "profile_counts": dict(Counter(record.profile for record in records)),
            "raw_payload_bytes": sum(record.content_length for record in records),
        }
        if any(summary.get(field) != value for field, value in expected.items()):
            raise ValueError(f"cohort summary mismatch: {name}")
        if set(summary) != set(expected_contract) or any(
            summary.get(field) != value for field, value in expected_contract.items()
        ):
            raise ValueError(f"cohort contract mismatch: {name}")


def _validate_representative_traces(representative_traces, payloads):
    """校验 p25、p50、p95 Trace 结构及其 payload 汇总。"""
    if not isinstance(representative_traces, dict) or set(representative_traces) != {
        "p25",
        "p50",
        "p95",
    }:
        raise ValueError("representative trace contract mismatch")
    for label, selection in representative_traces.items():
        fields = {
            "trace_id",
            "span_count",
            "payload_count",
            "profile_counts",
            "raw_payload_bytes",
        }
        if (
            not isinstance(selection, dict)
            or set(selection) != fields
            or not isinstance(selection["trace_id"], str)
            or not selection["trace_id"]
            or not _positive_int(selection["span_count"])
        ):
            raise ValueError(f"invalid representative trace: {label}")
        records = [record for record in payloads if record.trace_id == selection["trace_id"]]
        expected = {
            "payload_count": len(records),
            "profile_counts": dict(Counter(record.profile for record in records)),
            "raw_payload_bytes": sum(record.content_length for record in records),
        }
        if selection["span_count"] < len(records) or any(
            selection.get(field) != value for field, value in expected.items()
        ):
            raise ValueError(f"representative trace summary mismatch: {label}")


def _validate_detail_samples(raw_samples, payloads):
    """校验四类详情样本均来自主 cohort 且完全对应 payload truth。"""
    if not isinstance(raw_samples, list) or len(raw_samples) != len(DETAIL_PROFILES):
        raise ValueError("detail sample contract mismatch")
    samples = tuple(_payload_record(sample) for sample in raw_samples)
    if tuple(sample.profile for sample in samples) != DETAIL_PROFILES or any(
        sample.cohort != "main" for sample in samples
    ):
        raise ValueError("detail sample contract mismatch")
    payload_by_event = {record.event_id: record for record in payloads}
    if any(payload_by_event.get(sample.event_id) != sample for sample in samples):
        raise ValueError("detail sample mismatch")
    return samples


def load_truth(path: Path) -> TruthCatalog:
    """读取并完整验证阶段三 truth 及其 payload 文件。"""
    path = Path(path)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("invalid truth.json") from error
    if not isinstance(value, dict):
        raise ValueError("invalid truth.json")
    if value.get("format") != TRUTH_FORMAT or value.get("format_version") != FORMAT_VERSION:
        raise ValueError("truth format mismatch")

    seed = value.get("seed")
    record_count = value.get("record_count")
    block_size = value.get("block_size")
    block_count = value.get("block_count")
    watermarks = value.get("watermarks")
    source = value.get("source")
    representative_traces = value.get("representative_traces")
    query_window = value.get("query_window")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("invalid truth seed")
    if not _positive_int(record_count) or not _positive_int(block_size) or not _positive_int(block_count):
        raise ValueError("invalid truth block contract")
    expected_watermarks = list(range(block_size, record_count, block_size)) + [record_count]
    if watermarks != expected_watermarks or block_count != len(expected_watermarks):
        raise ValueError("truth watermarks mismatch")
    if not isinstance(source, dict):
        raise ValueError("invalid truth metadata")
    if (
        not isinstance(query_window, dict)
        or set(query_window)
        != {"project_id", "start_time", "end_time", "page_size", "row_count"}
        or any(
            not isinstance(query_window[field], str) or not query_window[field]
            for field in ("project_id", "start_time", "end_time")
        )
        or not _positive_int(query_window["page_size"])
        or isinstance(query_window["row_count"], bool)
        or not isinstance(query_window["row_count"], int)
        or not 0 <= query_window["row_count"] <= record_count
    ):
        raise ValueError("invalid query window")
    if not _is_sha256(value.get("identity_sha256")):
        raise ValueError("invalid identity SHA-256")
    raw_payloads = value.get("payloads")
    if not isinstance(raw_payloads, list) or not raw_payloads:
        raise ValueError("invalid truth payloads")
    payloads = tuple(_payload_record(record) for record in raw_payloads)
    if len({record.event_id for record in payloads}) != len(payloads):
        raise ValueError("duplicate payload event_id")
    if len({record.sha256 for record in payloads}) != len(payloads):
        raise ValueError("duplicate payload SHA-256")
    if len({record.payload_path for record in payloads}) != len(payloads):
        raise ValueError("duplicate payload path")
    cohorts = value.get("cohorts")
    _validate_cohorts(cohorts, payloads)
    _validate_representative_traces(representative_traces, payloads)
    detail_samples = _validate_detail_samples(value.get("detail_samples"), payloads)
    for record in payloads:
        _validate_payload_file(path.parent, record)

    return TruthCatalog(
        seed=seed,
        source=source,
        record_count=record_count,
        block_size=block_size,
        block_count=block_count,
        watermarks=tuple(watermarks),
        identity_sha256=value["identity_sha256"],
        query_window=query_window,
        payloads=payloads,
        cohorts=cohorts,
        representative_traces=representative_traces,
        detail_samples=detail_samples,
    )
