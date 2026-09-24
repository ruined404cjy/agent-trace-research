import re
import unittest
from pathlib import Path


CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "docs/project-background/json-storage-stage3-yellow-feedback-contract.md"
)
GUIDE = (
    Path(__file__).resolve().parents[3]
    / "docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md"
)


class FeedbackContractTest(unittest.TestCase):
    """反馈契约必须固定此前被不同轮次读出不同含义的口径。"""

    @classmethod
    def setUpClass(cls):
        cls.text = CONTRACT.read_text(encoding="utf-8")

    def test_guide_points_at_the_contract(self):
        """指南必须引用契约，否则执行方找不到回传口径。"""
        self.assertIn(CONTRACT.name, GUIDE.read_text(encoding="utf-8"))

    def test_write_total_and_block_median_are_defined_apart(self):
        """写入合计与单块 p50 相差约 190 倍，两者的来源字段必须分别写明。

        历史回传把两者混用过，混用时写入结论整体失真。
        """
        for term in (
            "190 个 block 的 `ingest.wall_ms` 之和",
            "`block_wall_ms.median`",
        ):
            self.assertIn(term, self.text, f"缺少写入口径定义：{term}")

    def test_percentile_states_the_aggregation_order(self):
        """p50 必须写明先轮内取中位数、再对四轮取中位数，并排除合并样本的算法。"""
        self.assertIn("先在轮内", self.text)
        self.assertIn("再对四轮的轮内中位数取中位数", self.text)
        self.assertIn("不要先把四轮样本合并再取中位数", self.text)

    def test_space_denominator_is_the_workload_payload(self):
        """空间比值的分母是该 workload 自己的载荷字节，不是三个 cohort 之和。"""
        self.assertIn("128,450,560", self.text)
        self.assertIn("不要用三者之和 296,223,744 作分母", self.text)
        self.assertIn("分列两个数", self.text, "asset_ref 的库内与对象存储空间不得相加")

    def test_baseline_alignment_preserves_local_work_without_stash(self):
        """本地改动是解释旧数据的唯一材料，必须固化为可找回的提交而不是 stash。"""
        self.assertIn("不要使用 `git stash`", self.text)
        self.assertIn("local-before-unification", self.text)
        self.assertIn("git reset --hard origin/stage3/xstore-yellow-handoff", self.text)

    def test_baseline_requires_identical_runner_digests(self):
        """两台必须证明运行的是同一份代码，否则结果仍然不可合并。"""
        self.assertIn("两台的同名文件必须逐项相同", self.text)
        self.assertIn("sha256sum", self.text)

    def test_engine_order_and_isolation_are_fixed(self):
        """两台使用同一执行顺序，且运行一侧时停止另一侧，消除内存与缓存干扰。"""
        self.assertIn("XStore 阶段为主矩阵 → 四布局混合负载 → Asset 故障 → 行存探针", self.text)
        self.assertIn("ClickHouse 阶段为\n主矩阵 → 四布局 part 状态 → 四布局混合负载 → Asset 故障", self.text)
        self.assertIn("停止另一个引擎的服务", self.text)
        self.assertIn("每次切换引擎之前重做一遍", self.text)

    def test_interference_rerun_states_the_per_stream_process_harness(self):
        """同进程多线程的请求流会把客户端 GIL 争用计入前台时延，重跑范围必须写明每流独立进程。"""
        row = next(
            line for line in self.text.splitlines() if line.startswith("| 混合负载 |")
        )
        self.assertIn("每个请求流在独立进程内运行", row)
        self.assertIn("`stream_execution`", row)
        self.assertIn("`process_per_stream`", row)

    def test_retained_paths_cover_every_evidence_directory(self):
        """回传确认之前不得删除的路径必须覆盖四类证据目录。"""
        for path in ("access-gate", "handback", "ch-part-states", "asset-failures"):
            self.assertIn(path, self.text, f"保留路径缺少 {path}")

    def test_handback_constrains_order_rather_than_line_count(self):
        """回传只约束项内字段顺序，不约束总行数。

        限制总行数会让执行方为凑数拆分或合并数据单元，回传方反而要重新拼装。
        """
        self.assertIn("回传不限制总行数", self.text)
        self.assertIn("一个数据单元占一行", self.text)
        self.assertIn("不需要写表头", self.text)
        self.assertNotIn("回传总行数为", self.text)

    def test_every_handback_item_declares_its_field_order(self):
        """每个回传项都要给出分行方式与行内字段顺序，否则顺序约定无法执行。"""
        rows = re.findall(r"^\| ([A-C]\d+) \| ([^|]+)\| ([^|]+)\|$", self.text, re.MULTILINE)
        self.assertGreaterEqual(len(rows), 16, "回传格式表缺项")
        for code, split, fields in rows:
            self.assertTrue(split.strip(), f"{code} 缺少分行方式")
            self.assertTrue(fields.strip(), f"{code} 缺少行内字段顺序")


if __name__ == "__main__":
    unittest.main()
