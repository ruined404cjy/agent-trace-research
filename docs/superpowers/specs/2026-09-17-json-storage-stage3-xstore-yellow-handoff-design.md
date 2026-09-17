# JSON Storage Stage 3 黄区 xstore 对比交接设计

## 1. 目标

本交接把 JSON Storage Stage 3 已有正式文档、实验代码和冻结输入契约带到黄区，指导黄区 agent 在已部署 xstore 的 EulerOS 2.13 ARM 环境中部署独立 ClickHouse，并在同一主机上执行四种长 payload 布局的 xstore/ClickHouse 对比实验。

交付完成时应满足以下条件：

1. 新分支从 `93ebf2319ae7cb60b1f68eb53b3562d26f80f443` 派生，保留该提交中的 Stage 3 实验代码和正式设计文档。
2. 正式评估文档说明现有 Python 实现的代表性、已观测区分度、extension 方案边界和当前证据状态。
3. 黄区指南可以独立阅读，覆盖环境探测、ClickHouse ARM64 部署、冻结输入校验、xstore 能力核对、adapter 实施、实验执行、证据验收和清理。
4. 约 308 MiB 的 Stage 3 正式输入以独立、确定性归档交付，原始目录与归档都具有 SHA-256 身份。
5. 分支、归档和结果均先保存在本地。GitHub 推送、Release 发布和数据外发需要独立授权。

## 2. 范围

### 2.1 本次交付内容

- 新分支 `stage3/xstore-yellow-handoff`。
- `docs/json-storage/json-storage-stage3-representativeness-assessment-2026-09-17.md`：记录四布局实现、阶段性量化结果、合成数据边界和 extension 判断。
- `docs/project-background/json-storage-stage3-xstore-clickhouse-yellow-guide.md`：提供黄区完整执行流程和可直接转发给黄区 agent 的交接 prompt。
- `docs/README.md`：增加上述两份正式文档的索引。
- 本地、gitignored 的正式输入归档与 SHA-256 清单。归档只包含 Stage 3 `events.jsonl`、`truth.json`、`generation-manifest.json` 和 `payloads/`，不包含 Stage 2 源输入或蓝区运行结果。

### 2.2 本次不包含的内容

- 不继续执行蓝区 Stage 3 candidate、正式矩阵、part-state、interference 或 Asset 故障实验。
- 不在蓝区实现 xstore adapter。蓝区无法读取 xstore 仓库、运行接口和物理统计能力，预写 adapter 会引入未经验证的接口假设。
- 不修改现有 openGauss、ClickHouse adapter、Collector、exporter 或数据库镜像。
- 不把 `docs/temp/`、`.superpowers/` 或本地 SDD ledger 提交到迁移分支。
- 不推送分支，不创建 GitHub Release，不上传冻结输入或实验结果。

## 3. 当前事实与证据边界

Stage 3 固定比较 `same_table`、`separate`、`full_core` 和 `asset_ref`。正式输入包含 48,534 条事件、190 个写入 block 和 160 个主 payload，主 payload 原始总量为 122.5 MiB。实验设计、布局定义和证据要求以 `docs/json-storage/json-storage-stage3-experiment-design-2026-09-09.md` 为准。

蓝区已有 `main` 工作负载切片覆盖两个引擎、四个布局和四轮 Latin square，但缺少两个等总字节控制与 `correctness_only`，不能通过 `experiments/json-storage-stage3/report/summarize.py` 的完整 workload 门禁。迁移评估文档可以把该切片作为阶段性观测，必须标注为不可发布的部分结果。

蓝区 `docs/temp/` 中的原始产物受 Git 忽略规则保护。黄区不能依赖这些本地路径。正式评估文档保留量化摘要、输入身份、证据门禁和适用边界；原始运行产物不进入迁移分支。

## 4. 交付架构

交付由三个相互独立的部分组成。

### 4.1 Git 分支

Git 分支承载可审查、可版本化的代码和文档。分支继承现有 Stage 3 generator、runner、adapter、summarizer 与测试，不复制文件形成第二套实现。

黄区从 GitHub 拉取该分支后，先报告 branch、HEAD、upstream 和 dirty state。执行结果必须记录实际 HEAD，避免把浮动分支名当作代码身份。

### 4.2 冻结输入归档

正式输入归档是 Git 分支之外的不可变资产。蓝区使用稳定排序、固定时间戳、固定 owner/group 和禁用 gzip 时间戳的方式生成 `.tar.gz`，并生成 SHA-256 清单。归档目录结构保持 Stage 3 loader 可直接读取的形态。

黄区下载后先校验归档 SHA-256，再解包并调用现有 `load_formal_input()` 契约验证 `generation-manifest.json`、`events.jsonl`、`truth.json` 和全部 payload。任一身份不匹配时停止实验。

归档包含由真实 Trace 普通字段派生的事件数据。发布到 GitHub 前必须确认仓库可见性和数据外发范围；本次只生成本地资产。

### 4.3 黄区执行流程

黄区流程分为四个门禁阶段：

1. **环境门禁**：记录 EulerOS 版本、`uname -m`、glibc、CPU、内存、磁盘、文件系统、Python、端口、时间同步、xstore 版本和服务状态。
2. **引擎门禁**：部署 ClickHouse ARM64，确认监听地址、版本、数据目录、日志目录和资源限制；只对本机开放实验端口。
3. **能力门禁**：黄区 agent 读取本地 xstore 文档、安装目录和仓库，形成 capability report，确认 DDL、批量写入、参数绑定、执行计划、扫描量、空间统计、事务、清理和长文本类型的实际接口。
4. **实验门禁**：实现并测试 xstore adapter 后，依次执行单布局 smoke、四布局 candidate、四轮主矩阵、等总字节控制、correctness-only、part-state 可适用项、混合负载和 Asset 故障。每一阶段通过 truth、访问路径、响应 bytes、水位、维护状态和清理证据后才能进入下一阶段。

## 5. ClickHouse ARM64 部署决策

蓝区正式切片使用 ClickHouse `25.12.11.4`。黄区优先使用相同版本的官方 ARM64 产物，以控制引擎版本变量。该版本仅用于短期、隔离、仅本机监听的复现实验。

若黄区安全策略禁止使用该版本，黄区使用策略批准的 ARM64 stable 或 LTS 版本，并把结果限定为“黄区同机 xstore/ClickHouse 对比”。该结果不能与蓝区 ClickHouse 数值直接合并；报告必须记录版本差异，后续需要在蓝区以相同 ClickHouse 版本复测后才能形成跨区趋势。

部署不依赖 Docker。指南同时提供以下路径：

- 有 root 和 RPM 安装权限时，使用固定版本的官方 AArch64 RPM。
- 无 root 或不允许系统级安装时，使用固定版本的官方 ARM64 TGZ，在用户目录运行独立 server、client、data 和 log 路径。

下载入口限定为 ClickHouse 官方 GitHub Release。每个下载文件先按 Release 提供的摘要校验，再进入安装步骤。

## 6. xstore adapter 边界

xstore adapter 应实现 `experiments/json-storage-stage3/runner/common.py` 中 `LayoutAdapter` 的等价能力，并复用现有 truth、query catalog、计时和响应校验逻辑。黄区允许新增 `runner/xstore.py`、对应测试、endpoint 配置和汇总器的 `xstore` 分支；所有 xstore 专用逻辑集中在这些文件中。

能力报告必须为下列概念提供实际命令或 API：

| 实验概念 | xstore 需要确认的能力 |
|---|---|
| namespace | 独立 schema/database/tenant 及确定性清理 |
| same_table | 普通列与长 payload 同一逻辑表 |
| separate | 分析表与 payload 表的联合定位 |
| full_core | Full/Core 双写及联合可见水位 |
| asset_ref | 事件引用、catalog 状态和同一用户可访问的本地对象目录 |
| ingest block | 固定 256 行边界、提交结果和可见水位 |
| query evidence | 实际执行计划、读取行数/字节和访问结构 |
| storage evidence | 表、索引、副本或分片、压缩前后字节与后台任务 |
| cleanup | namespace、对象目录和临时配置恢复 |

若 xstore 缺少某项可观测量，adapter 必须写入明确的 `unavailable` 与原因，汇总器不得估算。缺少 truth、完整 payload bytes、水位或清理证据属于停止条件；缺少非关键物理指标时保留诊断结果，不形成对应机制结论。

## 7. 公平性与数据流

两引擎使用同一 ARM 主机、同一冻结输入、同一 Python runner、同一查询参数、同一 190 个 block 和同一四阶 Latin square。默认串行运行布局，避免 xstore 与 ClickHouse 同时争用 CPU、内存和磁盘。混合负载场景只在单个目标内部产生并发。

`asset_ref` 在两引擎中继续使用应用侧 resolver 和同一文件系统上的独立内容寻址目录。xstore extension 或进程内对象读取不替换该布局；如需评估数据库内调度，应新增独立的 `db_lob_ref` 研究项并重新设计矩阵。

每个查询样本的数据流为：固定 QuerySpec → engine adapter → 完整结果读取 → 必要的 Asset resolver → 长度与 SHA-256 校验 → 应用可用时间 → 引擎物理证据。服务端摘要不能替代 payload 传输。

## 8. 失败处理与停止条件

黄区指南采用 fail-closed 规则：

- 环境身份、输入身份、代码 HEAD 或引擎版本缺失时停止。
- xstore capability report 尚未明确关键接口时停止 adapter 实施。
- ClickHouse 或 xstore 只完成部分布局时，保留产物并标为 diagnostic。
- 执行计划、扫描证据或输出契约不符合预期时，先诊断访问路径，不发布性能比较。
- 任一失败运行必须记录错误、已完成子产物和清理结果。
- 无法清理 namespace、对象目录、暂停的后台任务或临时配置时停止后续运行。
- 黄区无法上传结果时，保留本地只读目录，生成文件清单和 SHA-256；最终回复只报告路径、摘要和失败项，不粘贴大型原始产物。

## 9. 验证要求

### 9.1 迁移分支验证

- Stage 3 单元测试通过；数据库条件测试可以在缺少端点时跳过。
- `git diff --check` 通过。
- 两份新文档的相对链接全部存在。
- 文档扫描不存在占位标记、不存在的 Stage 3 实验 README 引用或把 `main` 切片描述为完整正式结论的文字。
- `docs/README.md` 能从正式文档入口定位评估和黄区指南。

### 9.2 输入归档验证

- 归档在两次独立构建中具有相同 SHA-256。
- 解包目录通过 `load_formal_input()`。
- 解包后的 `events.jsonl`、`truth.json`、`generation-manifest.json` 和 payload 集合身份与蓝区源目录一致。
- 归档不包含绝对路径、蓝区运行结果、Stage 2 源输入或临时文件。

### 9.3 黄区验收

- capability report 记录 xstore 实际接口及证据命令。
- ClickHouse 与 xstore 分别记录 engine version、host、代码 HEAD 和冻结输入身份。
- 四种布局在 smoke 中通过 truth、完整响应 bytes、水位和清理门禁。
- 正式汇总只接收完整 workload、四轮 Latin square、访问路径、维护状态和清理证据。
- 比较报告按引擎内布局结论、黄区同机跨引擎结论和不可比较边界分节陈述。

## 10. 参考资料

- [JSON Storage Stage 3 实验设计](../../json-storage/json-storage-stage3-experiment-design-2026-09-09.md)
- [JSON 存储设计调研](../../json-storage/json-storage-design-survey-2026-09-09.md)
- [JSON 存储原理](../../json-storage/json-storage-principles-2026-09-09.md)
- [黄区 Trace 摄入环境指南](../../project-background/trace-ingestion-demo-yellow-zone-guide.md)
- [ClickHouse 25.12.11.4 官方 Release](https://github.com/ClickHouse/ClickHouse/releases/tag/v25.12.11.4-stable)
- [ClickHouse 支持平台](https://clickhouse.com/support/platforms)
