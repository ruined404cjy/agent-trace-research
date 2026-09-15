import json
import sys
import tempfile
import unittest
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import (
    AccessEvidence, AssetStorageEvidence, BlockResult, CleanupResult, MaintenanceResult,
    QueryResult, QuerySpec, StorageEvidence, build_layout_catalog,
)
from run_clickhouse_part_states import capture_part_state, run_part_states, wait_stable
from run_layout_matrix import QueryTruth


ROW = {
    "event_id": "event-a", "trace_id": "trace-a", "project_id": "project-a",
    "start_time": "2030-01-01T00:00:00.000Z", "profile": "text_64k",
    "content_type": "application/json", "encoding": "utf-8", "content_length": 3,
    "preview": "abc",
    "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    "payload": b"abc",
}
QUERY = QuerySpec("detail", {
    "project_id": "project-a", "trace_id": "trace-a",
    "start_time": "2030-01-01T00:00:00.000Z", "event_id": "event-a",
})
TRUTH = QueryTruth("detail:text_64k", (ROW,))


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class FakeClickHouse:
    """用确定性物理观测替代数据库边界，保留 runner 的真实状态机。"""

    def __init__(self, layout="same_table", fragmented_parts=4,
                 fail_query=False, omit_query_finish=False, fail_restore=False):
        self.layout = layout
        self.targets = build_layout_catalog(layout).write_tables
        self.fragmented_parts = fragmented_parts
        self.fail_query = fail_query
        self.omit_query_finish = omit_query_finish
        self.fail_restore = fail_restore
        self.events = []
        self.paused = set()
        self.merges_enabled = True
        self.forced = False
        self.query_count = 0
        self.current_state = "new"
        self.merge_observations = [
            (4, ({"table": build_layout_catalog(layout).list_source, "elapsed": 0.1},)),
            (3, ({"table": build_layout_catalog(layout).list_source, "elapsed": 0.2},)),
            (2, ()), (2, ()), (2, ()),
        ]

    def create(self):
        self.events.append("create")
        return {"database": "jsons3_fake", "ddl": "CREATE TABLE"}

    def set_merges(self, enabled):
        self.events.append("merges:start" if enabled else "merges:stop")
        if enabled and self.fail_restore and self.fail_query:
            raise RuntimeError("restore failed")
        self.merges_enabled = enabled
        if enabled:
            self.paused.clear()
        else:
            self.paused.update(self.targets)
            self.current_state = "fragmented"

    def ingest_block(self, block):
        watermark = block[-1]["ingest_seq"] + 1
        self.events.append(f"ingest:{watermark}")
        return BlockResult(len(block), watermark, {table: watermark for table in self.targets}, 0.1)

    def wait_write_complete(self, watermark):
        return MaintenanceResult(True, {table: watermark for table in self.targets})

    def collect_storage(self):
        controlled = build_layout_catalog(self.layout).list_source
        if not self.merges_enabled:
            part_count, merges = self.fragmented_parts, ()
            self.current_state = "fragmented"
        elif self.forced:
            part_count, merges = 1, ()
            self.current_state = "single_part"
        else:
            part_count, merges = self.merge_observations.pop(0) if self.merge_observations else (2, ())
            self.current_state = "merging" if merges else "stable"
        self.events.append(f"storage:{self.current_state}:{part_count}")
        tables = {
            table: {
                "part_count": part_count if table == controlled else max(1, part_count - 1),
                "marks": 10, "compressed_bytes": 100, "uncompressed_bytes": 200,
                "columns": {},
            }
            for table in self.targets
        }
        asset_store = (
            AssetStorageEvidence(2, 300, 0, 0) if self.layout == "asset_ref" else None
        )
        return StorageEvidence(tables, merges, asset_store)

    def force_single_part(self):
        self.events.append("force:single_part")
        self.forced = True
        return self.targets

    def run_query(self, query):
        self.events.append(f"query:{self.current_state}")
        if self.fail_query:
            raise RuntimeError("query failed")
        self.query_count += 1
        return QueryResult(f"query-{self.query_count}", (ROW,), 100, 100, 0, 0.1, 0.1)

    def collect_access_evidence(self, query_ids):
        finish_ids = query_ids[:-1] if self.omit_query_finish else query_ids
        return AccessEvidence(
            {query_id: "ReadFromMergeTree" for query_id in query_ids},
            query_finish={query_id: {"type": "QueryFinish", "exception_code": 0}
                          for query_id in finish_ids},
        )

    def cleanup(self):
        self.events.append("cleanup")
        return CleanupResult("jsons3_fake", not self.paused)


def blocks():
    return ([{"ingest_seq": 0}], [{"ingest_seq": 1}])


class ClickHousePartStateTest(unittest.TestCase):
    def test_runner_proves_each_state_before_sampling_and_matches_query_finish(self):
        adapter = FakeClickHouse()
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            result = run_part_states(
                adapter, blocks(), ((QUERY, TRUTH),), Path(directory),
                clock=clock, sleep=clock.sleep, poll_interval=0.1,
            )

        self.assertEqual(
            [state.name for state in result.states],
            ["fragmented", "merging", "stable", "single_part"],
        )
        self.assertTrue(all(state.predicate_proven for state in result.states))
        self.assertTrue(all(
            state.query_finish_count == state.successful_samples == 1
            for state in result.states
        ))
        first_sample = result.states[0].query_samples[0]
        self.assertEqual(first_sample.status, "success")
        self.assertEqual(first_sample.response_bytes, 100)
        self.assertEqual(first_sample.validation.validated_payload_bytes, 3)
        self.assertGreaterEqual(
            first_sample.application_ready_ms,
            first_sample.query_complete_ms + first_sample.recovery_ms + first_sample.validation_ms,
        )
        self.assertEqual(
            [event for event in adapter.events if event.startswith("query:")],
            ["query:fragmented", "query:merging", "query:stable", "query:single_part"],
        )
        stable_query = adapter.events.index("query:stable")
        self.assertEqual(
            adapter.events[stable_query - 3:stable_query],
            ["storage:stable:2", "storage:stable:2", "storage:stable:2"],
        )
        self.assertLess(stable_query, adapter.events.index("force:single_part"))
        self.assertEqual(result.manifest["status"], "complete")
        self.assertTrue(result.manifest["restoration"]["restored"])

    def test_unproven_fragmented_state_is_never_sampled(self):
        adapter = FakeClickHouse(fragmented_parts=1)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "fragmented state was not proven"):
                run_part_states(
                    adapter, blocks(), ((QUERY, TRUTH),), output,
                    timeout_seconds=0.2, clock=clock, sleep=clock.sleep, poll_interval=0.1,
                )
            manifest = json.loads((output / "run-manifest.json").read_text())

        self.assertFalse(any(event.startswith("query:") for event in adapter.events))
        self.assertEqual(adapter.events.count("merges:start"), 1)
        self.assertFalse(adapter.paused)
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse(manifest["states"][0]["predicate_proven"])

    def test_query_failure_restores_every_paused_table(self):
        adapter = FakeClickHouse(layout="full_core", fail_query=True)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "query sampling failed"):
                run_part_states(
                    adapter, blocks(), ((QUERY, TRUTH),), Path(directory),
                    clock=clock, sleep=clock.sleep, poll_interval=0.1,
                )

        self.assertFalse(adapter.paused)
        self.assertIn("merges:start", adapter.events)
        self.assertEqual(adapter.events[-1], "cleanup")

    def test_restoration_failure_publishes_every_paused_target(self):
        adapter = FakeClickHouse(layout="full_core", fail_query=True, fail_restore=True)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "merge restoration failed"):
                run_part_states(
                    adapter, blocks(), ((QUERY, TRUTH),), output,
                    clock=clock, sleep=clock.sleep, poll_interval=0.1,
                )
            manifest = json.loads((output / "run-manifest.json").read_text())

        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(
            manifest["restoration"]["targets"], ["events_full", "events_core"],
        )
        self.assertFalse(manifest["restoration"]["restored"])
        self.assertEqual(manifest["restoration"]["error"], "restore failed")

    def test_missing_query_finish_fails_the_state_run(self):
        adapter = FakeClickHouse(omit_query_finish=True)
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "QueryFinish"):
                run_part_states(
                    adapter, blocks(), ((QUERY, TRUTH),), output,
                    samples_per_query=2, clock=clock, sleep=clock.sleep, poll_interval=0.1,
                )
            manifest = json.loads((output / "run-manifest.json").read_text())
        self.assertFalse(adapter.paused)
        self.assertEqual(len(manifest["states"][0]["query_samples"]), 2)
        self.assertEqual(manifest["states"][0]["query_finish_count"], 1)

    def test_wait_stable_requires_three_empty_unchanged_observations(self):
        adapter = FakeClickHouse()
        clock = FakeClock()

        state = wait_stable(
            adapter, consecutive_empty=3, timeout_seconds=1,
            clock=clock, sleep=clock.sleep, poll_interval=0.1,
        )

        self.assertTrue(state.predicate_proven)
        self.assertEqual(len(state.observations), 5)
        self.assertEqual([item["active_part_counts"]["events"]
                          for item in state.observations[-3:]], [2, 2, 2])

    def test_capture_records_asset_catalog_but_controls_analysis_table(self):
        adapter = FakeClickHouse(layout="asset_ref")
        adapter.set_merges(False)

        state = capture_part_state(adapter)

        self.assertEqual(state.controlled_table, "events_analytics")
        self.assertEqual(set(state.tables), {"events_analytics", "assets"})
        self.assertIn("compressed_bytes", state.tables["assets"])
        self.assertEqual(state.asset_store.available_object_count, 2)


if __name__ == "__main__":
    unittest.main()
