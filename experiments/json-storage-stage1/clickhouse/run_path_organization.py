#!/usr/bin/env python3
"""在 ClickHouse 25.12 上运行 JSON 路径组织对照实验。"""

import argparse
import hashlib
import json
import math
import re
import resource
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


BENCHMARK_BASELINE = "9529c8f389673132757f4da9a96878926f22b94f"
EXPORTER_BASELINE = "54ca553a7ed09ad1751c82adab3aa52c6e9357b1"
CURRENT_BENCHMARK_HEAD = "6472d8e1ac6cdb42494b79b28d4d5361919d4776"
CURRENT_EXPORTER_HEAD = "9a49c8a9d6091633112fe793fcf12310859aeb7f"
CURRENT_EXPORTER_SCHEMA_FREEZE = "0c26c9ecf03acf0bd6aa3a3c103ba4e7a78b523a"
EXPECTED_REPO_DIGEST = "clickhouse/clickhouse-server@sha256:8a790dd3468db22b1d4e7b18a176f378ff5ff6053b9c48dd4ea1fa71a24c5ba6"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
LAYOUTS = ("string", "native_limited", "native_hinted")
DYNAMIC_PATH_BUDGETS = {"native_limited": 100, "native_hinted": 1000}
TABLE_SETTINGS = (
    "min_bytes_for_wide_part=0,"
    "min_rows_for_wide_part=0,"
    "object_shared_data_serialization_version='advanced',"
    "object_shared_data_serialization_version_for_zero_level_parts='map_with_buckets'"
)


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
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")
    return completed.stdout


def inspect_database(container_name):
    """读取容器、镜像、端口和服务端版本身份。"""
    raw = run(
        [
            "docker",
            "inspect",
            container_name,
            "--format",
            "{{.Image}}|{{.Config.Image}}|{{.State.Running}}|{{json .NetworkSettings.Ports}}",
        ]
    ).strip()
    image_id, image_name, running, ports_raw = raw.split("|", 3)
    if running != "true":
        raise RuntimeError(f"ClickHouse container is not running: {container_name}")
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
        (item for item in digests if item.startswith("clickhouse/clickhouse-server@")),
        digests[0],
    )
    server_version = run(
        ["docker", "exec", container_name, "clickhouse-client", "--query", "SELECT version()"]
    ).strip()
    return {
        "container": container_name,
        "image": image_name,
        "image_id": resolved_id,
        "published_ports": json.loads(ports_raw),
        "repo_digest": repo_digest,
        "server_version": server_version,
        "size": int(size),
    }


def http_query(host, port, statement):
    """通过本机 HTTP 接口执行 SQL，返回响应、服务端摘要和客户端耗时。"""
    request = urllib.request.Request(
        f"http://{host}:{port}/",
        data=statement.encode("utf-8"),
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
            summary_raw = response.headers.get("X-ClickHouse-Summary")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ClickHouse query failed ({error.code}): {detail}") from error
    return body, (json.loads(summary_raw) if summary_raw else None), time.perf_counter() - started


def verify_input(input_dir):
    """验证生成器产物，并返回 run manifest 与 truth。"""
    manifest = json.loads((input_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "complete" or manifest.get("profile") != "path-organization":
        raise ValueError("input run manifest must be a complete path-organization profile")
    for name, expected in manifest["artifacts"].items():
        path = input_dir / name
        actual = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if actual != expected:
            raise ValueError(f"input artifact mismatch: {name}")
    truth = json.loads((input_dir / "truth-manifest.json").read_text(encoding="utf-8"))
    if truth["record_count"] != manifest["record_count"]:
        raise ValueError("input record count does not match truth manifest")
    return manifest, truth


def create_table_ddls(database_name):
    """返回 String、低预算 JSON 和热点提示 JSON 三种表定义。"""
    column_types = {
        "string": "String CODEC(ZSTD(3))",
        "native_limited": "JSON(max_dynamic_paths=100)",
        "native_hinted": "JSON(max_dynamic_paths=1000, hot.tenant String, hot.region String)",
    }
    return {
        layout: (
            f"CREATE TABLE {database_name}.events_{layout} ("
            f"event_id String, metadata {column_type}) "
            f"ENGINE=MergeTree ORDER BY event_id SETTINGS {TABLE_SETTINGS}"
        )
        for layout, column_type in column_types.items()
    }


def query_spec(layout, query):
    """把公共查询参数映射为 String 解析或 native JSON 子列谓词。"""
    value = query["parameters"]["value"].replace("'", "''")
    if query["query_id"] == "hot_tenant_equals":
        if layout == "string":
            return f"JSONExtractString(metadata, 'hot', 'tenant') = '{value}'"
        return f"getSubcolumn(metadata, 'hot.tenant')::String = '{value}'"
    if query["query_id"] == "cold_path_equals":
        path = query["parameters"]["path"]
        if not re.fullmatch(r"p[0-9]{5}", path):
            raise ValueError(f"unsupported cold path: {path}")
        if layout == "string":
            return f"JSONExtractString(metadata, 'paths', '{path}') = '{value}'"
        return f"getSubcolumn(metadata, 'paths.{path}')::String = '{value}'"
    raise ValueError(f"unsupported query: {query['query_id']}")


def iter_records(dataset_path):
    """逐行读取输入记录。"""
    with dataset_path.open("r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def insert_layout(container_name, database_name, layout, dataset_path, record_count, chunks):
    """分多个 INSERT 块流式载入布局，以便观察 merge 前后的 part。"""
    chunk_size = math.ceil(record_count / chunks)
    started = time.perf_counter()
    inserted = 0
    process = None
    try:
        for record in iter_records(dataset_path):
            if inserted % chunk_size == 0:
                if process is not None:
                    process.stdin.close()
                    stderr = process.stderr.read().decode("utf-8", errors="replace")
                    if process.wait() != 0:
                        raise RuntimeError(f"ClickHouse insert failed: {stderr.strip()}")
                process = subprocess.Popen(
                    [
                        "docker",
                        "exec",
                        "-i",
                        container_name,
                        "clickhouse-client",
                        "--query",
                        f"INSERT INTO {database_name}.events_{layout} FORMAT JSONEachRow",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            metadata = record["metadata"]
            value = metadata if layout != "string" else canonical_bytes(metadata).decode("utf-8")
            process.stdin.write(canonical_bytes({"event_id": record["event_id"], "metadata": value}) + b"\n")
            inserted += 1
    finally:
        if process is not None and process.stdin and not process.stdin.closed:
            process.stdin.close()
    if process is not None:
        stderr = process.stderr.read().decode("utf-8", errors="replace")
        if process.wait() != 0:
            raise RuntimeError(f"ClickHouse insert failed: {stderr.strip()}")
    elapsed = time.perf_counter() - started
    return {
        "input_mib_per_second": dataset_path.stat().st_size / 1048576 / elapsed,
        "rows": inserted,
        "rows_per_second": inserted / elapsed,
        "wall_seconds": elapsed,
    }


def parse_json_each_row(body):
    """解析 ClickHouse JSONEachRow 响应。"""
    return [json.loads(line) for line in body.splitlines() if line]


def collect_parts(host, port, database_name, layout):
    """汇总活动 part 的行数与压缩空间。"""
    statement = (
        "SELECT count() AS part_count, sum(rows) AS rows, "
        "sum(data_compressed_bytes) AS compressed_bytes, "
        "sum(data_uncompressed_bytes) AS uncompressed_bytes "
        "FROM system.parts WHERE active AND "
        f"database='{database_name}' AND table='events_{layout}' FORMAT JSONEachRow"
    )
    body, _, _ = http_query(host, port, statement)
    return parse_json_each_row(body)[0]


def path_inventory_statement(table):
    """构造跨行去重后的 dynamic/shared 路径全集统计 SQL。"""
    return (
        "SELECT length(arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(metadata))))) AS dynamic, "
        "length(arrayDistinct(arrayFlatten(groupArray(JSONSharedDataPaths(metadata))))) AS shared "
        f"FROM {table} FORMAT JSONEachRow"
    )


def collect_paths(host, port, database_name, layout):
    """统计 native JSON 表中 dynamic 与 shared data 的路径全集。"""
    if layout == "string":
        return None
    statement = path_inventory_statement(f"{database_name}.events_{layout}")
    body, _, _ = http_query(host, port, statement)
    return parse_json_each_row(body)[0]


def collect_hot_types(host, port, database_name, layout):
    """读取热点路径的实际 ClickHouse 类型。"""
    if layout == "string":
        return None
    statement = (
        "SELECT toTypeName(getSubcolumn(metadata, 'hot.region')) AS region, "
        "toTypeName(getSubcolumn(metadata, 'hot.tenant')) AS tenant "
        f"FROM {database_name}.events_{layout} LIMIT 1 FORMAT JSONEachRow"
    )
    body, _, _ = http_query(host, port, statement)
    row = parse_json_each_row(body)[0]
    return {"hot.region": row["region"], "hot.tenant": row["tenant"]}


def run_filter_query(host, port, statement, expected_ids, measurements):
    """预热一次并测量过滤查询，校验排序后的 event_id。"""
    http_query(host, port, statement)
    samples = []
    summaries = []
    actual_ids = []
    for _ in range(measurements):
        body, summary, elapsed = http_query(host, port, statement)
        actual_ids = body.splitlines()
        samples.append(elapsed * 1000)
        summaries.append(summary)
    return {
        "actual_row_count": len(actual_ids),
        "expected_row_count": len(expected_ids),
        "latency": {
            "max_ms": max(samples),
            "median_ms": statistics.median(samples),
            "min_ms": min(samples),
            "samples_ms": samples,
        },
        "matches_truth": actual_ids == expected_ids,
        "server_summaries": summaries,
    }


def verify_roundtrip(container_name, database_name, layout, truth):
    """流式回读完整 metadata，并按 event_id 校验 canonical hash。"""
    expected = {row["event_id"]: row["metadata_canonical_sha256"] for row in truth["rows"]}
    metadata_expression = "metadata" if layout == "string" else "toJSONString(metadata)"
    process = subprocess.Popen(
        [
            "docker",
            "exec",
            container_name,
            "clickhouse-client",
            "--query",
            f"SELECT event_id,{metadata_expression} AS metadata_json "
            f"FROM {database_name}.events_{layout} ORDER BY event_id FORMAT JSONEachRow",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    mismatches = []
    checked = 0
    for line in process.stdout:
        row = json.loads(line)
        actual = hashlib.sha256(canonical_bytes(json.loads(row["metadata_json"]))).hexdigest()
        if actual != expected.get(row["event_id"]):
            mismatches.append(row["event_id"])
        checked += 1
    stderr = process.stderr.read()
    if process.wait() != 0:
        raise RuntimeError(f"ClickHouse roundtrip query failed: {stderr.strip()}")
    return {"checked": checked, "hash_mismatches": mismatches}


def execute(args):
    """执行三布局对照并写结果与完成 manifest。"""
    if not IDENTIFIER.fullmatch(args.database_name):
        raise ValueError("database name must match ^[a-z][a-z0-9_]{0,62}$")
    if args.measurements < 5 or args.insert_chunks < 2:
        raise ValueError("measurements must be at least 5 and insert-chunks at least 2")
    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    input_manifest, truth = verify_input(input_dir)
    database = inspect_database(args.container_name)
    if not database["server_version"].startswith("25.12."):
        raise RuntimeError("server version must be ClickHouse 25.12")
    if database["repo_digest"] != EXPECTED_REPO_DIGEST:
        raise RuntimeError("container image digest does not match the fixed baseline")
    bindings = database["published_ports"].get("8123/tcp") or []
    if args.host not in {"127.0.0.1", "localhost", "::1"} or str(args.http_port) not in {
        binding["HostPort"] for binding in bindings
    }:
        raise RuntimeError("host and HTTP port do not match the container's published port")

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.unlink(missing_ok=True)
    database_created = False
    result = None
    manifest = None
    ddls = create_table_ddls(args.database_name)
    try:
        http_query(args.host, args.http_port, f"CREATE DATABASE {args.database_name}")
        database_created = True
        for ddl in ddls.values():
            http_query(args.host, args.http_port, ddl)

        expected_by_query = {
            query["query_id"]: query["expected_event_ids"] for query in truth["queries"]
        }
        layouts = {}
        all_passed = True
        for layout in LAYOUTS:
            table = f"{args.database_name}.events_{layout}"
            load = insert_layout(
                args.container_name,
                args.database_name,
                layout,
                input_dir / "dataset.jsonl",
                truth["record_count"],
                args.insert_chunks,
            )
            parts_before = collect_parts(args.host, args.http_port, args.database_name, layout)
            paths_before = collect_paths(args.host, args.http_port, args.database_name, layout)
            merge_started = time.perf_counter()
            http_query(args.host, args.http_port, f"OPTIMIZE TABLE {table} FINAL")
            merge_seconds = time.perf_counter() - merge_started
            parts_after = collect_parts(args.host, args.http_port, args.database_name, layout)
            paths_after = collect_paths(args.host, args.http_port, args.database_name, layout)
            hot_types = collect_hot_types(args.host, args.http_port, args.database_name, layout)

            queries = []
            plans = {}
            for query in truth["queries"]:
                predicate = query_spec(layout, query)
                statement = f"SELECT event_id FROM {table} WHERE {predicate} ORDER BY event_id FORMAT TSVRaw"
                measured = run_filter_query(
                    args.host,
                    args.http_port,
                    statement,
                    expected_by_query[query["query_id"]],
                    args.measurements,
                )
                measured.update(
                    {
                        "predicate_family": "string_parse" if layout == "string" else "native_subcolumn",
                        "query_id": query["query_id"],
                        "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
                    }
                )
                queries.append(measured)
                plan_body, _, _ = http_query(
                    args.host,
                    args.http_port,
                    f"EXPLAIN PIPELINE {statement.rsplit(' FORMAT ', 1)[0]}",
                )
                plans[query["query_id"]] = plan_body

            full_expression = "metadata" if layout == "string" else "toJSONString(metadata)"
            full_statement = f"SELECT sum(length({full_expression})) FROM {table} FORMAT TSVRaw"
            _, full_summary, full_elapsed = http_query(args.host, args.http_port, full_statement)
            roundtrip = verify_roundtrip(args.container_name, args.database_name, layout, truth)
            layout_passed = (
                load["rows"] == truth["record_count"]
                and parts_before["part_count"] >= 2
                and parts_after["part_count"] == 1
                and all(query["matches_truth"] for query in queries)
                and roundtrip["checked"] == truth["record_count"]
                and not roundtrip["hash_mismatches"]
            )
            all_passed = all_passed and layout_passed
            layouts[layout] = {
                "full_object_read": {
                    "client_elapsed_ms": full_elapsed * 1000,
                    "server_summary": full_summary,
                },
                "hot_path_types": hot_types,
                "load": load,
                "merge_wall_seconds": merge_seconds,
                "parts_after_merge": parts_after,
                "parts_before_merge": parts_before,
                "paths_after_merge": paths_after,
                "paths_before_merge": paths_before,
                "plans": plans,
                "queries": queries,
                "roundtrip": roundtrip,
            }

        limited = layouts["native_limited"]["paths_after_merge"]
        if truth["path_count"] > DYNAMIC_PATH_BUDGETS["native_limited"]:
            all_passed = all_passed and limited["shared"] > 0
        if truth["path_count"] <= DYNAMIC_PATH_BUDGETS["native_hinted"]:
            all_passed = all_passed and layouts["native_hinted"]["paths_after_merge"]["shared"] == 0
        all_passed = all_passed and layouts["native_hinted"]["hot_path_types"] == {
            "hot.region": "String",
            "hot.tenant": "String",
        }
        result = {
            "database": database,
            "layouts": layouts,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "roundtrip_scope": "metadata",
            "status": "pass" if all_passed else "fail",
            "storage": {"engine": "MergeTree", "json_type": "JSON"},
        }
        ddl_text = ";\n".join(ddls.values()) + ";"
        manifest = {
            "baselines": {"benchmark": BENCHMARK_BASELINE, "exporter": EXPORTER_BASELINE},
            "current_project_versions": {
                "benchmark_head": CURRENT_BENCHMARK_HEAD,
                "exporter_head": CURRENT_EXPORTER_HEAD,
                "exporter_schema_freeze": CURRENT_EXPORTER_SCHEMA_FREEZE,
                "paired_main_baseline": None,
            },
            "data_path": "independent_loader",
            "database": database,
            "ddl_sha256": hashlib.sha256(ddl_text.encode("utf-8")).hexdigest(),
            "dynamic_path_budgets": DYNAMIC_PATH_BUDGETS,
            "input": {
                "dataset_sha256": input_manifest["artifacts"]["dataset.jsonl"]["sha256"],
                "density_percent": input_manifest["density_percent"],
                "path_count": input_manifest["path_count"],
                "record_count": input_manifest["record_count"],
                "truth_sha256": input_manifest["artifacts"]["truth-manifest.json"]["sha256"],
            },
            "insert_chunks": args.insert_chunks,
            "measurements": args.measurements,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "shared_data_serialization": {"merged_parts": "advanced", "zero_level_parts": "map_with_buckets"},
            "status": "complete" if all_passed else "failed",
        }
    finally:
        if database_created:
            http_query(args.host, args.http_port, f"DROP DATABASE {args.database_name}")

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
        raise RuntimeError("ClickHouse path organization probe did not pass all gates")


def parse_args():
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="路径 profile run 目录")
    parser.add_argument("--output", required=True, type=Path, help="数据库实验结果目录")
    parser.add_argument("--container-name", required=True, help="已运行的 ClickHouse 容器名")
    parser.add_argument("--database-name", required=True, help="本次使用的唯一临时数据库")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--http-port", default=18123, type=int, help="HTTP 监听端口")
    parser.add_argument("--measurements", default=5, type=int, help="每个查询的正式测量次数")
    parser.add_argument("--insert-chunks", default=4, type=int, help="每张表的 INSERT 块数")
    return parser.parse_args()


def main():
    """执行 CLI，并把失败原因写入标准错误。"""
    try:
        execute(parse_args())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
