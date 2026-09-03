# JSON 存储第一阶段实验基础设施

> 状态：一次性 Spike 基础设施；openGauss 实验已运行，ClickHouse 正确性门禁已启动
> 范围：确定性数据、truth manifest、标准 openGauss JSON/JSONB 行存验证、固定版本端到端参照

本目录实现
[JSON 存储第一阶段穿刺实验设计](../../docs/json-storage-spike-experiment-design.md)
中的公共正确性与路径组织 profile，并提供标准 openGauss 6.0.0 的独立 loader。正确性探针
固定跨数据库实验的输入和判定口径，验证当前 exporter JSON schema 的读写语义；路径组织
实验比较同一 openGauss 实例内的 JSONB 索引机制。

## 数据契约

默认生成 300 条 canonical JSONL，12 个 case 各 25 条：

| Case | 验证目标 |
|---|---|
| `metadata_sql_null` | 顶层 `metadata` 列缺失，由 loader 映射为 SQL NULL |
| `metadata_path_missing` | `metadata` 存在，目标路径缺失 |
| `metadata_json_null` | 目标路径为显式 JSON null |
| `target_boolean`、`target_integer`、`target_number` | 布尔、整数和小数类型保持 |
| `conflict_string`、`conflict_integer`、`conflict_object`、`conflict_array` | 同一路径跨行类型冲突 |
| `escaped_path` | RFC 6901 中 `/` 与 `~` 的路径转义 |
| `array_order` | 数组顺序保持 |

记录中的 `case_id` 是 profile 对账字段，不属于候选业务 schema。数据库机制查询使用 truth
manifest 中的参数和预期 `event_id` 集合判定结果。

canonical JSON 使用 UTF-8、对象键排序和紧凑分隔符。对象键顺序不参与业务相等判断，数组
顺序参与判断。重复键无法安全进入 canonical JSON，因此保存在独立原始探针中；后续 loader
必须记录目标引擎是否接受该输入以及保留哪个值。

## 运行

生成器、ClickHouse runner 和 openGauss JSON 正确性 runner 只使用 Python 标准库。
`opengauss/run_path_organization.py` 额外需要 `psycopg>=3.1`。当前工作区可复用
`../trace-synthesis/.venv` 的 benchmark 依赖；独立 checkout 可建立本仓虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'psycopg[binary]>=3.1'
```

从仓库根目录执行：

```bash
python3 experiments/json-storage-stage1/generator/generate.py \
  --output docs/temp/json-storage-stage1/correctness-seed-20260902 \
  --count 300 \
  --seed 20260902
```

输出目录包含：

| 文件 | 内容 |
|---|---|
| `dataset.jsonl` | canonical 逻辑记录 |
| `truth-manifest.json` | 每行 canonical hash、列/路径状态、类型、查询参数和预期命中 ID |
| `duplicate-key-probes.jsonl` | 保留重复键的原始 JSON 探针 |
| `run-manifest.json` | seed、行数、完成状态、生成器身份及各文件 SHA-256 |

`run-manifest.json` 最后写入。缺少该文件或 `status` 不为 `complete` 的目录不构成有效运行。
覆盖运行会先移除旧 manifest；消费者还必须逐项验证其中的文件长度和 SHA-256。
运行产物位于 gitignored 的 `docs/temp/`，正式数据库实验按实验设计另行选择需要保留的 run。

## openGauss 6.0.0 正确性探针

`opengauss/run_correctness.py` 使用已经运行的标准 openGauss 6.0.0 容器。每次运行创建唯一
临时 schema，使用 exporter `54ca553a7ed09ad1751c82adab3aa52c6e9357b1` 的 26 列
`events` 行存 DDL，其中 `tags`、`input`、`output`、`metadata` 为 `JSON`。探针只创建
`events` 和重复键观察表，不创建索引、去重 view、`ingest_batches` 或 `scores`。

独立 loader 把公共记录投影到 `events`，使用 `span_id` 关联 truth manifest 中的
`event_id`。它验证 JSON 列和查询语义，不经过 Collector、exporter 或 benchmark，不能作为
端到端写入或性能基线。蓝区行存与黄区 dstore 属于两个系统环境的部署事实，本探针不输出
行存与列存的机制比较结论。

12 行真实集成测试：

```bash
RUN_OPENGAUSS_INTEGRATION=1 \
  python3 -m unittest \
  experiments/json-storage-stage1/tests/test_opengauss_correctness.py -v
```

300 行正式正确性运行：

```bash
python3 experiments/json-storage-stage1/opengauss/run_correctness.py \
  --input docs/temp/json-storage-stage1/correctness-seed-20260902 \
  --output docs/temp/json-storage-stage1/opengauss-v6-row-json-correctness-20260902 \
  --container-name agent-trace-opengauss-v6 \
  --schema-name json_stage1_run_20260902
```

2026-09-02 的运行结果为：

- 数据库：`openGauss 6.0.0 build aee4abd5`；镜像 digest
  `sha256:9bd81380273944e5a02a2139c90954d4f46813b71810f7b23fe8f738014d03b5`；
- 300 行载入，7 个 truth 查询的 ID 集合与预期完全一致；
- 300 行 metadata 回读 canonical hash 全部一致；
- SQL NULL、路径 missing、JSON null 和跨行类型冲突均可区分；
- 转义键与数组顺序查询结果正确；
- 重复键原始 JSON 被接受，路径读取保留末值 `2`；
- 运行时 `pg_type` 中存在 `jsonb` 类型；本次表和查询仍使用 `JSON`；
- 临时 schema 已删除，正式结果位于上述 gitignored 输出目录。

运行 manifest 固定 benchmark `9529c8f389673132757f4da9a96878926f22b94f` 与 exporter
`54ca553a7ed09ad1751c82adab3aa52c6e9357b1`。其中提交号是 schema 与后续链路的版本
身份；本次数据路径明确记录为 `independent_loader`。

## 蓝区系统级参照

### 固定输入与环境

2026-09-02 使用固定版本 exporter 与 benchmark 运行标准 openGauss 6.0.0 行存参照：

| 项目 | 固定值 |
|---|---|
| benchmark | `9529c8f389673132757f4da9a96878926f22b94f` |
| exporter | `54ca553a7ed09ad1751c82adab3aa52c6e9357b1` |
| collector 二进制 SHA-256 | `8b7afdc9bd1e39b91c3d795f6e0c3203223a2e0f7ef8be8072fb79ad9572e4cb` |
| Collector | OpenTelemetry Collector `v0.158.0` |
| 数据库 | `openGauss 6.0.0 build aee4abd5` |
| 镜像 | `enmotech/opengauss:6.0.0`，digest `sha256:9bd81380273944e5a02a2139c90954d4f46813b71810f7b23fe8f738014d03b5` |
| 输入 | `whowhen-pro` text split，seed `42`，6257 traces / 48,534 spans |
| 输入 manifest SHA-256 | `f46bbe843c5578faea9ddfb5e8eb3aac8b6dc4c2f4fb89beabab503043505e38` |
| 输入 JSONL SHA-256 | `3ff85d5060c765b3606cb2d620c3c5fd1815520c93153a61245e91d83b35c683` |
| SQL catalog | revision `2026-09-01.9`，`sha256:af71358713b65cd00147a924c027695cdf56c6fe49db24e7a74f71da417b4ae0` |

输入扫描发现 8 个 span 的 metadata 超过 exporter 默认 64 KiB，最大为 83,735 bytes；
input 和 output 最大值分别为 61,485 和 61,457 bytes。系统级参照使用
[`config/otelcol-opengauss-row-1mib.yaml`](config/otelcol-opengauss-row-1mib.yaml)，
把 `max_attr_value_length` 与 benchmark `--truncation-threshold` 同步固定为 1 MiB。
配置文件 SHA-256 为
`d745ae555c87cfb87edd1acb5b6c5563dc31a37ca0198fca55e0ad08495feff0`。

固定 exporter 使用 `--config` 和环境变量启动。首次 metrics 序列尚未建立时，固定
benchmark 的本地复用探测会在 warmup 导出完成前读取 metrics，随后把已占用的 4318 端口
报告为 `port_conflict`。运行前发送一个与输入 ID 不重合的随机 readiness span，并等待该行
和三项必需 metrics 可见。不得使用待测输入做该预热；exporter 的进程内去重窗口在数据库
清理后仍保留 ID，会使正式 Replay 缺行。

### 批量门禁

默认 `--batch-spans 8192` 的试运行出现 3 个失败 POST，共丢弃 24,576 spans。使用相同
Replay 组包代码的本地诊断中，8192-span 配置的最大请求体为 28,817,555 bytes；
1024-span 配置的最大请求体为 4,525,851 bytes。有效运行固定
`--batch-spans 1024`，完成 48,534/48,534 spans 发送和落库。

### 查询门禁

全场景试运行通过 schema、自恢复清理、Replay 和完整性门禁，Q14 在预热阶段被
openGauss 6.0.0 拒绝：

```text
column "events_dedup.metadata" must appear in the GROUP BY clause or be used in an aggregate function
```

固定 Q14 SQL 的 SELECT 表达式使用第一个 `metadata_key` 占位符，`GROUP BY` 表达式使用
第七个占位符。两者绑定值相同，服务器仍按不同参数表达式处理。该结果不形成全场景性能
基线；固定 benchmark 源码和 catalog 保持不变。

### 有效子集结果

不包含聚合组 Q10、Q11、Q13、Q14 的参照子集为 Q01–Q09 和 Q15。运行参数为一次预热、
五次正式测量、查询并发 1、`--batch-spans 1024`。结果如下：

- Replay 发送耗时 16.620 s，可见性等待 29.377 s，全流程 45.998 s；
- Replay 速率 2920.13 spans/s，Collector 到完整可见速率 1055.14 spans/s；
- 48,534 spans 的 missing、extra 和 duplicate 均为 0；
- Collector 接收和导出均为 48,534 spans，拒绝为 0，队列峰值为 17；
- Collector CPU 为 11.2 s，RSS 峰值为 364,446,000 bytes；
- 10 个查询的 50 次正式请求全部成功；
- 默认清理完成，`events`、`ingest_batches` 和 `scores` 均为 0；临时 schema 和用户已删除。

| 查询 | 平均延迟 | P50 | P95 |
|---|---:|---:|---:|
| Q01 | 39.40 ms | 39.41 ms | 40.70 ms |
| Q02 | 1.82 ms | 1.24 ms | 3.61 ms |
| Q03 | 2.44 ms | 2.13 ms | 3.45 ms |
| Q04 | 248.98 ms | 244.13 ms | 274.07 ms |
| Q05 | 228.61 ms | 226.48 ms | 248.48 ms |
| Q06 | 445.18 ms | 476.61 ms | 512.54 ms |
| Q07 | 619.54 ms | 449.06 ms | 1220.13 ms |
| Q08 | 483.10 ms | 485.52 ms | 518.14 ms |
| Q09 | 2.64 ms | 2.20 ms | 4.00 ms |
| Q15 | 471.31 ms | 476.39 ms | 513.03 ms |

Q08 的五次请求均返回 0 行，只证明空结果路径执行成功，不构成 ERROR/tool 文本正向命中
证据。以上数字来自单次本机运行，用于固定蓝区系统参照和发现运行约束，不构成蓝黄性能
比较或行列存机制结论。运行期间暂停了 Open-SWE 后台下载，结束后恢复下载。

## openGauss JSONB 路径组织实验

### 数据与布局

`generator/generate_path_profile.py` 生成 `metadata.paths.pNNNNN` 动态路径全集，并固定
`metadata.hot.tenant` 和 `metadata.hot.region` 两个热点字段。`path_count` 只统计动态路径；
热点字段不参与密度计算。密度以 100 行为周期，每个动态路径在一个周期内恰好出现 1、20
或 95 次。所有 profile 使用 seed `20260902`，通过调整行数把 canonical JSONL 控制在约
128 MiB：

| 动态路径数 | 密度 | 行数 | JSONL 大小 |
|---:|---:|---:|---:|
| 50 | 1% | 340,600 | 128.006 MiB |
| 50 | 20% | 250,200 | 127.988 MiB |
| 50 | 95% | 122,200 | 128.028 MiB |
| 500 | 1% | 291,100 | 128.003 MiB |
| 500 | 20% | 71,200 | 128.068 MiB |
| 500 | 95% | 17,900 | 128.220 MiB |
| 5000 | 1% | 118,100 | 127.956 MiB |
| 5000 | 20% | 8,700 | 127.658 MiB |
| 5000 | 95% | 1,900 | 129.803 MiB |

生成器 SHA-256 为
`7014874653d46e57653fd82a1235f79d158fc6ef1b38aa539fd982ab8c23890c`。9 个输入
manifest 的文件长度和 SHA-256 已逐项复算，均与产物一致。
生成器逐行写入临时 dataset 和 truth row 文件，流式计算文件 SHA-256，最后原子发布两个
artifact 和完成 manifest；覆盖运行在生成前移除旧完成 manifest。

`opengauss/run_path_organization.py` 使用 `independent_loader`，在同一临时 schema 中建立
三张 `JSONB` 行表：

| 布局 | JSON 索引 |
|---|---|
| `no_index` | 无 |
| `gin` | `metadata jsonb_ops` GIN 通用索引 |
| `hot_expression` | `metadata.hot.tenant` 文本表达式 B-tree 索引 |

每张表分别通过 psycopg COPY 载入同一 JSONL。载入计时包含 JSONB 转换和索引维护，不包含
建空表与 `ANALYZE`。三张表按 `no_index`、`gin`、`hot_expression` 固定顺序运行。热点查询
固定命中 12.5% 的行；冷路径查询的命中比例约为路径密度除以 17。无索引和 GIN 布局使用
同一 `@>` containment 谓词；热点表达式布局只在热点查询使用表达式谓词，冷路径仍使用
containment。每个查询预热一次并测量五次；EXPLAIN 与计时使用同一条含排序的 SQL。表内
完整逻辑记录逐行重建，并与 truth 的整行 canonical hash 校验。

runner SHA-256 为
`74f5151f9316fe72f8e0614b97e6c567aedbcf71650bb73c47cb59503b0d45a5`。运行使用与正确性
探针相同的标准 openGauss 6.0.0 镜像和 digest。9 组运行均通过 truth、回读和索引能力门禁，
manifest 记录实际 DDL SHA-256，且只在临时 schema 删除成功后发布。运行后临时 schema 数
为 0，runner 峰值 RSS 为 132.3–296.4 MiB。runner 同时校验容器 5432 端口的本机发布映射，
并要求 psycopg 实际连接的服务端版本与容器内 gsql 一致。原始结果位于 gitignored 的
`docs/temp/json-storage-stage1/opengauss-jsonb-path-runs/`。

### 载入与空间

下表时间单位为秒，空间单位为 MiB。`total` 使用 `pg_total_relation_size`，包含 TOAST；
`index` 使用 `pg_indexes_size`。

| 路径×密度 | 无索引 load / total | GIN load / index / total | 热点索引 load / index / total |
|---|---:|---:|---:|
| 50×1% | 5.123 / 91.805 | 5.377 / 7.711 / 99.516 | 5.604 / 18.508 / 110.312 |
| 50×20% | 5.579 / 107.055 | 8.625 / 14.625 / 121.680 | 5.819 / 13.594 / 120.648 |
| 50×95% | 5.139 / 136.453 | 10.923 / 17.852 / 154.305 | 5.471 / 6.633 / 143.086 |
| 500×1% | 5.102 / 103.430 | 7.067 / 14.359 / 117.789 | 5.128 / 15.828 / 119.258 |
| 500×20% | 4.976 / 139.125 | 12.251 / 32.625 / 171.750 | 5.231 / 3.844 / 142.969 |
| 500×95% | 8.307 / 149.250 | 16.898 / 22.188 / 171.438 | 8.610 / 0.945 / 150.195 |
| 5000×1% | 5.243 / 131.875 | 12.991 / 63.852 / 195.727 | 6.096 / 6.406 / 138.281 |
| 5000×20% | 8.696 / 156.406 | 20.681 / 16.023 / 172.430 | 9.044 / 0.461 / 156.867 |
| 5000×95% | 9.780 / 154.289 | 23.368 / 15.555 / 169.844 | 10.132 / 0.094 / 154.383 |

### 查询

下表为五次测量的中位数，单位为毫秒。括号表示自然计划：`S` 为顺序扫描，`G` 为 GIN，
`B` 为热点表达式 B-tree。禁用顺序扫描后的能力计划在 9 组中均能使用目标索引；该能力门禁
不等同于优化器自然选择索引。

| 路径×密度 | 热点：无索引 | 热点：GIN | 热点：表达式 | 冷路径：无索引 | 冷路径：GIN | 冷路径：热点布局 |
|---|---:|---:|---:|---:|---:|---:|
| 50×1% | 221.84 (S) | 100.93 (G) | 75.20 (B) | 170.70 (S) | 10.01 (G) | 150.58 (S) |
| 50×20% | 163.73 (S) | 70.61 (G) | 49.04 (B) | 123.49 (S) | 43.48 (G) | 120.95 (S) |
| 50×95% | 93.71 (S) | 42.99 (G) | 31.92 (B) | 88.39 (S) | 110.51 (G) | 84.98 (S) |
| 500×1% | 171.12 (S) | 81.22 (G) | 64.06 (B) | 130.84 (S) | 7.88 (G) | 129.53 (S) |
| 500×20% | 54.70 (S) | 23.13 (G) | 24.46 (B) | 53.97 (S) | 26.56 (G) | 58.08 (S) |
| 500×95% | 107.60 (S) | 107.42 (S) | 3.12 (B) | 106.34 (S) | 107.38 (S) | 110.06 (S) |
| 5000×1% | 82.41 (S) | 42.46 (G) | 34.04 (B) | 73.13 (S) | 7.62 (G) | 81.11 (S) |
| 5000×20% | 78.53 (S) | 80.30 (S) | 2.42 (B) | 74.41 (S) | 76.04 (S) | 78.35 (S) |
| 5000×95% | 52.64 (S) | 57.74 (S) | 1.74 (B) | 50.41 (S) | 55.45 (S) | 53.17 (S) |

本次单机运行得到以下机制证据：

- 热点表达式索引在 9 组自然计划中全部被采用，索引大小为 0.094–18.508 MiB；热点查询
  中位数均低于对应无索引布局。
- GIN 自然计划在 6 组中被采用；`500×95%`、`5000×20%` 和 `5000×95%` 选择顺序扫描。
- 冷路径选择性较低时 GIN 明显缩短查询；`50×95%` 中 GIN 被自然采用但中位数高于顺序
  扫描，说明通用索引可用性本身不足以保证收益。
- GIN 索引为 7.711–63.852 MiB，并在全部 9 组中增加载入时间。固定热点索引的空间随行数
  变化，宽路径数量不直接扩大该索引。

这些数字来自单次本机运行，用于选择下一阶段机制。固定载入顺序、缓存状态和单次载入样本
限制了小幅差异的解释；本结果不构成蓝区 openGauss 与黄区 GaussVector 的性能比较。

从 `agent-trace-research` 根目录生成和运行单组 profile：

```bash
../trace-synthesis/.venv/bin/python \
  experiments/json-storage-stage1/generator/generate_path_profile.py \
  --output docs/temp/json-storage-stage1/path-profiles/paths-500-density-20 \
  --path-count 500 \
  --density-percent 20 \
  --target-bytes 134217728 \
  --seed 20260902

../trace-synthesis/.venv/bin/python \
  experiments/json-storage-stage1/opengauss/run_path_organization.py \
  --input docs/temp/json-storage-stage1/path-profiles/paths-500-density-20 \
  --output docs/temp/json-storage-stage1/opengauss-jsonb-path-runs/paths-500-density-20 \
  --container-name agent-trace-opengauss-v6 \
  --port 15432 \
  --schema-name json_path_p500_d20_20260902 \
  --measurements 5
```

## ClickHouse native JSON 路径组织探针

`clickhouse/run_path_organization.py` 使用与 openGauss JSONB 实验相同的路径 profile 和 truth，
建立以下 MergeTree 布局：

| 布局 | metadata 定义 |
|---|---|
| `string` | `String CODEC(ZSTD(3))`，查询时使用 `JSONExtractString` |
| `native_limited` | `JSON(max_dynamic_paths=100)` |
| `native_hinted` | `JSON(max_dynamic_paths=1000, hot.tenant String, hot.region String)` |

当前环境固定 ClickHouse `25.12.11.4`，镜像 digest
`sha256:8a790dd3468db22b1d4e7b18a176f378ff5ff6053b9c48dd4ea1fa71a24c5ba6`。每张表分四个
INSERT part 载入；零层 part 使用 `map_with_buckets`，`OPTIMIZE FINAL` 后使用 `advanced`。
runner 记录 merge 前后空间、dynamic/shared 路径数、热点/冷路径查询、整对象读取和 metadata
canonical hash，并在结束时删除临时数据库。

500 路径×20% 密度、200 行的真实集成测试已通过，证明低预算表进入 shared data、高预算表
保留全部路径，查询与 hash 门禁均可执行。首个 128 MiB 正式组 50 路径×20% 密度载入
250,200 行后触发正确性停止条件：

- `string` 的 250,200 条 metadata hash 全部一致；
- 两个 native JSON 布局各有 77,562 条 hash 不一致；
- 不一致行均属于 `metadata.paths` 为空对象的记录，ClickHouse 重建时省略该空对象；
- 热点与冷路径过滤的 event ID 集合在三种布局中均与 truth 一致；
- 修正后的路径统计为：低预算表 52 dynamic / 0 shared，高预算+hint 表 50 dynamic / 0 shared；
  两个 type hint 不计入 dynamic 路径预算。

该 run 的 manifest 状态为 `failed`，原始结果位于 gitignored 的
`docs/temp/json-storage-stage1/clickhouse-json-path-runs/paths-50-density-20/`。该结果证明 native
JSON 的叶路径查询能力，同时证明其不满足当前“完整 metadata 结构可重建”的契约。明确空容器
业务语义或增加原始 String sidecar 前，不继续扩展九组 native JSON 性能矩阵。

运行真实集成测试：

```bash
RUN_CLICKHOUSE_INTEGRATION=1 \
  python3 -m unittest \
  experiments/json-storage-stage1/tests/test_clickhouse_path_organization.py -v
```

## 验证

```bash
python3 -m unittest discover -s experiments/json-storage-stage1/tests -v
```

测试验证同 seed 字节级复现、不同 seed 的身份变化、12 类正确性覆盖、三类空值、JSON 类型、
路径转义、数组顺序、重复键原始证据，以及路径 profile 的密度、truth、目标大小和谓词公平性。
默认测试跳过真实数据库集成测试；设置 `RUN_OPENGAUSS_INTEGRATION=1` 或
`RUN_CLICKHOUSE_INTEGRATION=1` 后运行对应数据库集成路径。

同时运行当前两个数据库集成门禁时，需要使用含 `psycopg` 的解释器：

```bash
RUN_OPENGAUSS_INTEGRATION=1 RUN_CLICKHOUSE_INTEGRATION=1 \
  .venv/bin/python -m unittest discover \
  -s experiments/json-storage-stage1/tests -v
```
