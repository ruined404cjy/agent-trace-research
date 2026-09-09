import contextlib
import io
import os
import sys
import tempfile
import unittest
import uuid
import hashlib
import json
from pathlib import Path
from unittest import mock


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import run_clickhouse_mechanisms as mechanisms


class ClickHouseMechanismUnitTest(unittest.TestCase):
    """验证 Native JSON 变体、路径转换和 FINAL 契约。"""

    def test_ddls_hold_budget_hint_and_sidecar_axes_constant(self):
        """捕获 hint 对照改变预算或 Sidecar，或 Sidecar 结构串组。"""
        ddls = mechanisms.mechanism_ddls("s2sup_mech")

        self.assertEqual(set(ddls), set(mechanisms.LAYOUTS))
        self.assertIn("max_dynamic_paths=32", ddls["ch_native_hinted32_sparse"])
        self.assertIn("gen_ai.operation.name String", ddls["ch_native_hinted32_sparse"])
        self.assertNotIn("fidelity_values", ddls["ch_native_auto32_none"])
        self.assertIn("fidelity_values Map(String,String)", ddls["ch_native_auto32_sparse"])
        self.assertIn("attributes_raw String CODEC(ZSTD(3))", ddls["ch_native_auto32_full"])
        self.assertIn("fidelity_values Map(String,String)", ddls["ch_native_hinted32_sparse"])
        self.assertNotIn("attributes_raw", ddls["ch_native_hinted32_sparse"])
        for layout, ddl in ddls.items():
            with self.subTest(layout=layout):
                self.assertEqual(ddl.count("max_dynamic_paths=32"), 1)
                self.assertIn("project_id String", ddl)
                self.assertIn("start_time DateTime64(3, 'UTC')", ddl)
        self.assertIn("failure.mistake_mode String", ddls["ch_native_hinted32_sparse"])
        auto_sparse = ddls["ch_native_auto32_sparse"]
        hinted_sparse = ddls["ch_native_hinted32_sparse"].replace(
            ", gen_ai.operation.name String, failure.mistake_mode String", ""
        ).replace("ch_native_hinted32_sparse", "ch_native_auto32_sparse")
        self.assertEqual(hinted_sparse, auto_sparse)

    def test_path_transition_preserves_union_and_reports_moves(self):
        """捕获物理路径迁移被误报为逻辑路径丢失。"""
        before = {
            "dynamic_paths": ["gen_ai.operation.name", "rare.a"],
            "shared_paths": ["rare.b"],
            "query_results_sha256": "truth",
            "analysis_results_sha256": "analysis",
        }
        after = {
            "dynamic_paths": ["gen_ai.operation.name", "rare.b"],
            "shared_paths": ["rare.a"],
            "query_results_sha256": "truth",
            "analysis_results_sha256": "analysis",
        }
        expected = {"gen_ai.operation.name", "rare.a", "rare.b"}

        result = mechanisms.validate_path_transition(before, after, expected)

        self.assertTrue(result["logical_paths_preserved"])
        self.assertTrue(result["query_results_preserved"])
        self.assertTrue(result["analysis_results_preserved"])
        self.assertEqual(result["entered_dynamic"], ["rare.b"])
        self.assertEqual(result["entered_shared"], ["rare.a"])

    def test_path_transition_rejects_overlap_or_missing_path(self):
        """捕获 dynamic/shared 重叠及逻辑路径缺失。"""
        expected = {"a", "b"}
        overlap = {"dynamic_paths": ["a"], "shared_paths": ["a", "b"], "query_results_sha256": "truth", "analysis_results_sha256": "analysis"}
        missing = {"dynamic_paths": ["a"], "shared_paths": [], "query_results_sha256": "truth", "analysis_results_sha256": "analysis"}

        self.assertFalse(mechanisms.validate_path_transition(overlap, overlap, expected)["logical_paths_preserved"])
        self.assertFalse(mechanisms.validate_path_transition(missing, missing, expected)["logical_paths_preserved"])
        complete = {"dynamic_paths": ["a"], "shared_paths": ["b"]}
        with self.assertRaisesRegex(ValueError, "query_results_sha256"):
            mechanisms.validate_path_transition(complete, complete, expected)

    def test_path_transition_counts_declared_hint_paths_in_logical_union(self):
        """捕获声明类型路径从 dynamic 集合移出后被误报丢失。"""
        state = {
            "dynamic_paths": ["rare.a"],
            "shared_paths": ["rare.b"],
            "hinted_paths": ["gen_ai.operation.name", "failure.mistake_mode"],
            "query_results_sha256": "truth",
            "analysis_results_sha256": "analysis",
        }

        result = mechanisms.validate_path_transition(
            state,
            state,
            {"rare.a", "rare.b", "gen_ai.operation.name", "failure.mistake_mode"},
        )

        self.assertTrue(result["logical_paths_preserved"])

    def test_mechanism_query_sql_matches_layout_specific_execution_and_identity(self):
        """捕获查询身份遗漏 Sidecar、完整文档或 hint 路径类型改写。"""
        expected = {}
        for layout in mechanisms.LAYOUTS:
            expected[layout] = {}
            for query_id in mechanisms.QUERY_IDS:
                statement = mechanisms.ClickHouseFourLayoutAdapter.query_sql("ch_native", query_id)
                if layout == "ch_native_auto32_none":
                    statement = statement.replace(", fidelity_values", "")
                elif layout == "ch_native_auto32_full":
                    statement = statement.replace(", fidelity_values", ", attributes_raw")
                elif layout == "ch_native_hinted32_sparse":
                    statement = statement.replace(
                        "attributes.gen_ai.operation.name.:String", "attributes.gen_ai.operation.name"
                    ).replace(
                        "attributes.failure.mistake_mode.:String", "attributes.failure.mistake_mode"
                    )
                expected[layout][query_id] = statement
                self.assertEqual(mechanisms.mechanism_query_sql(layout, query_id), statement)

        identity = mechanisms._mechanism_identity("s2sup_identity")
        self.assertEqual(
            identity["queries_sha256"],
            hashlib.sha256(mechanisms.canonical_bytes(expected)).hexdigest(),
        )
        original = mechanisms.mechanism_query_sql
        with mock.patch.object(
                mechanisms,
                "mechanism_query_sql",
                side_effect=lambda layout, query_id: (
                    original(layout, query_id) + " /* changed */"
                    if (layout, query_id) == ("ch_native_hinted32_sparse", "S02")
                    else original(layout, query_id)
                ),
        ):
            changed_identity = mechanisms._mechanism_identity("s2sup_identity")
        self.assertNotEqual(changed_identity["queries_sha256"], identity["queries_sha256"])

    def test_complete_document_evidence_keeps_logical_path_when_inventory_exits(self):
        """完整内容仍通过时，inventory 退出单列记录且不误报逻辑丢失。"""
        before = {
            "dynamic_paths": ["a", "b"], "shared_paths": [],
            "path_types": {"a": ["String"], "b": ["String"]},
            "analysis_results_sha256": "analysis", "query_results_sha256": "truth",
            "native_documents": {"ordinary_content_ok": True, "actual_sha256": "before"},
        }
        after = {
            "dynamic_paths": ["a"], "shared_paths": [],
            "path_types": {"a": ["String"]},
            "analysis_results_sha256": "analysis", "query_results_sha256": "truth",
            "native_documents": {"ordinary_content_ok": True, "actual_sha256": "after"},
        }

        result = mechanisms.validate_path_transition(before, after, {"a", "b"})

        self.assertTrue(result["logical_paths_preserved"])
        self.assertFalse(result["inventory_paths_preserved"])
        self.assertEqual(result["after_native_documents_sha256"], "after")

        after["native_documents"]["ordinary_content_ok"] = False
        self.assertFalse(
            mechanisms.validate_path_transition(before, after, {"a", "b"})[
                "logical_paths_preserved"
            ]
        )

    def test_none_layout_records_full_digest_drift_but_preserves_analysis(self):
        """无 Sidecar 的文档差异必须显式记录，不能伪装成全量摘要稳定。"""
        before = {
            "dynamic_paths": ["a"], "shared_paths": [],
            "analysis_results_sha256": "analysis", "query_results_sha256": "before",
            "query_matches": {query_id: query_id not in {"S05", "S06"} for query_id in mechanisms.QUERY_IDS},
        }
        after = {
            **before, "analysis_results_sha256": "analysis", "query_results_sha256": "after",
        }

        result = mechanisms.validate_path_transition(before, after, {"a"})

        self.assertTrue(result["logical_paths_preserved"])
        self.assertTrue(result["analysis_results_preserved"])
        self.assertFalse(result["query_results_preserved"])
        self.assertEqual(result["before_query_results_sha256"], "before")
        self.assertEqual(result["after_query_results_sha256"], "after")

    def test_native_loss_diagnostic_rejects_ordinary_content_error(self):
        """已知缺失门禁只接受 null 和空容器，普通字段错误必须失败。"""
        expected = [["2030-01-01T00:00:00.000Z", "event-1", {
            "empty": {}, "null": None, "items": [None, [], {}], "kept": "correct",
        }]]
        allowed = [["2030-01-01T00:00:00.000Z", "event-1", {
            "items": [{}, {}, {}], "kept": "correct",
        }]]
        wrong = [["2030-01-01T00:00:00.000Z", "event-1", {
            "items": [{}, {}, {}], "kept": "wrong",
        }]]

        accepted = mechanisms.diagnose_native_query_difference("S05", expected, allowed)
        rejected = mechanisms.diagnose_native_query_difference("S05", expected, wrong)

        self.assertTrue(accepted["known_loss_only"])
        self.assertGreater(accepted["loss_count"], 0)
        self.assertFalse(rejected["known_loss_only"])
        self.assertIn("ordinary_value_mismatch", rejected["reasons"])

    def test_merge_pause_is_restored_when_insert_fails(self):
        """捕获 INSERT 异常后自有表 merge 仍保持暂停。"""
        adapter = mock.MagicMock()
        connection = mock.MagicMock()
        adapter.connect_worker.return_value = connection
        calls = []

        def request(_connection, statement, **_kwargs):
            calls.append(statement)
            if statement == "INSERT second":
                raise RuntimeError("insert failed")
            return ""

        adapter._request.side_effect = request
        with self.assertRaisesRegex(RuntimeError, "insert failed"):
            mechanisms.observe_merge_stages(
                adapter,
                "s2sup_mech.ch_native_auto32_sparse",
                {"s2sup_mech.ch_native_auto32_sparse"},
                ["INSERT first", "INSERT second"],
                lambda phase: {"phase": phase},
            )

        self.assertIn("SYSTEM STOP MERGES s2sup_mech.ch_native_auto32_sparse", calls)
        self.assertEqual(calls[-1], "SYSTEM START MERGES s2sup_mech.ch_native_auto32_sparse")

    def test_merge_observer_rejects_tables_outside_mechanism_layouts(self):
        """捕获 runner 暂停未取得所有权的表。"""
        adapter = mock.MagicMock()

        with self.assertRaisesRegex(ValueError, "owned mechanism table"):
            mechanisms.observe_merge_stages(
                adapter, "production.ch_native_auto32_none", set(),
                ["INSERT first"], lambda phase: phase
            )

        adapter.connect_worker.assert_not_called()

    def test_runner_cleans_owned_database_when_table_creation_fails(self):
        """部分建表失败时只删除本次已创建的 database。"""
        adapter = mock.MagicMock()
        calls = []

        def request(_adapter, statement, **_kwargs):
            calls.append(statement)
            if statement.startswith("CREATE TABLE") and "ch_native_auto32_sparse" in statement:
                raise RuntimeError("create failed")
            return ""

        contract = {
            "parameters": {query_id: {} for query_id in mechanisms.QUERY_IDS},
            "results": {query_id: {} for query_id in mechanisms.QUERY_IDS},
        }
        generator = mock.MagicMock()
        generator.query_results.return_value = {}
        with mock.patch.object(mechanisms, "ClickHouseFourLayoutAdapter", return_value=adapter), \
                mock.patch.object(mechanisms, "_request", side_effect=request), \
                mock.patch.object(mechanisms, "_json_rows", side_effect=[[{"count": 0}], [{"count": 0}]]), \
                mock.patch.object(mechanisms, "_truth_generator", return_value=generator):
            with self.assertRaisesRegex(RuntimeError, "create failed"):
                mechanisms._run_loaded(
                    [{"event_id": "e", "attributes_analysis": {}}], {"block_size": 1},
                    {"parameters": contract["parameters"]}, {"results": contract["results"]},
                    {}, "127.0.0.1", 18123, "container", "s2sup_owned_create",
                )

        self.assertIn("DROP DATABASE IF EXISTS s2sup_owned_create SYNC", calls)
        self.assertFalse(any(call.startswith("SYSTEM STOP MERGES") for call in calls))

    def test_runner_restores_merges_and_cleans_database_when_insert_fails(self):
        """完整建表后 INSERT 失败必须恢复目标表 merge 并清理 database。"""
        adapter = mock.MagicMock()
        adapter._analytics_row.return_value = {"event_id": "e", "fidelity_values": {}}
        calls = []

        def request(_adapter, statement, **_kwargs):
            calls.append(statement)
            if statement.startswith("INSERT INTO"):
                raise RuntimeError("insert failed")
            return ""

        contract = {
            "parameters": {query_id: {} for query_id in mechanisms.QUERY_IDS},
            "results": {query_id: {} for query_id in mechanisms.QUERY_IDS},
        }
        generator = mock.MagicMock()
        generator.query_results.return_value = {}
        stage = {
            "dynamic_paths": [], "shared_paths": [], "hinted_paths": [], "path_types": {},
            "analysis_results_sha256": "a", "query_results_sha256": "q",
        }
        with mock.patch.object(mechanisms, "ClickHouseFourLayoutAdapter", return_value=adapter), \
                mock.patch.object(mechanisms, "_request", side_effect=request), \
                mock.patch.object(mechanisms, "_json_rows", side_effect=[[{"count": 0}], [{"count": 0}]]), \
                mock.patch.object(mechanisms, "_truth_generator", return_value=generator), \
                mock.patch.object(mechanisms, "collect_native_stage", return_value=stage):
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                mechanisms._run_loaded(
                    [{"event_id": "e", "attributes_analysis": {}}], {"block_size": 1},
                    {"parameters": contract["parameters"]}, {"results": contract["results"]},
                    {}, "127.0.0.1", 18123, "container", "s2sup_owned_insert",
                )

        table = "s2sup_owned_insert.ch_native_auto32_none"
        self.assertIn(f"SYSTEM START MERGES {table}", calls)
        self.assertIn("DROP DATABASE IF EXISTS s2sup_owned_insert SYNC", calls)

    def test_sidecar_observation_records_none_sparse_and_full_recovery(self):
        """捕获 Sidecar 差异、空间或客户端恢复结果缺项。"""
        originals = [{"event_id": "event-1", "attributes": {"empty": {}, "plain": "x"}}]
        native = {"event_id": "event-1", "attributes": {"plain": "x"}}

        none = mechanisms.sidecar_observation(
            "ch_native_auto32_none", originals, [native]
        )
        sparse = mechanisms.sidecar_observation(
            "ch_native_auto32_sparse",
            originals,
            [{**native, "fidelity_values": {"/empty": "{}"}}],
        )
        full = mechanisms.sidecar_observation(
            "ch_native_auto32_full",
            originals,
            [{**native, "attributes_raw": '{"empty":{},"plain":"x"}'}],
        )

        self.assertEqual(none["mismatch_count"], 1)
        self.assertEqual(none["sidecar_entries"], 0)
        self.assertEqual(sparse["mismatch_count"], 0)
        self.assertEqual(sparse["sidecar_entries"], 1)
        self.assertEqual(sparse["sidecar_bytes"], len("/empty".encode()) + len("{}".encode()))
        self.assertEqual(full["mismatch_count"], 0)
        self.assertEqual(full["sidecar_bytes"], len('{"empty":{},"plain":"x"}'.encode()))
        self.assertGreaterEqual(sparse["recovery_ms"], 0.0)
        self.assertGreaterEqual(full["recovery_ms"], 0.0)
        with self.assertRaisesRegex(ValueError, "canonical"):
            mechanisms.sidecar_observation(
                "ch_native_auto32_full", originals,
                [{**native, "attributes_raw": '{"plain":"x", "empty":{}}'}],
            )
        for invalid in ({1: "{}"}, {"/empty": {}}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "string"):
                    mechanisms.sidecar_observation(
                        "ch_native_auto32_sparse", originals,
                        [{**native, "fidelity_values": invalid}],
                    )

    def test_sparse_hint_presence_markers_remove_declared_path_defaults(self):
        """auto/hinted sparse 共用缺失标记，恢复时移除声明路径注入的默认值。"""
        attributes = {"gen_ai": {"operation": {"name": "execute_tool"}}}
        markers = mechanisms.hint_presence_markers(attributes)
        stored = {
            "attributes": {
                "failure": {"mistake_mode": ""},
                "gen_ai": {"operation": {"name": "execute_tool"}},
            },
            "fidelity_values": markers,
        }

        recovered = mechanisms.restore_sparse_row(stored)

        self.assertNotIn("failure", recovered["attributes"])
        self.assertEqual(recovered["fidelity_values"], {})
        self.assertEqual(
            markers, {"@missing:failure.mistake_mode": "null"},
        )

    def test_two_missing_markers_are_counted_in_sparse_sidecar_cost(self):
        """两个缺失 marker 必须在恢复前计入 Sidecar 条目和 UTF-8 字节。"""
        originals = [{"event_id": "event-1", "attributes": {}}]
        markers = mechanisms.hint_presence_markers({})
        stored = [{
            "event_id": "event-1",
            "attributes": {
                "gen_ai": {"operation": {"name": ""}},
                "failure": {"mistake_mode": ""},
            },
            "fidelity_values": markers,
        }]

        observed = mechanisms.sidecar_observation(
            "ch_native_hinted32_sparse", originals, stored
        )

        self.assertEqual(observed["mismatch_count"], 0)
        self.assertEqual(observed["sidecar_entries"], 2)
        self.assertEqual(observed["sidecar_bytes"], 67)
        self.assertEqual(observed["marker_entries"], 2)
        self.assertEqual(observed["marker_bytes"], 67)

    def test_cleanup_rejects_drop_when_database_still_exists(self):
        """DROP 返回后 database 仍存在时必须标记 failed 并抛错。"""
        adapter = mock.MagicMock()
        result = {"status": "running", "cleanup_confirmed": False}
        with mock.patch.object(mechanisms, "_request") as request, \
                mock.patch.object(mechanisms, "_json_rows", return_value=[{"count": 1}]):
            with self.assertRaisesRegex(RuntimeError, "database cleanup not confirmed"):
                mechanisms.cleanup_owned_database(
                    adapter, "s2sup_cleanup", result
                )

        request.assert_called_once_with(
            adapter, "DROP DATABASE IF EXISTS s2sup_cleanup SYNC"
        )
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["cleanup_confirmed"])

    def test_final_probe_holds_two_parts_and_restores_merges(self):
        """捕获后台 merge 在普通读观察前合并版本，或异常路径未恢复。"""
        calls = []
        optimized = False

        def request(_adapter, statement, **_kwargs):
            nonlocal optimized
            calls.append(statement)
            if statement.startswith("OPTIMIZE TABLE"):
                optimized = True
            return ""

        def rows(_adapter, statement, **_kwargs):
            if "system.databases" in statement:
                return [{"count": 0}]
            if "system.parts" in statement:
                return [{"count": 1 if optimized else 2}]
            if "SELECT version" in statement:
                if " FINAL " in statement or optimized:
                    return [{"version": 2}]
                return [{"version": 1}, {"version": 2}]
            raise AssertionError(statement)

        with mock.patch.object(mechanisms, "ClickHouseFourLayoutAdapter", return_value=mock.MagicMock()), \
                mock.patch.object(mechanisms, "_request", side_effect=request), \
                mock.patch.object(mechanisms, "_json_rows", side_effect=rows):
            result = mechanisms.run_final_probe("127.0.0.1", 18123, "unused", "s2sup_final_test")

        stop = "SYSTEM STOP MERGES s2sup_final_test.final_probe"
        start = "SYSTEM START MERGES s2sup_final_test.final_probe"
        self.assertIn(stop, calls)
        self.assertIn(start, calls)
        self.assertLess(calls.index(stop), calls.index("INSERT INTO s2sup_final_test.final_probe VALUES ('event-1',1,'old')"))
        self.assertLess(calls.index("INSERT INTO s2sup_final_test.final_probe VALUES ('event-1',2,'new')"), calls.index(start))
        self.assertEqual(result["before_optimize"]["ordinary_versions"], [1, 2])

    def test_stage_collection_records_parts_paths_types_and_truth(self):
        """捕获阶段快照缺少 part、空间、路径类型或固定 truth。"""
        adapter = mock.MagicMock()

        def rows(_adapter, statement, **_kwargs):
            if "system.parts" in statement:
                return [{"part_count": 2, "rows": 3, "compressed_bytes": 40, "uncompressed_bytes": 80}]
            if "JSONDynamicPaths" in statement:
                return [{"dynamic_paths": ["a"], "shared_paths": ["b"]}]
            if "arrayZip(mapKeys(path_types),mapValues(path_types))" in statement:
                return [{"path": "a", "types": ["String"]}, {"path": "b", "types": ["Int64"]}]
            if statement == "SELECT truth FORMAT JSONEachRow":
                return [{"count": 3}]
            raise AssertionError(statement)

        expected = {query_id: {"query": query_id} for query_id in mechanisms.QUERY_IDS}
        catalog = {"parameters": {query_id: {} for query_id in mechanisms.QUERY_IDS}}
        truth_result = {
            "actual": expected, "analysis_truth_ok": True, "analysis_digest": "analysis-digest",
            "diagnostics": {
                query_id: {"match": True, "known_loss_only": True}
                for query_id in mechanisms.QUERY_IDS
            },
            "digest": "truth-digest", "truth_digest": "expected-digest",
            "matches": {query_id: True for query_id in mechanisms.QUERY_IDS}, "truth_ok": True,
        }
        with mock.patch.object(mechanisms, "_json_rows", side_effect=rows), \
                mock.patch.object(mechanisms, "_execute_truth_queries", return_value=truth_result):
            stage = mechanisms.collect_native_stage(
                adapter,
                "s2sup_mech.ch_native_auto32_none",
                {"s2sup_mech.ch_native_auto32_none"},
                catalog,
                expected,
            )

        self.assertEqual(stage["parts"]["part_count"], 2)
        self.assertEqual(stage["dynamic_paths"], ["a"])
        self.assertEqual(stage["shared_paths"], ["b"])
        self.assertEqual(stage["path_types"], {"a": ["String"], "b": ["Int64"]})
        self.assertEqual(stage["truth_results"], expected)
        self.assertEqual(stage["query_results_sha256"], "truth-digest")
        self.assertEqual(stage["analysis_results_sha256"], "analysis-digest")

    def test_merge_stage_waits_for_stability_before_snapshot(self):
        """捕获恢复 merge 后立即采集尚未稳定的阶段。"""
        adapter = mock.MagicMock()
        adapter.connect_worker.return_value = mock.MagicMock()
        phases = []
        with mock.patch.object(mechanisms, "_wait_for_merges", return_value={"stable": True}) as wait:
            stages = mechanisms.observe_merge_stages(
                adapter,
                "s2sup_mech.ch_native_auto32_none",
                {"s2sup_mech.ch_native_auto32_none"},
                ["INSERT first"],
                lambda phase: phases.append(phase) or {"phase": phase},
            )

        wait.assert_called_once_with(
            adapter, "s2sup_mech.ch_native_auto32_none",
            {"s2sup_mech.ch_native_auto32_none"},
        )
        self.assertEqual(phases, ["ddl", "first_insert", "all_inserts", "merges_stable", "optimize_final"])
        self.assertEqual(stages["merge_wait"], {"stable": True})

    def test_merge_wait_requires_idle_and_stable_active_part_count(self):
        """短暂无 merge 不能在后续 part 合并尚未调度时被误判稳定。"""
        adapter = mock.MagicMock()
        observations = [
            {"merge_count": 0, "part_count": 190},
            {"merge_count": 0, "part_count": 190},
            {"merge_count": 1, "part_count": 190},
            {"merge_count": 0, "part_count": 12},
        ] + [{"merge_count": 0, "part_count": 12}] * 9
        with mock.patch.object(mechanisms, "_json_rows", side_effect=lambda *_args, **_kwargs: [observations.pop(0)]), \
                mock.patch.object(mechanisms.time, "sleep"):
            result = mechanisms._wait_for_merges(
                adapter, "s2sup_mech.ch_native_auto32_none",
                {"s2sup_mech.ch_native_auto32_none"},
            )

        self.assertTrue(result["stable"])
        self.assertEqual(result["observations"][-1], {"merge_count": 0, "part_count": 12})
        self.assertEqual(len(result["observations"]), 13)

    def test_truth_contract_requires_exact_s01_to_s06_and_nonempty_digests(self):
        """捕获缺失、额外查询或空 truth 摘要进入阶段观察。"""
        catalog = {"parameters": {query_id: {} for query_id in mechanisms.QUERY_IDS}}
        truth = {"results": {query_id: {"query": query_id} for query_id in mechanisms.QUERY_IDS}}
        valid = mechanisms.validate_truth_contract(catalog, truth)
        self.assertEqual(set(valid["digests"]), set(mechanisms.QUERY_IDS))
        for mutate in (
            lambda value: value["parameters"].pop("S06"),
            lambda value: value["parameters"].update({"S07": {}}),
        ):
            broken = {"parameters": dict(catalog["parameters"])}
            mutate(broken)
            with self.assertRaisesRegex(ValueError, "S01-S06"):
                mechanisms.validate_truth_contract(broken, truth)
        with self.assertRaisesRegex(ValueError, "digest"):
            mechanisms.validate_truth_contract(catalog, truth, digests={query_id: "" for query_id in mechanisms.QUERY_IDS})

    def test_run_entry_loads_frozen_inputs_before_database_work(self):
        """捕获 ClickHouse 入口绕过 Task 4 冻结身份门禁。"""
        loaded = ([{"event_id": "event-a"}], {"block_size": 256}, {"parameters": {}}, {"results": {}}, {"dataset": {}})
        with mock.patch.object(mechanisms, "load_inputs", return_value=loaded) as load, \
                mock.patch.object(mechanisms, "_run_loaded", return_value={"status": "complete"}) as run:
            result = mechanisms.run_clickhouse_mechanisms(
                Path("input"), Path("truth"), "127.0.0.1", 18123, "container", "s2sup_mech"
            )
        load.assert_called_once_with(Path("input"), Path("truth"))
        run.assert_called_once()
        self.assertEqual(result["status"], "complete")

    def test_cli_parses_required_paths_namespace_and_fixed_defaults(self):
        """捕获机制程序缺少可复现入口或默认固定容器端点漂移。"""
        args = mechanisms.parse_args([
            "--input", "input", "--truth", "truth", "--output", "output",
            "--namespace", "s2sup_cli",
        ])

        self.assertEqual(args.input, Path("input"))
        self.assertEqual(args.truth, Path("truth"))
        self.assertEqual(args.output, Path("output"))
        self.assertEqual(args.namespace, "s2sup_cli")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 18123)
        self.assertEqual(args.container_name, "agent-trace-clickhouse-25-12")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            mechanisms.parse_args(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("--input", output.getvalue())
        self.assertIn("--container-name", output.getvalue())

    def test_cli_publishes_result_identity_and_forwards_arguments(self):
        """捕获 CLI 未透传参数、未记录 artifact 身份或覆盖输出目录其他文件。"""
        result = {
            "status": "complete", "cleanup_confirmed": True,
            "input": {"dataset": {"bytes": 3, "sha256": "input"}},
            "layouts": {
                layout: {"transitions": [], "stages": {"ddl": {"analysis_truth_ok": True}}}
                for layout in mechanisms.LAYOUTS
            },
            "sidecars": {
                layout: {"mismatch_count": 1 if layout.endswith("_none") else 0}
                for layout in mechanisms.LAYOUTS
            },
            "final_probe": {"cleanup_confirmed": True},
        }
        environment = {
            "endpoint": {"host": "db.example", "port": 18124},
            "container": {"name": "ch-container", "database_version": "25.12.11.4"},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            call_order = []
            (output / "keep.txt").write_text("keep", encoding="utf-8")
            args = mechanisms.parse_args([
                "--input", "input", "--truth", "truth", "--output", str(output),
                "--namespace", "s2sup_cli", "--host", "db.example", "--port", "18124",
                "--container-name", "ch-container",
            ])
            args.command = ["python", "run_clickhouse_mechanisms.py", "--namespace", "s2sup_cli"]
            with mock.patch.object(
                    mechanisms, "collect_environment_identity",
                    side_effect=lambda *unused: call_order.append("environment") or environment,
            ), mock.patch.object(
                    mechanisms, "run_clickhouse_mechanisms",
                    side_effect=lambda *unused: call_order.append("run") or result,
            ) as run:
                mechanisms.execute(args)

            manifest = json.loads((output / "run-manifest.json").read_bytes())
            artifact = output / "mechanism-result.json"
            expected_bytes = mechanisms.canonical_bytes(result) + b"\n"
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["engine"], "clickhouse")
            self.assertEqual(manifest["command"], args.command)
            self.assertEqual(manifest["input"], result["input"])
            self.assertEqual(manifest["environment"], environment)
            self.assertTrue(all(manifest["correctness"].values()))
            self.assertIn("s2sup_cli", manifest["run_id"])
            self.assertEqual(manifest["mechanism"]["query_ids"], list(mechanisms.QUERY_IDS))
            self.assertIn("runner/run_clickhouse_mechanisms.py", manifest["mechanism"]["files"])
            self.assertEqual(manifest["artifacts"]["mechanism-result.json"], {
                "bytes": len(expected_bytes), "sha256": hashlib.sha256(expected_bytes).hexdigest(),
            })
            self.assertEqual((output / "keep.txt").read_text(encoding="utf-8"), "keep")
            self.assertEqual(call_order, ["environment", "run"])
            run.assert_called_once_with(
                Path("input"), Path("truth"), "db.example", 18124, "ch-container", "s2sup_cli",
            )

    def test_cli_publishes_failed_manifest_without_running_when_environment_check_fails(self):
        """捕获环境身份失败后仍建库、写入或遗留旧 complete artifact。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "run-manifest.json").write_text('{"status":"complete"}', encoding="utf-8")
            (output / "mechanism-result.json").write_text('{"status":"complete"}', encoding="utf-8")
            args = mechanisms.parse_args([
                "--input", "input", "--truth", "truth", "--output", str(output),
                "--namespace", "s2sup_cli",
            ])
            with mock.patch.object(
                    mechanisms, "collect_environment_identity",
                    side_effect=RuntimeError("invalid environment"),
            ), mock.patch.object(
                    mechanisms,
                    "run_clickhouse_mechanisms",
                    return_value={
                        "status": "complete", "cleanup_confirmed": True, "input": {},
                        "layouts": {
                            layout: {"stages": {"ddl": {"analysis_truth_ok": True}}}
                            for layout in mechanisms.LAYOUTS
                        },
                        "sidecars": {
                            layout: {"mismatch_count": 1 if layout.endswith("_none") else 0}
                            for layout in mechanisms.LAYOUTS
                        },
                        "final_probe": {"cleanup_confirmed": True},
                    },
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "invalid environment"):
                    mechanisms.execute(args)

            manifest = json.loads((output / "run-manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse((output / "mechanism-result.json").exists())
            run.assert_not_called()

    def test_cli_replaces_stale_complete_with_failed_manifest_for_run_or_cleanup_failure(self):
        """捕获执行异常或未确认清理后遗留 complete manifest。"""
        environment = {
            "endpoint": {"host": "127.0.0.1", "port": 18123},
            "container": {"name": "agent-trace-clickhouse-25-12", "database_version": "25.12.11.4"},
        }
        failures = (
            RuntimeError("database unavailable"),
            {"status": "complete", "cleanup_confirmed": False, "input": {}, "layouts": {}, "sidecars": {}},
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                (output / "run-manifest.json").write_text('{"status":"complete"}', encoding="utf-8")
                args = mechanisms.parse_args([
                    "--input", "input", "--truth", "truth", "--output", str(output),
                    "--namespace", "s2sup_cli",
                ])
                patch_value = ({"side_effect": failure} if isinstance(failure, Exception)
                               else {"return_value": failure})
                with mock.patch.object(mechanisms, "collect_environment_identity", return_value=environment), \
                        mock.patch.object(mechanisms, "run_clickhouse_mechanisms", **patch_value):
                    with self.assertRaises(RuntimeError):
                        mechanisms.execute(args)
                manifest = json.loads((output / "run-manifest.json").read_bytes())
                self.assertEqual(manifest["status"], "failed")
                if isinstance(failure, Exception):
                    self.assertFalse((output / "mechanism-result.json").exists())
                else:
                    self.assertIn("mechanism-result.json", manifest["artifacts"])

    def test_environment_identity_rejects_wrong_server_version(self):
        """捕获容器身份存在但 ClickHouse 服务端版本偏离固定版本。"""
        identity = {"name": "ch", "ports": {"8123/tcp": [{"HostPort": "18123"}]}}
        adapter = mock.MagicMock()
        adapter.database_version.return_value = "25.13.1.1"
        task4 = mock.MagicMock()
        task4._container_identity.return_value = identity
        task4._require_host_port.side_effect = lambda *args: None
        with mock.patch.object(mechanisms, "_task4_runner", return_value=task4), \
                mock.patch.object(mechanisms, "ClickHouseFourLayoutAdapter", return_value=adapter):
            with self.assertRaisesRegex(RuntimeError, "database version"):
                mechanisms.collect_environment_identity(
                    "127.0.0.1", 18123, "ch", "s2sup_cli"
                )


@unittest.skipUnless(os.environ.get("RUN_CLICKHOUSE_INTEGRATION") == "1", "set RUN_CLICKHOUSE_INTEGRATION=1")
class ClickHouseMechanismIntegrationTest(unittest.TestCase):
    """以 Task 1 冻结输入/truth 验证四结构机制链和 FINAL。"""

    DATA_ROOT = Path(os.environ.get("MECHANISM_DATA_ROOT", STAGE_DIR.parents[1]))
    INPUT_DIR = DATA_ROOT / "docs/temp/json-storage-stage2/cross-engine-input-20260907"
    TRUTH_DIR = DATA_ROOT / "docs/temp/json-storage-stage2-sup/input-20260908"

    def test_final_probe_observes_physical_and_logical_versions(self):
        database = f"s2sup_final_{uuid.uuid4().hex[:10]}"
        result = mechanisms.run_final_probe(
            "127.0.0.1", 18123, "agent-trace-clickhouse-25-12", database,
        )

        self.assertEqual(result["before_optimize"]["ordinary_versions"], [1, 2])
        self.assertEqual(result["before_optimize"]["final_versions"], [2])
        self.assertEqual(result["before_optimize"]["active_parts"], 2)
        self.assertEqual(result["after_optimize"]["ordinary_versions"], [2])
        self.assertEqual(result["after_optimize"]["active_parts"], 1)
        self.assertTrue(result["cleanup_confirmed"])

    def test_frozen_input_runs_four_layouts_five_stages_sidecars_and_cleanup(self):
        database = f"s2sup_mech_{uuid.uuid4().hex[:10]}"
        result = mechanisms.run_clickhouse_mechanisms(
            self.INPUT_DIR, self.TRUTH_DIR, "127.0.0.1", 18123,
            "agent-trace-clickhouse-25-12", database,
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(set(result["layouts"]), set(mechanisms.LAYOUTS))
        for layout in mechanisms.LAYOUTS:
            observed = result["layouts"][layout]
            self.assertEqual(
                set(observed["stages"]),
                {"ddl", "first_insert", "all_inserts", "merge_wait", "merges_stable", "optimize_final"},
            )
            for name, stage in observed["stages"].items():
                if name == "merge_wait":
                    continue
                self.assertEqual(set(stage["query_matches"]), set(mechanisms.QUERY_IDS))
                self.assertTrue(stage["analysis_truth_ok"])
                self.assertTrue(stage["known_native_loss_only"])
                for query_id in mechanisms.QUERY_IDS:
                    self.assertTrue(stage["query_diagnostics"][query_id]["actual_sha256"])
                    self.assertTrue(stage["query_diagnostics"][query_id]["truth_sha256"])
                if layout != "ch_native_auto32_none":
                    self.assertTrue(stage["truth_ok"])
            self.assertTrue(all(
                item["logical_paths_preserved"] and item["analysis_results_preserved"]
                for item in observed["transitions"]
            ))
            if layout != "ch_native_auto32_none":
                self.assertTrue(all(item["query_results_preserved"] for item in observed["transitions"]))
                self.assertFalse(observed["full_result_changed_across_transition"])
        self.assertGreater(result["sidecars"]["ch_native_auto32_none"]["mismatch_count"], 0)
        self.assertEqual(result["sidecars"]["ch_native_auto32_sparse"]["mismatch_count"], 0)
        self.assertEqual(result["sidecars"]["ch_native_auto32_full"]["mismatch_count"], 0)
        auto_markers = result["sidecars"]["ch_native_auto32_sparse"]
        hinted_markers = result["sidecars"]["ch_native_hinted32_sparse"]
        self.assertEqual(auto_markers["marker_rule"], hinted_markers["marker_rule"])
        self.assertEqual(auto_markers["marker_entries"], hinted_markers["marker_entries"])
        self.assertEqual(auto_markers["marker_bytes"], hinted_markers["marker_bytes"])
        self.assertGreater(auto_markers["marker_entries"], 0)
        self.assertGreater(auto_markers["marker_bytes"], 0)
        self.assertTrue(result["final_probe"]["cleanup_confirmed"])
        self.assertTrue(result["cleanup_confirmed"])


if __name__ == "__main__":
    unittest.main()
