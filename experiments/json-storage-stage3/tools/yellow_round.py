"""阶段三黄区一轮实验的驱动脚本：按固定顺序运行全部实验并生成可提交的 feedback 目录。

用法（在 $YELLOW_REPO 下，同一 shell 已按黄区指南第 2.2 节 source 变量块）：

    python experiments/json-storage-stage3/tools/yellow_round.py preflight
    python experiments/json-storage-stage3/tools/yellow_round.py xstore       # 先停止 ClickHouse 服务
    python experiments/json-storage-stage3/tools/yellow_round.py clickhouse   # 先停止 XStore 服务
    python experiments/json-storage-stage3/tools/yellow_round.py pack --host <IP 末段>
    python experiments/json-storage-stage3/tools/yellow_round.py core --host <IP 末段>   # 打印手敲精简版
    python experiments/json-storage-stage3/tools/yellow_round.py check --host <IP 末段>  # 补充核对，见 check_handback.py

意图：
- 每个实验步骤有固定输出名（见 plan_steps），打包脚本只按这些名字取数，不扫描其他目录。
- 已完成（run-manifest.json 的 status 为 complete）的步骤直接跳过，因此中断后重跑同一命令即可续跑。
- 输出已存在但未完成时，先把它改名为 <名字>.attempt-<时间> 保留作诊断，再重跑该步。
- 每个引擎阶段开始前按反馈契约第 3 节检查主机负载，原始输出追加到 facts/host-checks.txt。
- 全部日志写入 $YELLOW_OUTPUT/logs/<步骤>.log；失败时脚本停止并打印该步的意图与日志路径。

出错时怎么办（给执行本脚本的 agent）：
1. 先读打印出的日志末尾，定位是环境问题（服务未启动、端口、账号、动态库、磁盘）还是代码问题。
2. 环境问题：修复环境后重跑同一条命令，脚本会跳过已完成的步骤。
3. 代码问题：可以在本地修改工具包使其跑通，但不得改变查询语义、计时口径、冻结输入、
   四轮 Latin square 与 30/5 测量次数。修改后提交到本地分支；打包时脚本会把实际运行的
   源文件按摘要取回并随 feedback 回传，蓝区据此核对。
4. 某一步反复失败且无法修复时，用 --skip <步骤名> 跳过它继续后续步骤，并在回复中写明原因；
   打包结果会把该步标为缺失。
5. 主机检查不通过时先处理负载；确实无法满足时加 --accept-host-check 继续，检查原文仍会回传。
"""

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = STAGE_DIR.parents[1]
LAYOUTS = ("same_table", "separate", "full_core", "asset_ref")
WORKLOADS = ("main", "equal_total_few_large", "equal_total_many_medium", "correctness_only")
PREFIX = {"xstore": "xstore", "clickhouse": "ch"}
# 反馈契约第 3 节的通过条件。
LOAD_FRACTION = 0.10
MIN_AVAILABLE_GIB = 32
MIN_FREE_GIB = 200
MAX_BLOCKS_OUT = 10_000


class Step:
    """一个实验步骤：名字、输出路径、完成判据所检查的清单路径与命令。"""

    def __init__(self, name, output, manifests, command, intent):
        self.name = name
        self.output = Path(output)
        self.manifests = [Path(path) for path in manifests]
        self.command = [str(part) for part in command]
        self.intent = intent


def plan_steps(phase, input_root, output_root, clickhouse_port):
    """返回一个引擎阶段的固定步骤序列；输出名与 report/pack_handback.py 的 run_paths 一致。"""
    python = sys.executable
    runner = STAGE_DIR / "runner"
    root = Path(output_root)
    engine = phase
    prefix = PREFIX[engine]
    steps = []
    matrix = [python, runner / "run_layout_matrix.py", "--input", input_root, "--output", root / f"{engine}-main",
              "--engines", engine, "--layouts", ",".join(LAYOUTS), "--workloads", ",".join(WORKLOADS),
              "--measurements", "30", "--batch-measurements", "5"]
    if engine == "clickhouse":
        matrix += ["--clickhouse-host", "127.0.0.1", "--clickhouse-port", str(clickhouse_port)]
    steps.append(Step(
        f"{engine}-matrix", root / f"{engine}-main",
        [root / f"{engine}-main" / engine / layout / "run-manifest.json" for layout in LAYOUTS], matrix,
        "四布局四 workload 主矩阵，一次调用列出全部 workload；四个 target 都 complete 才算完成",
    ))
    if engine == "clickhouse":
        for layout in LAYOUTS:
            output = root / f"ch-part-states-{layout}"
            steps.append(Step(
                f"ch-part-states-{layout}", output, [output / "run-manifest.json"],
                [python, runner / "run_stage3.py", "part-states", "--input", input_root, "--output", output,
                 "--layout", layout],
                "ClickHouse part 状态控制：碎片态、合并中、自然稳定态、单 part 态各 360 个样本",
            ))
    for layout in LAYOUTS:
        output = root / f"{prefix}-interference-{layout}"
        steps.append(Step(
            f"{prefix}-interference-{layout}", output, [output / "run-manifest.json"],
            [python, runner / "run_stage3.py", "interference", "--input", input_root, "--output", output,
             "--layout", layout, "--engine", engine],
            "混合负载五阶段，每阶段 30 秒预热加 300 秒测量，约 28 分钟",
        ))
    output = root / f"{prefix}-asset-failures"
    steps.append(Step(
        f"{prefix}-asset-failures", output, [output / "run-manifest.json"],
        [python, runner / "run_stage3.py", "asset-failures", "--output", output, "--engine", engine],
        "asset_ref 六个固定故障用例，只记录分类与状态转换",
    ))
    if engine == "xstore":
        output = root / "xstore-row-probe.json"
        steps.append(Step(
            "xstore-row-probe", output, [output],
            [python, STAGE_DIR / "tools" / "probe_row_storage.py", "--input", input_root, "--output", output,
             "--engine", "xstore"],
            "载入 main 后测载荷列字节、reltoastrelid 与两种游标写法的计划，然后清理",
        ))
    return steps


def step_state(step):
    """返回 complete、absent 或 incomplete；只读清单的 status 字段。"""
    if not step.output.exists():
        return "absent"
    for manifest in step.manifests:
        try:
            if json.loads(manifest.read_text(encoding="utf-8")).get("status") != "complete":
                return "incomplete"
        except (OSError, ValueError):
            return "incomplete"
    return "complete"


def move_aside(path, clock=datetime.datetime.now):
    """把未完成的输出改名保留，返回新路径；runner 拒绝写入已存在的输出。"""
    stamp = clock().strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.attempt-{stamp}")
    path.rename(target)
    return target


def run_steps(steps, logs, skip=(), runner=subprocess.run, out=print):
    """依次执行步骤；完成的跳过，未完成的移开后重跑，失败时返回失败步骤名。"""
    logs.mkdir(parents=True, exist_ok=True)
    for step in steps:
        if step.name in skip:
            out(f"[skip] {step.name}: skipped by --skip")
            continue
        state = step_state(step)
        if state == "complete":
            out(f"[done] {step.name}")
            continue
        if state == "incomplete":
            out(f"[move] {step.name}: kept previous attempt at {move_aside(step.output)}")
        log = logs / f"{step.name}.log"
        out(f"[run ] {step.name}: {step.intent}; log {log}")
        with log.open("ab") as handle:
            handle.write(("\n$ " + " ".join(step.command) + "\n").encode())
            handle.flush()
            completed = runner(step.command, stdout=handle, stderr=subprocess.STDOUT, cwd=str(REPO_DIR))
        if completed.returncode != 0 or step_state(step) != "complete":
            out(f"[fail] {step.name}: exit {completed.returncode}; read the end of {log}")
            out("       intent: " + step.intent)
            out("       fix the cause and rerun the same command; completed steps are skipped")
            return step.name
    return None


# ---------- 主机检查与事实采集 ----------

def _command_output(command):
    """执行一条只读命令并返回带命令头的原文；命令不存在时记录原因而不中断。"""
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
        body = completed.stdout + completed.stderr
    except (OSError, subprocess.TimeoutExpired) as error:
        body = f"unavailable: {error}\n"
    return f"$ {' '.join(command)}\n{body}\n"


def evaluate_host(load_1m, cpu_count, available_kib, free_bytes, blocks_out):
    """按反馈契约第 3 节判定主机状态，返回不通过项的说明列表。"""
    failures = []
    if load_1m >= cpu_count * LOAD_FRACTION:
        failures.append(f"1-minute load {load_1m:.2f} is not below {cpu_count * LOAD_FRACTION:.1f}")
    if available_kib < MIN_AVAILABLE_GIB * 1024 * 1024:
        failures.append(f"available memory {available_kib // 1024 // 1024} GiB is not above {MIN_AVAILABLE_GIB} GiB")
    if free_bytes < MIN_FREE_GIB * 1024 ** 3:
        failures.append(f"free disk {free_bytes // 1024 ** 3} GiB is not above {MIN_FREE_GIB} GiB")
    if blocks_out and max(blocks_out) >= MAX_BLOCKS_OUT:
        failures.append(f"vmstat bo {blocks_out} reaches {MAX_BLOCKS_OUT}")
    return failures


def _vmstat_blocks_out(text):
    """取 vmstat 1 5 最后三次采样的 bo 列；格式不符时返回空列表。"""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    header = next((line for line in lines if "bo" in line), None)
    if header is None:
        return []
    column = header.index("bo")
    rows = [line for line in lines if line and line[0].isdigit() and len(line) > column]
    return [int(line[column]) for line in rows[-3:]]


def host_check(label, output_root, facts):
    """采集第 3 节六项检查原文，追加到 facts/host-checks.txt，并返回不通过项。"""
    vmstat = _command_output(["vmstat", "1", "5"])
    # 进程表只保留命令头与 CPU 占用最高的 20 行，与契约第 3 节的 head -20 一致。
    processes = "".join(_command_output(["ps", "-eo", "pid,comm,pcpu,rss", "--sort=-pcpu"])
                        .splitlines(keepends=True)[:22])
    text = "".join([
        f"### {label} {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n",
        _command_output(["uptime"]),
        processes + "\n",
        _command_output(["free", "-g"]),
        _command_output(["df", "-h", str(output_root)]),
        vmstat,
        _command_output(["date", "-u"]),
    ])
    facts.mkdir(parents=True, exist_ok=True)
    with (facts / "host-checks.txt").open("a", encoding="utf-8") as handle:
        handle.write(text)
    meminfo = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
    return evaluate_host(
        os.getloadavg()[0], os.cpu_count() or 1,
        int(meminfo.get("MemAvailable", "0 kB").split()[0]),
        shutil.disk_usage(output_root).free, _vmstat_blocks_out(vmstat),
    )


def _port_open(port):
    with socket.socket() as connection:
        connection.settimeout(1)
        return connection.connect_ex(("127.0.0.1", int(port))) == 0


def other_engine_running(phase, clickhouse_port):
    """契约要求两个引擎串行：返回另一引擎仍在运行的说明，未运行时返回 None。"""
    if phase == "xstore" and _port_open(clickhouse_port):
        return f"ClickHouse still listens on 127.0.0.1:{clickhouse_port}; stop it before the xstore phase"
    if phase == "clickhouse":
        # 只查运行账号自己的 XStore 实例；共享主机上其他用户的 gaussdb 无法停止，
        # 其负载由 host_check 的全机进程表记录并随 host-checks.txt 回传。
        found = subprocess.run(["pgrep", "-x", "-u", str(os.geteuid()), "gaussdb"],
                               capture_output=True, text=True)
        if found.returncode == 0:
            return "a gaussdb process of this account is running; stop XStore before the clickhouse phase"
    return None


NATIVE_REQUIRED = ("ENGINE", "ENGINE_VERSION", "PRODUCT_PATH", "PACKAGE_CHECKSUMS")


def preflight_problems(environment, input_root):
    """列出会让后续步骤必然失败的环境缺项。"""
    problems = []
    for name in ("XSTORE_USER", "GAUSSDB_LIB_DIR"):
        if not environment.get(name):
            problems.append(f"{name} is not exported (feedback contract section 2)")
    for prefix in ("XSTORE_NATIVE", "CH_NATIVE"):
        missing = [f"{prefix}_{key}" for key in NATIVE_REQUIRED if not environment.get(f"{prefix}_{key}")]
        if missing:
            problems.append("native package identity variables are missing: " + ", ".join(missing))
        elif not Path(environment[f"{prefix}_PRODUCT_PATH"]).is_file():
            problems.append(f"{prefix}_PRODUCT_PATH does not point to a file")
    for name in ("XSTORE_SOURCE_COMMIT", "XSTORE_BUILD_COMMAND"):
        if not environment.get(name):
            problems.append(f"{name} is not exported (feedback contract section 1.3)")
    # run_stage3.py 的控制项固定连接 127.0.0.1:18123，端口不同时这些步骤必然连接失败。
    if environment.get("CH_HTTP_PORT", "18123") != "18123":
        problems.append("CH_HTTP_PORT must be 18123: run_stage3.py control items use this fixed endpoint")
    if not (Path(input_root) / "truth.json").is_file():
        problems.append(f"frozen input is not unpacked at {input_root}")
    return problems


def write_preflight_facts(facts, environment):
    """记录硬件、XStore 重建事实、仓库状态与单元测试输出。"""
    facts.mkdir(parents=True, exist_ok=True)
    (facts / "hardware.txt").write_text("".join(_command_output(command) for command in (
        ["uname", "-a"], ["lscpu"], ["free", "-g"],
        ["lsblk", "-d", "-o", "NAME,ROTA,SIZE,MODEL,TYPE"],
        ["findmnt", "-T", environment.get("YELLOW_STATE", str(REPO_DIR))],
    )), encoding="utf-8")
    product = environment.get("XSTORE_NATIVE_PRODUCT_PATH", "")
    (facts / "xstore-build.txt").write_text("".join([
        f"source_commit: {environment.get('XSTORE_SOURCE_COMMIT', '')}\n",
        f"build_command: {environment.get('XSTORE_BUILD_COMMAND', '')}\n",
        f"product_path: {product}\n",
        _command_output(["sha256sum", product]) if product else "sha256sum: product path missing\n",
    ]), encoding="utf-8")
    (facts / "repository.txt").write_text("".join(_command_output(["git", "-C", str(REPO_DIR), *arguments])
                                                  for arguments in (["rev-parse", "HEAD"], ["status", "--short"],
                                                                    ["log", "--oneline", "-5"])),
                                          encoding="utf-8")
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(STAGE_DIR / "tests")],
                           capture_output=True, text=True, cwd=str(REPO_DIR))
    (facts / "unit-tests.txt").write_text(f"exit {tests.returncode}\n{tests.stdout}{tests.stderr}", encoding="utf-8")
    return tests.returncode


def write_clickhouse_facts(facts, port, environment):
    """记录 ClickHouse 版本、写入保护阈值与内存比例配置原文。"""
    import urllib.parse
    import urllib.request

    lines = []
    for query in ("SELECT version()",
                  "SELECT name, value FROM system.merge_tree_settings "
                  "WHERE name IN ('parts_to_delay_insert','parts_to_throw_insert')"):
        url = f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode({"query": query})
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                lines.append(f"$ {query}\n{response.read().decode()}\n")
        except OSError as error:
            lines.append(f"$ {query}\nunavailable: {error}\n")
    config = environment.get("CH_CONFIG")
    lines.append(_command_output(["grep", "-rn", "max_server_memory_usage_to_ram_ratio",
                                  str(Path(config).parent)]) if config else "CH_CONFIG is not exported\n")
    (facts / "clickhouse-settings.txt").write_text("".join(lines), encoding="utf-8")


# ---------- 命令行 ----------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("phase", choices=("preflight", "xstore", "clickhouse", "pack", "core", "check"))
    parser.add_argument("--output-root", type=Path, default=os.environ.get("YELLOW_OUTPUT"))
    parser.add_argument("--input", type=Path, default=(
        Path(os.environ["YELLOW_INPUT"]) / "json-storage-stage3-formal-input" if os.environ.get("YELLOW_INPUT")
        else None))
    parser.add_argument("--clickhouse-port", type=int, default=int(os.environ.get("CH_HTTP_PORT", "18123")))
    parser.add_argument("--skip", action="append", default=[], help="step name to skip, repeatable")
    parser.add_argument("--accept-host-check", action="store_true")
    parser.add_argument("--host", help="last octet of the host IP, required by pack")
    parser.add_argument("--date", default=datetime.date.today().isoformat())
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    if arguments.output_root is None or arguments.input is None:
        print("YELLOW_OUTPUT and YELLOW_INPUT must be exported, or pass --output-root and --input")
        return 2
    root = arguments.output_root.resolve()
    facts = root / "facts"
    root.mkdir(parents=True, exist_ok=True)
    if arguments.phase == "preflight":
        problems = preflight_problems(os.environ, arguments.input)
        for problem in problems:
            print("[preflight] " + problem)
        code = write_preflight_facts(facts, os.environ)
        print(f"[preflight] unit tests exit {code}; output in {facts / 'unit-tests.txt'}")
        return 1 if problems or code else 0
    if arguments.phase in {"pack", "core", "check"}:
        if not arguments.host:
            print(f"{arguments.phase} requires --host <last octet of the host IP>")
            return 2
        sys.path.insert(0, str(STAGE_DIR / "report"))
        import pack_handback

        feedback = REPO_DIR / "docs" / "yellow-handback" / f"{arguments.host}-{arguments.date}"
        if arguments.phase == "core":
            # 手敲精简版只读 pack 已生成的结果目录，可在 pack 之后任意次重新打印。
            return pack_handback.core_main([str(feedback)])
        if arguments.phase == "check":
            import check_handback

            if not feedback.is_dir():
                print(f"[check] {feedback} does not exist; run pack first or pass --date <pack date>")
                return 2

            lines = check_handback.check(feedback, root)
            print("\n".join(lines))
            print(f"[check] written to {feedback / 'followup' / 'checks.txt'}")
            # 任一项写成 "<编号> NA" 时以非零退出，提示执行方查看原因。
            return 1 if any(line.split(" ")[1:2] == ["NA"] for line in lines) else 0
        return pack_handback.main(["--output-root", str(root), "--repo", str(REPO_DIR),
                                   "--host", arguments.host, "--date", arguments.date])
    running = other_engine_running(arguments.phase, arguments.clickhouse_port)
    if running:
        print("[serial] " + running)
        return 1
    failures = host_check(f"before {arguments.phase} phase", root, facts)
    for failure in failures:
        print("[host] " + failure)
    if failures and not arguments.accept_host_check:
        print("[host] resolve the load or rerun with --accept-host-check; the raw checks are in facts/")
        return 1
    steps = plan_steps(arguments.phase, arguments.input.resolve(), root, arguments.clickhouse_port)
    failed = run_steps(steps, root / "logs", skip=set(arguments.skip))
    if arguments.phase == "clickhouse":
        write_clickhouse_facts(facts, arguments.clickhouse_port, os.environ)
    if failed:
        return 1
    following = {"xstore": "stop XStore, start ClickHouse, then run the clickhouse phase",
                 "clickhouse": "run the pack phase with --host <last octet>"}
    print("[next] " + following[arguments.phase])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
