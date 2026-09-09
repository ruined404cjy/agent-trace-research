# Agent Trace JSON 存储原理：openGauss JSONB 与 ClickHouse Native JSON

> 文档日期：2026-09-09
>
> 适用版本：openGauss 6.0.0、ClickHouse 25.12.11.4
>
> 对象：openGauss JSON、openGauss JSONB、ClickHouse String JSON、ClickHouse Native JSON

本文解释四种 JSON 存储结构从输入、内部保存到查询结果的处理流程。重点说明 openGauss JSONB 的二进制文档结构，以及 ClickHouse Native JSON 的 data part、动态路径、shared data、merge 和 Sidecar。本文作为 JSON 存储实验设计和结果解读的共同原理基础，可独立阅读。

## 1. 四种结构的基本区别

| 存储结构 | 写入后的主要表示 | 字段查询 | 完整文档读取 |
|---|---|---|---|
| openGauss JSON | 关系表行内或 TOAST 中的 JSON 文本 datum | 读取并解析文本，再定位路径 | 返回保存的文本表示 |
| openGauss JSONB | 关系表行内或 TOAST 中的二进制文档 datum | 遍历二进制容器；可配合表达式 B-tree 或 GIN | 遍历文档树并序列化为 JSON 文本 |
| ClickHouse String JSON | MergeTree data part 中的压缩 String 列 | 读取字符串并执行 JSON 提取函数 | 直接读取字符串 |
| ClickHouse Native JSON | MergeTree data part 中按路径组织的 JSON 列 | 读取 type hint、dynamic path 或 shared data | 汇集路径并重新组成 JSON |

openGauss JSONB 保存完整的解析后文档树。ClickHouse Native JSON 将一批行的同名路径组织成列式数据。两者都减少字段查询时的文本解析，但内部表示、完整文档恢复方式和物理维护过程不同。

## 2. openGauss JSONB

### 2.1 JSON 和 JSONB 在关系表中的位置

openGauss 使用行式关系存储。JSON 或 JSONB 都是表中一个列值，内部称为 datum；它们与同一行的 `event_id`、`trace_id`、`start_time` 等列共同组成 heap tuple，即关系表页面中的一行记录。

较小的 datum 可以保存在 heap tuple 内。较大的 datum 由 TOAST 压缩或移出主行，heap tuple 保存相应引用。JSONB 决定文档内部表示，TOAST 决定大列值怎样放入关系表页面，两者属于不同层次。

openGauss JSON 保存经过语法校验的文本表示。openGauss JSONB 在写入时完成结构化解析，并保存为二进制文档。

### 2.2 JSONB 的二进制文档结构

JSONB datum 是一个 varlena 可变长值。根节点和每个嵌套对象或数组都使用二进制容器：

```text
[varlena 总长度]
[容器头：对象、数组或标量标志及元素数量]
[JEntry 元数据数组]
[连续的键和值数据区]
```

各部分作用如下：

- 容器头标识当前容器类型和元素数量。
- `JEntry` 记录字符串、数值、布尔值、null 或嵌套容器等类型，并记录值的长度或结束位置。
- 数组的 `JEntry` 与元素一一对应，元素数据按数组顺序排列。
- 对象先保存键的 `JEntry`，再保存值的 `JEntry`；键和值位于连续数据区。
- 嵌套对象和数组继续使用同一容器结构，形成递归文档树。
- 对象键在构造时排序，同层对象键可执行有序查找。

JSONB 是紧凑的二进制容器表示。它不采用红黑树保存单个文档，也不保存一份压缩 JSON 字符串。

### 2.3 载入流程

客户端通过 COPY 或 SQL 发送 JSON 文本后，openGauss 依次执行：

1. 输入函数执行词法和语法解析，识别对象、数组、字符串、数值、布尔值和 null。
2. 字符串完成转义解码，数值转换为内部 Numeric，其他标量转换为相应 JSONB 值类型。
3. 解析回调构造由对象、数组和标量组成的 `JsonbValue` 树。
4. 对象键排序并处理重复键；重复键只保留最后一个值。数组顺序保持不变。
5. 文档树序列化为容器头、`JEntry` 数组和连续数据区组成的 JSONB datum。
6. datum 写入 heap tuple；大值按需由 TOAST 处理。
7. 已创建的表达式 B-tree 或 GIN 在同一写入事务中同步更新。

JSONB 在写入时承担完整解析和二进制构造成本。后续字段查询可直接遍历二进制结构。

### 2.4 查询字段的流程

以以下条件为例：

```sql
attributes #>> '{gen_ai,operation,name}' = 'execute_tool'
```

查询过程为：

1. 优化器根据稳定列条件、JSON 谓词和现有索引选择顺序扫描或索引扫描。
2. 索引扫描先得到候选 heap tuple；顺序扫描读取范围内全部候选行。
3. 执行器取得 JSONB datum。TOAST 外置值先被取回并按需解压。
4. `#>>` 从根容器逐层查找对象键；数组路径按下标访问。
5. 终点标量转换为 SQL 文本或目标类型，再执行过滤、分组或返回。

JSONB 减少候选行上的 JSON 文本词法和语法解析。它本身不会缩小候选行集合；表达式索引、GIN 或稳定列条件负责减少需要检查的数据。

### 2.5 查询完整文档的流程

查询整个 JSONB 列时，执行器读取完整 datum，遍历对象和数组容器，将键和值序列化为 JSON 文本，再通过客户端协议返回。

完整文档来自基列中的文档树。表达式 B-tree 和 GIN 只负责筛选候选行，不参与文档重建，也不会降低已选中行的完整 JSON 序列化成本。

openGauss JSON 可直接返回保存的文本表示，因此完整文档读取可能更低。JSONB 的主要收益集中在字段访问、包含查询和索引能力。

### 2.6 表达式 B-tree 和 GIN

| 索引 | 保存内容 | 适用查询 |
|---|---|---|
| 表达式 B-tree | 固定路径提取后的标量值 | 已知热点路径的等值或范围条件 |
| JSONB GIN | JSONB 文档中的键值项或路径散列 | 与操作符类匹配的包含、存在性查询 |

表达式 B-tree 只覆盖索引表达式对应的路径和转换。GIN 的收益依赖操作符类与查询谓词是否匹配。例如，`jsonb_hash_ops` 适合特定包含查询；普通 `#>> =` 表达式不会自动使用该 GIN。

索引降低匹配查询的候选行数，同时增加写入维护、空间占用和后续更新成本。应根据路径稳定性、查询频率、谓词形式和选择率决定是否创建。

### 2.7 文档恢复能力

JSONB 保留解析后的标量值、对象关系和数组顺序。写入过程会改变以下文本特征：

- 对象键原始顺序；
- 空白和缩进；
- 等价转义写法；
- 重复键的全部实例；
- 数值的原始文本形式。

因此需要区分两种恢复要求：

- **逻辑文档等价**要求解析后的键值、类型和数组顺序一致。JSONB 可直接保存这类 canonical 文档。
- **原文逐字节恢复**要求输入文本完全一致。该要求需要在首次解析前保存原始 UTF-8 bytes。

### 2.8 性能影响因素

| 因素 | 主要影响 |
|---|---|
| 文档大小和嵌套深度 | 写入构造、TOAST、路径遍历和完整序列化成本 |
| 候选行数 | 无匹配索引时，每个候选 JSONB datum 都需执行路径访问 |
| 谓词和选择率 | 决定表达式 B-tree 或 GIN 的有效性 |
| 索引数量 | 降低匹配查询成本，增加写入与空间成本 |
| 返回行数和内容 | 完整 JSON 返回量大时，序列化和传输成为主要成本 |
| 稳定字段是否独立建列 | 普通强类型列可绕过 JSON 路径访问 |

## 3. ClickHouse Native JSON

### 3.1 Native JSON 的目标

ClickHouse Native JSON 在 SQL 层表现为表中的一个 `JSON` 列。物理层将 JSON 对象展开为路径，并尽量把同名路径保存为可独立读取的子列。

查询 `attributes.gen_ai.operation.name` 时，执行器可以读取对应路径的数据，避免读取每行完整 JSON 文本。该结构面向字段级过滤、分组和聚合。写入增加路径展开、类型识别和子列组织，完整文档读取增加路径重建。

### 3.2 表、partition 和 data part

ClickHouse Native JSON 通常存放在 MergeTree 表中。其表内层级为：

```text
数据库
└── MergeTree 表
    ├── partition A                         表内逻辑数据分组
    │   ├── data part A1                    一批不可变、按排序键排列的行
    │   │   ├── 普通列及其物理数据流
    │   │   └── attributes JSON 列
    │   │       ├── type hint 路径
    │   │       ├── dynamic path
    │   │       └── shared data
    │   └── data part A2
    └── partition B
        └── data part B1
```

- **表**定义列、类型、排序键和 partition key。data part 只属于一张表。
- **partition**由 `PARTITION BY` 表达式确定，是表内数据管理边界。不同 partition 的 part 不会互相 merge。未显式分区时，数据位于默认 partition。
- **data part**是一批已落盘、按排序键排列的行，也是 MergeTree 的基本物理维护单元。一次 INSERT 通常为涉及的每个 partition 生成新 part。
- **列和子列**保存在 part 内，并按 part 内行顺序对应。同一行的普通列、JSON 子列和 shared data 通过位置对齐，无需业务 ID 连接。
- **granule 和 mark**是 part 内的分段读取与稀疏索引单位，用于缩小读取范围，不改变 dynamic path 的分配边界。

INSERT 直接生成的 part 层级为 0，称为 zero-level part。后台 merge 以若干旧 part 生成层级更高的新 part。Wide 与 Compact part 改变文件组织，不改变上述逻辑关系。

### 3.3 一个 data part 内的 JSON 表示

JSON 对象在解析后形成 `gen_ai.operation.name`、`failure.mistake_mode` 等扁平路径。一个 part 中的 JSON 路径有三类主要去向：

| 路径类别 | 产生方式 | 保存形式 | 主要特点 |
|---|---|---|---|
| type hint 路径 | DDL 显式声明路径和类型 | 固定类型子列 | 类型稳定；不占未声明路径的 dynamic path 预算 |
| dynamic path | 未声明路径进入动态路径预算 | `Dynamic` 类型子列 | 可独立读取；同一路径可容纳多种实际类型 |
| shared data 路径 | 未声明路径超过动态路径预算 | JSON 列内部的共享结构 | 限制子列数量；单路径读取通常需要更多定位工作 |

shared data 属于当前 part 中当前 JSON 列。它与 data part 的关系是包含关系，不是独立表或另一个 part。

shared data 在内存中可理解为 `Map(String, String)`：键是扁平路径，值是二进制编码后的 JSON 值。落盘可采用：

- `map`：写入和完整 JSON 读取较直接，单路径读取需要扫描较大的共享 Map；
- `map_with_buckets`：将路径分桶，单路径读取只访问相应桶；
- `advanced`：保存更多路径定位信息，减少单路径读取量，同时增加写入和空间成本。

具体序列化方式由 MergeTree 设置和 part 类型决定。比较 dynamic path 与 shared data 时，需要固定 shared data 序列化方式；三种序列化方式的差异属于另一组控制变量。

### 3.4 DDL 控制项

Native JSON 支持以下控制项：

```sql
attributes JSON(
    max_dynamic_paths = 32,
    max_dynamic_types = 32,
    gen_ai.operation.name String,
    SKIP unused.large_debug_field
)
```

| 控制项 | 控制对象 | 含义 |
|---|---|---|
| `max_dynamic_paths` | 一个独立保存的数据块；MergeTree 中通常对应单个 part | 最多有多少条未声明路径作为 dynamic path 保存 |
| `max_dynamic_types` | 单条 `Dynamic` 路径 | 同一路径最多有多少种实际类型独立保存 |
| type hint | 指定路径 | 声明固定类型并形成固定类型子列 |
| `SKIP` / `SKIP REGEXP` | 指定路径 | 解析时丢弃匹配路径 |

`max_dynamic_paths` 控制独立路径数量。`max_dynamic_types` 控制一条动态路径的实际类型数量；超过后，新类型进入该路径的 shared variant。shared variant 解决单一路径类型过多，shared data 解决 JSON 列路径过多。

`max_dynamic_paths=32` 表示容量上限，不表示路径出现 32 次后建立子列。type hint 由建表者显式声明，系统不会按查询频率自动生成 type hint。

### 3.5 INSERT 流程

一批 JSONEachRow 数据写入 MergeTree 表时，依次执行：

1. 输入格式解析一行中的普通列和 JSON 值。
2. JSON 对象展开为扁平路径；数组、嵌套对象和标量转换为相应值。
3. `SKIP` 路径被丢弃；type hint 路径转换为声明类型。
4. 其余路径识别为整数、浮点数、字符串、日期、数组等实际类型，并使用 `Dynamic` 容纳类型变化。
5. 当前解析数据块中的未声明路径进入 dynamic path；达到预算后出现的新路径进入 shared data。该阶段不使用全表查询热度或全表淘汰策略。
6. 同一 partition 的本批行按表的排序键排列。普通列、JSON 子列、shared data、压缩数据、mark 和校验信息共同写成 zero-level part。
7. part 完整生成后成为 active part，查询即可读取。

JSON 解析、类型识别和新 part 内的路径分配属于 INSERT 的同步工作。多个 part 的 merge 在 INSERT 完成后由后台执行。

### 3.6 后台 merge 和路径重组

频繁 INSERT 会产生多个小 part。每个 part 独立应用动态路径预算，因此同一路径可以在 part A 中属于 dynamic path，在 part B 中属于 shared data。SQL 调用方始终使用同一路径名，执行器按各 part 的实际表示读取。

后台 merge 的过程为：

1. MergeTree 在同一 partition 中选择若干适合合并的 active data part。
2. 读取源 data part，按排序键合并行流，并写入新的目标 data part。
3. 目标 part 重新应用 JSON 路径预算。全部源路径超过预算时，ClickHouse 通常保留非 null 值较多的路径，将较少出现的路径放入 shared data。
4. 目标 data part 完整生成后原子发布；旧 data part 变为 inactive，等待安全删除。

路径频率主要在 merge 重组时发挥作用。初次解析达到预算后，新路径进入 shared data；merge 则根据目标 part 中的路径统计重新选择 dynamic path。全表不会维护一套按最近使用顺序换入换出的路径缓存。

后台 merge 通常不会持有阻塞普通查询的表级锁。查询使用开始时可见的 active data part。merge 会消耗 CPU、内存和磁盘带宽，可能提高并发查询延迟。

### 3.7 后台 merge、OPTIMIZE、FINAL 和 mutation

| 操作 | 发生阶段 | 是否重写 part | 与 dynamic path 的关系 |
|---|---|---:|---|
| 后台 merge | 写入后的异步维护 | 是 | 目标 part 重新选择 dynamic/shared 路径 |
| `OPTIMIZE TABLE ... FINAL` | 用户发起的物理维护 | 是 | 强制尝试合并适用 data part，间接触发路径重组 |
| `SELECT ... FINAL` | 查询执行阶段 | 否 | 应用 ReplacingMergeTree 等表引擎的最终行语义 |
| mutation | `ALTER` 等结构变更后的后台重写 | 是 | 修改 `max_dynamic_paths` 等定义时可能重写已有 data part |

`OPTIMIZE ... FINAL` 强制执行物理 merge，并非专门调整动态路径。新目标 part 按正常规则重新组织路径。后续 INSERT 仍会生成新 part，后台 merge 也会继续。

`SELECT ... FINAL` 不修改磁盘数据。它在读取时应用 ReplacingMergeTree 等表引擎的行合并规则，与 dynamic path 重组无关。

修改 `max_dynamic_paths` 等 JSON 定义通常通过 mutation 重写已有 data part。该操作会消耗显著资源，应作为结构变更管理。

### 3.8 查询一条路径

以 `attributes.gen_ai.operation.name` 为例：

1. 排序键、主键稀疏索引和其他过滤条件缩小需要读取的 data part 与 granule。
2. 执行器查看目标路径在每个入选 part 中的物理表示。
3. type hint 路径读取固定类型子列；dynamic path 读取 `Dynamic` 子列；shared data 路径从 Map、bucket 或 advanced 结构定位并解码。
4. 各 part 的值转换为查询要求的共同类型，再执行过滤、分组或聚合。

同一路径在不同 data part 中可以具有不同物理去向。读取性能取决于入选 data part 和 granule 数量、路径归属、shared data 序列化、实际类型数和返回量。

### 3.9 查询完整 JSON

查询整个 JSON 列时，执行器读取该行涉及的 type hint、dynamic path 和 shared data，按照扁平路径重新组成对象，再序列化为输出格式。

完整读取需要汇集多个物理子流，无法利用“只读一个路径子列”的主要优势。路径越多、对象越宽、返回行越多，重建和序列化成本越明显。

因此，Native JSON 适合反复分析少量路径；String JSON 适合把 JSON 作为整体保存和返回。

### 3.10 信息边界与 Sidecar

Native JSON 按路径和值保存分析表示。以下输入信息不能仅依靠该列恢复：

- JSON null 与路径缺失在 Native JSON 语义中等价；
- 空对象没有叶路径，按叶路径重建时可能消失；
- 默认的点分隔扁平路径会使字面点键与嵌套路径形成相同表示；
- 空白、原始键顺序、重复键实例和数值原始写法不属于保留目标；
- type hint 路径缺失时产生声明类型的默认值，需要额外记录区分缺失和值等于默认值。

Sidecar 是与主 JSON 列同行保存的补充数据。Agent Trace 同时需要字段分析、逻辑文档恢复和原文逐字节恢复时，可采用以下结构：

1. **稀疏 Sidecar**保存包含 JSON null、空对象或空数组等特殊值的 canonical 属性值。完整读取先从 Native JSON 重建，再用 Sidecar 覆盖这些位置。
2. **presence marker**逐行记录指定 type hint 路径是否存在，用于区分路径缺失和值等于类型默认值。
3. **独立原文表**保存首次解析前的 UTF-8 bytes，用于签名校验、精确重放和字节级审计。

canonical 值是 JSON 解析后按确定规则重新序列化的文本，保留逻辑键值和数组顺序，不保留原始排版。Sidecar 由实验载入程序生成，属于显式数据结构，不是 ClickHouse 自动维护的隐藏结构。

### 3.11 观测 data part 和路径归属

查看 active data part：

```sql
SELECT
    partition,
    name AS part_name,
    level,
    rows,
    part_type,
    bytes_on_disk
FROM system.parts
WHERE active
  AND database = 'db'
  AND table = 'analytics'
ORDER BY partition, part_name;
```

查看每个 part 的 dynamic path 和 shared data 路径：

```sql
SELECT
    _part,
    groupArrayArrayDistinct(JSONDynamicPaths(attributes)) AS dynamic_paths,
    groupArrayArrayDistinct(JSONSharedDataPaths(attributes)) AS shared_paths
FROM db.analytics
GROUP BY _part
ORDER BY _part;
```

`JSONDynamicPathsWithTypes` 和 `JSONSharedDataPathsWithTypes` 同时返回类型。`JSONAllPaths` 和 `JSONAllPathsWithTypes` 返回一行中出现的全部路径。

查看 part 内 JSON 子列和空间：

```sql
SELECT
    name AS part_name,
    level,
    subcolumns.names,
    subcolumns.types,
    subcolumns.bytes_on_disk
FROM system.parts_columns
WHERE active
  AND database = 'db'
  AND table = 'analytics'
  AND column = 'attributes'
ORDER BY part_name;
```

`system.columns` 主要显示表的声明结构。`system.merges` 显示正在执行的 merge；`system.part_log` 可用于检查历史 part 和 merge 事件。

### 3.12 性能影响因素

| 因素 | 对写入和维护的影响 | 对查询的影响 |
|---|---|---|
| 路径总数与每行路径数 | 增加解析、子列和元数据工作 | 增加完整对象重建成本 |
| `max_dynamic_paths` | 预算越大，可产生更多独立子列 | 热路径位于 dynamic 时读取更直接；预算过大增加结构开销 |
| 路径出现频率 | merge 时影响 dynamic/shared 选择 | 高频路径通常更可能保留为 dynamic path |
| 每条路径的实际类型数 | 增加 Dynamic 类型子流或 shared variant | 增加类型判断和转换成本 |
| INSERT 批次大小 | 小批写入产生更多 data part 和 merge 压力 | data part 较多时需要合并更多读取流 |
| shared data 序列化 | `map` 写入较轻；`advanced` 写入和空间更高 | bucket 和 advanced 更利于单路径读取 |
| 返回字段和行数 | 影响输出序列化 | 少量路径有利于 Native；完整对象和大结果削弱优势 |
| 后台 merge 并发 | 消耗 CPU、内存和磁盘 I/O | 可能提高查询尾延迟 |

type hint 的选择需要同时考虑查询频率、类型稳定性和缺失语义。高频但类型经常变化的路径可以保留为 Dynamic；要求固定类型和严格校验的路径可使用普通列或 type hint。

## 4. 两种结构化表示的流程差异

| 阶段 | openGauss JSONB | ClickHouse Native JSON |
|---|---|---|
| 输入解析 | 每个 JSON 文本解析为完整二进制文档树 | 每批 JSON 展开为路径并识别类型 |
| 基本物理单位 | 一行中的 JSONB datum | 一张 MergeTree 表的单个 data part 内的 JSON 子流 |
| 字段读取 | 在每个候选 datum 内逐层定位路径 | 按 part 读取 type hint、dynamic path 或 shared data |
| 候选数据缩减 | 稳定列条件、表达式 B-tree 或 GIN | partition、排序键、granule、数据跳过条件 |
| 后台重组 | 常规表与索引维护；JSONB 文档本身不自动改组 | merge 生成新 part 并重新分配 dynamic/shared 路径 |
| 完整文档 | 遍历完整文档树并序列化 | 汇集叶路径和 shared data 后重建 |
| 逻辑恢复 | 可直接保存 canonical 文档 | 需要 Sidecar 补足 null、空容器等差异 |
| 原文恢复 | 依赖首次解析前保存的原文 | 依赖首次解析前保存的原文 |

两种结构的共同优势是减少字段查询时的 JSON 文本解析。openGauss JSONB 以单行完整文档为中心；ClickHouse Native JSON 以一批行的同名路径为中心。场景化性能差异应结合候选行数、访问路径、返回内容和后台维护共同解释。

## 参考资料

- [ClickHouse JSON 数据类型](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse system.parts_columns](https://clickhouse.com/docs/reference/system-tables/parts_columns)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/reference/statements/optimize)
- [openGauss 6.0 JSON/JSONB 类型](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-types.html)
- [openGauss 6.0 JSONB 数据结构定义](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/include/utils/jsonb.h)
- [openGauss 6.0 JSONB 容器实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_util.cpp)
- [openGauss 6.0 JSON 查询函数实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonfuncs.cpp)
