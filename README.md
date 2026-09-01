# Agent Trace Research

Agent Trace 的公开调研、复现指南和实验设计。仓库当前重点是多字段 JSON、
JSON/JSONB、长字段和多模态大 payload 的存储方案。

## 文档

| 文档 | 状态与用途 |
|---|---|
| [JSON 存储设计调研](docs/json-storage-design-survey.md) | 代表性系统、论文、现有项目状态、工程结论与遗留问题 |
| [JSON 存储穿刺与对比实验设计](docs/json-storage-spike-experiment-design.md) | E0–E9 候选、数据、workload、指标、门槛和工作量 |
| [OTel 与 Langfuse 学习指南](docs/otel-langfuse-study-guide.md) | OTel、Collector、GenAI 语义和 Langfuse 摄入链路 |
| [蓝区复现指南](docs/trace-ingestion-demo-blue-zone-guide.md) | 历史固定版本的标准 openGauss row profile 复现 |
| [黄区复现指南](docs/trace-ingestion-demo-yellow-zone-guide.md) | 历史固定版本的 GV xstore 复现 |

## 当前状态

- 资料基线日期：2026-09-01。
- exporter 本地基线：`4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b`。
- benchmark 本地基线：`e0b9c83e3bd8bd7bb78d68225f29df0753f5432e`。
- Langfuse 已确认基线：`983c2a6e5bbe9e8f35fe10eb017c9abd6220833b`。
- 当前 exporter events 为 28 列；benchmark v4 database catalog 仍为 26 列。
  建立性能基线前必须先处理 `service_name`、`service_version` 差异。
- 蓝区和黄区指南固定在历史三表版本，不能直接与当前 events 单宽表 exporter 混用。

## 来源与发布边界

本仓库只发布文档，不包含相关源码仓、凭据、环境日志和测试数据。文档中的源码链接
固定到调研时使用的提交：

- [exporter_demo](https://github.com/labmemW/exporter_demo)
- [trace-synthesis](https://github.com/zfwang2021/trace-synthesis)
- [Langfuse](https://github.com/langfuse/langfuse)
- [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector)
- [OpenTelemetry Proto](https://github.com/open-telemetry/opentelemetry-proto)
- [GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai)

公开可见不表示授予额外的软件或文档许可。本仓库当前未附加 LICENSE。
