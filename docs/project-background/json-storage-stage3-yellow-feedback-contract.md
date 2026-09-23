# 阶段三黄区结果反馈契约

本文规定黄区每一轮实验的重跑范围、统计口径、保留物与回传格式。执行[黄区执行指南](json-storage-stage3-xstore-clickhouse-yellow-guide.md)得到运行产物之后，按本文回传。

两台 ARM 主机执行相同的实验，各自独立回传，回传内容不互相引用。

## 1. 运行环境变量

工具包不再携带运行账号与动态库路径，两项由环境提供，运行前导出：

```bash
export XSTORE_USER=<XStore 运行账号>
export GAUSSDB_LIB_DIR=<含 libpq.so.5 的目录>
export GAUSSDB_SERVER_LIB_DIR=<GaussDB 服务端库目录>   # 可选，缺失时不加入搜索路径
```

未导出 `XSTORE_USER` 时 adapter 拒绝构造；未导出 `GAUSSDB_LIB_DIR` 时首次连接报出缺失原因。
两项都不影响 ClickHouse 侧。

## 2. 运行前检查

正式运行前采集下列事实，任一项不满足时先处理再运行，不要带着背景负载测量。

| 检查 | 命令 | 通过条件 |
|---|---|---|
| 系统负载 | `uptime` | 1 分钟平均负载低于 CPU 核数的 10% |
| 无其他数据库进程 | `ps -eo pid,comm,pcpu,rss --sort=-pcpu \| head -20` | 除本实验的 XStore 与 ClickHouse 外，无占用 CPU 超过 5% 的进程 |
| 内存可用量 | `free -g` | available 大于 32 GB |
| 磁盘余量 | `df -h $YELLOW_STATE $YELLOW_OUTPUT` | 可用空间大于 200 GB |
| 磁盘无持续写入 | `vmstat 1 5` | 最后三次采样的 `bo` 均低于 10,000 |
| 时钟 | `date -u` | 与另一台的偏差在 2 秒内 |

上述六项的原值随回传给出。两个引擎串行执行，同一时刻只运行一个引擎的目标。

## 3. 统一口径

以下定义在全部场景、全部回传字段上取同一含义。回传时不做二次换算。

### 3.1 时延

单个查询样本的应用可用时间为该样本的查询完成、客户端恢复与正确性校验三段之和，由程序按样本记录，不由分项中位数相加得到。

多轮聚合按固定顺序：**先在轮内对该查询目标的全部样本取中位数，再对四轮的轮内中位数取中位数**。回传的 p50 一律指这个「四轮轮级中位数」。不要先把四轮样本合并再取中位数，两者结果不同。

`correctness_only` 只有一轮，其 p50 即该轮的轮内中位数。

### 3.2 写入

写入有两个不同的量，回传时都要给，不要互相替代。

| 名称 | 定义 | 来源字段 |
|---|---|---|
| 写入合计 | 一轮中 190 个 block 的 `ingest.wall_ms` 之和，再取四轮的中位数 | `workloads.main.rounds[*].write.blocks[*].ingest.wall_ms` |
| 单块 p50 | 一轮的 `block_wall_ms.median`，再取四轮的中位数 | `workloads.main.rounds[*].write.block_wall_ms.median` |

「合计」只指前者。历史回传中这两个量曾被混用，量级相差约 190 倍。

### 3.3 空间

空间取该 target **最后一轮**的证据，不跨轮平均。

| 引擎 | 字段 | 来源 |
|---|---|---|
| XStore | `heap_bytes`、`index_bytes`、`toast_bytes`、`total_bytes` | `workloads.main.rounds[-1].storage.tables.<表名>` |
| ClickHouse | `part_count`、`rows`、`marks`、`compressed_bytes`、`uncompressed_bytes` | 同上 |
| `asset_ref` 的对象存储 | `available_bytes`、`available_object_count` | `workloads.main.rounds[-1].storage.asset_store` |

`asset_ref` 的库内空间与对象存储空间**分列两个数**，不相加。其余三个布局的 `asset_store` 为空。

空间比值一律以**该 workload 写入表中的载荷原始字节**为分母：`main` 为 128,450,560，`equal_total_few_large` 与 `equal_total_many_medium` 各为 83,886,080，`correctness_only` 为 1,024。runner 按 workload 隔离载荷，不属于当前 workload 的行的载荷字段写入 NULL，因此三个 cohort 的载荷不会同时存在于一张表中。不要用三者之和 296,223,744 作分母。

### 3.4 载荷列的直接测量

表差估计受元数据列与索引影响，另用下列查询直接测载荷列，按 profile 分组。

```sql
-- XStore：逻辑字节与实际存储字节
SELECT profile, count(*), sum(octet_length(payload)), sum(pg_column_size(payload))
FROM <载荷所在表> WHERE payload IS NOT NULL GROUP BY profile ORDER BY profile;
```

```sql
-- ClickHouse：载荷列的压缩前后字节
SELECT sum(data_compressed_bytes), sum(data_uncompressed_bytes)
FROM system.parts_columns
WHERE active AND database = {database:String} AND column = 'payload';
```

两条查询在清理之前、`main` workload 的库上执行。

## 4. 本轮重跑范围

工具包已统一，两台使用同一提交，运行账号由 `XSTORE_USER` 提供。

| 实验族 | XStore | ClickHouse | 说明 |
|---|---|---|---|
| 主矩阵四布局四 workload | 重跑 | 重跑 | 两台此前使用各自本地修改的 adapter，数据不可合并；本轮两台使用同一提交，两个引擎都在统一工具包下产出 |
| part 状态控制 | 不适用 | 重跑 | XStore 不使用 part 组织数据。重跑以取得四个布局、四个状态的时延，此前只有 part 数 |
| 混合负载 | 本轮不跑 | 重跑 | 采集路径要求 MergeTree 指标，行存不满足；该缺口待工具包后续变更，本轮不作为不适用结论 |
| Asset 故障六用例 | 重跑 | 重跑 | 结论为分类与状态转换，不产出时延 |

主矩阵一次调用必须列出全部四个 workload，分次调用会覆盖同一 target 的产物，属无效运行。

### 4.1 分页游标形式的探针

XStore 不支持行构造器比较，adapter 把 `(start_time,event_id) > (%s,%s)` 展开为
`(start_time > %s OR (start_time = %s AND event_id > %s))`。展开式能否作为索引范围起点由
优化器决定，本项在正式矩阵之前探一次，结果决定是否需要调整，不要自行改写查询。

对 `same_table` 布局的中间页游标，分别对下列两条语句执行 `EXPLAIN (ANALYZE, BUFFERS)`，
回传两份计划的访问节点类型与实际扫描行数：

```sql
-- 形式一：当前 adapter 使用的展开式
... AND (start_time > %s OR (start_time = %s AND event_id > %s))
ORDER BY start_time,event_id LIMIT 256;

-- 形式二：补一条被 OR 条件蕴含的冗余下界，结果集不变
... AND start_time >= %s
    AND (start_time > %s OR (start_time = %s AND event_id > %s))
ORDER BY start_time,event_id LIMIT 256;
```

两者的实际扫描行数相同时保持形式一不变；形式二显著更少时回传该事实，由蓝区决定是否改包。
该探针只做 EXPLAIN，不进入性能矩阵。

## 5. 回传前不得清理的路径

在回传通过确认之前，不执行指南第 9.1 节的任何删除动作。以下路径在确认前保持原样：

- `$YELLOW_OUTPUT/clickhouse-main/`、`$YELLOW_OUTPUT/xstore-main/`：主矩阵每个 target 的 `run-manifest.json` 与 `result.json`
- `$YELLOW_OUTPUT/access-gate/`：三类查询的计划原文与索引使用计数
- `$YELLOW_OUTPUT/handback/`：回传摘录与证据归档
- `$YELLOW_OUTPUT/ch-part-states-*/`、`$YELLOW_OUTPUT/ch-interference-*/`、`$YELLOW_OUTPUT/*-asset-failures/`
- `$YELLOW_STATE` 下的冻结输入与 Release 资产

数据库的 schema 与 database 在第 3.4 节的查询完成之前不要删除。

## 6. 回传格式

固定顺序，只给数值，不写引擎名、布局名与表头。

「8 行」指 XStore 的 `same_table`、`separate`、`full_core`、`asset_ref`，再 ClickHouse 同四布局。
「4 行」指 `same_table`、`separate`、`full_core`、`asset_ref`。

| 编号 | 行数 | 每行内容 |
|---|---:|---|
| A1 | 8 | `list:first` `list:middle` `preview:first` `preview:middle` 的应用可用 p50，单位 ms 保留一位小数 |
| A2 | 8 | `detail:text_64k` `detail:text_512k` `detail:text_2m` `detail:entropy_512k` 的 p50 |
| A3 | 8 | `trace:p25` `trace:p50` `trace:p95` `batch:main` 的 p50 |
| A4 | 8 | `batch:equal_total_few_large` `batch:equal_total_many_medium` 的 p50 |
| A5 | 8 | 写入合计 单块 p50 |
| A6 | 8 | 空间字段原值，按第 3.3 节该引擎的字段顺序，多个写目标按表名字典序，表之间用分号分隔 |
| A7 | 8 | `response_bytes` 的 database、resolver_payload、total 三项 |
| A8 | 8 | `access_validation` 中 list、detail、trace 三类 `scanned_rows` 的最小值与最大值，共六个数 |
| B1 | 8 | 第 3.4 节两条查询的结果，XStore 四行按 profile 分组给出四组，ClickHouse 四行给压缩后与压缩前两个数 |
| B2 | 16 | part 状态：四个布局各四个状态，每行 `list:first` p50、受控表 active part 数、进行中 merge 数；布局顺序同上，状态顺序为碎片态、合并中、自然稳定态、单 part 态 |
| B3 | 12 | Asset 故障：两个引擎各六个用例，每行 注入点 解析器错误分类 终态 事件可见 孤儿数 恢复动作 |
| C1 | 6 | 第 2 节的六项运行前检查原值 |
| C2 | 2 | 第一行 XStore 的运行提交短号、构建类型与其证据；第二行 ClickHouse 的运行版本、`parts_to_delay_insert`、`parts_to_throw_insert`、`max_server_memory_usage_to_ram_ratio` 四项生效值 |
| C3 | 1 | 本机 IP 末段、内存总量 GB、CPU 核数、数据盘类型 |
| C4 | 4 | XStore 每个布局的 `reltoastrelid`、`reloptions`、访问路径门禁结论 |
| C5 | 2 | 第 4.1 节两条语句的访问节点类型与实际扫描行数 |

回传总行数为 105 行。任一项缺失写 NA 并在同一行末尾写明原因，不估算、不换算、不合并。

## 7. 停止条件

出现以下情况时停止并回传已产生的部分，不继续后续阶段：

1. 第 2 节的运行前检查有任一项不通过且无法处理。
2. XStore 构建类型不是 release。
3. 任一布局的 list、detail 或 trace 未走索引访问路径。
4. 真值校验失败样本数不为 0。
5. 两个引擎中任一个的四布局四 workload 矩阵不完整。
6. 需要修改查询语义、计时口径、冻结输入、四轮 Latin square 或测量次数才能跑通。
