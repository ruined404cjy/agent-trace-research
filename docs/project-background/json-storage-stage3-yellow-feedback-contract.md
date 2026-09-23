# 阶段三黄区结果反馈契约

本文规定黄区每一轮实验的重跑范围、统计口径、保留物与回传格式。执行[黄区执行指南](json-storage-stage3-xstore-clickhouse-yellow-guide.md)得到运行产物之后，按本文回传。

两台 ARM 主机执行相同的实验，各自独立回传，回传内容不互相引用。

## 1. 基线对齐与重建

本节消除历史歧义。两台主机此前使用各自本地修改的 adapter，运行产物无法互相比较，也无法
从当前工具包复现。本轮之前的全部结果只作诊断材料，不与本轮数据混排。

### 1.1 保留本地工作

不要使用 `git stash`。本地改动是解释旧数据的唯一材料，按下列顺序固化为可找回的提交：

```bash
cd "$YELLOW_REPO"
git switch -c "local-before-unification-$(date -u +%Y%m%d)"
git add -A && git commit -m "wip: local state before toolkit unification"
git log --oneline -1                     # 记录该提交号，随回传给出
```

### 1.2 对齐到统一工具包

```bash
git switch stage3/xstore-yellow-handoff
git fetch origin && git reset --hard origin/stage3/xstore-yellow-handoff
git status --porcelain                   # 必须为空
git rev-parse --short HEAD               # 两台必须相同
```

随后核对六个运行文件的摘要，两台的同名文件必须逐项相同：

```bash
sha256sum experiments/json-storage-stage3/runner/{xstore.py,gaussdb_libpq.py,opengauss.py,clickhouse.py,common.py,run_layout_matrix.py}
```

工具包测试必须全绿后才进入重建：

```bash
"$PYTHON" -m unittest discover -s experiments/json-storage-stage3/tests
```

### 1.3 重建 XStore

按 release 重新构建并重新拉起服务，记录下列事实。构建类型不是 release 时停止。

| 项 | 内容 |
|---|---|
| 源码提交 | 构建所用的 XStore 源码提交号 |
| 构建选项 | 完整的构建命令或选项，含证明 release 的那一项 |
| 产物路径 | 服务端可执行文件路径，供 `XSTORE_PRODUCT_PATH` 使用 |
| 二进制摘要 | 该文件的 SHA-256 |
| 服务版本 | 拉起后 `SELECT version()` 的返回值 |

重建后的实例与本轮之前的实例不视为同一个被测对象，两者的数值不放在同一张表里比较。

### 1.4 引擎执行顺序

两台使用同一顺序：XStore 主矩阵 → ClickHouse 主矩阵 → ClickHouse part 状态 →
两个引擎的混合负载 → 两个引擎的 Asset 故障。两个引擎串行，运行一个引擎的目标时停止另一个引擎的服务，使内存与
页缓存不被另一侧占用。每次切换引擎之前重做一遍第 3 节的六项检查，不沿用上一次的结果。

## 2. 运行环境变量

工具包不再携带运行账号与动态库路径，两项由环境提供，运行前导出：

```bash
export XSTORE_USER=<XStore 运行账号>
export GAUSSDB_LIB_DIR=<含 libpq.so.5 的目录>
export GAUSSDB_SERVER_LIB_DIR=<GaussDB 服务端库目录>   # 可选，缺失时不加入搜索路径
```

未导出 `XSTORE_USER` 时 adapter 拒绝构造；未导出 `GAUSSDB_LIB_DIR` 时首次连接报出缺失原因。
两项都不影响 ClickHouse 侧。

## 3. 运行前检查

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

## 4. 统一口径

以下定义在全部场景、全部回传字段上取同一含义。回传时不做二次换算。

### 4.1 时延

单个查询样本的应用可用时间为该样本的查询完成、客户端恢复与正确性校验三段之和，由程序按样本记录，不由分项中位数相加得到。

多轮聚合按固定顺序：**先在轮内对该查询目标的全部样本取中位数，再对四轮的轮内中位数取中位数**。回传的 p50 一律指这个「四轮轮级中位数」。不要先把四轮样本合并再取中位数，两者结果不同。

`correctness_only` 只有一轮，其 p50 即该轮的轮内中位数。

### 4.2 写入

写入有两个不同的量，回传时都要给，不要互相替代。

| 名称 | 定义 | 来源字段 |
|---|---|---|
| 写入合计 | 一轮中 190 个 block 的 `ingest.wall_ms` 之和，再取四轮的中位数 | `workloads.main.rounds[*].write.blocks[*].ingest.wall_ms` |
| 单块 p50 | 一轮的 `block_wall_ms.median`，再取四轮的中位数 | `workloads.main.rounds[*].write.block_wall_ms.median` |

「合计」只指前者。历史回传中这两个量曾被混用，量级相差约 190 倍。

### 4.3 空间

空间取该 target **最后一轮**的证据，不跨轮平均。

| 引擎 | 字段 | 来源 |
|---|---|---|
| XStore | `heap_bytes`、`index_bytes`、`toast_bytes`、`total_bytes` | `workloads.main.rounds[-1].storage.tables.<表名>` |
| ClickHouse | `part_count`、`rows`、`marks`、`compressed_bytes`、`uncompressed_bytes` | 同上 |
| `asset_ref` 的对象存储 | `available_bytes`、`available_object_count` | `workloads.main.rounds[-1].storage.asset_store` |

`asset_ref` 的库内空间与对象存储空间**分列两个数**，不相加。其余三个布局的 `asset_store` 为空。

空间比值一律以**该 workload 写入表中的载荷原始字节**为分母：`main` 为 128,450,560，`equal_total_few_large` 与 `equal_total_many_medium` 各为 83,886,080，`correctness_only` 为 1,024。runner 按 workload 隔离载荷，不属于当前 workload 的行的载荷字段写入 NULL，因此三个 cohort 的载荷不会同时存在于一张表中。不要用三者之和 296,223,744 作分母。

### 4.4 载荷列的直接测量

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

## 5. 本轮重跑范围

工具包已统一，两台使用同一提交，运行账号由 `XSTORE_USER` 提供。

| 实验族 | XStore | ClickHouse | 说明 |
|---|---|---|---|
| 主矩阵四布局四 workload | 重跑 | 重跑 | 两台此前使用各自本地修改的 adapter，数据不可合并；本轮两台使用同一提交，两个引擎都在统一工具包下产出 |
| part 状态控制 | 不适用 | 重跑 | XStore 不使用 part 组织数据。重跑以取得四个布局、四个状态的时延，此前只有 part 数 |
| 混合负载 | 重跑 | 重跑 | 阶段快照改为按引擎实际报出的物理模型采集，行存记堆、索引与行外存储字节，part 积压记 0；前台时延与丢弃计数两侧同口径，物理旁证两侧各记各的，不横向对照 |
| Asset 故障六用例 | 重跑 | 重跑 | 结论为分类与状态转换，不产出时延 |

主矩阵一次调用必须列出全部四个 workload，分次调用会覆盖同一 target 的产物，属无效运行。

### 5.1 分页游标形式的探针

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

## 6. 回传前不得清理的路径

在回传通过确认之前，不执行指南第 9.1 节的任何删除动作。以下路径在确认前保持原样：

- `$YELLOW_OUTPUT/clickhouse-main/`、`$YELLOW_OUTPUT/xstore-main/`：主矩阵每个 target 的 `run-manifest.json` 与 `result.json`
- `$YELLOW_OUTPUT/access-gate/`：三类查询的计划原文与索引使用计数
- `$YELLOW_OUTPUT/handback/`：回传摘录与证据归档
- `$YELLOW_OUTPUT/ch-part-states-*/`、`$YELLOW_OUTPUT/ch-interference-*/`、`$YELLOW_OUTPUT/*-asset-failures/`
- `$YELLOW_STATE` 下的冻结输入与 Release 资产

数据库的 schema 与 database 在第 4.4 节的查询完成之前不要删除。

## 7. 回传格式

回传不限制总行数，也不要为了凑行数拆分或合并任何一项。约束只有两条：**一个数据单元占一行**，
**行内字段按本节给定的顺序排列**。顺序固定之后不需要写表头、引擎名和布局名，回传方按顺序
读取即可。

### 7.1 两个固定顺序

凡本节写「按引擎与布局」的项，顺序为 XStore 的 `same_table`、`separate`、`full_core`、
`asset_ref`，再 ClickHouse 的同四布局，共八行。

凡本节写「按布局」的项，顺序为 `same_table`、`separate`、`full_core`、`asset_ref`，共四行。

每项之前单起一行写项目编号，例如 `A1`，其后各行只写数值，字段之间用空格分隔。数值缺失
写 `NA`，并在该行末尾用一个词写明原因，例如 `NA 未运行`。不估算、不换算、不合并单元。

### 7.2 项目与字段顺序

| 编号 | 分行方式 | 行内字段顺序 |
|---|---|---|
| A1 | 按引擎与布局 | `list:first` `list:middle` `preview:first` `preview:middle` 的应用可用 p50，单位 ms 保留一位小数 |
| A2 | 按引擎与布局 | `detail:text_64k` `detail:text_512k` `detail:text_2m` `detail:entropy_512k` 的 p50 |
| A3 | 按引擎与布局 | `trace:p25` `trace:p50` `trace:p95` `batch:main` 的 p50 |
| A4 | 按引擎与布局 | `batch:equal_total_few_large` `batch:equal_total_many_medium` 的 p50 |
| A5 | 按引擎与布局 | 写入合计 单块 p50 |
| A6 | 按引擎与布局 | 该引擎在第 4.3 节的空间字段，按该表给定的字段顺序；多个写目标按表名字典序，表之间用分号分隔 |
| A7 | 按引擎与布局 | `response_bytes` 的 database resolver_payload total |
| A8 | 按引擎与布局 | `access_validation` 的 scanned_rows：list 最小 list 最大 detail 最小 detail 最大 trace 最小 trace 最大 |
| B1 | XStore 按布局，每布局再按 profile 分行；ClickHouse 按布局 | XStore 行为 profile 行数 逻辑字节 存储字节；ClickHouse 行为 压缩后字节 压缩前字节 |
| B2 | 按布局，每布局再按碎片态、合并中、自然稳定态、单 part 态分行 | `list:first` p50 受控表 active part 数 进行中 merge 数 |
| B3 | 按引擎与布局，每项再按 quiet、detail_2m、trace_long、batch_loop、continuous_ingest 五个阶段分行 | 前台 list p50 前台 preview p50 list 丢弃数 preview 丢弃数 存储模型 |
| B4 | 两个引擎各按 missing、corrupt、metadata_mismatch、upload_then_db_failure、publish_failure、delete_failure 分行 | 注入点 解析器错误分类 终态 事件可见 孤儿数 恢复动作 |
| C1 | 每项一行 | 第 3 节六项检查的原值，按该表的行序 |
| C2 | 两行 | 第一行 XStore 运行提交短号 构建类型 构建类型证据；第二行 ClickHouse 运行版本 `parts_to_delay_insert` `parts_to_throw_insert` `max_server_memory_usage_to_ram_ratio` |
| C3 | 一行 | 本机 IP 末段 内存总量 GB CPU 核数 数据盘类型 |
| C4 | 按布局 | XStore 的 `reltoastrelid` `reloptions` 访问路径门禁结论 |
| C5 | 两行 | 第 5.1 节两条语句各一行：访问节点类型 实际扫描行数 |
| C6 | 三行 | 第一行 本地保留提交号 对齐后的 HEAD 短号；第二行 第 1.2 节六个运行文件的 SHA-256，按命令中的文件顺序；第三行 第 1.3 节五项重建事实，按该表的行序 |

## 8. 停止条件

出现以下情况时停止并回传已产生的部分，不继续后续阶段：

1. 第 3 节的运行前检查有任一项不通过且无法处理。
2. XStore 构建类型不是 release。
3. 任一布局的 list、detail 或 trace 未走索引访问路径。
4. 真值校验失败样本数不为 0。
5. 两个引擎中任一个的四布局四 workload 矩阵不完整。
6. 需要修改查询语义、计时口径、冻结输入、四轮 Latin square 或测量次数才能跑通。
