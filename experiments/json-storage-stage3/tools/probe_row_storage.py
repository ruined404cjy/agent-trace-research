"""阶段三行存载荷列与游标形式探针。

主矩阵在每轮结束时清理 namespace，以下三项事实只能在数据仍在库内时采集，本工具补齐它们：
按 profile 分组的载荷列逻辑字节与存储字节、schema 内全部关系的 reltoastrelid 与 reloptions、
same_table 中间页两种游标写法的 EXPLAIN (ANALYZE, BUFFERS)。每个布局按主矩阵相同的
block 与水位载入 main workload，使用独立 namespace，无论成败都执行清理。
结果写入一个 JSON 文件，不产生性能数据，不进入矩阵汇总。

出错时怎么办：
1. 输出 JSON 的 status 为 failed 时，error 字段给出首个失败原因；已完成布局的记录仍在 layouts 中。
2. 某条统计 SQL 在 XStore 上不被支持时，可在本地改写 payload_profile_sql 或 RELATIONS_SQL，
   保持返回列的含义与顺序不变（profile、行数、逻辑字节、存储字节；relname、relkind、
   reltoastrelid、reloptions），并提交到本地分支。
3. 游标探针报 expanded cursor 不匹配时，说明 adapter 的中间页语句形态已变化，按新语句
   调整 EXPANDED_CURSOR 常量，两种写法的差别仍只是一条 start_time >= cursor_time 下界。
4. 清理未确认删除时，手工删除输出中记录的 namespace 后删掉该 JSON 重跑。
"""

import argparse
import sys
import uuid
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
RUNNER_DIR = STAGE_DIR / "runner"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import production
import run_layout_matrix
from opengauss import explain_sql


FORMAT = "agent-trace-json-storage-stage3-row-storage-probe"
FORMAT_VERSION = 1
PROBE_LAYOUTS = ("same_table", "separate", "full_core")
# 各布局中保存库内载荷的表；asset_ref 的载荷在数据库外，不在本探针范围内。
PAYLOAD_TABLES = {"same_table": "events", "separate": "event_payloads", "full_core": "events_full"}
# main workload 的载荷契约：四个 profile 各 40 个对象，由 validate_workload_contract 强制。
MAIN_PROFILE_ROWS = {"entropy_512k": 40, "text_2m": 40, "text_512k": 40, "text_64k": 40}
EXPANDED_CURSOR = "AND (start_time > %s OR (start_time = %s AND event_id > %s))"
RELATIONS_SQL = (
    "SELECT c.relname, c.relkind, c.reltoastrelid::bigint, c.reloptions "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = %s ORDER BY c.relname"
)


def payload_profile_sql(schema, table):
    """返回按 profile 统计载荷行数、逻辑字节与实际存储字节的查询。"""
    return (
        "SELECT profile, count(*), sum(octet_length(payload)), sum(pg_column_size(payload)) "
        f"FROM {schema}.{table} WHERE payload IS NOT NULL GROUP BY profile ORDER BY profile"
    )


def cursor_forms(statement, values):
    """由 adapter 的中间页语句派生契约第 5.1 节的两种游标写法。

    入参为 adapter 生成的展开式语句与绑定值；返回 {形式名: (语句, 绑定值)}。
    形式二在展开式之前增加一条被 OR 条件蕴含的下界 start_time >= cursor_time，结果集不变。
    """
    if statement.count(EXPANDED_CURSOR) != 1:
        raise ValueError("statement does not contain exactly one expanded cursor predicate")
    values = tuple(values)
    # 展开式绑定依次为 project_id、start_time、end_time、cursor_time、cursor_time、cursor_id、page_size。
    cursor_time = values[3]
    bounded = statement.replace(EXPANDED_CURSOR, "AND start_time >= %s " + EXPANDED_CURSOR)
    return {
        "expanded": (statement, values),
        "expanded_with_lower_bound": (bounded, values[:3] + (cursor_time,) + values[3:]),
    }


def _payload_profiles(connection, schema, table):
    """读取载荷列统计并核对 main workload 的 profile 契约。"""
    rows = connection.execute(payload_profile_sql(schema, table)).fetchall()
    profiles = [
        {"profile": row[0], "rows": int(row[1]), "logical_bytes": int(row[2]),
         "stored_bytes": int(row[3])}
        for row in rows
    ]
    observed = {item["profile"]: item["rows"] for item in profiles}
    if observed != MAIN_PROFILE_ROWS:
        raise ValueError(f"payload profile rows differ from the main workload: {observed}")
    return profiles


def _cursor_probe(adapter, connection, engine, middle_query):
    """对中间页两种游标写法各执行一次 EXPLAIN；行构造器引擎记为不适用。"""
    if engine != "xstore":
        return {"status": "not-applicable",
                "reason": "adapter uses the row constructor cursor predicate"}
    statement, values = adapter._query_statement(middle_query)
    probe = {}
    for name, (form, bindings) in cursor_forms(statement, values).items():
        rows = connection.execute(explain_sql(form), bindings).fetchall()
        probe[name] = {"statement": form, "values": list(bindings),
                       "plan": "\n".join(row[0] for row in rows)}
    return probe


def probe_layout(adapter, engine, blocks, middle_query, ready_timeout):
    """载入一个布局的 main workload，在清理前采集探针事实并返回记录。

    入参 blocks 为按正式水位切分的 main workload block，middle_query 为 list:middle 查询。
    任一步失败时仍执行清理，并把原异常抛给调用方；清理未确认删除时抛出 RuntimeError。
    """
    record = {"layout": adapter.layout, "namespace": adapter.namespace}
    try:
        adapter.create()
        for block in blocks:
            result = adapter.ingest_block(list(block))
            if not adapter.wait_write_complete(result.watermark).completed:
                raise RuntimeError(f"joint watermark incomplete at {result.watermark}")
        if not adapter.wait_query_ready(ready_timeout).completed:
            raise RuntimeError("query readiness incomplete")
        connection = adapter.connect_worker()
        try:
            record["payload_profiles"] = _payload_profiles(
                connection, adapter.schema, PAYLOAD_TABLES[adapter.layout],
            )
            record["relations"] = [
                {"relname": row[0], "relkind": row[1], "reltoastrelid": int(row[2]),
                 "reloptions": row[3]}
                for row in connection.execute(RELATIONS_SQL, (adapter.schema,)).fetchall()
            ]
            if adapter.layout == "same_table":
                record["cursor_probe"] = _cursor_probe(adapter, connection, engine, middle_query)
        finally:
            connection.close()
        record["storage"] = run_layout_matrix._as_json(adapter.collect_storage().tables)
    finally:
        cleanup = adapter.cleanup()
        record["cleanup"] = {"namespace": cleanup.namespace, "removed": cleanup.removed}
    if cleanup.removed is not True:
        raise RuntimeError(f"probe namespace was not removed: {cleanup.namespace}")
    return record


def _code_identity(adapter):
    """记录探针、工厂、载入路径与 adapter 继承链的源文件身份。"""
    modules = {"probe": sys.modules[__name__], "production": production,
               "layout_runner": run_layout_matrix}
    for index, cls in enumerate(type(adapter).__mro__[:-1]):
        modules["adapter" if index == 0 else f"adapter_base_{index}"] = sys.modules[cls.__module__]
    return {
        role: {"path": str(Path(module.__file__).resolve()),
               **run_layout_matrix._file_identity(Path(module.__file__).resolve())}
        for role, module in modules.items()
    }


def build_parser():
    """构造探针命令行：输入、输出、引擎与布局范围。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="frozen formal input root")
    parser.add_argument("--output", type=Path, required=True, help="probe JSON path")
    parser.add_argument("--engine", choices=("xstore", "opengauss"), required=True)
    parser.add_argument("--layouts", default=",".join(PROBE_LAYOUTS))
    parser.add_argument("--ready-timeout", type=int, default=300)
    return parser


def main(argv=None):
    """执行探针并原子写出 JSON；任一布局失败时写出 failed 记录并返回 1。"""
    arguments = build_parser().parse_args(argv)
    output = arguments.output.resolve()
    if output.exists():
        raise FileExistsError(f"probe output already exists: {output}")
    layouts = run_layout_matrix._parse_csv(arguments.layouts, PROBE_LAYOUTS, "layouts")
    formal = production.load_formal_input(arguments.input)
    middle_query = next(query for query, truth in formal.main_queries if truth.scenario == "list:middle")
    endpoints = production.EngineEndpoints()
    document = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "status": "running",
        "engine": arguments.engine, "command": list(sys.argv if argv is None else argv),
        "input": production._thaw(formal.identity), "layouts": [],
    }
    try:
        for layout in layouts:
            namespace = f"jsons3_probe_{layout}_{uuid.uuid4().hex[:10]}"
            adapter = production.create_adapter(arguments.engine, layout, namespace, formal, None, endpoints)
            if "engine_runtime" not in document:
                document["engine_runtime"] = run_layout_matrix._engine_runtime(adapter, arguments.engine)
                document["container"] = run_layout_matrix._container_evidence(
                    adapter.container_name, arguments.engine,
                )
                document["host"] = run_layout_matrix._host_evidence()
                document["code"] = _code_identity(adapter)
            document["layouts"].append(probe_layout(
                adapter, arguments.engine, formal.main_blocks, middle_query, arguments.ready_timeout,
            ))
        document["status"] = "complete"
    except Exception as error:
        document["status"] = "failed"
        document["error"] = f"{type(error).__name__}: {error}"
    run_layout_matrix.write_manifest_atomic(output, document)
    if document["status"] != "complete":
        print(document["error"], file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
