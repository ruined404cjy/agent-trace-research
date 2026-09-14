"""阶段二调优矩阵的固定契约和阶段二共享函数入口。"""

import copy
import importlib.util
from pathlib import Path


CONTRACT_VERSION = "json-storage-tuned-matrix-v2"
FOUR_LAYOUT_V1_CONTRACT_VERSION = "json-storage-four-layout-v1"
LAYOUTS = ("og_json", "og_jsonb", "ch_string", "ch_native")
ROUND_ORDERS = (
    ("og_json", "og_jsonb", "ch_string", "ch_native"),
    ("og_jsonb", "ch_native", "og_json", "ch_string"),
    ("ch_string", "og_json", "ch_native", "og_jsonb"),
    ("ch_native", "ch_string", "og_jsonb", "og_json"),
)
QUERY_IDS = ("S01", "S02", "S03", "S04", "S05", "S06", "S07")
FOUR_LAYOUT_V1_QUERY_IDS = QUERY_IDS[:6]
EXPECTED_RESULT_SHA256 = {
    "S01": "11b66e4cd5d0b2fd8310af52da1cdb9c76aeacada8d8fdbf821433aa2d1ac47c",
    "S02": "868ef72164c999ac75e4334700aeb1d3e1c12a7bd1e4ff9e4472a4ec1bb1c37e",
    "S03": "db0e00e0373f7d7a4fc16b3908824f6aa6be797369e2b8e0c040af9e8d40c69c",
    "S04": "ae94332c2d8a203e55c5d00f6baeda1202ff893d815923ab61f6fd40adc631a9",
    "S05": "c633ffe4f9651092325cca5dced8eb59bf581b74af158b69cc5ac2d3539c65ad",
    "S06": "77943c40f36f74eed18263ed10d8122ce990eb1332d8de9171cd734763e5e95b",
    "S07": "08d14304ff3cac9ca3c7ce7a07022d78bf4bdf643304bb87c1179b8c9042ad4a",
}
EXPECTED_ROW_COUNTS = {"S01": 3, "S02": 1, "S03": 741, "S04": 4277, "S05": 6, "S06": 256, "S07": 1}
FOUR_LAYOUT_V1_EXPECTED_RESULT_SHA256 = {
    **{query_id: EXPECTED_RESULT_SHA256[query_id] for query_id in QUERY_IDS[:4]},
    "S05": "cdd66eba4ca33847acf97d3cd89e65d42f3f488c8cb16cc46c926b444b65b167",
    "S06": "a1fc89802da0ccf1d5b7bc349b4b7b69b39bce93ec08325bc8ecbadd28388b3d",
}
FOUR_LAYOUT_V1_EXPECTED_ROW_COUNTS = {
    query_id: EXPECTED_ROW_COUNTS[query_id] for query_id in FOUR_LAYOUT_V1_QUERY_IDS
}


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


def derived_attributes(row):
    """返回加入数值聚合控制路径的分析属性副本。"""
    if not isinstance(row, dict) or not isinstance(row.get("attributes_analysis"), dict):
        raise ValueError("row must contain attributes_analysis")
    duration_ms = row.get("duration_ms")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise ValueError("duration_ms must be an integer")
    attributes = copy.deepcopy(row["attributes_analysis"])
    experiment = attributes.setdefault("experiment", {})
    if not isinstance(experiment, dict) or "duration_ms" in experiment:
        raise ValueError("experiment.duration_ms collides with source attributes")
    experiment["duration_ms"] = duration_ms
    return attributes
