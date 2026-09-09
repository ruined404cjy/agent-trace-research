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
EXPECTED_RESULT_SHA256 = {
    "S01": "11b66e4cd5d0b2fd8310af52da1cdb9c76aeacada8d8fdbf821433aa2d1ac47c",
    "S02": "868ef72164c999ac75e4334700aeb1d3e1c12a7bd1e4ff9e4472a4ec1bb1c37e",
    "S03": "db0e00e0373f7d7a4fc16b3908824f6aa6be797369e2b8e0c040af9e8d40c69c",
    "S04": "ae94332c2d8a203e55c5d00f6baeda1202ff893d815923ab61f6fd40adc631a9",
    "S05": "cdd66eba4ca33847acf97d3cd89e65d42f3f488c8cb16cc46c926b444b65b167",
    "S06": "a1fc89802da0ccf1d5b7bc349b4b7b69b39bce93ec08325bc8ecbadd28388b3d",
}
EXPECTED_ROW_COUNTS = {"S01": 3, "S02": 1, "S03": 741, "S04": 4277, "S05": 6, "S06": 256}


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
write_failed_manifest = _stage_two_common.write_failed_manifest
write_atomically = _stage_two_common.write_atomically
