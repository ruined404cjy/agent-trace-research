# Agent Trace Research

Agent Trace 的公开调研、复现指南和实验设计。仓库当前重点是 Trace 中多字段 JSON、动态属性和长 payload 的存储方案，覆盖 openGauss、XStore 与 ClickHouse。

## 实验

| 实验 | 状态与用途 |
|---|---|
| [JSON 存储阶段一](experiments/json-storage-stage1/README.md) | 已完成；生成正确性与路径组织数据，记录 openGauss JSONB 与 ClickHouse Native JSON 的引擎内机制 |
| [JSON 存储阶段二横向实验](experiments/json-storage-stage2/README.md) | 已完成；真实输入审计、六轮动态属性实验、正确性与原文恢复验证 |
| [JSON 存储阶段二调优矩阵](experiments/json-storage-stage2-sup/README.md) | 已完成；四种 JSON 表示、openGauss 定向索引与 ClickHouse Native JSON 调优候选的复现入口 |
| [JSON 存储阶段三](experiments/json-storage-stage3/README.md) | 已完成；四种长 payload 布局在 openGauss 与 ClickHouse 25.12 上的写入、读取、混合负载与对象故障实验 |
| [语料画像](experiments/payload-profile/profile_open_swe.py) | Open-SWE-Traces 真实轨迹的 payload 大小分布、压缩比与 instrumentation 写放大 |

ARM 主机上 XStore 与 ClickHouse 23.3 的阶段三实验程序与回传工具位于分支 `stage3/xstore-yellow-handoff`。

## 文档

全部文档的分类索引见 [docs/README.md](docs/README.md)。

| 文档 | 用途 |
|---|---|
| [JSON 存储设计决策](docs/json-storage/design-decision.md) | openGauss、XStore 与 ClickHouse 的逻辑模型、参数取值与判据、真实语料画像和证据缺口 |
| [JSON 存储设计调研](docs/json-storage/design-survey.md) | 代表性系统、论文、项目现状与设计方向 |
| [openGauss JSONB 与 ClickHouse Native JSON 原理](docs/json-storage/jsonb-native-json-principles.md) | 四种 JSON 存储结构的写入、物理存储、维护和查询流程 |
| 阶段一：多字段 JSON | [实验设计](docs/json-storage/stage1-multifield-design.md) · [报告](docs/json-storage/stage1-multifield-report.md) |
| 阶段二：JSON 表示比较 | [实验设计](docs/json-storage/stage2-representation-design.md) · [调优矩阵设计](docs/json-storage/stage2-tuning-design.md) · [报告](docs/json-storage/stage2-representation-report.md) · [组内汇报](docs/json-storage/stage2-representation-briefing.md) |
| 阶段三：长 payload 布局 | [实验设计](docs/json-storage/stage3-payload-design.md) · [原理与设计](docs/json-storage/stage3-payload-principles.md) · [x86 报告](docs/json-storage/stage3-payload-x86-report.md) · [XStore 报告](docs/json-storage/stage3-payload-xstore-report.md) · [三台主机组内汇报](docs/json-storage/stage3-payload-briefing.md) |
| [OTel 与 Langfuse 学习指南](docs/project-background/otel-langfuse-study-guide.md) | OTel、Collector、GenAI 语义和 Langfuse 摄入链路 |
| [标准 openGauss 行存复现指南](docs/project-background/trace-ingestion-demo-blue-zone-guide.md) | 历史固定版本的 openGauss row profile 复现 |
| [GV xstore 复现指南](docs/project-background/trace-ingestion-demo-yellow-zone-guide.md) | 历史固定版本的 GV xstore 复现 |

## 当前状态

- 资料修订日期：2026-10-09。JSON 存储三个阶段的实验与报告均已完成，结论收敛于[设计决策](docs/json-storage/design-decision.md)；待定项与后续实验见其第 7 节。
- 阶段三覆盖 x86 主机（openGauss 6.0.0、ClickHouse 25.12.11.4）与两台 ARM 主机（XStore、ClickHouse 23.3.10.5）。
- 运行数据与内部证据保存在 gitignored 的 `docs/temp/`，不随仓库发布。
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
