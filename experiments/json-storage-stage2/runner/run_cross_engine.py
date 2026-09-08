#!/usr/bin/env python3
"""执行阶段二单引擎单轮 residual layout 实验。"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import common
import clickhouse
import opengauss


QUERY_IDS = ("Q01", "Q02", "Q03", "Q04", "Q05")
CONCURRENT_QUERY_IDS = ("Q01", "Q02", "Q03", "Q05")
FROZEN_SOURCE_PATH = Path("/home/omm/work/agent-trace/trace-synthesis/output/whowhen-pro/traces-00001.jsonl").resolve()
FROZEN_SOURCE_SHA256 = "3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683"
FROZEN_UPSTREAM_MANIFEST_SHA256 = "f46bbe843c5578faea9ddfb5e8eb3aac8b6dc4c2f4fb89beabab503043505e38"
ENGINE_LAYOUTS = {
    "opengauss": opengauss.LAYOUTS,
    "clickhouse": clickhouse.LAYOUTS,
}


def parse_layout_order(value, engine):
    """解析并验证一个引擎的三个 layout 恰好各出现一次。"""
    if engine not in ENGINE_LAYOUTS:
        raise ValueError(f"unsupported engine: {engine}")
    if not isinstance(value, str):
        raise ValueError("layout order must be a comma-separated string")
    layouts = tuple(item.strip() for item in value.split(",") if item.strip())
    expected = ENGINE_LAYOUTS[engine]
    if len(layouts) != len(expected) or set(layouts) != set(expected):
        raise ValueError(f"layout order must contain each {engine} layout once")
    return layouts


def _close_connection(connection):
    """关闭 adapter 返回的连接对象；测试替身可省略 close。"""
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _expected_query(truth, query_id, watermark):
    """返回指定水位的独立 truth 摘要，不从数据库结果派生期望值。"""
    try:
        expected = truth[str(watermark)][query_id]
    except (KeyError, TypeError) as error:
        raise ValueError(f"missing concurrent truth for {query_id} watermark {watermark}") from error
    if not isinstance(expected, str):
        raise ValueError(f"invalid concurrent truth for {query_id} watermark {watermark}")
    return expected


def _concurrent_params(truth, query_id):
    """返回并发查询的固定 truth 参数，不允许以空参数执行。"""
    try:
        params = truth["parameters"][query_id]
    except (KeyError, TypeError) as error:
        raise ValueError(f"missing concurrent parameters for {query_id}") from error
    if not isinstance(params, dict) or not params:
        raise ValueError(f"invalid concurrent parameters for {query_id}")
    return dict(params)


def _sample(adapter, connection, query_id, params, watermark, expected_digest, phase):
    """执行一条查询并在计时外核对摘要，返回不含 payload 的样本。"""
    actual = adapter.execute_query(connection, query_id, params, watermark)
    if not isinstance(actual, dict):
        raise RuntimeError(f"invalid query envelope for {query_id}")
    actual_digest = actual.get("result_sha256")
    matches_truth = actual_digest == expected_digest
    sample = {
        "latency_ms": actual.get("latency_ms"),
        "matches_truth": matches_truth,
        "ok": matches_truth,
        "phase": phase,
        "query_id": query_id,
        "row_count": actual.get("row_count"),
        "watermark": watermark,
    }
    if "query_log" in actual:
        sample["query_log"] = actual["query_log"]
    if not matches_truth:
        raise RuntimeError(f"truth mismatch: {query_id} watermark {watermark}")
    return sample


def _summaries(samples):
    """按查询 ID 汇总所有已通过的正式样本。"""
    grouped = defaultdict(list)
    for sample in samples:
        grouped[sample["query_id"]].append(sample)
    return {query_id: common.summarize_samples(grouped[query_id]) for query_id in sorted(grouped)}


def run_ingest_with_queries(adapter, blocks, truth, query_workers):
    """持续写入 block，并在第五个成功水位后执行带屏障的并发查询。"""
    if not isinstance(query_workers, int) or isinstance(query_workers, bool) or query_workers <= 0:
        raise ValueError("query_workers must be positive")
    if len(blocks) < 5:
        raise ValueError("at least five blocks are required before concurrent queries")
    watermark_lock = threading.Lock()
    latest_watermark = None
    samples = []
    sample_lock = threading.Lock()
    errors = []
    error_lock = threading.Lock()
    stop = threading.Event()
    barrier = threading.Barrier(query_workers + 1)
    workers = []
    first_samples = [threading.Event() for _ in range(query_workers)]

    def worker(worker_number):
        connection = None
        try:
            connection = adapter.connect_worker()
            barrier.wait()
            while not stop.is_set():
                with watermark_lock:
                    watermark = latest_watermark
                if watermark is None:
                    continue
                for query_id in CONCURRENT_QUERY_IDS:
                    expected = _expected_query(truth, query_id, watermark)
                    sample = _sample(
                        adapter, connection, query_id, _concurrent_params(truth, query_id),
                        watermark, expected, "concurrent",
                    )
                    with sample_lock:
                        samples.append(sample)
                    if query_id == CONCURRENT_QUERY_IDS[0]:
                        first_samples[worker_number].set()
                if stop.is_set():
                    break
        except Exception as error:
            with error_lock:
                errors.append(error)
            stop.set()
            barrier.abort()
            for event in first_samples:
                event.set()
        finally:
            _close_connection(connection)

    block_metrics = []
    started_workers = False
    try:
        for index, (watermark, rows) in enumerate(blocks, start=1):
            if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark <= 0:
                raise ValueError("block watermark must be positive")
            started = time.perf_counter()
            inserted = adapter.insert_block(rows)
            wall_time_ms = (time.perf_counter() - started) * 1000
            if not isinstance(inserted, dict) or inserted.get("rows") != len(rows):
                raise RuntimeError(f"insert row count mismatch at watermark {watermark}")
            input_bytes = sum(len(row["raw_event"].encode("utf-8")) for row in rows)
            seconds = wall_time_ms / 1000
            block_metrics.append({
                "input_bytes": input_bytes,
                "mib_per_second": input_bytes / (1024 * 1024) / seconds if seconds else 0.0,
                "rows": len(rows),
                "rows_per_second": len(rows) / seconds if seconds else 0.0,
                "visible_after_commit": True,
                "wall_time_ms": wall_time_ms,
                "watermark": watermark,
            })
            with watermark_lock:
                latest_watermark = watermark
            if index == 5:
                workers = [
                    threading.Thread(target=worker, args=(number,), name=f"stage2-query-{number}")
                    for number in range(query_workers)
                ]
                for thread in workers:
                    thread.start()
                started_workers = True
                try:
                    barrier.wait()
                except threading.BrokenBarrierError as error:
                    with error_lock:
                        if errors:
                            raise errors[0]
                    raise RuntimeError("query worker startup failed") from error
                for event in first_samples:
                    event.wait()
            with error_lock:
                if errors:
                    raise errors[0]
    except Exception:
        stop.set()
        raise
    finally:
        if started_workers:
            stop.set()
            for thread in workers:
                thread.join()
    with error_lock:
        if errors:
            raise errors[0]
    if not started_workers:
        raise RuntimeError("query workers were not started")
    observed = {sample["query_id"] for sample in samples}
    missing = sorted(set(CONCURRENT_QUERY_IDS) - observed)
    if missing:
        raise RuntimeError("missing concurrent samples: " + ",".join(missing))
    return {
        "block_summary": common.summarize_samples([
            {"ok": True, "latency_ms": metric["wall_time_ms"] or sys.float_info.min}
            for metric in block_metrics
        ]),
        "blocks": block_metrics,
        "query_workers": query_workers,
        "samples": samples,
        "summary": _summaries(samples),
    }


def _static_expected(truth, query_id, watermark):
    """返回静态查询的完整 truth 摘要与参数。"""
    try:
        expected = truth["queries"][query_id][str(watermark)]
        params = {**truth["parameters"][query_id], "key_map": truth.get("key_map", {})}
    except (KeyError, TypeError) as error:
        raise ValueError(f"missing static truth for {query_id} watermark {watermark}") from error
    if not isinstance(expected, dict) or not isinstance(expected.get("result_sha256"), str):
        raise ValueError(f"invalid static truth for {query_id} watermark {watermark}")
    return params, expected["result_sha256"]


def run_static_queries(adapter, layout, truth, measurements, watermark=None):
    """复用一条连接执行每条查询的一次预热和 N 次正式测量。"""
    if not isinstance(measurements, int) or isinstance(measurements, bool) or measurements <= 0:
        raise ValueError("measurements must be positive")
    if watermark is None:
        try:
            watermark = max(int(value) for value in truth["queries"]["Q01"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("static watermark is required") from error
    connection = adapter.connect_worker()
    samples = []
    plans = {}
    try:
        for query_id in QUERY_IDS:
            params, expected = _static_expected(truth, query_id, watermark)
            _sample(adapter, connection, query_id, params, watermark, expected, "warmup")
            for _ in range(measurements):
                samples.append(_sample(adapter, connection, query_id, params, watermark, expected, "static"))
            plans[query_id] = adapter.collect_plan(query_id, params, watermark)
    finally:
        _close_connection(connection)
    return {"plans": plans, "samples": samples, "summary": _summaries(samples), "watermark": watermark}


class _LayoutSession:
    """将真实 adapter 的 layout 参数绑定为 runner 阶段接口。"""

    def __init__(self, adapter, layout):
        self.adapter = adapter
        self.layout = layout

    def insert_block(self, rows):
        """写入当前 layout 的一个 block。"""
        return self.adapter.insert_block(self.layout, rows)

    def connect_worker(self):
        """建立当前阶段可复用的独立连接。"""
        return self.adapter.connect_worker()

    def execute_query(self, connection, query_id, params, watermark):
        """执行当前 layout 的一条查询。"""
        return self.adapter.execute_query(connection, self.layout, query_id, params, watermark)

    def collect_plan(self, query_id, params, watermark):
        """采集当前 layout 的查询计划。"""
        return self.adapter.collect_plan(self.layout, query_id, params, watermark)


def _adapter_for(args):
    """根据 CLI engine 创建实际数据库 adapter。"""
    if args.engine == "opengauss":
        return opengauss.OpenGaussAdapter(args.host, args.port, args.container_name, args.namespace)
    if args.engine == "clickhouse":
        return clickhouse.ClickHouseAdapter(args.host, args.port, args.container_name, args.namespace)
    raise ValueError(f"unsupported engine: {args.engine}")


def _sha256_file(path):
    """返回 runner 与查询 catalog 使用的文件摘要。"""
    return common.file_identity(path)["sha256"]


def _utc_now():
    """返回 manifest 使用的 UTC 时间文本。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _git_identity(path):
    """读取本地 HEAD 与已存在的 origin/main，不更新远端引用。"""
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"repository directory does not exist: {path}")
    result = {"path": str(path), "head": "unavailable", "remote_main": "unavailable"}
    for field, command in (("head", ["git", "rev-parse", "HEAD"]), ("remote_main", ["git", "rev-parse", "origin/main"])):
        completed = subprocess.run(command, cwd=path, capture_output=True, text=True, check=False)
        if completed.returncode == 0:
            result[field] = completed.stdout.strip()
    return result


def _workspace_root(repository):
    """从 Git common directory 或父目录定位包含三个仓的工作区根目录。"""
    repository = Path(repository)
    completed = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repository, capture_output=True, text=True, check=False,
    )
    candidates = []
    if completed.returncode == 0:
        common_directory = Path(completed.stdout.strip())
        candidates.append(common_directory.parent.parent)
    candidates.extend(repository.parents)
    for candidate in candidates:
        if all((candidate / name).is_dir() for name in ("exporter_demo", "trace-synthesis", "agent-trace-research")):
            return candidate
    raise ValueError(f"workspace root with required repositories was not found from: {repository}")


def _safe_container_identity(container_name):
    """读取不含环境变量的容器、镜像、端口和资源限制身份。"""
    fields = "{{.Config.Image}}|{{.Image}}|{{json .HostConfig.PortBindings}}|{{.HostConfig.Memory}}|{{.HostConfig.NanoCpus}}|{{.State.Running}}"
    completed = subprocess.run(
        ["docker", "inspect", container_name, "--format", fields],
        capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        return {"container": container_name, "status": "unavailable"}
    image, image_id, ports, memory, cpu, running = completed.stdout.strip().split("|", 5)
    image_completed = subprocess.run(
        ["docker", "image", "inspect", image_id, "--format", "{{json .RepoDigests}}"],
        capture_output=True, text=True, check=False,
    )
    if image_completed.returncode != 0:
        raise RuntimeError(f"unable to inspect container image repo digests: {container_name}")
    try:
        repo_digests = json.loads(image_completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid container image repo digests: {container_name}") from error
    if not isinstance(repo_digests, list) or not repo_digests or not all(isinstance(value, str) for value in repo_digests):
        raise RuntimeError(f"missing container image repo digests: {container_name}")
    return {
        "container": container_name,
        "cpu_nanocpus": int(cpu),
        "image": image,
        "image_id": image_id,
        "memory_bytes": int(memory),
        "ports": ports,
        "repo_digests": sorted(repo_digests),
        "running": running == "true",
    }


def _database_version(args):
    """读取已运行容器的数据库版本，不读取或记录任何认证环境变量。"""
    if args.engine == "clickhouse":
        command = ["curl", "-fsS", f"http://{args.host}:{args.port}/?query=SELECT%20version()"]
    else:
        command = [
            "docker", "exec", "-u", "omm", args.container_name, "bash", "-lc",
            "/usr/local/opengauss/bin/gsql -d postgres -Atc 'SELECT version()'",
        ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _collect_environment(args):
    """收集 run manifest 所需的仓、容器和宿主只读身份。"""
    repository = Path(__file__).resolve().parents[3]
    root = _workspace_root(repository)
    memory_pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    disk = shutil.disk_usage(repository)
    return {
        "container": {**_safe_container_identity(args.container_name), "database_version": _database_version(args)},
        "host": {
            "cpu_count": os.cpu_count(),
            "disk_free_bytes": disk.free,
            "disk_total_bytes": disk.total,
            "memory_bytes": memory_pages * page_size,
        },
        "repositories": {
            "research": _git_identity(repository),
            "exporter_demo": _git_identity(root / "exporter_demo"),
            "trace_synthesis": _git_identity(root / "trace-synthesis"),
        },
    }


def _write_json(path, value):
    """原子写入一个 result 或 manifest JSON 文件。"""
    common.write_atomically(path, common.canonical_bytes(value) + b"\n")


def _publish_manifest(output_dir, manifest, status, artifacts, error=None):
    """在所有 result 已落盘后发布 complete 或 failed 顶层 manifest。"""
    result = common.contract_fields(manifest)
    result["artifacts"] = artifacts
    result["status"] = status
    if error is not None:
        result["error"] = {"message": str(error), "type": type(error).__name__}
    _write_json(Path(output_dir) / "run-manifest.json", result)


def _validate_args(args, truth):
    """验证 CLI 正数、正式 block 契约和输入 truth 的一致性。"""
    if not isinstance(args.round, int) or isinstance(args.round, bool) or args.round <= 0:
        raise ValueError("round must be positive")
    for name in ("measurements", "block_size", "query_workers", "maintenance_timeout_seconds"):
        value = getattr(args, name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be positive")
    if args.block_size != truth.get("block_size"):
        raise ValueError("CLI block size must match input truth manifest")


def _build_blocks(rows, watermarks):
    """按 truth 水位的相邻边界切分输入，保留最后不足 block size 的 block。"""
    blocks = []
    previous = 0
    for watermark in watermarks:
        if not isinstance(watermark, int) or isinstance(watermark, bool) or watermark <= previous:
            raise ValueError("truth watermarks must be strictly increasing positive integers")
        if watermark > len(rows):
            raise ValueError("truth watermark exceeds dataset rows")
        blocks.append((watermark, rows[previous:watermark]))
        previous = watermark
    if previous != len(rows):
        raise ValueError("truth final watermark must equal dataset rows")
    return blocks


def _input_lineage(input_dir, rows, truth):
    """校验 generator、audit 与冻结上游 lineage，并返回顶层 input 记录。"""
    input_dir = Path(input_dir)
    generator_manifest = common.read_json(input_dir / "run-manifest.json", "input run manifest")
    if generator_manifest.get("status") != "complete":
        raise ValueError("input run manifest status must be complete")
    if generator_manifest.get("data_path") != common.DATA_PATH:
        raise ValueError("input run manifest data path mismatch")
    dataset = common.file_identity(input_dir / "dataset.jsonl")
    truth_identity = common.file_identity(input_dir / "truth-manifest.json")
    artifacts = generator_manifest.get("artifacts")
    if not isinstance(artifacts, dict) or artifacts.get("dataset.jsonl") != dataset or artifacts.get("truth-manifest.json") != truth_identity:
        raise ValueError("generator artifact identity mismatch")
    for field, actual in (
        ("record_count", len(rows)),
        ("block_size", truth.get("block_size")),
        ("block_count", truth.get("block_count")),
        ("watermarks", truth.get("watermarks")),
    ):
        if generator_manifest.get(field) != actual:
            raise ValueError(f"generator {field} mismatch")
    source_input = generator_manifest.get("input")
    audit = generator_manifest.get("audit")
    if not isinstance(source_input, dict) or not isinstance(audit, dict):
        raise ValueError("generator lineage missing")
    source_value = source_input.get("path")
    if not isinstance(source_value, str) or Path(source_value).resolve() != FROZEN_SOURCE_PATH:
        raise ValueError("frozen source input mismatch")
    if source_input.get("sha256") != FROZEN_SOURCE_SHA256:
        raise ValueError("frozen source input SHA-256 mismatch")
    source_path = Path(source_value)
    if common.file_identity(source_path).get("sha256") != source_input.get("sha256"):
        raise ValueError("source input identity mismatch")
    audit_path = Path(audit.get("path", ""))
    if common.file_identity(audit_path).get("sha256") != audit.get("sha256"):
        raise ValueError("audit artifact identity mismatch")
    audit_manifest = common.read_json(audit_path.parent / "run-manifest.json", "audit run manifest")
    if audit_manifest.get("status") != "complete" or audit_manifest.get("data_path") != common.DATA_PATH:
        raise ValueError("audit run manifest mismatch")
    if audit_manifest.get("artifacts", {}).get("audit.json") != common.file_identity(audit_path):
        raise ValueError("audit artifact identity mismatch")
    audit_input = audit_manifest.get("input")
    if not isinstance(audit_input, dict) or audit_input.get("path") != source_input.get("path") or audit_input.get("sha256") != source_input.get("sha256"):
        raise ValueError("audit source input mismatch")
    upstream_manifest = audit_input.get("upstream_manifest")
    if not isinstance(upstream_manifest, dict):
        raise ValueError("upstream manifest lineage missing")
    if upstream_manifest.get("sha256") != FROZEN_UPSTREAM_MANIFEST_SHA256:
        raise ValueError("frozen upstream manifest SHA-256 mismatch")
    upstream_path = Path(upstream_manifest.get("path", ""))
    if common.file_identity(upstream_path).get("sha256") != upstream_manifest.get("sha256"):
        raise ValueError("upstream manifest identity mismatch")
    upstream = common.read_json(upstream_path, "upstream manifest")
    if upstream.get("status") != "complete" or upstream.get("seed") != 42:
        raise ValueError("upstream seed must be 42")
    source_shard = next((
        shard for shard in upstream.get("shards", [])
        if isinstance(shard, dict) and shard.get("file") == source_path.name
    ), None)
    if not source_shard or source_shard.get("sha256") != source_input.get("sha256") or source_shard.get("span_count") != len(rows):
        raise ValueError("upstream source shard mismatch")
    return {
        "audit": {"path": str(audit_path), "sha256": audit["sha256"]},
        "block_count": truth["block_count"],
        "block_size": truth["block_size"],
        "dataset": dataset,
        "record_count": len(rows),
        "seed": upstream["seed"],
        "source_input": {"path": str(source_path), "sha256": source_input["sha256"]},
        "truth": truth_identity,
        "upstream_manifest": {"path": str(upstream_path), "sha256": upstream_manifest["sha256"]},
        "watermarks": list(truth["watermarks"]),
    }


def execute(args):
    """执行单引擎单轮所有 layout，并始终在最后发布诊断或完成 manifest。"""
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run-manifest.json").unlink(missing_ok=True)
    manifest = {
        "cache_state": "query_warmup_1_no_os_cache_drop",
        "command": shlex.join([Path(sys.executable).name, *sys.argv]),
        "engine": getattr(args, "engine", "unavailable"),
        "gates": {"all_samples_successful": False, "cleanup": False, "correctness": False, "raw_recovery": False},
        "layout_order": [],
        "round": getattr(args, "round", "unavailable"),
        "run_id": f"{getattr(args, 'engine', 'run')}-r{getattr(args, 'round', 'unknown')}-{uuid.uuid4().hex}",
        "runner": {"path": "runner/run_cross_engine.py", "sha256": _sha256_file(__file__)},
        "started_at_utc": _utc_now(),
    }
    artifacts = {}
    try:
        rows, truth = common.verify_input(args.input)
        _validate_args(args, truth)
        layout_order = parse_layout_order(args.layout_order, args.engine)
        manifest["layout_order"] = list(layout_order)
        manifest["input"] = _input_lineage(args.input, rows, truth)
        manifest["environment"] = _collect_environment(args)
        adapter = _adapter_for(args)
        blocks = _build_blocks(rows, truth["watermarks"])
        concurrent_truth = {
            str(watermark): {
                query_id: truth["queries"][query_id][str(watermark)]["result_sha256"]
                for query_id in CONCURRENT_QUERY_IDS
            }
            for watermark in truth["watermarks"]
        }
        concurrent_truth["parameters"] = {
            query_id: {**truth["parameters"][query_id], "key_map": truth.get("key_map", {})}
            for query_id in CONCURRENT_QUERY_IDS
        }
        all_layouts_ok = True
        for layout in layout_order:
            session = _LayoutSession(adapter, layout)
            layout_result = {"layout": layout, "status": "failed"}
            cleanup = {"removed": False}
            try:
                created = adapter.create_layout(layout, truth["native_json"]["path_budget"])
                layout_result["ddl"] = {
                    "sha256": hashlib.sha256(created["ddl"].encode("utf-8")).hexdigest(),
                    "identity": created,
                }
                layout_result["queries"] = {
                    "sha256": hashlib.sha256(common.canonical_bytes({
                        query_id: adapter.query_sql(layout, query_id) for query_id in QUERY_IDS
                    })).hexdigest(),
                }
                layout_result["runner"] = dict(manifest["runner"])
                layout_result["ingest"] = run_ingest_with_queries(
                    session, blocks, concurrent_truth, args.query_workers
                )
                layout_result["maintenance"] = adapter.finish_maintenance(layout, args.maintenance_timeout_seconds)
                if layout_result["maintenance"].get("completed") is False:
                    raise RuntimeError("maintenance did not complete")
                layout_result["static_queries"] = run_static_queries(
                    session, layout, truth, args.measurements, watermark=truth["watermarks"][-1]
                )
                layout_result["analysis_correctness"] = adapter.verify_analysis(layout, truth)
                layout_result["raw_recovery"] = adapter.verify_raw(layout, truth)
                layout_result["storage"] = adapter.collect_storage(layout)
                if not layout_result["analysis_correctness"].get("ok"):
                    raise RuntimeError("analysis correctness gate failed")
                if not layout_result["raw_recovery"].get("ok"):
                    raise RuntimeError("raw recovery gate failed")
                layout_result["status"] = "complete"
            except Exception as error:
                layout_result["error"] = {"message": str(error), "type": type(error).__name__}
                all_layouts_ok = False
                raise
            finally:
                try:
                    cleanup = adapter.cleanup(layout)
                except Exception as cleanup_error:
                    cleanup = {"error": {"message": str(cleanup_error), "type": type(cleanup_error).__name__}, "removed": False}
                    all_layouts_ok = False
                    if layout_result.get("status") == "complete":
                        layout_result["status"] = "failed"
                        layout_result["error"] = cleanup["error"]
                layout_result["cleanup"] = cleanup
                result_path = output_dir / layout / "result.json"
                result_path.parent.mkdir(parents=True, exist_ok=True)
                _write_json(result_path, layout_result)
                artifacts[str(result_path.relative_to(output_dir))] = common.file_identity(result_path)
            if layout_result["status"] != "complete":
                raise RuntimeError("layout cleanup failed")
        manifest["gates"] = {
            "all_samples_successful": all_layouts_ok,
            "cleanup": all_layouts_ok,
            "correctness": all_layouts_ok,
            "raw_recovery": all_layouts_ok,
        }
        manifest["ended_at_utc"] = _utc_now()
        _publish_manifest(output_dir, manifest, "complete", artifacts)
    except Exception as error:
        manifest["ended_at_utc"] = _utc_now()
        _publish_manifest(output_dir, manifest, "failed", artifacts, error)
        raise


def parse_args():
    """解析单轮 runner CLI 参数和引擎默认端口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--engine", required=True, choices=tuple(ENGINE_LAYOUTS))
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--round", required=True, type=int)
    parser.add_argument("--layout-order", required=True)
    parser.add_argument("--measurements", default=100, type=int)
    parser.add_argument("--block-size", default=256, type=int)
    parser.add_argument("--query-workers", default=2, type=int)
    parser.add_argument("--maintenance-timeout-seconds", default=120, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.port is None:
        args.port = 15432 if args.engine == "opengauss" else 18123
    return args


def main():
    """运行 CLI 并以非零退出码暴露任意失败门禁。"""
    try:
        execute(parse_args())
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
