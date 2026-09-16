"""阶段三 Asset 六种确定性故障的独立运行入口。"""

import hashlib
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol

from assets import (
    AssetCatalogReader, AssetError, AssetReference, AssetResolver, LocalAssetStore,
    StoredObject,
)
from common import PayloadRecord
from run_layout_matrix import write_manifest_atomic


CATALOG_STATUSES = frozenset({"absent", "pending", "available", "failed", "deleting"})
FAILURE_CASE_NAMES = (
    "missing", "corrupt", "metadata_mismatch", "upload_then_db_failure",
    "publish_failure", "delete_failure",
)


@dataclass(frozen=True)
class FailureCase:
    """描述一个固定 Asset 故障类型。"""
    name: str


@dataclass(frozen=True)
class ExpectedState:
    """描述 runner 必须独立观测到的单阶段状态。"""
    statuses: frozenset[str]
    resolver_errors: frozenset[str | None]
    orphan_count: int
    object_exists: bool
    event_visible: bool | None = None
    publish_errors: frozenset[str | None] | None = None


@dataclass(frozen=True)
class CaseSpec:
    """保存 runner 生成的固定标签与前后置条件。"""
    injection_point: str
    recovery_action: str
    prepared_status: str
    injected: ExpectedState
    recovered: ExpectedState


def _state(statuses, errors, orphans, exists, visible=None, publish_errors=None):
    """构造简洁的固定状态规则。"""
    return ExpectedState(
        frozenset(statuses),
        frozenset(errors),
        orphans,
        exists,
        visible,
        None if publish_errors is None else frozenset(publish_errors),
    )


CASE_SPECS = {
    "missing": CaseSpec(
        "remove_published_object", "restore_missing_object", "available",
        _state({"available"}, {"missing"}, 0, False, True),
        _state({"available"}, {None}, 0, True)),
    "corrupt": CaseSpec(
        "modify_published_bytes", "replace_corrupt_object", "available",
        _state({"available"}, {"corrupt"}, 0, True, True),
        _state({"available"}, {None}, 0, True)),
    "metadata_mismatch": CaseSpec(
        "replace_catalog_metadata", "restore_catalog_metadata", "available",
        _state({"available"}, {"metadata_mismatch"}, 0, True, True),
        _state({"available"}, {None}, 0, True)),
    "upload_then_db_failure": CaseSpec(
        "fail_after_object_upload", "remove_orphan_object", "absent",
        _state({"absent"}, {"missing"}, 1, True, False, {None}),
        _state({"absent"}, {"missing"}, 0, False)),
    "publish_failure": CaseSpec(
        "fail_pending_publication", "confirm_failed_publication", "pending",
        _state({"failed"}, {"failed"}, 0, False, None, {"failed"}),
        _state({"failed"}, {"failed"}, 0, False)),
    "delete_failure": CaseSpec(
        "fail_deleting_object_removal", "confirm_delete_failure_state", "available",
        _state({"deleting", "failed"}, {"deleting", "failed"}, 0, True, True),
        _state({"deleting", "failed"}, {"deleting", "failed"}, 0, True)),
}
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
    asset_id: str | None
    sha256: str | None
    injection_point: str | None
    catalog_transitions: tuple[dict[str, str], ...]
    resolver: dict[str, object] | None
    event_visible: bool | None
    reconcile: dict[str, object] | None
    store_observation: dict[str, object] | None
    recovery_actions: tuple[str, ...]
    recovery_resolver: dict[str, object] | None
    reconcile_after_recovery: dict[str, object] | None
    recovery_store_observation: dict[str, object] | None
    final_status: str | None
    cleanup: dict[str, object]
    validation_errors: tuple[str, ...] = ()
    execution_error: str | None = None

    @property
    def error(self) -> str | None:
        """返回 resolver 的稳定错误分类。"""
        value = None if self.resolver is None else self.resolver.get("error")
        return value if isinstance(value, str) else None

    @property
    def orphan_count(self) -> int | None:
        """返回注入后 reconciler 识别到的 orphan 数量。"""
        value = None if self.reconcile is None else self.reconcile.get("orphan_count")
        return value if isinstance(value, int) else None


class FailureCatalog(AssetCatalogReader, Protocol):
    """定义 runner 观测 catalog、事件和对象可达性的边界。"""
    def event_visible(self, reference: AssetReference) -> bool: ...
    def reachable_paths(self) -> set[Path]: ...


class ObservedStore:
    """代理真实 LocalAssetStore，只记录 publish_bytes 调用结果。"""
    def __init__(self, store: LocalAssetStore) -> None:
        self._store = store
        self.publish_attempts: list[dict[str, object]] = []

    def publish_bytes(self, asset_id: str, payload: bytes) -> StoredObject:
        """调用真实发布边界并记录成功或稳定异常。"""
        try:
            stored = self._store.publish_bytes(asset_id, payload)
        except AssetError as error:
            self.publish_attempts.append({
                "asset_id": asset_id,
                "error": error.category,
                "final_object_exists": self.object_path(asset_id).exists(),
            })
            raise
        self.publish_attempts.append({
            "asset_id": asset_id,
            "error": None,
            "final_object_exists": stored.path.exists(),
        })
        return stored

    def __getattr__(self, name):
        """将非发布操作透传到真实 store。"""
        return getattr(self._store, name)


class FaultInjector(Protocol):
    """只实施准备、故障和恢复动作，不产生可发布证据。"""
    def prepare(self, case: FailureCase, fixture: FailureFixture, **context) -> None: ...
    def inject(self, case: FailureCase, fixture: FailureFixture, **context) -> None: ...
    def recover(self, case: FailureCase, fixture: FailureFixture, **context) -> None: ...


def _case_spec(case: FailureCase) -> CaseSpec:
    """返回固定 case 规则。"""
    try:
        return CASE_SPECS[case.name]
    except KeyError as error:
        raise ValueError("unsupported asset failure case: " + case.name) from error


def build_failure_fixture(case: FailureCase) -> FailureFixture:
    """为固定故障场景构造独立的最小内容。"""
    _case_spec(case)
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
    """返回符合 adapter 标识符约束的独占 namespace。"""
    return "jsons3_asset_failure_" + case.name + "_" + uuid.uuid4().hex[:10]


def _resolver_evidence(catalog, store, reference):
    """运行真实 resolver，仅记录完整校验后的成功内容。"""
    try:
        resolved = AssetResolver(catalog, store).resolve(reference)
    except AssetError as error:
        return {"error": error.category, "content_visible": False,
                "content_length": None, "sha256": None, "preview": None}
    payload = resolved.payload
    preview = payload.decode(reference.encoding)[:200]
    if (len(payload) != reference.content_length
            or hashlib.sha256(payload).hexdigest() != reference.asset_id
            or preview != reference.preview):
        raise RuntimeError("resolver returned content without complete verification")
    return {"error": None, "content_visible": True, "content_length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(), "preview": preview}


def _catalog_status(catalog: FailureCatalog, asset_id: str) -> str:
    """从真实 catalog 快照读取并校验当前状态。"""
    record = catalog.get_available(asset_id)
    if record is None:
        return "absent"
    if record.asset_id != asset_id or record.sha256 != asset_id:
        raise RuntimeError("catalog asset identity mismatch")
    if record.status not in CATALOG_STATUSES:
        raise RuntimeError("unsupported catalog status: " + str(record.status))
    return record.status


def _observe(fixture, catalog, store, publish_start=0):
    """从 catalog、resolver 和真实 store 收集单个阶段快照。"""
    asset_id = fixture.reference.asset_id
    object_path = store.object_path(asset_id)
    orphans = store.find_orphans(catalog.reachable_paths())
    return {
        "status": _catalog_status(catalog, asset_id),
        "resolver": _resolver_evidence(catalog, store, fixture.reference),
        "event_visible": catalog.event_visible(fixture.reference),
        "reconcile": {
            "orphan_count": len(orphans),
            "orphan_paths": tuple(str(path) for path in orphans),
        },
        "store": {
            "object_path": str(object_path),
            "object_exists": object_path.exists(),
            "publish_attempts": tuple(store.publish_attempts[publish_start:]),
        },
    }


def _resolver_matches(evidence, expected_errors):
    """校验 resolver 错误分类或完整成功内容。"""
    if evidence.get("error") not in expected_errors:
        return False
    detail = (evidence.get("content_length"), evidence.get("sha256"), evidence.get("preview"))
    if evidence.get("error") is None:
        return evidence.get("content_visible") is True and all(value is not None for value in detail)
    return evidence.get("content_visible") is False and all(value is None for value in detail)


def _observation_errors(phase, fixture, expected, observed):
    """返回阶段快照与固定规则之间的差异。"""
    errors = []
    if observed["status"] not in expected.statuses:
        errors.append(phase + " catalog status")
    if not _resolver_matches(observed["resolver"], expected.resolver_errors):
        errors.append(phase + " resolver result")
    if expected.event_visible is not None and observed["event_visible"] != expected.event_visible:
        errors.append(phase + " event visibility")
    reconcile = observed["reconcile"]
    paths = reconcile["orphan_paths"]
    if reconcile["orphan_count"] != expected.orphan_count:
        errors.append(phase + " orphan count")
    object_path = observed["store"]["object_path"]
    if paths != ((object_path,) if expected.orphan_count == 1 else ()):
        errors.append(phase + " orphan identity")
    if observed["store"]["object_exists"] != expected.object_exists:
        errors.append(phase + " object presence")
    if expected.publish_errors is not None:
        attempts = observed["store"]["publish_attempts"]
        if (len(attempts) != 1
                or attempts[0].get("asset_id") != fixture.reference.asset_id
                or attempts[0].get("error") not in expected.publish_errors
                or attempts[0].get("final_object_exists") != expected.object_exists):
            errors.append(phase + " publish evidence")
    return errors


def _transition(phase, asset_id, before, after):
    """从 runner 的前后 catalog 快照生成状态转换。"""
    return {"phase": phase, "asset_id": asset_id, "sha256": asset_id,
            "before": before, "after": after}


def _cleanup_case(adapter, namespace, case_directory, object_directory, owns_directory):
    """清理当前场景的 namespace 和对象目录。"""
    errors = []
    removed = False
    cleanup_target = None
    if adapter is not None:
        try:
            result = adapter.cleanup()
            cleanup_target = result.namespace
            if cleanup_target not in {namespace, *adapter.cleanup_targets()}:
                errors.append("namespace cleanup: identity mismatch")
            else:
                removed = bool(result.removed)
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
    object_removed = owns_directory and not object_directory.exists()
    if not object_removed:
        errors.append("object directory was not removed")
    return {"namespace": namespace, "adapter_cleanup_target": cleanup_target,
            "namespace_removed": removed, "object_directory": str(object_directory),
            "object_directory_removed": object_removed, "errors": tuple(errors)}


def run_failure_case(
    case: FailureCase,
    workspace: Path,
    *,
    adapter_factory: Callable,
    catalog_factory: Callable,
    fault_injector: FaultInjector,
    namespace: str | None = None,
    fixture_factory: Callable = build_failure_fixture,
    store_factory: Callable = LocalAssetStore,
) -> FailureResult:
    """按准备、注入、真实观测、恢复、后置观测运行场景。"""
    spec = _case_spec(case)
    namespace = _default_namespace(case) if namespace is None else namespace
    case_directory = Path(workspace) / "asset-failure-cases" / case.name
    object_directory = case_directory / "objects"
    fixture = fixture_factory(case)
    adapter = None
    injected = recovered = None
    prepared_status = execution_error = None
    injection_point = None
    recovery_actions = ()
    transitions = ()
    validation_errors = ()
    owns_directory = False
    try:
        if case_directory.exists() or case_directory.is_symlink():
            raise RuntimeError("asset failure case directory already exists")
        case_directory.mkdir(parents=True)
        owns_directory = True
        store = ObservedStore(store_factory(object_directory))
        adapter = adapter_factory(namespace, object_directory)
        adapter.create()
        catalog = catalog_factory(adapter)
        context = {"adapter": adapter, "catalog": catalog, "store": store}

        fault_injector.prepare(case, fixture, **context)
        prepared_status = _catalog_status(catalog, fixture.reference.asset_id)
        publish_start = len(store.publish_attempts)
        fault_injector.inject(case, fixture, **context)
        injected = _observe(fixture, catalog, store, publish_start)
        injection_errors = _observation_errors("injected", fixture, spec.injected, injected)
        if prepared_status != spec.prepared_status:
            injection_errors.append("prepared catalog status")
        if not injection_errors:
            injection_point = spec.injection_point

        fault_injector.recover(case, fixture, **context)
        recovered = _observe(fixture, catalog, store, len(store.publish_attempts))
        recovery_errors = _observation_errors("recovered", fixture, spec.recovered, recovered)
        if not recovery_errors:
            recovery_actions = (spec.recovery_action,)
        validation_errors = tuple(injection_errors + recovery_errors)
        transitions = (
            _transition("injection", fixture.reference.asset_id,
                        prepared_status, injected["status"]),
            _transition("recovery", fixture.reference.asset_id,
                        injected["status"], recovered["status"]),
        )
    except Exception as error:
        execution_error = str(error) or type(error).__name__
    finally:
        cleanup = _cleanup_case(
            adapter, namespace, case_directory, object_directory, owns_directory,
        )

    return FailureResult(
        case=case.name, namespace=namespace, asset_id=fixture.reference.asset_id,
        sha256=fixture.record.sha256, injection_point=injection_point,
        catalog_transitions=transitions,
        resolver=None if injected is None else injected["resolver"],
        event_visible=None if injected is None else injected["event_visible"],
        reconcile=None if injected is None else injected["reconcile"],
        store_observation=None if injected is None else injected["store"],
        recovery_actions=recovery_actions,
        recovery_resolver=None if recovered is None else recovered["resolver"],
        reconcile_after_recovery=None if recovered is None else recovered["reconcile"],
        recovery_store_observation=None if recovered is None else recovered["store"],
        final_status=None if recovered is None else recovered["status"],
        cleanup=cleanup, validation_errors=validation_errors,
        execution_error=execution_error,
    )


def _validate_result(case: FailureCase, result: FailureResult) -> list[str]:
    """返回阻止 complete manifest 发布的结果封装错误。"""
    spec = _case_spec(case)
    errors = list(result.validation_errors)
    if result.case != case.name:
        errors.append("case identity")
    if not result.asset_id or result.asset_id != result.sha256:
        errors.append("asset identity")
    if result.execution_error is not None:
        errors.append("execution error: " + result.execution_error)
    if result.injection_point != spec.injection_point:
        errors.append("injection point")
    if result.recovery_actions != (spec.recovery_action,):
        errors.append("recovery action")
    if len(result.catalog_transitions) != 2:
        errors.append("catalog transitions")
    if not result.cleanup.get("namespace_removed"):
        errors.append("namespace cleanup")
    if not result.cleanup.get("object_directory_removed"):
        errors.append("object directory cleanup")
    if result.cleanup.get("errors"):
        errors.append("cleanup errors")
    return errors


def run_failure_catalog(
    workspace: Path,
    *,
    adapter_factory: Callable,
    catalog_factory: Callable,
    fault_injector: FaultInjector,
    namespace_factory: Callable = _default_namespace,
    fixture_factory: Callable = build_failure_fixture,
    store_factory: Callable = LocalAssetStore,
    manifest_writer: Callable = write_manifest_atomic,
) -> dict[str, FailureResult]:
    """运行六种固定故障，仅在全部证据成立时发布 complete。"""
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
            case, workspace, adapter_factory=adapter_factory,
            catalog_factory=catalog_factory, fault_injector=fault_injector,
            namespace=namespaces[case.name], fixture_factory=fixture_factory,
            store_factory=store_factory,
        )
        results[case.name] = result
        errors.extend(case.name + ": " + error for error in _validate_result(case, result))
        manifest["results"] = [asdict(value) for value in results.values()]
        manifest_writer(manifest_path, manifest)
    manifest["status"] = "failed" if errors else "complete"
    if errors:
        manifest["errors"] = errors
    manifest_writer(manifest_path, manifest)
    if errors:
        raise RuntimeError("asset failure catalog missing required evidence: " + "; ".join(errors))
    return results
