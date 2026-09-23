# Agent Trace Research 文档索引

正式文档按主题分为项目背景与 JSON 存储两组。实验代码和复现入口位于仓库的 `experiments/`，运行数据与内部证据保存在 gitignored 的 `docs/temp/`。

## 项目背景

| 文档 | 内容 |
|---|---|
| [OTel、OTLP、Collector、GenAI 与 Langfuse 学习指南](project-background/otel-langfuse-study-guide.md) | Trace 数据模型、采集链路和相关项目概念 |
| [标准 openGauss 行存复现指南](project-background/trace-ingestion-demo-blue-zone-guide.md) | 历史固定版本的 openGauss 三表摄入 Demo |
| [GV xstore 复现指南](project-background/trace-ingestion-demo-yellow-zone-guide.md) | GV 环境、定制 Collector 构建和 dstore 验收 |
| [阶段三黄区 XStore/ClickHouse 同机对比指南](project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md) | 黄区 ARM64 环境探测、ClickHouse 无 Docker 部署、XStore 能力门禁和四布局对比流程 |

## JSON 存储

| 类型 | 文档 | 内容 |
|---|---|---|
| 原理 | [openGauss JSONB 与 ClickHouse Native JSON](json-storage/json-storage-principles-2026-09-09.md) | 四种 JSON 存储结构的写入、物理存储、后台维护、查询和恢复流程 |
| 调研 | [JSON 存储设计调研](json-storage/json-storage-design-survey-2026-09-09.md) | 代表性系统、论文、当前项目状态和设计方向 |
| 实验设计 | [阶段一](json-storage/json-storage-stage1-experiment-design-2026-09-09.md) · [阶段二](json-storage/json-storage-stage2-experiment-design-2026-09-09.md) · [阶段二调优矩阵](json-storage/json-storage-stage2-sup-experiment-design-2026-09-10.md) · [阶段三](json-storage/json-storage-stage3-experiment-design-2026-09-09.md) | 多字段 JSON、跨引擎表示比较、定向调优和长 payload 布局 |
| 实验报告 | [阶段一](json-storage/json-storage-stage1-report-2026-09-09.md) · [阶段二](json-storage/json-storage-stage2-report-2026-09-10.md) | 已完成实验的证据、数据结果、分析和适用范围 |
| 评估 | [阶段三四布局代表性评估](json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md) | 部分正式切片的区分度、合成数据边界和 extension 后续实验 |
| 汇报材料 | [阶段二组内汇报辅助材料](json-storage/json-storage-stage2-sup-pre-2026-09-10.md) | 核心流程与场景化比较摘要 |

JSON 存储流程图位于 `json-storage/assets/`，由原理文档、报告和汇报材料共同使用。

## 执行记录与临时资料

- `superpowers/` 保存已执行工作的设计说明与实施计划；阶段三黄区交接的[设计](superpowers/specs/2026-09-17-json-storage-stage3-xstore-yellow-handoff-design.md)与[实施计划](superpowers/plans/2026-09-17-json-storage-stage3-xstore-yellow-handoff.md)位于该目录。
- `temp/` 保存本地运行清单、原始结果和内部核对材料；该目录不作为正式交付入口。
- `yellow-handback/` 保存黄区按执行指南第 9.2 节通道一推送的结果文件，按 `<主机 IP 末段>-<日期>/` 分目录；每个目录含回传摘录、各 target `result.json` 与汇总器 `summary.json`。
