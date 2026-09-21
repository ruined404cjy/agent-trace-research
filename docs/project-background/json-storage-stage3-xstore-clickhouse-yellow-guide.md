
# JSON 存储阶段三黄区 XStore/ClickHouse 同机对比指南

> 文档状态：黄区执行指南。配套评估见 [阶段三四布局代表性评估](../json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md)。
>
> 适用环境：EulerOS 2.13 ARM64（aarch64），已部署 XStore，不使用 Docker。
>
> 配套设计：[阶段三黄区 xstore 对比交接设计](../superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md)；配套计划：[阶段三黄区交接实施计划](../superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md)。

本文给出在一台 ARM64 主机上部署 ClickHouse 23.3.10.5、校验 Stage 3 冻结输入、核对 XStore 能力、实现 XStore adapter、执行四布局同机对比并交付证据的完整流程。文档可独立阅读，执行者可以是黄区操作者，也可以是按本文执行的黄区 agent。

## 1. 蓝区事实与黄区目标

### 1.1 蓝区已验证事实

| 项目 | 事实 | 证据 |
|---|---|---|
| 四种布局 | same_table、separate、full_core、asset_ref | [阶段三实验设计](../json-storage/json-storage-stage3-experiment-design-2026-09-09.md) |
| 冻结输入 | 48,534 条事件、190 个写入 block（每 block 256 行）、main cohort 160 条 payload、原始 122.5 MiB、seed 20260907 | generation-manifest.json（status=complete） |
| 输入身份 | identity_sha256 = a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8 | 八 target 共用同一输入身份 |
| 正式结果 | 2 引擎 × 4 布局 × 4 workload 共 32 个 target，四轮 Latin square，正式样本 17,560 个，失败 0 个 | [阶段三实验报告](../json-storage/json-storage-stage3-report-2026-09-20.md) |
| 控制项 | part 状态控制 4 布局 × 4 状态共 5,760 样本；混合负载 243,194 请求；Asset 故障 2 引擎 × 6 用例 | 同上，合并汇总 summary.json 通过全部门禁 |
| 布局空间排序 | ClickHouse 1.000 / 1.097 / 1.170 / 6.921，openGauss 1.000 / 1.530 / 1.586 / 2.884 | 报告第 4.1 节 |
| 现有 engine | 适配器只实现 opengauss 与 clickhouse | [布局矩阵 runner](../../experiments/json-storage-stage3/runner/run_layout_matrix.py) |
| 交付状态 | 分支与归档在蓝区本地生成；GitHub 上的分支和 Release 资产由后续授权的发布动作产生 | 本文第 2 节可用性检查 |

### 1.2 黄区需要采集的事实

黄区在同一台 ARM64 主机、同一冻结输入、同一 Python runner、同一查询参数和同一四轮 Latin square 下补齐 XStore 四布局结果，并与同机 ClickHouse 23.3.10.5 串行对比。以下事实由黄区采集并附证据：

| 类别 | 需要采集的事实 |
|---|---|
| 环境 | EulerOS 版本、aarch64、glibc、CPU、内存、磁盘容量、文件系统类型与挂载选项、时钟、内核限制、端口占用、XStore 进程与服务、客户端可用性、权限级别 |
| 引擎 | XStore 产品与协议版本、服务状态、连接方式；ClickHouse 包身份、安装路径与运行版本 |
| 能力 | SQL 与驱动、DDL/DML、JSON 与长文本类型、事务语义、extension 支持、查询统计、空间统计、后台任务、清理、缓存控制 |
| 输入 | 归档 SHA-256、解包后 identity_sha256、payload 集合身份 |
| 代码 | 分支、HEAD、工作区状态、运行命令 |
| 运行 | 每 target 的 run-manifest.json、真值校验、响应字节、访问路径、维护状态、存储证据、清理证据 |
| 主机 | 执行期间 CPU、内存、磁盘 IO 水位 |

### 1.3 停止条件

本文采用 fail-closed 规则。出现以下任一情况时停止当前阶段，保留已产生的产物，并在回复中记录失败点与已完成子产物：

1. 分支或归档资产不可用。
2. 归档 SHA-256 校验失败，或解包后输入身份与冻结身份不一致。
3. XStore 能力报告的任一必填字段状态为 unverified、状态取值未知，或缺少可执行证据。
4. 代码 HEAD、工作区状态、引擎版本或输入身份无法记录。
5. ClickHouse 未在回环地址就绪，或运行版本与本文不一致且未记录策略原因。
6. 真值、完整 payload bytes、联合水位或清理证据缺失。
7. 执行计划、扫描证据或输出契约与预期不符。
8. namespace、对象目录、后台任务或临时配置无法清理。
9. XStore 构建类型不是 release，或构建类型缺少可执行证据。
10. 6.6 节访问路径门禁未通过，即任一布局的 list、detail 或 trace 查询不走索引访问路径。
11. ClickHouse 四布局矩阵缺失或不完整。
12. 回传摘录未通过 9.2 节校验即执行 9.1 节删除动作。

## 2. 交付身份、全局变量与可用性检查

### 2.1 固定身份

| 项目 | 值 |
|---|---|
| 仓库（SSH） | git@github.com:ruined404cjy/agent-trace-research.git |
| 仓库（HTTPS） | https://github.com/ruined404cjy/agent-trace-research.git |
| 分支 | stage3/xstore-yellow-handoff |
| 基线提交 | 2a7fe245a3cb8843b0e9da77cdf05e290ab96b1b（短写 2a7fe24） |
| 冻结输入归档 | json-storage-stage3-formal-input-20260917.tar.gz |
| 归档校验文件 | json-storage-stage3-formal-input-20260917.tar.gz.sha256 |
| 归档蓝区已验证身份 | SHA-256 47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f，大小 19,313,037 bytes |
| 归档根目录 | json-storage-stage3-formal-input/，内含 events.jsonl、truth.json、generation-manifest.json、payloads/ |

归档由 [冻结输入打包工具](../../experiments/json-storage-stage3/tools/package_formal_input.py) 生成。冻结源目录中的 run-manifest.json 由运行入口的 generate-input 操作写入，记录的是生成运行自身的 operation、command、代码身份与清理结果，不是冻结输入内容；打包工具按 SOURCE_ONLY_FILES 把它保留在源目录，不进入归档。归档只包含 events.jsonl、truth.json、generation-manifest.json 与 payloads/，多出任何其他条目时停止。

### 2.2 全局变量与删除守卫

开始执行前设置以下变量，并把 /path/to 替换为黄区实际路径。变量块以 HTML 注释标记，供文档契约测试作为 shell 前置脚本复用。

变量块定义全部全局变量、删除守卫函数与端口判据函数，但不修改当前目录，也不默认选择安装方式。每个可执行片段都要求这些变量与函数在当前 shell 中已经存在：把本变量块 `source` 一次，或在同一 shell 中粘贴一次，然后执行片段。CH_INSTALL_MODE 在第 5.3 或 5.4 节导出，之后的所有片段都在同一 shell 中继承该取值；新建 shell 时必须重新 source 变量块并按所选路径重新导出 CH_INSTALL_MODE，未设置时 5.7 与 9.1 节直接停止。

<!-- shell-preamble -->
```bash
export YELLOW_WORKSPACE=/path/to/agent-trace-yellow
export YELLOW_STATE="$YELLOW_WORKSPACE/.stage3-yellow"
export YELLOW_REPO="$YELLOW_WORKSPACE/agent-trace-research"
export YELLOW_INPUT="$YELLOW_STATE/input"
export YELLOW_OUTPUT="$YELLOW_STATE/runs"
export YELLOW_RELEASE="$YELLOW_STATE/release"

export YELLOW_REPO_SSH=git@github.com:ruined404cjy/agent-trace-research.git
export YELLOW_REPO_HTTPS=https://github.com/ruined404cjy/agent-trace-research.git
export YELLOW_BRANCH=stage3/xstore-yellow-handoff
export YELLOW_BASE_COMMIT=2a7fe245a3cb8843b0e9da77cdf05e290ab96b1b
export YELLOW_RELEASE_TAG=stage3-formal-input-20260917
export YELLOW_RELEASE_URL="https://github.com/ruined404cjy/agent-trace-research/releases/download/${YELLOW_RELEASE_TAG}"
export YELLOW_ARCHIVE_NAME=json-storage-stage3-formal-input-20260917.tar.gz

export CH_VERSION=23.3.10.5
export CH_RELEASE_TAG=${CH_RELEASE_TAG:-v23.3.10.5-lts}
export CH_RELEASE_URL="https://github.com/ClickHouse/ClickHouse/releases/download/${CH_RELEASE_TAG}"
export CH_TGZ_SUFFIX=${CH_TGZ_SUFFIX:-arm64}
export CH_STATE="$YELLOW_STATE/clickhouse"
export CH_BIN="$CH_STATE/opt/clickhouse-common-static-${CH_VERSION}/usr/bin/clickhouse"
export CH_CONFIG="$CH_STATE/etc/config.xml"
export CH_HTTP_PORT=18123
export CH_TCP_PORT=19000
unset CH_INSTALL_MODE
export CH_ETC_ROOT=${CH_ETC_ROOT:-/etc}
export CH_RPM_OVERRIDE="$CH_ETC_ROOT/clickhouse-server/config.d/00-stage3-yellow.xml"
export CH_PIP_REQUIREMENT='psycopg[binary]==3.3.5'
export PYTHON_BOOTSTRAP=python3
export PYTHON="$YELLOW_STATE/venv/bin/python"

require_guide_path() {
  # 只允许删除本指南创建的状态目录下的路径；空根、相对路径、根目录与目录外路径一律拒绝。
  local target="$1"
  local root="${YELLOW_STATE:-}"
  if [ -z "$root" ] || [ "$root" = "/" ]; then
    printf 'refusing to delete: YELLOW_STATE is empty or root\n' >&2
    return 1
  fi
  if [ -z "$target" ]; then
    printf 'refusing to delete: empty target\n' >&2
    return 1
  fi
  case "$target" in
    /*) ;;
    *) printf 'refusing to delete relative path %s\n' "$target" >&2; return 1 ;;
  esac
  local root_path target_path
  root_path=$(cd -- "$root" 2>/dev/null && pwd -P) || {
    printf 'refusing to delete: YELLOW_STATE does not exist: %s\n' "$root" >&2
    return 1
  }
  target_path=$(cd -- "$target" 2>/dev/null && pwd -P) || {
    printf 'refusing to delete: target does not exist: %s\n' "$target" >&2
    return 1
  }
  case "$target_path" in
    "$root_path"/?*) ;;
    *) printf 'refusing to delete %s outside %s\n' "$target_path" "$root_path" >&2; return 1 ;;
  esac
  printf '%s\n' "$target_path"
  return 0
}

require_asset_workspace_path() {
  # Asset 工作树必须来自 YELLOW_OUTPUT 下的某个输出根，并保持 .asset-work 固定末级目录名。
  local target="$1"
  local target_path output_path
  if [ "${target##*/}" != ".asset-work" ]; then
    printf 'refusing to delete non-asset workspace %s\n' "$target" >&2
    return 1
  fi
  target_path=$(require_guide_path "$target") || return 1
  output_path=$(cd -- "$YELLOW_OUTPUT" 2>/dev/null && pwd -P) || {
    printf 'refusing to delete asset workspace: YELLOW_OUTPUT does not exist: %s\n' "$YELLOW_OUTPUT" >&2
    return 1
  }
  case "$target_path" in
    "$output_path"/?*/.asset-work) ;;
    *) printf 'refusing to delete asset workspace outside output roots: %s\n' "$target_path" >&2; return 1 ;;
  esac
  printf '%s\n' "$target_path"
  return 0
}

require_rpm_override_path() {
  # 只允许操作本指南创建的固定覆盖文件；父目录由 CH_ETC_ROOT 派生，文件名固定。
  local target="${CH_RPM_OVERRIDE:-}"
  case "$target" in
    */clickhouse-server/config.d/00-stage3-yellow.xml) ;;
    *) printf 'refusing to touch unexpected override path %s\n' "$target" >&2; return 1 ;;
  esac
  return 0
}

listening_local_addresses() {
  # 输出监听套接字的本地地址字段。ss 缺失或执行失败时返回 1，调用方不得把失败当作端口空闲。
  local output
  if output=$(ss -ltnH 2>/dev/null); then
    if [ -n "$output" ]; then
      printf '%s\n' "$output" | awk 'NF >= 4 { print $4 }'
    fi
    return 0
  fi
  if output=$(ss -ltn 2>/dev/null); then
    if [ -n "$output" ]; then
      printf '%s\n' "$output" | awk 'NR > 1 && NF >= 4 { print $4 }'
    fi
    return 0
  fi
  printf 'ss is unavailable or failed; listening sockets are unknown\n' >&2
  return 1
}

experiment_listeners() {
  # 只保留实验端口的本地地址；无命中时输出为空，探测失败时返回 1。
  local addresses
  addresses=$(listening_local_addresses) || return 1
  printf '%s\n' "$addresses" | grep -E ":(${CH_HTTP_PORT}|${CH_TCP_PORT})$" || true
}

assert_ports_free() {
  # 任一实验端口仍被监听或探测失败时返回 1，并打印命中的监听地址。
  local hits
  if ! hits=$(experiment_listeners); then
    printf 'port probe failed: listening sockets are unknown; treat as not free\n' >&2
    return 1
  fi
  if [ -n "$hits" ]; then
    printf 'experiment ports are still listening:\n%s\n' "$hits" >&2
    return 1
  fi
  return 0
}

assert_ports_loopback_only() {
  # 两个实验端口都必须存在且只绑定 127.0.0.1；探测失败或判据不满足时返回 1。
  local hits port
  if ! hits=$(experiment_listeners); then
    printf 'port probe failed: listening sockets are unknown; cannot confirm loopback\n' >&2
    return 1
  fi
  if [ -z "$hits" ]; then
    printf 'no listener found on ports %s and %s\n' "$CH_HTTP_PORT" "$CH_TCP_PORT" >&2
    return 1
  fi
  if printf '%s\n' "$hits" | grep -vqE '^127\.0\.0\.1:'; then
    printf 'experiment ports are not loopback-only:\n%s\n' "$hits" >&2
    return 1
  fi
  for port in "$CH_HTTP_PORT" "$CH_TCP_PORT"; do
    if ! printf '%s\n' "$hits" | grep -qE ":$port$"; then
      printf 'missing listener on port %s\n' "$port" >&2
      return 1
    fi
  done
  return 0
}
```

### 2.3 可用性检查

分支与归档资产由发布动作产生。YELLOW_RELEASE_TAG 的取值与发布动作使用的标签一致；执行时把该变量设为实际标签并记入运行记录。以下检查先于一切实验动作；任一检查失败时停止并请求发布。

```bash
mkdir -p "$YELLOW_WORKSPACE" "$YELLOW_STATE" "$YELLOW_RELEASE"

if ! git ls-remote --exit-code --heads "$YELLOW_REPO_HTTPS" "$YELLOW_BRANCH" \
     >"$YELLOW_STATE/branch-check.txt" 2>&1; then
  cat "$YELLOW_STATE/branch-check.txt" >&2
  printf 'branch %s is unavailable; stop here\n' "$YELLOW_BRANCH" >&2
  exit 1
fi
cat "$YELLOW_STATE/branch-check.txt"

for asset in "$YELLOW_ARCHIVE_NAME" "$YELLOW_ARCHIVE_NAME.sha256"; do
  code=$(curl -sSL -o /dev/null --max-time 60 -w '%{http_code}' -r 0-0 \
    "$YELLOW_RELEASE_URL/$asset" 2>/dev/null || true)
  printf '%s %s\n' "$code" "$asset"
  case "$code" in
    200|206) ;;
    *) printf 'release asset %s is unavailable (HTTP %s); stop here\n' "$asset" "$code" >&2; exit 1 ;;
  esac
done
```

## 3. 环境探测

### 3.1 探测命令

把原始探测输出保存在状态目录内，供能力报告和最终报告引用。

```bash
mkdir -p "$YELLOW_STATE/probe"
{
  uname -m
  uname -r
  cat /etc/os-release
  nproc
  lscpu | head -20
  free -m
  df -T "$YELLOW_WORKSPACE"
  df -h "$YELLOW_WORKSPACE"
  findmnt -T "$YELLOW_WORKSPACE"
  date -u
  timedatectl || true
  ulimit -a
  sysctl vm.max_map_count kernel.shmmax kernel.shmall fs.file-max net.core.somaxconn
  ss -ltn
  ps -ef | grep -i -E 'xstore|gauss' | grep -v grep || true
  systemctl list-units --type=service --state=running | grep -i -E 'xstore|gauss' || true
  id
  id -u
  sudo -n true 2>/dev/null && echo 'privilege=sudo' || echo 'privilege=no-sudo'
  command -v gsql || true
  command -v python3 || true
  "$PYTHON_BOOTSTRAP" -c 'import sys; print(sys.version)'
} 2>&1 | tee "$YELLOW_STATE/probe/environment.txt"
```

探测记录必须包含：aarch64 架构、glibc 版本、CPU 核数、可用内存、工作目录所在文件系统类型与挂载选项、剩余容量、UTC 时钟、文件描述符与内存映射上限、已监听端口、XStore 进程与服务状态、可用客户端、当前用户与 sudo 权限。

### 3.2 XStore 能力报告

能力报告是 adapter 实施的前置门禁。报告写入 $YELLOW_STATE/xstore/capability-report.json，每个字段给出实际执行的命令或 API 调用、原始输出摘录和状态。status 取值限定为 available、unavailable、unverified；模板对全部字段默认 unverified 且证据为空，未执行任何验证前不得写成 available。填写时 available 与非空的 evidence.command、evidence.observed 必须同时具备；unavailable 表示已执行验证但能力缺失或不可观测，observed 记录失败输出或缺失原因。汇总器不得估算缺失字段。

```json
{
  "format": "agent-trace-json-storage-stage3-xstore-capability-report",
  "format_version": 1,
  "engine": {"product": "", "version": "", "protocol": "", "evidence": ""},
  "sql_driver": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "ddl_dml": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "json_lob_types": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "transaction": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "extension_support": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "query_statistics": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "storage_accounting": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "background_work": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "cleanup": {"status": "unverified", "evidence": {"command": "", "observed": ""}},
  "cache_control": {"status": "unverified", "evidence": {"command": "", "observed": ""}}
}
```

各字段必须回答的问题：

| 字段 | 必须确认的内容 |
|---|---|
| sql_driver | 可用客户端、连接参数、批量写入接口、参数绑定方式、结果读取方式 |
| ddl_dml | 创建与删除 schema、表、索引的语句形式，写入与更新语句，提交语义 |
| json_lob_types | JSON、长文本、大对象或等价类型的名称与长度上限，内容读取方式 |
| transaction | 显式事务边界、提交与回滚语义、跨表写入的原子性 |
| extension_support | 可用 extension 或进程内对象接口、安装与启用方式、失败与恢复语义 |
| query_statistics | 执行计划、读取行数与扫描字节的获取方式 |
| storage_accounting | 表与索引空间、压缩前后字节、分片或副本状态的查询方式 |
| background_work | 后台合并、压缩、清理任务的状态查询与暂停、恢复动作 |
| cleanup | namespace、对象目录与临时配置的确定性清理方式与确认方式 |
| cache_control | 冷热缓存控制动作及其生效证据 |

报告填写完成后执行下列门禁；门禁输出记入运行记录。十个字段中任一字段为 unverified、状态取值未知，或缺少证据命令与原始输出时，adapter 实施停在门禁之前。

```bash
# 能力报告门禁：status 与证据必须由实际执行结果填充，模板默认值不能通过。
"$PYTHON" - "$YELLOW_STATE/xstore/capability-report.json" > "$YELLOW_STATE/xstore/capability-gate.txt" <<'PY'
import json
import sys
from pathlib import Path

CAPABILITIES = (
    "sql_driver", "ddl_dml", "json_lob_types", "transaction", "extension_support",
    "query_statistics", "storage_accounting", "background_work", "cleanup",
    "cache_control",
)
VALID_STATUSES = ("available", "unavailable")

path = Path(sys.argv[1])
try:
    report = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"capability report is unreadable: {error}")

blocked = []
unavailable = []
for name in CAPABILITIES:
    entry = report.get(name)
    if not isinstance(entry, dict):
        raise SystemExit(f"capability field is missing: {name}")
    evidence = entry.get("evidence")
    status = str(entry.get("status") or "").strip()
    command = str(evidence.get("command") or "").strip() if isinstance(evidence, dict) else ""
    observed = str(evidence.get("observed") or "").strip() if isinstance(evidence, dict) else ""
    if status not in VALID_STATUSES:
        blocked.append(f"{name}={status or 'missing'}")
        continue
    if not command or not observed:
        raise SystemExit(f"capability field lacks evidence command or observed output: {name}")
    if status == "unavailable":
        unavailable.append(f"{name}: {observed}")

if blocked:
    raise SystemExit(
        "capability report fields are unverified or unknown, adapter work is blocked: "
        + ", ".join(sorted(blocked))
    )
engine = report.get("engine")
if not isinstance(engine, dict):
    raise SystemExit("capability report lacks engine identity")
if not str(engine.get("product") or "").strip() or not str(engine.get("version") or "").strip():
    raise SystemExit("capability report lacks engine product or version")

print(f"capability report accepted: available={len(CAPABILITIES) - len(unavailable)} unavailable={len(unavailable)}")
for reason in unavailable:
    print(f"unavailable: {reason}")
PY
gate_status=$?
cat "$YELLOW_STATE/xstore/capability-gate.txt"
if [ "$gate_status" -ne 0 ]; then
  printf 'capability report gate failed; adapter implementation stops\n' >&2
  exit 1
fi
```

### 3.3 Python 运行环境

Stage 3 runner 需要 Python 3.10 及以上（adapter 契约使用 PEP 604 联合类型标注），并需要 psycopg 供 openGauss adapter 的导入链使用，其余代码只用标准库。蓝区实测环境为 Python 3.11.6 与 psycopg 3.3.5，黄区按相同版本对齐。

依赖来源限定为黄区策略批准的 PyPI 镜像；镜像不可用时在蓝区用同一解释器执行 pip download 生成 wheelhouse 并随交接材料传入，黄区用 --no-index --find-links 安装同一 pin 版本。系统 python3 只用于创建虚拟环境，不用于 loader 与 runner。

```bash
"$PYTHON_BOOTSTRAP" -m venv "$YELLOW_STATE/venv"
"$YELLOW_STATE/venv/bin/python" -m pip install --upgrade pip
"$YELLOW_STATE/venv/bin/python" -m pip install "$CH_PIP_REQUIREMENT"
"$YELLOW_STATE/venv/bin/python" -m pip freeze | tee "$YELLOW_STATE/probe/python-freeze.txt"
```

虚拟环境前置检查在创建后立即执行，任一项失败即停止：

```bash
"$YELLOW_STATE/venv/bin/python" - <<'PY'
import importlib
import sys

if sys.version_info < (3, 10):
    raise SystemExit(f"python >= 3.10 required, found {sys.version.split()[0]}")

psycopg = importlib.import_module("psycopg")
if not psycopg.__version__.startswith("3."):
    raise SystemExit(f"psycopg 3.x required, found {psycopg.__version__}")

print(f"python={sys.version.split()[0]} psycopg={psycopg.__version__}")
PY
```

该检查必须输出 python 与 psycopg 版本，版本或依赖不符时停止。runner 导入检查在取得仓库后执行，见 4.1 节；虚拟环境版本与 freeze 清单记入运行记录，供后续复现。

## 4. 仓库与冻结输入获取

### 4.1 获取代码并固定 HEAD

```bash
mkdir -p "$YELLOW_WORKSPACE"
cd "$YELLOW_WORKSPACE"
test -d "$YELLOW_REPO/.git" || git clone "$YELLOW_REPO_HTTPS" "$YELLOW_REPO"
git -C "$YELLOW_REPO" fetch origin "$YELLOW_BRANCH"
git -C "$YELLOW_REPO" checkout --detach FETCH_HEAD

head_commit=$(git -C "$YELLOW_REPO" rev-parse HEAD)
printf 'HEAD=%s\n' "$head_commit"
git -C "$YELLOW_REPO" merge-base --is-ancestor "$YELLOW_BASE_COMMIT" "$head_commit"
test -z "$(git -C "$YELLOW_REPO" status --porcelain)"
git -C "$YELLOW_REPO" log --oneline -3
```

运行记录写明实际 HEAD，分支名只作为查找入口。基线提交 2a7fe245a3cb8843b0e9da77cdf05e290ab96b1b 是所有结果的祖先提交。

仓库就绪后立即执行 runner 导入检查：

```bash
cd "$YELLOW_REPO"
"$PYTHON" - <<'PY'
import importlib
import sys

sys.path.insert(0, "experiments/json-storage-stage3/runner")
production = importlib.import_module("production")
if not callable(getattr(production, "load_formal_input", None)):
    raise SystemExit("load_formal_input is unavailable")

psycopg = importlib.import_module("psycopg")
print(f"python={sys.version.split()[0]} psycopg={psycopg.__version__} runner=importable")
PY
```

该检查必须输出 runner=importable。缺少 psycopg 时 production 的导入链（production → opengauss → psycopg）会失败，属于停止条件：不进入 4.3 节的冻结身份校验，也不执行任何 runner 命令。

### 4.2 下载并校验冻结输入

```bash
mkdir -p "$YELLOW_RELEASE" "$YELLOW_INPUT"
for asset in "$YELLOW_ARCHIVE_NAME" "$YELLOW_ARCHIVE_NAME.sha256"; do
  curl -sSL --max-time 3600 -o "$YELLOW_RELEASE/$asset" "$YELLOW_RELEASE_URL/$asset"
done

cd "$YELLOW_RELEASE"
sha256sum -c "$YELLOW_ARCHIVE_NAME.sha256"
sha256sum "$YELLOW_ARCHIVE_NAME"
stat -c '%s %n' "$YELLOW_ARCHIVE_NAME"
```

sha256sum -c 必须输出 OK。归档大小与 SHA-256 同蓝区记录 19,313,037 bytes 与 47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f 不一致时停止执行，并先确认资产来源。

### 4.3 解包并校验冻结身份

```bash
tar -xzf "$YELLOW_RELEASE/$YELLOW_ARCHIVE_NAME" -C "$YELLOW_INPUT"
ls -l "$YELLOW_INPUT/json-storage-stage3-formal-input"
```

解包后调用正式 loader 复核输入契约，并记录冻结身份：

```bash
cd "$YELLOW_REPO"
"$PYTHON" - "$YELLOW_INPUT/json-storage-stage3-formal-input" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path("experiments/json-storage-stage3/runner").resolve()))
from production import load_formal_input

formal = load_formal_input(Path(sys.argv[1]))
print(json.dumps({
    "record_count": formal.truth.record_count,
    "block_count": formal.truth.block_count,
    "block_size": formal.truth.block_size,
    "payload_count": len(formal.truth.payloads),
    "identity_sha256": formal.truth.identity_sha256,
    "input_kind": formal.identity.get("kind"),
    "main_blocks": len(formal.main_blocks),
    "main_queries": len(formal.main_queries),
}, ensure_ascii=False, sort_keys=True))
PY
```

输出中的 identity_sha256 必须等于 a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8，record_count 必须为 48,534，block_count 必须为 190，block_size 必须为 256。任何一项不一致时停止。

## 5. ClickHouse ARM64 部署

### 5.1 版本选择、实测边界与资产

目标主机为 Kunpeng 920，CPU 不提供 SVE 指令集。ClickHouse 官方 ARM64 产物自 23.8 起在该芯片上启动即 Illegal instruction。逐版本实测结果如下。

| 版本 | Kunpeng 920 启动结果 |
|---|---|
| 22.8.21.38 | 正常 |
| 23.3.10.5 | 正常 |
| 23.8.15.35 | SIGILL，Illegal instruction |
| 24.3.18.7 | SIGILL，Illegal instruction |
| 25.3、25.12 | SIGILL，Illegal instruction |

本指南固定 CH_VERSION=23.3.10.5，即实测可启动的最高版本。更换主机型号时先按本表逐版本复测，把实际结果记入运行记录，再确定版本。黄区安全策略不允许使用该版本时，改用策略批准且实测可启动的 ARM64 版本，并在运行记录中写明策略依据、包名与运行版本。

蓝区正式结果使用 ClickHouse 25.12.11.4。两个版本的差异范围如下。

存储维度跨版本一致。四布局库内压缩字节的对照值：

| 布局 | 蓝区 25.12 | 黄区 23.3 实测 | 偏差 |
|---|---:|---:|---:|
| same_table | 19,035,002 | 19,017,603 至 19,023,364 | 0.06% 至 0.09% |
| separate | 20,890,688 | 20,890,114 | 0.003% |
| full_core | 22,271,831 | 22,256,332 | 0.07% |
| asset_ref 库内 | 3,283,393 | 3,278,565 | 0.15% |

黄区实测值同时来自两台主机、两种 part 状态，四个布局的空间排序与蓝区一致。该表也是黄区重跑后的期望值：偏差超过 1% 时先核对冻结输入身份与建表 DDL，再继续。

写入保护阈值不同。23.3 的 parts_to_delay_insert 与 parts_to_throw_insert 默认值为 150 与 300，25.12 为 1000 与 3000。碎片态控制在暂停 merge 后连续写入 190 个 block，低阈值会触发写入延迟。5.6 节把两项显式设为 1000 与 3000，消除该差异。

其余执行期差异未逐项验证。黄区 ClickHouse 数值不与蓝区 ClickHouse 数值直接比较，报告按 8.3 节记录版本差异，黄区结论限定为同机 XStore 与 ClickHouse 的对比。

资产文件名由 CH_VERSION 与 CH_TGZ_SUFFIX 派生。不同 Release 的 ARM64 归档后缀取值不同，以 Release 页面实际列出为准。

| 资产 | 文件名 | 用途 |
|---|---|---|
| common-static | clickhouse-common-static-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz | 可执行文件与运行时资源，解包后二进制位于 clickhouse-common-static-${CH_VERSION}/usr/bin/clickhouse |
| server | clickhouse-server-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz | etc/clickhouse-server/config.xml 与 users.xml 模板 |
| client | clickhouse-client-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz | 客户端与格式化工具符号链接 |

RPM 路径使用同一 Release 的 AArch64 包：clickhouse-common-static-${CH_VERSION}.aarch64.rpm、clickhouse-server-${CH_VERSION}.aarch64.rpm、clickhouse-client-${CH_VERSION}.aarch64.rpm。

Release 页面：

```text
https://github.com/ClickHouse/ClickHouse/releases/tag/${CH_RELEASE_TAG}
```

CH_RELEASE_TAG 的后缀按 Release 类型取值：stable 版本为 -stable，LTS 版本为 -lts。23.3 是 LTS 版本，默认值为 v23.3.10.5-lts；与 Release 页面不一致时以页面为准，并把实际取值记入运行记录。

黄区主机不能直连外网时，在可联网主机下载全部资产与摘要文件，传输到目标主机的 $CH_STATE/pkg 目录，再执行 5.2 节。5.2 节在文件已存在时跳过下载，校验步骤始终执行。实际使用的文件名与 SHA-256 写入 $CH_STATE/pkg/package-names.txt，作为包身份证据。


### 5.2 下载与 SHA-512 校验

```bash
mkdir -p "$CH_STATE/opt" "$CH_STATE/pkg" "$CH_STATE/log" "$CH_STATE/run"
cd "$CH_STATE/pkg"

for name in clickhouse-common-static clickhouse-server clickhouse-client; do
  file="${name}-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz"
  # 离线主机预先放置文件；已存在即跳过下载，校验步骤始终执行。
  if [ ! -s "$file" ]; then
    curl -sSL --max-time 3600 -o "$file" "$CH_RELEASE_URL/$file"
  fi
  if [ ! -s "$file.sha512" ]; then
    curl -sSL --max-time 300 -o "$file.sha512" "$CH_RELEASE_URL/$file.sha512"
  fi
  test -s "$file" || { printf 'missing package: %s\n' "$file" >&2; exit 1; }
  test -s "$file.sha512" || { printf 'missing checksum: %s\n' "$file.sha512" >&2; exit 1; }
  sha512sum -c "$file.sha512"
done

sha512sum clickhouse-common-static-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz \
  clickhouse-server-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz \
  clickhouse-client-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz | tee "$CH_STATE/pkg/package-sha512.txt"

sha256sum clickhouse-*-${CH_VERSION}-${CH_TGZ_SUFFIX}.tgz | tee "$CH_STATE/pkg/package-names.txt"
```

sha512sum -c 对三个包都必须输出 OK。摘要文件使用标准校验格式（摘要、两个空格、文件名），在校验目录内直接执行 sha512sum -c 即可。package-names.txt 记录实际文件名与 SHA-256，是 6.4 节包身份证据的来源。

### 5.3 root 与 RPM 权限路径

RPM 路径与 TGZ 路径互斥：每台主机只选一条路径，并把 CH_INSTALL_MODE 设为 rpm 或 tgz 记入运行记录。两条路径使用同一 HTTP 端口 18123 与原生端口 19000，同时安装会互相冲突。

系统配置根目录由 CH_ETC_ROOT 指定，默认 /etc；下表路径按该默认值列出，改写 CH_ETC_ROOT 时表中路径相应替换。

RPM 路径写入系统位置。按 rpm -qlp 核对，clickhouse-server-${CH_VERSION}.aarch64.rpm 与同批包安装后涉及的路径如下。

| 路径 | 归属 |
|---|---|
| /etc/clickhouse-server/config.xml、/etc/clickhouse-server/users.xml | 包安装的默认配置 |
| /etc/clickhouse-server/config.d/00-stage3-yellow.xml | 本指南创建的回环与端口覆盖 |
| /lib/systemd/system/clickhouse-server.service | systemd 单元 |
| /usr/bin/clickhouse、/usr/bin/clickhouse-server、/usr/bin/clickhouse-client | 多调用二进制与符号链接 |
| /var/lib/clickhouse、/var/log/clickhouse-server | 包创建的数据与日志目录 |

回滚范围覆盖指南创建项与既有系统配置：停止服务、删除覆盖文件、在安装前已有既有配置时恢复备份的 $CH_ETC_ROOT/clickhouse-server、重新启动并验证。包创建的数据目录保留，实验数据由 runner 的按 namespace DROP DATABASE 清理；只有在操作者明确决定该主机不再保留 ClickHouse 时，才执行包移除与数据目录处理。

备份既有系统配置。备份在包安装前执行，归档的是 $CH_ETC_ROOT/clickhouse-server 中安装前已有的内容；该目录在安装前不存在时只记录 preexisting=no，不产生归档。恢复只适用于 preexisting=yes：归档与校验清单必须同时存在，清单缺失时第 9.1 节停止，不执行恢复，也不删除状态目录。

```bash
# 5.3 备份既有系统配置
export CH_INSTALL_MODE=rpm
mkdir -p "$CH_STATE/backup"
if [ -d "$CH_ETC_ROOT/clickhouse-server" ]; then
  sudo tar -czf "$CH_STATE/backup/etc-clickhouse-server.tar.gz" -C "$CH_ETC_ROOT" clickhouse-server
  sha256sum "$CH_STATE/backup/etc-clickhouse-server.tar.gz" \
    | tee "$CH_STATE/backup/etc-clickhouse-server.sha256"
  test -s "$CH_STATE/backup/etc-clickhouse-server.sha256" || {
    printf 'configuration backup checksum is missing; stop here\n' >&2
    exit 1
  }
  printf 'preexisting=yes\n' | tee "$CH_STATE/backup/preexisting.txt"
else
  printf 'preexisting=no\n' | tee "$CH_STATE/backup/preexisting.txt"
fi
```

下载与安装：

```bash
cd "$CH_STATE/pkg"
for name in clickhouse-common-static clickhouse-server clickhouse-client; do
  file="${name}-${CH_VERSION}.aarch64.rpm"
  curl -sSL --max-time 3600 -o "$file" "$CH_RELEASE_URL/$file"
done

sha256sum clickhouse-*.aarch64.rpm | tee "$CH_STATE/pkg/package-sha256.txt"
rpm -K clickhouse-common-static-${CH_VERSION}.aarch64.rpm \
  clickhouse-server-${CH_VERSION}.aarch64.rpm \
  clickhouse-client-${CH_VERSION}.aarch64.rpm | tee "$CH_STATE/pkg/rpm-signature.txt"

sudo dnf install -y ./clickhouse-common-static-${CH_VERSION}.aarch64.rpm \
  ./clickhouse-server-${CH_VERSION}.aarch64.rpm \
  ./clickhouse-client-${CH_VERSION}.aarch64.rpm
command -v clickhouse clickhouse-server clickhouse-client
```

RPM 资产没有随 Release 提供 SHA-512 摘要，该路径记录包 SHA-256 与 rpm -K 输出，并以服务端 SELECT version() 返回值作为运行版本证据；需要摘要级校验时使用 5.4 节的 TGZ 路径。rpm -K 的验收语义：摘要行必须为 digests OK；出现 NOKEY 表示本机缺少发布者公钥，该行允许存在，前提是摘要行仍为 OK 且操作者把 NOKEY 状态记入运行记录；出现 BAD、NOT OK 或 MISSING KEYS 时停止并重新下载。

回环覆盖与服务验证：

```bash
sudo install -d -m 0755 "$(dirname "$CH_RPM_OVERRIDE")"
sudo tee "$CH_RPM_OVERRIDE" >/dev/null <<'XML'
<clickhouse>
    <listen_host>127.0.0.1</listen_host>
    <http_port>18123</http_port>
    <tcp_port>19000</tcp_port>
    <merge_tree>
        <parts_to_delay_insert>1000</parts_to_delay_insert>
        <parts_to_throw_insert>3000</parts_to_throw_insert>
    </merge_tree>
    <max_server_memory_usage_to_ram_ratio>0.5</max_server_memory_usage_to_ram_ratio>
</clickhouse>
XML
sudo sha256sum "$CH_RPM_OVERRIDE" \
  | tee "$CH_STATE/backup/override.sha256"

sudo systemctl start clickhouse-server
sudo systemctl is-active clickhouse-server

ready=0
for attempt in $(seq 1 60); do
  version=$(curl -sS --max-time 3 "http://127.0.0.1:18123/?query=SELECT%20version()" 2>/dev/null || true)
  if [ "$version" = "$CH_VERSION" ]; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1

assert_ports_loopback_only || exit 1
sudo journalctl -u clickhouse-server --no-pager -n 100 | tee "$CH_STATE/log/journal.txt"
if grep -En '<Error>|<Fatal>' "$CH_STATE/log/journal.txt"; then
  printf 'clickhouse journal contains <Error> or <Fatal>; stop here\n' >&2
  exit 1
fi
```

服务验证判据：systemctl is-active 输出 active，curl 返回 $CH_VERSION，18123 与 19000 只出现在 127.0.0.1 上，journal 中无 <Error> 或 <Fatal>。

回滚与清理：RPM 路径的回滚命令集中在 9.1 节，删除本指南创建的固定覆盖文件、按 preexisting 标记恢复既有系统配置、验证服务与配置状态，并断言覆盖文件不再存在。安装验证后需要立即回滚时执行同一段命令。

包移除只在操作者明确决定该主机不再保留 ClickHouse 时执行，范围限定为三个包：

```bash
sudo dnf remove -y clickhouse-server clickhouse-client clickhouse-common-static
```

/var/lib/clickhouse 与 /var/log/clickhouse-server 属于包创建目录，本指南的自动清理不删除它们；需要清理时由操作者单独确认并记录。

### 5.4 无 root 时的 TGZ 独立运行路径

该路径在用户目录内运行独立的 server、client、data、log 与配置文件，不写入系统目录。

```bash
export CH_INSTALL_MODE=tgz
mkdir -p "$CH_STATE/opt" "$CH_STATE/etc"
cd "$CH_STATE/pkg"
for name in clickhouse-common-static clickhouse-server clickhouse-client; do
  tar -xzf "${name}-${CH_VERSION}-arm64.tgz" -C "$CH_STATE/opt"
done
test -x "$CH_BIN"

cp "$CH_STATE/opt/clickhouse-server-${CH_VERSION}/etc/clickhouse-server/config.xml" \
  "$CH_STATE/etc/config.xml.template"
cp "$CH_STATE/opt/clickhouse-server-${CH_VERSION}/etc/clickhouse-server/users.xml" \
  "$CH_STATE/etc/users.xml"

mkdir -p "$CH_STATE/data" "$CH_STATE/tmp" "$CH_STATE/user_files" \
  "$CH_STATE/format_schemas" "$CH_STATE/caches" "$CH_STATE/access" "$CH_STATE/log"

sha256sum "$CH_BIN" | tee "$CH_STATE/pkg/clickhouse-binary-sha256.txt"
```

包内自带配置指向 /var/lib/clickhouse 与 /var/log/clickhouse-server。下面的脚本把路径、端口与监听地址改写为状态目录内的隔离值，并在写入前逐项断言，任一项不符时脚本以非零状态退出。脚本锚点取自 ClickHouse 23.3.10.5 包内 ARM64 etc/clickhouse-server/config.xml 的原文；更换版本或更换发行包时先核对锚点文本，锚点缺失与锚点重复会分别以 config anchor not found 与 config anchor duplicated 失败。

```bash
"$PYTHON" - \
  "$CH_STATE/etc/config.xml.template" \
  "$CH_STATE/etc/config.xml" \
  "$CH_STATE" "$CH_HTTP_PORT" "$CH_TCP_PORT" <<'PY'
import hashlib
import pathlib
import sys
import xml.etree.ElementTree as ElementTree

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
state = sys.argv[3]
http_port = sys.argv[4]
tcp_port = sys.argv[5]
text = source.read_text(encoding="utf-8")


def replace(old, new):
    """执行一次唯一字符串替换；锚点未命中或重复命中时分别失败。"""
    count = text.count(old)
    if count == 0:
        raise SystemExit(f"config anchor not found: {old}")
    if count != 1:
        raise SystemExit(f"config anchor duplicated ({count}): {old}")
    return text.replace(old, new, 1)


text = replace("<log>/var/log/clickhouse-server/clickhouse-server.log</log>",
               f"<log>{state}/log/clickhouse-server.log</log>")
text = replace("<errorlog>/var/log/clickhouse-server/clickhouse-server.err.log</errorlog>",
               f"<errorlog>{state}/log/clickhouse-server.err.log</errorlog>")
text = replace("<custom_cached_disks_base_directory>/var/lib/clickhouse/caches/</custom_cached_disks_base_directory>",
               f"<custom_cached_disks_base_directory>{state}/caches/</custom_cached_disks_base_directory>")
text = replace("<path>/var/lib/clickhouse/</path>", f"<path>{state}/data/</path>")
text = replace("<tmp_path>/var/lib/clickhouse/tmp/</tmp_path>", f"<tmp_path>{state}/tmp/</tmp_path>")
text = replace("<user_files_path>/var/lib/clickhouse/user_files/</user_files_path>",
               f"<user_files_path>{state}/user_files/</user_files_path>")
text = replace("<format_schema_path>/var/lib/clickhouse/format_schemas/</format_schema_path>",
               f"<format_schema_path>{state}/format_schemas/</format_schema_path>")
text = replace("<path>users.xml</path>", f"<path>{state}/etc/users.xml</path>")
text = replace("<path>/var/lib/clickhouse/access/</path>", f"<path>{state}/access/</path>")
text = replace("<http_port>8123</http_port>",
               f"<listen_host>127.0.0.1</listen_host>\n    <http_port>{http_port}</http_port>")
text = replace("<tcp_port>9000</tcp_port>", f"<tcp_port>{tcp_port}</tcp_port>")
# 包内已有生效的 ratio 元素，按值替换保持唯一；max_server_memory_usage 保留包默认 0（自动）。
text = replace("<max_server_memory_usage_to_ram_ratio>0.9</max_server_memory_usage_to_ram_ratio>",
               "<max_server_memory_usage_to_ram_ratio>0.5</max_server_memory_usage_to_ram_ratio>")
# 5.6 节的写入保护阈值：包内配置没有 merge_tree 元素，按根闭合标签插入，保留原文注释与格式。
text = replace("</clickhouse>",
               "    <merge_tree>\n"
               "        <parts_to_delay_insert>1000</parts_to_delay_insert>\n"
               "        <parts_to_throw_insert>3000</parts_to_throw_insert>\n"
               "    </merge_tree>\n"
               "</clickhouse>")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(text, encoding="utf-8")

root = ElementTree.parse(destination).getroot()


def expect(path, value):
    """断言生效配置值，避免改写结果与预期不一致。"""
    actual = root.findtext(path)
    if actual != value:
        raise SystemExit(f"config value mismatch at {path}: {actual!r}")


expect("logger/log", f"{state}/log/clickhouse-server.log")
expect("logger/errorlog", f"{state}/log/clickhouse-server.err.log")
expect("path", f"{state}/data/")
expect("tmp_path", f"{state}/tmp/")
expect("user_files_path", f"{state}/user_files/")
expect("format_schema_path", f"{state}/format_schemas/")
expect("custom_cached_disks_base_directory", f"{state}/caches/")
expect("max_server_memory_usage", "0")
expect("max_server_memory_usage_to_ram_ratio", "0.5")
expect("merge_tree/parts_to_delay_insert", "1000")
expect("merge_tree/parts_to_throw_insert", "3000")
expect("http_port", http_port)
expect("tcp_port", tcp_port)
expect("listen_host", "127.0.0.1")
expect("user_directories/users_xml/path", f"{state}/etc/users.xml")
expect("user_directories/local_directory/path", f"{state}/access/")

ratios = root.findall("max_server_memory_usage_to_ram_ratio")
if len(ratios) != 1 or (ratios[0].text or "").strip() != "0.5":
    raise SystemExit("max_server_memory_usage_to_ram_ratio must be the only active ratio element with value 0.5")

listeners = root.findall("listen_host")
if len(listeners) != 1 or (listeners[0].text or "").strip() != "127.0.0.1":
    raise SystemExit("listen_host must be the only active listener and bound to 127.0.0.1")

digest = hashlib.sha256(destination.read_bytes()).hexdigest()
print(f"config={destination} sha256={digest}")
PY
```

改写后的配置只监听 127.0.0.1，HTTP 端口 18123 与 runner 默认端点一致，原生协议端口 19000 用于客户端查询。实验期间不得把监听地址改成对外地址。

包内 config.xml 的 listen_host 行全部处于注释状态（含 IPv6 回环 ::1 与 127.0.0.1 两个示例），脚本插入一条生效的 127.0.0.1，因此只监听 IPv4 回环，IPv6 回环与其他网络接口都不监听；生效 listen_host 数量为 1 由脚本断言。多盘示例块内的 /data/ 与 blob 元数据路径同样位于注释内，不参与生效配置。

### 5.5 启动、健康检查与版本证据

```bash
cd "$CH_STATE"
nohup "$CH_BIN" server --config-file="$CH_CONFIG" \
  >"$CH_STATE/log/server-console.log" 2>&1 &
echo $! >"$CH_STATE/run/clickhouse.pid"

ready=0
for attempt in $(seq 1 60); do
  version=$(curl -sS --max-time 3 "http://127.0.0.1:${CH_HTTP_PORT}/?query=SELECT%20version()" 2>/dev/null || true)
  if [ "$version" = "$CH_VERSION" ]; then
    ready=1
    break
  fi
  sleep 2
done
test "$ready" = 1

curl -sS "http://127.0.0.1:${CH_HTTP_PORT}/?query=SELECT%20version()"
"$CH_BIN" client --host 127.0.0.1 --port "$CH_TCP_PORT" --query 'SELECT version()'
```

健康判据：curl 与客户端都返回 $CH_VERSION，18123 与 19000 只出现在 127.0.0.1 上，启动日志不含 <Error> 或 <Fatal> 标记。启动失败、版本不一致或出现上述标记时保留日志并停止，不进入 adapter 与实验阶段。

```bash
assert_ports_loopback_only || exit 1
experiment_listeners

grep -En '<Error>|<Fatal>' "$CH_STATE/log/server-console.log" \
  "$CH_STATE/log/clickhouse-server.log" | tee "$CH_STATE/log/error-scan.txt" || true
if [ -s "$CH_STATE/log/error-scan.txt" ]; then
  printf 'clickhouse startup log contains <Error> or <Fatal>; stop here\n' >&2
  exit 1
fi
```

### 5.6 MergeTree 与内存参数对齐

以下三项在两台主机、两条安装路径上取同一值，消除版本默认值与主机内存规模带来的差异。

| 参数 | 取值 | 依据 |
|---|---|---|
| parts_to_delay_insert | 1000 | 23.3 默认 150，25.12 默认 1000；碎片态控制在暂停 merge 后写入 190 个 block |
| parts_to_throw_insert | 3000 | 23.3 默认 300，25.12 默认 3000 |
| max_server_memory_usage_to_ram_ratio | 0.5 | 两台主机物理内存相差四倍以上，按比例取同一值 |

RPM 路径把下列片段并入 $CH_RPM_OVERRIDE，TGZ 路径由 5.4 节的配置改写脚本写入 $CH_CONFIG。

```xml
<clickhouse>
    <merge_tree>
        <parts_to_delay_insert>1000</parts_to_delay_insert>
        <parts_to_throw_insert>3000</parts_to_throw_insert>
    </merge_tree>
    <max_server_memory_usage_to_ram_ratio>0.5</max_server_memory_usage_to_ram_ratio>
</clickhouse>
```

服务就绪后核对生效值，两行都必须与上表一致，不一致时停止并修正配置：

```bash
"$CH_BIN" client --host 127.0.0.1 --port "$CH_TCP_PORT" --query "
SELECT name, value FROM system.merge_tree_settings
WHERE name IN ('parts_to_delay_insert','parts_to_throw_insert')
ORDER BY name FORMAT TSV" | tee "$CH_STATE/merge-tree-settings.txt"

grep -E '1000|3000' "$CH_STATE/merge-tree-settings.txt" | wc -l | grep -qx 2 || {
  printf 'merge_tree thresholds not aligned; stop here\n' >&2
  exit 1
}
```

内存比例以 5.4 节配置断言的生效值为准；system.server_settings 在该版本可用时补记一行实测值，不可用时记 unavailable。

### 5.7 停止服务

暂停或结束实验时先停止引擎，再收集证据。本节不删除目录；目录、覆盖配置与备份的清理在第 9.1 节执行，重复执行保持幂等。

```bash
# 5.7 停止服务：先确定安装模式，未设置或非 rpm/tgz 时停止，不执行任何停止动作。
case "${CH_INSTALL_MODE:-}" in
  rpm|tgz) ;;
  *)
    printf 'CH_INSTALL_MODE must be rpm or tgz, found %s; stop here\n' "${CH_INSTALL_MODE:-unset}" >&2
    exit 1
    ;;
esac

if [ "$CH_INSTALL_MODE" = "rpm" ]; then
  sudo systemctl stop clickhouse-server || true
  if sudo systemctl is-active --quiet clickhouse-server; then
    printf 'clickhouse-server is still active after stop; stop here\n' >&2
    exit 1
  fi
else
  if [ -f "$CH_STATE/run/clickhouse.pid" ]; then
    kill "$(cat "$CH_STATE/run/clickhouse.pid")"
    for attempt in $(seq 1 30); do
      kill -0 "$(cat "$CH_STATE/run/clickhouse.pid")" 2>/dev/null || break
      sleep 1
    done
  fi
fi
assert_ports_free || exit 1
```

停止与确认完成后按第 9.1 节执行清理；清理片段可重复执行，已经缺失的目录按已清理处理。

## 6. XStore adapter 实施门禁

### 6.1 实施前置条件

在能力报告补齐前停止 adapter 实施。进入实施阶段需要同时满足：能力报告通过 3.2 节门禁，即十个字段的 status 都为 available 或 unavailable，且每个字段都有非空 evidence.command 与非空 evidence.observed；XStore 的 namespace 创建与删除、批量写入、提交、读取、执行统计、空间统计与后台任务接口已经用实际命令或 API 验证；冻结输入与 ClickHouse 侧已经完成一次单布局 smoke。出现 unverified、未知状态或缺失证据时停止，不进入 adapter 代码改动。

adapter 实现并测试通过后，主矩阵之外的控制项按 7.1 节阶段 6 至 8 逐项执行：part-state 可适用项、混合负载与 Asset 故障恢复。任一控制项不适用时，在运行记录中引用能力报告的对应条目写明原因，并把结论标为不适用；未记录不适用与证据前不声明黄区对比完成。

实现范围限定为新增 experiments/json-storage-stage3/runner/xstore.py、对应单元测试、endpoint 配置和汇总器的 xstore 分支。现有 openGauss 与 ClickHouse adapter、runner 语义、汇总器既有的四布局口径保持不变。

### 6.2 LayoutAdapter 方法到证据的映射

XStore adapter 实现 [common.py](../../experiments/json-storage-stage3/runner/common.py) 中 LayoutAdapter 定义的等价能力：

| 方法 | 黄区需要提供的证据 |
|---|---|
| create() | namespace 与表的创建 DDL、DDL 文本、创建后对象清单，以及重复创建的拒绝行为 |
| ingest_block(block) | 256 行 block 写入接口、提交返回值、BlockResult 的 rows、watermark、watermarks、wall_ms、写目标耗时与提交字节 |
| ingest_failure_evidence() | 失败 block 已提交的请求字节，或 unavailable 与原因 |
| wait_write_complete(watermark) | 联合水位查询命令、可见性判定与 completed 结果 |
| wait_query_ready(timeout_seconds) | 查询就绪判定命令、超时行为与最终水位 |
| get_available(asset_id) | 单行 catalog 读取接口与状态判定 |
| run_query(query) | list、preview、detail、trace、batch 五类查询的 SQL、完整结果读取与 QueryResult 字段 |
| collect_storage() | 表与索引空间、压缩前后字节、分片或副本状态、后台任务状态 |
| collect_access_evidence(query_ids) | 执行计划、读取行数、扫描字节与查询完成时刻证据 |
| audit_dataset() | 目标行集合对账、等价性判定与差异明细 |
| cleanup() | namespace、对象目录与临时配置的删除动作及删除后确认 |

以下契约在 XStore 侧按同一口径保留：

1. 真值校验：客户端长度、SHA-256 与 truth 行对比必须完成。
2. 响应字节：记录数据库与 resolver 分项、总响应字节与提交字节。
3. 访问路径：记录每种查询的实际访问结构与扫描量。
4. 存储证据：记录表与索引空间、压缩前后字节与后台任务状态。
5. 水位：分表与双写布局给出联合水位，且每个水位目标等于块边界。
6. 失败证据：失败运行记录错误、已完成子产物与清理结果。
7. 清理：命名空间与对象目录在运行后确定性删除并确认。

### 6.3 接入 runner、工厂与汇总器

接入顺序固定为：先补齐单元测试与集成 smoke，再进入正式执行。

1. runner：在 [run_layout_matrix.py](../../experiments/json-storage-stage3/runner/run_layout_matrix.py) 的 engine 校验、adapter 构造与端点参数中加入 xstore，并新增测试覆盖端点解析与 engine 拒绝行为。
2. 工厂：[production.py](../../experiments/json-storage-stage3/runner/production.py) 的 create_adapter 与 EngineEndpoints 增加 xstore 端点，保持未实现 engine 的拒绝行为。
3. 运行入口：[run_stage3.py](../../experiments/json-storage-stage3/runner/run_stage3.py) 的 candidate、part-states、interference、asset-failures 操作按引擎扩展，或在 xstore 不适用时在运行清单中记录不适用与原因。
4. 汇总器：[summarize.py](../../experiments/json-storage-stage3/report/summarize.py) 增加 xstore 分支，保持既有 workload、provenance、访问路径、维护状态与清理门禁不变。
5. 引擎身份证据：无 Docker 的 ClickHouse 与 XStore 都缺少容器镜像摘要，接入前先按 6.4 节实现原生包引擎身份证据并补测试。

### 6.4 原生包引擎身份契约

现有 run manifest 在写入 container 字段时对 engine 为 opengauss 与 clickhouse 的目标调用 docker inspect，缺少镜像摘要即报 container image digest evidence is missing。黄区不使用 Docker，该门禁必然失败，因此 ClickHouse 与 XStore 都要先完成同一项代码改动并通过测试，才能进入 7.1 节第 3 阶段的单 target candidate。改动完成前，现有 runner 不支持无 Docker 的本机引擎。

引擎身份证据改为原生包身份，字段与来源如下。

| 字段 | ClickHouse TGZ 路径 | ClickHouse RPM 路径 | XStore |
|---|---|---|---|
| engine | clickhouse | clickhouse | xstore |
| engine_version | 服务端 SELECT version() | 服务端 SELECT version() | 能力报告的版本查询 |
| product_path | $CH_BIN 的解包路径 | /usr/bin/clickhouse | 能力报告的安装路径 |
| package_files | 三个 tgz 文件名 | 三个 rpm 文件名 | 能力报告的安装包清单 |
| package_checksums | 三个 .sha512 校验结果 | 包 SHA-256 与 rpm -K 结果 | 能力报告的校验方式与结果 |
| binary_sha256 | sha256sum "$CH_BIN" | sha256sum /usr/bin/clickhouse | 能力报告的二进制或等价身份 |
| config_identity | config.xml 路径与 SHA-256 | config.d 覆盖文件路径与 SHA-256 | 能力报告的配置身份 |
| service_identity | PID 文件与启动命令 | systemd 单元与 is-active 结果 | 能力报告的服务与进程状态 |
| host | uname -n 与 uname -m | 同左 | 同左 |

改动范围与验收：runner 的 container 证据路径按 engine 分流，容器引擎保持原行为，原生包引擎写入上表字段；缺少 engine_version、package_checksums 或 binary_sha256 时同样拒绝发布 complete manifest。该改动需要单元测试覆盖两类引擎的分流与缺字段拒绝，并在正式矩阵前完成一次单 target candidate 验证。

### 6.5 布局边界

asset_ref 保留为应用侧参考布局：查询引用与 assets 记录，再由应用侧 resolver 读取本地内容寻址目录，两引擎使用同一实现。XStore extension 或进程内对象读取属于数据库内调度，定义为第五个候选布局 db_lob_ref，使用独立的查询、存储与失败语义，单独汇总，不重命名 asset_ref，也不并入四布局矩阵的排序。

### 6.6 公平性与访问路径门禁

ClickHouse 依靠主键裁剪读取远小于全表的行数。XStore 侧只有同样具备可用的索引访问路径时，两侧比较才成立；四布局全表扫描会把布局差异淹没在扫描成本里，得到的倍率不反映布局代价。本门禁在正式矩阵之前执行，四个布局逐项通过。

前置条件：表与索引按 6.2 节 create() 创建完成并确认对象清单；冻结输入完整写入且联合水位达到 48,534；wait_query_ready 已执行，maintenance 证据中 analyze_ms 为正数。

| 检查 | 命令 | 通过条件 |
|---|---|---|
| 统计存在 | SELECT relname, reltuples FROM pg_class WHERE relnamespace=(SELECT oid FROM pg_namespace WHERE nspname='<schema>') | 每个写目标的 reltuples 大于 0 |
| 索引存在 | SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='<schema>' | 每张表的 list 与 trace 两条索引都存在 |
| 计划走索引 | 对 list:first、detail:text_64k、trace:p50 各执行一次 EXPLAIN | 计划出现索引访问节点，不是全表扫描 |
| 索引被实际使用 | 执行查询前后读取 pg_stat_user_indexes.idx_scan | 至少一条索引的 idx_scan 增量大于 0 |

任一项不通过时停止，不进入正式矩阵，按以下顺序排查。

1. 确认表的实际存储形态：SELECT relname, reloptions, reltoastrelid FROM pg_class 读取 orientation 与 TOAST 关联。蓝区落在行存，TOAST 成立。落在其他存储形态时按第 2 步处理，不要改建为行存绕开——那会把被测对象换成 openGauss。
2. 确认索引在该存储形态下的实际类型。列存表的索引为 psort，要求运行账号具备 cstore schema 权限：GRANT USAGE, CREATE ON SCHEMA cstore TO <账号>。索引创建成功但计划不选时进入第 3 步。
3. 区分优化器不选与索引不可用：SET enable_seqscan=off 后重跑 EXPLAIN。仍为全表扫描说明索引不能承载该谓词，属于能力限制，按 6.1 节记入能力报告并停止；改为索引扫描说明是代价估算问题，核对统计是否最新并记录。

门禁产物写入 $YELLOW_OUTPUT/access-gate/<engine>/<layout>/，包含三类查询的 EXPLAIN 原文、idx_scan 前后值与判定结论。该目录不在 9.1 节删除范围内。

ClickHouse 侧执行同一门禁，判据改为计划出现主键裁剪且 QueryFinish 的 read_rows 小于表行数。两个引擎都通过后才进入正式矩阵。

### 6.7 XStore 适配卡

XStore 与 openGauss 同源，adapter 默认实现直接复用 openGauss adapter 的 SQL 与系统视图。以下六项按卡执行：先按探测命令取实际值，与蓝区值一致时不改代码；不一致时按建议改法处理，并把实际值与改动记入运行记录。

| 卡 | 蓝区实现 | 探测 | 不一致时的参考改法 | 允许改动 | 禁止改动 |
|---|---|---|---|---|---|
| 一 驱动与认证 | psycopg 3.3.5 直连，md5 认证 | 建立一次连接并读取服务端版本 | openGauss 默认 sha256 认证与 psycopg 不兼容。把运行账号的 password_encryption_type 设为 1 并重建密码，或改用官方 Python 连接器，任选其一并记录 | 连接参数与驱动导入 | SQL 文本与查询语义 |
| 二 参数绑定 | 空 error_category 已内联为 NULL 字面量，其余参数经驱动绑定 | 对 asset 状态转换与 workload 隔离各执行一次写入 | 该改法已随基线提交进入 adapter，无需再改。其他位置出现同类类型推断失败时按同一方式内联，并记录语句与列名 | 参数构造 | 写入的列集合与取值 |
| 三 表存储形态 | 裸 CREATE TABLE 落在行存，reltoastrelid 非零 | SELECT relname, reloptions, reltoastrelid FROM pg_class | 记录实际形态，按卡四处理索引，并在报告中声明 XStore 的空间模型与 openGauss 的 TOAST 模型不可直接对齐 | 无 | 建表语句的列定义 |
| 四 索引形态 | 两条复合 btree：(project_id,start_time,event_id) 与 (project_id,trace_id,start_time,event_id) | SELECT indexname, indexdef FROM pg_indexes | 按实际存储形态所需的索引类型创建，列顺序保持不变；列存时先补 cstore schema 权限 | 索引类型与建索引语法 | 索引列与列顺序 |
| 五 索引使用统计 | SELECT indexrelname, idx_scan FROM pg_stat_user_indexes | 同左 | 该视图不可用时改用 XStore 的等价统计并记录来源；无等价统计时把该字段记为 unavailable | 统计来源 | 把缺失记为通过 |
| 六 执行计划 | EXPLAIN (ANALYZE, BUFFERS) | 对三类查询各执行一次 | 选项不被支持时退到 EXPLAIN ANALYZE 或 EXPLAIN 并记录实际选项；计划文本格式不同不构成偏离 | EXPLAIN 选项 | 访问路径的判定标准 |

以下内容在任何卡下都不自适应：查询语义与参数、五层计时口径、汇总器门禁字段集、冻结输入、四轮 Latin square、每查询 30 次与批量 5 次的测量次数。黄区改动超出上表允许范围时先停止并回传改动意图，不自行扩大范围。

### 6.8 XStore 构建类型门禁

性能结论只在 release 构建上成立。debug 构建的绝对耗时与布局间比值都不可用，且该差异不能通过归一化消除。

进入 6.6 节门禁前记录构建类型与证据：构建命令或构建选项、能证明构建类型的输出（构建目录标志、pg_config 输出或等价证据）。构建类型不是 release，或证据缺失时停止，不执行任何性能运行。构建类型写入每个 target 的运行记录。

## 7. 执行序列

### 7.1 阶段顺序

| 阶段 | 动作 | 通过条件 |
|---|---|---|
| 1 预检 | 可用性检查、环境探测、输入校验、ClickHouse 健康检查与 5.6 节参数对齐 | 第 1 至 5 节全部门禁通过，merge_tree 阈值与内存比例核对一致 |
| 2 构建类型门禁 | 按 6.8 节记录 XStore 构建类型与证据 | 构建类型为 release 且证据齐全 |
| 3 adapter 冒烟 | xstore adapter 单元测试与集成测试 | 单元测试通过；集成测试对真实 XStore 完成 create、ingest_block、wait_write_complete、cleanup |
| 4 单 target candidate | 单引擎单布局的运行入口候选验证 | child run-manifest.json 为 complete，真值、响应字节、水位与清理证据齐全 |
| 5 访问路径门禁 | 按 6.6 节对两个引擎的四个布局逐项检查 | 四布局的 list、detail 与 trace 都走索引访问路径，idx_scan 有增量 |
| 6 单布局冒烟倍率 | 任选一个布局、一轮、main workload，比较两个引擎的应用可用 p50 | 倍率记入运行记录；超过 10 倍时停止排查，不进入正式矩阵 |
| 7 清理验证 | 清理后确认 namespace 与对象目录不存在 | 清理命令返回成功且对象清单为空 |
| 8 正式矩阵 | ClickHouse 与 XStore 在同一主机串行执行四布局四 workload | 两个引擎各 16 个 target 全部 complete；任一引擎缺少四布局矩阵即判本次运行无效 |
| 9 part-state | 对每个引擎与布局判定 part-state 控制是否适用；适用时执行 run_stage3.py part-states，记录 part 状态与合并证据；不适用时在运行记录中引用能力报告条目写明原因 | 每个 target 的结论为已执行通过，或记录不适用并附证据 |
| 10 混合负载 | 执行 run_stage3.py interference，在单个 target 内部产生并发负载，并记录与串行基线的对比 | 混合负载证据与串行基线对比齐全，或记录不适用并附证据 |
| 11 Asset 故障与恢复 | 执行 run_stage3.py asset-failures，记录失败注入、失败 block 证据、恢复动作与清理结果 | 失败与恢复证据齐全，或记录不适用并附证据 |
| 12 回传摘录 | 按 9.2 节生成回传摘录并校验 | 摘录存在、非空且覆盖每个 target；未通过前不执行 9.1 节的删除动作 |

黄区对比只有在第 8 至第 11 阶段全部通过，或对不适用项记录了带证据的不适用结论之后才成立；任一阶段失败时保留产物并标为 diagnostic，不发布完整比较结论。第 9 至第 11 阶段在驱动方式上与第 8 阶段一致：同一冻结输入、同一代码 HEAD、同一串行约束。

ClickHouse 侧与 XStore 侧使用同一条命令形态、同一组 workload 与同一轮次安排。只跑其中一个引擎的矩阵不构成对比，第 8 阶段不通过。

串行执行：同一时刻只运行一个引擎的布局目标，避免 XStore 与 ClickHouse 争用 CPU、内存与磁盘。混合负载场景只在单个 target 内部产生并发。

### 7.2 ClickHouse 侧运行

```bash
mkdir -p "$YELLOW_OUTPUT/clickhouse-main"
cd "$YELLOW_REPO"
"$PYTHON" experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input "$YELLOW_INPUT/json-storage-stage3-formal-input" \
  --output "$YELLOW_OUTPUT/clickhouse-main" \
  --engines clickhouse \
  --layouts same_table,separate,full_core,asset_ref \
  --workloads main,equal_total_few_large,equal_total_many_medium,correctness_only \
  --measurements 30 \
  --batch-measurements 5 \
  --clickhouse-host 127.0.0.1 \
  --clickhouse-port "$CH_HTTP_PORT"
```

一次调用必须列出全部四个 workload：三个性能 workload 各四轮 Latin square，correctness_only 一轮。汇总器要求每个 target 的 workload 证据同时包含 main、equal_total_few_large、equal_total_many_medium 与 correctness_only，缺任一项即拒绝发布；可发布的 target 只由本条命令产生。分次调用会覆盖同一 target 的 run-manifest.json 与轮次目录，留下不完整的 provenance，属于无效运行。四个 workload 分别输出统计口径，不与 main 合并，命令保留在 target run-manifest.json 的 command 字段中。

### 7.3 XStore 侧运行

XStore 使用与 ClickHouse 相同的输入、查询集合、测量次数、四轮 Latin square 与响应契约，engine 参数为 xstore：

```text
python experiments/json-storage-stage3/runner/run_layout_matrix.py \
  --input <冻结输入根> \
  --output <输出根>/xstore-main \
  --engines xstore \
  --layouts same_table,separate,full_core,asset_ref \
  --workloads main,equal_total_few_large,equal_total_many_medium,correctness_only \
  --measurements 30 \
  --batch-measurements 5
```

该命令在 6.3 节的 runner 与工厂改动完成并通过测试后才可执行。改动前 runner 只接受 opengauss 与 clickhouse，xstore 参数会被拒绝。

### 7.4 缓存状态标注

运行清单的 cache_state 字段记录缓存状态。默认标签 warm-reused-connections-no-os-cache-drop 表示连接复用且未丢弃操作系统缓存。需要区分冷热缓存时，先确认 XStore 与 ClickHouse 都支持对应的缓存控制动作：控制动作可执行且生效时，分别执行 cold 与 warm 两组并分别报告；任一引擎不支持该动作时，记录不适用与原因，不生成对比结论。

## 8. 证据与验收

### 8.1 证据清单

| 项目 | 证据来源 |
|---|---|
| 引擎与包身份 | 服务端版本查询、包文件名与 SHA-512、二进制 SHA-256、配置 SHA-256 |
| 代码身份 | 仓库 HEAD、分支、工作区状态、运行命令 |
| 输入身份 | 归档 SHA-256、解包后 identity_sha256、payload 集合 |
| 真值 | 每轮 result.json 的 correctness：formal_samples、successful_samples、failed_samples |
| 响应字节 | response_bytes 的 database、resolver_payload、total、validated_payload 分项 |
| 访问路径 | 每查询的执行计划、scanned_rows、scanned_bytes、访问结构判定 |
| 查询完成与后台状态 | ClickHouse 的 QueryFinish 事件、natural_stable_parts 与 optimize_final；XStore 的等价证据或 unavailable |
| 存储状态 | 表与索引空间、压缩前后字节、part 与 merge 或分片状态 |
| 主机水位 | 执行期间 CPU、内存、磁盘 IO 记录 |
| 失败 | 失败运行的错误、已完成子产物与失败 block 证据 |
| 清理 | namespace 与对象目录删除动作及删除后确认 |

### 8.2 运行分类

| 分类 | 判据 | 处理 |
|---|---|---|
| complete | 真值、完整响应字节、水位、访问路径、存储证据、维护状态与清理证据齐全，且目标 status 为 complete | 进入汇总 |
| diagnostic | 引擎或布局只用部分产物完成，或缺少非关键物理指标 | 保留产物，标注 diagnostic，不进入性能比较 |
| invalid | 输入身份、代码身份、引擎版本、输出契约或清理任一门禁失败 | 保留错误与子产物，重跑前先修复原因 |

### 8.3 比较结论门禁

比较表只在四个布局的 complete 结果、四轮 Latin square、访问路径、维护状态、清理证据，以及 7.1 节第 6 至第 8 阶段结论齐全后生成；控制项适用时给出通过证据，不适用时给出带证据的不适用结论。汇总器对不完整 provenance 报错时，结论保持未发布状态。报告按三层陈述：引擎内布局结论、黄区同机跨引擎结论、不可比较边界。ClickHouse 数值与蓝区结果的版本差异必须在报告中记录。

## 9. 恢复、清理与回传

### 9.1 清理顺序

删除主矩阵输出根会一并删除其中的每个 run-manifest.json，存储证据、访问路径与真值结论都在其中。9.2 节的回传摘录因此是本节的前置门禁：摘录不存在、为空或未覆盖全部 target 时停止，不执行任何删除动作。

清理保持幂等：已经缺失的目录按已清理处理，最终断言始终执行。顺序为校验回传摘录，校验 CH_INSTALL_MODE，停止 XStore 侧查询与后台任务，删除 XStore namespace 与对象目录，停止 ClickHouse，按安装路径回滚 ClickHouse 配置，删除 $YELLOW_OUTPUT 下的 clickhouse-main 与 xstore-main 两个主矩阵输出根及 $YELLOW_STATE/clickhouse，确认端口不再监听。$YELLOW_STATE 下的 venv、冻结输入、Release 资产、$YELLOW_OUTPUT/access-gate、$YELLOW_OUTPUT/handback 与后续控制运行的证据不在本片段的删除范围内。

```bash
# 1) XStore 侧：确认无运行中的查询与后台任务后删除 namespace 与对象目录，命令来自能力报告，
#    删除后确认对象清单为空。能力报告把 XStore 对象目录放在 $YELLOW_STATE 之外时，先记录实际路径
#    并停止，由操作者按同一条命令清理；本片段的递归删除只覆盖 $YELLOW_STATE 内的指南路径。

# 2) 安装模式先于任何动作校验：未设置或取值非 rpm/tgz 时停止，不恢复备份，也不删除任何目录。
case "${CH_INSTALL_MODE:-}" in
  rpm|tgz) ;;
  *)
    printf 'CH_INSTALL_MODE must be rpm or tgz, found %s; stop here\n' "${CH_INSTALL_MODE:-unset}" >&2
    exit 1
    ;;
esac

# 2.5) 回传摘录门禁：输出根下存在 run manifest 时，摘录必须覆盖全部 target，否则不删除任何目录。
"$PYTHON_BOOTSTRAP" - "$YELLOW_OUTPUT" <<'HANDBACK' || exit 1
import json, sys
from pathlib import Path

root = Path(sys.argv[1])
expected = {
    str(manifest.parent.relative_to(root))
    for manifest in root.rglob("run-manifest.json")
    if "handback" not in manifest.parts
} if root.is_dir() else set()
if not expected:
    print("no run manifest under the output root; handback gate not applicable")
    raise SystemExit(0)
summary = root / "handback" / "summary.json"
if not summary.is_file() or summary.stat().st_size == 0:
    raise SystemExit("handback summary missing or empty: %s" % summary)
targets = json.loads(summary.read_text(encoding="utf-8")).get("targets", [])
missing = sorted(expected - {entry["target"] for entry in targets})
if missing:
    raise SystemExit("handback summary misses targets: " + ", ".join(missing))
print("handback summary covers %d targets" % len(targets))
HANDBACK

# 3) ClickHouse 侧：RPM 路径在本片段内完成系统级回滚；读取备份与覆盖文件前不删除状态目录。
if [ "$CH_INSTALL_MODE" = "rpm" ]; then
  sudo systemctl stop clickhouse-server || true

  # 2.1 删除本指南创建的固定覆盖文件，并断言其不再存在。
  require_rpm_override_path || exit 1
  if sudo test -f "$CH_RPM_OVERRIDE"; then
    sudo rm -f -- "$CH_RPM_OVERRIDE"
  fi
  if sudo test -f "$CH_RPM_OVERRIDE"; then
    printf 'guide-owned override still exists: %s\n' "$CH_RPM_OVERRIDE" >&2
    exit 1
  fi

# 2.2 仅在 preexisting=yes 时按标记恢复既有系统配置；yes 必须先有归档与校验清单，标记缺失或取值非法时停止。
  case "$(cat "$CH_STATE/backup/preexisting.txt" 2>/dev/null)" in
    preexisting=yes)
      if [ ! -f "$CH_STATE/backup/etc-clickhouse-server.tar.gz" ] \
        || [ ! -f "$CH_STATE/backup/etc-clickhouse-server.sha256" ]; then
        printf 'pre-existing configuration backup is incomplete; stop here\n' >&2
        exit 1
      fi
      if ! sha256sum -c "$CH_STATE/backup/etc-clickhouse-server.sha256" >&2; then
        printf 'configuration backup checksum verification failed; stop here\n' >&2
        exit 1
      fi
      sudo tar -xzf "$CH_STATE/backup/etc-clickhouse-server.tar.gz" -C "$CH_ETC_ROOT"
      ;;
    preexisting=no)
      printf 'no pre-existing configuration backup to restore\n'
      ;;
    *)
      printf 'preexisting marker is missing or invalid; stop here\n' >&2
      exit 1
      ;;
  esac

  # 2.3 写入临时回环覆盖再启动服务，验证回环与恢复后的配置状态，随后停止服务并删除临时覆盖文件。
  # 临时验证状态的清理助手幂等：先停止服务再删除覆盖文件；失败路径调用后保留状态目录用于诊断。
  cleanup_temporary_rpm_override() {
    sudo systemctl stop clickhouse-server || true
    if sudo test -f "$CH_RPM_OVERRIDE"; then
      sudo rm -f -- "$CH_RPM_OVERRIDE"
    fi
  }

  require_rpm_override_path || exit 1
  sudo install -d -m 0755 "$(dirname "$CH_RPM_OVERRIDE")"
  sudo tee "$CH_RPM_OVERRIDE" >/dev/null <<'XML'
<clickhouse>
    <listen_host>127.0.0.1</listen_host>
    <http_port>18123</http_port>
    <tcp_port>19000</tcp_port>
</clickhouse>
XML
  if ! sudo systemctl start clickhouse-server; then
    printf 'clickhouse-server failed to start with the restored configuration; stop here\n' >&2
    cleanup_temporary_rpm_override
    exit 1
  fi
  if ! sudo systemctl is-active --quiet clickhouse-server; then
    printf 'clickhouse-server did not start with the restored configuration; stop here\n' >&2
    cleanup_temporary_rpm_override
    exit 1
  fi
  if ! assert_ports_loopback_only; then
    printf 'restored configuration is not loopback-only; stop here\n' >&2
    cleanup_temporary_rpm_override
    exit 1
  fi
  cleanup_temporary_rpm_override
  if sudo test -f "$CH_RPM_OVERRIDE"; then
    printf 'temporary loopback override still exists: %s\n' "$CH_RPM_OVERRIDE" >&2
    exit 1
  fi
elif [ -f "$CH_STATE/run/clickhouse.pid" ]; then
  kill "$(cat "$CH_STATE/run/clickhouse.pid")" 2>/dev/null || true
fi

# 4) 先从已知输出根及 run-manifest.json 证据收集 runner Asset 工作树。
asset_targets=(
  "$YELLOW_OUTPUT/clickhouse-main/.asset-work"
  "$YELLOW_OUTPUT/xstore-main/.asset-work"
)
if [ -d "$YELLOW_OUTPUT" ]; then
  asset_evidence=$(
    "$PYTHON_BOOTSTRAP" - "$YELLOW_OUTPUT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
for manifest in sorted(root.rglob("run-manifest.json")):
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read cleanup evidence {manifest}: {error}")
    cleanup = record.get("global_cleanup")
    if not isinstance(cleanup, dict) or "asset_workspace" not in cleanup:
        continue
    workspace = cleanup["asset_workspace"]
    if not isinstance(workspace, str) or not workspace.strip():
        raise SystemExit(f"invalid asset_workspace cleanup evidence: {manifest}")
    print(workspace)
PY
  ) || {
    printf 'failed to collect Asset workspace paths from run manifests\n' >&2
    exit 1
  }
  while IFS= read -r target; do
    [ -n "$target" ] && asset_targets+=("$target")
  done <<< "$asset_evidence"
fi

for target in "${asset_targets[@]}"; do
  if [ -d "$target" ]; then
    require_asset_workspace_path "$target" >/dev/null || exit 1
    rm -rf -- "$target"
    printf 'removed asset workspace: %s\n' "$target"
  fi
done

# 5) 删除指南创建的主矩阵输出根与 ClickHouse 状态目录，缺失即视为已清理。
guide_targets=(
  "$YELLOW_OUTPUT/clickhouse-main"
  "$YELLOW_OUTPUT/xstore-main"
  "$YELLOW_STATE/clickhouse"
)
for target in "${guide_targets[@]}"; do
  if [ -d "$target" ]; then
    require_guide_path "$target" || exit 1
    rm -rf -- "$target"
  else
    printf 'already clean: %s\n' "$target"
  fi
done

# 6) 最终断言覆盖本片段删除的每个路径，任一不满足即失败退出。
for target in "${asset_targets[@]}"; do
  if [ -e "$target" ]; then
    printf 'cleanup assertion failed, Asset workspace remains: %s\n' "$target" >&2
    exit 1
  fi
done
for target in "${guide_targets[@]}"; do
  if [ -e "$target" ]; then
    printf 'cleanup assertion failed, directory remains: %s\n' "$target" >&2
    exit 1
  fi
done
assert_ports_free || exit 1
printf 'cleanup complete: %s\n' "$YELLOW_STATE"
```

删除范围限定为指南创建的路径：$YELLOW_OUTPUT 下的 clickhouse-main 与 xstore-main 两个主矩阵输出根、$YELLOW_STATE/clickhouse，以及各 target 的 run-manifest.json 在 global_cleanup.asset_workspace 中记录的后续控制运行 Asset 工作树。runner 为每个布局轮次在输出根下创建 .asset-work/engine/layout/workload/round-N，矩阵结束时按空目录清理；非空残留会被 runner 记为 global_cleanup.removed=false。本片段读取该证据，只接受 $YELLOW_OUTPUT 内以 .asset-work 结尾的路径，对每个路径执行删除守卫并断言消失；后续控制运行的其余证据目录保持不变。RPM 路径只删除本指南创建的固定覆盖文件 $CH_RPM_OVERRIDE（默认 /etc/clickhouse-server/config.d/00-stage3-yellow.xml），包创建的 /var/lib/clickhouse 与 /var/log/clickhouse-server 不在自动清理范围内。恢复既有系统配置、验证服务与断言覆盖文件消失都需要 root 权限，缺少权限时停止。对象目录、后台任务或临时配置无法清理时停止后续运行，并记录未清理对象清单。

### 9.2 回传材料

黄区不能直接上传数据时，回传材料由三部分组成：

1. 代码改动：黄区策略允许推送时以 Git 提交形式推送分支；不允许推送时在本地保留提交，并给出提交哈希与 diff --stat。
2. 清单：文件清单加每个文件的 SHA-256。
3. 结果摘录：每 target 的 run-manifest.json 摘要、真值结论、响应字节、访问路径、存储证据、资源水位、失败项与清理确认。

结果摘录落盘为 $YELLOW_OUTPUT/handback/summary.json，在 9.1 节删除动作之前生成。targets 数组每项对应一个 target 目录，字段如下，缺任一字段视为摘录不完整。

| 字段 | 含义 |
|---|---|
| target | 相对 $YELLOW_OUTPUT 的 target 目录路径 |
| engine、layout、workload | 运行身份 |
| status | run-manifest.json 的 status |
| build_type | XStore 侧的构建类型与证据，ClickHouse 侧记 package |
| storage | 每个写目标的空间字段原值 |
| access | 每类查询的访问路径判定与 idx_scan 增量 |
| truth | 真值校验结论与失败样本数 |
| response_bytes | database、resolver_payload、total 三项 |
| cleanup | 清理动作与删除后确认结果 |
| failures | 失败项清单，无失败时为空数组 |

storage 与 access 两项在清理后无法重建，逐 target 落盘，不用聚合值代替。

原始 payload、完整 samples.jsonl 与归档文件留在黄区本地只读目录，回复中只给出路径、摘要与失败项。

## 10. 转发用黄区 agent prompt

```text
在黄区 ARM64（EulerOS 2.13，无 Docker）主机上执行
docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md。

执行顺序按指南第 7.1 节的十二个阶段，不跳过、不调序：可用性检查与环境探测 →
XStore 构建类型门禁（release）→ ClickHouse 23.3.10.5 部署与 5.6 节参数对齐 →
adapter 冒烟 → 单 target candidate → 6.6 节访问路径门禁 → 单布局冒烟倍率 →
清理验证 → 两个引擎各自的四布局四轮四 workload 正式矩阵 →
part-state、混合负载、Asset 故障与恢复（不适用时记录原因与证据）→ 回传摘录。

三条硬性要求：
1. 构建类型不是 release 时停止，不跑任何性能运行。
2. 四个布局的 list、detail、trace 未走索引访问路径时停止，按 6.6 节排查，
   不改建为行存绕开，不带着全表扫描进入矩阵。
3. ClickHouse 与 XStore 都要跑完整四布局矩阵；只跑一侧不构成对比。

实现细节按第 6.7 节的六张适配卡处理：先探测，与蓝区一致就不改代码，不一致按卡内
参考改法处理并记录实际值。查询语义、五层计时口径、门禁字段集、冻结输入、四轮
Latin square 与 30/5 测量次数不自适应；需要超出适配卡允许范围时先停止并回传意图。

按该指南的 fail-closed 规则执行：分支或归档资产不可用时、能力报告字段缺少证据时、
真值或水位或清理证据缺失时停止并报告，不猜测 XStore 能力。
回传内容按指南第 9.2 节给出：代码提交或 diff --stat、文件清单与 SHA-256，
以及 $YELLOW_OUTPUT/handback/summary.json。该摘录通过校验之前不执行第 9.1 节的删除动作。
```

## 11. 参考入口

- [阶段三实验设计与证据契约](../json-storage/json-storage-stage3-experiment-design-2026-09-09.md)
- [阶段三四布局代表性评估](../json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md)
- [阶段三黄区 xstore 对比交接设计](../superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md)
- [阶段三黄区交接实施计划](../superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md)
- [GV xstore 复现指南](trace-ingestion-demo-yellow-zone-guide.md)
- [布局矩阵 runner](../../experiments/json-storage-stage3/runner/run_layout_matrix.py)
- [不变量、计时与 adapter 契约](../../experiments/json-storage-stage3/runner/common.py)
- [生产运行入口](../../experiments/json-storage-stage3/runner/run_stage3.py)
- [汇总器与 workload 门禁](../../experiments/json-storage-stage3/report/summarize.py)
- [冻结输入打包工具](../../experiments/json-storage-stage3/tools/package_formal_input.py)
- [Asset resolver](../../experiments/json-storage-stage3/runner/assets.py)
- [冻结输入生成器](../../experiments/json-storage-stage3/generator/generate_payloads.py)
- [ClickHouse v23.3.10.5-lts Release](https://github.com/ClickHouse/ClickHouse/releases/tag/v23.3.10.5-lts)
