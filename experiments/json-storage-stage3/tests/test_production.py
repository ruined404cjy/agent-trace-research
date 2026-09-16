import hashlib
import json
import operator
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import BlockResult, MaintenanceResult, PayloadRecord, QuerySpec, TruthCatalog
from run_layout_matrix import (
    FORMAL_GENERATION_FORMAT,
    FORMAL_QUERY_WINDOW,
    FORMAL_SOURCE_ARTIFACTS,
    FORMAL_SOURCE_MANIFEST,
)
import production
from run_interference import DeadlineTarget, FIXED_PHASES, fixed_phase_schedules


LAYOUT_WATERMARK_KEYS = {
    "same_table": ("events",),
    "separate": ("events_analytics", "event_payloads"),
    "full_core": ("events_full", "events_core"),
    "asset_ref": ("assets", "events_analytics"),
}


def formal_generation(**changes):
    """返回满足既有正式契约验证器的 generation manifest。"""
    value = {
        "format": FORMAL_GENERATION_FORMAT,
        "format_version": 1,
        "status": "complete",
    }
    value.update(changes)
    return value


def formal_fixture(root):
    """构造读取边界替身所需的完整正式 identity、truth 与事件序列。"""
    root.mkdir(parents=True, exist_ok=True)
    payload_root = root / "payloads"
    payload_root.mkdir()
    payloads = []
    events = []
    profiles = ("text_64k", "text_512k", "text_2m", "entropy_512k")
    payload_lengths = {
        "text_64k": 65_536, "text_512k": 524_288,
        "text_2m": 2_097_152, "entropy_512k": 524_288,
    }
    payload_profiles = {
        **{index: profiles[index // 40] for index in range(115)},
        **{
            block_index * 256: profiles[2] if block_index < 113 else profiles[3]
            for block_index in range(108, 153)
        },
    }
    for index in range(48_534):
        profile = payload_profiles.get(index)
        cohort = "main" if profile is not None else None
        payload = None
        if profile is not None:
            payload = (f'{{"event":{index}}}').encode()
            digest = hashlib.sha256(payload).hexdigest()
            relative = f"payloads/{digest}.json"
            (root / relative).write_bytes(payload)
            record = PayloadRecord(
                event_id=f"event-{index:05d}", trace_id="trace-p50" if index < 3 else f"trace-{index}",
                project_id="Leoxx/whowhen_pro",
                start_time=f"2030-01-01T00:{index // 1000:02d}:{index % 60:02d}.000Z",
                cohort="main", profile=profile, content_type="application/json",
                encoding="utf-8", content_length=payload_lengths[profile], preview=payload.decode(),
                sha256=digest, payload_path=relative,
            )
            payloads.append(record)
        events.append({
            "ingest_seq": index, "event_id": f"event-{index:05d}",
            "trace_id": "trace-p50" if index < 3 else f"trace-{index}",
            "span_id": f"span-{index}", "parent_span_id": None,
            "project_id": "Leoxx/whowhen_pro" if index < 27_561 else "other-project",
            "start_time": f"2030-01-01T00:{index // 1000:02d}:{index % 60:02d}.000Z",
            "end_time": f"2030-01-01T00:{index // 1000:02d}:{index % 60:02d}.500Z",
            "duration_ms": 500, "span_type": "llm", "framework": "fixture",
            "level": "INFO", "cohort": cohort, "profile": profile,
            "content_type": "application/json" if payload else None,
            "encoding": "utf-8" if payload else None,
            "content_length": payload_lengths[profile] if payload else None,
            "preview": payload.decode() if payload else None,
            "sha256": hashlib.sha256(payload).hexdigest() if payload else None,
            "payload_path": f"payloads/{hashlib.sha256(payload).hexdigest()}.json" if payload else None,
        })
    truth = TruthCatalog(
        seed=20260907, source={"manifest": FORMAL_SOURCE_MANIFEST, "artifacts": FORMAL_SOURCE_ARTIFACTS},
        record_count=48_534, block_size=256, block_count=190,
        watermarks=tuple(list(range(256, 48_534, 256)) + [48_534]),
        identity_sha256="0" * 64, query_window=FORMAL_QUERY_WINDOW,
        payloads=tuple(payloads), cohorts={},
        representative_traces={
            "p25": {"trace_id": "trace-p50", "span_count": 3},
            "p50": {"trace_id": "trace-p50", "span_count": 3},
            "p95": {"trace_id": "trace-p50", "span_count": 3},
        },
        detail_samples=tuple(payloads[index] for index in (0, 40, 80, 120)),
    )
    return truth, tuple(events), {
        "kind": "formal", "identity_sha256": "0" * 64,
        "provenance": {"source": "fixture"},
    }


def load_fixture_formal(root):
    """经正式 loader 返回测试所需的已验证输入快照。"""
    truth, events, identity = formal_fixture(root)
    (root / "generation-manifest.json").write_text(json.dumps(formal_generation()))
    with patch.object(production, "load_run_input", return_value=(truth, events, identity)):
        return production.load_formal_input(root)


def fixture_canonical_digest(value):
    """以测试侧独立 canonical JSON 规则计算 SHA-256。"""
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InterferenceAdapter:
    """记录生产 targets 的可观察输入，并返回可配置的真实结果对象。"""

    def __init__(self, layout, failure=None, mutate_blocks=False):
        self.layout = layout
        self.failure = failure
        self.mutate_blocks = mutate_blocks
        self.submitted = []
        self.received_first_event_ids = []
        self.returned_results = []
        self.waited = []
        self.ready_timeouts = []
        self.continuous_result = None

    def ingest_block(self, block):
        index = len(self.submitted)
        self.received_first_event_ids.append(block[0]["event_id"])
        watermark = int(block[-1]["ingest_seq"]) + 1
        result = BlockResult(
            len(block), watermark,
            {key: watermark for key in LAYOUT_WATERMARK_KEYS[self.layout]}, 1.0,
        )
        if index == 0 and self.failure == "result_type":
            result = object()
        elif index == 0 and self.failure == "rows":
            result = replace(result, rows=len(block) - 1)
        elif index == 0 and self.failure == "watermark":
            result = replace(result, watermark=watermark - 1)
        elif index == 0 and self.failure == "keys":
            result = replace(result, watermarks={"wrong": watermark})
        elif index == 0 and self.failure == "key_value":
            result = replace(result, watermarks={
                key: watermark + 1 for key in LAYOUT_WATERMARK_KEYS[self.layout]
            })
        elif self.continuous_result is not None:
            result = self.continuous_result
            self.continuous_result = None
        self.submitted.append(block)
        if self.mutate_blocks:
            block[0]["event_id"] = "changed-by-adapter"
        self.returned_results.append(result)
        return result

    def wait_write_complete(self, watermark):
        self.waited.append(watermark)
        visible_watermark = watermark + 1 if self.failure == "write_watermark" else watermark
        return MaintenanceResult(
            self.failure != "write_incomplete",
            {key: visible_watermark for key in LAYOUT_WATERMARK_KEYS[self.layout]},
        )

    def wait_query_ready(self, timeout_seconds):
        self.ready_timeouts.append(timeout_seconds)
        ready_watermark = 48_535 if self.failure == "ready_watermark" else 48_534
        return MaintenanceResult(
            self.failure != "ready_incomplete",
            {key: ready_watermark for key in LAYOUT_WATERMARK_KEYS[self.layout]},
        )


class ProductionFactoryTest(unittest.TestCase):
    """验证正式输入与数据库 adapter 的受限构造边界。"""

    def test_loader_builds_main_blocks_queries_and_contract_from_formal_input(self):
        """捕获跳过正式门禁、重排事件或按固定范围切块。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "formal"
            truth, events, identity = formal_fixture(root)
            (root / "generation-manifest.json").write_text(json.dumps(formal_generation()))
            with patch.object(production, "load_run_input", return_value=(truth, events, identity)):
                loaded = production.load_formal_input(root)

        self.assertEqual(loaded.root, root.resolve())
        self.assertEqual(len(loaded.events), 48_534)
        self.assertEqual(loaded.events[0]["event_id"], "event-00000")
        self.assertEqual(tuple(map(len, loaded.main_blocks))[-2:], (256, 150))
        self.assertEqual(len(loaded.main_blocks), 190)
        self.assertEqual(loaded.main_blocks[-1][-1]["ingest_seq"], 48_533)
        self.assertEqual(sum(row["payload_path"] is not None for row in loaded.main_events), 160)
        self.assertEqual(
            [index for index, block in enumerate(loaded.main_blocks)
             if any(row["payload_path"] is not None for row in block)][-45:],
            list(range(108, 153)),
        )
        scenarios = {truth.scenario for _, truth in loaded.main_queries}
        self.assertTrue({"list:first", "preview:first", "detail:text_64k", "detail:text_512k",
                         "detail:text_2m", "detail:entropy_512k", "trace:p25", "trace:p50",
                         "trace:p95", "batch:main"}.issubset(scenarios))
        self.assertEqual(loaded.main_contract, {"payload_count": 160, "raw_payload_bytes": 128_450_560})

    def test_loader_rejects_nonformal_or_invalid_generation_before_adapter_creation(self):
        """捕获 smoke、自洽替代和不完整 generation 被正式入口放行。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            truth, events, identity = formal_fixture(root)
            with patch.object(production, "load_run_input", return_value=(truth, events, identity)):
                with self.assertRaisesRegex(ValueError, "generation manifest"):
                    production.load_formal_input(root)
            cases = (
                (formal_generation(status="running"), identity, "incomplete"),
                (formal_generation(format="smoke"), identity, "format"),
                (formal_generation(format_version=2), identity, "format"),
                (formal_generation(), {**identity, "kind": "smoke"}, "formal"),
            )
            for generation, supplied_identity, message in cases:
                with self.subTest(generation=generation, identity=supplied_identity):
                    (root / "generation-manifest.json").write_text(json.dumps(generation))
                    with patch.object(
                        production, "load_run_input", return_value=(truth, events, supplied_identity),
                    ):
                        with self.assertRaisesRegex(ValueError, message):
                            production.load_formal_input(root)

    def test_loader_keeps_existing_formal_contract_validator_as_the_gate(self):
        """捕获冻结规模、来源、窗口或 30/5 契约被平行实现替代。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            truth, events, identity = formal_fixture(root)
            (root / "generation-manifest.json").write_text(json.dumps(formal_generation()))
            original_validator = production.validate_formal_contract
            received = []

            def validate_with_record(generation, received_truth, measurements, batch_measurements):
                received.append((generation, received_truth, measurements, batch_measurements))
                return original_validator(generation, received_truth, measurements, batch_measurements)

            with patch.object(production, "validate_formal_contract", side_effect=validate_with_record), patch.object(
                production, "load_run_input", return_value=(truth, events, identity),
            ):
                production.load_formal_input(root)
            self.assertEqual(received, [(formal_generation(), truth, 30, 5)])
            invalid_truths = (
                replace(truth, seed=1),
                replace(truth, record_count=8),
                replace(truth, block_size=8),
                replace(truth, block_count=4),
                replace(truth, watermarks=(48_534,)),
                replace(truth, source={"manifest": {}, "artifacts": {}}),
                replace(truth, query_window={}),
            )
            for invalid_truth in invalid_truths:
                with self.subTest(invalid_truth=invalid_truth):
                    with patch.object(
                        production, "load_run_input", return_value=(invalid_truth, events, identity),
                    ):
                        with self.assertRaisesRegex(ValueError, "formal frozen source contract"):
                            production.load_formal_input(root)

    def test_adapter_factory_uses_real_constructors_and_isolated_asset_store(self):
        """捕获 adapter 构造连接数据库、误用 Asset 目录或接受非法选择。"""
        endpoints = production.EngineEndpoints()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            for engine in ("opengauss", "clickhouse"):
                for layout in ("same_table", "separate", "full_core", "asset_ref"):
                    asset_root = root / f"{engine}-{layout}"
                    adapter = production.create_adapter(
                        engine, layout, "jsons3factory", formal,
                        asset_root if layout == "asset_ref" else root / "ignored", endpoints,
                    )
                    self.assertEqual(adapter.layout, layout)
                    self.assertEqual(adapter.input_root, formal.root)
                    self.assertIsNone(adapter.asset_store) if layout != "asset_ref" else self.assertEqual(
                        adapter.asset_store.root, asset_root.resolve(),
                    )
            occupied = root / "occupied"
            occupied.mkdir()
            (occupied / "prior-object").write_text("x")
            with self.assertRaisesRegex(ValueError, "empty"):
                production.create_adapter("clickhouse", "asset_ref", "jsons3factory", formal, occupied, endpoints)
        for engine, layout, namespace in (("other", "same_table", "ok"), ("clickhouse", "other", "ok"),
                                          ("clickhouse", "same_table", "bad namespace")):
            with self.subTest(engine=engine, layout=layout, namespace=namespace):
                with self.assertRaises(ValueError):
                    production.create_adapter(engine, layout, namespace, formal, None, endpoints)

    def test_candidate_and_part_state_inputs_are_fixed_main_contract(self):
        """捕获候选配置暴露缩短测量或替换 main workload 的入口。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            config = production.candidate_config(
                formal, root / "out", root / "assets", ("runner",),
            )

        self.assertEqual((config.engine, config.layout, config.workload), ("clickhouse", "asset_ref", "main"))
        self.assertEqual((config.round_index, config.round_order), (0, ("same_table", "separate", "full_core", "asset_ref")))
        self.assertEqual((config.measurements, config.batch_measurements), (30, 5))
        self.assertEqual(config.verified_events, formal.main_events)
        self.assertEqual(production.part_state_inputs(formal), (formal.main_blocks, formal.main_queries))

    def test_factories_reject_handmade_formal_input_without_loader_validation(self):
        """捕获调用方以同类型对象绕过正式 input loader。"""
        forged = production.FormalInput(Path("/tmp/formal"), None, (), {}, {}, (), (), (), {})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "validated"):
                production.create_adapter(
                    "clickhouse", "same_table", "jsons3factory", forged, None,
                    production.EngineEndpoints(),
                )
            with self.assertRaisesRegex(ValueError, "validated"):
                production.candidate_config(forged, root / "out", root / "assets", ("runner",))
            with self.assertRaisesRegex(ValueError, "validated"):
                production.part_state_inputs(forged)

    def test_interference_factory_rejects_static_identity_errors_before_adapter_creation(self):
        """捕获错误输入身份、布局、端点或 query catalog 留下数据库与 Asset 产物。"""
        forged = production.FormalInput(Path("/tmp/formal"), None, (), {}, {}, (), (), (), {})
        endpoints = production.EngineEndpoints()
        with patch.object(production, "create_adapter") as create:
            with self.assertRaisesRegex(ValueError, "validated"):
                production.interference_factories(forged, "same_table", None, endpoints)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                invalid = (
                    ("other", None, endpoints, "layout"),
                    ("same_table", None, object(), "endpoints"),
                    ("asset_ref", None, endpoints, "asset_root"),
                )
                for layout, asset_root, supplied_endpoints, message in invalid:
                    formal = load_fixture_formal(root / f"formal-{message}")
                    with self.subTest(layout=layout, message=message):
                        with self.assertRaisesRegex(ValueError, message):
                            production.interference_factories(
                                formal, layout, asset_root, supplied_endpoints,
                            )

                base = load_fixture_formal(root / "formal-queries")
                list_case = next(
                    case for case in base.main_queries if case[1].scenario == "list:first"
                )
                query_cases = {
                    "missing": tuple(
                        case for case in base.main_queries if case[1].scenario != "list:first"
                    ),
                    "duplicate": base.main_queries + (list_case,),
                    "kind": tuple(
                        (QuerySpec("trace", dict(query.parameters)), truth)
                        if truth.scenario == "list:first" else (query, truth)
                        for query, truth in base.main_queries
                    ),
                }
                for name, cases in query_cases.items():
                    formal = load_fixture_formal(root / f"formal-query-{name}")
                    object.__setattr__(formal, "main_queries", cases)
                    with self.subTest(query_catalog=name):
                        with self.assertRaisesRegex(ValueError, "query"):
                            production.interference_factories(
                                formal, "same_table", None, endpoints,
                            )
            create.assert_not_called()

    def test_interference_metadata_selects_exact_blocks_and_is_json_isolated(self):
        """捕获持续写选择放宽、只哈希 index、query digest 漂移或 metadata 反向改写工厂。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            _, targets_factory, metadata = production.interference_factories(
                formal, "same_table", None, production.EngineEndpoints(),
            )

            expected_indices = list(range(108, 153))
            expected_digests = [
                fixture_canonical_digest([dict(row) for row in formal.main_blocks[index]])
                for index in expected_indices
            ]
            expected_query_digest = fixture_canonical_digest([
                {
                    "scenario": truth.scenario, "kind": query.kind,
                    "parameters": dict(query.parameters),
                }
                for query, truth in formal.main_queries
            ])
            self.assertEqual(metadata["eligible_block_count"], 45)
            self.assertEqual(metadata["eligible_block_indices"], expected_indices)
            self.assertEqual(metadata["eligible_block_sha256"], expected_digests)
            self.assertEqual(metadata["main_query_catalog_sha256"], expected_query_digest)
            self.assertEqual(
                (metadata["preload_block_count"], metadata["block_size"],
                 metadata["final_watermark"], metadata["seed"]),
                (190, 256, 48_534, 20260907),
            )
            self.assertIs(metadata["cyclic_replay"], True)
            window = formal.truth.query_window
            for index in expected_indices:
                block = formal.main_blocks[index]
                self.assertEqual(len(block), 256)
                self.assertTrue(all(not (
                    row["project_id"] == window["project_id"]
                    and window["start_time"] <= row["start_time"] < window["end_time"]
                ) for row in block))
                self.assertTrue(any(row["payload_path"] is not None for row in block))
            json.dumps(metadata, allow_nan=False)
            metadata["eligible_block_indices"][0] = 999
            metadata["selection_rules"]["full_block_rows"] = 1

            adapter = InterferenceAdapter("same_table")
            with patch.object(
                production, "build_query_target",
                side_effect=lambda adapter, query, truth: DeadlineTarget(
                    lambda deadline, cancellation: truth.scenario
                ),
            ):
                targets = targets_factory(adapter, FIXED_PHASES[-1], 20260907)
            adapter.submitted.clear()
            targets["continuous_ingest"](float("inf"), threading.Event())
            self.assertEqual(adapter.submitted[0][0]["ingest_seq"], 108 * 256)
            self.assertEqual(formal.main_blocks[108][0]["ingest_seq"], 108 * 256)

    def test_interference_preloads_each_layout_and_builds_exact_phase_targets(self):
        """捕获跳块预载、遗漏联合水位等待、重复预载或 phase 查询映射错误。"""
        cases = (
            ("same_table", FIXED_PHASES[0], {"list": "list:first", "preview": "preview:first"}),
            ("separate", FIXED_PHASES[1], {
                "list": "list:first", "preview": "preview:first", "detail_2m": "detail:text_2m",
            }),
            ("full_core", FIXED_PHASES[2], {
                "list": "list:first", "preview": "preview:first", "trace_long": "trace:p95",
            }),
            ("asset_ref", FIXED_PHASES[3], {
                "list": "list:first", "preview": "preview:first", "batch_loop": "batch:main",
            }),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            for layout, phase, expected in cases:
                with self.subTest(layout=layout, phase=phase.name):
                    _, targets_factory, _ = production.interference_factories(
                        formal, layout, root / "assets", production.EngineEndpoints(),
                    )
                    adapter = InterferenceAdapter(layout)
                    with patch.object(
                        production, "build_query_target",
                        side_effect=lambda adapter, query, truth: DeadlineTarget(
                            lambda deadline, cancellation, scenario=truth.scenario: scenario
                        ),
                    ):
                        targets = targets_factory(adapter, phase, 20260907)

                    self.assertEqual(set(targets), set(fixed_phase_schedules(phase, measurement=True)))
                    self.assertEqual(
                        {name: target(float("inf"), threading.Event())
                         for name, target in targets.items()},
                        expected,
                    )
                    self.assertEqual(len(adapter.submitted), 190)
                    self.assertTrue(all(
                        isinstance(block, list) and block
                        and all(type(row) is dict for row in block)
                        for block in adapter.submitted
                    ))
                    self.assertEqual(adapter.waited, list(formal.truth.watermarks))
                    self.assertEqual(adapter.ready_timeouts, [60])
                    self.assertEqual(adapter.submitted[0][0]["ingest_seq"], 0)
                    self.assertEqual(adapter.submitted[-1][-1]["ingest_seq"], 48_533)

    def test_interference_preload_fails_closed_on_invalid_adapter_evidence(self):
        """捕获错误 block/维护证据仍启动 phase 请求流。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            _, targets_factory, _ = production.interference_factories(
                formal, "same_table", None, production.EngineEndpoints(),
            )
            for failure in (
                "result_type", "rows", "watermark", "keys", "key_value",
                "write_incomplete", "write_watermark", "ready_incomplete", "ready_watermark",
            ):
                adapter = InterferenceAdapter("same_table", failure=failure)
                with self.subTest(failure=failure):
                    with self.assertRaises(RuntimeError):
                        targets_factory(adapter, FIXED_PHASES[0], 20260907)
                    self.assertEqual(
                        len(adapter.submitted), 190
                        if failure in {"ready_incomplete", "ready_watermark"} else 1,
                    )

    def test_interference_adapter_factory_enforces_phase_seed_and_namespace_assets(self):
        """捕获错误 phase/seed 创建 adapter，或 Asset 目录跨 namespace 复用。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            endpoints = production.EngineEndpoints()
            adapter_factory, _, _ = production.interference_factories(
                formal, "same_table", root / "ignored", endpoints,
            )
            with patch.object(production, "create_adapter", return_value=object()) as create:
                for phase, seed in (("quiet", 20260907), (FIXED_PHASES[0], 1)):
                    with self.subTest(phase=phase, seed=seed):
                        with self.assertRaises(ValueError):
                            adapter_factory("phase_ns", phase, seed)
                adapter_factory("phase_ns", FIXED_PHASES[0], 20260907)
                self.assertEqual(create.call_args.args, (
                    "clickhouse", "same_table", "phase_ns", formal, None, endpoints,
                ))

            for index, layout in enumerate(LAYOUT_WATERMARK_KEYS):
                namespace = f"layout_{index}"
                asset_base = root / f"assets-{layout}"
                factory, _, _ = production.interference_factories(
                    formal, layout, asset_base, endpoints,
                )
                adapter = factory(namespace, FIXED_PHASES[0], 20260907)
                if layout == "asset_ref":
                    self.assertEqual(adapter.asset_store.root, (asset_base / namespace).resolve())
                    with self.assertRaisesRegex(ValueError, "namespace"):
                        factory(namespace, FIXED_PHASES[0], 20260907)
                else:
                    self.assertIsNone(adapter.asset_store)
                    self.assertFalse(asset_base.exists())

    def test_continuous_target_cycles_isolated_blocks_and_returns_adapter_result(self):
        """捕获多 block 单次提交、循环乱序、复制泄漏或替换真实 BlockResult。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            _, targets_factory, _ = production.interference_factories(
                formal, "same_table", None, production.EngineEndpoints(),
            )
            adapter = InterferenceAdapter("same_table", mutate_blocks=True)
            with patch.object(
                production, "build_query_target",
                side_effect=lambda adapter, query, truth: DeadlineTarget(
                    lambda deadline, cancellation: truth.scenario
                ),
            ):
                target = targets_factory(adapter, FIXED_PHASES[-1], 20260907)["continuous_ingest"]
            adapter.submitted.clear()
            adapter.received_first_event_ids.clear()
            adapter.returned_results.clear()

            results = [target(float("inf"), threading.Event()) for _ in range(46)]
            self.assertEqual(
                [block[0]["ingest_seq"] // 256 for block in adapter.submitted],
                list(range(108, 153)) + [108],
            )
            self.assertTrue(all(len(block) == 256 for block in adapter.submitted))
            self.assertTrue(all(isinstance(result, BlockResult) for result in results))
            self.assertTrue(all(
                result is returned for result, returned in zip(results, adapter.returned_results)
            ))
            self.assertEqual(adapter.received_first_event_ids[-1], "event-27648")
            self.assertEqual(formal.main_blocks[108][0]["event_id"], "event-27648")

            sentinel = BlockResult(256, 28_160, {"events": 28_160}, 7.0)
            adapter.continuous_result = sentinel
            self.assertIs(target(float("inf"), threading.Event()), sentinel)

    def test_continuous_target_timeout_and_invalid_result_do_not_advance(self):
        """捕获取消、到期或错误写证据仍执行写入并推进循环位置。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            formal = load_fixture_formal(root / "formal")
            _, targets_factory, _ = production.interference_factories(
                formal, "same_table", None, production.EngineEndpoints(),
            )
            adapter = InterferenceAdapter("same_table")
            with patch.object(
                production, "build_query_target",
                side_effect=lambda adapter, query, truth: DeadlineTarget(
                    lambda deadline, cancellation: truth.scenario
                ),
            ):
                target = targets_factory(adapter, FIXED_PHASES[-1], 20260907)["continuous_ingest"]
            adapter.submitted.clear()

            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(TimeoutError):
                target(float("inf"), cancelled)
            with patch.object(production.time, "monotonic", return_value=10.0):
                with self.assertRaises(TimeoutError):
                    target(10.0, threading.Event())
                first = target(11.0, threading.Event())
            self.assertEqual(len(adapter.submitted), 1)
            self.assertEqual(adapter.submitted[0][0]["ingest_seq"] // 256, 108)
            self.assertIsInstance(first, BlockResult)

            adapter.continuous_result = replace(first, rows=255)
            with self.assertRaises(RuntimeError):
                target(float("inf"), threading.Event())
            target(float("inf"), threading.Event())
            self.assertEqual(
                [block[0]["ingest_seq"] // 256 for block in adapter.submitted[-2:]],
                [109, 109],
            )

    def test_loader_returns_deep_readonly_snapshot_for_factory_consumers(self):
        """捕获正式门禁后仍可篡改 identity、事件或 query truth。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "formal"
            truth, events, identity = formal_fixture(root)
            (root / "generation-manifest.json").write_text(json.dumps(formal_generation()))
            with patch.object(production, "load_run_input", return_value=(truth, events, identity)):
                formal = production.load_formal_input(root)
            config = production.candidate_config(formal, root / "out", root / "assets", ("runner",))
            blocks, queries = production.part_state_inputs(formal)

        self.assertEqual(formal.events[0]["event_id"], "event-00000")
        self.assertEqual(formal.identity["provenance"]["source"], "fixture")
        self.assertEqual(formal.truth.query_window["page_size"], 256)
        source_event = events[0]
        source_event["event_id"] = "changed-outside-loader"
        identity["provenance"]["source"] = "changed-outside-loader"
        truth.query_window["page_size"] = 1
        self.assertEqual(formal.events[0]["event_id"], "event-00000")
        self.assertEqual(formal.identity["provenance"]["source"], "fixture")
        self.assertEqual(formal.truth.query_window["page_size"], 256)
        query, query_truth = queries[0]
        mutation_attempts = (
            lambda: operator.setitem(formal.identity, "kind", "smoke"),
            lambda: operator.setitem(formal.identity["provenance"], "source", "changed"),
            lambda: operator.setitem(formal.generation, "status", "failed"),
            lambda: operator.setitem(formal.events[0], "event_id", "changed"),
            lambda: operator.setitem(blocks[0][0], "event_id", "changed"),
            lambda: operator.setitem(formal.main_contract, "payload_count", 0),
            lambda: operator.setitem(query.parameters, "page_size", 1),
            lambda: operator.setitem(query_truth.rows[0], "event_id", "changed"),
            lambda: operator.setitem(formal.truth.query_window, "page_size", 1),
            lambda: dict.__setitem__(formal.identity, "kind", "smoke"),
            lambda: dict.update(formal.events[0], {"event_id": "changed"}),
            lambda: dict.clear(blocks[0][0]),
            lambda: dict.pop(query.parameters, "page_size"),
        )
        for attempt in mutation_attempts:
            with self.assertRaises(TypeError):
                attempt()
        config.input_identity["kind"] = "smoke"
        config.verified_events[0]["event_id"] = "changed"
        self.assertEqual(formal.identity["kind"], "formal")
        self.assertEqual(formal.main_events[0]["event_id"], "event-00000")

    def test_endpoints_reject_whitespace_and_out_of_range_ports(self):
        """捕获不可用的连接身份或端口仍进入 adapter 工厂。"""
        invalid = (
            {"opengauss_host": " host"},
            {"opengauss_host": "host name"},
            {"clickhouse_host": "host\nname"},
            {"opengauss_host": "host\u0080name"},
            {"opengauss_container": "container\tname"},
            {"clickhouse_container": "container\u200bname"},
            {"clickhouse_container": " container"},
            {"opengauss_port": 65_536},
            {"clickhouse_port": 65_536},
            {"clickhouse_port": True},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    production.EngineEndpoints(**values)
        endpoints = production.EngineEndpoints(
            opengauss_host="db.example", clickhouse_host="::1",
            opengauss_container="gauss-v6", clickhouse_container="clickhouse-25",
            opengauss_port=1, clickhouse_port=65_535,
        )
        self.assertEqual((endpoints.opengauss_host, endpoints.clickhouse_host), ("db.example", "::1"))


if __name__ == "__main__":
    unittest.main()
