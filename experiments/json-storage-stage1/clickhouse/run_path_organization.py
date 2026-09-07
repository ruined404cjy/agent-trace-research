#!/usr/bin/env python3
"""在 ClickHouse 25.12 上运行 JSON 路径组织对照实验。"""

import argparse
from collections import Counter
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
import urllib.parse
import urllib.request
import uuid
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
HINTED_PATHS = {
    "native_limited": set(),
    "native_hinted": {"hot.region", "hot.tenant"},
}
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


def http_query(host, port, statement, *, query_id=None):
    """通过本机 HTTP 接口执行 SQL，返回响应、服务端摘要和客户端耗时。"""
    query_string = urllib.parse.urlencode({"query_id": query_id}) if query_id else ""
    url = f"http://{host}:{port}/" + (f"?{query_string}" if query_string else "")
    request = urllib.request.Request(
        url,
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
    column_definitions = {
        "string": "metadata String CODEC(ZSTD(3))",
        "native_limited": (
            "metadata JSON(max_dynamic_paths=100), "
            "metadata_raw String CODEC(ZSTD(3))"
        ),
        "native_hinted": (
            "metadata JSON(max_dynamic_paths=1000, hot.tenant String, hot.region String), "
            "metadata_raw String CODEC(ZSTD(3))"
        ),
    }
    return {
        layout: (
            f"CREATE TABLE {database_name}.events_{layout} ("
            f"event_id String, start_time DateTime64(3, 'UTC'), {column_definition}) "
            f"ENGINE=MergeTree ORDER BY (start_time,event_id) SETTINGS {TABLE_SETTINGS}"
        )
        for layout, column_definition in column_definitions.items()
    }


def query_spec(layout, query):
    """把公共查询参数映射为 String 解析或 native JSON 子列谓词。"""
    value = query["parameters"]["value"].replace("'", "''")
    if query["query_id"] == "hot_tenant_equals":
        if layout == "string":
            return f"JSONExtractString(metadata, 'hot', 'tenant') = '{value}'"
        if layout == "native_hinted":
            return f"metadata.hot.tenant = '{value}'"
        return f"metadata.hot.tenant.:String = '{value}'"
    if query["query_id"] == "cold_path_equals":
        path = query["parameters"]["path"]
        if not re.fullmatch(r"p[0-9]{5}", path):
            raise ValueError(f"unsupported cold path: {path}")
        if layout == "string":
            return f"JSONExtractString(metadata, 'paths', '{path}') = '{value}'"
        return f"metadata.paths.{path}.:String = '{value}'"
    raise ValueError(f"unsupported query: {query['query_id']}")


def parse_layout_order(value):
    """解析跨轮布局顺序，并要求三个布局各出现一次。"""
    layouts = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(layouts) != len(LAYOUTS) or set(layouts) != set(LAYOUTS):
        raise ValueError(f"layout-order must contain each of {LAYOUTS} exactly once")
    return layouts


def timestamp_literal(value):
    """把生成器 UTC 时间转换为固定精度的 ClickHouse 时间字面量。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value):
        raise ValueError(f"unsupported timestamp: {value}")
    return value.replace("T", " ").removesuffix("Z")


def time_predicate(window):
    """构造左闭右开的 DateTime64 时间范围谓词。"""
    start = timestamp_literal(window["start_inclusive"])
    end = timestamp_literal(window["end_exclusive"])
    return (
        f"start_time >= toDateTime64('{start}', 3, 'UTC') AND "
        f"start_time < toDateTime64('{end}', 3, 'UTC')"
    )


def region_expression(layout):
    """返回 String 或 native JSON 布局的 region 分组表达式。"""
    if layout == "string":
        return "JSONExtractString(metadata, 'hot', 'region')"
    if layout == "native_hinted":
        return "metadata.hot.region"
    return "metadata.hot.region.:String"


def correctness_statement(layout, table, query, window):
    """构造一次性 ID truth 校验 SQL。"""
    predicate = query_spec(layout, query)
    return (
        f"SELECT event_id FROM {table} WHERE {time_predicate(window)} AND {predicate} "
        "ORDER BY event_id FORMAT TSVRaw"
    )


def performance_statement(layout, table, query, window):
    """构造时间范围内的小结果分组计数 SQL。"""
    predicate = query_spec(layout, query)
    region = region_expression(layout)
    return (
        f"SELECT {region} AS group_key, count() AS rows FROM {table} "
        f"WHERE {time_predicate(window)} AND {predicate} "
        "GROUP BY group_key ORDER BY group_key FORMAT JSONEachRow"
    )


def full_object_statement(layout, table, window, source):
    """构造强制读取 JSON 内容的时间范围 hash 聚合。"""
    if source == "storage":
        expression = "metadata" if layout == "string" else "toJSONString(metadata)"
    elif source == "fidelity" and layout != "string":
        expression = "metadata_raw"
    else:
        raise ValueError(f"unsupported full object source: {layout}/{source}")
    return (
        f"SELECT sum(cityHash64({expression})) AS content_digest FROM {table} "
        f"WHERE {time_predicate(window)} FORMAT TSVRaw"
    )


def query_log_statement(query_id):
    """构造已完成查询的服务端资源指标读取 SQL。"""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", query_id):
        raise ValueError("query id contains unsupported characters")
    return (
        "SELECT query_duration_ms, read_rows, read_bytes, memory_usage, result_rows, result_bytes, "
        "ProfileEvents['SelectedRows'] AS selected_rows, "
        "ProfileEvents['SelectedBytes'] AS selected_bytes "
        "FROM system.query_log "
        f"WHERE query_id = '{query_id}' AND type = 'QueryFinish' "
        "ORDER BY event_time_microseconds DESC LIMIT 1 FORMAT JSONEachRow"
    )


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
            metadata_raw = canonical_bytes(metadata).decode("utf-8")
            row = {
                "event_id": record["event_id"],
                "start_time": timestamp_literal(record["start_time"]),
                "metadata": metadata if layout != "string" else metadata_raw,
            }
            if layout != "string":
                row["metadata_raw"] = metadata_raw
            process.stdin.write(canonical_bytes(row) + b"\n")
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
        "SELECT length(dynamic_paths) AS dynamic, length(shared_paths) AS shared, "
        "dynamic_paths, shared_paths FROM (SELECT "
        "arraySort(arrayDistinct(arrayFlatten(groupArray(JSONDynamicPaths(metadata))))) AS dynamic_paths, "
        "arraySort(arrayDistinct(arrayFlatten(groupArray(JSONSharedDataPaths(metadata))))) AS shared_paths "
        f"FROM {table}) FORMAT JSONEachRow"
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


def build_workload(dataset_path, truth):
    """从已校验输入构造固定 50% 时间窗口及其查询 truth。"""
    records = list(iter_records(dataset_path))
    if len(records) != truth["record_count"]:
        raise ValueError("dataset record count does not match truth manifest")
    lower = len(records) // 4
    upper = len(records) * 3 // 4
    window = {
        "end_exclusive": records[upper]["start_time"],
        "start_inclusive": records[lower]["start_time"],
    }
    selected = records[lower:upper]
    path_occurrences = Counter()
    path_first_seen_order = []
    seen_paths = set()
    for record in records:
        row_paths = [
            "hot.region",
            "hot.tenant",
            *(f"paths.{path}" for path in record["metadata"]["paths"]),
        ]
        path_occurrences.update(row_paths)
        for path in row_paths:
            if path not in seen_paths:
                seen_paths.add(path)
                path_first_seen_order.append(path)
    queries = {}
    for query in truth["queries"]:
        expected = set(query["expected_event_ids"])
        matched = [record for record in selected if record["event_id"] in expected]
        groups = Counter(record["metadata"]["hot"]["region"] for record in matched)
        queries[query["query_id"]] = {
            "expected_event_ids": sorted(record["event_id"] for record in matched),
            "expected_groups": [
                {"group_key": group_key, "rows": rows}
                for group_key, rows in sorted(groups.items())
            ],
        }
    return {
        "input_bytes": dataset_path.stat().st_size,
        "path_first_seen_order": path_first_seen_order,
        "path_occurrences": dict(sorted(path_occurrences.items())),
        "queries": queries,
        "selected_rows": len(selected),
        "time_fraction": len(selected) / len(records),
        "time_window": window,
        "total_rows": len(records),
    }


def validate_path_inventory(
    layout,
    inventory,
    occurrences,
    *,
    first_seen_order=None,
    require_strict_uplift=False,
):
    """校验 native JSON 路径预算、全集覆盖与按出现次数保留的优先级。"""
    hinted = HINTED_PATHS[layout]
    candidate_paths = set(occurrences) - hinted
    dynamic_paths = set(inventory["dynamic_paths"])
    shared_paths = set(inventory["shared_paths"])
    budget = DYNAMIC_PATH_BUDGETS[layout]
    expected_dynamic = min(budget, len(candidate_paths))
    expected_shared = len(candidate_paths) - expected_dynamic
    min_dynamic = min((occurrences[path] for path in dynamic_paths), default=None)
    max_shared = max((occurrences[path] for path in shared_paths), default=None)
    priority_passed = (
        not dynamic_paths
        or not shared_paths
        or min_dynamic >= max_shared
    )
    first_seen_candidates = [
        path
        for path in (first_seen_order or [])
        if path in candidate_paths
    ]
    first_seen_complete = (
        first_seen_order is None
        or (
            len(first_seen_candidates) == len(candidate_paths)
            and set(first_seen_candidates) == candidate_paths
        )
    )
    initial_dynamic = set(first_seen_candidates[:budget])
    entered_dynamic = dynamic_paths - initial_dynamic
    exited_dynamic = initial_dynamic - dynamic_paths
    min_entered = min((occurrences[path] for path in entered_dynamic), default=None)
    max_exited = max((occurrences[path] for path in exited_dynamic), default=None)
    strict_uplift_passed = (
        bool(entered_dynamic)
        and bool(exited_dynamic)
        and min_entered > max_exited
    )
    passed = (
        inventory["dynamic"] == len(dynamic_paths) == expected_dynamic
        and inventory["shared"] == len(shared_paths) == expected_shared
        and not dynamic_paths.intersection(shared_paths)
        and dynamic_paths.union(shared_paths) == candidate_paths
        and priority_passed
        and first_seen_complete
        and (not require_strict_uplift or strict_uplift_passed)
    )
    return {
        "entered_dynamic_count": len(entered_dynamic),
        "expected_dynamic": expected_dynamic,
        "expected_shared": expected_shared,
        "exited_dynamic_count": len(exited_dynamic),
        "first_seen_complete": first_seen_complete,
        "max_exited_occurrences": max_exited,
        "max_shared_occurrences": max_shared,
        "min_entered_occurrences": min_entered,
        "min_dynamic_occurrences": min_dynamic,
        "passed": passed,
        "priority_passed": priority_passed,
        "strict_uplift_passed": strict_uplift_passed,
        "strict_uplift_required": require_strict_uplift,
    }


def run_correctness_query(host, port, statement, expected_ids):
    """执行一次 ID truth 查询，避免把结果传输计入性能样本。"""
    body, _, elapsed = http_query(host, port, statement)
    actual_ids = body.splitlines()
    return {
        "actual_row_count": len(actual_ids),
        "client_elapsed_ms": elapsed * 1000,
        "expected_row_count": len(expected_ids),
        "matches_truth": actual_ids == expected_ids,
    }


def summarize_samples(samples):
    """汇总各次 QueryFinish 的服务端指标。"""
    metrics = {}
    for key in samples[0]:
        values = [sample[key] for sample in samples]
        metrics[key] = {
            "max": max(values),
            "median": statistics.median(values),
            "min": min(values),
            "samples": values,
        }
    return metrics


def collect_query_log(host, port, query_id):
    """刷新查询日志并读取唯一 QueryFinish 记录。"""
    rows = []
    for attempt in range(20):
        http_query(host, port, "SYSTEM FLUSH LOGS")
        body, _, _ = http_query(host, port, query_log_statement(query_id))
        rows = parse_json_each_row(body)
        if len(rows) == 1:
            return {key: int(value) for key, value in rows[0].items()}
        if attempt < 19:
            time.sleep(0.05)
    raise RuntimeError(f"expected one QueryFinish row for {query_id}, got {len(rows)}")


def run_performance_query(host, port, statement, expected_groups, measurements):
    """预热后测量小结果聚合，并从 query_log 读取完整服务端指标。"""
    http_query(host, port, statement)
    client_samples = []
    server_samples = []
    actual_groups = []
    for _ in range(measurements):
        query_id = f"json_stage1_{uuid.uuid4().hex}"
        body, _, elapsed = http_query(host, port, statement, query_id=query_id)
        actual_groups = parse_json_each_row(body)
        client_samples.append(elapsed * 1000)
        server_samples.append(collect_query_log(host, port, query_id))
    return {
        "actual_groups": actual_groups,
        "client_latency_ms": {
            "max": max(client_samples),
            "median": statistics.median(client_samples),
            "min": min(client_samples),
            "samples": client_samples,
        },
        "expected_groups": expected_groups,
        "matches_truth": actual_groups == expected_groups,
        "server_metrics": summarize_samples(server_samples),
    }


def run_scalar_performance_query(host, port, statement, measurements):
    """预热并测量强制内容读取的标量查询，校验多次结果稳定。"""
    http_query(host, port, statement)
    client_samples = []
    server_samples = []
    results = []
    for _ in range(measurements):
        query_id = f"json_stage1_{uuid.uuid4().hex}"
        body, _, elapsed = http_query(host, port, statement, query_id=query_id)
        results.append(body.strip())
        client_samples.append(elapsed * 1000)
        server_samples.append(collect_query_log(host, port, query_id))
    return {
        "client_latency_ms": {
            "max": max(client_samples),
            "median": statistics.median(client_samples),
            "min": min(client_samples),
            "samples": client_samples,
        },
        "result_sha256": hashlib.sha256(results[0].encode("utf-8")).hexdigest(),
        "server_metrics": summarize_samples(server_samples),
        "stable_result": len(set(results)) == 1,
    }


def audit_identities(returned_ids, expected_ids):
    """校验回读 ID 的缺失、额外与重复情况。"""
    counts = Counter(returned_ids)
    expected = set(expected_ids)
    duplicates = sorted(identifier for identifier, count in counts.items() if count > 1)
    extras = sorted(set(counts) - expected)
    missing = sorted(expected - set(counts))
    return {
        "duplicate_count": sum(counts[identifier] - 1 for identifier in duplicates),
        "duplicate_sample": duplicates[:10],
        "extra_count": len(extras),
        "extra_sample": extras[:10],
        "missing_count": len(missing),
        "missing_sample": missing[:10],
    }


def identity_audit_passed(roundtrip):
    """判断一次回读是否覆盖且仅覆盖预期 ID，并保持一行一个 ID。"""
    return all(
        roundtrip[key] == 0
        for key in ("duplicate_count", "extra_count", "missing_count")
    )


def verify_roundtrip(container_name, database_name, layout, truth, source_column):
    """流式回读指定来源，并按 event_id 校验 metadata canonical hash。"""
    expected = {row["event_id"]: row["metadata_canonical_sha256"] for row in truth["rows"]}
    if source_column == "metadata_raw":
        metadata_expression = "metadata_raw"
    elif layout == "string":
        metadata_expression = "metadata"
    else:
        metadata_expression = "toJSONString(metadata)"
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
    mismatch_count = 0
    mismatch_sample = []
    checked = 0
    returned_ids = []
    for line in process.stdout:
        row = json.loads(line)
        returned_ids.append(row["event_id"])
        actual = hashlib.sha256(canonical_bytes(json.loads(row["metadata_json"]))).hexdigest()
        if actual != expected.get(row["event_id"]):
            mismatch_count += 1
            if len(mismatch_sample) < 10:
                mismatch_sample.append(row["event_id"])
        checked += 1
    stderr = process.stderr.read()
    if process.wait() != 0:
        raise RuntimeError(f"ClickHouse roundtrip query failed: {stderr.strip()}")
    return {
        "checked": checked,
        "hash_mismatch_count": mismatch_count,
        "hash_mismatch_sample": mismatch_sample,
        "source_column": source_column,
        **audit_identities(returned_ids, expected),
    }


def execute(args):
    """执行三布局对照并写结果与完成 manifest。"""
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.unlink(missing_ok=True)
    if not IDENTIFIER.fullmatch(args.database_name):
        raise ValueError("database name must match ^[a-z][a-z0-9_]{0,62}$")
    if args.measurements < 5 or args.insert_chunks < 2:
        raise ValueError("measurements must be at least 5 and insert-chunks at least 2")
    input_dir = args.input.resolve()
    input_manifest, truth = verify_input(input_dir)
    workload = build_workload(input_dir / "dataset.jsonl", truth)
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

    database_created = False
    result = None
    manifest = None
    ddls = create_table_ddls(args.database_name)
    try:
        http_query(args.host, args.http_port, f"CREATE DATABASE {args.database_name}")
        database_created = True
        for ddl in ddls.values():
            http_query(args.host, args.http_port, ddl)

        layouts = {}
        analysis_passed = True
        fidelity_passed = True
        for layout in args.layout_order:
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
            inventory_validation = (
                None
                if layout == "string"
                else validate_path_inventory(
                    layout,
                    paths_after,
                    workload["path_occurrences"],
                    first_seen_order=workload["path_first_seen_order"],
                    require_strict_uplift=(
                        layout == "native_limited"
                        and input_manifest.get("density_profile", {}).get("kind") == "mixed"
                    ),
                )
            )
            hot_types = collect_hot_types(args.host, args.http_port, args.database_name, layout)

            queries = []
            plans = {}
            for query in truth["queries"]:
                expected = workload["queries"][query["query_id"]]
                correctness_sql = correctness_statement(
                    layout,
                    table,
                    query,
                    workload["time_window"],
                )
                performance_sql = performance_statement(
                    layout,
                    table,
                    query,
                    workload["time_window"],
                )
                correctness = run_correctness_query(
                    args.host,
                    args.http_port,
                    correctness_sql,
                    expected["expected_event_ids"],
                )
                performance = run_performance_query(
                    args.host,
                    args.http_port,
                    performance_sql,
                    expected["expected_groups"],
                    args.measurements,
                )
                queries.append(
                    {
                        "correctness": correctness,
                        "correctness_statement_sha256": hashlib.sha256(
                            correctness_sql.encode("utf-8")
                        ).hexdigest(),
                        "performance": performance,
                        "performance_statement_sha256": hashlib.sha256(
                            performance_sql.encode("utf-8")
                        ).hexdigest(),
                        "predicate_family": (
                            "string_parse" if layout == "string" else "native_subcolumn"
                        ),
                        "query_id": query["query_id"],
                    }
                )
                plan_body, _, _ = http_query(
                    args.host,
                    args.http_port,
                    f"EXPLAIN PIPELINE {performance_sql.rsplit(' FORMAT ', 1)[0]}",
                )
                plans[query["query_id"]] = plan_body

            full_object_read = {}
            for source in ("storage", "fidelity"):
                if source == "fidelity" and layout == "string":
                    full_object_read[source] = None
                    continue
                statement = full_object_statement(
                    layout,
                    table,
                    workload["time_window"],
                    source,
                )
                measurement = run_scalar_performance_query(
                    args.host,
                    args.http_port,
                    statement,
                    args.measurements,
                )
                measurement["statement_sha256"] = hashlib.sha256(
                    statement.encode("utf-8")
                ).hexdigest()
                full_object_read[source] = measurement
            storage_roundtrip = verify_roundtrip(
                args.container_name,
                args.database_name,
                layout,
                truth,
                "metadata",
            )
            fidelity_roundtrip = (
                dict(storage_roundtrip)
                if layout == "string"
                else verify_roundtrip(
                    args.container_name,
                    args.database_name,
                    layout,
                    truth,
                    "metadata_raw",
                )
            )
            layout_analysis_passed = (
                load["rows"] == truth["record_count"]
                and parts_before["part_count"] >= 2
                and parts_after["part_count"] == 1
                and parts_after["rows"] == truth["record_count"]
                and all(
                    query["correctness"]["matches_truth"]
                    and query["performance"]["matches_truth"]
                    for query in queries
                )
                and storage_roundtrip["checked"] == truth["record_count"]
                and identity_audit_passed(storage_roundtrip)
                and (inventory_validation is None or inventory_validation["passed"])
                and full_object_read["storage"]["stable_result"]
            )
            layout_fidelity_passed = (
                fidelity_roundtrip["checked"] == truth["record_count"]
                and fidelity_roundtrip["hash_mismatch_count"] == 0
                and identity_audit_passed(fidelity_roundtrip)
                and (
                    full_object_read["fidelity"] is None
                    or full_object_read["fidelity"]["stable_result"]
                )
            )
            analysis_passed = analysis_passed and layout_analysis_passed
            fidelity_passed = fidelity_passed and layout_fidelity_passed
            layouts[layout] = {
                "fidelity_roundtrip": fidelity_roundtrip,
                "full_object_read": full_object_read,
                "gates": {
                    "analysis_equivalence": "pass" if layout_analysis_passed else "fail",
                    "document_fidelity": "pass" if layout_fidelity_passed else "fail",
                },
                "hot_path_types": hot_types,
                "load": load,
                "merge_wall_seconds": merge_seconds,
                "path_inventory_validation": inventory_validation,
                "parts_after_merge": parts_after,
                "parts_before_merge": parts_before,
                "paths_after_merge": paths_after,
                "paths_before_merge": paths_before,
                "plans": plans,
                "queries": queries,
                "storage_roundtrip": storage_roundtrip,
            }

        string_full_digest = layouts["string"]["full_object_read"]["storage"]["result_sha256"]
        for layout in ("native_limited", "native_hinted"):
            fidelity_read = layouts[layout]["full_object_read"]["fidelity"]
            fidelity_read["matches_string_storage"] = (
                fidelity_read["result_sha256"] == string_full_digest
            )
            if not fidelity_read["matches_string_storage"]:
                layouts[layout]["gates"]["document_fidelity"] = "fail"
                fidelity_passed = False
        analysis_passed = analysis_passed and layouts["native_hinted"]["hot_path_types"] == {
            "hot.region": "String",
            "hot.tenant": "String",
        }
        all_passed = analysis_passed and fidelity_passed
        result = {
            "correctness_contract": {
                "analysis_source": "metadata",
                "document_fidelity_source": {
                    "native_hinted": "metadata_raw",
                    "native_limited": "metadata_raw",
                    "string": "metadata",
                },
                "native_storage_roundtrip": "observed_engine_semantics",
            },
            "database": database,
            "gates": {
                "analysis_equivalence": "pass" if analysis_passed else "fail",
                "document_fidelity": "pass" if fidelity_passed else "fail",
            },
            "layouts": layouts,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "status": "pass" if all_passed else "fail",
            "storage": {"engine": "MergeTree", "json_type": "JSON"},
            "workload": workload,
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
                "density_percent": input_manifest.get("density_percent"),
                "density_profile": input_manifest.get(
                    "density_profile",
                    {"density_percent": input_manifest.get("density_percent"), "kind": "uniform"},
                ),
                "path_count": input_manifest["path_count"],
                "record_count": input_manifest["record_count"],
                "truth_sha256": input_manifest["artifacts"]["truth-manifest.json"]["sha256"],
            },
            "insert_chunks": args.insert_chunks,
            "layout_order": list(args.layout_order),
            "measurements": args.measurements,
            "correctness_contract": result["correctness_contract"],
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
    parser.add_argument(
        "--layout-order",
        default=LAYOUTS,
        type=parse_layout_order,
        help="三个布局的逗号分隔执行顺序，用于跨轮轮换",
    )
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
