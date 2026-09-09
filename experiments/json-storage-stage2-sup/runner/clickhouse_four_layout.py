"""ClickHouse String JSON 与 Native JSON 四结构基础比较适配器。"""

import hashlib
import http.client
import json
import re
import time
import urllib.parse
import uuid
from collections import Counter

from supplement_common import LAYOUTS, QUERY_IDS, canonical_bytes


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
QUERY_LOG_ID = re.compile(r"^[A-Za-z0-9_-]+$")
CLICKHOUSE_LAYOUTS = LAYOUTS[2:]
QUERY_LOG_METRICS = (
    "query_duration_ms", "read_rows", "read_bytes", "memory_usage",
    "result_rows", "result_bytes", "selected_rows", "selected_bytes",
)


def validate_identifier(value, name):
    """验证临时 database 与对象名称的 SQL identifier。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match ^[a-z][a-z0-9_]{{0,62}}$")
    return value


def validate_layout(layout):
    """验证 ClickHouse 基础比较的布局 ID。"""
    if layout not in CLICKHOUSE_LAYOUTS:
        raise ValueError(f"unsupported ClickHouse layout: {layout}")
    return layout


def validate_budget(budget):
    """验证 Native JSON 动态路径预算。"""
    if type(budget) is not int or budget != 32:
        raise ValueError("budget must be 32 for the fixed comparison contract")
    return budget


def database_name(namespace, layout):
    """返回布局独占且通过验证的 database 名。"""
    validate_identifier(namespace, "namespace")
    validate_layout(layout)
    return validate_identifier(f"{namespace}_{layout}", "database name")


def create_layout_ddl(namespace, layout, budget):
    """返回一个布局的 database、分析表和原文表 DDL。"""
    database = database_name(namespace, layout)
    validate_budget(budget)
    attributes = {
        "ch_string": "attributes String CODEC(ZSTD(3))",
        "ch_native": f"attributes JSON(max_dynamic_paths={budget})",
    }[layout]
    sidecar = ",\n    fidelity_values Map(String,String)" if layout == "ch_native" else ""
    return (
        f"CREATE DATABASE {database};\n"
        f"CREATE TABLE {database}.analytics (\n"
        "    ingest_seq UInt64,\n"
        "    event_id String,\n"
        "    trace_id String,\n"
        "    span_id String,\n"
        "    parent_span_id String,\n"
        "    project_id String,\n"
        "    start_time DateTime64(3, 'UTC'),\n"
        "    end_time DateTime64(3, 'UTC'),\n"
        "    duration_ms Int64,\n"
        "    span_type String,\n"
        "    framework String,\n"
        "    level String,\n"
        f"    {attributes}{sidecar}\n"
        ") ENGINE=MergeTree\n"
        "ORDER BY (project_id,start_time,event_id);\n"
        f"CREATE TABLE {database}.raw (\n"
        "    event_id String,\n"
        "    ingest_seq UInt64,\n"
        "    raw_event String CODEC(ZSTD(3))\n"
        ") ENGINE=MergeTree\n"
        "ORDER BY (event_id,ingest_seq);"
    )


def query_sql(layout, query_id):
    """返回使用 `{analytics}` 占位符的固定补充查询 SQL。"""
    validate_layout(layout)
    if query_id not in QUERY_IDS:
        raise ValueError(f"unsupported query ID: {query_id}")
    visibility = (
        "project_id = {project_id:String} "
        "AND start_time >= {start_time:DateTime64(3, 'UTC')} "
        "AND start_time < {end_time:DateTime64(3, 'UTC')}"
    )
    operation = {
        "ch_string": "JSONExtractString(attributes, 'gen_ai', 'operation', 'name') = {operation_name:String}",
        "ch_native": "attributes.gen_ai.operation.name.:String = {operation_name:String}",
    }[layout]
    failure = {
        "ch_string": "JSONExtractString(attributes, 'failure', 'mistake_mode') = {failure_mistake_mode:String}",
        "ch_native": "attributes.failure.mistake_mode.:String = {failure_mistake_mode:String}",
    }[layout]
    projection = {
        "ch_string": "JSONExtractRaw(attributes, 'gen_ai', 'output', 'messages')",
        "ch_native": "attributes.gen_ai.output.messages",
    }[layout]
    document_fields = "start_time, event_id, attributes" + (", fidelity_values" if layout == "ch_native" else "")
    return {
        "S01": (
            "SELECT span_type, count() AS count FROM {analytics} WHERE "
            f"{visibility} GROUP BY span_type ORDER BY span_type FORMAT JSONEachRow"
        ),
        "S02": (
            "SELECT span_type, count() AS count FROM {analytics} WHERE "
            f"{visibility} AND {operation} GROUP BY span_type ORDER BY span_type FORMAT JSONEachRow"
        ),
        "S03": (
            "SELECT event_id FROM {analytics} WHERE "
            f"{visibility} AND {failure} ORDER BY event_id FORMAT JSONEachRow"
        ),
        "S04": (
            f"SELECT {projection} AS value FROM {{analytics}} WHERE {visibility} FORMAT JSONEachRow"
        ),
        "S05": (
            f"SELECT {document_fields} FROM {{analytics}} WHERE {visibility} "
            "AND trace_id = {trace_id:String} ORDER BY start_time, event_id FORMAT JSONEachRow"
        ),
        "S06": (
            "WITH filtered AS (SELECT " + document_fields + " FROM {analytics} WHERE " + visibility + ") "
            "SELECT count() OVER () AS total_rows, " + document_fields + " FROM filtered "
            "ORDER BY start_time, event_id LIMIT {page_size:UInt64} OFFSET "
            "(SELECT greatest(intDiv(count() + 3, 4) - 1, 0) FROM filtered) FORMAT JSONEachRow"
        ),
    }[query_id]


def clickhouse_timestamp(value):
    """把毫秒 UTC ISO 文本转换为 ClickHouse DateTime64 输入文本。"""
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value):
        raise ValueError("timestamp must use millisecond UTC ISO format")
    return value.replace("T", " ").removesuffix("Z")


def _pointer_escape(part):
    """编码 JSON Pointer 的一个路径片段。"""
    return str(part).replace("~", "~0").replace("/", "~1")


def _pointer_unescape(part):
    """严格解码一个 JSON Pointer 路径片段。"""
    result = []
    index = 0
    while index < len(part):
        character = part[index]
        if character != "~":
            result.append(character)
            index += 1
            continue
        if index + 1 >= len(part) or part[index + 1] not in {"0", "1"}:
            raise ValueError("fidelity path contains an invalid JSON Pointer escape")
        result.append("~" if part[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _contains_lossy_value(value):
    """返回值是否递归包含 Native JSON 无法保留的 JSON 状态。"""
    if value is None or value == {} or value == []:
        return True
    if isinstance(value, dict):
        return any(_contains_lossy_value(nested) for nested in value.values())
    if isinstance(value, list):
        return any(_contains_lossy_value(nested) for nested in value)
    return False


def _contains_dotted_descendant(value):
    """返回顶层分支是否包含会被 Native JSON 改写的点号键。"""
    if isinstance(value, dict):
        return any(
            "." in key or _contains_dotted_descendant(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_dotted_descendant(nested) for nested in value)
    return False


def fidelity_values(attributes):
    """保存受 Native JSON 结构改写影响的完整根或顶层分支。"""
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be an object")
    if any("." in key for key in attributes):
        return {"": canonical_bytes(attributes).decode("utf-8")}
    return {
        "/" + _pointer_escape(key): canonical_bytes(value).decode("utf-8")
        for key, value in sorted(attributes.items())
        if _contains_lossy_value(value) or _contains_dotted_descendant(value)
    }


def merge_fidelity(attributes, special):
    """按根或顶层 JSON Pointer 覆盖 Sidecar 值并恢复分析属性。"""
    if not isinstance(attributes, dict) or not isinstance(special, dict):
        raise ValueError("attributes and fidelity_values must be objects")
    values = {}
    for pointer, canonical_value in special.items():
        if not isinstance(pointer, str):
            raise ValueError("fidelity path must be a JSON Pointer")
        if not isinstance(canonical_value, str):
            raise ValueError("fidelity value must be canonical JSON")
        try:
            value = json.loads(canonical_value)
        except json.JSONDecodeError as error:
            raise ValueError("fidelity value must be canonical JSON") from error
        if canonical_bytes(value).decode("utf-8") != canonical_value:
            raise ValueError("fidelity value must be canonical JSON")
        values[pointer] = value
    if "" in values:
        if len(values) != 1 or not isinstance(values[""], dict):
            raise ValueError("root fidelity value must be the only object value")
        return values[""]
    merged = json.loads(canonical_bytes(attributes))
    for pointer, value in values.items():
        if not pointer.startswith("/") or "/" in pointer[1:]:
            raise ValueError("fidelity path must name one top-level key")
        key = _pointer_unescape(pointer[1:])
        merged[key] = value
    return merged


class ClickHouseFourLayoutAdapter:
    """执行 ClickHouse String JSON 与 Native JSON 基础布局操作。"""

    def __init__(self, host, port, container_name, namespace):
        """保存 HTTP 端点、容器身份与本实例 database 前缀。"""
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be positive")
        validate_identifier(namespace, "namespace")
        for layout in CLICKHOUSE_LAYOUTS:
            database_name(namespace, layout)
        self.host = host
        self.port = port
        self.container_name = container_name
        self.namespace = namespace
        self._created_databases = set()

    create_layout_ddl = staticmethod(create_layout_ddl)
    query_sql = staticmethod(query_sql)

    def _database(self, layout):
        """返回布局的独占 database 名。"""
        return database_name(self.namespace, layout)

    def _analytics(self, layout):
        """返回布局的分析表全限定名。"""
        return f"{self._database(layout)}.analytics"

    def _raw(self, layout):
        """返回布局的原文表全限定名。"""
        return f"{self._database(layout)}.raw"

    def connect_worker(self):
        """建立一条由调用方关闭的独立 HTTP 连接。"""
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.connect()
        except Exception:
            connection.close()
            raise
        return connection

    def database_version(self):
        """返回当前 ClickHouse 服务端版本。"""
        connection = self.connect_worker()
        try:
            return self._request(connection, "SELECT version()").strip()
        finally:
            connection.close()

    @staticmethod
    def _json_rows(body):
        """解析 ClickHouse JSONEachRow 响应。"""
        try:
            return [json.loads(line) for line in body.splitlines() if line]
        except json.JSONDecodeError as error:
            raise RuntimeError("invalid ClickHouse JSONEachRow response") from error

    def _request(self, connection, statement, body=None, parameters=None, query_id=None, timing=None):
        """提交 SQL 并完整读取响应，调用方控制查询延迟计时区间。"""
        if query_id is not None and not QUERY_LOG_ID.fullmatch(query_id):
            raise ValueError("query_id contains unsupported characters")
        query = {"log_queries": 1 if query_id is not None else 0, "log_processors_profiles": 0,
                 "memory_profiler_step": 0, "log_query_settings": 0}
        if query_id is not None:
            query["query_id"] = query_id
        if parameters:
            query.update({f"param_{key}": str(value) for key, value in parameters.items()})
        payload = statement.encode("utf-8") if body is None else (statement + "\n" + body).encode("utf-8")
        try:
            connection.request("POST", "/?" + urllib.parse.urlencode(query), body=payload,
                               headers={"Content-Type": "text/plain; charset=utf-8"})
            response = connection.getresponse()
            response_started = time.perf_counter()
            response_body = response.read().decode("utf-8", errors="replace")
            if timing is not None:
                timing["response_read_ms"] = timing.get("response_read_ms", 0.0) + (
                    time.perf_counter() - response_started
                ) * 1000
        except (http.client.HTTPException, OSError) as error:
            raise RuntimeError("ClickHouse HTTP request failed") from error
        if not 200 <= response.status < 300:
            raise RuntimeError(f"ClickHouse request failed ({response.status}): {response_body.strip()}")
        return response_body

    def database_exists(self, layout):
        """返回布局 database 是否存在。"""
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(connection,
                "SELECT count() AS count FROM system.databases WHERE name = {database:String} FORMAT JSONEachRow",
                parameters={"database": self._database(layout)}))
            return bool(rows and int(rows[0]["count"]))
        finally:
            connection.close()

    def create_layout(self, layout, budget):
        """创建一个空 database、分析表和原文表。"""
        validate_layout(layout)
        validate_budget(budget)
        database = self._database(layout)
        if self.database_exists(layout):
            raise ValueError(f"database already exists: {database}")
        connection = self.connect_worker()
        try:
            ddl = create_layout_ddl(self.namespace, layout, budget)
            statements = [item.strip() for item in ddl.split(";") if item.strip()]
            self._request(connection, statements[0])
            self._created_databases.add(database)
            for statement in statements[1:]:
                self._request(connection, statement)
            return {"database": database, "ddl": ddl}
        except Exception:
            if database in self._created_databases:
                self.cleanup(layout)
            raise
        finally:
            connection.close()

    @staticmethod
    def _analytics_row(layout, row):
        """把预处理记录转换为指定 ClickHouse 分析表行。"""
        try:
            attributes = row["attributes_analysis"]
            result = {
                "ingest_seq": row["ingest_seq"], "event_id": row["event_id"], "trace_id": row["trace_id"],
                "span_id": row["span_id"], "parent_span_id": row["parent_span_id"] or "",
                "project_id": row["project_id"], "start_time": clickhouse_timestamp(row["start_time"]),
                "end_time": clickhouse_timestamp(row["end_time"]), "duration_ms": row["duration_ms"],
                "span_type": row["span_type"], "framework": row["framework"], "level": row["level"],
                "attributes": canonical_bytes(attributes).decode("utf-8") if layout == "ch_string" else attributes,
            }
            if layout == "ch_native":
                result["fidelity_values"] = fidelity_values(attributes)
            return result
        except (KeyError, TypeError) as error:
            raise ValueError("invalid dataset row") from error

    def insert_block(self, layout, rows):
        """写入一个预生成 block，并记录构造、两次 INSERT 与完整 wall 时间。"""
        validate_layout(layout)
        if not isinstance(rows, list):
            raise ValueError("rows must be a list")
        if not rows:
            return {"analytics_insert_ms": 0.0, "block_wall_ms": 0.0, "client_row_build_ms": 0.0,
                    "raw_insert_ms": 0.0, "response_read_ms": 0.0, "rows": 0}
        wall_started = time.perf_counter()
        build_started = time.perf_counter()
        analytics_rows = [self._analytics_row(layout, row) for row in rows]
        raw_rows = [{"event_id": row["event_id"], "ingest_seq": row["ingest_seq"], "raw_event": row["raw_event"]} for row in rows]
        client_row_build_ms = (time.perf_counter() - build_started) * 1000
        response_timing = {}
        connection = self.connect_worker()
        try:
            analytics_started = time.perf_counter()
            self._request(connection, "INSERT INTO " + self._analytics(layout) + " FORMAT JSONEachRow",
                          "".join(canonical_bytes(row).decode("utf-8") + "\n" for row in analytics_rows),
                          timing=response_timing)
            analytics_insert_ms = (time.perf_counter() - analytics_started) * 1000
            raw_started = time.perf_counter()
            self._request(connection, "INSERT INTO " + self._raw(layout) + " FORMAT JSONEachRow",
                          "".join(canonical_bytes(row).decode("utf-8") + "\n" for row in raw_rows),
                          timing=response_timing)
            raw_insert_ms = (time.perf_counter() - raw_started) * 1000
        finally:
            connection.close()
        return {"analytics_insert_ms": analytics_insert_ms, "block_wall_ms": (time.perf_counter() - wall_started) * 1000,
                "client_row_build_ms": client_row_build_ms, "raw_insert_ms": raw_insert_ms,
                "response_read_ms": response_timing["response_read_ms"], "rows": len(rows)}

    def _query_parameters(self, query_id, params):
        """把 query catalog 参数转换为 ClickHouse 安全参数。"""
        if query_id not in QUERY_IDS or not isinstance(params, dict):
            raise ValueError("invalid query parameters")
        try:
            values = {"project_id": params["project_id"], "start_time": clickhouse_timestamp(params["start_time"]),
                      "end_time": clickhouse_timestamp(params["end_time"])}
            if query_id == "S02": values["operation_name"] = params["operation_name"]
            if query_id == "S03": values["failure_mistake_mode"] = params["failure_mistake_mode"]
            if query_id == "S05": values["trace_id"] = params["trace_id"]
            if query_id == "S06":
                if isinstance(params["page_size"], bool) or not isinstance(params["page_size"], int) or params["page_size"] <= 0:
                    raise ValueError("page_size must be positive")
                values["page_size"] = params["page_size"]
            return values
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None

    def _statement(self, layout, query_id):
        """返回填入本实例分析表名的固定 SQL。"""
        return query_sql(layout, query_id).replace("{analytics}", self._analytics(layout))

    @staticmethod
    def _format_timestamp(value):
        """把 ClickHouse DateTime64 文本规范化为毫秒 UTC ISO 格式。"""
        if not isinstance(value, str):
            raise ValueError("start_time must be a string")
        text = value.replace(" ", "T")
        if text.endswith("Z") or "+" in text[10:]: return text
        if "." not in text: text += ".000"
        whole, fraction = text.split(".", 1)
        return whole + "." + fraction[:3].ljust(3, "0") + "Z"

    @staticmethod
    def _load_attributes(value):
        """把 String JSON 或 Native JSON 响应恢复为对象。"""
        if isinstance(value, str):
            try: value = json.loads(value)
            except json.JSONDecodeError as error: raise ValueError("attributes must be JSON") from error
        if not isinstance(value, dict): raise ValueError("attributes must be an object")
        return value

    @staticmethod
    def _path_value(value):
        """把 String JSON 路径投影恢复为原始 JSON 值。"""
        if value is None or value == "": return None
        if isinstance(value, str):
            try: return json.loads(value)
            except json.JSONDecodeError: return value
        return value

    def _document(self, row, layout):
        """恢复一条完整文档行，包括 Native JSON Sidecar 覆盖。"""
        attributes = self._load_attributes(row["attributes"])
        if layout == "ch_native": attributes = merge_fidelity(attributes, row.get("fidelity_values", {}))
        return [self._format_timestamp(row["start_time"]), row["event_id"], attributes]

    def _normalize_result(self, query_id, rows, params=None, layout=None):
        """在完整响应读取后规范化 S01 至 S06 结果。"""
        if query_id in {"S01", "S02"}:
            return [[row["span_type"], int(row["count"])] for row in sorted(rows, key=lambda row: row["span_type"])]
        if query_id == "S03":
            event_ids = sorted(row["event_id"] for row in rows)
            return {"row_count": len(event_ids), "identity_sha256": hashlib.sha256(canonical_bytes(event_ids)).hexdigest()}
        if query_id == "S04":
            values = [self._path_value(row.get("value")) for row in rows]
            values = [value for value in values if value is not None]
            return {"non_null_count": len(values), "utf8_bytes": sum(len(canonical_bytes(value)) for value in values)}
        if query_id == "S05":
            return [self._document(row, layout) for row in sorted(rows, key=lambda row: (row["start_time"], row["event_id"]))]
        documents = [self._document(row, layout) for row in rows]
        return {"identity_sha256": hashlib.sha256(canonical_bytes([row[1] for row in documents])).hexdigest(),
                "page_row_count": len(documents), "row_count": int(rows[0]["total_rows"]) if rows else 0, "rows": documents}

    def execute_query(self, connection, layout, query_id, params):
        """执行一条查询并返回延迟、恢复耗时和待批量采集的唯一 query ID。"""
        validate_layout(layout)
        server_query_id = f"json_s2sup_{query_id.lower()}_{uuid.uuid4().hex}"
        started = time.perf_counter()
        body = self._request(connection, self._statement(layout, query_id), parameters=self._query_parameters(query_id, params), query_id=server_query_id)
        latency_ms = (time.perf_counter() - started) * 1000
        recovery_started = time.perf_counter()
        result = self._normalize_result(query_id, self._json_rows(body), params, layout)
        recovery_ms = (time.perf_counter() - recovery_started) * 1000
        row_count = result["row_count"] if query_id == "S03" else result["non_null_count"] if query_id == "S04" else result["page_row_count"] if query_id == "S06" else len(result)
        return {"latency_ms": latency_ms, "recovery_ms": recovery_ms, "result": result,
                "result_sha256": hashlib.sha256(canonical_bytes(result)).hexdigest(), "row_count": row_count,
                "query_log_id": server_query_id}

    def collect_plan(self, layout, query_id, params):
        """返回与正式查询使用相同绑定参数的 ClickHouse 自然执行计划。"""
        validate_layout(layout)
        connection = self.connect_worker()
        try:
            plan = self._request(
                connection,
                "EXPLAIN " + self._statement(layout, query_id),
                parameters=self._query_parameters(query_id, params),
            ).strip()
            if not plan:
                raise RuntimeError("empty ClickHouse natural plan")
            return {"natural": plan}
        finally:
            connection.close()

    def collect_query_logs(self, query_ids, attempts=20):
        """阶段结束后批量获取每个 query ID 的 QueryFinish 指标。"""
        if not isinstance(query_ids, list) or len(set(query_ids)) != len(query_ids) or any(not isinstance(value, str) or not QUERY_LOG_ID.fullmatch(value) for value in query_ids):
            raise ValueError("query_ids must contain unique valid IDs")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0: raise ValueError("attempts must be positive")
        if not query_ids: return {}
        statement = ("SELECT query_id,type,exception_code,query_duration_ms,read_rows,read_bytes,memory_usage,result_rows,result_bytes,"
                     "ProfileEvents['SelectedRows'] AS selected_rows,ProfileEvents['SelectedBytes'] AS selected_bytes FROM system.query_log "
                     "WHERE query_id IN {target_query_ids:Array(String)} AND type != 'QueryStart' FORMAT JSONEachRow")
        parameters = {"target_query_ids": "[" + ",".join("'" + value + "'" for value in query_ids) + "]"}
        expected = set(query_ids); metrics = {}
        connection = self.connect_worker()
        try:
            self._request(connection, "SYSTEM FLUSH LOGS query_log")
            for attempt in range(attempts):
                metrics = {}
                for row in self._json_rows(self._request(connection, statement, parameters=parameters)):
                    query_id = row.get("query_id")
                    if query_id not in expected or query_id in metrics or row.get("type") != "QueryFinish" or row.get("exception_code") != 0:
                        raise RuntimeError(f"invalid QueryFinish status for {query_id}")
                    values = {}
                    for field in QUERY_LOG_METRICS:
                        value = row.get(field)
                        if isinstance(value, bool) or not (isinstance(value, int) and value >= 0 or isinstance(value, str) and re.fullmatch(r"[0-9]+", value)):
                            raise RuntimeError(f"invalid QueryFinish {field} for {query_id}")
                        values[field] = int(value)
                    metrics[query_id] = values
                if set(metrics) == expected: return metrics
                if attempt < attempts - 1: time.sleep(0.05)
            raise RuntimeError("missing QueryFinish rows: " + ",".join(sorted(expected - set(metrics))))
        finally:
            connection.close()

    def _merge_backlog(self, layout):
        """返回目标 database 当前 active merge 数。"""
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(connection, "SELECT count() AS count FROM system.merges WHERE database = {database:String} FORMAT JSONEachRow", parameters={"database": self._database(layout)}))
            return int(rows[0]["count"])
        finally: connection.close()

    def finish_maintenance(self, layout, timeout_seconds):
        """等待 merge backlog 连续三次为零，基础比较不执行 OPTIMIZE FINAL。"""
        validate_layout(layout)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0: raise ValueError("timeout_seconds must be positive")
        started = time.monotonic(); observations = []; zero_streak = 0
        while True:
            backlog = self._merge_backlog(layout); observations.append(backlog)
            zero_streak = zero_streak + 1 if backlog == 0 else 0
            if zero_streak == 3: return {"completed": True, "observations": observations, "timed_out": False, "waited_seconds": time.monotonic() - started, "zero_streak": zero_streak}
            if time.monotonic() - started >= timeout_seconds: return {"completed": False, "observations": observations, "timed_out": True, "waited_seconds": time.monotonic() - started, "zero_streak": zero_streak}
            time.sleep(0.1)

    def collect_storage(self, layout):
        """采集 active parts、压缩字节、merge 状态和 Native JSON 路径。"""
        validate_layout(layout); database = self._database(layout); connection = self.connect_worker()
        try:
            tables = {}
            for table in ("analytics", "raw"):
                row = self._json_rows(self._request(connection, "SELECT count() AS part_count,sum(rows) AS rows,sum(data_compressed_bytes) AS compressed_bytes,sum(data_uncompressed_bytes) AS uncompressed_bytes FROM system.parts WHERE active AND database = {database:String} AND table = {table:String} FORMAT JSONEachRow", parameters={"database": database, "table": table}))[0]
                tables[table] = {field: int(row[field] or 0) for field in ("part_count", "rows", "compressed_bytes", "uncompressed_bytes")}
            paths = None
            if layout == "ch_native":
                row = self._json_rows(self._request(connection, "SELECT arraySort(arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(attributes))))) AS dynamic_paths,arraySort(arrayDistinct(arrayFlatten(groupArray(JSONSharedDataPaths(attributes))))) AS shared_paths FROM " + self._analytics(layout) + " FORMAT JSONEachRow"))[0]
                paths = {"dynamic_paths": row["dynamic_paths"], "shared_paths": row["shared_paths"]}
            return {"tables": tables, "merge_backlog": self._merge_backlog(layout), "paths": paths}
        finally: connection.close()

    def _verify_records(self, layout, truth, field, statement, digest):
        """验证 identity、重复、缺失、额外与逐行摘要。"""
        try: expected = {record["event_id"]: record[field] for record in truth["records"]}
        except (KeyError, TypeError) as error: raise ValueError("invalid truth records") from error
        connection = self.connect_worker()
        try: rows = self._json_rows(self._request(connection, statement))
        finally: connection.close()
        actual_ids = [row["event_id"] for row in rows]; counts = Counter(actual_ids); actual = set(actual_ids)
        mismatches = sorted(row["event_id"] for row in rows if row["event_id"] in expected and digest(row) != expected[row["event_id"]])
        diagnostics = {"actual_count": len(actual_ids), "duplicate_count": sum(count > 1 for count in counts.values()), "duplicates": sorted(key for key, count in counts.items() if count > 1), "expected_count": len(expected), "extra": sorted(actual - set(expected)), "missing": sorted(set(expected) - actual), f"{field}_mismatches": mismatches}
        diagnostics["ok"] = not any((diagnostics["duplicates"], diagnostics["extra"], diagnostics["missing"], mismatches)) and diagnostics["actual_count"] == diagnostics["expected_count"]
        return diagnostics

    def verify_raw(self, layout, truth):
        """验证原文表的 UTF-8 SHA-256 和 identity。"""
        return self._verify_records(layout, truth, "raw_sha256", "SELECT event_id,raw_event FROM " + self._raw(layout) + " ORDER BY event_id FORMAT JSONEachRow", lambda row: hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest())

    def verify_analysis(self, layout, truth):
        """验证分析属性 canonical SHA-256 和 identity。"""
        fields = "event_id,attributes" + (",fidelity_values" if layout == "ch_native" else "")
        def digest(row):
            attributes = self._load_attributes(row["attributes"])
            if layout == "ch_native": attributes = merge_fidelity(attributes, row.get("fidelity_values", {}))
            return hashlib.sha256(canonical_bytes(attributes)).hexdigest()
        return self._verify_records(layout, truth, "analysis_sha256", "SELECT " + fields + " FROM " + self._analytics(layout) + " ORDER BY event_id FORMAT JSONEachRow", digest)

    def cleanup(self, layout):
        """仅删除当前实例成功创建的 database，并确认没有残留。"""
        database = self._database(layout)
        if database not in self._created_databases: return {"database": database, "removed": False}
        connection = self.connect_worker()
        try: self._request(connection, "DROP DATABASE IF EXISTS " + database + " SYNC")
        finally: connection.close()
        if self.database_exists(layout): raise RuntimeError(f"database cleanup failed: {database}")
        self._created_databases.remove(database)
        return {"database": database, "removed": True}

    def cleanup_all(self):
        """删除当前实例仍持有的全部临时 database。"""
        return [self.cleanup(layout) for layout in CLICKHOUSE_LAYOUTS]
