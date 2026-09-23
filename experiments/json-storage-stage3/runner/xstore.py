"""阶段三 XStore (GaussVector) 四种 payload 布局 adapter。

XStore 是华为 GaussVector，兼容 PostgreSQL 协议但使用自定义 SASL 认证。
本 adapter 继承 OpenGaussAdapter，替换连接层为 gaussdb_libpq。
GaussDB 将空字符串视为 NULL（Oracle 兼容行为），因此 framework 列改为可空。
审计时需将 NULL framework 还原为空字符串以匹配冻结输入的 truth。
"""

import os
from pathlib import Path

from opengauss import OpenGaussAdapter, create_layout_ddls, _event_columns, LOGICAL_FIELDS
from common import build_layout_catalog, DatasetAudit, PhysicalTargetAudit

import gaussdb_libpq


def _xstore_event_columns(include_payload=False, include_asset=False):
    """GaussDB 兼容的事件列定义：framework 改为可空（空字符串等于 NULL）。"""
    columns = """ingest_seq BIGINT NOT NULL, event_id TEXT NOT NULL PRIMARY KEY,
trace_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT,
project_id TEXT NOT NULL, start_time TIMESTAMP(6) WITH TIME ZONE NOT NULL,
end_time TIMESTAMP(6) WITH TIME ZONE NOT NULL, duration_ms BIGINT NOT NULL,
span_type TEXT NOT NULL, framework TEXT, level TEXT NOT NULL,
cohort TEXT, profile TEXT, content_type TEXT, encoding TEXT,
content_length BIGINT, preview TEXT, sha256 TEXT"""
    if include_asset:
        columns += ", asset_id TEXT"
    if include_payload:
        columns += ", payload TEXT"
    return columns


def _xstore_create_layout_ddls(namespace, layout):
    """返回四种布局的完整 schema、表和访问结构 DDL（GaussDB 兼容版）。"""
    from opengauss import schema_name, _payload_columns, _indexes, ASSET_STATUSES
    schema = schema_name(namespace, layout)
    statements = [f"CREATE SCHEMA {schema}"]
    if layout == "same_table":
        statements.append(f"CREATE TABLE {schema}.events ({_xstore_event_columns(include_payload=True)})")
        statements.extend(_indexes(schema, "events"))
    elif layout == "separate":
        statements.append(f"CREATE TABLE {schema}.events_analytics ({_xstore_event_columns()})")
        statements.append(f"CREATE TABLE {schema}.event_payloads ({_payload_columns()})")
        statements.extend(_indexes(schema, "events_analytics"))
        statements.extend(_indexes(schema, "event_payloads"))
    elif layout == "full_core":
        statements.append(f"CREATE TABLE {schema}.events_full ({_xstore_event_columns(include_payload=True)})")
        statements.append(f"CREATE TABLE {schema}.events_core ({_xstore_event_columns()})")
        statements.extend(_indexes(schema, "events_full"))
        statements.extend(_indexes(schema, "events_core"))
    else:
        statements.append(f"CREATE TABLE {schema}.events_analytics ({_xstore_event_columns(include_asset=True)})")
        statements.extend(_indexes(schema, "events_analytics"))
        states = ",".join(f"'{status}'" for status in ASSET_STATUSES)
        statements.append(f"""CREATE TABLE {schema}.assets (
asset_id TEXT NOT NULL PRIMARY KEY, sha256 TEXT NOT NULL, content_type TEXT NOT NULL,
encoding TEXT NOT NULL, content_length BIGINT NOT NULL, storage_path TEXT NOT NULL,
status TEXT NOT NULL CHECK (status IN ({states})), updated_at TIMESTAMP(6) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
error_category TEXT)""")
    return ";\n".join(statements) + ";"


# 相对 openGauss adapter 的实现偏离，随每个 target 的 layout_definition 落盘。
XSTORE_DEVIATIONS = {
    "framework_nullable": "GaussDB 将空字符串视为 NULL，framework 列去掉 NOT NULL；"
                          "审计时把 NULL 还原为空字符串，逻辑记录不变",
    "keyset_predicate": "行构造器比较不被支持，展开为等价的 OR 形式；该形式能否作为索引范围起点待实测",
    "watermark_aggregate": "FILTER (WHERE ...) 不被支持，改用 count(CASE WHEN ...)",
    "connection": "经本地 Unix domain socket 的 trust 认证连接",
}


class XStoreAdapter(OpenGaussAdapter):
    """XStore (GaussVector) adapter，复用 OpenGauss 的全部 SQL 与布局逻辑。"""

    def __init__(self, host, port, container_name, namespace, layout,
                 input_root: Path, asset_store=None,
                 user=None, dbname=None):
        super().__init__(host, port, container_name, namespace, layout,
                         input_root, asset_store)
        # 连接走本地 Unix domain socket 的 trust 认证，运行账号由环境提供，不入版本控制。
        self._xstore_user = user or os.environ.get("XSTORE_USER", "")
        if not self._xstore_user:
            raise ValueError("XSTORE_USER must be set for the XStore adapter")
        self._xstore_dbname = dbname or os.environ.get("XSTORE_DBNAME", "postgres")
        self._password = ""

    def _password_from_container(self):
        """XStore 经 socket trust 认证连接，不使用口令。"""
        return ""

    def connect_worker(self):
        """建立由调用方负责关闭的 GaussDB 连接。

        XStore 的 gs_hba.conf 对本地 Unix domain socket 使用 trust 认证，
        对 TCP 连接使用 SHA256 认证。通过 /tmp socket 连接避免密码问题。
        """
        socket_dir = os.environ.get("XSTORE_SOCKET_DIR", "/tmp")
        conninfo = (
            f"host={socket_dir} port={self.port} dbname={self._xstore_dbname} "
            f"user={self._xstore_user}"
        )
        return gaussdb_libpq.connect(conninfo)

    def create(self):
        """创建独占 schema，使用 GaussDB 兼容的 DDL。"""
        from opengauss import schema_name
        if self.namespace_exists():
            raise ValueError(f"schema already exists: {self.schema}")
        ddl = _xstore_create_layout_ddls(self.namespace, self.layout)
        connection = self.connect_worker()
        try:
            try:
                for statement in ddl.removesuffix(";").split(";\n"):
                    connection.execute(statement)
                self._owned = True
            except Exception:
                raise
            return {"schema": self.schema, "ddl": ddl, "engine_deviations": XSTORE_DEVIATIONS}
        finally:
            connection.close()

    def _query_statement(self, query):
        """重写查询语句：GaussDB 不支持行构造器 (col1,col2)>(%s,%s)，改用等价标准 SQL。

        行构造器 (start_time,event_id)>(cursor_time,cursor_id) 等价于
        (start_time > cursor_time OR (start_time = cursor_time AND event_id > cursor_id))。
        """
        from opengauss import QuerySpec
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
            ("NULL::text" if query.kind == "list" and field == "preview" else alias + field)
            + " AS " + field
            for field in LOGICAL_FIELDS
        )
        if query.kind in {"list", "preview"}:
            payload = "NULL::text"
        fields = list_fields + "," + payload + " AS payload_value"
        if query.kind in {"list", "preview"}:
            cursor_time = params.get("cursor_time", "1900-01-01T00:00:00.000Z")
            cursor_id = params.get("cursor_id", "")
            if cursor_id:
                # Non-empty cursor: use equivalent of row constructor
                statement = (
                    f"SELECT {fields} FROM {source} WHERE project_id=%s AND start_time>=%s AND start_time<%s "
                    f"AND ({alias}start_time > %s OR ({alias}start_time = %s AND {alias}event_id > %s)) "
                    f"ORDER BY {alias}start_time,{alias}event_id LIMIT %s"
                )
                values = (params["project_id"], params["start_time"], params["end_time"],
                          cursor_time, cursor_time, cursor_id, params.get("page_size", 256))
            else:
                # Empty cursor: skip cursor condition (GaussDB treats empty string as NULL)
                statement = (
                    f"SELECT {fields} FROM {source} WHERE project_id=%s AND start_time>=%s AND start_time<%s "
                    f"ORDER BY {alias}start_time,{alias}event_id LIMIT %s"
                )
                values = (params["project_id"], params["start_time"], params["end_time"],
                          params.get("page_size", 256))
        elif query.kind == "detail":
            statement = (f"SELECT {fields} FROM {source} WHERE {alias}project_id=%s AND {alias}trace_id=%s "
                         f"AND {alias}start_time=%s AND {alias}event_id=%s")
            values = tuple(params[key] for key in ("project_id", "trace_id", "start_time", "event_id"))
        elif query.kind == "trace":
            statement = (f"SELECT {fields} FROM {source} WHERE {alias}project_id=%s AND {alias}trace_id=%s "
                         f"AND {alias}start_time>=%s AND {alias}start_time<%s ORDER BY {alias}start_time,{alias}event_id")
            values = tuple(params[key] for key in ("project_id", "trace_id", "start_time", "end_time"))
        else:
            statement = (
                f"SELECT {fields} FROM {source} WHERE {alias}cohort=%s "
                f"AND {alias}sha256 IS NOT NULL ORDER BY {alias}start_time,{alias}event_id"
            )
            values = (params["cohort"],)
        return statement, values

    def _watermarks(self):
        """覆盖水位查询：GaussDB 不支持 FILTER (WHERE ...) 语法，改用 count(CASE WHEN ...)。"""
        catalog = build_layout_catalog(self.layout)
        connection = self.connect_worker()
        try:
            result = {}
            for table in catalog.write_tables:
                if table == "assets":
                    row = connection.execute(
                        f"SELECT COALESCE(MAX(e.ingest_seq)+1,0),"
                        f"count(CASE WHEN e.asset_id IS NOT NULL AND a.status='available' THEN 1 END),"
                        f"count(CASE WHEN e.asset_id IS NOT NULL AND a.status IS DISTINCT FROM 'available' THEN 1 END) "
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

    def audit_dataset(self):
        """重写审计：GaussDB 将空字符串存为 NULL，审计时需还原为空字符串。

        仅对 target audit 行做 framework 规范化（target audit 使用 EVENT_AUDIT_FIELDS
        包含 framework），主审计行使用 LOGICAL_FIELDS 不含 framework，无需处理。
        """
        audit = super().audit_dataset()
        normalized_targets = {}
        for target, ta in audit.target_audits.items():
            normalized_target_rows = tuple(
                {**row, "framework": row.get("framework") or ""}
                if "framework" in row
                else row
                for row in ta.rows
            )
            normalized_targets[target] = PhysicalTargetAudit(
                normalized_target_rows, ta.duplicate_identities, ta.event_mappings,
            )
        return DatasetAudit(
            audit.rows, audit.duplicate_event_ids,
            audit.logical_response_bytes, audit.database_protocol_bytes,
            normalized_targets,
        )
