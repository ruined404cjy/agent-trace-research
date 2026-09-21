"""黄区交接正式文档的契约测试。"""

import json
import os
import re
import shlex
import shutil
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
    "完整矩阵中的观测区分度",
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
    "全部正式运行",
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

# "完成" 只能用于描述操作、正式运行、计时阶段、门禁、水位、block 或样本，或用于否定整体完成状态。
COMPLETION_SCOPES = (
    "操作",
    "正式运行",
    "查询完成",
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
    ("430.62", "`recovery_ms`"),
    ("7,713.97", "`recovery_ms`"),
    ("250.20", "`recovery_ms`"),
    ("1,772.09", "`recovery_ms`"),
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
HANDOFF_BASE_COMMIT = "2a7fe245a3cb8843b0e9da77cdf05e290ab96b1b"
HANDOFF_BASE_COMMIT_SHORT = "2a7fe24"
SSH_CLONE_URL = "git@github.com:ruined404cjy/agent-trace-research.git"
PUBLIC_CLONE_URL = "https://github.com/ruined404cjy/agent-trace-research.git"
ARCHIVE_NAME = "json-storage-stage3-formal-input-20260917.tar.gz"
CHECKSUM_NAME = ARCHIVE_NAME + ".sha256"
PACKAGED_ARCHIVE_SHA256 = (
    "47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f"
)
PACKAGED_ARCHIVE_BYTES = "19,313,037"

# Kunpeng 920 无 SVE，官方 ARM64 产物自 23.8 起启动即 SIGILL；指南固定实测可启动的最高版本。
CLICKHOUSE_VERSION = "23.3.10.5"
CLICKHOUSE_RELEASE_TAG = "v23.3.10.5-lts"
CLICKHOUSE_RELEASE_TAG_URL = (
    "https://github.com/ClickHouse/ClickHouse/releases/tag/${CH_RELEASE_TAG}"
)
CLICKHOUSE_RELEASE_DOWNLOAD = (
    "https://github.com/ClickHouse/ClickHouse/releases/download/${CH_RELEASE_TAG}"
)
CLICKHOUSE_VERSION_PROBE_ROWS = (
    "| 22.8.21.38 | 正常 |",
    "| 23.3.10.5 | 正常 |",
    "| 23.8.15.35 | SIGILL，Illegal instruction |",
    "| 24.3.18.7 | SIGILL，Illegal instruction |",
)
CLICKHOUSE_ARM64_ARCHIVES = (
    "clickhouse-common-static-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz",
    "clickhouse-server-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz",
    "clickhouse-client-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz",
)
CLICKHOUSE_AARCH64_RPMS = (
    "clickhouse-common-static-${CH_VERSION}.aarch64.rpm",
    "clickhouse-server-${CH_VERSION}.aarch64.rpm",
    "clickhouse-client-${CH_VERSION}.aarch64.rpm",
)
CLICKHOUSE_TGZ_BINARY = "clickhouse-common-static-${CH_VERSION}/usr/bin/clickhouse"
# 5.6 节把写入保护阈值与内存比例对齐到蓝区默认值，消除 23.3 与 25.12 的差异。
CLICKHOUSE_ALIGNED_SETTINGS = (
    "<parts_to_delay_insert>1000</parts_to_delay_insert>",
    "<parts_to_throw_insert>3000</parts_to_throw_insert>",
    "<max_server_memory_usage_to_ram_ratio>0.5</max_server_memory_usage_to_ram_ratio>",
)

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

# 能力报告模板只声明未验证状态，门禁脚本在 unverified、未知状态或缺少证据时阻止 adapter 实施。
CAPABILITY_DEFAULT_STATUS = "unverified"
CAPABILITY_GATE_BLOCKED = "adapter work is blocked"
CAPABILITY_GATE_EVIDENCE_ERROR = "lacks evidence command or observed output"

# runner 在每个输出根下创建 Asset 工作树；清理必须覆盖真实路径而不是虚构目录。
ASSET_WORKSPACE_NAME = ".asset-work"
GUIDE_OUTPUT_ROOTS = ("clickhouse-main", "xstore-main")
STALE_ASSET_PATH = "runs/xstore-assets"

# 每条可执行片段都要求安装模式已经确定为 rpm 或 tgz。
INSTALL_MODE_ERROR = "CH_INSTALL_MODE must be rpm or tgz"
STOP_FENCE_MARKER = "# 5.7 停止服务"
BACKUP_FENCE_MARKER = "# 5.3 备份既有系统配置"

# §7.1 阶段表在正式矩阵之后必须覆盖 part-state、混合负载与 Asset 故障恢复。
def seed_handback_summary(output_root):
    """按 9.1 节门禁写入覆盖全部 target 的回传摘录，使清理片段可以执行。"""
    output_root = Path(output_root)
    targets = [
        {"target": str(manifest.parent.relative_to(output_root))}
        for manifest in output_root.rglob("run-manifest.json")
        if "handback" not in manifest.parts
    ]
    handback = output_root / "handback"
    handback.mkdir(parents=True, exist_ok=True)
    (handback / "summary.json").write_text(
        json.dumps({"targets": targets}, ensure_ascii=False), encoding="utf-8",
    )


CONTROL_STAGE_MARKERS = (
    "9 part-state",
    "10 混合负载",
    "11 Asset 故障与恢复",
)
# 正式矩阵之前的三道公平性门禁：构建类型、访问路径与单布局冒烟倍率。
FAIRNESS_STAGE_MARKERS = (
    "2 构建类型门禁",
    "5 访问路径门禁",
    "6 单布局冒烟倍率",
)
CONTROL_COMPLETION_GATE = "记录了带证据的不适用结论"

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
# RPM 路径只删除本指南创建的固定覆盖文件；默认根为 /etc，实际路径由 CH_ETC_ROOT 派生。
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
        """评估只在操作、正式运行、计时阶段、门禁、水位、block、样本或否定整体状态时使用“完成”。"""
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

    def test_document_names_median_fields_and_summarizer_scope(self):
        """中位数表标注 latency_ms 与 application_ready_ms，恢复阶段表述与汇总范围明确。"""
        content = self.read_document()
        self.assertNotIn(
            "批量恢复总耗时", content, "recovery_ms 描述的是查询后结果恢复阶段，不是批量场景总耗时",
        )
        self.assertIn("查询后结果恢复阶段", content, "评估缺少查询后结果恢复阶段表述")
        self.assertIn("`latency_ms`", content, "中位数表未标注轮次 result.json 的 latency_ms 字段")
        self.assertIn("`application_ready_ms`", content, "评估未标注 application_ready_ms 口径")
        self.assertIn(
            "参与合并的目录为", content,
            "汇总范围必须写明参与合并的运行目录",
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
        for row in CLICKHOUSE_VERSION_PROBE_ROWS:
            self.assertIn(row, content, f"指南缺少版本实测边界：{row}")
        for setting in CLICKHOUSE_ALIGNED_SETTINGS:
            self.assertEqual(
                3, content.count(setting),
                f"对齐参数必须出现在 5.6 节说明、RPM 覆盖与 TGZ 配置改写三处：{setting}",
            )

    def test_guide_verifies_packages_and_frozen_input(self):
        """指南要求 SHA-512 校验安装包、SHA-256 校验冻结输入。"""
        content = self.read_guide()
        for term in ("sha512sum -c", "sha256sum -c", "load_formal_input", "identity_sha256"):
            self.assertIn(term, content, f"指南缺少校验命令或字段：{term}")
        for term in ('"$file.sha512"', "package-names.txt"):
            self.assertIn(term, content, f"指南缺少包摘要或包身份记录：{term}")

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
        script = heredoc_body(fence_with(fences, "config anchor")["body"], "PY")
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

    def test_guide_cleanup_requires_handback_summary_before_deleting(self):
        """回传摘录缺失或漏 target 时，清理片段拒绝执行且输出根保持完整。"""
        fences = extract_code_fences(self.read_guide())
        cleanup = fence_with(fences, "already clean")["body"]
        for label, seed in (("摘录缺失", None), ("漏记 target", {"targets": []})):
            with self.subTest(label), tempfile.TemporaryDirectory() as root:
                state = Path(root) / "state"
                target = state / "runs" / GUIDE_OUTPUT_ROOTS[0] / "clickhouse" / "same_table"
                target.mkdir(parents=True)
                (target / "run-manifest.json").write_text("{}", encoding="utf-8")
                if seed is not None:
                    handback = state / "runs" / "handback"
                    handback.mkdir(parents=True)
                    (handback / "summary.json").write_text(json.dumps(seed), encoding="utf-8")
                result = self.run_guide_fragment(
                    cleanup, state, seed_handback=False,
                    extra_lines=(
                        f"export YELLOW_OUTPUT={shlex.quote(str(state / 'runs'))}",
                        "export CH_INSTALL_MODE=tgz",
                        "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
                    ),
                )
                self.assertNotEqual(0, result.returncode, f"{label} 时清理必须停止")
                self.assertIn("handback summary", result.stderr + result.stdout, "必须给出摘录门禁原因")
                self.assertTrue(target.is_dir(), f"{label} 时输出根必须保持完整")

    def test_guide_cleanup_is_idempotent_and_keeps_foreign_paths(self):
        """清理片段可重复执行：真实输出根与 Asset 工作树被删除，目录外路径不变。"""
        fences = extract_code_fences(self.read_guide())
        cleanup = fence_with(fences, "already clean")["body"]
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            keep = Path(root) / "keep"
            for relative in (
                f"clickhouse/data",
                f"runs/{GUIDE_OUTPUT_ROOTS[0]}/{ASSET_WORKSPACE_NAME}/clickhouse/same_table",
                f"runs/{GUIDE_OUTPUT_ROOTS[1]}/{ASSET_WORKSPACE_NAME}/xstore/same_table",
                f"runs/{GUIDE_OUTPUT_ROOTS[1]}/xstore/same_table",
            ):
                target = state / relative
                target.mkdir(parents=True)
                (target / "sentinel.txt").write_text("x", encoding="utf-8")
            (state / "runs" / "keep").mkdir(parents=True)
            (state / "runs" / "keep" / "keep.txt").write_text("keep", encoding="utf-8")
            keep.mkdir()
            (keep / "keep.txt").write_text("keep", encoding="utf-8")
            extra = (
                f"export YELLOW_OUTPUT={shlex.quote(str(state / 'runs'))}",
                "export CH_INSTALL_MODE=tgz",
                f"export CH_STATE={shlex.quote(str(state / Path('clickhouse')))}",
                "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
            )
            first = self.run_guide_fragment(cleanup, state, extra_lines=extra)
            self.assertEqual(0, first.returncode, f"清理片段第一次执行失败：{first.stderr.strip()}")
            second = self.run_guide_fragment(cleanup, state, extra_lines=extra)
            self.assertEqual(0, second.returncode, f"清理片段第二次执行失败：{second.stderr.strip()}")
            self.assertIn("cleanup complete", first.stdout, "最终断言必须执行")
            self.assertIn("already clean", second.stdout, "重复执行必须按已清理处理")
            self.assertFalse((state / "clickhouse").exists(), "状态目录未删除")
            for name in GUIDE_OUTPUT_ROOTS:
                self.assertFalse((state / "runs" / name).exists(), f"输出根未删除：{name}")
            self.assertTrue((state / "runs" / "keep" / "keep.txt").is_file(), "清理不得触碰指南目录外路径")
            self.assertTrue((keep / "keep.txt").is_file(), "清理不得触碰指南目录外路径")

    def test_guide_cleanup_uses_manifest_asset_workspace_evidence(self):
        """后续控制运行的 Asset 工作树由 manifest 证据发现、守卫、删除并断言。"""
        cleanup = fence_with(extract_code_fences(self.read_guide()), "already clean")["body"]
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            output = state / "runs"
            future_root = output / "asset-failures-clickhouse"
            asset_workspace = future_root / ASSET_WORKSPACE_NAME
            manifest_dir = future_root / "clickhouse" / "asset_ref"
            asset_workspace.mkdir(parents=True)
            (asset_workspace / "sentinel.txt").write_text("owned", encoding="utf-8")
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "run-manifest.json").write_text(json.dumps({
                "global_cleanup": {
                    "asset_workspace": str(asset_workspace),
                    "removed": False,
                },
            }), encoding="utf-8")
            (state / "clickhouse" / "run").mkdir(parents=True)
            completed = self.run_guide_fragment(cleanup, state, extra_lines=(
                f"export YELLOW_OUTPUT={shlex.quote(str(output))}",
                "export CH_INSTALL_MODE=tgz",
                "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
            ))
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertFalse(asset_workspace.exists(), "manifest 记录的 Asset 工作树未删除")
            self.assertTrue(manifest_dir.is_dir(), "证据派生清理不得删除后续运行产物")
            self.assertIn(str(asset_workspace), completed.stdout, "最终输出缺少证据派生路径断言")

    def test_guide_cleanup_targets_runner_asset_workspaces(self):
        """清理指向 runner 真实输出根下的 .asset-work，不出现虚构目录名。"""
        content = self.read_guide()
        fences = extract_code_fences(content)
        cleanup = fence_with(fences, "already clean")["body"]
        self.assertNotIn(STALE_ASSET_PATH, content, "指南不得引用虚构的 runs/xstore-assets 路径")
        for name in GUIDE_OUTPUT_ROOTS:
            self.assertIn(f"$YELLOW_OUTPUT/{name}", cleanup, f"清理缺少指南创建的输出根：{name}")
        self.assertIn(ASSET_WORKSPACE_NAME, cleanup, "清理必须覆盖输出根下的 .asset-work")
        self.assertIn(DELETION_GUARD, cleanup, "清理中的递归删除必须带路径守卫")

    def test_guide_cleanup_overview_states_the_exact_deletion_scope(self):
        """9.1 概述只声明两个主矩阵输出根与 $YELLOW_STATE/clickhouse，并列出保留项。"""
        content = self.read_guide()
        section = content[content.index("### 9.1"):content.index("### 9.2")]
        self.assertNotIn(
            "删除指南创建的运行输出根与状态目录", section,
            "概述不得把删除范围写成整个状态目录",
        )
        self.assertIn(
            "$YELLOW_OUTPUT 下的 clickhouse-main 与 xstore-main 两个主矩阵输出根及 $YELLOW_STATE/clickhouse",
            section, "概述必须声明删除的确切范围",
        )
        for retained in ("venv", "冻结输入", "Release 资产", "后续控制运行的证据"):
            self.assertIn(retained, section, f"概述必须说明保留 {retained}")

    def test_guide_backup_covers_preexisting_system_configuration(self):
        """5.3 备份在包安装前执行，恢复只在 preexisting=yes 时适用。"""
        content = self.read_guide()
        section = content[content.index("### 5.3"):content.index("### 5.4")]
        self.assertIn("备份既有系统配置", section, "5.3 必须把备份对象写成既有系统配置")
        self.assertNotIn("备份包默认配置", section, "5.3 不得把备份对象写成包默认配置")
        self.assertIn("备份在包安装前执行", section, "必须说明备份发生在包安装前")
        self.assertIn("恢复只适用于 preexisting=yes", section, "必须说明恢复只在 preexisting=yes 时适用")

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
            etc_root = Path(root) / "etc"
            override = etc_root / "clickhouse-server" / "config.d" / "00-stage3-yellow.xml"
            override.parent.mkdir(parents=True)
            override.write_text("<clickhouse></clickhouse>\n", encoding="utf-8")
            (state / "clickhouse" / "backup").mkdir(parents=True)
            (state / "clickhouse" / "backup" / "preexisting.txt").write_text(
                "preexisting=no\n", encoding="utf-8",
            )
            (state / "clickhouse" / "log").mkdir(parents=True)
            for name in GUIDE_OUTPUT_ROOTS:
                (state / "runs" / name / ASSET_WORKSPACE_NAME / "engine" / "same_table").mkdir(parents=True)
            completed = self.run_guide_fragment(cleanup, state, extra_lines=(
                f"export YELLOW_OUTPUT={shlex.quote(str(state / 'runs'))}",
                "export CH_INSTALL_MODE=rpm",
                self.sandbox_sudo_stub(),
                self.loopback_systemctl_stub(Path(root) / "observed-start.txt"),
                self.loopback_ss_stub(),
            ), env_overrides={"CH_ETC_ROOT": str(etc_root)})
            self.assertFalse(override.exists(), "指南创建的覆盖文件必须被删除")
            self.assertFalse((state / "clickhouse").exists(), "状态目录必须删除")
            for name in GUIDE_OUTPUT_ROOTS:
                self.assertFalse((state / "runs" / name).exists(), f"输出根必须删除：{name}")
            self.assertTrue((Path(root) / "etc").is_dir(), "不得删除覆盖文件之外的系统路径")
            self.assertTrue(
                (Path(root) / "etc" / "clickhouse-server" / "config.d").is_dir(),
                "覆盖文件所在目录保持不变",
            )
        self.assertIn("cleanup complete", completed.stdout, "清理必须输出完成标记")

    def run_guide_fragment(self, fragment, state, extra_lines=(), env_overrides=None,
                           seed_handback=True):
        """在受控临时状态目录与指南变量块下执行片段，返回子进程结果。

        env_overrides 在 bash 启动前生效，与操作者先设置环境变量再 source 变量块一致。
        seed_handback 为真时先写入回传摘录，对应已完成回传的正常路径；
        置假用于验证 9.1 节在摘录缺失时拒绝删除。
        """
        if seed_handback:
            seed_handback_summary(Path(state) / "runs")
        fences = extract_code_fences(self.read_guide())
        script = "\n".join((
            shell_preamble(fences),
            f"export YELLOW_STATE={shlex.quote(str(state))}",
            f"export CH_STATE={shlex.quote(str(state / Path('clickhouse')))}",
            *extra_lines,
            fragment,
        ))
        return subprocess.run(
            ["bash", "-c", script], text=True, capture_output=True, check=False,
            env={**os.environ, **(env_overrides or {})},
        )

    def run_rpm_restore_failure(self, root, systemctl_stub, ss_stub):
        """构造 RPM 恢复验证的前置状态并执行清理片段，返回结果与关键路径。"""
        state = Path(root) / "state"
        backup = state / "clickhouse" / "backup"
        backup.mkdir(parents=True)
        (backup / "preexisting.txt").write_text("preexisting=no\n", encoding="utf-8")
        (state / "clickhouse" / "run").mkdir(parents=True)
        etc_root = Path(root) / "etc"
        override = etc_root / "clickhouse-server" / "config.d" / "00-stage3-yellow.xml"
        override.parent.mkdir(parents=True)
        override.write_text("<clickhouse></clickhouse>\n", encoding="utf-8")
        observed = Path(root) / "observed-systemctl.txt"
        cleanup = fence_with(extract_code_fences(self.read_guide()), "cleanup assertion failed")["body"]
        result = self.run_guide_fragment(cleanup, state, extra_lines=(
            "export CH_INSTALL_MODE=rpm",
            self.sandbox_sudo_stub(),
            systemctl_stub(observed),
            ss_stub(),
        ), env_overrides={"CH_ETC_ROOT": str(etc_root)})
        return result, state, override, observed

    def parse_probe(self, stdout):
        """把 key=value 探针输出解析为字典。"""
        return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)

    def sandbox_sudo_stub(self):
        """模拟 sudo：在测试目录内直接执行，拒绝任何真实 /etc 路径的操作。"""
        return "\n".join((
            "sudo() {",
            "  for argument in \"$@\"; do",
            "    case \"$argument\" in",
            "      /etc|/etc/*) printf 'refusing real system path: %s\\n' \"$argument\" >&2; return 1 ;;",
            "    esac",
            "  done",
            "  \"$@\"",
            "}",
        ))

    def loopback_systemctl_stub(self, observed):
        """模拟 systemd：start 时记录覆盖文件状态并标记监听，stop 时清除标记。"""
        return "\n".join((
            "systemctl() {",
            "  case \"$1\" in",
            "    start)",
            f"      if [ -f \"$CH_RPM_OVERRIDE\" ]; then printf 'override-present\\n' >> {shlex.quote(str(observed))};",
            f"      else printf 'override-missing\\n' >> {shlex.quote(str(observed))}; fi",
            "      mkdir -p \"$CH_STATE/run\"; touch \"$CH_STATE/run/.fake-listening\"; return 0 ;;",
            "    stop)",
            "      rm -f \"$CH_STATE/run/.fake-listening\"; return 0 ;;",
            "    is-active)",
            "      [ -f \"$CH_STATE/run/.fake-listening\" ]; return $? ;;",
            "  esac",
            "  return 0",
            "}",
        ))

    def loopback_ss_stub(self):
        """模拟 ss：标记文件存在时报告两个实验端口的回环监听。"""
        return "\n".join((
            "ss() {",
            "  printf '%s\\n' 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'",
            "  if [ -f \"$CH_STATE/run/.fake-listening\" ]; then",
            "    printf '%s\\n' 'LISTEN 0 4096 127.0.0.1:18123 0.0.0.0:*'",
            "    printf '%s\\n' 'LISTEN 0 4096 127.0.0.1:19000 0.0.0.0:*'",
            "  fi",
            "}",
        ))

    def recording_systemctl_stub(self, start_status, active_status):
        """返回 systemctl 模拟函数：把 start 与 stop 调用追加到记录文件，按给定状态返回。"""
        def stub(observed):
            return "\n".join((
                "systemctl() {",
                "  case \"$1\" in",
                f"    start) printf 'start\\n' >> {shlex.quote(str(observed))}; return {start_status} ;;",
                f"    stop) printf 'stop\\n' >> {shlex.quote(str(observed))}; return 0 ;;",
                f"    is-active) return {active_status} ;;",
                "  esac",
                "  return 0",
                "}",
            ))
        return stub

    def recorded_systemctl_calls(self, observed):
        """返回 systemctl 模拟记录的调用序列；没有记录文件时返回空列表。"""
        if not observed.is_file():
            return []
        return observed.read_text(encoding="utf-8").split()

    def assert_systemctl_stopped_after_start(self, observed, label):
        """断言记录序列中 start 之后出现 stop，即失败路径确实停止了 clickhouse-server。"""
        calls = self.recorded_systemctl_calls(observed)
        self.assertIn("start", calls, f"{label} 必须先启动 clickhouse-server")
        self.assertIn(
            "stop", calls[calls.index("start"):],
            f"{label} 失败后必须停止 clickhouse-server",
        )

    def any_address_ss_stub(self):
        """模拟 ss：两个实验端口绑定 0.0.0.0，用于回环断言失败的清理路径。"""
        return "\n".join((
            "ss() {",
            "  printf '%s\\n' 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'",
            "  printf '%s\\n' 'LISTEN 0 4096 0.0.0.0:18123 0.0.0.0:*'",
            "  printf '%s\\n' 'LISTEN 0 4096 0.0.0.0:19000 0.0.0.0:*'",
            "}",
        ))

    def capability_report_template(self):
        """返回指南中覆盖全部能力字段的 JSON 模板。"""
        for fence in extract_code_fences(self.read_guide()):
            if fence["info"].split()[:1] != ["json"]:
                continue
            try:
                schema = json.loads(fence["body"])
            except json.JSONDecodeError:
                continue
            if isinstance(schema, dict) and set(schema) >= set(CAPABILITY_FIELDS):
                return schema
        raise AssertionError("指南缺少覆盖全部能力字段的 JSON 模板")

    def filled_capability_report(self):
        """返回所有字段都带状态与证据的能力报告。"""
        report = self.capability_report_template()
        report["engine"] = {
            "product": "xstore", "version": "1.0.0", "protocol": "native", "evidence": "SELECT version()",
        }
        for name in CAPABILITY_FIELDS:
            report[name] = {
                "status": "available",
                "evidence": {"command": f"probe {name}", "observed": f"{name} ok"},
            }
        return report

    def run_capability_gate(self, report):
        """把能力报告写入临时文件并执行指南门禁脚本。"""
        fences = extract_code_fences(self.read_guide())
        script = heredoc_body(fence_with(fences, CAPABILITY_GATE_BLOCKED)["body"], "PY")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "capability-report.json"
            path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            return subprocess.run(
                ["python3", "-", str(path)], input=script, text=True,
                capture_output=True, check=False,
            )

    def test_guide_capability_template_defaults_to_unverified(self):
        """能力报告模板不得把任何字段默认写成 available。"""
        template = self.capability_report_template()
        for name in CAPABILITY_FIELDS:
            entry = template[name]
            self.assertEqual(
                CAPABILITY_DEFAULT_STATUS, entry["status"], f"{name} 默认状态必须为 unverified",
            )
            self.assertEqual("", entry["evidence"]["command"], f"{name} 默认证据命令必须为空")
            self.assertEqual("", entry["evidence"]["observed"], f"{name} 默认证据输出必须为空")
        self.assertEqual("", template["engine"]["product"], "引擎身份默认必须为空")

    def test_guide_capability_gate_blocks_unverified_and_missing_evidence(self):
        """门禁接受完整报告，拒绝模板默认值、未知状态与缺少证据的字段。"""
        blocked = self.run_capability_gate(self.capability_report_template())
        self.assertNotEqual(0, blocked.returncode, "未验证的模板必须被门禁拒绝")
        self.assertIn(CAPABILITY_GATE_BLOCKED, blocked.stderr, "拒绝原因必须说明 adapter 实施被阻止")
        for name in CAPABILITY_FIELDS:
            self.assertIn(name, blocked.stderr, f"拒绝原因缺少字段：{name}")

        accepted = self.run_capability_gate(self.filled_capability_report())
        self.assertEqual(0, accepted.returncode, f"完整报告必须通过门禁：{accepted.stderr.strip()}")
        self.assertIn("capability report accepted", accepted.stdout, "通过时必须输出接受摘要")

        unknown = self.filled_capability_report()
        unknown["cleanup"]["status"] = "unknown"
        self.assertNotEqual(0, self.run_capability_gate(unknown).returncode, "未知状态必须被拒绝")

        missing = self.filled_capability_report()
        missing["cache_control"]["evidence"]["observed"] = ""
        incomplete = self.run_capability_gate(missing)
        self.assertNotEqual(0, incomplete.returncode, "缺少 observed 证据必须被拒绝")
        self.assertIn(CAPABILITY_GATE_EVIDENCE_ERROR, incomplete.stderr, "缺少证据时必须给出可定位原因")

    def test_guide_port_helpers_fail_closed_when_ss_is_unavailable(self):
        """ss 返回非零或缺失时两个端口断言都失败，不得把探测失败当作端口空闲。"""
        preamble = shell_preamble(extract_code_fences(self.read_guide()))
        probe = "\n".join((
            "assert_ports_free >/dev/null 2>&1; echo \"free=$?\"",
            "assert_ports_loopback_only >/dev/null 2>&1; echo \"loop=$?\"",
        ))
        failing = subprocess.run(
            ["bash", "-c", "\n".join((preamble, "ss() { return 1; }", probe))],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, failing.returncode, f"探针失败：{failing.stderr.strip()}")
        self.assertEqual(
            {"free": "1", "loop": "1"}, self.parse_probe(failing.stdout),
            "ss 返回非零时两个端口断言都必须失败",
        )
        missing = subprocess.run(
            [shutil.which("bash"), "-c", "\n".join((preamble, probe))],
            text=True, capture_output=True, check=False,
            env={**os.environ, "PATH": ""},
        )
        self.assertEqual(0, missing.returncode, f"探针失败：{missing.stderr.strip()}")
        self.assertEqual(
            {"free": "1", "loop": "1"}, self.parse_probe(missing.stdout),
            "ss 缺失时两个端口断言都必须失败",
        )

    def test_guide_rejects_unknown_install_mode_before_cleanup(self):
        """未设置或非法的 CH_INSTALL_MODE 在停止与清理前退出，备份与状态目录保持不变。"""
        content = self.read_guide()
        self.assertIn(INSTALL_MODE_ERROR, content, "指南缺少安装模式校验")
        fences = extract_code_fences(content)
        stop = fence_with(fences, STOP_FENCE_MARKER)["body"]
        cleanup = fence_with(fences, "already clean")["body"]
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            (state / "clickhouse" / "backup").mkdir(parents=True)
            (state / "clickhouse" / "backup" / "preexisting.txt").write_text(
                "preexisting=no\n", encoding="utf-8",
            )
            (state / "clickhouse" / "run").mkdir(parents=True)
            for fragment, label in ((stop, "5.7 停止服务"), (cleanup, "9.1 清理")):
                for mode_command in (
                    "unset CH_INSTALL_MODE",
                    "export CH_INSTALL_MODE=''",
                    "export CH_INSTALL_MODE=RPM",
                    "export CH_INSTALL_MODE=tar",
                ):
                    result = self.run_guide_fragment(fragment, state, extra_lines=(
                        mode_command,
                        "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
                    ))
                    self.assertNotEqual(0, result.returncode, f"{label} 未拒绝安装模式命令 {mode_command!r}")
                    self.assertIn(INSTALL_MODE_ERROR, result.stderr, f"{label} 缺少安装模式错误信息")
                    self.assertTrue(
                        (state / "clickhouse" / "backup" / "preexisting.txt").is_file(),
                        f"{label} 在安装模式非法时删除了备份",
                    )
                    self.assertTrue(
                        (state / "clickhouse").is_dir(), f"{label} 在安装模式非法时删除了状态目录",
                    )
            valid = self.run_guide_fragment(stop, state, extra_lines=(
                "export CH_INSTALL_MODE=tgz",
                "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
            ))
            self.assertEqual(0, valid.returncode, f"tgz 模式必须继续执行：{valid.stderr.strip()}")

    def test_guide_preamble_requires_explicit_install_mode_choice(self):
        """仅加载变量块时不得默认选择安装方式，停止片段必须拒绝继续。"""
        fences = extract_code_fences(self.read_guide())
        stop = fence_with(fences, STOP_FENCE_MARKER)["body"]
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            (state / "clickhouse" / "run").mkdir(parents=True)
            result = self.run_guide_fragment(stop, state, extra_lines=(
                "ss() { echo 'State Recv-Q Send-Q Local Address:Port Peer Address:Port'; }",
            ))
        self.assertNotEqual(0, result.returncode, "变量块不得默认选择 tgz 安装方式")
        self.assertIn(INSTALL_MODE_ERROR, result.stderr)

    def test_guide_scopes_control_runs_after_the_main_matrix(self):
        """执行序列在正式矩阵后覆盖 part-state、混合负载与 Asset 故障恢复，并给出完成门禁。"""
        content = self.read_guide()
        for marker in CONTROL_STAGE_MARKERS:
            self.assertIn(marker, content, f"执行序列缺少阶段：{marker}")
        table = content[content.index("### 7.1"):content.index("### 7.2")]
        self.assertLess(table.index("8 正式矩阵"), table.index("9 part-state"), "控制阶段必须排在正式矩阵之后")
        self.assertLess(table.index("9 part-state"), table.index("10 混合负载"), "阶段顺序必须为 part-state → 混合负载")
        self.assertLess(table.index("10 混合负载"), table.index("11 Asset 故障与恢复"), "Asset 故障恢复排在混合负载之后")
        for marker in FAIRNESS_STAGE_MARKERS:
            self.assertIn(marker, table, f"执行序列缺少公平性门禁阶段：{marker}")
            self.assertLess(
                table.index(marker), table.index("8 正式矩阵"),
                f"{marker} 必须排在正式矩阵之前",
            )
        for command in ("part-states", "interference", "asset-failures"):
            self.assertIn(command, content, f"执行序列缺少命令：{command}")
        self.assertIn(CONTROL_COMPLETION_GATE, content, "指南缺少不适用与证据的完成门禁")
        self.assertIn("不适用", content, "指南缺少不适用的记录要求")

    def test_guide_creates_workspace_before_changing_directory(self):
        """仓库获取片段先创建 YELLOW_WORKSPACE 再进入该目录，可用性检查同样先创建。"""
        fences = extract_code_fences(self.read_guide())
        fetch = fence_with(fences, "git clone")["body"]
        self.assertLess(
            fetch.index('mkdir -p "$YELLOW_WORKSPACE"'), fetch.index('cd "$YELLOW_WORKSPACE"'),
            "必须先创建 YELLOW_WORKSPACE 再 cd",
        )
        self.assertIn(
            'mkdir -p "$YELLOW_WORKSPACE"', fence_with(fences, "branch-check.txt")["body"],
            "可用性检查前必须创建 YELLOW_WORKSPACE",
        )

    def test_guide_explains_run_manifest_exclusion_without_external_reference(self):
        """run-manifest.json 排除说明自包含，不依赖未提交的内部裁决记录。"""
        content = self.read_guide()
        self.assertNotIn("裁决", content, "指南不得引用未提交的裁决记录")
        for term in ("run-manifest.json", "generate-input", "不进入归档"):
            self.assertIn(term, content, f"指南缺少归档排除说明：{term}")

    def test_guide_config_rewrite_distinguishes_missing_and_duplicate_anchors(self):
        """配置锚点缺失与重复分别失败，锚点文本限定为 23.3.10.5 包内原文。"""
        content = self.read_guide()
        self.assertIn("23.3.10.5 包内", content, "必须说明锚点取自 23.3.10.5 包内 config.xml")
        fences = extract_code_fences(content)
        script = heredoc_body(fence_with(fences, "config anchor")["body"], "PY")
        with tempfile.TemporaryDirectory() as root:
            missing_template = Path(root) / "missing.xml"
            missing_template.write_text(
                "<clickhouse><http_port>8123</http_port></clickhouse>\n", encoding="utf-8",
            )
            missing = subprocess.run(
                ["python3", "-", str(missing_template), str(Path(root) / "out.xml"),
                 str(Path(root) / "state"), "18123", "19000"],
                input=script, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(0, missing.returncode, "锚点缺失必须失败")
            self.assertIn("config anchor not found", missing.stderr, "锚点缺失必须给出缺失原因")
            duplicate_template = Path(root) / "duplicate.xml"
            duplicate_template.write_text(
                CONFIG_TEMPLATE.replace(
                    "<http_port>8123</http_port>",
                    "<http_port>8123</http_port>\n    <http_port>8123</http_port>",
                ),
                encoding="utf-8",
            )
            duplicated = subprocess.run(
                ["python3", "-", str(duplicate_template), str(Path(root) / "dup.xml"),
                 str(Path(root) / "state"), "18123", "19000"],
                input=script, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(0, duplicated.returncode, "锚点重复必须失败")
            self.assertIn("config anchor duplicated", duplicated.stderr, "锚点重复必须给出重复原因")

    def test_guide_rpm_override_paths_follow_ch_etc_root(self):
        """5.3 的覆盖目录、文件写入与校验都使用 CH_ETC_ROOT 派生的 CH_RPM_OVERRIDE。"""
        fences = extract_code_fences(self.read_guide())
        self.assertIn(
            'export CH_RPM_OVERRIDE="$CH_ETC_ROOT/clickhouse-server/config.d/00-stage3-yellow.xml"',
            shell_preamble(fences),
            "变量块必须由 CH_ETC_ROOT 派生 CH_RPM_OVERRIDE",
        )
        override_fragment = fence_with(fences, "override.sha256")["body"]
        with tempfile.TemporaryDirectory() as root:
            etc_root = Path(root) / "etc"
            state = Path(root) / "state"
            (state / "clickhouse" / "backup").mkdir(parents=True)
            (state / "clickhouse" / "log").mkdir(parents=True)
            completed = self.run_guide_fragment(override_fragment, state, extra_lines=(
                self.sandbox_sudo_stub(),
                self.loopback_systemctl_stub(Path(root) / "observed-start.txt"),
                self.loopback_ss_stub(),
                "curl() { printf '%s' \"$CH_VERSION\"; }",
                "journalctl() { return 0; }",
            ), env_overrides={"CH_ETC_ROOT": str(etc_root)})
            self.assertEqual(
                0, completed.returncode, f"5.3 覆盖片段执行失败：{completed.stderr.strip()}",
            )
            override = etc_root / "clickhouse-server" / "config.d" / "00-stage3-yellow.xml"
            self.assertTrue(override.is_file(), "覆盖文件必须写入 CH_ETC_ROOT 派生路径")
            self.assertIn(
                "<listen_host>127.0.0.1</listen_host>", override.read_text(encoding="utf-8"),
                "覆盖文件内容必须与指南给出的回环配置一致",
            )
            checksum = (state / "clickhouse" / "backup" / "override.sha256").read_text(encoding="utf-8")
            self.assertIn(str(override), checksum, "覆盖校验清单必须记录派生路径")

    def test_guide_rpm_backup_and_restore_round_trips_real_bytes(self):
        """preexisting=yes 必须先有校验清单，恢复片段真实还原备份字节。"""
        fences = extract_code_fences(self.read_guide())
        backup = fence_with(fences, BACKUP_FENCE_MARKER)["body"]
        cleanup = fence_with(fences, "cleanup assertion failed")["body"]
        self.assertLess(backup.index("sha256sum"), backup.index("preexisting=yes"), "备份清单必须先于 preexisting 标记")
        self.assertIn("preexisting=yes", self.read_guide(), "指南必须记录 preexisting=yes 的处理")
        self.assertIn("preexisting.txt", cleanup, "恢复片段必须读取 preexisting 标记")
        with tempfile.TemporaryDirectory() as root:
            etc_root = Path(root) / "etc"
            (etc_root / "clickhouse-server").mkdir(parents=True)
            original = b"<clickhouse><http_port>8123</http_port></clickhouse>\n"
            (etc_root / "clickhouse-server" / "config.xml").write_bytes(original)
            (etc_root / "clickhouse-server" / "users.xml").write_bytes(b"<clickhouse></clickhouse>\n")
            state = Path(root) / "state"
            (state / "clickhouse" / "run").mkdir(parents=True)
            extra = (
                "export CH_INSTALL_MODE=rpm",
                self.sandbox_sudo_stub(),
                self.loopback_systemctl_stub(Path(root) / "observed-start.txt"),
                self.loopback_ss_stub(),
            )
            etc_environment = {"CH_ETC_ROOT": str(etc_root)}
            prepared = self.run_guide_fragment(
                backup, state, extra_lines=extra, env_overrides=etc_environment,
            )
            self.assertEqual(0, prepared.returncode, f"备份片段执行失败：{prepared.stderr.strip()}")
            backup_dir = state / "clickhouse" / "backup"
            self.assertTrue((backup_dir / "etc-clickhouse-server.tar.gz").is_file(), "preexisting=yes 缺少备份归档")
            self.assertTrue((backup_dir / "etc-clickhouse-server.sha256").is_file(), "preexisting=yes 缺少校验清单")
            self.assertEqual(
                "preexisting=yes\n", (backup_dir / "preexisting.txt").read_text(encoding="utf-8"),
            )

            (etc_root / "clickhouse-server" / "config.xml").write_bytes(b"<clickhouse>installed</clickhouse>\n")
            restored = self.run_guide_fragment(
                cleanup, state, extra_lines=extra, env_overrides=etc_environment,
            )
            self.assertEqual(0, restored.returncode, f"恢复片段执行失败：{restored.stderr.strip()}")
            self.assertEqual(
                original, (etc_root / "clickhouse-server" / "config.xml").read_bytes(),
                "恢复未还原备份字节",
            )
            self.assertFalse(
                (etc_root / "clickhouse-server" / "config.d" / "00-stage3-yellow.xml").exists(),
                "恢复验证的临时覆盖文件必须由 CH_ETC_ROOT 派生路径删除",
            )

        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            etc_root = Path(root) / "etc"
            (etc_root / "clickhouse-server" / "config.d").mkdir(parents=True)
            backup_dir = state / "clickhouse" / "backup"
            backup_dir.mkdir(parents=True)
            (backup_dir / "preexisting.txt").write_text("preexisting=yes\n", encoding="utf-8")
            (backup_dir / "etc-clickhouse-server.tar.gz").write_bytes(b"not-a-real-archive")
            (state / "clickhouse" / "run").mkdir(parents=True)
            incomplete = self.run_guide_fragment(cleanup, state, extra_lines=(
                "export CH_INSTALL_MODE=rpm",
                self.sandbox_sudo_stub(),
                self.loopback_systemctl_stub(Path(root) / "observed-start.txt"),
                self.loopback_ss_stub(),
            ), env_overrides={"CH_ETC_ROOT": str(etc_root)})
            self.assertNotEqual(0, incomplete.returncode, "preexisting=yes 缺少校验清单时必须停止")
            self.assertTrue((state / "clickhouse").is_dir(), "缺少校验清单时不得删除状态目录")

    def test_guide_rpm_restore_verification_stays_on_loopback(self):
        """RPM 恢复验证用临时回环覆盖启动服务，验证回环后停止并删除临时覆盖文件。"""
        fences = extract_code_fences(self.read_guide())
        cleanup = fence_with(fences, "cleanup assertion failed")["body"]
        segment = cleanup[cleanup.index("# 2.3"):]
        self.assertLess(segment.index("systemctl start"), segment.index("assert_ports_loopback_only"), "先启动再验证回环")
        loopback = segment.index("assert_ports_loopback_only")
        self.assertLess(
            loopback, segment.index("cleanup_temporary_rpm_override", loopback),
            "验证回环后调用清理助手停止服务",
        )
        self.assertLess(segment.index("systemctl stop"), segment.index('rm -f -- "$CH_RPM_OVERRIDE"'), "停止后删除临时覆盖文件")
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            (state / "clickhouse" / "backup").mkdir(parents=True)
            (state / "clickhouse" / "backup" / "preexisting.txt").write_text(
                "preexisting=no\n", encoding="utf-8",
            )
            (state / "clickhouse" / "run").mkdir(parents=True)
            observed = Path(root) / "observed-start.txt"
            etc_root = Path(root) / "etc"
            override = etc_root / "clickhouse-server" / "config.d" / "00-stage3-yellow.xml"
            override.parent.mkdir(parents=True)
            override.write_text("<clickhouse></clickhouse>\n", encoding="utf-8")
            completed = self.run_guide_fragment(cleanup, state, extra_lines=(
                "export CH_INSTALL_MODE=rpm",
                self.sandbox_sudo_stub(),
                self.loopback_systemctl_stub(observed),
                self.loopback_ss_stub(),
            ), env_overrides={"CH_ETC_ROOT": str(etc_root)})
            self.assertEqual(0, completed.returncode, f"RPM 恢复验证失败：{completed.stderr.strip()}")
            self.assertEqual(
                "override-present\n", observed.read_text(encoding="utf-8"),
                "启动服务前必须已经写入临时回环覆盖文件",
            )
            self.assertFalse(override.exists(), "临时回环覆盖文件必须在验证后删除")
            self.assertFalse((state / "clickhouse").exists(), "状态目录必须删除")

    def test_guide_rpm_restore_verification_cleans_up_when_service_fails(self):
        """start 或 is-active 失败时必须停服、删除临时覆盖文件，并保留状态目录与失败状态。"""
        cases = {
            "start": self.recording_systemctl_stub(start_status=1, active_status=1),
            "is-active": self.recording_systemctl_stub(start_status=0, active_status=1),
        }
        for label, systemctl_stub in cases.items():
            with self.subTest(service_failure=label), tempfile.TemporaryDirectory() as root:
                result, state, override, observed = self.run_rpm_restore_failure(
                    root, systemctl_stub, self.loopback_ss_stub,
                )
                self.assertNotEqual(0, result.returncode, f"{label} 失败必须保留非零退出状态")
                self.assert_systemctl_stopped_after_start(observed, label)
                self.assertFalse(override.exists(), f"{label} 失败必须删除临时覆盖文件")
                self.assertTrue((state / "clickhouse").is_dir(), f"{label} 失败必须保留状态目录")

    def test_guide_rpm_restore_verification_cleans_up_when_loopback_fails(self):
        """回环断言失败时必须停服、删除临时覆盖文件，并保留状态目录与失败状态。"""
        with tempfile.TemporaryDirectory() as root:
            result, state, override, observed = self.run_rpm_restore_failure(
                root, self.recording_systemctl_stub(start_status=0, active_status=0),
                self.any_address_ss_stub,
            )
            self.assertNotEqual(0, result.returncode, "回环断言失败必须保留非零退出状态")
            self.assert_systemctl_stopped_after_start(observed, "回环断言")
            self.assertFalse(override.exists(), "回环断言失败必须删除临时覆盖文件")
            self.assertTrue((state / "clickhouse").is_dir(), "回环断言失败必须保留状态目录")

    def test_plan_references_existing_experiment_paths(self):
        """实施计划引用的 Stage 3 路径都必须存在。"""
        self.assertTrue(HANDOFF_PLAN.is_file(), f"缺少实施计划：{HANDOFF_PLAN}")
        plan = HANDOFF_PLAN.read_text(encoding="utf-8")
        self.assertNotIn(
            "experiments/json-storage-stage3/README.md", plan, "计划不得引用不存在的 Stage 3 README",
        )
        for target in sorted(set(re.findall(r"`(experiments/json-storage-stage3/[^`]+)`", plan))):
            self.assertTrue((REPOSITORY_ROOT / target).exists(), f"计划引用了不存在的路径：{target}")


if __name__ == "__main__":
    unittest.main()
