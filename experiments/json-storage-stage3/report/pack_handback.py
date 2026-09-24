"""把一台黄区主机一轮运行的产物打包为可提交的 feedback 目录。

输入是固定命名的输出根（见黄区指南第 7 节）。打包结果包含五部分：
汇总器按族、按引擎分别产出的 summary、各主矩阵 target 的 result.json、从轮次清单抽取的紧凑证据、
实际运行代码的身份与本地修改原文、全部 run manifest 的压缩分片归档，以及按反馈契约第 7 节
自动生成的数据段。缺失的族、汇总失败、代码不在 HEAD、引擎版本不一致均写入包清单并打印，
不阻断打包；只有目标目录已存在或输出根不可读时失败。

出错时怎么办：
1. 目标目录已存在：确认旧包已提交或不再需要后删除该目录，重跑即可，本脚本不修改输出根。
2. 打印的 summary error 是汇总器拒绝某一族的原因，照原文回传，不要为通过汇总而改动运行产物。
3. 抽取或数据段生成因字段缺失抛出异常时，可在本地修正对应的 extract_round 或
   feedback_lines 分支，保持输出字段名不变，并提交到本地分支；修改后的本脚本会作为
   local 代码随包回传。
4. 所有输出文件都低于 PART_BYTES；提交或传输受文件大小限制时先核对 feedback-manifest.json 的 files 列表。
"""

import argparse
import hashlib
import json
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
from pathlib import Path


REPORT_DIR = Path(__file__).resolve().parent
if str(REPORT_DIR) not in sys.path:
    sys.path.insert(0, str(REPORT_DIR))

import summarize


FORMAT = "agent-trace-json-storage-stage3-yellow-feedback"
FORMAT_VERSION = 1
ENGINES = ("xstore", "clickhouse")
ENGINE_PREFIX = {"xstore": "xstore", "clickhouse": "ch"}
LAYOUTS = ("same_table", "separate", "full_core", "asset_ref")
STAGE_PREFIX = "experiments/json-storage-stage3/"
# 归档分片上限低于 Git 托管服务常见的单文件 50 MiB 警告线；任何输出文件都不超过该值。
PART_BYTES = 40 * 1024 * 1024
FACT_FILE_LIMIT = 1024 * 1024
RAW_SAMPLE_SUFFIX = ".jsonl"


def run_paths(root):
    """返回输出根下各族的固定目录；键为 (族, 引擎, 布局)，布局对 Asset 故障为 None。"""
    root = Path(root)
    paths = {}
    for engine in ENGINES:
        prefix = ENGINE_PREFIX[engine]
        for layout in LAYOUTS:
            paths[("matrix", engine, layout)] = root / f"{engine}-main" / engine / layout
            paths[("interference", engine, layout)] = root / f"{prefix}-interference-{layout}"
        paths[("asset-failures", engine, None)] = root / f"{prefix}-asset-failures"
    for layout in LAYOUTS:
        paths[("part-states", "clickhouse", layout)] = root / f"ch-part-states-{layout}"
    return paths


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def _git(repo, *arguments):
    return subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, check=True).stdout


def manifests_under(directory):
    """列出目录内全部 run manifest（target、轮次、信封与 child），按路径排序。"""
    return sorted(Path(directory).rglob("run-manifest.json")) if Path(directory).is_dir() else []


# ---------- 代码身份 ----------

def _relative_stage_path(recorded):
    marker = recorded.replace("\\", "/").find(STAGE_PREFIX)
    return None if marker < 0 else recorded.replace("\\", "/")[marker:]


class CodeIndex:
    """按仓库历史判定一份源文件字节来自 HEAD、已发布提交还是本地修改。"""

    def __init__(self, repo):
        self.repo = Path(repo)
        self.head = _git(repo, "rev-parse", "HEAD").decode().strip()
        self._history = {}

    def _versions(self, relative):
        if relative not in self._history:
            commits = _git(self.repo, "rev-list", "HEAD", "--", relative).decode().split()
            versions = {}
            for commit in reversed(commits):
                try:
                    content = _git(self.repo, "show", f"{commit}:{relative}")
                except subprocess.CalledProcessError:
                    continue
                versions.setdefault(hashlib.sha256(content).hexdigest(), commit)
            try:
                head = _git(self.repo, "show", f"HEAD:{relative}")
                self._history[relative] = (hashlib.sha256(head).hexdigest(), versions)
            except subprocess.CalledProcessError:
                self._history[relative] = (None, versions)
        return self._history[relative]

    def classify(self, recorded_path, sha256):
        """返回 (归类, 仓库相对路径)；归类为 head、published:<提交> 或 local。"""
        relative = _relative_stage_path(recorded_path)
        if relative is None:
            return "outside-toolkit", None
        head_sha, versions = self._versions(relative)
        if sha256 == head_sha:
            return "head", relative
        if sha256 in versions:
            return f"published:{versions[sha256][:12]}", relative
        return "local", relative


def _code_entries(document):
    code = document.get("code")
    if not isinstance(code, dict):
        return []
    return [(role, item["path"], item["sha256"]) for role, item in sorted(code.items())
            if isinstance(item, dict) and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)]


def collect_code_identity(documents, index, repo, destination):
    """汇总每份运行记录的代码身份，把 local 文件在本机可找回的原文复制到 code/local/。"""
    files = {}
    runs = []
    for label, document in documents:
        entries = []
        for role, recorded, sha256 in _code_entries(document):
            status, relative = index.classify(recorded, sha256)
            entries.append({"role": role, "path": relative or recorded, "sha256": sha256, "status": status})
            files.setdefault((relative or recorded, sha256), {"recorded_path": recorded, "status": status})
        if entries:
            runs.append({"run": label, "code": entries,
                         "all_head": all(entry["status"] == "head" for entry in entries)})
    for (path, sha256), item in sorted(files.items()):
        if item["status"] != "local":
            continue
        candidates = [Path(item["recorded_path"])]
        if path.startswith(STAGE_PREFIX):
            candidates.append(Path(repo) / path)
        match = next((candidate for candidate in candidates
                      if candidate.is_file() and _sha256_file(candidate) == sha256), None)
        if match is None:
            item["copy"] = "unrecoverable: no file on this host has the recorded digest"
            continue
        target = destination / "code" / "local" / sha256[:12] / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(match, target)
        item["copy"] = str(target.relative_to(destination))
    return runs, [{"path": path, "sha256": sha256, **item} for (path, sha256), item in sorted(files.items())]


# ---------- 证据抽取 ----------

_PLAN_DROP = ("Index Cond", "Filter:", "Sort Key", "Sort Method", "(Buffers", "Buffers:", "Total runtime",
              "Hash Cond", "Join Filter", "EXPLAIN", "Planning", "Execution", "Recheck Cond", "Output:")


def plan_signature(plan):
    """把单条计划文本压缩为节点签名：去掉代价、时间、谓词取值与 namespace 前缀。"""
    nodes = []
    for raw in str(plan).splitlines():
        line = raw.strip()
        if line.startswith("{") and '"explain"' in line:
            try:
                line = json.loads(line)["explain"].strip()
            except (json.JSONDecodeError, KeyError):
                continue
            if not re.match(r"(ReadFromMergeTree|Join|Parts:|Granules:|Filling|Sorting|Limit)", line):
                continue
        else:
            line = line.lstrip("->").strip()
            if not line or line.startswith(_PLAN_DROP):
                continue
            line = re.sub(r"\s*\(cost=.*$", "", line)
            line = re.sub(r"\s*\(actual .*$", "", line)
        line = re.sub(r"\bjsons3_[A-Za-z0-9_]+\.", "", line)
        nodes.append(line)
    return " > ".join(nodes)


def _span(values):
    values = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return {"minimum": min(values), "maximum": max(values)} if values else None


def extract_round(manifest):
    """从一个矩阵轮次清单抽取写入、空间与按 scenario 分组的访问证据。"""
    write = manifest.get("write") or {}
    blocks = write.get("blocks") or []
    access = manifest.get("access") or {}
    plans = access.get("plans") or {}
    finish = access.get("query_finish") or {}
    details = access.get("query_details") or {}
    scenarios = {}
    for query_id, item in sorted((manifest.get("access_validation") or {}).items()):
        group = scenarios.setdefault(item.get("scenario"), {
            "kind": item.get("kind"), "samples": 0, "plans": {}, "scanned_rows": [],
            "scanned_bytes": [], "read_rows": [], "read_bytes": [], "payload_selected": set(),
            "access_structure": set(), "expected_sources": set(),
        })
        group["samples"] += 1
        group["scanned_rows"].append(item.get("scanned_rows"))
        group["scanned_bytes"].append(item.get("scanned_bytes"))
        group["access_structure"].add(str(item.get("access_structure")))
        group["expected_sources"].update(re.sub(r"^jsons3_[A-Za-z0-9_]+\.", "", source)
                                         for source in item.get("expected_sources") or ())
        if query_id in details:
            group["payload_selected"].add(bool(details[query_id].get("payload_selected")))
        if query_id in finish:
            group["read_rows"].append(finish[query_id].get("read_rows"))
            group["read_bytes"].append(finish[query_id].get("read_bytes"))
        if query_id in plans:
            signature = plan_signature(plans[query_id])
            group["plans"][signature] = group["plans"].get(signature, 0) + 1
    for group in scenarios.values():
        for key in ("scanned_rows", "scanned_bytes", "read_rows", "read_bytes"):
            group[key] = _span(group[key])
        for key in ("payload_selected", "access_structure", "expected_sources"):
            group[key] = sorted(group[key])
    return {
        "workload": manifest.get("workload"), "round_index": manifest.get("round_index"),
        "status": manifest.get("status"), "correctness": manifest.get("correctness"),
        "write": {
            "wall_ms": write.get("wall_ms"),
            "block_ingest_wall_ms_sum": sum(block["ingest"]["wall_ms"] for block in blocks)
            if blocks and all(isinstance(block.get("ingest", {}).get("wall_ms"), (int, float))
                              for block in blocks) else None,
            "block_wall_ms": write.get("block_wall_ms"), "asset_publish_ms": write.get("asset_publish_ms"),
            "rows_per_second": write.get("rows_per_second"),
        },
        "maintenance": manifest.get("maintenance"),
        "storage": manifest.get("storage"),
        "index_scans": access.get("index_scans"),
        "scenarios": scenarios,
        "engine_runtime": manifest.get("engine_runtime"), "container": manifest.get("container"),
        "host": manifest.get("host"),
    }


def extract_target(target_dir):
    """抽取一个矩阵 target 的全部轮次，按 workload 与轮次排序。"""
    rounds = [extract_round(_read_json(path)) for path in sorted(Path(target_dir).glob("rounds/*/round-*/run-manifest.json"))]
    rounds.sort(key=lambda item: (str(item["workload"]), item["round_index"] if item["round_index"] is not None else -1))
    return {"target": str(target_dir), "rounds": rounds}


# ---------- 反馈数据段 ----------

def _fmt(value, digits=1, reason="缺失"):
    if value is None:
        return f"NA {reason}"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _main_rounds(extract):
    return [item for item in (extract or {}).get("rounds", []) if item["workload"] == "main"]


def feedback_lines(matrix, extracts, part_states, interference, asset_failures, probe, identity,
                   matrix_present=None):
    """按反馈契约第 7 节生成数据段；缺失值写 NA 与原因，不估算。"""
    lines = []
    matrix_present = matrix_present or {}
    order = [(engine, layout) for engine in ENGINES for layout in LAYOUTS]
    entries = {}
    for engine in ENGINES:
        for entry in (matrix.get(engine) or {}).get("matrix", []):
            entries[(entry["engine"], entry["layout"])] = entry

    def p50(engine, layout, workload, scenario):
        try:
            return entries[(engine, layout)]["workloads"][workload]["scenarios"][scenario][
                "round_statistic_median"]["application_ready_ms"]["p50"]
        except KeyError:
            return None

    items = {
        "A1": [("main", q) for q in ("list:first", "list:middle", "preview:first", "preview:middle")],
        "A2": [("main", q) for q in ("detail:text_64k", "detail:text_512k", "detail:text_2m", "detail:entropy_512k")],
        "A3": [("main", q) for q in ("trace:p25", "trace:p50", "trace:p95", "batch:main")],
        "A4": [("equal_total_few_large", "batch:equal_total_few_large"),
               ("equal_total_many_medium", "batch:equal_total_many_medium")],
    }
    # 目录齐全但汇总器拒绝时写“汇总失败”，拒绝原因见 feedback-manifest.json 的 summary_errors。
    matrix_reason = {engine: ("汇总失败" if matrix_present.get(engine) else "缺失") for engine in ENGINES}
    for code, scenarios in items.items():
        lines.append(code)
        for engine, layout in order:
            lines.append(" ".join(_fmt(p50(engine, layout, w, q), reason=matrix_reason[engine])
                                  for w, q in scenarios))
    lines.append("A5")
    for key in order:
        rounds = _main_rounds(extracts.get(key))
        totals = [item["write"]["block_ingest_wall_ms_sum"] for item in rounds]
        medians = [(item["write"]["block_wall_ms"] or {}).get("median") for item in rounds]
        total = statistics.median(totals) if rounds and None not in totals else None
        median = statistics.median(medians) if rounds and None not in medians else None
        lines.append(f"{_fmt(total)} {_fmt(median)}")
    lines.append("A6")
    for key in order:
        rounds = _main_rounds(extracts.get(key))
        storage = rounds[-1]["storage"] if rounds else None
        if not storage:
            lines.append("NA 缺失")
            continue
        parts = []
        for table, value in sorted(storage["tables"].items()):
            fields = (("heap_bytes", "index_bytes", "toast_bytes", "total_bytes") if "heap_bytes" in value
                      else ("part_count", "rows", "marks", "compressed_bytes", "uncompressed_bytes"))
            parts.append(table + ":" + ",".join(str(value.get(field)) for field in fields))
        asset = storage.get("asset_store") or {}
        if asset.get("available_bytes"):
            parts.append(f"asset_store:{asset['available_bytes']},{asset['available_object_count']}")
        lines.append(";".join(parts))
    lines.append("A7")
    for engine, layout in order:
        try:
            response = entries[(engine, layout)]["workloads"]["main"]["scenarios"]["batch:main"][
                "round_statistic_median"]["response_bytes"]
            lines.append(" ".join(_fmt(response.get(field), 0) for field in ("database", "resolver_payload", "total")))
        except KeyError:
            lines.append("NA 缺失")
    lines.append("A8")
    for key in order:
        rounds = _main_rounds(extracts.get(key))
        spans = []
        for kind in ("list", "detail", "trace"):
            values = []
            for item in rounds:
                for group in item["scenarios"].values():
                    if group["kind"] == kind and group["scanned_rows"]:
                        values += [group["scanned_rows"]["minimum"], group["scanned_rows"]["maximum"]]
            spans += [_fmt(min(values)) if values else "NA", _fmt(max(values)) if values else "NA"]
        lines.append(" ".join(spans))
    lines.append("B1")
    probe_layouts = {item["layout"]: item for item in (probe or {}).get("layouts", [])}
    for layout in LAYOUTS:
        if layout == "asset_ref":
            lines.append("NA 载荷在库外")
            continue
        profiles = probe_layouts.get(layout, {}).get("payload_profiles")
        if not profiles:
            lines.append("NA 探针缺失")
            continue
        for item in profiles:
            lines.append(f"{item['profile']} {item['rows']} {item['logical_bytes']} {item['stored_bytes']}")
    for layout in LAYOUTS:
        rounds = _main_rounds(extracts.get(("clickhouse", layout)))
        tables = (rounds[-1]["storage"] or {}).get("tables", {}) if rounds else {}
        columns = [value.get("columns", {}).get("payload") for value in tables.values()]
        columns = [column for column in columns if column]
        lines.append(" ".join(str(sum(column[field] for column in columns))
                              for field in ("compressed_bytes", "uncompressed_bytes")) if columns
                     else ("NA 载荷在库外" if layout == "asset_ref" else "NA 缺失"))
    lines.append("B2")
    states_by_layout = {item["layout"]: item for item in (part_states or {}).get("part_states", [])}
    for layout in LAYOUTS:
        item = states_by_layout.get(layout)
        if not item:
            lines += ["NA 缺失"] * 4
            continue
        for state in item["states"]:
            table = (state.get("tables") or {}).get(item["controlled_table"], {})
            value = (state.get("scenarios") or {}).get("list:first", {}).get("application_ready_ms", {}).get("p50")
            lines.append(f"{_fmt(value)} {_fmt(table.get('part_count'))} {_fmt(state.get('active_merge_count'))}")
    lines.append("B3")
    for engine in ENGINES:
        layouts = {item["layout"]: item for item in (interference.get(engine) or {}).get("layouts", [])}
        for layout in LAYOUTS:
            item = layouts.get(layout)
            if not item:
                lines += ["NA 缺失"] * 5
                continue
            for phase in item["phases"]:
                streams = phase.get("streams") or {}
                snapshots = phase.get("snapshots") or [{}]
                model = snapshots[0].get("storage_model", "part")
                lines.append(" ".join([
                    _fmt(streams.get("list", {}).get("latency_ms", {}).get("p50")),
                    _fmt(streams.get("preview", {}).get("latency_ms", {}).get("p50")),
                    _fmt(streams.get("list", {}).get("counts", {}).get("dropped_requests")),
                    _fmt(streams.get("preview", {}).get("counts", {}).get("dropped_requests")),
                    str(model),
                ]))
    lines.append("B4")
    engines = {item["engine"]: item for item in (asset_failures or {}).get("engines", [])}
    for engine in ENGINES:
        cases = {case["case"]: case for case in (engines.get(engine) or {}).get("cases", [])}
        for name in ("missing", "corrupt", "metadata_mismatch", "upload_then_db_failure",
                     "publish_failure", "delete_failure"):
            case = cases.get(name)
            if not case:
                lines.append("NA 缺失")
                continue
            lines.append(" ".join([
                str(case.get("injection_point")), str((case.get("resolver") or {}).get("error")),
                str(case.get("final_status")), str(case.get("event_visible")),
                str((case.get("reconcile") or {}).get("orphan_count")),
                ",".join(case.get("recovery_actions") or []) or "none",
            ]))
    lines.append("C4")
    for layout in LAYOUTS:
        relations = probe_layouts.get(layout, {}).get("relations")
        tables = [item for item in relations or [] if item.get("relkind") == "r"]
        gate = []
        for item in _main_rounds(extracts.get(("xstore", layout)))[-1:]:
            for scenario, group in sorted(item["scenarios"].items()):
                if group["kind"] in {"list", "detail", "trace"}:
                    gate.append("index" if all("Index" in plan for plan in group["plans"]) and group["plans"]
                                else "non-index")
        verdict = ("NA 缺失" if not gate else "pass" if set(gate) == {"index"} else "fail")
        toast = ";".join(f"{item['relname']}:{item['reltoastrelid']}:{item['reloptions']}" for item in tables)
        lines.append(f"{toast or 'NA 探针缺失'} {verdict}")
    lines.append("C5")
    cursor = probe_layouts.get("same_table", {}).get("cursor_probe") or {}
    for name in ("expanded", "expanded_with_lower_bound"):
        plan = (cursor.get(name) or {}).get("plan")
        if not plan:
            lines.append("NA 探针缺失")
            continue
        scan = next((line.strip().lstrip("->").strip() for line in plan.splitlines() if "Scan" in line), "")
        rows = re.search(r"actual time=[^)]* rows=(\d+)", scan)
        node = re.sub(r" *\(.*$", "", scan) or "NA"
        lines.append(f"{node} {rows.group(1) if rows else 'NA'}")
    lines.append("C6")
    lines.append(f"{identity['head'][:7]} {identity['summary']}")
    return lines


# ---------- 原始清单归档 ----------

def archive_manifests(sources, root, destination, part_bytes=PART_BYTES):
    """把全部 run manifest、result.json 与探针 JSON 打成 tar.gz 并按上限切片，返回清单。"""
    members = sorted({path for path in sources if path.is_file()})
    raw = destination / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    whole = raw / "manifests.tar.gz"
    with tarfile.open(whole, mode="w:gz", compresslevel=6) as archive:
        for path in members:
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    digest = _sha256_file(whole)
    size = whole.stat().st_size
    parts = []
    with whole.open("rb") as handle:
        for index in range(max(1, -(-size // part_bytes))):
            part = raw / f"manifests.tar.gz.part-{index:03d}"
            part.write_bytes(handle.read(part_bytes))
            parts.append(part.name)
    whole.unlink()
    return {
        "archive_sha256": digest, "archive_bytes": size,
        "parts": parts, "reassemble": "cat manifests.tar.gz.part-* > manifests.tar.gz",
        "members": [{"path": str(path.relative_to(root)), "bytes": path.stat().st_size,
                     "sha256": _sha256_file(path)} for path in members],
    }


# ---------- 手敲精简版 ----------

CORE_STATES = ("fragmented", "merging", "stable", "single_part")
CORE_CASES = ("missing", "corrupt", "metadata_mismatch", "upload_then_db_failure", "publish_failure",
              "delete_failure")


def _optional_json(path):
    return _read_json(path) if Path(path).is_file() else None


def core_lines(feedback_dir, meta):
    """由结果目录生成手敲精简版：固定项目与字段顺序，只保留核心数值。

    入参 feedback_dir 为 pack 的输出目录，meta 为 feedback-manifest.json 的内容。
    每项先单起一行写编号，其后每行一个数据单元；缺值写 NA，不写原因，原因见 feedback.txt。
    """
    directory = Path(feedback_dir)
    order = [(engine, layout) for engine in ENGINES for layout in LAYOUTS]
    matrix = {}
    for engine in ENGINES:
        for entry in (_optional_json(directory / "summary" / f"matrix-{engine}.json") or {}).get("matrix", []):
            matrix[(entry["engine"], entry["layout"])] = entry
    evidence = {key: _optional_json(directory / "evidence" / f"matrix-{key[0]}-{key[1]}.json") for key in order}

    def value(item, digits=1):
        return "NA" if item is None else (f"{item:.{digits}f}" if isinstance(item, float) else str(item))

    def p50(key, workload, scenario):
        try:
            return matrix[key]["workloads"][workload]["scenarios"][scenario]["round_statistic_median"][
                "application_ready_ms"]["p50"]
        except KeyError:
            return None

    def main_rounds(key):
        return [item for item in (evidence[key] or {}).get("rounds", []) if item["workload"] == "main"]

    host = next((item["host"] for key in order for item in main_rounds(key) if item.get("host")), {}) or {}
    xstore = " ".join(meta.get("engine_versions", {}).get("xstore", []))
    build = re.search(r"build (\w+)", xstore)
    build_type = "release" if "release" in xstore else "debug" if "debug" in xstore else None
    clickhouse = ",".join(meta.get("engine_versions", {}).get("clickhouse", [])) or None
    memory = host.get("memory_total_kib")
    lines = ["H", " ".join(value(item) for item in (
        host.get("cpu_count"), memory // 1024 // 1024 if isinstance(memory, int) else None,
        build.group(1) if build else None, build_type, clickhouse,
        meta.get("repository", {}).get("head", "")[:7] or None,
        sum(not run["all_head"] for run in meta.get("code_runs", [])),
        sum(not present for present in meta.get("presence", {}).values()),
        len(meta.get("summary_errors", [])),
    ))]
    for code, scenarios in (
        ("K1", (("main", "list:first"), ("main", "list:middle"), ("main", "trace:p95"), ("main", "batch:main"))),
        ("K2", (("main", "detail:text_64k"), ("main", "detail:text_2m"), ("main", "detail:entropy_512k"))),
        ("K3", (("equal_total_few_large", "batch:equal_total_few_large"),
                ("equal_total_many_medium", "batch:equal_total_many_medium"))),
    ):
        lines.append(code)
        lines += [" ".join(value(p50(key, w, q)) for w, q in scenarios) for key in order]
    lines.append("K4")
    for key in order:
        rounds = main_rounds(key)
        totals = [item["write"]["block_ingest_wall_ms_sum"] for item in rounds]
        write = statistics.median(totals) if rounds and None not in totals else None
        storage = rounds[-1]["storage"] if rounds else None
        database = None
        asset = None
        if storage:
            database = sum(table.get("total_bytes", table.get("compressed_bytes", 0)) or 0
                           for table in storage["tables"].values())
            asset = (storage.get("asset_store") or {}).get("available_bytes", 0)
        lines.append(" ".join((value(write), value(database), value(asset))))
    lines.append("K5")
    part_states = {item["layout"]: item for item in
                   (_optional_json(directory / "summary" / "part-states.json") or {}).get("part_states", [])}
    for layout in LAYOUTS:
        states = {state["name"]: state for state in (part_states.get(layout) or {}).get("states", [])}
        lines.append(" ".join(value(((states.get(name) or {}).get("scenarios") or {}).get("list:first", {})
                                    .get("application_ready_ms", {}).get("p50")) for name in CORE_STATES))
    lines.append("K6")
    for engine, layout in order:
        summary = _optional_json(directory / "summary" / f"interference-{engine}.json") or {}
        item = next((entry for entry in summary.get("layouts", []) if entry["layout"] == layout), {})
        phases = {phase["phase"]: phase.get("streams", {}).get("list", {}) for phase in item.get("phases", [])}
        lines.append(" ".join((
            value(phases.get("quiet", {}).get("latency_ms", {}).get("p50")),
            value(phases.get("batch_loop", {}).get("latency_ms", {}).get("p50")),
            value(phases.get("batch_loop", {}).get("counts", {}).get("dropped_requests")),
        )))
    lines.append("K7")
    engines = {item["engine"]: item for item in
               (_optional_json(directory / "summary" / "asset-failures.json") or {}).get("engines", [])}
    for engine in ENGINES:
        cases = {case["case"]: case for case in (engines.get(engine) or {}).get("cases", [])}
        lines.append(" ".join(value((cases.get(name) or {}).get("final_status")) for name in CORE_CASES))
    return lines


def core_main(argv=None):
    """对已有结果目录重新生成并打印手敲精简版，同时写入该目录的 feedback-core.txt。"""
    parser = argparse.ArgumentParser(description=core_lines.__doc__)
    parser.add_argument("feedback_dir", type=Path)
    arguments = parser.parse_args(argv)
    lines = core_lines(arguments.feedback_dir, _read_json(arguments.feedback_dir / "feedback-manifest.json"))
    (arguments.feedback_dir / "feedback-core.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


# ---------- 主流程 ----------

def _summaries(paths, errors):
    """按族、按引擎调用汇总器；单项失败只记录原因。"""
    def attempt(name, function, directories):
        present = [path for path in directories if (path / "run-manifest.json").is_file()]
        if len(present) != len(directories):
            errors.append({"summary": name, "error": "run directories missing",
                           "missing": [str(path) for path in directories if path not in present]})
            return None
        try:
            return function(present)
        except Exception as error:  # noqa: BLE001 - 汇总器的全部拒绝原因都要回传
            errors.append({"summary": name, "error": f"{type(error).__name__}: {error}"})
            return None

    matrix = {engine: attempt(f"matrix-{engine}", summarize.summarize,
                              [paths[("matrix", engine, layout)] for layout in LAYOUTS]) for engine in ENGINES}
    interference = {engine: attempt(f"interference-{engine}", summarize.summarize_interference,
                                    [paths[("interference", engine, layout)] for layout in LAYOUTS])
                    for engine in ENGINES}
    part_states = attempt("part-states", summarize.summarize_part_states,
                          [paths[("part-states", "clickhouse", layout)] for layout in LAYOUTS])
    asset_failures = attempt("asset-failures", summarize.summarize_asset_failures,
                             [paths[("asset-failures", engine, None)] for engine in ENGINES])
    return matrix, interference, part_states, asset_failures


def pack(output_root, repo, host, date, destination, part_bytes=PART_BYTES):
    """执行打包并返回 feedback 清单；目标目录已存在时拒绝。"""
    root = Path(output_root).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError(f"feedback destination already exists: {destination}")
    if not root.is_dir():
        raise FileNotFoundError(f"output root is not a directory: {root}")
    destination.mkdir(parents=True)
    paths = run_paths(root)
    errors = []
    presence = {"/".join(str(part) for part in key if part): (path / "run-manifest.json").is_file()
                for key, path in paths.items()}

    matrix, interference, part_states, asset_failures = _summaries(paths, errors)
    for name, value in ([(f"matrix-{engine}", matrix[engine]) for engine in ENGINES]
                        + [(f"interference-{engine}", interference[engine]) for engine in ENGINES]
                        + [("part-states", part_states), ("asset-failures", asset_failures)]):
        if value is not None:
            _write_json(destination / "summary" / f"{name}.json", value)

    extracts = {}
    for engine in ENGINES:
        for layout in LAYOUTS:
            target = paths[("matrix", engine, layout)]
            if (target / "result.json").is_file():
                (destination / "results").mkdir(parents=True, exist_ok=True)
                shutil.copy2(target / "result.json", destination / "results" / f"{engine}-{layout}.json")
            if target.is_dir():
                extracts[(engine, layout)] = extract_target(target)
                _write_json(destination / "evidence" / f"matrix-{engine}-{layout}.json", extracts[(engine, layout)])

    probe_path = root / "xstore-row-probe.json"
    probe = _read_json(probe_path) if probe_path.is_file() else None
    if probe is None:
        errors.append({"probe": str(probe_path), "error": "missing"})

    manifest_paths = [path for directory in set(paths.values()) for path in manifests_under(directory)]
    documents = [(str(path.relative_to(root)), _read_json(path)) for path in sorted(manifest_paths)]
    if probe is not None:
        documents.append((probe_path.name, probe))
    index = CodeIndex(repo)
    runs, files = collect_code_identity(documents, index, repo, destination)
    (destination / "code").mkdir(parents=True, exist_ok=True)
    (destination / "code" / "worktree.diff").write_bytes(_git(repo, "diff", "HEAD", "--", STAGE_PREFIX))
    (destination / "code" / "status.txt").write_bytes(_git(repo, "status", "--porcelain", "--", STAGE_PREFIX))

    engines = {}
    for label, document in documents:
        runtime = document.get("engine_runtime") or (document.get("runtime") or {}).get("engine_runtime")
        engine = document.get("engine") or (document.get("runtime") or {}).get("engine")
        if isinstance(runtime, dict) and engine in ENGINES:
            engines.setdefault(engine, set()).add(runtime.get("version"))
    engine_versions = {engine: sorted(str(version) for version in versions) for engine, versions in engines.items()}
    anomalies = []
    for engine, versions in engine_versions.items():
        if len(versions) != 1:
            anomalies.append(f"{engine} runs report {len(versions)} engine versions")
    if any("release" not in version for version in engine_versions.get("xstore", [])):
        anomalies.append("an xstore run reports a non-release build")
    local = [item for item in files if item["status"] == "local"]
    if local:
        anomalies.append(f"{len(local)} code files differ from every published commit")
    identity = {
        "head": index.head,
        "summary": ("all runs use HEAD code" if all(run["all_head"] for run in runs)
                    else f"{sum(not run['all_head'] for run in runs)} of {len(runs)} runs use code other than HEAD"),
    }

    facts_dir = root / "facts"
    facts = []
    if facts_dir.is_dir():
        for path in sorted(facts_dir.iterdir()):
            if path.is_file() and path.stat().st_size <= FACT_FILE_LIMIT:
                target = destination / "facts" / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                facts.append(path.name)
    else:
        errors.append({"facts": str(facts_dir), "error": "missing"})
    if probe is not None:
        (destination / "facts").mkdir(parents=True, exist_ok=True)
        shutil.copy2(probe_path, destination / "facts" / probe_path.name)

    sources = manifest_paths + [path / "result.json" for path in paths.values()]
    archive = archive_manifests(sources + ([probe_path] if probe is not None else []), root, destination, part_bytes)
    local_only = [
        {"path": str(path.relative_to(root)), "bytes": path.stat().st_size}
        for directory in set(paths.values()) if directory.is_dir()
        for path in sorted(directory.rglob("*" + RAW_SAMPLE_SUFFIX))
    ]

    if asset_failures is None:
        # 汇总器要求两个引擎齐全；缺一侧时直接读取已完成引擎的信封，信封内含全部用例结果。
        asset_failures = {"engines": []}
        for engine in ENGINES:
            envelope = paths[("asset-failures", engine, None)] / "run-manifest.json"
            if envelope.is_file() and _read_json(envelope).get("status") == "complete":
                asset_failures["engines"].append(
                    {"engine": engine, "cases": _read_json(envelope)["asset_failures"]["results"]}
                )
        asset_failures["source"] = "run envelopes; the summarizer requires both engines"
        _write_json(destination / "summary" / "asset-failures.json", asset_failures)
    matrix_present = {engine: all(presence[f"matrix/{engine}/{layout}"] for layout in LAYOUTS)
                      for engine in ENGINES}
    lines = feedback_lines(matrix, extracts, part_states, interference, asset_failures, probe, identity,
                           matrix_present)
    (destination / "feedback.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    meta = {"presence": presence, "summary_errors": errors, "engine_versions": engine_versions,
            "code_runs": runs, "repository": {"head": index.head}}
    (destination / "feedback-core.txt").write_text("\n".join(core_lines(destination, meta)) + "\n",
                                                   encoding="utf-8")

    listing = [{"path": str(path.relative_to(destination)), "bytes": path.stat().st_size,
                "sha256": _sha256_file(path)}
               for path in sorted(destination.rglob("*")) if path.is_file()]
    oversized = [item["path"] for item in listing if item["bytes"] > part_bytes]
    if oversized:
        anomalies.append("files above the part limit: " + ", ".join(oversized))
    document = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "host": host, "date": date,
        "output_root": str(root), "repository": {"head": index.head}, "presence": presence,
        "summary_errors": errors, "anomalies": anomalies, "engine_versions": engine_versions,
        "code_runs": runs, "code_files": files, "facts": facts, "archive": archive,
        "local_only_samples": local_only, "files": listing,
        "total_bytes": sum(item["bytes"] for item in listing),
    }
    _write_json(destination / "feedback-manifest.json", document)
    return document


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True, help="the run output root ($YELLOW_OUTPUT)")
    parser.add_argument("--repo", type=Path, required=True, help="the toolkit checkout ($YELLOW_REPO)")
    parser.add_argument("--host", required=True, help="last octet of the host IP")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--destination", type=Path,
                        help="defaults to <repo>/docs/yellow-handback/<host>-<date>")
    return parser


def main(argv=None):
    """打包并打印缺失项、汇总失败与身份异常；异常不改变退出码，由回传方判读。"""
    arguments = build_parser().parse_args(argv)
    destination = arguments.destination or (
        arguments.repo / "docs" / "yellow-handback" / f"{arguments.host}-{arguments.date}"
    )
    document = pack(arguments.output_root, arguments.repo, arguments.host, arguments.date, destination)
    missing = [name for name, present in document["presence"].items() if not present]
    print(f"feedback: {destination}")
    print(f"total bytes: {document['total_bytes']}; archive parts: {len(document['archive']['parts'])}")
    print("missing runs: " + (", ".join(missing) if missing else "none"))
    for error in document["summary_errors"]:
        print("summary error: " + json.dumps(error, ensure_ascii=False))
    for anomaly in document["anomalies"]:
        print("anomaly: " + anomaly)
    return 0


if __name__ == "__main__":
    # 第一个参数为 core 时只重新生成手敲精简版，其余情况执行完整打包。
    if sys.argv[1:2] == ["core"]:
        raise SystemExit(core_main(sys.argv[2:]))
    raise SystemExit(main())
