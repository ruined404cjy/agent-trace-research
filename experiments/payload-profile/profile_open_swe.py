#!/usr/bin/env python3
"""统计 Open-SWE-Traces 真实轨迹的 payload 大小分布、压缩比与 instrumentation 写放大。

输入为 nvidia/Open-SWE-Traces 的本地副本，目录结构为
``<root>/data/<agent_type>/<model>/<split>/train-*.parquet``。每行 parquet 记录一条
软件工程轨迹，``messages`` 为按序排列的 system/user/assistant/tool 消息。

抽样规则固定：每个 config 取字典序居中的 shard，读取首个 row group 的前 SAMPLE_ROWS
行。同一份数据副本上重复执行得到同一结果。
"""

import argparse
import glob
import json
import os
import zlib

SAMPLE_ROWS = 400
COMPRESS_SAMPLE_STRIDE = 7
MIN_COMPRESS_BYTES = 4_096


def _content_bytes(message):
    """返回单条消息 content 的 UTF-8 字节数，非字符串 content 按 canonical JSON 计量。"""
    content = message.get("content")
    if content is None:
        return 0
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return len(content.encode("utf-8"))


def _quantile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def profile_config(path):
    """统计单个 config，返回单条消息、累积 prompt、压缩比和写放大的原始序列。"""
    import pyarrow.parquet as pq

    shards = sorted(glob.glob(os.path.join(path, "*", "*.parquet")))
    if not shards:
        return None
    shard = shards[len(shards) // 2]
    rows = pq.ParquetFile(shard).read_row_group(0).to_pylist()[:SAMPLE_ROWS]

    single, cumulative, ratios = [], [], []
    trace_lengths, cumulative_total, once_total = [], 0, 0
    for index, row in enumerate(rows):
        messages = row.get("messages") or []
        trace_lengths.append(len(messages))
        # 累积 prompt：OTel GenAI 约定下每个 generation span 的 gen_ai.input.messages
        # 是该次调用实际发送的完整对话，即此前全部消息之和。
        accumulated = 0
        per_trace_cumulative = 0
        for message in messages:
            size = _content_bytes(message)
            single.append(size)
            accumulated += size
            if message.get("role") == "assistant":
                cumulative.append(accumulated)
                per_trace_cumulative += accumulated
        cumulative_total += per_trace_cumulative
        once_total += accumulated
        if index % COMPRESS_SAMPLE_STRIDE == 0 and accumulated >= MIN_COMPRESS_BYTES:
            blob = "".join(
                m["content"] for m in messages if isinstance(m.get("content"), str)
            ).encode("utf-8")
            if blob:
                ratios.append(len(blob) / len(zlib.compress(blob, 6)))

    return {
        "config": os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path),
        "shard": os.path.relpath(shard, path),
        "traces": len(rows),
        "single": single,
        "cumulative": cumulative,
        "ratios": ratios,
        "trace_lengths": trace_lengths,
        "cumulative_total": cumulative_total,
        "once_total": once_total,
    }


def summarize(results):
    """把各 config 的原始序列汇总为分位数、超阈占比和写放大倍数。"""
    single = [x for r in results for x in r["single"]]
    cumulative = [x for r in results for x in r["cumulative"]]
    ratios = [x for r in results for x in r["ratios"]]
    lengths = [x for r in results for x in r["trace_lengths"]]
    cumulative_total = sum(r["cumulative_total"] for r in results)
    once_total = sum(r["once_total"] for r in results)
    return {
        "configs": len(results),
        "traces": sum(r["traces"] for r in results),
        "messages": len(single),
        "generation_spans": len(cumulative),
        "single_message_bytes": {
            "p50": _quantile(single, 0.50), "p95": _quantile(single, 0.95),
            "p99": _quantile(single, 0.99), "max": max(single),
        },
        "cumulative_prompt_bytes": {
            "p50": _quantile(cumulative, 0.50), "p95": _quantile(cumulative, 0.95),
            "p99": _quantile(cumulative, 0.99), "max": max(cumulative),
            "over_64KiB_pct": 100 * sum(1 for x in cumulative if x > 65_536) / len(cumulative),
            "over_512KiB_pct": 100 * sum(1 for x in cumulative if x > 524_288) / len(cumulative),
            "over_2MiB_pct": 100 * sum(1 for x in cumulative if x > 2_097_152) / len(cumulative),
        },
        "messages_per_trace": {
            "p50": _quantile(lengths, 0.50), "p95": _quantile(lengths, 0.95), "max": max(lengths),
        },
        "zlib6_ratio": {
            "p10": _quantile(ratios, 0.10), "p50": _quantile(ratios, 0.50),
            "p90": _quantile(ratios, 0.90), "samples": len(ratios),
        },
        # 写放大：逐 span 记录累积 prompt 的总字节，除以每条 trace 只保存一份对话文本的字节。
        "write_amplification": cumulative_total / once_total,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="Open-SWE-Traces 本地副本根目录")
    parser.add_argument("--out", help="汇总 JSON 输出路径")
    args = parser.parse_args()

    results = []
    for config in sorted(glob.glob(os.path.join(args.root, "data", "*", "*"))):
        result = profile_config(config)
        if result is not None:
            results.append(result)
    if not results:
        raise SystemExit(f"未在 {args.root} 下找到 parquet shard")

    summary = summarize(results)
    summary["per_config"] = [
        {
            "config": r["config"], "shard": r["shard"], "traces": r["traces"],
            "single_p50": _quantile(r["single"], 0.50),
            "single_p95": _quantile(r["single"], 0.95),
            "single_max": max(r["single"]),
            "cumulative_p50": _quantile(r["cumulative"], 0.50),
            "cumulative_p95": _quantile(r["cumulative"], 0.95),
            "cumulative_max": max(r["cumulative"]),
            "over_64KiB_pct": 100 * sum(1 for x in r["cumulative"] if x > 65_536) / len(r["cumulative"]),
        }
        for r in results
    ]
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
