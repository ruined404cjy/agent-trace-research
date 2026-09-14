"""阶段三 Asset 内容存储与运行时恢复契约。"""

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from common import PayloadRecord


def _is_sha256(value):
    """判断值是否为小写 SHA-256 十六进制摘要。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class AssetError(RuntimeError):
    """表示可记录的 Asset 运行时错误分类。"""

    def __init__(self, category):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class AssetReference:
    """保存事件中固定格式的 Asset 内容引用。"""

    ref: str
    content_type: str
    encoding: str
    content_length: int
    preview: str

    @property
    def asset_id(self):
        """返回引用中的 SHA-256 内容地址。"""
        prefix = "asset:sha256:"
        if not isinstance(self.ref, str) or not self.ref.startswith(prefix):
            raise AssetError("metadata_mismatch")
        asset_id = self.ref.removeprefix(prefix)
        if not _is_sha256(asset_id):
            raise AssetError("metadata_mismatch")
        return asset_id

    @classmethod
    def from_record(cls, record: PayloadRecord):
        """从冻结 payload 元数据构造事件引用。"""
        return cls(
            ref=f"asset:sha256:{record.sha256}",
            content_type=record.content_type,
            encoding=record.encoding,
            content_length=record.content_length,
            preview=record.preview,
        )


@dataclass(frozen=True)
class StoredObject:
    """描述已发布的本地内容寻址对象。"""

    asset_id: str
    sha256: str
    content_length: int
    path: Path


@dataclass(frozen=True)
class AssetRecord:
    """描述数据库 assets catalog 的运行时读取结果。"""

    asset_id: str
    sha256: str
    content_type: str
    encoding: str
    content_length: int
    storage_path: Path
    status: str = "available"
    updated_at: str | None = None
    error_category: str | None = None

    @classmethod
    def from_stored(cls, record: PayloadRecord, stored: StoredObject):
        """由 payload 元数据和已发布对象构造可用 catalog 行。"""
        return cls(
            asset_id=record.sha256,
            sha256=record.sha256,
            content_type=record.content_type,
            encoding=record.encoding,
            content_length=record.content_length,
            storage_path=stored.path,
        )


@dataclass(frozen=True)
class ResolvedAsset:
    """描述完成完整性核对后可交给上层的内容。"""

    asset_id: str
    payload: bytes


class AssetCatalogReader(Protocol):
    """定义 resolver 所需的数据库 catalog 只读边界。"""

    def get_available(self, asset_id: str) -> AssetRecord | None:
        """读取指定内容地址的 catalog 行。"""


class LocalAssetStore:
    """在调用方提供的目录中管理内容寻址对象。"""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def object_path(self, asset_id: str) -> Path:
        """返回 SHA-256 内容地址的唯一最终对象路径。"""
        if not _is_sha256(asset_id):
            raise AssetError("corrupt")
        return self.root / asset_id[:2] / asset_id

    def publish_bytes(self, asset_id: str, payload: bytes) -> StoredObject:
        """校验 bytes 后以同目录临时文件原子发布内容对象。"""
        if not isinstance(payload, bytes):
            raise AssetError("corrupt")
        destination = self.object_path(asset_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{asset_id}.", suffix=".tmp", dir=destination.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            published_bytes = temporary_path.read_bytes()
            if (
                len(published_bytes) != len(payload)
                or hashlib.sha256(published_bytes).hexdigest() != asset_id
            ):
                raise AssetError("corrupt")
            os.replace(temporary_path, destination)
        except OSError as error:
            raise AssetError("failed") from error
        finally:
            temporary_path.unlink(missing_ok=True)
        return StoredObject(
            asset_id=asset_id,
            sha256=asset_id,
            content_length=len(payload),
            path=destination,
        )

    def read_bytes(self, asset_id: str, storage_path: Path) -> bytes:
        """读取指定 catalog 路径，并限制其为内容地址的最终路径。"""
        expected_path = self.object_path(asset_id)
        candidate_path = Path(storage_path).resolve()
        if candidate_path != expected_path:
            raise AssetError("metadata_mismatch")
        try:
            return candidate_path.read_bytes()
        except FileNotFoundError as error:
            raise AssetError("missing") from error
        except OSError as error:
            raise AssetError("corrupt") from error

    def find_orphans(self, reachable_paths: set[Path]) -> tuple[Path, ...]:
        """返回目录中没有被调用方声明为可达的已发布对象。"""
        reachable = {
            (self.root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
            for path in reachable_paths
        }
        objects = []
        for path in self.root.glob("*/*"):
            if (
                path.is_file()
                and _is_sha256(path.name)
                and path.parent.name == path.name[:2]
            ):
                objects.append(path.resolve())
        return tuple(sorted((path for path in objects if path not in reachable), key=str))


class AssetResolver:
    """通过事件引用、数据库 catalog 与本地对象恢复完整内容。"""

    def __init__(self, catalog: AssetCatalogReader, store: LocalAssetStore):
        self.catalog = catalog
        self.store = store

    def resolve(self, reference: AssetReference) -> ResolvedAsset:
        """核对引用、catalog 和实际 bytes 后返回可用内容。"""
        asset_id = reference.asset_id
        record = self.catalog.get_available(asset_id)
        if record is None:
            raise AssetError("missing")
        if (
            record.asset_id != asset_id
            or record.sha256 != asset_id
            or record.content_type != reference.content_type
            or record.encoding != reference.encoding
            or record.content_length != reference.content_length
        ):
            raise AssetError("metadata_mismatch")
        if record.status != "available":
            if record.status in {"pending", "failed", "deleting"}:
                raise AssetError(record.status)
            raise AssetError("failed")
        payload = self.store.read_bytes(asset_id, record.storage_path)
        try:
            preview = payload.decode(reference.encoding)[:200]
        except (LookupError, UnicodeDecodeError) as error:
            raise AssetError("corrupt") from error
        if (
            len(payload) != reference.content_length
            or hashlib.sha256(payload).hexdigest() != asset_id
            or preview != reference.preview
        ):
            raise AssetError("corrupt")
        return ResolvedAsset(asset_id=asset_id, payload=payload)
