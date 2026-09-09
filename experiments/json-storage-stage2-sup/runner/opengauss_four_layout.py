"""openGauss JSON 与 openGauss JSONB 四结构基础比较适配器。"""

import hashlib
import json
import re
import subprocess
import time
from collections import Counter

import psycopg

from supplement_common import LAYOUTS, QUERY_IDS, canonical_bytes


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
OPENGAUSS_LAYOUTS = LAYOUTS[:2]


def validate_identifier(value, name):
    """验证用于 schema 或关系名的 SQL identifier。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match ^[a-z][a-z0-9_]{{0,62}}$")
    return value


def validate_layout(layout):
    """验证 openGauss JSON 基础比较的布局 ID。"""
    if layout not in OPENGAUSS_LAYOUTS:
        raise ValueError(f"unsupported openGauss layout: {layout}")
    return layout


def schema_name(namespace, layout):
    """返回指定布局独占且经过验证的 schema 名。"""
    validate_identifier(namespace, "namespace")
    validate_layout(layout)
    return validate_identifier(f"{namespace}_{layout}", "schema name")


def _attributes_type(layout):
    """返回布局唯一可变的属性列类型。"""
    return "JSON" if layout == "og_json" else "JSONB"


def create_layout_ddls(namespace, layout):
    """返回 schema、分析表和原文表的无索引 DDL。"""
    schema = schema_name(namespace, layout)
    attributes_type = _attributes_type(layout)
    statements = [
        f"CREATE SCHEMA {schema}",
        f"""CREATE TABLE {schema}.analytics (
    ingest_seq BIGINT NOT NULL,
    event_id TEXT NOT NULL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    project_id TEXT NOT NULL,
    start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    duration_ms BIGINT NOT NULL,
    span_type TEXT NOT NULL,
    framework TEXT NOT NULL,
    level TEXT NOT NULL,
    attributes {attributes_type} NOT NULL
)""",
        f"""CREATE TABLE {schema}."raw" (
    event_id TEXT NOT NULL PRIMARY KEY,
    raw_event TEXT NOT NULL
)""",
    ]
    return ";\n".join(statements) + ";"


def query_sql(layout, query_id):
    """返回使用 `{analytics}` 占位符的固定补充查询 SQL。"""
    validate_layout(layout)
    if query_id not in QUERY_IDS:
        raise ValueError(f"unsupported query ID: {query_id}")
    visibility = "project_id = %s AND start_time >= %s AND start_time < %s"
    statements = {
        "S01": (
            "SELECT span_type, count(*) FROM {analytics} WHERE "
            f"{visibility} GROUP BY span_type ORDER BY span_type"
        ),
        "S02": (
            "SELECT span_type, count(*) FROM {analytics} WHERE "
            f"{visibility} AND attributes #>> '{{gen_ai,operation,name}}' = %s "
            "GROUP BY span_type ORDER BY span_type"
        ),
        "S03": (
            "SELECT event_id FROM {analytics} WHERE "
            f"{visibility} AND attributes #>> '{{failure,mistake_mode}}' = %s"
        ),
        "S04": (
            "SELECT attributes #>> '{gen_ai,output,messages}' FROM {analytics} WHERE "
            f"{visibility}"
        ),
        "S05": (
            "SELECT start_time, event_id, attributes::text FROM {analytics} WHERE "
            f"{visibility} AND trace_id = %s ORDER BY start_time, event_id"
        ),
        "S06": (
            "WITH filtered AS ("
            "SELECT start_time, event_id, attributes::text FROM {analytics} WHERE "
            f"{visibility}"
            "), numbered AS ("
            "SELECT count(*) OVER () AS total_rows, start_time, event_id, attributes "
            "FROM filtered ORDER BY start_time, event_id"
            ") SELECT total_rows, start_time, event_id, attributes FROM numbered "
            "ORDER BY start_time, event_id OFFSET (SELECT GREATEST("
            "FLOOR((count(*) + 3) / 4.0) - 1, 0) FROM filtered) LIMIT %s"
        ),
    }
    return statements[query_id]


class OpenGaussFourLayoutAdapter:
    """执行 openGauss JSON 与 openGauss JSONB 基础布局操作。"""

    def __init__(self, host, port, container_name, namespace):
        """保存连接端点、容器名和当前实例独占的 schema 前缀。"""
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be positive")
        validate_identifier(namespace, "namespace")
        for layout in OPENGAUSS_LAYOUTS:
            schema_name(namespace, layout)
        self.host = host
        self.port = port
        self.container_name = container_name
        self.namespace = namespace
        self._password = None
        self._created_schemas = set()

    create_layout_ddls = staticmethod(create_layout_ddls)
    query_sql = staticmethod(query_sql)

    def _password_from_container(self):
        """从运行中的容器读取密码，仅缓存在当前实例内存中。"""
        if self._password is not None:
            return self._password
        completed = subprocess.run(
            [
                "docker", "inspect", self.container_name, "--format",
                "{{range .Config.Env}}{{println .}}{{end}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("unable to inspect openGauss container environment")
        for line in completed.stdout.splitlines():
            if line.startswith("GS_PASSWORD="):
                self._password = line.split("=", 1)[1]
                return self._password
        raise RuntimeError("GS_PASSWORD is not configured on openGauss container")

    def _schema(self, layout):
        """返回布局的独占 schema 名。"""
        return schema_name(self.namespace, layout)

    def _analytics(self, layout):
        """返回布局的分析表全限定名。"""
        return f"{self._schema(layout)}.analytics"

    def _raw(self, layout):
        """返回布局的原文表全限定名。"""
        return f'{self._schema(layout)}."raw"'

    def connect_worker(self):
        """建立由调用方负责关闭的 psycopg 连接。"""
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname="postgres",
            user="gaussdb",
            password=self._password_from_container(),
            autocommit=False,
        )

    def database_version(self):
        """返回当前 openGauss 服务端版本。"""
        connection = self.connect_worker()
        try:
            return connection.execute("SELECT version()").fetchone()[0]
        finally:
            connection.close()

    def schema_exists(self, layout):
        """返回布局 schema 是否存在。"""
        connection = self.connect_worker()
        try:
            return connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                (self._schema(layout),),
            ).fetchone()[0]
        finally:
            connection.close()

    def create_layout(self, layout):
        """创建空布局，DDL 计时由调用方排除在载入阶段外。"""
        validate_layout(layout)
        schema = self._schema(layout)
        connection = self.connect_worker()
        try:
            with connection.transaction():
                exists = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                    (schema,),
                ).fetchone()[0]
                if exists:
                    raise ValueError(f"schema already exists: {schema}")
                for statement in create_layout_ddls(self.namespace, layout).split(";\n"):
                    connection.execute(statement.rstrip(";"))
            self._created_schemas.add(schema)
            return {"ddl": create_layout_ddls(self.namespace, layout), "schema": schema}
        finally:
            connection.close()

    @staticmethod
    def _copy_analytics(cursor, relation, rows):
        """把一个预生成 block 写入分析表。"""
        with cursor.copy(
            "COPY " + relation + "(ingest_seq,event_id,trace_id,span_id,parent_span_id,"
            "project_id,start_time,end_time,duration_ms,span_type,framework,level,attributes) "
            "FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row((
                    row["ingest_seq"], row["event_id"], row["trace_id"], row["span_id"],
                    row["parent_span_id"], row["project_id"], row["start_time"], row["end_time"],
                    row["duration_ms"], row["span_type"], row["framework"], row["level"],
                    canonical_bytes(row["attributes_analysis"]).decode("utf-8"),
                ))

    @staticmethod
    def _copy_raw(cursor, relation, rows):
        """把同一 block 的摄入原文写入 raw 表。"""
        with cursor.copy("COPY " + relation + "(event_id,raw_event) FROM STDIN") as copy:
            for row in rows:
                copy.write_row((row["event_id"], row["raw_event"]))

    def insert_block(self, layout, rows):
        """在同一事务写入分析与原文 block，并返回分项耗时。"""
        validate_layout(layout)
        if not isinstance(rows, list):
            raise ValueError("rows must be a list")
        if not rows:
            return {
                "analysis_copy_ms": 0.0, "block_wall_ms": 0.0, "commit_ms": 0.0,
                "raw_copy_ms": 0.0, "rows": 0,
            }
        connection = self.connect_worker()
        wall_started = time.perf_counter()
        try:
            try:
                with connection.cursor() as cursor:
                    analysis_started = time.perf_counter()
                    self._copy_analytics(cursor, self._analytics(layout), rows)
                    analysis_copy_ms = (time.perf_counter() - analysis_started) * 1000
                    raw_started = time.perf_counter()
                    self._copy_raw(cursor, self._raw(layout), rows)
                    raw_copy_ms = (time.perf_counter() - raw_started) * 1000
                commit_started = time.perf_counter()
                connection.commit()
                commit_ms = (time.perf_counter() - commit_started) * 1000
            except Exception:
                connection.rollback()
                raise
            return {
                "analysis_copy_ms": analysis_copy_ms,
                "block_wall_ms": (time.perf_counter() - wall_started) * 1000,
                "commit_ms": commit_ms,
                "raw_copy_ms": raw_copy_ms,
                "rows": len(rows),
            }
        finally:
            connection.close()

    def _query_parameters(self, query_id, params):
        """把 query catalog 参数转换为 SQL 位置参数。"""
        if query_id not in QUERY_IDS:
            raise ValueError(f"unsupported query ID: {query_id}")
        if not isinstance(params, dict):
            raise ValueError("query params must be an object")
        try:
            values = [params["project_id"], params["start_time"], params["end_time"]]
            if query_id == "S02":
                values.append(params["operation_name"])
            elif query_id == "S03":
                values.append(params["failure_mistake_mode"])
            elif query_id == "S05":
                values.append(params["trace_id"])
            elif query_id == "S06":
                page_size = params["page_size"]
                if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0:
                    raise ValueError("page_size must be positive")
                values.append(page_size)
            return tuple(values)
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None

    def _statement(self, layout, query_id):
        """返回替换表名后的完整固定查询 SQL。"""
        return query_sql(layout, query_id).replace("{analytics}", self._analytics(layout))

    @staticmethod
    def _format_timestamp(value):
        """把数据库 UTC 时间戳规范化为 truth 使用的 ISO 文本。"""
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _load_attributes(value):
        """把 JSON 文本或驱动返回对象恢复为属性对象。"""
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError("attributes must be an object")
        return value

    @staticmethod
    def _path_value(text):
        """把 `#>>` 返回的文本恢复为其原始 JSON 值。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _normalize_result(self, query_id, rows):
        """在完整响应读取后恢复 S01 至 S06 的 truth 结果。"""
        if query_id in {"S01", "S02"}:
            return [[row[0], int(row[1])] for row in sorted(rows)]
        if query_id == "S03":
            event_ids = sorted(row[0] for row in rows)
            return {
                "identity_sha256": hashlib.sha256(canonical_bytes(event_ids)).hexdigest(),
                "row_count": len(event_ids),
            }
        if query_id == "S04":
            values = [self._path_value(row[0]) for row in rows if row[0] is not None]
            return {
                "non_null_count": len(values),
                "utf8_bytes": sum(len(canonical_bytes(value)) for value in values),
            }
        if query_id == "S05":
            return [
                [self._format_timestamp(row[0]), row[1], self._load_attributes(row[2])]
                for row in sorted(rows, key=lambda row: (row[0], row[1]))
            ]
        documents = [
            [self._format_timestamp(row[1]), row[2], self._load_attributes(row[3])]
            for row in rows
        ]
        event_ids = [row[1] for row in documents]
        return {
            "identity_sha256": hashlib.sha256(canonical_bytes(event_ids)).hexdigest(),
            "page_row_count": len(documents),
            "row_count": int(rows[0][0]) if rows else 0,
            "rows": documents,
        }

    def execute_query(self, connection, layout, query_id, params):
        """执行查询并在响应读取后单独记录客户端恢复耗时。"""
        statement = self._statement(layout, query_id)
        query_parameters = self._query_parameters(query_id, params)
        started = time.perf_counter()
        rows = connection.execute(statement, query_parameters).fetchall()
        latency_ms = (time.perf_counter() - started) * 1000
        recovery_started = time.perf_counter()
        result = self._normalize_result(query_id, rows)
        recovery_ms = (time.perf_counter() - recovery_started) * 1000
        if query_id == "S03":
            row_count = result["row_count"]
        elif query_id == "S04":
            row_count = result["non_null_count"]
        elif query_id == "S06":
            row_count = result["page_row_count"]
        else:
            row_count = len(result)
        return {
            "latency_ms": latency_ms,
            "recovery_ms": recovery_ms,
            "result": result,
            "result_sha256": hashlib.sha256(canonical_bytes(result)).hexdigest(),
            "row_count": row_count,
        }

    def collect_plan(self, layout, query_id, params):
        """返回无索引基础布局的自然执行计划。"""
        connection = self.connect_worker()
        try:
            rows = connection.execute(
                "EXPLAIN " + self._statement(layout, query_id),
                self._query_parameters(query_id, params),
            ).fetchall()
            return {"natural": "\n".join(row[0] for row in rows)}
        finally:
            connection.close()

    def finish_maintenance(self, layout):
        """单独执行并计时分析表的 ANALYZE。"""
        validate_layout(layout)
        connection = self.connect_worker()
        try:
            connection.autocommit = True
            started = time.perf_counter()
            connection.execute("ANALYZE " + self._analytics(layout))
            return {"analyze_ms": (time.perf_counter() - started) * 1000, "analyzed": True}
        finally:
            connection.close()

    def collect_storage(self, layout):
        """分别采集分析表和原文表的 heap、TOAST、索引和总空间。"""
        validate_layout(layout)
        connection = self.connect_worker()
        try:
            storage = {}
            for name, relation in (("analytics", self._analytics(layout)), ("raw", self._raw(layout))):
                row = connection.execute(
                    "SELECT pg_relation_size(%s::regclass), GREATEST("
                    "pg_total_relation_size(%s::regclass) - pg_relation_size(%s::regclass) "
                    "- pg_indexes_size(%s::regclass), 0), pg_total_relation_size(%s::regclass)",
                    (relation, relation, relation, relation, relation),
                ).fetchall()[0]
                heap_bytes, toast_bytes, total_bytes = (int(value) for value in row)
                storage[name] = {
                    "heap_bytes": heap_bytes,
                    "toast_bytes": toast_bytes,
                    "index_bytes": total_bytes - heap_bytes - toast_bytes,
                    "total_bytes": total_bytes,
                }
            return storage
        finally:
            connection.close()

    def _verify_records(self, layout, truth, field, relation, column, digest):
        """验证一个表的 identity、重复、缺失、额外和逐行摘要。"""
        validate_layout(layout)
        try:
            expected_hashes = {record["event_id"]: record[field] for record in truth["records"]}
        except (KeyError, TypeError) as error:
            raise ValueError("invalid truth records") from error
        connection = self.connect_worker()
        try:
            rows = connection.execute(
                f"SELECT event_id, {column} FROM {relation} ORDER BY event_id"
            ).fetchall()
        finally:
            connection.close()
        actual_ids = [row[0] for row in rows]
        actual_set = set(actual_ids)
        expected_set = set(expected_hashes)
        mismatches = sorted({
            event_id for event_id, value in rows
            if event_id in expected_hashes and digest(value) != expected_hashes[event_id]
        })
        diagnostics = {
            "actual_count": len(actual_ids),
            "duplicate_count": sum(count > 1 for count in Counter(actual_ids).values()),
            "duplicates": sorted(event_id for event_id, count in Counter(actual_ids).items() if count > 1),
            "expected_count": len(expected_set),
            "extra": sorted(actual_set - expected_set),
            "missing": sorted(expected_set - actual_set),
        }
        diagnostics[f"{field}_mismatches"] = mismatches
        diagnostics["ok"] = not any((
            diagnostics["duplicates"], diagnostics["extra"], diagnostics["missing"], mismatches,
        )) and diagnostics["actual_count"] == diagnostics["expected_count"]
        return diagnostics

    def verify_analysis(self, layout, truth):
        """验证分析属性的 canonical SHA-256 和 identity。"""
        return self._verify_records(
            layout, truth, "analysis_sha256", self._analytics(layout), "attributes",
            lambda value: hashlib.sha256(canonical_bytes(self._load_attributes(value))).hexdigest(),
        )

    def verify_raw(self, layout, truth):
        """验证 raw 表中摄入原文的 UTF-8 SHA-256 和 identity。"""
        return self._verify_records(
            layout, truth, "raw_sha256", self._raw(layout), "raw_event",
            lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest(),
        )

    def cleanup(self, layout):
        """仅删除当前实例成功创建的 schema，并确认删除结果。"""
        schema = self._schema(layout)
        if schema not in self._created_schemas:
            return {"schema": schema, "removed": False}
        connection = self.connect_worker()
        try:
            with connection.transaction():
                connection.execute("DROP SCHEMA IF EXISTS " + schema + " CASCADE")
                exists = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)", (schema,)
                ).fetchone()[0]
                if exists:
                    raise RuntimeError(f"schema cleanup failed: {schema}")
            self._created_schemas.remove(schema)
            return {"schema": schema, "removed": True}
        finally:
            connection.close()

    def cleanup_all(self):
        """删除当前实例仍持有的全部 schema。"""
        return [self.cleanup(layout) for layout in OPENGAUSS_LAYOUTS]
