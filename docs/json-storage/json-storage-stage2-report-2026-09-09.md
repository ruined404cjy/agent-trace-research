# Agent Trace JSON 存储阶段二报告：场景化比较

> 实验完成日期：2026-09-09；文档修订日期：2026-09-09
>
> 状态：原六结构实验与补充四结构实验均完成正确性检查
>
> 数据路径：独立载入程序
>
> 数据库版本：openGauss 6.0.0 build `aee4abd5`；ClickHouse 25.12.11.4

配套的[JSON 存储原理](json-storage-principles-2026-09-09.md)按写入、物理存储、后台维护和查询顺序解释 JSONB 与 Native JSON；本文集中说明实验设计、数据结果和场景结论。

## 1. 结论与适用范围

阶段二在同一份 48,534 行 Agent Trace 数据上比较 openGauss JSON、openGauss JSONB、ClickHouse String JSON 和 ClickHouse Native JSON，并补充 openGauss 索引、ClickHouse Map、type hint、data part、merge 和 Sidecar 机制实验。

本阶段的主要结果为：

| 场景 | openGauss 引擎内结论 | ClickHouse 引擎内结论 |
|---|---|---|
| 基础载入 | JSON 为 **6,322 rows/s**，高于 JSONB 的 6,134 rows/s | String JSON 为 **5,754 rows/s**，高于 Native JSON 的 5,106 rows/s |
| 稳定列聚合 | JSON 与 JSONB 接近；查询不读取 JSON | String JSON 与 Native JSON 接近；查询不读取 JSON |
| 高频路径过滤 | 基础 S02：JSONB 为 **96.45 ms**，JSON 为 281.72 ms；独立 Q02：表达式索引为 **20.59 ms**，JSONB 为 112.49 ms | 基础 S02：Native JSON 为 **91.91 ms**，String JSON 为 121.78 ms |
| 低密度路径过滤 | 基础 S03：JSONB 为 **88.10 ms**，JSON 为 271.10 ms；独立 Q05：GIN 为 **3.84 ms**，JSONB 为 88.77 ms | 基础 S03：Native JSON 为 **47.60 ms**，String JSON 为 68.60 ms |
| 单路径投影 | JSONB 为 **221.97 ms**，低于 JSON 的 389.15 ms | String JSON 与 Native JSON 的端到端中位数接近，分别为 172.42 和 173.40 ms |
| 完整 Trace | JSON 与 JSONB 接近 | String JSON 低于 Native JSON |
| 完整文档分页 | JSON 为 **197.21 ms**，低于 JSONB 的 459.78 ms | String JSON 为 **48.21 ms**，低于 Native JSON 的 77.53 ms |

上述数字均为对应实验组的 p50 或载入中位结果。两批实验的查询、轮次和计时边界不同，只在各自表内比较。跨引擎数字描述固定软硬件和访问路径下的端到端结果，同时包含行存/列存、排序键、索引、协议和后台维护的影响，不能解释为 JSON 表示本身的单因素差距。

所有结果来自单机固定版本、热查询和独立载入程序。实验未覆盖 Collector、exporter、benchmark 的完整链路，也未覆盖饱和吞吐、冷缓存、多节点和故障恢复。长 payload、Full/Core、独立 payload 表和 asset reference 由[阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)负责。

## 2. 数据、结构和比较契约

### 2.1 数据来源与特征

数据源是 Hugging Face `Leoxx/whowhen_pro` 的 text split。源数据的 6,257 条失败轨迹记录经 `trace-synthesis` 投影为 6,257 条 Trace 和 48,534 个 Span。投影按 trajectory 事件生成 Trace 根 Span、LLM Span 和 Tool Span，并保留任务、framework、benchmark、failure ground truth 和事件内容。

该投影由输入、配置和 `seed=42` 共同确定。Trace ID 根据 split 和源记录 ID 使用 UUID5 派生，Span ID 根据 Trace ID 和固定局部名称派生；时间、token、部署环境和 telemetry dialect 等补充字段使用 seed 与 ID 的哈希计算。相同输入、配置和 seed 生成相同的行序、标识和字段值。阶段二生成器抽取稳定列，把点分隔的 Attribute key 可逆投影为嵌套 JSON，并生成 canonical 内容、原始事件 bytes 和预先计算的正确结果。

固定输入包含 48,534 个 Span、6,257 个 Trace 和 29 个顶层 Attribute key。每行 Attribute key 数的 p50/p95/p99/最大值为 8/14/15/15。输入按 256 行分块，共 190 个 block，末 block 为 150 行。

| 数据特征 | 结果 |
|---|---|
| Span 类型 | llm 17,486；tool 24,791；trace 6,257 |
| 路径密度 | `span.type` 100%；`gen_ai.operation.name` 87.11%；`prompt_text` 3.96% |
| attributes canonical UTF-8 长度 | p50 1,042；p95 4,663；p99 7,697；最大 62,197 bytes |
| raw event UTF-8 长度 | p50 1,480；p95 5,087；p99 8,121；最大 62,626 bytes |
| 混合类型 | `failure.mistake_agent` 和 `failure.mistake_step` 均含 JSON null 与普通值 |

时间戳从 2030-01-01 起确定性构造，只用于固定时间窗口，不代表生产到达时间。当前输入只覆盖 text split。tenant、真实 project 和 instrumentation scope 在源数据中不可观测。29 个顶层 Attribute key 代表较窄样本，不能支撑 5,000 路径的生产分布假设。

### 2.2 四种基础结构

| 存储结构 | 动态属性表示 | 字段访问 | 完整文档恢复 |
|---|---|---|---|
| openGauss JSON | JSON 文本 datum | JSON 路径操作符解析文本 | 读取文本表示 |
| openGauss JSONB | 二进制文档 datum | 遍历文档容器，可使用表达式 B-tree 或 GIN | 遍历文档树并序列化 |
| ClickHouse String JSON | 压缩 String 列 | JSON 提取函数解析字符串 | 读取字符串 |
| ClickHouse Native JSON | data part 内的 type hint、dynamic path 和 shared data | 读取相应路径的列式表示 | 汇集路径，并按需使用 Sidecar 补齐 |

四种结构使用相同行序、稳定列、动态属性内容和查询语义。ClickHouse Native JSON 设置 `max_dynamic_paths=32`。基础四结构不创建表达式索引、GIN、type hint、投影或物化热点列；这些优化只进入机制扩展实验。

各结构的完整文档结果按 canonical JSON 比较。canonical JSON 是输入解析后按确定规则重新序列化的逻辑文档，保留键值、类型和数组顺序。字节级恢复统一读取首次解析前保存的原始 UTF-8 bytes，因此不依赖 JSON、JSONB 或 Native JSON 是否保留空白、键顺序和原始数值写法。

ClickHouse Native JSON 的稀疏 Sidecar 保存包含 JSON null、空对象或空数组等特殊值的 canonical 属性值。Sidecar 与 Native JSON 一起承担逻辑文档恢复，相关写入、空间、返回和客户端合并成本均纳入对应实验。

除 3.5 的 Sidecar 机制观察外，本文场景表中的“ClickHouse Native JSON”均指 Native JSON 与稀疏 Sidecar 组成的可恢复结构。

### 2.3 查询和计时契约

| 场景 | 访问内容 | 结果契约 |
|---|---|---|
| S01 稳定列聚合 | 时间、项目和 Span 类型 | 分组计数一致 |
| S02 高频路径 | `gen_ai.operation.name` | 命中计数与分组结果一致 |
| S03 低密度路径 | `failure.mistake_mode` | 命中计数与排序身份摘要一致 |
| S04 单路径投影 | `gen_ai.output.messages` | 非空数量、规范化值和 UTF-8 字节数一致 |
| S05 完整 Trace | 固定 `trace_id` 的 6 个 Span | 排序身份和完整 canonical attributes 一致 |
| S06 完整文档分页 | 时间窗口内固定 256 行页面 | 排序身份和完整 canonical attributes 一致 |

补充四结构的端到端延迟从已建立连接提交语句开始，到响应 bytes 完整读取结束。结果规范化、Sidecar 合并、canonical 序列化、哈希和正确性核对另记为客户端恢复时间。

载入计时从提交预生成 block 开始，到分析表和原文表均写入成功且可见结束。计时不含客户端预处理、openGauss `ANALYZE` 或 ClickHouse merge 等待，因此表示固定批次下的端到端载入能力，不表示纯 JSON 转换成本或系统饱和吞吐。

### 2.4 实验证据

| 实验 | 存储结构与样本 | 回答的问题 |
|---|---|---|
| 原六结构实验 | openGauss JSONB、JSONB + 表达式 B-tree、JSONB + GIN；ClickHouse String JSON、Map、Native JSON；各三轮 | 索引、Map、持续写入和并发查询 |
| 补充四结构实验 | openGauss JSON、JSONB；ClickHouse String JSON、Native JSON + 稀疏 Sidecar；各四轮 | 四种基础表示的载入和场景化比较 |
| openGauss 机制观察 | 五种结构各一次 | JSON/JSONB、表达式索引、GIN 和完整文档 |
| ClickHouse 机制观察 | 四种 Native JSON 结构各一次 | Sidecar、type hint、data part、merge 和两种 FINAL |

补充四结构每种结构执行四轮，每个 p50/p95 先在单轮计算，再取四轮中位数。原六结构每种结构执行三轮，表中为三轮中位数。两组实验使用同一输入，但查询 SQL、轮次和计时边界不同，不能计算跨批次变化比例。

## 3. 原理与机制观察

完整的写入、物理存储、后台维护和查询流程见[JSON 存储原理](json-storage-principles-2026-09-09.md)。本节只保留理解实验对象所需的机制摘要和实测结果。

### 3.1 两种结构化 JSON 的流程差异

![openGauss JSON 与 JSONB 处理流程](./assets/json-storage-opengauss-jsonb-flow.svg)

openGauss JSONB 在每行内保存解析后的二进制文档树。写入时完成解析、对象键排序和二进制序列化；字段查询逐层遍历容器，完整读取遍历整棵文档树。大值可由 TOAST 压缩或移出 heap 主行。表达式 B-tree 与 GIN 在写入事务中同步维护，只用于候选行筛选。

![ClickHouse String JSON 与 ClickHouse Native JSON 处理流程](./assets/json-storage-clickhouse-native-json-flow.svg)

ClickHouse MergeTree 表由若干 data part 组成。每次 INSERT 通常生成新的不可变 data part；后台 merge 将同一 partition 中的若干 data part 合并成新的 data part。Native JSON 在每个 data part 内把路径保存为 type hint 子列、dynamic path 或 shared data。`max_dynamic_paths` 限制单个 data part 中可独立保存的未声明路径数量；merge 生成目标 data part 时重新选择 dynamic/shared 路径，通常优先保留非 null 值较多的路径。

shared data 属于一个 data part 中的 JSON 列，不是独立表。路径在不同 data part 中可以具有不同物理归属，查询执行器按每个 data part 的实际表示读取。后台 merge 通常不持有阻塞普通查询的表级锁，但会竞争 CPU、内存和磁盘 I/O。

`OPTIMIZE TABLE ... FINAL` 强制尝试物理合并 data part，并随目标 data part 的生成重新组织路径。`SELECT ... FINAL` 在查询时应用 ReplacingMergeTree 等表引擎的最终行语义，不改写 data part，也不负责 dynamic path 重组。

### 3.2 openGauss 机制观察

载入与空间：

| 结构 | COPY 与索引维护 | 总空间 |
|---|---:|---:|
| openGauss JSON | 4,879.252 ms | 73,089,024 bytes |
| openGauss JSON + 表达式索引 | 5,479.584 ms | 75,415,552 bytes |
| openGauss JSONB | 4,905.363 ms | 77,193,216 bytes |
| openGauss JSONB + GIN | 6,022.325 ms | 85,204,992 bytes |
| openGauss JSONB + 表达式索引 | 5,127.186 ms | 79,536,128 bytes |

查询观察：

| 结构 | 复合探针：服务端 / 端到端 | 完整文档：服务端返回 / 客户端恢复 |
|---|---:|---:|
| openGauss JSON | 13.347 / 15.726 ms | 781.965 / 5,486.643 ms |
| openGauss JSON + 表达式索引 | 12.671 / 14.312 ms | 615.157 / 5,774.496 ms |
| openGauss JSONB | 15.838 / 18.843 ms | 1,061.466 / 5,436.239 ms |
| openGauss JSONB + GIN | 16.921 / 19.081 ms | 1,532.748 / 6,082.003 ms |
| openGauss JSONB + 表达式索引 | 11.086 / 13.646 ms | 1,085.153 / 6,012.375 ms |

数据特征为固定 48,534 行输入和一次机制运行。空间包含 heap、TOAST 和索引的分配字节。表达式索引和 GIN 增加写入维护与空间，只有匹配索引的查询能够获得筛选收益。

复合探针覆盖全表扫描、`ingest_seq` 过滤、`attributes::jsonb` 转换和结果生成，是组合指标。客户端端到端时间还包含协议与客户端处理。该单次观察不进入四轮基础结构性能统计。

### 3.3 ClickHouse data part、merge 与 type hint

机制实验将 48,534 行分为 190 个 block 写入，并暂停表级后台 merge，以观察由 INSERT 直接生成的 zero-level data part；随后恢复后台 merge，最后执行一次 `OPTIMIZE TABLE ... FINAL`。

data part 数量和最终路径：

| Native JSON 结构 | INSERT 后 data part | 后台 merge 稳定后 | FINAL 后 | FINAL 后 dynamic / shared / hinted |
|---|---:|---:|---:|---:|
| 无 Sidecar | 190 | 5 | 1 | 32 / 8 / 0 |
| 稀疏 Sidecar | 190 | 3 | 1 | 32 / 8 / 0 |
| 完整 Sidecar | 190 | 4 | 1 | 32 / 8 / 0 |
| type hint + 稀疏 Sidecar | 190 | 3 | 1 | 32 / 6 / 2 |

路径归属随 merge 的变化：

| Native JSON 结构 | INSERT 后 dynamic / shared / hinted | 后台 merge 稳定后 | FINAL 后 |
|---|---:|---:|---:|
| 无 Sidecar | 38 / 13 / 0 | 35 / 9 / 0 | 32 / 8 / 0 |
| 稀疏 Sidecar | 38 / 13 / 0 | 34 / 7 / 0 | 32 / 8 / 0 |
| 完整 Sidecar | 38 / 13 / 0 | 35 / 9 / 0 | 32 / 8 / 0 |
| type hint + 稀疏 Sidecar | 38 / 3 / 2 | 34 / 5 / 2 | 32 / 6 / 2 |

active data part 压缩空间：

| Native JSON 结构 | INSERT 后 | 后台 merge 稳定后 | FINAL 后 |
|---|---:|---:|---:|
| 无 Sidecar | 31.70 MiB | 27.78 MiB | 27.60 MiB |
| 稀疏 Sidecar | 39.86 MiB | 35.93 MiB | 35.82 MiB |
| 完整 Sidecar | 44.76 MiB | 40.18 MiB | 40.06 MiB |
| type hint + 稀疏 Sidecar | 39.83 MiB | 35.91 MiB | 35.81 MiB |

各 zero-level data part 独立选择路径，因此 INSERT 完成时全表 dynamic path 并集为 38，超过单个 data part 的 32 条上限。后台 merge 和强制合并改变 data part 数量及路径物理归属。最终 type hint 结构包含 **32 dynamic、6 shared 和 2 hinted**，说明本实验版本中 hinted 路径未占 32 个 dynamic path 名额。

压缩空间只描述 ClickHouse active data part，不能与 openGauss 分配空间计算跨引擎压缩率。

### 3.4 两种 FINAL 的语义观察

| 时点 | active data part | 普通读取版本 | `SELECT ... FINAL` 版本 |
|---|---:|---|---|
| `OPTIMIZE ... FINAL` 前 | 2 | `[1,2]` | `[2]` |
| `OPTIMIZE ... FINAL` 后 | 1 | `[2]` | `[2]` |

小型 ReplacingMergeTree 探针表明，`SELECT ... FINAL` 在读取时消解跨 data part 版本，`OPTIMIZE TABLE ... FINAL` 将两个 active data part 物理重写为一个。强制合并耗时 4.565 ms，只用于验证语义，不构成生产环境性能基准。

### 3.5 Native JSON 与 Sidecar

![ClickHouse Native JSON 与 Sidecar 恢复流程](./assets/json-storage-native-json-sidecar-flow.svg)

Native JSON 的叶路径表示将 JSON null 与路径缺失视为等价，并可能省略没有叶路径的空对象。阶段二逻辑文档恢复先读取 Native JSON，再以稀疏 Sidecar 覆盖特殊值；type hint 路径使用 presence marker 区分缺失和值等于类型默认值。字节级恢复读取首次解析前保存的原始 UTF-8 bytes。

| 方案 | Sidecar 条目 / UTF-8 bytes | presence marker 条目 / bytes | 客户端全语料恢复 | 差异行 |
|---|---:|---:|---:|---:|
| 无 Sidecar | 0 / 0 | 0 / 0 | 45.000 ms | **5,456** |
| 稀疏 Sidecar | 66,613 / 26,698,849 | 48,534 / 1,607,879 | 4,721.643 ms | **0** |
| 完整 canonical Sidecar | 整列 / 81,634,867 | 0 / 0 | 2,180.746 ms | **0** |
| type hint + 稀疏 Sidecar | 66,613 / 26,698,849 | 48,534 / 1,607,879 | 5,076.783 ms | **0** |

无 Sidecar 结构的 5,456 条差异来自 5,446 个 JSON null 和 10 个空对象；普通值内容逐行检查通过，空数组差异为 0。稀疏 Sidecar 规则仍保存递归包含 JSON null、空对象或空数组的 Attribute，使恢复契约不依赖当前样本恰好没有空数组差异。

表中恢复时间是一次客户端全语料机制观察，不是查询延迟。稀疏 Sidecar 的 entries 和 bytes 已包含 marker。canonical Sidecar 保存逻辑键值、数组顺序和空容器，不保存原始排版，也不是 RFC 8785 JCS。

## 4. 场景化结果与分析

### 4.1 基础载入与空间

四种基础结构按相同行序写入 190 个 block，每个 block 同时写分析表和原文表。计时不含客户端预处理、openGauss `ANALYZE` 或 ClickHouse merge 等待。

基础四结构结果：

| 存储结构 | 载入 rows/s | 分析数据 MiB | 原文 MiB | 合计 MiB |
|---|---:|---:|---:|---:|
| openGauss JSON | **6,322.38** | 69.695 | 83.281 | 152.977 |
| openGauss JSONB | 6,134.30 | 73.641 | 83.281 | 156.922 |
| ClickHouse String JSON | **5,754.01** | 15.352 | 17.653 | 33.005 |
| ClickHouse Native JSON | 5,105.91 | 35.840 | 17.653 | 53.494 |

扩展结构结果：

| 存储结构 | 载入 rows/s | 分析 / 原文 / 合计 MiB |
|---|---:|---:|
| openGauss JSONB | **4,747** | 73.64 / 83.28 / 156.92 |
| openGauss JSONB + 表达式索引 | 4,638 | 75.88 / 83.28 / 159.16 |
| openGauss JSONB + GIN | 4,523 | 81.28 / 83.28 / 164.56 |
| ClickHouse String JSON | **5,860** | 15.35 / 17.65 / 33.01 |
| ClickHouse Map | 5,482 | 24.99 / 17.65 / 42.65 |
| ClickHouse Native JSON + 稀疏 Sidecar | 3,688 | 27.69 / 17.65 / 45.34 |

**结论：openGauss JSON 和 ClickHouse String JSON 在各自引擎的基础结构中载入较高。**JSONB 构造、Native JSON 路径组织、索引和 Sidecar 都增加写入或空间成本。

openGauss 数值为 heap、TOAST 和索引的已分配字节；ClickHouse 数值为 active data part 压缩后字节。空间结果只作引擎内说明。

### 4.2 稳定列聚合

S01 在固定半程时间窗口读取 27,561 行，只按强类型 `span_type` 分组。Q01 和 Q03 同样只读取稳定列，分别返回分组计数和 `count + sum(duration_ms)`。该控制场景不访问动态属性。
基础四结构 S01：

| 存储结构 | p50 / p95 |
|---|---:|
| openGauss JSON | 24.82 / 28.83 ms |
| openGauss JSONB | **24.62 / 27.41 ms** |
| ClickHouse String JSON | **91.14 / 96.03 ms** |
| ClickHouse Native JSON | 91.71 / 96.07 ms |

扩展结构 Q01 / Q03：

| 存储结构 | Q01 p50 | Q03 p50 |
|---|---:|---:|
| openGauss JSONB | 26.93 ms | **32.26 ms** |
| openGauss JSONB + 表达式索引 | **26.89 ms** | 32.42 ms |
| openGauss JSONB + GIN | 26.96 ms | 32.37 ms |
| ClickHouse String JSON | **7.88 ms** | 9.14 ms |
| ClickHouse Map | 7.91 ms | **8.55 ms** |
| ClickHouse Native JSON + 稀疏 Sidecar | 8.12 ms | 8.69 ms |

**结论：同一引擎中的不同 JSON 表示接近，符合查询不读取 JSON 的设计。**跨实验组的绝对时间不同，反映查询 SQL 和计时路径差异。该场景不能判断 JSON 表示本身的优劣。

### 4.3 高频路径过滤

S02 和 Q02 读取 `gen_ai.operation.name`，筛选 `execute_tool` 后按稳定列分组。固定窗口内命中 **20,155 行**，属于高频、高命中量路径，最终只返回一行聚合。

基础四结构 S02：

| 存储结构 | p50 / p95 |
|---|---:|
| openGauss JSON | 281.72 / 297.36 ms |
| openGauss JSONB | **96.45 / 104.02 ms** |
| ClickHouse String JSON | 121.78 / 128.32 ms |
| ClickHouse Native JSON | **91.91 / 98.30 ms** |

扩展结构 Q02：

| 存储结构 | p50 |
|---|---:|
| openGauss JSONB | 112.49 ms |
| openGauss JSONB + 表达式索引 | **20.59 ms** |
| openGauss JSONB + GIN | 110.97 ms |
| ClickHouse String JSON | 85.31 ms |
| ClickHouse Map | 51.34 ms |
| ClickHouse Native JSON + 稀疏 Sidecar | **9.83 ms** |

**结论：openGauss JSONB 和 ClickHouse Native JSON 均降低了同引擎文本表示的路径过滤延迟。**表达式 B-tree 进一步减少 openGauss 候选行；GIN 与本场景的 `#>> =` 谓词不匹配，因此没有改善。

### 4.4 低密度路径过滤

S03 和 Q05 筛选 `failure.mistake_mode=A.3`。固定窗口内命中 **741 行**，返回计数和排序身份摘要，避免大量明细传输影响计时。S03 使用各基础类型的自然路径谓词；Q05 使用可与 `jsonb_hash_ops` GIN 匹配的包含谓词。

基础四结构 S03：

| 存储结构 | p50 / p95 |
|---|---:|
| openGauss JSON | 271.10 / 284.71 ms |
| openGauss JSONB | **88.10 / 94.33 ms** |
| ClickHouse String JSON | 68.60 / 76.25 ms |
| ClickHouse Native JSON | **47.60 / 54.60 ms** |

扩展结构 Q05：

| 存储结构 | p50 |
|---|---:|
| openGauss JSONB | 88.77 ms |
| openGauss JSONB + 表达式索引 | 88.67 ms |
| openGauss JSONB + GIN | **3.84 ms** |
| ClickHouse String JSON | 69.58 ms |
| ClickHouse Map | 35.11 ms |
| ClickHouse Native JSON + 稀疏 Sidecar | **7.57 ms** |

低命中率本身不会减少需要检查的候选行。文本表示仍需在时间窗口候选集上解析路径；JSONB 和 Native JSON 可读取结构化路径。**匹配谓词的 GIN 与 Native JSON 直接路径进一步降低了 Q05 延迟。**收益仍取决于谓词、选择率和路径在 ClickHouse part 中的归属。

### 4.5 单路径投影

S04 只投影 `gen_ai.output.messages`，固定窗口内返回 **4,277 个非空值、6,973,065 UTF-8 bytes**。端到端延迟统计到响应 bytes 读取完成；客户端再规范化值并核对数量和字节数。

| 存储结构 | 端到端 p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | 389.15 / 418.51 ms | **99.522 ms** |
| openGauss JSONB | **221.97 / 255.21 ms** | 102.561 ms |
| ClickHouse String JSON | **172.42 / 214.07 ms** | 226.935 ms |
| ClickHouse Native JSON | 173.40 / 243.35 ms | **146.953 ms** |

**结论：openGauss JSONB 的端到端延迟低于 openGauss JSON；ClickHouse 两种结构的 p50 接近。**本场景响应约 7 MB，输出编码、传输和客户端读取会掩盖部分服务端路径提取差异。客户端恢复值独立计量，不能与端到端延迟相加后解释为服务端耗时。

### 4.6 完整 Trace

S05 和 Q04 使用同一个代表性 `trace_id`，回查 **6 条 Span**，按 `start_time,event_id` 排序并返回完整 canonical attributes。该场景模拟单次 Trace 详情和故障下钻。

基础四结构 S05：

| 存储结构 | 端到端 p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | **20.95 / 25.97 ms** | 0.174 ms |
| openGauss JSONB | 21.13 / **24.05 ms** | 0.174 ms |
| ClickHouse String JSON | **54.38 / 62.28 ms** | **0.310 ms** |
| ClickHouse Native JSON | 62.92 / 106.71 ms | 0.647 ms |

扩展结构 Q04：

| 存储结构 | p50 |
|---|---:|
| openGauss JSONB | **22.93 ms** |
| openGauss JSONB + 表达式索引 | 23.05 ms |
| openGauss JSONB + GIN | 23.37 ms |
| ClickHouse String JSON | **13.38 ms** |
| ClickHouse Map | 14.24 ms |
| ClickHouse Native JSON + 稀疏 Sidecar | 29.57 ms |

**结论：openGauss JSON 与 JSONB 接近，索引没有改善本次固定 Trace 回查；ClickHouse String JSON 在两组结果中均低于 Native JSON。**Native JSON 查询还需返回稀疏 Sidecar，客户端合并位于端到端计时之外。

### 4.7 完整文档分页

S06 在固定窗口 27,561 行中按 `start_time,event_id` 排序，读取固定 **256 行**页面，并返回每行完整 canonical attributes。该场景模拟列表分页或批量导出，突出扫描、排序、完整文档生成、响应传输和客户端恢复。

| 存储结构 | 端到端 p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | **197.21 / 204.93 ms** | **1.936 ms** |
| openGauss JSONB | 459.78 / 477.82 ms | 2.016 ms |
| ClickHouse String JSON | **48.21 / 65.40 ms** | **2.956 ms** |
| ClickHouse Native JSON | 77.53 / 127.34 ms | 7.294 ms |

**结论：openGauss JSON 和 ClickHouse String JSON 在各自引擎内的完整文档分页更低。**JSONB 需要遍历文档树并序列化；Native JSON 需要汇集路径并返回 Sidecar。本输入 attributes 最大约 62 KiB，超长 payload 不在本阶段范围内。

### 4.8 持续写入与后台 merge

原六结构实验按 256 行 block 持续提交。前五个 block 完成后启动两个已建立连接的查询线程，循环执行稳定列、高频路径、聚合和低密度路径查询。每个样本按查询开始前已提交的数据水位，与相应的预计算结果核对。

| 存储结构 | 每轮并发样本中位数 | Q02 p50 | Q05 p50 |
|---|---:|---:|---:|
| openGauss JSONB | 728 | 24.68 ms | 14.19 ms |
| openGauss JSONB + 表达式索引 | 984 | **12.72 ms** | 19.62 ms |
| openGauss JSONB + GIN | 952 | 30.64 ms | **3.57 ms** |
| ClickHouse String JSON | 572 | 41.13 ms | 36.17 ms |
| ClickHouse Map | 652 | 42.39 ms | 31.28 ms |
| ClickHouse Native JSON | 1,436 | **17.35 ms** | **16.53 ms** |

全部样本与相应写入水位的正确结果一致。Q02/Q05 是持续写入期间的查询延迟，不是载入延迟。查询线程采用完成一次再发送下一次的闭环方式，各结构的样本数和水位不同，因此本表只说明写入干扰下的行为，不代替静态横向比较或饱和吞吐测试。

补充四结构的载入计时排除 openGauss `ANALYZE` 和 ClickHouse merge 等待。ClickHouse 机制观察另行记录 190 个 zero-level data part、后台 merge 和 `OPTIMIZE ... FINAL`，使写入耗时与后续生命周期维护分开。

### 4.9 定向优化

| 扩展 | 对照与关键数字 | 结论 | 使用边界 |
|---|---|---|---|
| openGauss 表达式 B-tree | Q02：112.49 → **20.59 ms** | 固定热点路径收益明确 | 只覆盖匹配表达式；不改善 Q05 |
| openGauss `jsonb_hash_ops` GIN | Q05：88.77 → **3.84 ms** | 匹配包含谓词时收益明确 | 增加写入和空间；不代表任意路径均有收益 |
| ClickHouse type hint | 最终 part：32 dynamic / 6 shared / **2 hinted** | 固定类型且本版本中不占 dynamic path 预算 | 缺失路径产生类型默认值，恢复需要 presence marker |

openGauss 数字来自原六结构三轮静态查询；ClickHouse type hint 来自一次机制观察。扩展结构用于解释优化机制，不进入四种基础结构排名。

## 5. 正确性与建议

### 5.1 正确性结果

补充四结构共 16/16 个运行结果完成；每个结果包含 190 个 block 和 520 个正式查询样本。原六结构共 18/18 个运行结果完成，静态查询、写入期间查询、逻辑文档恢复、原文恢复和清理均通过。

### 5.2 使用建议

1. 稳定、高频且类型明确的字段继续使用普通强类型列。
2. openGauss 动态属性需要路径分析时优先保留 JSONB 候选。固定热点路径使用表达式 B-tree；包含查询只在谓词和选择率验证后使用 GIN。
3. ClickHouse 完整文档读取频繁时保留 String JSON 候选；路径过滤和聚合频繁时保留 Native JSON，并计入 Sidecar、客户端恢复和 merge 生命周期成本。
4. Native JSON 的 `max_dynamic_paths` 按真实 Trace 路径分布和工作负载设置。阶段一 `5000×1%` 只表示机制压力边界。
5. type hint 用于查询频繁、类型稳定且缺失语义明确的路径。稳定业务字段优先独立建列。
6. 签名、字节级审计和精确重放统一保存首次解析前的原始 UTF-8 bytes；canonical Sidecar 只承担逻辑文档恢复。

## 参考资料

- [JSON 存储原理](json-storage-principles-2026-09-09.md)
- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-09.md)
- [阶段二补充实验设计](json-storage-stage2-sup-experiment-design-2026-09-09.md)
- [ClickHouse JSON 数据类型](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/reference/statements/optimize)
- [ClickHouse SELECT FINAL](https://clickhouse.com/docs/reference/statements/select/from#final-modifier)
- [openGauss 6.0 JSON/JSONB 类型](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-types.html)
