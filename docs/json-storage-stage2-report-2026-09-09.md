# Agent Trace JSON 存储阶段二报告：处理流程与场景化比较

> 实验完成日期：2026-09-09；文档修订日期：2026-09-09
> 状态：原六结构实验与补充四结构实验均完成正式门禁
> 数据路径：独立载入程序
> 数据库版本：openGauss 6.0.0 build `aee4abd5`；ClickHouse 25.12.11.4

## 1. 结论与适用范围

阶段二在同一份 48,534 行 Agent Trace 数据上完成两批实验。原实验比较六种带索引或 Sidecar 的动态属性存储结构；补充实验比较 openGauss JSON、openGauss JSONB、ClickHouse String JSON、ClickHouse Native JSON 四种基础存储结构。两批实验的查询、轮次和计时边界不同，本文分别报告，**不直接混算两批样本**。

补充四结构实验表明：openGauss JSONB 在高频路径、低密度路径和单路径投影上低于 openGauss JSON 的延迟；openGauss JSON 在本次完整文档分页上更低。ClickHouse Native JSON 在高频路径和低密度路径上低于 ClickHouse String JSON；ClickHouse String JSON 在完整 Trace 和完整文档分页上更低。稳定列聚合主要反映引擎及执行路径，不能判断 JSON 表示本身。

原六结构实验继续支持两个机制结论：稳定高频路径可用表达式索引，适合 containment 的低密度路径可用 `jsonb_hash_ops` GIN；持续写入期间的样本只说明对应写入水位下的行为。阶段一的宽路径结果只用于说明引擎内机制和压力边界，**不作为本阶段横向结论**。

所有结果来自单机固定版本、热查询和独立载入程序。实验未覆盖 Collector、exporter、benchmark 的完整链路，也未覆盖饱和吞吐、冷缓存、多节点和故障恢复。

## 2. 数据、查询和证据

### 2.1 输入特征

固定输入包含 48,534 个 span、6,257 个 trace 和 29 个顶层 Attribute key。每行 Attribute key 数的 p50/p95/p99/最大值为 8/14/15/15。输入按 256 行分块，共 190 个 block，末 block 为 150 行。

| 数据特征 | 结果 |
|---|---|
| span 类型 | llm 17,486；tool 24,791；trace 6,257 |
| 路径密度 | `span.type` 100%；`gen_ai.operation.name` 87.11%；`prompt_text` 3.96% |
| attributes canonical UTF-8 长度 | p50 1,042；p95 4,663；p99 7,697；最大 62,197 bytes |
| raw event UTF-8 长度 | p50 1,480；p95 5,087；p99 8,121；最大 62,626 bytes |
| 混合类型 | `failure.mistake_agent` 和 `failure.mistake_step` 均含 JSON null 与普通值 |

数据中的时间戳由生成器构造，只用于固定时间窗口。tenant、真实 project 和 instrumentation scope 在源数据中不可观测。29 个顶层 Attribute key 代表较窄样本，不能支撑 5000 路径的生产分布假设。

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

客户端读取原始 UTF-8 JSON 文本，经 COPY 或 SQL 输入数据库。openGauss JSON 和 openGauss JSONB 都执行 JSON 语法校验。openGauss JSON 保存进入 JSON datum 后的文本表示；逐字节原始事件仍由首次解析前保存的原文表承担。openGauss JSONB 将输入分解为二进制文档表示，不保留对象键顺序；重复对象键仅保留最后一个值。

两种 datum 均由 heap/TOAST 存储，大值可能压缩或移出主行。写入或更新时，数据库同时维护主键索引，以及显式创建的热点表达式 B-tree 或 JSONB GIN。索引服务于匹配查询，完整文档从基列返回，不从索引重建。

路径读取对基列使用 JSON/JSONB 操作符，优化器决定自然计划是否采用索引。openGauss JSONB 避免查询时重复执行 JSON 文本词法解析，并支持 JSONB 索引；实际收益取决于操作、选择率、索引和返回量。完整回读满足本实验的 canonical 文档等价，不保留原始空白、对象键顺序、等价转义和重复键文本。

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

客户端使用 JSONEachRow 输入，每行被解析为独立 JSON 对象。ClickHouse String JSON 把动态属性保存为压缩 String，查询路径时调用 JSON 提取函数。ClickHouse Native JSON 在输入时识别路径和值类型。

声明了 type hint 的路径写入固定类型子列；缺失的声明路径按该类型返回默认值。其余路径在单个 data part 内受 `max_dynamic_paths=32` 约束：预算内路径进入 dynamic subcolumn，预算外路径进入 shared data。INSERT 生成可立即查询的不可变 data part；多个 active part 的路径集合并集可以超过单 part 的 32 路径预算。

后台 merge 读取若干源 part 并写出目标 part。本版本观察到目标 part 通常按非空出现量重新选择 dynamic subcolumn，其余路径进入 shared data；这不是所有 merge 的固定结果。路径在两类物理表示之间移动不表示逻辑数据丢失。

`OPTIMIZE TABLE ... FINAL` 是存储操作，强制把 active part 物理合并。`SELECT ... FINAL` 在读取阶段应用表引擎的合并语义，不重写底层 part。两者用途和成本不同。路径查询可直接读取子列；完整对象读取需要组合 hinted、dynamic 和 shared 表示，并遵循 ClickHouse Native JSON 的叶路径语义。

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

## 6. 补充四结构场景化比较

以下小节均来自补充四结构的四轮正式结果。每个 p50/p95 先在单轮计算，再取四轮中位数。四种结构均通过 2,080 个正式查询样本、S01～S06 truth、分析恢复、原文恢复和清理门禁。

### 6.1 基础载入与空间

| 存储结构 | 载入 rows/s | 分析数据 MiB | 原文 MiB | 合计 MiB |
|---|---:|---:|---:|---:|
| openGauss JSON | **6,322.38** | 69.695 | 83.281 | 152.977 |
| openGauss JSONB | 6,134.30 | 73.641 | 83.281 | 156.922 |
| ClickHouse String JSON | **5,754.01** | 15.352 | 17.653 | 33.005 |
| ClickHouse Native JSON | 5,105.91 | 35.840 | 17.653 | 53.494 |

数据特征：48,534 行、190 个固定 block；按引擎内同口径比较。

关键数字与结论：openGauss JSON 和 ClickHouse String JSON 在各自引擎内的载入速率较高。**空间只作引擎内说明**。

边界：载入包含原文表和分析表写入，不含客户端预处理与维护等待。openGauss 为 heap、TOAST 和 index 的 allocated bytes；ClickHouse 为 active parts compressed bytes，二者不能用于跨引擎压缩率排名。

### 6.2 稳定列聚合

| 存储结构 | S01 p50 / p95 |
|---|---:|
| openGauss JSON | 24.82 / 28.83 ms |
| openGauss JSONB | **24.62 / 27.41 ms** |
| ClickHouse String JSON | **91.14 / 96.03 ms** |
| ClickHouse Native JSON | 91.71 / 96.07 ms |

数据特征：按 `span_type` 分组，只读取稳定列。

结论：同一引擎的两种 JSON 表示接近。边界：该查询不读取动态属性，**不能用来判断 JSON 类型优劣或作通用跨引擎排名**。

### 6.3 高频路径

| 存储结构 | S02 p50 / p95 |
|---|---:|
| openGauss JSON | 281.72 / 297.36 ms |
| openGauss JSONB | **96.45 / 104.02 ms** |
| ClickHouse String JSON | 121.78 / 128.32 ms |
| ClickHouse Native JSON | **91.91 / 98.30 ms** |

数据特征：`gen_ai.operation.name=execute_tool` 等值过滤并按稳定列分组；基础结构不含表达式索引、GIN 或 type hint。

结论：**openGauss JSONB 和 ClickHouse Native JSON 在本查询中分别低于同引擎文本表示**。边界：结果依赖路径密度、直接子列语法、返回量和热缓存。

### 6.4 低密度路径

| 存储结构 | S03 p50 / p95 |
|---|---:|
| openGauss JSON | 271.10 / 284.71 ms |
| openGauss JSONB | **88.10 / 94.33 ms** |
| ClickHouse String JSON | 68.60 / 76.25 ms |
| ClickHouse Native JSON | **47.60 / 54.60 ms** |

数据特征：`failure.mistake_mode=A.3`，返回 count 和 identity digest；不使用 GIN containment。

结论：两引擎的结构化表示均降低了本次低密度路径查询延迟。边界：路径选择率和谓词形式变化会改变索引或子列收益。

### 6.5 单路径投影

| 存储结构 | S04 wall p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | 389.15 / 418.51 ms | 99.522 ms |
| openGauss JSONB | **221.97 / 255.21 ms** | 102.561 ms |
| ClickHouse String JSON | **172.42 / 214.07 ms** | 226.935 ms |
| ClickHouse Native JSON | 173.40 / 243.35 ms | **146.953 ms** |

数据特征：投影 `gen_ai.output.messages`，返回非空值数量和 UTF-8 总字节数。

结论：openGauss JSONB 的 wall latency 低于 openGauss JSON；ClickHouse 两种结构的 wall p50 接近。边界：wall latency 不含客户端恢复；恢复值必须单列，不能与 wall 直接相加后称为服务端延迟。

### 6.6 完整 Trace

| 存储结构 | S05 wall p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | **20.95 / 25.97 ms** | 0.174 ms |
| openGauss JSONB | 21.13 / **24.05 ms** | 0.174 ms |
| ClickHouse String JSON | **54.38 / 62.28 ms** | **0.310 ms** |
| ClickHouse Native JSON | 62.92 / 106.71 ms | 0.647 ms |

数据特征：按固定 trace 回查六条记录，返回排序身份和完整 canonical attributes。

结论：openGauss 两种结构接近；ClickHouse String JSON 在本次完整 Trace 回查中较低。边界：Native 结果包含稀疏 Sidecar 返回，恢复在计时区间外。

### 6.7 完整文档分页

| 存储结构 | S06 wall p50 / p95 | 客户端恢复 p50 |
|---|---:|---:|
| openGauss JSON | **197.21 / 204.93 ms** | **1.936 ms** |
| openGauss JSONB | 459.78 / 477.82 ms | 2.016 ms |
| ClickHouse String JSON | **48.21 / 65.40 ms** | **2.956 ms** |
| ClickHouse Native JSON | 77.53 / 127.34 ms | 7.294 ms |

数据特征：固定 256 行页面，返回排序身份和完整 canonical attributes；每轮测量 20 次。

结论：**openGauss JSON 和 ClickHouse String JSON 在各自引擎内的完整文档分页更低**。边界：本输入的 attributes 最大约 62 KiB；更长 payload 由阶段三单独验证。

### 6.8 持续写入与后台 merge

原六结构实验在写入期间运行查询；每个样本使用启动查询前的已提交水位。样本数和水位分布受写入持续时间与查询耗时共同影响。

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

| 扩展 | 对照与关键数字 | 结论 | 边界 |
|---|---|---|---|
| openGauss 表达式索引 | 原六结构 Q02：112.49 → **20.59 ms** | 匹配稳定高频路径时收益明确 | 索引不匹配 Q05 |
| openGauss `jsonb_hash_ops` GIN | 原六结构 Q05：88.77 → **3.84 ms** | containment 与索引匹配时收益明确 | 维护和空间增加；不代表任意路径都有收益 |
| ClickHouse type hint | 最终 part：32 dynamic / 6 shared / **2 hinted** | hinted 路径使用固定类型且不占本版本 dynamic budget | 缺失路径返回类型默认值，恢复需 presence marker |

扩展结果不进入四种基础结构排名。openGauss 数字来自原六结构的三轮静态查询；ClickHouse type hint 来自一次机制观察，两者不能直接比较。

## 7. 原六结构实验保留结果

原实验的 18 个存储结构结果全部通过正确性与原文恢复门禁。下表保留其主要静态结果，防止补充四结构覆盖原证据边界。

| 存储结构 | 载入 rows/s | 分析/原文/总计 MiB | Q01 | Q02 | Q03 | Q04 | Q05 |
|---|---:|---:|---:|---:|---:|---:|---:|
| openGauss JSONB | 4,747 | 73.64/83.28/156.92 | 26.93 | 112.49 | 32.26 | 22.93 | 88.77 |
| openGauss JSONB + 表达式索引 | 4,638 | 75.88/83.28/159.16 | 26.89 | **20.59** | 32.42 | 23.05 | 88.67 |
| openGauss JSONB + GIN | 4,523 | 81.28/83.28/164.56 | 26.96 | 110.97 | 32.37 | 23.37 | **3.84** |
| ClickHouse String JSON | **5,860** | 15.35/17.65/33.01 | **7.88** | 85.31 | 9.14 | **13.38** | 69.58 |
| ClickHouse Map | 5,482 | 24.99/17.65/42.65 | 7.91 | 51.34 | **8.55** | 14.24 | 35.11 |
| ClickHouse Native JSON | 3,688 | 27.69/17.65/45.34 | 8.12 | **9.83** | 8.69 | 29.57 | **7.57** |

查询列均为 p50 ms。Q01/Q03 是稳定列聚合，Q02 是高频路径，Q04 是六行完整 Trace，Q05 是低密度路径。数据特征、查询返回和计时范围以[阶段二实验设计](json-storage-stage2-experiment-design-2026-09-09.md)为准。

空间仍采用两引擎不同的原生口径。ClickHouse Native JSON 在该实验中包含稀疏 Sidecar。补充四结构改变了查询集、轮次、载入流程和 ClickHouse Native JSON 存储结构，因此不得与本表逐格计算变化比例。

## 8. 正确性、异常与建议

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
