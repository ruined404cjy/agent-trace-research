#!/usr/bin/env python3
"""按固定顺序执行一轮四种 JSON 存储结构实验。"""

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from clickhouse_four_layout import ClickHouseFourLayoutAdapter, QUERY_LOG_METRICS
from opengauss_four_layout import OpenGaussFourLayoutAdapter
from supplement_common import (
    CONTRACT_VERSION, LAYOUTS, QUERY_IDS,
    ROUND_ORDERS, canonical_bytes, file_identity, read_json, verify_input,
    write_failed_manifest, write_manifest_last,
)


EXPECTED_DATASET_SHA256 = "8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405"
EXPECTED_SOURCE_SHA256 = "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683"
EXPECTED_TRUTH_SHA256 = "929d79b729c5b7ad5fa51150ee00f55249647c91365271e02ffc345e6760bb28"
EXPECTED_CATALOG_SHA256 = "554f7ead33fc17aada0432997ff314f84b6f44e7945d8355544818f5d868c4a8"
EXPECTED_FILE_IDENTITIES = {
    "dataset": {"bytes": 302518948, "sha256": EXPECTED_DATASET_SHA256},
    "truth": {"bytes": 138577, "sha256": EXPECTED_TRUTH_SHA256},
    "query_catalog": {"bytes": 1074, "sha256": EXPECTED_CATALOG_SHA256},
    "input_run_manifest": {
        "bytes": 2397,
        "sha256": "25181ebc6f22fe4f09fa9aa3d36c997d4b60744ffb82a9fa75f9741e7c216437",
    },
    "input_truth_manifest": {
        "bytes": 18073179,
        "sha256": "b04f49ab89cb9da9636b60134317915708ff207a96058392ca0734636b525d04",
    },
}
EXPECTED_ENDPOINTS = {
    "host": "127.0.0.1",
    "opengauss_container": "agent-trace-opengauss-v6",
    "opengauss_port": 15432,
    "clickhouse_container": "agent-trace-clickhouse-25-12",
    "clickhouse_port": 18123,
}


def parse_layout_order(value, round_no):
    """解析布局顺序，并要求其与指定轮次完全一致。"""
    if round_no not in range(1, 5):
        raise ValueError("round must be 1, 2, 3, or 4")
    order = tuple(item.strip() for item in value.split(",")) if isinstance(value, str) else ()
    if order != ROUND_ORDERS[round_no - 1]:
        raise ValueError("layout order does not match the fixed round order")
    return order


def _close(connection):
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _query_sample(adapter, connection, layout, query_id, params, expected, phase, worker):
    """执行一个查询，并在计时区间外核对 Task 1 truth。"""
    actual = adapter.execute_query(connection, layout, query_id, params)
    if not isinstance(actual, dict) or actual.get("result") != expected:
        raise RuntimeError(f"truth mismatch: {layout} {query_id}")
    latency = actual.get("latency_ms")
    recovery = actual.get("recovery_ms")
    if (type(latency) not in (int, float) or not math.isfinite(latency) or latency <= 0
            or type(recovery) not in (int, float) or not math.isfinite(recovery) or recovery < 0):
        raise RuntimeError(f"invalid query timing: {layout} {query_id}")
    expected_digest = hashlib.sha256(canonical_bytes(expected)).hexdigest()
    if actual.get("result_sha256") != expected_digest:
        raise RuntimeError(f"invalid query result digest: {layout} {query_id}")
    sample = {
        "latency_ms": actual.get("latency_ms"), "recovery_ms": actual.get("recovery_ms"),
        "matches_truth": True, "ok": True, "phase": phase, "query_id": query_id,
        "result_sha256": actual.get("result_sha256"), "row_count": actual.get("row_count"),
        "worker": worker,
    }
    if "query_log_id" in actual:
        sample["query_log_id"] = actual["query_log_id"]
    return sample


def _backfill_query_logs(adapter, samples):
    """在全部测量结束后批量关联 ClickHouse QueryFinish。"""
    collect = getattr(adapter, "collect_query_logs", None)
    if not callable(collect):
        return
    ids = [sample.get("query_log_id") for sample in samples]
    if all(value is None for value in ids):
        return
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("missing or duplicate ClickHouse query log IDs")
    metrics = collect(ids)
    if set(metrics) != set(ids):
        raise RuntimeError("incomplete ClickHouse QueryFinish metrics")
    for sample in samples:
        values = metrics[sample["query_log_id"]]
        if not isinstance(values, dict) or set(values) != set(QUERY_LOG_METRICS):
            raise RuntimeError("incomplete ClickHouse QueryFinish metrics")
        sample["query_log"] = values


def run_query_stage(adapter, layout, catalog, truth, measurements, document_measurements, workers=2):
    """以两个已连接 worker 和阶段屏障执行预热与正式查询。"""
    for name, value in (("measurements", measurements), ("document_measurements", document_measurements)):
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be positive")
    if workers != 2:
        raise ValueError("workers must be 2")
    try:
        parameters = catalog["parameters"]
        expected = truth["results"]
    except (KeyError, TypeError) as error:
        raise ValueError("invalid query catalog or truth") from error
    tasks = []
    for query_id in QUERY_IDS:
        count = document_measurements if query_id == "S06" else measurements
        tasks.extend((query_id, index % workers) for index in range(count))
    assignments = [[query for query, worker in tasks if worker == number] for number in range(workers)]
    barrier = threading.Barrier(workers + 1)
    samples, warmups, errors = [], [], []
    lock = threading.Lock()

    def work(number):
        connection = None
        try:
            connection = adapter.connect_worker()
            barrier.wait()
            # 每条查询总计预热一次，由任务序号对应的 worker 执行。
            for index, query_id in enumerate(QUERY_IDS):
                if index % workers == number:
                    warmup = _query_sample(adapter, connection, layout, query_id,
                                           parameters[query_id], expected[query_id], "warmup", number)
                    with lock:
                        warmups.append(warmup)
            barrier.wait()
            local = [_query_sample(adapter, connection, layout, query_id, parameters[query_id],
                                   expected[query_id], "measurement", number)
                     for query_id in assignments[number]]
            with lock:
                samples.extend(local)
        except Exception as error:
            with lock:
                errors.append(error)
            try:
                barrier.abort()
            except threading.BrokenBarrierError:
                pass
        finally:
            _close(connection)

    threads = [threading.Thread(target=work, args=(number,), name=f"s2sup-query-{number}")
               for number in range(workers)]
    for thread in threads:
        thread.start()
    try:
        barrier.wait()
        barrier.wait()
    except threading.BrokenBarrierError:
        pass
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    warmups.sort(key=lambda item: item["query_id"])
    samples.sort(key=lambda item: (item["query_id"], item["worker"]))
    _backfill_query_logs(adapter, samples)
    if all(isinstance(sample.get("query_log_id"), str) for sample in warmups):
        _backfill_query_logs(adapter, warmups)
    return {
        "plans": {query: adapter.collect_plan(layout, query, parameters[query]) for query in QUERY_IDS},
        "samples": samples, "warmups": warmups, "workers": workers,
        "worker_sample_counts": {str(number): sum(s["worker"] == number for s in samples)
                                 for number in range(workers)},
    }


def load_inputs(input_dir, truth_dir):
    """核对冻结输入与 Task 1 truth，并返回执行数据及身份。"""
    rows, source_truth = verify_input(input_dir)
    input_manifest = read_json(Path(input_dir) / "run-manifest.json", "input manifest")
    dataset = file_identity(Path(input_dir) / "dataset.jsonl")
    source_input = input_manifest.get("input")
    if (not isinstance(source_input, dict) or set(source_input) != {"path", "sha256"}
            or not isinstance(source_input["path"], str) or not source_input["path"]
            or source_input["sha256"] != EXPECTED_SOURCE_SHA256):
        raise ValueError("source input identity mismatch")
    if dataset != EXPECTED_FILE_IDENTITIES["dataset"]:
        raise ValueError("fixed input identity mismatch")
    truth_manifest = read_json(Path(truth_dir) / "run-manifest.json", "supplement manifest")
    if truth_manifest.get("status") != "complete":
        raise ValueError("supplement truth is incomplete")
    artifacts = truth_manifest.get("artifacts", {})
    expected_artifacts = {"query-catalog.json": EXPECTED_FILE_IDENTITIES["query_catalog"],
                          "truth-manifest.json": EXPECTED_FILE_IDENTITIES["truth"]}
    for name in ("query-catalog.json", "truth-manifest.json"):
        actual = file_identity(Path(truth_dir) / name)
        if actual != expected_artifacts[name] or artifacts.get(name) != actual:
            raise ValueError(f"supplement truth artifact mismatch: {name}")
    catalog = read_json(Path(truth_dir) / "query-catalog.json", "query catalog")
    truth = read_json(Path(truth_dir) / "truth-manifest.json", "supplement truth")
    if catalog.get("contract_version") != CONTRACT_VERSION or truth.get("contract_version") != CONTRACT_VERSION:
        raise ValueError("supplement contract mismatch")
    if catalog.get("query_ids") != list(QUERY_IDS) or truth.get("query_ids") != list(QUERY_IDS):
        raise ValueError("supplement query set mismatch")
    if truth.get("query_catalog_sha256") != hashlib.sha256(canonical_bytes(catalog)).hexdigest():
        raise ValueError("query catalog identity mismatch")
    if truth.get("input", {}).get("dataset_sha256") != dataset["sha256"]:
        raise ValueError("input and supplement truth identity mismatch")
    source = truth.get("source", {})
    input_run = file_identity(Path(input_dir) / "run-manifest.json")
    input_truth = file_identity(Path(input_dir) / "truth-manifest.json")
    if (input_run != EXPECTED_FILE_IDENTITIES["input_run_manifest"]
            or input_truth != EXPECTED_FILE_IDENTITIES["input_truth_manifest"]
            or source.get("run_manifest_sha256") != input_run["sha256"]
            or source.get("truth_manifest_sha256") != input_truth["sha256"]
            or source.get("artifacts", {}).get("dataset.jsonl") != dataset
            or source.get("artifacts", {}).get("truth-manifest.json") != input_truth
            or source.get("input") != input_manifest.get("input")
            or truth_manifest.get("source") != source
            or truth_manifest.get("input") != truth.get("input")):
        raise ValueError("supplement truth source identity mismatch")
    if source_truth.get("block_count") != 190 or source_truth.get("block_size") != 256 or len(rows) != 48534:
        raise ValueError("frozen block contract mismatch")
    if truth.get("record_count") != len(rows) or truth.get("block_count") != 190 or truth.get("block_size") != 256:
        raise ValueError("supplement truth size mismatch")
    return rows, source_truth, catalog, truth, {
        "dataset": dataset, "source_input": source_input, "input_run_manifest": input_run,
        "input_truth_manifest": input_truth,
        "truth": file_identity(Path(truth_dir) / "truth-manifest.json"),
        "query_catalog": file_identity(Path(truth_dir) / "query-catalog.json"),
    }


def _container_identity(name):
    """以参数数组读取不含容器环境变量的身份。"""
    if not isinstance(name, str) or not name:
        raise RuntimeError("invalid container identity: missing name")
    completed = subprocess.run([
        "docker", "inspect", name, "--format",
        "{{.Config.Image}}|{{.Image}}|{{json .HostConfig.PortBindings}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.State.Running}}",
    ], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"unable to inspect container: {name}")
    try:
        image, image_id, ports_json, memory_text, nanocpus_text, running = completed.stdout.strip().split("|", 5)
        ports = json.loads(ports_json)
        memory = int(memory_text)
        nanocpus = int(nanocpus_text)
    except (ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid container identity: {name}") from error
    if (not image or not image_id or not isinstance(ports, dict) or not ports
            or memory < 0 or nanocpus < 0):
        raise RuntimeError(f"invalid container identity: {name}")
    if running != "true":
        raise RuntimeError(f"container is not running: {name}")
    image_result = subprocess.run(
        ["docker", "image", "inspect", image_id, "--format", "{{json .RepoDigests}}"],
        capture_output=True, text=True, check=False,
    )
    try:
        repo_digests = json.loads(image_result.stdout) if image_result.returncode == 0 else None
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid container image identity: {name}") from error
    if (not isinstance(repo_digests, list) or not repo_digests
            or not all(isinstance(item, str) and item for item in repo_digests)):
        raise RuntimeError(f"missing container image identity: {name}")
    return {"name": name, "image": image, "image_id": image_id, "ports": ports,
            "memory_bytes": memory, "cpu_nanocpus": nanocpus,
            "repo_digests": sorted(repo_digests), "running": True}


def _require_host_port(identity, container_port, host_port, engine):
    """要求固定容器端口至少包含指定宿主端口映射。"""
    bindings = identity.get("ports", {}).get(container_port)
    if (not isinstance(bindings, list) or not bindings
            or not any(isinstance(item, dict) and item.get("HostPort") == str(host_port)
                       for item in bindings)):
        raise RuntimeError(f"invalid {engine} container port")


def collect_environment(args, adapters):
    """采集两容器与宿主的可比环境身份。"""
    disk = shutil.disk_usage(Path(__file__).resolve().parents[3])
    og, ch = adapters
    og_identity = _container_identity(args.opengauss_container)
    ch_identity = _container_identity(args.clickhouse_container)
    _require_host_port(og_identity, "5432/tcp", 15432, "openGauss")
    _require_host_port(ch_identity, "8123/tcp", 18123, "ClickHouse")
    og_version = og.database_version()
    ch_version = ch.database_version()
    if not isinstance(og_version, str) or not og_version.startswith("(openGauss 6.0.0 "):
        raise RuntimeError("invalid openGauss database version")
    if ch_version != "25.12.11.4":
        raise RuntimeError("invalid ClickHouse database version")
    return {"containers": {"opengauss": {**og_identity, "database_version": og_version},
                           "clickhouse": {**ch_identity, "database_version": ch_version}},
            "host": {"cpu_count": os.cpu_count(), "disk_total_bytes": disk.total,
                     "memory_bytes": os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"),
                     "platform": platform.platform(), "kernel": platform.release()}}


def make_adapters(args):
    """创建两个引擎 adapter；每个 adapter 管理两个独立布局。"""
    return (OpenGaussFourLayoutAdapter(args.host, args.opengauss_port, args.opengauss_container, args.namespace),
            ClickHouseFourLayoutAdapter(args.host, args.clickhouse_port, args.clickhouse_container, args.namespace))


def _code_identity():
    """记录编排、公用契约和两个 adapter 的代码摘要。"""
    runner_dir = Path(__file__).resolve().parent
    files = {
        "runner/run_four_layouts.py": Path(__file__),
        "runner/supplement_common.py": runner_dir / "supplement_common.py",
        "runner/opengauss_four_layout.py": runner_dir / "opengauss_four_layout.py",
        "runner/clickhouse_four_layout.py": runner_dir / "clickhouse_four_layout.py",
    }
    return {"path": "runner/run_four_layouts.py", "sha256": file_identity(__file__)["sha256"],
            "files": {name: file_identity(path)["sha256"] for name, path in files.items()}}


def _validate_execution_args(args):
    """在创建数据库对象前固定正式采样与维护参数。"""
    expected = {"measurements": 100, "document_page_measurements": 20, "query_workers": 2,
                **EXPECTED_ENDPOINTS}
    for name, value in expected.items():
        if getattr(args, name, None) != value:
            raise ValueError(f"{name} must be {value}")
    if type(args.maintenance_timeout_seconds) is not int or args.maintenance_timeout_seconds <= 0:
        raise ValueError("maintenance_timeout_seconds must be positive")


def execute(args):
    """执行单轮四布局；完成清理后最后发布 manifest。"""
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "run-manifest.json").unlink(missing_ok=True)
    manifest = {"format": "agent-trace-json-storage-four-layout-run", "format_version": 1,
                "supplement_contract_version": CONTRACT_VERSION, "round": args.round,
                "layout_order": [], "gates": dict.fromkeys(("truth", "analysis", "raw", "cleanup", "samples"), False),
                "runner": _code_identity(), "namespace": args.namespace,
                "run_id": f"four-layout-r{args.round}-{uuid.uuid4().hex}",
                "cache_state": "query_warmup_1_no_os_cache_drop",
                "command": list(getattr(args, "command", [Path(sys.executable).name, *sys.argv])),
                "measurements": {"S01-S05": args.measurements, "S06": args.document_page_measurements,
                                 "query_workers": args.query_workers}}
    artifact_bytes = {}
    try:
        order = parse_layout_order(args.layout_order, args.round)
        _validate_execution_args(args)
        manifest["layout_order"] = list(order)
        rows, source_truth, catalog, truth, identity = load_inputs(args.input, args.truth)
        manifest["input"] = identity
        og, ch = make_adapters(args)
        manifest["environment"] = collect_environment(args, (og, ch))
        adapters = {"og_json": og, "og_jsonb": og, "ch_string": ch, "ch_native": ch}
        blocks = [rows[index:index + source_truth["block_size"]]
                  for index in range(0, len(rows), source_truth["block_size"])]
        formal_query_log_ids = set()
        for layout in order:
            adapter = adapters[layout]
            result = {"status": "failed", "layout": layout, "round": args.round,
                      "layout_order": list(order), "input": identity, "environment": manifest["environment"],
                      "runner": manifest["runner"], "run_id": manifest["run_id"],
                      "namespace": args.namespace, "cache_state": manifest["cache_state"],
                      "command": manifest["command"], "measurements": manifest["measurements"]}
            owned = False
            try:
                created = adapter.create_layout(layout, 32) if layout.startswith("ch_") else adapter.create_layout(layout)
                owned = True
                ddl = created["ddl"]
                result["ddl"] = {"identity": created, "sha256": hashlib.sha256(ddl.encode()).hexdigest()}
                result["queries"] = {"sha256": hashlib.sha256(canonical_bytes(
                    {query: adapter.query_sql(layout, query) for query in QUERY_IDS})).hexdigest()}
                result["ingest"] = {"blocks": [adapter.insert_block(layout, block) for block in blocks]}
                result["maintenance"] = (adapter.finish_maintenance(layout, args.maintenance_timeout_seconds)
                                         if layout.startswith("ch_") else adapter.finish_maintenance(layout))
                if result["maintenance"].get("completed") is False:
                    raise RuntimeError("maintenance did not complete")
                result["query_stage"] = run_query_stage(adapter, layout, catalog, truth,
                                                         args.measurements, args.document_page_measurements,
                                                         args.query_workers)
                if layout.startswith("ch_"):
                    ids = [sample.get("query_log_id") for sample in result["query_stage"]["samples"]]
                    if len(set(ids)) != len(ids) or formal_query_log_ids.intersection(ids):
                        raise RuntimeError("formal ClickHouse query log IDs are not globally unique")
                    formal_query_log_ids.update(ids)
                result["analysis_recovery"] = adapter.verify_analysis(layout, source_truth)
                result["raw_recovery"] = adapter.verify_raw(layout, source_truth)
                if not result["analysis_recovery"].get("ok"):
                    raise RuntimeError("analysis recovery failed")
                if not result["raw_recovery"].get("ok"):
                    raise RuntimeError("raw recovery failed")
                result["storage"] = adapter.collect_storage(layout)
                result["status"] = "complete"
            except Exception as error:
                result["error"] = {"message": str(error), "type": type(error).__name__}
                raise
            finally:
                try:
                    result["cleanup"] = adapter.cleanup(layout) if owned else {"removed": False, "skipped": True}
                except Exception as cleanup_error:
                    result["cleanup"] = {"removed": False, "error": {"message": str(cleanup_error), "type": type(cleanup_error).__name__}}
                    result["status"] = "failed"
                if result["cleanup"].get("removed") is not True:
                    result["status"] = "failed"
                name = f"result-{layout}.json"
                content = canonical_bytes(result) + b"\n"
                artifact_bytes[name] = content
            if result["status"] != "complete" or result["cleanup"].get("removed") is not True:
                raise RuntimeError(result.get("error", {}).get("message", "layout cleanup failed"))
        manifest["gates"] = dict.fromkeys(("truth", "analysis", "raw", "cleanup", "samples"), True)
        write_manifest_last(output, manifest, artifact_bytes)
    except Exception as error:
        try:
            existing = read_json(output / "run-manifest.json", "run manifest")
        except ValueError:
            existing = None
        if not isinstance(existing, dict) or existing.get("status") != "failed":
            write_failed_manifest(output, manifest, artifact_bytes, error)
        raise


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--layout-order", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--opengauss-container", required=True)
    parser.add_argument("--opengauss-port", default=15432, type=int)
    parser.add_argument("--clickhouse-container", required=True)
    parser.add_argument("--clickhouse-port", default=18123, type=int)
    parser.add_argument("--measurements", default=100, type=int)
    parser.add_argument("--document-page-measurements", default=20, type=int)
    parser.add_argument("--query-workers", default=2, type=int)
    parser.add_argument("--maintenance-timeout-seconds", default=120, type=int)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    arguments.command = [Path(sys.executable).name, *sys.argv]
    execute(arguments)
