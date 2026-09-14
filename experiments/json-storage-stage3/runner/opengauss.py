"""阶段三 openGauss 四种 payload 布局 adapter。"""

import json
import re
import subprocess
import time
import uuid
from pathlib import Path

import psycopg

from assets import AssetRecord, AssetReference, AssetResolver, LocalAssetStore
from common import (
    AccessEvidence, BlockResult, CleanupResult, LAYOUTS, MaintenanceResult,
    QueryResult, QuerySpec, StorageEvidence, build_layout_catalog,
)


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
ASSET_STATUSES = ("pending", "available", "failed", "deleting")
LOGICAL_FIELDS = (
    "event_id", "trace_id", "project_id", "start_time", "cohort", "profile",
    "content_type", "encoding", "content_length", "preview", "sha256",
)


def validate_identifier(value, name):
    """验证 schema 和对象名可安全直接进入 SQL identifier。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match ^[a-z][a-z0-9_]{{0,62}}$")
    return value


def schema_name(namespace, layout):
    """返回 layout 独占的 schema 名。"""
    validate_identifier(namespace, "namespace")
    build_layout_catalog(layout)
    return validate_identifier(f"{namespace}_{layout}", "schema name")


def _event_columns(include_payload=False, include_asset=False):
    columns = """ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
project_id TEXT NOT NULL, start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, duration_ms BIGINT NOT NULL,
span_type TEXT NOT NULL, framework TEXT NOT NULL, level TEXT NOT NULL,
cohort TEXT, profile TEXT, content_type TEXT, encoding TEXT,
content_length BIGINT, preview TEXT, sha256 TEXT"""
    if include_asset:
        columns += ", asset_id TEXT"
    if include_payload:
        columns += ", payload TEXT"
    return columns


def _payload_columns():
    """返回独立 payload 表的定位键、内容元数据与 payload 列。"""
    return """ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
trace_id TEXT NOT NULL, project_id TEXT NOT NULL,
start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, profile TEXT,
content_type TEXT, encoding TEXT, content_length BIGINT, preview TEXT, sha256 TEXT,
payload TEXT"""


def _indexes(schema, table):
    """返回列表和 Trace 实际负载所需的两个 B-tree 索引。"""
    return [
        f"CREATE INDEX {table}_list_idx ON {schema}.{table} (project_id,start_time,event_id)",
        f"CREATE INDEX {table}_trace_idx ON {schema}.{table} (project_id,trace_id,start_time,event_id)",
    ]


def create_layout_ddls(namespace, layout):
    """返回四种布局的完整 schema、表和访问结构 DDL。"""
    schema = schema_name(namespace, layout)
    statements = [f"CREATE SCHEMA {schema}"]
    if layout == "same_table":
        statements.append(f"CREATE TABLE {schema}.events ({_event_columns(include_payload=True)})")
        statements.extend(_indexes(schema, "events"))
    elif layout == "separate":
        statements.append(f"CREATE TABLE {schema}.events_analytics ({_event_columns()})")
        statements.append(f"CREATE TABLE {schema}.event_payloads ({_payload_columns()})")
        statements.extend(_indexes(schema, "events_analytics"))
        statements.extend(_indexes(schema, "event_payloads"))
    elif layout == "full_core":
        statements.append(f"CREATE TABLE {schema}.events_full ({_event_columns(include_payload=True)})")
        statements.append(f"CREATE TABLE {schema}.events_core ({_event_columns()})")
        statements.extend(_indexes(schema, "events_full"))
        statements.extend(_indexes(schema, "events_core"))
    else:
        statements.append(f"CREATE TABLE {schema}.events_analytics ({_event_columns(include_asset=True)})")
        statements.extend(_indexes(schema, "events_analytics"))
        states = ",".join(f"'{status}'" for status in ASSET_STATUSES)
        statements.append(f"""CREATE TABLE {schema}.assets (
asset_id TEXT NOT NULL PRIMARY KEY, sha256 TEXT NOT NULL, content_type TEXT NOT NULL,
encoding TEXT NOT NULL, content_length BIGINT NOT NULL, storage_path TEXT NOT NULL,
status TEXT NOT NULL CHECK (status IN ({states})), updated_at TIMESTAMP(6) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
error_category TEXT)""")
    return ";\n".join(statements) + ";"


def storage_sql(schema, table):
    """返回 heap、索引、TOAST 和 relation 总空间的分项 SQL。"""
    relation = f"{schema}.{table}"
    return (
        "SELECT pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass), "
        "COALESCE((SELECT pg_total_relation_size(reltoastrelid) FROM pg_class "
        "WHERE oid=%s::regclass AND reltoastrelid<>0),0), pg_total_relation_size(%s::regclass)"
    ) % tuple("'" + relation + "'" for _ in range(4))


def access_evidence_sql(schema):
    """返回 namespace 内索引实际 idx_scan 计数 SQL。"""
    validate_identifier(schema, "schema")
    return (
        "SELECT indexrelname,idx_scan FROM pg_stat_user_indexes "
        f"WHERE schemaname='{schema}' ORDER BY indexrelname"
    )


def explain_sql(statement):
    """将正式绑定查询转换为 EXPLAIN ANALYZE 证据查询。"""
    return "EXPLAIN ANALYZE " + statement


class OpenGaussAdapter:
    """实现阶段三 openGauss layout 的写入、查询和证据接口。"""

    def __init__(self, host, port, container_name, namespace, layout,
                 input_root: Path, asset_store: LocalAssetStore | None = None):
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be positive")
        self.layout = build_layout_catalog(layout).name
        self.namespace = namespace
        self.schema = schema_name(namespace, layout)
        self.host, self.port, self.container_name = host, port, container_name
        self.input_root = Path(input_root).resolve()
        self.asset_store = asset_store
        if layout == "asset_ref" and asset_store is None:
            raise ValueError("asset_ref requires LocalAssetStore")
        self._password = None
        self._owned = False
        self._queries = {}

    def _password_from_container(self):
        """从已运行容器读取密码并仅缓存在 adapter 内存。"""
        if self._password is not None:
            return self._password
        completed = subprocess.run([
            "docker", "inspect", self.container_name, "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ], capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError("unable to inspect openGauss container environment")
        for line in completed.stdout.splitlines():
            if line.startswith("GS_PASSWORD="):
                self._password = line.split("=", 1)[1]
                return self._password
        raise RuntimeError("GS_PASSWORD is not configured on openGauss container")

    def connect_worker(self):
        """建立由调用方负责关闭的独立数据库连接。"""
        return psycopg.connect(host=self.host, port=self.port, dbname="postgres",
                               user="gaussdb", password=self._password_from_container(),
                               autocommit=False)

    def namespace_exists(self):
        """返回本 adapter schema 当前是否存在。"""
        connection = self.connect_worker()
        try:
            return bool(connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s)",
                (self.schema,),
            ).fetchone()[0])
        finally:
            connection.close()

    def create(self):
        """创建独占 schema，并在部分失败时回收已经创建的对象。"""
        if self.namespace_exists():
            raise ValueError(f"schema already exists: {self.schema}")
        ddl = create_layout_ddls(self.namespace, self.layout)
        connection = self.connect_worker()
        try:
            try:
                with connection.transaction():
                    for statement in ddl.removesuffix(";").split(";\n"):
                        connection.execute(statement)
                self._owned = True
            except Exception:
                connection.rollback()
                raise
            return {"schema": self.schema, "ddl": ddl}
        finally:
            connection.close()

    @staticmethod
    def _row_values(row, payload=None, asset_id_marker=False):
        """把冻结事件行转换为固定列顺序的 COPY tuple。"""
        try:
            values = tuple(row[field] for field in (
                "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
                "project_id", "start_time", "end_time", "duration_ms", "span_type",
                "framework", "level", "cohort", "profile", "content_type", "encoding",
                "content_length", "preview", "sha256",
            ))
        except (KeyError, TypeError) as error:
            raise ValueError("invalid event row") from error
        if asset_id_marker:
            values += (row["sha256"],)
        if payload is not None or "payload" in row:
            values += (payload,)
        return values

    def _payload(self, row):
        """按冻结相对路径读取并核对 payload bytes。"""
        path = row.get("payload_path")
        if path is None:
            return None
        candidate = (self.input_root / path).resolve()
        try:
            candidate.relative_to(self.input_root)
        except ValueError as error:
            raise ValueError("payload path escapes input root") from error
        payload = candidate.read_bytes()
        import hashlib
        if len(payload) != row["content_length"] or hashlib.sha256(payload).hexdigest() != row["sha256"]:
            raise ValueError(f"payload identity mismatch: {row['event_id']}")
        return payload

    @staticmethod
    def _copy(cursor, relation, rows, include_payload=False, include_asset=False):
        """COPY 已转换的 tuple 到指定布局表。"""
        fields = [
            "ingest_seq", "event_id", "trace_id", "span_id", "parent_span_id",
            "project_id", "start_time", "end_time", "duration_ms", "span_type",
            "framework", "level", "cohort", "profile", "content_type", "encoding",
            "content_length", "preview", "sha256",
        ]
        if include_asset:
            fields.append("asset_id")
        if include_payload:
            fields.append("payload")
        with cursor.copy(f"COPY {relation}({','.join(fields)}) FROM STDIN") as copy:
            for values in rows:
                copy.write_row(values)

    @staticmethod
    def _copy_payloads(cursor, relation, block, payloads):
        """COPY 独立 payload 表的最小定位与内容列。"""
        fields = (
            "ingest_seq", "event_id", "trace_id", "project_id", "start_time", "profile",
            "content_type", "encoding", "content_length", "preview", "sha256", "payload",
        )
        with cursor.copy(f"COPY {relation}({','.join(fields)}) FROM STDIN") as copy:
            for row, payload in zip(block, payloads):
                copy.write_row(tuple(row[field] for field in fields[:-1]) +
                               (None if payload is None else payload.decode("utf-8"),))

    def _insert_assets(self, connection, block, payloads):
        """依次记录 pending、原子发布对象并转换为 available。"""
        for row, payload in zip(block, payloads):
            if payload is None:
                continue
            path = self.asset_store.object_path(row["sha256"])
            connection.execute(
                f"INSERT INTO {self.schema}.assets(asset_id,sha256,content_type,encoding,content_length,storage_path,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'pending')",
                (row["sha256"], row["sha256"], row["content_type"], row["encoding"],
                 row["content_length"], str(path)),
            )
            try:
                self.asset_store.publish_bytes(row["sha256"], payload)
            except Exception:
                connection.execute(
                    f"UPDATE {self.schema}.assets SET status='failed',error_category='publish',updated_at=CURRENT_TIMESTAMP WHERE asset_id=%s",
                    (row["sha256"],),
                )
                raise
            connection.execute(
                f"UPDATE {self.schema}.assets SET status='available',error_category=NULL,updated_at=CURRENT_TIMESTAMP WHERE asset_id=%s",
                (row["sha256"],),
            )

    def ingest_block(self, block):
        """在单事务中完成 layout 的一个显式单写或双写 block。"""
        if not isinstance(block, list) or not block:
            raise ValueError("block must be a non-empty list")
        payloads = [self._payload(row) for row in block]
        started = time.perf_counter()
        catalog = build_layout_catalog(self.layout)
        relation = lambda table: f"{self.schema}.{table}"
        connection = self.connect_worker()
        try:
            try:
                with connection.cursor() as cursor:
                    if self.layout == "same_table":
                        converted = [self._row_values({**row, "payload": True}, None if payload is None else payload.decode("utf-8"))
                                     for row, payload in zip(block, payloads)]
                        self._copy(cursor, relation("events"), converted, include_payload=True)
                    elif self.layout == "separate":
                        self._copy(cursor, relation("events_analytics"), [self._row_values(row) for row in block])
                        self._copy_payloads(cursor, relation("event_payloads"), block, payloads)
                    elif self.layout == "full_core":
                        converted = [self._row_values({**row, "payload": True}, None if payload is None else payload.decode("utf-8"))
                                     for row, payload in zip(block, payloads)]
                        self._copy(cursor, relation("events_full"), converted, include_payload=True)
                        self._copy(cursor, relation("events_core"), [self._row_values(row) for row in block])
                    else:
                        self._insert_assets(connection, block, payloads)
                        converted = [self._row_values(row, asset_id_marker=True) for row in block]
                        self._copy(cursor, relation("events_analytics"), converted, include_asset=True)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()
        watermark = max(int(row["ingest_seq"]) for row in block) + 1
        return BlockResult(len(block), watermark,
                           {table: watermark for table in catalog.write_tables},
                           (time.perf_counter() - started) * 1000)

    def _watermarks(self):
        """读取各写目标的可见联合水位。"""
        catalog = build_layout_catalog(self.layout)
        connection = self.connect_worker()
        try:
            result = {}
            for table in catalog.write_tables:
                if table == "assets":
                    row = connection.execute(
                        f"SELECT COALESCE(MAX(e.ingest_seq)+1,0),count(*) FILTER (WHERE e.asset_id IS NOT NULL AND a.status='available'),"
                        f"count(*) FILTER (WHERE e.asset_id IS NOT NULL AND a.status IS DISTINCT FROM 'available') "
                        f"FROM {self.schema}.events_analytics e LEFT JOIN {self.schema}.assets a ON a.asset_id=e.asset_id"
                    ).fetchone()
                    result[table] = int(row[0]) if int(row[2]) == 0 else -1
                else:
                    value = connection.execute(
                        f"SELECT COALESCE(MAX(ingest_seq)+1,0) FROM {self.schema}.{table}"
                    ).fetchone()[0]
                    result[table] = int(value)
            return result
        finally:
            connection.close()

    def wait_write_complete(self, watermark):
        """证明所有写目标的联合水位覆盖指定 block。"""
        values = self._watermarks()
        return MaintenanceResult(all(value >= watermark for value in values.values()), values)

    def wait_query_ready(self, timeout_seconds=30):
        """对全部实际查询表执行 ANALYZE 并返回维护耗时。"""
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = time.perf_counter()
        connection = self.connect_worker()
        try:
            connection.autocommit = True
            connection.execute(f"SET statement_timeout='{timeout_seconds * 1000}ms'")
            for table in build_layout_catalog(self.layout).write_tables:
                connection.execute(f"ANALYZE {self.schema}.{table}")
        finally:
            connection.close()
        values = self._watermarks()
        return MaintenanceResult(True, values, ({"analyzed": True},),
                                 (time.perf_counter() - started) * 1000)

    def set_asset_status(self, asset_id, status, error_category=None):
        """显式转换 catalog 状态，供故障实验控制。"""
        if self.layout != "asset_ref" or status not in ASSET_STATUSES:
            raise ValueError("invalid asset status transition")
        connection = self.connect_worker()
        try:
            with connection.transaction():
                cursor = connection.execute(
                    f"UPDATE {self.schema}.assets SET status=%s,error_category=%s,updated_at=CURRENT_TIMESTAMP WHERE asset_id=%s",
                    (status, error_category, asset_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"unknown asset: {asset_id}")
        finally:
            connection.close()

    def get_available(self, asset_id):
        """返回匹配 catalog 行的真实状态，不在 SQL 中过滤 available。"""
        if self.layout != "asset_ref":
            return None
        connection = self.connect_worker()
        try:
            row = connection.execute(
                f"SELECT asset_id,sha256,content_type,encoding,content_length,storage_path,status,updated_at,error_category "
                f"FROM {self.schema}.assets WHERE asset_id=%s", (asset_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return AssetRecord(row[0], row[1], row[2], row[3], int(row[4]), Path(row[5]),
                           row[6], row[7].isoformat() if row[7] else None, row[8])

    def _query_statement(self, query):
        """生成固定 QuerySpec 的 SQL、参数和 payload 是否来自 Asset。"""
        params = query.parameters
        if self.layout == "same_table":
            source, alias, payload = f"{self.schema}.events", "", "payload"
        elif self.layout == "full_core":
            table = "events_core" if query.kind in {"list", "preview"} else "events_full"
            source, alias = f"{self.schema}.{table}", ""
            payload = "NULL::text" if table == "events_core" else "payload"
        elif self.layout == "separate":
            if query.kind in {"list", "preview"}:
                source, alias, payload = f"{self.schema}.events_analytics", "", "NULL::text"
            else:
                source = (f"{self.schema}.events_analytics a LEFT JOIN {self.schema}.event_payloads p "
                          "ON p.event_id=a.event_id")
                alias, payload = "a.", "p.payload"
                list_fields = ",".join("a." + field for field in LOGICAL_FIELDS)
        else:
                source, alias, payload = f"{self.schema}.events_analytics", "", "asset_id"
        list_fields = ",".join(
            "NULL::text" if query.kind == "list" and field == "preview" else alias + field
            for field in LOGICAL_FIELDS
        )
        if query.kind in {"list", "preview"}:
            payload = "NULL::text"
        fields = list_fields + "," + payload
        if query.kind in {"list", "preview"}:
            statement = (f"SELECT {fields} FROM {source} WHERE project_id=%s AND start_time>=%s AND start_time<%s "
                         "AND (start_time,event_id)>(%s,%s) ORDER BY start_time,event_id LIMIT %s")
            values = (params["project_id"], params["start_time"], params["end_time"],
                      params.get("cursor_time", "1900-01-01T00:00:00.000Z"),
                      params.get("cursor_id", ""), params.get("page_size", 256))
        elif query.kind == "detail":
            statement = (f"SELECT {fields} FROM {source} WHERE {alias}project_id=%s AND {alias}trace_id=%s "
                         f"AND {alias}start_time=%s AND {alias}event_id=%s")
            values = tuple(params[key] for key in ("project_id", "trace_id", "start_time", "event_id"))
        elif query.kind == "trace":
            statement = (f"SELECT {fields} FROM {source} WHERE {alias}project_id=%s AND {alias}trace_id=%s "
                         f"AND {alias}start_time>=%s AND {alias}start_time<%s ORDER BY {alias}start_time,{alias}event_id")
            values = tuple(params[key] for key in ("project_id", "trace_id", "start_time", "end_time"))
        else:
            statement = f"SELECT {fields} FROM {source} ORDER BY {alias}start_time,{alias}event_id"
            values = ()
        return statement, values

    @staticmethod
    def _timestamp(value):
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _normalize_rows(self, rows):
        """恢复统一逻辑字段并确保 payload bytes 到达调用方。"""
        result = []
        for raw in rows:
            item = dict(zip(LOGICAL_FIELDS, raw[:len(LOGICAL_FIELDS)]))
            item["start_time"] = self._timestamp(item["start_time"])
            if self.layout == "asset_ref" and raw[-1] is not None:
                reference = AssetReference(
                    "asset:sha256:" + raw[-1], item["content_type"], item["encoding"],
                    int(item["content_length"]), item["preview"],
                )
                item["payload"] = AssetResolver(self, self.asset_store).resolve(reference).payload
            else:
                item["payload"] = raw[-1].encode("utf-8") if isinstance(raw[-1], str) else None
            result.append(item)
        return tuple(result)

    def run_query(self, query: QuerySpec):
        """执行查询、完整读取响应并保存后续 EXPLAIN ANALYZE 所需绑定。"""
        if not isinstance(query, QuerySpec):
            raise ValueError("query must be QuerySpec")
        query_id = "json_s3_" + query.kind + "_" + uuid.uuid4().hex
        statement, values = self._query_statement(query)
        connection = self.connect_worker()
        started = time.perf_counter()
        try:
            rows = connection.execute("/* " + query_id + " */ " + statement, values).fetchall()
        finally:
            connection.close()
        query_ms = (time.perf_counter() - started) * 1000
        recovery_started = time.perf_counter()
        normalized = self._normalize_rows(rows)
        recovery_ms = (time.perf_counter() - recovery_started) * 1000
        response_bytes = sum(len(row["payload"] or b"") for row in normalized)
        response_bytes += len(json.dumps([{k: v for k, v in row.items() if k != "payload"}
                                         for row in normalized], ensure_ascii=False).encode("utf-8"))
        self._queries[query_id] = (statement, values)
        return QueryResult(query_id, normalized, response_bytes, query_ms, recovery_ms)

    def collect_access_evidence(self, query_ids):
        """执行正式查询的 EXPLAIN ANALYZE 并读取累计 idx_scan。"""
        if len(set(query_ids)) != len(query_ids) or any(query_id not in self._queries for query_id in query_ids):
            raise ValueError("query_ids must be unique completed queries")
        connection = self.connect_worker()
        try:
            plans = {}
            for query_id in query_ids:
                statement, values = self._queries[query_id]
                rows = connection.execute(explain_sql(statement), values).fetchall()
                plans[query_id] = "EXPLAIN ANALYZE\n" + "\n".join(row[0] for row in rows)
            scans = {row[0]: int(row[1]) for row in connection.execute(
                "SELECT indexrelname,idx_scan FROM pg_stat_user_indexes WHERE schemaname=%s ORDER BY indexrelname",
                (self.schema,),
            ).fetchall()}
            return AccessEvidence(plans, scans)
        finally:
            connection.close()

    def collect_storage(self):
        """返回每个写目标的 relation、index 与 TOAST 分项空间。"""
        connection = self.connect_worker()
        try:
            tables = {}
            for table in build_layout_catalog(self.layout).write_tables:
                row = connection.execute(storage_sql(self.schema, table)).fetchone()
                tables[table] = {
                    "heap_bytes": int(row[0]), "index_bytes": int(row[1]),
                    "toast_bytes": int(row[2]), "total_bytes": int(row[3]),
                }
            return StorageEvidence(tables)
        finally:
            connection.close()

    def cleanup(self):
        """仅删除本实例成功创建的 schema，并确认没有残留。"""
        if not self._owned:
            return CleanupResult(self.schema, not self.namespace_exists())
        connection = self.connect_worker()
        try:
            with connection.transaction():
                connection.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        finally:
            connection.close()
        if self.namespace_exists():
            raise RuntimeError(f"schema cleanup failed: {self.schema}")
        self._owned = False
        return CleanupResult(self.schema, True)
