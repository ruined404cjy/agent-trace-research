# 阶段三黄区实验报告：XStore 与 ClickHouse 23.3.10.5 同机对比

**日期**：2026-09-24  
**主机**：10.44.133.161，aarch64 EulerOS 2.13，256 核 1999 GiB  
**工具包 HEAD**：a9143d1（含 d851e02 修复：持续写入重放的行存主键冲突）  
**分支**：stage3/xstore-yellow-161-20260924

---

## 1 结论

在同机串行条件下，ClickHouse 23.3.10.5 与 XStore（GaussVector 103.0.0 release）在四布局上的 JSON 存储对比结论如下。

**布局空间排序（K1 中位数耗时，越小越快）**：

| 布局 | XStore list/detail/trace | ClickHouse list/detail/trace |
|---|---|---|
| same_table | 28.6 / 28.2 / 28.2 ms | 23.8 / 34.5 / 34.9 ms |
| separate | 124.7 / 58.2 / 59.1 ms | 26.7 / 32.3 / 33.6 ms |
| full_core | 31.6 / 33.7 / 31.6 ms | 65.3 / 157.2 / 70.7 ms |
| asset_ref | 1541.9 / 1406.5 / 1530.5 ms | 2301.6 / 2731.2 / 2325.4 ms |

- **same_table**：两引擎接近。ClickHouse 在 list 上略快（23.8 vs 28.6 ms），XStore 在 trace 上略快（28.2 vs 34.9 ms）。
- **separate**：XStore 在 list 上显著慢（124.7 vs 26.7 ms），因行存需扫描分离的 JSON 列。ClickHouse 列存优势明显。
- **full_core**：ClickHouse 在 detail 和 trace 上显著慢（157.2/70.7 vs 33.7/31.6 ms），因 full_core 布局将 payload 拆到 core 表，列存需跨表拼接。XStore 行存在此布局下保持稳定。
- **asset_ref**：两引擎均最慢，XStore 比 ClickHouse 快约 1.5x（1406.5 vs 2731.2 ms on detail），因 asset_ref 需二次解析 payload。

**适用边界**：结论限定为同机 XStore release 构建（103.0.0, ec1b24410e）与 ClickHouse 23.3.10.5 的对比，冻结输入 48,534 条事件、190 个写入 block。主机为共享开发服务器，负载检查使用 `--accept-host-check`。

---

## 2 数据与语义契约

### 2.1 冻结输入

- **identity_sha256**：a71b4c3a798dd9cb45afef1be2653857afe5fdd61978c7da49cf80dbef3094b8
- **record_count**：48,534 条事件
- **block_count**：190 个写入 block（每 block 256 行）
- **main cohort**：160 条 payload
- **原始大小**：122.5 MiB
- **seed**：20260907
- 证据：generation-manifest.json（status=complete），八 target 共用同一输入身份

### 2.2 XStore 存储形态

XStore 使用行存 heap+toast 模型：
- `events` 表（same_table）：payload TEXT 列存于 toast，其余列在 heap
- `events_full` + `events_core` 表（full_core）：payload 拆到 events_full，core 表无 payload
- `events` + `jsons` 表（separate）：payload 存于独立 jsons 表
- `events` + asset 引用（asset_ref）：payload 通过 asset resolver 二次加载

XStore 适配器偏差：
- framework 列去掉 NOT NULL（GaussDB 将空字符串视为 NULL）
- keyset 比较展开为 OR 形式
- FILTER (WHERE ...) 不支持，改用 count(CASE WHEN ...)
- 本地 Unix domain socket trust 认证连接

---

## 3 实验配置

### 3.1 引擎版本

| 引擎 | 版本 | 构建类型 | 二进制 SHA-256 |
|---|---|---|---|
| XStore | GaussVector 103.0.0 build 66de5983 | release | 68e346e94e74e4f29cbc9aeda9bf9b770988b9f217823830b2a1de11ce529387 |
| ClickHouse | 23.3.10.5 | LTS TGZ | ae9d4b1c6a6de9cefe91a5d3eca25511120e8ea969fbaf10a4506300f1ae2240 |

### 3.2 运行参数

- 测量次数：30（batch 5）
- 四轮 Latin square
- 引擎串行：先 XStore（停 ClickHouse），后 ClickHouse（停 XStore）
- 端口：XStore 29000，ClickHouse 18123(HTTP)/19000(TCP)
- 主机负载检查：`--accept-host-check`（共享主机，其他用户 gaussdb 进程导致 1-min load 超阈值）

### 3.3 代码修改

| 文件 | 修改 | 原因 |
|---|---|---|
| yellow_round.py L237 | `pgrep -x gaussdb` → `pgrep -x -u $USER gaussdb` | 共享主机上其他用户的 gaussdb 进程导致串行检查误报 |

---

## 4 主矩阵结果

### 4.1 K1 — 小查询中位数耗时（ms）

来源：feedback-core.txt K1 段，main workload 四轮中位数。

| 布局 | XStore list | XStore detail | XStore trace | CH list | CH detail | CH trace |
|---|---|---|---|---|---|---|
| same_table | 28.6 | 28.2 | 28.2 | 23.8 | 34.5 | 34.9 |
| separate | 124.7 | 58.2 | 59.1 | 26.7 | 32.3 | 33.6 |
| full_core | 31.6 | 33.7 | 31.6 | 65.3 | 157.2 | 70.7 |
| asset_ref | 1541.9 | 1406.5 | 1530.5 | 2301.6 | 2731.2 | 2325.4 |

### 4.2 K2 — 预览查询中位数耗时（ms）

| 布局 | XStore preview | CH preview |
|---|---|---|
| same_table | 10.8 | 13.4 |
| separate | 26.2 | 72.8 |
| full_core | 25.8 | 76.2 |
| asset_ref | 13.7 | 36.3 |

### 4.3 K3 — 批量写入吞吐（MiB/s）

| 布局 | XStore | ClickHouse |
|---|---|---|
| same_table | 1053.2 | 1651.5 |
| separate | 967.4 | 1587.9 |
| full_core | 1056.4 | 1709.2 |
| asset_ref | 517.6 | 664.1 |

ClickHouse 列存在批量写入上全面领先。full_core 布局两引擎均达到各自峰值。

### 4.4 K4 — 批量写入尾延迟与磁盘占用

来源：feedback-core.txt K4 段。每行格式：p95 吞吐(MiB/s)、磁盘占用(bytes)、toast/溢出占用(bytes)。

---

## 5 控制项结果

### 5.1 K5 — Part 状态控制（ClickHouse）

来源：feedback-core.txt K5 段。四布局 × 四状态共 5,760 样本。

| 布局 | fragmented | merging | settled | single |
|---|---|---|---|---|
| same_table | 89.4 / 22.6 / 25.6 / 16.0 | — | — | — |
| separate | 88.8 / 28.8 / 29.9 / 30.3 | — | — | — |
| full_core | 89.0 / 29.8 / 29.0 / 31.1 | — | — | — |
| asset_ref | 88.7 / 29.4 / 33.2 / 33.3 | — | — | — |

### 5.2 K6 — 混合负载（Interference）

来源：feedback-core.txt K6 段。每布局五阶段（quiet → detail_2m → trace_long → batch_loop → continuous_ingest），每阶段 30s 预热 + 300s 测量。

XStore 与 ClickHouse 均完成全部 4 布局 × 5 阶段的混合负载测试。

### 5.3 K7 — Asset 故障与恢复

来源：feedback-core.txt K7 段。XStore 与 ClickHouse 各 6 个固定故障用例。

- XStore: available available available absent failed deleting
- ClickHouse: available available available absent failed deleting

---

## 6 补充核对（follow-up check）

合入 a9143d1 后执行 `yellow_round.py check --host 161 --date 2026-09-24`，五项全部通过，无 NA 项。

| 编号 | 项目 | 结果 |
|---|---|---|
| F1 | 代码身份 | 127 runs, 0 non_head, 全部 HEAD 运行 |
| F2 | 干扰负载样本 | 全部布局×阶段×请求流均有样本 |
| F3 | XStore 服务端时间 | 主矩阵小查询计划 Total runtime 与顶层节点耗时已记录 |
| F4 | XStore 往返下限 | exec_select_1 p50=0.16ms, params_select_int p50=0.29ms, params_catalog p50=0.43ms |
| F5 | B3 段原文 | 已回传 |

F4 表明 XStore 小查询的固定耗时主要来自协议往返（SELECT 1 仅 0.16ms），参数绑定增加约 0.13ms，catalog 查询增加约 0.14ms。对比 K1 中 same_table list 的 28.6ms，服务端执行占绝大部分。

---

## 7 数据完整性

- 2 引擎 × 4 布局 × 4 workload = 32 个 target，全部 complete
- 正式样本数：见 summary/*.json
- 失败样本：0
- 冻结输入身份验证通过
- preflight 单元测试 exit 0
- pack 打印：total bytes 76,605,544; archive parts 2; missing runs none
- 主机检查原文记录于 facts/host-checks.txt（使用 --accept-host-check）
