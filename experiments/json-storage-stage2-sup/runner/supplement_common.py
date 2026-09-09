"""阶段二补充实验的固定契约和阶段二共享函数入口。"""

import importlib.util
from pathlib import Path


CONTRACT_VERSION = "json-storage-four-layout-v1"
LAYOUTS = ("og_json", "og_jsonb", "ch_string", "ch_native")
ROUND_ORDERS = (
    ("og_json", "og_jsonb", "ch_string", "ch_native"),
    ("og_jsonb", "ch_native", "og_json", "ch_string"),
    ("ch_string", "og_json", "ch_native", "og_jsonb"),
    ("ch_native", "ch_string", "og_jsonb", "og_json"),
)
QUERY_IDS = ("S01", "S02", "S03", "S04", "S05", "S06")


def _load_stage_two_common():
    """从阶段二 runner 的固定文件路径加载公共实现。"""
    common_path = Path(__file__).resolve().parents[2] / "json-storage-stage2" / "runner" / "common.py"
    specification = importlib.util.spec_from_file_location("json_storage_stage2_common", common_path)
    if specification is None or specification.loader is None:
        raise ImportError(f"cannot load stage two common module: {common_path}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


_stage_two_common = _load_stage_two_common()

# 复用阶段二确定性编码、输入验证与原子发布，不复制实现。
canonical_bytes = _stage_two_common.canonical_bytes
file_identity = _stage_two_common.file_identity
nearest_rank = _stage_two_common.nearest_rank
read_json = _stage_two_common.read_json
verify_input = _stage_two_common.verify_input
write_manifest_last = _stage_two_common.write_manifest_last
write_atomically = _stage_two_common.write_atomically
