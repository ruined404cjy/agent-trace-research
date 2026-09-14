"""阶段三 runner 共用的 payload 与 truth 契约。"""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


TRUTH_FORMAT = "agent-trace-json-storage-stage3-truth"
FORMAT_VERSION = 1


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


def _validate_cohorts(cohorts, payloads):
    """核对 cohort 与 profile 的计数和原始 bytes 汇总。"""
    if not isinstance(cohorts, dict) or not cohorts:
        raise ValueError("invalid cohorts")
    grouped = {name: [] for name in cohorts}
    for record in payloads:
        if record.cohort not in grouped:
            raise ValueError("payload cohort missing")
        grouped[record.cohort].append(record)
    for name, summary in cohorts.items():
        records = grouped[name]
        if not isinstance(summary, dict) or not isinstance(summary.get("performance"), bool):
            raise ValueError(f"invalid cohort: {name}")
        expected = {
            "payload_count": len(records),
            "profile_counts": dict(Counter(record.profile for record in records)),
            "raw_payload_bytes": sum(record.content_length for record in records),
        }
        if any(summary.get(field) != value for field, value in expected.items()):
            raise ValueError(f"cohort summary mismatch: {name}")


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
    if not isinstance(source, dict) or not isinstance(representative_traces, dict):
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
    )
