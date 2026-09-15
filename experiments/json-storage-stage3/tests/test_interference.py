import fcntl
import hashlib
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import AccessEvidence, CleanupResult, QueryResult, QuerySpec, StorageEvidence
import run_interference as interference_runner
from run_interference import (
    FIXED_PHASES, IPC_PROTOCOL_VERSION, DeadlineTarget, LoadResult, LoadSchedule,
    PhaseProcessPolicy, UnresolvedExecution, build_query_target,
    fixed_phase_schedules, run_interference, run_offered_load,
    run_owned_phase_process, summarize_load, validate_formal_phase_coverage,
    validate_resource_snapshot,
)
from run_layout_matrix import QueryTruth, write_manifest_atomic


PAYLOAD = b"abc"
ROW = {
    "event_id": "event-a", "trace_id": "trace-a", "project_id": "project-a",
    "start_time": "2030-01-01T00:00:00.000Z", "profile": "text_2m",
    "content_type": "application/json", "encoding": "utf-8", "content_length": 3,
    "preview": "abc", "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
    "payload": PAYLOAD,
}
QUERY = QuerySpec("detail", {
    "project_id": "project-a", "trace_id": "trace-a",
    "start_time": "2030-01-01T00:00:00.000Z", "event_id": "event-a",
})
TRUTH = QueryTruth("detail:text_2m", (ROW,))
INLINE_PROCESS_POLICY = PhaseProcessPolicy(mode="inline")


class FakeClock:
    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def sleep(self, duration):
        if duration < 0:
            raise AssertionError("negative sleep")
        self.now += duration


class ImmediateFuture:
    def __init__(self, value):
        self.value = value

    def done(self):
        return True

    def result(self):
        return self.value

    def cancel(self):
        return False


class InlineExecutor:
    """同步执行提交内容，使 fake clock 精确控制开始和完成时刻。"""

    def __init__(self, max_workers):
        self.max_workers = max_workers

    def submit(self, function):
        return ImmediateFuture(function())

    def shutdown(self, wait=True, cancel_futures=False):
        return None


class DelayedFuture(ImmediateFuture):
    def __init__(self, value, clock, ready_at):
        super().__init__(value)
        self.clock = clock
        self.ready_at = ready_at

    def done(self):
        return self.clock() >= self.ready_at or self.value.cancellation.is_set()


class DelayedExecutor(InlineExecutor):
    def __init__(self, max_workers, clock):
        super().__init__(max_workers)
        self.clock = clock

    def submit(self, function):
        return DelayedFuture(function(), self.clock, self.clock() + 10.0)


class CountingFuture(ImmediateFuture):
    def __init__(self, value, executor):
        super().__init__(value)
        self.executor = executor

    def done(self):
        self.executor.done_calls += 1
        return True


class CountingExecutor(InlineExecutor):
    def __init__(self, max_workers):
        super().__init__(max_workers)
        self.done_calls = 0

    def submit(self, function):
        return CountingFuture(function(), self)


class NeverDoneFuture:
    def done(self):
        return False


class TrackingExecutor:
    def __init__(self, max_workers):
        self.max_workers = max_workers
        self.shutdown_calls = []

    def submit(self, _function):
        return NeverDoneFuture()

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdown_calls.append((wait, cancel_futures))


def success_sample(latency_ms=1.0):
    return SimpleNamespace(status="success", application_ready_ms=latency_ms)


def deadline_target(function):
    """把确定性即时夹具转换为 deadline/cancellation target。"""
    return DeadlineTarget(lambda _deadline, _cancellation: function())


class FakeAdapter:
    layout = "same_table"

    def __init__(self, namespace, omit_query_finish=False, omit_plans=False,
                 omit_query_details=False, fail_storage_call=None):
        self.database = namespace + "_same_table"
        self.omit_query_finish = omit_query_finish
        self.omit_plans = omit_plans
        self.omit_query_details = omit_query_details
        self.query_count = 0
        self.query_kinds = {}
        self.fail_storage_call = fail_storage_call
        self.storage_calls = 0
        self.cleaned = False
        self.evidence_batches = []

    def create(self):
        return {"database": self.database, "ddl": "CREATE TABLE events"}

    def run_query(self, query):
        self.query_count += 1
        query_id = f"query-{self.database}-{self.query_count}"
        self.query_kinds[query_id] = query.kind
        row = dict(ROW)
        if query.kind == "list":
            row["preview"] = None
        if query.kind in {"list", "preview"}:
            row["payload"] = None
        return QueryResult(
            query_id, (row,), 100, 100, 0, 0.4, 0.1,
        )

    def collect_access_evidence(self, query_ids):
        self.evidence_batches.append(tuple(query_ids))
        finished = query_ids[:-1] if self.omit_query_finish else query_ids
        return AccessEvidence(
            {} if self.omit_plans else {
                query_id: "ReadFromMergeTree" for query_id in query_ids
            },
            query_finish={
                query_id: {"type": "QueryFinish", "exception_code": 0,
                           "read_rows": 1, "read_bytes": 100}
                for query_id in finished
            },
            query_details={} if self.omit_query_details else {
                query_id: {
                    "kind": self.query_kinds[query_id],
                    "statement": "SELECT columns FROM events",
                    "declared_source": "events", "scanned_rows": 1,
                    "scanned_bytes": 100,
                }
                for query_id in query_ids
            },
        )

    def collect_storage(self):
        self.storage_calls += 1
        if self.storage_calls == self.fail_storage_call:
            raise RuntimeError("snapshot failed")
        return StorageEvidence(
            {"events": {
                "part_count": 2, "rows": 8, "marks": 1,
                "compressed_bytes": 50, "uncompressed_bytes": 100, "columns": {},
            }},
            ({"table": "events", "elapsed": 0.2, "progress": 0.5},),
        )

    def cleanup(self):
        self.cleaned = True
        return CleanupResult(self.database, True)


def short_phase_runner(targets, schedules, **_):
    """保留真实 target 和 scheduler，只缩短单元测试的阶段时间。"""
    results = {}
    for name, target in targets.items():
        clock = FakeClock()
        results[name] = run_offered_load(
            target,
            LoadSchedule(name, 1.0, 1.0, 1, timeout_seconds=2.0),
            clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
        )
    return results


def fixed_query_target(adapter, stream):
    row = dict(ROW)
    if stream == "list":
        row.update({"preview": None, "payload": None})
        return build_query_target(
            adapter, QuerySpec("list", {}), QueryTruth("list:first", (row,)),
        )
    if stream == "preview":
        row["payload"] = None
        return build_query_target(
            adapter, QuerySpec("preview", {}), QueryTruth("preview:first", (row,)),
        )
    if stream == "detail_2m":
        return build_query_target(adapter, QUERY, TRUTH)
    if stream == "trace_long":
        return build_query_target(
            adapter, QuerySpec("trace", {}), QueryTruth("trace:p95", (row,)),
        )
    if stream == "batch_loop":
        return build_query_target(
            adapter, QuerySpec("batch", {"cohort": "main"}),
            QueryTruth("batch:main", (row,)),
        )
    raise AssertionError(f"unexpected query stream: {stream}")


class OfferedLoadTest(unittest.TestCase):
    def test_uncooperative_target_raises_json_unresolved_evidence_without_exiting_host(self):
        """捕获不合作 worker 从请求线程直接终止宿主进程。"""
        entered = threading.Event()
        release = threading.Event()

        def target(_deadline, _cancellation):
            entered.set()
            release.wait()

        started = time.monotonic()
        try:
            with self.assertRaises(UnresolvedExecution) as raised:
                run_offered_load(
                    DeadlineTarget(target),
                    LoadSchedule("list", 1.0, 0.02, 1, timeout_seconds=0.02),
                )
        finally:
            release.set()
        elapsed = time.monotonic() - started

        self.assertTrue(entered.is_set())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(raised.exception.evidence["status"], "unresolved_execution")
        self.assertEqual(raised.exception.evidence["stream"], "list")
        json.dumps(raised.exception.evidence, allow_nan=False)

    def test_unresolved_worker_uses_non_waiting_executor_shutdown(self):
        """捕获未解决 worker 再次进入等待式 executor shutdown。"""
        clock = FakeClock()
        executor = TrackingExecutor(1)

        with self.assertRaises(UnresolvedExecution):
            run_offered_load(
                deadline_target(lambda: success_sample()),
                LoadSchedule("list", 1.0, 0.01, 1, timeout_seconds=0.01),
                clock=clock, sleep=clock.sleep,
                executor_factory=lambda max_workers: executor,
            )

        self.assertEqual(executor.shutdown_calls, [(False, True)])

    def test_zero_argument_target_is_rejected_before_a_worker_can_block(self):
        """捕获通用零参数 target 进入线程后无法在 drain 上界内中断。"""
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def non_returning_target():
            entered.set()
            release.wait()

        def invoke_runner():
            try:
                run_offered_load(
                    non_returning_target,
                    LoadSchedule("list", 1.0, 0.05, 1, timeout_seconds=0.05),
                )
            except Exception as error:
                errors.append(error)

        watchdog = threading.Thread(target=invoke_runner)
        watchdog.start()
        watchdog.join(0.5)
        try:
            self.assertFalse(watchdog.is_alive())
            self.assertFalse(entered.is_set())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ValueError)
            self.assertIn("DeadlineTarget", str(errors[0]))
        finally:
            release.set()
            watchdog.join(1.0)

    def test_cancellation_aware_target_returns_within_drain_bound_without_worker_race(self):
        """捕获 drain 仅标记 timeout 后仍由 worker 与下一阶段并发运行。"""
        entered = threading.Event()
        exited = threading.Event()

        def target(deadline, cancellation):
            entered.set()
            cancellation.wait(max(0.0, deadline - time.monotonic()) + 0.5)
            exited.set()
            return success_sample()

        started = time.monotonic()
        result = run_offered_load(
            DeadlineTarget(target),
            LoadSchedule("list", 1.0, 0.05, 1, timeout_seconds=0.05),
        )
        elapsed = time.monotonic() - started

        self.assertTrue(entered.is_set())
        self.assertTrue(exited.is_set())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result.timed_out_requests, 1)

    def test_arrival_deadlines_share_one_origin_and_do_not_drift_with_completion(self):
        """捕获下一次到达时间从上一请求完成时刻串行推导。"""
        clock = FakeClock(10.0)
        starts = []

        def target():
            starts.append(clock())
            clock.sleep(0.2)
            return success_sample(200.0)

        result = run_offered_load(
            deadline_target(target),
            LoadSchedule("list", 2.0, 2.0, 1, timeout_seconds=1.0),
            clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
        )

        self.assertEqual(starts, [10.0, 10.5, 11.0, 11.5])
        self.assertEqual(
            [sample.scheduled_offset_seconds for sample in result.samples],
            [0.0, 0.5, 1.0, 1.5],
        )
        self.assertEqual(result.scheduled_requests, 4)

    def test_failure_timeout_and_late_drop_have_separate_counts(self):
        """捕获失败、超时和错过固定到达时间被合并或静默丢弃。"""
        clock = FakeClock()
        calls = 0

        def target():
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(status="failed", application_ready_ms=1.0,
                                       error="query failed")
            if calls == 2:
                raise TimeoutError("request timeout")
            clock.sleep(0.75)
            return success_sample(750.0)

        result = run_offered_load(
            deadline_target(target),
            LoadSchedule(
                "preview", 2.0, 2.0, 1, timeout_seconds=0.5,
                late_tolerance_seconds=0.1,
            ),
            clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
        )

        self.assertEqual(result.scheduled_requests, 4)
        self.assertEqual(result.started_requests, 3)
        self.assertEqual(result.completed_requests, 3)
        self.assertEqual(result.successful_requests, 0)
        self.assertEqual(result.failed_requests, 1)
        self.assertEqual(result.timed_out_requests, 2)
        self.assertEqual(result.dropped_requests, 1)
        self.assertEqual(result.late_requests, 1)
        self.assertEqual(
            [sample.status for sample in result.samples],
            ["failed", "timed_out", "timed_out", "dropped"],
        )
        self.assertEqual(
            summarize_load(result)["latency_ms"]["p99_status"],
            "unavailable_insufficient_successes",
        )

    def test_completed_throughput_uses_actual_phase_wall_time(self):
        """捕获吞吐使用配置时长而忽略阶段真实越界时间。"""
        clock = FakeClock()
        calls = 0

        def target():
            nonlocal calls
            calls += 1
            if calls == 2:
                clock.sleep(1.0)
            return success_sample(1.0)

        result = run_offered_load(
            deadline_target(target),
            LoadSchedule("list", 2.0, 1.0, 1, timeout_seconds=2.0),
            clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
        )
        summary = summarize_load(result)

        self.assertEqual(result.phase_wall_seconds, 1.5)
        self.assertAlmostEqual(summary["completed_throughput_requests_s"], 2 / 1.5)

    def test_p99_is_unavailable_below_one_thousand_successes(self):
        """捕获小样本仍发布 p99。"""
        below_clock = FakeClock()
        below = run_offered_load(
            deadline_target(lambda: success_sample(2.0)),
            LoadSchedule("list", 999.0, 1.0, 2, timeout_seconds=1.0),
            clock=below_clock, sleep=below_clock.sleep, executor_factory=InlineExecutor,
        )
        enough_clock = FakeClock()
        enough = run_offered_load(
            deadline_target(lambda: success_sample(2.0)),
            LoadSchedule("list", 1000.0, 1.0, 2, timeout_seconds=1.0),
            clock=enough_clock, sleep=enough_clock.sleep, executor_factory=InlineExecutor,
        )

        below_summary = summarize_load(below)
        enough_summary = summarize_load(enough)
        self.assertEqual(below.successful_requests, 999)
        self.assertIsNone(below_summary["latency_ms"]["p99"])
        self.assertEqual(
            below_summary["latency_ms"]["p99_status"],
            "unavailable_insufficient_successes",
        )
        self.assertEqual(enough.successful_requests, 1000)
        self.assertEqual(enough_summary["latency_ms"]["p99"], 2.0)
        self.assertEqual(enough_summary["latency_ms"]["p99_status"], "publishable")

    def test_completed_futures_are_retired_from_the_scheduler(self):
        """捕获正式 6,000 次到达对全部历史 future 重复扫描。"""
        clock = FakeClock()
        executor = CountingExecutor(2)
        result = run_offered_load(
            deadline_target(lambda: success_sample()),
            LoadSchedule("list", 1000.0, 1.0, 2, timeout_seconds=1.0),
            clock=clock, sleep=clock.sleep,
            executor_factory=lambda max_workers: executor,
        )

        self.assertEqual(result.successful_requests, 1000)
        self.assertLess(executor.done_calls, 10_000)

    def test_batch_interference_keeps_one_worker_looping_until_phase_end(self):
        """捕获 batch 干扰被误改为固定 offered-rate 请求。"""
        clock = FakeClock()
        starts = []

        def target():
            starts.append(round(clock(), 1))
            clock.sleep(0.2)
            return success_sample(200.0)

        result = run_offered_load(
            deadline_target(target),
            LoadSchedule(
                "batch_loop", None, 1.0, 1, timeout_seconds=1.0, mode="continuous",
            ),
            clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
        )

        self.assertEqual(starts, [0.0, 0.2, 0.4, 0.6, 0.8])
        self.assertEqual(result.successful_requests, 5)

    def test_timeout_keeps_draining_worker_until_the_bounded_phase_deadline(self):
        """捕获超时计数发布后立即 cleanup，仍运行的 worker 与清理发生竞争。"""
        clock = FakeClock()
        result = run_offered_load(
            deadline_target(lambda: success_sample()),
            LoadSchedule("list", 1.0, 1.0, 1, timeout_seconds=1.0),
            clock=clock, sleep=clock.sleep,
            executor_factory=lambda max_workers: DelayedExecutor(max_workers, clock),
        )

        self.assertEqual(result.timed_out_requests, 1)
        self.assertEqual(result.phase_wall_seconds, 1.0)


class PhaseProcessBoundaryTest(unittest.TestCase):
    def test_policy_is_frozen_and_keeps_the_fixed_formal_watchdog(self):
        """捕获 phase watchdog 脱离冻结的 30/300 秒请求上界。"""
        policy = PhaseProcessPolicy()

        self.assertAlmostEqual(policy.watchdog_seconds_for(FIXED_PHASES[0]), 690.3)
        with self.assertRaises(FrozenInstanceError):
            policy.mode = "inline"

    def test_linux_fork_child_executes_local_closure_and_emits_versioned_events(self):
        """捕获 process seam 改用需要 pickle 的启动方式。"""
        closed_over = "local-closure"

        def execute(send):
            send("segment_complete", {"value": closed_over})
            return {"result": closed_over}

        outcome = run_owned_phase_process(
            execute, run_id="run-a", phase="quiet",
            policy=PhaseProcessPolicy(watchdog_seconds=1.0),
        )

        self.assertEqual(
            [event["type"] for event in outcome.events],
            ["phase_initialized", "segment_complete", "phase_terminal"],
        )
        self.assertEqual([event["sequence"] for event in outcome.events], [0, 1, 2])
        self.assertTrue(all(
            event["protocol_version"] == IPC_PROTOCOL_VERSION
            and event["run_id"] == "run-a"
            and event["phase"] == "quiet"
            for event in outcome.events
        ))
        self.assertEqual(outcome.events[1]["payload"], {"value": closed_over})
        self.assertEqual(
            outcome.events[-1]["payload"],
            {"status": "complete", "result": {"result": closed_over}},
        )
        self.assertEqual(outcome.exit, {"kind": "exited", "exit_code": 0})

    def test_parent_continuously_drains_a_large_child_event(self):
        """捕获父进程等 child 退出后才读取 IPC，导致 sender 填满 pipe。"""
        payload = "x" * (2 * 1024 * 1024)

        outcome = run_owned_phase_process(
            lambda send: send("snapshot_captured", {"payload": payload}),
            run_id="run-large", phase="quiet",
            policy=PhaseProcessPolicy(watchdog_seconds=1.0),
        )

        self.assertEqual(len(outcome.events[1]["payload"]["payload"]), len(payload))
        self.assertEqual(outcome.events[-1]["type"], "phase_terminal")

    def test_blocking_parent_consumer_is_interrupted_and_child_reaped_within_hard_bound(self):
        """捕获同步 consumer 阻塞后 parent watchdog 停止检查 deadline。"""
        context = multiprocessing.get_context("fork")
        receive_result, send_result = context.Pipe(duplex=False)

        def invoke():
            receive_result.close()
            original_handler = signal.getsignal(signal.SIGALRM)

            def previous_handler(_signum, _frame):
                return None

            signal.signal(signal.SIGALRM, previous_handler)
            try:
                try:
                    run_owned_phase_process(
                        lambda _send: threading.Event().wait(),
                        run_id="run-blocking-consumer", phase="quiet",
                        policy=PhaseProcessPolicy(
                            watchdog_seconds=0.1, terminate_join_seconds=0.05,
                            kill_join_seconds=0.05, poll_interval_seconds=0.005,
                        ),
                        event_consumer=lambda _event: threading.Event().wait(),
                    )
                except UnresolvedExecution as error:
                    send_result.send({
                        "evidence": error.evidence,
                        "handler_restored": (
                            signal.getsignal(signal.SIGALRM) is previous_handler
                        ),
                        "timer": signal.getitimer(signal.ITIMER_REAL),
                    })
            finally:
                signal.signal(signal.SIGALRM, original_handler)
                send_result.close()

        worker = context.Process(target=invoke)
        started = time.monotonic()
        worker.start()
        send_result.close()
        worker.join(2.0)
        elapsed = time.monotonic() - started
        if worker.is_alive():
            worker.terminate()
            worker.join(1.0)
            self.fail("blocking consumer exceeded the outer two-second watchdog")

        self.assertEqual(worker.exitcode, 0)
        self.assertTrue(receive_result.poll(0.1))
        result = receive_result.recv()
        receive_result.close()
        self.assertLess(elapsed, 0.6)
        self.assertEqual(result["evidence"]["reason"], "parent_event_timeout")
        self.assertNotEqual(result["evidence"]["child_exit"]["kind"], "running")
        self.assertTrue(result["handler_restored"])
        self.assertEqual(result["timer"], (0.0, 0.0))

    def test_bounded_parent_consumer_fails_closed_outside_the_main_thread(self):
        """捕获 signal timeout 从非 main thread 静默降级为无界调用。"""
        operation_started = threading.Event()
        errors = []

        def invoke():
            try:
                run_owned_phase_process(
                    lambda _send: operation_started.set(),
                    run_id="run-thread", phase="quiet",
                    policy=PhaseProcessPolicy(watchdog_seconds=0.1),
                    event_consumer=lambda _event: None,
                )
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=invoke)
        worker.start()
        worker.join(0.5)

        self.assertFalse(worker.is_alive())
        self.assertFalse(operation_started.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("main thread", str(errors[0]))

    def test_unjoined_pipe_reader_is_reported_before_another_phase_can_fork(self):
        """捕获收到 EOF 后返回时仍遗留 daemon Pipe reader。"""
        original_reader = interference_runner._read_phase_events
        release = threading.Event()

        def delayed_reader(connection, messages):
            original_reader(connection, messages)
            release.wait()

        interference_runner._read_phase_events = delayed_reader
        try:
            with self.assertRaises(UnresolvedExecution) as raised:
                run_owned_phase_process(
                    lambda _send: None,
                    run_id="run-reader", phase="quiet",
                    policy=PhaseProcessPolicy(
                        watchdog_seconds=1.0, reader_join_seconds=0.05,
                    ),
                )
        finally:
            release.set()
            interference_runner._read_phase_events = original_reader

        self.assertEqual(raised.exception.evidence["reason"], "ipc_reader_join_timeout")
        self.assertTrue(raised.exception.evidence["reader"]["alive"])

    def test_child_reports_unresolved_execution_as_an_ordered_ipc_event(self):
        """捕获未解决请求证据仍依赖 child 内同步 artifact writer。"""
        evidence = {
            "status": "unresolved_execution", "stream": "list",
            "unresolved_requests": [{"sequence": 7}],
        }

        def execute(_send):
            raise UnresolvedExecution(evidence)

        outcome = run_owned_phase_process(
            execute,
            run_id="run-unresolved", phase="quiet",
            policy=PhaseProcessPolicy(watchdog_seconds=1.0),
        )

        self.assertEqual(
            [event["type"] for event in outcome.events],
            ["phase_initialized", "execution_unresolved", "phase_terminal"],
        )
        self.assertEqual(outcome.events[1]["payload"], evidence)
        self.assertEqual(outcome.events[-1]["payload"]["status"], "failed")
        self.assertEqual(outcome.exit, {"kind": "exited", "exit_code": 0})

    def test_parent_kills_child_that_ignores_sigterm_within_hard_bound(self):
        """捕获 terminate 后无界等待忽略 SIGTERM 的 owned child。"""
        def execute(send):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            send("snapshot_captured", {"ready": True})
            threading.Event().wait()

        policy = PhaseProcessPolicy(
            watchdog_seconds=0.15, terminate_join_seconds=0.05,
            kill_join_seconds=0.05, poll_interval_seconds=0.005,
        )
        started = time.monotonic()
        with self.assertRaises(UnresolvedExecution) as raised:
            run_owned_phase_process(
                execute, run_id="run-stuck", phase="quiet", policy=policy,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        evidence = raised.exception.evidence
        self.assertEqual(evidence["reason"], "watchdog_timeout")
        self.assertEqual(evidence["child_exit"]["kind"], "signal")
        self.assertEqual(evidence["child_exit"]["signal_name"], "SIGKILL")
        self.assertEqual(
            [step["action"] for step in evidence["lifecycle"]],
            ["terminate", "join_after_terminate", "kill", "join_after_kill"],
        )
        json.dumps(evidence, allow_nan=False)

    def test_terminal_event_does_not_hide_a_later_nonzero_child_exit(self):
        """捕获合法 terminal 被错误视为足以发布 complete。"""
        def execute(_send):
            def crash_after_return():
                time.sleep(0.02)
                os._exit(9)

            threading.Thread(target=crash_after_return).start()
            return {"ready": True}

        with self.assertRaises(UnresolvedExecution) as raised:
            run_owned_phase_process(
                execute, run_id="run-late-crash", phase="quiet",
                policy=PhaseProcessPolicy(watchdog_seconds=1.0),
            )

        self.assertEqual(raised.exception.evidence["reason"], "child_exit")
        self.assertTrue(raised.exception.evidence["terminal_received"])
        self.assertEqual(raised.exception.evidence["child_exit"]["exit_code"], 9)

    def test_parent_rejects_non_contiguous_child_event_sequence(self):
        """捕获 parent reducer 接受丢失或重排的 child event。"""
        def execute(send):
            send.sequence = 7
            send("snapshot_captured", {"name": "before_warmup"})

        with self.assertRaises(UnresolvedExecution) as raised:
            run_owned_phase_process(
                execute, run_id="run-bad-sequence", phase="quiet",
                policy=PhaseProcessPolicy(watchdog_seconds=1.0),
            )

        self.assertEqual(raised.exception.evidence["reason"], "protocol_error")
        self.assertIn("non-contiguous", raised.exception.evidence["protocol_error"])


class InterferenceRunnerTest(unittest.TestCase):
    def test_formal_scope_rejects_inline_phase_execution(self):
        """捕获 formal interference 继续使用不可强杀的 inline 边界。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "formal.*process"):
                run_interference(
                    lambda *_: None, lambda *_: {}, Path(directory), scope="formal",
                    process_policy=PhaseProcessPolicy(mode="inline"),
                )

    def test_formal_scope_rejects_any_phase_subset_before_execution(self):
        """捕获少于固定五阶段的运行被标为 formal complete。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "exact fixed five phases"):
                run_interference(
                    lambda *_: None, lambda *_: {}, Path(directory),
                    scope="formal", phases=FIXED_PHASES[:1],
                )

    def test_formal_scope_rejects_injected_execution_controls(self):
        """捕获注入 runner 的缩短路径被标为 formal。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "injected execution controls"):
                run_interference(
                    lambda *_: None, lambda *_: {}, Path(directory), scope="formal",
                    phase_runner=short_phase_runner,
                )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "injected execution controls"):
                run_interference(
                    lambda *_: None, lambda *_: {}, Path(directory), scope="formal",
                    process_policy=PhaseProcessPolicy(watchdog_seconds=1.0),
                )

        def injected_control(*_args, **_kwargs):
            raise AssertionError("formal execution invoked an injected control")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "injected execution controls"):
                run_interference(
                    lambda *_: None, lambda *_: {}, Path(directory), scope="formal",
                    namespace_factory=injected_control,
                )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "injected execution controls"):
                run_interference(
                    lambda *_: None, lambda *_: {}, Path(directory), scope="formal",
                    manifest_writer=injected_control,
                )

    def test_parent_reducer_rejects_segment_that_cannot_roundtrip_load_result(self):
        """捕获 parent 只检查字典外形便持久化损坏的 IPC 分段。"""
        invalid_result = {
            "name": "list", "phase_origin": 0.0, "phase_finished": 1.0,
            "phase_wall_seconds": 1.0, "offered_rate_per_second": 20.0,
            "scheduled_requests": 0, "started_requests": 0,
            "completed_requests": 0, "successful_requests": 1,
            "failed_requests": 0, "timed_out_requests": 0,
            "dropped_requests": 0, "late_requests": 0, "samples": [],
        }
        preview_result = dict(invalid_result, name="preview")

        with tempfile.TemporaryDirectory() as directory:
            manifest = interference_runner._phase_manifest(
                "run-reducer", "jsons3_reducer", FIXED_PHASES[0], 20260907,
                "diagnostic",
            )
            reducer = interference_runner._ParentPhaseReducer(
                manifest, Path(directory), lambda: None,
            )
            reducer.consume({"type": "phase_initialized", "payload": {"pid": 1}})
            reducer.consume({
                "type": "snapshot_captured",
                "payload": {
                    "name": "before_warmup",
                    "snapshot": {"name": "before_warmup"},
                    "layout": "same_table",
                    "layout_definition": {},
                },
            })

            with self.assertRaisesRegex(RuntimeError, "LoadResult"):
                reducer.consume({
                    "type": "segment_complete",
                    "payload": {
                        "name": "warmup",
                        "results": {"list": invalid_result, "preview": preview_result},
                    },
                })

    def test_parent_reducer_rejects_non_positive_child_pid(self):
        """捕获初始化事件接受不可能属于 owned child 的 PID。"""
        with tempfile.TemporaryDirectory() as directory:
            manifest = interference_runner._phase_manifest(
                "run-pid", "jsons3_pid", FIXED_PHASES[0], 20260907, "diagnostic",
            )
            reducer = interference_runner._ParentPhaseReducer(
                manifest, Path(directory), lambda: None,
            )

            with self.assertRaisesRegex(RuntimeError, "initialization"):
                reducer.consume({"type": "phase_initialized", "payload": {"pid": 0}})

    def test_process_crash_skips_child_cleanup_and_stops_later_phase(self):
        """捕获 child 异常退出前仍清理未知服务端 namespace。"""
        context = multiprocessing.get_context("fork")
        factory_calls = context.Value("i", 0)
        cleanup_calls = context.Value("i", 0)

        def adapter_factory(namespace, _phase, _seed):
            with factory_calls.get_lock():
                factory_calls.value += 1
            adapter = FakeAdapter(namespace)

            def cleanup():
                with cleanup_calls.get_lock():
                    cleanup_calls.value += 1
                return CleanupResult(adapter.database, True)

            adapter.cleanup = cleanup
            return adapter

        def targets_factory(adapter, phase, _seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        def crash_phase_runner(_targets, _schedules, **_kwargs):
            raise SystemExit(9)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "child_exit"):
                run_interference(
                    adapter_factory, targets_factory, output, scope="diagnostic",
                    phases=FIXED_PHASES[:2], phase_runner=crash_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    process_policy=PhaseProcessPolicy(watchdog_seconds=1.0),
                )
            phase_manifest = json.loads(
                (output / "quiet" / "run-manifest.json").read_text()
            )
            root_manifest = json.loads((output / "run-manifest.json").read_text())
            root_manifest = json.loads((output / "run-manifest.json").read_text())

        self.assertEqual(factory_calls.value, 1)
        self.assertEqual(cleanup_calls.value, 0)
        self.assertEqual(phase_manifest["status"], "failed")
        self.assertEqual(root_manifest["status"], "failed")
        self.assertEqual(
            phase_manifest["cleanup"]["status"],
            "not_attempted_server_completion_unknown",
        )

    def test_inline_unresolved_execution_skips_cleanup_and_later_phase(self):
        """捕获显式 inline diagnostic 在未知完成后继续 cleanup 或下一 phase。"""
        adapters = []

        def adapter_factory(namespace, _phase, _seed):
            adapter = FakeAdapter(namespace)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, _seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        def unresolved_phase_runner(_targets, _schedules, **_kwargs):
            raise UnresolvedExecution({
                "status": "unresolved_execution", "reason": "inline_test",
            })

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "unresolved execution"):
                run_interference(
                    adapter_factory, targets_factory, output, scope="diagnostic",
                    phases=FIXED_PHASES[:2], phase_runner=unresolved_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    process_policy=INLINE_PROCESS_POLICY,
                )
            phase_manifest = json.loads(
                (output / "quiet" / "run-manifest.json").read_text()
            )

        self.assertEqual(len(adapters), 1)
        self.assertFalse(adapters[0].cleaned)
        self.assertEqual(
            phase_manifest["cleanup"]["status"],
            "not_attempted_server_completion_unknown",
        )
        self.assertEqual(
            phase_manifest["execution_resolution"]["server_side_completion"], "unknown",
        )

    def test_inline_rejects_duplicate_namespace_before_second_adapter(self):
        """捕获 inline diagnostic 在重复 namespace 中建立第二个 adapter。"""
        factory_calls = []

        def adapter_factory(namespace, _phase, _seed):
            factory_calls.append(namespace)
            return FakeAdapter(namespace)

        def targets_factory(adapter, phase, _seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "fresh namespaces"):
                run_interference(
                    adapter_factory, targets_factory, Path(directory), scope="diagnostic",
                    phases=FIXED_PHASES[:2], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    namespace_factory=lambda _phase: "jsons3_duplicate",
                    process_policy=INLINE_PROCESS_POLICY,
                )

        self.assertEqual(factory_calls, ["jsons3_duplicate"])

    def test_process_diagnostic_rebuilds_results_with_parent_only_artifact_writer(self):
        """捕获 child 构造 adapter 或 parent 单写者未接入真实 orchestrator。"""
        parent_pid = os.getpid()
        context = multiprocessing.get_context("fork")
        parent_writes = context.Value("i", 0)
        child_writes = context.Value("i", 0)
        parent_jsonl_writes = context.Value("i", 0)
        child_jsonl_writes = context.Value("i", 0)
        original_jsonl_writer = interference_runner._write_jsonl_atomic

        def adapter_factory(namespace, _phase, _seed):
            adapter = FakeAdapter(namespace)
            creator_pid = os.getpid()
            root_status = json.loads((output / "run-manifest.json").read_text())["status"]
            phase_status = json.loads(
                (output / "quiet" / "run-manifest.json").read_text()
            )["status"]
            adapter.create = lambda: {
                "database": adapter.database, "ddl": "CREATE TABLE events",
                "creator_pid": creator_pid, "root_status_before_factory": root_status,
                "phase_status_before_factory": phase_status,
            }
            return adapter

        def targets_factory(adapter, phase, _seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        def manifest_writer(path, manifest):
            counter = parent_writes if os.getpid() == parent_pid else child_writes
            with counter.get_lock():
                counter.value += 1
            write_manifest_atomic(path, manifest)

        def jsonl_writer(path, results):
            counter = (
                parent_jsonl_writes if os.getpid() == parent_pid else child_jsonl_writes
            )
            with counter.get_lock():
                counter.value += 1
            original_jsonl_writer(path, results)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            interference_runner._write_jsonl_atomic = jsonl_writer
            try:
                result = run_interference(
                    adapter_factory, targets_factory, output, scope="diagnostic",
                    phases=FIXED_PHASES[:1], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    namespace_factory=lambda phase: "jsons3_process_" + phase.name,
                    manifest_writer=manifest_writer,
                    process_policy=PhaseProcessPolicy(watchdog_seconds=2.0),
                )
            finally:
                interference_runner._write_jsonl_atomic = original_jsonl_writer
            phase_manifest = json.loads(
                (output / "quiet" / "run-manifest.json").read_text()
            )
            measured = (output / "quiet" / "samples.jsonl").read_text().splitlines()

        self.assertEqual(result.manifest["status"], "complete")
        self.assertEqual(phase_manifest["status"], "complete")
        self.assertNotEqual(phase_manifest["layout_definition"]["creator_pid"], parent_pid)
        self.assertEqual(
            phase_manifest["layout_definition"]["root_status_before_factory"], "running",
        )
        self.assertEqual(
            phase_manifest["layout_definition"]["phase_status_before_factory"], "running",
        )
        self.assertGreater(parent_writes.value, 0)
        self.assertEqual(child_writes.value, 0)
        self.assertEqual(parent_jsonl_writes.value, 2)
        self.assertEqual(child_jsonl_writes.value, 0)
        self.assertEqual(len(measured), 2)
        self.assertEqual(len(phase_manifest["snapshots"]), 3)
        self.assertEqual(len(phase_manifest["query_evidence"]["query_finish"]), 4)
        self.assertEqual(set(phase_manifest["statistics"]), {"list", "preview"})

    def test_process_failure_preserves_segment_and_suppresses_later_phase_factory(self):
        """捕获 segment 后失败丢样本或继续建立下一 phase adapter。"""
        context = multiprocessing.get_context("fork")
        factory_calls = context.Value("i", 0)

        def adapter_factory(namespace, _phase, _seed):
            with factory_calls.get_lock():
                factory_calls.value += 1
            return FakeAdapter(namespace, fail_storage_call=2)

        def targets_factory(adapter, phase, _seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                run_interference(
                    adapter_factory, targets_factory, output, scope="diagnostic",
                    phases=FIXED_PHASES[:2], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    process_policy=PhaseProcessPolicy(watchdog_seconds=2.0),
                )
            warmup = (output / "quiet" / "warmup-samples.jsonl").read_text().splitlines()
            phase_manifest = json.loads(
                (output / "quiet" / "run-manifest.json").read_text()
            )
            root_manifest = json.loads((output / "run-manifest.json").read_text())

        self.assertEqual(factory_calls.value, 1)
        self.assertEqual(len(warmup), 2)
        self.assertEqual(phase_manifest["status"], "failed")
        self.assertTrue(phase_manifest["cleanup"]["removed"])
        self.assertEqual(root_manifest["status"], "failed")

    def test_process_watchdog_keeps_namespace_when_server_completion_is_unknown(self):
        """捕获 killed child 后 cleanup 或下一 phase 仍被执行。"""
        context = multiprocessing.get_context("fork")
        factory_calls = context.Value("i", 0)
        cleanup_calls = context.Value("i", 0)

        def adapter_factory(namespace, _phase, _seed):
            with factory_calls.get_lock():
                factory_calls.value += 1
            adapter = FakeAdapter(namespace)

            def cleanup():
                with cleanup_calls.get_lock():
                    cleanup_calls.value += 1
                return CleanupResult(adapter.database, True)

            adapter.cleanup = cleanup
            return adapter

        def blocking_phase_runner(_targets, _schedules, **_kwargs):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            threading.Event().wait()

        def targets_factory(adapter, phase, _seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "watchdog_timeout"):
                run_interference(
                    adapter_factory, targets_factory, output, scope="diagnostic",
                    phases=FIXED_PHASES[:2], phase_runner=blocking_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    process_policy=PhaseProcessPolicy(
                        watchdog_seconds=0.15, terminate_join_seconds=0.05,
                        kill_join_seconds=0.05, poll_interval_seconds=0.005,
                    ),
                )
            elapsed = time.monotonic() - started
            phase_manifest = json.loads(
                (output / "quiet" / "run-manifest.json").read_text()
            )
            root_manifest = json.loads((output / "run-manifest.json").read_text())

        self.assertLess(elapsed, 0.6)
        self.assertEqual(factory_calls.value, 1)
        self.assertEqual(cleanup_calls.value, 0)
        self.assertEqual(root_manifest["status"], "failed")
        self.assertEqual(
            phase_manifest["cleanup"]["status"],
            "not_attempted_server_completion_unknown",
        )
        self.assertFalse(phase_manifest["cleanup"]["removed"])
        self.assertEqual(
            phase_manifest["execution_resolution"]["server_side_completion"], "unknown",
        )

    def test_blocking_manifest_write_is_interrupted_without_retrying_that_writer(self):
        """捕获 parent writer 超时后使用同一已知阻塞 writer 重试失败发布。"""
        context = multiprocessing.get_context("fork")
        receive_result, send_result = context.Pipe(duplex=False)

        def invoke():
            receive_result.close()
            reader_errors = []
            previous_excepthook = threading.excepthook
            threading.excepthook = lambda args: reader_errors.append(
                str(args.exc_value) or type(args.exc_value).__name__
            )
            read_fd, write_fd = os.pipe()
            flags = fcntl.fcntl(write_fd, fcntl.F_GETFL)
            fcntl.fcntl(write_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            try:
                while True:
                    os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                pass
            fcntl.fcntl(write_fd, fcntl.F_SETFL, flags)
            attempts = 0

            def blocking_writer(_path, _manifest):
                nonlocal attempts
                attempts += 1
                if attempts == 4:
                    os.write(write_fd, b"x")

            def targets_factory(adapter, phase, _seed):
                return {
                    name: fixed_query_target(adapter, name)
                    for name in fixed_phase_schedules(phase, measurement=True)
                }

            def blocking_phase_runner(_targets, _schedules, **_kwargs):
                threading.Event().wait()

            try:
                with tempfile.TemporaryDirectory() as directory:
                    try:
                        run_interference(
                            lambda namespace, *_: FakeAdapter(namespace),
                            targets_factory, Path(directory), scope="diagnostic",
                            phases=FIXED_PHASES[:1],
                            phase_runner=blocking_phase_runner,
                            resource_collector=lambda: {
                                "cpu": {}, "memory": {}, "io": {},
                            },
                            manifest_writer=blocking_writer,
                            process_policy=PhaseProcessPolicy(
                                watchdog_seconds=0.1, terminate_join_seconds=0.05,
                                kill_join_seconds=0.05, poll_interval_seconds=0.005,
                            ),
                        )
                    except UnresolvedExecution as error:
                        send_result.send({
                            "attempts": attempts, "evidence": error.evidence,
                            "reader_errors": reader_errors,
                        })
            finally:
                threading.excepthook = previous_excepthook
                os.close(read_fd)
                os.close(write_fd)
                send_result.close()

        worker = context.Process(target=invoke)
        worker.start()
        send_result.close()
        worker.join(2.0)
        if worker.is_alive():
            worker.terminate()
            worker.join(1.0)
            self.fail("blocking manifest writer exceeded the outer two-second watchdog")

        self.assertEqual(worker.exitcode, 0)
        self.assertTrue(receive_result.poll(0.1))
        result = receive_result.recv()
        receive_result.close()
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(result["reader_errors"], [])
        self.assertEqual(
            result["evidence"]["artifact_publication"]["status"], "unavailable",
        )
        self.assertEqual(
            result["evidence"]["artifact_publication"]["reason"],
            "parent_event_timeout",
        )

    def test_formal_coverage_requires_actual_warmup_and_measurement_windows(self):
        """捕获仅记录 30/300 配置却没有覆盖对应实际 wall time。"""
        def results(duration):
            return {
                name: LoadResult(
                    name, 0.0, duration, duration, schedule.rate_per_second,
                    0, 0, 0, 0, 0, 0, 0, 0, (),
                )
                for name, schedule in fixed_phase_schedules(
                    FIXED_PHASES[0], measurement=duration >= 300.0,
                ).items()
            }

        with self.assertRaisesRegex(RuntimeError, "warmup.*actual duration"):
            validate_formal_phase_coverage(
                FIXED_PHASES[0], results(29.9), results(300.0),
            )

        coverage = validate_formal_phase_coverage(
            FIXED_PHASES[0], results(30.0), results(300.0),
        )
        self.assertEqual(coverage["warmup_actual_seconds"], 30.0)
        self.assertEqual(coverage["measurement_actual_seconds"], 300.0)

    def test_formal_resource_snapshot_requires_cpu_memory_and_io(self):
        """捕获平台资源缺项仍被发布为 formal complete。"""
        resources = {
            "cpu": {"status": "available", "ticks": [1, 2, 3]},
            "memory": {"status": "available", "total_kib": 100,
                       "available_kib": 50},
            "io": {"status": "available", "devices": 1,
                   "read_sectors": 10, "written_sectors": 20},
        }
        self.assertEqual(validate_resource_snapshot(resources), resources)
        for missing in ("cpu", "memory", "io"):
            incomplete = {name: dict(value) for name, value in resources.items()}
            incomplete[missing] = {"status": "unavailable"}
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(RuntimeError, missing):
                    validate_resource_snapshot(incomplete)

    def test_fixed_phases_keep_foreground_and_independent_interference_contract(self):
        self.assertEqual(
            [phase.name for phase in FIXED_PHASES],
            ["quiet", "detail_2m", "trace_long", "batch_loop", "continuous_ingest"],
        )
        self.assertTrue(all(
            phase.warmup_seconds == 30.0 and phase.measurement_seconds == 300.0
            for phase in FIXED_PHASES
        ))
        expected = {
            "quiet": {},
            "detail_2m": {"detail_2m": (1.0, "fixed")},
            "trace_long": {"trace_long": (0.2, "fixed")},
            "batch_loop": {"batch_loop": (None, "continuous")},
            "continuous_ingest": {"continuous_ingest": (1.0, "fixed")},
        }
        for phase in FIXED_PHASES:
            schedules = fixed_phase_schedules(phase, measurement=True)
            self.assertEqual(
                (schedules["list"].rate_per_second, schedules["list"].workers),
                (20.0, 2),
            )
            self.assertEqual(
                (schedules["preview"].rate_per_second, schedules["preview"].workers),
                (20.0, 2),
            )
            observed = {
                name: (schedule.rate_per_second, schedule.mode)
                for name, schedule in schedules.items() if name not in {"list", "preview"}
            }
            self.assertEqual(observed, expected[phase.name])

    def test_query_target_reuses_application_ready_and_payload_truth_validation(self):
        adapter = FakeAdapter("jsons3_query")
        target = build_query_target(adapter, QUERY, TRUTH)

        sample = target(float("inf"), threading.Event())

        self.assertEqual(sample.status, "success")
        self.assertEqual(sample.response_bytes, 100)
        self.assertEqual(sample.validation.validated_payload_bytes, 3)
        self.assertGreaterEqual(
            sample.application_ready_ms,
            sample.query_complete_ms + sample.recovery_ms + sample.validation_ms,
        )

    def test_each_phase_uses_fresh_namespace_and_publishes_raw_and_physical_evidence(self):
        adapters = []
        factory_calls = []
        manifest_states = {}

        def adapter_factory(namespace, phase, seed):
            factory_calls.append((namespace, phase.name, seed))
            adapter = FakeAdapter(namespace)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, seed):
            self.assertEqual(seed, 20260907)
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        def manifest_writer(path, manifest):
            manifest_states.setdefault(str(path), []).append(manifest["status"])
            write_manifest_atomic(path, manifest)

        resources = {
            "cpu": {"status": "available", "total_ticks": 10},
            "memory": {"status": "available", "available_kib": 20},
            "io": {"status": "available", "read_sectors": 30, "written_sectors": 40},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "interference"
            result = run_interference(
                adapter_factory, targets_factory, output,
                scope="diagnostic",
                phases=FIXED_PHASES[:2], phase_runner=short_phase_runner,
                resource_collector=lambda: resources,
                namespace_factory=lambda phase: "jsons3_if_" + phase.name,
                manifest_writer=manifest_writer,
                process_policy=INLINE_PROCESS_POLICY,
            )
            quiet_manifest = json.loads((output / "quiet" / "run-manifest.json").read_text())
            detail_manifest = json.loads((output / "detail_2m" / "run-manifest.json").read_text())
            measured_lines = (output / "detail_2m" / "samples.jsonl").read_text().splitlines()

        self.assertEqual(result.manifest["status"], "complete")
        self.assertEqual(result.manifest["execution_scope"], "diagnostic")
        self.assertEqual(result.manifest["classification"], "diagnostic_complete")
        self.assertEqual(len({call[0] for call in factory_calls}), 2)
        self.assertEqual({call[2] for call in factory_calls}, {20260907})
        self.assertTrue(all(adapter.cleaned for adapter in adapters))
        self.assertEqual(quiet_manifest["status"], "complete")
        self.assertEqual(detail_manifest["status"], "complete")
        self.assertEqual(detail_manifest["classification"], "diagnostic_complete")
        self.assertEqual(len(detail_manifest["snapshots"]), 3)
        self.assertEqual(
            detail_manifest["snapshots"][0]["storage"]["tables"]["events"]["part_count"], 2,
        )
        self.assertEqual(detail_manifest["snapshots"][0]["storage"]["merges"][0]["table"], "events")
        self.assertEqual(detail_manifest["snapshots"][0]["resources"]["io"]["status"], "available")
        self.assertEqual(len(detail_manifest["query_evidence"]["query_finish"]), 6)
        self.assertEqual(len(measured_lines), 3)
        self.assertTrue(all(
            states == ["running", "complete"] for states in manifest_states.values()
        ))

    def test_all_streams_use_the_same_actual_phase_wall_time(self):
        adapters = []

        def adapter_factory(namespace, phase, seed):
            adapter = FakeAdapter(namespace)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        def uneven_phase_runner(targets, schedules, **kwargs):
            results = short_phase_runner(targets, schedules, **kwargs)
            return {
                name: replace(
                    result,
                    phase_finished=result.phase_origin + (2.0 if name == "preview" else 1.0),
                    phase_wall_seconds=2.0 if name == "preview" else 1.0,
                )
                for name, result in results.items()
            }

        with tempfile.TemporaryDirectory() as directory:
            result = run_interference(
                adapter_factory, targets_factory, Path(directory) / "interference",
                scope="diagnostic",
                phases=FIXED_PHASES[:1], phase_runner=uneven_phase_runner,
                resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                namespace_factory=lambda phase: "jsons3_wall_" + phase.name,
                process_policy=INLINE_PROCESS_POLICY,
            )

        statistics = result.phases[0]["statistics"]
        self.assertEqual(statistics["list"]["phase_wall_seconds"], 2.0)
        self.assertEqual(statistics["preview"]["phase_wall_seconds"], 2.0)

    def test_query_finish_collection_is_bounded_for_formal_request_counts(self):
        adapters = []

        def adapter_factory(namespace, phase, seed):
            adapter = FakeAdapter(namespace)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        def many_samples_runner(targets, schedules, **_):
            results = {}
            for name, target in targets.items():
                clock = FakeClock()
                results[name] = run_offered_load(
                    target, LoadSchedule(name, 101.0, 1.0, 2, timeout_seconds=2.0),
                    clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
                )
            return results

        with tempfile.TemporaryDirectory() as directory:
            run_interference(
                adapter_factory, targets_factory, Path(directory) / "interference",
                scope="diagnostic",
                phases=FIXED_PHASES[:1], phase_runner=many_samples_runner,
                resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                namespace_factory=lambda phase: "jsons3_batches_" + phase.name,
                process_policy=INLINE_PROCESS_POLICY,
            )

        self.assertGreater(len(adapters[0].evidence_batches), 1)
        self.assertTrue(all(len(batch) <= 200 for batch in adapters[0].evidence_batches))

    def test_missing_query_finish_fails_manifest_but_cleanup_still_runs(self):
        adapters = []

        def adapter_factory(namespace, phase, seed):
            adapter = FakeAdapter(namespace, omit_query_finish=True)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "interference"
            with self.assertRaisesRegex(RuntimeError, "QueryFinish"):
                run_interference(
                    adapter_factory, targets_factory, output,
                    scope="diagnostic",
                    phases=FIXED_PHASES[:1], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    namespace_factory=lambda phase: "jsons3_fail_" + phase.name,
                    process_policy=INLINE_PROCESS_POLICY,
                )
            phase_manifest = json.loads((output / "quiet" / "run-manifest.json").read_text())
            root_manifest = json.loads((output / "run-manifest.json").read_text())
            failed_samples = (output / "quiet" / "samples.jsonl").read_text().splitlines()

        self.assertTrue(adapters[0].cleaned)
        self.assertEqual(phase_manifest["status"], "failed")
        self.assertIn("QueryFinish", phase_manifest["error"])
        self.assertEqual(root_manifest["status"], "failed")
        self.assertEqual(len(failed_samples), 2)

    def test_missing_query_plan_fails_manifest_but_preserves_cleanup(self):
        """捕获成功查询缺少 plan 时仍发布完整 interference 证据。"""
        adapters = []

        def adapter_factory(namespace, phase, seed):
            adapter = FakeAdapter(namespace, omit_plans=True)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "query plans"):
                run_interference(
                    adapter_factory, targets_factory, Path(directory),
                    scope="diagnostic", phases=FIXED_PHASES[:1],
                    phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    process_policy=INLINE_PROCESS_POLICY,
                )
        self.assertTrue(adapters[0].cleaned)

    def test_missing_query_access_details_fails_manifest(self):
        """捕获成功查询仅有 plan 和 QueryFinish 时仍发布完整证据。"""
        def adapter_factory(namespace, phase, seed):
            return FakeAdapter(namespace, omit_query_details=True)

        def targets_factory(adapter, phase, seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "access details"):
                run_interference(
                    adapter_factory, targets_factory, Path(directory),
                    scope="diagnostic", phases=FIXED_PHASES[:1],
                    phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    process_policy=INLINE_PROCESS_POLICY,
                )

    def test_continuous_ingest_success_requires_block_result_evidence(self):
        adapters = []

        def adapter_factory(namespace, phase, seed):
            adapter = FakeAdapter(namespace)
            adapters.append(adapter)
            return adapter

        def targets_factory(adapter, phase, seed):
            return {
                "list": fixed_query_target(adapter, "list"),
                "preview": fixed_query_target(adapter, "preview"),
                "continuous_ingest": deadline_target(lambda: success_sample()),
            }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "BlockResult"):
                run_interference(
                    adapter_factory, targets_factory, Path(directory) / "interference",
                    scope="diagnostic",
                    phases=FIXED_PHASES[-1:], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    namespace_factory=lambda phase: "jsons3_ingest_" + phase.name,
                    process_policy=INLINE_PROCESS_POLICY,
                )

        self.assertTrue(adapters[0].cleaned)

    def test_each_completed_segment_is_persisted_before_later_snapshot_failure(self):
        """捕获后置物理快照失败时丢失已形成的逐请求分类。"""
        def targets_factory(adapter, phase, seed):
            return {
                name: fixed_query_target(adapter, name)
                for name in fixed_phase_schedules(phase, measurement=True)
            }

        for fail_call, expected_files in (
            (2, ("warmup-samples.jsonl",)),
            (3, ("warmup-samples.jsonl", "samples.jsonl")),
        ):
            with self.subTest(fail_call=fail_call), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                with self.assertRaisesRegex(RuntimeError, "snapshot failed"):
                    run_interference(
                        lambda namespace, *_: FakeAdapter(
                            namespace, fail_storage_call=fail_call,
                        ),
                        targets_factory, output, scope="diagnostic",
                        phases=FIXED_PHASES[:1], phase_runner=short_phase_runner,
                        resource_collector=lambda: {
                            "cpu": {}, "memory": {}, "io": {},
                        },
                        process_policy=INLINE_PROCESS_POLICY,
                    )
                for name in expected_files:
                    records = [json.loads(line) for line in (
                        output / "quiet" / name
                    ).read_text().splitlines()]
                    self.assertEqual(len(records), 2)
                    self.assertEqual({record["status"] for record in records}, {"success"})


if __name__ == "__main__":
    unittest.main()
