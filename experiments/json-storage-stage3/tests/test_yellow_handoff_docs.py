"""黄区交接正式文档的契约测试。"""

import json
import re
import shlex
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
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

# shell 片段检查依赖指南标记的变量声明块；递归删除必须带路径守卫。
PREAMBLE_MARKER = "<!-- shell-preamble -->"
FENCE_OPEN = re.compile(r"^(?P<fence>(?:`{3,}|~{3,}))\s*(?P<info>[^\n]*?)\s*$")
RECURSIVE_DELETION = re.compile(r"\brm\s+-[A-Za-z]*[rR]")
ANY_DELETION = re.compile(r"\brm\s+-")
DELETION_GUARD = "require_guide_path"
# RPM 路径只删除本指南创建的固定覆盖文件，路径为字面常量。
FIXED_RPM_OVERRIDE = "/etc/clickhouse-server/config.d/00-stage3-yellow.xml"
ALL_WORKLOADS_ARGUMENT = (
    "--workloads main,equal_total_few_large,equal_total_many_medium,correctness_only"
)

# 代表性 ss 输出：真实 ss 行以对端地址结尾，端口判据必须读取本地地址字段。
LISTEN_HEADER = "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
LISTEN_TABLE_ONLY = LISTEN_HEADER + "\n"
LISTEN_OCCUPIED = (
    LISTEN_HEADER + "\n"
    "LISTEN 0      4096          127.0.0.1:18123       0.0.0.0:*     users:((\"clickhouse\",pid=4242,fd=57))\n"
    "LISTEN 0      4096             [::1]:19000          [::]:*      users:((\"clickhouse\",pid=4242,fd=58))\n"
)
LISTEN_LOOPBACK_ONLY = (
    LISTEN_HEADER + "\n"
    "LISTEN 0      4096          127.0.0.1:18123       0.0.0.0:*     users:((\"clickhouse\",pid=4242,fd=57))\n"
    "LISTEN 0      4096          127.0.0.1:19000       0.0.0.0:*     users:((\"clickhouse\",pid=4242,fd=58))\n"
)
LISTEN_ANY_ADDRESS = (
    LISTEN_HEADER + "\n"
    "LISTEN 0      4096            0.0.0.0:18123       0.0.0.0:*     users:((\"clickhouse\",pid=4242,fd=57))\n"
    "LISTEN 0      4096            0.0.0.0:19000       0.0.0.0:*     users:((\"clickhouse\",pid=4242,fd=58))\n"
)

# 与包内 config.xml 同形的测试模板，用于执行指南的配置改写脚本。
CONFIG_TEMPLATE = """<clickhouse>
    <logger>
        <log>/var/log/clickhouse-server/clickhouse-server.log</log>
        <errorlog>/var/log/clickhouse-server/clickhouse-server.err.log</errorlog>
    </logger>
    <!--
    <storage_configuration>
        <disks>
            <blob_storage_disk>
                <metadata_path>/var/lib/clickhouse/disks/blob_storage_disk/</metadata_path>
            </blob_storage_disk>
        </disks>
    </storage_configuration>
    -->
    <custom_cached_disks_base_directory>/var/lib/clickhouse/caches/</custom_cached_disks_base_directory>
    <http_port>8123</http_port>
    <tcp_port>9000</tcp_port>
    <!--
    <listen_host>::1</listen_host>
    <listen_host>127.0.0.1</listen_host>
    -->
    <max_server_memory_usage>0</max_server_memory_usage>
    <max_server_memory_usage_to_ram_ratio>0.9</max_server_memory_usage_to_ram_ratio>
    <path>/var/lib/clickhouse/</path>
    <tmp_path>/var/lib/clickhouse/tmp/</tmp_path>
    <user_files_path>/var/lib/clickhouse/user_files/</user_files_path>
    <format_schema_path>/var/lib/clickhouse/format_schemas/</format_schema_path>
    <user_directories>
        <users_xml>
            <path>users.xml</path>
        </users_xml>
        <local_directory>
            <path>/var/lib/clickhouse/access/</path>
        </local_directory>
    </user_directories>
</clickhouse>
"""


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


def assert_bash_ok(case, script, label):
    """执行受控 shell 片段，非零退出时输出命令结果以便定位。"""
    completed = subprocess.run(
        ["bash", "-c", script], text=True, capture_output=True, check=False,
    )
    case.assertEqual(
        0, completed.returncode,
        f"{label} 执行失败：{completed.stderr.strip()} {completed.stdout.strip()}",
    )
    return completed


def fence_with(fences, needle):
    """返回首个正文包含 needle 的围栏；缺失时直接失败。"""
    for fence in fences:
        if needle in fence["body"]:
            return fence
    raise AssertionError(f"指南缺少包含 {needle} 的代码块")


def heredoc_body(body, marker):
    """提取 <<'MARKER' 与其后单独 MARKER 行之间的脚本正文。"""
    lines = body.splitlines()
    opener = "<<'" + marker + "'"
    start = next(
        index for index, line in enumerate(lines) if line.rstrip().endswith(opener)
    )
    end = next(
        index for index in range(start + 1, len(lines)) if lines[index].strip() == marker
    )
    return "\n".join(lines[start + 1:end])


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
        self.assertIn(
            'cat "$YELLOW_STATE/branch-check.txt" >&2', content,
            "分支可用性失败时必须回显检查输出",
        )
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
        """递归删除限制在指南创建的目录内；唯一例外是指南创建的固定覆盖文件。"""
        fences = extract_code_fences(self.read_guide())
        preamble = shell_preamble(fences)
        self.assertIn(DELETION_GUARD, preamble, "指南缺少删除路径守卫函数")
        self.assertIn("refusing", preamble, "指南的删除守卫必须拒绝非指南目录")
        deletions = [fence for fence in fences if ANY_DELETION.search(fence["body"])]
        self.assertTrue(deletions, "指南缺少清理命令")
        for fence in deletions:
            self.assertTrue(
                DELETION_GUARD in fence["body"] or FIXED_RPM_OVERRIDE in fence["body"],
                f"第 {fence['line']} 行的删除命令既无路径守卫，也不是固定覆盖文件",
            )
        for fence in fences:
            if not RECURSIVE_DELETION.search(fence["body"]):
                continue
            self.assertIn(
                DELETION_GUARD, fence["body"],
                f"第 {fence['line']} 行的递归删除缺少路径守卫",
            )
            self.assertNotIn(
                FIXED_RPM_OVERRIDE, fence["body"],
                f"第 {fence['line']} 行不得递归删除系统路径",
            )


    def test_guide_shell_preamble_runs_and_guard_rejects_unsafe_paths(self):
        """前置变量块可执行；删除守卫拒绝空根、相对路径、目录外与状态根目录本身。"""
        fences = extract_code_fences(self.read_guide())
        preamble = shell_preamble(fences)
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            inside = state / "child"
            outside = Path(root) / "outside"
            inside.mkdir(parents=True)
            outside.mkdir()
            probe = "\n".join((
                f"export YELLOW_STATE={shlex.quote(str(state))}",
                'probe() { require_guide_path "$1" >/dev/null 2>&1; echo "$?"; }',
                f'echo "inside=$(probe {shlex.quote(str(inside))})"',
                f'echo "outside=$(probe {shlex.quote(str(outside))})"',
                f'echo "root=$(probe {shlex.quote(str(state))})"',
                f'echo "traversal=$(probe {shlex.quote(str(state / ".." / "state"))})"',
                'echo "relative=$(probe relative/child)"',
                f'echo "missing=$(probe {shlex.quote(str(state / "missing"))})"',
                f'echo "empty_root=$(YELLOW_STATE= probe {shlex.quote(str(inside))})"',
                f'echo "message=$(require_guide_path {shlex.quote(str(outside))} 2>&1 >/dev/null || true)"',
            ))
            completed = assert_bash_ok(self, preamble + "\n" + probe, "前置变量块与删除守卫")
        values = dict(
            line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
        )
        self.assertEqual("0", values["inside"], "守卫必须放行状态目录内的路径")
        for name in ("outside", "root", "traversal", "relative", "missing", "empty_root"):
            self.assertNotEqual("0", values[name], f"守卫必须拒绝 {name}")
        self.assertIn("refusing", values["message"], "拒绝时必须输出 refusing 说明")

    def test_guide_config_rewrite_pins_state_paths_and_single_memory_ratio(self):
        """TGZ 配置改写把路径与端口指向状态目录，并只保留一个生效内存比例。"""
        fences = extract_code_fences(self.read_guide())
        script = heredoc_body(fence_with(fences, "config anchor not unique")["body"], "PY")
        with tempfile.TemporaryDirectory() as root:
            template = Path(root) / "config.xml"
            template.write_text(CONFIG_TEMPLATE, encoding="utf-8")
            output = Path(root) / "etc" / "config.xml"
            state = Path(root) / "state"
            completed = subprocess.run(
                ["python3", "-", str(template), str(output), str(state), "18123", "19000"],
                input=script, text=True, capture_output=True, check=False,
            )
            self.assertEqual(
                0, completed.returncode, f"配置改写脚本执行失败：{completed.stderr.strip()}",
            )
            text = output.read_text(encoding="utf-8")
        tree = ElementTree.fromstring(text)
        expected = {
            "logger/log": f"{state}/log/clickhouse-server.log",
            "logger/errorlog": f"{state}/log/clickhouse-server.err.log",
            "path": f"{state}/data/",
            "tmp_path": f"{state}/tmp/",
            "user_files_path": f"{state}/user_files/",
            "format_schema_path": f"{state}/format_schemas/",
            "custom_cached_disks_base_directory": f"{state}/caches/",
            "http_port": "18123",
            "tcp_port": "19000",
            "listen_host": "127.0.0.1",
            "user_directories/users_xml/path": f"{state}/etc/users.xml",
            "user_directories/local_directory/path": f"{state}/access/",
            "max_server_memory_usage": "0",
        }
        for path, value in expected.items():
            self.assertEqual(value, tree.findtext(path), f"配置项 {path} 未按预期改写")
        ratios = tree.findall("max_server_memory_usage_to_ram_ratio")
        self.assertEqual(1, len(ratios), "生效内存比例必须只有一个")
        self.assertEqual("0.5", (ratios[0].text or "").strip(), "内存比例必须改写为 0.5")
        self.assertEqual(
            1, text.count("<max_server_memory_usage_to_ram_ratio>"), "内存比例元素出现重复",
        )
        self.assertEqual(1, len(tree.findall("listen_host")), "生效 listen_host 必须只有一个")
        self.assertIn("<listen_host>::1</listen_host>", text, "包内注释示例保持注释状态")
        self.assertNotIn("<path>/var/lib/clickhouse/</path>", text, "数据目录必须指向状态目录")

    def test_guide_cleanup_is_idempotent_and_keeps_foreign_paths(self):
        """清理片段可重复执行：状态目录被删除，重复执行成功，目录外路径不变。"""
        fences = extract_code_fences(self.read_guide())
        cleanup = fence_with(fences, "already clean")["body"]
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            keep = Path(root) / "keep"
            for relative in ("clickhouse/data", "runs/xstore-assets", "runs/xstore-main"):
                target = state / relative
                target.mkdir(parents=True)
                (target / "sentinel.txt").write_text("x", encoding="utf-8")
            keep.mkdir()
            (keep / "keep.txt").write_text("keep", encoding="utf-8")
            script = "\n".join((
                shell_preamble(fences),
                f"export YELLOW_STATE={shlex.quote(str(state))}",
                "export CH_INSTALL_MODE=tgz",
                f"export CH_STATE={shlex.quote(str(state / Path('clickhouse')))}",
                "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
                cleanup,
            ))
            first = assert_bash_ok(self, script, "清理片段第一次执行")
            second = assert_bash_ok(self, script, "清理片段第二次执行")
            self.assertIn("cleanup complete", first.stdout, "最终断言必须执行")
            self.assertIn("already clean", second.stdout, "重复执行必须按已清理处理")
            self.assertFalse((state / "clickhouse").exists(), "状态目录未删除")
            self.assertFalse((state / "runs" / "xstore-assets").exists(), "对象目录未删除")
            self.assertFalse((state / "runs" / "xstore-main").exists(), "XStore 运行目录未删除")
            self.assertTrue((keep / "keep.txt").is_file(), "清理不得触碰指南目录外路径")

    def test_guide_defines_python_environment_and_preflight(self):
        """指南给出可复现虚拟环境与依赖来源，并在 loader 前做版本与导入检查。"""
        content = self.read_guide()
        for term in (
            "-m venv", "-m pip install", "psycopg[binary]==3.3.5", "PYTHON_BOOTSTRAP",
            "sys.version_info < (3, 10)", 'import_module("psycopg")',
            'import_module("production")', "load_formal_input", "runner=importable",
        ):
            self.assertIn(term, content, f"指南缺少 Python 环境契约：{term}")
        fences = extract_code_fences(content)
        self.assertTrue(
            [fence for fence in fences if "runner=importable" in fence["body"]],
            "指南缺少 Python 前置检查代码块",
        )

    def test_guide_documents_native_package_engine_identity_contract(self):
        """无 Docker 的 ClickHouse 与 XStore 都要先完成原生包引擎身份改动与测试。"""
        content = self.read_guide()
        for term in (
            "container image digest evidence is missing", "原生包引擎身份",
            "engine_version", "package_checksums", "binary_sha256", "config_identity",
            "单 target candidate", "ClickHouse", "XStore",
        ):
            self.assertIn(term, content, f"指南缺少引擎身份契约：{term}")

    def test_guide_runs_all_workloads_in_one_invocation(self):
        """可发布 target 必须在一次调用中列出四个 workload，分次调用属于无效运行。"""
        fences = extract_code_fences(self.read_guide())
        clickhouse = [fence for fence in fences if "--engines clickhouse" in fence["body"]]
        self.assertTrue(clickhouse, "指南缺少 ClickHouse 矩阵调用")
        for fence in clickhouse:
            self.assertIn(
                ALL_WORKLOADS_ARGUMENT, fence["body"],
                f"第 {fence['line']} 行的 ClickHouse 调用未列出四个 workload",
            )
        for fence in fences:
            for line in fence["body"].splitlines():
                stripped = line.strip().rstrip("\\").strip()
                self.assertNotEqual(
                    "--workloads main", stripped,
                    f"第 {fence['line']} 行只列出 main workload",
                )
        content = self.read_guide()
        for term in ("覆盖同一 target 的 run-manifest.json", "无效运行"):
            self.assertIn(term, content, f"指南缺少单次调用说明：{term}")

    def test_guide_makes_rpm_and_tgz_paths_exclusive(self):
        """RPM 与 TGZ 路径互斥，RPM 具备备份、服务验证、回滚与卸载语义。"""
        content = self.read_guide()
        for term in (
            "互斥", "CH_INSTALL_MODE", "systemctl start clickhouse-server",
            "systemctl stop clickhouse-server", "dnf remove", "tar -czf",
            FIXED_RPM_OVERRIDE, "NOKEY", "digests OK", "preexisting",
        ):
            self.assertIn(term, content, f"指南缺少 RPM 路径契约：{term}")

    def test_guide_treats_error_and_fatal_logs_as_failures(self):
        """启动日志出现 <Error> 或 <Fatal> 标记时停止。"""
        fences = extract_code_fences(self.read_guide())
        scanners = [fence for fence in fences if "<Error>|<Fatal>" in fence["body"]]
        self.assertTrue(scanners, "指南缺少 ERROR/FATAL 日志判据")
        for fence in scanners:
            self.assertIn(
                "exit 1", fence["body"],
                f"第 {fence['line']} 行未在发现 ERROR/FATAL 时停止",
            )


    def test_guide_uses_address_field_extraction_for_port_gates(self):
        """端口门禁读取本地地址字段，不使用行尾锚定的 ss 匹配，三处门禁统一调用辅助函数。"""
        content = self.read_guide()
        self.assertNotIn(
            "grep -E ':(18123|19000)$'", content,
            "ss 输出以对端地址结尾，锚定行尾的端口匹配会失效",
        )
        for name in (
            "listening_local_addresses", "experiment_listeners",
            "assert_ports_free", "assert_ports_loopback_only",
        ):
            self.assertIn(name, content, f"指南缺少端口辅助函数：{name}")
        fences = extract_code_fences(content)
        gates = [
            fence for fence in fences
            if "assert_ports_free || exit 1" in fence["body"]
            or "assert_ports_loopback_only || exit 1" in fence["body"]
        ]
        self.assertGreaterEqual(
            len(gates), 3, "占用检查、回环检查与清理确认都必须调用端口辅助函数",
        )

    def test_guide_port_helpers_detect_listeners_and_accept_free_ports(self):
        """占用端口被识别，非回环监听被拒绝，只有表头时视为空闲。"""
        fences = extract_code_fences(self.read_guide())
        preamble = shell_preamble(fences)
        samples = {
            "free": LISTEN_TABLE_ONLY,
            "occupied": LISTEN_OCCUPIED,
            "loopback": LISTEN_LOOPBACK_ONLY,
            "any": LISTEN_ANY_ADDRESS,
        }
        for name, sample in samples.items():
            script = "\n".join((
                preamble,
                "ss() { cat <<'SSOUT'",
                sample.rstrip("\n"),
                "SSOUT",
                "}",
                "assert_ports_free >/dev/null 2>&1; echo \"free=$?\"",
                "assert_ports_loopback_only >/dev/null 2>&1; echo \"loop=$?\"",
            ))
            completed = subprocess.run(
                ["bash", "-c", script], text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, completed.returncode, f"{name} 探针失败：{completed.stderr.strip()}")
            values = dict(line.split("=") for line in completed.stdout.splitlines() if "=" in line)
            if name == "free":
                self.assertEqual("0", values["free"], "只有表头时必须视为端口空闲")
            else:
                self.assertEqual("1", values["free"], f"{name} 的占用端口未被识别")
            if name == "loopback":
                self.assertEqual("0", values["loop"], "回环双端口监听必须通过回环判据")
            else:
                self.assertEqual("1", values["loop"], f"{name} 不应通过回环判据")
        message_script = "\n".join((
            preamble,
            "ss() { cat <<'SSOUT'",
            LISTEN_OCCUPIED.rstrip("\n"),
            "SSOUT",
            "}",
            "assert_ports_free 2>&1 >/dev/null || true",
        ))
        message = subprocess.run(
            ["bash", "-c", message_script], text=True, capture_output=True, check=False,
        )
        self.assertIn("18123", message.stdout, "占用端口必须出现在拒绝信息中")

    def test_guide_cleanup_rpm_branch_rolls_back_guide_owned_override(self):
        """RPM 清理分支可执行：删除指南覆盖文件、按备份状态恢复、断言覆盖文件消失。"""
        fences = extract_code_fences(self.read_guide())
        cleanup = fence_with(fences, "cleanup assertion failed")["body"]
        self.assertLess(
            cleanup.index("sha256sum -c"), cleanup.index("tar -xzf"),
            "备份校验必须先于配置恢复",
        )
        self.assertLess(
            cleanup.index("preexisting.txt"), cleanup.index("tar -xzf"),
            "配置恢复必须排在 preexisting 判定之后",
        )
        self.assertIn("is-active", cleanup, "恢复后必须验证服务状态")
        self.assertIn("require_rpm_override_path", cleanup, "覆盖文件删除前必须校验路径形状")
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            override = (
                Path(root) / "etc" / "clickhouse-server" / "config.d" / "00-stage3-yellow.xml"
            )
            override.parent.mkdir(parents=True)
            override.write_text("<clickhouse></clickhouse>\n", encoding="utf-8")
            (state / "backup").mkdir(parents=True)
            (state / "backup" / "preexisting.txt").write_text("no\n", encoding="utf-8")
            (state / "clickhouse" / "log").mkdir(parents=True)
            (state / "runs" / "xstore-assets").mkdir(parents=True)
            (state / "runs" / "xstore-main").mkdir(parents=True)
            script = "\n".join((
                shell_preamble(fences),
                f"export YELLOW_STATE={shlex.quote(str(state))}",
                f"export CH_STATE={shlex.quote(str(state / Path('clickhouse')))}",
                f"export CH_RPM_OVERRIDE={shlex.quote(str(override))}",
                "export CH_INSTALL_MODE=rpm",
                'sudo() { "$@"; }',
                "systemctl() { return 0; }",
                "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
                cleanup,
            ))
            completed = assert_bash_ok(self, script, "RPM 清理分支")
            self.assertFalse(override.exists(), "指南创建的覆盖文件必须被删除")
            self.assertFalse((state / "clickhouse").exists(), "状态目录必须删除")
            self.assertFalse((state / "runs" / "xstore-assets").exists(), "对象目录必须删除")
            self.assertFalse((state / "runs" / "xstore-main").exists(), "XStore 运行目录必须删除")
            self.assertTrue((Path(root) / "etc").is_dir(), "不得删除覆盖文件之外的系统路径")
            self.assertTrue(
                (Path(root) / "etc" / "clickhouse-server" / "config.d").is_dir(),
                "覆盖文件所在目录保持不变",
            )
        self.assertIn("cleanup complete", completed.stdout, "清理必须输出完成标记")


if __name__ == "__main__":
    unittest.main()
