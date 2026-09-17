"""确定性打包阶段三正式输入，生成可校验的 tar.gz 归档与 SHA-256 清单。

两项产物都以硬链接原子发布，已存在的最终名不会被覆盖。由本工具发布的归档最终名标记一对完整产物：
先发布清单，最后发布归档；发布中断只可能留下清单，该状态可见且会阻止后续自动覆盖。
边界：输入源在门禁校验与归档写入之间被修改时，归档记录的是写入时刻读到的字节，本工具不锁定源目录，
因此不消除该竞态。外来写入者在发布窗口内抢先写入归档最终名时，本次清单已经落盘，两者可以共存；
摘要不匹配会暴露该状态，归档发布同时抛出错误。
"""

import argparse
import gzip
import hashlib
import os
import sys
import tarfile
import tempfile
from pathlib import Path


RUNNER_DIR = Path(__file__).resolve().parents[1] / "runner"
if str(RUNNER_DIR) not in sys.path:
    sys.path.insert(0, str(RUNNER_DIR))

import production


ARCHIVE_NAME = "json-storage-stage3-formal-input-20260917.tar.gz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
ARCHIVE_ROOT = "json-storage-stage3-formal-input"
REQUIRED_FILES = ("events.jsonl", "generation-manifest.json", "truth.json")
PAYLOAD_DIR = "payloads"
# 源目录中由 generate-input 写入的包装元数据：保留在冻结源，不进入归档。
SOURCE_ONLY_FILES = ("run-manifest.json",)
DIRECTORY_MODE = 0o755
FILE_MODE = 0o644
_CHUNK_SIZE = 1024 * 1024


def _require_regular_file(path: Path, label: str):
    """拒绝符号链接与非常规文件，保证归档只包含普通文件。"""
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular file")


def _payload_files(payload_root: Path):
    """校验 payload 目录只含普通文件并返回按名排序的列表。"""
    if payload_root.is_symlink() or not payload_root.is_dir():
        raise ValueError(f"{PAYLOAD_DIR} must be a regular directory")
    payloads = sorted(payload_root.iterdir(), key=lambda path: path.name)
    for payload in payloads:
        _require_regular_file(payload, f"{PAYLOAD_DIR}/{payload.name}")
    return payloads


def _archive_members(root: Path):
    """校验冻结源目录结构，返回按成员名排序的归档成员。"""
    seen = set()
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.is_symlink():
            raise ValueError(f"source entry must be a regular file or directory: {entry.name}")
        if entry.is_dir():
            if entry.name != PAYLOAD_DIR:
                raise ValueError(f"unexpected source directory: {entry.name}")
        else:
            _require_regular_file(entry, entry.name)
            if entry.name not in REQUIRED_FILES + SOURCE_ONLY_FILES:
                raise ValueError(f"unexpected source file: {entry.name}")
        seen.add(entry.name)
    for name in REQUIRED_FILES:
        if name not in seen:
            raise ValueError(f"missing required source file: {name}")
    if PAYLOAD_DIR not in seen:
        raise ValueError(f"missing required source directory: {PAYLOAD_DIR}")

    payload_root = root / PAYLOAD_DIR
    payloads = _payload_files(payload_root)
    members = [(ARCHIVE_ROOT + "/", root, True)]
    for name in REQUIRED_FILES:
        members.append((f"{ARCHIVE_ROOT}/{name}", root / name, False))
    members.append((f"{ARCHIVE_ROOT}/{PAYLOAD_DIR}/", payload_root, True))
    for payload in payloads:
        members.append((f"{ARCHIVE_ROOT}/{PAYLOAD_DIR}/{payload.name}", payload, False))
    return sorted(members, key=lambda member: member[0])


def _normalized_info(name: str, mode: int, is_dir: bool, size: int = 0):
    """构造固定 owner、group、时间戳与权限位的 tar 成员元数据。"""
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
    info.mode = mode
    info.size = size
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    return info


def _write_archive(members, target: Path):
    """以零 mtime、无文件名的 gzip 头写入确定性归档。"""
    with target.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT,
            ) as archive:
                for name, source, is_dir in members:
                    if is_dir:
                        archive.addfile(_normalized_info(name, DIRECTORY_MODE, True))
                        continue
                    info = _normalized_info(name, FILE_MODE, False, source.stat().st_size)
                    with source.open("rb") as handle:
                        archive.addfile(info, handle)


def _sha256(path: Path) -> str:
    """分块计算文件 SHA-256，避免把大归档整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_file(output: Path) -> tuple[int, Path]:
    """在输出目录内创建临时文件，保证硬链接发布不跨文件系统。"""
    descriptor, name = tempfile.mkstemp(dir=output, prefix=".package-formal-input-")
    return descriptor, Path(name)


def _publish(temporary: Path, final: Path):
    """以硬链接发布最终名；目标已存在时抛 FileExistsError 且不覆盖。"""
    try:
        os.link(temporary, final)
    except FileExistsError:
        raise FileExistsError(f"refusing to overwrite {final}") from None


def package_formal_input(input_root: Path, output_dir: Path) -> tuple[Path, Path]:
    """Validate and package the frozen Stage 3 input reproducibly."""
    root = Path(input_root).resolve()
    output = Path(output_dir).resolve()
    if output == root or output.is_relative_to(root):
        raise ValueError(f"output directory must be outside the input root: {output}")
    production.load_formal_input(root)
    members = _archive_members(root)

    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / ARCHIVE_NAME
    checksum_path = output / CHECKSUM_NAME
    for target in (archive_path, checksum_path):
        if target.exists():
            raise FileExistsError(f"refusing to overwrite {target}")

    temporary = []
    try:
        for _ in range(2):
            descriptor, path = _temporary_file(output)
            os.close(descriptor)
            temporary.append(path)
        archive_temp, checksum_temp = temporary
        _write_archive(members, archive_temp)
        digest = _sha256(archive_temp)
        with checksum_temp.open("wb") as handle:
            handle.write(f"{digest}  {ARCHIVE_NAME}\n".encode("ascii"))
        # 两项产物都落盘后再发布。归档最终名是本工具发布的提交标记：先发布清单，最后发布归档，
        # 因此本工具的归档存在即表示两项产物都已发布完整，中断只留下清单。失败恢复不删除任何
        # 最终名，避免删除并发写入者在发布窗口内写入的文件；外来归档与本次清单共存时，
        # 摘要不匹配会暴露该状态，归档发布同时抛出错误。
        _publish(checksum_temp, checksum_path)
        _publish(archive_temp, archive_path)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return archive_path, checksum_path


def main(argv=None) -> int:
    """解析 CLI 参数，打包正式输入并打印两项产物路径。"""
    parser = argparse.ArgumentParser(
        description="Package the frozen Stage 3 formal input reproducibly.",
    )
    parser.add_argument("--input", required=True, type=Path, help="frozen input root")
    parser.add_argument("--output", required=True, type=Path, help="archive output directory")
    arguments = parser.parse_args(argv)
    archive_path, checksum_path = package_formal_input(arguments.input, arguments.output)
    print(archive_path)
    print(checksum_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
