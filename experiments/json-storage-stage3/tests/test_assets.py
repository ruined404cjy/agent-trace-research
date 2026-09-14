import hashlib
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))

import assets
from common import PayloadRecord


PAYLOAD = b'{"content":"asset payload"}'
RECORD = PayloadRecord(
    event_id="trace-a:span-1",
    trace_id="trace-a",
    project_id="Leoxx/whowhen_pro",
    start_time="2030-01-01T00:00:00.000Z",
    cohort="main",
    profile="text_64k",
    content_type="application/json",
    encoding="utf-8",
    content_length=len(PAYLOAD),
    preview=PAYLOAD.decode("utf-8"),
    sha256=hashlib.sha256(PAYLOAD).hexdigest(),
    payload_path="payloads/asset.json",
)


class FakeCatalog:
    """模拟数据库 assets 表的只读边界。"""

    def __init__(self, record):
        self.record = record

    def get_available(self, asset_id):
        if self.record is None or self.record.asset_id != asset_id:
            return None
        return self.record


class AssetStoreTest(unittest.TestCase):
    """验证 Asset 运行时内容路径不依赖 truth。"""

    def test_resolver_returns_available_payload_when_all_three_identities_agree(self):
        """捕获 resolver 跳过引用、catalog 或 bytes 任一身份核对。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            catalog = FakeCatalog(assets.AssetRecord.from_stored(RECORD, stored))
            reference = assets.AssetReference.from_record(RECORD)

            resolved = assets.AssetResolver(catalog, store).resolve(reference)

            self.assertEqual(resolved.payload, PAYLOAD)
            self.assertEqual(resolved.asset_id, RECORD.sha256)
            self.assertEqual(
                reference.ref, f"asset:sha256:{hashlib.sha256(PAYLOAD).hexdigest()}"
            )
            self.assertEqual(reference.preview, PAYLOAD.decode("utf-8"))

    def test_resolver_rejects_missing_corrupt_unpublished_and_metadata_mismatch(self):
        """捕获缺失、损坏、未发布或元数据错误仍返回 payload。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            reference = assets.AssetReference.from_record(RECORD)
            available = assets.AssetRecord.from_stored(RECORD, stored)

            stored.path.unlink()
            with self.assertRaisesRegex(assets.AssetError, "^missing$"):
                assets.AssetResolver(FakeCatalog(available), store).resolve(reference)

            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            stored.path.write_bytes(b"x" * len(PAYLOAD))
            corrupt = assets.AssetRecord.from_stored(RECORD, stored)
            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                assets.AssetResolver(FakeCatalog(corrupt), store).resolve(reference)

            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            for status in ("pending", "failed", "deleting"):
                with self.subTest(status=status):
                    record = replace(
                        assets.AssetRecord.from_stored(RECORD, stored), status=status
                    )
                    with self.assertRaisesRegex(assets.AssetError, f"^{status}$"):
                        assets.AssetResolver(FakeCatalog(record), store).resolve(reference)

            mismatch = replace(
                assets.AssetRecord.from_stored(RECORD, stored), content_type="text/plain"
            )
            with self.assertRaisesRegex(assets.AssetError, "^metadata_mismatch$"):
                assets.AssetResolver(FakeCatalog(mismatch), store).resolve(reference)

    def test_store_publishes_only_verified_bytes_with_same_directory_replace(self):
        """捕获直接写最终对象或校验失败后留下可解析对象。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            observed = {}
            real_replace = os.replace

            def observe_replace(source, destination):
                source = Path(source)
                destination = Path(destination)
                observed.update(
                    source_parent=source.parent,
                    destination_parent=destination.parent,
                    source_exists=source.exists(),
                    destination_exists=destination.exists(),
                )
                return real_replace(source, destination)

            with patch.object(assets.os, "replace", side_effect=observe_replace):
                stored = store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertEqual(observed["source_parent"], observed["destination_parent"])
            self.assertTrue(observed["source_exists"])
            self.assertFalse(observed["destination_exists"])
            self.assertEqual(stored.path.read_bytes(), PAYLOAD)
            self.assertEqual(tuple(stored.path.parent.glob(".*.tmp")), ())

            wrong_id = "0" * 64
            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.publish_bytes(wrong_id, PAYLOAD)
            self.assertFalse(store.object_path(wrong_id).exists())

    def test_store_replace_failure_leaves_no_resolvable_object(self):
        """捕获原子替换失败后遗留对象或未归类的发布错误。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            with patch.object(assets.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(assets.AssetError, "^failed$"):
                    store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertFalse(store.object_path(RECORD.sha256).exists())
            self.assertEqual(
                tuple(store.object_path(RECORD.sha256).parent.glob(".*.tmp")), ()
            )
            missing = assets.AssetRecord(
                asset_id=RECORD.sha256,
                sha256=RECORD.sha256,
                content_type=RECORD.content_type,
                encoding=RECORD.encoding,
                content_length=RECORD.content_length,
                storage_path=store.object_path(RECORD.sha256),
            )
            with self.assertRaisesRegex(assets.AssetError, "^missing$"):
                assets.AssetResolver(FakeCatalog(missing), store).resolve(
                    assets.AssetReference.from_record(RECORD)
                )

    def test_store_finds_only_unreachable_published_objects(self):
        """捕获 orphan 检测把临时文件或可达对象报告为遗留内容。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            first = store.publish_bytes(RECORD.sha256, PAYLOAD)
            other_payload = b'{"content":"orphan"}'
            second = store.publish_bytes(
                hashlib.sha256(other_payload).hexdigest(), other_payload
            )
            (first.path.parent / ".publication.tmp").write_bytes(b"temporary")

            self.assertEqual(store.find_orphans({first.path}), (second.path,))

    def test_resolver_rejects_catalog_path_outside_local_store(self):
        """捕获 catalog 路径越出本地对象目录仍被读取。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = assets.LocalAssetStore(root / "store")
            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            outside_path = root / "outside-object"
            outside_path.write_bytes(PAYLOAD)
            record = replace(
                assets.AssetRecord.from_stored(RECORD, stored), storage_path=outside_path
            )

            with self.assertRaisesRegex(assets.AssetError, "^metadata_mismatch$"):
                assets.AssetResolver(FakeCatalog(record), store).resolve(
                    assets.AssetReference.from_record(RECORD)
                )
