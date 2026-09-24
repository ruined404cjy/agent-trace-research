import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "tools"))
sys.path.insert(0, str(STAGE_DIR / "report"))

import pack_handback
import yellow_round as driver


class PlanTest(unittest.TestCase):
    """驱动脚本写出的目录必须正是打包脚本读取的目录，否则结果会在打包时静默缺失。"""

    def test_every_run_directory_the_packer_reads_is_produced_by_one_step(self):
        root = Path("/runs")
        produced = {step.output for phase in ("xstore", "clickhouse")
                    for step in driver.plan_steps(phase, Path("/input"), root, 18123)}
        for key, path in pack_handback.run_paths(root).items():
            family, engine, layout = key
            expected = path.parents[1] if family == "matrix" else path
            self.assertIn(expected, produced, key)
        self.assertIn(root / "xstore-row-probe.json", produced)

    def test_matrix_step_lists_all_workloads_in_one_call(self):
        step = driver.plan_steps("clickhouse", Path("/input"), Path("/runs"), 18123)[0]
        workloads = step.command[step.command.index("--workloads") + 1]
        self.assertEqual(workloads.split(","), list(driver.WORKLOADS))
        self.assertEqual(step.command[-2:], ["--clickhouse-port", "18123"])


class RunStepsTest(unittest.TestCase):
    """验证续跑、保留未完成尝试与失败即停。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root))

    def step(self, name):
        output = self.root / name
        return driver.Step(name, output, [output / "run-manifest.json"], ["run", name], "intent " + name)

    def finish(self, step, status="complete"):
        step.output.mkdir(parents=True, exist_ok=True)
        (step.output / "run-manifest.json").write_text(json.dumps({"status": status}))

    def test_completed_steps_are_skipped_and_an_incomplete_attempt_is_kept(self):
        done, stale = self.step("done"), self.step("stale")
        self.finish(done)
        self.finish(stale, status="failed")
        calls = []

        def runner(command, **_):
            calls.append(command[-1])
            self.finish(stale)
            return SimpleNamespace(returncode=0)

        failed = driver.run_steps([done, stale], self.root / "logs", runner=runner, out=lambda _: None)
        self.assertIsNone(failed)
        self.assertEqual(calls, ["stale"])
        kept = [path.name for path in self.root.iterdir() if path.name.startswith("stale.attempt-")]
        self.assertEqual(len(kept), 1)
        self.assertIn("$ run stale", (self.root / "logs" / "stale.log").read_text())

    def test_a_failed_step_stops_the_phase_and_names_the_step(self):
        first, second = self.step("first"), self.step("second")
        calls = []

        def runner(command, **_):
            calls.append(command[-1])
            return SimpleNamespace(returncode=3)

        messages = []
        failed = driver.run_steps([first, second], self.root / "logs", runner=runner, out=messages.append)
        self.assertEqual(failed, "first")
        self.assertEqual(calls, ["first"])
        self.assertTrue(any(message.startswith("[fail] first") for message in messages))

    def test_skipped_steps_do_not_run(self):
        step = self.step("skipped")
        failed = driver.run_steps([step], self.root / "logs", skip={"skipped"},
                                  runner=lambda *_, **__: self.fail("must not run"), out=lambda _: None)
        self.assertIsNone(failed)

    def test_move_aside_uses_a_timestamped_name(self):
        path = self.root / "attempt"
        path.mkdir()
        moved = driver.move_aside(path, clock=lambda: datetime.datetime(2026, 9, 25, 8, 30, 0))
        self.assertEqual(moved.name, "attempt.attempt-20260925T083000")
        self.assertFalse(path.exists())


class HostCheckTest(unittest.TestCase):
    """主机检查按反馈契约第 3 节的阈值判定。"""

    def test_thresholds(self):
        healthy = driver.evaluate_host(1.0, 256, 400 * 1024 * 1024, 500 * 1024 ** 3, [10, 20, 30])
        self.assertEqual(healthy, [])
        failures = driver.evaluate_host(30.0, 256, 16 * 1024 * 1024, 100 * 1024 ** 3, [12000])
        self.assertEqual(len(failures), 4)

    def test_vmstat_blocks_out_takes_the_last_three_samples(self):
        text = ("procs -----------memory---------- ---swap-- -----io----\n"
                " r  b   swpd   free   buff  cache   si   so    bi    bo\n"
                " 1  0      0 100 1 1 0 0 5 900\n 1 0 0 100 1 1 0 0 5 11\n"
                " 1 0 0 100 1 1 0 0 5 12\n 1 0 0 100 1 1 0 0 5 13\n")
        self.assertEqual(driver._vmstat_blocks_out(text), [11, 12, 13])

    def test_preflight_reports_missing_identity_and_a_nonstandard_port(self):
        with tempfile.TemporaryDirectory() as directory:
            problems = driver.preflight_problems({"CH_HTTP_PORT": "8123"}, Path(directory))
        joined = "\n".join(problems)
        for expected in ("XSTORE_USER", "GAUSSDB_LIB_DIR", "XSTORE_NATIVE_ENGINE", "CH_NATIVE_PRODUCT_PATH",
                         "XSTORE_SOURCE_COMMIT", "CH_HTTP_PORT must be 18123", "frozen input"):
            self.assertIn(expected, joined)


if __name__ == "__main__":
    unittest.main()
