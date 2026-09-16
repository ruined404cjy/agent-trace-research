import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

from assets import AssetRecord
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
    """以真实 LocalAssetStore 和 AssetResolver 依赖构造六种确定性故障。"""

    def inject(self, case, fixture, *, adapter, catalog, store):
        stored = store.publish_bytes(fixture.record.sha256, fixture.payload)
        available = AssetRecord.from_stored(fixture.record, stored)
        transitions = [
            {"asset_id": fixture.record.sha256, "before": "pending", "after": "available"},
        ]

        if case.name == "missing":
            catalog.publish(fixture.reference, available)
            stored.path.unlink()
            point = "remove_published_object"
        elif case.name == "corrupt":
            catalog.publish(fixture.reference, available)
            stored.path.write_bytes(b"x" * len(fixture.payload))
            point = "modify_published_bytes"
        elif case.name == "metadata_mismatch":
            catalog.publish(
                fixture.reference,
                replace(available, content_type="text/plain"),
            )
            point = "replace_catalog_metadata"
        elif case.name == "upload_then_db_failure":
            transitions.append(
                {"asset_id": fixture.record.sha256, "before": "available",
                 "after": "db_write_failed"}
            )
            point = "fail_after_object_upload"
        elif case.name == "publish_failure":
            catalog.publish(
                fixture.reference,
                replace(available, status="failed", error_category="failed"),
            )
            transitions[-1] = {
                "asset_id": fixture.record.sha256, "before": "pending", "after": "failed",
            }
            point = "fail_pending_publication"
        elif case.name == "delete_failure":
            catalog.publish(
                fixture.reference,
                replace(available, status="deleting"),
            )
            transitions.append(
                {"asset_id": fixture.record.sha256, "before": "available", "after": "deleting"}
            )
            point = "fail_deleting_object_removal"
        else:
            raise AssertionError("unexpected failure case: " + case.name)

        return {"injection_point": point, "catalog_transitions": tuple(transitions)}

    def reconcile(self, case, fixture, injection, *, adapter, catalog, store):
        orphans = store.find_orphans(catalog.reachable_paths())
        return {
            "orphan_paths": tuple(str(path) for path in orphans),
            "orphan_count": len(orphans),
        }

    def recover(self, case, fixture, injection, reconcile, *, adapter, catalog, store):
        asset_id = fixture.record.sha256
        if case.name == "missing":
            stored = store.publish_bytes(asset_id, fixture.payload)
            catalog.record = AssetRecord.from_stored(fixture.record, stored)
            return ("restore_missing_object",)
        if case.name == "corrupt":
            store.object_path(asset_id).unlink()
            stored = store.publish_bytes(asset_id, fixture.payload)
            catalog.record = AssetRecord.from_stored(fixture.record, stored)
            return ("replace_corrupt_object",)
        if case.name == "metadata_mismatch":
            catalog.record = AssetRecord(
                asset_id, asset_id, fixture.record.content_type, fixture.record.encoding,
                fixture.record.content_length, store.object_path(asset_id), "available",
            )
            return ("restore_catalog_metadata",)
        if case.name == "upload_then_db_failure":
            for path in reconcile["orphan_paths"]:
                Path(path).unlink()
            return ("remove_orphan_object",)
        if case.name == "publish_failure":
            return ("retain_failed_catalog_status",)
        if case.name == "delete_failure":
            return ("retain_deleting_catalog_status",)
        raise AssertionError("unexpected failure case: " + case.name)


class IncompleteFaultInjector(FakeFaultInjector):
    """删除注入点证据，验证运行级 manifest 保持失败。"""

    def inject(self, *arguments, **keyword_arguments):
        evidence = super().inject(*arguments, **keyword_arguments)
        return {**evidence, "injection_point": "", "catalog_transitions": ()}


class ExplodingFaultInjector(FakeFaultInjector):
    """在对象已创建后抛出异常，验证 finally 路径仍清理资源。"""

    def inject(self, *arguments, **keyword_arguments):
        super().inject(*arguments, **keyword_arguments)
        raise RuntimeError("injected fault injector failure")


class IncorrectPublishRecoveryInjector(FakeFaultInjector):
    """伪造恢复结论但把 catalog 留在可用状态，验证 runner 读取真实最终状态。"""

    def recover(self, case, fixture, injection, reconcile, *, adapter, catalog, store):
        evidence = super().recover(
            case, fixture, injection, reconcile,
            adapter=adapter, catalog=catalog, store=store,
        )
        if case.name == "publish_failure":
            catalog.record = replace(catalog.record, status="available")
        return evidence


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
        """捕获缺少注入或状态转换证据仍发布 complete manifest。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            harness = FakeHarness()

            with self.assertRaisesRegex(RuntimeError, "missing required evidence"):
                self.run_catalog(workspace, harness, IncompleteFaultInjector())

            manifest = json.loads((workspace / "run-manifest.json").read_text("utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(len(manifest["results"]), 6)
            self.assertTrue(all(
                item["cleanup"]["namespace_removed"]
                and item["cleanup"]["object_directory_removed"]
                for item in manifest["results"]
            ))

    def test_catalog_final_status_comes_from_catalog_after_recovery(self):
        """捕获 injector 伪造 failed 结论而 catalog 实际恢复为 available。"""
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)

            with self.assertRaisesRegex(RuntimeError, "publish failure final status"):
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


if __name__ == "__main__":
    unittest.main()
