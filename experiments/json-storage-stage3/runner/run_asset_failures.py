"""阶段三 Asset 六种确定性故障的独立运行入口。"""

import hashlib
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from assets import AssetCatalogReader, AssetError, AssetReference, AssetResolver, LocalAssetStore
from common import PayloadRecord
from run_layout_matrix import write_manifest_atomic


FAILURE_CASE_NAMES = (
    "missing",
    "corrupt",
    "metadata_mismatch",
    "upload_then_db_failure",
    "publish_failure",
    "delete_failure",
)

CASE_EXPECTATIONS = {
    "missing": (("stable resolver category", "error", "missing"),),
    "corrupt": (("stable resolver category", "error", "corrupt"),),
    "metadata_mismatch": (("stable resolver category", "error", "metadata_mismatch"),),
    "upload_then_db_failure": (
        ("upload failure event visibility", "event_visible", False),
        ("upload failure orphan count", "orphan_count", 1),
    ),
    "publish_failure": (("publish failure final status", "final_status", "failed"),),
    "delete_failure": (
        ("delete failure final status", "final_status", frozenset({"deleting", "failed"})),
    ),
}


@dataclass(frozen=True)
class FailureCase:
    """描述一个固定 Asset 故障类型。"""

    name: str


FAILURE_CASES = tuple(FailureCase(name) for name in FAILURE_CASE_NAMES)


@dataclass(frozen=True)
class FailureFixture:
    """保存单个故障场景所需的最小事件引用和内容。"""

    record: PayloadRecord
    reference: AssetReference
    payload: bytes


@dataclass(frozen=True)
class FailureResult:
    """保存一个故障场景从注入到清理的完整可发布证据。"""

    case: str
    namespace: str
    injection_point: str | None
    catalog_transitions: tuple[dict[str, object], ...]
    resolver: dict[str, object] | None
    event_visible: bool | None
    reconcile: dict[str, object] | None
    recovery_actions: tuple[str, ...]
    final_status: str | None
    cleanup: dict[str, object]
    execution_error: str | None = None

    @property
    def error(self):
        """返回 resolver 的稳定错误分类。"""
        return None if self.resolver is None else self.resolver.get("error")

    @property
    def orphan_count(self):
        """返回 reconciler 识别到的 orphan 数量。"""
        return None if self.reconcile is None else self.reconcile.get("orphan_count")


class FailureAdapter(Protocol):
    """定义故障场景所需的 namespace 生命周期边界。"""

    def create(self): ...
    def cleanup(self): ...


class FailureCatalog(AssetCatalogReader, Protocol):
    """定义 resolver 和故障核对所需的 catalog 读取边界。"""

    def event_visible(self, reference: AssetReference): ...
    def reachable_paths(self): ...


class FaultInjector(Protocol):
    """注入、核对和恢复的最小边界；返回值进入 FailureResult。"""

    def inject(self, case, fixture, *, adapter, catalog, store): ...
    def reconcile(self, case, fixture, injection, *, adapter, catalog, store): ...
    def recover(self, case, fixture, injection, reconcile, *, adapter, catalog, store): ...


def build_failure_fixture(case: FailureCase) -> FailureFixture:
    """为一个固定故障场景构造独立的最小内容和事件引用。"""
    if case.name not in FAILURE_CASE_NAMES:
        raise ValueError("unsupported asset failure case: " + case.name)
    payload = ('{"content":"asset failure ' + case.name + '"}').encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    record = PayloadRecord(
        event_id="asset-failure:" + case.name,
        trace_id="asset-failure-trace:" + case.name,
        project_id="agent-trace-asset-failure",
        start_time="2030-01-01T00:00:00.000Z",
        cohort="asset_failure",
        profile="asset_failure",
        content_type="application/json",
        encoding="utf-8",
        content_length=len(payload),
        preview=payload.decode("utf-8"),
        sha256=digest,
        payload_path="asset-failures/" + case.name + ".json",
    )
    return FailureFixture(record, AssetReference.from_record(record), payload)


def _default_namespace(case: FailureCase) -> str:
    """返回一个符合 adapter 标识符约束的独占 namespace。"""
    return "jsons3_asset_failure_" + case.name + "_" + uuid.uuid4().hex[:10]


def _resolver_evidence(catalog: FailureCatalog, store: LocalAssetStore, reference: AssetReference):
    """仅在 resolver 已完成完整性校验后记录成功内容摘要。"""
    try:
        resolved = AssetResolver(catalog, store).resolve(reference)
    except AssetError as error:
        return {
            "error": error.category,
            "content_visible": False,
            "content_length": None,
            "sha256": None,
            "preview": None,
        }

    payload = resolved.payload
    preview = payload.decode(reference.encoding)[:200]
    if (
        len(payload) != reference.content_length
        or hashlib.sha256(payload).hexdigest() != reference.asset_id
        or preview != reference.preview
    ):
        raise RuntimeError("resolver returned content without complete verification")
    return {
        "error": None,
        "content_visible": True,
        "content_length": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "preview": preview,
    }


def _catalog_final_status(catalog: FailureCatalog, asset_id: str):
    """从实际 catalog 读取恢复后的最终状态。"""
    record = catalog.get_available(asset_id)
    return "absent" if record is None else record.status


def _cleanup_case(adapter, namespace, case_directory, object_directory, owns_directory):
    """清理一个场景的逻辑 namespace、物理目标与文件目录。"""
    errors = []
    namespace_removed = False
    adapter_cleanup_target = None
    if adapter is not None:
        try:
            result = adapter.cleanup()
            adapter_cleanup_target = result.namespace
            expected_targets = {namespace, getattr(adapter, "schema", None),
                                getattr(adapter, "database", None)}
            if result.namespace not in expected_targets:
                errors.append("namespace cleanup: identity mismatch")
            else:
                namespace_removed = bool(result.removed)
        except Exception as error:
            errors.append("namespace cleanup: " + (str(error) or type(error).__name__))
    if owns_directory:
        try:
            if case_directory.is_symlink():
                case_directory.unlink()
            elif case_directory.exists():
                shutil.rmtree(case_directory)
        except OSError as error:
            errors.append("object cleanup: " + (str(error) or type(error).__name__))
    object_directory_removed = (
        owns_directory and not object_directory.exists() and not object_directory.is_symlink()
    )
    if not object_directory_removed:
        errors.append("object directory was not removed")
    return {"namespace": namespace, "adapter_cleanup_target": adapter_cleanup_target,
            "namespace_removed": namespace_removed, "object_directory": str(object_directory),
            "object_directory_removed": object_directory_removed, "errors": tuple(errors)}


def run_failure_case(
    case: FailureCase, workspace: Path, *, adapter_factory, catalog_factory,
    fault_injector: FaultInjector, namespace: str | None = None,
    fixture_factory=build_failure_fixture, store_factory=LocalAssetStore,
) -> FailureResult:
    """在独立 namespace 和对象目录中运行一个 Asset 故障场景。"""
    if case.name not in FAILURE_CASE_NAMES:
        raise ValueError("unsupported asset failure case: " + case.name)
    workspace = Path(workspace)
    namespace = _default_namespace(case) if namespace is None else namespace
    case_directory = workspace / "asset-failure-cases" / case.name
    object_directory = case_directory / "objects"
    adapter = injection = resolver = reconcile = None
    event_visible = final_status = execution_error = None
    recovery_actions = ()
    owns_directory = False

    try:
        if case_directory.exists() or case_directory.is_symlink():
            raise RuntimeError("asset failure case directory already exists")
        case_directory.mkdir(parents=True)
        owns_directory = True
        store = store_factory(object_directory)
        adapter = adapter_factory(namespace, object_directory)
        adapter.create()
        catalog = catalog_factory(adapter)
        fixture = fixture_factory(case)
        injection = fault_injector.inject(
            case, fixture, adapter=adapter, catalog=catalog, store=store,
        )
        resolver = _resolver_evidence(catalog, store, fixture.reference)
        event_visible = catalog.event_visible(fixture.reference)
        reconcile = fault_injector.reconcile(
            case, fixture, injection, adapter=adapter, catalog=catalog, store=store,
        )
        recovery_actions = fault_injector.recover(
            case, fixture, injection, reconcile, adapter=adapter, catalog=catalog,
            store=store,
        )
        final_status = _catalog_final_status(catalog, fixture.record.sha256)
    except Exception as error:
        execution_error = str(error) or type(error).__name__
    finally:
        cleanup = _cleanup_case(
            adapter, namespace, case_directory, object_directory, owns_directory,
        )

    return FailureResult(
        case.name,
        namespace,
        None if injection is None else injection.get("injection_point"),
        () if injection is None else tuple(injection.get("catalog_transitions", ())),
        resolver,
        event_visible,
        reconcile,
        recovery_actions,
        final_status,
        cleanup,
        execution_error,
    )


def _validate_result(case: FailureCase, result: FailureResult):
    """返回阻止 complete manifest 发布的缺失或错误证据。"""
    errors = []
    if result.case != case.name:
        errors.append("case identity")
    if result.execution_error is not None:
        errors.append("execution error: " + result.execution_error)
    if not result.injection_point:
        errors.append("injection point")
    if not result.catalog_transitions:
        errors.append("catalog transitions")
    if result.resolver is None:
        errors.append("resolver result")
    elif result.resolver.get("content_visible"):
        if (
            result.resolver.get("error") is not None
            or result.resolver.get("content_length") is None
            or result.resolver.get("sha256") is None
            or result.resolver.get("preview") is None
        ):
            errors.append("resolver visible content verification")
    elif any(value is not None for value in (
        result.resolver.get("content_length"), result.resolver.get("sha256"),
        result.resolver.get("preview"),
    )):
        errors.append("resolver hidden content")
    if result.event_visible is None:
        errors.append("event visibility")
    if result.reconcile is None:
        errors.append("reconcile result")
    elif (
        not isinstance(result.reconcile.get("orphan_count"), int)
        or result.reconcile["orphan_count"] < 0
        or not isinstance(result.reconcile.get("orphan_paths"), (tuple, list))
        or len(result.reconcile["orphan_paths"]) != result.reconcile["orphan_count"]
    ):
        errors.append("reconcile orphan evidence")
    if not result.recovery_actions:
        errors.append("recovery actions")
    if not result.final_status:
        errors.append("final status")
    if not result.cleanup.get("namespace_removed"):
        errors.append("namespace cleanup")
    if not result.cleanup.get("object_directory_removed"):
        errors.append("object directory cleanup")
    if result.cleanup.get("errors"):
        errors.append("cleanup errors")

    for label, attribute, expected in CASE_EXPECTATIONS[case.name]:
        actual = getattr(result, attribute)
        matched = actual in expected if isinstance(expected, frozenset) else actual == expected
        if not matched:
            errors.append(label)
    return errors


def run_failure_catalog(
    workspace: Path, *, adapter_factory, catalog_factory, fault_injector: FaultInjector,
    namespace_factory=_default_namespace, fixture_factory=build_failure_fixture,
    store_factory=LocalAssetStore, manifest_writer=write_manifest_atomic,
) -> dict[str, FailureResult]:
    """运行固定六种 Asset 故障，并以 manifest 门禁全部证据。"""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    namespaces = {case.name: namespace_factory(case) for case in FAILURE_CASES}
    if len(set(namespaces.values())) != len(FAILURE_CASES):
        raise ValueError("asset failure cases require distinct namespaces")

    manifest_path = workspace / "run-manifest.json"
    manifest = {
        "format": "agent-trace-json-storage-stage3-asset-failure-run",
        "format_version": 1,
        "run_id": "jsons3-asset-failures-" + uuid.uuid4().hex[:10],
        "status": "running",
        "case_order": list(FAILURE_CASE_NAMES),
        "results": [],
    }
    manifest_writer(manifest_path, manifest)
    results = {}
    errors = []
    for case in FAILURE_CASES:
        result = run_failure_case(
            case,
            workspace,
            adapter_factory=adapter_factory,
            catalog_factory=catalog_factory,
            fault_injector=fault_injector,
            namespace=namespaces[case.name],
            fixture_factory=fixture_factory,
            store_factory=store_factory,
        )
        results[case.name] = result
        errors.extend(
            case.name + ": " + error for error in _validate_result(case, result)
        )
        manifest["results"] = [asdict(value) for value in results.values()]
        manifest_writer(manifest_path, manifest)

    manifest["status"] = "failed" if errors else "complete"
    if errors:
        manifest["errors"] = errors
    manifest_writer(manifest_path, manifest)
    if errors:
        raise RuntimeError("asset failure catalog missing required evidence: " + "; ".join(errors))
    return results
