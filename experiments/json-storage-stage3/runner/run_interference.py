#!/usr/bin/env python3
"""执行固定 offered-load 前台查询与独立干扰阶段。"""

import json
import inspect
import math
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path

from common import AccessEvidence, BlockResult, CleanupResult, QuerySpec
from run_clickhouse_part_states import capture_part_state
from run_layout_matrix import QuerySample, QueryTruth, measure_query, write_manifest_atomic


P99_MINIMUM_SUCCESSES = 1_000
QUERY_EVIDENCE_BATCH_SIZE = 200
TERMINAL_ABORT_EXIT_CODE = 70
TERMINATION_GRACE_SECONDS = 0.1
QUERY_STREAMS = {
    "list": ("list", None),
    "preview": ("preview", None),
    "detail_2m": ("detail", "detail:text_2m"),
    "trace_long": ("trace", "trace:p95"),
    "batch_loop": ("batch", "batch:main"),
}


@dataclass(frozen=True)
class LoadSchedule:
    """描述一个阶段内单条请求流的固定到达或持续循环配置。"""

    name: str
    rate_per_second: float | None
    duration_seconds: float
    workers: int
    timeout_seconds: float
    late_tolerance_seconds: float = 0.05
    mode: str = "fixed"

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("schedule name must be non-empty")
        if self.mode not in {"fixed", "continuous"}:
            raise ValueError("schedule mode must be fixed or continuous")
        if (
            not isinstance(self.duration_seconds, (int, float))
            or isinstance(self.duration_seconds, bool)
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be positive")
        if not isinstance(self.workers, int) or isinstance(self.workers, bool) or self.workers <= 0:
            raise ValueError("workers must be positive")
        for name, value in (
            ("timeout_seconds", self.timeout_seconds),
            ("late_tolerance_seconds", self.late_tolerance_seconds),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
                or (name == "timeout_seconds" and value == 0)
            ):
                requirement = "positive" if name == "timeout_seconds" else "non-negative"
                raise ValueError(f"{name} must be {requirement}")
        if self.mode == "fixed" and (
            not isinstance(self.rate_per_second, (int, float))
            or isinstance(self.rate_per_second, bool)
            or self.rate_per_second <= 0
        ):
            raise ValueError("fixed schedule rate_per_second must be positive")
        if self.mode == "continuous" and self.rate_per_second is not None:
            raise ValueError("continuous schedule does not use rate_per_second")


@dataclass(frozen=True)
class InterferencePhase:
    """保存一个安静或单类干扰阶段的冻结配置。"""

    name: str
    interference_stream: str | None
    rate_per_second: float | None = None
    mode: str = "fixed"
    workers: int = 1
    warmup_seconds: float = 30.0
    measurement_seconds: float = 300.0


@dataclass(frozen=True)
class DeadlineTarget:
    """声明会在 deadline 到达或收到 cancellation 后结束的请求目标。"""

    operation: object

    def __post_init__(self):
        if not callable(self.operation):
            raise ValueError("deadline target operation must be callable")
        try:
            inspect.signature(self.operation).bind(0.0, threading.Event())
        except (TypeError, ValueError) as error:
            raise ValueError(
                "deadline target operation must accept deadline and cancellation arguments"
            ) from error

    def __call__(self, deadline, cancellation):
        return self.operation(deadline, cancellation)


FIXED_PHASES = (
    InterferencePhase("quiet", None),
    InterferencePhase("detail_2m", "detail_2m", 1.0),
    InterferencePhase("trace_long", "trace_long", 0.2),
    InterferencePhase("batch_loop", "batch_loop", None, "continuous"),
    InterferencePhase("continuous_ingest", "continuous_ingest", 1.0),
)


@dataclass(frozen=True)
class RequestSample:
    """保存固定到达请求的调度、完成状态和目标返回证据。"""

    stream: str
    sequence: int
    scheduled_offset_seconds: float
    started_offset_seconds: float | None
    completed_offset_seconds: float | None
    duration_ms: float | None
    application_ready_ms: float | None
    status: str
    late_by_ms: float
    sample: object | None = None
    error: str | None = None


@dataclass(frozen=True)
class LoadResult:
    """保存一个请求流的阶段 wall time、显式计数和逐请求样本。"""

    name: str
    phase_origin: float
    phase_finished: float
    phase_wall_seconds: float
    offered_rate_per_second: float | None
    scheduled_requests: int
    started_requests: int
    completed_requests: int
    successful_requests: int
    failed_requests: int
    timed_out_requests: int
    dropped_requests: int
    late_requests: int
    samples: tuple[RequestSample, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InterferenceRunResult:
    """返回总 manifest 和每个独立阶段的最终 manifest。"""

    manifest: dict[str, object]
    phases: tuple[dict[str, object], ...]


@dataclass
class _Invocation:
    sequence: int
    scheduled_at: float
    started_at: float | None = None
    completed_at: float | None = None
    sample: object | None = None
    error: Exception | None = None
    deadline_at: float | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Pending:
    invocation: _Invocation
    future: object
    emitted: bool = False


def fixed_phase_schedules(phase, *, measurement):
    """返回列表、preview 与至多一类干扰的冻结阶段配置。"""
    if isinstance(phase, str):
        matches = [item for item in FIXED_PHASES if item.name == phase]
        if not matches:
            raise ValueError(f"unsupported interference phase: {phase}")
        phase = matches[0]
    if not isinstance(phase, InterferencePhase):
        raise ValueError("phase must be an InterferencePhase")
    duration = phase.measurement_seconds if measurement else phase.warmup_seconds
    schedules = {
        name: LoadSchedule(name, 20.0, duration, 2, timeout_seconds=30.0)
        for name in ("list", "preview")
    }
    if phase.interference_stream is not None:
        schedules[phase.interference_stream] = LoadSchedule(
            phase.interference_stream, phase.rate_per_second, duration, phase.workers,
            timeout_seconds=30.0, mode=phase.mode,
        )
    return schedules


def validate_formal_phase_coverage(phase, warmup, measured):
    """核对正式阶段各请求流实际覆盖冻结的 30/300 秒窗口。"""
    coverage = {}
    for name, results, measurement in (
        ("warmup", warmup, False), ("measurement", measured, True),
    ):
        schedules = fixed_phase_schedules(phase, measurement=measurement)
        if set(results) != set(schedules) or any(
            not isinstance(result, LoadResult) for result in results.values()
        ):
            raise RuntimeError(f"{name} results do not cover fixed schedules")
        required = phase.measurement_seconds if measurement else phase.warmup_seconds
        actual = min(
            min(result.phase_wall_seconds, result.phase_finished - result.phase_origin)
            for result in results.values()
        )
        if actual < required:
            raise RuntimeError(f"{name} actual duration is shorter than {required} seconds")
        coverage[f"{name}_actual_seconds"] = actual
    return coverage


def build_query_target(adapter, query: QuerySpec, truth: QueryTruth):
    """构造复用 Task 4 应用可用和完整响应 bytes 门禁的查询目标。"""
    if not isinstance(query, QuerySpec) or not isinstance(truth, QueryTruth):
        raise ValueError("query target requires QuerySpec and QueryTruth")
    def target(deadline, cancellation):
        if cancellation.is_set():
            raise TimeoutError("query target deadline expired before execution")
        return measure_query(adapter, query, truth)

    return DeadlineTarget(target)


def _invoke(target, invocation, clock, timeout_seconds):
    invocation.started_at = clock()
    invocation.deadline_at = invocation.started_at + timeout_seconds
    try:
        invocation.sample = target(invocation.deadline_at, invocation.cancellation)
    except Exception as error:
        invocation.error = error
    finally:
        invocation.completed_at = clock()
    return invocation


def _request_sample(schedule, invocation, status=None, error=None):
    started = invocation.started_at
    completed = invocation.completed_at
    duration_ms = (
        (completed - started) * 1000
        if started is not None and completed is not None else None
    )
    late_by = max(0.0, ((started or invocation.scheduled_at) - invocation.scheduled_at) * 1000)
    sample = invocation.sample
    if status is None:
        if isinstance(invocation.error, TimeoutError) or (
            duration_ms is not None and duration_ms > schedule.timeout_seconds * 1000
        ):
            status = "timed_out"
        elif invocation.error is not None:
            status = "failed"
        else:
            status = "success" if getattr(sample, "status", "success") == "success" else "failed"
    if error is None:
        if invocation.error is not None:
            error = str(invocation.error) or type(invocation.error).__name__
        elif status == "failed":
            error = getattr(sample, "error", None) or "target returned failed status"
        elif status == "timed_out":
            error = "request exceeded timeout or phase drain boundary"
    application_ready_ms = getattr(sample, "application_ready_ms", duration_ms)
    return RequestSample(
        schedule.name, invocation.sequence,
        invocation.scheduled_at, started, completed, duration_ms,
        application_ready_ms if isinstance(application_ready_ms, (int, float)) else duration_ms,
        status, late_by, sample, error,
    )


def _load_result(schedule, origin, finished, samples):
    samples = tuple(sorted(samples, key=lambda item: item.sequence))
    return LoadResult(
        schedule.name, origin, finished, finished - origin, schedule.rate_per_second,
        len(samples), sum(item.started_offset_seconds is not None for item in samples),
        sum(item.completed_offset_seconds is not None for item in samples),
        sum(item.status == "success" for item in samples),
        sum(item.status == "failed" for item in samples),
        sum(item.status == "timed_out" for item in samples),
        sum(item.status == "dropped" for item in samples),
        sum(item.late_by_ms > schedule.late_tolerance_seconds * 1000 for item in samples),
        samples,
    )


def _harvest(schedule, pending, samples, now, origin, final=False):
    for item in pending:
        if item.emitted:
            continue
        invocation = item.invocation
        if item.future.done():
            item.future.result()
            samples.append(_request_sample(schedule, _relative_invocation(invocation, origin)))
            item.emitted = True
        elif final or (
            invocation.started_at is not None
            and now - invocation.started_at >= schedule.timeout_seconds
        ):
            invocation.cancellation.set()
            samples.append(_request_sample(
                schedule, _relative_invocation(invocation, origin), "timed_out",
                "request exceeded timeout or phase drain boundary",
            ))
            item.emitted = True
    pending[:] = [
        item for item in pending if not (item.emitted and item.future.done())
    ]


def _relative_invocation(invocation, origin):
    return _Invocation(
        invocation.sequence,
        invocation.scheduled_at - origin,
        None if invocation.started_at is None else invocation.started_at - origin,
        None if invocation.completed_at is None else invocation.completed_at - origin,
        invocation.sample,
        invocation.error,
    )


def _submit(target, schedule, sequence, deadline, executor, clock, pending):
    invocation = _Invocation(sequence, deadline)
    future = executor.submit(
        lambda: _invoke(target, invocation, clock, schedule.timeout_seconds)
    )
    pending.append(_Pending(invocation, future))


def run_offered_load(target, schedule: LoadSchedule, *, clock=time.monotonic,
                     sleep=time.sleep, executor_factory=ThreadPoolExecutor,
                     phase_origin=None, terminal_abort=None) -> LoadResult:
    """从单一 monotonic origin 产生到达 deadline，并按真实阶段 wall time计数。"""
    if not isinstance(target, DeadlineTarget) or not isinstance(schedule, LoadSchedule):
        raise ValueError("DeadlineTarget and LoadSchedule are required")
    if terminal_abort is not None and not callable(terminal_abort):
        raise ValueError("terminal_abort must be callable")
    origin = clock() if phase_origin is None else phase_origin
    now = clock()
    if now < origin:
        sleep(origin - now)
    arrival_end = origin + schedule.duration_seconds
    executor = executor_factory(max_workers=schedule.workers)
    pending = []
    samples = []
    sequence = 0
    try:
        if schedule.mode == "fixed":
            arrival_count = int(math.ceil(
                schedule.duration_seconds * schedule.rate_per_second - 1e-12
            ))
            for sequence in range(arrival_count):
                deadline = origin + sequence / schedule.rate_per_second
                now = clock()
                if now < deadline:
                    sleep(deadline - now)
                    now = clock()
                _harvest(schedule, pending, samples, now, origin)
                lateness = now - deadline
                if lateness > schedule.late_tolerance_seconds:
                    relative = _Invocation(sequence, deadline - origin)
                    samples.append(_request_sample(
                        schedule, relative, "dropped", "arrival deadline missed",
                    ))
                    samples[-1] = RequestSample(
                        **{**asdict(samples[-1]), "late_by_ms": lateness * 1000}
                    )
                    continue
                active = sum(not item.future.done() for item in pending)
                if active >= schedule.workers:
                    relative = _Invocation(sequence, deadline - origin)
                    samples.append(_request_sample(
                        schedule, relative, "dropped", "worker capacity unavailable",
                    ))
                    continue
                _submit(target, schedule, sequence, deadline, executor, clock, pending)
        else:
            for sequence in range(schedule.workers):
                _submit(target, schedule, sequence, origin, executor, clock, pending)
            sequence = schedule.workers
            while clock() < arrival_end:
                now = clock()
                _harvest(schedule, pending, samples, now, origin)
                active = sum(not item.future.done() for item in pending)
                while active < schedule.workers and clock() < arrival_end:
                    _submit(target, schedule, sequence, clock(), executor, clock, pending)
                    sequence += 1
                    active += 1
                waiting = all(
                    not item.future.done() for item in pending if not item.emitted
                )
                if clock() < arrival_end and waiting:
                    sleep(min(0.01, arrival_end - clock()))

        if clock() < arrival_end:
            sleep(arrival_end - clock())
        drain_end = arrival_end + schedule.timeout_seconds
        while any(not item.future.done() for item in pending) and clock() < drain_end:
            _harvest(schedule, pending, samples, clock(), origin)
            if any(not item.future.done() for item in pending) and clock() < drain_end:
                sleep(min(0.01, drain_end - clock()))
        _harvest(schedule, pending, samples, clock(), origin, final=True)
        termination_end = clock() + TERMINATION_GRACE_SECONDS
        while any(not item.future.done() for item in pending) and clock() < termination_end:
            sleep(min(0.01, termination_end - clock()))
        unresolved = [item for item in pending if not item.future.done()]
        if unresolved:
            evidence = {
                "status": "unresolved_execution",
                "stream": schedule.name,
                "termination_grace_seconds": TERMINATION_GRACE_SECONDS,
                "unresolved_requests": [
                    {
                        "sequence": item.invocation.sequence,
                        "scheduled_offset_seconds": item.invocation.scheduled_at - origin,
                        "started_offset_seconds": (
                            None if item.invocation.started_at is None
                            else item.invocation.started_at - origin
                        ),
                    }
                    for item in unresolved
                ],
                "request_classifications": [_json_value(sample) for sample in samples],
            }
            try:
                if terminal_abort is not None:
                    terminal_abort(evidence)
            finally:
                os._exit(TERMINAL_ABORT_EXIT_CODE)
    finally:
        # ClickHouse 请求自身受同一 30 秒超时约束；等待 worker 退出后再允许 phase cleanup。
        executor.shutdown(wait=True, cancel_futures=True)
    return _load_result(schedule, origin, clock(), samples)


def _percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_load(result: LoadResult):
    """仅以成功样本发布 p50/p95，并对 p99 应用 1,000 样本门禁。"""
    if not isinstance(result, LoadResult) or result.phase_wall_seconds <= 0:
        raise ValueError("completed LoadResult is required")
    successful = [
        item.application_ready_ms for item in result.samples
        if item.status == "success" and item.application_ready_ms is not None
    ]
    publish_p99 = len(successful) >= P99_MINIMUM_SUCCESSES
    latency = {
        "minimum": min(successful) if successful else None,
        "p50": _percentile(successful, 0.50) if successful else None,
        "p95": _percentile(successful, 0.95) if successful else None,
        "p99": _percentile(successful, 0.99) if publish_p99 else None,
        "maximum": max(successful) if successful else None,
        "p99_status": (
            "publishable" if publish_p99 else "unavailable_insufficient_successes"
        ),
        "p99_minimum_successes": P99_MINIMUM_SUCCESSES,
    }
    return {
        "offered_rate_requests_s": result.offered_rate_per_second,
        "phase_wall_seconds": result.phase_wall_seconds,
        "scheduled_requests": result.scheduled_requests,
        "started_requests": result.started_requests,
        "completed_requests": result.completed_requests,
        "successful_requests": result.successful_requests,
        "failed_requests": result.failed_requests,
        "timed_out_requests": result.timed_out_requests,
        "dropped_requests": result.dropped_requests,
        "late_requests": result.late_requests,
        "completed_throughput_requests_s": (
            result.successful_requests / result.phase_wall_seconds
        ),
        "latency_ms": latency,
    }


def run_load_phase(targets, schedules, *, clock=time.monotonic, sleep=time.sleep,
                   executor_factory=ThreadPoolExecutor, terminal_abort=None):
    """使用同一 phase origin 并发执行各自独立 worker pool 的请求流。"""
    if set(targets) != set(schedules):
        raise ValueError("targets must exactly match schedules")
    origin = clock() + 0.05
    coordinator = ThreadPoolExecutor(max_workers=len(targets))
    try:
        futures = {
            name: coordinator.submit(
                run_offered_load, targets[name], schedule,
                clock=clock, sleep=sleep, executor_factory=executor_factory,
                phase_origin=origin, terminal_abort=terminal_abort,
            )
            for name, schedule in schedules.items()
        }
        return {name: future.result() for name, future in futures.items()}
    finally:
        coordinator.shutdown(wait=True)


def _normalize_phase_wall(results):
    """使同一阶段的所有流使用共同 origin 到最晚完成时刻。"""
    if not isinstance(results, dict) or not results or any(
        not isinstance(result, LoadResult) for result in results.values()
    ):
        raise RuntimeError("phase runner returned invalid load results")
    origins = {result.phase_origin for result in results.values()}
    if len(origins) != 1:
        raise RuntimeError("phase streams do not share one monotonic origin")
    origin = origins.pop()
    finished = max(result.phase_finished for result in results.values())
    return {
        name: replace(
            result, phase_finished=finished, phase_wall_seconds=finished - origin,
        )
        for name, result in results.items()
    }


def collect_resource_snapshot():
    """读取可用的宿主 CPU、内存和块设备 I/O 累计计数。"""
    result = {
        "cpu": {"status": "unavailable"},
        "memory": {"status": "unavailable"},
        "io": {"status": "unavailable"},
    }
    try:
        cpu = Path("/proc/stat").read_text().splitlines()[0].split()
        if cpu and cpu[0] == "cpu":
            result["cpu"] = {
                "status": "available", "ticks": [int(value) for value in cpu[1:]],
            }
    except (OSError, ValueError, IndexError):
        pass
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            name, value = line.split(":", 1)
            if name in {"MemTotal", "MemAvailable"}:
                memory[name] = int(value.split()[0])
        if set(memory) == {"MemTotal", "MemAvailable"}:
            result["memory"] = {
                "status": "available", "total_kib": memory["MemTotal"],
                "available_kib": memory["MemAvailable"],
            }
    except (OSError, ValueError):
        pass
    try:
        read_sectors = written_sectors = 0
        devices = 0
        for line in Path("/proc/diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) < 14 or fields[2].startswith(("loop", "ram")):
                continue
            read_sectors += int(fields[5])
            written_sectors += int(fields[9])
            devices += 1
        if devices:
            result["io"] = {
                "status": "available", "devices": devices,
                "read_sectors": read_sectors, "written_sectors": written_sectors,
            }
    except (OSError, ValueError):
        pass
    return result


def validate_resource_snapshot(resources):
    """验证正式运行所需的宿主 CPU、内存和 I/O 观测。"""
    if not isinstance(resources, dict):
        raise RuntimeError("resource snapshot is invalid")
    cpu = resources.get("cpu", {})
    ticks = cpu.get("ticks")
    if cpu.get("status") != "available" or not isinstance(ticks, list) or not ticks or any(
        type(value) is not int or value < 0 for value in ticks
    ):
        raise RuntimeError("cpu resource evidence is unavailable or incomplete")
    memory = resources.get("memory", {})
    if memory.get("status") != "available" or any(
        type(memory.get(field)) is not int or memory[field] < 0
        for field in ("total_kib", "available_kib")
    ) or memory["available_kib"] > memory["total_kib"]:
        raise RuntimeError("memory resource evidence is unavailable or incomplete")
    io = resources.get("io", {})
    if io.get("status") != "available" or any(
        type(io.get(field)) is not int or io[field] < minimum
        for field, minimum in (
            ("devices", 1), ("read_sectors", 0), ("written_sectors", 0),
        )
    ):
        raise RuntimeError("io resource evidence is unavailable or incomplete")
    return resources


def _json_value(value):
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return {key: _json_value(item) for key, item in vars(value).items()}
    return value


def _write_jsonl_atomic(path, load_results):
    records = []
    for stream, result in load_results.items():
        for sample in result.samples:
            records.append({"stream": stream, **_json_value(sample)})
    content = b"".join(
        json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n"
        for record in records
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _snapshot(adapter, name, origin, clock, resource_collector, require_resources=False):
    state = capture_part_state(adapter, name)
    storage = {
        "tables": state.tables,
        "merges": state.active_merges,
        "asset_store": state.asset_store,
    }
    resources = _json_value(resource_collector())
    if require_resources:
        validate_resource_snapshot(resources)
    return {
        "name": name,
        "captured_offset_seconds": clock() - origin,
        "resources": resources,
        "storage": _json_value(storage),
        "active_part_backlog": sum(
            max(0, int(table.get("part_count", 0)) - 1)
            for table in state.tables.values()
        ),
        "active_merge_count": len(state.active_merges),
    }


def _query_evidence(adapter, warmup, measured):
    query_samples = []
    for result_set in (warmup, measured):
        for stream, result in result_set.items():
            expected = QUERY_STREAMS.get(stream)
            for request in result.samples:
                if request.status != "success":
                    continue
                if stream == "continuous_ingest":
                    if not isinstance(request.sample, BlockResult):
                        raise RuntimeError(
                            "continuous_ingest success lacks BlockResult evidence"
                        )
                    continue
                if expected is None:
                    continue
                sample = request.sample
                if not isinstance(sample, QuerySample):
                    raise RuntimeError(f"{stream} success lacks Task 4 QuerySample")
                expected_kind, expected_scenario = expected
                if sample.kind != expected_kind or (
                    expected_scenario is not None and sample.scenario != expected_scenario
                ):
                    raise RuntimeError(f"{stream} query target does not match fixed scenario")
                if sample.response_bytes <= 0 or sample.validation.row_count < 0:
                    raise RuntimeError(f"{stream} response-byte validation is incomplete")
                query_samples.append(sample)
    query_ids = [sample.query_id for sample in query_samples]
    if any(not query_id for query_id in query_ids) or len(set(query_ids)) != len(query_ids):
        raise RuntimeError("successful ClickHouse query IDs are missing or duplicated")
    plans = {}
    query_finish = {}
    query_details = {}
    index_scans = {}
    for offset in range(0, len(query_ids), QUERY_EVIDENCE_BATCH_SIZE):
        batch = query_ids[offset:offset + QUERY_EVIDENCE_BATCH_SIZE]
        access = adapter.collect_access_evidence(batch)
        if not isinstance(access, AccessEvidence):
            raise RuntimeError("adapter returned invalid QueryFinish evidence")
        if set(access.plans) != set(batch) or any(
            not isinstance(plan, str) or not plan for plan in access.plans.values()
        ):
            raise RuntimeError("query plans do not cover successful ClickHouse samples")
        sample_by_id = {sample.query_id: sample for sample in query_samples}
        if set(access.query_details) != set(batch) or any(
            detail.get("kind") != sample_by_id[query_id].kind
            or not isinstance(detail.get("statement"), str)
            or not detail["statement"]
            or not isinstance(detail.get("declared_source"), str)
            or not detail["declared_source"]
            or any(type(detail.get(field)) is not int or detail[field] < 0
                   for field in ("scanned_rows", "scanned_bytes"))
            for query_id in batch
            for detail in (access.query_details.get(query_id, {}),)
        ):
            raise RuntimeError("access details do not cover successful ClickHouse samples")
        if set(access.query_finish) != set(batch):
            raise RuntimeError("QueryFinish does not match successful ClickHouse samples")
        plans.update(access.plans)
        query_finish.update(access.query_finish)
        query_details.update(access.query_details)
        for name, count in access.index_scans.items():
            index_scans[name] = index_scans.get(name, 0) + count
    access = AccessEvidence(
        plans, index_scans=index_scans, query_finish=query_finish,
        query_details=query_details,
    )
    if set(access.query_finish) != set(query_ids):
        raise RuntimeError("QueryFinish does not match successful ClickHouse samples")
    if any(
        row.get("type") != "QueryFinish" or int(row.get("exception_code", -1)) != 0
        or any(type(row.get(field)) is not int or row[field] < 0
               for field in ("read_rows", "read_bytes"))
        for row in access.query_finish.values()
    ):
        raise RuntimeError("QueryFinish contains unsuccessful rows")
    return _json_value(access)


def _coerce_cleanup(adapter):
    cleanup = adapter.cleanup()
    if not isinstance(cleanup, CleanupResult):
        raise RuntimeError("adapter returned invalid cleanup evidence")
    if not cleanup.removed:
        raise RuntimeError("cleanup was not confirmed")
    return _json_value(cleanup)


def _run_phase(adapter_factory, targets_factory, output, phase, seed, *,
               scope, phase_runner, resource_collector, namespace_factory,
               manifest_writer, clock, sleep, executor_factory, terminal_abort):
    namespace = namespace_factory(phase)
    phase_output = output / phase.name
    phase_output.mkdir(parents=True, exist_ok=True)
    manifest_path = phase_output / "run-manifest.json"
    manifest = {
        "format": "agent-trace-json-storage-stage3-interference-phase",
        "format_version": 1,
        "run_id": "jsons3-interference-" + phase.name + "-" + uuid.uuid4().hex[:10],
        "status": "running", "phase": phase.name, "seed": seed,
        "execution_scope": scope, "classification": scope + "_running",
        "namespace": namespace,
        "cache_state": "warm-fixed-offered-load-no-os-cache-drop",
        "warmup_seconds": phase.warmup_seconds,
        "measurement_seconds": phase.measurement_seconds,
        "schedules": {
            "warmup": _json_value(fixed_phase_schedules(phase, measurement=False)),
            "measurement": _json_value(fixed_phase_schedules(phase, measurement=True)),
        },
    }
    manifest_writer(manifest_path, manifest)
    error = None
    adapter = None
    cleanup = {"namespace": namespace, "removed": False}
    warmup = measured = {}
    origin = clock()

    def report_terminal_abort(evidence):
        manifest["execution_resolution"] = evidence
        manifest["cleanup"] = {
            "namespace": namespace,
            "removed": False,
            "status": "not_attempted_unresolved_execution",
        }
        manifest["status"] = "failed"
        manifest["classification"] = scope + "_failed"
        manifest["error"] = "request worker remained unresolved after terminal cancellation"
        manifest_writer(manifest_path, manifest)
        terminal_abort(manifest)

    try:
        adapter = adapter_factory(namespace, phase, seed)
        manifest["layout"] = adapter.layout
        manifest["layout_definition"] = adapter.create()
        targets = targets_factory(adapter, phase, seed)
        expected = set(fixed_phase_schedules(phase, measurement=True))
        if not isinstance(targets, dict) or set(targets) != expected or any(
            not isinstance(target, DeadlineTarget) for target in targets.values()
        ):
            raise RuntimeError("phase targets do not match fixed schedules")
        manifest["snapshots"] = [
            _snapshot(
                adapter, "before_warmup", origin, clock, resource_collector,
                require_resources=scope == "formal",
            ),
        ]
        warmup = _normalize_phase_wall(phase_runner(
            targets, fixed_phase_schedules(phase, measurement=False),
            clock=clock, sleep=sleep, executor_factory=executor_factory,
            terminal_abort=report_terminal_abort,
        ))
        _write_jsonl_atomic(phase_output / "warmup-samples.jsonl", warmup)
        manifest["snapshots"].append(
            _snapshot(
                adapter, "before_measurement", origin, clock, resource_collector,
                require_resources=scope == "formal",
            )
        )
        measured = _normalize_phase_wall(phase_runner(
            targets, fixed_phase_schedules(phase, measurement=True),
            clock=clock, sleep=sleep, executor_factory=executor_factory,
            terminal_abort=report_terminal_abort,
        ))
        _write_jsonl_atomic(phase_output / "samples.jsonl", measured)
        if scope == "formal":
            manifest["execution_coverage"] = validate_formal_phase_coverage(
                phase, warmup, measured,
            )
        manifest["snapshots"].append(
            _snapshot(
                adapter, "after_measurement", origin, clock, resource_collector,
                require_resources=scope == "formal",
            )
        )
        manifest["warmup"] = {name: summarize_load(result) for name, result in warmup.items()}
        manifest["statistics"] = {name: summarize_load(result) for name, result in measured.items()}
        manifest["query_evidence"] = _query_evidence(adapter, warmup, measured)
    except Exception as exception:
        error = exception
    finally:
        if adapter is not None:
            try:
                cleanup = _coerce_cleanup(adapter)
            except Exception as cleanup_error:
                cleanup = {
                    "namespace": getattr(adapter, "database", namespace), "removed": False,
                    "error": str(cleanup_error) or type(cleanup_error).__name__,
                }
                if error is None:
                    error = cleanup_error
    manifest["cleanup"] = cleanup
    manifest["status"] = "complete" if error is None else "failed"
    manifest["classification"] = scope + "_" + manifest["status"]
    if error is not None:
        manifest["error"] = str(error) or type(error).__name__
    manifest_writer(manifest_path, manifest)
    return manifest, error


def run_interference(adapter_factory, targets_factory, output: Path, *, scope, seed=20260907,
                     phases=FIXED_PHASES, phase_runner=run_load_phase,
                     resource_collector=collect_resource_snapshot,
                     namespace_factory=lambda phase: (
                         f"jsons3_if_{phase.name}_{uuid.uuid4().hex[:8]}"
                     ),
                     manifest_writer=write_manifest_atomic, clock=time.monotonic,
                     sleep=time.sleep, executor_factory=ThreadPoolExecutor):
    """以同一 seed 和全新 namespace 运行安静阶段及四类独立干扰。"""
    phases = tuple(phases)
    if scope not in {"formal", "diagnostic"}:
        raise ValueError("scope must be formal or diagnostic")
    if not phases or len({phase.name for phase in phases}) != len(phases):
        raise ValueError("phases must be non-empty and unique")
    if scope == "formal":
        if phases != FIXED_PHASES:
            raise ValueError("formal scope requires the exact fixed five phases")
        if (
            phase_runner is not run_load_phase
            or clock is not time.monotonic
            or sleep is not time.sleep
            or executor_factory is not ThreadPoolExecutor
            or resource_collector is not collect_resource_snapshot
        ):
            raise ValueError("formal scope does not allow injected execution controls")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "run-manifest.json"
    manifest = {
        "format": "agent-trace-json-storage-stage3-interference-run",
        "format_version": 1,
        "run_id": "jsons3-interference-" + uuid.uuid4().hex[:10],
        "status": "running", "seed": seed,
        "execution_scope": scope, "classification": scope + "_running",
        "phase_order": [phase.name for phase in phases], "phases": [],
        "statistics_boundary": "raw-samples-retained-p99-requires-1000-successes",
    }
    manifest_writer(manifest_path, manifest)
    phase_manifests = []
    errors = []
    namespaces = set()

    def report_terminal_abort(phase_manifest):
        manifest["phases"] = [*phase_manifests, phase_manifest]
        manifest["status"] = "failed"
        manifest["classification"] = scope + "_failed"
        manifest["execution_resolution"] = phase_manifest["execution_resolution"]
        manifest["errors"] = [
            f"{phase_manifest['phase']}: {phase_manifest['error']}"
        ]
        manifest_writer(manifest_path, manifest)

    for phase in phases:
        phase_manifest, error = _run_phase(
            adapter_factory, targets_factory, output, phase, seed,
            scope=scope, phase_runner=phase_runner, resource_collector=resource_collector,
            namespace_factory=namespace_factory, manifest_writer=manifest_writer,
            clock=clock, sleep=sleep, executor_factory=executor_factory,
            terminal_abort=report_terminal_abort,
        )
        namespace = phase_manifest["namespace"]
        if namespace in namespaces:
            error = RuntimeError("interference phases require fresh namespaces")
            phase_manifest["status"] = "failed"
            phase_manifest["classification"] = scope + "_failed"
            phase_manifest["error"] = str(error)
            manifest_writer(output / phase.name / "run-manifest.json", phase_manifest)
        namespaces.add(namespace)
        phase_manifests.append(phase_manifest)
        if error is not None:
            errors.append(f"{phase.name}: {error}")
    manifest["phases"] = phase_manifests
    manifest["status"] = "failed" if errors else "complete"
    manifest["classification"] = scope + "_" + manifest["status"]
    if errors:
        manifest["errors"] = errors
    manifest_writer(manifest_path, manifest)
    if errors:
        raise RuntimeError("; ".join(errors))
    return InterferenceRunResult(manifest, tuple(phase_manifests))
