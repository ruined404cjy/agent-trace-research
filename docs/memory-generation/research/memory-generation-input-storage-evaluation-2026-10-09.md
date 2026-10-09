# 基于 Agent Trace 生成记忆：输入、存储与评测调研

> 状态：调研报告
>
> 调研日期：2026-10-09
>
> 范围：从已存 agent trace 生成供同一 agent 使用的记忆——trace 预处理与生成输入、trace 中的标识与内容采集、生成侧的模型接口、存储溯源与生命周期、评测、公开数据集上的分析
>
> 相关报告：[《生成方法调研》](memory-generation-methods-2026-10-09.md)（生成方法：论文、开源实现、产品机制、相邻方向）；[《信号与选样调研》](memory-generation-signals-2026-10-09.md)（信号与选样：trace 挖掘、失败归因与评估、用户偏好、data agent）。正文以书名号简称引用这两份报告的章节。

## 1. 概要

### 1.1 研究问题

Agent 运行时产生的 trace（模型调用、工具调用与返回、用户消息、最终结果及评估分数）被可观测平台持久化后，库中积累了同一 agent 在大量任务上的执行记录。本报告研究如何从这些已存 trace 中有策略地批量生成记忆，供产生 trace 的 agent 在后续任务中使用，目的是避免重复错误、复用已验证做法、遵循用户偏好，场景优先 code agent 与 data agent。问题包含输入加工、选样与分组、生成方法、整合与生命周期、存储与溯源、与评估和归因的关系、偏好与 data agent 的特殊性、评测八个子问题（第 2.1 节）。

### 1.2 调研范围与方法

本报告是"基于 Agent Trace 生成记忆"调研的三份报告之一，覆盖生成的输入、存储与评测：trace 预处理与生成输入（第 4 节）、trace 中的标识与内容采集（第 5 节）、生成侧的模型接口（第 6 节）、存储溯源与生命周期（第 7 节）、评测（第 8 节）、公开数据集上的分析（第 9 节），综合分析与证据边界见第 10、11 节。生成方法见《生成方法调研》；信号与选样见《信号与选样调研》。三份报告的调研过程与证据分级相同，概述如下。

调研分四个阶段，检索截至 2026-10-09（第 3 节）。第一阶段做广度覆盖，包括从轨迹生成经验记忆的论文、开源记忆组件、产品记忆机制、trace 库挖掘与失败归因、存储溯源与评测、data agent、与评估归因的耦合七个方向；第二阶段针对第一阶段的空白拓展 trace 预处理与结果判定、用户偏好、近似 trace 与选样、开源实现源码深读、评测方法五个方向；第三阶段补充 trace 标识与内容采集、生成侧模型接口两项基础事实；第四阶段回到全文与一手来源核实约 30 项被结论引用的说法，并补充相邻方向与 2026 年 7–10 月的新工作。方法包括阅读 arXiv 全文、在固定 commit 上只读源码、阅读官方文档与 changelog、在 Open-SWE-Traces 与 Who&When Pro 等公开数据集上做统计。证据分为原文或源码核实、据摘要、二手资料、未核实与公开数据统计五级（第 3.7 节），据摘要、二手资料、未核实三级在句末括注，公开数据统计注明样本与方法；与第四阶段核实结果冲突的说法按核实后的口径书写。

### 1.3 主要发现

1. 编码 agent 轨迹中工具消息占字节数 67.5%，只保留最近 10 条工具输出全文后体量降到 43.1%；运行时研究显示观察掩码能保持任务效果，而预先生成的摘要会丢失生成所需的信号（Meta-Harness 中分数 + 摘要 34.9，分数 + 源码 + 原始 trace 50.0）（第 4.1、9.3 节）。
2. OTel GenAI 语义约定已定义记忆操作 span、评估结果事件、上下文压缩标记、顶层 agent 实体与技能属性，全部为 development 级；用户、仓库与工作目录只能借用通用约定且实际埋点普遍缺省；编码 agent CLI 把用户与工具决策信息放在 logs 中，内容采集默认关闭（第 5 节）。
3. OpenAI 兼容服务在结构化输出上的公共子集只有 `json_object` 加 prompt 内 schema；strict schema、强制工具调用、seed、logprobs 的支持在服务之间与思考模式之间都不一致，服务端不保证可复现（第 6.8 节）。
4. 生产系统采用文本可读的非参数化存储；开源框架的溯源普遍薄弱，来源丢失多发生在合并操作中；只追加或只在存储层标记失效时旧条目仍会被检索并主导决策，删除源数据需按派生图级联（第 7.9 节）。
5. 自动构建与检索的记忆在编码任务上多数没有收益：VibeMemBench 中 12 组有 11 组未超过无记忆基线，直接注入已验证经验的 1.1–4.5 个百分点增益置信区间均跨零；已报告增益多在 1–5 个百分点，检出需要数百至上千个配对运行（估算）；整合质量分与真实迁移不相关（ρ=−0.24，n=12，置信区间跨零，据摘要）（第 8.3、8.8 节、《生成方法调研》第 4.6、4.7 节）。
6. 公开 SWE 数据集之间存在实例级重叠：SWE-bench Verified / Lite 与 Open-SWE-Traces 无实例重叠，SWE-Gym 与 SWE-rebench V1 有重叠；以 `(repo, PR 编号)` 为实例键的检查成本低（第 8.6 节）。
7. 结果信号的可靠性差异大。Open-SWE-Traces 上失败轨迹自述成功的比例（47.6%）高于成功轨迹（39.2%）；自评记忆的被信任错误比例单次静态估计为 0.28，闭环中为 0.42，更强或跨厂商的 judge 继承同一偏差，执行侧信号与检索外部信息的核验有效（2608.00017）；步骤级自动归因在长日志上为 3.5%–8.8%，接近随机基线 4.2%（Who&When）（第 9.4 节、《信号与选样调研》第 5.1、5.4 节）。
8. 对单一 agent 与模型，任务结果主要由任务本身决定：同 instance 分组中只有 12.2% 的组成败并存；放宽到同 repo 后可对比轨迹从 14.8% 增至 81.9%，新增对比大多混入难度差异，提高文本相似度只把成功率差从 0.36 降到 0.31。动作结构作为检索键几乎无用（P@1，即首位近邻与查询属于同一 repo 的比例：动作结构 0.043，文本 0.670）；阈值连通分量的可用阈值集中在 0.3 附近，簇标识跨运行不稳定（第 9.2、9.5、9.6 节、《信号与选样调研》第 4.4–4.6 节）。

### 1.4 主要倾向

下表为现有证据下更可取的做法及其适用条件，依据与证据强度见第 10 节。本表只列与本报告主题相关的环节。

表 1-1 主要倾向及其适用条件

| 环节 | 更可取的做法 | 适用条件 |
|---|---|---|
| 生成 | 逐 trace 抽取（可并行、可缓存）加分组后一次性整合；并行提议、层次合并，LLM 只输出增量操作与归属关系，合并由程序执行；输入保留回查原始 span 的能力；偏好条目保留原话、来源与适用条件 | 抽取结果按 trace 缓存；生成接口以 `json_object` 加客户端校验为公共子集 |
| 存储与溯源 | 文本可读的非参数化存储；原始 trace 不可变，记忆与来源多对多并在合并时继承来源；失效而不删除与使用驱动保留组合；删除按派生关系级联；检索层按归属、受众与状态过滤，派生条目保留来源的权限标签 | 需要人工审阅、按来源删除与多租户隔离 |
| 评测 | 以有 / 无记忆的端到端成功率差为主指标，并用"直接注入"与"自动生成 + 检索"两组对照分开测内容与检索；时间前向切分并按实例键去重；选成败并存实例提高功效；多重比较校正；离线效用统计只作排序信号 | 有可复跑环境；配对数达到数百至上千 |

### 1.5 主要开放问题

- 以 span 为锚点的溯源，以及以 span 为锚点的撤销与重新生成，尚无研究与开源实现；按源数据删除的级联修复已有研究（第 7.7.6 节）。
- 多层 span 树作为生成输入的收益、压缩对抽取质量的影响，均无直接消融。
- 无标签 trace 上 judge 误差对记忆质量的影响，以及纯离线 trace 库上三类门控方式的效果比较，缺少实证。
- 整合环节的投毒（少量一致记录即可越过频次门槛）与代码库、schema 演化后的记忆失效，缺少成熟方案。

## 2. 问题与术语

### 2.1 研究问题

Agent 运行时产生 trace：用户消息、模型调用、工具调用及其返回、最终结果，以及关联到这些对象的评估分数。trace 平台把它们持久化后，库中积累了同一 agent 在大量任务上的执行记录。本报告研究的问题是：从已存的 agent trace 中有策略地批量生成记忆，供产生这些 trace 的 agent 在后续任务中使用。记忆服务于三个目的：避免重复已经出现过的错误；复用已经验证有效的做法；遵循用户表达过的偏好与约定。场景上优先 code agent（修复 issue、实现功能、运行测试）与 data agent（text-to-SQL、数据分析、数据整理），两者的结果都可以通过执行（测试、查询结果）验证，且都有公开的执行轨迹数据（第 9 节）。

问题的限定如下：

- **输入为已存 trace**。生成在 agent 运行之外的后台或离线流程中进行，可以跨会话、跨任务观察。会话内由主 agent 即时写入记忆的做法（如 GitHub Copilot 的 `store_memory`、Claude Code auto memory，《生成方法调研》第 6 节）作为对照。
- **有策略**。库中 trace 的数量远超可逐条调用模型的预算，需要决定哪些 trace 进入生成（选样）、按什么单位做对比与归纳（分组），以及预算如何分配。
- **批量**。生成以批为单位运行，包含逐条 trace 的抽取与跨 trace 的整合，需要处理增量、去重、冲突与淘汰。
- **使用方为同一 agent**。记忆以文本可读的条目注入该 agent 的上下文。把经验固化进模型权重属于相邻方向（《生成方法调研》第 7 节）。

该问题可拆分为以下子问题：

表 2-1 子问题与对应章节

| 子问题 | 内容 | 主要章节 |
|---|---|---|
| 输入加工 | span 树的渲染、压缩与任务切分；无标签 trace 的结果判定；trace 中可用的标识字段 | 第 4、5 节 |
| 选样与分组 | 选取哪些 trace；同任务与近似任务分组；预算分配 | 第 9 节、《生成方法调研》第 4 节、《信号与选样调研》第 4 节 |
| 生成方法 | prompt 与流水线；成败分路；对比归纳；生成后校验 | 《生成方法调研》第 4、5、6 节 |
| 整合与生命周期 | 合并、去重、冲突消解、更新操作、遗忘 | 第 7 节、《生成方法调研》第 4、5 节 |
| 存储与溯源 | 存储形态；记忆到 trace / span 的回指 | 第 7 节 |
| 与评估、归因的关系 | outcome 信号、归因输出、记忆效用的反向统计 | 《信号与选样调研》第 5 节 |
| 偏好与 data agent | 偏好信号的识别与写入门槛；data agent 特有的记忆类型 | 《信号与选样调研》第 6、7 节 |
| 评测 | 基准、指标、数据泄漏 | 第 8 节 |

### 2.2 术语

下表只列本报告用到的术语；完整术语表、综述分类法对照与本调研使用的记忆分类见《生成方法调研》第 2.2–2.4 节。

表 2-2 术语

| 术语 | 定义 |
|---|---|
| 记忆（memory） | 从历史执行中生成、持久保存、在后续任务中注入 agent 上下文的文本条目。本报告的记忆指跨任务持久的外置记忆；单次任务内的上下文与 scratchpad（工作记忆）以及模型权重不在此列 |
| trace / span | 采用 OpenTelemetry（OTel）的定义。trace 是一次端到端执行，由若干 span 组成树；span 是其中的一次操作，带 `trace_id`、`span_id`、`parent_span_id`、起止时间、状态与属性。OTel GenAI 语义约定把 agent 的操作区分为 `invoke_agent`、`chat`（一次模型调用）、`execute_tool` 等。Langfuse 中对应的对象为 trace 与 observation |
| 会话（session） | 同一用户与 agent 的一段连续交互，可包含多个 trace；OTel 中以 `gen_ai.conversation.id` 或 `session.id` 标识 |
| 经验记忆（experiential memory） | 从执行结果中总结的教训与策略，例如某类失败的原因与预防方法、失败后的恢复步骤、较优的处理方式。术语取自 Hu 等（2512.13564） |
| 偏好记忆 | 用户表达的、跨任务成立的要求与约定，例如代码风格、提交流程、输出格式、指标口径。信号主要来自用户消息中的纠正、明确要求与拒绝 |
| 程序性记忆（procedural memory） | 可按步骤执行的流程，例如工作流、SOP、技能文件（`SKILL.md`）、可复用脚本与已验证查询模板 |
| outcome | trace 或任务级的结果标签，例如单元测试判定的 resolved、环境 reward、评估分数、用户确认 |
| 信号来源类别（signal source type） | outcome 的来源类别：ground truth 或测试、执行信号（退出码、报错、空结果）、用户反馈、LLM judge、agent 自述。不同类别的可信度不同，同一 outcome 需要记录其来源类别 |
| 失败归因（failure attribution） | 在失败 trace 中定位出错的 agent、步骤与错误类型；结果可作为生成预防类记忆的输入（《信号与选样调研》第 5 节） |
| 溯源（provenance） | 记忆与其来源之间的可回溯关系：来源 trace / span、派生自哪些已有记忆、由哪一次生成运行产生。用途包括回源校验（回到来源内容核对记忆是否仍成立）、安全门控（按来源可信度过滤）与价值归因（第 7 节） |
| 阶段一 / 阶段二生成 | 两阶段生成流水线的两个环节：阶段一对单条 trace 或单个会话做抽取，可并行、可缓存；阶段二读取阶段一的产物做跨 trace 整合，通常串行、全局执行。OpenAI Codex CLI 的 Phase 1 / Phase 2 是该模式的典型实现（《生成方法调研》第 6 节）。注意与第 3 节中描述调研过程的"第一阶段"至"第四阶段"区分 |
| 成败并存 | 同一分组（如同一任务的多次执行）内同时有成功与失败的 trace，是做成败对比的前提 |
| 难度混杂（confounding） | 跨任务比较成功与失败轨迹时，任务本身的难度差异混入对比，对比得到的差异同时反映难度与 agent 行为 |
| 自述成功 | agent 在最后的输出中自称任务完成或成功，未经外部判定 |
| 晋升（升格） | 候选条目达到写入门槛后成为生效记忆，或从一种载体提升到更持久、作用范围更大的载体（如从可检索条目到技能文件） |
| 选样（selection） | 决定哪些 trace 进入生成，依据包括 outcome、是否同组成败并存、是否为失败后恢复、新颖度与预算 |
| 分组（grouping） | 把 trace 组织为对比或归纳的单位，例如同一任务、同一仓库或数据库、近似 trace 簇 |
| 整合（consolidation） | 把候选记忆并入已有记忆库：去重、合并、冲突消解、按支持频次保留、以增量操作（ADD、UPDATE、DELETE 等）更新 |
| 注入（injection） | 记忆进入 agent 上下文的方式：每次会话常驻加载的小体量条目、按任务相似度检索的条目、由 agent 通过工具按需读取的条目 |
| 个百分点（pp） | 两个百分比之间的差值单位。正文写"个百分点"，表格内简写为 pp；"%"只用于比例本身或相对变化 |
| GT | ground truth，标准答案或测试判定的真实结果 |
| harness / scaffold | 包裹模型的 agent 运行框架，含系统提示、工具定义、上下文管理与执行循环 |

## 3. 调研方向与过程

### 3.1 总体安排

调研分四个阶段进行，检索截至 2026-10-09。第一阶段做广度覆盖，第二阶段针对第一阶段暴露的空白做拓展与聚焦，第三阶段补充 trace 侧与模型侧两项基础事实，第四阶段做可信度核实并补充相邻方向与最新工作。

表 3-1 调研阶段与方向

| 阶段 | 方向 | 主要材料 |
|---|---|---|
| 第一阶段：广度调研 | 1 从轨迹生成经验记忆的论文；2 开源记忆组件；3 产品记忆机制；4 trace 库挖掘、聚类与失败归因；5 存储、溯源、生命周期与评测；6 data agent 的经验记忆；7 记忆生成与评估、归因的耦合 | 论文、源码、产品文档、规范、公开数据集 |
| 第二阶段：拓展与聚焦 | 1 trace 预处理与结果判定；2 用户偏好与纠正挖掘；3 近似 trace 判定、分组归纳与选样；4 开源实现源码深读；5 评测方法 | 论文、源码、公开数据集统计 |
| 第三阶段：补充 | 1 trace 标识与内容采集；2 生成侧的模型接口 | 规范、源码、服务文档 |
| 第四阶段：补充 | 1 可信度核实；2 相邻方向；3 2026 年 7–10 月新工作与覆盖缺口 | 论文全文、官方文档与 changelog、固定 commit 源码 |

本报告涉及第一阶段方向 5，第二阶段方向 1、5，第三阶段方向 1、2 与第四阶段方向 1、3，下文只展开这些方向；其余方向见《生成方法调研》与《信号与选样调研》第 3 节。

### 3.2 第一阶段：广度调研

**方向 5：存储、溯源、生命周期与评测。** 要回答的问题：记忆有哪些存储形态及其取舍，各综述如何分类，溯源的现有做法与研究，更新、冲突、遗忘机制，以及评测基准与指标。检索范围为 7 篇综述、主要系统（Zep / Graphiti、Mem0、MemOS、A-MEM 等）、2026 年的溯源研究（TierMem、MemLineage、MemQ、Eywa）、W3C PROV-O 与 OpenLineage 词汇、对话记忆与经验记忆基准。发现：生产系统全部采用文本可读的非参数化形态；溯源的三种用途（回源校验、安全门控、价值归因）对应不同字段；LoCoMo、LongMemEval 等对话记忆基准与经验复用能力的相关性弱。详见第 7、8 节、《生成方法调研》第 2.3 节。

第一阶段同时核对了 Open-SWE-Traces 与 Who&When Pro 两个公开数据集中可用于选样与评测的标签（第 9 节）。

### 3.3 第二阶段：拓展与聚焦

**方向 1：trace 预处理与结果判定。** 由第一阶段的两项空白引出：现有实现先把 trace 压平为消息文本，父子结构、耗时、状态码丢失；多数方法依赖 ground truth，生产 trace 多数无标签。要回答的问题：span 树如何渲染与压缩，会话内如何切分任务，OTel 消息记录的语义如何影响输入组装，无标签 trace 如何判定结果，脱敏与外部内容如何处理。方法为文献与源码阅读，加上在 Open-SWE-Traces 上的统计。关键过程是在 1,800 条轨迹上测量工具输出占比与两种压缩方式的效果，以及在 623 条带标签轨迹上检验 agent 自述成功的区分度。发现：工具消息占轨迹字节数的 67.5%，观察掩码是主要压缩手段；失败轨迹自述成功的比例（47.6%）高于成功轨迹（39.2%），自述没有区分度；带执行能力的 judge 优于只读文本的 judge，judge 读取 agent 的思考文本时会受其情绪影响（R2E-Gym，2504.07164）。详见第 4、9 节。

**方向 5：评测方法。** 由第一阶段的评测分层引出：端到端有 / 无记忆对照、操作级与溯源级离线评测可在哪些公开数据与协议上进行。方法包括统计 Open-SWE-Traces 的来源子集与标签分布；以"仓库名 + PR 编号"为实例键，通过 HF datasets-server 只读远端 parquet 的标识列，检查与 SWE-bench Verified、SWE-Gym、SWE-rebench 等基准的实例重叠；阅读 HaluMem、Evo-Memory、MemRL、AMemGym 等评测方法。发现：带标签轨迹只来自 4 个 agent × 模型组合；SWE-bench Verified / Lite 与 Open-SWE-Traces 无实例重叠，SWE-Gym 与 SWE-rebench V1 有重叠；已报告的记忆增益为 1.1–5.3 个百分点，按配对二值结果估算，检出这一量级需要数百至上千个配对运行；记忆被检索后的成功率作为效用统计存在选择偏差与信用分配问题。详见第 8、9 节。

### 3.4 第三阶段：补充

**方向 1：trace 标识与内容采集。** 由第二阶段引出：偏好的作用域需要用户、会话、仓库等标识；记忆效用统计需要记忆使用记录；结果判定需要评估事件。要回答的问题：会话、用户、作用域、消息结构、outcome、记忆使用分别由 OTel 语义约定、Langfuse 映射与主流 agent 埋点中的哪些属性承载。检索范围为 `open-telemetry/semantic-conventions-genai` 仓库（commit `06ec68e`，2026-10-07，schema `gen-ai-dev/1.42.0-dev`）与通用约定、Langfuse v4 源码、Claude Code 与 Codex 的遥测文档、主流框架的埋点实现。发现：GenAI 约定中承载身份的属性均为 development 级；用户、仓库、工作目录没有 GenAI 属性，只能借用 `user.id`、`vcs.*`、`process.working_directory` 等通用约定；会话 ID 的属性名在各实现中分散；没有 agent 或框架默认导出仓库与工作目录；Claude Code 与 Codex 的用户信息与工具决策主要出现在 logs 事件中。详见第 5 节。

**方向 2：生成侧的模型接口。** 由生成方法的调研引出：记忆抽取与 LLM judge 通过 OpenAI 兼容接口调用模型，各服务在结构化输出与思考模式上存在差异。要回答的问题：参数支持、结构化输出做法、上下文与最大输出、思考模式对 JSON 输出的影响、长输入可靠性、缓存与可复现、测试方法。检索范围为 DeepSeek、阿里云百炼、MiniMax、vLLM、SGLang、OpenRouter 的官方文档，结构化输出工具，以及格式约束影响推理质量的研究（2408.02442）。发现：`json_object` 加 prompt 内给出 schema 是唯一普遍可用的公共子集；DeepSeek 与 Qwen 的思考模式不支持强制工具调用；`seed` 在各服务均为尽力而为，可复现依赖客户端侧的响应缓存。详见第 6 节。

### 3.5 第四阶段：补充

**方向 1：可信度核实。** 要回答的问题：被结论引用、但前三阶段只据摘要或二手资料的说法是否成立。范围为 26 组、约 30 项说法，覆盖失败归因准确率、记忆判定误差、偏好写入风险、编码记忆基准、簇稳定性阈值、产品记忆机制、开源框架行为、trace 平台规模、生成 prompt 的公开情况、评测标签误差、注入防护、模型 API。方法为回到 arXiv HTML 全文或 PDF、官方文档与 changelog、固定 commit 的源码，判定为"确认""更正""无法核实"三类，并对可复算的数字做复算。关键更正举例：Who&When（2505.00212）的 agent 级 53.5% 与 step 级 14.2% 来自两种不同方法、为四个设置的平均，长日志手工系统上 step 级只有 3.5%–8.8%，接近随机基线 4.2%；AgenTracer（2509.03312）"至多高 18.18%"无法从正文表格复算，表中最大绝对差为 17.4 个百分点；PASB 的两组为观察分组；VibeMemBench（2609.23570）直接注入的 1.1–4.5 个百分点增益置信区间均跨零，且目标按"注入经验有效"筛选；Claude Code Auto Dream 在官方文档与 changelog 中不存在，只能作为第三方逆向描述；Cursor Memories 已于 2.1.x 移除；LangSmith Engine 截至 2026-09-24 分析超过 7000 万条 trace；Raindrop 的官方描述为按描述训练的小模型分类器，与 embedding 聚类属于不同路线；Mem0 2.x 的开源写入只做 ADD。无法核实的项为 Claude Code Auto Dream 的细节与 ChatGPT 偏好遵循 71.3% 的官方出处。凡与核实结果冲突的说法，本报告以核实结果为准。

**方向 3：2026 年 7–10 月新工作与覆盖缺口。** 要回答的问题：前三阶段之后出现了哪些相关论文；隐私、删除、投毒与注入策略方面有哪些覆盖缺口。方法为逐篇阅读 arXiv 摘要页，按相关度记录；删除与投毒方向检索 2026 年 2–10 月的工作。发现：同任务多模型轨迹对比（CONTRAMEM，2608.22533）、只读环境中核验候选记忆后再写入（2609.11060）、技能的版本化与回放不退化门控（Skill-V，2610.11781）等新工作；源数据删除后派生记忆仍可见的级联问题及其修复（MEMOREPAIR，2605.07242；Agentic Unlearning，2602.17692）；两篇新综述（2607.10113、2608.03392）。限制：该方向证据多为摘要级；ICLR 2027 投稿尚未被索引，2026 年 10 月上旬的覆盖可能不全。详见第 7 节、《生成方法调研》第 4、7 节。

### 3.6 方向之间的衔接

表 3-2 方向之间的衔接

| 前一阶段发现的空白或问题 | 引出的方向 |
|---|---|
| 现有实现把 trace 压平为文本，未利用 span 树 | 第二阶段方向 1（预处理）；第三阶段方向 1（标识与内容采集） |
| 多数方法依赖 ground truth，生产 trace 无标签 | 第二阶段方向 1（结果判定） |
| 对话记忆基准与经验复用相关性弱 | 第二阶段方向 5（评测方法） |
| 偏好作用域与记忆效用需要 trace 中的身份与使用记录 | 第三阶段方向 1 |
| 生成依赖 OpenAI 兼容接口上的结构化输出 | 第三阶段方向 2 |
| 结论引用了只据摘要或二手资料的数字 | 第四阶段方向 1（可信度核实） |
| 删除级联、投毒与注入时机覆盖不足；2026 年下半年新工作 | 第四阶段方向 3 |

### 3.7 证据分级与核对方法

表 3-3 证据分级

| 级别 | 依据 | 报告中的标注 |
|---|---|---|
| 原文或源码核实 | arXiv HTML 或 PDF 全文、官方文档、官方博客、changelog、固定 commit 的源码、HF 数据卡与 API | 不加标注 |
| 据摘要 | 只读 arXiv 摘要页或项目首页 | 句末括注"据摘要" |
| 二手资料 | 搜索摘录、第三方文章、其他论文的转述 | 句末括注"二手资料" |
| 未核实 | 一手来源不可访问，或官方来源中不存在该说法 | 句末括注"未核实" |
| 公开数据统计 | 在公开数据集上按给定样本与方法所做的统计 | 注明样本、方法与结论边界（第 9 节） |
| 推论 | 由已核实的事实推出、尚无实验或文献直接验证的判断 | 句末括注"推论"；设计取向的判断写作"调研倾向（推论）" |
| 估算 | 按公开数字与写明的假设计算得到、未经实测的量 | 句末括注"估算"，并写明假设 |

表格中的"证据"或"级别"列以"原文""摘要"（或"据摘要"）"二手资料""未核实"表示同样的级别，官方文档与工程博客注明来源类型；单元格内的数字只据摘要时，在该单元格括注"据摘要"。

核对方法如下：

1. 论文数字注明表号、图号或节号，并记录实验条件（模型、数据子集、种子数）。同一数字在摘要与正文中口径不同时，以正文表格为准；不能从表格复算的数字注明"作者称"。
2. 源码事实记录仓库、commit 与文件行号。论文描述与源码行为不一致时，两者分别陈述（《生成方法调研》第 5.5 节）。
3. 产品能力与平台规模属于时效信息，注明官方来源的发布日期；官方页面无法访问时，注明核对途径（如官方 RSS 与检索摘录）。
4. 公开数据集统计只读取所需的列，并以总量交叉校验（例如 Open-SWE-Traces 各组合的成功与失败轨迹之和等于全集的带标签轨迹数）。
5. 第四阶段的核实独立于前三阶段重新取证；前三阶段中被更正的说法在本报告中按更正后的口径书写，无法核实的项不作为结论依据。

## 4. trace 预处理与生成输入

本节讨论把存储中的 span 树加工为记忆生成输入的各个步骤：渲染与压缩、任务切分、消息记录的语义与重组、无标签 trace 的结果判定、脱敏与外部内容标记，以及由此引出的记忆投毒入口。

### 4.1 渲染与压缩

表 4-1 trace 渲染与压缩做法

| 做法 | 机制 | 实证 |
|---|---|---|
| Codex 分层预算（v2） | 证据分层，优先级 Human > Final（最终回复）> OtherAgent > Commentary > Context > Tool；由新到旧选入、按原序渲染，跳过区间用占位文本；单条工具输出截断到约 2000 token、单行 10 KB；全部文本先脱敏 | 无公开消融 |
| PostHog 行号树 | ASCII 树，每行 `L001:`；超长时保留头部、均匀下采样并注明显示比例 | 无公开消融 |
| LangSmith Engine 压缩视图 | 每轮一行：role、工具名、延迟、内容长度；正文按需加载 | — |
| 观察掩码（Complexity Trap，2508.21433） | 保留全部推理与动作，早于最近 M 轮的观察替换为占位（SWE-agent M = 10） | SWE-bench Verified、5 组模型：4 组成本降 50%–57%；解决率与 LLM 摘要持平或略高（Qwen3-Coder 480B：54.8% vs 53.8%，原始 53.4%）；观察约占单轮 token 的 84% |
| LLM 摘要（同上对照） | 每 21 轮摘要一次，保留最近 10 轮原文 | 轨迹比掩码长约 13%–15%；摘要调用占实例成本至多 7.2%；混合方案再省 7%–11% |
| AgentDiet（2509.23586） | 浪费分无用、冗余、过期三类；小模型在第 s 步回看第 s − 2 步，替换为保留结构的短版本 | 输入 token 降 39.9%–59.7%，计入开销后成本降 21.1%–35.9%，通过率变化 −1.0 ～ +2.0 个百分点 |
| ACON（2510.00615） | 用"全上下文成功、压缩后失败"的对比样本迭代改写压缩准则，再蒸馏到小模型 | 峰值 token 降 26%–54%（据摘要） |
| SWE-Pruner（2601.16746） | agent 给出目标提示，0.6B 模型对文件读取输出逐行保留相关行 | token 降 23%–54%（二手资料） |
| Acontext | 每条消息截断到 512 字符后交给蒸馏 prompt | 无消融 |

上述压缩研究都在 agent 运行时进行，目标是降低推理成本并保持成功率。记忆生成是离线读取，可以看到整条轨迹的后续部分，"过期"判定可以依据后文是否再引用该内容。

压缩对记忆生成质量的证据：

表 4-2 压缩对记忆生成质量的证据

| 证据 | 内容 |
|---|---|
| Meta-Harness（2603.28052） | proposer 只看分数时中位准确率 34.6，分数 + LLM 摘要 34.9，分数 + 源码 + 原始执行 trace 50.0（在线文本分类）；TerminalBench-2 运行中 proposer 每轮中位读取 82 个文件，约 40% 为执行 trace。作者结论为摘要无法恢复丢失的信号 |
| 2601.22436 | 4 个框架、13 个骨干模型、9 个环境：agent 稳定依赖原始经验，常忽略或误读压缩后的经验，即使压缩经验是唯一提供的经验 |
| 2605.28224 | 单任务多次尝试之间传递原子事实对准确率无影响，在环境结构可复用的任务上把轨迹缩短 19%–26% |
| ReMe（2512.10696） | keypoint 级经验优于轨迹级 |
| TraceElephant（2604.22708） | 完整 trace 比只有输出时的归因准确率相对高 22%（agent）与 76%（step） |
| R2E-Gym（2504.07164） | 判定输入含 agent 思考文本时，验证器以其中情绪作为正确性代理 |

Open-SWE-Traces 1,800 条轨迹的体量统计（第 9.3 节）：平均每条约 221.5 KB（约 55k token），tool 消息占 67.5%；每条工具输出截断到 8 KB 后总量剩 84.4%，只保留最近 10 条工具输出全文后剩 43.1%。单条截断只处理少数超长输出，观察掩码是主要的压缩手段。

检索范围内未见"先压缩再抽取经验、与原始轨迹抽取对比"的直接消融。现有证据指向两点：预先生成的 trace 摘要会丢失生成所需的信号（Meta-Harness），规则截断与观察掩码在保持任务效果的同时去掉大部分体量（Complexity Trap）。现有渲染做法中，PostHog 以带行号的树渲染并要求摘要引用行号，LangSmith Engine 先读每轮一行的压缩视图、深查时再加载全文，Codex v2 按证据层级在预算内选入正文（表 4-1）；这些做法适用于需要回查原文的批量挖掘。调研倾向（推论）：渲染结果保留回查原始 span 的能力，报错 span 及其前后步骤、验证类命令的最后输出在预算中优先。

### 4.2 任务切分

表 4-3 任务切分做法

| 做法 | 切分依据 | 结果 |
|---|---|---|
| Codex Phase 1 | prompt 内由模型划分任务，每个任务单独标 outcome；不同工作目录拆为不同条目 | — |
| Acontext Task Agent | "Tasks = user requests, NOT agent execution steps"；任务关联消息区间、记录里程碑，进入终态时触发学习 | — |
| Nemori（2508.03341） | 缓冲满 20 条消息后由 LLM 划分不相交 episode；线索为话题变化、意图转换、过渡语、间隔超过 30 分钟 | LoCoMo：自适应划分 80.8，固定 20 条切块 75.7；窗口 5–40 波动在 ±1% 内；划分占构建 token 15.3% |
| SeCom（2502.05589） | 话题连贯的段作为记忆单元，与轮级、会话级、摘要级对比 | 段级优于其他粒度（二手资料） |
| H²R（2509.12810） | 事后由 LLM 从任务与轨迹推断子目标序列，再把成功轨迹切成对应子轨迹；高层存规划洞见，低层存执行洞见 | AlfWorld 75.9（ExpeL 72.4），PDDLGame 80.5（72.2）；每环境 3 个测试 episode |
| Learn-by-Interact（2501.10893） | 对任意观察区间 T[i:j] 由 LLM 重写子轨迹实际完成的指令；LLM 评审一致通过才保留 | 保留约 37%；训练场景中该步骤贡献至多 14.0% |

现有做法形成"会话 → 任务 → 子目标段"三层：任务的单位是用户请求（Codex、Acontext 一致）；子目标切分是另一层，只在产出流程类记忆时进行（H²R、Learn-by-Interact）。用户后续消息需先分类再决定边界：SWE-chat（2604.20779，约 18,000 个会话、229,000+ 条用户提示）中约 46% 的提示为纠正、拒绝或失败报告，另有 4.5% 的轮次被打断；这类消息属于同一任务的继续，同时是结果信号（第 4.4 节）。Learn-by-Interact 的重写适用于实际完成内容与用户请求不符的轨迹，失败轨迹中的成功片段可按其实际效果作为流程记忆。

span 结构提供的边界候选，按可靠度排列：trace 边界（多数 SDK 每个用户请求一个 trace，会话由 `session.id` 或 `gen_ai.conversation.id` 关联）；`invoke_agent` 根 span 与子 agent span；新 user 消息位置；工作目录、仓库、分支、数据库连接的变化；相邻 span 的空闲间隔；用户消息的语义转换（需 LLM 判定）。前五项可由确定性规则得到。

### 4.3 消息记录语义与重组

OTel GenAI 语义约定中与内容采集相关的属性如下，全部为 Development 状态（详见第 5.2 节）：

表 4-4 OTel GenAI 中与内容采集相关的属性

| 属性 | 级别 | 规定 |
|---|---|---|
| `gen_ai.input.messages` | Opt-In | "The chat history provided to the model as an input"；按发送给模型的顺序记录；instrumentation 可过滤或截断 |
| `gen_ai.output.messages` | Opt-In | 每条对应一个 choice；出错时仅在确有输出时设置 |
| `gen_ai.system_instructions` | Opt-In | 与对话历史分开提供的系统指令 |
| `gen_ai.tool.call.arguments` / `.result` | Opt-In | 工具 span 上的参数与结果 |
| `gen_ai.request.previous_response.id` | Recommended | 有状态 API 引用的前一响应 |
| `gen_ai.conversation.id` | Conditionally Required | 有现成会话 ID 时填写，不应（SHOULD NOT）用新 UUID、trace ID 或内容哈希代替 |
| `gen_ai.conversation.compacted` | 条件 | 只在确知发生上下文压缩时置 true |

消息 schema 中 `role` 取 system / user / assistant / tool，工具调用请求与结果分别为 `tool_call` 与 `tool_call_response` 两种 part，以 `id` 与 execute_tool span 的 `gen_ai.tool.call.id` 配对；schema 中没有内容来源或可信度字段。结构化属性在 span 上序列化为 JSON 字符串，允许按消息截断内容但保持 JSON 结构；"记录外部内容引用的通用方式"在约定中仍为 TODO。

约定按"该次调用实际发送的内容"定义输入。对无状态 chat API，每次调用都携带完整历史，记录即为累积语义；对有状态 API，记录只含本次新增输入，历史需沿 `previous_response.id` 回溯。约定中没有增量语义与跨 span 去重机制，Langfuse 的 generation 同样按每次调用的完整输入记录。累积语义下，一条含 n 次模型调用的轨迹，其记录的消息总量随 n 近似二次增长，增量记录随 n 线性增长。

主流编码 agent 的内容采集默认关闭，用户提示与工具决策主要出现在 logs 事件，trace 中缺少这部分内容（第 5.5 节）。

重组时需处理三种来源：累积记录取相邻调用输入的最长公共前缀，后缀为新增；增量记录按时间序拼接；有状态 API 沿 `previous_response.id` 链拼接。累积语义下的前缀断裂对应上下文压缩、截断、系统提示变化或子 agent 独立历史，可与 `gen_ai.conversation.compacted` 对照，作为"后续行为可能因遗忘出错"的信号保留。工具结果在 tool span 与下一次调用的 tool 角色消息中各出现一次，按工具调用 ID 合并，保留 tool span 的状态与耗时。

### 4.4 无标签 trace 的结果判定

表 4-5 无标签 trace 的结果信号

| 信号 | 场景 | 证据 |
|---|---|---|
| 测试 / 构建退出码；同一测试由失败转为通过 | code | trace 内可直接计算 |
| commit 存活 | code | SWE-chat v2：agent 输出进入提交的比例 59.4%，人保留 agent 净输出的比例 65.4% |
| PR 合并 | code | 各 agent 合并率约 55%–86%，口径差异大（二手资料，AIDev 后续研究）；被拒 PR 中只有 35.7% 属于明确的 agent 失败（2605.22534，据摘要） |
| 用户 pushback / 打断 | code、data | SWE-chat：约 46% 提示为纠正、拒绝或失败报告，4.5% 轮次被打断 |
| 用户转入下一任务且无遗留 | 通用 | Codex 分诊：倾向 success；同一产物反复修改为 partial；只有助手自称成功为 uncertain |
| 执行报错、空结果、超时 | data | span 状态与工具返回 |
| 执行验证门控 | data | Crystallization（2608.07213）：验证门控贡献 +4.85pp，无门控的自投票卡片库比无记忆低 2.03pp |
| 带执行能力的 judge | 通用 | Agent-as-a-Judge 与人类共识 83.9%–92.1%；R2E-Gym 执行与无执行验证器互补，混合 51.0% |
| 只读 judge | 通用 | AgentRewardBench 最佳精确率约 69%–70%；DeepSWE 中独立 judge 与验证器不一致 32.4% |
| 步级过程奖励模型 | 通用 | AgentPRM（2511.08325）、ToolPRMBench（2601.12294）主要服务测试时搜索（二手资料）；SWE-RM（2512.21919）无执行奖励模型用于测试时扩展，Qwen3-Coder-Flash 51.6 → 62.0（据摘要） |

agent 自述不能作为结果信号，有三方面证据。Open-SWE-Traces 上 623 条有结果标签的轨迹中（第 9.4 节），失败轨迹最后一条助手消息含成功措辞的比例为 47.6%，成功轨迹为 39.2%，自述没有区分度。Memory Reward Inflation 中自评把自身错误答案判为正确的比例为 0.31–0.54（《信号与选样调研》第 5.4 节）。R2E-Gym 中无执行验证器受 agent 思考文本中的情绪影响。

各类信号可靠性的现有证据：带执行能力的 judge 优于只读 judge（Agent-as-a-Judge、R2E-Gym），只读 judge 的成功标签精确率约 70%（AgentRewardBench），独立 judge 与验证器的不一致率为 32.4%（DeepSWE）；Codex 第一阶段 prompt 规定显式反馈优先于其他线索，只有助手自称成功时记为 uncertain。调研倾向（推论）：agent 自述不作为结果信号；judge 输入去掉隐藏推理与完成自述，只保留任务、动作、执行证据与用户反馈。无结果信号的 trace 仍可用于偏好与环境事实类记忆，这两类不依赖成败（《信号与选样调研》第 5.3 节 A 类）。

### 4.5 脱敏、外部内容标记与记忆投毒入口

**密钥与个人信息。** Codex 的 `redact_secrets` 用 4 条正则（`sk-` 开头的 key、AWS `AKIA`、`Bearer` token、`api_key / token / secret / password = 值`），在输入渲染与输出解析后各执行一次；gitleaks、TruffleHog、detect-secrets 提供覆盖主流云与 SaaS 凭据的规则库（二手资料）；Microsoft Presidio 以 NER 与正则识别个人信息（二手资料）；Clio 在摘要阶段去隐私并设簇最小唯一用户数。data agent 的查询结果行本身是业务数据，可能含个人信息；记忆需要的是表列语义、口径与查询模式。

**外部内容标记。** Spotlighting（2403.14720，模型为 text-davinci-003、GPT-3.5-Turbo、GPT-4）用变换标出外部输入的来源：

表 4-6 Spotlighting 外部内容标记的效果

| 方法 | 间接注入成功率 | 任务效果 |
|---|---|---|
| 分隔（delimiting） | GPT-3.5-Turbo 摘要任务约 60% 降到约一半；易被绕过，作者不推荐 | — |
| 数据标记（datamarking） | 摘要：GPT-3.5-Turbo 约 50% → 3.1%；问答：GPT-3.5-Turbo 8.0%，GPT-4 1.0% | SQuAD、IMDB、WiC、BoolQ 上无损（GPT-3.5） |
| 编码（base64） | GPT-3.5-Turbo 摘要 0.0%、问答 1.8% | 对 GPT-3.5 损害大，对 GPT-4 基本无损 |

Codex 把 rollout 视为不可变证据、第三方内容视为数据，输入模板声明不执行其中指令；开启 `disable_on_external_context` 后，调用过 MCP、web 搜索、tool search 的会话整体退出记忆输入，已整合的相关记忆在下次整合中被遗忘。该判定以会话为单位；OTel 的 `gen_ai.tool.type` 区分工具类型（function / extension / datastore），不区分内容是否来自外部，MCP 调用另有 `mcp.*` 属性可用于识别。

**记忆投毒。** 以下工作除 MINJA 外均据摘要：

表 4-7 记忆投毒与防御

| 工作 | 入口 | 结果 |
|---|---|---|
| MINJA（2503.03704） | 攻击者只通过普通查询让 agent 写入恶意记录 | 注入成功率 98.2%，攻击成功率 76.8%；嵌入层清洗不可行，提示级检测漏检或误报高 |
| Sleeper Memory Poisoning（2605.15338） | 外部文档诱导写入关于用户的伪造记忆 | 写入率最高 99.8%；检索命中后 60%–89% 导致攻击者期望的动作 |
| Injection-Execution Dissociation（2605.08442） | 恶意指令进入记忆 | 存储率 > 97.5%，执行率 0%–95% 且与存储率无关；把召回记忆与可执行上下文隔离后 9 个模型中 8 个降到 0% |
| A-MemGuard（2510.02373） | 防御：多条相关记忆的推理路径一致性校验，失败教训单独存储 | 攻击成功率降低 95% 以上 |

整合环节的投毒（PoisonedEvolution、TBA、OEP）见《生成方法调研》表 4-6，整合中的来源权限丢失（AuthMem-Bench、TMA-NM）见第 7.4 节。

从 trace 批量生成记忆时的投毒入口与文献中对应的措施：

表 4-8 投毒入口与文献中的措施

| 入口 | 文献中的措施 |
|---|---|
| 多租户场景下用户消息中的恶意内容 | 记忆作用域绑定用户或项目；按写入时在场受众限制可见范围（2608.17148，据摘要） |
| 工具输出中的注入指令（README、issue、网页、MCP 返回） | 渲染时标记来源（datamarking 一类）；外部内容中的指令不构成偏好或规则；仅由外部内容支持的候选隔离 |
| 低成本重复投毒绕过频次门槛 | 权限提升需抗伪造佐证（TMA-NM，据摘要）；PoisonedEvolution、TBA 只报告了攻击，未给出针对频次门槛的防御 |
| agent 自身的总结与幻觉陈述 | 持久化来源权限标签，agent 自身总结不提升权限（AuthMem-Bench、TMA-NM，据摘要） |

### 4.6 结论与倾向

结论：运行时压缩研究表明规则截断与观察掩码能去掉大部分体量而保持任务效果，但预先生成的摘要会丢失生成所需的信号，压缩对记忆抽取质量的直接影响尚无消融。会话内任务切分在现有工作中以用户请求为单位，子目标切分只用于流程类记忆。OTel 约定下的消息记录为累积语义且内容采集默认关闭，记忆生成需要先重组再渲染。agent 自述不能作为结果信号，judge 的可靠性取决于是否具备执行能力及其输入是否去除自述。经验整合本身是投毒入口，少量一致的恶意轨迹即可被固化为规则。

调研倾向：预处理保持原始 trace 不变，脱敏、掩码与标记只作用于渲染结果；渲染保留回查原始 span 的能力；外部内容在渲染时标记来源，并把来源类别带入候选记忆的写入门槛。

## 5. trace 中的标识与内容采集

从存量 trace 批量生成记忆，需要从 trace 中读出几类信息：任务与会话边界、用户与作用域、消息与工具调用的配对、结果信号、记忆本身的使用记录。本节梳理这些信息在 OpenTelemetry（OTel）GenAI 语义约定、通用语义约定、Langfuse 的 OTel 接收逻辑以及主流 agent 与框架实际埋点中的承载方式，归纳生态现状下的可得性与缺口。

### 5.1 记忆生成对 trace 信息的需求

记忆生成需要的 trace 信息分四组：会话与任务边界（跨 trace 稳定的会话标识、span 起止时间，第 4 节）；复现计数与作用域（用户标识，agent 名称与版本，仓库、分支、工作目录，数据库与 schema，第 7 节、《信号与选样调研》第 6 节）；消息与工具调用（消息角色与内容、两侧一致的工具调用标识、外部内容来源，第 4 节）；结果与记忆使用（评估结果及其对象、记忆检索与写入记录，《信号与选样调研》第 5 节、本报告第 8.7 节）。

### 5.2 OTel GenAI 语义约定

依据为 `open-telemetry/semantic-conventions-genai` 仓库 commit `06ec68e`（2026-10-07），schema 为 `gen-ai-dev/1.42.0-dev`，依赖通用约定 v1.44.0。本节所列属性、span、事件与实体的稳定性全部为 development，属性名与要求级别仍可能变化。

表 5-1 OTel GenAI 语义约定中的相关属性与对象

| 属性或对象 | 出现位置（要求级别） | 语义要点 |
|---|---|---|
| `gen_ai.conversation.id` | inference、invoke_agent、invoke_workflow、execute_tool：条件必需 | 会话（session、thread）标识。没有现成标识时不应填写，不应（SHOULD NOT）用新 UUID、trace id 或请求内容哈希代替 |
| `gen_ai.conversation.compacted` | inference：推荐 | 上下文是先前会话的压缩视图；只在确知压缩时置 `true` |
| `gen_ai.agent.name` | invoke_agent、create_agent、plan：条件必需；execute_tool：条件必需 | 应用给出的 agent 名称 |
| `gen_ai.agent.id` / `.version` | 仅 invoke_agent.client 与 create_agent | 托管 agent 资源的稳定标识（如 Bedrock agent ARN），不记录内存实例 ID |
| `gen_ai.main_agent` 实体（`.id` 必需） | 关联 invoke_agent.internal、execute_tool、invoke_workflow、plan | 进程中的顶层 agent，适用于定时任务、云端托管、A2A Agent Card 声明等有稳定标识的 agent；无稳定标识时不发出 |
| `gen_ai.tool.call.id` / `gen_ai.tool.name` / `.type`；`.call.arguments` / `.call.result` | execute_tool：id 推荐、name 必需；参数与结果 opt-in | type 取 function / extension / datastore，表示工具类型（datastore 指数据存储类工具），不标记内容来源 |
| `gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.system_instructions` | inference、invoke_agent、invoke_workflow：opt-in（`gen_ai.system_instructions` 不在 invoke_workflow 上） | 结构化消息，见下文 |
| `gen_ai.skill.*`（name、description、source.uri、resource.name） | execute_tool 的 load_skill、read_skill_resource、command 细化 | 技能加载、资源读取与脚本执行 |
| `gen_ai.memory.*` | `gen_ai.memory.client` span | 见下文 |

**消息结构与配对**。`gen-ai-input-messages.json` 中每条消息含 `role`（system / user / assistant / tool）、`parts` 与可选 `name`。与配对相关的 part 为 `tool_call`（字段 `id`、`name`、`arguments`）与 `tool_call_response`（字段 `id`、`response`）；`tool_call.id` 与对应 execute_tool span 的 `gen_ai.tool.call.id` 取值相同。消息必须按发送给模型的顺序记录。规范示例中，第二次调用的 `gen_ai.input.messages` 包含用户消息、前一次调用产生的工具调用与工具返回，即无状态接口下每次调用记录截至当时的完整历史（累积语义，第 4 节）；使用 `gen_ai.request.previous_response.id` 一类有状态接口时，历史保存在服务端，请求只携带新增输入（据定义推论）。消息 schema 中没有内容来源或可信度字段。

**内容采集**。规范规定三种模式：默认不记录指令、输入与输出；记录在 span 属性上（适合数据量可控、合规允许的环境）；上传到外部存储并在 span 上记录引用（推荐用于生产）。插桩不应默认采集，常见 opt-in 开关为 `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`；记录外部引用的通用方式仍标为 TODO。2026-10-05 的修订（#459）补充：操作失败时只在确有输出（含部分流式输出）时设置 `gen_ai.output.messages`，不得合成内容。

**记忆操作 span**。`gen_ai.memory.client` 覆盖记忆库的创建与删除，以及记录的 search、create、update、upsert、delete。`gen_ai.memory.record.id` 只在操作针对单条记录时条件必需；`gen_ai.memory.query.text` 与 `gen_ai.memory.records`（每项含 `content`、`id`、`score`、`metadata`）为 opt-in。因此 `search_memory` 返回多条记录时，各条 ID 只能从 opt-in 的 `records` 中取得。

**评估结果事件**。`gen_ai.evaluation.result` 是带事件名的独立 log record。属性为 `gen_ai.evaluation.name`（必需）、`.score.value` 与 `.score.label`（适用时条件必需，label 应为低基数）、`.explanation`（推荐）、`gen_ai.response.id`（无 span id 时用于关联）。事件应关联到被评估的 GenAI 操作 span。约定只覆盖单次输出评估，未定义 trace 级、会话级结果，也未定义失败归因（出错 agent、步骤与类型）。

**近期变化**。2026-09-01 至 2026-10-07 之间与本节相关的修改有：execute_tool 允许携带 `gen_ai.conversation.id`（#518，2026-09-16）；新增 `gen_ai.skill.*` 与 execute_tool 的技能细化（#498，2026-09-29）；新增 `gen_ai.main_agent` 实体（#270，2026-09-30）。`gen_ai.conversation.compacted`、`gen_ai.memory.client` 与 `gen_ai.evaluation.result` 在 2026-08-20 的提交中已存在。

### 5.3 通用语义约定

GenAI 约定本身未定义用户、仓库与工作目录，也未引用 `session.id` 与 `user.id`；全仓只在 `gen_ai.conversation.id` 的注释中提到 LangChain 的 `session_id`。这些信息需借用通用约定。下表稳定性中，session、user、enduser、vcs、process 五组读自上游 v1.44.0 的 `model/*/registry.yaml`（与 2026-10-09 的 main 分支一致）；db、deployment、service 读自 Go 生成包 `semconv/v1.41.0`。

表 5-2 通用语义约定中的相关属性

| 属性 | 稳定性 | 含义 |
|---|---|---|
| `session.id` / `session.previous_id` | development | 会话标识；同一用户的上一会话标识 |
| `user.id` / `user.name` / `user.email` / `user.hash` / `user.roles` | development | 用户；`user.hash` 为匿名关联值 |
| `enduser.id` / `enduser.pseudo.id` | development | 终端用户标识与假名标识 |
| `vcs.repository.url.full` / `vcs.repository.name` | release_candidate | 仓库 URL 与名称 |
| `vcs.ref.head.name` / `vcs.ref.head.revision` | release_candidate | 分支或 tag；提交 |
| `process.working_directory` / `process.command_args` | release_candidate | 进程工作目录；命令行参数 |
| `db.system.name` / `db.namespace` / `db.collection.name` / `db.query.summary` / `db.query.text` | stable | DBMS、库名、表、低基数查询摘要、查询文本 |
| `deployment.environment.name` / `service.name` / `service.version` | stable | 环境、服务名称与版本 |

registry 属性不绑定信号类型。`process.*`、`service.*`、`deployment.*` 按惯例写在 resource 上；`db.*` 由数据库客户端插桩写在 DB client span 上，data agent 经工具执行 SQL 时，只有工具内部也有数据库客户端插桩才会出现（二手资料）。

### 5.4 Langfuse 的 OTel 接收映射

依据为 `langfuse/langfuse` commit `d179469`（2026-09-01）的 `packages/shared/src/server/otel/OtelIngestionProcessor.ts` 与 `ObservationTypeMapper.ts`。

表 5-3 Langfuse 的 OTel 接收映射

| 目标字段 | 取值键（按优先级） | 读取范围 |
|---|---|---|
| `session_id` | `langfuse.session.id` → `session.id` → `gen_ai.conversation.id` → 两个 Langfuse metadata 键 → `ai.telemetry.metadata.sessionId` | 仅 span 属性 |
| `user_id` | `langfuse.user.id` → `user.id` → 两个 metadata 键 → `ai.telemetry.metadata.userId` | 仅 span 属性；不读 `enduser.id` |
| `environment` | `langfuse.environment` → `deployment.environment.name` → `deployment.environment`，缺省 `default` | 先 span 后 resource |
| `version` | `langfuse.version` → resource `service.version` | span + resource |
| `metadata` | `{attributes, resourceAttributes, scope}` | 全部保留 |
| 观测类型 | `invoke_agent`、`create_agent` 映射为 AGENT；`execute_tool` 映射为 TOOL；带 `gen_ai.tool.name` 或 `gen_ai.tool.call.id` 时也判为 TOOL | — |

Langfuse 的 `scores` 表支持 trace、observation、session 与 dataset run 级评分，列含 `source`（API / EVAL / ANNOTATION）、`data_type`、`string_value`、`comment`、`session_id`，以及记录评估器自身运行的 `execution_trace_id`（《信号与选样调研》第 5.6 节）。OTel `gen_ai.evaluation.result` 事件不会被映射为 score：非测试 TypeScript 代码中未检索到该名称。

### 5.5 主流 agent 与框架的实际埋点

下表事实读自官方文档或 GitHub 源码：Claude Code 依据 monitoring 文档（文档内最高版本 v2.1.287）；Codex CLI 依据 `openai/codex` commit `2351d9e`（2026-10-09，`codex-rs/`）；其余依据 2026-10-08 至 2026-10-09 的 main 分支源码。

表 5-4 主流 agent 与框架的埋点

| agent / 框架 | 会话键 | 用户键 | 仓库 / 分支 / 目录 | 工具调用配对 | 内容采集默认 | 主要信号 |
|---|---|---|---|---|---|---|
| Claude Code | `session.id`，附在每条记录上；`/clear` 后换新值 | `user.id`（匿名安装 ID 或 IdP subject）、`user.account_uuid`、`user.email` | 仓库需 `OTEL_METRICS_INCLUDE_REPOSITORY`（v2.1.269+）；分支只在 `git commit` 成功且开启工具详情时出现；无工作目录 | 事件带 `tool_use_id`；`tool` span 带 `gen_ai.tool.call.id` | `OTEL_LOG_USER_PROMPTS`、`OTEL_LOG_TOOL_DETAILS`、`OTEL_LOG_TOOL_CONTENT` 等默认关闭 | logs 与 metrics；traces 为 beta，需 `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` |
| Codex CLI | 事件带 `conversation.id`；`turn` span 带 `thread.id` | `user.account_id`、`user.email`，只在 logs | 默认不导出，可经 `OTEL_RESOURCE_ATTRIBUTES` 或 `[otel].span_attributes` 注入；一处 span 带 `cwd`（是否导出未核实） | `codex.tool_decision`（仅 logs）与 `codex.tool_result` 以 `call_id` 配对 | `log_user_prompt=false`；`tool_result` 的 logs 版本默认带参数与输出，截断到 2048 字节 | logs 与 traces 的 exporter 默认均为 none |
| OpenInference 系插桩 | `session.id`，需 `using_session` 写入 Context；LangChain 插桩依次取 metadata 的 `session_id`、`conversation_id`、`thread_id` | `user.id`，需 `using_user` 写入 | 无约定 | 消息内 `tool_call.id` | 默认采集（`OPENINFERENCE_HIDE_*` 默认 False） | traces |
| OpenAI Agents SDK | trace 的 `group_id`（关联同一对话的多个 trace），需经 `RunConfig` 传入；经 OpenInference 导出时不映射，经 Logfire 导出时写入根 span | 无 | 无 | function span 不带 call_id，仅消息内有 | `trace_include_sensitive_data` 默认 True（可由 `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA` 改默认值） | SDK 自有 tracing，不走 OTel |
| LangGraph / LangSmith | LangGraph 把 `configurable.thread_id` 复制进 run metadata；LangSmith OTel 导出写 `langsmith.metadata.thread_id` 等；`langsmith.trace.session_id` 指 LangSmith 项目 | `langsmith.metadata.user_id`，需显式传入 | 无 | tool run 写 `gen_ai.tool.call.id` | 默认采集；`LANGSMITH_HIDE_INPUTS` / `HIDE_OUTPUTS` 为 `true` 时隐藏 | traces（`LANGSMITH_OTEL_ENABLED`） |
| OpenHands SDK（Laminar） | 配置 `LMNR_PROJECT_API_KEY` 或 `OTEL_EXPORTER_OTLP_*` 时启用，每个 span 带 `lmnr.association.properties.session_id` | 同前缀 `user_id`，需传入 | 不写入 | 工具 span metadata 带 `tool_call_id` | 未核实 | traces；`OpenHands/OpenHands` main 分支未见 OTel 埋点 |
| Logfire | 透传框架提供的键 | 透传 | 配置 `code_source` 时在 resource 写 `vcs.repository.url.full`、`vcs.repository.ref.revision`（非当前约定名，当前为 `vcs.ref.head.revision`）、`logfire.code.work_dir` | `gen_ai.tool.call.id` 列为安全键 | OpenAI 集成默认记录请求消息；默认脱敏规则按键名与值匹配 `password`、`secret`、`session`、`cookie` 等模式 | traces |

两条补充：

1. Claude Code 的 `claude_code.tool_decision` 事件带 `decision`（accept / reject）与 `source`（如 `user_reject`、`user_abort`），可直接识别用户拒绝工具调用；`llm_request` span 带 `agent_id` 与 `parent_agent_id`，用于子 agent 关联。这些用户与决策信息主要在 logs 信号中。
2. Logfire 脱敏器（commit `84d0554`，2026-10-08，`scrubbing.py`）的安全键列表包含 `gen_ai.conversation.id`、`gen_ai.input.messages`、`langsmith.metadata.session_id` 等，不包含 `session.id`。按源码逻辑，键名含 `session` 且不在安全键中的属性值会被替换为 `[Scrubbed due to 'session']`（源码阅读结论，未实测）。

### 5.6 记忆生成所需信息的可得性与缺口

表 5-5 记忆生成所需信息的可得性

| 信息 | 约定承载 | 实际埋点 | 缺口 |
|---|---|---|---|
| 会话标识 | `gen_ai.conversation.id`、`session.id` | 最常见；键名有 `session.id`、`conversation.id`、`thread.id`、`group_id`、`lmnr.*.session_id`、`langsmith.metadata.thread_id` | 需维护键名映射；约定规定不应以 trace id 代替；Claude Code 在 `/clear` 后换值，会话与任务边界不一定一致 |
| 用户标识 | `user.id`、`enduser.id` | 编码 agent CLI 自带账号标识；框架类需显式传入 | Codex 用户键只在 logs；Langfuse 不读 `enduser.id` 与 resource |
| 仓库、分支、工作目录 | `vcs.*`、`process.working_directory` | 调研范围内无默认导出；Claude Code 需开关，Logfire 需配置且用旧属性名 | 依赖部署方经 `OTEL_RESOURCE_ATTRIBUTES` 注入 resource，只读 span 属性的接收端会丢失 |
| agent 身份 | `gen_ai.agent.*`、`gen_ai.main_agent` | CLI 类 agent 主要以 `service.name` 区分 | 本地 CLI agent 无托管意义上的稳定 ID，`main_agent` 不适用 |
| 工具调用配对 | `gen_ai.tool.call.id` 与 part `id` | 普遍可得；键名有 `tool_use_id`、`call_id`、`tool_call.id`、`tool_call_id` 等 | 需统一键名；OpenAI Agents SDK 的 function span 不带 ID |
| 消息内容 | `gen_ai.input/output.messages`（opt-in） | OTel 约定、Claude Code、Codex 默认不采集；OpenInference、OpenAI Agents SDK、LangSmith、Logfire 默认采集 | 关闭时无法抽取偏好与纠正；累积语义下消息体随会话增长，易超出后端属性长度上限 |
| 外部内容来源 | 无专门字段；`gen_ai.tool.type` 只区分工具类型 | 依据工具名、`mcp.method.name` 判定 | 需按 agent 维护工具名单 |
| 上下文压缩、记忆使用、技能使用 | `gen_ai.conversation.compacted`；`gen_ai.memory.client`；`gen_ai.skill.*` | 各 agent 是否发出未核实；技能 span 面向 ADK、OpenAI Agents、Strands、Agno 等有专用加载工具的框架 | 多条检索结果的 ID 只在 opt-in 的 `records` 中；以读取 `SKILL.md` 方式加载技能的 agent（如 Claude Code）需从工具参数中的路径识别 |
| 结果信号 | `gen_ai.evaluation.result`（logs，单次输出级） | 平台各自定义评分（如 Langfuse scores） | 无 trace 级、会话级与归因结果的约定；只接 traces 的接收端看不到该事件 |
| 用户拒绝与纠正 | 无专门约定 | Claude Code `tool_decision`、Codex `codex.tool_decision`，均在 logs | 只接 traces 的接收端丢失这类信号 |

### 5.7 结论与倾向

1. OTel GenAI 约定已覆盖记忆生成需要的大部分结构：会话标识、工具调用配对、消息结构、记忆操作、技能使用、上下文压缩与单次输出评估。全部属性为 development 级，2026-09 内仍有三处与本主题直接相关的新增，依赖这些属性的实现需要跟踪版本。
2. 用户、仓库与工作目录不在 GenAI 约定内，通用约定中 `vcs.*` 与 `process.working_directory` 已到 release_candidate，`session.id` 与 `user.id` 仍为 development。实际埋点中作用域键普遍缺省，依赖部署方注入，且常写在 resource 上。
3. 编码 agent CLI（Claude Code、Codex）把用户、工具决策与提示关联放在 logs 信号中，traces 为可选或 beta；框架类插桩以 traces 为主但会话与用户需调用方传入。只采集 traces 或只读 span 属性时，这两类信息会分别丢失。
4. 内容采集的默认值在生态中分为两类：OTel 约定与编码 agent CLI 默认关闭，OpenInference、OpenAI Agents SDK、LangSmith、Logfire 默认开启。依赖用户原话的偏好与纠正抽取（《信号与选样调研》第 6 节）以内容采集开启为前提。
5. 调研倾向：会话、用户与工具调用 ID 宜按多键回退读取并记录命中来源，缺失时保留为空；trace 级与会话级 outcome 在现有约定中缺位，需要平台侧约定补充。

## 6. 生成侧的模型接口

记忆抽取与离线评测中的 LLM judge 常经 OpenAI 兼容的 chat completions 接口调用模型。本节比较常用服务在结构化输出、工具调用、思考模式、可复现与上下文上限上的差异，以及长输入下的引用可靠性、缓存、批量与测试方法。模型名、价格与上限属时效信息，以下为 2026-10-09 读取值。

### 6.1 请求参数支持

表 6-1 OpenAI 兼容服务的请求参数支持

| 服务 | `json_object` | `json_schema` strict | 工具调用与强制调用 | `seed` | `logprobs` |
|---|---|---|---|---|---|
| DeepSeek 官方 | 支持；prompt 须含 "json"；已知偶发返回空 content | `response_format` 只有 `text` / `json_object`；strict 只用于 function calling（Beta 端点） | 支持；思考模式下 `tool_choice` 为 `required` 或指定函数时返回 400 | API 参考未列出 | 支持，`top_logprobs` ≤ 20 |
| 阿里云百炼（兼容模式） | 支持；消息中须含 "JSON" | 仅 Qwen3.7-Plus / Flash / Max、Qwen3.8-Max / Flash；新加坡地域暂不支持；Qwen3.5 / 3.6 只有 `json_object` | 支持；Qwen 不支持 `required`，思考模式不能强制指定工具 | 支持，尽力一致 | 部分快照；`top_logprobs` ≤ 5；思维链不返回 logprobs |
| MiniMax（`api.minimax.io/v1`） | OpenAI 兼容接口参数表无 `response_format` | 同左；原生接口的 `response_format` 只支持已不在当前枚举中的 MiniMax-Text-01 | 支持 `tools`，不支持旧 `function_call` | 未提及 | 未提及 |
| vLLM 自部署 | 支持 | 支持 `response_format` json_schema 与 `structured_outputs`（json / regex / choice / grammar / structural_tag）；后端默认 `auto`，含 xgrammar、guidance；`guided_*` 字段在 v0.12.0 移除 | 需 `--enable-auto-tool-choice --tool-call-parser` | 支持；默认不保证可复现，在线模式需开启 batch invariance（批次不变性，输出不随同批其他请求变化）且限同硬件同版本 | 支持（二手资料） |
| SGLang 自部署 | 支持 | 支持；后端 xgrammar（默认）、outlines、llguidance；单请求只能给一种约束 | 需 `--tool-call-parser` | 支持（二手资料） | 支持（二手资料） |
| OpenRouter | 视上游 | 支持 `strict: true`，按 endpoint 判定；`provider.require_parameters: true` 只路由到支持该参数的上游；非流式 json_schema 可开 Response Healing 插件修复无效 JSON | 视上游 | 视上游 | 视上游 |

在上述服务中，`json_object` 加 prompt 内给出 schema 是唯一普遍可用的公共子集。MiniMax 的 OpenAI 兼容接口在文档层面不支持 `response_format`，结构化输出只能依靠 prompt 约束加本地校验；该接口忽略 `presence_penalty` 等参数，`n` 只能为 1。以工具调用承载结构化输出依赖强制调用，DeepSeek 与 Qwen 的思考模式都不支持强制调用。`seed` 在 OpenAI 与百炼均为尽力而为，OpenAI cookbook 说明相同 seed、参数与 `system_fingerprint` 时输出"大多相同"。

### 6.2 上下文窗口与最大输出

表 6-2 上下文窗口与最大输出

| 模型 | 上下文 | 最大输出 | 来源 |
|---|---|---|---|
| DeepSeek `deepseek-flash`（V4.1-Flash） | 1M | 384K（`max_tokens` 上限 393,216）；默认非思考 8K、思考 64K | 官方定价页；`max_tokens` 上限与默认值见 API 参考 |
| DeepSeek `deepseek-v4-pro` | 1M | 同上；账户并发上限 500（Flash 为 2,500） | 官方定价页 |
| qwen3.6-plus（百炼） | 1,000,000；最大输入 991,808（思考模式 983,616） | 65,536；思维链 81,920 | 百炼官方模型页 |
| qwen3.6-max-preview（百炼） | 262,144；最大输入 245,760（思考模式 229,376） | 65,536；思维链 131,072；内置工具不支持 | 百炼官方模型页 |
| qwen3.5-plus（百炼） | 同 qwen3.6-plus | 同 qwen3.6-plus | 百炼官方模型页 |
| Qwen3.5-397B-A17B（开源权重） | 原生 262,144，YaRN（RoPE 位置编码的长度外推方法）可扩到约 1,010,000（静态缩放损害短文本） | 模型卡建议 32,768，复杂任务 81,920；实际由部署的 `max_model_len` 决定 | 模型卡 |
| MiniMax-M2.5 | 204,800 | `max_completion_tokens` 推荐 65,536，上限 204,800 | MiniMax 官方文档 |
| MiniMax-M2.5（百炼托管） | 192K | — | 百炼页面；与官方不同，按实际端点取值 |

单条编码 agent trace 的原始最终上下文约 5–7 万 token（第 9.3 节），在上述模型的窗口内。约束主要来自输出侧：思考 token 计入输出预算。百炼推荐思考模型使用 `max_completion_tokens`；MiniMax 的 `max_tokens` 计入思考 token，取值过小会得到 `finish_reason: "length"` 且 content 为空。

### 6.3 思考模式与结构化输出

表 6-3 思考模式对结构化输出的影响

| 服务 | 默认 | 思维链位置 | 对 JSON 抽取的影响 |
|---|---|---|---|
| DeepSeek | 默认开启；`extra_body={"thinking": {"type": "disabled"}}` 关闭 | `reasoning_content` | 思考模式下 temperature 无效、`top_p` 下限 0.95；带工具的多轮须完整回传 `reasoning_content`，否则 400；思考模式下 `json_object` 的行为文档未说明 |
| 百炼 Qwen | 混合思考由 `enable_thinking` 控制；开源 Qwen3.5 默认思考 | `reasoning_content` | 思考模式设 `json_object` 不报错，但可能返回非严格 JSON；官方建议失败后交给非思考模型修复 |
| MiniMax M2.x | 始终思考 | 默认在 `content` 中以 `<think>` 包裹；`reasoning_split: True` 时移到单独字段 | 不分离时直接解析 content 失败 |
| vLLM / SGLang | 由聊天模板决定 | 需 `--reasoning-parser` 拆出 | 约束解码只作用于正文；vLLM 对 Qwen3 Coder 在推理段未被解析时禁用结构化输出 |

格式约束本身会影响推理质量：Tam 等（2408.02442）报告格式限制下推理能力明显下降，约束越严下降越多（据摘要）。百炼文档建议思考模式下 JSON 输出失败时交给非思考模型修复；MiniMax 不分离思维链时 content 以 `<think>` 段开头，解析前需剥离（表 6-3）。

### 6.4 结构化输出工具

表 6-4 结构化输出工具

| 工具（PyPI 版本） | 机制 | 适用条件 |
|---|---|---|
| instructor 1.17.0 | 包装 openai 客户端；模式含 TOOLS（默认）、JSON、MD_JSON；Pydantic 校验失败时带错误信息重问，可设 `token_budget`；文档建议客户端设 `max_retries=0`，避免与 SDK 传输重试相乘 | 任意兼容端点可用 JSON 模式；默认 TOOLS 模式在思考模型上受强制调用限制；传递依赖十余个 |
| outlines 1.3.3 | 本地 logits 约束（outlines_core）；对远程服务经 `from_openai` 接入，能力取决于服务端 | 约束解码只在本地推理或服务端支持时生效 |
| guidance 0.3.1 | llguidance 语法引擎 | 主要面向本地模型 |
| xgrammar 0.2.8 | vLLM / SGLang 默认语法后端 | 只在推理服务内部使用 |

服务端约束解码（vLLM、SGLang、strict 模式）能消除语法错误，但不同服务覆盖不全；DeepSeek 的 strict 模式不支持 `minLength`、`maxItems` 等关键字。

### 6.5 长上下文与引用可靠性

表 6-5 长上下文与引用可靠性的证据

| 证据 | 内容 | 级别 |
|---|---|---|
| Lost in the Middle（2307.03172） | 相关信息位于输入开头或结尾时表现最好，位于中段时显著下降，长上下文模型同样如此 | 据摘要 |
| Chroma Context Rot（2025-07-14） | 18 个模型在全部实验中随输入变长性能下降；问题与目标信息语义相似度低、干扰项多时下降更快；LongMemEval 全量输入（约 113k token）显著差于聚焦版（约 300 token） | 原文 |
| Anthropic 长上下文提示指南 | 20k token 以上输入时长文档置顶、问题与指令置底，测试中质量最多提升 30%；先摘录相关引文再作答 | 官方文档 |
| ALCE（2305.14627） | ELI5 上最好的模型约 50% 的回答缺少完整引用支撑 | 二手资料 |
| LongCite（2409.02897） | 长上下文带引用问答仍有较大提升空间；专门训练的 8B / 9B 模型引用质量超过 GPT-4o | 据摘要 |
| CiteFix（2504.15629） | 生成式搜索引擎引用准确率约 74%；对生成引用做后处理校正，整体准确率相对提升 15.46% | 据摘要 |
| Citation Failure（2510.20303）、GhostCite（2602.06718） | 区分"答案对、引用不全"与"答案无依据"；13 个模型文献引用编造率 14%–95% | 二手资料 |

调研范围内未找到直接度量"引用上下文中不存在的段落编号"比例的研究。这类错误可由程序完全检测。

### 6.6 缓存、批量与速率限制

**可复现**。服务端 `seed` 与温度 0 不足以保证可复现（DeepSeek 未提供 seed 且思考模式下 temperature 无效；vLLM 默认不保证）。

表 6-6 前缀缓存、计费与批量接口

| 服务 | 前缀缓存 | 计费 | 批量接口 |
|---|---|---|---|
| DeepSeek | 默认对所有用户开启；须完整命中缓存前缀单元，尽力而为，数小时至数天清除 | 命中约为未命中的 2%–3.3%；错峰价格为高峰的一半，高峰为 UTC 工作日（不含中国法定节假日）01:00–04:00 与 06:00–10:00（2026-08-16 起） | 官方无 batch；Files API 只接受 `purpose=user_data` 图片上传 |
| 百炼 | 隐式缓存自动且不可关闭；显式缓存 `cache_control: {"type":"ephemeral"}`，5 分钟有效、命中重置，最小 1,024 token | 隐式命中通常为 20%；显式创建 125%、命中 10% | OpenAI 兼容 Batch，价格为实时的 50%；单文件 ≤ 50,000 请求、≤ 500 MB；`completion_window` 24–336 小时；同一文件须同模型同思考模式；列出的 Qwen 3.x 批量单请求上下文上限 256K |
| vLLM | 自部署前缀缓存 | — | 离线 `run_batch`，输入为 OpenAI batch 格式（二手资料） |

同一条 trace 上的多次调用（多类记忆抽取、格式重试、多个 judge 维度）共享"静态指令 + trace"前缀时，后续调用的输入成本可降到原来的约 2%–20%；这要求随调用变化的问题放在 trace 之后，同一 trace 的调用在短时间内连续发出（推论）。该布局与长上下文指南中"问题置底"的建议一致。

**速率限制**。DeepSeek 按账户限并发（Flash 2,500、Pro 500），超出返回 429；排队期间非流式请求持续返回空行、流式返回 `: keep-alive`，10 分钟未开始推理则断开。openai SDK 默认对 408 / 409 / 429 / ≥500 与连接错误重试 2 次、指数退避。格式重试与传输重试分开计数时，总尝试次数上限为二者乘积（instructor 文档）。

### 6.7 测试方法

表 6-7 测试工具现状

| 工具 | 现状 |
|---|---|
| openai-python 3.x | 3.0.0（2026-08-12）起默认 HTTP 客户端换为 `httpx2`，不再自动安装 `httpx`；官方迁移文档要求 mock 拦截 httpx2；当前 3.26.1 |
| respx 0.23.1 | 仅依赖 `httpx`，无法拦截 openai 3.x 默认客户端（依赖关系核实，行为为推论） |
| vcrpy 8.3.0 | `vcr/patch.py` 已补丁 `httpx2` 传输类；默认 `match_on` 为 method / scheme / host / port / path / query，不含 body |
| pytest-recording 0.14.0 | 默认 `record_mode=none`；`filter_headers` 可过滤 `authorization`；`--block-network` 全局禁网 |

chat completions 请求都是 POST 到同一路径，按 vcrpy 默认规则匹配会互相混淆，录制回放需把 `body` 加入 `match_on` 或按顺序回放。

### 6.8 结论与倾向

1. OpenAI 兼容服务在结构化输出上的公共子集只有 `json_object` 加 prompt 内 schema；strict schema、强制工具调用、seed、logprobs 的支持在服务之间与思考模式之间都不一致。生成侧可移植的做法是把正确性保证放在客户端校验与重试上，服务端约束解码作为可选增强。
2. 1M 级上下文窗口覆盖单条 trace，但长输入下的位置效应与干扰项效应在多项研究中一致出现，引用错误有可程序检测的部分。调研倾向是保留输入压缩预算，引用采用可校验编号，并把校验失败计入指标。
3. 成本侧，DeepSeek 依赖错峰与默认前缀缓存，百炼提供半价批量与显式缓存；两者都使"同一 trace 的多次调用共享前缀"成为主要的降本手段（推论）。服务端不保证可复现。
4. 调研倾向（推论）：客户端依次处理 `finish_reason == "length"`、空 content、思维链与代码围栏剥离、JSON 解析与模型校验，重试耗尽时返回带失败状态的结果，与"无可抽取内容"区分；引用由程序校验编号是否存在并计数无效引用；以规范化请求的哈希为键保存原始响应，用于复跑与评测；测试以纯函数与按预置响应返回的假客户端为主，录制回放只覆盖服务适配器。

## 7. 存储、溯源与生命周期

本节讨论生成后的记忆如何存放、如何追溯到源 trace、以及如何更新、失效、遗忘与删除。存储形态决定检索与审计能力，溯源决定校验、门控与归因能否实现，生命周期机制决定过时与有害记忆能否被及时移出上下文。

### 7.1 存储形态谱系

表 7-1 记忆存储形态谱系

| 形态 | 代表系统 | 检索方式 | 可审计与可编辑 | 更新成本 | 适合内容 |
|---|---|---|---|---|---|
| Markdown 文件 + 索引 | Codex（`memory_summary.md`、`MEMORY.md`、`rollout_summaries/`，目录为 git 仓库）；Claude Code（`MEMORY.md` 索引 + 带 frontmatter 的主题文件）；letta-code（MemFS + git） | 全量或按索引注入、grep | 最高，人可直接读改，git 留版本 | 低 | 少量高价值事实、偏好、经验摘要 |
| 上下文 playbook | ACE（2510.04618）：生成、反思、整理三角色，以增量 delta 更新条目列表 | 整体注入 | 高 | 低，增量 | 策略条目、领域经验 |
| 关系表条目 | Copilot Memory（subject / fact / citations / reason）；LangGraph PostgresStore（`(prefix, key) → value jsonb`） | 键与前缀、过滤，可叠加向量 | 高，可加约束 | 低 | 事实、偏好、带引用的经验 |
| 向量条目 | Mem0 pgvector；Letta archival passages | ANN + 元数据过滤 | 中，条目间关联弱 | 低 | 大量情景片段、经验案例 |
| 图与时态图 | Zep / Graphiti（episode、语义实体、社区三层子图，边带双时态）；A-MEM 笔记互链 | 图遍历 + 向量 + 关键词 | 中高，事实级可追溯 | 中高，实体消解与矛盾检测需 LLM | 实体关系、随时间变化的事实 |
| 技能与代码 | Voyager 可执行技能库；AWM 从轨迹归纳 workflow；NVIDIA `helper.py` | 按描述检索后注入或调用 | 高，可测试 | 中，需验证可执行 | 程序性记忆 |
| 参数化 | Memory Layers at Scale（2412.09764）；Sparse Memory Finetuning（2510.15103，NQ 上遗忘 11%，全量微调 89%、LoRA 71%，据摘要）；MemOS parametric memory | 无显式检索 | 低，难以定位与删除 | 高，需训练 | 稳定、高频知识 |
| 激活 | MemOS activation memory（KV cache、隐藏状态） | 推理时复用 | 低 | — | 会话内加速 |

要点：

1. 生产系统（Copilot、Codex、Claude Code、LangGraph、Mem0、Letta）全部采用文本可读的非参数化形态。参数化形态处于研究阶段，与溯源、删除需求冲突。
2. MemOS（2507.03724）把明文、激活、参数三种形态统一为 MemCube，并定义迁移路径：高频明文 → 激活，稳定知识 → 参数，冷参数 → 卸载回明文。形态可以作为生命周期中的阶段属性。
3. Codex 以 git 管理的 Markdown 目录为记忆本体，SQLite 承载流水线状态与阶段一输出（每会话抽取结果、作业队列、使用计数）。
4. 图形态的检索收益在公开数字上有限（Mem0 graph 变体在 LoCoMo 上约 +2%），代价是实体消解与矛盾检测的 LLM 调用；时态图的主要价值在双时态与可追溯性。Mem0 2.x 已把外部图存储从开源版移到 Platform，开源版保留实体链接作为混合检索的一路信号。

### 7.2 关系库上的已有 schema 范例

表 7-2 关系库上的记忆 schema 范例

| 框架 | 结构 | 溯源字段 | 生命周期字段 |
|---|---|---|---|
| LangGraph PostgresStore | `store(prefix, key, value jsonb, created_at, updated_at, expires_at, ttl_minutes, PK(prefix,key))`；`store_vectors(prefix, key, field_name, embedding)` 外键级联删除；HNSW 或 IVFFlat | 无，由 value 自定 | `expires_at`、`ttl_minutes`，部分索引 `WHERE expires_at IS NOT NULL` |
| Mem0 pgvector（2.2.1） | `<collection>(id uuid PK, vector vector(d), payload jsonb)`；payload 文本 GIN 全文索引；HNSW 或 DiskANN | 全部在 payload；不保存源消息 id | 无 |
| Mem0 history | `history(id, memory_id, old_memory, new_memory, event, created_at, updated_at, is_deleted, actor_id, role)`；Python SDK 只支持 SQLite，TS SDK 另有 Supabase | actor_id、role | event、is_deleted |
| Letta（archive 分支 0.16.8，V1 服务端已退役） | `archival_passages(id, text, embedding, embedding_config, metadata_, tags, archive_id)`，embedding 维度上限 4096；`source_passages(source_id, file_id, file_name, …)`；`block_history(sequence_number, actor_type, actor_id)`；`messages(tool_calls, tool_returns, step_id, run_id, …)` | source_id、file_id | block_history 版本序号（生产代码中无调用方，不自动写历史） |
| Codex（SQLite，源码迁移脚本） | `stage1_outputs(thread_id PK, source_updated_at, raw_memory, rollout_summary, rollout_slug, generated_at, usage_count, last_usage, selected_for_phase2, …)`；`jobs(kind, job_key, status, ownership_token, lease_until, retry_remaining, input_watermark, last_success_watermark, …)` | thread_id 对应会话原文 | usage_count、last_usage、输入水位 |
| Copilot Memory | 记录字段 subject、fact、citations、reason | citations 指向文件与行号，用户偏好可引用原话 | 28 天未使用自动删除 |
| claude-mem Hosted PG | `agent_events`（idempotency_key UNIQUE、payload JSONB）→ `observation_generation_jobs`（状态、尝试次数、锁）→ `observations`（content_search TSVECTOR）+ `observation_sources(observation_id, agent_event_id, generation_job_id)` 多对多；`observation_generation_job_events` 审计 | 事件级多对多来源表 | 作业状态；embedding 存 JSONB，未用 pgvector |
| altk-evolve | 每 namespace 一张 `ns_<id>(id, type, content, created_at, embedding vector, metadata jsonb)`；guideline 字段（category、trigger、support、evidence、sources[]）全部在 metadata | `sources[]` 含 conversation_id、task_id、source_span_id、status（supporting / superseded）；合并后的条目丢失全部来源（源码缺陷） | last_accessed；按年龄、未使用天数、源删除的淘汰规则 |
| MIRIX | `skill_experience(session_id, experience_type, content, credibility, evidence, status, consumed_by, influenced_skill_ids)`；`procedural_memory(name, instructions, triggers, version)` | 会话、逐字证据、经验 → 技能血缘；其余六类记忆不记录来源 | status、version |
| Honcho | `documents(level: explicit / deductive / inductive / contradiction, times_derived, embedding, observer, observed)`；`document_sources(derived_id, source_id)` 推理树 | `internal_metadata.message_ids`、推理链 | times_derived |

观察：主流框架采用"一张主表 + JSONB 载荷 + 向量列或向量表"的形态，溯源与生命周期字段多数缺失或放在 JSONB 中；以独立关系表表达记忆与来源多对多关系的有 claude-mem Hosted PG（`observation_sources`）与 Supermemory（`MemoryDocumentSource`），Honcho 用 `document_sources` 记录推理树；溯源细化到 span 的只有 altk-evolve 的单个 span_id 字段，且在合并后丢失。Graphiti 官方后端为 Neo4j、FalkorDB、Neptune，无 PostgreSQL 驱动。

### 7.3 PostgreSQL 生态中的相关特性

《生成方法调研》第 5 节考察的开源实现中，使用 PostgreSQL 的组件依赖三类能力：pgvector 承载向量检索（Mem0、MIRIX、Honcho、altk-evolve、Memobase）；内置 `tsvector` 与 GIN 索引承载关键词检索，与 pgvector 组合为混合检索（Mem0、MIRIX、claude-mem）；Apache AGE 提供 openCypher 图查询（MemOS polardb 后端、Mem0 1.x 图记忆）。Cognee 的 PG 图后端为 demo，Graphiti 无 PostgreSQL 驱动（《生成方法调研》第 5.4 节）。

### 7.4 溯源研究与实现

表中 MEMOREPAIR、Agentic Unlearning（SBU）、Execution-State Unlearning、Authorization Before Context、AuthMem-Bench、TMA-NM 与 From Agent Traces to Trust 的机制与数字均据摘要。

表 7-3 溯源研究与实现

| 工作或系统 | arXiv / 来源 | 溯源粒度 | 机制 | 使用前校验 |
|---|---|---|---|---|
| Zep / Graphiti | 2501.13956 | 事实（边）→ episode | episode 节点保存原始输入；边字段 `episodes[]`；论文声明语义产物可回溯来源，实验未评估 | 否 |
| Copilot Memory | 官方文档与博客 | 事实 → 代码位置；偏好 → 用户原话 | `store_memory` 写入带 citations；使用前按当前分支读取被引位置，矛盾或位置失效时写入修正版，校验通过并使用后刷新时间戳 | 是 |
| Codex | 源码 | 记忆条目 → rollout 摘要 → 会话原文 | `MEMORY.md` 每个任务列出 `rollout_summary_files`（thread_id、rollout_path、updated_at）；回答末尾输出 `<oai-mem-citation>`，解析后对相应行 `usage_count+1` | 否，用于展示与使用统计 |
| MemOS MemCube | 2507.03724 | 条目级元数据 | origin signature（推理抽取、用户输入、外部检索、微调）、版本链与回滚、访问控制、TTL、敏感标签 | 否 |
| TierMem | 2602.17913 | 摘要 → 不可变原始日志 | 摘要证据不足时升级到原始日志，核实后写成新摘要并链接来源；LoCoMo 0.851（纯原始日志 0.873），输入 token −54.1% | 按需回源 |
| Eywa | 2605.30771 | 事实 → 不可变证据 | "evidence before belief"：先存原始证据，再派生规范事实并按来源支持度校验；检索路径不调用 LLM | 写入时 |
| MemLineage | 2605.14421 | 条目 → 签名日志 + 派生图 | Merkle 日志 + 每主体 Ed25519 签名；加权派生图记录哪些被检索条目影响了新记忆；敏感动作追溯到外部来源时拒绝；三类投毒 ASR 降为 0，单次开销 <1 ms | 动作门控 |
| MemQ | 2605.08374 | 记忆 → 生成时检索到的记忆 | 在 provenance DAG 上做 TD(λ) 信用回传，按 (γλ)^d 衰减 | 否，用于学习价值 |
| MEMOREPAIR | 2605.07242 | 源数据 → 派生产物（summary、embedding、skill） | 源删除、更正或失效后撤下受影响后代，用剩余支撑重建，校验后重新发布；修复集选择归约为一次 s-t min-cut；前提为完整的影响溯源 | — |
| Agentic Unlearning（SBU） | 2602.17692 | 原始 episodic 数据 → summary、reflection、KG 节点 | 建依赖闭包并引用计数；仅由被删数据支撑的产物删除，有其他来源的共享产物标为逻辑失效 | — |
| Execution-State Unlearning | 2609.04875 | 记忆 → 执行状态 | provenance 引导的选择性重放与全量重置等效，重算 token 至多减少到 1/9 | — |
| Authorization Before Context | 2608.17148 | 记忆 → 写入时在场受众 | 仅当当前所有 viewer 属于原受众时进入上下文；渠道不明按公开处理；投毒记忆无法扩大受众 | 检索时 |
| AuthMem-Bench | 2608.01679 | 声明 → 来源权限 | 整合保留声明却丢失来源权限约束（authority collapse），49 个配置中 48 个出现；无 authority 元数据时越权动作率均值 50.3%，持久化 authority 标签后 16.9% → 0.0% | 执行时 |
| TMA-NM | 2606.24322 | 记忆 → 来源权限 | 指出 agent 自身总结、可信工具回显、伪造佐证三条把低权限内容提升为高权限的通道（洗白，laundering）；只看内容或只看 lineage 的防御均不可靠，写入时须绑定来源，权限提升须抗 Sybil 佐证（Sybil 指同一攻击者伪造多个身份提供佐证）；经这三条通道的洗白攻击对现有防御的成功率最高 68%，该方法在 8 个模型上为 0% | 写入与提升时 |
| From Agent Traces to Trust | 2606.04990 | 综述 | 把记忆视为带来源、变换、修订、有效条件与后续影响的制品（provenance-bearing memory） | — |

### 7.5 溯源的三种用途

表 7-4 溯源的三种用途

| 用途 | 解决的问题与证据 | 所需字段 |
|---|---|---|
| 回源校验 | 记忆过时与冲突：Copilot 以代码引用即时校验，对抗测试中能检测并修正指向无关代码的假记忆；摘要丢失细节：TierMem 回到原始日志补全；写入时丢失来源：PASB 中被写入的用户主张 33.1% 丢失来源 | 可解析的来源定位符（trace、span、片段区间）与来源内容 |
| 安全门控 | 写入路径投毒：AgentPoison 投毒率 <0.1% 时平均 ASR >80%（2407.12784），MINJA 只用普通查询即可诱导写入恶意记录（2503.03704）；整合中的权限丢失：AuthMem-Bench、TMA-NM；跨受众泄漏：Authorization Before Context | 来源可信度与权限类别、owner 与受众、派生链 |
| 价值归因 | 判断哪些记忆有用：Codex 以 `rollout_ids` 统计哪些历史会话被使用；MemQ 在派生 DAG 上做信用分配；删除与修复：MEMOREPAIR、SBU 依赖完整派生图 | 使用记录、记忆 → 记忆与记忆 → 来源的派生边 |

trace 场景与代码库场景的差别在于：代码库中来源可被廉价重读，Copilot 的校验对象是"被引代码是否仍成立"；trace 中的 span 不可变，校验对象变为"派生结论是否仍被后续 trace 支持"。

### 7.6 PROV 与 OpenLineage 词汇

W3C PROV-O（2013 年 W3C 推荐标准）以 Entity、Activity、Agent 为核心类，关系包括 `wasGeneratedBy`、`used`、`wasDerivedFrom`、`wasAttributedTo`，扩展词汇有 `wasRevisionOf`、`wasQuotedFrom`、`wasInvalidatedBy`、`generatedAtTime`、`invalidatedAtTime`，qualified 模式可为关系附加属性。OpenLineage 以 Job、Run（runId 建议 UUIDv7）、Dataset 为对象，RunEvent 记录 START 到 COMPLETE 的状态，用 facet 扩展父运行、源码位置、数据质量等信息。

两套词汇与记忆生成的对应关系：

表 7-5 记忆生成概念与 PROV、OpenLineage 的对应

| 记忆生成中的概念 | PROV | OpenLineage |
|---|---|---|
| 记忆条目 | Entity | Dataset（输出） |
| 源 trace 或 span | Entity，经 `wasDerivedFrom` / `wasQuotedFrom` 关联 | Dataset（输入） |
| 一次生成或整合作业 | Activity（`wasGeneratedBy`） | Run |
| 抽取模型与提示版本 | Agent 或 Plan | Job facet |
| 版本链 | `wasRevisionOf` | — |
| 失效 | `wasInvalidatedBy`、`invalidatedAtTime` | — |

目前没有面向 agent 记忆的 PROV 或 OpenLineage 扩展规范。

### 7.7 生命周期机制

#### 7.7.1 更新操作语义

表 7-6 更新操作语义

| 语义 | 实现 |
|---|---|
| LLM 决策增删改 | Mem0 1.x（ADD / UPDATE / DELETE / NONE，论文称 NOOP）、Agno、LangMem（insert / patch / remove）、CrewAI（keep / update / delete，相似度 ≥0.85 才调 LLM）、altk-evolve（向量召回 10 个候选后 LLM 决策，程序校验返回 ID 合法） |
| 只追加 | Mem0 2.x（2026-04 起开源版只做 ADD，按哈希去重）、claude-mem（只计再确认次数）、Cognee lessons |
| 失效而不删除 | Graphiti（旧边 `invalid_at` 设为新边 `valid_at`、`expired_at` 设为当前时间，能处理乱序到达，边从不物理删除）、Supermemory（`isLatest` 版本链）、LongMemory（valid 与 recorded 两条时间线）、TRACE（Supersede 后加 `_archived_` 前缀归档） |
| delta 加计数 | ACE（ADD / UPDATE / TAG / REMOVE 软删除，helpful / harmful / neutral 计数）、MIRIX（编辑预算，单次字符差超过 800 或变化率 ≥0.4 时拒绝） |
| 只增不删的整合 | ReMe HEAD（CREATE / CORROBORATE / REFINE / CORRECT，冲突以内联标注保留） |

#### 7.7.2 使用驱动与效用驱动淘汰

表 7-7 使用驱动与效用驱动淘汰

| 机制 | 代表 | 规则 |
|---|---|---|
| 未使用过期 | Copilot | 28 天未使用自动删除，校验通过并使用后重置计时 |
| 使用计数参与选择 | Codex | 第二阶段按 `usage_count`、`last_usage` 排序取前 N（默认 256）；超出 `max_unused_days` 且未入选的行分批删除 |
| TTL | LangGraph PostgresStore | `expires_at` 到期清理 |
| 规则表淘汰 | altk-evolve | 最大年龄、最大未使用天数、源删除；动作为标记或删除 |
| 效用统计 | ReMe 0.2（freq / utility）、MemOS 插件（增益或 Beta 后验）、ACE 计数 | 按效用删除或停用（ReMe 的实现失效，ACE 的计数不参与排序与淘汰） |
| 衰减 | claude-mem（ACT-R 幂律，默认关闭）、MemoryBank（Ebbinghaus 曲线）、FadeMem（2601.18642） | FadeMem 按重要度自适应的拉伸指数衰减，冲突分 compatible / contradictory / subsumes / subsumed；LoCoMo 上存储削减 45%，多跳 F1 与 Mem0 持平略高 |
| 热度晋升 | MemoryOS | 热度达到阈值晋升到长期层 |

#### 7.7.3 差异驱动遗忘

Codex 第二阶段以记忆目录相对上次成功基线的 git diff 为路由：新增与修改进入摄入队列，删除进入遗忘队列，被删除的 rollout summary 只删除仅由其支撑的记忆；污染标记使相关会话退出输入并在下次整合中被遗忘（机制细节见《生成方法调研》第 6.2 节）。这一机制把"来源集合的变化"直接转化为记忆的增删。

#### 7.7.4 冲突撤销与过时约束

第 7.7.4–7.7.7 节所列工作（TEPA、2608.25553、STALE、2609.08258、2609.04875、2606.15903、MEMOREPAIR、SBU、MemLeak、SPORE、GateMem）的机制与数字均据摘要。

表 7-8 冲突撤销与过时约束

| 工作 | arXiv | 结果 |
|---|---|---|
| TEPA | 2608.07429 | 观察按 key 存为先例，同 key 出现矛盾证据时撤销旧条目；完全反转场景中 append-only 与 last-write-wins 为 0.210，低于无记忆 0.309，TEPA 为 0.950 |
| When Stale Constraints Go Unchecked | 2608.25553 | 16 个模型、核查预算 2 条记录；模型只在约五分之一的 episode 中核查约束出处，约束被取代后 74.7%–77.3% 的决策沿用旧约束；把一个核查名额强制分配给关键路径，使沿用旧约束的决策比例减少 61.3–74.0 pp，该策略利用了实验者已知的关键路径信息，且只度量可恢复的风险；一句通用规则在"约束限制诱人动作"的决策上恢复 89.3 pp |
| STALE | 2605.06527 | 400 个冲突场景、1,200 个查询，最好模型总体 55.2% |

#### 7.7.5 撤销须在检索层生效

Revoked but Still Authoritative（2609.08258）测试 5 个记忆系统、9 个策略场景、9 个模型：没有系统默认执行撤销，被标记为 invalid 的事实仍被检索且排在替代事实之前，导致不安全动作；修复方式是在 agent 与记忆后端之间加 guard 层。Execution-State Unlearning（2609.04875）表明只删除记忆记录后泄漏不变，指令式遗忘在诱导下 Leak@probes（探测提问中出现泄漏的比例）为 1.00。Control-Plane Placement Shapes Forgetting（2606.15903）比较 13 种配置：确定性原语在标识符混淆下的删除成功率只有 5%、跨语言为 0%；写入时调用 LLM 规范化 100% 但按意图删除 0%；在变更时调用 LLM 钩子按意图删除 78%–85%，每条 2.3 s，确定性方法为 64–191 ms。

#### 7.7.6 级联删除

源数据删除后派生记忆仍可见的问题称为级联更新问题。MEMOREPAIR 报告失效记忆暴露率 69.8%–94.3%，修复后为 0%，相对全量重修恢复 91.1%–94.3% 的有效后继，成本为全量的 0.57–0.76。SBU 用依赖闭包与引用计数区分"只由被删数据支撑"与"另有来源"的产物。altk-evolve 在删除 trajectory 时只级联删除"恰好一个 supporting 来源且溯源完整"的条目，而其合并路径丢失来源，使级联对合并结果失效。产品侧的删除语义见《生成方法调研》第 6.4 节。

#### 7.7.7 跨用户泄漏与 ownership 过滤

表 7-9 跨用户泄漏

| 工作 | arXiv | 结果 |
|---|---|---|
| MemLeak | 2610.04195 | 多租户共用向量库时，团队共享检索池在正常使用中泄漏 70%–100%，对抗记忆进入 top-k 90%–100%；只有检索后硬性 ownership 过滤把回答污染度从 5.00/5 降到 1.00/5，每查询约 +1.4 ms |
| SPORE | 2607.23444 | 恶意工具诱导 agent 把长期记忆放进工具参数，按用户隔离仍外泄：不限触发 80.0%，20 次触发 47.0% |
| GateMem | 2606.18829 | 多主体共享记忆同时评效用、访问控制与删除后遗忘，没有方法三者兼顾 |

### 7.8 记忆质量评估

表 7-10 记忆质量评估方法

| 方法 | 来源 | 说明 |
|---|---|---|
| 操作级幻觉评测 | HaluMem（2511.03506） | 拆为抽取、更新、问答三个任务；抽取与更新阶段产生并累积幻觉，传导到问答（据摘要） |
| 来源支持度校验 | Eywa、Copilot、ALCE（2305.14627） | 写入时或使用时判定记忆是否被来源支持；ALCE 从流畅度、正确性、引用质量评测 |
| 线上 A/B | Copilot | coding agent PR 合并率 83%→90%，code review 精确率 +3%、召回 +4%（官方博客自报） |
| 使用与信用 | Codex 使用计数、MemQ Q 值 | 用于排序与淘汰 |
| 质量分与迁移效果的关系 | Epistemics of Agent Memory（2609.33013） | 质量分与真实迁移不相关（ρ=−0.24，n=12，据摘要） |
| 治理风险清单 | SSGM（2603.11768） | 写入投毒、整合时语义漂移、检索时冲突与幻觉三个失效点（二手资料） |

可归纳的评测层级：抽取（记忆点精确率、召回率、幻觉率）、更新（更新正确率、冲突检出率、过时残留率）、溯源（引用精确率与来源可解析率）、检索（recall@k，熟悉与新情境分开）、端到端（有无记忆的成功率、步数、token）、安全（投毒 ASR、越权与泄漏率）、生命周期（存储增长率、陈旧率、使用率分布）。第 8.5 节给出完整的指标体系。

### 7.9 结论与倾向

1. 生产系统采用文本可读的非参数化存储；Codex 以 git 管理的 Markdown 目录为记忆本体、SQLite 承载流水线状态，适用于需要人工审阅与版本追踪的场景。
2. 现有开源框架的溯源普遍薄弱，记忆与来源的多对多关系表只在少数实现中出现，来源丢失多发生在合并操作中。溯源研究的共同前提是"原始证据不可变、派生记忆可再生成"，三种用途（回源校验、安全门控、价值归因）对字段的要求不同。
3. 生命周期的主流选择是"失效而不删除"（Graphiti、PROV `wasInvalidatedBy`）与"使用驱动保留"（Copilot、Codex），二者可以组合：有效期管事实是否成立，使用统计管是否值得注入。
4. 撤销与删除必须在检索层生效。只追加或只在存储层标记失效，旧条目仍会被检索并主导决策（TEPA、2609.08258、2608.25553）；删除源数据需按派生图级联（MEMOREPAIR、SBU）；多租户场景需检索后按 ownership 与受众硬过滤（MemLeak、Authorization Before Context），整合时需保留来源权限（AuthMem-Bench、TMA-NM）。
5. 开放问题：缺少面向 agent 记忆的溯源标准；以 span 为锚点的溯源与撤销尚无开源实现；记忆质量评分与真实迁移效果的关系缺少验证。

## 8. 评测

本节整理记忆评测的公开基准、在编码与 data 场景中的主要结论、指标体系、公开数据集之间的重叠检查、离线效用统计的可靠性与对照组设计。

### 8.1 对话记忆基准及局限

表 8-1 对话记忆基准

| 基准 | 编号 | 规模与设置 | 考察能力 |
|---|---|---|---|
| LoCoMo | 2402.17753 | 平均约 300 轮、9K token，最多 35 个会话 | 问答、事件摘要、多模态对话生成 |
| LongMemEval | 2410.10813（ICLR 2025） | 500 题 | 信息抽取、多会话推理、时间推理、知识更新、拒答；长期交互下准确率下降约 30% |
| MemBench | 2506.21605（ACL 2025 Findings） | factual 与 reflective 两个记忆层次；participation 与 observation 两种场景 | 有效性、效率、容量 |
| MemoryAgentBench | 2507.05257（v4 2026-06） | 把长上下文数据改造为增量多轮输入，新增 EventQA、FactConsolidation | 准确检索、测试时学习、长程理解、选择性遗忘 |
| HaluMem | 2511.03506 | Medium 版 20 用户、14,948 记忆点、3,467 问答；上下文超过 1M token；8 名标注员抽检 700 个 session，正确率 95.70% | 抽取、更新、问答三个操作级任务 |
| BEAM | — | 700 题技术记忆压力测试，nugget 评分（按答案应含的关键信息点逐项判分） | 长程技术记忆（二手资料，经 Eywa 引用） |

这些基准的局限有三点。第一，评测对象是"对话中的事实能否被记住并回答"，与经验复用能力相关性弱：在 LoCoMo 上接近饱和的 agent 在 MemoryArena（2602.16313）上表现差。第二，端到端问答分数无法区分错误来自抽取还是更新；HaluMem 把评测拆成三个操作级任务后发现抽取与更新阶段产生并累积幻觉，并传导到问答。第三，HaluMem 的 judge 为 GPT-4o，未报告 judge 与人工的一致性。

### 8.2 经验与程序性记忆基准

表 8-2 经验与程序性记忆基准

| 基准 | 编号 | 环境与规模 | 指标 | 主要结论 |
|---|---|---|---|---|
| Evo-Memory | 2511.20857 | 单轮 MMLU-Pro、GPQA-Diamond、AIME、ToolBench；多轮 AlfWorld、BabyAI、PDDL、ScienceWorld；任务流式到达，每题 search → synthesis → evolve | 准确率、成功率、进度率、步数、顺序鲁棒性（Easy→Hard 与 Hard→Easy） | 比较 10 余种记忆模块；简单的 ExpRAG（bge-base 检索 k=4、条目带正确性标签）已很强；ReMem 在 AlfWorld 把平均步数从 22.6 降到 11.5；代码未发布 |
| MemoryArena | 2602.16313 | 网页导航、偏好约束规划、渐进式信息搜索、序列化形式推理；子任务相互依赖、多会话 | 任务完成 | LoCoMo 上接近饱和的 agent 在此表现差；团体旅行任务所有方法成功率为 0 |
| 程序性记忆检索基准 | 2511.21730 | ALFWorld；专家轨迹与 LLM 生成轨迹两个语料 | 检索质量 | 嵌入检索在新情境出现 generalization cliff；LLM 生成的程序抽象跨情境迁移可靠；扩充语料收益大于丰富表示 |
| SWE Context Bench | 2602.08316 | 1,100 基础任务 + 376 关联任务，51 仓库、9 语言；Claude Code、Codex、Qwen Code | 解决率、运行时间、token | 正确选择的摘要经验提升解决率并降低成本；未过滤或选错的经验收益有限甚至为负；正文无代码与数据链接 |
| EvoAgentBench | 2607.05202 | 网页研究、算法推理、软件工程、知识工作；528 / 267 划分（SWE 子集 87 / 56，来自 Verified） | 编码、路由、吸收的细粒度诊断 | 人工整理的 Ability 跨模型族稳定迁移；没有自动方法在所有设置下保持正收益 |
| VibeMemBench | 2609.23570 | 90 个 SWE-rebench V2 仓库的 111 个目标，3,634 条历史轨迹 | 可执行解决率、步数 | 见第 8.3 节；代码与数据未发布（仓库目录只有内容为 "Coming" 的 README，最后提交 2026-09-15） |
| DreamBench-SWE | 2608.20664 | 多会话 SWE，可执行隐藏 oracle；预注册设计 | 解决数 | 无记忆 21/180、逐字记忆 82/180、Mem0 97/180；主对比不显著（据摘要） |
| EvoPathBench | 2609.24663 | 按 checkpoint 冻结演化产物 | 泛化、保持、规则适应 | 瓶颈在候选的评估与选择（据摘要） |
| ConsolidationBench | 2609.33013 | 整合决策（保留、压缩、抽象为 skill 或 rule、遗忘） | 任务成功、压缩比 | 任务成功 +22.7%、压缩 7 倍；生产检索系统跨层迁移得 0；整合质量分与真实迁移不相关（ρ = −0.24，n = 12，置信区间跨零）（据摘要） |
| DolphinBench | 2609.24971 | 600 任务 | 任务完成度，强制报告成本与延迟 | 未取得结论数字（据摘要） |
| MemCalib | 2609.24259 | 逐命题度量记忆对回答的影响 | 记忆过用与欠用 | 前沿模型普遍过用或欠用记忆；常规后训练只改善一个方向（据摘要） |
| SkillsBench | 2602.12670 | 当前版本 87 题、8 个领域，18 个模型与 harness 配置，确定性验证器 | 有 / 无技能配对通过率 | 最新摘要：人工整理技能使平均通过率 33.9% → 50.5%（+16.6pp，配置间 +4.1 至 +25.7）；早期版本正文为 24.3% → 40.6%，模型在解题前自行撰写技能约 −1.3pp；不超过 3 个模块的聚焦技能优于大而全的技能包 |
| AFTER | 2606.23127 | 382 个企业任务、6 种角色、22 项程序性技能 | 跨任务、角色、骨干模型的技能迁移 | 二手资料 |
| AMA-Bench | 2602.22769 | 含 Spider2 子集：51 条 text-to-SQL 轨迹、612 个记忆问答对 | 轨迹记忆问答 + 端到端 | Spider2 端到端：AMA-Agent 26.2、长上下文 23.5、Mem0 15.6 |

方法论文中的评测多沿用任务环境：AWM 用 WebArena、Mind2Web；ReasoningBank 用网页浏览与软件工程；MemGovern 用 SWE-bench Verified。PlugMem（2603.03296）认为没有单一基准同时覆盖情景积累、语义组织与程序复用（二手资料）。

### 8.3 编码与 data 场景的结论

**编码场景**。2026 年的多项结果指向同一结论：经验内容本身有价值，自动构建与检索常常没有收益。

- VibeMemBench：直接注入已验证经验时，5 个求解器中 4 个解决率提升 1.1–4.5 个百分点（glm-5 +2.4、deepseek-v4-pro 0、kimi-k2.7-code +4.5、glm-5.2 +3.6、qwen3.8-max +1.1），5 个步数均下降；5 个 bootstrap 区间均跨零，作者称为方向性证据。目标只在"注入历史经验能在参照设置下提升执行结果"时保留（参照设置 memory-off 43.2%、注入后 78.6%），属于选样内的上限估计。无关记忆对照比无记忆低 0.7–3.4 个百分点。3 个求解器 × 4 个记忆系统（Mem0、SimpleMem、MemoryOS、A-MEM）的 12 组中 11 组未超过无记忆基线；唯一为正的 glm-5 + MemoryOS（+2.0）区间跨零，glm-5 + Mem0 为 −5.5，区间 [−10.59, −0.45] 不含零。剥离实验把收益缺失的主要原因指向记忆记录中转录内容的体量。
- SWE Context Bench 与 EvoAgentBench：正确选择或人工整理的经验有效，自动选择与自动生成的经验不稳定。
- DreamBench-SWE：逐字记忆与 Mem0 的解决数高于无记忆，主对比未达到显著。
- 已有工作报告的记忆增益多在 1.1–5.3 个百分点（VibeMemBench、SWE Context Bench、Memory Transfer Learning 2604.14004），与第 8.8 节估算的 600 个配对下约 ±4 个百分点的 95% 置信区间半宽相当（估算）。

**data 场景**。Crystallization（2608.07213）在 BIRD dev 上以卡片记忆使留出首答准确率提高 4.34 个百分点，且验证门控本身贡献 4.85 个百分点；Memory Reward Inflation（2608.00017）显示自评记忆的增益小于以执行侧信号降权后的增益。机制与数字见《信号与选样调研》第 7.2、7.7 节。

### 8.4 data agent 评测基准

表 8-3 data agent 评测基准

| 基准 | 规模 | 运行条件 | gold | 运行成本参考 |
|---|---|---|---|---|
| BIRD dev | 1,534 题 / 11 库 | Python + SQLite，`dev.zip` 约 346MB，官方 `evaluation.py` | 公开 | — |
| BIRD mini-dev | 500 题 / 11 库（简单 30%、中等 50%、困难 20%） | SQLite 直接可用；MySQL、PG 需自行导入 | 公开 | — |
| Spider 2.0-Lite | 547 题：SQLite 135、BigQuery 205、Snowflake 207（按 jsonl 统计，README 表格数字不同） | SQLite 部分离线；BigQuery 需 GCP；Snowflake 需申请账号，2026-08-12 公告称评测账号被暂停 | 547 题均有执行结果，gold SQL 180 题 | — |
| DABstep | 450 题（easy 72 / hard 378），由 95 个核心问题派生 | 仅需 Python，上下文文件约 24MB | dev 10 题公开，其余须提交 leaderboard | 整轮测试 $2（DeepSeek-V3）至 $155（GPT-4.1） |
| DA-Code | 500 题（数据整理 100、ML 100、EDA 300） | 必须 docker 沙箱；最多 20 步、单步 300 s | 下载获取 | 论文未报告 |
| BIRD-Interact | Lite 300 / Full 600 | docker compose 起 PG 14；用户模拟器默认 gemini-2.0-flash | 邮件申请 | 被测模型 $0.04–0.60 / 题，模拟器约 $0.03 / 题 |

**标注错误**。2601.08778 报告 BIRD Mini-Dev 498 题中 263 题（52.8%）、Spider 2.0-Snow 公开 gold 的 121 题中 76 题（62.8%）存在标注错误。在 BIRD Dev 随机 100 题子集上修正 48 题后，16 个开源 agent 的相对表现变化 −7% 到 +31%，排名平均变动 5 位（−9 到 +9，例如第 1 名降到第 7 名）；原始子集与修正子集的排名 Spearman ρ = 0.32（p = 0.23）。以 gold 正确性作为记忆选样信号或评测标签时，该误差会进入记忆或掩盖记忆收益。DABstep 的 450 题来自 95 个核心问题，按题留出仍有近重复。

data agent 记忆论文常用的评测协议（Crystallization 三分法与结晶率、Tk-Boost 修好率与改坏率、Continual Learning Bench 的 schema 在线迁移、AMA-Bench 轨迹记忆问答）见《信号与选样调研》第 7.9 节。

### 8.5 指标体系

表 8-4 记忆评测指标体系

| 层级 | 指标 | 数据来源 | 参考 |
|---|---|---|---|
| 操作级（抽取） | Memory Recall、Weighted Memory Recall（Σw·s / Σw，s ∈ {1, 0.5, 0}）、Target Memory Precision、Memory Accuracy（抽取项是否被原文支持）、FMR（干扰项被正确忽略的比例） | 人工或 LLM 起草加人工核对的 gold 记忆点 | HaluMem |
| 操作级（更新） | 更新正确率、幻觉率、遗漏率；冲突检出率；过时记忆残留率 | 构造前后矛盾的时序输入 | HaluMem、LongMemEval 知识更新、MemoryAgentBench FactConsolidation |
| 溯源级 | 引用召回（全部引用拼接后蕴含记忆内容）、引用精确率（去掉某条引用后其余仍支持则该引用无关）、来源可解析率 | 记忆到来源片段的引用 | ALCE（TRUE NLI 与人工 κ：召回 0.698、精确 0.525）；Eywa（2605.30771）写入前的词面重合、硬锚点一致、否定保留检查 |
| 检索级 | recall@k、MRR，按熟悉情境与新情境分组 | 查询到 gold 记忆 | 2511.21730 |
| 端到端 | 成功率、步数、token、延迟、改坏率，对照无记忆基线 | 可复跑任务环境或线上 A/B | Evo-Memory、VibeMemBench、Crystallization；Copilot 编码 agent 线上 PR 合并率 83% → 90% |
| 稳定与安全 | 任务顺序鲁棒性、投毒攻击成功率、正常任务性能损失、记忆过用与欠用 | 顺序变换、注入对抗记录 | Evo-Memory、AgentPoison、MemLineage、MemCalib |
| 生命周期 | 存储增长率、陈旧率、使用率分布、支持计数 | 运行日志 | FadeMem、Codex `usage_count` |

调研范围内未查到专门评测"agent 记忆是否被来源 trace 支持"并报告与人工一致性的工作。ALCE 的 NLI 判定受模型输入长度限制；span 内容较长时可改用 LLM judge，并以人工标注样本校准（推论，未见实证）。

指标之间的关系有两条已知结论。第一，操作级质量分与下游效果可能不一致：ConsolidationBench 中整合质量分与真实迁移的秩相关为 −0.24（n=12，置信区间跨零，据摘要）。第二，离线比较需做多重比较校正：Prompt-side Playbooks（2608.05778）在 TAU2-Bench 上检验的 135 个路由级效应中只有 1 个通过 Holm 校正（据摘要）。

### 8.6 公开数据集之间的实例重叠与泄漏

本节为公开数据集上的分析。方法：以 `(repo 小写, PR 编号)` 为实例键，比较候选基准与 Open-SWE-Traces（2606.16038）的实例交集、仓库交集与 PR 创建时间；数据来自本地 parquet 列与 HF datasets-server 的 `instance_id`、`repo`、`created_at` 三列，读取于 2026-10-09。Open-SWE-Traces 的标签分布见第 9.1 节。

表 8-5 公开 SWE 基准与 Open-SWE-Traces 的实例重叠

| 基准 | 规模 | 创建时间 | 与 Open-SWE-Traces 实例交集 | 与带标签实例交集 | 位于 Open-SWE-Traces 已有仓库的任务数 |
|---|---|---|---|---|---|
| SWE-bench Verified | 500 / 12 repo | 2013-01 – 2023-08 | 0 | 0 | 1 |
| SWE-bench Lite test | 300 / 12 repo | 2012-09 – 2023-07 | 0 | 0 | 3 |
| SWE-Gym | 2,438 / 11 repo | 2018-09 – 2024-05 | 236 | 222 | 1,698 |
| SWE-rebench V1 test | 21,336 | 2014-04 – 2025-04 | 1,363 | 496 | 6,866 |
| SWE-rebench leaderboard（月度合计） | 860 / 413 repo | 2025-01 – 2026-05 | 57 | 26 | 276 |
| SWE-rebench-V2 | 32,079 | 2014-11 – 2025-10 | 23,069（V2 子集全部） | 16,886 | 23,239 |
| Scale-SWE（HF 公开部分） | 20,181 | — | 19,462（96%） | 0 | — |

结论：

1. SWE-bench Verified 与 Lite 与 Open-SWE-Traces 在实例与仓库上几乎不重叠，没有泄漏；以 Open-SWE-Traces 为记忆源时，对这两个基准只能提供跨仓库的通用经验。
2. SWE-Gym、SWE-rebench V1 与 Open-SWE-Traces 有实例级重叠，直接用作测试集需先剔除重叠实例。
3. SWE-rebench-V2 与 Scale-SWE 是 Open-SWE-Traces 的来源，与 Open-SWE-Traces 不构成独立的数据集；Open-SWE-Traces 中成败并存、且同 repo 有不少于 5 个更早带标签 instance 的 instance 有 3,016 个。
4. Open-SWE-Traces 带标签实例的 PR 创建时间最晚为 2025-07-22；SWE-rebench leaderboard 中晚于该日期的任务与带标签实例无交集，2025-08 至 2026-03 共 463 题，其中 134 题位于有带标签实例的 74 个仓库。
5. 数据卡与论文均未声明对 SWE-bench Verified 等评测集去重；VibeMemBench 的目标同样来自 SWE-rebench V2，发布后需按实例键再做剔除。

记忆源与测试集切分的常见规则（VibeMemBench、STAIR 2607.29658）：测试实例的全部轨迹从记忆源删除；同仓库只使用 PR 创建时间早于测试实例的轨迹；跨仓库记忆分开报告；生成记忆的流程不可见测试实例的参考补丁与测试补丁。

### 8.7 离线效用统计的可靠性

"记忆被检索或注入后的任务成功率"常被用作记忆效用的离线替代，已知问题如下。

表 8-6 离线效用统计的已知问题

| 问题 | 证据 |
|---|---|
| 只有被检索的记忆得到反馈；多条记忆同时注入时信用分配不明 | MemRL（2601.03192）只更新实际注入的记忆，无偏性依赖策略冻结与任务分布平稳，多条记忆的信用分配列为开放问题 |
| 小样本下估计偏差与误删 | 2505.16067 要求被检索至少 n 次才参与删除判定，并指出评估器噪声导致误删 |
| 在预生成 trace 上评测的复用偏差 | AMemGym（2603.01966）：off-policy 评测引入 reuse bias，记忆实现的排名与 on-policy 不同 |
| 缺少反事实校正 | 2604.27283 用 contextual bandit 检索，作者称离线 replay 不提供因果保证，IPS / DR（逆倾向加权估计 / 双重稳健估计）列为后续工作；调研范围内未查到用 IPS / DR 校正记忆效用的工作 |
| 计数规则未处理选择偏差 | ReMe：检索时 freq+1，判为有用时 utility+1，freq 达阈值且 utility / freq 低于阈值时删除 |
| 自评分数的膨胀 | 2608.00017：自评分数在记忆闭环中膨胀，更强或跨厂商的复核器继承同一偏差，检索外部信息的核验器与执行侧信号与之去相关（《信号与选样调研》第 5.4 节） |

可从离线策略评估迁移的做法有三类（推论，记忆文献中未见实证）：以难度协变量减轻"记忆多被用于简单任务"的选择偏差，例如以同一实例的历史成功率作为无记忆期望成功率；以小概率随机屏蔽候选记忆并记录倾向分数，使单条记忆的效用可用 IPS 估计；在子集上做逐条记忆消融，检查效用统计与消融结果的秩相关。

### 8.8 对照组设计的常见做法

表 8-7 对照组设计

| 对照组 | 衡量的内容 | 采用者 |
|---|---|---|
| 无记忆 | 基线 | 全部端到端评测 |
| 直接注入（不检索），如同仓库已验证经验、Oracle Summary | 记忆内容本身的价值上限 | VibeMemBench、SWE Context Bench |
| 自动生成 + 检索（完整流程），如 Free Summary、现有记忆系统 | 实际可得收益 | VibeMemBench、SWE Context Bench、EvoAgentBench |
| 原始轨迹检索 | 不做抽象时的基线 | Evo-Memory 的 ExpRAG、2603.18272 |
| 无关记忆 | 注入本身的干扰 | VibeMemBench（比无记忆低 0.7–3.4 pp） |
| 去掉验证门控 | 门控的贡献 | Crystallization（+4.85pp）、OpsHarness |
| 本库与外库卡片 | 作用域的贡献 | Crystallization（+6.73pp） |
| 不同任务顺序 | 流式更新的稳定性 | Evo-Memory |

"直接注入"与"无记忆"之差衡量内容价值，"直接注入"与"自动生成 + 检索"之差衡量检索与路由损失。

**公开 harness 的单次运行成本量级**。SWE-bench bash-only 榜（Verified，mini-swe-agent）上 MiniMax-M2.5 为 75.8%、$0.073 / 实例、60.5 次调用，DeepSeek-V3.2 $0.448，GLM-5 $0.534；VibeMemBench 无记忆运行每次输入 1.56M–4.06M token、50–77 步，444 次运行共 8.82 亿输入 token；STAIR（mini-swe-agent v2 + MiniMax-M2.5）$0.84 / 实例、1.24M token；SWE-rebench leaderboard 上 Qwen3.6-27B $0.62 / 题；SWE Context Bench 每任务 $0.31–0.67、344–801 s。Evo-Memory 作者称 API 总花费为数万美元。

**统计功效**。已报告的增益多在 1–5 个百分点。成对二值结果下差值标准误约为 √(不一致率 / 配对数)；按不一致率 25% 估算，600 个"任务 × 种子"配对的 95% 置信区间约 ±4 个百分点，2,000 个配对约 ±2.2 个百分点（估算）。选择成败并存的实例可提高不一致率，从而提高功效。

### 8.9 结论与倾向

1. 对话记忆基准已接近饱和，且与经验复用能力相关性弱；经验记忆的评测以有 / 无记忆的端到端任务成功率差为主指标，操作级、溯源级、检索级指标用于定位问题。
2. 编码场景的证据一致显示：人工整理或直接注入的经验有正向作用但幅度小且常不显著，现有记忆系统自动构建与检索时多数未超过无记忆基线。评测需要把内容质量与检索路由质量分开测。
3. 操作级质量分与下游迁移可能不相关，多项离线比较需做多重比较校正；接收决策以端到端对照为准的做法有摘要级证据支持（2609.33013、2608.05778，据摘要）。
4. data 场景的公开基准存在高比例标注错误，以 gold 作为选样信号或评测标签时需要计入该误差；验证门控在 data 场景有量化收益。
5. 公开 SWE 数据集之间存在实例级重叠，以 `(repo, PR 编号)` 为键的检查成本低，是使用公开轨迹作记忆源时的必要步骤；时间前向切分是避免泄漏的通用做法。
6. 记忆效用的离线统计受选择偏差、off-policy 偏差与自评膨胀影响，调研范围内尚无经过反事实校正的实证方法，效用统计宜作为排序信号，不作为评测结论。

## 9. 公开数据集上的分析

本节汇总调研中在公开数据集上做的统计，各项只读取所需列或小样本，样本与方法随表注明。Open-SWE-Traces 的统计基于 HF 上的 `nvidia/Open-SWE-Traces`（254 个 parquet 分片）。

### 9.1 Open-SWE-Traces：规模、来源与标签覆盖

Open-SWE-Traces（2606.16038）是 code agent 在 SWE 任务上的执行轨迹集。字段包括 instance_id、repo、language、trajectory_id、messages（role、content、reasoning_content、tool_calls）、tools、resolved、metadata（category、reference_patch、model_patch、git_hack_attempted）与 hf_dataset_name。数据卡说明：v1.0 由 OpenHands v0.53.0 与 SWE-agent 生成，后按 2609.06780 删除含 git hacking 行为的轨迹（约 20.7 万条减至 15.1 万条）；v1.1 加入 DeepSeek-V4-Flash 与 Qwen3.6-27B；v1.2 加入 Qwen3.8-27B（仅 mini-swe-agent）；resolved 由单元测试判定；数据卡与论文均未声明对 SWE-bench Verified 等评测集去重。

表 9-1 Open-SWE-Traces 规模与来源

| 项 | 值 |
|---|---|
| 轨迹数 | 618,821 |
| instance 数 / repo 数 | 42,531 / 3,789；最大 repo 为 app-sre/qontract-reconcile（7,374 条） |
| 来源子集 | SWE-rebench-V2：384,466 条、23,069 个 instance、2,678 个 repo，多语言；Scale-SWE：234,355 条、19,462 个 instance、1,180 个 repo，仅 Python。两子集 instance 无交集，repo 交集 69 个 |
| agent × 模型组合 | mini-swe-agent、SWE-agent、OpenHands 与 5 个模型共 9 组 |
| resolved 分布 | 未判定（−1）502,938（81.3%）；失败 67,590；成功 48,293 |
| 多次尝试 | 42,472 个 instance 有多次尝试，38,893 个不少于 10 次 |

带标签轨迹只出现在 v1.0 的 4 个组合中，且全部属于 SWE-rebench-V2 子集；mini-swe-agent 与 Qwen3.6-27B、Qwen3.8-27B、DeepSeek-V4-Flash 的轨迹全部未判定，Scale-SWE 子集全部未判定。

表 9-2 带标签组合的成败分布

| agent × 模型 | 成功 | 失败 | 未判定 | 已判定成功率 | 每 instance 尝试数中位数 |
|---|---|---|---|---|---|
| SWE-agent × MiniMax-M2.5 | 16,749 | 19,206 | 10,864 | 46.6% | 3 |
| OpenHands × MiniMax-M2.5 | 14,363 | 19,510 | 9,730 | 42.4% | 3 |
| SWE-agent × Qwen3.5-122B | 7,348 | 7,750 | 5,236 | 48.7% | 2 |
| OpenHands × Qwen3.5-122B | 9,833 | 21,124 | 9,506 | 31.8% | 2 |

带标签的 instance 共 16,886 个（2,345 个 repo），其 PR 创建时间为 2014-11-10 至 2025-07-22（p90 为 2024-12-11）。

结论边界：标签覆盖只有 18.7%，且集中在两个 scaffold 与两个模型上；以 outcome 为前提的选样与对比方法只能在这部分数据上验证。

### 9.2 同 instance 成败分布与跨任务难度混杂

方法：按 instance 汇总 resolved 标签。全量统计把 4 个带标签组合合并计算；分组粒度与难度混杂的统计限于 OpenHands × Qwen3.5-122B 单一组合（30,957 条带标签轨迹，成功 9,833、失败 21,124，14,142 个 instance，2,234 个 repo）。

表 9-3 同 instance 成败分布与难度混杂

| 统计 | 结果 |
|---|---|
| 带标签 instance 的结果（4 组合合并） | 全失败 7,661（45.4%）；全成功 3,362（19.9%）；成败并存 5,863（34.7%）；成败并存 instance 的成功率四分位为 0.33 / 0.55 / 0.75 |
| 单一组合中 3 次尝试的 instance（6,013 个） | 全失败 58.0%；全成功 23.2%；成败并存 18.8% |
| 单一组合按同 instance 分组 | 成败并存 1,726 / 14,142 组（12.2%），覆盖 4,581 条轨迹（14.8%） |
| 单一组合按同 repo 分组 | 成败并存 1,293 / 2,234 组（57.9%），覆盖 25,367 条轨迹（81.9%）；943 个 repo 同时有全成功与全失败的 instance |
| repo 内 instance 数 | 中位数 3，P90 为 13，最大 315；不少于 5 个 instance 的 repo 886 个 |
| 同 repo 随机 instance 对的成功率差绝对值 | 0.356（不同 repo 为 0.417） |
| 按参考补丁修改文件的 Jaccard 分组的成功率差 | =0：0.362；(0, 0.5)：0.334；≥0.5：0.339 |
| 按 issue 文本相似度分组的成功率差 | <0.1：0.359；0.1–0.3：0.349；≥0.3：0.311（该组 74% 共享修改文件） |

结论：对单一 agent 与模型，多数任务的多次尝试结果一致（全部成功或全部失败），结果主要由任务决定。把对比从同一 instance 放宽到同一 repo，可对比的轨迹从约 15% 增至约 82%，但跨任务的成败对中，成功轨迹多来自简单任务、失败轨迹多来自困难任务；提高文本相似度或要求共享修改文件只把成功率差从 0.36 缩小到 0.31。结论边界：4 组合合并的 5,863 个成败并存 instance 包含跨 agent、跨模型的成败对，同一 agent 与模型内的成败并存比例更低。

### 9.3 轨迹体量、工具输出占比与压缩比例

方法：9 个组合各取居中分片的前 200 条，共 1,800 条轨迹，按消息的 UTF-8 字节数统计；另按组合各抽 500 条统计轮次与最终上下文长度。

表 9-4 轨迹体量与压缩比例

| 项 | 值 |
|---|---|
| 平均每条轨迹 | 221.5 KB（约 5.5 万 token） |
| 字节占比 | tool 消息 67.5%；assistant（含 tool_calls JSON）28.7%；user 2.2%；system 1.6% |
| 每条工具输出截断到 8 KB 后的总量 | 原总量的 84.4% |
| 只保留最近 10 条工具输出全文、其余替换为占位符后的总量 | 原总量的 43.1% |
| 超过 600 KB 的轨迹 | 12 / 1,800 |
| assistant 轮次中位数（每组合 500 条） | 54–129；SWE-agent × MiniMax-M2.5 为 68，p90 为 123 |
| 最终上下文字符数中位数（每组合 500 条） | 17.8 万–26.6 万（约 5–7 万 token） |

结论：工具输出是体量的主要来源。单条截断只影响少数超长输出，观察掩码（只保留最近若干条工具输出）是主要压缩手段。结论边界：两种做法结合后的总体量未测量；只测量体量，未测量压缩对记忆抽取质量的影响（第 4.1 节）。

### 9.4 自述成功的区分度

方法：在 17.3 的 1,800 条样本中取 resolved ∈ {0, 1} 的 623 条，用正则（`successfully`、`resolved`、`fixed the`、`all tests pass`、`works correctly`、`implemented` 等）检查最后一条非空助手消息是否含成功措辞。

表 9-5 自述成功的区分度

| resolved | 自述成功 | 比例 |
|---|---|---|
| 0（失败） | 157 / 330 | 47.6% |
| 1（成功） | 115 / 293 | 39.2% |

结论：失败轨迹自述成功的比例高于成功轨迹，agent 自述不能作为结果信号。结论边界：正则口径粗糙，结论只用于说明自述没有区分度，不用于估计自述的错误率。

### 9.5 结构相似度与文本相似度

方法：数据为 OpenHands × Qwen3.5-122B 的 30,957 条带标签轨迹。动作抽象为 64 种 token：`str_replace_editor` 取子命令（VIEW、EDIT_create、EDIT_str_replace 等），`execute_bash` 取首个有效程序并归类（TEST、RUN、SEARCH、READ、GIT_diff 等）；轨迹长度中位数 88。比较三种表示：issue 文本 TF-IDF 余弦、动作 bigram 袋余弦、连续去重后动作序列的归一化编辑距离相似度（NED）。每类配对各 1,500 对。

表 9-6 三种表示的配对相似度

| 配对（均值 / 中位数） | 文本 TF-IDF | 动作 bigram | 动作 NED |
|---|---|---|---|
| 同 instance | 1.000 / 1.000 | 0.751 / 0.778 | 0.457 / 0.458 |
| 同 repo 不同 instance | 0.151 / 0.122 | 0.651 / 0.670 | 0.393 / 0.393 |
| 不同 repo | 0.013 / 0.010 | 0.506 / 0.516 | 0.338 / 0.337 |

表 9-7 结构与文本表示的区分度与预测能力

| 指标 | 结果 |
|---|---|
| 配对类型可分性 AUC（同 instance vs 同 repo；同 repo vs 不同 repo） | bigram 0.741 / 0.736；NED 0.747 / 0.718 |
| 近邻检索 P@1（池 8,000 条，300 个查询，排除同 instance，命中同 repo 为正确） | 文本 0.670；bigram 0.043；文本 0.7 + bigram 0.3 混合 0.647 |
| 同 instance 内成功-成功对与成功-失败对的 bigram 相似度 AUC | 0.563（均值 0.752 vs 0.715） |
| 同 repo 跨 instance 的 bigram 相似度均值 | 成功-成功 0.683，成功-失败 0.648，失败-失败 0.633 |
| kNN（k=10，排除同 instance）预测 resolved 的 AUC | 文本 0.617；bigram 0.613；轨迹长度 0.565；同 repo 其他 instance 的成功率 0.707 |

结论：动作结构在群体层面有信号（配对类型 AUC 约 0.74），作为检索键几乎无用（P@1 为 0.043，文本为 0.670）；同一 agent 与模型的动作分布高度同质，结构相似主要反映该 agent 的习惯。结构与结果只有弱相关（同任务内 AUC 0.563）。预测结果最强的特征是同 repo 其他 instance 的成功率，与 17.2 的难度混杂结论一致。结论边界：限于单一 agent 与模型以及上述动作抽象，未比较 LLM 生成的任务描述。

### 9.6 阈值连通分量

方法：在 17.5 数据的 instance 级 issue 文本 TF-IDF 上构建 kNN 图（k=10），按相似度阈值取连通分量。repo 纯度为各分量中占比最高的 repo 的成员数之和除以分量成员总数；成败并存指分量内同时有可解（任一次成功）与不可解的 instance。

表 9-8 阈值连通分量

| 阈值 | 多成员分量数 | 覆盖 instance | 最大三个分量 | repo 纯度 | 成败并存分量 |
|---|---|---|---|---|---|
| 0.2 | 1,004 | 9,918（70.1%） | 5,839 / 103 / 61 | 0.383 | 566 |
| 0.3 | 1,394 | 5,322（37.6%） | 108 / 95 / 83 | 0.910 | 683 |
| 0.4 | 894 | 2,606（18.4%） | 72 / 45 / 38 | 0.962 | 363 |
| 0.5 | 454 | 1,196（8.5%） | 49 / 31 / 17 | 0.970 | 155 |
| 0.7 | 124 | 290（2.1%） | 8 / 6 / 6 | 1.000 | 36 |

结论：阈值 0.2 时出现覆盖 41% instance 的单个超大连通分量（纯度 0.38），0.3 时该超大分量消失、纯度升至 0.91，覆盖率随阈值升高快速下降（0.4 时 18.4%）。单链接的链式效应（chaining）使可用阈值集中在 0.3 附近，阈值变化时大量分量合并或拆分，簇标识跨运行不能直接沿用。在元数据作用域内运行或采用显式处理离群点的方法（如 HDBSCAN）可减少这一问题（推论，《信号与选样调研》第 4.6 节）。

### 9.7 与公开基准的实例重叠

该项统计的方法、结果表与结论见第 8.6 节。

### 9.8 Who&When Pro

Who&When Pro（2607.09996，HF 数据集，CC BY 4.0）是失败归因基准。每条 trace 在重放成功前缀后注入一个已知错误，标签 `ground_truth = {agent, step, mode}` 记录出错的 agent、步骤与错误模式；单 agent 轨迹的 agent 为 null；`image_gui` 中 step 从 1 起始，与轨迹的 `step_number` 一致。行字段为 id、framework、benchmark、task、trajectory、ground_truth、extras。

表 9-9 Who&When Pro 规模与错误模式

| 项 | 值 |
|---|---|
| split | text、image、image_gui、video；全集 12,326 条 |
| text / video 规模 | 6,257 / 916 条 |
| text 中的错误模式 | 出现 14 种；最多为 A.3 1,207、R.4 1,120、R.2 1,089、R.3 739、R.1 621、V.2 486 |
| text 中与 data agent 相关的 smolagents 轨迹 | 3,276 条（tablebench 913、dsqa 830、databench 818、dabench 327、dacode 306、kramabench 82） |
| 原始任务（text split） | 2,599 个（按 `task.query` 全文去重）；同一任务最多有 11 个注入变体 |

taxonomy 共 17 种错误模式、6 大类：

表 9-10 Who&When Pro 错误分类

| 类别 | 模式 |
|---|---|
| Perception（P） | P.1 视觉误识别；P.2 空间定位错误 |
| Reasoning（R） | R.1 幻觉；R.2 推理错误；R.3 数值计算错误；R.4 任务误解 |
| Planning（PL） | PL.1 无效规划；PL.2 目标偏离 |
| Action（A） | A.1 工具参数或调用错误；A.2 输出格式错误；A.3 过早终止；A.4 重复循环 |
| Verification（V） | V.1 上下文或记忆丢失；V.2 验证不足或错误 |
| Coordination（C） | C.1 委派或编排错误；C.2 通信失败；C.3 过度依赖其他 agent |

模式描述含判别规则，例如候选输出经若干处局部修补后能够通过的记为 R.2，否则记为 R.4。V.1 可用于标注"记忆误导"类错误。

数据性质：标签给出注入错误的 agent、步骤与模式，是已知正确的归因结果，可与归因模型的输出对照。结论边界：错误为合成注入，单点且因果清晰，难度低于真实失败；数据不含修复后成功的对照轨迹；text split 中没有 SWE 类轨迹。

### 9.9 其他公开数据集的字段与标签

表 9-11 其他公开数据集的字段与标签

| 数据集 | 规模 | 字段与标签 | 与本问题的关系 |
|---|---|---|---|
| SALT-NLP/SWE-chat（2604.20779） | v2：17,968 个真实开发者会话、229,909 条用户 prompt、707 个仓库，约 69% 来自 Claude Code | `conversations` 含 `session_id`、`turn_type`、`tool_name`、`tool_call_id`、`tool_input_json`、逐行 `timestamp`；逐 prompt 的 `prompt_pushback` ∈ {correction, rejection, failure_report, non_pushback}（LLM 标注）；`queue_op_subtype` 标识运行中插话；`sessions` 含 `user_persona`、`session_success`（0–100）、`prompt_attributions`（人与 agent 的代码行归属）；另有 commits、subagent_tasks、skill_invocations 等配置。ODC-BY，需登录同意条款 | 唯一同时具备真实用户、大规模、逐 prompt 纠正标注、结构化工具调用与时间戳的数据集；标签取值分布未读取 |
| experiential-labs/wmo-dabstep-traces | 687 个 trace、9,718 个 span | OTLP JSON 格式的 OTel GenAI span，span 名为 `chat dabstep` / `execute_tool dabstep`，属性含 `gen_ai.operation.name`、`gen_ai.tool.name`、`gen_ai.tool.call.arguments` 等；根 span 元数据含 task_id、split、reward、final_answer；模型 claude-opus-4-7 344 条（reward=1 共 156）、claude-opus-4-8 340 条（158）、gpt-5.4 3 条；80 个 span 为 ERROR 状态。CC BY 4.0 | 直接以 OTel GenAI span 发布的 data agent 轨迹；每任务只有 1 条轨迹，没有同任务成败对比 |
| OpenHands/openhands-feedback | 275 条真实会话 | 会话级 `feedback` ∈ {positive, negative}（164 / 111）；轨迹事件含 action、observation、source ∈ {environment, agent, user}、timestamp。MIT | 会话级用户评价；无逐条纠正标注，无独立 LLM 调用记录 |
| trace-commons/agent-traces | 30 个会话 | Claude Code 原始 JSONL，含 `uuid`、`parentUuid`、`timestamp`；中断以 `[Request interrupted by user]` 文本出现，工具拒绝以固定 tool_result 文本出现。CC BY 4.0 | 无标注；可用于核对中断与拒绝信号的原始形态 |
| microsoft/WildFeedback（2408.15549） | 20,281 个偏好对；1,017,754 行话语级标注；24,070 行偏好抽取 | 话语级 SAT / DSAT 布尔值与理由、对话状态（含 REFINEMENT、FEEDBACK）；无工具、无时间戳、无会话 ID。ODC-BY | 纯聊天；可作反馈分类器的标注参考 |
| birdsql/tapilot-crossing（2403.05307） | 约 950 条（各统计口径为 946–952 条，1,094 个意图），5 个领域 | 模拟用户的交互式数据分析对话；`action_correction` 等纠正子集（16 + 15 条）含"报错 → 修正"回合，另有私有库与欠定问题子集；带可执行评测代码。许可未声明 | data agent 场景的纠正样本，规模小且为模拟用户 |
| analytics-agents-uncertainty/da-code-evaluation-results | DA-Code 500 个任务 × 约 10 个模型配置 | 每任务 `raw_trajectory.json`、`result.json`；每配置逐任务 total_score 与 PASS / FAIL。许可未声明 | 同一任务有多模型结果，可构造同任务成败对比 |

结论边界：真实用户与 data agent 交互的公开日志缺失，见《信号与选样调研》第 6.9 节。以上数据集中只有 wmo-dabstep-traces 为 OTel span 格式。

## 10. 综合分析与倾向

本节汇总第 4–9 节的结论：先列多个方向独立得出的共识，再按证据强度列出主要结论与分歧，然后给出现有工作中有前景的方法组合及其前提，最后列出研究空白。本节内容为调研结论与倾向；各表只列与本报告主题相关的行，引用其他报告的章节以报告简称标出。

### 10.1 跨方向的共识

表 10-1 跨方向的共识

| 共识 | 独立得出该结论的方向与代表证据 |
|---|---|
| 原始证据需保留并可回查，预先生成的摘要会丢失信号 | 程序优化：Meta-Harness（第 4.1 节）；经验复用：QCR、2601.22436（《生成方法调研》第 4.8.4 节）；溯源研究：TierMem、Eywa（第 7.4 节）；产品：Codex 以 rollout summary 作证据层（《生成方法调研》第 6.2 节）；平台：簇与 issue 回指源 trace（《信号与选样调研》第 4.1 节） |
| agent 自述与同源自评不能作为结果信号，执行侧与外部确认信号更可靠 | 公开数据统计：自述成功无区分度（第 9.4 节）；评估研究：2608.00017、R2E-Gym、DeepSWE（《信号与选样调研》第 5.4 节）；产品与开源 prompt：Codex 把只有助手自称成功的任务标为 uncertain，altk-evolve 写明 "Self-reported success is not evidence"（《生成方法调研》第 5.2、6.2 节）；软件工程：agent PR 合并标签噪声大（《生成方法调研》第 7.6 节） |
| 撤销与删除需在检索层与派生产物上生效 | 偏好研究：TEPA、2608.25553（《信号与选样调研》第 6.6 节）；溯源研究：MEMOREPAIR、SBU、2609.08258（第 7.7 节）；产品：Codex diff 驱动遗忘，Claude.ai 与 ChatGPT 把源删除与派生记忆删除做成独立操作（《生成方法调研》第 6.4 节） |

### 10.2 分歧与证据强度

依据类型分五种：原文实验（论文全文中的对照与消融）、生产部署自报（产品或工程博客自报的线上数字）、摘要（只读摘要页或二手资料）、本报告公开数据统计（第 9 节与第 8.6 节，样本与方法见各节）、无（检索范围内未见实验）。强度判据：强为多篇独立的原文实验结论一致且有消融；中为单篇原文实验、多篇间接证据或原文实验与摘要混合；弱为只有摘要、生产部署自报或合成单例；无证据为检索范围内未见实验。

表 10-2 主要结论的证据强度与分歧

| 结论 | 强度 | 依据类型 | 主要依据 | 分歧或限制 |
|---|---|---|---|---|
| agent 自述无区分度，同源自评在记忆闭环中膨胀 | 强 | 原文实验 + 本报告公开数据统计 | 2608.00017、R2E-Gym、第 9.4 节 | 第 9.4 节为正则口径，只说明无区分度 |
| 对单一 agent 与模型，结果主要由任务决定，跨任务成败对比混入难度差异 | 中 | 本报告公开数据统计 + 原文实验（间接） | 第 9.2、9.5 节；Evo-Memory 增益与任务相似度相关 | 统计限于一个 scaffold 与一个模型；未比较 LLM 生成的任务描述 |
| 自动构建与检索的记忆在编码任务上多数无收益 | 中 | 原文实验 | VibeMemBench、SWE Context Bench、DreamBench-SWE | 分歧：CONTRAMEM（据摘要）、Letta skill learning（无训练与测试划分）报告正收益；直接注入的上限估计经过选样 |
| 观察掩码保持任务效果 | 中 | 原文实验 | Complexity Trap 五组模型、AgentDiet | 只在运行时测量，压缩对记忆抽取质量的影响无消融 |
| 外置记忆在未见任务与需删除的内容上优于参数化 | 中 | 原文实验 + 摘要 | 2603.18272、2607.07847 | SciConsolidate（据摘要）显示小模型从注入中获益少、从 SFT 中获益多 |
| 整合环节的投毒与权限塌缩 | 弱至中 | 摘要 + 原文实验（MINJA） | PoisonedEvolution、TBA、OEP、AuthMem-Bench、TMA-NM；MINJA 为原文 | 攻击设置多为作者构造 |
| span 树结构对记忆生成的收益 | 无证据 | 无 | — | 检索范围内未见实验 |

### 10.3 有前景的方法组合及其前提

下表组合来自现有工作中已出现的环节搭配，每项列出代表工作与成立的前提。

表 10-3 有前景的方法组合及其前提

| 组合 | 环节构成 | 代表工作 | 前提 |
|---|---|---|---|
| 不可变证据 + 派生图溯源 | 原始 trace 不可变；记忆与来源多对多，合并时继承来源集合；保留来源权限标签；删除按派生图级联；检索层按归属、受众与状态过滤 | Codex、claude-mem Server、MEMOREPAIR、SBU、AuthMem-Bench、Authorization Before Context | 稳定的 trace 与 span 标识；合并路径实现来源继承（altk-evolve 的反例见《生成方法调研》第 5.5 节） |
| 三组对照评测 | 无记忆、直接注入、自动生成加检索三组；时间前向切分与实例键去重；成败并存实例提高功效 | VibeMemBench、SWE Context Bench、Crystallization | 可复跑环境；数百至上千个配对运行的成本可承受 |

### 10.4 研究空白与机会

表 10-4 研究空白与机会

| 主题 | 现状 | 空白 |
|---|---|---|
| trace 结构 | 开源实现与研究均把 trace 压平为消息或步骤文本；H²R、G-Memory、LEGOMem、MACE 的层级结构最接近 span 树；过程挖掘有父子 span 作子过程边界、参数级数据流边等做法 | 以多 agent、多层 span 树为输入做渲染、切分与归纳并评估收益的工作；span 的耗时、状态码、父子关系作为判别特征的实证 |
| span 级溯源 | 最细为 altk-evolve 的单个 span_id，合并后丢失；ACE、claude-mem 回溯到 trace 或事件；溯源研究给出派生图与引用计数 | 以 span 为锚点、支持按来源撤销与重新生成的实现与评测；记忆是否被来源 span 支持的判定及其与人工一致性 |
| 无标签 trace | 生产 trace 多数无标签；judge 的可靠性取决于是否具备执行能力；隐式信号（用户推回、commit 存活、PR 合并）噪声大 | judge 误差对记忆质量影响的系统分析（现有只有 ReasoningBank 的敏感性分析与 2608.00017 的反例）；不依赖结果标签的生成方法（DENSE、altk-evolve、AgentBrew）与有标签方法的对照 |
| 离线评测闭环 | 接收门控多依赖可重放环境；操作级质量分与迁移不相关；离线效用统计存在选择偏差与 off-policy 偏差 | 纯离线 trace 库上环境只读核验、LLM 校验、部署后使用统计三类门控的对比；经反事实校正（IPS、DR）的记忆效用估计；抽取环节过度升格率的公开基准 |
| 安全与删除 | 整合投毒与权限塌缩针对批量归纳环节；删除级联有 MEMOREPAIR、SBU 的方法；撤销在检索层不生效的问题有基准 | 批量生成流水线上频次门槛、来源独立性、隔离期与组合审查的联合评测；按来源删除在多层派生（摘要、技能、注入上下文的产物、缓存）上的端到端验证 |
| 失效与漂移 | SWE-Exp 只在检索时按仓库与时间排除；Continual Learning Bench 是 schema 在线迁移的唯一设置；Braintrust Patterns 只在近期数据表明行为停止时关闭条目，数据缺失或查询失败不计为停止 | 代码库、API 与 schema 变化后记忆失效的检测与再生成；"无新 trace"与"新 trace 中不再出现"两类情形的淘汰规则评估 |

## 11. 证据边界

### 11.1 数字跨论文不可比

本报告引用的效果数字来自不同的基准、底座模型、指标与基线，彼此不能直接比较，主要差异如下：

表 11-1 数字不可比的来源

| 差异 | 例子 |
|---|---|
| 指标不同 | 成功率（SR、TGC / SGC）、pass^1、pass@k 与 Avg@4、执行准确率（EX）、解决率、F1 与 MAP 混用；Agent KB 的 GAIA 对比为 pass@3 对 pass@1，Memento 报告 Pass@3 |
| 相对值与绝对值 | "约高 6%"（WISE-Flow，原文未说明绝对或相对）、"至多高 18.18%"（AgenTracer，作者称）为相对值或作者口径；"个百分点"（表内 pp）为绝对差；相对提升与绝对差在原文中并存时按原文写法引用 |
| 底座模型与 harness | 同一方法在不同模型上的增益差别大（如 CONTRAMEM 各模型 23.0–28.0 提升到 52.5–61.0，据摘要）；AgentSM 的前后对比底座模型不同 |
| 基线不同 | 无记忆、ReAct、GRPO、人写技能、其他记忆系统等基线并存；"比最强基线高"依赖作者选取的基线集合 |
| 评测规模 | 小样本评测（H²R 每环境 3 个 episode、Letta skill learning 无训练与测试划分、meta-agent 单次运行）与大规模评测并列；已报告的 1–5 pp 记忆增益与第 8.8 节估算的置信区间半宽（600 个配对约 ±4 pp）相当（估算） |
| 自报与独立评测 | 产品数字（Copilot、Bugbot、Cursor、LangSmith Engine 处理量）为生产部署自报，无公开实验设计 |
| 判定方式 | 部分工作的成败由 LLM judge 判定（ReasoningBank、PASB、HaluMem）；ReasoningBank 报告了 judge 与 GT 的一致率 72.7%，HaluMem 未报告 judge 与人工的一致性 |

### 11.2 时点与版本

- 检索截至 2026-10-09。产品能力、平台规模、开源实现与模型接口均为该时点的状态，后续变化未纳入。
- 产品状态有时效：Cursor Memories 于 2.1.x 移除；Devin Knowledge 于 2026-09-18 迁移为 Skills；Codex memories v2 于 2026-09-17 发布、默认仍为 v1；Claude Code 依据官方文档最高版本 v2.1.287 与 CHANGELOG；LangSmith Engine 的处理量为截至 2026-09-24 的官方数字；Managed Agents Dreams 为研究预览。
- 开源实现的源码事实对应文末所列 commit 与版本。多个项目在 2026 年发生不兼容变化（Mem0 2.x、Letta V1 服务端退役、ReMe 主线删除 task / tool 记忆、memU 抽取移出服务端、ACE skillbook schema v2），引用效果数字时需区分论文实验版本与当前开源版本（《生成方法调研》第 5.4、5.5 节）。
- OTel GenAI 语义约定依据 commit `06ec68e`（2026-10-07，schema `gen-ai-dev/1.42.0-dev`），全部属性为 development 级，2026-09 内仍有三处相关新增；通用约定依据 v1.44.0；Langfuse 依据 commit `d179469`（2026-09-01）。
- 模型服务的参数支持、上下文上限、价格与限流为 2026-10-09 读取值（第 6 节）。
- arXiv 论文按检索时的最新版本阅读；同一工作的摘要与正文口径不同时以正文表格为准。

### 11.3 据摘要与二手资料项

以下各项只读过摘要页、二手资料或无法访问一手来源，在正文中已逐项括注。凡用作结论依据的摘要级说法，第 10.2 节的证据强度已相应降级。

表 11-2 据摘要与二手资料项

| 节 | 据摘要 | 二手资料或未核实 |
|---|---|---|
| 4 | ACON、SWE-RM、2605.22534、表 4-7 中除 MINJA 外的投毒工作、2608.17148、AuthMem-Bench、TMA-NM | 二手资料：SWE-Pruner、SeCom、AgentPRM 与 ToolPRMBench 的用途、各 agent PR 合并率（AIDev 后续研究）、gitleaks 等凭据规则库、Presidio |
| 5 | — | 二手资料：`db.*` 属性的出现条件。未核实：Codex `cwd` 是否导出、OpenHands SDK 内容采集默认值、各 agent 是否发出压缩与记忆操作 span |
| 6 | 2408.02442、Lost in the Middle、LongCite、CiteFix | 二手资料：vLLM 与 SGLang 的 seed、logprobs 与离线批量；ALCE 数字、Citation Failure、GhostCite |
| 7 | Sparse Memory Finetuning；表 7-3 中 MEMOREPAIR、SBU、Execution-State Unlearning、Authorization Before Context、AuthMem-Bench、TMA-NM、2606.04990；第 7.7.4–7.7.7 节的 TEPA、2608.25553、STALE、2609.08258、2606.15903、MemLeak、SPORE、GateMem；HaluMem 的结论；Epistemics of Agent Memory 的质量分与迁移相关性 | 二手资料：SSGM |
| 8 | DreamBench-SWE、EvoPathBench、ConsolidationBench（2609.33013）、DolphinBench、MemCalib、Prompt-side Playbooks | 二手资料：BEAM、AFTER、PlugMem 的观点 |

### 11.4 公开数据集统计的样本与代表性

表 11-3 公开数据集统计的样本与代表性

| 统计 | 样本与方法 | 代表性限制 |
|---|---|---|
| 标签覆盖（第 9.1 节） | Open-SWE-Traces 全量 618,821 条 | 带标签轨迹只占 18.7%，只来自 v1.0 的 4 个 agent × 模型组合，全部属于 SWE-rebench-V2 子集 |
| 成败分布与难度混杂（第 9.2 节） | 分组与难度统计限于 OpenHands × Qwen3.5-122B（30,957 条） | 单一 scaffold 与模型；4 组合合并的成败并存比例包含跨 agent、跨模型的成败对 |
| 体量与压缩（第 9.3 节） | 9 个组合各取居中分片前 200 条，共 1,800 条 | 非随机抽样；只测体量，未测压缩对抽取质量的影响 |
| 自述成功（第 9.4 节） | 1,800 条中带标签的 623 条，正则匹配成功措辞 | 正则口径粗糙，只说明无区分度 |
| 结构与文本相似度（第 9.5 节） | 单一组合，64 种动作 token，每类配对 1,500 对 | 结论依赖动作抽象方式；未比较 LLM 生成的任务描述与嵌入表示 |
| 阈值连通分量（第 9.6 节） | instance 级 issue 文本 TF-IDF 的 kNN 图（k=10） | 只测 TF-IDF 与单链接，未测 HDBSCAN 等方法 |
| 实例重叠（第 8.6 节） | 以"repo 小写 + PR 编号"为实例键 | 键规则无法识别跨仓库迁移或重命名的同一任务；Scale-SWE 只比较 HF 公开部分 |
| Who&When Pro（第 9.8 节） | text split 6,257 条的字段与错误模式分布；任务数按 `task.query` 全文去重 | 错误为合成注入、单点且因果清晰；不含修复后成功的对照；text split 中没有 SWE 类轨迹 |
| 其他数据集（第 9.9 节） | 数据卡、HF API 与小样本 | SWE-chat 的标签取值分布未读取；data agent 方向只有模拟用户数据，OTel 格式轨迹每题 1 条 |

上述统计只读取所需的列或小样本，用于说明数据的可用性与量级；统计中没有运行记忆生成方法，结果只反映数据性质。

### 11.5 检索覆盖的局限

- 2026 年 10 月上旬的新论文多数只读了摘要页；ICLR 2027 投稿在检索时尚未被索引，相关工作可能遗漏。
- 部分官方页面不可访问：ChatGPT 记忆帮助中心与 Dreaming 公告、OpenAI 内部 data agent 博文返回 403，经官方 RSS、检索摘录与第三方转述核对；Cursor 旧 Memories 文档已跳转。
- 部分机制未公开：Claude Code memory extraction 的触发、prompt 与模型；产品的会话选取规则（Codex 除外）与记忆冲突的量化规则；产品内部的效果评估。
- 部分代码或数据未发布：WISE-Flow 无代码；VibeMemBench 代码与数据未发布；Agent KB 的构建流程未开源；AgenTracer 不发布 8B 权重；Supermemory、Memori 的抽取引擎闭源；Evo-Memory 代码未发布。
- 源码事实来自固定 commit 的只读阅读，未安装与运行，运行时行为（如调度、并发、实际 prompt 拼接结果）以代码逻辑推断。Claude Code 的内嵌提示词依据第三方镜像，并与 2.1.263 安装包逐字对照。
- 检索以 arXiv、官方文档、GitHub 与 Hugging Face 为主；非 arXiv 的会议论文与工业资料（过程挖掘、AIOps、软件工程、查询日志方向）部分经二手资料。

## 来源

论文按"arXiv 编号 名称"列出，链接为 `https://arxiv.org/abs/<编号>`；会议版本、代码仓库与数据集链接在对应条目或后续类别中给出。开源仓库与产品文档的读取日期为 2026-10-09，另注者除外。

### 经验记忆论文

- 2023–2024：2308.10144 ExpeL（prompt：https://github.com/LeapLabTHU/ExpeL ，`prompts/templates/human.py`）；2305.16291 Voyager。
- 2025：2501.10893 Learn-by-Interact；2506.07398 G-Memory；2507.06229 Agent KB；2507.23361 SWE-Exp；2508.16153 Memento；2509.12810 H²R；2509.25140 ReasoningBank（ICLR 2026，https://iclr.cc/virtual/2026/poster/10007887 ）；2510.04618 ACE（附录 prompt：https://arxiv.org/pdf/2510.04618 ）；2510.04851 LEGOMem；2512.10696 ReMe；2505.16067 记忆管理实证研究。
- 2026：2601.03192 MemRL；2601.08158 WISE-Flow；2601.22436 压缩经验的因果依赖研究；2605.28224 原子事实抽取研究；2608.22533 CONTRAMEM；2609.05837 AgentBrew；2609.11060 Grounding Agent Memory；2609.21423 DENSE；2609.21533 MACE；2609.32091 Memory as Middleware。

### 技能与整合

- 技能生成与演化：2607.24459 SciConsolidate。
- 技能生命周期与治理：2610.11781 Skill-V。
- 整合与合并：2609.33013 Epistemics of Agent Memory（ConsolidationBench）。
- 技能规模、风险与评测：2601.10338 Agent Skills in the Wild；2602.12670 SkillsBench；2605.24050 Skill Shadowing；2608.11888 Agent Skills Can Be Harmful。

### 开源仓库

仓库地址为 `https://github.com/<仓库>`，括注为读取的 commit 或版本。

- 记忆组件：AgentToolkit/altk-evolve 493c313（v1.6.1；评审制品说明 jayaramkr/Middleware-2026-artifact-evaluation）；kayba-ai/agentic-context-engine 3a31983（v0.13.0）；agentscope-ai/ReMe 084c02e（HEAD，v0.4.1.13）/ 554eec1（v0.2.0.6）；FlowLLM-AI/flowllm bd64f9c（v0.2.0.10）；thedotmack/claude-mem eccb15e（v13.34.2）；memodb-io/Acontext 259d73b；memodb-io/memobase 358c16b；Mirix-AI/MIRIX 8cb06a6；topoteretes/cognee 0ec7a9f（v1.6.3）；MemTensor/MemOS a7367d0（v2.0.34）；letta-ai/letta 5bcdd17（main）/ 56ba9c2（archive，0.16.8）；letta-ai/letta-code 8f20b78；NevaMind-AI/memU 718f6a9（v1.5.1）；agno-agi/agno 5f1fd0c；crewAIInc/crewAI 274fba6（旧版 d28daa2）；mem0ai/mem0 b7ad69a（Python 2.2.1）/ 144627c（v1.0.11）；langchain-ai/langmem 48e3c11（0.0.30）；langchain-ai/langgraph bfcfea5（`libs/checkpoint-postgres/langgraph/store/postgres/base.py`）；getzep/graphiti 1026ae7（v0.30.2）；plastic-labs/honcho cb8ab1a（v3.2.2）；supermemoryai/supermemory 02474bb；MemoriLabs/Memori 574b1ea（v3.3.6）；CaviraOSS/LongMemory 9ee2c8e；agiresearch/A-mem ceffb86；BAI-LAB/MemoryOS 587ed77。
- 论文配套代码：ag2ai/Agents_Failure_Attribution f4d2b6d；bingreeky/AgenTracer 256b19e；whowhenpro/whowhen_pro；YujunZhou/TRACE_exp；henrymao2004/agent-sycophancy（PASB）；ai-jiaqian/text-to-sql-memory-crystallization；MohammadAsadolahi/Reliable-Memory-Agents-in-the-Wild（Memory Reward Inflation）；MemTensor/HaluMem；princeton-nlp/ALCE；canvas-org/meta-agent（README）；AlibabaResearch/DAMO-ConvAI 下 VibeMemBench（截至 2026-10-09 仅占位 README）。
- 产品与 agent：openai/codex 82883da（2026-10-09，记忆流水线与 prompt 模板 `codex-rs/memories/write/templates/memories/`、`state/memory_migrations/`、`codex-rs/secrets/src/sanitizer.rs`；release rust-v0.155.0、0.156.0；PR #43797、#43799、#43800、#43808、#43813、#43827、#45956、#45960）与 2351d9e（2026-10-09，遥测埋点）；Piebald-AI/claude-code-system-prompts（第三方镜像，经 Claude Code 2.1.263 安装包对照）。
- 可观测与插桩：langfuse/langfuse d179469（2026-09-01，`packages/shared/src/server/otel/`、ClickHouse migrations）；Arize-ai/openinference（`spec/semantic_conventions.md`、`spec/configuration.md`）；openai/openai-agents-python（`docs/tracing.md`、`src/agents/run_config.py`）；langchain-ai/langsmith-sdk（`_otel_exporter.py`、`client.py`）；OpenHands/software-agent-sdk；lmnr-ai/lmnr-python；pydantic/logfire 84d0554（2026-10-08，`config.py`、`scrubbing.py`、`integrations/llm_providers/openai.py`）。
- 日志与工具：openai/openai-python（README、httpx2.md）；kiwicom/pytest-recording；instructor、outlines、guidance、xgrammar、vcrpy、respx 的 PyPI 元数据。

### 产品与平台文档

- OpenAI Codex：https://learn.chatgpt.com/docs/customization/memories ；https://learn.chatgpt.com/codex/changelog ；https://learn.chatgpt.com/docs/changelog ；Codex Skills https://learn.chatgpt.com/docs/build-skills
- Claude Code：https://code.claude.com/docs/en/memory ；https://code.claude.com/docs/en/sub-agents ；https://code.claude.com/docs/en/skills ；https://code.claude.com/docs/en/monitoring-usage ；https://code.claude.com/docs/llms-full.txt ；CHANGELOG https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md ；Auto Dream 第三方说明（二手资料）https://claudefa.st/blog/guide/mechanics/auto-dream 、https://www.mindstudio.ai/blog/what-is-claude-code-autodream-memory-consolidation ；/insights 第三方说明 https://blog.vincentqiao.com/en/posts/claude-code-insights/
- Anthropic 平台：https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool ；https://platform.claude.com/docs/en/managed-agents/memory ；https://platform.claude.com/docs/en/managed-agents/dreams ；https://platform.claude.com/docs/en/release-notes/overview ；长上下文提示 https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/long-context-tips ；Agent Skills 工程博客 https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills
- Claude.ai：https://support.claude.com/en/articles/11817273 ；https://claude.com/blog/claudes-memory-works-everywhere-and-you-decide-whats-in-it
- GitHub Copilot：https://docs.github.com/en/copilot/concepts/agents/copilot-memory ；https://github.blog/ai-and-ml/github-copilot/building-an-agentic-memory-system-for-github-copilot/ ；https://github.blog/changelog/ （2026-08-11、2026-09-25 条目）；https://code.visualstudio.com/docs/copilot/agents/memory
- Cursor：https://cursor.com/changelog/1-0 ；https://cursor.com/changelog/1-2 ；https://cursor.com/changelog ；https://cursor.com/blog/bugbot-learning ；https://forum.cursor.com/t/are-my-memories-gone/144057
- Windsurf 与 Devin：https://docs.devin.ai/desktop/cascade/memories ；https://docs.devin.ai/product-guides/knowledge ；https://docs.devin.ai/release-notes
- Letta：https://docs.letta.com/letta-code/memory ；https://docs.letta.com/reference/changelog ；Skill Learning 博客 https://www.letta.com/blog/skill-learning/
- ChatGPT：https://openai.com/news/rss.xml （"Dreaming: Better memory for a more helpful ChatGPT"，2026-06-04）；https://openai.com/index/chatgpt-memory-dreaming （403，经 RSS 与检索摘录核对）；https://help.openai.com/en/articles/8590148-memory-faq （403）；第三方转述 https://gigazine.net/gsc_news/en/20260605-chatgpt-memory-dreaming-v3
- 其他开源组件文档：Zep 概念文档 https://help.getzep.com/concepts ；memU v1.5.1 README；LongMemory v1.2.3 README；Honcho https://docs.honcho.dev 、https://blog.plasticlabs.ai
- 数据产品：OpenAI 内部 data agent https://openai.com/index/inside-our-in-house-data-agent/ （403；转述 https://www.zenml.io/llmops-database/building-a-production-data-agent-for-90000-tables-at-scale 、https://blog.bytebytego.com/p/how-openai-built-its-data-agent ）
- trace 平台：Braintrust https://www.braintrust.dev/docs/observe/topics 、https://www.braintrust.dev/blog/topics 、https://www.braintrust.dev/docs/observe/patterns 、https://braintrust.dev/docs/instrument/user-feedback ；PostHog https://posthog.com/blog/llm-analytics-clustering-how-it-works 、https://posthog.com/docs/llm-analytics/clusters ；LangSmith https://docs.langchain.com/langsmith/insights 、https://langchain.com/blog/how-we-built-langsmith-engine-our-agent-for-improving-agents 、https://www.langchain.com/blog/new-in-langsmith-engine-2x-better-issue-detection 、https://www.langchain.com/blog/langsmith-engine-v2-redteam 、https://docs.langchain.com/langsmith/feedback-data-format ；Langfuse https://langfuse.com/blog/2025-08-29-error-analysis-to-evaluate-llm-applications ；Raindrop https://www.ycombinator.com/launches/Nn7-raindrop-deep-search 、https://www.raindrop.ai/docs/platform/signals
- 模型服务：DeepSeek https://api-docs.deepseek.com/quick_start/pricing 、https://api-docs.deepseek.com/guides/json_mode 、https://api-docs.deepseek.com/guides/thinking_mode 、https://api-docs.deepseek.com/guides/tool_calls 、https://api-docs.deepseek.com/guides/kv_cache 、https://api-docs.deepseek.com/quick_start/rate_limit ；阿里云百炼 https://www.alibabacloud.com/help/en/model-studio/qwen-structured-output 、https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions 、https://www.alibabacloud.com/help/en/model-studio/context-cache 、https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai 、https://www.alibabacloud.com/help/en/model-studio/qwen3-6-plus 、https://www.alibabacloud.com/help/en/model-studio/qwen3-6-max ；MiniMax https://platform.minimax.io/docs/api-reference/text-openai-api ；Qwen3.5-397B-A17B 模型卡 https://huggingface.co/Qwen/Qwen3.5-397B-A17B ；vLLM https://docs.vllm.ai/en/latest/features/structured_outputs.html 、https://docs.vllm.ai/en/latest/usage/reproducibility.html ；SGLang https://docs.sglang.io/advanced_features/structured_outputs.html 、https://docs.sglang.io/advanced_features/structured_outputs_for_reasoning_models.html ；OpenRouter https://openrouter.ai/docs/features/structured-outputs ；OpenAI https://developers.openai.com/api/docs/guides/structured-outputs 、https://developers.openai.com/cookbook/examples/reproducible_outputs_with_the_seed_parameter ；instructor https://python.useinstructor.com/concepts/retrying/ ；outlines https://dottxt-ai.github.io/outlines/latest/features/models/openai_compatible/ ；Chroma Context Rot https://www.trychroma.com/research/context-rot

### 失败归因与评测

- 失败归因：2505.00212 Who&When；2509.03312 AgenTracer（ICLR 2026）；2604.22708 TraceElephant。
- 结果判定与 judge：2410.10934 Agent-as-a-Judge；2504.08942 AgentRewardBench；2504.07164 R2E-Gym；2607.07946 DeepSWE；2512.21919 SWE-RM；2511.08325 AgentPRM；2601.12294 ToolPRMBench；2608.00017 Memory Reward Inflation。
- 记忆效用与信用分配：2603.01966 AMemGym；2604.27283 基于 contextual bandit 的记忆检索；2604.14004 Memory Transfer Learning；2607.29658 STAIR。
- 对话记忆基准：2402.17753 LoCoMo；2410.10813 LongMemEval（ICLR 2025）；2506.21605 MemBench（ACL 2025 Findings）；2507.05257 MemoryAgentBench；2511.03506 HaluMem。
- 经验与程序性记忆基准：2511.20857 Evo-Memory；2602.16313 MemoryArena；2511.21730 程序性记忆检索基准；2602.08316 SWE Context Bench；2607.05202 EvoAgentBench；2609.23570 VibeMemBench；2608.20664 DreamBench-SWE；2609.24663 EvoPathBench；2609.24971 DolphinBench；2609.24259 MemCalib；2606.23127 AFTER；2602.22769 AMA-Bench；2603.03296 PlugMem；2608.05778 Prompt-side Playbooks。
- 引用与长上下文：2305.14627 ALCE；2307.03172 Lost in the Middle；2409.02897 LongCite；2504.15629 CiteFix；2510.20303 Citation Failure；2602.06718 GhostCite；2408.02442 Let Me Speak Freely?（格式约束对推理的影响）。
- 榜单：SWE-bench 榜单 https://raw.githubusercontent.com/SWE-bench/swe-bench.github.io/master/data/leaderboards.json

### 用户偏好

- 偏好记忆方法：2606.13174 TRACE；2607.10526 PASB；2609.02265 CAPTURE。
- 偏好基准：2502.09328 Copilot Arena。

### data agent

- text-to-SQL：2602.13521 Tk-Boost；2608.07213 Crystallization；2601.15709 AgentSM。
- 标注错误与评测：2601.08778 text-to-SQL 基准标注错误；2606.05661 Continual Learning Bench。

### 存储、溯源与安全

- 存储与生命周期：2507.03724 MemOS；2501.13956 Zep / Graphiti；2504.19413 Mem0；2502.12110 A-MEM；2507.07957 MIRIX；2305.10250 MemoryBank；2601.18642 FadeMem；2603.11768 SSGM。
- 溯源：2602.17913 TierMem；2605.30771 Eywa；2605.14421 MemLineage；2605.08374 MemQ。
- 删除、撤销与冲突：2605.07242 MEMOREPAIR；2602.17692 Agentic Unlearning（SBU）；2609.04875 Execution-State Unlearning；2606.15903 Control-Plane Placement Shapes Forgetting；2609.08258 Revoked but Still Authoritative；2608.07429 TEPA；2608.25553 When Stale Constraints Go Unchecked；2605.06527 STALE。
- 访问控制与泄漏：2606.18829 GateMem；2610.04195 MemLeak；2608.17148 Authorization Before Context；2607.23444 SPORE；2608.01679 AuthMem-Bench；2606.24322 TMA-NM。
- 投毒、注入与安全偏置：2407.12784 AgentPoison；2503.03704 MINJA；2510.02373 A-MemGuard；2605.15338 Sleeper Memory Poisoning；2605.08442 Injection-Execution Dissociation；2608.05563 PoisonedEvolution；2403.14720 Spotlighting。
- 规范与数据库：W3C PROV-O https://www.w3.org/TR/prov-o/ ；OpenLineage 对象模型 https://openlineage.io/docs/spec/object-model ；PostgreSQL 18 发布公告 https://www.postgresql.org/about/news/postgresql-18-released-3142/ 与 CREATE TABLE 文档 https://www.postgresql.org/docs/18/sql-createtable.html ；pgvector https://github.com/pgvector/pgvector ；pgvectorscale https://github.com/timescale/pgvectorscale ；VectorChord https://github.com/tensorchord/VectorChord ；Apache AGE https://github.com/apache/age ；ParadeDB https://github.com/paradedb/paradedb

### trace 预处理与规范

- 渲染、压缩与切分：2508.21433 The Complexity Trap；2509.23586 AgentDiet；2510.00615 ACON；2601.16746 SWE-Pruner；2508.03341 Nemori；2502.05589 SeCom。
- trace 挖掘、表示与相似度：2412.13678 Clio。
- OpenTelemetry：semantic-conventions-genai commit 06ec68e（2026-10-07，`model/gen-ai/{registry.yaml, spans.yaml, events.yaml, entities.yaml, gen-ai-input-messages.json, gen-ai-memory-records.json}`、`docs/gen-ai/gen-ai-spans.md`、`docs/gen-ai/non-normative/examples-llm-calls.md`）https://github.com/open-telemetry/semantic-conventions-genai ；semantic-conventions v1.44.0 与 main（`model/{session, user, enduser, vcs, process}/registry.yaml`）https://github.com/open-telemetry/semantic-conventions ；Go semconv v1.41.0（`go.opentelemetry.io/otel@v1.44.0/semconv/v1.41.0/attribute_group.go`）
- 聚类与稳定性工具：HDBSCAN 预测 https://hdbscan.readthedocs.io/en/latest/prediction_tutorial.html 

### 相邻方向

- 过程挖掘：2505.20127 Agentic AI Process Observability
- AIOps 与案例推理：2608.25661 OpsHarness
- trace 驱动的 prompt 与程序优化：2508.03680 Agent Lightning；2603.28052 Meta-Harness
- 参数化固化：2603.18272 ExpRAG 与 LoRA 对比；2508.09874 Memory Decoder；2510.15103 Sparse Memory Finetuning；2412.09764 Memory Layers at Scale；2607.07847 持续学习方法对比。
- 软件工程经验挖掘：2507.15003 AIDev；2605.22534 Peralta 等，agent PR 合并与拒绝研究

### 综述

- 2512.13564 Hu 等，Memory in the Age of AI Agents
- 2605.06716 From Storage to Experience
- 2606.04990 From Agent Traces to Trust
- 2607.10113 Dynamic Agent Skills: A Lifecycle Survey（TMLR）
- 2608.03392 Self-Evolving Coding Agents

### 数据集

- 编码 agent 轨迹与基准：2606.16038 Open-SWE-Traces https://huggingface.co/datasets/nvidia/Open-SWE-Traces ；2609.06780 git hacking 轨迹过滤；2602.23866 SWE-rebench V2 https://huggingface.co/datasets/nebius/SWE-rebench-V2 ；2505.20411 SWE-rebench https://huggingface.co/datasets/nebius/SWE-rebench 与 leaderboard https://huggingface.co/datasets/nebius/SWE-rebench-leaderboard ；2602.09892 Scale-SWE https://huggingface.co/datasets/AweAI-Team/Scale-SWE ；SWE-bench Verified / Lite https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified 、https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite ；2412.21139 SWE-Gym https://huggingface.co/datasets/SWE-Gym/SWE-Gym
- 失败归因：2607.09996 Who&When Pro https://huggingface.co/datasets/Leoxx/whowhen_pro
- 用户会话与反馈：2604.20779 SWE-chat https://huggingface.co/datasets/SALT-NLP/SWE-chat ；OpenHands feedback https://huggingface.co/datasets/OpenHands/openhands-feedback ；trace-commons/agent-traces https://huggingface.co/datasets/trace-commons/agent-traces ；2408.15549 WildFeedback https://huggingface.co/datasets/microsoft/WildFeedback ；peteromallet/my-dataclaw-data
- data agent：wmo-dabstep-traces https://huggingface.co/datasets/experiential-labs/wmo-dabstep-traces ；2506.23719 DABstep https://huggingface.co/datasets/adyen/DABstep ；2410.07331 DA-Code https://github.com/yiyihum/da-code 与评测结果 https://huggingface.co/datasets/analytics-agents-uncertainty/da-code-evaluation-results ；2403.05307 Tapilot-Crossing https://huggingface.co/datasets/birdsql/tapilot-crossing ；2510.05318 BIRD-Interact https://github.com/bird-bench/BIRD-Interact （birdsql/bird-interact-lite、bird-interact-full、mini-interact）；BIRD https://bird-bench.github.io/ 、https://github.com/bird-bench/mini_dev ；2411.07763 Spider 2.0 https://github.com/xlang-ai/Spider2 （xlangai/spider2-lite）；AMA-Bench https://ama-bench.github.io/
