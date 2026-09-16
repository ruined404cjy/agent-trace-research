import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from common import PayloadRecord, TruthCatalog
from run_layout_matrix import (
    FORMAL_GENERATION_FORMAT,
    FORMAL_QUERY_WINDOW,
    FORMAL_SOURCE_ARTIFACTS,
    FORMAL_SOURCE_MANIFEST,
)
import production


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
    for index in range(48_534):
        cohort = "main" if index < 160 else None
        profile = profiles[index // 40] if index < 160 else None
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
    return truth, tuple(events), {"kind": "formal", "identity_sha256": "0" * 64}


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
        self.assertTrue(all(
            row["payload_path"] is None and row["content_length"] is None
            for row in loaded.main_events[160:]
        ))
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
        formal = production.FormalInput(Path("/tmp/formal"), None, (), {}, {}, (), (), (), {})
        endpoints = production.EngineEndpoints()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for engine in ("opengauss", "clickhouse"):
                for layout in ("same_table", "separate", "full_core", "asset_ref"):
                    asset_root = root / f"{engine}-{layout}"
                    adapter = production.create_adapter(
                        engine, layout, "jsons3factory", formal,
                        asset_root if layout == "asset_ref" else root / "ignored", endpoints,
                    )
                    self.assertEqual(adapter.layout, layout)
                    self.assertEqual(adapter.input_root, Path("/tmp/formal").resolve())
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
        formal = production.FormalInput(
            Path("/tmp/formal"), None, ({"ingest_seq": 0},), {"kind": "formal"}, {},
            ({"ingest_seq": 0},), (({"ingest_seq": 0},),), (("query", "truth"),), {"payload_count": 160},
        )
        config = production.candidate_config(formal, Path("/tmp/out"), Path("/tmp/assets"), ("runner",))

        self.assertEqual((config.engine, config.layout, config.workload), ("clickhouse", "asset_ref", "main"))
        self.assertEqual((config.round_index, config.round_order), (0, ("same_table", "separate", "full_core", "asset_ref")))
        self.assertEqual((config.measurements, config.batch_measurements), (30, 5))
        self.assertEqual(config.verified_events, formal.main_events)
        self.assertEqual(production.part_state_inputs(formal), (formal.main_blocks, formal.main_queries))


if __name__ == "__main__":
    unittest.main()
