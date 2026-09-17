"""黄区交接正式文档的契约测试。"""

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ASSESSMENT = (
    REPOSITORY_ROOT
    / "docs/json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md"
)

# 评估文档必须覆盖的主题，按文档章节标题匹配。
REQUIRED_SECTIONS = (
    "范围与证据等级",
    "四种布局表示什么",
    "部分切片中的观测区分度",
    "合成数据的区分能力与边界",
    "Extension 方案对比",
    "最小后续实验集合",
    "结论边界与来源",
)

REQUIRED_TERMS = (
    "same_table",
    "separate",
    "full_core",
    "asset_ref",
    "db_lob_ref",
    "部分正式切片",
    "N+1",
)

# 把当前 Stage 3 或主矩阵描述为已完成、可发布的措辞。
FORBIDDEN_COMPLETION_CLAIMS = (
    "阶段三完成",
    "阶段三已完成",
    "主矩阵完成",
    "主矩阵已完成",
    "正式矩阵完成",
    "已通过完整 workload 门禁",
    "完整正式结论",
    "可以发布",
)

# "完成" 只能用于描述操作、门禁、水位、block 或样本，或用于否定整体完成状态。
COMPLETION_SCOPES = (
    "操作",
    "门禁",
    "水位",
    "block",
    "样本",
    "写入完成条件",
    "尚未",
    "未完成",
    "待",
)

MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# 批量恢复的数值必须与产生它的字段标注在同一行，避免把恢复总耗时写成 resolver 读取。
BATCH_METRIC_ATTRIBUTION = (
    ("1,984.5", "`recovery_ms`"),
    ("1,284.1", "`recovery_ms`"),
    ("384 ms", "`resolver.read_ms`"),
    ("392 ms", "`resolver.read_ms`"),
)


class AssessmentDocumentContractTest(unittest.TestCase):
    """验证代表性评估文档存在，并满足交接受众所需的契约。"""

    def read_document(self):
        """读取评估文档文本；缺失时直接失败。"""
        self.assertTrue(
            ASSESSMENT.is_file(), f"缺少代表性评估文档：{ASSESSMENT}",
        )
        return ASSESSMENT.read_text(encoding="utf-8")

    def test_assessment_document_exists(self):
        """评估文档位于仓库内固定路径。"""
        self.read_document()

    def test_document_covers_required_sections(self):
        """评估覆盖范围、布局、区分度、数据边界、extension、后续实验和结论边界。"""
        content = self.read_document()
        for section in REQUIRED_SECTIONS:
            self.assertIn(section, content, f"评估缺少章节：{section}")

    def test_document_uses_required_terms(self):
        """评估使用固定的布局名、切片标签和检索路径术语。"""
        content = self.read_document()
        for term in REQUIRED_TERMS:
            self.assertIn(term, content, f"评估缺少术语：{term}")

    def test_document_rejects_completion_claims(self):
        """评估不得把当前切片或 Stage 3 描述为已完成。"""
        content = self.read_document()
        for claim in FORBIDDEN_COMPLETION_CLAIMS:
            self.assertNotIn(claim, content, f"评估出现完成声明：{claim}")

    def test_completion_wording_stays_scoped(self):
        """评估只在操作、门禁、水位、block、样本或否定整体状态时使用“完成”。"""
        content = self.read_document()
        for number, line in enumerate(content.splitlines(), start=1):
            if "完成" not in line:
                continue
            self.assertTrue(
                any(scope in line for scope in COMPLETION_SCOPES),
                f"第 {number} 行的“完成”用法未限定范围：{line.strip()}",
            )

    def test_relative_links_resolve(self):
        """评估中的每个相对 Markdown 链接都指向仓库内存在的路径。"""
        content = self.read_document()
        targets = [
            match.group(1).strip()
            for match in MARKDOWN_LINK.finditer(content)
        ]
        self.assertTrue(targets, "评估缺少 Markdown 链接")
        for target in targets:
            if "://" in target or target.startswith("#") or target.startswith("mailto:"):
                continue
            path = target.split("#", 1)[0]
            resolved = (ASSESSMENT.parent / path).resolve()
            self.assertTrue(
                resolved.is_relative_to(REPOSITORY_ROOT),
                f"链接指向仓库外路径：{target}",
            )
            self.assertTrue(resolved.exists(), f"链接目标不存在：{target}")

    def test_batch_metrics_keep_their_own_fields(self):
        """批量恢复总耗时标注为 recovery_ms，逐行读取标注为 resolver.read_ms。"""
        content = self.read_document()
        for number, field in BATCH_METRIC_ATTRIBUTION:
            lines = [line for line in content.splitlines() if number in line]
            self.assertTrue(lines, f"评估缺少批量恢复数值：{number}")
            for line in lines:
                self.assertIn(
                    field, line, f"{number} 未标注为 {field}：{line.strip()}",
                )


if __name__ == "__main__":
    unittest.main()
