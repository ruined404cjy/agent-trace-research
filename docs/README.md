# Agent Trace Research 文档索引

正式文档按主题分为项目背景与 JSON 存储两组。实验代码和复现入口位于仓库的 `experiments/`（语料画像脚本位于 `experiments/payload-profile/`），运行数据与内部证据保存在 gitignored 的 `docs/temp/`。

## 项目背景

| 文档 | 内容 |
|---|---|
| [OTel、OTLP、Collector、GenAI 与 Langfuse 学习指南](project-background/otel-langfuse-study-guide.md) | Trace 数据模型、采集链路和相关项目概念 |
| [标准 openGauss 行存复现指南](project-background/trace-ingestion-demo-blue-zone-guide.md) | 历史固定版本的 openGauss 三表摄入 Demo |
| [GV xstore 复现指南](project-background/trace-ingestion-demo-yellow-zone-guide.md) | GV 环境、定制 Collector 构建和 dstore 验收 |

## JSON 存储

| 类型 | 文档 | 内容 |
|---|---|---|
| 设计 | [JSON 存储设计决策](json-storage/design-decision.md) | openGauss、XStore 与 ClickHouse 的逻辑模型、参数取值与判据、真实语料画像和证据缺口 |
| 原理 | [openGauss JSONB 与 ClickHouse Native JSON](json-storage/jsonb-native-json-principles.md) · [阶段三原理与设计](json-storage/stage3-payload-principles.md) | 四种 JSON 存储结构的写入、物理存储、后台维护、查询和恢复流程；长载荷四种布局在行存与列存中的物理组织、外置对象组件与场景设计 |
| 调研 | [JSON 存储设计调研](json-storage/design-survey.md) | 代表性系统、论文、当前项目状态和设计方向 |
| 实验设计 | [阶段一](json-storage/stage1-multifield-design.md) · [阶段二](json-storage/stage2-representation-design.md) · [阶段二调优矩阵](json-storage/stage2-tuning-design.md) · [阶段三](json-storage/stage3-payload-design.md) | 多字段 JSON、跨引擎表示比较、定向调优和长 payload 布局 |
| 实验报告 | [阶段一](json-storage/stage1-multifield-report.md) · [阶段二](json-storage/stage2-representation-report.md) · [阶段三（x86）](json-storage/stage3-payload-x86-report.md) · [阶段三（XStore，ARM 主机 A）](json-storage/stage3-payload-xstore-report.md) | 已完成实验的证据、数据结果、分析和适用范围 |
| 汇报材料 | [阶段二组内汇报辅助材料](json-storage/stage2-representation-briefing.md) · [阶段三组内汇报](json-storage/stage3-payload-briefing.md) | 核心流程与场景化比较摘要；阶段三三台主机的原理、环境、逐场景结果与跨主机对照 |

JSON 存储流程图位于 `json-storage/assets/`，由原理文档、报告和汇报材料共同使用。

## 临时资料

- `temp/` 保存本地运行清单、原始结果和内部核对材料；该目录不作为正式交付入口。
