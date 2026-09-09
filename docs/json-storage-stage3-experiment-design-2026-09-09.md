# Agent Trace JSON 存储阶段三实验设计

> 状态：待执行；阶段二横向报告完成后启动
> 初始设计日期：2026-09-07；文档修订日期：2026-09-09
> 数据路径：独立载入程序
> 上游边界：[阶段一报告](json-storage-stage1-report-2026-09-09.md)第 6、7 节、[阶段二实验设计](json-storage-stage2-experiment-design-2026-09-09.md)

## 1. 目标与范围

本实验比较 openGauss 6.0.0 与 ClickHouse 25.12.11.4 中四种长 payload 物理布局：同表内联、独立 payload 表、Full/Core 物化双表和 asset reference。实验统一数据、写入顺序、查询、正确性、原文恢复和指标口径，回答以下问题：

1. 列表和预览查询能否稳定裁剪完整 payload；
2. 物理分层产生的写放大、空间变化和详情恢复成本；
3. 单个长值与相同总字节的多字段对象是否表现不同；
4. asset reference 的内容恢复、引用完整性和基本故障行为。

阶段二已确定横向运行基础设施和可比性口径。阶段三复用该契约，主矩阵只包含两引擎相同的稳定列和 payload 相关列，不携带动态属性。数据库内 payload 使用字节保持的 TEXT/String，openGauss JSONB、Map、ClickHouse Native JSON 和长 payload 路径查询不进入本阶段变量。

实验不经过 Collector、exporter 或 benchmark，不形成端到端系统性能结论。完整对象存储服务、网络、鉴权、保留策略和多租户隔离不进入物理布局主矩阵。

## 2. 产物与目录

正式代码位于 `experiments/json-storage-stage3/`：

```text
experiments/json-storage-stage3/
  README.md
  generator/generate_payloads.py
  runner/run_payload_layouts.py
  runner/run_asset_failures.py
  tests/
```

运行产物位于 gitignored 的 `docs/temp/json-storage-stage3/`。每个完成目录包含 `run-manifest.json`；缺少该文件或 `status` 不是 `complete` 的目录不进入比较。最终结果写入 `docs/json-storage-stage3-report-YYYY-MM-DD.md`，报告主题写入文档标题。

## 3. 数据契约

seed 固定为 `20260907`。生成 160 个 payload，四组各 40 条：

| profile | 每条 canonical JSON bytes | 结构 |
|---|---:|---|
| `single_64k` | 65,536 | 单个长字符串 |
| `single_512k` | 524,288 | 单个长字符串 |
| `single_2m` | 2,097,152 | 单个长字符串 |
| `many_512k` | 524,288 | 256 个短字段组成的对象 |

内容由 seed、event ID 和 counter 经 SHA-256 扩展为确定性高基数 ASCII。生成器调整尾部字段，使 canonical JSON 达到精确目标字节数。每个 payload 使用独立内容，避免内容去重改变布局比较。

逻辑记录固定为：

```text
event_id, trace_id, project_id, start_time, profile,
payload, preview, content_type, encoding, content_length, sha256
```

`content_type` 固定为 `application/json`，`encoding` 固定为 `utf-8`，preview 为按 Unicode code point 截取的前 200 个字符。数据目录保存 payload 原始文件、事件目录、truth manifest 和生成 manifest。truth 记录每条 payload 的身份、长度、preview 和原始 SHA-256。

行序按四个 profile 交错排列。固定 INSERT block 为 16 行，每个 block 各含 4 条四种 profile，共 10 个 block。所有布局使用相同行序和 block 边界。

## 4. 物理布局矩阵

两个引擎使用相同的四种逻辑布局，每个布局执行三轮，布局顺序采用平衡轮换：

| layout ID | 物理组织 |
|---|---|
| `inline` | analytics 表内保存完整 payload |
| `separate` | `events_analytics` 保存 preview、length、hash，`event_payloads` 保存完整 payload |
| `full_core` | `events_full` 保存完整 payload，独立物化 `events_core` 保存 preview、length、hash |
| `asset_ref` | analytics 表保存结构化引用，本地内容寻址目录保存完整 payload |

openGauss 的数据库内 payload 使用 TEXT，并记录主表、索引和 TOAST 分项空间。ClickHouse 使用 `String CODEC(ZSTD(3))`，并记录列压缩前后字节、part 和 merge 状态。Full/Core 的 Core 表必须物化；普通 view 不构成物理分层候选。

asset reference 固定为：

```json
{
  "$ref": "asset:sha256:<digest>",
  "content_type": "application/json",
  "encoding": "utf-8",
  "content_length": 524288,
  "preview": "..."
}
```

asset resolver 只读取 manifest 中存在的 digest，并核对引用中的 MIME、encoding、长度和 SHA-256。物理布局主矩阵使用本地内容寻址目录，使结果只包含引用解析和本地文件读取成本。

## 5. 写入与查询

每轮使用相同行序和 block 边界。runner 记录载入 wall time、rows/s、MiB/s、block 延迟、可见延迟和后台维护状态。查询固定为：

| ID | 查询 | 返回内容 |
|---|---|---|
| L01 | 按 profile 和时间过滤、排序、分页 | 稳定列 |
| L02 | 按相同条件读取预览 | 稳定列和 200 字符 preview |
| L03 | 按 event ID 读取并恢复一个 64 KiB、512 KiB 和 2 MiB payload | payload bytes |
| L04 | 一次测量依次批量恢复四个 profile 的全部 payload | identity、length、SHA-256 摘要 |
| L05 | 比较 `single_512k` 与 `many_512k` | 写入、空间和恢复指标 |

查询测量分为两类：

- L01、L02 和 L03 各预热一次、正式测量 100 次，报告 median、p95、p99 和范围；
- L04 各预热一次、正式测量 5 次，报告 median 和范围；L04 每次完整读取约 122.5 MiB，全矩阵正式测量约读取 14.4 GiB，预热另读取约 2.9 GiB；
- L05 由对应 run 的写入、空间和恢复指标计算，不增加重复查询。

查询延迟从语句或 resolver 请求提交开始，至结果完整读取结束。连接建立、结果规范化和 SHA-256 核对位于计时区间外。每个 worker 在一个阶段内复用独立连接。请求等价速率定义为 `成功样本数 × 1000 / 成功样本延迟总和(ms)`，不表示饱和吞吐量。完成轮次要求所有正式样本和正确性门禁成功；失败轮次保存诊断 manifest，不进入布局比较。

## 6. 正确性与原文恢复

所有布局必须通过以下门禁：

- 160 条 identity 一一对应，没有缺失、额外和重复记录；
- `content_type`、encoding、content length 和 preview 与 truth 完全一致；
- 每条恢复内容的原始 SHA-256 与 truth 一致；
- L01 和 L02 的排序、分页和返回集合一致；
- Full/Core 的物化水位覆盖本轮全部成功写入；
- asset reference 的 digest、元数据和实际对象一致。

数据库中的 canonical JSON 不能替代原始 bytes。每个布局均需提供完整 payload 的字节级恢复路径，报告分别记录数据库备份范围内字节和 asset 目录字节。

## 7. Asset 故障实验

物理布局主矩阵通过后，对 `asset_ref` 单独执行以下确定性故障：

| 场景 | 预期结果 |
|---|---|
| 引用存在、对象缺失 | resolver 返回明确的 missing 状态并记录 digest |
| 对象内容被修改 | 长度或 SHA-256 门禁失败，内容不作为成功结果返回 |
| 元数据长度或 MIME 不一致 | resolver 返回 metadata mismatch |
| upload-first 后数据库写入失败 | 核对程序识别 orphan 对象 |
| 数据库引用存在后对象删除失败 | 核对程序识别仍被引用的对象，不发布删除完成状态 |

故障实验采用 `pending`、`available`、`failed`、`deleting` 四个状态记录操作结果。状态转换、诊断字段和恢复动作进入 manifest。网络重试、鉴权、跨区域复制和生产级删除传播需要接入目标对象存储后另行验证。

## 8. 指标与 Manifest

每轮记录：

- 主表、payload 表、Core、Full、索引、TOAST/part 和 asset 目录的分项空间；
- 载入时间、写入字节、写放大、物化可见延迟和后台维护时间；
- 查询延迟、请求等价速率、返回字节和引擎可提供的 read rows/bytes、CPU、内存；
- resolver 请求数、读取字节、校验时间和错误分类；
- 数据库备份范围内字节、外部 asset 字节和两者总和。

run manifest 至少记录 run ID、状态、完整复现命令、输入与 truth SHA-256、代码/DDL/查询 catalog SHA-256、数据库版本与镜像 digest、宿主资源、layout、轮次、布局顺序、缓存状态、测量次数、正确性结果和清理状态。

## 9. 停止条件与报告边界

出现以下情况时停止对应候选，发布 `status=failed` 的诊断 manifest：

- 输入或 truth SHA-256 不一致；
- identity、preview、长度或原始 SHA-256 门禁失败；
- Full/Core 无法确定物化水位；
- asset resolver 把缺失、损坏或元数据不一致的对象作为成功结果返回；
- 临时数据库对象或本轮 asset 目录清理失败；
- 需要修改 exporter、benchmark、数据库镜像或完整产品服务才能继续。

阶段三报告只记录长 payload 数据契约、四种存储结构结果、正确性、原文恢复、asset 故障结果、适用范围和建议。阶段一引擎内机制数据与阶段二动态属性数据只作为引用，不进入阶段三结构比例计算。

## 10. 参考资料

- [阶段一报告](json-storage-stage1-report-2026-09-09.md)
- [阶段一实验设计](json-storage-stage1-experiment-design-2026-09-09.md)
- [阶段二实验设计](json-storage-stage2-experiment-design-2026-09-09.md)
- [JSON 存储设计调研](json-storage-design-survey-2026-09-09.md)
