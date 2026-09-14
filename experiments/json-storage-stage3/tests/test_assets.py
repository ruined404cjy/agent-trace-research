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

            stored.path.unlink()
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

    def test_resolver_prioritizes_unpublished_status_over_stale_metadata(self):
        """捕获未发布对象因陈旧内容元数据被错误分类为 metadata_mismatch。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            reference = assets.AssetReference.from_record(RECORD)
            for status in ("pending", "failed", "deleting"):
                with self.subTest(status=status):
                    stale = replace(
                        assets.AssetRecord.from_stored(RECORD, stored),
                        content_type="text/plain",
                        encoding="ascii",
                        content_length=0,
                        storage_path=Path("stale-path"),
                        status=status,
                    )
                    with self.assertRaisesRegex(assets.AssetError, f"^{status}$"):
                        assets.AssetResolver(FakeCatalog(stale), store).resolve(reference)

    def test_store_rejects_symlinked_shard_without_writing_outside_root(self):
        """捕获分片目录符号链接把发布写入带出 store root。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = assets.LocalAssetStore(root / "store")
            outside = root / "outside"
            outside.mkdir()
            (store.root / RECORD.sha256[:2]).symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertEqual(tuple(outside.iterdir()), ())

    def test_store_normalizes_self_referential_shard_symlink(self):
        """捕获自引用分片 symlink 泄漏 resolve 的 RuntimeError。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = assets.LocalAssetStore(root / "store")
            shard = store.root / RECORD.sha256[:2]
            outside = root / "outside"
            outside.mkdir()
            shard.symlink_to(shard, target_is_directory=True)

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.publish_bytes(RECORD.sha256, PAYLOAD)
            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.find_orphans({shard})

            self.assertEqual(tuple(store.root.iterdir()), (shard,))
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_orphan_scan_normalizes_self_referential_reachable_path(self):
        """捕获 orphan 可达路径解析泄漏自引用 symlink 的 RuntimeError。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            shard = store.root / RECORD.sha256[:2]
            shard.symlink_to(shard, target_is_directory=True)

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.find_orphans({shard})

    def test_store_rejects_symlinked_final_object_without_overwriting_it(self):
        """捕获最终对象符号链接被原子替换覆盖或跟随。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = assets.LocalAssetStore(root / "store")
            destination = store.object_path(RECORD.sha256)
            destination.parent.mkdir()
            outside = root / "outside-object"
            outside.write_bytes(b"outside")
            destination.symlink_to(outside)

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(outside.read_bytes(), b"outside")

    def test_orphan_scan_rejects_symlinked_shard_instead_of_returning_outside_path(self):
        """捕获 orphan 扫描经分片符号链接返回 root 外路径。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = assets.LocalAssetStore(root / "store")
            outside = root / "outside"
            outside.mkdir()
            (outside / RECORD.sha256).write_bytes(PAYLOAD)
            (store.root / RECORD.sha256[:2]).symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.find_orphans(set())

    def test_orphan_scan_rejects_symlinked_object_instead_of_returning_outside_path(self):
        """捕获 orphan 扫描解析最终对象符号链接到 root 外。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = assets.LocalAssetStore(root / "store")
            destination = store.object_path(RECORD.sha256)
            destination.parent.mkdir()
            outside = root / "outside-object"
            outside.write_bytes(PAYLOAD)
            destination.symlink_to(outside)

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.find_orphans(set())

    def test_store_reuses_an_existing_verified_object_without_replacing_it(self):
        """捕获重复发布已验证对象仍执行替换或报告失败。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            first = store.publish_bytes(RECORD.sha256, PAYLOAD)
            with patch.object(assets.os, "replace", side_effect=OSError("should not replace")):
                repeated = store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertEqual(repeated, first)
            self.assertEqual(first.path.read_bytes(), PAYLOAD)

    def test_store_rejects_existing_corrupt_object_without_overwriting_it(self):
        """捕获重复发布覆盖同地址下损坏的已有对象。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            stored = store.publish_bytes(RECORD.sha256, PAYLOAD)
            stored.path.write_bytes(b"corrupt")

            with self.assertRaisesRegex(assets.AssetError, "^corrupt$"):
                store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertEqual(stored.path.read_bytes(), b"corrupt")

    def test_store_normalizes_mkdir_and_mkstemp_failures(self):
        """捕获准备目录或创建临时文件泄漏原始 OSError。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            with patch.object(Path, "mkdir", side_effect=OSError("mkdir failed")):
                with self.assertRaisesRegex(assets.AssetError, "^failed$"):
                    store.publish_bytes(RECORD.sha256, PAYLOAD)

        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            with patch.object(assets.tempfile, "mkstemp", side_effect=OSError("mkstemp failed")):
                with self.assertRaisesRegex(assets.AssetError, "^failed$"):
                    store.publish_bytes(RECORD.sha256, PAYLOAD)

    def test_store_normalizes_write_failure_and_cleans_temporary_file(self):
        """捕获写临时文件失败泄漏原始 OSError 或遗留临时文件。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            real_fdopen = assets.os.fdopen

            class FailingWriter:
                def __init__(self, descriptor):
                    self.handle = real_fdopen(descriptor, "wb")

                def __enter__(self):
                    return self

                def __exit__(self, *arguments):
                    return self.handle.__exit__(*arguments)

                def write(self, value):
                    raise OSError("write failed")

            with patch.object(
                assets.os, "fdopen", side_effect=lambda descriptor, mode: FailingWriter(descriptor)
            ):
                with self.assertRaisesRegex(assets.AssetError, "^failed$"):
                    store.publish_bytes(RECORD.sha256, PAYLOAD)

            destination = store.object_path(RECORD.sha256)
            self.assertFalse(destination.exists())
            self.assertEqual(tuple(destination.parent.glob(".*.tmp")), ())

    def test_store_reports_cleanup_failure_as_failed(self):
        """捕获临时文件清理失败被伪装为发布成功或原始 OSError。"""
        with tempfile.TemporaryDirectory() as directory:
            store = assets.LocalAssetStore(Path(directory))
            real_unlink = Path.unlink

            def fail_temporary_unlink(path, *args, **kwargs):
                if Path(path).name.startswith(f".{RECORD.sha256}."):
                    raise OSError("unlink failed")
                return real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=fail_temporary_unlink):
                with self.assertRaisesRegex(assets.AssetError, "^failed$"):
                    store.publish_bytes(RECORD.sha256, PAYLOAD)

            self.assertEqual(store.object_path(RECORD.sha256).read_bytes(), PAYLOAD)
