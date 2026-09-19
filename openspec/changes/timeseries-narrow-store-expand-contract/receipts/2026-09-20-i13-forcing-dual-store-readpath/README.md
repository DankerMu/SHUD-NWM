# I13 8.1 第二部分 — forcing 双存储读路径对照（2026-09-20）

`000061` 已于 2026-09-20 00:27 落地（见 `../2026-09-20-i13-forcing-expand-window/`）。当时窄表 0 行，两条读探针字节相同，第二部分被记为"blocked on data, not on effort"。本 receipt 记录数据到位后补齐的部分。

**全程零生产写入。** 所有 DB 访问走 `nhms_ingest_rw` 车道、`BEGIN READ ONLY`，DSN 仅经 env；HTTP 只发 GET。没有手动触发任何 timer、pipeline 或压缩。

## 0. 对照组

同 basin、同 model、同源，一个 legacy 路由一个 narrow 路由：

| | legacy | narrow |
|---|---|---|
| `run_id` | `fcst_gfs_2026091812_dg_0883c7e9…` | `fcst_gfs_2026091900_dg_0883c7e9…` |
| `forcing_version_id` | `forc_gfs_2026091812_…` | `forc_gfs_2026091900_…` |
| `cycle_time` | 2026-09-18 12:00Z | 2026-09-19 00:00Z |
| `run.status` | `published` | `parsed` |
| `timeseries_store` | `legacy` | `narrow` |
| `forcing_version.station_count` | 71 | 71 |
| `checksum IS NOT NULL` | t | t |

`model_id` 两边同为 `dg_0883c7e9c1006c6fd347df500315e9df`，`basin_version_id` 同为 `basins_qhh_vbasins`。

## 1. station-series API 字段一致性

**先界定主张的边界。** 本节比的是**两个不同的 forcing 版本**（不同 cycle），一个走 legacy 一个走 narrow。这不是 spec `:36`——`:36` 要求"同一个 forcing version 分别以 legacy 和 narrow 物化"，`tasks.md` 已记录的偏离说明该场景需要两个 candidate run、同一版本，生产上不存在。这里成立的是**跨存储的结构一致性**：同一读接口在两种存储下产出同形载荷、同口径计数。这是 I8 在生产上的首次真实验证，但不替代 `:36`。


两个版本都经公网读路径 `https://test.nwm.ac.cn/api/v1/mvp/qhh/latest-product` 严格定址取回（`source` + `model_id` + `run_id` + `cycle_time`，`forecast.py:133` → `forecast_store.py:2086`）。**`run_id` 严格定址让两者同时可达**，不必等端点选中谁——`parsed` 状态的 narrow run 也返回 200。

载荷存档：`payload-legacy.json` / `payload-narrow.json`（已剥 `request_id`）。两侧均 HTTP 200，4141 B / 4138 B。

全树穷举比对（下钻每个 dict 键与**每个**数组元素，不是只看 `[0]`）：

```
legacy-only paths : NONE
narrow-only paths : NONE
type mismatches   : NONE
paths compared    : 144
value-differing   : 23
```

**144 条叶子路径，键集零差异，类型零不符。** 更强的是值层：144 条里只有 **23** 条取值不同，且逐条核对**全部**属于两个 run 本就不同的身份或时间属性——`run_id`、`forcing_version_id`、`cycle_time`、`run_status`，以及 `valid_time_start/end` 及其在 `river_*` / `forcing_* ` / `availability.quality_notes[0]` / 六个 `station_variable_coverage[i]` 下的重复投影。**其余 121 条路径逐值相同**，包括全部计数、单位数、质量标志数、变量标签与索引条目。

这是 I8 "同一读接口在两种存储下产出同形载荷"在生产上的首次真实验证——此前所有证据都来自集成测试。

逐变量覆盖块（`quality.station_variable_coverage`，直读 forcing 事实表产出）六个变量各自两侧相同：

```
station_count=71  sample_count=3976  unit_count=1  quality_flag_count=1
missing_unit_samples=0  missing_quality_flag_samples=0
```

`quality.station_sample_count` 两侧同为 **23856**（71 站 × 6 变量 × 56 时步），`required_station_variables` 同为 `[PRCP,TEMP,RH,wind,Rn,Press]`。

窄表侧 `unit_count=1` / `quality_flag_count=1` / `missing_*_samples=0` 是 `variable_e::text` / `unit_e::text` / `quality_flag_e::text` 三个枚举投影正确工作的直接证据：标签文本与 legacy 的 text 列逐值相同，且无一为 NULL。

**声明的是结构一致，不是字节一致**——两个 cycle 的数值本就不同。

## 2. 覆盖损失列表：空

窄腿按 `met.met_station.basin_version_id` 连接，legacy 腿按事实行自身的 `basin_version_id`（这一分歧正是 `tasks.md` 已记录的 `:36` 偏离）。对 `forc_gfs_2026091900_…` 三处独立计数：

| 口径 | 值 |
|---|---|
| `met.forcing_version.station_count` | 71 |
| `count(DISTINCT station_key)`（窄表事实行） | 71 |
| `hydro.run_display_coverage.station_count` | 71 |

三者相等，**覆盖损失列表为空**。API 载荷的 `station_count` / `expected_station_count` 亦同为 71。

## 3. QHH fallback before/after EXPLAIN

`explain-before-after.txt` 是 `EXPLAIN (ANALYZE, BUFFERS, COSTS)` 全文。两条腿取自 `forecast_store.py:265` `_LATEST_PRODUCT_STATION_SOURCE_TEMPLATES` 的 `legacy` / `narrow` 成员，`candidate_runs` 用 `VALUES` 注入各自 run 的真实身份。**改名不改变计划**，所以 legacy 模板打 `met.forcing_station_timeseries_legacy` 就是 "before"，narrow 模板打窄表就是 "after"。

| | before（legacy 腿） | after（窄腿） |
|---|---|---|
| 返回行数 | 23 856 | 23 856 |
| Execution Time | 1 612.573 ms | 408.723 ms |
| Planning Time | 13.303 ms | 6.103 ms |
| Seq Scan 数 | 0 | 0 |
| shared hit / read | 210 904 / 1 244 | 533 616 / 0 |

行数与 §1 的 `station_sample_count=23856` 一致——两条腿看见同一批样本。窄腿墙钟快 **3.9×**。

**但窄腿有一处真实的索引覆盖退化，记录在此而不在此修。** legacy 腿上 `interp_weight` 的 semi-join 是纯索引条件：

```
Index Cond: (model_id = … AND station_id = fst_1.station_id
             AND variable = fst_1.variable AND lower(source_id) = 'gfs')
  → rows=1 per loop, buffers shared hit=173376
```

窄腿上 `variable` 掉出 Index Cond，降级为 Join Filter：

```
Join Filter: ((fst_1.variable_e)::text = iw.variable)
Rows Removed by Join Filter: 59640
Index Cond: (model_id = … AND station_id = ms.station_id AND lower(source_id) = 'gfs')
  → rows=4 per loop, buffers shared hit=417928
```

原因是 `met.interp_weight.variable` 是 `text`（`000005_met.sql:66`），而窄表是 `met.forcing_variable` 枚举（`000061:202-209,245`），模板 `forecast_store.py:348` 写的是 `iw.variable = fst.variable_e::text`；`interp_weight_qhh_latest_membership_idx`（`000024_qhh_latest_display_product_indexes.sql:19-25`，列 `(model_id, station_id, variable, LOWER(source_id))`）的第三列因此不再可用，每次探测退化为扫该 `(model_id, station_id, source_id)` 下的全部 4 个变量行再过滤。该节点 buffer 命中是 legacy 的 **2.4×**，总 buffer 命中 2.5×。当前全内存命中（`read=0`）掩盖了代价，墙钟仍净赢；但这是 fallback 腿上唯一一处 after 比 before 差的量纲。不在本任务范围内修——**issue #2516**。

同形兄弟两处，本 receipt 未取证、仅登记：`display_coverage.py:276`（legacy 副本在 `:231`）文本完全相同，在 coverage 刷新路径上；`best_available.py:67` 的 `fvc.variable = fst.variable_e::text` 是同形态、不同表。文本由 `271e0d8bf`（PR #2494）写入时按构造不可执行，`4400f3730`（PR #2509）切路由后才成为在产语句。

**取证边界：两条 EXPLAIN 不是同一批输入。** legacy 腿喂 `forc_gfs_2026091812_…`、窄腿喂 `forc_gfs_2026091900_…`，显示窗口随之不同。相同的是身份形态、模板结构与 23 856 行的结果规模；不同 cycle 的绝对耗时不可直接相减作为改造收益。

## 4. M3a：`finalize_forcing_version` 在生产上确实运行

窄表落地后写入的全部 **11** 个 narrow 版本，`checksum` 均 NOT NULL。7.3 的 P1 若复发，表现恰是 `checksum` 为 NULL 继而 409；十一个版本干净写入是该缺陷未发生的最强在产陈述。

## 5. 生产 API 正在返回一条错误事实（新发现）

两份载荷的 `quality.query_indexes` 都断言：

```json
{"table": "met.forcing_station_timeseries",
 "index": "forcing_station_timeseries_qhh_latest_window_idx",
 "status": "covered_by_latest_product_station_window_index", …}
```

该索引**只存在于 legacy 表上**。目录实测：

```
forcing_station_timeseries        | forcing_station_timeseries_narrow_pkey
forcing_station_timeseries        | forcing_ts_version_variable_time_key_idx
forcing_station_timeseries_legacy | forcing_station_timeseries_pkey
forcing_station_timeseries_legacy | forcing_station_timeseries_qhh_latest_window_idx
forcing_station_timeseries_legacy | forcing_station_timeseries_valid_time_idx
```

§3 的 after 计划也印证：窄腿实际走的是 `forcing_ts_version_variable_time_key_idx`。`forecast_store.py:4262-4274` 是硬编码字面量、不读目录（装配于 `:3666`，路由 `apps/api/routes/forecast.py:132`），所以 `000061` 改名后它开始对公网说假话。**issue #2517。**

**两处收窄与一处加重，均已复核：**

- **收窄**：兄弟副本 `:4759-4768` **不是**对外输出。`station_forcing_readiness` 全仓只出现在 `forecast_store.py` 与 `tests/`，无 HTTP 路由。"公网错误断言"这一档只挂在 `:4262-4274`；`:4759-4768` 是同类内部副本，同样双错但不对外。本节初稿把两者合并表述，是我说过头了。
- **加重**：**今天两条路由的响应都是假的，不只是窄腿。** `forcing_ts_render.py:92` 现值已是 `FORCING_TABLE_LEGACY = "met.forcing_station_timeseries_legacy"`，所以 legacy 路由的 SQL 读 `_legacy`，载荷却报表名 `met.forcing_station_timeseries`——窄腿错在索引名与 `columns`，legacy 腿错在**表名**。过渡期两条路由都在产（legacy 4 289 / narrow 4 600）。讽刺的是同一方法里 SQL 是按路由选的（`forecast_store.py:2088`），描述 SQL 的诊断却是无参常量。
- **归属更正**：我先前以为 `tasks.md` 的 8.3 带着这条"陈旧索引诊断载荷"，不成立——8.3（`:357`）里没有，#1993 正文里也没有。该句实际在 **PR #2509 的 body**，且其中"spec assigns index pins to 8.3"无出处：仓内唯一归属记录是 `fixtures/I11-1990.md:190`，把这两个站点列为 census 豁免、owner 写的是 **"7.3 (I12) index pins"**，而 I12 从未重钉（该行引的 `:4009` / `:4507` 亦已是陈旧行号）。所以要改的是那条 fixture 注记，不是某条 8.3 行。

## 6. 仍欠：压缩 / retention tick 覆盖两表

`000061` 于 00:27 落地，而两个 tick 都在其后、都还没跑：

| timer | 下次 |
|---|---|
| `nhms-node27-timeseries-compression.timer` | 2026-09-20 12:25 CST |
| `nhms-node27-timeseries-retention.timer` | 2026-09-20 14:36 CST |

这是纯等待，不是工作量。**绝不手动触发**——手动压缩即制造证据。

## 7. 顺带核准：8.2 的入场门禁写法有缺陷（报告，不在此修 — issue #2515）

`tasks.md:356` 把 8.2 的入场门禁写成"fourteen daily receipts + `legacy_chunks = 0`"。三处与实测不符：

**(a) `legacy_chunks` 的语义是"已过期可删的 legacy chunk 数"，不是"剩余 legacy chunk 数"。** `node27_timeseries_retention.py:1138-1144`：`cutoff = reference_time - window_days`，`eligible = fetch_chunks(config, cutoff)`（`_CHUNK_QUERY` 带 `range_end <= cutoff`），`legacy_chunks` 只对 `eligible` 计数。`= 0` 在"一个都还没到期"和"全部已删光"两种相反状态下都成立；而 `_build` 里 `if legacy_chunks:` 意味着零值时该键根本不出现。作为门禁它无法区分未开始与已完成。

**(b) 河道 I9 已经废掉过这个门禁。** `tasks.md:293` 的 6 节标题原文："replaces fourteen daily receipts + `legacy_chunks = 0`"；6.1 记："amended by #2382: legacy chunks may remain; they are dropped with the table"。`DROP TABLE` 本就连同其 chunk 一起删。forcing 的 8.2 没有跟上这次改写。

**(c) 若仍按字面等 chunk 自然过期，horizon 是 2026-10-19，不是 14 天。** 实测：node-27 活配置 `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS=21`（`proposal.md:12` 写的"retention 维持 14 天"与活配置不符，`storage.py:35` 默认亦为 14——这是第四处文档漂移）。`met.forcing_station_timeseries_legacy` 现存 **6** 个 chunk，目录原文：

```
_hyper_1_87_chunk   2026-08-27 -> 2026-09-03  compressed
_hyper_1_106_chunk  2026-09-03 -> 2026-09-10  compressed
_hyper_1_109_chunk  2026-09-10 -> 2026-09-17  uncompressed
_hyper_1_112_chunk  2026-09-17 -> 2026-09-24  uncompressed
_hyper_1_176_chunk  2026-09-24 -> 2026-09-25  uncompressed
_hyper_1_184_chunk  2026-09-25 -> 2026-09-28  uncompressed
```

跨度不统一（前四块 7 天，`_176` 1 天，`_184` 3 天），所以此处只记目录事实、不反推一个 chunk interval。末块 `range_end = 2026-09-28`，`range_end <= now - 21d` 要到 **2026-10-19** 才成立。

**(d) 这不是措辞问题，是契约阻塞——而且那条需求自相矛盾。** `specs/forcing-narrow-store/spec.md:45` 把同一判据写了第二遍，且这一遍是**迁移自身的 fail-closed 拒绝谓词**，不是门禁：

> The forcing contract migration SHALL refuse while `met.forcing_station_timeseries_legacy` holds any chunk … and the fourteen-day receipt gate applies as for river.

该需求的标题是"The forcing contract SHALL mirror the river contract"，但河道 `000060_river_timeseries_contract.sql:71` 的拒绝谓词是**窗内 legacy 路由 run 数 ≠ 0**，从来不是 chunk 数。所以正文规定的恰是河道没有采用的那条判据，"as for river" 字面为假。后果：只改 `tasks.md` 的门禁而不动这条谓词，迁移照样 fail-close 到 2026-10-19。

**(e) 14 天的副本不止一处。** 除 `proposal.md:12` 外，`design.md:15/:39/:195/:211` 也把 14 天当活事实。`storage.py:35` 与 `infra/env/node27-timeseries-retention.example:27` 不在此列——那是 runner 默认值与模板，6.2 已记为已知分叉，改动会触及运行时行为。

**承接 issue 是 #1993（I14，OPEN），不是 #1991（I12，已 CLOSED）**；#1993 body 的 In Scope 首条带着同一句坏门禁，修订需一并覆盖。

**retention 与 compression 都确实覆盖 `_legacy`**，这一点已核准、不是风险：两者的枚举都走 `node27_timeseries_discovery.py:26` `RUNTIME_HYPERTABLES_SQL`（正名恒含 + 目录中存在的 `_legacy` 兄弟纳入），是目录驱动而非字面表名。两处 canonical-only 常量都不在过滤路径上：`retention.py:169` 的 `TARGET_HYPERTABLES` 只被 `tests/test_node27_timeseries_retention.py:401` 断言；`compression.py:75` 的 `HYPERTABLES` 只被 `:711` `_blank_totals()` 用来给 `per_table_totals` 播种 canonical 零值键，legacy 键在处理到 legacy chunk 时补入——这正是 3.2 把 `_legacy` 键设为可选的原因。

## 8. 本 receipt 不主张的事

- 不主张 M5。窄表落地后未发生任何 legacy 重放，`skipped: 0`，拒绝分支从未在生产上触发过。
- 不主张字节一致，只主张 §1 的结构一致与 §2 的计数相等。
- §3 的耗时是全内存命中下的单次测量，不构成容量结论；`pg_stat_statements` 未安装，未为此重启集群。
- 未观测到任何覆盖两表的压缩 / retention tick（§6）。
