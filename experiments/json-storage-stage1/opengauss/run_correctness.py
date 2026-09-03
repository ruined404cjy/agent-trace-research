#!/usr/bin/env python3
"""在标准 openGauss 6.0.0 行存上运行 exporter JSON schema 正确性探针。"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


BENCHMARK_BASELINE = "9529c8f389673132757f4da9a96878926f22b94f"
EXPORTER_BASELINE = "54ca553a7ed09ad1751c82adab3aa52c6e9357b1"
GSQL = "/usr/local/opengauss/bin/gsql"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
FIELD_SEPARATOR = "\x1f"

EVENT_COLUMNS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "project_id",
    "start_time",
    "end_time",
    "type",
    "name",
    "level",
    "status_message",
    "is_app_root",
    "trace_name",
    "user_id",
    "session_id",
    "tags",
    "release",
    "version",
    "environment",
    "input",
    "output",
    "metadata",
    "model",
    "input_tokens",
    "output_tokens",
    "total_cost",
    "event_version",
)

EVENTS_DDL = """CREATE TABLE {table} (
    trace_id       VARCHAR(32) NOT NULL,
    span_id        VARCHAR(64) NOT NULL,
    parent_span_id VARCHAR(64),
    project_id     VARCHAR(64) NOT NULL DEFAULT 'default',
    start_time     TIMESTAMP(6) NOT NULL,
    end_time       TIMESTAMP(6),
    type           VARCHAR(16) NOT NULL,
    name           VARCHAR(255),
    level          VARCHAR(16) NOT NULL,
    status_message TEXT,
    is_app_root    BOOLEAN NOT NULL DEFAULT false,
    trace_name     VARCHAR(255),
    user_id        VARCHAR(128),
    session_id     VARCHAR(128),
    tags           JSON,
    release        VARCHAR(128),
    version        VARCHAR(128),
    environment    VARCHAR(64) NOT NULL DEFAULT 'default',
    input          JSON,
    output         JSON,
    metadata       JSON,
    model          VARCHAR(128),
    input_tokens   BIGINT,
    output_tokens  BIGINT,
    total_cost     DOUBLE PRECISION,
    event_version  BIGINT NOT NULL
)"""

QUERY_SQL = {
    "metadata_column_is_sql_null": "metadata IS NULL",
    "metadata_target_is_missing": (
        "metadata IS NOT NULL AND (metadata->'target') IS NULL"
    ),
    "metadata_target_is_json_null": (
        "metadata IS NOT NULL AND json_typeof(metadata->'target') = 'null'"
    ),
    "metadata_target_integer_equals_one": (
        "json_typeof(metadata->'target') = 'number' AND metadata->>'target' = '1'"
    ),
    "metadata_conflict_is_object": "json_typeof(metadata->'conflict') = 'object'",
    "escaped_path_equals_escaped": (
        "metadata #>> '{a/b,tilde~key}' = 'escaped'"
    ),
    "array_order_equals_3_2_1": (
        "json_typeof(metadata->'ordered') = 'array' "
        "AND metadata #>> '{ordered,0}' = '3' "
        "AND metadata #>> '{ordered,1}' = '2' "
        "AND metadata #>> '{ordered,2}' = '1'"
    ),
}

SUMMARY_SQL = {
    "row_count": "TRUE",
    "sql_null_count": QUERY_SQL["metadata_column_is_sql_null"],
    "target_missing_count": QUERY_SQL["metadata_target_is_missing"],
    "json_null_count": QUERY_SQL["metadata_target_is_json_null"],
    "target_boolean_count": (
        "json_typeof(metadata->'target') = 'boolean' AND metadata->>'target' = 'true'"
    ),
    "target_integer_count": QUERY_SQL["metadata_target_integer_equals_one"],
    "target_number_count": (
        "json_typeof(metadata->'target') = 'number' AND metadata->>'target' = '1.5'"
    ),
    "conflict_string_count": "json_typeof(metadata->'conflict') = 'string'",
    "conflict_integer_count": (
        "json_typeof(metadata->'conflict') = 'number' AND metadata->>'conflict' = '1'"
    ),
    "conflict_object_count": QUERY_SQL["metadata_conflict_is_object"],
    "conflict_array_count": "json_typeof(metadata->'conflict') = 'array'",
    "escaped_path_count": QUERY_SQL["escaped_path_equals_escaped"],
    "array_order_count": QUERY_SQL["array_order_equals_3_2_1"],
}


def canonical_bytes(value):
    """返回与公共生成器一致的 canonical JSON bytes。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_hex(content):
    """计算输入 bytes 的十六进制 SHA-256。"""
    return hashlib.sha256(content).hexdigest()


def run(command, *, stdin=None, check=True):
    """执行外部命令并返回结果；失败时保留可读诊断。"""
    completed = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{detail}")
    return completed


def gsql(container_name, sql, *, field_separator=None, check=True):
    """通过容器内 gsql 执行 SQL，并显式识别返回码为零的 SQL 错误。"""
    command = [
        "docker",
        "exec",
        "-i",
        "-u",
        "omm",
        container_name,
        "bash",
        "-lc",
        'exec "$0" "$@"',
        GSQL,
        "-X",
        "-d",
        "postgres",
        "-At",
    ]
    if field_separator is not None:
        command.extend(["-F", field_separator])
    completed = run(command, stdin=sql, check=False)
    diagnostic = "\n".join((completed.stdout, completed.stderr))
    sql_error = any(
        line.lstrip().startswith(("ERROR:", "FATAL:"))
        for line in diagnostic.splitlines()
    )
    if check and (completed.returncode != 0 or sql_error):
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"gsql failed ({completed.returncode})\n{detail}")
    return completed


def verify_input(input_dir):
    """验证公共数据产物，并返回 manifest、truth 和逻辑记录。"""
    input_manifest = json.loads(
        (input_dir / "run-manifest.json").read_text(encoding="utf-8")
    )
    if input_manifest.get("status") != "complete":
        raise ValueError("input run manifest is not complete")
    for name, expected in input_manifest["artifacts"].items():
        content = (input_dir / name).read_bytes()
        actual = {"bytes": len(content), "sha256": sha256_hex(content)}
        if actual != expected:
            raise ValueError(f"input artifact mismatch: {name}")
    truth = json.loads((input_dir / "truth-manifest.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (input_dir / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    if len(records) != truth["record_count"]:
        raise ValueError("dataset row count does not match truth manifest")
    return input_manifest, truth, records


def sql_literal(value):
    """把受控测试值编码为 SQL 字面量；None 映射为 SQL NULL。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def event_values(record, event_version):
    """把公共逻辑记录投影到 exporter 54ca553 的 26 列 events schema。"""
    metadata = record.get("metadata")
    metadata_sql = "NULL"
    if metadata is not None:
        metadata_sql = sql_literal(canonical_bytes(metadata).decode("utf-8")) + "::json"
    values = (
        sql_literal(record["trace_id"]),
        sql_literal(record["span_id"]),
        sql_literal(record["parent_span_id"]),
        sql_literal("default"),
        sql_literal(record["start_time"]),
        sql_literal(record["end_time"]),
        sql_literal(record["type"]),
        "NULL",
        sql_literal(record["level"]),
        "NULL",
        "FALSE",
        "NULL",
        "NULL",
        "NULL",
        "NULL",
        "NULL",
        "NULL",
        sql_literal("default"),
        "NULL",
        "NULL",
        metadata_sql,
        sql_literal(record["model"]),
        "NULL",
        "NULL",
        "NULL",
        str(event_version),
    )
    return "(" + ",".join(values) + ")"


def build_load_sql(table, duplicate_table, records, duplicate_probe):
    """生成分批 INSERT SQL；批大小与 exporter eventsInsertChunk 一致。"""
    statements = []
    columns = ",".join(EVENT_COLUMNS)
    for start in range(0, len(records), 100):
        rows = [
            event_values(record, index + 1)
            for index, record in enumerate(records[start : start + 100], start=start)
        ]
        statements.append(f"INSERT INTO {table} ({columns}) VALUES " + ",".join(rows) + ";")
    statements.append(
        f"INSERT INTO {duplicate_table}(raw_json) VALUES "
        f"({sql_literal(duplicate_probe)}::json);"
    )
    return "\n".join(statements)


def query_event_ids(container_name, table, predicate, event_id_by_span_id):
    """执行 JSON 谓词并把数据库 span_id 映射回公共 truth 的 event_id。"""
    output = gsql(
        container_name,
        f"SELECT span_id FROM {table} WHERE {predicate} ORDER BY span_id;",
    ).stdout
    span_ids = [line.strip() for line in output.splitlines() if line.strip()]
    return sorted(event_id_by_span_id[span_id] for span_id in span_ids)


def collect_summary(container_name, table):
    """执行公共 case 的计数查询。"""
    summary = {}
    for name, predicate in SUMMARY_SQL.items():
        output = gsql(
            container_name,
            f"SELECT count(*) FROM {table} WHERE {predicate};",
        ).stdout.strip()
        summary[name] = int(output)
    return summary


def verify_roundtrip(container_name, table, records, truth):
    """回读 metadata JSON，并按原 span_id 对比 canonical hash。"""
    output = gsql(
        container_name,
        f"SELECT span_id, metadata::text FROM {table} ORDER BY span_id;",
        field_separator=FIELD_SEPARATOR,
    ).stdout
    truth_by_event_id = {row["event_id"]: row for row in truth["rows"]}
    expected_by_span_id = {
        record["span_id"]: truth_by_event_id[record["event_id"]]
        for record in records
    }
    mismatches = []
    checked = 0
    for line in output.splitlines():
        if not line:
            continue
        span_id, metadata_text = line.split(FIELD_SEPARATOR, 1)
        expected = expected_by_span_id[span_id]["columns"]["metadata"]
        checked += 1
        if expected["state"] == "sql_null":
            if metadata_text:
                mismatches.append(span_id)
            continue
        actual_hash = sha256_hex(canonical_bytes(json.loads(metadata_text)))
        if actual_hash != expected["canonical_sha256"]:
            mismatches.append(span_id)
    return {"checked": checked, "hash_mismatches": sorted(mismatches)}


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
    ).stdout.strip()
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
    ).stdout.strip()
    digests_raw, resolved_id, size = image_raw.split("|", 2)
    digests = json.loads(digests_raw)
    repo_digest = next(
        (item for item in digests if item.startswith("enmotech/opengauss@")),
        digests[0],
    )
    server_version = gsql(container_name, "SELECT version();").stdout.strip()
    return {
        "container": container_name,
        "image": image_name,
        "image_id": resolved_id,
        "repo_digest": repo_digest,
        "server_version": server_version,
        "size": int(size),
    }


def inspect_duplicate_probe(container_name, table):
    """记录 JSON 重复键输入是否接受，以及路径提取得到的保留值。"""
    output = gsql(
        container_name,
        f"SELECT json_typeof(raw_json #> '{{metadata,dup}}'), "
        f"raw_json #>> '{{metadata,dup}}' FROM {table};",
        field_separator=FIELD_SEPARATOR,
    ).stdout.strip()
    retained_type, retained_text = output.split(FIELD_SEPARATOR, 1)
    return {
        "accepted": True,
        "retained_type": retained_type,
        "retained_value": json.loads(retained_text),
    }


def execute(args):
    """执行完整探针并写 correctness 与完成 manifest。"""
    if not IDENTIFIER.fullmatch(args.schema_name):
        raise ValueError("schema name must match ^[a-z][a-z0-9_]{0,62}$")
    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    input_manifest, truth, records = verify_input(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    manifest_path.unlink(missing_ok=True)

    database = inspect_database(args.container_name)
    existing = gsql(
        args.container_name,
        f"SELECT count(*) FROM pg_namespace WHERE nspname='{args.schema_name}';",
    ).stdout.strip()
    if existing != "0":
        raise ValueError(f"schema already exists: {args.schema_name}")

    table = f"{args.schema_name}.events"
    duplicate_table = f"{args.schema_name}.duplicate_probes"
    ddl = EVENTS_DDL.format(table=table) + ";\n" + (
        f"CREATE TABLE {duplicate_table} (raw_json JSON NOT NULL);"
    )
    schema_created = False
    try:
        gsql(args.container_name, f"CREATE SCHEMA {args.schema_name};\n{ddl}")
        schema_created = True
        duplicate_probe = (
            input_dir / "duplicate-key-probes.jsonl"
        ).read_text(encoding="utf-8").strip()
        gsql(
            args.container_name,
            build_load_sql(table, duplicate_table, records, duplicate_probe),
        )

        event_id_by_span_id = {
            record["span_id"]: record["event_id"] for record in records
        }
        queries = []
        all_queries_match = True
        for query in truth["queries"]:
            query_id = query["query_id"]
            actual_ids = query_event_ids(
                args.container_name,
                table,
                QUERY_SQL[query_id],
                event_id_by_span_id,
            )
            matches = actual_ids == query["expected_event_ids"]
            all_queries_match = all_queries_match and matches
            queries.append(
                {
                    "actual_event_ids": actual_ids,
                    "actual_row_count": len(actual_ids),
                    "expected_event_ids": query["expected_event_ids"],
                    "expected_row_count": query["expected_row_count"],
                    "matches_truth": matches,
                    "query_id": query_id,
                }
            )

        roundtrip = verify_roundtrip(args.container_name, table, records, truth)
        duplicate = inspect_duplicate_probe(args.container_name, duplicate_table)
        summary = collect_summary(args.container_name, table)
        jsonb_available = int(
            gsql(
                args.container_name,
                "SELECT count(*) FROM pg_type WHERE typname='jsonb';",
            ).stdout.strip()
        ) > 0
        passed = (
            all_queries_match
            and roundtrip["checked"] == truth["record_count"]
            and not roundtrip["hash_mismatches"]
            and duplicate == {
                "accepted": True,
                "retained_type": "number",
                "retained_value": 2,
            }
        )
        result = {
            "database": database,
            "duplicate_key_probe": duplicate,
            "queries": queries,
            "roundtrip": roundtrip,
            "runtime_capabilities": {"jsonb_type_available": jsonb_available},
            "status": "pass" if passed else "fail",
            "storage": {"json_type": "JSON", "profile": "row"},
            "summary": summary,
        }
        result_bytes = canonical_bytes(result) + b"\n"
        (output_dir / "correctness.json").write_bytes(result_bytes)

        manifest = {
            "artifacts": {
                "correctness.json": {
                    "bytes": len(result_bytes),
                    "sha256": sha256_hex(result_bytes),
                }
            },
            "baselines": {
                "benchmark": BENCHMARK_BASELINE,
                "exporter": EXPORTER_BASELINE,
            },
            "data_path": "independent_loader",
            "database": database,
            "ddl_scope": "exporter_events_table_without_indexes_or_view",
            "ddl_sha256": sha256_hex(ddl.encode("utf-8")),
            "input": {
                "dataset_sha256": input_manifest["artifacts"]["dataset.jsonl"]["sha256"],
                "truth_sha256": input_manifest["artifacts"]["truth-manifest.json"]["sha256"],
            },
            "runner_sha256": sha256_hex(Path(__file__).read_bytes()),
            "status": "complete" if passed else "failed",
        }
        manifest_path.write_bytes(canonical_bytes(manifest) + b"\n")
        if not passed:
            raise RuntimeError("openGauss correctness probe did not match truth manifest")
    finally:
        if schema_created:
            gsql(args.container_name, f"DROP SCHEMA {args.schema_name} CASCADE;")


def parse_args():
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="公共正确性 run 目录")
    parser.add_argument("--output", required=True, type=Path, help="数据库实验结果目录")
    parser.add_argument("--container-name", required=True, help="已运行的 openGauss 容器名")
    parser.add_argument("--schema-name", required=True, help="本次使用的唯一临时 schema")
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
