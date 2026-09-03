#!/usr/bin/env python3
"""在 openGauss 6.0.0 上运行 JSONB 路径组织实验。"""

import argparse
import hashlib
import json
import re
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

import psycopg


BENCHMARK_BASELINE = "9529c8f389673132757f4da9a96878926f22b94f"
EXPORTER_BASELINE = "54ca553a7ed09ad1751c82adab3aa52c6e9357b1"
EXPECTED_REPO_DIGEST = "enmotech/opengauss@sha256:9bd81380273944e5a02a2139c90954d4f46813b71810f7b23fe8f738014d03b5"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
LAYOUTS = ("no_index", "gin", "hot_expression")
TABLE_DDL = """CREATE TABLE {table} (
    event_id    VARCHAR(32) NOT NULL,
    trace_id    VARCHAR(32) NOT NULL,
    span_id     VARCHAR(16) NOT NULL,
    parent_span_id VARCHAR(16),
    start_time  TIMESTAMP(6) NOT NULL,
    end_time    TIMESTAMP(6) NOT NULL,
    service_name VARCHAR(64) NOT NULL,
    type        VARCHAR(16) NOT NULL,
    model       VARCHAR(64) NOT NULL,
    level       VARCHAR(16) NOT NULL,
    metadata    JSONB NOT NULL
)"""


def canonical_bytes(value):
    """返回与公共生成器一致的 canonical JSON bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path):
    """流式计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command):
    """执行外部命令并在失败时保留诊断。"""
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")
    return completed.stdout


def inspect_database(container_name):
    """读取容器、镜像和服务端版本身份。"""
    raw = run(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{.Image}}|{{.Config.Image}}|{{.State.Running}}",
        ]
    ).strip()
    image_id, image_name, running = raw.split("|", 2)
    if running != "true":
        raise RuntimeError(f"openGauss container is not running: {container_name}")
    image_raw = run(
        [
            "docker",
            "image",
            "inspect",
            image_id,
            "--format",
            "{{json .RepoDigests}}|{{.Id}}|{{.Size}}",
        ]
    ).strip()
    digests_raw, resolved_id, size = image_raw.split("|", 2)
    digests = json.loads(digests_raw)
    repo_digest = next(
        (item for item in digests if item.startswith("enmotech/opengauss@")),
        digests[0],
    )
    server_version = run_gsql(container_name, "SELECT version();").strip()
    published = json.loads(
        run(
            [
                "docker",
                "inspect",
                container_name,
                "--format",
                "{{json .NetworkSettings.Ports}}",
            ]
        ).strip()
    )
    return {
        "container": container_name,
        "image": image_name,
        "image_id": resolved_id,
        "repo_digest": repo_digest,
        "server_version": server_version,
        "size": int(size),
        "published_ports": published,
    }


def run_gsql(container_name, sql):
    """使用容器登录环境执行只读身份 SQL。"""
    return run(
        [
            "docker",
            "exec",
            "-i",
            "-u",
            "omm",
            container_name,
            "bash",
            "-lc",
            'exec "$0" "$@"',
            "/usr/local/opengauss/bin/gsql",
            "-X",
            "-d",
            "postgres",
            "-Atc",
            sql,
        ]
    )


def container_password(container_name):
    """从现有容器环境读取数据库密码，不把密码写入产物。"""
    environment = run(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ]
    )
    for line in environment.splitlines():
        if line.startswith("GS_PASSWORD="):
            return line.split("=", 1)[1]
    raise RuntimeError(f"GS_PASSWORD is not configured on container: {container_name}")


def verify_input(input_dir):
    """验证生成器产物，并返回 run manifest 与 truth。"""
    manifest = json.loads((input_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "complete":
        raise ValueError("input run manifest is not complete")
    if manifest.get("profile") != "path-organization":
        raise ValueError("input profile must be path-organization")
    for name, expected in manifest["artifacts"].items():
        path = input_dir / name
        actual = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if actual != expected:
            raise ValueError(f"input artifact mismatch: {name}")
    truth = json.loads((input_dir / "truth-manifest.json").read_text(encoding="utf-8"))
    if truth["record_count"] != manifest["record_count"]:
        raise ValueError("input record count does not match truth manifest")
    return manifest, truth


def iter_records(dataset_path):
    """逐行读取 canonical JSONL，限制正式 profile 的峰值内存。"""
    with dataset_path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def create_layouts(connection, schema_name):
    """创建无索引、GIN 和热点表达式索引三种 JSONB 布局。"""
    statements = []
    for layout in LAYOUTS:
        table = f"{schema_name}.events_{layout}"
        statements.append(TABLE_DDL.format(table=table))
    statements.append(
        f"CREATE INDEX events_gin_metadata_idx "
        f"ON {schema_name}.events_gin USING gin(metadata jsonb_ops)"
    )
    statements.append(
        f"CREATE INDEX events_hot_tenant_idx ON {schema_name}.events_hot_expression "
        "(jsonb_object_field_text(jsonb_object_field(metadata, 'hot'), 'tenant'))"
    )
    for statement in statements:
        connection.execute(statement)
    return ";\n".join(statements) + ";"


def copy_layout(connection, table, dataset_path, input_bytes):
    """通过 COPY 流式载入一个布局，返回载入耗时和行数。"""
    started = time.perf_counter()
    count = 0
    with connection.cursor().copy(
        f"COPY {table}(event_id,trace_id,span_id,parent_span_id,start_time,end_time,"
        "service_name,type,model,level,metadata) FROM STDIN"
    ) as copy:
        for record in iter_records(dataset_path):
            copy.write_row(
                (
                    record["event_id"],
                    record["trace_id"],
                    record["span_id"],
                    record["parent_span_id"],
                    record["start_time"],
                    record["end_time"],
                    record["service_name"],
                    record["type"],
                    record["model"],
                    record["level"],
                    canonical_bytes(record["metadata"]).decode("utf-8"),
                )
            )
            count += 1
    wall_seconds = time.perf_counter() - started
    return {
        "input_mib_per_second": input_bytes / 1048576 / wall_seconds,
        "rows": count,
        "rows_per_second": count / wall_seconds,
        "wall_seconds": wall_seconds,
    }


def query_spec(layout, query):
    """把公共查询参数映射为目标布局可用的等价 JSONB 谓词。"""
    parameters = query["parameters"]
    if query["query_id"] == "hot_tenant_equals":
        contained = {"hot": {"tenant": parameters["value"]}}
    else:
        contained = {"paths": {parameters["path"]: parameters["value"]}}

    if layout in {"no_index", "gin"} or query["query_id"] == "cold_path_equals":
        return (
            "containment",
            "metadata @> %s::jsonb",
            (canonical_bytes(contained).decode("utf-8"),),
        )
    if query["query_id"] == "hot_tenant_equals":
        return (
            "expression",
            "jsonb_object_field_text(jsonb_object_field(metadata, 'hot'), 'tenant') = %s",
            (parameters["value"],),
        )
    raise ValueError(f"unsupported query: {query['query_id']}")


def explain(connection, statement, parameters, *, force_index):
    """返回自然计划或禁用顺序扫描后的索引能力计划。"""
    if force_index:
        connection.execute("SET enable_seqscan = off")
    try:
        rows = connection.execute(
            f"EXPLAIN {statement}",
            parameters,
        ).fetchall()
        return "\n".join(row[0] for row in rows)
    finally:
        if force_index:
            connection.execute("SET enable_seqscan = on")


def run_query(connection, statement, parameters, measurements):
    """预热一次并测量查询，返回排序后的 event_id 与延迟统计。"""
    connection.execute(statement, parameters).fetchall()
    samples = []
    event_ids = []
    for _ in range(measurements):
        started = time.perf_counter()
        event_ids = sorted(
            row[0] for row in connection.execute(statement, parameters).fetchall()
        )
        samples.append((time.perf_counter() - started) * 1000)
    return event_ids, {
        "max_ms": max(samples),
        "median_ms": statistics.median(samples),
        "min_ms": min(samples),
        "samples_ms": samples,
    }


def verify_roundtrip(connection, table, truth):
    """回读完整逻辑记录并按 event_id 校验 canonical hash。"""
    expected = {
        row["event_id"]: row["canonical_sha256"]
        for row in truth["rows"]
    }
    mismatches = []
    checked = 0
    with connection.transaction():
        with connection.cursor(name=f"roundtrip_{table.rsplit('.', 1)[1]}") as cursor:
            cursor.execute(
                f"SELECT event_id,trace_id,span_id,parent_span_id,start_time,end_time,"
                f"service_name,type,model,level,metadata::text FROM {table} ORDER BY event_id"
            )
            for row in cursor:
                checked += 1
                event_id = row[0]
                record = {
                    "end_time": row[5].isoformat(timespec="milliseconds") + "Z",
                    "event_id": event_id,
                    "level": row[9],
                    "metadata": json.loads(row[10]),
                    "model": row[8],
                    "parent_span_id": row[3],
                    "service_name": row[6],
                    "span_id": row[2],
                    "start_time": row[4].isoformat(timespec="milliseconds") + "Z",
                    "trace_id": row[1],
                    "type": row[7],
                }
                actual = hashlib.sha256(canonical_bytes(record)).hexdigest()
                if actual != expected[event_id]:
                    mismatches.append(event_id)
    return {"checked": checked, "hash_mismatches": sorted(mismatches)}


def collect_space(connection, table):
    """读取表、索引和总关系大小。"""
    row = connection.execute(
        "SELECT pg_relation_size(%s::regclass), pg_indexes_size(%s::regclass), "
        "pg_total_relation_size(%s::regclass)",
        (table, table, table),
    ).fetchone()
    return {"heap_bytes": row[0], "index_bytes": row[1], "total_bytes": row[2]}


def execute(args):
    """执行三布局探针并写结果与完成 manifest。"""
    if not IDENTIFIER.fullmatch(args.schema_name):
        raise ValueError("schema name must match ^[a-z][a-z0-9_]{0,62}$")
    if args.measurements < 5:
        raise ValueError("measurements must be at least 5")

    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    input_manifest, truth = verify_input(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.unlink(missing_ok=True)
    database = inspect_database(args.container_name)
    if "openGauss 6.0.0" not in database["server_version"]:
        raise RuntimeError("server version must be openGauss 6.0.0")
    if database["repo_digest"] != EXPECTED_REPO_DIGEST:
        raise RuntimeError("container image digest does not match the fixed baseline")
    published_bindings = database["published_ports"].get("5432/tcp") or []
    if args.host not in {"127.0.0.1", "localhost", "::1"} or str(args.port) not in {
        binding["HostPort"] for binding in published_bindings
    }:
        raise RuntimeError("host and port do not match the container's published database port")
    password = container_password(args.container_name)
    connection = psycopg.connect(
        host=args.host,
        port=args.port,
        dbname="postgres",
        user="gaussdb",
        password=password,
        autocommit=True,
    )
    schema_created = False
    result = None
    manifest = None
    try:
        connected_version = connection.execute("SELECT version()").fetchone()[0]
        if connected_version != database["server_version"]:
            raise RuntimeError("connected server version does not match the inspected container")
        exists = connection.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname=%s",
            (args.schema_name,),
        ).fetchone()[0]
        if exists:
            raise ValueError(f"schema already exists: {args.schema_name}")
        connection.execute(f"CREATE SCHEMA {args.schema_name}")
        schema_created = True
        ddl = create_layouts(connection, args.schema_name)

        layouts = {}
        all_passed = True
        expected_by_query = {
            query["query_id"]: query["expected_event_ids"]
            for query in truth["queries"]
        }
        dataset_path = input_dir / "dataset.jsonl"
        input_bytes = input_manifest["artifacts"]["dataset.jsonl"]["bytes"]
        for layout in LAYOUTS:
            table = f"{args.schema_name}.events_{layout}"
            load = copy_layout(connection, table, dataset_path, input_bytes)
            connection.execute(f"ANALYZE {table}")
            queries = []
            plans = {}
            for query in truth["queries"]:
                predicate_family, predicate, parameters = query_spec(layout, query)
                statement = f"SELECT event_id FROM {table} WHERE {predicate} ORDER BY event_id"
                natural = explain(
                    connection,
                    statement,
                    parameters,
                    force_index=False,
                )
                forced = explain(
                    connection,
                    statement,
                    parameters,
                    force_index=True,
                )
                actual_ids, latency = run_query(
                    connection,
                    statement,
                    parameters,
                    args.measurements,
                )
                expected_ids = expected_by_query[query["query_id"]]
                matches = actual_ids == expected_ids
                all_passed = all_passed and matches
                queries.append(
                    {
                        "actual_row_count": len(actual_ids),
                        "expected_row_count": len(expected_ids),
                        "latency": latency,
                        "matches_truth": matches,
                        "predicate_family": predicate_family,
                        "query_id": query["query_id"],
                        "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
                    }
                )
                plans[query["query_id"]] = {"forced": forced, "natural": natural}

            roundtrip = verify_roundtrip(connection, table, truth)
            all_passed = all_passed and (
                load["rows"] == truth["record_count"]
                and roundtrip["checked"] == truth["record_count"]
                and not roundtrip["hash_mismatches"]
            )
            layouts[layout] = {
                "load": load,
                "plans": plans,
                "queries": queries,
                "roundtrip": roundtrip,
                "space": collect_space(connection, table),
            }

        index_gate = (
            "events_gin_metadata_idx"
            in layouts["gin"]["plans"]["cold_path_equals"]["forced"]
            and "events_hot_tenant_idx"
            in layouts["hot_expression"]["plans"]["hot_tenant_equals"]["forced"]
        )
        all_passed = all_passed and index_gate
        result = {
            "database": database,
            "index_capability_gate": index_gate,
            "layouts": layouts,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "roundtrip_scope": "canonical_record",
            "status": "pass" if all_passed else "fail",
            "storage": {"json_type": "JSONB", "profile": "row"},
        }
        manifest = {
            "baselines": {
                "benchmark": BENCHMARK_BASELINE,
                "exporter": EXPORTER_BASELINE,
            },
            "data_path": "independent_loader",
            "database": database,
            "ddl_scope": "three_jsonb_row_tables_with_gin_or_hot_expression_index",
            "ddl_sha256": hashlib.sha256(ddl.encode("utf-8")).hexdigest(),
            "input": {
                "dataset_sha256": input_manifest["artifacts"]["dataset.jsonl"]["sha256"],
                "density_percent": input_manifest["density_percent"],
                "path_count": input_manifest["path_count"],
                "record_count": input_manifest["record_count"],
                "truth_sha256": input_manifest["artifacts"]["truth-manifest.json"]["sha256"],
            },
            "measurements": args.measurements,
            "connection": {
                "dbname": "postgres",
                "host": args.host,
                "port": args.port,
                "published_container_port": 5432,
                "server_version": connected_version,
                "user": "gaussdb",
            },
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "status": "complete" if all_passed else "failed",
        }
    finally:
        try:
            if schema_created:
                connection.execute(f"DROP SCHEMA {args.schema_name} CASCADE")
        finally:
            connection.close()

    result_bytes = canonical_bytes(result) + b"\n"
    (output_dir / "result.json").write_bytes(result_bytes)
    manifest["artifacts"] = {
        "result.json": {
            "bytes": len(result_bytes),
            "sha256": hashlib.sha256(result_bytes).hexdigest(),
        }
    }
    manifest_path.write_bytes(canonical_bytes(manifest) + b"\n")
    if result["status"] != "pass":
        raise RuntimeError("openGauss path organization probe did not pass all gates")


def parse_args():
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="路径 profile run 目录")
    parser.add_argument("--output", required=True, type=Path, help="数据库实验结果目录")
    parser.add_argument("--container-name", required=True, help="已运行的 openGauss 容器名")
    parser.add_argument("--schema-name", required=True, help="本次使用的唯一临时 schema")
    parser.add_argument("--host", default="127.0.0.1", help="数据库监听地址")
    parser.add_argument("--port", default=15432, type=int, help="数据库监听端口")
    parser.add_argument("--measurements", default=5, type=int, help="每个查询的正式测量次数")
    return parser.parse_args()


def main():
    """执行 CLI，并把失败原因写入标准错误。"""
    try:
        execute(parse_args())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, psycopg.Error) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
