"""阶段二 openGauss JSONB residual 布局 adapter。"""

import hashlib
import json
import re
import subprocess
import time
from collections import Counter

import psycopg


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
LAYOUTS = ("og_jsonb", "og_jsonb_hot", "og_jsonb_gin")
QUERY_IDS = ("Q01", "Q02", "Q03", "Q04", "Q05")


def canonical_bytes(value):
    """返回对象键排序且保留数组顺序的 JSON UTF-8 bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_identifier(value, name):
    """验证 schema 和对象名称使用的 SQL identifier。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match ^[a-z][a-z0-9_]{{0,62}}$")
    return value


def validate_layout(layout):
    """验证阶段二固定的 openGauss 布局 ID。"""
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported openGauss layout: {layout}")
    return layout


def schema_name(namespace, layout):
    """返回一个 layout 独占的已验证 schema 名。"""
    validate_identifier(namespace, "namespace")
    validate_layout(layout)
    return validate_identifier(f"{namespace}_{layout}", "schema name")


def _analytics_expression(path):
    """返回嵌套 projected JSONB 路径的文本提取表达式。"""
    expression = "attributes"
    for part in path[:-1]:
        expression = f"jsonb_object_field({expression}, '{part}')"
    return f"jsonb_object_field_text({expression}, '{path[-1]}')"


HOT_OPERATION_EXPRESSION = _analytics_expression(("gen_ai", "operation", "name"))


def create_layout_ddls(namespace, layout):
    """返回指定 layout 的 schema、分析表、原文表和索引 DDL。"""
    schema = schema_name(namespace, layout)
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
    attributes JSONB NOT NULL
)""",
        f"""CREATE TABLE {schema}."raw" (
    event_id TEXT NOT NULL PRIMARY KEY,
    raw_event TEXT NOT NULL
)""",
    ]
    if layout == "og_jsonb_hot":
        statements.append(
            f"CREATE INDEX analytics_hot_operation_idx ON {schema}.analytics "
            f"({HOT_OPERATION_EXPRESSION})"
        )
    if layout == "og_jsonb_gin":
        statements.append(
            f"CREATE INDEX analytics_gin_attributes_idx ON {schema}.analytics "
            "USING gin(attributes jsonb_ops)"
        )
    return ";\n".join(statements) + ";"


def query_sql(layout, query_id):
    """返回使用 `{analytics}` 占位符的固定查询 SQL。"""
    validate_layout(layout)
    if query_id not in QUERY_IDS:
        raise ValueError(f"unsupported query ID: {query_id}")
    visibility = (
        "project_id = %s AND start_time >= %s AND start_time < %s "
        "AND ingest_seq < %s"
    )
    statements = {
        "Q01": (
            "SELECT span_type, count(*) FROM {analytics} WHERE "
            f"{visibility} GROUP BY span_type ORDER BY span_type"
        ),
        "Q02": (
            "SELECT span_type, count(*) FROM {analytics} WHERE "
            f"{visibility} AND {HOT_OPERATION_EXPRESSION} = %s "
            "GROUP BY span_type ORDER BY span_type"
        ),
        "Q03": (
            "SELECT span_type, count(*), sum(duration_ms) FROM {analytics} WHERE "
            f"{visibility} GROUP BY span_type ORDER BY span_type"
        ),
        "Q04": (
            "SELECT start_time, event_id, attributes FROM {analytics} WHERE "
            f"{visibility} AND trace_id = %s ORDER BY start_time, event_id"
        ),
        "Q05": (
            "SELECT event_id FROM {analytics} WHERE "
            f"{visibility} AND attributes @> %s::jsonb"
        ),
    }
    return statements[query_id]


class OpenGaussAdapter:
    """提供阶段二 openGauss layout 的连接、查询和完整性操作。"""

    def __init__(self, host, port, container_name, namespace):
        """保存连接端点和临时 schema 前缀，密码按需读取。"""
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be positive")
        validate_identifier(namespace, "namespace")
        for layout in LAYOUTS:
            schema_name(namespace, layout)
        self.host = host
        self.port = port
        self.container_name = container_name
        self.namespace = namespace
        self._password = None

    create_layout_ddls = staticmethod(create_layout_ddls)
    query_sql = staticmethod(query_sql)

    def _password_from_container(self):
        """从已运行容器环境读取密码，仅保留在 adapter 内存中。"""
        if self._password is not None:
            return self._password
        completed = subprocess.run(
            [
                "docker",
                "inspect",
                self.container_name,
                "--format",
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
        """返回 layout 的独立 schema 名。"""
        return schema_name(self.namespace, layout)

    def _analytics(self, layout):
        """返回 layout 的分析表关系名。"""
        return f"{self._schema(layout)}.analytics"

    def _raw(self, layout):
        """返回 layout 的原文表关系名。"""
        return f'{self._schema(layout)}."raw"'

    def connect_worker(self):
        """建立一个由调用方负责关闭的独立 psycopg 连接。"""
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname="postgres",
            user="gaussdb",
            password=self._password_from_container(),
            autocommit=False,
        )

    def schema_exists(self, layout):
        """返回 layout schema 是否仍存在。"""
        schema = self._schema(layout)
        connection = self.connect_worker()
        try:
            return connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                (schema,),
            ).fetchone()[0]
        finally:
            connection.close()

    def create_layout(self, layout, budget):
        """创建一个空的 layout schema；budget 保持 adapter 公共签名一致。"""
        validate_layout(layout)
        if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
            raise ValueError("budget must be positive")
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
            return {"ddl": create_layout_ddls(self.namespace, layout), "schema": schema}
        finally:
            connection.close()

    def insert_block(self, layout, rows):
        """在一个事务中 COPY 写入分析表与原文表，并返回行数。"""
        validate_layout(layout)
        if not rows:
            return {"rows": 0}
        analytics = self._analytics(layout)
        raw = self._raw(layout)
        connection = self.connect_worker()
        try:
            with connection.transaction():
                with connection.cursor().copy(
                    "COPY " + analytics + "(ingest_seq,event_id,trace_id,span_id,"
                    "parent_span_id,project_id,start_time,end_time,duration_ms,span_type,"
                    "framework,level,attributes) FROM STDIN"
                ) as copy:
                    for row in rows:
                        copy.write_row(
                            (
                                row["ingest_seq"], row["event_id"], row["trace_id"],
                                row["span_id"], row["parent_span_id"], row["project_id"],
                                row["start_time"], row["end_time"], row["duration_ms"],
                                row["span_type"], row["framework"], row["level"],
                                canonical_bytes(row["attributes_analysis"]).decode("utf-8"),
                            )
                        )
                with connection.cursor().copy(
                    "COPY " + raw + "(event_id,raw_event) FROM STDIN"
                ) as copy:
                    for row in rows:
                        copy.write_row((row["event_id"], row["raw_event"]))
            return {"rows": len(rows)}
        finally:
            connection.close()

    def _query_parameters(self, query_id, params, watermark):
        """把 truth 查询参数与水位映射为 SQL 位置参数。"""
        if not isinstance(params, dict):
            raise ValueError("query params must be an object")
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 0:
            raise ValueError("watermark must be a non-negative integer")
        try:
            values = [params["project_id"], params["start_time"], params["end_time"], watermark]
            if query_id == "Q02":
                values.append(params["operation_name"])
            elif query_id == "Q04":
                values.append(params["trace_id"])
            elif query_id == "Q05":
                values.append(canonical_bytes({"failure": {"mistake_mode": params["failure_mistake_mode"]}}).decode("utf-8"))
            return tuple(values)
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None

    def _statement(self, layout, query_id):
        """返回某 layout 的完整限定查询 SQL。"""
        return query_sql(layout, query_id).format(analytics=self._analytics(layout))

    @staticmethod
    def _format_timestamp(value):
        """把数据库 UTC 时间戳规范化为生成器使用的毫秒 ISO 文本。"""
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _restore_attributes(attributes, key_map):
        """按 truth key_map 从 nested JSONB 恢复原始键与 canonical JSON 值。"""
        if not isinstance(key_map, dict):
            raise ValueError("Q04 requires truth key_map")
        if isinstance(attributes, str):
            attributes = json.loads(attributes)
        if not isinstance(attributes, dict):
            raise ValueError("Q04 attributes must be an object")
        restored = {}

        def visit(value, path):
            joined = ".".join(path)
            if joined in key_map:
                restored[key_map[joined]] = canonical_bytes(value).decode("utf-8")
                return
            if not isinstance(value, dict):
                raise ValueError(f"missing key map entry: {joined}")
            for key, nested_value in value.items():
                visit(nested_value, path + [key])

        visit(attributes, [])
        return {key: restored[key] for key in sorted(restored)}

    def _normalize_result(self, query_id, rows, params):
        """在计时区间外把数据库行转换为公共 result。"""
        if query_id in {"Q01", "Q02"}:
            return [[row[0], int(row[1])] for row in sorted(rows)]
        if query_id == "Q03":
            return [[row[0], int(row[1]), int(row[2])] for row in sorted(rows)]
        if query_id == "Q04":
            key_map = params.get("key_map")
            return [
                [self._format_timestamp(row[0]), row[1], self._restore_attributes(row[2], key_map)]
                for row in sorted(rows, key=lambda row: (row[0], row[1]))
            ]
        event_ids = sorted(row[0] for row in rows)
        return {
            "row_count": len(event_ids),
            "identity_sha256": hashlib.sha256(canonical_bytes(event_ids)).hexdigest(),
        }

    def execute_query(self, connection, layout, query_id, params, watermark):
        """执行并完整读取一条查询，返回带计时与 digest 的公共 envelope。"""
        statement = self._statement(layout, query_id)
        query_parameters = self._query_parameters(query_id, params, watermark)
        started = time.perf_counter()
        rows = connection.execute(statement, query_parameters).fetchall()
        latency_ms = (time.perf_counter() - started) * 1000
        result = self._normalize_result(query_id, rows, params)
        row_count = result["row_count"] if query_id == "Q05" else len(result)
        return {
            "latency_ms": latency_ms,
            "result": result,
            "result_sha256": hashlib.sha256(canonical_bytes(result)).hexdigest(),
            "row_count": row_count,
        }

    def _explain(self, connection, statement, parameters, force_index):
        """读取自然或禁用顺扫后的可复现 EXPLAIN 文本。"""
        if force_index:
            connection.execute("SET enable_seqscan = off")
        try:
            return "\n".join(row[0] for row in connection.execute(
                "EXPLAIN " + statement, parameters
            ).fetchall())
        finally:
            if force_index:
                connection.execute("RESET enable_seqscan")

    def collect_plan(self, layout, query_id, params, watermark):
        """返回同一查询的自然计划和强制索引可见性计划。"""
        statement = self._statement(layout, query_id)
        query_parameters = self._query_parameters(query_id, params, watermark)
        connection = self.connect_worker()
        try:
            return {
                "natural": self._explain(connection, statement, query_parameters, False),
                "forced": self._explain(connection, statement, query_parameters, True),
            }
        finally:
            connection.close()

    def collect_storage(self, layout):
        """分别采集 analytics 与 raw 的 heap、index 和 total 字节数。"""
        validate_layout(layout)
        connection = self.connect_worker()
        try:
            result = {}
            for name, relation in (("analytics", self._analytics(layout)), ("raw", self._raw(layout))):
                row = connection.execute(
                    "SELECT pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass), "
                    "pg_total_relation_size(%s::regclass)",
                    (relation, relation, relation),
                ).fetchone()
                result[name] = {
                    "heap_bytes": int(row[0]),
                    "index_bytes": int(row[1]),
                    "total_bytes": int(row[2]),
                }
            return result
        finally:
            connection.close()

    def finish_maintenance(self, layout, timeout_seconds):
        """在给定语句超时内执行分析表 ANALYZE。"""
        validate_layout(layout)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        connection = self.connect_worker()
        try:
            connection.autocommit = True
            connection.execute(f"SET statement_timeout = '{timeout_seconds * 1000}ms'")
            connection.execute("ANALYZE " + self._analytics(layout))
            return {"analyzed": True, "timeout_seconds": timeout_seconds}
        finally:
            connection.close()

    def verify_raw(self, layout, truth):
        """按 event_id 集合和 raw UTF-8 SHA-256 验证原文恢复。"""
        validate_layout(layout)
        try:
            expected_hashes = {
                record["event_id"]: record["raw_sha256"] for record in truth["records"]
            }
        except (KeyError, TypeError) as error:
            raise ValueError("invalid raw truth records") from error
        connection = self.connect_worker()
        try:
            rows = connection.execute(
                "SELECT event_id, raw_event FROM " + self._raw(layout) + " ORDER BY event_id"
            ).fetchall()
        finally:
            connection.close()
        actual_ids = [row[0] for row in rows]
        actual_set = set(actual_ids)
        expected_set = set(expected_hashes)
        duplicates = sorted(
            event_id for event_id, count in Counter(actual_ids).items() if count > 1
        )
        mismatches = sorted(
            event_id for event_id, raw_event in rows
            if event_id in expected_hashes
            and hashlib.sha256(raw_event.encode("utf-8")).hexdigest() != expected_hashes[event_id]
        )
        diagnostics = {
            "actual_count": len(actual_ids),
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
            "expected_count": len(expected_set),
            "extra": sorted(actual_set - expected_set),
            "missing": sorted(expected_set - actual_set),
            "raw_sha256_mismatches": mismatches,
        }
        diagnostics["ok"] = not any(
            diagnostics[name]
            for name in ("duplicates", "extra", "missing", "raw_sha256_mismatches")
        ) and diagnostics["actual_count"] == diagnostics["expected_count"]
        return diagnostics

    def verify_analysis(self, layout, truth):
        """验证 analytics identity 与 canonical analysis SHA-256，不读取 raw 表。"""
        validate_layout(layout)
        try:
            expected_hashes = {
                record["event_id"]: record["analysis_sha256"] for record in truth["records"]
            }
        except (KeyError, TypeError) as error:
            raise ValueError("invalid analysis truth records") from error
        connection = self.connect_worker()
        try:
            rows = connection.execute(
                "SELECT event_id, attributes FROM " + self._analytics(layout) + " ORDER BY event_id"
            ).fetchall()
        finally:
            connection.close()
        actual_ids = [row[0] for row in rows]
        actual_set = set(actual_ids)
        expected_set = set(expected_hashes)
        duplicates = sorted(event_id for event_id, count in Counter(actual_ids).items() if count > 1)
        mismatches = sorted({
            event_id for event_id, attributes in rows
            if event_id in expected_hashes
            and hashlib.sha256(canonical_bytes(
                json.loads(attributes) if isinstance(attributes, str) else attributes
            )).hexdigest() != expected_hashes[event_id]
        })
        diagnostics = {
            "actual_count": len(actual_ids),
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
            "expected_count": len(expected_set),
            "extra": sorted(actual_set - expected_set),
            "missing": sorted(expected_set - actual_set),
            "analysis_sha256_mismatches": mismatches,
        }
        diagnostics["ok"] = not any(
            diagnostics[name]
            for name in ("duplicates", "extra", "missing", "analysis_sha256_mismatches")
        ) and diagnostics["actual_count"] == diagnostics["expected_count"]
        return diagnostics

    def cleanup(self, layout):
        """删除一个 layout schema，并确认没有同名 schema 残留。"""
        schema = self._schema(layout)
        connection = self.connect_worker()
        try:
            with connection.transaction():
                connection.execute("DROP SCHEMA IF EXISTS " + schema + " CASCADE")
                exists = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = %s)",
                    (schema,),
                ).fetchone()[0]
                if exists:
                    raise RuntimeError(f"schema cleanup failed: {schema}")
            return {"schema": schema, "removed": True}
        finally:
            connection.close()
