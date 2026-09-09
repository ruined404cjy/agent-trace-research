# Agent Trace JSON 存储阶段二报告：处理流程与场景化比较

> 实验完成日期：2026-09-09；文档修订日期：2026-09-09
> 状态：原六结构实验与补充四结构实验均完成正式门禁
> 数据路径：独立载入程序
> 数据库版本：openGauss 6.0.0 build `aee4abd5`；ClickHouse 25.12.11.4

## 1. 结论与适用范围

阶段二在同一份 48,534 行 Agent Trace 数据上完成基础结构、索引与 Sidecar 扩展实验。结果统一按载入、稳定列、高频路径、低密度路径、完整 Trace、完整文档和持续写入等场景组织。查询、轮次或计时边界不同时在表内标明，**不直接混算或计算跨批次变化比例**。

补充四结构实验表明：openGauss JSONB 在高频路径、低密度路径和单路径投影上低于 openGauss JSON 的延迟；openGauss JSON 在本次完整文档分页上更低。ClickHouse Native JSON 在高频路径和低密度路径上低于 ClickHouse String JSON；ClickHouse String JSON 在完整 Trace 和完整文档分页上更低。稳定列聚合主要反映引擎及执行路径，不能判断 JSON 表示本身。

原六结构实验继续支持两个机制结论：稳定高频路径可用表达式索引，适合 containment 的低密度路径可用 `jsonb_hash_ops` GIN；持续写入期间的样本只说明对应写入水位下的行为。阶段一的宽路径结果只用于说明引擎内机制和压力边界，**不作为本阶段横向结论**。

所有结果来自单机固定版本、热查询和独立载入程序。实验未覆盖 Collector、exporter、benchmark 的完整链路，也未覆盖饱和吞吐、冷缓存、多节点和故障恢复。

## 2. 数据、查询和证据

### 2.1 输入特征

数据源是 Hugging Face `Leoxx/whowhen_pro` 的 text split。源数据的 6,257 条失败轨迹记录经 `trace-synthesis` 投影为 6,257 条 Trace 和 48,534 个 Span。投影按 trajectory 事件生成 Trace 根 Span、LLM Span 和 Tool Span，并保留任务、framework、benchmark、failure ground truth 和事件内容。

该投影由输入、配置和 `seed=42` 共同确定。Trace ID 根据 split 和源记录 ID 使用 UUID5 派生，Span ID 根据 Trace ID 和固定局部名称派生；时间、token、部署环境和 telemetry dialect 等补充字段使用 seed 与 ID 的哈希计算。相同输入、配置和 seed 生成相同的行序、标识和字段值。阶段二生成器再抽取稳定列，把点分隔的 Attribute key 可逆投影为嵌套 JSON，并生成 canonical 内容、原始事件 bytes 和独立查询 truth。

固定输入包含 48,534 个 span、6,257 个 trace 和 29 个顶层 Attribute key。每行 Attribute key 数的 p50/p95/p99/最大值为 8/14/15/15。输入按 256 行分块，共 190 个 block，末 block 为 150 行。

| 数据特征 | 结果 |
|---|---|
| span 类型 | llm 17,486；tool 24,791；trace 6,257 |
| 路径密度 | `span.type` 100%；`gen_ai.operation.name` 87.11%；`prompt_text` 3.96% |
| attributes canonical UTF-8 长度 | p50 1,042；p95 4,663；p99 7,697；最大 62,197 bytes |
| raw event UTF-8 长度 | p50 1,480；p95 5,087；p99 8,121；最大 62,626 bytes |
| 混合类型 | `failure.mistake_agent` 和 `failure.mistake_step` 均含 JSON null 与普通值 |

数据中的时间戳从 2030-01-01 起构造，只用于固定时间窗口，不代表原数据的生产到达时间。当前输入只覆盖 text split。tenant、真实 project 和 instrumentation scope 在源数据中不可观测。29 个顶层 Attribute key 代表较窄样本，不能支撑 5000 路径的生产分布假设。

### 2.2 两批正式实验

| 实验 | 存储结构与样本 | 可回答的问题 | 结果入口 |
|---|---|---|---|
| 原六结构实验 | 两引擎各三轮；每轮每个静态查询 100 次，并含写入期间查询 | 索引、ClickHouse Map、持续分批写入和并发查询 | `docs/temp/json-storage-stage2/formal-20260907-retry-3/` |
| 补充四结构实验 | 四结构各四轮；S01～S05 每轮 100 次，S06 每轮 20 次 | JSON/JSONB、String/Native 的基础载入和场景化比较 | `docs/temp/json-storage-stage2-sup/formal-20260909-summary/summary-a/` |
| openGauss 机制观察 | 五种结构各一次 | JSON/JSONB、表达式索引、GIN、完整文档和复合探针 | `docs/temp/json-storage-stage2-sup/opengauss-mechanisms-20260909/` |
| ClickHouse 机制观察 | 四种 ClickHouse Native JSON 结构各一次 | Sidecar、type hint、part、merge 和两种 FINAL | `docs/temp/json-storage-stage2-sup/clickhouse-mechanisms-20260909/` |

补充四结构的 wall latency 从已建立连接提交语句开始，到响应 bytes 完整读取结束。结果规范化、Sidecar 合并、canonical 序列化、hash 和 truth 核对另记为客户端恢复时间。载入 wall 汇总同时包含原文表和分析表的 block 写入，不是纯 JSON 转换成本，也不是饱和吞吐。

## 3. openGauss JSON 与 openGauss JSONB

![openGauss JSON 与 JSONB 处理流程](./assets/json-storage-opengauss-jsonb-flow.svg)

处理流程分为五步：

1. 客户端把 UTF-8 JSON 通过 COPY 或 SQL 发送给数据库；openGauss JSON 和 openGauss JSONB 都先校验 JSON 语法。
2. openGauss JSON 保存进入 JSON datum 后的文本表示。路径查询时执行 JSON 操作符并处理文本内容。
3. openGauss JSONB 在写入时解析对象和数组，生成二进制文档表示；对象键顺序不再保留，重复键只保留最后一个值。
4. 两种 datum 都写入 heap；较大值由 TOAST 压缩或移出主行。显式创建的表达式 B-tree 或 JSONB GIN 在写入时同步维护。
5. 字段查询使用基列操作符，优化器按谓词和选择率决定是否使用索引；完整文档始终读取基列并序列化返回，不从索引重建。

openGauss JSONB 避免查询时反复执行 JSON 文本词法解析，并支持 JSONB 索引；实际收益取决于操作、选择率、索引和返回量。完整回读满足本实验的 canonical 文档等价，不保留原始空白、对象键顺序、等价转义和重复键文本。逐字节恢复由首次解析前保存的原文表承担。

### 3.1 机制观察

| 结构 | COPY 与索引维护 | 总空间 | 复合探针服务端 / 客户端 wall | 完整文档服务端返回 / 恢复 |
|---|---:|---:|---:|---:|
| openGauss JSON | 4,879.252 ms | 73,089,024 bytes | 13.347 / 15.726 ms | 781.965 / 5,486.643 ms |
| openGauss JSON + 热点索引 | 5,479.584 ms | 75,415,552 bytes | 12.671 / 14.312 ms | 615.157 / 5,774.496 ms |
| openGauss JSONB | 4,905.363 ms | 77,193,216 bytes | 15.838 / 18.843 ms | 1,061.466 / 5,436.239 ms |
| openGauss JSONB + GIN | 6,022.325 ms | 85,204,992 bytes | 16.921 / 19.081 ms | 1,532.748 / 6,082.003 ms |
| openGauss JSONB + 热点索引 | 5,127.186 ms | 79,536,128 bytes | 11.086 / 13.646 ms | 1,085.153 / 6,012.375 ms |

数据特征：48,534 行固定输入，一次机制运行。空间包含 heap、TOAST 和 index 的分配字节。

关键数字：JSON 与 JSONB 基础结构的 COPY 差为 **26.111 ms**；JSONB + GIN 的总空间为 **85,204,992 bytes**。

结论：表达式索引和 GIN 增加维护与空间成本，收益取决于查询能否匹配索引。`attributes_to_jsonb_probe` 覆盖全表 scan、`ingest_seq` filter、`attributes::jsonb` expression 和结果生成，是复合指标。客户端 wall 与服务端执行时间之差还包含协议、plan 传输和客户端处理，不能称为纯网络耗时。

边界：本表只有一次机制观察，不进入补充四结构的四轮性能统计。

## 4. ClickHouse String JSON 与 ClickHouse Native JSON

![ClickHouse String JSON 与 ClickHouse Native JSON 处理流程](./assets/json-storage-clickhouse-native-json-flow.svg)

处理流程按写入、组织、维护和读取展开：

1. JSONEachRow 解析每个输入对象并找到目标列。ClickHouse String JSON 把动态属性保存为压缩 String，字段查询时再调用 JSON 提取函数。
2. ClickHouse Native JSON 在写入时展开叶路径并识别值类型。手动声明的 type hint 路径进入固定类型子列，不参与本实验的动态路径预算。
3. 未声明路径成为 dynamic path 候选。每个 data part 独立应用 `max_dynamic_paths=32`：预算内路径保存为 dynamic subcolumn，其余路径保存到 shared data。
4. INSERT 写出不可变的零层 data part，提交后即可查询。多个 active parts 各自拥有路径集合，因此全表可见的 dynamic path 并集可以超过 32。
5. 后台 merge 读取多个源 part 并写出目标 part，同时重新组织 dynamic subcolumn 与 shared data。本版本实测中，非空出现量较高的路径通常保留为 dynamic subcolumn。
6. 路径查询先根据 part 元数据确定读取固定子列、dynamic subcolumn 还是 shared data；完整对象读取需要组合这些物理表示。
7. `OPTIMIZE TABLE ... FINAL` 强制执行物理 part 合并；`SELECT ... FINAL` 只在查询时应用表引擎的合并语义，不改写磁盘上的 part。

| dynamic subcolumn 影响因素 | 对物理组织的影响 |
|---|---|
| type hint | 手动指定稳定路径和类型，形成固定类型子列；本版本实测不占 32 个 dynamic path 名额 |
| 单个 part 的未声明路径数 | 与 `max_dynamic_paths` 比较；超过预算的路径进入 shared data |
| 路径在源 part 中的非空出现量 | merge 生成目标 part 时用于路径统计；本轮观察到高频路径通常优先成为 dynamic subcolumn |
| block 与 part 组成 | 每个零层 part 独立选路；不同 part 的 dynamic path 集合可以不同 |
| 同一路径的值类型数量 | 由 Dynamic/Variant 表示及 `max_dynamic_types` 约束，影响类型子流、转换和读取成本，不等同于 dynamic path 名额 |
| merge 后目标 part 的上限 | 目标 part 仍受 JSON 类型参数和相关 MergeTree 设置约束，路径可在 dynamic 与 shared 之间移动 |

`max_dynamic_paths` 是容量上限，不是按出现频率触发建列的阈值。频率是 merge 重组时的选择依据之一。路径是否值得声明为 type hint，还应结合查询频率、类型稳定性和缺失值语义判断。缺失的 hinted 路径按声明类型返回默认值。

### 4.1 data part 与 merge 观察

| ClickHouse Native JSON 结构 | 全部 INSERT：parts / 路径 / 压缩 bytes | 后台 merge 稳定：parts / 路径 / 压缩 bytes | `OPTIMIZE ... FINAL`：parts / 路径 / 压缩 bytes |
|---|---|---|---|
| 无 Sidecar | 190；38/13/0；33,237,237 | 5；35/9/0；29,134,796 | 1；32/8/0；28,944,159 |
| 稀疏 Sidecar | 190；38/13/0；41,791,120 | 3；34/7/0；37,672,532 | 1；32/8/0；37,563,308 |
| 完整 Sidecar | 190；38/13/0；46,933,115 | 4；35/9/0；42,133,468 | 1；32/8/0；42,010,202 |
| type hint + 稀疏 Sidecar | 190；38/3/2；41,759,974 | 3；34/5/2；37,657,860 | 1；32/6/2；37,547,576 |

路径列依次为 dynamic/shared/hinted。数据特征是 190 个 256 行 block，末 block 150 行，并在机制实验中暂停和恢复表级后台 merge。

关键数字：最终单 part 中，type hint 结构为 **32 dynamic + 6 shared + 2 hinted**，说明本版本 hinted 路径不占 32 个 dynamic path 预算。

结论：data part 生成后即可查询；后台 merge 和强制合并改变 part 数量及路径物理归属。

边界：压缩 bytes 属于 ClickHouse active parts，不能与 openGauss 分配空间计算压缩率排名。上述路径选择是固定版本和固定输入下的观察。

### 4.2 两种 FINAL

| 时点 | active parts | 普通读取版本 | `SELECT ... FINAL` 版本 |
|---|---:|---|---|
| `OPTIMIZE ... FINAL` 前 | 2 | `[1,2]` | `[2]` |
| `OPTIMIZE ... FINAL` 后 | 1 | `[2]` | `[2]` |

关键数字：强制物理合并耗时 **4.565 ms**。

结论：`SELECT ... FINAL` 在读取时消解 `ReplacingMergeTree(version)` 的跨 part 版本，`OPTIMIZE TABLE ... FINAL` 把两个 active parts 重写为一个。

边界：该小型探针只验证语义，4.565 ms 不是生产 `OPTIMIZE` 性能基准。

## 5. ClickHouse Native JSON 与 Sidecar

![ClickHouse Native JSON 与 Sidecar 恢复流程](./assets/json-storage-native-json-sidecar-flow.svg)

恢复流程分为三个目标：

1. 字段分析直接读取 ClickHouse Native JSON 的路径表示，执行过滤、分组和聚合。
2. 逻辑文档恢复读取 Native JSON 后，再用稀疏 Sidecar 覆盖 JSON null 和空容器；type hint 路径还需要 presence marker 区分缺失和值等于类型默认值。完整 canonical Sidecar 可以直接提供整列逻辑内容。
3. 字节级恢复直接读取首次解析前保存的原始 UTF-8 bytes，用于签名校验、精确重放和字节级审计。

ClickHouse Native JSON 的叶路径表示不能区分 JSON null 与路径缺失，并会省略空对象。本轮无 Sidecar 结构观测到 **5,446 个 JSON null 和 10 个空对象造成共 5,456 条差异；空数组差异为 0**；普通值内容逐行门禁通过。冻结输入包含 12,623 个顶层空数组，未造成差异。

稀疏 Sidecar 规则仍保守保存递归包含 JSON null、空对象或空数组的 Attribute。该规则覆盖当前未出现损失的空数组，避免恢复契约依赖一次输入和固定版本的观察。

| 方案 | Sidecar entries / UTF-8 bytes | marker entries / bytes | 恢复时间 | mismatch |
|---|---:|---:|---:|---:|
| 无 Sidecar | 0 / 0 | 0 / 0 | 45.000 ms | **5,456** |
| 稀疏 Sidecar | 66,613 / 26,698,849 | 48,534 / 1,607,879 | 4,721.643 ms | **0** |
| 完整 canonical Sidecar | 整列 / 81,634,867 | 0 / 0 | 2,180.746 ms | **0** |
| type hint + 稀疏 Sidecar | 66,613 / 26,698,849 | 48,534 / 1,607,879 | 5,076.783 ms | **0** |

数据特征：一次全语料机制恢复。稀疏 Sidecar 的 entries 和 bytes 已包含 marker，不能再次相加。

结论：无 Sidecar 适合观察 ClickHouse Native JSON 自身语义；稀疏 Sidecar 保存特殊值并在读取端覆盖；完整 canonical Sidecar 保存整列确定性 JSON 文本。type hint 会为缺失声明路径注入默认值，因此恢复流程必须使用 presence marker 区分“原文缺失”和“原文存在且等于默认值”。本实验为保持对照一致，在自动类型结构中也使用同一 marker 规则。

边界：恢复时间是一次客户端全语料机制观察，不是查询 latency。canonical Sidecar 保存逻辑键值、数组顺序和空容器，不保存原始空白、原始键顺序、等价转义、数值词法形式或重复键实例，也不是 RFC 8785 JCS。签名、字节级审计和精确重放依赖首次解析前保存的原始 UTF-8 bytes。

## 6. 场景化结果与分析

以下小节把基础结构、索引与 Sidecar 扩展结果统一放入对应场景。补充四结构每种结构执行四轮，每个 p50/p95 先在单轮计算，再取四轮中位数；原六结构每种结构执行三轮，表中为三轮中位数。两组实验使用同一 48,534 行输入，但查询、轮次和计时边界不同，只在各自表内比较。

### 6.1 基础载入与空间

场景设计：四种基础结构按相同行序写入 190 个 block，每个 block 同时写分析表和原文表。计时从提交预生成 block 开始，到该 block 成功且可见结束，不含客户端预处理、openGauss `ANALYZE` 或 ClickHouse merge 等待。该场景隔离 JSON 表示、索引和 Sidecar 对写入及表空间的影响，不代表饱和并发写入吞吐。

基础四结构结果：

| 存储结构 | 载入 rows/s | 分析数据 MiB | 原文 MiB | 合计 MiB |
|---|---:|---:|---:|---:|
| openGauss JSON | **6,322.38** | 69.695 | 83.281 | 152.977 |
| openGauss JSONB | 6,134.30 | 73.641 | 83.281 | 156.922 |
| ClickHouse String JSON | **5,754.01** | 15.352 | 17.653 | 33.005 |
| ClickHouse Native JSON | 5,105.91 | 35.840 | 17.653 | 53.494 |

带索引、Map 或 Sidecar 的扩展结构结果：

| 存储结构 | 载入 rows/s | 分析 / 原文 / 合计 MiB |
|---|---:|---:|
| openGauss JSONB | **4,747** | 73.64 / 83.28 / 156.92 |
| openGauss JSONB + 表达式索引 | 4,638 | 75.88 / 83.28 / 159.16 |
| openGauss JSONB + GIN | 4,523 | 81.28 / 83.28 / 164.56 |
| ClickHouse String JSON | **5,860** | 15.35 / 17.65 / 33.01 |
| ClickHouse Map | 5,482 | 24.99 / 17.65 / 42.65 |
| ClickHouse Native JSON + 稀疏 Sidecar | 3,688 | 27.69 / 17.65 / 45.34 |

数据特征：48,534 行、190 个固定 block。基础结构载入计时包含分析表和原文表写入；扩展结构还包含对应索引、Map 或 Sidecar 的维护成本。

分析结论：**openGauss JSON 和 ClickHouse String JSON 在各自引擎的基础结构中载入较高**。表达式索引与 GIN 增加 openGauss 写入维护和空间；ClickHouse Map 与 Native JSON 增加动态属性组织成本。空间只作引擎内说明。

边界：载入包含原文表和分析表写入，不含客户端预处理与维护等待。openGauss 为 heap、TOAST 和 index 的 allocated bytes；ClickHouse 为 active parts compressed bytes，二者不能用于跨引擎压缩率排名。

### 6.2 稳定列聚合

场景设计：S01 在固定半程时间窗口内读取 27,561 行，只按强类型 `span_type` 分组，返回 llm、tool、trace 三行计数。Q01 和 Q03 同样只读取稳定列，分别返回分组计数和 `count + sum(duration_ms)`。该场景是控制组，用于确认 JSON 表示、索引或 Sidecar 在动态属性未参与查询时是否引入额外影响。

基础四结构 S01：

| 存储结构 | S01 p50 / p95 |
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

数据特征：S01、Q01 和 Q03 均只读取稳定列，分组维度不同。

分析结论：同一引擎中的不同 JSON 表示或索引结构接近，差异主要来自查询和执行路径。该场景**不能判断动态 JSON 表示本身的优劣，也不用于通用跨引擎排名**。

### 6.3 高频路径

场景设计：S02 和 Q02 都读取 `gen_ai.operation.name`，筛选 `execute_tool` 后按稳定列分组。固定窗口内命中 **20,155 行**，属于高频、较高命中量路径；结果集只有一行聚合。基础结构比较文本解析、JSONB 操作符与 Native JSON 路径读取，扩展结构再观察表达式索引、GIN、Map 和 Native JSON 子列的差异。

基础四结构 S02：

| 存储结构 | S02 p50 / p95 |
|---|---:|
| openGauss JSON | 281.72 / 297.36 ms |
| openGauss JSONB | **96.45 / 104.02 ms** |
| ClickHouse String JSON | 121.78 / 128.32 ms |
| ClickHouse Native JSON | **91.91 / 98.30 ms** |

扩展结构 Q02：

| 存储结构 | Q02 p50 |
|---|---:|
| openGauss JSONB | 112.49 ms |
| openGauss JSONB + 表达式索引 | **20.59 ms** |
| openGauss JSONB + GIN | 110.97 ms |
| ClickHouse String JSON | 85.31 ms |
| ClickHouse Map | 51.34 ms |
| ClickHouse Native JSON + 稀疏 Sidecar | **9.83 ms** |

数据特征：S02 与 Q02 都过滤高频路径；基础结构不含表达式索引、GIN 或 type hint，扩展结构用于验证定向优化。

分析结论：**openGauss JSONB 和 ClickHouse Native JSON 在基础查询中分别低于同引擎文本表示；稳定热点进一步受益于表达式索引或直接子列**。GIN 没有改善不匹配的等值表达式查询。结果仍依赖路径密度、谓词、返回量和热缓存。

### 6.4 低密度路径

场景设计：S03 和 Q05 筛选 `failure.mistake_mode=A.3`，固定窗口内只命中 **741 行**，返回计数和排序后身份摘要，避免把大量明细传输混入计时。S03 使用各基础类型的自然路径谓词；Q05 使用可与 `jsonb_hash_ops` GIN 匹配的 containment 谓词，并同时比较 ClickHouse Map 与 Native JSON。

基础四结构 S03：

| 存储结构 | S03 p50 / p95 |
|---|---:|
| openGauss JSON | 271.10 / 284.71 ms |
| openGauss JSONB | **88.10 / 94.33 ms** |
| ClickHouse String JSON | 68.60 / 76.25 ms |
| ClickHouse Native JSON | **47.60 / 54.60 ms** |

扩展结构 Q05：

| 存储结构 | Q05 p50 |
|---|---:|
| openGauss JSONB | 88.77 ms |
| openGauss JSONB + 表达式索引 | 88.67 ms |
| openGauss JSONB + GIN | **3.84 ms** |
| ClickHouse String JSON | 69.58 ms |
| ClickHouse Map | 35.11 ms |
| ClickHouse Native JSON + 稀疏 Sidecar | **7.57 ms** |

数据特征：S03 使用 `failure.mistake_mode=A.3` 并返回 count 和 identity digest，不使用 GIN containment；Q05 使用与 GIN 匹配的低密度 containment 查询。

分析结论：两引擎的结构化表示均降低了本次低密度路径查询延迟；**匹配谓词的 GIN 和 Native JSON 直接子列进一步降低 Q05 延迟**。选择率或谓词形式变化会改变收益。

### 6.5 单路径投影

场景设计：S04 只投影 `gen_ai.output.messages`，固定窗口内返回 **4,277 个非空值、6,973,065 UTF-8 bytes**。查询 wall latency 统计到响应 bytes 读取完成；客户端再规范化返回值并核对数量和字节数。该场景强调单个较宽 JSON 路径的列裁剪、解析和传输成本，不包含整文档重建。

| 存储结构 | S04 wall p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | 389.15 / 418.51 ms | 99.522 ms |
| openGauss JSONB | **221.97 / 255.21 ms** | 102.561 ms |
| ClickHouse String JSON | **172.42 / 214.07 ms** | 226.935 ms |
| ClickHouse Native JSON | 173.40 / 243.35 ms | **146.953 ms** |

数据特征：投影 `gen_ai.output.messages`，返回非空值数量和 UTF-8 总字节数。

结论：openGauss JSONB 的 wall latency 低于 openGauss JSON；ClickHouse 两种结构的 wall p50 接近。边界：wall latency 不含客户端恢复；恢复值必须单列，不能与 wall 直接相加后称为服务端延迟。

### 6.6 完整 Trace

场景设计：S05 和 Q04 使用同一个代表性 `trace_id`，回查 **6 条 Span**，按 `start_time,event_id` 排序并返回完整 canonical attributes。该场景同时读取稳定列和完整动态文档，数据量小，主要观察点查、文档序列化、Sidecar 返回和客户端恢复的组合成本。

基础四结构 S05：

| 存储结构 | S05 wall p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | **20.95 / 25.97 ms** | 0.174 ms |
| openGauss JSONB | 21.13 / **24.05 ms** | 0.174 ms |
| ClickHouse String JSON | **54.38 / 62.28 ms** | **0.310 ms** |
| ClickHouse Native JSON | 62.92 / 106.71 ms | 0.647 ms |

扩展结构 Q04：

| 存储结构 | Q04 p50 |
|---|---:|
| openGauss JSONB | **22.93 ms** |
| openGauss JSONB + 表达式索引 | 23.05 ms |
| openGauss JSONB + GIN | 23.37 ms |
| ClickHouse String JSON | **13.38 ms** |
| ClickHouse Map | 14.24 ms |
| ClickHouse Native JSON + 稀疏 Sidecar | 29.57 ms |

数据特征：S05 与 Q04 均按固定 trace 回查六条记录，返回排序身份和完整 canonical attributes。

分析结论：openGauss JSON 与 JSONB 基础结构接近，索引对本次完整 Trace 回查没有明显收益；**ClickHouse String JSON 在两组完整 Trace 结果中均低于 Native JSON**。Native JSON 结果包含稀疏 Sidecar 返回，客户端恢复在 wall 计时区间外。

### 6.7 完整文档分页

场景设计：S06 在固定窗口 27,561 行中按 `start_time,event_id` 排序，读取固定 **256 行**页面，并返回每行完整 canonical attributes。它模拟详情页或批量导出的有界页面读取，突出完整文档序列化、响应体传输和客户端恢复；本输入 attributes 最大约 62 KiB，不覆盖超长 payload。

| 存储结构 | S06 wall p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | **197.21 / 204.93 ms** | **1.936 ms** |
| openGauss JSONB | 459.78 / 477.82 ms | 2.016 ms |
| ClickHouse String JSON | **48.21 / 65.40 ms** | **2.956 ms** |
| ClickHouse Native JSON | 77.53 / 127.34 ms | 7.294 ms |

数据特征：固定 256 行页面，返回排序身份和完整 canonical attributes；每轮测量 20 次。

结论：**openGauss JSON 和 ClickHouse String JSON 在各自引擎内的完整文档分页更低**。边界：本输入的 attributes 最大约 62 KiB；更长 payload 由阶段三单独验证。

### 6.8 持续写入与后台 merge

场景设计：每种扩展结构按 256 行 block 持续提交，前五个 block 完成后启动两个已建立连接的查询 worker，循环执行稳定列、高频路径、聚合和低密度路径查询。每个样本绑定启动查询前的已提交水位，并与该水位的独立 truth 核对；后续 INSERT 不改变该样本的期望结果。该场景观察写入干扰下的查询行为和 ClickHouse 后台 merge，不形成固定并发压力或饱和吞吐结论。

每轮样本数和水位分布受写入持续时间与查询耗时共同影响。

| 原六结构实验存储结构 | 每轮并发样本中位数 | Q02 p50 | Q05 p50 |
|---|---:|---:|---:|
| openGauss JSONB | 728 | 24.68 ms | 14.19 ms |
| openGauss JSONB + 表达式索引 | 984 | **12.72 ms** | 19.62 ms |
| openGauss JSONB + GIN | 952 | 30.64 ms | **3.57 ms** |
| ClickHouse String JSON | 572 | 41.13 ms | 36.17 ms |
| ClickHouse Map | 652 | 42.39 ms | 31.28 ms |
| ClickHouse Native JSON | 1,436 | **17.35 ms** | **16.53 ms** |

结论：全部样本与对应水位 truth 一致。边界：各结构的样本数和水位不同，**本表只说明写入干扰下的行为，不代替静态横向比较**。

生命周期维护与载入计时分开。补充四结构的载入计时排除 openGauss `ANALYZE` 和 ClickHouse merge 等待；ClickHouse 机制观察另行记录从 190 个零层 part 到后台 merge 稳定，再到 `OPTIMIZE TABLE ... FINAL` 的过程。

### 6.9 索引或 type hint 扩展

场景设计：openGauss 表达式索引只覆盖稳定高频路径，GIN 只覆盖与 containment 谓词匹配的低密度路径；ClickHouse type hint 手动固定两条已知路径的类型，并与相同 `max_dynamic_paths=32` 和稀疏 Sidecar 对照。该场景回答定向物理优化何时有效，以及需要承担多少写入、空间和恢复语义成本。

| 扩展 | 对照与关键数字 | 结论 | 边界 |
|---|---|---|---|
| openGauss 表达式索引 | 原六结构 Q02：112.49 → **20.59 ms** | 匹配稳定高频路径时收益明确 | 索引不匹配 Q05 |
| openGauss `jsonb_hash_ops` GIN | 原六结构 Q05：88.77 → **3.84 ms** | containment 与索引匹配时收益明确 | 维护和空间增加；不代表任意路径都有收益 |
| ClickHouse type hint | 最终 part：32 dynamic / 6 shared / **2 hinted** | hinted 路径使用固定类型且不占本版本 dynamic budget | 缺失路径返回类型默认值，恢复需 presence marker |

扩展结果不进入四种基础结构排名。openGauss 数字来自原六结构的三轮静态查询；ClickHouse type hint 来自一次机制观察，两者不能直接比较。

## 7. 正确性、异常与建议

补充四结构共 16/16 个结果完成；每个结果含 190 个 block 和 520 个正式查询样本。原六结构共 18/18 个结果完成，静态与写入期间查询、分析恢复、原文恢复和清理均通过。

原六结构的三组早期失败结果只作诊断：openGauss 6.0.0 `jsonb_ops` GIN 在递归空字符串上失败，正式实验改用 `jsonb_hash_ops`；ClickHouse 查询日志逐请求刷新触发内存限制；另一次运行的首个并发样本存在连接建立计时风险。正式统计均使用完整重跑结果。

当前建议如下：

1. 稳定列继续使用强类型列，避免让 JSON 表示承担稳定列查询。
2. openGauss 路径分析优先选择 openGauss JSONB；明确且高频的路径使用表达式索引，经过谓词和选择率验证的包含查询再使用 GIN。
3. ClickHouse 完整文档读取频繁时保留 ClickHouse String JSON 候选；路径过滤频繁时保留 ClickHouse Native JSON，并计入 Sidecar、恢复和 merge 生命周期成本。
4. ClickHouse Native JSON 的动态路径预算按真实 Trace 分布设置。阶段一 `5000×1%` 仅是压力边界，不能作为默认生产配置依据。
5. 字节级审计、签名和精确重放在首次解析前保存原始 UTF-8 bytes；canonical Sidecar 只承担逻辑文档恢复。

长 payload、Full/Core、独立 payload 表和 asset reference 由[阶段三实验设计](json-storage-stage3-experiment-design-2026-09-09.md)验证。

## 参考资料

- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-09.md)
- [阶段二补充实验设计](json-storage-stage2-sup-experiment-design-2026-09-09.md)
- [补充实验复现说明](../experiments/json-storage-stage2-sup/README.md)
- [ClickHouse JSON data type](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse JSONEachRow](https://clickhouse.com/docs/interfaces/formats/JSONEachRow)
- [ClickHouse MergeTree](https://clickhouse.com/docs/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/sql-reference/statements/optimize)
- [ClickHouse SELECT FINAL](https://clickhouse.com/docs/sql-reference/statements/select/from#final-modifier)
- [openGauss 6.0 JSON/JSONB 类型](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-types.html)
