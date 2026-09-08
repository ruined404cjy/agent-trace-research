# Agent Trace Research

Agent Trace 的公开调研、复现指南和实验设计。仓库当前重点是多字段 JSON、JSON/JSONB、长字段和多模态大 payload 的存储方案。

## 实验

| 实验 | 状态与用途 |
|---|---|
| [JSON 存储第一阶段实验基础设施](experiments/json-storage-stage1/README.md) | 生成正确性与路径组织数据，并记录 openGauss JSONB 与 ClickHouse native JSON 实验结果 |
| [JSON 存储阶段二横向实验](experiments/json-storage-stage2/README.md) | 已完成真实输入审计、六轮 residual 横向实验、正确性与原文恢复验证 |

## 文档

| 文档 | 状态与用途 |
|---|---|
| [JSON 存储阶段一机制报告](docs/json-storage-stage1-report-2026-09-07.md) | 汇总阶段一背景与基线、openGauss/ClickHouse 引擎内实测、机制结论和阶段二入口 |
| [JSON 存储阶段二横向报告](docs/json-storage-stage2-cross-engine-report-2026-09-07.md) | 统一契约下的六布局写入、静态与并发查询、空间和异常证据；唯一正式根为 `formal-20260907-retry-2/` |
| [JSON 存储设计调研](docs/json-storage-design-survey.md) | 代表性系统、论文、现有项目状态、工程结论与遗留问题 |
| [JSON 存储阶段一实验设计](docs/json-storage-stage1-experiment-design.md) | 阶段一多字段机制实验及顺延目标的数据、workload、指标和门槛 |
| [JSON 存储阶段二实验设计](docs/json-storage-stage2-experiment-design.md) | 真实 Trace 审计与 openGauss/ClickHouse residual 统一横向实验 |
| [JSON 存储阶段三实验设计](docs/json-storage-stage3-experiment-design.md) | inline、独立 payload 表、Full/Core 与 asset reference 实验 |
| [OTel 与 Langfuse 学习指南](docs/otel-langfuse-study-guide.md) | OTel、Collector、GenAI 语义和 Langfuse 摄入链路 |
| [蓝区复现指南](docs/trace-ingestion-demo-blue-zone-guide.md) | 历史固定版本的标准 openGauss row profile 复现 |
| [黄区复现指南](docs/trace-ingestion-demo-yellow-zone-guide.md) | 历史固定版本的 GV xstore 复现 |

## 当前状态

- 资料基线日期：2026-09-07。
- 阶段二正式统计根为 `docs/temp/json-storage-stage2/formal-20260907-retry-2/`；`formal-20260907/`、`formal-20260907-retry-1/` 的产物仅保留诊断。
- exporter 远端 main：`81b55be6d6912d18c4e2ac7102fd7906e9dac3e8`；SPEC v1.8 仍冻结 18 列最小 OTel schema。
- trace-synthesis 远端 main：`ef3be141cc17415de9fb5a9d8003c16a4cd679ac`；v4 database catalog revision 仍为 `2026-09-02.3`、28 列。
- 已验证历史配对：benchmark `9529c8f389673132757f4da9a96878926f22b94f`、exporter `54ca553a7ed09ad1751c82adab3aa52c6e9357b1`。
- Langfuse 已确认基线：`983c2a6e5bbe9e8f35fe10eb017c9abd6220833b`。
- exporter 远端 main 为 18 列；trace-synthesis 远端 main 的 v4 database catalog 仍为 28 列，两仓尚未形成联合冻结。系统级回归使用已验证历史配对；独立机制实验记录 `data_path=independent_loader`。
- 蓝区和黄区指南固定在历史三表版本，不能直接与当前 events 单宽表 exporter 混用。

## 来源与发布边界

本仓库发布调研文档和小型可复现实验脚本，不包含相关源码仓、凭据、环境日志和生成数据。文档中的源码链接固定到调研时使用的提交：

- [exporter_demo](https://github.com/labmemW/exporter_demo)
- [trace-synthesis](https://github.com/zfwang2021/trace-synthesis)
- [Langfuse](https://github.com/langfuse/langfuse)
- [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector)
- [OpenTelemetry Proto](https://github.com/open-telemetry/opentelemetry-proto)
- [GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai)

公开可见不表示授予额外的软件或文档许可。本仓库当前未附加 LICENSE。
