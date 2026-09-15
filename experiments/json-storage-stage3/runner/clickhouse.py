"""阶段三 ClickHouse 四种 payload 布局 adapter。"""

import hashlib
import http.client
import json
import re
import time
import urllib.parse
import uuid
from pathlib import Path

from assets import AssetRecord, AssetReference, AssetResolver, LocalAssetStore
from common import (
    AccessEvidence, AssetStorageEvidence, BlockResult, CleanupResult, DatasetAudit, LAYOUTS,
    MaintenanceResult, PhysicalTargetAudit, QueryResult, QuerySpec, StorageEvidence, build_layout_catalog,
    logical_response_bytes, logical_target_row_bytes,
)


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
QUERY_ID = re.compile(r"^[A-Za-z0-9_-]+$")
ASSET_STATUSES = ("pending", "available", "failed", "deleting")
LOGICAL_FIELDS = (
    "event_id", "trace_id", "project_id", "start_time", "profile",
    "content_type", "encoding", "content_length", "preview", "sha256",
)
EVENT_AUDIT_FIELDS = (
    "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
    "project_id", "start_time", "end_time", "duration_ms", "span_type",
    "framework", "level", "cohort", "profile", "content_type", "encoding",
    "content_length", "preview", "sha256",
)
PAYLOAD_AUDIT_FIELDS = (
    "ingest_seq", "event_id", "trace_id", "project_id", "start_time", "profile",
    "content_type", "encoding", "content_length", "preview", "sha256",
)
QUERY_FINISH_FIELDS = (
    "type", "exception_code", "query_duration_ms", "read_rows", "read_bytes",
    "memory_usage", "result_rows", "result_bytes",
)


class _CatalogRow:
    """向 AssetResolver 提供一次数据库读取所得的固定 catalog 行。"""

    def __init__(self, record):
        self.record = record

    def get_available(self, asset_id):
        if self.record is None or self.record.asset_id != asset_id:
            return None
        return self.record


def validate_identifier(value, name):
    """验证 database 和对象名可安全直接进入 SQL identifier。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match ^[a-z][a-z0-9_]{{0,62}}$")
    return value


def database_name(namespace, layout):
    """返回 layout 独占的 database 名。"""
    validate_identifier(namespace, "namespace")
    build_layout_catalog(layout)
    return validate_identifier(f"{namespace}_{layout}", "database name")


def _event_columns(include_payload=False, include_asset=False):
    columns = """ingest_seq UInt64, event_id String, trace_id String, span_id String,
parent_span_id Nullable(String), project_id String, start_time DateTime64(3, 'UTC'),
end_time DateTime64(3, 'UTC'), duration_ms Int64, span_type String, framework String,
level String, cohort Nullable(String), profile Nullable(String), content_type Nullable(String),
encoding Nullable(String), content_length Nullable(UInt64), preview Nullable(String), sha256 Nullable(String)"""
    if include_asset:
        columns += ", asset_id Nullable(String)"
    if include_payload:
        columns += ", payload String CODEC(ZSTD(3))"
    return columns


def _event_table(database, table, include_payload=False, include_asset=False):
    return (f"CREATE TABLE {database}.{table} ({_event_columns(include_payload, include_asset)}) "
            "ENGINE=MergeTree ORDER BY (project_id,start_time,event_id)")


def _payload_table(database):
    """返回独立 payload 表的最小定位和内容列 DDL。"""
    return f"""CREATE TABLE {database}.event_payloads (
ingest_seq UInt64, event_id String, trace_id String, project_id String,
start_time DateTime64(3, 'UTC'), profile Nullable(String), content_type Nullable(String),
encoding Nullable(String), content_length Nullable(UInt64), preview Nullable(String),
sha256 Nullable(String), payload String CODEC(ZSTD(3))) ENGINE=MergeTree
ORDER BY (project_id,start_time,event_id)"""


def create_layout_ddl(namespace, layout):
    """返回四种布局的完整 database 与 MergeTree 表 DDL。"""
    database = database_name(namespace, layout)
    statements = [f"CREATE DATABASE {database}"]
    if layout == "same_table":
        statements.append(_event_table(database, "events", include_payload=True))
    elif layout == "separate":
        statements.append(_event_table(database, "events_analytics"))
        statements.append(_payload_table(database))
    elif layout == "full_core":
        statements.append(_event_table(database, "events_full", include_payload=True))
        statements.append(_event_table(database, "events_core"))
    else:
        statements.append(_event_table(database, "events_analytics", include_asset=True))
        states = ",".join(f"'{status}'={index}" for index, status in enumerate(ASSET_STATUSES, 1))
        statements.append(f"""CREATE TABLE {database}.assets (
asset_id String, sha256 String, content_type String, encoding String, content_length UInt64,
storage_path String, status Enum8({states}), updated_at DateTime64(3, 'UTC') DEFAULT now64(3),
error_category Nullable(String)) ENGINE=MergeTree ORDER BY asset_id""")
    return ";\n".join(statements) + ";"


def storage_statements(database):
    """返回 active part、mark、列字节和 merge 状态的证据 SQL。"""
    validate_identifier(database, "database")
    return (
        "SELECT table,count() AS part_count,sum(rows) AS rows,sum(marks) AS marks,"
        "sum(data_compressed_bytes) AS compressed_bytes,sum(data_uncompressed_bytes) AS uncompressed_bytes "
        f"FROM system.parts WHERE active AND database='{database}' GROUP BY table FORMAT JSONEachRow",
        "SELECT table,column,sum(column_data_compressed_bytes) AS compressed_bytes,"
        "sum(column_data_uncompressed_bytes) AS uncompressed_bytes "
        f"FROM system.parts_columns WHERE active AND database='{database}' GROUP BY table,column FORMAT JSONEachRow",
        f"SELECT table,elapsed,progress,num_parts FROM system.merges WHERE database='{database}' FORMAT JSONEachRow",
    )


def query_finish_sql():
    """返回按唯一 query ID 批量读取 QueryFinish 的 SQL 模板。"""
    return (
        "SELECT query_id,type,exception_code,query_duration_ms,read_rows,read_bytes,memory_usage,result_rows,result_bytes "
        "FROM system.query_log WHERE query_id IN {query_ids:Array(String)} AND type='QueryFinish' FORMAT JSONEachRow"
    )


def clickhouse_timestamp(value):
    """将 truth 毫秒 UTC 时间转换为 ClickHouse DateTime64 文本。"""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
    ):
        raise ValueError("timestamp must use millisecond UTC ISO format")
    return value.replace("T", " ").removesuffix("Z")


class ClickHouseAdapter:
    """实现阶段三 ClickHouse layout 的写入、查询和物理证据接口。"""

    def __init__(self, host, port, container_name, namespace, layout,
                 input_root: Path, asset_store: LocalAssetStore | None = None):
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be positive")
        self.layout = build_layout_catalog(layout).name
        self.namespace = namespace
        self.database = database_name(namespace, layout)
        self.host, self.port, self.container_name = host, port, container_name
        self.input_root = Path(input_root).resolve()
        self.asset_store = asset_store
        if layout == "asset_ref" and asset_store is None:
            raise ValueError("asset_ref requires LocalAssetStore")
        self._owned = False
        self._queries = {}
        self._merges_stopped = set()
        self._target_watermark = 0
        self._asset_ids = set()

    def connect_worker(self):
        """建立供一个 worker 复用且由调用方关闭的 HTTP 连接。"""
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.connect()
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _json_rows(body):
        """解析完整读取的 JSONEachRow 响应。"""
        try:
            return [json.loads(line) for line in body.splitlines() if line]
        except json.JSONDecodeError as error:
            raise RuntimeError("invalid ClickHouse JSONEachRow response") from error

    def _request(self, connection, statement, body=None, parameters=None, query_id=None,
                 return_request_body_bytes=False):
        """发送 SQL/HTTP 请求并在返回前完整读取响应。"""
        if query_id is not None and not QUERY_ID.fullmatch(query_id):
            raise ValueError("query_id contains unsupported characters")
        query = {
            "log_queries": 1 if query_id else 0,
            "log_processors_profiles": 0,
            "memory_profiler_step": 0,
            "log_query_settings": 0,
        }
        if query_id:
            query["query_id"] = query_id
        if parameters:
            query.update({"param_" + key: str(value) for key, value in parameters.items()})
        path = "/?" + urllib.parse.urlencode(query)
        payload = statement if body is None else statement + "\n" + body
        request_body = payload.encode("utf-8")
        try:
            connection.request("POST", path, body=request_body,
                               headers={"Content-Type": "text/plain; charset=utf-8"})
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
        except (OSError, http.client.HTTPException) as error:
            raise RuntimeError("ClickHouse HTTP request failed") from error
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ClickHouse request failed ({response.status}): {response_body.strip()}")
        if return_request_body_bytes:
            return response_body, len(request_body)
        return response_body

    def namespace_exists(self):
        """返回本 adapter database 当前是否存在。"""
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(
                connection,
                "SELECT count() AS count FROM system.databases WHERE name={database:String} FORMAT JSONEachRow",
                parameters={"database": self.database},
            ))
            return bool(rows and int(rows[0]["count"]))
        finally:
            connection.close()

    def create(self):
        """创建独占 database，并清理由当前调用创建的部分对象。"""
        if self.namespace_exists():
            raise ValueError(f"database already exists: {self.database}")
        ddl = create_layout_ddl(self.namespace, self.layout)
        connection = self.connect_worker()
        created = False
        try:
            statements = [statement for statement in ddl.removesuffix(";").split(";\n") if statement]
            self._request(connection, statements[0])
            created = True
            self._owned = True
            for statement in statements[1:]:
                self._request(connection, statement)
            return {"database": self.database, "ddl": ddl}
        except Exception as error:
            if created:
                try:
                    self.cleanup()
                except Exception as cleanup_error:
                    error.add_note(f"database creation cleanup failed: {cleanup_error}")
            raise
        finally:
            connection.close()

    def _payload(self, row):
        """按冻结路径读取并核对 payload bytes。"""
        path = row.get("payload_path")
        if path is None:
            return None
        candidate = (self.input_root / path).resolve()
        try:
            candidate.relative_to(self.input_root)
        except ValueError as error:
            raise ValueError("payload path escapes input root") from error
        payload = candidate.read_bytes()
        if len(payload) != row["content_length"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError(f"payload identity mismatch: {row['event_id']}")
        return payload

    @staticmethod
    def _event_row(row, payload=None, include_payload=False, include_asset=False):
        """把冻结事件行转换为 ClickHouse JSONEachRow 对象。"""
        try:
            result = {field: row[field] for field in (
                "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
                "project_id", "end_time", "duration_ms", "span_type", "framework",
                "level", "cohort", "profile", "content_type", "encoding", "content_length",
                "preview", "sha256",
            )}
            result["start_time"] = clickhouse_timestamp(row["start_time"])
            result["end_time"] = clickhouse_timestamp(row["end_time"])
        except (KeyError, TypeError) as error:
            raise ValueError("invalid event row") from error
        if include_asset:
            result["asset_id"] = row["sha256"]
        if include_payload:
            result["payload"] = "" if payload is None else payload.decode("utf-8")
        return result

    @staticmethod
    def _payload_row(row, payload):
        """把事件转换为独立 payload 表的最小行。"""
        result = {field: row[field] for field in (
            "ingest_seq", "event_id", "trace_id", "project_id", "profile",
            "content_type", "encoding", "content_length", "preview", "sha256",
        )}
        result["start_time"] = clickhouse_timestamp(row["start_time"])
        result["payload"] = "" if payload is None else payload.decode("utf-8")
        return result

    def _insert(self, connection, table, rows):
        """通过 JSONEachRow 向一个明确写目标插入完整 block。"""
        body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                       for row in rows)
        _, request_body_bytes = self._request(
            connection, f"INSERT INTO {self.database}.{table} FORMAT JSONEachRow", body,
            return_request_body_bytes=True,
        )
        return request_body_bytes

    def _insert_assets(self, connection, block, payloads):
        """记录 pending，原子发布对象后同步转换为 available。"""
        asset_bytes = 0
        assets_started = time.perf_counter()
        publish_ms = 0.0
        pending = []
        pending_ids = set()
        for row, payload in zip(block, payloads):
            if payload is None or row["sha256"] in self._asset_ids or row["sha256"] in pending_ids:
                continue
            pending.append((row, payload))
            pending_ids.add(row["sha256"])
        catalog_rows = []
        for row, _ in pending:
            path = self.asset_store.object_path(row["sha256"])
            catalog_rows.append({
                "asset_id": row["sha256"], "sha256": row["sha256"],
                "content_type": row["content_type"], "encoding": row["encoding"],
                "content_length": row["content_length"], "storage_path": str(path),
                "status": "pending", "error_category": None,
            })
        database_bytes = self._insert(connection, "assets", catalog_rows) if catalog_rows else 0
        for row, payload in pending:
            try:
                publish_started = time.perf_counter()
                self.asset_store.publish_bytes(row["sha256"], payload)
                publish_ms += (time.perf_counter() - publish_started) * 1000
            except Exception as error:
                database_bytes += self.set_asset_status(
                    row["sha256"], "failed",
                    getattr(error, "category", type(error).__name__),
                )
                raise
            database_bytes += self.set_asset_status(row["sha256"], "available")
            asset_bytes += len(payload)
        self._asset_ids.update(pending_ids)
        return pending_ids, database_bytes, asset_bytes, (time.perf_counter() - assets_started) * 1000, publish_ms

    def ingest_block(self, block):
        """顺序完成 layout 的单写或显式双写并返回联合水位。"""
        if not isinstance(block, list) or not block:
            raise ValueError("block must be a non-empty list")
        payloads = [self._payload(row) for row in block]
        started = time.perf_counter()
        protocol_body_bytes = {}
        asset_submitted = 0
        target_ms = {}
        connection = self.connect_worker()
        try:
            if self.layout == "same_table":
                target_started = time.perf_counter()
                protocol_body_bytes["events"] = self._insert(
                    connection, "events", [self._event_row(row, payload, True)
                                              for row, payload in zip(block, payloads)],
                )
                target_ms["events"] = (time.perf_counter() - target_started) * 1000
            elif self.layout == "separate":
                for table, rows in (
                    ("events_analytics", [self._event_row(row) for row in block]),
                    ("event_payloads", [self._payload_row(row, payload) for row, payload in zip(block, payloads)]),
                ):
                    target_started = time.perf_counter()
                    protocol_body_bytes[table] = self._insert(connection, table, rows)
                    target_ms[table] = (time.perf_counter() - target_started) * 1000
            elif self.layout == "full_core":
                for table, rows in (
                    ("events_full", [self._event_row(row, payload, True) for row, payload in zip(block, payloads)]),
                    ("events_core", [self._event_row(row) for row in block]),
                ):
                    target_started = time.perf_counter()
                    protocol_body_bytes[table] = self._insert(connection, table, rows)
                    target_ms[table] = (time.perf_counter() - target_started) * 1000
            else:
                submitted_asset_ids, catalog_bytes, asset_submitted, assets_ms, publish_ms = self._insert_assets(
                    connection, block, payloads,
                )
                protocol_body_bytes["assets"] = catalog_bytes
                target_ms["assets"] = assets_ms
                target_started = time.perf_counter()
                protocol_body_bytes["events_analytics"] = self._insert(
                    connection, "events_analytics", [self._event_row(row, include_asset=True) for row in block],
                )
                target_ms["events_analytics"] = (time.perf_counter() - target_started) * 1000
        finally:
            connection.close()
        watermark = max(int(row["ingest_seq"]) for row in block) + 1
        self._target_watermark = watermark
        return BlockResult(
            len(block), watermark,
            {table: watermark for table in build_layout_catalog(self.layout).write_tables},
            (time.perf_counter() - started) * 1000,
            write_target_ms=target_ms,
            asset_publish_ms=publish_ms if self.layout == "asset_ref" else 0.0,
            logical_target_row_bytes=logical_target_row_bytes(
                self.layout, block, payloads,
                ({row["sha256"]: str(self.asset_store.object_path(row["sha256"]))
                  for row, payload in zip(block, payloads) if payload is not None}
                 if self.layout == "asset_ref" else None),
                submitted_asset_ids if self.layout == "asset_ref" else None,
            ),
            database_ingest_request_body_bytes=protocol_body_bytes,
            asset_raw_object_bytes=asset_submitted,
        )

    def _watermarks(self):
        """读取各写目标的真实可见联合水位。"""
        result = {}
        connection = self.connect_worker()
        try:
            for table in build_layout_catalog(self.layout).write_tables:
                if table == "assets":
                    rows = self._json_rows(self._request(
                        connection,
                        f"SELECT if(countIf(e.asset_id IS NOT NULL AND (a.asset_id IS NULL OR a.asset_id='' OR a.status!='available'))=0,toInt64(coalesce(max(e.ingest_seq)+1,0)),toInt64(-1)) AS watermark "
                        f"FROM {self.database}.events_analytics e LEFT JOIN {self.database}.assets a ON a.asset_id=e.asset_id FORMAT JSONEachRow",
                    ))
                else:
                    rows = self._json_rows(self._request(
                        connection,
                        f"SELECT coalesce(max(ingest_seq)+1,0) AS watermark FROM {self.database}.{table} FORMAT JSONEachRow",
                    ))
                result[table] = int(rows[0]["watermark"])
            return result
        finally:
            connection.close()

    def wait_write_complete(self, watermark):
        """证明所有写目标的联合水位覆盖指定 block。"""
        started = time.perf_counter()
        values = self._watermarks()
        elapsed = (time.perf_counter() - started) * 1000
        return MaintenanceResult(
            all(value >= watermark for value in values.values()), values,
            wall_ms=elapsed, watermark_wait_ms=elapsed,
        )

    def _physical_snapshot(self):
        """返回 readiness 所需的 active merge 和各表 part 数。"""
        connection = self.connect_worker()
        try:
            merges = self._json_rows(self._request(
                connection,
                "SELECT count() AS count FROM system.merges WHERE database={database:String} FORMAT JSONEachRow",
                parameters={"database": self.database},
            ))
            parts = self._json_rows(self._request(
                connection,
                "SELECT table,count() AS count FROM system.parts WHERE active AND database={database:String} GROUP BY table ORDER BY table FORMAT JSONEachRow",
                parameters={"database": self.database},
            ))
            return {"merges": int(merges[0]["count"]),
                    "parts": {row["table"]: int(row["count"]) for row in parts}}
        finally:
            connection.close()

    def wait_query_ready(self, timeout_seconds=30):
        """等待 merge 连续三次为空且 active part 数不再变化。"""
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = time.monotonic()
        observations, stable, previous = [], 0, None
        while True:
            current = self._physical_snapshot()
            observations.append(current)
            stable = stable + 1 if current["merges"] == 0 and current["parts"] == previous else 1 if current["merges"] == 0 else 0
            if stable >= 3:
                values = self._watermarks()
                completed = all(
                    value >= self._target_watermark for value in values.values()
                )
                wall_ms = (time.monotonic() - started) * 1000
                return MaintenanceResult(
                    completed, values, tuple(observations), wall_ms,
                    merge_wait_ms=wall_ms, watermark_wait_ms=0.0,
                )
            if time.monotonic() - started >= timeout_seconds:
                return MaintenanceResult(False, self._watermarks(), tuple(observations),
                                         (time.monotonic() - started) * 1000)
            previous = current["parts"]
            time.sleep(0.1)

    def set_merges(self, enabled):
        """启停本布局所有 MergeTree 写目标，并跟踪恢复责任。"""
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        action = "START" if enabled else "STOP"
        connection = self.connect_worker()
        try:
            for table in build_layout_catalog(self.layout).write_tables:
                self._request(connection, f"SYSTEM {action} MERGES {self.database}.{table}")
                if enabled:
                    self._merges_stopped.discard(table)
                else:
                    self._merges_stopped.add(table)
        finally:
            connection.close()

    def set_asset_status(self, asset_id, status, error_category=None):
        """同步执行 catalog 状态转换，供故障实验控制。"""
        if self.layout != "asset_ref" or status not in ASSET_STATUSES:
            raise ValueError("invalid asset status transition")
        connection = self.connect_worker()
        try:
            error = "NULL" if error_category is None else "{error:String}"
            _, request_body_bytes = self._request(
                connection,
                f"ALTER TABLE {self.database}.assets UPDATE status={{status:String}},error_category={error},updated_at=now64(3) "
                "WHERE asset_id={asset_id:String} SETTINGS mutations_sync=2",
                parameters={"status": status, "asset_id": asset_id,
                            **({"error": error_category} if error_category is not None else {})},
                return_request_body_bytes=True,
            )
            return request_body_bytes
        finally:
            connection.close()

    def _read_asset_record(self, asset_id):
        """读取真实 catalog 行，并返回对应 HTTP body bytes。"""
        if self.layout != "asset_ref":
            return None, 0
        connection = self.connect_worker()
        try:
            body = self._request(
                connection,
                f"SELECT asset_id,sha256,content_type,encoding,content_length,storage_path,toString(status) AS status,"
                f"toString(updated_at) AS updated_at,error_category FROM {self.database}.assets "
                "WHERE asset_id={asset_id:String} ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow",
                parameters={"asset_id": asset_id},
            )
            rows = self._json_rows(body)
        finally:
            connection.close()
        if not rows:
            return None, len(body.encode("utf-8"))
        row = rows[0]
        record = AssetRecord(
            row["asset_id"], row["sha256"], row["content_type"], row["encoding"],
            int(row["content_length"]), Path(row["storage_path"]), row["status"],
            row["updated_at"], row.get("error_category"),
        )
        return record, len(body.encode("utf-8"))

    def get_available(self, asset_id):
        """返回匹配 catalog 行的真实状态，不折叠未发布状态。"""
        return self._read_asset_record(asset_id)[0]

    def _query_statement(self, query):
        """生成统一 QuerySpec 对应的 ClickHouse SQL 和绑定参数。"""
        params = query.parameters
        if self.layout == "same_table":
            source, prefix, payload = f"{self.database}.events", "", "payload"
        elif self.layout == "full_core":
            table = "events_core" if query.kind in {"list", "preview"} else "events_full"
            source, prefix = f"{self.database}.{table}", ""
            payload = "''" if table == "events_core" else "payload"
        elif self.layout == "separate":
            if query.kind in {"list", "preview"}:
                source, prefix, payload = f"{self.database}.events_analytics", "", "''"
            else:
                source = (f"{self.database}.events_analytics AS a LEFT JOIN {self.database}.event_payloads AS p "
                          "ON p.event_id=a.event_id")
                prefix, payload = "a.", "p.payload"
        else:
            source, prefix, payload = f"{self.database}.events_analytics", "", "asset_id"
        fields = []
        for field in LOGICAL_FIELDS:
            expression = "CAST(NULL AS Nullable(String))" if query.kind == "list" and field == "preview" else prefix + field
            fields.append(f"{expression} AS {field}")
        if query.kind in {"list", "preview"}:
            payload = "CAST(NULL AS Nullable(String))"
        fields.append(payload + " AS payload_value")
        select = ",".join(fields)
        values = {}
        if query.kind in {"list", "preview"}:
            statement = (f"SELECT {select} FROM {source} WHERE project_id={{project_id:String}} "
                         "AND start_time>={start_time:DateTime64(3,'UTC')} AND start_time<{end_time:DateTime64(3,'UTC')} "
                         "AND (start_time,event_id)>({cursor_time:DateTime64(3,'UTC')},{cursor_id:String}) "
                         "ORDER BY start_time,event_id LIMIT {page_size:UInt64}")
            values = {"project_id": params["project_id"], "start_time": clickhouse_timestamp(params["start_time"]),
                      "end_time": clickhouse_timestamp(params["end_time"]),
                      "cursor_time": clickhouse_timestamp(params.get("cursor_time", "1900-01-01T00:00:00.000Z")),
                      "cursor_id": params.get("cursor_id", ""), "page_size": params.get("page_size", 256)}
        elif query.kind == "detail":
            statement = (f"SELECT {select} FROM {source} WHERE {prefix}project_id={{project_id:String}} "
                         f"AND {prefix}trace_id={{trace_id:String}} AND {prefix}start_time={{start_time:DateTime64(3,'UTC')}} "
                         f"AND {prefix}event_id={{event_id:String}}")
            values = {key: params[key] for key in ("project_id", "trace_id", "event_id")}
            values["start_time"] = clickhouse_timestamp(params["start_time"])
        elif query.kind == "trace":
            statement = (f"SELECT {select} FROM {source} WHERE {prefix}project_id={{project_id:String}} "
                         f"AND {prefix}trace_id={{trace_id:String}} AND {prefix}start_time>={{start_time:DateTime64(3,'UTC')}} "
                         f"AND {prefix}start_time<{{end_time:DateTime64(3,'UTC')}} ORDER BY {prefix}start_time,{prefix}event_id")
            values = {"project_id": params["project_id"], "trace_id": params["trace_id"],
                      "start_time": clickhouse_timestamp(params["start_time"]),
                      "end_time": clickhouse_timestamp(params["end_time"])}
        else:
            statement = (
                f"SELECT {select} FROM {source} WHERE {prefix}cohort={{cohort:String}} "
                f"AND {prefix}sha256 IS NOT NULL ORDER BY {prefix}start_time,{prefix}event_id"
            )
            values = {"cohort": params["cohort"]}
        return statement + " FORMAT JSONEachRow", values

    @staticmethod
    def _timestamp(value):
        text = value.replace(" ", "T")
        if "." not in text:
            text += ".000"
        whole, fraction = text.split(".", 1)
        return whole + "." + fraction[:3].ljust(3, "0") + "Z"

    def _normalize_rows(self, rows):
        """恢复统一逻辑字段，并把完整 String payload 转为原始 bytes。"""
        result = []
        catalog_response_bytes = 0
        resolver_requests = 0
        resolver_read_ms = 0.0
        for raw in rows:
            item = {field: raw[field] for field in LOGICAL_FIELDS}
            item["start_time"] = self._timestamp(item["start_time"])
            if self.layout == "asset_ref" and raw["payload_value"] is not None:
                record, response_bytes = self._read_asset_record(raw["payload_value"])
                catalog_response_bytes += response_bytes
                reference = AssetReference(
                    "asset:sha256:" + raw["payload_value"], item["content_type"], item["encoding"],
                    int(item["content_length"]), item["preview"],
                )
                resolve_started = time.perf_counter()
                item["payload"] = AssetResolver(
                    _CatalogRow(record), self.asset_store,
                ).resolve(reference).payload
                resolver_read_ms += (time.perf_counter() - resolve_started) * 1000
                resolver_requests += 1
            elif item["sha256"] is None or raw["payload_value"] is None:
                item["payload"] = None
            else:
                item["payload"] = raw["payload_value"].encode("utf-8")
            result.append(item)
        return tuple(result), catalog_response_bytes, resolver_requests, resolver_read_ms

    def run_query(self, query: QuerySpec):
        """以唯一 query ID 执行并完整读取、恢复查询结果。"""
        if not isinstance(query, QuerySpec):
            raise ValueError("query must be QuerySpec")
        query_id = "json_s3_" + query.kind + "_" + uuid.uuid4().hex
        statement, values = self._query_statement(query)
        connection = self.connect_worker()
        started = time.perf_counter()
        try:
            body = self._request(connection, statement, parameters=values, query_id=query_id)
        finally:
            connection.close()
        query_ms = (time.perf_counter() - started) * 1000
        recovery_started = time.perf_counter()
        normalized, catalog_response_bytes, resolver_requests, resolver_read_ms = self._normalize_rows(
            self._json_rows(body)
        )
        recovery_ms = (time.perf_counter() - recovery_started) * 1000
        resolver_payload_bytes = (
            sum(len(row["payload"] or b"") for row in normalized)
            if self.layout == "asset_ref" else 0
        )
        response_bytes = logical_response_bytes(normalized)
        database_response_bytes = response_bytes - resolver_payload_bytes
        protocol_bytes = len(body.encode("utf-8")) + catalog_response_bytes
        self._queries[query_id] = (statement, values, query.kind)
        return QueryResult(
            query_id, normalized, response_bytes,
            database_response_bytes, resolver_payload_bytes, query_ms, recovery_ms,
            protocol_bytes, resolver_requests, resolver_read_ms,
        )

    def _collect_query_finish(self, query_ids, attempts=20):
        """单次 flush 后轮询全部正式查询的 QueryFinish。"""
        if not query_ids:
            return {}
        expected = set(query_ids)
        params = {"query_ids": "[" + ",".join("'" + value + "'" for value in query_ids) + "]"}
        connection = self.connect_worker()
        try:
            self._request(connection, "SYSTEM FLUSH LOGS query_log")
            for attempt in range(attempts):
                rows = self._json_rows(self._request(connection, query_finish_sql(), parameters=params))
                found = {}
                for row in rows:
                    query_id = row.get("query_id")
                    if query_id not in expected or query_id in found:
                        raise RuntimeError(f"unexpected or duplicate QueryFinish: {query_id}")
                    if row.get("type") != "QueryFinish" or int(row.get("exception_code", -1)) != 0:
                        raise RuntimeError(f"invalid QueryFinish: {query_id}")
                    found[query_id] = {field: (row[field] if field == "type" else int(row[field]))
                                       for field in QUERY_FINISH_FIELDS}
                if set(found) == expected:
                    return found
                if attempt < attempts - 1:
                    time.sleep(0.05)
            raise RuntimeError("missing QueryFinish rows: " + ",".join(sorted(expected - set(found))))
        finally:
            connection.close()

    def collect_access_evidence(self, query_ids):
        """返回正式查询计划和对应唯一 QueryFinish 指标。"""
        if len(set(query_ids)) != len(query_ids) or any(query_id not in self._queries for query_id in query_ids):
            raise ValueError("query_ids must be unique completed queries")
        connection = self.connect_worker()
        try:
            plans = {query_id: self._request(
                connection, "EXPLAIN indexes=1 " + self._queries[query_id][0],
                parameters=self._queries[query_id][1],
            ).strip() for query_id in query_ids}
        finally:
            connection.close()
        if any(not plan for plan in plans.values()):
            raise RuntimeError("empty ClickHouse query plan")
        query_finish = self._collect_query_finish(query_ids)
        details = {}
        for query_id in query_ids:
            statement, _, kind = self._queries[query_id]
            details[query_id] = {
                "kind": kind,
                "statement": statement,
                "payload_selected": kind in {"detail", "trace", "batch"},
                "declared_source": "events_analytics" if self.layout == "asset_ref" else (
                    build_layout_catalog(self.layout).list_source
                    if kind in {"list", "preview"} else build_layout_catalog(self.layout).detail_source
                ),
                "scanned_rows": query_finish[query_id]["read_rows"],
                "scanned_bytes": query_finish[query_id]["read_bytes"],
                "scanned_bytes_status": "observed",
            }
        return AccessEvidence(
            plans, query_finish=query_finish, query_details=details,
        )

    def audit_dataset(self):
        """传回重建逻辑行及每个物理写目标的独立有序审计。"""
        if self.layout in {"same_table", "full_core"}:
            table = "events" if self.layout == "same_table" else "events_full"
            source, prefix, payload = f"{self.database}.{table}", "", "payload"
        elif self.layout == "separate":
            source = (f"{self.database}.events_analytics a LEFT JOIN "
                      f"{self.database}.event_payloads p ON p.event_id=a.event_id")
            prefix, payload = "a.", "p.payload"
        else:
            source, prefix, payload = f"{self.database}.events_analytics", "", "asset_id"
        fields = ",".join(f"{prefix}{field} AS {field}" for field in LOGICAL_FIELDS)
        statement = (f"SELECT {prefix}ingest_seq AS ingest_seq,{fields},{payload} AS payload_value "
                     f"FROM {source} ORDER BY {prefix}ingest_seq FORMAT JSONEachRow")
        connection = self.connect_worker()
        try:
            body = self._request(connection, statement)
            target_audits = {}
            target_protocol_bytes = 0
            for target in build_layout_catalog(self.layout).write_tables:
                if target == "assets":
                    fields = (
                        "asset_id", "sha256", "content_type", "encoding", "content_length", "status",
                    )
                    target_statement = (
                        f"SELECT asset_id,sha256,content_type,encoding,content_length,toString(status) AS status "
                        f"FROM {self.database}.assets ORDER BY asset_id FORMAT JSONEachRow"
                    )
                    identity = "asset_id"
                    mapping_body = self._request(
                        connection,
                        f"SELECT ingest_seq,event_id,asset_id FROM {self.database}.events_analytics "
                        "WHERE asset_id IS NOT NULL ORDER BY ingest_seq FORMAT JSONEachRow",
                    )
                    target_protocol_bytes += len(mapping_body.encode("utf-8"))
                    event_mappings = tuple(self._json_rows(mapping_body))
                else:
                    fields = PAYLOAD_AUDIT_FIELDS if target == "event_payloads" else EVENT_AUDIT_FIELDS
                    if target == "events_analytics" and self.layout == "asset_ref":
                        fields += ("asset_id",)
                    selected = ",".join(f"{field} AS {field}" for field in fields)
                    target_statement = (
                        f"SELECT {selected} FROM {self.database}.{target} "
                        "ORDER BY ingest_seq FORMAT JSONEachRow"
                    )
                    identity = "event_id"
                    event_mappings = ()
                target_body = self._request(connection, target_statement)
                target_protocol_bytes += len(target_body.encode("utf-8"))
                projected = self._json_rows(target_body)
                for item in projected:
                    for timestamp in ("start_time", "end_time"):
                        if item.get(timestamp) is not None:
                            item[timestamp] = self._timestamp(item[timestamp])
                duplicate_identities = len(projected) - len({row[identity] for row in projected})
                target_audits[target] = PhysicalTargetAudit(
                    tuple(projected), duplicate_identities, event_mappings,
                )
        finally:
            connection.close()
        raw_rows = self._json_rows(body)
        normalized, catalog_bytes, _, _ = self._normalize_rows(raw_rows)
        rows = tuple({"ingest_seq": int(raw["ingest_seq"]), **row}
                     for raw, row in zip(raw_rows, normalized))
        duplicate_count = len(rows) - len({row["event_id"] for row in rows})
        return DatasetAudit(
            rows, duplicate_count, logical_response_bytes(rows),
            len(body.encode("utf-8")) + catalog_bytes + target_protocol_bytes,
            target_audits,
        )

    def collect_storage(self):
        """返回 active parts、marks、列压缩字节和 active merge。"""
        connection = self.connect_worker()
        try:
            part_rows = self._json_rows(self._request(connection, storage_statements(self.database)[0]))
            column_rows = self._json_rows(self._request(connection, storage_statements(self.database)[1]))
            merge_rows = self._json_rows(self._request(connection, storage_statements(self.database)[2]))
        finally:
            connection.close()
        columns = {}
        for row in column_rows:
            columns.setdefault(row["table"], {})[row["column"]] = {
                "compressed_bytes": int(row["compressed_bytes"]),
                "uncompressed_bytes": int(row["uncompressed_bytes"]),
            }
        tables = {row["table"]: {
            "part_count": int(row["part_count"]), "rows": int(row["rows"]),
            "marks": int(row["marks"]), "compressed_bytes": int(row["compressed_bytes"]),
            "uncompressed_bytes": int(row["uncompressed_bytes"]),
            "columns": columns.get(row["table"], {}),
        } for row in part_rows}
        for table in build_layout_catalog(self.layout).write_tables:
            tables.setdefault(table, {"part_count": 0, "rows": 0, "marks": 0,
                                      "compressed_bytes": 0, "uncompressed_bytes": 0,
                                      "columns": columns.get(table, {})})
        asset_store = self._collect_asset_storage() if self.layout == "asset_ref" else None
        return StorageEvidence(tables, tuple(merge_rows), asset_store)

    def _collect_asset_storage(self):
        """通过事件引用和 LocalAssetStore 统计可达对象及 orphan bytes。"""
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(
                connection,
                f"SELECT DISTINCT a.asset_id AS asset_id,a.sha256 AS sha256,a.content_length AS content_length,"
                f"a.storage_path AS storage_path FROM {self.database}.events_analytics e "
                f"INNER JOIN {self.database}.assets a ON a.asset_id=e.asset_id "
                "WHERE a.status='available' FORMAT JSONEachRow",
            ))
        finally:
            connection.close()
        reachable_paths = set()
        available_bytes = 0
        for row in rows:
            path = Path(row["storage_path"])
            payload = self.asset_store.read_bytes(row["asset_id"], path)
            if (
                row["asset_id"] != row["sha256"]
                or len(payload) != int(row["content_length"])
                or hashlib.sha256(payload).hexdigest() != row["asset_id"]
            ):
                raise RuntimeError(f"invalid published asset: {row['asset_id']}")
            reachable_paths.add(path)
            available_bytes += len(payload)
        orphans = self.asset_store.find_orphans(reachable_paths)
        orphan_bytes = 0
        for path in orphans:
            payload = self.asset_store.read_bytes(path.name, path)
            if hashlib.sha256(payload).hexdigest() != path.name:
                raise RuntimeError(f"invalid orphan asset: {path.name}")
            orphan_bytes += len(payload)
        return AssetStorageEvidence(
            len(rows), available_bytes, len(orphans), orphan_bytes,
        )

    def cleanup(self):
        """恢复本 adapter 暂停的 merge，再删除并确认独占 database。"""
        if self._owned and self._merges_stopped:
            self.set_merges(True)
        if not self._owned:
            return CleanupResult(self.database, not self.namespace_exists())
        connection = self.connect_worker()
        try:
            self._request(connection, f"DROP DATABASE IF EXISTS {self.database} SYNC")
        finally:
            connection.close()
        if self.namespace_exists():
            raise RuntimeError(f"database cleanup failed: {self.database}")
        self._owned = False
        return CleanupResult(self.database, True)
