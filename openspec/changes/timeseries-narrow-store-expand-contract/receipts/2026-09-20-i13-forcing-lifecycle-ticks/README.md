# 8.1 第四项 — 压缩与 retention tick 覆盖两表（2026-09-20）

`000061` 于 2026-09-20 00:27 落地。这是**改名之后两个生命周期 tick 各自的第一次自然触发**，两者都在窗口之后排程、都未被人工触发。

**零干预。** 没有手动触发任何 timer，没有 `systemctl start`，没有手工压缩或 `drop_chunks`。两份 receipt 是各自 unit 自行写出的产物，原样存档。DB 只读一次目录（`BEGIN READ ONLY`、ingest_rw 车道、DSN 仅经 env）。

## 1. 压缩 tick — `outcome: clean`，三张表全部入枚举

`nhms-node27-timeseries-compression.service`，**12:25:32 → 13:02:25 CST（36 m 53 s）**，`Result=success`，`ExecMainStatus=0`。存档：`compression-20260920T042532Z.json`。

`per_table_totals` 带 **三个** 键——这是 `000061` 之后混合目录被正确枚举的**在产证据**，此前只有单元测试这么主张过：

```
hydro.river_timeseries               before=46 073 020 416  after=12 299 067 392  chunks_compressed=2
met.forcing_station_timeseries       before=0  after=null   chunks_compressed=0
met.forcing_station_timeseries_legacy before=0 after=null   chunks_compressed=0
```

正名键与 `_legacy` 键同时出现，正是 3.2 把 `_legacy` 设为 `patternProperties` 可选键所预期的形状。河道实压 2 个 chunk，46.07 GB → 12.30 GB（3.75×）。

`skipped` 22 项，按表分解**证明 forcing 两张表的每个 chunk 都被逐一评估过**，而不是整表被跳过：

| 表 | skipped |
|---|---|
| `hydro.river_timeseries` | 10 |
| `met.forcing_station_timeseries`（窄） | 8 |
| `met.forcing_station_timeseries_legacy` | 4 |

forcing 侧全部 12 项的理由都是 `range_end inside lag window`——即 chunk 被看见、被判定、因太新而不压，这是正确行为。legacy 出现 4 而非 6，是因为压缩枚举过滤 `is_compressed = false`（与 retention 的 H3 分歧点一致），而 legacy 6 个 chunk 中 `_hyper_1_87` 与 `_hyper_1_106` 已压缩。`deferred` 8 项全属河道，理由 `per-tick bound reached`（`per_tick_bound: 2`）。

**窄表本身尚无 chunk 被压缩**，因为它最老的 chunk 生于 09-19，全部落在 lag 窗内。本 receipt 不主张窄表的压缩比。

## 2. retention tick — `outcome: enforced`，21 天窗口，只汰了该汰的

`nhms-node27-timeseries-retention.service`，**14:36:32 → 14:36:33 CST（1 s）**，`Result=success`。存档：`retention-20260920T063632Z.json`。

```
mode        : enforce
window_days : 21
cutoff      : 2026-08-29T00:00:00Z
dropped_chunks : 1  —  _timescaledb_internal._hyper_9_151_chunk, freed 4 708 761 600 B (4.71 GB)
deferred_remainder : []
salvage_backed_windows : []
archive_gate : disabled
```

唯一被汰的是河道的一个 chunk。**forcing 两张表一个都没被汰，且这是正确的**：`forcing_station_timeseries_legacy` 最老 chunk 的 `range_end` 是 `2026-09-03`，晚于 cutoff `2026-08-29`；窄表最老 chunk 生于 09-19。

`window_days: 21` 是该窗口值的**在产实测确认**，与 `proposal.md:12` 和 `design.md` 四处所写的 14 天不符（已记于 #2515）。

## 3. tick 之后的 chunk 现状

```
forcing_station_timeseries        |  8 chunks | 0 compressed | 2026-09-19 -> 2026-09-27
forcing_station_timeseries_legacy |  6 chunks | 2 compressed | 2026-08-27 -> 2026-09-28
river_timeseries                  | 29 chunks |10 compressed | 2026-08-29 -> 2026-09-27
```

## 4. 顺带：`legacy_chunks` 语义缺陷的现场印证

retention receipt 里 **`legacy_chunks` 键完全缺席**。

这正是 #2515 第 (a) 条所述：`node27_timeseries_retention.py:1141-1143` 只对 `eligible`（已过 cutoff 者）中名字以 `_legacy` 结尾的 chunk 计数，而 `:1130` 是 `if legacy_chunks:`——零值时键根本不写入。所以把 `legacy_chunks = 0` 当作 8.2 的入场门禁，在今天（6 个 legacy chunk 一个都没到期）与将来（全部汰光）**读数完全相同，且都表现为键缺席**。这不是推演，是本 receipt 的原始 JSON。

## 5. 本 receipt 不主张的事

- 不主张窄表的压缩效果——它还没有任何 chunk 出 lag 窗。
- 不主张 retention 会按期汰掉 legacy 表的 chunk；只记录本次 cutoff 下它们均不合格。按现状最后一个 legacy chunk（`range_end 2026-09-28`）要到 2026-10-19 才满足 `range_end <= now - 21d`。
- 河道 3.75× 的压缩比是单次观测，非容量结论。
- `reference_time` 记为 `2026-09-19T00:00:00Z`，而 tick 实跑于 2026-09-20 06:36Z，比实跑时刻早约 30 h；cutoff 由它减 21 天得出，方向偏保守（少汰而非多汰）。本 receipt 只记录该观测，未判定其是否符合设计意图。
