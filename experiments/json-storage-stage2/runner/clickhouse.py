"""阶段二 ClickHouse residual 布局 adapter。"""

import hashlib
import http.client
import json
import re
import time
import urllib.parse
import uuid
from collections import Counter


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
QUERY_LOG_ID = re.compile(r"^[A-Za-z0-9_-]+$")
LAYOUTS = ("ch_string", "ch_map", "ch_native")
QUERY_IDS = ("Q01", "Q02", "Q03", "Q04", "Q05")
QUERY_LOG_METRICS = (
    "query_duration_ms", "read_rows", "read_bytes", "memory_usage",
    "result_rows", "result_bytes", "selected_rows", "selected_bytes",
)


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
    """验证临时 database 与对象名称的 SQL identifier。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must match ^[a-z][a-z0-9_]{{0,62}}$")
    return value


def validate_layout(layout):
    """验证阶段二固定 ClickHouse layout ID。"""
    if layout not in LAYOUTS:
        raise ValueError(f"unsupported ClickHouse layout: {layout}")
    return layout


def validate_budget(budget):
    """验证 native JSON 动态路径预算。"""
    if not isinstance(budget, int) or isinstance(budget, bool) or not 0 < budget <= 128:
        raise ValueError("budget must be between 1 and 128")
    return budget


def database_name(namespace, layout):
    """返回一个 layout 独占的已验证 database 名。"""
    validate_identifier(namespace, "namespace")
    validate_layout(layout)
    return validate_identifier(f"{namespace}_{layout}", "database name")


def create_layout_ddl(namespace, layout, budget):
    """返回一个 layout database、analytics 与 raw 表的固定 DDL。"""
    database = database_name(namespace, layout)
    validate_budget(budget)
    residual = {
        "ch_string": "attributes String CODEC(ZSTD(3))",
        "ch_map": "attributes Map(String,String)",
        "ch_native": f"attributes JSON(max_dynamic_paths={budget})",
    }[layout]
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
        f"    {residual}" + (",\n    fidelity_values Map(String,String)" if layout == "ch_native" else "") + "\n"
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
    """返回使用 `{analytics}` 占位符的固定 ClickHouse 查询 SQL。"""
    validate_layout(layout)
    if query_id not in QUERY_IDS:
        raise ValueError(f"unsupported query ID: {query_id}")
    visibility = (
        "project_id = {project_id:String} "
        "AND start_time >= {start_time:DateTime64(3, 'UTC')} "
        "AND start_time < {end_time:DateTime64(3, 'UTC')} "
        "AND ingest_seq < {watermark:UInt64}"
    )
    operation = {
        "ch_string": "JSONExtractString(attributes, 'gen_ai', 'operation', 'name') = {operation_name:String}",
        "ch_map": "attributes['gen_ai.operation.name'] = {operation_value:String}",
        "ch_native": "attributes.gen_ai.operation.name.:String = {operation_name:String}",
    }[layout]
    failure = {
        "ch_string": "JSONExtractString(attributes, 'failure', 'mistake_mode') = {failure_mistake_mode:String}",
        "ch_map": "attributes['failure.mistake_mode'] = {failure_value:String}",
        "ch_native": "attributes.failure.mistake_mode.:String = {failure_mistake_mode:String}",
    }[layout]
    statements = {
        "Q01": (
            "SELECT span_type, count() AS count FROM {analytics} WHERE "
            f"{visibility} GROUP BY span_type ORDER BY span_type FORMAT JSONEachRow"
        ),
        "Q02": (
            "SELECT span_type, count() AS count FROM {analytics} WHERE "
            f"{visibility} AND {operation} GROUP BY span_type ORDER BY span_type FORMAT JSONEachRow"
        ),
        "Q03": (
            "SELECT span_type, count() AS count, sum(duration_ms) AS duration_ms FROM {analytics} WHERE "
            f"{visibility} GROUP BY span_type ORDER BY span_type FORMAT JSONEachRow"
        ),
        "Q04": (
            "SELECT start_time, event_id, attributes" + (", fidelity_values" if layout == "ch_native" else "") + " FROM {analytics} WHERE "
            f"{visibility} AND trace_id = {{trace_id:String}} "
            "ORDER BY start_time, event_id FORMAT JSONEachRow"
        ),
        "Q05": (
            "SELECT event_id FROM {analytics} WHERE "
            f"{visibility} AND {failure} ORDER BY event_id FORMAT JSONEachRow"
        ),
    }
    return statements[query_id]


def clickhouse_timestamp(value):
    """把生成器毫秒 UTC ISO 文本转换为 ClickHouse DateTime64 输入文本。"""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value
    ):
        raise ValueError("timestamp must use millisecond UTC ISO format")
    return value.replace("T", " ").removesuffix("Z")


class ClickHouseAdapter:
    """提供阶段二 ClickHouse layout 的连接、查询和完整性操作。"""

    def __init__(self, host, port, container_name, namespace):
        """保存 HTTP 端点、容器身份和临时 database 前缀。"""
        if not isinstance(host, str) or not host:
            raise ValueError("host must be a non-empty string")
        if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
            raise ValueError("port must be positive")
        validate_identifier(namespace, "namespace")
        for layout in LAYOUTS:
            database_name(namespace, layout)
        self.host = host
        self.port = port
        self.container_name = container_name
        self.namespace = namespace

    create_layout_ddl = staticmethod(create_layout_ddl)
    query_sql = staticmethod(query_sql)

    def _database(self, layout):
        """返回 layout 的独立 database 名。"""
        return database_name(self.namespace, layout)

    def _analytics(self, layout):
        """返回已验证的 analytics 表名。"""
        return f"{self._database(layout)}.analytics"

    def _raw(self, layout):
        """返回已验证的 raw 表名。"""
        return f"{self._database(layout)}.raw"

    def connect_worker(self):
        """建立一条供单个 worker 在阶段内复用的独立 HTTP 连接。"""
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.connect()
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _json_rows(body):
        """解析 ClickHouse JSONEachRow 响应。"""
        try:
            return [json.loads(line) for line in body.splitlines() if line]
        except json.JSONDecodeError as error:
            raise RuntimeError("invalid ClickHouse JSONEachRow response") from error

    def _request(self, connection, statement, body=None, parameters=None, query_id=None):
        """发送 SQL 并完整读取响应；调用方决定 latency 计时边界。"""
        if query_id is not None and not QUERY_LOG_ID.fullmatch(query_id):
            raise ValueError("query_id contains unsupported characters")
        query = {
            "log_queries": 1 if query_id is not None else 0,
            "log_processors_profiles": 0,
            "memory_profiler_step": 0,
            "log_query_settings": 0,
        }
        if query_id is not None:
            query["query_id"] = query_id
        if parameters:
            query.update({f"param_{key}": str(value) for key, value in parameters.items()})
        path = "/" + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = statement.encode("utf-8") if body is None else (statement + "\n" + body).encode("utf-8")
        try:
            connection.request("POST", path, body=payload, headers={"Content-Type": "text/plain; charset=utf-8"})
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
        except (http.client.HTTPException, OSError) as error:
            raise RuntimeError("ClickHouse HTTP request failed") from error
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"ClickHouse request failed ({response.status}): {response_body.strip()}")
        return response_body

    def database_exists(self, layout):
        """返回 layout database 是否仍存在。"""
        database = self._database(layout)
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(
                connection,
                "SELECT count() AS count FROM system.databases WHERE name = {database:String} FORMAT JSONEachRow",
                parameters={"database": database},
            ))
            return bool(rows and int(rows[0]["count"]))
        finally:
            connection.close()

    def create_layout(self, layout, budget):
        """创建一个空 layout database 与两张 MergeTree 表。"""
        validate_layout(layout)
        validate_budget(budget)
        database = self._database(layout)
        if self.database_exists(layout):
            raise ValueError(f"database already exists: {database}")
        connection = self.connect_worker()
        database_created = False
        try:
            ddl = create_layout_ddl(self.namespace, layout, budget)
            statements = [item.strip() for item in ddl.split(";") if item.strip()]
            self._request(connection, statements[0])
            database_created = True
            for statement in statements[1:]:
                self._request(connection, statement)
            return {"database": database, "ddl": ddl}
        except Exception as error:
            # CREATE DATABASE 成功后取得所有权，建表失败时回滚本次部分创建。
            if database_created:
                try:
                    self.cleanup(layout)
                except Exception as cleanup_error:
                    error.add_note(f"layout creation cleanup failed: {cleanup_error}")
            raise
        finally:
            connection.close()

    @staticmethod
    def _analytics_row(layout, row):
        """把统一记录转换为指定 analytics residual 表示。"""
        try:
            attributes = {
                "ch_string": canonical_bytes(row["attributes_analysis"]).decode("utf-8"),
                "ch_map": row["attributes_map"],
                "ch_native": row["attributes_analysis"],
            }[layout]
            result = {
                "ingest_seq": row["ingest_seq"],
                "event_id": row["event_id"],
                "trace_id": row["trace_id"],
                "span_id": row["span_id"],
                "parent_span_id": row["parent_span_id"],
                "project_id": row["project_id"],
                "start_time": clickhouse_timestamp(row["start_time"]),
                "end_time": clickhouse_timestamp(row["end_time"]),
                "duration_ms": row["duration_ms"],
                "span_type": row["span_type"],
                "framework": row["framework"],
                "level": row["level"],
                "attributes": attributes,
            }
            if layout == "ch_native":
                result["fidelity_values"] = ClickHouseAdapter._native_special_values(row)
            return result
        except (KeyError, TypeError) as error:
            raise ValueError("invalid dataset row") from error

    @staticmethod
    def _contains_native_lossy_value(value):
        """返回值是否递归包含 native JSON 无法区分的 null 或空容器。"""
        if value is None:
            return True
        if isinstance(value, dict):
            return not value or any(
                ClickHouseAdapter._contains_native_lossy_value(item)
                for item in value.values()
            )
        if isinstance(value, list):
            return not value or any(
                ClickHouseAdapter._contains_native_lossy_value(item)
                for item in value
            )
        return False

    @staticmethod
    def _native_special_values(row):
        """提取递归含 null 或空容器的原始 Attribute canonical 值。"""
        try:
            values = row["attributes_map"]
        except (KeyError, TypeError) as error:
            raise ValueError("invalid dataset row") from error
        if not isinstance(values, dict):
            raise ValueError("attributes_map must be an object")
        special = {}
        for key, canonical_value in values.items():
            if not isinstance(key, str) or not isinstance(canonical_value, str):
                raise ValueError("attributes_map must contain string keys and values")
            try:
                value = json.loads(canonical_value)
            except json.JSONDecodeError as error:
                raise ValueError("attributes_map value must be canonical JSON") from error
            if canonical_bytes(value).decode("utf-8") != canonical_value:
                raise ValueError("attributes_map value must be canonical JSON")
            if ClickHouseAdapter._contains_native_lossy_value(value):
                special[key] = canonical_value
        return special

    def insert_block(self, layout, rows):
        """顺序写 analytics 与 raw；两次成功后才返回，失败直接抛出。"""
        validate_layout(layout)
        if not rows:
            return {"rows": 0}
        analytics_rows = [self._analytics_row(layout, row) for row in rows]
        try:
            raw_rows = [
                {"event_id": row["event_id"], "ingest_seq": row["ingest_seq"], "raw_event": row["raw_event"]}
                for row in rows
            ]
        except (KeyError, TypeError) as error:
            raise ValueError("invalid dataset row") from error
        connection = self.connect_worker()
        try:
            self._request(
                connection,
                "INSERT INTO " + self._analytics(layout) + " FORMAT JSONEachRow",
                "".join(canonical_bytes(row).decode("utf-8") + "\n" for row in analytics_rows),
            )
            self._request(
                connection,
                "INSERT INTO " + self._raw(layout) + " FORMAT JSONEachRow",
                "".join(canonical_bytes(row).decode("utf-8") + "\n" for row in raw_rows),
            )
        finally:
            connection.close()
        return {"rows": len(rows)}

    def _query_parameters(self, query_id, params, watermark):
        """把 truth 查询参数及水位映射为 ClickHouse 安全参数。"""
        if not isinstance(params, dict):
            raise ValueError("query params must be an object")
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark < 0:
            raise ValueError("watermark must be a non-negative integer")
        try:
            values = {
                "project_id": params["project_id"],
                "start_time": clickhouse_timestamp(params["start_time"]),
                "end_time": clickhouse_timestamp(params["end_time"]),
                "watermark": watermark,
            }
            if query_id == "Q02":
                values["operation_name"] = params["operation_name"]
                values["operation_value"] = canonical_bytes(params["operation_name"]).decode("utf-8")
            elif query_id == "Q04":
                values["trace_id"] = params["trace_id"]
            elif query_id == "Q05":
                values["failure_mistake_mode"] = params["failure_mistake_mode"]
                values["failure_value"] = canonical_bytes(params["failure_mistake_mode"]).decode("utf-8")
            return values
        except KeyError as error:
            raise ValueError(f"missing query parameter: {error.args[0]}") from None

    def _statement(self, layout, query_id):
        """返回使用已验证表名的完整查询 SQL。"""
        return query_sql(layout, query_id).replace("{analytics}", self._analytics(layout))

    @staticmethod
    def _format_timestamp(value):
        """把 ClickHouse DateTime64 文本规范化为生成器毫秒 UTC 格式。"""
        if not isinstance(value, str):
            raise ValueError("Q04 start_time must be a string")
        text = value.replace(" ", "T")
        if text.endswith("Z"):
            return text
        if "+" in text[10:]:
            return text
        if "." not in text:
            text += ".000"
        whole, fraction = text.split(".", 1)
        return whole + "." + fraction[:3].ljust(3, "0") + "Z"

    @staticmethod
    def _restore_attributes(attributes, key_map):
        """按 truth key_map 把 nested residual 恢复为原始键及 canonical JSON 值。"""
        if not isinstance(key_map, dict):
            raise ValueError("Q04 requires truth key_map")
        if isinstance(attributes, str):
            try:
                attributes = json.loads(attributes)
            except json.JSONDecodeError as error:
                raise ValueError("Q04 attributes must be JSON") from error
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

    @staticmethod
    def _restore_map_attributes(attributes, key_map):
        """验证并返回 Map residual 已保存的原始键及 canonical 值。"""
        if not isinstance(key_map, dict):
            raise ValueError("Q04 requires truth key_map")
        if not isinstance(attributes, dict):
            raise ValueError("Q04 Map attributes must be an object")
        known_keys = set(key_map.values())
        restored = {}
        for key, canonical_value in attributes.items():
            if key not in known_keys:
                raise ValueError(f"unknown Map attribute key: {key}")
            if not isinstance(canonical_value, str):
                raise ValueError("Map attribute value must be canonical JSON")
            try:
                value = json.loads(canonical_value)
            except json.JSONDecodeError as error:
                raise ValueError("Map attribute value must be canonical JSON") from error
            if canonical_bytes(value).decode("utf-8") != canonical_value:
                raise ValueError("Map attribute value must be canonical JSON")
            restored[key] = canonical_value
        return {key: restored[key] for key in sorted(restored)}

    @staticmethod
    def _merge_native_special_values(restored, special, key_map):
        """用 native JSON 无法表达的 Attribute canonical 状态覆盖 residual 恢复值。"""
        if not isinstance(special, dict):
            raise ValueError("native fidelity_values must be an object")
        known_keys = set(key_map.values())
        merged = dict(restored)
        for key, canonical_value in special.items():
            if key not in known_keys:
                raise ValueError(f"unknown native special attribute key: {key}")
            if not isinstance(canonical_value, str):
                raise ValueError("native special attribute value must be canonical JSON")
            try:
                value = json.loads(canonical_value)
            except json.JSONDecodeError as error:
                raise ValueError("native special attribute value must be canonical JSON") from error
            if canonical_bytes(value).decode("utf-8") != canonical_value:
                raise ValueError("native special attribute value must be canonical JSON")
            merged[key] = canonical_value
        return {key: merged[key] for key in sorted(merged)}

    def _normalize_result(self, query_id, rows, params, layout=None):
        """在 latency 计时外将 JSONEachRow 响应转换为公共 result。"""
        if query_id in {"Q01", "Q02"}:
            return [[row["span_type"], int(row["count"])] for row in sorted(rows, key=lambda row: row["span_type"])]
        if query_id == "Q03":
            return [[row["span_type"], int(row["count"]), int(row["duration_ms"])] for row in sorted(rows, key=lambda row: row["span_type"])]
        if query_id == "Q04":
            key_map = params.get("key_map")
            if layout == "ch_map":
                return [
                    [self._format_timestamp(row["start_time"]), row["event_id"], self._restore_map_attributes(row["attributes"], key_map)]
                    for row in sorted(rows, key=lambda row: (row["start_time"], row["event_id"]))
                ]
            return [
                [
                    self._format_timestamp(row["start_time"]),
                    row["event_id"],
                    self._merge_native_special_values(
                        self._restore_attributes(row["attributes"], key_map),
                        row.get("fidelity_values", {}),
                        key_map,
                    ) if layout == "ch_native" else self._restore_attributes(row["attributes"], key_map),
                ]
                for row in sorted(rows, key=lambda row: (row["start_time"], row["event_id"]))
            ]
        event_ids = sorted(row["event_id"] for row in rows)
        return {
            "row_count": len(event_ids),
            "identity_sha256": hashlib.sha256(canonical_bytes(event_ids)).hexdigest(),
        }

    def collect_query_logs(self, query_ids, attempts=20):
        """阶段结束时单次 flush query_log，返回每个唯一 ID 的完整 QueryFinish 指标。"""
        if not isinstance(query_ids, list) or any(
            not isinstance(query_id, str) or not QUERY_LOG_ID.fullmatch(query_id)
            for query_id in query_ids
        ) or len(set(query_ids)) != len(query_ids):
            raise ValueError("query_ids must contain unique valid IDs")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
            raise ValueError("attempts must be positive")
        if not query_ids:
            return {}
        statement = (
            "SELECT query_id, type, exception_code, query_duration_ms, read_rows, read_bytes, memory_usage, result_rows, result_bytes, "
            "ProfileEvents['SelectedRows'] AS selected_rows, "
            "ProfileEvents['SelectedBytes'] AS selected_bytes "
            "FROM system.query_log WHERE query_id IN {target_query_ids:Array(String)} "
            "AND type != 'QueryStart' FORMAT JSONEachRow"
        )
        parameters = {"target_query_ids": "[" + ",".join("'" + query_id + "'" for query_id in query_ids) + "]"}
        expected = set(query_ids)
        connection = self.connect_worker()
        try:
            self._request(connection, "SYSTEM FLUSH LOGS query_log")
            for attempt in range(attempts):
                rows = self._json_rows(self._request(connection, statement, parameters=parameters))
                metrics = {}
                for row in rows:
                    query_id = row.get("query_id")
                    if query_id not in expected or query_id in metrics:
                        raise RuntimeError(f"unexpected or duplicate query log ID: {query_id}")
                    if row.get("type") != "QueryFinish" or row.get("exception_code") != 0:
                        raise RuntimeError(f"invalid QueryFinish status for {query_id}")
                    values = {}
                    for field in QUERY_LOG_METRICS:
                        value = row.get(field)
                        if isinstance(value, bool) or not (
                            isinstance(value, int) and value >= 0
                            or isinstance(value, str) and re.fullmatch(r"[0-9]+", value)
                        ):
                            raise RuntimeError(f"invalid QueryFinish {field} for {query_id}")
                        values[field] = int(value)
                    metrics[query_id] = values
                if set(metrics) == expected:
                    return metrics
                if attempt < attempts - 1:
                    time.sleep(0.05)
            raise RuntimeError("missing QueryFinish rows: " + ",".join(sorted(expected - set(metrics))))
        finally:
            connection.close()

    def execute_query(self, connection, layout, query_id, params, watermark):
        """执行并完整读取查询，返回公共结果、hash 与待批量采集的 query ID。"""
        validate_layout(layout)
        statement = self._statement(layout, query_id)
        query_parameters = self._query_parameters(query_id, params, watermark)
        server_query_id = f"json_s2_{query_id.lower()}_{uuid.uuid4().hex}"
        started = time.perf_counter()
        body = self._request(connection, statement, parameters=query_parameters, query_id=server_query_id)
        latency_ms = (time.perf_counter() - started) * 1000
        rows = self._json_rows(body)
        result = self._normalize_result(query_id, rows, params, layout=layout)
        row_count = result["row_count"] if query_id == "Q05" else len(result)
        return {
            "latency_ms": latency_ms,
            "result": result,
            "result_sha256": hashlib.sha256(canonical_bytes(result)).hexdigest(),
            "row_count": row_count,
            "query_log_id": server_query_id,
        }

    def collect_plan(self, layout, query_id, params, watermark):
        """返回同一参数绑定查询的 ClickHouse EXPLAIN 文本。"""
        validate_layout(layout)
        connection = self.connect_worker()
        try:
            return {
                "explain": self._request(
                    connection,
                    "EXPLAIN indexes = 1 " + self._statement(layout, query_id),
                    parameters=self._query_parameters(query_id, params, watermark),
                )
            }
        finally:
            connection.close()

    def _merge_backlog(self, layout):
        """返回目标 database 当前 active merge 数。"""
        validate_layout(layout)
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(
                connection,
                "SELECT count() AS count FROM system.merges WHERE database = {database:String} FORMAT JSONEachRow",
                parameters={"database": self._database(layout)},
            ))
            return int(rows[0]["count"])
        finally:
            connection.close()

    def collect_storage(self, layout):
        """采集 analytics/raw active parts、压缩字节、merge backlog 与 native 路径。"""
        validate_layout(layout)
        database = self._database(layout)
        connection = self.connect_worker()
        try:
            tables = {}
            for name in ("analytics", "raw"):
                rows = self._json_rows(self._request(
                    connection,
                    "SELECT count() AS part_count, sum(rows) AS rows, "
                    "sum(data_compressed_bytes) AS compressed_bytes, "
                    "sum(data_uncompressed_bytes) AS uncompressed_bytes "
                    "FROM system.parts WHERE active AND database = {database:String} "
                    "AND table = {table:String} FORMAT JSONEachRow",
                    parameters={"database": database, "table": name},
                ))
                row = rows[0]
                tables[name] = {key: int(row[key] or 0) for key in ("part_count", "rows", "compressed_bytes", "uncompressed_bytes")}
            paths = None
            if layout == "ch_native":
                rows = self._json_rows(self._request(
                    connection,
                    "SELECT arraySort(arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(attributes))))) AS dynamic_paths, "
                    "arraySort(arrayDistinct(arrayFlatten(groupArray(JSONSharedDataPaths(attributes))))) AS shared_paths "
                    "FROM " + self._analytics(layout) + " FORMAT JSONEachRow",
                ))
                paths = {"dynamic_paths": rows[0]["dynamic_paths"], "shared_paths": rows[0]["shared_paths"]}
            return {"tables": tables, "merge_backlog": self._merge_backlog(layout), "paths": paths}
        finally:
            connection.close()

    def finish_maintenance(self, layout, timeout_seconds):
        """等待 active merge backlog 连续三次为零，并返回完整等待证据。"""
        validate_layout(layout)
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = time.monotonic()
        observations = []
        zero_streak = 0
        while True:
            backlog = self._merge_backlog(layout)
            observations.append(backlog)
            zero_streak = zero_streak + 1 if backlog == 0 else 0
            if zero_streak == 3:
                return {"completed": True, "observations": observations, "timed_out": False, "waited_seconds": time.monotonic() - started, "zero_streak": zero_streak}
            if time.monotonic() - started >= timeout_seconds:
                return {"completed": False, "observations": observations, "timed_out": True, "waited_seconds": time.monotonic() - started, "zero_streak": zero_streak}
            time.sleep(0.1)

    def verify_raw(self, layout, truth):
        """按 event_id 集合和 UTF-8 raw SHA-256 验证 raw 表，不保留 payload。"""
        validate_layout(layout)
        try:
            expected_hashes = {record["event_id"]: record["raw_sha256"] for record in truth["records"]}
        except (KeyError, TypeError) as error:
            raise ValueError("invalid raw truth records") from error
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(connection, "SELECT event_id, raw_event FROM " + self._raw(layout) + " ORDER BY event_id FORMAT JSONEachRow"))
        finally:
            connection.close()
        actual_ids = [row["event_id"] for row in rows]
        actual_set = set(actual_ids)
        expected_set = set(expected_hashes)
        duplicates = sorted(event_id for event_id, count in Counter(actual_ids).items() if count > 1)
        mismatches = sorted(
            row["event_id"] for row in rows
            if row["event_id"] in expected_hashes
            and hashlib.sha256(row["raw_event"].encode("utf-8")).hexdigest() != expected_hashes[row["event_id"]]
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
        diagnostics["ok"] = not any(diagnostics[name] for name in ("duplicates", "extra", "missing", "raw_sha256_mismatches")) and diagnostics["actual_count"] == diagnostics["expected_count"]
        return diagnostics

    @staticmethod
    def _analysis_from_values(values, key_map):
        """按 key_map 将原始键的 canonical 值重建为 nested analysis 对象。"""
        if not isinstance(values, dict) or not isinstance(key_map, dict):
            raise ValueError("analysis values and key_map must be objects")
        inverse_map = {original: path for path, original in key_map.items()}
        analysis = {}
        for original_key, canonical_value in values.items():
            if original_key not in inverse_map:
                raise ValueError(f"unknown analysis attribute key: {original_key}")
            if not isinstance(canonical_value, str):
                raise ValueError("analysis attribute value must be canonical JSON")
            try:
                value = json.loads(canonical_value)
            except json.JSONDecodeError as error:
                raise ValueError("analysis attribute value must be canonical JSON") from error
            if canonical_bytes(value).decode("utf-8") != canonical_value:
                raise ValueError("analysis attribute value must be canonical JSON")
            current = analysis
            parts = inverse_map[original_key].split(".")
            for index, part in enumerate(parts):
                if not part:
                    raise ValueError("invalid analysis key_map path")
                if index == len(parts) - 1:
                    if part in current:
                        raise ValueError("duplicate analysis key_map path")
                    current[part] = value
                else:
                    nested = current.setdefault(part, {})
                    if not isinstance(nested, dict):
                        raise ValueError("analysis key_map prefix conflict")
                    current = nested
        return analysis

    def verify_analysis(self, layout, truth):
        """验证 analytics identity 与 canonical analysis SHA-256，不读取 raw 表。"""
        validate_layout(layout)
        try:
            expected_hashes = {
                record["event_id"]: record["analysis_sha256"] for record in truth["records"]
            }
            key_map = truth["key_map"]
        except (KeyError, TypeError) as error:
            raise ValueError("invalid analysis truth records") from error
        fields = "event_id, attributes"
        if layout == "ch_native":
            fields += ", fidelity_values"
        connection = self.connect_worker()
        try:
            rows = self._json_rows(self._request(
                connection,
                "SELECT " + fields + " FROM " + self._analytics(layout)
                + " ORDER BY event_id FORMAT JSONEachRow",
            ))
        finally:
            connection.close()
        actual_ids = [row["event_id"] for row in rows]
        actual_set = set(actual_ids)
        expected_set = set(expected_hashes)
        duplicates = sorted(event_id for event_id, count in Counter(actual_ids).items() if count > 1)
        mismatches = []
        for row in rows:
            event_id = row["event_id"]
            if event_id not in expected_hashes:
                continue
            if layout == "ch_string":
                attributes = row["attributes"]
                analysis = json.loads(attributes) if isinstance(attributes, str) else attributes
            elif layout == "ch_map":
                analysis = self._analysis_from_values(
                    self._restore_map_attributes(row["attributes"], key_map), key_map
                )
            else:
                restored = self._restore_attributes(row["attributes"], key_map)
                restored = self._merge_native_special_values(
                    restored, row.get("fidelity_values", {}), key_map
                )
                analysis = self._analysis_from_values(restored, key_map)
            if hashlib.sha256(canonical_bytes(analysis)).hexdigest() != expected_hashes[event_id]:
                mismatches.append(event_id)
        diagnostics = {
            "actual_count": len(actual_ids),
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
            "expected_count": len(expected_set),
            "extra": sorted(actual_set - expected_set),
            "missing": sorted(expected_set - actual_set),
            "analysis_sha256_mismatches": sorted(set(mismatches)),
        }
        diagnostics["ok"] = not any(
            diagnostics[name]
            for name in ("duplicates", "extra", "missing", "analysis_sha256_mismatches")
        ) and diagnostics["actual_count"] == diagnostics["expected_count"]
        return diagnostics

    def cleanup(self, layout):
        """删除一个 layout database，并确认没有同名 database 残留。"""
        database = self._database(layout)
        connection = self.connect_worker()
        try:
            self._request(connection, "DROP DATABASE IF EXISTS " + database + " SYNC")
        finally:
            connection.close()
        if self.database_exists(layout):
            raise RuntimeError(f"database cleanup failed: {database}")
        return {"database": database, "removed": True}
