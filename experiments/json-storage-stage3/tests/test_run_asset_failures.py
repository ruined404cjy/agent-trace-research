import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import assets
from assets import AssetError, AssetRecord
from common import CleanupResult
import run_asset_failures as runner


class FakeCatalog:
    """提供故障 runner 所需的独立 catalog 读取和事件可见性边界。"""

    def __init__(self):
        self.record = None
        self.visible_references = set()

    def publish(self, reference, record):
        self.record = record
        self.visible_references.add(reference.ref)

    def get_available(self, asset_id):
        if self.record is None or self.record.asset_id != asset_id:
            return None
        return self.record

    def event_visible(self, reference):
        return reference.ref in self.visible_references

    def reachable_paths(self):
        if self.record is None:
            return set()
        return {self.record.storage_path}


class FakeAdapter:
    """模拟独占 namespace 的创建和清理，不访问主矩阵资源。"""

    def __init__(self, namespace):
        self.namespace = namespace
        self.catalog = FakeCatalog()
        self.created = False
        self.cleaned = False

    def create(self):
        self.created = True
        return {"namespace": self.namespace}

    def cleanup(self):
        self.cleaned = True
        return CleanupResult(self.namespace, True)

    def cleanup_targets(self):
        return (self.namespace,)


class WrongNamespaceCleanupAdapter(FakeAdapter):
    """模拟错误确认另一个 namespace 已清理的 adapter。"""

    def cleanup(self):
        self.cleaned = True
        return CleanupResult("asset_failure_wrong_namespace", True)


class PhysicalNamespaceCleanupAdapter(FakeAdapter):
    """模拟 adapter 删除其命名空间派生出的物理 schema。"""

    def __init__(self, namespace):
        super().__init__(namespace)
        self.schema = namespace + "_asset_ref"

    def cleanup(self):
        self.cleaned = True
        return CleanupResult(self.schema, True)

    def cleanup_targets(self):
        return (self.namespace, self.schema)


class FakeHarness:
    """保留每个 fake adapter 和对象目录，供隔离与清理断言使用。"""

    def __init__(self):
        self.adapters = []
        self.object_directories = []

    def adapter_factory(self, namespace, object_directory):
        adapter = FakeAdapter(namespace)
        self.adapters.append(adapter)
        self.object_directories.append(object_directory)
        return adapter

    @staticmethod
    def catalog_factory(adapter):
        return adapter.catalog

    @staticmethod
    def namespace_factory(case):
        return "asset_failure_" + case.name


class WrongNamespaceCleanupHarness(FakeHarness):
    """为所有 case 返回 cleanup 身份不匹配的 adapter。"""

    def adapter_factory(self, namespace, object_directory):
        adapter = WrongNamespaceCleanupAdapter(namespace)
        self.adapters.append(adapter)
        self.object_directories.append(object_directory)
        return adapter


class PhysicalNamespaceCleanupHarness(FakeHarness):
    """返回报告物理 schema 清理结果的 adapter。"""

    def adapter_factory(self, namespace, object_directory):
        adapter = PhysicalNamespaceCleanupAdapter(namespace)
        self.adapters.append(adapter)
        self.object_directories.append(object_directory)
        return adapter


class FakeFaultInjector:
    """用真实 LocalAssetStore 动作准备、注入和恢复六种确定性故障。"""

    @staticmethod
    def available(fixture, stored):
        return AssetRecord.from_stored(fixture.record, stored)

    @staticmethod
    def pending(fixture, store):
        return AssetRecord(
            fixture.record.sha256,
            fixture.record.sha256,
            fixture.record.content_type,
            fixture.record.encoding,
            fixture.record.content_length,
            store.object_path(fixture.record.sha256),
            "pending",
        )

    def prepare(self, case, fixture, *, context):
        catalog, store = context.catalog, context.store
        if case.name == "upload_then_db_failure":
            return
        if case.name == "publish_failure":
            catalog.publish(fixture.reference, self.pending(fixture, store))
            return
        stored = store.publish_bytes(fixture.record.sha256, fixture.payload)
        catalog.publish(fixture.reference, self.available(fixture, stored))

    def inject(self, case, fixture, *, context):
        catalog, store = context.catalog, context.store
        asset_id = fixture.record.sha256
        if case.name == "missing":
            store.object_path(asset_id).unlink()
            return
        if case.name == "corrupt":
            store.object_path(asset_id).write_bytes(b"x" * len(fixture.payload))
            return
        if case.name == "metadata_mismatch":
            catalog.record = replace(catalog.record, content_type="text/plain")
            return
        if case.name == "upload_then_db_failure":
            store.publish_bytes(asset_id, fixture.payload)
            return
        if case.name == "publish_failure":
            with patch.object(assets.os, "replace", side_effect=OSError("disk full")):
                with self.assert_publish_failed(store, asset_id, fixture.payload):
                    store.publish_bytes(asset_id, fixture.payload)
            catalog.record = replace(catalog.record, status="failed", error_category="failed")
            return
        if case.name == "delete_failure":
            catalog.record = replace(catalog.record, status="deleting")
            return
        raise AssertionError("unexpected failure case: " + case.name)

    @staticmethod
    def assert_publish_failed(store, asset_id, payload):
        """以 context manager 形式确认真实 store 的 injected publish 抛出失败。"""
        class PublishFailure:
            def __enter__(self):
                return self

            def __exit__(self, error_type, error, traceback):
                if not isinstance(error, AssetError) or error.category != "failed":
                    raise AssertionError("injected publish did not raise AssetError('failed')")
                return True

        return PublishFailure()

    def recover(self, case, fixture, *, context):
        catalog, store = context.catalog, context.store
        asset_id = fixture.record.sha256
        if case.name == "missing":
            stored = store.publish_bytes(asset_id, fixture.payload)
            catalog.publish(fixture.reference, self.available(fixture, stored))
            return
        if case.name == "corrupt":
            store.object_path(asset_id).unlink()
            stored = store.publish_bytes(asset_id, fixture.payload)
            catalog.publish(fixture.reference, self.available(fixture, stored))
            return
        if case.name == "metadata_mismatch":
            stored = store.publish_bytes(asset_id, fixture.payload)
            catalog.publish(fixture.reference, self.available(fixture, stored))
            return
        if case.name == "upload_then_db_failure":
            store.object_path(asset_id).unlink()
            return
        if case.name in {"publish_failure", "delete_failure"}:
            return
        raise AssertionError("unexpected failure case: " + case.name)


class MissingFaultEffectInjector(FakeFaultInjector):
    """省略 missing 的真实故障动作，验证 manifest 不接受空注入。"""

    def inject(self, case, fixture, *, context):
        if case.name == "missing":
            return
        return super().inject(case, fixture, context=context)


class ExplodingFaultInjector(FakeFaultInjector):
    """在对象已创建后抛出异常，验证 finally 路径仍清理资源。"""

    def inject(self, case, fixture, *, context):
        super().inject(case, fixture, context=context)
        raise RuntimeError("injected fault injector failure")


class IncorrectPublishRecoveryInjector(FakeFaultInjector):
    """将失败发布错误恢复为 available，验证 runner 读取真实最终状态。"""

    def recover(self, case, fixture, *, context):
        super().recover(case, fixture, context=context)
        if case.name == "publish_failure":
            context.catalog.record = replace(context.catalog.record, status="available")


class ForgedOrphanInjector(FakeFaultInjector):
    """让 catalog 声明上传对象可达，同时保留 runner 不可读取的伪报。"""

    def inject(self, case, fixture, *, context):
        super().inject(case, fixture, context=context)
        if case.name == "upload_then_db_failure":
            catalog, store = context.catalog, context.store
            catalog.record = AssetRecord(
                fixture.record.sha256,
                fixture.record.sha256,
                fixture.record.content_type,
                fixture.record.encoding,
                fixture.record.content_length,
                store.object_path(fixture.record.sha256),
            )
            self.reported_orphans = (store.object_path(fixture.record.sha256),)

    def reconcile(self, case, fixture, *, context):
        return {"orphan_count": 1, "orphan_paths": self.reported_orphans}


class ForgedLifecycleInjector(FakeFaultInjector):
    """伪造 missing 的转换和恢复字段，但不执行对应恢复。"""

    def inject(self, case, fixture, *, context):
        super().inject(case, fixture, context=context)
        return {"before": "fabricated", "after": "fabricated"}

    def recover(self, case, fixture, *, context):
        if case.name == "missing":
            return "claimed_recovery_without_action"
        return super().recover(case, fixture, context=context)


class CatalogOnlyPublishFailureInjector(FakeFaultInjector):
    """只写入 failed catalog 行，不尝试对象发布。"""

    def inject(self, case, fixture, *, context):
        if case.name != "publish_failure":
            return super().inject(case, fixture, context=context)
        catalog = context.catalog
        catalog.record = replace(catalog.record, status="failed", error_category="failed")


class PrematureFaultInjector(FakeFaultInjector):
    """在 prepare 阶段提前实施故障，inject 本身不执行动作。"""

    def prepare(self, case, fixture, *, context):
        super().prepare(case, fixture, context=context)
        super().inject(case, fixture, context=context)

    def inject(self, case, fixture, *, context):
        return


class ForgedPublishObservationInjector(FakeFaultInjector):
    """不发布对象，只尝试向 store 写入伪造的 publish 记录。"""

    saw_public_attempts = None

    def inject(self, case, fixture, *, context):
        if case.name != "publish_failure":
            return super().inject(case, fixture, context=context)
        catalog, store = context.catalog, context.store
        self.saw_public_attempts = hasattr(store, "publish_attempts")
        getattr(store, "publish_attempts", []).append({
            "asset_id": fixture.record.sha256,
            "error": "failed",
            "final_object_exists": False,
        })
        catalog.record = replace(catalog.record, status="failed", error_category="failed")


class AssetFailureRunnerTest(unittest.TestCase):
    """验证六种 Asset 故障的可重复结果和独立运行边界。"""

    def run_catalog(self, workspace, harness, injector):
        return runner.run_failure_catalog(
            workspace,
            adapter_factory=harness.adapter_factory,
            catalog_factory=harness.catalog_factory,
            fault_injector=injector,
            namespace_factory=harness.namespace_factory,
        )

    def test_failure_catalog_has_stable_expected_results(self):
        """捕获故障分类、事件不可见性或最终状态被普通成功结果替代。"""
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_catalog(Path(directory), FakeHarness(), FakeFaultInjector())

        self.assertEqual(results["missing"].error, "missing")
        self.assertEqual(results["corrupt"].error, "corrupt")
        self.assertEqual(results["metadata_mismatch"].error, "metadata_mismatch")
        self.assertFalse(results["upload_then_db_failure"].event_visible)
        self.assertEqual(results["upload_then_db_failure"].orphan_count, 1)
        self.assertEqual(results["publish_failure"].final_status, "failed")
        self.assertIn(results["delete_failure"].final_status, {"deleting", "failed"})
        self.assertFalse(results["missing"].resolver["content_visible"])
        self.assertFalse(results["publish_failure"].resolver["content_visible"])

    def test_catalog_records_runner_owned_labels_and_catalog_transitions(self):
        """捕获固定标签或 catalog 转换偏离六种故障契约。"""
        expected = {
            "missing": (
                "remove_published_object", "restore_missing_object", "available",
                {"available"}, {"available"},
            ),
            "corrupt": (
                "modify_published_bytes", "replace_corrupt_object", "available",
                {"available"}, {"available"},
            ),
            "metadata_mismatch": (
                "replace_catalog_metadata", "restore_catalog_metadata", "available",
                {"available"}, {"available"},
            ),
            "upload_then_db_failure": (
                "fail_after_object_upload", "remove_orphan_object", "absent",
                {"absent"}, {"absent"},
            ),
            "publish_failure": (
                "fail_pending_publication", "confirm_failed_publication", "pending",
                {"failed"}, {"failed"},
            ),
            "delete_failure": (
                "fail_deleting_object_removal", "confirm_delete_failure_state", "available",
                {"deleting", "failed"}, {"deleting", "failed"},
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_catalog(Path(directory), FakeHarness(), FakeFaultInjector())

        for case_name, result in results.items():
            label, recovery, prepared, injected, recovered = expected[case_name]
            self.assertEqual(result.injection_point, label)
            self.assertEqual(result.recovery_actions, (recovery,))
            self.assertEqual(result.asset_id, result.sha256)
            self.assertEqual(len(result.catalog_transitions), 2)
            injection, recovery_transition = result.catalog_transitions
            self.assertEqual(injection["phase"], "injection")
            self.assertEqual(recovery_transition["phase"], "recovery")
            self.assertEqual(injection["before"], prepared)
            self.assertIn(injection["after"], injected)
            self.assertIn(recovery_transition["before"], injected)
            self.assertIn(recovery_transition["after"], recovered)

    def test_catalog_isolates_namespaces_and_object_directories_and_publishes_manifest(self):
        """捕获 case 复用资源、删除主矩阵目录或遗漏逐 case cleanup 证据。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            main_matrix = workspace / "main-matrix-assets"
            main_matrix.mkdir()
            sentinel = main_matrix / "sentinel"
            sentinel.write_bytes(b"main matrix must remain untouched")
            harness = FakeHarness()

            self.run_catalog(workspace, harness, FakeFaultInjector())

            self.assertEqual(len(harness.adapters), 6)
            self.assertEqual(
                {adapter.namespace for adapter in harness.adapters},
                {
                    "asset_failure_missing",
                    "asset_failure_corrupt",
                    "asset_failure_metadata_mismatch",
                    "asset_failure_upload_then_db_failure",
                    "asset_failure_publish_failure",
                    "asset_failure_delete_failure",
                },
            )
            self.assertTrue(all(adapter.created and adapter.cleaned for adapter in harness.adapters))
            self.assertEqual(len(set(harness.object_directories)), 6)
            self.assertTrue(all(not path.exists() for path in harness.object_directories))
            self.assertEqual(sentinel.read_bytes(), b"main matrix must remain untouched")

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["case_order"], list(runner.FAILURE_CASE_NAMES))
            self.assertEqual(len(manifest["results"]), 6)
            self.assertTrue(all(
                item["cleanup"]["namespace_removed"]
                and item["cleanup"]["object_directory_removed"]
                for item in manifest["results"]
            ))

    def test_case_exception_still_cleans_its_namespace_and_object_directory(self):
        """捕获 fault injector 异常绕过 namespace 或文件系统清理。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            harness = FakeHarness()
            result = runner.run_failure_case(
                runner.FailureCase("missing"), workspace,
                adapter_factory=harness.adapter_factory,
                catalog_factory=harness.catalog_factory,
                fault_injector=ExplodingFaultInjector(),
                namespace="asset_failure_exception",
            )

            self.assertIn("injected fault injector failure", result.execution_error)
            self.assertTrue(result.cleanup["namespace_removed"])
            self.assertTrue(result.cleanup["object_directory_removed"])
            self.assertTrue(harness.adapters[0].cleaned)
            self.assertFalse(harness.object_directories[0].exists())

    def test_catalog_marks_manifest_failed_when_any_case_lacks_required_evidence(self):
        """捕获省略真实故障动作仍发布 complete manifest。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            with self.assertRaisesRegex(RuntimeError, "missing required evidence"):
                self.run_catalog(workspace, FakeHarness(), MissingFaultEffectInjector())

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(len(manifest["results"]), 6)

    def test_catalog_final_status_comes_from_catalog_after_recovery(self):
        """捕获 injector 伪造 failed 结论而 catalog 实际恢复为 available。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            with self.assertRaisesRegex(RuntimeError, "publish_failure"):
                self.run_catalog(
                    workspace, FakeHarness(), IncorrectPublishRecoveryInjector()
                )

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            publish_result = next(
                item for item in manifest["results"]
                if item["case"] == "publish_failure"
            )
            self.assertEqual(publish_result["final_status"], "available")
            self.assertEqual(manifest["status"], "failed")

    def test_catalog_rejects_cleanup_confirmed_for_a_different_namespace(self):
        """捕获 adapter 把其他 namespace 的 cleanup 误作当前场景的证据。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            with self.assertRaisesRegex(RuntimeError, "namespace cleanup"):
                self.run_catalog(
                    workspace, WrongNamespaceCleanupHarness(), FakeFaultInjector()
                )

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertTrue(all(
                not item["cleanup"]["namespace_removed"]
                for item in manifest["results"]
            ))

    def test_catalog_accepts_cleanup_of_the_adapters_physical_namespace(self):
        """捕获把 adapter 的 schema/database cleanup 误判为其他 namespace。"""
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_catalog(
                Path(directory), PhysicalNamespaceCleanupHarness(), FakeFaultInjector()
            )

        self.assertTrue(all(result.cleanup["namespace_removed"] for result in results.values()))
        self.assertTrue(all(
            result.cleanup["adapter_cleanup_target"] == result.namespace + "_asset_ref"
            for result in results.values()
        ))

    def test_catalog_rejects_injector_reported_orphan_when_store_finds_none(self):
        """捕获 injector 伪报 orphan=1 而实际 catalog 已声明对象可达。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "upload_then_db_failure"):
                self.run_catalog(Path(directory), FakeHarness(), ForgedOrphanInjector())

            manifest = json.loads((Path(directory) / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            upload = next(item for item in manifest["results"] if item["case"] == "upload_then_db_failure")
            self.assertEqual(upload["reconcile"]["orphan_count"], 0)

    def test_catalog_rejects_forged_transition_and_no_recovery(self):
        """捕获伪造转换或动作掩盖未完成的对象和 resolver 恢复。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                RuntimeError, r"missing: (recovery action|recovered resolver result)",
            ):
                self.run_catalog(Path(directory), FakeHarness(), ForgedLifecycleInjector())

            manifest = json.loads((Path(directory) / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            missing = next(item for item in manifest["results"] if item["case"] == "missing")
            self.assertFalse(missing["recovery_resolver"]["content_visible"])

    def test_catalog_rejects_publish_failure_without_failed_publish_attempt(self):
        """捕获只改 failed catalog 状态而未让 LocalAssetStore 发布失败。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "publish_failure"):
                self.run_catalog(
                    Path(directory), FakeHarness(), CatalogOnlyPublishFailureInjector()
                )

            manifest = json.loads((Path(directory) / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            publish = next(item for item in manifest["results"] if item["case"] == "publish_failure")
            self.assertEqual(publish["store_observation"]["publish_attempts"], [])

    def test_catalog_records_recovered_resolver_and_cleared_upload_orphan(self):
        """捕获恢复后未重新验证三类 resolver 成功或 upload orphan 已清零。"""
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_catalog(Path(directory), FakeHarness(), FakeFaultInjector())

        for case_name in ("missing", "corrupt", "metadata_mismatch"):
            self.assertTrue(results[case_name].recovery_resolver["content_visible"])
        self.assertEqual(
            results["upload_then_db_failure"].reconcile_after_recovery["orphan_count"], 0
        )

    def test_publish_failure_records_real_failed_publish_and_absent_final_object(self):
        """捕获 publish_failure 未经真实 publish_bytes 异常或留下最终对象。"""
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_catalog(Path(directory), FakeHarness(), FakeFaultInjector())

        observation = results["publish_failure"].store_observation
        self.assertFalse(observation["object_exists"])
        self.assertEqual(len(observation["publish_attempts"]), 1)
        self.assertEqual(observation["publish_attempts"][0]["error"], "failed")
        self.assertFalse(observation["publish_attempts"][0]["final_object_exists"])

    def test_catalog_rejects_fault_moved_into_prepare(self):
        """捕获任一 case 的 prepare 已破坏基线而 inject 为空操作。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "prepared"):
                self.run_catalog(workspace, FakeHarness(), PrematureFaultInjector())

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(len(manifest["results"]), len(runner.FAILURE_CASE_NAMES))
            self.assertTrue(all(
                item["injection_point"] is None for item in manifest["results"]
            ))

    def test_catalog_rejects_forged_publish_observation_without_real_call(self):
        """捕获 injector 伪造 publish 记录掩盖底层发布调用数为零。"""
        stores = {}

        class CountingStore(assets.LocalAssetStore):
            def __init__(self, root):
                super().__init__(root)
                self.publish_calls = 0

            def publish_bytes(self, asset_id, payload):
                self.publish_calls += 1
                return super().publish_bytes(asset_id, payload)

        def store_factory(root):
            store = CountingStore(root)
            stores[root.parent.name] = store
            return store

        injector = ForgedPublishObservationInjector()
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "publish_failure"):
                runner.run_failure_catalog(
                    workspace,
                    adapter_factory=FakeHarness().adapter_factory,
                    catalog_factory=FakeHarness.catalog_factory,
                    fault_injector=injector,
                    namespace_factory=FakeHarness.namespace_factory,
                    store_factory=store_factory,
                )

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertFalse(injector.saw_public_attempts)
            self.assertEqual(stores["publish_failure"].publish_calls, 0)

    def test_fixture_factory_failure_publishes_failed_manifest_with_cleanup(self):
        """捕获 fixture 构造异常跳过 case 诊断和最终 failed manifest。"""
        def fixture_factory(case):
            if case.name == "missing":
                raise RuntimeError("fixture construction failed")
            return runner.build_failure_fixture(case)

        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "missing required evidence"):
                runner.run_failure_catalog(
                    workspace,
                    adapter_factory=FakeHarness().adapter_factory,
                    catalog_factory=FakeHarness.catalog_factory,
                    fault_injector=FakeFaultInjector(),
                    namespace_factory=FakeHarness.namespace_factory,
                    fixture_factory=fixture_factory,
                )

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            missing = next(item for item in manifest["results"] if item["case"] == "missing")
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(len(manifest["results"]), 6)
            self.assertIn("fixture construction failed", missing["execution_error"])
            self.assertTrue(missing["cleanup"]["namespace_removed"])
            self.assertTrue(missing["cleanup"]["object_directory_removed"])


if __name__ == "__main__":
    unittest.main()
