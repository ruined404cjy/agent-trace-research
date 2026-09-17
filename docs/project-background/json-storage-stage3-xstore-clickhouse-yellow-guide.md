
# JSON 存储阶段三黄区 XStore/ClickHouse 同机对比指南

> 文档状态：黄区执行指南。配套评估见 [阶段三四布局代表性评估](../json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md)。
>
> 适用环境：EulerOS 2.13 ARM64（aarch64），已部署 XStore，不使用 Docker。
>
> 配套设计：[阶段三黄区 xstore 对比交接设计](../superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md)；配套计划：[阶段三黄区交接实施计划](../superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md)。

本文给出在一台 ARM64 主机上部署 ClickHouse 25.12.11.4、校验 Stage 3 冻结输入、核对 XStore 能力、实现 XStore adapter、执行四布局同机对比并交付证据的完整流程。文档可独立阅读，执行者可以是黄区操作者，也可以是按本文执行的黄区 agent。

## 1. 蓝区事实与黄区目标

### 1.1 蓝区已验证事实

| 项目 | 事实 | 证据 |
|---|---|---|
| 四种布局 | same_table、separate、full_core、asset_ref | [阶段三实验设计](../json-storage/json-storage-stage3-experiment-design-2026-09-09.md) |
| 冻结输入 | 48,534 条事件、190 个写入 block（每 block 256 行）、main cohort 160 条 payload、原始 122.5 MiB、seed 20260907 | generation-manifest.json（status=complete） |
| 输入身份 | identity_sha256 = a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8 | 八 target 共用同一输入身份 |
| 部分正式切片 | 2 引擎 × 4 布局、每 target 四轮 Latin square、每 target 1,340/1,340 次查询操作 | main-matrix-attempt-1 轮次产物 |
| 切片边界 | 缺少 equal_total_few_large、equal_total_many_medium 与 correctness_only，未通过汇总器 provenance 门禁 | [汇总器](../../experiments/json-storage-stage3/report/summarize.py) 报 ValueError: provenance evidence is incomplete |
| 现有 engine | 适配器只实现 opengauss 与 clickhouse | [布局矩阵 runner](../../experiments/json-storage-stage3/runner/run_layout_matrix.py) |
| 交付状态 | 分支与归档在蓝区本地生成；GitHub 上的分支和 Release 资产由后续授权的发布动作产生 | 本文第 2 节可用性检查 |

### 1.2 黄区需要采集的事实

黄区在同一台 ARM64 主机、同一冻结输入、同一 Python runner、同一查询参数和同一四轮 Latin square 下补齐 XStore 四布局结果，并与同机 ClickHouse 25.12.11.4 串行对比。以下事实由黄区采集并附证据：

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
3. XStore 能力报告的任一必填字段缺少可执行证据。
4. 代码 HEAD、工作区状态、引擎版本或输入身份无法记录。
5. ClickHouse 未在回环地址就绪，或运行版本与本文不一致且未记录策略原因。
6. 真值、完整 payload bytes、联合水位或清理证据缺失。
7. 执行计划、扫描证据或输出契约与预期不符。
8. namespace、对象目录、后台任务或临时配置无法清理。

## 2. 交付身份、全局变量与可用性检查

### 2.1 固定身份

| 项目 | 值 |
|---|---|
| 仓库（SSH） | git@github.com:ruined404cjy/agent-trace-research.git |
| 仓库（HTTPS） | https://github.com/ruined404cjy/agent-trace-research.git |
| 分支 | stage3/xstore-yellow-handoff |
| 基线提交 | 93ebf2319ae7cb60b1f68eb53b3562d26f80f443（短写 93ebf23） |
| 冻结输入归档 | json-storage-stage3-formal-input-20260917.tar.gz |
| 归档校验文件 | json-storage-stage3-formal-input-20260917.tar.gz.sha256 |
| 归档蓝区已验证身份 | SHA-256 47737b02335c7b2ce76640599f2b2f8d7142935df3acfead29b95270db60ac8f，大小 19,313,037 bytes |
| 归档根目录 | json-storage-stage3-formal-input/，内含 events.jsonl、truth.json、generation-manifest.json、payloads/ |

归档由 [冻结输入打包工具](../../experiments/json-storage-stage3/tools/package_formal_input.py) 生成。源目录中的 run-manifest.json 属于生成阶段的包装元数据，按已记录裁决不进入归档。

### 2.2 全局变量与删除守卫

开始执行前设置以下变量，并把 /path/to 替换为黄区实际路径。变量块以 HTML 注释标记，供文档契约测试作为 shell 前置脚本复用。

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
export YELLOW_BASE_COMMIT=93ebf2319ae7cb60b1f68eb53b3562d26f80f443
export YELLOW_RELEASE_TAG=stage3-formal-input-20260917
export YELLOW_RELEASE_URL="https://github.com/ruined404cjy/agent-trace-research/releases/download/${YELLOW_RELEASE_TAG}"
export YELLOW_ARCHIVE_NAME=json-storage-stage3-formal-input-20260917.tar.gz

export CH_VERSION=25.12.11.4
export CH_RELEASE_TAG=v25.12.11.4-stable
export CH_RELEASE_URL="https://github.com/ClickHouse/ClickHouse/releases/download/${CH_RELEASE_TAG}"
export CH_STATE="$YELLOW_STATE/clickhouse"
export CH_BIN="$CH_STATE/opt/clickhouse-common-static-${CH_VERSION}/usr/bin/clickhouse"
export CH_CONFIG="$CH_STATE/etc/config.xml"
export CH_HTTP_PORT=18123
export CH_TCP_PORT=19000
export CH_INSTALL_MODE=tgz
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
```

### 2.3 可用性检查

分支与归档资产由发布动作产生。YELLOW_RELEASE_TAG 的取值与发布动作使用的标签一致；执行时把该变量设为实际标签并记入运行记录。以下检查先于一切实验动作；任一检查失败时停止并请求发布。

```bash
mkdir -p "$YELLOW_STATE" "$YELLOW_RELEASE"

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

能力报告是 adapter 实施的前置门禁。报告写入 $YELLOW_STATE/xstore/capability-report.json，每个字段给出实际执行的命令或 API 调用、原始输出摘录和状态 available 或 unavailable。缺少证据的字段写 unavailable 与原因，汇总器不得估算。

```json
{
  "format": "agent-trace-json-storage-stage3-xstore-capability-report",
  "format_version": 1,
  "engine": {"product": "", "version": "", "protocol": "", "evidence": ""},
  "sql_driver": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "ddl_dml": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "json_lob_types": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "transaction": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "extension_support": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "query_statistics": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "storage_accounting": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "background_work": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "cleanup": {"status": "available", "evidence": {"command": "", "observed": ""}},
  "cache_control": {"status": "available", "evidence": {"command": "", "observed": ""}}
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

### 3.3 Python 运行环境

Stage 3 runner 需要 Python 3.10 及以上（adapter 契约使用 PEP 604 联合类型标注），并需要 psycopg 供 openGauss adapter 的导入链使用，其余代码只用标准库。蓝区实测环境为 Python 3.11.6 与 psycopg 3.3.5，黄区按相同版本对齐。

依赖来源限定为黄区策略批准的 PyPI 镜像；镜像不可用时在蓝区用同一解释器执行 pip download 生成 wheelhouse 并随交接材料传入，黄区用 --no-index --find-links 安装同一 pin 版本。系统 python3 只用于创建虚拟环境，不用于 loader 与 runner。

```bash
cd "$YELLOW_REPO"
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

运行记录写明实际 HEAD，分支名只作为查找入口。基线提交 93ebf2319ae7cb60b1f68eb53b3562d26f80f443 是所有结果的祖先提交。

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

## 5. ClickHouse 25.12.11.4 ARM64 部署

### 5.1 版本与官方资产

蓝区正式切片使用 ClickHouse 25.12.11.4。黄区使用同版本官方 ARM64 产物，控制引擎版本变量。下载入口限定为官方 GitHub Release v25.12.11.4-stable。

| 资产 | 文件 | 用途 |
|---|---|---|
| common-static | clickhouse-common-static-25.12.11.4-arm64.tgz | 可执行文件与运行时资源，解包后二进制位于 clickhouse-common-static-25.12.11.4/usr/bin/clickhouse |
| server | clickhouse-server-25.12.11.4-arm64.tgz | etc/clickhouse-server/config.xml 与 users.xml 模板 |
| client | clickhouse-client-25.12.11.4-arm64.tgz | 客户端与格式化工具符号链接 |

RPM 路径使用同一 Release 的 AArch64 包：

| 包 | 文件 |
|---|---|
| common-static | clickhouse-common-static-25.12.11.4.aarch64.rpm |
| server | clickhouse-server-25.12.11.4.aarch64.rpm |
| client | clickhouse-client-25.12.11.4.aarch64.rpm |

Release 页面与下载入口：

```text
https://github.com/ClickHouse/ClickHouse/releases/tag/v25.12.11.4-stable
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-common-static-25.12.11.4-arm64.tgz
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-common-static-25.12.11.4-arm64.tgz.sha512
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-server-25.12.11.4-arm64.tgz
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-server-25.12.11.4-arm64.tgz.sha512
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-client-25.12.11.4-arm64.tgz
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-client-25.12.11.4-arm64.tgz.sha512
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-common-static-25.12.11.4.aarch64.rpm
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-server-25.12.11.4.aarch64.rpm
https://github.com/ClickHouse/ClickHouse/releases/download/v25.12.11.4-stable/clickhouse-client-25.12.11.4.aarch64.rpm
```

### 5.2 下载与 SHA-512 校验

```bash
mkdir -p "$CH_STATE/opt" "$CH_STATE/pkg" "$CH_STATE/log" "$CH_STATE/run"
cd "$CH_STATE/pkg"

for name in clickhouse-common-static clickhouse-server clickhouse-client; do
  file="${name}-${CH_VERSION}-arm64.tgz"
  curl -sSL --max-time 3600 -o "$file" "$CH_RELEASE_URL/$file"
  curl -sSL --max-time 300 -o "$file.sha512" "$CH_RELEASE_URL/$file.sha512"
  sha512sum -c "$file.sha512"
done

sha512sum clickhouse-common-static-${CH_VERSION}-arm64.tgz \
  clickhouse-server-${CH_VERSION}-arm64.tgz \
  clickhouse-client-${CH_VERSION}-arm64.tgz | tee "$CH_STATE/pkg/package-sha512.txt"
```

sha512sum -c 对三个包都必须输出 OK。摘要文件使用标准校验格式（摘要、两个空格、文件名），在校验目录内直接执行 sha512sum -c 即可。

### 5.3 root 与 RPM 权限路径

RPM 路径与 TGZ 路径互斥：每台主机只选一条路径，并把 CH_INSTALL_MODE 设为 rpm 或 tgz 记入运行记录。两条路径使用同一 HTTP 端口 18123 与原生端口 19000，同时安装会互相冲突。

RPM 路径写入系统位置。按 rpm -qlp 核对，clickhouse-server-25.12.11.4.aarch64.rpm 与同批包安装后涉及的路径如下。

| 路径 | 归属 |
|---|---|
| /etc/clickhouse-server/config.xml、/etc/clickhouse-server/users.xml | 包默认配置 |
| /etc/clickhouse-server/config.d/00-stage3-yellow.xml | 本指南创建的回环与端口覆盖 |
| /lib/systemd/system/clickhouse-server.service | systemd 单元 |
| /usr/bin/clickhouse、/usr/bin/clickhouse-server、/usr/bin/clickhouse-client | 多调用二进制与符号链接 |
| /var/lib/clickhouse、/var/log/clickhouse-server | 包创建的数据与日志目录 |

回滚范围覆盖指南创建项与包默认配置：停止服务、删除覆盖文件、恢复备份的 /etc/clickhouse-server、重新启动并验证。包创建的数据目录保留，实验数据由 runner 的按 namespace DROP DATABASE 清理；只有在操作者明确决定该主机不再保留 ClickHouse 时，才执行包移除与数据目录处理。

备份、下载与安装：

```bash
export CH_INSTALL_MODE=rpm
mkdir -p "$CH_STATE/backup"
if [ -d /etc/clickhouse-server ]; then
  sudo tar -czf "$CH_STATE/backup/etc-clickhouse-server.tar.gz" -C /etc clickhouse-server
  sha256sum "$CH_STATE/backup/etc-clickhouse-server.tar.gz" \
    | tee "$CH_STATE/backup/etc-clickhouse-server.sha256"
  printf 'preexisting=yes\n' | tee "$CH_STATE/backup/preexisting.txt"
else
  printf 'preexisting=no\n' | tee "$CH_STATE/backup/preexisting.txt"
fi

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
sudo install -d -m 0755 /etc/clickhouse-server/config.d
sudo tee /etc/clickhouse-server/config.d/00-stage3-yellow.xml >/dev/null <<'XML'
<clickhouse>
    <listen_host>127.0.0.1</listen_host>
    <http_port>18123</http_port>
    <tcp_port>19000</tcp_port>
</clickhouse>
XML
sudo sha256sum /etc/clickhouse-server/config.d/00-stage3-yellow.xml \
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

ss -ltn | grep -E ':(18123|19000)$'
sudo journalctl -u clickhouse-server --no-pager -n 100 | tee "$CH_STATE/log/journal.txt"
if grep -En '<Error>|<Fatal>' "$CH_STATE/log/journal.txt"; then
  printf 'clickhouse journal contains <Error> or <Fatal>; stop here\n' >&2
  exit 1
fi
```

服务验证判据：systemctl is-active 输出 active，curl 返回 25.12.11.4，18123 与 19000 只出现在 127.0.0.1 上，journal 中无 <Error> 或 <Fatal>。

回滚：

```bash
sudo systemctl stop clickhouse-server || true
sudo rm -f -- /etc/clickhouse-server/config.d/00-stage3-yellow.xml
if [ "$(cat "$CH_STATE/backup/preexisting.txt")" = "yes" ]; then
  sudo tar -xzf "$CH_STATE/backup/etc-clickhouse-server.tar.gz" -C /etc
fi
sudo systemctl start clickhouse-server
sudo systemctl is-active clickhouse-server
```

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

包内自带配置指向 /var/lib/clickhouse 与 /var/log/clickhouse-server。下面的脚本把路径、端口与监听地址改写为状态目录内的隔离值，并在写入前逐项断言，任一项不符时脚本以非零状态退出。

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
    """执行一次唯一字符串替换；未命中或重复命中时直接失败。"""
    if text.count(old) != 1:
        raise SystemExit(f"config anchor not unique: {old}")
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

健康判据：curl 与客户端都返回 25.12.11.4，18123 与 19000 只出现在 127.0.0.1 上，启动日志不含 <Error> 或 <Fatal> 标记。启动失败、版本不一致或出现上述标记时保留日志并停止，不进入 adapter 与实验阶段。

```bash
listeners=$(ss -ltn | awk 'NR>1 {print $4}' | grep -E ':(18123|19000)$' || true)
printf '%s\n' "$listeners"
loopback=$(printf '%s\n' "$listeners" | grep -c '^127\.0\.0\.1:' || true)
total=$(printf '%s\n' "$listeners" | grep -c ':' || true)
test "$loopback" = "$total"

grep -En '<Error>|<Fatal>' "$CH_STATE/log/server-console.log" \
  "$CH_STATE/log/clickhouse-server.log" | tee "$CH_STATE/log/error-scan.txt" || true
if [ -s "$CH_STATE/log/error-scan.txt" ]; then
  printf 'clickhouse startup log contains <Error> or <Fatal>; stop here\n' >&2
  exit 1
fi
```

### 5.6 稳定版本回退

黄区安全策略不允许使用 25.12.11.4 时，改用策略批准的 ARM64 stable 或 LTS 版本，并在运行记录中写明策略依据、包名与运行版本。结论限定为黄区同机 XStore/ClickHouse 对比，不与蓝区 ClickHouse 数值合并；需要跨区趋势时先记录版本差异，并由蓝区按相同版本复测。

### 5.7 停止服务

暂停或结束实验时先停止引擎，再收集证据。本节不删除目录；目录、覆盖配置与备份的清理在第 9.1 节执行，重复执行保持幂等。

```bash
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
if ss -ltn | grep -E ':(18123|19000)$'; then
  printf 'clickhouse ports are still listening; stop here\n' >&2
  exit 1
fi
```

停止与确认完成后按第 9.1 节执行清理；清理片段可重复执行，已经缺失的目录按已清理处理。

## 6. XStore adapter 实施门禁

### 6.1 实施前置条件

在能力报告补齐前停止 adapter 实施。进入实施阶段需要同时满足：能力报告的十个字段都有状态与证据；XStore 的 namespace 创建与删除、批量写入、提交、读取、执行统计、空间统计与后台任务接口已经用实际命令或 API 验证；冻结输入与 ClickHouse 侧已经完成一次单布局 smoke。

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

## 7. 执行序列

### 7.1 阶段顺序

| 阶段 | 动作 | 通过条件 |
|---|---|---|
| 1 预检 | 可用性检查、环境探测、输入校验、ClickHouse 健康检查 | 第 1 至 5 节全部门禁通过 |
| 2 adapter 冒烟 | xstore adapter 单元测试与集成测试 | 单元测试通过；集成测试对真实 XStore 完成 create、ingest_block、wait_write_complete、cleanup |
| 3 单 target candidate | 单引擎单布局的运行入口候选验证 | child run-manifest.json 为 complete，真值、响应字节、水位与清理证据齐全 |
| 4 清理验证 | 清理后确认 namespace 与对象目录不存在 | 清理命令返回成功且对象清单为空 |
| 5 正式矩阵 | ClickHouse 与 XStore 在同一主机串行执行四布局 | 每 target 四轮 Latin square 完成，全部门禁通过 |

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

比较表只在四个布局的 complete 结果、四轮 Latin square、访问路径、维护状态与清理证据齐全后生成。汇总器对不完整 provenance 报错时，结论保持未发布状态。报告按三层陈述：引擎内布局结论、黄区同机跨引擎结论、不可比较边界。ClickHouse 数值与蓝区结果的版本差异必须在报告中记录。

## 9. 恢复、清理与回传

### 9.1 清理顺序

清理保持幂等：已经缺失的目录按已清理处理，最终断言始终执行。顺序为停止 XStore 侧查询与后台任务，删除 XStore namespace 与对象目录，停止 ClickHouse，按安装路径回滚 ClickHouse 配置，删除指南创建的状态目录，确认端口不再监听。

```bash
# 1) XStore 侧：确认无运行中的查询与后台任务后删除 namespace 与对象目录，命令来自能力报告，
#    删除后确认对象清单为空。

# 2) ClickHouse 侧：先停止；RPM 路径再按 5.3 节回滚覆盖配置并恢复备份，读取备份前不删除状态目录。
#    RPM 路径在执行第 3 步前先运行 5.3 节的回滚命令，删除覆盖文件并恢复备份。
if [ "$CH_INSTALL_MODE" = "rpm" ]; then
  sudo systemctl stop clickhouse-server || true
elif [ -f "$CH_STATE/run/clickhouse.pid" ]; then
  kill "$(cat "$CH_STATE/run/clickhouse.pid")" 2>/dev/null || true
fi

# 3) 删除指南创建的状态目录，缺失即视为已清理。
for target in "$YELLOW_STATE/clickhouse" "$YELLOW_STATE/runs/xstore-assets" "$YELLOW_STATE/runs/xstore-main"; do
  if [ -d "$target" ]; then
    require_guide_path "$target" || exit 1
    rm -rf -- "$target"
  else
    printf 'already clean: %s\n' "$target"
  fi
done

# 4) 最终断言始终执行。
test ! -d "$YELLOW_STATE/clickhouse"
test ! -d "$YELLOW_STATE/runs/xstore-assets"
if ss -ltn | grep -E ':(18123|19000)$'; then
  printf 'experiment ports are still listening; stop here\n' >&2
  exit 1
fi
printf 'cleanup complete: %s\n' "$YELLOW_STATE"
```

删除范围限定为指南创建的状态目录；RPM 路径的系统覆盖文件按 5.3 节的固定字面路径删除，包创建的 /var/lib/clickhouse 与 /var/log/clickhouse-server 不在自动清理范围内。对象目录、后台任务或临时配置无法清理时停止后续运行，并记录未清理对象清单。

### 9.2 回传材料

黄区不能直接上传数据时，回传材料由三部分组成：

1. 代码改动：黄区策略允许推送时以 Git 提交形式推送分支；不允许推送时在本地保留提交，并给出提交哈希与 diff --stat。
2. 清单：文件清单加每个文件的 SHA-256。
3. 结果摘录：每 target 的 run-manifest.json 摘要、真值结论、响应字节、访问路径、存储证据、资源水位、失败项与清理确认。

原始 payload、完整 samples.jsonl 与归档文件留在黄区本地只读目录，回复中只给出路径、摘要与失败项。

## 10. 转发用黄区 agent prompt

```text
在黄区 ARM64（EulerOS 2.13，无 Docker）主机上执行
docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md。

执行顺序：可用性检查 → 环境探测与 XStore 能力报告 → 冻结输入校验 →
ClickHouse 25.12.11.4 ARM64 部署与健康检查 → XStore adapter 实施门禁 →
四布局同机串行对比（ClickHouse 与 XStore 各四轮 Latin square）→ 证据与清理。

按该指南的 fail-closed 规则执行：分支或归档资产不可用时、能力报告字段缺少证据时、
真值或水位或清理证据缺失时停止并报告，不猜测 XStore 能力。
回传内容按指南第 9.2 节给出：代码提交或 diff --stat、文件清单与 SHA-256，
以及每 target 的 run-manifest.json 摘要、访问路径、存储证据、资源水位、失败项与清理确认。
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
- [ClickHouse v25.12.11.4-stable Release](https://github.com/ClickHouse/ClickHouse/releases/tag/v25.12.11.4-stable)
