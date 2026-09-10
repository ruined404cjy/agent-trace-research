# Agent Trace JSON 存储原理：openGauss JSONB 与 ClickHouse Native JSON

> 文档日期：2026-09-09
>
> 适用版本：openGauss 6.0.0、ClickHouse 25.12.11.4
>
> 对象：openGauss JSON、openGauss JSONB、ClickHouse String JSON、ClickHouse Native JSON

本文解释四种 JSON 存储结构从输入、内部保存到查询结果的处理流程。重点说明
openGauss JSONB 的二进制文档结构，以及 ClickHouse Native JSON 的 data part、
动态路径、shared data、merge 和 Sidecar。

本文是 JSON 存储实验设计和结果解读的共同原理基础，可独立阅读。

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

JSONB datum 属于 varlena 可变长值。openGauss 6.0 的 JSONB 类型默认使用
`EXTENDED` 存储策略，对应列目录中的 `attstorage=x`。该策略允许系统压缩 datum，
也允许将它移出主 heap tuple。

当 datum 外置时，主 tuple 保留指针，内容写入原表关联的 TOAST
relation。两者的物理关系如下：

```text
主 relation
└── heap tuple (event_id = 42)
    └── attributes：TOAST 外置指针
        ├── va_rawsize：未压缩 datum 大小
        ├── va_extsize：外置内容大小
        ├── va_toastrelid = T
        └── va_valueid = V

TOAST relation T（原表的内部附属表）
└── 外置值 V
    ├── chunk 0：(chunk_id=V, chunk_seq=0, chunk_data)
    ├── chunk 1：(chunk_id=V, chunk_seq=1, chunk_data)
    ├── ...
    └── 唯一索引键：(chunk_id, chunk_seq)
```

`va_toastrelid` 选择附属表 T，`va_valueid` 选择其中的外置值 V。这两个
标识将指针关联到一组 TOAST 行，关联过程不使用业务主键 `event_id`。

TOAST 根据整条 heap tuple 的预计大小决定是否介入。触发阈值
`TOAST_TUPLE_THRESHOLD` 由页大小、页头、行指针和“每页容纳四条 tuple”的目标
共同计算；默认 8 KiB heap page 下为 2,032 bytes，`TOAST_TUPLE_TARGET` 与它相同。
这个阈值针对整行，而非单个 JSONB 列。

对 `EXTENDED` 值，TOAST 先用 PGLZ 尝试压缩已序列化的 JSONB datum，
只保留能实际节省空间的结果。压缩后整行仍超过目标时，系统将较大的
datum 切成约 2 KiB 的有序 chunk 并外置。读取时，de-TOAST 按
`chunk_seq` 拼接内容，并在需要时解压。

`EXTERNAL` 跳过压缩并允许外置，`MAIN` 优先保留行内，`PLAIN` 只使用行内
表示。

openGauss JSON 保存经过语法校验的文本表示。openGauss JSONB 在写入时完成结构化解析，并保存为二进制文档。

### 2.2 JSONB 的二进制文档结构

以下 JSON 贯穿本章示例。它包含嵌套对象、数组、空对象、转义字符串、科学计数法和重复键：

```json
{
  "gen_ai": {"operation": {"attempt": 2e0, "name": "execute\u005ftool"}},
  "tags": ["db", "json"],
  "empty": {},
  "score": 1.2300e+2,
  "dup": "old",
  "dup": "new"
}
```

`JsonbValue` 是 JSONB 构造和操作期间使用的内存表示，主要节点包括：

- 标量：`null`、string、Numeric、bool；
- 对象：`JsonbPair` 数组，每个 pair 包含字符串键和 `JsonbValue` 值；
- 数组：保持输入顺序的 `JsonbValue` 元素数组。

解析完成并关闭各层容器后，上例形成以下逻辑树：

```text
JsonbValue：object，5 pairs
├── "dup" → string("new")
├── "tags" → array，2 elements
│   ├── [0] string("db")
│   └── [1] string("json")
├── "empty" → object，0 pairs
├── "score" → Numeric(123.00)
└── "gen_ai" → object，1 pair
    └── "operation" → object，2 pairs
        ├── "name" → string("execute_tool")
        └── "attempt" → Numeric(2)
```

对象键按“键的字节长度优先、等长时按字节比较”排序，因此根对象依次为
`dup`、`tags`、`empty`、`score`、`gen_ai`。对象关闭时会合并重复键；示例最终
保留 `dup: "new"`。

`JsonbValue` 树只存在于构造和操作期间。表中保存的是下一步生成的连续 JSONB
datum。

JSONB datum 以四字节 varlena 总长度开头。根容器完整包含自己的
header、`JEntry` 数组和 data area。嵌套对象或数组作为 data area 中的一项，
再递归包含同样的三部分。完整的包含关系如下：

```text
JSONB datum
├── varlena header：datum 总长度
└── root container：object，5 pairs
    ├── container header：JB_FOBJECT | 5
    ├── JEntry[0..9]
    │   ├── [0,1]  "dup"    → string
    │   ├── [2,3]  "tags"   → nested container
    │   ├── [4,5]  "empty"  → nested container
    │   ├── [6,7]  "score"  → Numeric
    │   └── [8,9]  "gen_ai" → nested container
    └── data area
        ├── "dup" + "new"
        ├── "tags"
        │   └── array container
        │       ├── header：JB_FARRAY | 2
        │       ├── JEntry[0..1]：string, string
        │       └── data："db", "json"
        ├── "empty"
        │   └── empty object container
        │       ├── header：JB_FOBJECT | 0
        │       ├── JEntry：0 项
        │       └── empty data area
        ├── "score" + aligned Numeric(123.00)
        └── "gen_ai"
            └── object container：1 pair
                ├── header：JB_FOBJECT | 1
                ├── JEntry[0..1]
                └── data area
                    ├── "operation"
                    └── object container：2 pairs
                        ├── header：JB_FOBJECT | 2
                        ├── JEntry[0..3]
                        └── data area
                            ├── "name" + "execute_tool"
                            └── "attempt" + aligned Numeric(2)
```

容器的序列化规则如下：

- 容器头使用标志位区分 object、array 和顶层标量包装，并在其余位记录 pair 或元素数量。
- openGauss 6.0 的对象 `JEntry` 按排序后的
  `key0, value0, key1, value1, ...` 成对排列；数组的 `JEntry` 与元素一一对应，
  并保持数组顺序。
- `JEntry` 的类型位区分 string、Numeric、bool、null 和嵌套容器。位置位记录当前项
  在连续数据区中的累计结束位置，当前项长度由本项与前一项的结束位置之差得到。
- string 的 UTF-8 字节、Numeric 的内部 varlena 和完整的嵌套容器写入连续数据区。
  Numeric 与嵌套容器按整数边界对齐，对齐字节也计入位置。
- bool 和 null 的值包含在 `JEntry` 类型位中，不占用独立 payload 字节。
- 顶层 JSON 标量使用带 `JB_FSCALAR` 标志的单元素伪数组保存，使 datum 根部仍采用
  统一的容器格式。

按上述规则，示例根对象序列化后可表示为以下连续字节布局。这是结构
示意，不是实际的十六进制字节转储；`e0`—`e9` 代表各项在 data area
中的累计结束位置。

```text
[varlena：total_length]
[root header：JB_FOBJECT | 5]
[JEntry：
  key "dup"@e0      value string@e1
  key "tags"@e2     value nested@e3
  key "empty"@e4    value nested@e5
  key "score"@e6    value numeric@e7
  key "gen_ai"@e8   value nested@e9
]
[data area：
  "dup" "new"
  "tags" <array header + JEntry × 2 + "db" "json">
  "empty" <object header + JEntry × 0>
  "score" <padding + Numeric(123.00)>
  "gen_ai" <object header + JEntry × 2 +
            "operation" <object header + JEntry × 4 +
                         "name" "execute_tool" "attempt" <padding + Numeric(2)>>>
]
```

尖括号中的数组和对象是嵌入 data area 的完整子容器，不是指向另一处的引用。

JSONB 是紧凑的递归二进制容器。对象的排序键用于容器内查找，数组按下标顺序访问。
表中没有另一份供查询时重新解析的 JSON 字符串。

TOAST 位于 JSONB 表示的外层。它可以压缩整个 datum，但不改变容器内部布局。

### 2.3 载入流程

用一张简化表载入上述示例：

```sql
CREATE TABLE trace_events (
    event_id bigint PRIMARY KEY,
    attributes jsonb
);

INSERT INTO trace_events(event_id, attributes)
VALUES (
    42,
    '{
      "gen_ai":{"operation":{"attempt":2e0,"name":"execute\u005ftool"}},
      "tags":["db","json"], "empty":{}, "score":1.2300e+2,
      "dup":"old", "dup":"new"
    }'::jsonb
);
```

客户端通过 COPY 或 SQL 发送 JSON 文本后，openGauss 依次执行以下过程：

1. 输入函数执行词法和语法解析，识别对象、数组、字符串、数值、布尔值和 null。
2. 词法层忽略结构外空白；字符串转义在词法解析时解码，因此 `execute\u005ftool` 成为 `execute_tool`。数值 token 交给 Numeric 输入函数，因此 `2e0` 和 `1.2300e+2` 不再保留指数写法。
3. 语义回调按 `BEGIN_OBJECT`、`KEY`、`VALUE`、`BEGIN_ARRAY`、`ELEM` 等事件维护 `JsonbParseState` 栈，逐层构造对象 pair、数组元素和标量 `JsonbValue`。
4. 每个对象结束时，对象 pair 按键长度和字节排序；相同键按输入次序辅助排序并去重，保留最后出现的值。上例的 `dup:"old"` 在此处消失。数组元素不排序。
5. `JsonbValueToJsonb` 深度优先遍历这棵树，为每个容器预留容器头和 `JEntry` 数组，再依次写入标量 payload 或完整嵌套容器，并回填类型和累计结束位置。
6. 完成的 datum 与 `event_id` 一起组成 heap tuple。tuple 超过 TOAST 阈值时，系统按 2.1 节所述顺序压缩或外置可变长列。
7. 已创建的表达式 B-tree 或 GIN 在同一写入事务中同步更新。

文本特征的改变发生在第 2 步和第 4 步，早于 TOAST。空白被忽略，转义写法被
解码，数值进入 Numeric，对象键被重排，重复键被合并。

TOAST 只处理已经序列化的二进制 datum。JSONB 在写入时承担完整解析和二进制
构造成本，后续字段查询可直接遍历该结构。

### 2.4 查询字段的流程

查询 `event_id=42` 的操作名：

```sql
SELECT
    event_id,
    attributes #>> '{gen_ai,operation,name}' AS operation_name
FROM trace_events
WHERE event_id = 42
  AND attributes #>> '{gen_ai,operation,name}' = 'execute_tool';

 event_id | operation_name
----------+----------------
       42 | execute_tool
```

`#>>` 的左操作数是 JSON/JSONB 容器，右操作数是 `text[]` 路径。
`'{gen_ai,operation,name}'` 包含三个路径分量，即三个依次访问的对象键：
`gen_ai` → `operation` → `name`。终点值 `execute_tool` 是 `WHERE` 子句中的
比较值，不属于路径。

当当前容器是数组时，路径分量表示从 0 开始的数组下标。例如，
`attributes #>> '{tags,0}'` 先找到 `tags` 数组，再读取第 0 个元素，结果为
SQL text `db`。

`#>>` 将终点转换为 SQL `text`，字符串结果不带 JSON 双引号。`#>` 使用相同路径，
但保留 JSON/JSONB 返回类型。路径不存在时，两者均返回 SQL NULL。

查询过程为：

1. 优化器根据独立列条件、JSON 谓词和现有索引选择顺序扫描或索引扫描。
2. 索引扫描先得到候选 heap tuple；顺序扫描读取范围内全部候选行。
3. 执行器取得 `attributes` datum。外置值先根据 TOAST 指针读取并拼接 chunk；压缩值随后解压。
4. `#>>` 在根对象的已排序 pair 中二分查找 `gen_ai`，进入其嵌套容器，
   再依次找到 `operation` 和 `name`。`JEntry` 给出类型和累计结束位置，
   执行器由此定位连续数据区中的 payload。
5. 终点的 JSONB string `execute_tool` 转换为 SQL text，随后用于谓词比较并返回。

JSONB 免去文本词法和语法解析，原因是类型、元素数量、位置和 payload 已经
编码在二进制容器中。对象容器不是额外的树索引；它把排序后的 pair 保存为
连续数组。含 `m` 个 pair 的单层对象通常需要 `O(log m)` 次键比较，整条路径的
查找成本是各层成本之和；字符串比较、de-TOAST 和 payload 读取仍会产生开销。
数组容器则根据下标直接访问对应 `JEntry`。

表达式索引、GIN 或独立列条件负责缩小候选行集合，JSONB 容器负责候选行内的
路径访问。

### 2.5 查询完整文档的流程

查询同一行的完整 JSONB 文档：

```sql
SELECT attributes::text
FROM trace_events
WHERE event_id = 42;

{"dup": "new", "tags": ["db", "json"], "empty": {}, "score": 123.00,
 "gen_ai": {"operation": {"name": "execute_tool", "attempt": 2}}}
```

执行器先通过主键取得 heap tuple，并在需要时完成 de-TOAST。JSONB 输出函数从根
容器开始迭代 `JEntry`：对象输出键和值，数组按下标输出元素，嵌套容器递归执行
同一过程，标量按当前类型转为 JSON 文本。

示例输出反映了写入阶段的规范化结果：根对象键已排序，重复键只剩 `new`，转义
字符串已解码，科学计数法已转换为 Numeric 输出形式。空对象和数组顺序仍然保留。

完整文档来自基列中的文档树。表达式 B-tree 和 GIN 只筛选候选行，不参与文档
重建，也不降低已选中行的完整 JSON 序列化成本。

openGauss JSON 可直接返回保存的文本表示，因此完整文档读取可能更低。JSONB 的主要收益集中在字段访问、包含查询和索引能力。

### 2.6 B-tree 和 GIN 索引

B-tree 为每行生成一个可排序的索引键，GIN 从一行 JSONB 中提取多个索引项。
因此，它们面向不同的谓词形式。

| 索引形式 | 每行写入的索引键 | 可匹配的典型谓词 | 适用场景 |
|---|---|---|---|
| JSONB 基列 B-tree | 完整 JSONB datum，按 JSONB 比较顺序排列 | 基列的 `=`、`<`、`<=`、`>=`、`>` | 整份文档比较或排序；通常不用于某个内部路径的过滤 |
| 表达式 B-tree | 表达式的结果，例如 `#>>` 提取的 SQL text | 与索引表达式及类型匹配的等值、范围或排序 | 路径稳定的高频标量条件 |
| GIN `jsonb_ops` | 独立的键和标量值项 | `@>`、`?`、<code>?&#124;</code>、`?&` | 路径和谓词多样的包含查询，以及顶层键或数组字符串的存在性查询 |
| GIN `jsonb_hash_ops` | 由根容器类型、沿途对象键和终点标量计算的 hash | `@>` | 以固定结构包含为主的查询 |

以下语句分别展示四种可选布局。实际表只创建负载需要的索引。

```sql
CREATE INDEX trace_events_attributes_btree
ON trace_events (attributes);

CREATE INDEX trace_events_operation_name_btree
ON trace_events ((attributes #>> '{gen_ai,operation,name}'));

CREATE INDEX trace_events_attributes_gin_ops
ON trace_events USING gin (attributes jsonb_ops);

CREATE INDEX trace_events_attributes_gin_hash_ops
ON trace_events USING gin (attributes jsonb_hash_ops);
```

表达式 B-tree 将每行的 `operation_name` 字符串作为索引键，可直接服务以下
谓词：

```sql
WHERE attributes #>> '{gen_ai,operation,name}' = 'execute_tool'
```

查询表达式应与索引表达式的路径、操作和结果类型匹配。数值范围查询可在
建索引和查询时使用相同的数值类型转换，避免按 text 字典序比较。
基列 B-tree 的完整 JSONB 索引项还受单个 B-tree 索引项大小限制，因此不适合
大文档列。

GIN `jsonb_ops` 把对象键和字符串数组元素编码为带 `K` 前缀的 text 索引项，
把标量值编码为带 `V` 前缀的 text 索引项，可同时服务包含和存在性查询。
`jsonb_hash_ops` 把根容器类型、沿途对象键和终点标量合并为 hash 索引项，
通常产生更具体的候选集，但只支持包含操作符。
两者都可为以下谓词缩小候选行范围：

```sql
WHERE attributes @>
      '{"gen_ai":{"operation":{"name":"execute_tool"}}}'::jsonb
```

`@>` 表示左侧 JSONB 包含右侧描述的结构和值。基列上的 `?` 检查一个顶层
对象键或字符串数组元素，`?|` 和 `?&` 分别检查给定字符串集合中的任意一个和
全部元素。这三个存在性操作符只由 `jsonb_ops` 支持。

GIN `jsonb_ops` 不在单个索引项中保留键值间的完整结构关系，
`jsonb_hash_ops` 的 hash 也可能冲突。执行器会对两种 GIN 为 `@>` 返回的
候选行重新检查 JSONB 包含关系。普通 `#>> =` 谓词匹配表达式 B-tree，
不会自动使用上述两种 GIN。

openGauss 6.0.0 的 `jsonb_ops` GIN 在递归对象或数组中写入空字符串时存在
零长度复制错误。只需 `@>` 的负载可使用 `jsonb_hash_ops`；需要存在性操作符时，
应先在目标版本验证包含空字符串的写入。

索引能够降低候选行数，也会增加写入维护和空间成本。索引选择应结合路径
稳定性、谓词形式、查询频率和选择率。

### 2.7 文档恢复能力

JSONB 自动保存解析后的标量值、对象关系、空容器和数组顺序。写入过程会改变以下文本特征：

- 对象键原始顺序；
- 空白和缩进；
- 等价转义写法；
- 重复键的全部实例；
- 数值的原始文本形式。

因此需要区分三种输出或保存要求：

- **逻辑文档等价**要求解析后的键值、类型、空容器和数组顺序一致。JSONB 在写入时
  自动保存这份逻辑文档，不需要另建列。
- **数据库归一化文本**由 `jsonb_out` 或 `attributes::text` 在读取时自动生成。该文本
  遵循 openGauss 的键排序、转义和 Numeric 输出规则；主表不额外保存同样的文本
  bytes。
- **项目约定的 canonical 文本或原文 bytes**属于显式数据。跨引擎哈希要求统一
  canonical 规则时，载入程序必须执行该规则并保存结果。签名校验、精确重放、
  重复键审计或逐字节恢复要求在首次 JSON 解析前保存原始 UTF-8 bytes。JSONB
  不会自动创建这两类 Sidecar。

### 2.8 性能影响因素

| 因素 | 主要影响 |
|---|---|
| 文档大小和嵌套深度 | 写入构造、TOAST、路径遍历和完整序列化成本 |
| 候选行数 | 无匹配索引时，每个候选 JSONB datum 都需执行路径访问 |
| 谓词和选择率 | 决定表达式 B-tree 或 GIN 的有效性 |
| 索引数量 | 降低匹配查询成本，增加写入与空间成本 |
| 返回行数和内容 | 完整 JSON 返回量大时，序列化和传输成为主要成本 |
| 查询字段是否独立建列 | 普通强类型列可绕过 JSON 路径访问 |

## 3. ClickHouse Native JSON

### 3.1 Native JSON 的目标

ClickHouse Native JSON 在 SQL 层表现为表中的一个 `JSON` 列。物理层将 JSON 对象展开为路径，并尽量把同名路径保存为可独立读取的子列。

查询 `attributes.gen_ai.operation.name` 时，执行器可以读取对应路径的数据，避免读取
每行完整 JSON 文本。该结构面向字段级过滤、分组和聚合。写入增加路径展开、类型识别
和子列组织，完整文档读取增加路径重建。

### 3.2 表定义、partition 和 data part

ClickHouse Native JSON 通常存放在 MergeTree 表中。以下 DDL 用于本章的载入、
查询和 merge 示例：

```sql
CREATE TABLE analytics (
    project_id String,
    start_time DateTime64(3, 'UTC'),
    event_id UInt64,
    attributes JSON(
        max_dynamic_paths = 2,
        gen_ai.operation.name String
    )
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(start_time)
ORDER BY (project_id, start_time, event_id)
SETTINGS
    index_granularity = 2,
    min_bytes_for_wide_part = 0,
    min_rows_for_wide_part = 0,
    object_shared_data_serialization_version = 'advanced',
    object_shared_data_serialization_version_for_zero_level_parts = 'map_with_buckets';
```

`project_id`、`start_time` 和 `event_id` 是本章自定义的示例业务列。
ClickHouse 不会为每张表自动创建它们，Native JSON 也不要求表包含这些列。
本例使用它们表示项目、事件时间和事件标识，并在后续子句中定义分区与排序。

下面只解释与 Native JSON 或 MergeTree 物理组织相关的声明，顺序与 DDL 一致：

1. `attributes JSON(...)` 定义名为 `attributes` 的 Native JSON 列。列名由建表者
   选择，`JSON` 类型决定路径展开和子列存储方式。括号内的两项声明为：

   - `max_dynamic_paths = 2` 将每个 MergeTree data part 的 dynamic path 上限设为
     2。它限制专用 `Dynamic` 子列的数量，其余未声明路径进入 shared data。
     默认上限为 1,024；路径分类和重选规则集中在 3.3 节说明。
   - `gen_ai.operation.name String` 是 type hint：建表者显式声明路径和目标
     类型。ClickHouse 把该路径转换为 `String` 并始终作为固定类型子列
     保存，不计入 `max_dynamic_paths` 上限。路径缺失时，该子列使用
     `String` 的默认值空字符串，需要区分“缺失”和“值为空字符串”时还需
     额外保存存在性。

   `JSON(...)` 还支持以下本例未启用的控制项：

   - `max_dynamic_types` 限制单条 `Dynamic` 路径在一个 data part 中可分别
     保存的实际类型数，取值范围为 1—255，默认为 32。例如设为 2 后，
     `tokens` 先后保存整数和数组已占用两种类型，之后出现的新类型将转换为
     `String`。它限制单条路径的类型数，不改变 `max_dynamic_paths`。
   - `SKIP path.to.skip` 在解析时丢弃指定路径；路径指向对象时，整个对象都不
     写入 JSON 列。`SKIP REGEXP '^debug\.'` 丢弃所有匹配的扁平路径。

2. `ENGINE = MergeTree` 选择 MergeTree 存储引擎。`JSON` 是列的数据类型，
   MergeTree 是表的存储引擎，两者属于不同层次。Native JSON 也可用在
   `Memory` 等支持该类型的其他表引擎中，例如
   `CREATE TABLE memory_json (attributes JSON) ENGINE = Memory`。本章选择
   MergeTree，因为 data part、partition、稀疏主键索引和后台 merge 都是
   MergeTree 的持久化机制。

   本文将 data part 简称为 part。每批 INSERT 为每个涉及的 partition 生成新
   part。part 完整写入并发布后，其列流、压缩块、mark 和校验信息保持不变，
   查询由此获得一致快照。merge 和 mutation 通过写出新 part 并原子替换旧 part
   完成变更。

   merge 写出新 part 时也可重新分配 dynamic/shared 路径。具体选择规则见
   3.3 节，merge 的触发和重写流程见 3.5 节。
3. `PARTITION BY toYYYYMM(start_time)` 按月份生成 partition key。示例时间均进入
   partition `202609`。partition 是 part 的管理和 merge 边界；后台只会合并同一
   partition 内的 part。与分区表达式兼容的时间条件还可排除整个月份的 part，
   更细的行范围由排序键和索引处理。
4. `ORDER BY (project_id, start_time, event_id)` 定义排序键。括号内的三列是三个
   **排序分量**：先比较 `project_id`，相同时再比较 `start_time`，仍相同时
   比较 `event_id`。这种按先后优先级比较的方式称为字典序。3.4 节的
   三条输入因此按 `event_id=1`、`event_id=2`、`event_id=3` 的顺序写入 part。

   MergeTree 的排序键用于排列数据和构造稀疏主键索引，不是唯一性约束。
   多行可以具有完全相同的三个排序键值；这与 JSON 对象内的重复字段名无关。
   未单独声明 `PRIMARY KEY` 时，稀疏主键索引使用同一表达式。

   排序后的顺序定义 part 内行位置。所有普通列和 JSON 子流共享这套行号：
   位置 1 的 `event_id=2`、`operation.name=execute_tool`、`tokens=20` 和该行的
   shared data 共同构成一行，读取时不需要再按业务 ID 连接。
5. `index_granularity = 2` 控制 granule 的行数上限。**granule** 是 ClickHouse
   在列存文件中一次定位和读取的最小连续行集合；存储层不会只读取 granule
   中的单独一行。本例每个 granule 最多两行，因此三行数据形成行位置
   `[0, 2)` 和 `[2, 3)` 两个 granule。默认上限为 8,192 行，实际行数还受
   `index_granularity_bytes` 的字节限制。

   **mark** 是 granule 的起点定位信息。稀疏主键索引在 mark 处保存该 granule
   第一行的排序键，各列的 mark 文件保存同一行边界在压缩数据流中的偏移。
   使用排序键过滤时，查询先由稀疏索引确定可能命中的 mark range，再通过列流
   偏移读取对应 granule。更小的 `index_granularity` 能细化行范围裁剪，也会增加
   mark、索引和定位开销。

   granule 和 mark 主要服务于 MergeTree 的排序键和数据跳过条件。只读一条 JSON
   路径可以裁剪其他列流，但不会自动生成值索引；该路径未参与排序键、数据跳过索引
   或投影时，查询仍需要读取入选 part 中的相关 granule。

   启用 final mark 时，流末尾还会写入一个结束 mark。它不对应新 granule，
   因此本例有两个 granule，`system.parts.marks` 可显示 3。
6. `min_bytes_for_wide_part = 0` 和 `min_rows_for_wide_part = 0` 分别控制 data part
   使用 Wide 格式所需的最小字节数和行数。ClickHouse 25.12 开源版默认的
   字节门槛为 10 MiB，行数门槛为 0；小 part 因此常使用 Compact，达到门槛的
   part 使用 Wide。部署可修改这些默认值，实际值可从 `system.merge_tree_settings`
   查询。

   Wide 为每个列或子流使用独立数据文件，适合较大 part 和选择性读取。
   Compact 把多个列流写入同一数据文件，减少小批写入产生的文件数。
   本例把两个门槛都设为 0，固定生成 Wide part 以展示独立 JSON 子流；
   该取值是演示设置。
7. `object_shared_data_serialization_version = 'advanced'` 选择 merge 生成的
   较高层 part 如何落盘 shared data。`advanced` 为单路径读取保存更细的定位
   信息，增加相应的写入和元数据工作。它与另外两种取值的结构差异见 3.3 节。
8. `object_shared_data_serialization_version_for_zero_level_parts = 'map_with_buckets'`
   单独控制 INSERT 直接生成的 zero-level part。这里选择 `map_with_buckets`，
   使新 part 的单路径查询只读一个 bucket，同时避免在 INSERT 阶段生成
   `advanced` 的全部定位元数据。后台 merge 生成较高层 part 时，再按上一项设置
   改用 `advanced`。两项设置共同决定不同层级 part 的 shared-data 序列化，
   不改变 SQL 中的路径名和结果类型；三种序列化与单路径查询的关系见 3.3 节。

以下完整行用于展示这些声明的作用：

```json
{
  "project_id": "p1",
  "start_time": "2026-09-10 10:00:00.200",
  "event_id": 42,
  "attributes": {
    "gen_ai": {
      "operation": {
        "name": "execute_tool"
      }
    },
    "tokens": 20,
    "region": "us",
    "vendor_model": "m1"
  }
}
```

`attributes` 展平后包含 `gen_ai.operation.name`、`tokens`、`region` 和
`vendor_model`。第一条路径命中 type hint；按示例的发现顺序，`tokens` 和
`region` 成为两条 dynamic path，`vendor_model` 进入 shared data。

字段和 DDL 设置确定逻辑数据的物理去向。3.4 节三条输入生成的 Wide part
可表示为：

```text
数据库
└── MergeTree 表 analytics
    └── partition 202609
        ├── data part A：一批不可变、按 ORDER BY 排列的行
        │   ├── 行位置 0：[project=p1, time=.100, event=1]
        │   ├── 行位置 1：[project=p1, time=.300, event=2]
        │   ├── 行位置 2：[project=p2, time=.200, event=3]
        │   ├── granule 0：行位置 [0, 2)，第一行为位置 0
        │   │   └── mark 0
        │   │       ├── 稀疏主键记录：(p1, .100, 1)
        │   │       └── 各数据流记录：该行边界的压缩数据偏移
        │   ├── granule 1：行位置 [2, 3)，第一行为位置 2
        │   │   └── mark 1
        │   │       ├── 稀疏主键记录：(p2, .200, 3)
        │   │       └── 各数据流记录：该行边界的压缩数据偏移
        │   ├── 稀疏主键索引：[mark 0 的键, mark 1 的键]
        │   ├── 普通列数据流：project_id、start_time、event_id
        │   └── attributes JSON 列
        │       ├── type hint 子列：gen_ai.operation.name
        │       ├── dynamic 子列：tokens、region
        │       ├── shared data：vendor_model
        │       └── 各子流的 mark：与 granule 0、1 的行边界对齐
        └── data part B：另一次 INSERT 形成的另一批有序行
```

图中的所有列流按同一行位置对齐，granule 与 mark 按第 5 项的规则切分行流。
granule 和 mark 用于缩小行范围，type hint、dynamic path 和 shared data
用于组织 JSON 路径的列范围。

### 3.3 一个 data part 内的 JSON 表示

本节展开 3.2 节第 1、7、8 项涉及的 JSON 物理表示。JSON 对象在解析后形成
`gen_ai.operation.name`、`tokens`、`region`、`vendor_model` 等扁平路径。
一个 part 中的 JSON 路径有三类去向：

| 路径类别 | 产生方式 | 保存形式 | 主要特点 |
|---|---|---|---|
| type hint 路径 | DDL 写出路径及目标类型 | 固定类型子列 | 示例的 `gen_ai.operation.name String`；不占 dynamic path 数量 |
| dynamic path | 未声明路径在 `max_dynamic_paths` 上限内自动建立 | `Dynamic` 类型专用子列 | 示例 part 中的 `tokens`、`region`；可独立定位和读取 |
| shared data 路径 | `max_dynamic_paths` 已达上限后接收其余未声明路径 | JSON 列内部的共享子列 | 示例 part 中的 `vendor_model`；多个路径共用物理结构 |

type hint 路径在每个 part 中都有固定类型表示。它来自 DDL，查询历史不会
自动生成或更改 type hint。

路径基数是一个 part 中不同叶路径的数量；路径频率是某条路径具有非 null 值的
行数，路径密度则是该行数占 part 总行数的比例。例如，3.4 节的三行包含三条
`tokens`，因此该路径的频率为 3、密度为 100%；`vendor_model` 的频率为 1、
密度约为 33.3%。

`max_dynamic_paths=K` 只限制每个 part 最多保存 K 条 dynamic path，不规定最低
频率或密度。以下候选路径均指排除 type hint 后的未声明叶路径，其 dynamic/shared
归属在两个阶段确定：

1. INSERT 构造 zero-level part 时，未声明路径按发现顺序成为 dynamic path，直到
   数量达到 K；后续发现的路径进入 shared data。这个阶段不先统计并排序整批路径。
2. merge 构造目标 part 时，ClickHouse 汇总源 part 中候选路径的非 null 值数。
   候选路径基数不超过 K 时全部保留为 dynamic path；超过 K 时按非 null 值数
   从高到低选择前 K 条，计数相同则按路径名升序确定顺序，其余路径进入 shared data。

因此，shared path 可以在新目标 part 中成为 dynamic path，原 dynamic path 也可以
转入 shared data。这种归属重选发生在 merge 写出新 part 时，已发布 part 不会
就地换列。重选指标反映目标数据中的路径密度；查询次数和最近访问时间不参与选择。

专用 dynamic 子列拥有自己的类型元数据、值流和 mark，读取 `tokens` 时可直接打开
对应数据。shared data 需要先确定目标路径所在的共享结构、bucket 或 granule，再从
多个路径共用的数据中定位值。这些步骤构成单路径读取的额外定位工作。

shared data 属于当前 part 的当前 JSON 列。在内存中，它是一条
`Map(String, String)` 子列：每一行的 Map 将扁平路径映射到二进制编码值。示例中，
排序后的第一行包含 `{'vendor_model': encoded('m1')}`，后两行是空 Map。

它保存超过路径上限的残余路径和值，与 type hint 和 dynamic 子列共同组成该行的
Native JSON 分析表示。shared data 只是 JSON 列的一部分；扁平路径保留可重建的
对象关系，不保留空容器或原始文本。

写出 data part 时，shared data 可采用三种落盘序列化：

- `map` 把 shared data 写成一个 `Map(String, String)` 列流。读取单一路径时，执行器
  读取入选 granule 的 Map 键和值，再逐行筛选目标路径。
- `map_with_buckets` 按确定性规则将路径分到 `N` 个 bucket，每个 bucket 是一条 Map
  列流。执行器由路径计算 bucket，只读取该 bucket，再在桶内筛选目标路径。
- `advanced` 也可先分 bucket，并为每个 granule 保存路径列表和定位信息。
  `.structure` 记录 granule 是否包含目标路径及相关偏移，`.paths_marks` 记录该路径
  在 `.data` 中的起点。嵌套值还可拆成长度、元素或子字段等 substream。

从 `map` 到 `advanced`，单路径读取范围逐步缩小，同时增加写入工作、定位元数据和
磁盘空间。bucket 数量增加时，物理流和文件数量也会增加。

这三个名称都描述 shared data 写入 part 时的物理序列化，不是 INSERT
输入格式或 SQL 类型。3.2 节的 DDL 使 zero-level part 使用 `map_with_buckets`，
使 merge 生成的较高层 part 使用 `advanced`。这些设置直接影响 shared 路径的
读取量、写入工作和磁盘占用。

### 3.4 INSERT 流程

向 3.2 节的表提交三条 JSONEachRow 记录。为便于阅读，下面用 JSON 数组展示，
各行记录以逗号分隔。实际 JSONEachRow 输入使用同样的三个对象，每个对象独占
一行，不包含外层方括号和行间逗号。输入顺序有意不符合排序键顺序：

```json
[
  {
    "project_id": "p2",
    "start_time": "2026-09-10 10:00:00.200",
    "event_id": 3,
    "attributes": {
      "gen_ai": {
        "operation": {
          "name": "chat"
        }
      },
      "tokens": 30,
      "region": "cn"
    }
  },
  {
    "project_id": "p1",
    "start_time": "2026-09-10 10:00:00.300",
    "event_id": 2,
    "attributes": {
      "gen_ai": {
        "operation": {
          "name": "execute_tool"
        }
      },
      "tokens": 20,
      "region": "us"
    }
  },
  {
    "project_id": "p1",
    "start_time": "2026-09-10 10:00:00.100",
    "event_id": 1,
    "attributes": {
      "gen_ai": {
        "operation": {
          "name": "plan"
        }
      },
      "tokens": 10,
      "vendor_model": "m1"
    }
  }
]
```

该批数据依次经过以下过程：

1. 输入格式解析一行中的普通列和 JSON 值。
2. `attributes` 中的嵌套对象展开为 `gen_ai.operation.name`、`tokens`、`region`
   和 `vendor_model` 等叶路径。对象层级编码进点分隔路径，值保存在对应路径下。
3. `gen_ai.operation.name` 命中 DDL type hint，转换为固定 `String` 子列。
   `tokens` 推断为 `Int64`，`region` 和 `vendor_model` 推断为 `String` 候选；
   这些未声明路径以 `Dynamic` 容纳实际类型变化。
4. 当前 part 的 `max_dynamic_paths` 上限为 2。`tokens` 和 `region` 占用专用子列，
   随后出现的 `vendor_model` 写入 shared data。分配依据当前 part 中发现的路径和上限，
   不读取全表查询频率。
5. 三行均属于 partition `202609`。ClickHouse 生成排序排列
   `[输入行 3, 输入行 2, 输入行 1]`，并按该排列重排所有普通列和 JSON 子流。
6. 列流经过编码和压缩，按 granule 边界写出 mark、稀疏主键索引、校验和及元数据。
   该 INSERT 生成 zero-level part，shared data 使用 `map_with_buckets`。
7. part 完整写入后原子变为 active，查询从此可以看到这三行。

最终 part 内各物理流按相同行位置对齐：

| 位置 | 排序键 `(project, time, event)` | 固定路径 `name` | dynamic `tokens` | dynamic `region` | shared data |
|---:|---|---|---:|---|---|
| 0 | `(p1, 10:00:00.100, 1)` | `plan` | 10 | NULL | `{vendor_model:m1}` |
| 1 | `(p1, 10:00:00.300, 2)` | `execute_tool` | 20 | `us` | `{}` |
| 2 | `(p2, 10:00:00.200, 3)` | `chat` | 30 | `cn` | `{}` |

JSON 解析、类型识别和新 part 内的路径分配属于 INSERT 的同步工作。多个 part 的
merge 在 INSERT 完成后由后台执行。

### 3.5 后台 merge 和路径重组

频繁 INSERT 会产生多个小 part。每个 part 独立应用 dynamic path 上限，因此同一路径可以
在不同 part 中采用不同物理表示。

MergeTree 的后台调度器周期性检查各 partition，由 **merge selector** 从同一
partition 中选择一段相邻 active part。常规 merge 没有固定的行数或 part 数量
触发阈值，候选选择同时考虑以下因素：

- part 的数量、压缩大小和相邻 part 的大小比例，避免反复把悬殊大小的 part 合并；
- 后台线程池和磁盘可用空间。ClickHouse 25.12 的
  `max_bytes_to_merge_at_max_space_in_pool` 默认为 150 GiB，控制资源充足时的
  候选 part 总大小上限；`max_bytes_to_merge_at_min_space_in_pool` 默认为 1 MiB，
  控制资源紧张时的上限。这两个值限制一次 merge 的规模，不表示达到相应大小才启动；
- 可选的强制合并年龄。`min_age_to_force_merge_seconds` 默认为 0，即不按年龄强制
  merge。部署可以覆盖以上设置。

`parts_to_delay_insert=1000` 和 `parts_to_throw_insert=3000` 是 25.12 的默认写入
保护阈值：单个 partition 的 active part 过多时，前者延迟 INSERT，后者拒绝
INSERT。它们反映 merge 已跟不上写入，不负责启动 merge。

假设调度器选中 3.4 节生成的 part A 和随后生成的 part B，两者统计如下：

| 源 part | 路径及非 null 行数 | dynamic path | shared data |
|---|---|---|---|
| A，3 行 | `tokens=3`、`region=2`、`vendor_model=1` | `tokens`、`region` | `vendor_model` |
| B，4 行 | `vendor_model=4`、`failure.mistake_mode=2`、`tokens=1` | `vendor_model`、`failure.mistake_mode` | `tokens` |

SQL 始终使用 `attributes.tokens` 等逻辑路径名。读取 part A 时，执行器从 `tokens`
的 Dynamic 子列取值；读取 part B 时，同一路径改从 shared data 定位。具体物理结构
记录在 part 元数据中，不进入 SQL 表达式。

后台把 A 和 B merge 为目标 part C 时，依次执行：

```text
merge job
├── source parts
│   ├── part A：有序行流
│   └── part B：有序行流
├── merge processing
│   ├── 按 (project_id, start_time, event_id) 归并
│   ├── 汇总目标路径的非 null 数量
│   │   ├── vendor_model = 5
│   │   ├── tokens = 4
│   │   ├── region = 2
│   │   └── failure.mistake_mode = 2
│   └── 按 max_dynamic_paths = 2 重新分配路径
└── target part C
    ├── dynamic：vendor_model、tokens
    ├── shared：region、failure.mistake_mode
    ├── 新的列流、granule 和 mark
    └── level > 0；shared data 使用 advanced
```

1. MergeTree 在同一 partition 中选择 A、B 等 active part。
2. merge 读取普通列、JSON 专用子列和 shared data，将不同物理来源恢复为路径值流，
   再按排序键生成新的行序。
3. 目标 part 按 3.3 节的规则重新应用 dynamic path 上限。本例选择非 null 值数
   最多的 `vendor_model` 和 `tokens`；`region` 从 A 的
   dynamic 子列进入 C 的 shared data。`vendor_model` 和 `tokens` 各自在一个源 part
   中来自 shared data，进入 C 后都成为 dynamic path。
4. ClickHouse 按新行序和路径归属重新编码数据，并重新生成压缩块、granule、mark、
   稀疏主键索引、JSON 路径元数据和校验和。较高层 part 使用 `advanced` 序列化。
5. C 完整生成后原子发布为 active。A 和 B 转为 inactive，并在没有查询引用后等待
   清理。merge 创建新 part，不在原文件中就地移动路径。

每次 merge 都对新目标 part 执行路径归属计算。源 part 保持不变，直到目标 part
发布后转为 inactive；查询频率和最近访问时间不参与该过程。

查询使用开始时可见的 active data part。后台 merge 通常不持有阻塞普通查询的表级
锁，但会消耗 CPU、内存和磁盘带宽，可能提高并发查询延迟。

### 3.6 后台 merge、OPTIMIZE、FINAL 和 mutation

| 操作 | 发生阶段 | 是否重写 part | 与 dynamic path 的关系 |
|---|---|---:|---|
| 后台 merge | 写入后的异步维护 | 是 | 目标 part 重新选择 dynamic/shared 路径 |
| `OPTIMIZE TABLE ... FINAL` | 用户发起的物理维护 | 是 | 强制尝试合并适用 data part，间接触发路径重组 |
| `SELECT ... FINAL` | 查询执行阶段 | 否 | 应用 ReplacingMergeTree 等表引擎的最终行语义 |
| mutation | `ALTER` 等结构变更后的后台重写 | 是 | 修改 `max_dynamic_paths` 等定义时可能重写已有 data part |

`OPTIMIZE ... FINAL` 强制执行物理 merge，并非专门调整动态路径。新目标 part 按
正常规则重新组织路径；后续 INSERT 和后台 merge 仍继续运行。

`SELECT ... FINAL` 不修改磁盘数据。它在读取时应用 ReplacingMergeTree 等表引擎的行合并规则，与 dynamic path 重组无关。

修改 `max_dynamic_paths` 等 JSON 定义通常通过 mutation 重写已有 data part。该操作
会消耗显著资源，应作为结构变更管理。

### 3.7 查询一条路径

查询 3.4 节中 `p1` 的 `execute_tool` 事件：

```sql
SELECT
    event_id,
    attributes.gen_ai.operation.name AS operation_name
FROM analytics
PREWHERE project_id = 'p1'
  AND start_time >= toDateTime64('2026-09-10 10:00:00.200', 3, 'UTC')
  AND start_time <  toDateTime64('2026-09-10 10:00:00.400', 3, 'UTC')
WHERE attributes.gen_ai.operation.name = 'execute_tool';

 event_id | operation_name
----------+----------------
        2 | execute_tool
```

该查询从逻辑范围逐层缩小到物理读取位置：

1. `start_time` 的月份约束将 partition 缩小到 `202609`，查询只分析其中的 active
   part。
2. 稀疏主键索引使用排序键前缀 `project_id='p1'` 和时间范围确定 mark range。本例
   选择 mark 0 对应的 granule 0。该 granule 包含行位置 0 和 1，因此位置 0 也会被读取。
3. 各列的 mark 0 提供压缩数据偏移。执行器只读取 `project_id`、`start_time`、
   `event_id` 和固定类型路径 `gen_ai.operation.name` 在 granule 0 中的数据。
4. 解码后的两行执行精确 PREWHERE/WHERE 谓词。位置 0 被过滤，位置 1 返回
   `event_id=2`。

如果查询只包含 `attributes.gen_ai.operation.name='execute_tool'`，并且该路径不在
排序键、数据跳过索引或投影中，稀疏主键索引无法排除 granule。ClickHouse 仍只读取这个
固定类型子列，但需要覆盖所有入选 part 的 granule。

granule 和 mark 负责行范围定位；路径子列化负责列范围裁剪。

查询未提示的路径时，`attributes.vendor_model.:String` 中的 `.:String` 选择该
`Dynamic` 路径的 String 类型值。执行器先查看每个 part 的路径元数据：dynamic path
直接读取专用子列；shared data 路径按 `map`、`map_with_buckets` 或 `advanced`
结构定位。

各 part 的结果随后转换为查询要求的共同类型，再参与过滤、分组或聚合。

同一路径在不同 data part 中可以具有不同物理去向。读取性能取决于入选 data part 和 granule 数量、路径归属、shared data 序列化、实际类型数和返回量。

### 3.8 查询完整 JSON

查询整个 JSON 列时，执行器读取该行涉及的 type hint、dynamic path 和 shared data，
按照扁平路径重新组成对象，再序列化为输出格式。

完整读取需要汇集多个物理子流，无法利用“只读一个路径子列”的主要优势。路径越多、对象越宽、返回行越多，重建和序列化成本越明显。

因此，Native JSON 适合反复分析少量路径；String JSON 适合把 JSON 作为整体保存和返回。

### 3.9 信息边界与实验 Sidecar

Native JSON 按路径和值保存分析表示。以下输入信息不能仅依靠该列恢复：

- JSON null 与路径缺失在 Native JSON 语义中等价；
- 空对象没有叶路径，按叶路径重建时可能消失；
- 默认的点分隔扁平路径会使字面点键与嵌套路径形成相同表示；
- 空白、原始键顺序、重复键实例和数值原始写法不属于保留目标；
- type hint 路径缺失时产生声明类型的默认值，需要额外记录区分缺失和值等于默认值。

本实验的 Sidecar 是 Agent Trace 载入和查询程序实现的自定义结构，不是 ClickHouse
自动创建或维护的 Native JSON 功能。它用于补齐上述信息边界，使 ClickHouse
Native JSON 与 openGauss JSONB 按同一份解析后逻辑文档进行正确性校验和场景比较。
该可比性来自 Native JSON 与 Sidecar 的组合，不代表 Native JSON 单列具备完整文档
保真能力。

阶段二跨引擎实验采用以下结构：

1. `fidelity_values Map(String, String)` 是与 `attributes` 同行保存的稀疏
   Sidecar。载入程序只写入包含 JSON null、空对象、空数组或其他受 Native JSON
   展平影响的顶层分支及其 canonical 值。
2. 完整读取先由 Native JSON 重建对象，再由客户端按路径使用 `fidelity_values`
   覆盖对应分支。恢复结果规范化为 canonical JSON，并与同一输入在 openGauss
   JSONB 中的完整文档结果比较。
3. type hint 机制实验另加 presence marker，逐行记录指定路径是否存在，用于区分
   路径缺失和值等于声明类型默认值。

canonical 值是 JSON 解析后按确定规则重新序列化的文本，保留逻辑键值、类型和数组
顺序，不保留原始排版。机制实验还分别观察无 Sidecar、稀疏 Sidecar 和完整
canonical Sidecar，以分离保真范围、存储空间和客户端恢复成本。

字节级恢复使用另一张原文表保存首次解析前的 UTF-8 bytes。原文表与 Sidecar 的
语义恢复职责不同，用于签名校验、精确重放和字节级审计。

### 3.10 观测 data part 和路径归属

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

`JSONDynamicPathsWithTypes` 和 `JSONSharedDataPathsWithTypes` 同时返回类型。
`JSONAllPaths` 和 `JSONAllPathsWithTypes` 返回一行中出现的全部路径。

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

`system.columns` 主要显示表的声明结构。`system.merges` 显示正在执行的 merge，
`system.part_log` 用于检查历史 part 和 merge 事件。

### 3.11 性能影响因素

| 因素 | 对写入和维护的影响 | 对查询的影响 |
|---|---|---|
| 路径基数与每行路径数 | 增加解析、子列和元数据工作 | 增加完整对象重建成本 |
| `max_dynamic_paths` | 上限越大，可产生更多独立子列 | 命中 dynamic path 时读取更直接；上限过大增加结构开销 |
| 路径频率和密度 | merge 时按非 null 值数影响 dynamic/shared 选择 | 高密度路径更可能保留为 dynamic；查询频率不参与选择 |
| 每条路径的实际类型数 | 增加 Dynamic 类型子流；超过 `max_dynamic_types` 后的新类型转换为 String | 增加类型判断和转换成本 |
| INSERT 批次大小 | 小批写入产生更多 data part 和 merge 压力 | data part 较多时需要合并更多读取流 |
| shared data 序列化 | `map` 写入较轻；`advanced` 写入和空间更高 | bucket 和 advanced 更利于单路径读取 |
| 返回字段和行数 | 影响输出序列化 | 少量路径有利于 Native；完整对象和大结果削弱优势 |
| 后台 merge 并发 | 消耗 CPU、内存和磁盘 I/O | 可能提高查询尾延迟 |

type hint 的选择需要同时考虑查询频率、类型稳定性和缺失语义。类型经常变化的高频
路径可以保留为 Dynamic；要求固定类型和严格校验的路径可使用普通列或 type hint。

## 4. 两种结构化表示的流程差异

| 阶段 | openGauss JSONB | ClickHouse Native JSON |
|---|---|---|
| 输入解析 | 每个 JSON 文本解析为完整二进制文档树 | 每批 JSON 展开为路径并识别类型 |
| 基本物理单位 | 一行中的 JSONB datum | 一张 MergeTree 表的单个 data part 内的 JSON 子流 |
| 字段读取 | 在每个候选 datum 内逐层定位路径 | 按 part 读取 type hint、dynamic path 或 shared data |
| 候选数据缩减 | 独立列条件、表达式 B-tree 或 GIN | partition、排序键、granule、数据跳过条件 |
| 后台重组 | 常规表与索引维护；JSONB 文档本身不自动改组 | merge 生成新 part 并重新分配 dynamic/shared 路径 |
| 完整文档 | 遍历完整文档树并序列化 | 汇集叶路径和 shared data 后重建 |
| 逻辑恢复 | 自动保存解析后的完整逻辑文档 | 需要 Sidecar 补足 null、空容器等差异 |
| 原文恢复 | 依赖首次解析前保存的原文 | 依赖首次解析前保存的原文 |

两种结构的共同优势是减少字段查询时的 JSON 文本解析。openGauss JSONB 以单行完整
文档为中心；ClickHouse Native JSON 以一批行的同名路径为中心。场景化性能差异应
结合候选行数、访问路径、返回内容和后台维护共同解释。

## 参考资料

- [ClickHouse JSON 数据类型](https://clickhouse.com/docs/reference/data-types/newjson)
- [ClickHouse MergeTree](https://clickhouse.com/docs/reference/engines/table-engines/mergetree-family/mergetree)
- [ClickHouse 25.12 JSON 类型文档源文件](https://github.com/ClickHouse/ClickHouse/blob/v25.12.11.4-stable/docs/en/sql-reference/data-types/newjson.md)
- [ClickHouse 25.12 MergeTree 文档源文件](https://github.com/ClickHouse/ClickHouse/blob/v25.12.11.4-stable/docs/en/engines/table-engines/mergetree-family/mergetree.md)
- [ClickHouse 25.12 MergeTree 设置定义](https://github.com/ClickHouse/ClickHouse/blob/v25.12.11.4-stable/src/Storages/MergeTree/MergeTreeSettings.cpp)
- [ClickHouse 25.12 Native JSON 路径重选实现](https://github.com/ClickHouse/ClickHouse/blob/v25.12.11.4-stable/src/Columns/ColumnObject.cpp)
- [ClickHouse shared data 序列化实现说明](https://clickhouse.com/blog/json-data-type-gets-even-better)
- [ClickHouse system.parts_columns](https://clickhouse.com/docs/reference/system-tables/parts_columns)
- [ClickHouse OPTIMIZE](https://clickhouse.com/docs/reference/statements/optimize)
- [openGauss 6.0 JSON/JSONB 类型](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-types.html)
- [openGauss 6.0 JSON/JSONB 函数与操作符](https://docs.opengauss.org/en/docs/6.0.0/docs/SQLReference/json-jsonb-functions-and-operators.html)
- [openGauss 6.0 TOAST 阈值与外置结构定义](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/include/access/tuptoaster.h)
- [openGauss 6.0 TOAST 压缩与外置实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/gausskernel/storage/access/heap/tuptoaster.cpp)
- [openGauss 6.0 JSONB 数据结构定义](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/include/utils/jsonb.h)
- [openGauss 6.0 JSONB 输入输出实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb.cpp)
- [openGauss 6.0 JSONB 容器实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_util.cpp)
- [openGauss 6.0 JSONB GIN 实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonb_gin.cpp)
- [openGauss 6.0 JSONB 索引操作符类](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/include/catalog/pg_opclass.h)
- [openGauss 6.0 JSONB 索引操作符映射](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/include/catalog/pg_amop.data)
- [openGauss 6.0 JSON 查询函数实现](https://github.com/opengauss-mirror/openGauss-server/blob/v6.0.0/src/common/backend/utils/adt/jsonfuncs.cpp)
