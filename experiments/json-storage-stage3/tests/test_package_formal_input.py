import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "runner"))
sys.path.insert(0, str(STAGE_DIR / "tools"))

import package_formal_input as packager
import production


ARCHIVE_NAME = "json-storage-stage3-formal-input-20260917.tar.gz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
ARCHIVE_ROOT = "json-storage-stage3-formal-input"


class PackageFormalInputTest(unittest.TestCase):
    """验证正式输入归档的确定性、成员契约与失败可见性。"""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.workspace = Path(self._directory.name)
        self.output = self.workspace / "output"
        self.source = self.build_source()
        self.calls = []
        self.real_loader = production.load_formal_input
        gate = patch.object(production, "load_formal_input", side_effect=self.record_gate)
        self.addCleanup(gate.stop)
        self.gate = gate.start()

    def record_gate(self, root):
        """记录正式门禁调用并返回哨兵，避免单元测试依赖完整正式输入。"""
        self.calls.append(Path(root))
        return object()

    def build_source(self, payload_names=("aa.json", "zz.json")):
        """构造含三个 loader 文件与少量 payload 的最小输入树。"""
        root = self.workspace / "input"
        (root / "payloads").mkdir(parents=True)
        (root / "events.jsonl").write_bytes(b'{"event_id": "e1"}\n')
        (root / "truth.json").write_bytes(b'{"seed": 20260907}\n')
        (root / "generation-manifest.json").write_bytes(b'{"status": "complete"}\n')
        for name in payload_names:
            (root / "payloads" / name).write_bytes(b'{"payload": "' + name.encode() + b'"}')
        return root

    def expected_members(self, payload_names=("aa.json", "zz.json")):
        """返回排序后不含源目录包装元数据的归档成员列表。"""
        return [
            ARCHIVE_ROOT,
            ARCHIVE_ROOT + "/events.jsonl",
            ARCHIVE_ROOT + "/generation-manifest.json",
            ARCHIVE_ROOT + "/payloads",
            *[ARCHIVE_ROOT + "/payloads/" + name for name in sorted(payload_names)],
            ARCHIVE_ROOT + "/truth.json",
        ]

    def inventory(self, root):
        """返回输入树的相对路径与内容摘要，用于核对源目录未被改动。"""
        return {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def open_archive(self, archive):
        """打开归档并返回成员名与成员对象。"""
        with tarfile.open(archive) as opened:
            infos = opened.getmembers()
            names = [info.name for info in infos]
            payloads = {
                info.name: opened.extractfile(info).read()
                for info in infos if info.isfile()
            }
        return names, {info.name: info for info in infos}, payloads

    def assert_no_output(self, output=None):
        """断言失败后输出目录没有归档、清单或残留临时文件。"""
        output = output or self.output
        self.assertEqual(sorted(path.name for path in output.iterdir()) if output.exists() else [], [])

    def test_two_runs_are_byte_identical_and_leave_source_untouched(self):
        """捕获归档引入时间戳、顺序或源目录改动导致的不可复现。"""
        before = self.inventory(self.source)

        first_archive, first_checksum = packager.package_formal_input(
            self.source, self.workspace / "a",
        )
        second_archive, second_checksum = packager.package_formal_input(
            self.source, self.workspace / "b",
        )

        self.assertEqual(first_archive.name, ARCHIVE_NAME)
        self.assertEqual(first_archive.read_bytes(), second_archive.read_bytes())
        first_digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()
        second_digest = hashlib.sha256(second_archive.read_bytes()).hexdigest()
        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_checksum, self.workspace / "a" / CHECKSUM_NAME)
        self.assertEqual(first_checksum.read_bytes(), second_checksum.read_bytes())
        self.assertEqual(self.inventory(self.source), before)

    def test_members_are_sorted_beneath_one_root(self):
        """捕获成员顺序漂移、缺少单一根目录或混入源目录包装元数据。"""
        archive, _ = packager.package_formal_input(self.source, self.output)
        names, infos, payloads = self.open_archive(archive)

        self.assertEqual(names, self.expected_members())
        self.assertEqual(names, sorted(names))
        self.assertEqual(infos[ARCHIVE_ROOT].type, tarfile.DIRTYPE)
        self.assertEqual(infos[ARCHIVE_ROOT + "/payloads"].type, tarfile.DIRTYPE)
        self.assertEqual(payloads[ARCHIVE_ROOT + "/events.jsonl"], b'{"event_id": "e1"}\n')
        self.assertEqual(
            payloads[ARCHIVE_ROOT + "/payloads/zz.json"], b'{"payload": "zz.json"}',
        )

    def test_member_metadata_is_normalized(self):
        """捕获 owner、时间戳或权限位随打包环境漂移。"""
        archive, _ = packager.package_formal_input(self.source, self.output)
        _, infos, _ = self.open_archive(archive)

        for name, info in infos.items():
            with self.subTest(name=name):
                self.assertEqual((info.uid, info.gid), (0, 0))
                self.assertEqual((info.uname, info.gname), ("", ""))
                self.assertEqual(info.mtime, 0)
                self.assertIn(info.type, (tarfile.DIRTYPE, tarfile.REGTYPE))
                self.assertEqual(info.mode, 0o755 if info.isdir() else 0o644)

    def test_gzip_header_has_no_filename_or_timestamp(self):
        """捕获 gzip 头嵌入输出文件名或当前时间。"""
        archive, _ = packager.package_formal_input(self.source, self.output)
        raw = archive.read_bytes()

        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(raw[2], 8)
        self.assertEqual(raw[3] & 0x08, 0)
        self.assertEqual(raw[3] & 0x04, 0)
        self.assertEqual(raw[4:8], b"\x00\x00\x00\x00")
        self.assertNotIn(b"output", raw[8:32])
        head = gzip.decompress(raw)[:100].rstrip(b"\x00")
        self.assertEqual(head, (ARCHIVE_ROOT + "/").encode())

    def test_checksum_file_uses_standard_format(self):
        """捕获清单写入错误摘要、绝对路径或非标准分隔格式。"""
        archive, checksum = packager.package_formal_input(self.source, self.output)

        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.assertEqual(checksum.name, CHECKSUM_NAME)
        self.assertEqual(checksum.read_bytes(), f"{digest}  {ARCHIVE_NAME}\n".encode())
        self.assertIsNotNone(
            re.fullmatch(rf"[0-9a-f]{{64}}  {re.escape(ARCHIVE_NAME)}\n", checksum.read_text()),
        )

    def test_source_tree_is_gated_by_production_loader(self):
        """捕获跳过正式门禁、以未解析路径调用门禁或未在打包前校验。"""
        archive, _ = packager.package_formal_input(self.source, self.output)

        self.assertEqual(self.calls, [self.source.resolve()])
        self.assertTrue(self.gate.called)
        self.assertTrue(archive.is_file())

    def test_gate_failure_aborts_before_any_output(self):
        """捕获门禁失败后仍留下归档、清单或临时文件。"""
        self.gate.side_effect = ValueError("input generation is incomplete")
        output = self.workspace / "failed"

        with self.assertRaisesRegex(ValueError, "incomplete"):
            packager.package_formal_input(self.source, output)

        self.assert_no_output(output)

    def test_real_production_loader_rejects_incomplete_generation(self):
        """捕获门禁被替换为不读取 generation manifest 的表层检查。"""
        (self.source / "generation-manifest.json").write_bytes(b'{"status": "incomplete"}\n')

        with patch.object(production, "load_formal_input", self.real_loader):
            with self.assertRaisesRegex(ValueError, "input generation is incomplete"):
                packager.package_formal_input(self.source, self.output)

        self.assert_no_output()

    def test_source_only_run_manifest_is_tolerated_and_excluded(self):
        """捕获源目录包装元数据被写入归档或触碰冻结源文件。"""
        manifest = self.source / "run-manifest.json"
        manifest.write_bytes(b'{"operation": "generate-input"}\n')
        before = self.inventory(self.source)

        archive, _ = packager.package_formal_input(self.source, self.output)
        names, _, _ = self.open_archive(archive)

        self.assertEqual(names, self.expected_members())
        self.assertFalse([name for name in names if "run-manifest" in name])
        self.assertEqual(self.inventory(self.source), before)
        self.assertEqual(manifest.read_bytes(), b'{"operation": "generate-input"}\n')

    def test_source_only_run_manifest_must_be_a_regular_file(self):
        """捕获被符号链接替换的源目录包装元数据绕过校验。"""
        (self.source / "run-manifest.json").symlink_to(self.source / "truth.json")

        with self.assertRaisesRegex(ValueError, "run-manifest.json"):
            packager.package_formal_input(self.source, self.output)

        self.assert_no_output()

    def test_unexpected_source_entries_are_rejected(self):
        """捕获阶段二输入或蓝区结果等额外条目进入归档。"""
        cases = {
            "extra-file": self.unexpected_file,
            "extra-directory": self.unexpected_directory,
            "nested-directory": self.unexpected_payload_directory,
            "payload-symlink": self.unexpected_payload_symlink,
            "absolute-symlink": self.unexpected_top_level_symlink,
            "non-regular-file": self.unexpected_fifo,
            "missing-loader-file": self.missing_loader_file,
        }

        for label, prepare in cases.items():
            with self.subTest(label=label):
                case = self.build_case(label, prepare)
                with self.assertRaises(ValueError):
                    packager.package_formal_input(case, self.workspace / ("out-" + label))
                self.assert_no_output(self.workspace / ("out-" + label))

    def build_case(self, label, prepare):
        """构造一个独立源树并施加单一非法条目。"""
        source = self.workspace / ("case-" + label)
        (source / "payloads").mkdir(parents=True)
        (source / "events.jsonl").write_bytes(b'{"event_id": "e1"}\n')
        (source / "truth.json").write_bytes(b'{"seed": 20260907}\n')
        (source / "generation-manifest.json").write_bytes(b'{"status": "complete"}\n')
        (source / "payloads" / "aa.json").write_bytes(b'{"payload": "aa"}')
        prepare(source)
        return source

    def unexpected_file(self, source):
        (source / "stage2-events.jsonl").write_bytes(b'{"event_id": "old"}\n')

    def unexpected_directory(self, source):
        (source / "blue-results").mkdir()
        (source / "blue-results" / "main.json").write_bytes(b"{}")

    def unexpected_payload_directory(self, source):
        (source / "payloads" / "nested").mkdir()

    def unexpected_payload_symlink(self, source):
        (source / "payloads" / "link.json").symlink_to(source / "truth.json")

    def unexpected_top_level_symlink(self, source):
        (source / "events-link.jsonl").symlink_to(source / "events.jsonl")

    def unexpected_fifo(self, source):
        os.mkfifo(source / "pipeline.fifo")

    def missing_loader_file(self, source):
        (source / "truth.json").unlink()

    def test_existing_targets_are_never_overwritten(self):
        """捕获覆盖既有归档或清单造成证据丢失。"""
        self.output.mkdir()
        archive_path = self.output / ARCHIVE_NAME
        archive_path.write_bytes(b"keep-archive")

        with self.assertRaises(FileExistsError):
            packager.package_formal_input(self.source, self.output)

        self.assertEqual(archive_path.read_bytes(), b"keep-archive")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [ARCHIVE_NAME])

        archive_path.unlink()
        checksum_path = self.output / CHECKSUM_NAME
        checksum_path.write_bytes(b"keep-checksum")

        with self.assertRaises(FileExistsError):
            packager.package_formal_input(self.source, self.output)

        self.assertEqual(checksum_path.read_bytes(), b"keep-checksum")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [CHECKSUM_NAME])

    def test_success_leaves_only_archive_and_checksum(self):
        """捕获临时文件残留或被重命名进输出目录。"""
        packager.package_formal_input(self.source, self.output)

        self.assertEqual(
            sorted(path.name for path in self.output.iterdir()),
            [ARCHIVE_NAME, CHECKSUM_NAME],
        )

    def test_output_inside_input_root_is_rejected(self):
        """捕获把归档写进冻结输入目录造成源身份被污染。"""
        before = self.inventory(self.source)
        for output in (self.source / "out", self.source):
            with self.assertRaises(ValueError):
                packager.package_formal_input(self.source, output)

        self.assertEqual(self.inventory(self.source), before)
        self.assertFalse((self.source / "out").exists())

    def test_checksum_publication_failure_leaves_no_commit_marker(self):
        """捕获清单发布失败后留下归档提交标记，或失败恢复删除最终输出路径。"""
        self.output.mkdir()
        archive_path = self.output / ARCHIVE_NAME
        checksum_path = self.output / CHECKSUM_NAME
        real_publish = packager._publish
        real_unlink = Path.unlink
        removed = []

        def failing_publish(temporary, final):
            """只让清单发布失败，归档发布保持原行为。"""
            if Path(final) == checksum_path:
                raise OSError("checksum publication failed")
            return real_publish(temporary, final)

        def recording_unlink(path, *args, **kwargs):
            """记录清理与失败恢复阶段删除的最终输出路径。"""
            if path in (archive_path, checksum_path):
                removed.append(path)
            return real_unlink(path, *args, **kwargs)

        with patch.object(packager, "_publish", failing_publish), \
                patch.object(Path, "unlink", recording_unlink):
            with self.assertRaisesRegex(OSError, "checksum publication failed"):
                packager.package_formal_input(self.source, self.output)

        self.assertEqual(removed, [])
        self.assertFalse(archive_path.exists())
        self.assertFalse(checksum_path.exists())
        self.assert_no_output()

    def test_interrupted_archive_publication_leaves_only_the_checksum(self):
        """捕获归档最终名发布中断时缺少可见清单、摘要不匹配或删除最终输出路径。"""
        self.output.mkdir()
        archive_path = self.output / ARCHIVE_NAME
        checksum_path = self.output / CHECKSUM_NAME
        real_publish = packager._publish
        real_sha256 = packager._sha256
        real_unlink = Path.unlink
        digests = []
        removed = []

        def recording_sha256(path):
            """记录本次归档的摘要，用于核对清单内容。"""
            digest = real_sha256(path)
            digests.append(digest)
            return digest

        def failing_publish(temporary, final):
            """只让归档最终名发布失败，清单发布保持原行为。"""
            if Path(final) == archive_path:
                raise OSError("archive publication failed")
            return real_publish(temporary, final)

        def recording_unlink(path, *args, **kwargs):
            """记录清理与失败恢复阶段删除的最终输出路径。"""
            if path in (archive_path, checksum_path):
                removed.append(path)
            return real_unlink(path, *args, **kwargs)

        with patch.object(packager, "_sha256", recording_sha256), \
                patch.object(packager, "_publish", failing_publish), \
                patch.object(Path, "unlink", recording_unlink):
            with self.assertRaisesRegex(OSError, "archive publication failed"):
                packager.package_formal_input(self.source, self.output)

        self.assertEqual(removed, [])
        self.assertFalse(archive_path.exists())
        self.assertEqual(
            checksum_path.read_bytes(), f"{digests[-1]}  {ARCHIVE_NAME}\n".encode(),
        )
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [CHECKSUM_NAME])

    def test_checksum_failure_does_not_remove_replaced_archive(self):
        """捕获清单发布失败时误删其他写入者已写入的归档最终名。"""
        self.output.mkdir()
        archive_path = self.output / ARCHIVE_NAME
        checksum_path = self.output / CHECKSUM_NAME
        real_publish = packager._publish

        def racing_publish(temporary, final):
            """清单发布失败前，模拟其他写入者写入自己的归档最终名。"""
            if Path(final) == checksum_path:
                if archive_path.exists():
                    archive_path.unlink()
                archive_path.write_bytes(b"replacement-archive")
                raise OSError("checksum publication failed after replacement")
            return real_publish(temporary, final)

        with patch.object(packager, "_publish", racing_publish):
            with self.assertRaisesRegex(OSError, "checksum publication failed after replacement"):
                packager.package_formal_input(self.source, self.output)

        self.assertEqual(archive_path.read_bytes(), b"replacement-archive")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [ARCHIVE_NAME])

    def test_replaced_final_path_in_the_ownership_window_is_not_removed(self):
        """捕获失败恢复删除归属检查与删除之间被并发写入者替换的最终名。"""
        self.output.mkdir()
        archive_path = self.output / ARCHIVE_NAME
        checksum_path = self.output / CHECKSUM_NAME
        replacement = self.workspace / "replacement-archive.tar.gz"
        replacement.write_bytes(b"replacement-archive")
        real_publish = packager._publish
        real_unlink = Path.unlink
        removed_finals = []

        def failing_publish(temporary, final):
            """只让清单发布失败，触发归档回滚。"""
            if Path(final) == checksum_path:
                raise OSError("checksum publication failed")
            return real_publish(temporary, final)

        def racing_unlink(path, *args, **kwargs):
            """删除最终名前模拟并发写入者已用同名文件完成替换。"""
            if path in (archive_path, checksum_path):
                os.rename(replacement, path)
                removed_finals.append(path)
            return real_unlink(path, *args, **kwargs)

        with patch.object(packager, "_publish", failing_publish), \
                patch.object(Path, "unlink", racing_unlink):
            with self.assertRaisesRegex(OSError, "checksum publication failed"):
                packager.package_formal_input(self.source, self.output)

        self.assertEqual(removed_finals, [])
        self.assertEqual(replacement.read_bytes(), b"replacement-archive")

    def test_publication_does_not_overwrite_a_racing_target(self):
        """捕获并发写入者在门禁之后创建最终名时被覆盖，或未留下可见的中断状态。"""
        self.output.mkdir()
        archive_path = self.output / ARCHIVE_NAME
        checksum_path = self.output / CHECKSUM_NAME
        real_write = packager._write_archive

        def racing_write(members, target):
            """在归档写入后模拟另一进程抢占最终名。"""
            real_write(members, target)
            archive_path.write_bytes(b"racing-archive")

        with patch.object(packager, "_write_archive", racing_write):
            with self.assertRaises(FileExistsError):
                packager.package_formal_input(self.source, self.output)

        self.assertEqual(archive_path.read_bytes(), b"racing-archive")
        self.assertEqual(sorted(path.name for path in self.output.iterdir()), [ARCHIVE_NAME, CHECKSUM_NAME])
        self.assertIsNotNone(
            re.fullmatch(rf"[0-9a-f]{{64}}  {re.escape(ARCHIVE_NAME)}\n", checksum_path.read_text()),
        )

    def test_write_failure_removes_temporary_files(self):
        """捕获写入中断后留下半成品归档、清单或临时文件。"""
        with patch.object(packager, "_write_archive", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                packager.package_formal_input(self.source, self.output)

        self.assert_no_output()

    def test_cli_packages_given_input_and_output(self):
        """捕获入口未绑定 --input/--output 或未生成两项产物。"""
        stream = io.StringIO()

        with redirect_stdout(stream):
            status = packager.main([
                "--input", str(self.source), "--output", str(self.output),
            ])

        self.assertEqual(status, 0)
        self.assertEqual(
            sorted(path.name for path in self.output.iterdir()),
            [ARCHIVE_NAME, CHECKSUM_NAME],
        )
        self.assertIn(ARCHIVE_NAME, stream.getvalue())
        self.assertEqual(self.calls, [self.source.resolve()])


if __name__ == "__main__":
    unittest.main()
