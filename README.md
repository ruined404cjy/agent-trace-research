# Agent Trace Research

Agent Trace 的公开调研、复现指南和实验设计。仓库当前重点是多字段 JSON、JSON/JSONB、长字段和多模态大 payload 的存储方案。

## 实验

| 实验 | 状态与用途 |
|---|---|
| [JSON 存储第一阶段实验基础设施](experiments/json-storage-stage1/README.md) | 生成正确性与路径组织数据，并记录 openGauss JSONB 与 ClickHouse Native JSON 实验结果 |
| [JSON 存储阶段二横向实验](experiments/json-storage-stage2/README.md) | 已完成真实输入审计、六轮动态属性实验、正确性与原文恢复验证 |
| [JSON 存储阶段二补充实验](experiments/json-storage-stage2-sup/README.md) | openGauss JSON、openGauss JSONB、ClickHouse String JSON 与 ClickHouse Native JSON 的复现入口 |

## 文档

| 文档 | 状态与用途 |
|---|---|
| [文档分类索引](docs/README.md) | 按项目背景、JSON 存储和执行记录组织全部文档 |
| [JSON 存储原理](docs/json-storage/json-storage-principles-2026-09-09.md) | openGauss JSONB 与 ClickHouse Native JSON 的写入、物理存储、维护和查询流程 |
| [JSON 存储阶段一机制报告](docs/json-storage/json-storage-stage1-report-2026-09-09.md) | 汇总阶段一背景与基线、openGauss/ClickHouse 引擎内实测、机制结论和阶段二入口 |
| [JSON 存储阶段二报告](docs/json-storage/json-storage-stage2-report-2026-09-09.md) | 原六结构与补充四结构的处理流程、场景化结果和证据边界 |
| [JSON 存储阶段二汇报辅助材料](docs/json-storage/json-storage-stage2-sup-pre-2026-09-09.md) | 两引擎流程、Sidecar 与四结构场景化比较摘要 |
| [JSON 存储设计调研](docs/json-storage/json-storage-design-survey-2026-09-09.md) | 代表性系统、论文、现有项目状态、工程结论与遗留问题 |
| [JSON 存储阶段一实验设计](docs/json-storage/json-storage-stage1-experiment-design-2026-09-09.md) | 阶段一多字段机制实验及顺延目标的数据、workload、指标和门槛 |
| [JSON 存储阶段二实验设计](docs/json-storage/json-storage-stage2-experiment-design-2026-09-09.md) | 真实 Trace 审计与 openGauss/ClickHouse 动态属性统一横向实验 |
| [JSON 存储阶段二补充实验设计](docs/json-storage/json-storage-stage2-sup-experiment-design-2026-09-09.md) | JSON、JSONB、ClickHouse String JSON、ClickHouse Native JSON 四结构横比及 ClickHouse 机制观察 |
| [JSON 存储阶段三实验设计](docs/json-storage/json-storage-stage3-experiment-design-2026-09-09.md) | 同表内联、独立 payload 表、Full/Core 与 asset reference 实验 |
| [OTel 与 Langfuse 学习指南](docs/project-background/otel-langfuse-study-guide.md) | OTel、Collector、GenAI 语义和 Langfuse 摄入链路 |
| [标准 openGauss 行存复现指南](docs/project-background/trace-ingestion-demo-blue-zone-guide.md) | 历史固定版本的 openGauss row profile 复现 |
| [GV xstore 复现指南](docs/project-background/trace-ingestion-demo-yellow-zone-guide.md) | 历史固定版本的 GV xstore 复现 |

## 当前状态

- 资料修订日期：2026-09-09；阶段二原实验基线日期：2026-09-07，补充实验基线日期：2026-09-09。
- 阶段二原实验有效结果位于 `docs/temp/json-storage-stage2/formal-20260907-retry-3/`；早期失败产物仅保留诊断。
- exporter 远端 main：2026-09-07 · `81b55be`；SPEC v1.8 仍冻结 18 列最小 OTel schema。
- trace-synthesis 远端 main：2026-09-07 · `ef3be14`；v4 database catalog revision 仍为 `2026-09-02.3`、28 列。
- 已验证历史配对：benchmark `9529c8f`、exporter `54ca553`。
- Langfuse 已确认基线：`983c2a6`。
- exporter 远端 main 为 18 列；trace-synthesis 远端 main 的 v4 database catalog 仍为 28 列，两仓尚未形成联合冻结。系统级回归使用已验证历史配对；独立机制实验记录 `data_path=independent_loader`。
- 两份历史复现指南固定在三表版本，不能直接与当前 events 单宽表 exporter 混用。

## 来源与发布边界

本仓库发布调研文档和小型可复现实验脚本，不包含相关源码仓、凭据、环境日志和生成数据。文档中的源码链接固定到调研时使用的提交：

- [exporter_demo](https://github.com/labmemW/exporter_demo)
- [trace-synthesis](https://github.com/zfwang2021/trace-synthesis)
- [Langfuse](https://github.com/langfuse/langfuse)
- [OpenTelemetry Collector](https://github.com/open-telemetry/opentelemetry-collector)
- [OpenTelemetry Proto](https://github.com/open-telemetry/opentelemetry-proto)
- [GenAI Semantic Conventions](https://github.com/open-telemetry/semantic-conventions-genai)

公开可见不表示授予额外的软件或文档许可。本仓库当前未附加 LICENSE。
