# Agent Trace JSON 存储阶段二补充实验设计：四结构横比与 ClickHouse 机制观察

> 状态：设计已确认，待实现和正式运行
> 日期：2026-09-08
> 数据路径：`independent_loader`
> 基础契约：`json-storage-cross-engine-v1`
> 补充契约：`json-storage-four-layout-v1`

## 1. 目标与证据边界

本实验在同一真实 Trace 输入、查询语义、返回内容、正确性和原文恢复契约下比较四种基础 JSON 存储结构：openGauss JSON、openGauss JSONB、ClickHouse String JSON 和 ClickHouse Native JSON。实验回答以下问题：

1. 四种结构在载入、路径过滤、路径投影和完整文档读取中的性能差异；
2. openGauss JSON 文本与 JSONB 二进制文档在同一引擎内的转换、查询和空间成本；
3. ClickHouse Native JSON 的 type hint、动态路径预算、data part、merge、`OPTIMIZE FINAL` 和 Sidecar 在完整流程中的作用；
4. 四种候选布局在统一 workload 下的适用场景。

[阶段二正式报告](json-storage-stage2-report-2026-09-08.md)及其正式根保持冻结。本实验使用新契约、新 run ID 和独立结果目录，不覆盖或重新解释阶段二已有结果。[阶段三实验](json-storage-stage3-experiment-design-2026-09-08.md)继续负责 Full/Core、长 payload 和 asset reference。

本实验不覆盖 Map、饱和吞吐、冷缓存、多节点、故障恢复、Collector/exporter 全链路和对象存储。数据库空间保留引擎原生口径，不计算跨引擎压缩比例。

## 2. 产物与目录

正式代码位于 `experiments/json-storage-stage2-sup/`：

```text
experiments/json-storage-stage2-sup/
  README.md
  generator/generate_supplement_truth.py
  runner/common.py
  runner/opengauss.py
  runner/clickhouse.py
  runner/run_four_layouts.py
  runner/run_clickhouse_mechanisms.py
  report/summarize_results.py
  tests/
```

运行产物位于 gitignored 的 `docs/temp/json-storage-stage2-sup/`：

```text
docs/temp/json-storage-stage2-sup/
  input-20260908/
  smoke-*/
  formal-*/
  clickhouse-mechanisms-*/
```

`input-20260908/` 只保存补充 truth、查询 catalog 和来源 manifest，不复制 302,518,948 bytes 的阶段二 dataset。来源文件路径、字节数和 SHA-256 写入 manifest。正式结果完成后生成 `docs/json-storage-stage2-sup-pre-YYYY-MM-DD.md`，作为组内进展汇报辅助材料。

## 3. 输入与预处理边界

输入复用阶段二冻结数据：

| 项目 | 固定值 |
|---|---|
| dataset | `docs/temp/json-storage-stage2/cross-engine-input-20260907/dataset.jsonl` |
| 行数 | 48,534 |
| dataset SHA-256 | `8de6be1f74f075b12d598d15bf48e2bbae57c6e3da9472c909afcd42fccc3405` |
| 原始输入 SHA-256 | `3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683` |
| block | 256 行，共 190 个，末 block 150 行 |
| Native 动态路径预算 | 32 |

公共生成器在数据库计时前完成以下步骤，并单独记录 wall time、输入字节和输出字节：

1. 读取原始 JSONL bytes；
2. 解析事件和 Attribute；
3. 把点分隔 Attribute key 投影为可逆嵌套路径；
4. 生成 canonical analysis JSON、Map 辅助值、稀疏 fidelity 值和 raw bytes；
5. 计算查询 truth、canonical hash 与 raw SHA-256。

数据库载入计时从提交一个预生成 block 开始，到该 block 成功且可见结束。客户端预处理、数据库载入和维护分别报告，避免把一次性数据准备解释为 JSON 类型的数据库成本。

## 4. 四结构正式横向矩阵

### 4.1 基础布局

所有布局具有相同的身份、时间、项目和稳定分析列，并保存独立 raw 表。

| layout ID | 引擎 | residual 结构 | 附加加速 |
|---|---|---|---|
| `og_json` | openGauss 6.0.0 | `attributes JSON NOT NULL` | 无 |
| `og_jsonb` | openGauss 6.0.0 | `attributes JSONB NOT NULL` | 无 |
| `ch_string` | ClickHouse 25.12.11.4 | `attributes String CODEC(ZSTD(3))` | 无 |
| `ch_native` | ClickHouse 25.12.11.4 | `attributes JSON(max_dynamic_paths=32)` | 稀疏 `fidelity_values Map(String,String)` |

`ch_native` 的稀疏 Sidecar 仅保存递归包含 JSON null、空对象或空数组的原始 Attribute canonical value。它与 Native JSON 一起构成可恢复的分析布局，成本计入载入、空间和完整读取。四结构都通过独立 raw 表恢复摄入原文。

基础矩阵不创建 GIN、表达式索引、type hint、投影或物化热点列。Native JSON 的自动动态子列属于该类型的基础物理组织，不视为额外索引。

### 4.2 公共查询

全部查询固定 project、时间边界和可见水位。路径查询采用相同逻辑谓词，允许各类型使用对应的原生读取表达式。

| ID | 场景 | 返回契约 | 正式测量 |
|---|---|---|---:|
| S01 | 稳定列分组 | 排序后的 `span_type,count` | 100 |
| S02 | 高频路径等值过滤与分组 | 排序后的 `span_type,count` | 100 |
| S03 | 低密度路径等值过滤 | `count` 与排序 identity digest | 100 |
| S04 | 单路径投影 | 非空值数量与 UTF-8 总字节数 | 100 |
| S05 | 固定 trace 完整回查 | 排序身份与 canonical attributes | 100 |
| S06 | 固定 256 行完整文档页 | 排序身份与 canonical attributes | 20 |

S02、S03 和 S04 的 openGauss JSON/JSONB 使用相同 `#>>` 路径文本提取语义。ClickHouse String 使用 JSON 提取函数，Native 使用直接子列语法。S03 不使用 JSONB containment；GIN containment 只进入优化扩展证据。

查询延迟从已建立连接提交语句开始，到响应 bytes 完整读取结束。结果规范化、Native Sidecar 合并、canonical 序列化、hash 和 truth 核对位于该区间外，并另行记录客户端恢复耗时。S06 报告 median、p95 和范围，不报告 p99。

### 4.3 轮次与顺序

四种布局各执行四轮，采用以下 Latin square 顺序；任一时刻只有一个布局执行载入或查询：

| 轮次 | 顺序 |
|---|---|
| 1 | `og_json, og_jsonb, ch_string, ch_native` |
| 2 | `og_jsonb, ch_native, og_json, ch_string` |
| 3 | `ch_string, og_json, ch_native, og_jsonb` |
| 4 | `ch_native, ch_string, og_jsonb, og_json` |

每个布局按 190 个公共 block 载入。openGauss 完成 `ANALYZE`；ClickHouse 等待目标 database 的 merge backlog 连续三次为零。每个查询预热一次后执行正式测量。缓存状态固定为 `query_warmup_1_no_os_cache_drop`。

本矩阵不运行写入期间并发查询。阶段二已有并发证据使用不同布局集合，继续单独引用；补充实验集中控制四结构载入和静态查询变量。

## 5. 引擎内优化扩展

### 5.1 openGauss

openGauss 内部结果分为基础类型和能力扩展：

| 布局 | 目的 |
|---|---|
| `og_json` / `og_jsonb` | 隔离 JSON 文本与 JSONB 的基础载入、路径查询和整文档读取 |
| `og_json_hot` / `og_jsonb_hot` | 使用同一热点路径表达式 B-tree，比较索引维护和查询收益 |
| `og_jsonb_gin` | 使用 `jsonb_hash_ops` 和 containment，记录 JSONB 特有能力 |

热点表达式必须先通过 openGauss 6.0.0 能力探针，确认 JSON 与 JSONB 的表达式和自然计划均可用。`og_jsonb_gin` 不进入 JSON/JSONB 成对比例，也不进入基础四结构排名。

### 5.2 ClickHouse

ClickHouse 机制观察建立以下布局：

| 布局 | 控制变量 | 用途 |
|---|---|---|
| `ch_native_auto32_none` | 预算 32，无 Sidecar | 观察 Native 自身语义边界 |
| `ch_native_auto32_sparse` | 预算 32，稀疏 Sidecar | 对应基础矩阵的 `ch_native` |
| `ch_native_auto32_full` | 预算 32，完整 canonical Sidecar | 观察完整 Sidecar 的载入、空间和整文档读取成本 |
| `ch_native_hinted32_sparse` | 预算 32，两个热点 type hint，稀疏 Sidecar | 在相同预算下隔离手动 type hint |

该组是机制观察，不形成新的全布局性能排名。String/Native 正式性能来自四结构矩阵；上述变体记录 DDL、载入、空间、路径库存、正确性和固定查询观察值。

## 6. ClickHouse 载入、merge 与 FINAL 流程

机制 runner 对其拥有的临时表执行以下步骤：

1. 创建 MergeTree 表，并对该表暂停后台 merge；
2. 写入多个 block，记录首个 part 和全部零层 part 的列、压缩空间、dynamic/shared paths 与实际类型；
3. 使用相同 truth 完成 merge 前查询；
4. 恢复该表后台 merge，等待 backlog 稳定并记录新 part；
5. 执行 `OPTIMIZE TABLE ... FINAL`，记录强制合并耗时和最终 part；
6. 重复正确性查询，核对逻辑结果保持一致；
7. 在 `finally` 路径恢复 merge 并删除临时 database。

路径在 merge 后从 shared data 进入 dynamic subcolumn，或从 dynamic subcolumn 进入 shared data，只表示物理组织变化。type hint 路径使用声明类型，并与自动动态路径预算分开统计。

`SELECT ... FINAL` 不进入四结构正式查询。独立正确性探针使用小型 `ReplacingMergeTree(version)` 创建跨 part 重复键，分别验证普通查询、查询时 `FINAL` 和 `OPTIMIZE TABLE ... FINAL`：查询时 `FINAL` 在读取阶段应用合并语义；`OPTIMIZE ... FINAL` 强制生成合并后的物理 part。该探针只解释概念，不输出性能结论。

## 7. 正确性、指标与 Manifest

所有正式布局必须通过以下门禁：

- 48,534 条分析记录与 raw 记录一一对应；
- identity 没有缺失、额外或重复；
- S01～S06 的结果与独立 truth 一致；
- analysis canonical hash 全部匹配；
- raw UTF-8 bytes SHA-256 全部匹配；
- Native 的分析等价、逻辑文档恢复和原文恢复分别记录；
- 临时 schema、database、暂停 merge 状态和 active merge 全部清理。

每轮记录预处理、analysis INSERT、raw INSERT、完整 block、维护和查询的分项耗时。openGauss 记录 heap、TOAST、index 和总分配空间；ClickHouse 记录 active part 压缩/未压缩 bytes、part、dynamic/shared paths 和 QueryFinish 指标。两引擎共同报告客户端完整响应延迟、返回 bytes、正确性和请求等价速率；引擎专有指标不做数值等同。

manifest 记录 run ID、状态、完整命令、四布局顺序、输入 lineage、数据和 truth SHA-256、代码/DDL/查询摘要、容器镜像 digest、服务端版本、宿主资源、测量参数、正确性、维护、产物 SHA-256 和 cleanup。失败轮次使用新 run ID 重跑，失败产物只作诊断。

## 8. 执行门槛与停止条件

正式运行前依次完成：

1. 文档链接和命名检查；
2. 单元测试；
3. openGauss JSON/JSONB 运算符与表达式索引能力探针；
4. ClickHouse type hint、表级 merge 控制和路径库存探针；
5. 每引擎至少一个小数据 smoke run；
6. 四轮正式运行与确定性汇总。

Docker daemon、固定镜像或端口不可用时停止数据库 smoke 和正式运行，代码与单元测试可以继续。输入身份、truth、容器版本、查询结果、恢复或 cleanup 任一门禁失败时发布诊断结果，不进入正式汇总。

## 9. 汇报辅助材料

`json-storage-stage2-sup-pre-YYYY-MM-DD.md` 按以下结构组织：

1. 当前问题、实验范围和结论摘要；
2. openGauss JSON/JSONB 载入与查询流程图；
3. openGauss JSON 与 JSONB 引擎内结果；
4. ClickHouse String/Native 载入、动态子列、shared data、merge 和 FINAL 流程图；
5. Native 信息边界及无 Sidecar、稀疏 Sidecar、完整 Sidecar 的作用；
6. 四结构在各场景下的统一横向表；
7. 证据边界、当前建议和阶段三入口。

流程图使用仓内 SVG，图中文字和指标可独立阅读。材料区分已完成正式结果、机制观察和设计说明；比例只在相同数据、查询、返回契约和同轮实验内计算。

## 10. 完成标准

- 文档命名统一为带日期形式，报告主题只写入标题；
- 四布局各四轮共 16 个正式 result 全部完成；
- S01～S05 每布局每轮各 100 个样本，S06 各 20 个样本，全部通过 truth；
- ClickHouse 四种机制布局和 FINAL 正确性探针完成；
- 汇总器从原始产物重算结果，两次输出 bytes 和 SHA-256 一致；
- 汇报辅助材料中的数字均可定位到正式 summary 或机制 manifest；
- 运行后无实验临时数据库对象、暂停 merge 状态或 active merge。

## 11. 参考资料

- [阶段一实验设计](json-storage-stage1-experiment-design-2026-09-08.md)
- [阶段一报告](json-storage-stage1-report-2026-09-08.md)
- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-08.md)
- [阶段二报告](json-storage-stage2-report-2026-09-08.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-08.md)
