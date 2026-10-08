"""由回传目录生成手敲版 V2：结果无法以文件离开本机时，按本文件给出的固定格式人工转述。

格式定义见反馈契约第 7.4 节，本文件是该定义的唯一实现。用法：

    python experiments/json-storage-stage3/tools/yellow_round.py hand --host <IP 末段> --date <回传目录日期>

前置：同一目录已执行 pack 与 check（本脚本读取 summary/、evidence/、facts/、followup/checks.txt）。
输出写入回传目录的 feedback-hand.txt，同时打印。

要点：
- 每节先单起一行写 "<节编号> <校验码>"，其后每行一个数据单元，行内字段只用空格分隔，不写表头。
- 布局行固定按 ST SP FC AR（same_table、separate、full_core、asset_ref）排列，不写布局名。
- 数值：小于 100 保留一位小数，大于等于 100 取整，去掉末尾的 .0；XQ 的服务端时间与 XD 的计时小于 10 时
  保留两位小数并去掉末尾的 0；缺值写 NA。
- 校验码为该节数据行的 CRC32 低 16 位（4 位十六进制），转述方照抄，接收方据此定位抄错的节。
- 某节生成失败时写 "<节编号> ERR"，其后一行为错误摘要，其余各节照常生成。
- N 行之后先列脚本生成的 "N auto" 备注（缺失运行、非 HEAD 代码、I 节改用 F2），转述方照抄后再补写自己的备注。

出错时怎么办（给执行本脚本的 agent）：
1. 先确认 pack 与 check 已在同一回传目录执行；缺少 followup/checks.txt 时 XQ、XD、I 中的部分字段为 NA。
2. 某节为 ERR 时照抄 ERR 行，并在 N 节用一句话写明原因；不要手工补算该节。
3. 本脚本整体无法运行时，改为转述 pack 生成的 feedback-core.txt，并在首行写 "V1 FALLBACK <错误摘要>"。
"""

import argparse
import json
import re
import statistics
import sys
import zlib
from pathlib import Path


STAGE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE_DIR))

LAYOUTS = ("same_table", "separate", "full_core", "asset_ref")
MATRIX_SCENARIOS = ("list:first", "list:middle", "preview:middle", "detail:text_64k",
                    "detail:entropy_512k", "detail:text_2m", "trace:p95", "batch:main")
PART_STATES = ("fragmented", "merging", "stable", "single_part")
ASSET_CASES = ("missing", "corrupt", "metadata_mismatch", "upload_then_db_failure",
               "publish_failure", "delete_failure")
ASSET_CODES = {"available": "A", "absent": "X", "failed": "F", "deleting": "D"}
# 驱动基准三个用例，与 bench_libpq_fetch.CASES 一致。
BENCH_CASES = ("list_first", "detail_64k", "detail_2m")
ROUNDTRIP_CASES = ("exec_select_1", "params_select_int", "params_catalog")


def number(value):
    """按格式规则写数值；None 写 NA。"""
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    text = f"{value:.0f}" if abs(value) >= 100 else f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def fine(value):
    """亚毫秒到 10 毫秒量级的计时：保留两位小数并去掉末尾的 0；10 及以上按 number 规则。"""
    if value is None:
        return "NA"
    if abs(value) >= 10:
        return number(value)
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def megabytes(value):
    """字节写为 10^6 字节单位、一位小数。"""
    return "NA" if value is None else number(round(value / 1_000_000, 1))


def crc(lines):
    """节校验码：数据行以换行连接后的 CRC32 低 16 位。"""
    return f"{zlib.crc32(chr(10).join(lines).encode('utf-8')) & 0xFFFF:04x}"


def _json(path):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


class Source:
    """回传目录内各文件的只读视图。"""

    def __init__(self, feedback_dir):
        self.dir = Path(feedback_dir)
        self.manifest = _json(self.dir / "feedback-manifest.json") or {}
        self.matrix = {}
        for engine in ("xstore", "clickhouse"):
            for entry in (_json(self.dir / "summary" / f"matrix-{engine}.json") or {}).get("matrix", []):
                self.matrix[(entry["engine"], entry["layout"])] = entry
        checks = self.dir / "followup" / "checks.txt"
        self.checks = checks.read_text(encoding="utf-8").splitlines() if checks.is_file() else []

    def scenario(self, engine, layout, workload, scenario, metric="application_ready_ms"):
        try:
            return self.matrix[(engine, layout)]["workloads"][workload]["scenarios"][scenario][
                "round_statistic_median"][metric]["p50"]
        except KeyError:
            return None

    def main_rounds(self, engine, layout):
        evidence = _json(self.dir / "evidence" / f"matrix-{engine}-{layout}.json") or {}
        return [item for item in evidence.get("rounds", []) if item["workload"] == "main"]

    def check_lines(self, code):
        return [line.split()[1:] for line in self.checks if line.startswith(code + " ")]


def section_h(source):
    """H：构建、版本、代码身份、汇总状态与主机负载。"""
    meta = source.manifest
    xstore = " ".join(meta.get("engine_versions", {}).get("xstore", []))
    build = re.search(r"build (\w+)", xstore)
    build_type = "R" if "release" in xstore else "D" if "debug" in xstore else "NA"
    clickhouse = ",".join(meta.get("engine_versions", {}).get("clickhouse", [])) or "NA"
    runs = meta.get("code_runs", [])
    presence = meta.get("presence", {})
    interference = all((source.dir / "summary" / f"interference-{engine}.json").is_file()
                       for engine in ("xstore", "clickhouse"))
    host_checks = source.dir / "facts" / "host-checks.txt"
    text = host_checks.read_text(encoding="utf-8") if host_checks.is_file() else ""
    loads = [float(value) for value in re.findall(r"load average: ([\d.]+)", text)]
    # 进入 ClickHouse 阶段时 XStore 已停止，此时仍占用 CPU 超过 5% 的 gaussdb 进程属于他人实例。
    last_clickhouse = text.split("### before clickhouse phase")[-1] if "before clickhouse phase" in text else ""
    others = sum(1 for line in last_clickhouse.splitlines()
                 if re.match(r"\s*\d+\s+gaussdb\S*\s+([\d.]+)", line)
                 and float(re.match(r"\s*\d+\s+gaussdb\S*\s+([\d.]+)", line).group(1)) > 5)
    return [" ".join((
        build.group(1) if build else "NA", build_type, clickhouse,
        (meta.get("repository", {}).get("head") or "NA")[:7],
        str(sum(not run["all_head"] for run in runs)),
        str(sum(not present for present in presence.values())),
        str(len(meta.get("summary_errors", []))),
        "P" if interference else "X",
        f"{min(loads):.0f}-{max(loads):.0f}" if loads else "NA",
        str(others) if last_clickhouse else "NA",
    ))]


def section_matrix(source, engine):
    """XM/CM：main workload 八个目标的应用可用 p50。"""
    return [" ".join(number(source.scenario(engine, layout, "main", scenario)) for scenario in MATRIX_SCENARIOS)
            for layout in LAYOUTS]


def section_xq(source):
    """XQ：XStore 列表第一页与中间页的查询完成、服务端时间与 Filter 移除行数。"""
    server = {}
    for fields in source.check_lines("F3"):
        # server <布局> <轮次> <场景> total_runtime_ms <值> top_node_ms <值> removed <行数>
        if fields[0] != "server" or len(fields) < 10 or fields[5] == "NA":
            continue
        layout, scenario = fields[1], fields[3]
        server.setdefault((layout, scenario), []).append((float(fields[5]), int(fields[9])))

    def median(layout, scenario, index):
        values = [item[index] for item in server.get((layout, scenario), [])]
        return statistics.median(values) if values else None

    lines = []
    for layout in LAYOUTS:
        removed = median(layout, "list:middle", 1)
        preview_removed = median(layout, "preview:middle", 1)
        lines.append(" ".join((
            number(source.scenario("xstore", layout, "main", "list:first", "query_complete_ms")),
            fine(median(layout, "list:first", 0)),
            number(source.scenario("xstore", layout, "main", "list:middle", "query_complete_ms")),
            fine(median(layout, "list:middle", 0)),
            "NA" if removed is None else str(int(removed)),
            "NA" if preview_removed is None else str(int(preview_removed)),
        )))
    return lines


def section_equal(source, engine):
    """XE/CE：两个等总字节 workload 的批量恢复 p50。"""
    return [" ".join(number(source.scenario(engine, layout, workload, f"batch:{workload}"))
                     for workload in ("equal_total_few_large", "equal_total_many_medium"))
            for layout in LAYOUTS]


def section_write(source, engine):
    """XW/CW：写入合计、末轮库内空间（MB）与对象存储（MB）。"""
    lines = []
    for layout in LAYOUTS:
        rounds = source.main_rounds(engine, layout)
        totals = [item["write"].get("block_ingest_wall_ms_sum") for item in rounds]
        write = statistics.median(totals) if rounds and None not in totals else None
        database = asset = None
        if rounds:
            storage = rounds[-1]["storage"]
            database = sum(table.get("total_bytes", table.get("compressed_bytes", 0)) or 0
                           for table in storage["tables"].values())
            asset = (storage.get("asset_store") or {}).get("available_bytes", 0)
        lines.append(" ".join((number(write), megabytes(database), megabytes(asset))))
    return lines


def _asset_token(source, engine):
    summary = _json(source.dir / "summary" / "asset-failures.json") or {}
    cases = {item["case"]: item for entry in summary.get("engines", []) if entry["engine"] == engine
             for item in entry["cases"]}
    if not cases:
        return "NA"
    states = [(cases.get(name) or {}).get("final_status") for name in ASSET_CASES]
    if all(state in ASSET_CODES for state in states):
        return "".join(ASSET_CODES[state] for state in states)
    # 出现编码表以外的终态时写全称，逗号分隔，避免信息被编码丢失。
    return ",".join(str(state) for state in states)


def section_xa(source):
    """XA：XStore Asset 六用例终态编码与行存探针两种游标写法的服务端时间、Filter 移除行数。"""
    probe = _json(source.dir / "facts" / "xstore-row-probe.json") or {}
    forms = {}
    for layout in probe.get("layouts", []):
        for name, form in (layout.get("cursor_probe") or {}).items():
            plan = form.get("plan") or ""
            total = re.search(r"Total runtime: ([\d.]+) ms", plan)
            removed = re.search(r"Rows Removed by Filter: (\d+)", plan)
            forms[name] = (float(total.group(1)) if total else None, int(removed.group(1)) if removed else 0)
    fields = [_asset_token(source, "xstore")]
    for name in ("expanded", "expanded_with_lower_bound"):
        runtime, removed = forms.get(name, (None, None))
        fields += [fine(runtime), "NA" if removed is None else str(removed)]
    return [" ".join(fields)]


def section_xd(source):
    """XD：XStore 往返下限三条 p50，驱动基准三个用例的 fetchall p50 与 list_first 的逐单元下限。"""
    roundtrip = {fields[0]: fine(float(fields[2])) for fields in source.check_lines("F4")
                 if len(fields) > 2 and fields[0] != "NA"}
    bench = {}
    for fields in source.check_lines("F6"):
        if fields and fields[0] in BENCH_CASES and "fetchall_p50_ms" in fields:
            bench[fields[0]] = (float(fields[fields.index("fetchall_p50_ms") + 1]),
                                float(fields[fields.index("getvalue_floor_p50_ms") + 1]))
    values = [roundtrip.get(name, "NA") for name in ROUNDTRIP_CASES]
    values += [fine(bench[name][0]) if name in bench else "NA" for name in BENCH_CASES]
    values.append(fine(bench["list_first"][1]) if "list_first" in bench else "NA")
    return [" ".join(values)]


def section_parts(source):
    """CP：ClickHouse 四个 part 状态下 list:first 的 p50。"""
    summary = _json(source.dir / "summary" / "part-states.json") or {}
    by_layout = {item["layout"]: item for item in summary.get("part_states", [])}
    lines = []
    for layout in LAYOUTS:
        states = {state["name"]: state for state in (by_layout.get(layout) or {}).get("states", [])}
        lines.append(" ".join(number(((states.get(name) or {}).get("scenarios") or {}).get("list:first", {})
                                     .get("application_ready_ms", {}).get("p50")) for name in PART_STATES))
    return lines


def _f2_layouts(source, prefix):
    """汇总缺失时的回退：从 F2 行重建 {布局: {阶段: {流: 统计}}}，结构与 interference 汇总一致。"""
    layouts = {}
    for fields in source.check_lines("F2"):
        # F2 <运行> <阶段> <流> <样本数> status=... dropped_by=... p50/p95/p99/max=...
        if len(fields) < 7 or not fields[0].startswith(f"{prefix}-interference-"):
            continue
        status = dict(item.split(":") for item in fields[4].split("=", 1)[1].split(",") if item)
        quantiles = fields[6].split("=", 1)[1].split("/")
        values = [None if value == "None" else float(value) for value in quantiles[:2]]
        layout = fields[0][len(f"{prefix}-interference-"):]
        layouts.setdefault(layout, {}).setdefault(fields[1], {})[fields[2]] = {
            "latency_ms": {"p50": values[0], "p95": values[1]},
            "counts": {"dropped_requests": int(status.get("dropped", 0)),
                       "successful_requests": int(status.get("success", 0))}}
    return layouts


def section_i(source):
    """I：两个引擎各四布局的混合负载，八行，XStore 在前；字段分五组，组间用 | 分隔。

    汇总要求四个布局齐全；只跑了部分布局时汇总缺失，该引擎改用 check 的 F2 行逐运行取数。
    """
    late = {}
    for fields in source.check_lines("F2"):
        # F2 <运行> <阶段> <流> ... dropped_by=arrival_deadline_missed:N,...
        if len(fields) > 5 and fields[1] == "batch_loop" and fields[2] == "list":
            match = re.search(r"arrival_deadline_missed:(\d+)", " ".join(fields))
            late[fields[0]] = int(match.group(1)) if match else 0
    lines = []
    for engine, prefix in (("xstore", "xstore"), ("clickhouse", "ch")):
        summary = _json(source.dir / "summary" / f"interference-{engine}.json")
        if summary:
            by_layout = {item["layout"]: {phase["phase"]: phase.get("streams", {}) for phase in item.get("phases", [])}
                         for item in summary.get("layouts", [])}
        else:
            by_layout = _f2_layouts(source, prefix)
        for layout in LAYOUTS:
            phases = by_layout.get(layout) or {}

            def stream(phase, name):
                return (phases.get(phase) or {}).get(name) or {}

            def latency(phase, name, key):
                return stream(phase, name).get("latency_ms", {}).get(key)

            def drops(phase, name="list"):
                count = stream(phase, name).get("counts", {}).get("dropped_requests")
                return "NA" if count is None else str(count)

            batch = stream("batch_loop", "batch_loop")
            groups = (
                f"{number(latency('quiet', 'list', 'p50'))} {number(latency('quiet', 'list', 'p95'))} {drops('quiet')}",
                f"{drops('detail_2m')} {drops('trace_long')}",
                f"{number(latency('batch_loop', 'list', 'p50'))} {number(latency('batch_loop', 'list', 'p95'))} "
                f"{drops('batch_loop')} {late.get(f'{prefix}-interference-{layout}', 'NA')}",
                f"{number(latency('continuous_ingest', 'list', 'p50'))} "
                f"{number(latency('continuous_ingest', 'list', 'p95'))} {drops('continuous_ingest')}",
                f"{number(batch.get('latency_ms', {}).get('p50'))} "
                f"{batch.get('counts', {}).get('successful_requests', 'NA')} "
                f"{number(latency('continuous_ingest', 'continuous_ingest', 'p50'))}",
            )
            lines.append("|".join(groups))
    return lines


SECTIONS = (
    ("H", section_h),
    ("XM", lambda source: section_matrix(source, "xstore")),
    ("XQ", section_xq),
    ("XE", lambda source: section_equal(source, "xstore")),
    ("XW", lambda source: section_write(source, "xstore")),
    ("XA", section_xa),
    ("XD", section_xd),
    ("I", section_i),
    ("CM", lambda source: section_matrix(source, "clickhouse")),
    ("CE", lambda source: section_equal(source, "clickhouse")),
    ("CW", lambda source: section_write(source, "clickhouse")),
    ("CP", section_parts),
    ("CA", lambda source: [_asset_token(source, "clickhouse")]),
)


def auto_notes(source):
    """N 节的自动备注：缺失的运行、使用非 HEAD 代码的运行、I 节改用 F2 取数的引擎。"""
    missing = {}
    for name, present in (source.manifest.get("presence") or {}).items():
        if not present:
            group, _, item = name.rpartition("/")
            missing.setdefault(group or item, []).append(item if group else "")
    notes = [f"N auto missing {group} {','.join(item for item in items if item)}".rstrip()
             for group, items in missing.items()]
    stale = sorted({run["run"].split("/")[0] for run in source.manifest.get("code_runs", []) if not run["all_head"]})
    notes += [f"N auto nonhead {run}" for run in stale]
    notes += [f"N auto I-from-F2 {engine}" for engine in ("xstore", "clickhouse")
              if not (source.dir / "summary" / f"interference-{engine}.json").is_file()]
    return notes


def hand_lines(feedback_dir, host, date):
    """返回手敲版 V2 的全部行。单节失败写 ERR 与错误摘要，不影响其余各节。"""
    source = Source(feedback_dir)
    lines = [f"V2 {host} {date}"]
    for code, produce in SECTIONS:
        try:
            data = produce(source)
        except Exception as error:  # noqa: BLE001 - 失败原因随手敲版回传
            lines += [f"{code} ERR", f"{type(error).__name__}: {str(error)[:80]}"]
            continue
        lines.append(f"{code} {crc(data)}")
        lines += data
    lines.append("N")
    try:
        lines += auto_notes(source)
    except Exception as error:  # noqa: BLE001 - 备注失败不影响各节
        lines.append(f"N auto ERR {type(error).__name__}")
    return lines


def verify_lines(lines):
    """接收方核对转述文本：返回 [(节编号, 结果)]，结果为 ok、mismatch、err 或 missing。

    节以 "<编号> <校验码>" 开头，到下一个节头或 N 行为止；缺少的节记为 missing。
    """
    codes = [code for code, _ in SECTIONS]
    found = {}
    current = None
    for raw in lines:
        line = raw.rstrip()
        head = line.split(" ")
        if head[0] in codes and len(head) == 2:
            current = head[0]
            found[current] = [head[1], []]
        elif head[0] == "N" or line.startswith("V"):
            current = None
        elif current is not None:
            found[current][1].append(line)
    results = []
    for code in codes:
        if code not in found:
            results.append((code, "missing"))
        elif found[code][0] == "ERR":
            results.append((code, "err"))
        else:
            results.append((code, "ok" if crc(found[code][1]) == found[code][0] else "mismatch"))
    return results


def main(argv=None):
    """生成 feedback-hand.txt 并打印；--verify 时只核对转述文本。返回 0，核对不通过时返回 1。"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("feedback_dir", type=Path, nargs="?")
    parser.add_argument("--host")
    parser.add_argument("--date")
    parser.add_argument("--verify", type=Path, help="转述文本文件；只核对各节校验码")
    arguments = parser.parse_args(argv)
    if arguments.verify:
        results = verify_lines(arguments.verify.read_text(encoding="utf-8").splitlines())
        for code, status in results:
            print(code, status)
        return 0 if all(status in ("ok", "missing") for _, status in results) else 1
    if arguments.feedback_dir is None or not arguments.host or not arguments.date:
        parser.error("feedback_dir, --host and --date are required unless --verify is given")
    lines = hand_lines(arguments.feedback_dir, arguments.host, arguments.date)
    (arguments.feedback_dir / "feedback-hand.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
