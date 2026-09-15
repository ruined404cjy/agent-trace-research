import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import AccessEvidence, CleanupResult, QueryResult, QuerySpec, StorageEvidence
from run_interference import (
    FIXED_PHASES, LoadSchedule, build_query_target, fixed_phase_schedules,
    run_interference, run_offered_load, summarize_load,
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
        return self.clock() >= self.ready_at


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


def success_sample(latency_ms=1.0):
    return SimpleNamespace(status="success", application_ready_ms=latency_ms)


class FakeAdapter:
    layout = "same_table"

    def __init__(self, namespace, omit_query_finish=False):
        self.database = namespace + "_same_table"
        self.omit_query_finish = omit_query_finish
        self.query_count = 0
        self.cleaned = False
        self.evidence_batches = []

    def create(self):
        return {"database": self.database, "ddl": "CREATE TABLE events"}

    def run_query(self, query):
        self.query_count += 1
        row = dict(ROW)
        if query.kind == "list":
            row["preview"] = None
        if query.kind in {"list", "preview"}:
            row["payload"] = None
        return QueryResult(
            f"query-{self.database}-{self.query_count}", (row,), 100, 100, 0, 0.4, 0.1,
        )

    def collect_access_evidence(self, query_ids):
        self.evidence_batches.append(tuple(query_ids))
        finished = query_ids[:-1] if self.omit_query_finish else query_ids
        return AccessEvidence(
            {query_id: "ReadFromMergeTree" for query_id in query_ids},
            query_finish={
                query_id: {"type": "QueryFinish", "exception_code": 0,
                           "read_rows": 1, "read_bytes": 100}
                for query_id in finished
            },
        )

    def collect_storage(self):
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
    def test_arrival_deadlines_share_one_origin_and_do_not_drift_with_completion(self):
        """捕获下一次到达时间从上一请求完成时刻串行推导。"""
        clock = FakeClock(10.0)
        starts = []

        def target():
            starts.append(clock())
            clock.sleep(0.2)
            return success_sample(200.0)

        result = run_offered_load(
            target, LoadSchedule("list", 2.0, 2.0, 1, timeout_seconds=1.0),
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
            target,
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
            target, LoadSchedule("list", 2.0, 1.0, 1, timeout_seconds=2.0),
            clock=clock, sleep=clock.sleep, executor_factory=InlineExecutor,
        )
        summary = summarize_load(result)

        self.assertEqual(result.phase_wall_seconds, 1.5)
        self.assertAlmostEqual(summary["completed_throughput_requests_s"], 2 / 1.5)

    def test_p99_is_unavailable_below_one_thousand_successes(self):
        """捕获小样本仍发布 p99。"""
        below_clock = FakeClock()
        below = run_offered_load(
            lambda: success_sample(2.0),
            LoadSchedule("list", 999.0, 1.0, 2, timeout_seconds=1.0),
            clock=below_clock, sleep=below_clock.sleep, executor_factory=InlineExecutor,
        )
        enough_clock = FakeClock()
        enough = run_offered_load(
            lambda: success_sample(2.0),
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
            lambda: success_sample(),
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
            target,
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
            lambda: success_sample(),
            LoadSchedule("list", 1.0, 1.0, 1, timeout_seconds=1.0),
            clock=clock, sleep=clock.sleep,
            executor_factory=lambda max_workers: DelayedExecutor(max_workers, clock),
        )

        self.assertEqual(result.timed_out_requests, 1)
        self.assertEqual(result.phase_wall_seconds, 2.0)


class InterferenceRunnerTest(unittest.TestCase):
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

        sample = target()

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
                phases=FIXED_PHASES[:2], phase_runner=short_phase_runner,
                resource_collector=lambda: resources,
                namespace_factory=lambda phase: "jsons3_if_" + phase.name,
                manifest_writer=manifest_writer,
            )
            quiet_manifest = json.loads((output / "quiet" / "run-manifest.json").read_text())
            detail_manifest = json.loads((output / "detail_2m" / "run-manifest.json").read_text())
            measured_lines = (output / "detail_2m" / "samples.jsonl").read_text().splitlines()

        self.assertEqual(result.manifest["status"], "complete")
        self.assertEqual(len({call[0] for call in factory_calls}), 2)
        self.assertEqual({call[2] for call in factory_calls}, {20260907})
        self.assertTrue(all(adapter.cleaned for adapter in adapters))
        self.assertEqual(quiet_manifest["status"], "complete")
        self.assertEqual(detail_manifest["status"], "complete")
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
                phases=FIXED_PHASES[:1], phase_runner=uneven_phase_runner,
                resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                namespace_factory=lambda phase: "jsons3_wall_" + phase.name,
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
                phases=FIXED_PHASES[:1], phase_runner=many_samples_runner,
                resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                namespace_factory=lambda phase: "jsons3_batches_" + phase.name,
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
                    phases=FIXED_PHASES[:1], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    namespace_factory=lambda phase: "jsons3_fail_" + phase.name,
                )
            phase_manifest = json.loads((output / "quiet" / "run-manifest.json").read_text())
            root_manifest = json.loads((output / "run-manifest.json").read_text())
            failed_samples = (output / "quiet" / "samples.jsonl").read_text().splitlines()

        self.assertTrue(adapters[0].cleaned)
        self.assertEqual(phase_manifest["status"], "failed")
        self.assertIn("QueryFinish", phase_manifest["error"])
        self.assertEqual(root_manifest["status"], "failed")
        self.assertEqual(len(failed_samples), 2)

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
                "continuous_ingest": lambda: success_sample(),
            }

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "BlockResult"):
                run_interference(
                    adapter_factory, targets_factory, Path(directory) / "interference",
                    phases=FIXED_PHASES[-1:], phase_runner=short_phase_runner,
                    resource_collector=lambda: {"cpu": {}, "memory": {}, "io": {}},
                    namespace_factory=lambda phase: "jsons3_ingest_" + phase.name,
                )

        self.assertTrue(adapters[0].cleaned)


if __name__ == "__main__":
    unittest.main()
