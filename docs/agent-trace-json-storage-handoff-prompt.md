# Agent Trace JSON 存储调研与实验交接 Prompt

将以下内容作为新会话的首条请求：

```text
你接手 Agent Trace 的 JSON 存储调研和穿刺实验。工作目录为
/home/omm/work/agent-trace。默认使用中文输出和编写文档。先读根目录
AGENTS.md、CONTEXT.md，再完整阅读以下资料：

1. /home/omm/work/agent-trace/docs/json-storage-design-survey.md
2. /home/omm/work/agent-trace/docs/json-storage-spike-experiment-design.md
3. /home/omm/work/agent-trace/exporter_demo/docs/SCHEMA.md
4. /home/omm/work/agent-trace/exporter_demo/docs/references/engine-verification-2026-08-07.md
5. /home/omm/work/agent-trace/trace-synthesis/benchmark/schema/v4/database/catalog.json
6. /home/omm/work/agent-trace/trace-synthesis/benchmark/schema/v4/langfuse/catalog.json
7. /home/omm/work/agent-trace/trace-synthesis/docs/report/large-payload-multimodal-trace.md

公开资料仓库：https://github.com/ruined404cjy/agent-trace-research

当前本地版本：

- exporter_demo main：4cc3bf2d21ab9ecd5d014a182e66d6b83b7f446b
- trace-synthesis main：e0b9c83e3bd8bd7bb78d68225f29df0753f5432e
- langfuse main：983c2a6e5bbe9e8f35fe10eb017c9abd6220833b

2026-09-01 的重试中，Langfuse 成功拉取且 main 不变，并获取 v4.25.0、
v4.26.0 tag；exporter_demo 和 trace-synthesis 仍因 GitHub TLS 错误未刷新远端
引用。开始工作时先重试 git fetch/pull，记录精确提交和 diff。

必须先处理的事实：当前 exporter events 写 28 列，benchmark v4 database
catalog 和 cleanup 仍定义 26 列，差异是 service_name、service_version。
二者不能直接组成有效性能基线。根 CONTEXT.md 和旧蓝/黄区指南仍包含历史三表
术语；当前 schema 以 exporter_demo/docs/SCHEMA.md、schema.go 和匹配的
benchmark catalog 为准。

现有调研的核心结论：

- JSON 路径组织与大 payload 生命周期是相交的两条设计轴。
- Agent Trace 候选形态为热点强类型列 + residual JSON/Map + 物化 Core/Full
  + inline-or-reference asset。
- 单个 JSON 长值可与媒体共用 asset 基础设施；数千个短字段需要 residual、
  Map、自动子列或专用列式布局。
- JSONB 改善解析和索引，不负责媒体、对象一致性、保留和删除。
- 当前 exporter 默认 64 KiB 截断发生在数据库之前，必须在大 JSON 实验中
  单独控制和报告。
- Langfuse 使用 events_full/events_core、metadata names/values、默认 2 MiB
  field overflow 和 Media/对象存储；它没有用 ClickHouse native JSON 保存
  input/output。

下一阶段目标：按照实验设计执行 P0，不直接开始数据库选型。

1. 更新仓库并确定匹配的 exporter/catalog。
2. 填写真实版本 manifest，运行 schema preflight、正确性和可见性验证。
3. 固定 C0/W1/L1 数据规范、truth manifest 和测量口径。
4. 建立 E0 当前引擎基线，分别报告 row/xstore、64 KiB/不截断策略。
5. 再按证据选择 E1 PostgreSQL JSONB、E2 ClickHouse JSON、E3 Langfuse 和
   E4 Tempo 的独立机制穿刺。

工作约束：

- 不加载 ~/.gauss_env，除非任务明确需要 DataInfra/openGauss 工具链。
- 不把历史三表指南当作当前 schema；所有运行绑定精确 commit 和 catalog hash。
- 正确性、canonical hash 和预期计数失败的 run 不进入性能比较。
- 系统级结果与数据库引擎级结果分开报告。
- 记录 bytes read、分项空间、CPU/RSS、compaction/ANALYZE、截断和对象失败，
  不只记录 latency 和 rows/s。
- 修改本地调研或新增实验产物后，同步更新公开资料仓库的 docs，并在交付中给出
  commit 和可复现命令。

先汇报仓库更新、版本配对和 P0 缺口，再提出本会话可完成的最小实验步骤并执行。
```
