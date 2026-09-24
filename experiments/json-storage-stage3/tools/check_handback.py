"""阶段三回传目录的补充核对：从已完成的结果中取出判读所需、但 feedback.txt 未展开的事实。

用法（在 $YELLOW_REPO 下，pack 已生成 docs/yellow-handback/<IP 末段>-<日期>/）：

    python experiments/json-storage-stage3/tools/yellow_round.py check --host <IP 末段>

只读取 $YELLOW_OUTPUT 与回传目录，另在 XStore 上执行三条最小 SELECT；不重跑实验。
结果写入回传目录的 followup/checks.txt，同时打印。五项输出，每行以编号开头：

- F1 代码身份：非 HEAD 代码按运行目录与文件分组计数，列出全部 HEAD 运行与 local 文件副本。
  用于确认非 HEAD 代码只来自有意复用的运行，local 源文件都已随回传保留。
- F2 干扰负载样本：按运行、阶段、请求流给出状态计数、丢弃原因、成功请求耗时分位与最长连续丢弃。
  用于区分持续处理能力不足（worker capacity unavailable 均匀分布）与少数长请求造成的集中卡顿。
- F3 XStore 服务端时间：主矩阵每轮小查询计划中的 Total runtime 与顶层节点耗时，
  以及 result.json 中同场景的客户端中位数。用于判断小查询的客户端耗时主要花在服务端执行还是执行之外。
- F4 XStore 往返下限：同一连接上三类最小语句各 200 次的客户端耗时，连接方式与 runner 相同。
  用于判断小查询的固定耗时来自协议往返、参数绑定还是查询本身。XStore 未运行时该项写 NA 与原因。
- F5 feedback.txt 的 B3 段原文。

出错时怎么办（给执行本脚本的 agent）：
1. 某一项报错时，其余各项照常输出，该项写一行 `<编号> NA <错误>`；先看错误是否是字段名或路径与本机数据不符。
2. 字段名不符时按上面的用途修正本脚本后重跑；修改后的脚本复制到 followup/ 下一并提交，回复中写明修改点。
3. F4 需要 XSTORE_USER 与 GAUSSDB_LIB_DIR；端口取 XSTORE_PORT，默认与 runner 相同的 29000。
"""

import collections
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_DIR / "report"))

import pack_handback  # noqa: E402 - 固定目录名与打包脚本共用

SMALL_SCENARIOS = ("list:first", "preview:first", "detail:text_64k")
ROUNDTRIP_CASES = (
    ("exec_select_1", "SELECT 1", None),
    ("params_select_int", "SELECT %s::int", (1,)),
    ("params_catalog", "SELECT relname FROM pg_class WHERE relname = %s", ("pg_class",)),
)


def _counts(counter):
    return ",".join(f"{str(key).replace(' ', '_')}:{value}"
                    for key, value in sorted(counter.items(), key=lambda item: str(item[0])))


def _quantile(values, q, digits=1):
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], digits) if values else "NA"


def code_lines(feedback_dir):
    """F1：读取 feedback-manifest.json 的 code_runs 与 code_files。"""
    meta = json.loads((Path(feedback_dir) / "feedback-manifest.json").read_text(encoding="utf-8"))
    runs = meta["code_runs"]
    lines = [f"F1 runs {len(runs)} non_head {sum(not run['all_head'] for run in runs)}"]
    groups = collections.Counter()
    for run in runs:
        for entry in run["code"]:
            if entry["status"] != "head":
                groups[(run["run"].split("/")[0], entry["path"], entry["status"])] += 1
    lines += [f"F1 non_head {directory} {path} {status} {count}"
              for (directory, path, status), count in sorted(groups.items())]
    lines += [f"F1 head_run {run['run']}" for run in runs if run["all_head"]]
    for item in meta["code_files"]:
        if item["status"] == "local":
            copy = item.get("copy") or "NA"
            kept = (Path(feedback_dir) / copy).is_file()
            lines.append(f"F1 local {item['path']} {item['sha256'][:12]} {copy} kept={kept}")
    return lines


def interference_lines(output_root):
    """F2：逐个干扰运行目录读取各阶段的正式样本 samples.jsonl。"""
    lines = []
    paths = pack_handback.run_paths(output_root)
    for engine in pack_handback.ENGINES:
        for layout in pack_handback.LAYOUTS:
            run = paths[("interference", engine, layout)]
            files = sorted(run.rglob("samples.jsonl"))
            if not files:
                lines.append(f"F2 NA {run.name} has no samples.jsonl")
            for path in files:
                streams = collections.defaultdict(list)
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        streams[row["stream"]].append(row)
                for stream, rows in sorted(streams.items()):
                    rows.sort(key=lambda row: row["sequence"])
                    durations = [row["duration_ms"] for row in rows if row["status"] == "success"]
                    longest = current = 0
                    for row in rows:
                        current = current + 1 if row["status"] == "dropped" else 0
                        longest = max(longest, current)
                    lines.append(" ".join((
                        "F2", run.name, path.parent.name, stream, str(len(rows)),
                        "status=" + _counts(collections.Counter(row["status"] for row in rows)),
                        "dropped_by=" + _counts(collections.Counter(
                            row.get("error") for row in rows if row["status"] == "dropped")),
                        "p50/p95/p99/max=" + "/".join(str(_quantile(durations, q)) for q in (0.5, 0.95, 0.99, 1.0)),
                        f"max_consecutive_dropped={longest}",
                    )))
    return lines


def xstore_plan_lines(output_root):
    """F3：XStore 主矩阵 main workload 的客户端中位数与逐轮计划时间。"""
    lines = []
    paths = pack_handback.run_paths(output_root)
    for layout in pack_handback.LAYOUTS:
        target = paths[("matrix", "xstore", layout)]
        summary = json.loads((target / "result.json").read_text(encoding="utf-8"))["summary"]["main"]
        for scenario in SMALL_SCENARIOS:
            item = summary[scenario]
            lines.append(f"F3 client {layout} {scenario} latency_median {item['latency_ms']['median']} "
                         f"query_complete_median {item['query_complete_ms']['median']}")
        for manifest in sorted((target / "rounds" / "main").glob("round-*/run-manifest.json")):
            document = json.loads(manifest.read_text(encoding="utf-8"))
            plans = document.get("access", {}).get("plans", {})
            for query, validation in sorted(document.get("access_validation", {}).items()):
                if validation.get("scenario") not in SMALL_SCENARIOS:
                    continue
                plan = plans.get(query) or ""
                plan = plan if isinstance(plan, str) else json.dumps(plan)
                total = re.search(r"Total runtime: ([\d.]+) ms", plan)
                top = re.search(r"actual time=[\d.]+\.\.([\d.]+)", plan)
                lines.append(f"F3 server {layout} {manifest.parent.name} {validation['scenario']} "
                             f"total_runtime_ms {total.group(1) if total else 'NA'} "
                             f"top_node_ms {top.group(1) if top else 'NA'}")
    return lines


def roundtrip_lines(connect=None, repeat=200, warmup=20):
    """F4：在一条 XStore 连接上逐条计时最小语句；connect 供测试替换。"""
    if connect is None:
        sys.path.insert(0, str(STAGE_DIR / "runner"))
        import gaussdb_libpq

        def connect():
            return gaussdb_libpq.connect(
                f"host={os.environ.get('XSTORE_SOCKET_DIR', '/tmp')} port={os.environ.get('XSTORE_PORT', '29000')} "
                f"dbname={os.environ.get('XSTORE_DBNAME', 'postgres')} user={os.environ['XSTORE_USER']}")
    connection = connect()
    try:
        cursor = connection.cursor()
        lines = []
        for name, statement, parameters in ROUNDTRIP_CASES:
            for _ in range(warmup):
                cursor.execute(statement, parameters)
                cursor.fetchall()
            times = []
            for _ in range(repeat):
                start = time.perf_counter()
                cursor.execute(statement, parameters)
                cursor.fetchall()
                times.append((time.perf_counter() - start) * 1000)
            lines.append(f"F4 {name} p50 {statistics.median(times):.2f} p95 {_quantile(times, 0.95, 2)} "
                         f"max {max(times):.2f}")
        connection.rollback()
        return lines
    finally:
        connection.close()


def b3_lines(feedback_dir):
    """F5：feedback.txt 中 B3 与 B4 之间的原文。"""
    lines = (Path(feedback_dir) / "feedback.txt").read_text(encoding="utf-8").splitlines()
    return ["F5 " + line for line in lines[lines.index("B3") + 1:lines.index("B4")]]


def check(feedback_dir, output_root, connect=None):
    """依次生成五项；单项失败写 NA 与错误，不影响其余各项。返回全部行。"""
    lines = []
    for code, produce in (("F1", lambda: code_lines(feedback_dir)),
                          ("F2", lambda: interference_lines(output_root)),
                          ("F3", lambda: xstore_plan_lines(output_root)),
                          ("F4", lambda: roundtrip_lines(connect)),
                          ("F5", lambda: b3_lines(feedback_dir))):
        try:
            lines += produce()
        except Exception as error:  # noqa: BLE001 - 每项的失败原因都要回传
            lines.append(f"{code} NA {type(error).__name__}: {error}")
    target = Path(feedback_dir) / "followup" / "checks.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines
