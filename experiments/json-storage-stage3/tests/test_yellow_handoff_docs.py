"""黄区交接正式文档的契约测试。"""

import json
import re
import subprocess
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

# 黄区交接指南、文档索引与指南内清单契约。
GUIDE = (
    REPOSITORY_ROOT
    / "docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md"
)
DOCS_INDEX = REPOSITORY_ROOT / "docs/README.md"
HANDOFF_DESIGN = (
    REPOSITORY_ROOT
    / "docs/superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md"
)
HANDOFF_PLAN = (
    REPOSITORY_ROOT
    / "docs/superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md"
)
INDEX_LINK_TARGETS = (
    "project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md",
    "json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md",
    "superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md",
    "superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md",
)

HANDOFF_BRANCH = "stage3/xstore-yellow-handoff"
HANDOFF_BASE_COMMIT = "93ebf2319ae7cb60b1f68eb53b3562d26f80f443"
HANDOFF_BASE_COMMIT_SHORT = "93ebf23"
SSH_CLONE_URL = "git@github.com:ruined404cjy/agent-trace-research.git"
PUBLIC_CLONE_URL = "https://github.com/ruined404cjy/agent-trace-research.git"
ARCHIVE_NAME = "json-storage-stage3-formal-input-20260917.tar.gz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
PACKAGED_ARCHIVE_SHA256 = (
    "47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f"
)
PACKAGED_ARCHIVE_BYTES = "19,313,037"

CLICKHOUSE_VERSION = "25.12.11.4"
CLICKHOUSE_RELEASE_TAG = "v25.12.11.4-stable"
CLICKHOUSE_RELEASE_TAG_URL = (
    "https://github.com/ClickHouse/ClickHouse/releases/tag/v25.12.11.4-stable"
)
CLICKHOUSE_RELEASE_DOWNLOAD = (
    "https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/"
)
CLICKHOUSE_ARM64_ARCHIVES = (
    "clickhouse-common-static-25.12.11.4-arm64.tgz",
    "clickhouse-server-25.12.11.4-arm64.tgz",
    "clickhouse-client-25.12.11.4-arm64.tgz",
)
CLICKHOUSE_AARCH64_RPMS = (
    "clickhouse-common-static-25.12.11.4.aarch64.rpm",
    "clickhouse-server-25.12.11.4.aarch64.rpm",
    "clickhouse-client-25.12.11.4.aarch64.rpm",
)
CLICKHOUSE_TGZ_BINARY = "clickhouse-common-static-25.12.11.4/usr/bin/clickhouse"

# 指南必须逐项覆盖 LayoutAdapter 的方法，用于把 adapter 实施约束到既有契约。
LAYOUT_ADAPTER_METHODS = (
    "create", "ingest_block", "ingest_failure_evidence", "wait_write_complete",
    "wait_query_ready", "get_available", "run_query", "collect_storage",
    "collect_access_evidence", "audit_dataset", "cleanup",
)

# 能力报告的机器可读 schema 必须覆盖的字段。
CAPABILITY_FIELDS = (
    "sql_driver", "ddl_dml", "json_lob_types", "transaction", "extension_support",
    "query_statistics", "storage_accounting", "background_work", "cleanup",
    "cache_control",
)

ENVIRONMENT_PROBE_COMMANDS = (
    "uname -m", "/etc/os-release", "nproc", "free", "df -T", "findmnt",
    "timedatectl", "ulimit -a", "sysctl", "ss -ltn", "ps -ef", "systemctl",
    "id -u", "sudo -n true",
)

EVIDENCE_TERMS = (
    "run-manifest.json", "QueryFinish", "natural_stable_parts", "optimize_final",
    "response_bytes", "scanned_bytes", "diagnostic", "SHA-256",
)

SERIAL_AND_CACHE_TERMS = ("串行", "cache_state", "cold", "warm")

# 指南只能描述本地已完成事实，发布动作在未执行前不得写成结果。
FORBIDDEN_PUBLICATION_CLAIMS = (
    "已推送", "已经推送", "已发布", "已经发布", "已上传", "已经上传",
    "Release 已创建", "已创建 Release",
)

STOP_CONDITION_MARKERS = ("停止", "git ls-remote", "可用性")

# shell 片段检查依赖指南标记的变量声明块，删除命令必须带路径守卫。
PREAMBLE_MARKER = "<!-- shell-preamble -->"
FENCE_OPEN = re.compile(r"^(?P<fence>(?:`{3,}|~{3,}))\s*(?P<info>[^\n]*?)\s*$")
DELETION_COMMAND = re.compile(r"\brm\s+-[A-Za-z]*[rRf]")
DELETION_GUARD = "require_guide_path"


def extract_code_fences(text):
    """按行扫描 Markdown 围栏，返回 info、正文、起始行号和前置变量标记。"""
    lines = text.splitlines()
    fences = []
    index = 0
    while index < len(lines):
        match = FENCE_OPEN.match(lines[index])
        if match is None:
            index += 1
            continue
        fence = match.group("fence")
        body = []
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].strip() != fence:
            body.append(lines[cursor])
            cursor += 1
        marker = lines[index - 1].strip() if index else ""
        fences.append({
            "info": match.group("info"),
            "body": "\n".join(body),
            "line": index + 1,
            "closed": cursor < len(lines),
            "preamble": marker == PREAMBLE_MARKER,
        })
        index = cursor + 1
    return fences


def read_guide_text(case):
    """读取黄区指南文本；缺失时直接使当前测试失败。"""
    case.assertTrue(GUIDE.is_file(), f"缺少黄区指南：{GUIDE}")
    return GUIDE.read_text(encoding="utf-8")


def shell_preamble(fences):
    """拼接指南标记的变量声明块，作为 shell 语法检查的前置脚本。"""
    return "\n".join(fence["body"] for fence in fences if fence["preamble"])


def assert_shell_syntax(case, script, label):
    """用 bash -n 检查脚本文本，不做任何执行。"""
    completed = subprocess.run(
        ["bash", "-n"], input=script, text=True, capture_output=True, check=False,
    )
    case.assertEqual(
        0, completed.returncode,
        f"{label} 未通过 bash -n：{completed.stderr.strip()}",
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


class YellowGuideContractTest(unittest.TestCase):
    """验证黄区 XStore/ClickHouse 指南与文档索引满足交接契约。"""

    def read_guide(self):
        """读取黄区指南文本；缺失时直接失败。"""
        return read_guide_text(self)

    def read_index(self):
        """读取文档索引文本；缺失时直接失败。"""
        self.assertTrue(DOCS_INDEX.is_file(), f"缺少文档索引：{DOCS_INDEX}")
        return DOCS_INDEX.read_text(encoding="utf-8")

    def test_guide_document_exists(self):
        """黄区指南位于仓库内固定路径。"""
        self.read_guide()

    def test_index_links_every_handoff_document(self):
        """索引包含评估、指南、交接设计与实施计划，且链接目标存在。"""
        index = self.read_index()
        for target in INDEX_LINK_TARGETS:
            self.assertIn(f"]({target})", index, f"索引缺少链接：{target}")
            self.assertTrue(
                (DOCS_INDEX.parent / target).exists(), f"索引链接目标不存在：{target}",
            )

    def test_guide_records_handoff_identities(self):
        """指南固定仓库地址、分支、基线提交和冻结输入身份。"""
        content = self.read_guide()
        for term in (
            HANDOFF_BRANCH, HANDOFF_BASE_COMMIT, HANDOFF_BASE_COMMIT_SHORT,
            SSH_CLONE_URL, PUBLIC_CLONE_URL, ARCHIVE_NAME, CHECKSUM_NAME,
            PACKAGED_ARCHIVE_SHA256, PACKAGED_ARCHIVE_BYTES,
        ):
            self.assertIn(term, content, f"指南缺少交接身份：{term}")

    def test_guide_stops_when_branch_or_release_is_unavailable(self):
        """指南要求先做可用性检查，且不把推送或发布写成已完成事实。"""
        content = self.read_guide()
        for marker in STOP_CONDITION_MARKERS:
            self.assertIn(marker, content, f"指南缺少停止条件标记：{marker}")
        for claim in FORBIDDEN_PUBLICATION_CLAIMS:
            self.assertNotIn(claim, content, f"指南出现未执行的发布声明：{claim}")

    def test_guide_states_platform_and_no_docker_assumption(self):
        """指南固定在 EulerOS 2.13 ARM64 上执行，并排除 Docker 依赖。"""
        content = self.read_guide()
        for term in ("EulerOS 2.13", "aarch64", "uname -m", "Docker"):
            self.assertIn(term, content, f"指南缺少平台约束：{term}")

    def test_guide_probes_required_environment_signals(self):
        """指南要求探测架构、系统、资源、限制、端口、XStore 和权限。"""
        content = self.read_guide()
        for command in ENVIRONMENT_PROBE_COMMANDS:
            self.assertIn(command, content, f"指南缺少环境探测命令：{command}")

    def test_guide_pins_clickhouse_release_and_packages(self):
        """指南固定 ClickHouse 版本号、官方 Release 入口和 ARM64 包名。"""
        content = self.read_guide()
        for term in (
            CLICKHOUSE_VERSION, CLICKHOUSE_RELEASE_TAG, CLICKHOUSE_RELEASE_TAG_URL,
            CLICKHOUSE_RELEASE_DOWNLOAD,
        ):
            self.assertIn(term, content, f"指南缺少 ClickHouse 版本或入口：{term}")
        for name in CLICKHOUSE_ARM64_ARCHIVES + CLICKHOUSE_AARCH64_RPMS:
            self.assertIn(name, content, f"指南缺少官方 ARM64 包：{name}")

    def test_guide_verifies_packages_and_frozen_input(self):
        """指南要求 SHA-512 校验安装包、SHA-256 校验冻结输入。"""
        content = self.read_guide()
        for term in ("sha512sum -c", "sha256sum -c", "load_formal_input", "identity_sha256"):
            self.assertIn(term, content, f"指南缺少校验命令或字段：{term}")
        for name in CLICKHOUSE_ARM64_ARCHIVES:
            self.assertIn(f"{name}.sha512", content, f"指南缺少包摘要文件：{name}.sha512")

    def test_guide_documents_both_install_paths(self):
        """指南同时给出 root RPM 路径和无特权 TGZ 路径。"""
        content = self.read_guide()
        for term in ("dnf install", "tar -xzf", CLICKHOUSE_TGZ_BINARY, "--config-file"):
            self.assertIn(term, content, f"指南缺少安装路径命令：{term}")

    def test_guide_binds_clickhouse_to_loopback_and_checks_health(self):
        """指南把 ClickHouse 限制在回环地址，并给出健康与版本检查。"""
        content = self.read_guide()
        for term in (
            "<listen_host>127.0.0.1</listen_host>", "18123", "19000",
            "SELECT version()", "curl", "ss -ltn",
        ):
            self.assertIn(term, content, f"指南缺少回环或健康检查：{term}")

    def test_guide_requires_capability_report_before_adapter(self):
        """指南要求能力报告先于 adapter 实施，并给出机器可读 schema。"""
        content = self.read_guide()
        self.assertIn("capability-report.json", content, "指南缺少能力报告文件名")
        schemas = [
            fence["body"] for fence in extract_code_fences(content)
            if fence["info"].split()[:1] == ["json"]
        ]
        self.assertTrue(schemas, "指南缺少能力报告 JSON schema 代码块")
        parsed = []
        for body in schemas:
            try:
                parsed.append(json.loads(body))
            except json.JSONDecodeError:
                continue
        self.assertTrue(parsed, "指南的 JSON schema 代码块无法解析")
        self.assertTrue(
            any(
                isinstance(schema, dict)
                and set(schema) >= set(CAPABILITY_FIELDS)
                for schema in parsed
            ),
            "指南的 JSON schema 未覆盖全部能力报告字段",
        )

    def test_guide_maps_every_layout_adapter_method(self):
        """指南逐项覆盖 LayoutAdapter 的方法契约。"""
        content = self.read_guide()
        self.assertIn("LayoutAdapter", content, "指南缺少 LayoutAdapter 契约引用")
        for method in LAYOUT_ADAPTER_METHODS:
            self.assertIn(f"{method}(", content, f"指南缺少 adapter 方法：{method}")

    def test_guide_keeps_asset_ref_application_side(self):
        """指南保留应用侧 asset_ref，并把 extension 承载布局记为 db_lob_ref。"""
        content = self.read_guide()
        for term in ("asset_ref", "db_lob_ref", "应用侧"):
            self.assertIn(term, content, f"指南缺少布局边界术语：{term}")

    def test_guide_records_evidence_gates_and_run_classes(self):
        """指南要求运行清单、真值、访问路径、维护状态和运行分类证据。"""
        content = self.read_guide()
        for term in EVIDENCE_TERMS + ("complete", "invalid"):
            self.assertIn(term, content, f"指南缺少证据或运行分类项：{term}")

    def test_guide_records_serial_execution_and_cache_labels(self):
        """指南要求同机串行执行，并标注冷热缓存状态。"""
        content = self.read_guide()
        for term in SERIAL_AND_CACHE_TERMS + ("warm-reused-connections-no-os-cache-drop",):
            self.assertIn(term, content, f"指南缺少串行或缓存标签：{term}")

    def test_guide_has_forwardable_prompt_citing_the_guide(self):
        """指南包含一段引用本指南的简洁转发 prompt。"""
        content = self.read_guide()
        prompts = [
            fence["body"] for fence in extract_code_fences(content)
            if GUIDE.name in fence["body"]
        ]
        self.assertTrue(prompts, "指南缺少引用本指南的转发 prompt")
        self.assertTrue(
            any(len(prompt) <= 2500 for prompt in prompts),
            "转发 prompt 必须保持简洁",
        )

    def test_guide_relative_links_resolve(self):
        """指南中的每个相对 Markdown 链接都指向仓库内存在的路径。"""
        content = self.read_guide()
        targets = [
            match.group(1).strip() for match in MARKDOWN_LINK.finditer(content)
        ]
        self.assertTrue(targets, "指南缺少 Markdown 链接")
        for target in targets:
            if "://" in target or target.startswith("#") or target.startswith("mailto:"):
                continue
            path = target.split("#", 1)[0]
            resolved = (GUIDE.parent / path).resolve()
            self.assertTrue(
                resolved.is_relative_to(REPOSITORY_ROOT),
                f"链接指向仓库外路径：{target}",
            )
            self.assertTrue(resolved.exists(), f"链接目标不存在：{target}")

    def test_guide_shell_fences_are_syntactically_valid(self):
        """在指南变量声明块下，每个 bash 片段都通过 bash -n。"""
        fences = extract_code_fences(self.read_guide())
        preamble = shell_preamble(fences)
        self.assertTrue(preamble.strip(), "指南缺少带标记的变量声明块")
        assert_shell_syntax(self, preamble, "前置变量块")
        bash_fences = [
            fence for fence in fences if fence["info"].split()[:1] == ["bash"]
        ]
        self.assertTrue(bash_fences, "指南缺少 bash 片段")
        for fence in bash_fences:
            self.assertTrue(fence["closed"], f"第 {fence['line']} 行的围栏未闭合")
            assert_shell_syntax(
                self, preamble + "\n" + fence["body"], f"第 {fence['line']} 行 bash 片段",
            )

    def test_guide_deletion_commands_are_guarded(self):
        """删除命令限制在指南创建的目录内，并带路径守卫。"""
        fences = extract_code_fences(self.read_guide())
        preamble = shell_preamble(fences)
        self.assertIn(DELETION_GUARD, preamble, "指南缺少删除路径守卫函数")
        self.assertIn("refusing", preamble, "指南的删除守卫必须拒绝非指南目录")
        deletions = [fence for fence in fences if DELETION_COMMAND.search(fence["body"])]
        self.assertTrue(deletions, "指南缺少清理命令")
        for fence in deletions:
            self.assertIn(
                DELETION_GUARD, fence["body"],
                f"第 {fence['line']} 行的删除命令缺少路径守卫",
            )


if __name__ == "__main__":
    unittest.main()
