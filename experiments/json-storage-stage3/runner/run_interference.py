#!/usr/bin/env python3
"""执行固定 offered-load 前台查询与独立干扰阶段。"""

import json
import inspect
import math
import multiprocessing
import os
import queue
import signal
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
IPC_PROTOCOL_VERSION = 1
IPC_DRAIN_BATCH_SIZE = 256
PHASE_START_DELAY_SECONDS = 0.05
PHASE_CONTROL_BUDGET_SECONDS = 300.0
TERMINATION_GRACE_SECONDS = 0.1
IPC_EVENT_TYPES = frozenset({
    "phase_initialized", "snapshot_captured", "segment_complete",
    "phase_terminal", "execution_unresolved",
})
_OPERATION_EVENT_TYPES = frozenset({"snapshot_captured", "segment_complete"})
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


def _default_namespace_factory(phase):
    """为每个 interference phase 生成独立 namespace。"""
    return f"jsons3_if_{phase.name}_{uuid.uuid4().hex[:8]}"


class UnresolvedExecution(RuntimeError):
    """携带可序列化证据，表示调用边界内仍有未退出执行。"""

    def __init__(self, evidence):
        encoded = json.dumps(evidence, ensure_ascii=False, allow_nan=False)
        self.evidence = json.loads(encoded)
        reason = self.evidence.get("reason", self.evidence.get("status", "unresolved"))
        super().__init__(f"unresolved execution: {reason}")


@dataclass(frozen=True)
class PhaseProcessPolicy:
    """冻结 phase 执行模式、watchdog 和两级进程收束上界。"""

    mode: str = "process"
    watchdog_seconds: float | None = None
    control_budget_seconds: float = PHASE_CONTROL_BUDGET_SECONDS
    terminate_join_seconds: float = 1.0
    kill_join_seconds: float = 1.0
    reader_join_seconds: float = 1.0
    poll_interval_seconds: float = 0.01

    def __post_init__(self):
        if self.mode not in {"process", "inline"}:
            raise ValueError("phase process mode must be process or inline")
        if self.watchdog_seconds is not None and (
            not isinstance(self.watchdog_seconds, (int, float))
            or isinstance(self.watchdog_seconds, bool)
            or not math.isfinite(self.watchdog_seconds)
            or self.watchdog_seconds <= 0
        ):
            raise ValueError("watchdog_seconds must be positive when provided")
        for name, value, allow_zero in (
            ("control_budget_seconds", self.control_budget_seconds, True),
            ("terminate_join_seconds", self.terminate_join_seconds, False),
            ("kill_join_seconds", self.kill_join_seconds, False),
            ("reader_join_seconds", self.reader_join_seconds, False),
            ("poll_interval_seconds", self.poll_interval_seconds, False),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                requirement = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be {requirement}")

    def watchdog_seconds_for(self, phase):
        """按两段到达窗口、请求 drain 和控制预算计算 phase 上界。"""
        if self.watchdog_seconds is not None:
            return float(self.watchdog_seconds)
        if isinstance(phase, str):
            matches = [item for item in FIXED_PHASES if item.name == phase]
            if not matches:
                raise ValueError(f"unsupported interference phase: {phase}")
            phase = matches[0]
        if not isinstance(phase, InterferencePhase):
            raise ValueError("phase must be an InterferencePhase")
        bound = self.control_budget_seconds
        for measurement in (False, True):
            schedules = fixed_phase_schedules(phase, measurement=measurement)
            bound += (
                PHASE_START_DELAY_SECONDS
                + max(
                    schedule.duration_seconds + schedule.timeout_seconds
                    for schedule in schedules.values()
                )
                + TERMINATION_GRACE_SECONDS
            )
        return bound


@dataclass(frozen=True)
class PhaseProcessOutcome:
    """保存父进程验证后的有序 IPC 事件与 child 退出解释。"""

    events: tuple[dict[str, object], ...]
    exit: dict[str, object]


@dataclass(frozen=True)
class _PhaseTerminal:
    """请求 child wrapper 原样发送已形成的 phase terminal payload。"""

    payload: dict[str, object]


class _ParentEventTimeout(BaseException):
    """中断主线程内超过 phase deadline 的同步 event consumer。"""


def _validate_parent_event_boundary():
    """在 fork 前确认当前线程可安全独占 Linux real-time timer。"""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("bounded parent event consumer requires the main thread")
    if not hasattr(signal, "setitimer") or not hasattr(signal, "ITIMER_REAL"):
        raise RuntimeError("bounded parent event consumer requires Linux setitimer")
    delay, interval = signal.getitimer(signal.ITIMER_REAL)
    if delay > 0 or interval > 0:
        raise RuntimeError("bounded parent event consumer requires an idle SIGALRM timer")


def _consume_parent_event(event_consumer, event, deadline):
    """以 phase 剩余时间为硬上界同步消费一个 parent event。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ParentEventTimeout("parent event consumer exceeded phase deadline")
    previous_handler = signal.getsignal(signal.SIGALRM)

    def interrupt(_signum, _frame):
        raise _ParentEventTimeout("parent event consumer exceeded phase deadline")

    signal.signal(signal.SIGALRM, interrupt)
    try:
        signal.setitimer(signal.ITIMER_REAL, remaining)
        return event_consumer(event)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


class _PhaseEventSender:
    def __init__(self, connection, run_id, phase):
        self.connection = connection
        self.run_id = run_id
        self.phase = phase
        self.sequence = 0

    def __call__(self, event_type, payload):
        if event_type not in _OPERATION_EVENT_TYPES:
            raise ValueError(f"operation cannot emit IPC event type: {event_type}")
        self._send(event_type, payload)

    def _send(self, event_type, payload):
        event = {
            "protocol_version": IPC_PROTOCOL_VERSION,
            "run_id": self.run_id,
            "phase": self.phase,
            "sequence": self.sequence,
            "type": event_type,
            "payload": payload,
        }
        encoded = json.dumps(
            event, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.connection.send_bytes(encoded)
        self.sequence += 1


def _phase_process_child(receive_connection, send_connection, operation, run_id, phase):
    receive_connection.close()
    sender = _PhaseEventSender(send_connection, run_id, phase)
    try:
        sender._send("phase_initialized", {"pid": os.getpid()})
        try:
            result = operation(sender)
        except UnresolvedExecution as error:
            sender._send("execution_unresolved", error.evidence)
            sender._send("phase_terminal", {
                "status": "failed", "error": str(error),
                "error_type": type(error).__name__,
            })
        except Exception as error:
            sender._send("phase_terminal", {
                "status": "failed", "error": str(error) or type(error).__name__,
                "error_type": type(error).__name__,
            })
        except BaseException as error:
            sender._send("phase_terminal", {
                "status": "failed", "error": str(error) or type(error).__name__,
                "error_type": type(error).__name__,
            })
            raise
        else:
            if isinstance(result, _PhaseTerminal):
                payload = result.payload
            else:
                payload = {"status": "complete"}
                if result is not None:
                    payload["result"] = result
            sender._send("phase_terminal", payload)
    finally:
        send_connection.close()


def _read_phase_events(connection, messages):
    try:
        while True:
            messages.put(("event", connection.recv_bytes()))
    except EOFError:
        pass
    except OSError as error:
        messages.put(("reader_error", str(error) or type(error).__name__))
    finally:
        try:
            connection.close()
        except OSError:
            # 父进程在 reader 未收束时也会关闭同一 Pipe 端点以解除 recv。
            pass
        messages.put(("eof", None))


def _validate_phase_event(raw, run_id, phase, expected_sequence, terminal_seen):
    try:
        event = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("child emitted malformed phase IPC JSON") from error
    fields = {"protocol_version", "run_id", "phase", "sequence", "type", "payload"}
    if not isinstance(event, dict) or set(event) != fields:
        raise RuntimeError("child emitted invalid phase IPC fields")
    if event["protocol_version"] != IPC_PROTOCOL_VERSION:
        raise RuntimeError("child emitted unsupported phase IPC version")
    if event["run_id"] != run_id or event["phase"] != phase:
        raise RuntimeError("child emitted mismatched phase IPC identity")
    if type(event["sequence"]) is not int or event["sequence"] != expected_sequence:
        raise RuntimeError("child emitted non-contiguous phase IPC sequence")
    if event["type"] not in IPC_EVENT_TYPES:
        raise RuntimeError("child emitted unsupported phase IPC event")
    if terminal_seen:
        raise RuntimeError("child emitted phase IPC after terminal")
    if expected_sequence == 0 and event["type"] != "phase_initialized":
        raise RuntimeError("child phase IPC does not start with initialization")
    if expected_sequence > 0 and event["type"] == "phase_initialized":
        raise RuntimeError("child emitted duplicate phase initialization")
    return event


def _explain_process_exit(exit_code):
    if exit_code is None:
        return {"kind": "running", "exit_code": None}
    if exit_code < 0:
        number = -exit_code
        try:
            name = signal.Signals(number).name
        except ValueError:
            name = "UNKNOWN"
        return {
            "kind": "signal", "exit_code": exit_code,
            "signal": number, "signal_name": name,
        }
    return {"kind": "exited", "exit_code": exit_code}


def _stop_phase_process(process, policy):
    lifecycle = []
    if process.is_alive():
        process.terminate()
        lifecycle.append({"action": "terminate"})
        process.join(policy.terminate_join_seconds)
        lifecycle.append({
            "action": "join_after_terminate",
            "timeout_seconds": policy.terminate_join_seconds,
            "alive": process.is_alive(),
        })
    if process.is_alive():
        process.kill()
        lifecycle.append({"action": "kill"})
        process.join(policy.kill_join_seconds)
        lifecycle.append({
            "action": "join_after_kill",
            "timeout_seconds": policy.kill_join_seconds,
            "alive": process.is_alive(),
        })
    return lifecycle


def run_owned_phase_process(operation, *, run_id, phase, policy, event_consumer=None):
    """在 Linux fork child 中执行局部闭包，并由父 watchdog 收束生命周期。"""
    if not callable(operation):
        raise ValueError("phase operation must be callable")
    if event_consumer is not None and not callable(event_consumer):
        raise ValueError("event_consumer must be callable when provided")
    try:
        inspect.signature(operation).bind(lambda *_: None)
    except (TypeError, ValueError) as error:
        raise ValueError("phase operation must accept one event sender") from error
    if not isinstance(run_id, str) or not run_id or not isinstance(phase, str) or not phase:
        raise ValueError("run_id and phase must be non-empty strings")
    if not isinstance(policy, PhaseProcessPolicy) or policy.mode != "process":
        raise ValueError("owned phase execution requires process policy")
    if "fork" not in multiprocessing.get_all_start_methods():
        raise RuntimeError("owned phase execution requires Linux fork support")
    if event_consumer is not None:
        _validate_parent_event_boundary()

    context = multiprocessing.get_context("fork")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_phase_process_child,
        args=(receive_connection, send_connection, operation, run_id, phase),
        name=f"stage3-interference-{phase}",
    )
    try:
        process.start()
    except BaseException:
        receive_connection.close()
        send_connection.close()
        raise
    send_connection.close()
    messages = queue.Queue()
    reader = threading.Thread(
        target=_read_phase_events, args=(receive_connection, messages),
        name=f"stage3-interference-ipc-{phase}", daemon=True,
    )
    reader.start()

    watchdog_seconds = policy.watchdog_seconds_for(phase)
    deadline = time.monotonic() + watchdog_seconds
    events = []
    terminal_seen = False
    eof_seen = False
    failure_reason = None
    protocol_error = None
    while True:
        for _ in range(IPC_DRAIN_BATCH_SIZE):
            try:
                message_type, value = messages.get_nowait()
            except queue.Empty:
                break
            if message_type == "event":
                try:
                    event = _validate_phase_event(
                        value, run_id, phase, len(events), terminal_seen,
                    )
                except RuntimeError as error:
                    failure_reason = "protocol_error"
                    protocol_error = str(error)
                    break
                events.append(event)
                terminal_seen = event["type"] == "phase_terminal"
                if event_consumer is not None:
                    try:
                        _consume_parent_event(event_consumer, event, deadline)
                    except _ParentEventTimeout as error:
                        failure_reason = "parent_event_timeout"
                        protocol_error = str(error)
                        break
                    except Exception as error:
                        failure_reason = "parent_event_error"
                        protocol_error = str(error) or type(error).__name__
                        break
            elif message_type == "reader_error":
                failure_reason = "ipc_reader_error"
                protocol_error = value
                break
            else:
                eof_seen = True
        if failure_reason is not None:
            break
        if not process.is_alive() and eof_seen and messages.empty():
            break
        now = time.monotonic()
        if now >= deadline:
            failure_reason = (
                "watchdog_timeout" if process.is_alive() else "ipc_drain_timeout"
            )
            break
        time.sleep(min(policy.poll_interval_seconds, deadline - now))

    lifecycle = _stop_phase_process(process, policy) if failure_reason else []
    if not process.is_alive():
        process.join(0)
    reader.join(policy.reader_join_seconds)
    reader_join_attempts = 1
    receive_connection.close()
    if reader.is_alive():
        reader.join(policy.reader_join_seconds)
        reader_join_attempts += 1
    reader_evidence = {
        "connection_closed": True,
        "join_timeout_seconds": policy.reader_join_seconds,
        "join_attempts": reader_join_attempts,
        "alive": reader.is_alive(),
    }
    if reader.is_alive() and failure_reason is None:
        failure_reason = "ipc_reader_join_timeout"
    child_exit = _explain_process_exit(process.exitcode)
    if failure_reason is None:
        if child_exit != {"kind": "exited", "exit_code": 0}:
            failure_reason = "child_exit"
        elif not terminal_seen:
            failure_reason = "missing_terminal_event"
    if failure_reason is not None:
        evidence = {
            "status": "unresolved_execution",
            "reason": failure_reason,
            "run_id": run_id,
            "phase": phase,
            "watchdog_seconds": watchdog_seconds,
            "lifecycle": lifecycle,
            "child_exit": child_exit,
            "event_count": len(events),
            "last_sequence": events[-1]["sequence"] if events else None,
            "terminal_received": terminal_seen,
            "reader": reader_evidence,
        }
        if protocol_error is not None:
            evidence["protocol_error"] = protocol_error
        if failure_reason == "parent_event_timeout":
            evidence["artifact_publication"] = {
                "status": "unavailable",
                "reason": "parent_event_timeout",
                "retry_safe": False,
            }
        if not process.is_alive():
            process.close()
        raise UnresolvedExecution(evidence)
    process.close()
    return PhaseProcessOutcome(tuple(events), child_exit)


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
                     phase_origin=None) -> LoadResult:
    """从单一 monotonic origin 产生到达 deadline，并按真实阶段 wall time计数。"""
    if not isinstance(target, DeadlineTarget) or not isinstance(schedule, LoadSchedule):
        raise ValueError("DeadlineTarget and LoadSchedule are required")
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
            executor.shutdown(wait=False, cancel_futures=True)
            executor = None
            raise UnresolvedExecution(evidence)
    finally:
        # 已收束请求在返回前回收；未解决线程交给 owned child 的父进程处理。
        if executor is not None:
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
                   executor_factory=ThreadPoolExecutor):
    """使用同一 phase origin 并发执行各自独立 worker pool 的请求流。"""
    if set(targets) != set(schedules):
        raise ValueError("targets must exactly match schedules")
    origin = clock() + PHASE_START_DELAY_SECONDS
    coordinator = ThreadPoolExecutor(max_workers=len(targets))
    try:
        futures = {
            name: coordinator.submit(
                run_offered_load, targets[name], schedule,
                clock=clock, sleep=sleep, executor_factory=executor_factory,
                phase_origin=origin,
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


def _load_result_from_json(payload, stream):
    """将已通过 JSON IPC 的单流结果严格还原为 LoadResult。"""
    load_fields = {
        "name", "phase_origin", "phase_finished", "phase_wall_seconds",
        "offered_rate_per_second", "scheduled_requests", "started_requests",
        "completed_requests", "successful_requests", "failed_requests",
        "timed_out_requests", "dropped_requests", "late_requests", "samples",
    }
    sample_fields = {
        "stream", "sequence", "scheduled_offset_seconds", "started_offset_seconds",
        "completed_offset_seconds", "duration_ms", "application_ready_ms", "status",
        "late_by_ms", "sample", "error",
    }

    def number(value, name, *, nullable=False, minimum=None):
        if value is None and nullable:
            return None
        if type(value) not in {int, float} or not math.isfinite(value):
            raise RuntimeError(f"segment LoadResult {name} is not a finite number")
        if minimum is not None and value < minimum:
            raise RuntimeError(f"segment LoadResult {name} is below its minimum")
        return float(value)

    if not isinstance(payload, dict) or set(payload) != load_fields:
        raise RuntimeError("segment LoadResult fields are invalid")
    if payload["name"] != stream:
        raise RuntimeError("segment LoadResult stream name is invalid")
    if not isinstance(payload["samples"], list):
        raise RuntimeError("segment LoadResult samples are invalid")

    samples = []
    for item in payload["samples"]:
        if not isinstance(item, dict) or set(item) != sample_fields:
            raise RuntimeError("segment LoadResult sample fields are invalid")
        if item["stream"] != stream or type(item["sequence"]) is not int or item["sequence"] < 0:
            raise RuntimeError("segment LoadResult sample identity is invalid")
        if item["status"] not in {"success", "failed", "timed_out", "dropped"}:
            raise RuntimeError("segment LoadResult sample status is invalid")
        if item["error"] is not None and not isinstance(item["error"], str):
            raise RuntimeError("segment LoadResult sample error is invalid")
        samples.append(RequestSample(
            stream=item["stream"], sequence=item["sequence"],
            scheduled_offset_seconds=number(item["scheduled_offset_seconds"], "scheduled offset", minimum=0.0),
            started_offset_seconds=number(item["started_offset_seconds"], "started offset", nullable=True, minimum=0.0),
            completed_offset_seconds=number(item["completed_offset_seconds"], "completed offset", nullable=True, minimum=0.0),
            duration_ms=number(item["duration_ms"], "duration", nullable=True, minimum=0.0),
            application_ready_ms=number(
                item["application_ready_ms"], "application-ready duration", nullable=True,
                minimum=0.0,
            ),
            status=item["status"], late_by_ms=number(item["late_by_ms"], "lateness", minimum=0.0),
            sample=item["sample"], error=item["error"],
        ))
    if [item.sequence for item in samples] != list(range(len(samples))):
        raise RuntimeError("segment LoadResult sample sequence is invalid")

    counts = {}
    for name in (
        "scheduled_requests", "started_requests", "completed_requests",
        "successful_requests", "failed_requests", "timed_out_requests",
        "dropped_requests", "late_requests",
    ):
        value = payload[name]
        if type(value) is not int or value < 0:
            raise RuntimeError(f"segment LoadResult {name} is invalid")
        counts[name] = value
    if (
        counts["scheduled_requests"] != len(samples)
        or counts["started_requests"] != sum(item.started_offset_seconds is not None for item in samples)
        or counts["completed_requests"] != sum(item.completed_offset_seconds is not None for item in samples)
        or counts["successful_requests"] != sum(item.status == "success" for item in samples)
        or counts["failed_requests"] != sum(item.status == "failed" for item in samples)
        or counts["timed_out_requests"] != sum(item.status == "timed_out" for item in samples)
        or counts["dropped_requests"] != sum(item.status == "dropped" for item in samples)
        or counts["late_requests"] > len(samples)
    ):
        raise RuntimeError("segment LoadResult counts do not match samples")

    origin = number(payload["phase_origin"], "phase origin")
    finished = number(payload["phase_finished"], "phase finished")
    phase_wall = number(payload["phase_wall_seconds"], "phase wall time", minimum=0.0)
    if finished < origin or not math.isclose(
        phase_wall, finished - origin, rel_tol=1e-9, abs_tol=1e-9,
    ):
        raise RuntimeError("segment LoadResult phase timing is invalid")
    offered_rate = number(
        payload["offered_rate_per_second"], "offered rate", nullable=True, minimum=0.0,
    )
    if offered_rate == 0.0:
        raise RuntimeError("segment LoadResult offered rate is invalid")
    return LoadResult(
        stream, origin, finished, phase_wall, offered_rate,
        counts["scheduled_requests"], counts["started_requests"], counts["completed_requests"],
        counts["successful_requests"], counts["failed_requests"], counts["timed_out_requests"],
        counts["dropped_requests"], counts["late_requests"], tuple(samples),
    )


def _write_jsonl_atomic(path, load_results):
    records = []
    for stream, result in load_results.items():
        if not isinstance(result, LoadResult):
            raise ValueError("JSONL persistence requires LoadResult values")
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


def _phase_manifest(run_id, namespace, phase, seed, scope):
    """构造由父进程发布的 phase 初始 manifest。"""
    return {
        "format": "agent-trace-json-storage-stage3-interference-phase",
        "format_version": 1,
        "run_id": run_id,
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
        "snapshots": [],
    }


def _execute_phase_child(adapter_factory, targets_factory, phase, namespace, seed, *,
                         scope, phase_runner, resource_collector, clock, sleep,
                         executor_factory, event_sender):
    """在 owned child 内执行数据库生命周期，仅通过 IPC 返回证据。"""
    adapter = None
    error = None
    completion_unknown = False
    cleanup = {"namespace": namespace, "removed": False,
               "status": "not_attempted_adapter_unavailable"}
    warmup = measured = {}
    terminal = {}
    origin = clock()
    try:
        adapter = adapter_factory(namespace, phase, seed)
        layout_definition = adapter.create()
        targets = targets_factory(adapter, phase, seed)
        expected = set(fixed_phase_schedules(phase, measurement=True))
        if not isinstance(targets, dict) or set(targets) != expected or any(
            not isinstance(target, DeadlineTarget) for target in targets.values()
        ):
            raise RuntimeError("phase targets do not match fixed schedules")
        first_snapshot = _snapshot(
            adapter, "before_warmup", origin, clock, resource_collector,
            require_resources=scope == "formal",
        )
        event_sender("snapshot_captured", {
            "name": "before_warmup", "snapshot": first_snapshot,
            "layout": adapter.layout, "layout_definition": layout_definition,
        })
        warmup = _normalize_phase_wall(phase_runner(
            targets, fixed_phase_schedules(phase, measurement=False),
            clock=clock, sleep=sleep, executor_factory=executor_factory,
        ))
        event_sender("segment_complete", {
            "name": "warmup", "results": _json_value(warmup),
        })
        event_sender("snapshot_captured", {
            "name": "before_measurement",
            "snapshot": _snapshot(
                adapter, "before_measurement", origin, clock, resource_collector,
                require_resources=scope == "formal",
            ),
        })
        measured = _normalize_phase_wall(phase_runner(
            targets, fixed_phase_schedules(phase, measurement=True),
            clock=clock, sleep=sleep, executor_factory=executor_factory,
        ))
        event_sender("segment_complete", {
            "name": "measurement", "results": _json_value(measured),
        })
        if scope == "formal":
            terminal["execution_coverage"] = validate_formal_phase_coverage(
                phase, warmup, measured,
            )
        event_sender("snapshot_captured", {
            "name": "after_measurement",
            "snapshot": _snapshot(
                adapter, "after_measurement", origin, clock, resource_collector,
                require_resources=scope == "formal",
            ),
        })
        terminal["warmup"] = {
            name: summarize_load(result) for name, result in warmup.items()
        }
        terminal["statistics"] = {
            name: summarize_load(result) for name, result in measured.items()
        }
        terminal["query_evidence"] = _query_evidence(adapter, warmup, measured)
    except UnresolvedExecution as unresolved:
        completion_unknown = True
        evidence = dict(unresolved.evidence)
        evidence.update({
            "namespace": namespace, "server_side_completion": "unknown",
        })
        raise UnresolvedExecution(evidence)
    except Exception as exception:
        error = exception
    except BaseException:
        completion_unknown = True
        raise
    finally:
        if adapter is not None and not completion_unknown:
            try:
                cleanup = _coerce_cleanup(adapter)
            except Exception as cleanup_error:
                cleanup = {
                    "namespace": getattr(adapter, "database", namespace),
                    "removed": False,
                    "error": str(cleanup_error) or type(cleanup_error).__name__,
                }
                if error is None:
                    error = cleanup_error
    terminal["cleanup"] = cleanup
    terminal["status"] = "complete" if error is None else "failed"
    if error is not None:
        terminal["error"] = str(error) or type(error).__name__
        terminal["error_type"] = type(error).__name__
    return _PhaseTerminal(_json_value(terminal))


class _ParentPhaseReducer:
    """在父进程按固定状态机归并事件，并即时发布 segment。"""

    _OPERATIONS = (
        ("snapshot_captured", "before_warmup"),
        ("segment_complete", "warmup"),
        ("snapshot_captured", "before_measurement"),
        ("segment_complete", "measurement"),
        ("snapshot_captured", "after_measurement"),
    )

    def __init__(self, manifest, phase_output, publish):
        self.manifest = manifest
        self.phase_output = phase_output
        self.publish = publish
        self.initialized = False
        self.operation_index = 0
        self.unresolved = None
        self.terminal = None
        self.segments = {}

    def consume(self, event):
        payload = event["payload"]
        if not isinstance(payload, dict):
            raise RuntimeError("phase IPC payload must be an object")
        event_type = event["type"]
        if event_type == "phase_initialized":
            if (
                self.initialized or set(payload) != {"pid"}
                or type(payload.get("pid")) is not int or payload["pid"] <= 0
            ):
                raise RuntimeError("invalid phase initialization event")
            self.initialized = True
            self.manifest["child_pid"] = payload["pid"]
            self.publish()
            return
        if not self.initialized:
            raise RuntimeError("phase operation preceded initialization")
        if event_type == "execution_unresolved":
            if (
                self.unresolved is not None
                or payload.get("status") != "unresolved_execution"
                or payload.get("server_side_completion") != "unknown"
            ):
                raise RuntimeError("duplicate unresolved execution event")
            self.unresolved = payload
            self.publish()
            return
        if event_type == "phase_terminal":
            if self.terminal is not None or payload.get("status") not in {"complete", "failed"}:
                raise RuntimeError("invalid phase terminal event")
            if payload["status"] == "complete" and (
                self.unresolved is not None or self.operation_index != len(self._OPERATIONS)
            ):
                raise RuntimeError("complete terminal preceded required phase evidence")
            required = {"status", "cleanup", "warmup", "statistics", "query_evidence"}
            if payload["status"] == "complete" and (
                not required.issubset(payload)
                or (self.manifest["execution_scope"] == "formal"
                    and "execution_coverage" not in payload)
            ):
                raise RuntimeError("complete terminal payload is incomplete")
            if payload["status"] == "complete":
                expected_warmup = {
                    name: summarize_load(result)
                    for name, result in self.segments.get("warmup", {}).items()
                }
                expected_statistics = {
                    name: summarize_load(result)
                    for name, result in self.segments.get("measurement", {}).items()
                }
                if (
                    payload["warmup"] != expected_warmup
                    or payload["statistics"] != expected_statistics
                ):
                    raise RuntimeError("complete terminal does not match segment LoadResult")
            if self.unresolved is not None and payload["status"] != "failed":
                raise RuntimeError("unresolved execution requires failed terminal")
            self.terminal = payload
            return
        if self.unresolved is not None or self.operation_index >= len(self._OPERATIONS):
            raise RuntimeError("phase operation event is out of order")
        expected_type, expected_name = self._OPERATIONS[self.operation_index]
        if event_type != expected_type or payload.get("name") != expected_name:
            raise RuntimeError("phase operation event is out of order")
        if event_type == "snapshot_captured":
            snapshot = payload.get("snapshot")
            expected_fields = {"name", "snapshot"}
            if expected_name == "before_warmup":
                expected_fields.update({"layout", "layout_definition"})
            if not isinstance(snapshot, dict) or snapshot.get("name") != expected_name:
                raise RuntimeError("snapshot event payload is invalid")
            if set(payload) != expected_fields:
                raise RuntimeError("snapshot event fields are invalid")
            if expected_name == "before_warmup":
                if not isinstance(payload.get("layout"), str) or not isinstance(
                    payload.get("layout_definition"), dict
                ):
                    raise RuntimeError("phase initialization evidence is incomplete")
                self.manifest["layout"] = payload["layout"]
                self.manifest["layout_definition"] = payload["layout_definition"]
            self.manifest["snapshots"].append(snapshot)
        else:
            results = payload.get("results")
            expected_streams = set(self.manifest["schedules"][expected_name])
            if (
                set(payload) != {"name", "results"} or not isinstance(results, dict)
                or set(results) != expected_streams
            ):
                raise RuntimeError("segment event results are invalid")
            results = {
                stream: _load_result_from_json(result, stream)
                for stream, result in results.items()
            }
            self.segments[expected_name] = results
            filename = "warmup-samples.jsonl" if expected_name == "warmup" else "samples.jsonl"
            _write_jsonl_atomic(self.phase_output / filename, results)
        self.operation_index += 1
        self.publish()

    def finalize(self):
        """仅在 child 已以 0 退出后将 terminal 状态应用到 manifest。"""
        if self.terminal is None:
            raise RuntimeError("phase terminal event is missing")
        terminal = dict(self.terminal)
        status = terminal.pop("status")
        if self.unresolved is not None:
            self.manifest["execution_resolution"] = {
                **self.unresolved, "server_side_completion": "unknown",
            }
            self.manifest["cleanup"] = {
                "namespace": self.manifest["namespace"], "removed": False,
                "status": "not_attempted_server_completion_unknown",
            }
            terminal.pop("cleanup", None)
        self.manifest.update(terminal)
        self.manifest["status"] = status
        self.manifest["classification"] = self.manifest["execution_scope"] + "_" + status
        if status == "complete" and not self.manifest.get("cleanup", {}).get("removed"):
            raise RuntimeError("complete phase lacks confirmed cleanup")
        self.publish()
        return None if status == "complete" else RuntimeError(
            self.manifest.get("error", "phase execution failed")
        )


def _run_phase(adapter_factory, targets_factory, output, phase, seed, *,
               scope, phase_runner, resource_collector, namespace,
               manifest_writer, clock, sleep, executor_factory):
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
    completion_unknown = False
    cleanup = {"namespace": namespace, "removed": False}
    warmup = measured = {}
    origin = clock()

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
    except UnresolvedExecution as exception:
        error = exception
        completion_unknown = True
    except Exception as exception:
        error = exception
    finally:
        if adapter is not None and not completion_unknown:
            try:
                cleanup = _coerce_cleanup(adapter)
            except Exception as cleanup_error:
                cleanup = {
                    "namespace": getattr(adapter, "database", namespace), "removed": False,
                    "error": str(cleanup_error) or type(cleanup_error).__name__,
                }
                if error is None:
                    error = cleanup_error
    if completion_unknown:
        manifest["execution_resolution"] = {
            **error.evidence, "server_side_completion": "unknown",
        }
        cleanup = {
            "namespace": namespace, "removed": False,
            "status": "not_attempted_server_completion_unknown",
        }
    manifest["cleanup"] = cleanup
    manifest["status"] = "complete" if error is None else "failed"
    manifest["classification"] = scope + "_" + manifest["status"]
    if error is not None:
        manifest["error"] = str(error) or type(error).__name__
    manifest_writer(manifest_path, manifest)
    return manifest, error


def _run_phase_process(adapter_factory, targets_factory, phase_output, phase, namespace,
                       seed, *, scope, phase_runner, resource_collector, clock, sleep,
                       executor_factory, process_policy, phase_manifest, publish):
    """运行一个 owned child，并在父进程归并和发布全部 phase 证据。"""
    reducer = _ParentPhaseReducer(phase_manifest, phase_output, publish)

    def execute(event_sender):
        return _execute_phase_child(
            adapter_factory, targets_factory, phase, namespace, seed,
            scope=scope, phase_runner=phase_runner,
            resource_collector=resource_collector, clock=clock, sleep=sleep,
            executor_factory=executor_factory, event_sender=event_sender,
        )

    try:
        run_owned_phase_process(
            execute, run_id=phase_manifest["run_id"], phase=phase.name,
            policy=process_policy, event_consumer=reducer.consume,
        )
    except UnresolvedExecution as unresolved:
        if unresolved.evidence.get("artifact_publication", {}).get(
            "status"
        ) == "unavailable":
            # 同一 writer 已知可能阻塞；异常 evidence 是唯一可靠的失败出口。
            raise
        evidence = {**(reducer.unresolved or {}), **unresolved.evidence}
        evidence["server_side_completion"] = "unknown"
        phase_manifest["execution_resolution"] = evidence
        phase_manifest["cleanup"] = {
            "namespace": namespace, "removed": False,
            "status": "not_attempted_server_completion_unknown",
        }
        phase_manifest["status"] = "failed"
        phase_manifest["classification"] = scope + "_failed"
        phase_manifest["error"] = evidence.get("reason", str(unresolved))
        publish()
        return phase_manifest, RuntimeError(phase_manifest["error"])
    try:
        error = reducer.finalize()
    except Exception as exception:
        phase_manifest["status"] = "failed"
        phase_manifest["classification"] = scope + "_failed"
        phase_manifest["error"] = str(exception) or type(exception).__name__
        phase_manifest.setdefault("cleanup", {
            "namespace": namespace, "removed": False,
            "status": "not_confirmed_terminal_reduction_failed",
        })
        publish()
        return phase_manifest, exception
    return phase_manifest, error


def run_interference(adapter_factory, targets_factory, output: Path, *, scope, seed=20260907,
                     phases=FIXED_PHASES, phase_runner=run_load_phase,
                     resource_collector=collect_resource_snapshot,
                     namespace_factory=_default_namespace_factory,
                     manifest_writer=write_manifest_atomic, clock=time.monotonic,
                     sleep=time.sleep, executor_factory=ThreadPoolExecutor,
                     process_policy=PhaseProcessPolicy()):
    """以同一 seed 和全新 namespace 运行安静阶段及四类独立干扰。"""
    phases = tuple(phases)
    if scope not in {"formal", "diagnostic"}:
        raise ValueError("scope must be formal or diagnostic")
    if not phases or len({phase.name for phase in phases}) != len(phases):
        raise ValueError("phases must be non-empty and unique")
    if not isinstance(process_policy, PhaseProcessPolicy):
        raise ValueError("process_policy must be a PhaseProcessPolicy")
    if scope == "formal":
        if phases != FIXED_PHASES:
            raise ValueError("formal scope requires the exact fixed five phases")
        if process_policy.mode != "process":
            raise ValueError("formal scope requires process phase execution")
        if (
            phase_runner is not run_load_phase
            or clock is not time.monotonic
            or sleep is not time.sleep
            or executor_factory is not ThreadPoolExecutor
            or resource_collector is not collect_resource_snapshot
            or process_policy != PhaseProcessPolicy()
            or namespace_factory is not _default_namespace_factory
            or manifest_writer is not write_manifest_atomic
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

    for phase in phases:
        if process_policy.mode == "process":
            namespace = namespace_factory(phase)
            phase_output = output / phase.name
            phase_output.mkdir(parents=True, exist_ok=True)
            phase_run_id = manifest["run_id"] + "-" + phase.name
            phase_manifest = _phase_manifest(
                phase_run_id, namespace, phase, seed, scope,
            )
            phase_manifests.append(phase_manifest)

            def publish():
                manifest_writer(phase_output / "run-manifest.json", phase_manifest)
                manifest["phases"] = [_json_value(item) for item in phase_manifests]
                manifest_writer(manifest_path, manifest)

            publish()
            if namespace in namespaces:
                error = RuntimeError("interference phases require fresh namespaces")
                phase_manifest["status"] = "failed"
                phase_manifest["classification"] = scope + "_failed"
                phase_manifest["error"] = str(error)
                phase_manifest["cleanup"] = {
                    "namespace": namespace, "removed": False,
                    "status": "not_attempted_duplicate_namespace",
                }
                publish()
            else:
                namespaces.add(namespace)
                phase_manifest, error = _run_phase_process(
                    adapter_factory, targets_factory, phase_output, phase, namespace, seed,
                    scope=scope, phase_runner=phase_runner,
                    resource_collector=resource_collector, clock=clock, sleep=sleep,
                    executor_factory=executor_factory, process_policy=process_policy,
                    phase_manifest=phase_manifest, publish=publish,
                )
            if error is not None:
                errors.append(f"{phase.name}: {error}")
                break
            continue

        namespace = namespace_factory(phase)
        if namespace in namespaces:
            error = RuntimeError("interference phases require fresh namespaces")
            phase_manifest = _phase_manifest(
                manifest["run_id"] + "-" + phase.name, namespace, phase, seed, scope,
            )
            phase_manifest.update({
                "status": "failed", "classification": scope + "_failed",
                "error": str(error),
                "cleanup": {
                    "namespace": namespace, "removed": False,
                    "status": "not_attempted_duplicate_namespace",
                },
            })
            phase_output = output / phase.name
            phase_output.mkdir(parents=True, exist_ok=True)
            manifest_writer(phase_output / "run-manifest.json", phase_manifest)
        else:
            namespaces.add(namespace)
            phase_manifest, error = _run_phase(
                adapter_factory, targets_factory, output, phase, seed,
                scope=scope, phase_runner=phase_runner,
                resource_collector=resource_collector, namespace=namespace,
                manifest_writer=manifest_writer, clock=clock, sleep=sleep,
                executor_factory=executor_factory,
            )
        phase_manifests.append(phase_manifest)
        if error is not None:
            errors.append(f"{phase.name}: {error}")
            break
    manifest["phases"] = phase_manifests
    manifest["status"] = "failed" if errors else "complete"
    manifest["classification"] = scope + "_" + manifest["status"]
    if errors:
        manifest["errors"] = errors
    manifest_writer(manifest_path, manifest)
    if errors:
        raise RuntimeError("; ".join(errors))
    return InterferenceRunResult(manifest, tuple(phase_manifests))
