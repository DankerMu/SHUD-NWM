# Proposal: drop-redundant-river-ts-indexes（#1338，epic #1336 M1）

## Why

`hydro.river_timeseries` 上两条索引的存储成本（另有索引维护写开销，未实测）与其实测使用面严重失衡（2026-08-14 node-27 实测，8 chunk 聚合）——删除是**实测 TRADEOFF**，不是零使用冗余清理：

- `river_timeseries_mvt_identity_lookup_idx`（000019 创建；列 `(run_id, variable, valid_time, river_network_version_id, river_segment_id)`）——**162 GB / idx_scan 5,571**（同期 pkey 被扫 **7.96 亿**次）。列**集合**与 pkey `(run_id, river_network_version_id, river_segment_id, variable, valid_time)` 相同但**覆盖不同**：`variable` 全表单值使其实际表现为 `(run_id, valid_time, rnv, segid)` 的 run 级时间前缀——pkey 给不出这个形状（rnv/segid 排在 variable/valid_time 之前）。实测会走该索引的在册查询形状是两条读面（predrop-baseline Q1/Q8 形状捕获：national tile typed_values 的 ts 访问腿与 source-identity stats probe，均绑 run_id+variable+valid_time+rnv、**无** basin_version_id，保留的 selected_identity 索引第 2 列即 basin_version_id 故无法承接）；5,571 是累计计数器，**未逐一归因**到具体查询（与下条索引同一口径）。post-drop 承接者已由 3.4 post-drop EXPLAIN 门实测证实 = **pkey Index Only Scan**（可用前缀 run_id+rnv，余谓词 in-index filter；Q1 形状热缓存 10.4ms vs pre 2.5ms，4.2x 残余代价即披露的 tradeoff，非数量级劣化；receipt 见 PR #1377 评论 2026-08-14）。删除依据是 162 GB 成本 vs 该两读面的权衡，不是"planner 不用它"。
- `river_timeseries_valid_time_discovery_idx`（列 `(run_id, variable, valid_time DESC)`，**仓内无迁移出处——node-27 带外创建**）——4663 MB / idx_scan 10,864；列是上者的严格前缀（方向差异 btree 反向扫描等价），实测（predrop-baseline Q2/Q3）其两条 discovery 面本就分别由保留的 `mvt_selected_identity_valid_time_discovery_idx` 与 `valid_time_idx` 服务（被删索引不在这两个计划中）——预期删除不改变计划，由 3.4 post-drop EXPLAIN 复核确认。其 10,864 次 idx_scan 的具体消费查询未逐一归因，post-drop 门以八查询全集覆盖回归面。

issue 开票时的 95 GB / 999 scans 是**单 chunk 3_32 口径**，本次 162 GB 是 **8-chunk 聚合口径**，两者不可直接相减（该 chunk 现已压缩、总大小 56 kB——predrop-baseline）；可比的表级增长证据见 ADR 0002：2026-07 表级 32 GB → 2026-08-14 162 GB。合计预计回收 **~167 GB**（活库 `/home` 1.7 TB 卷，DB 已 389 GB+增长）。删除可逆（重建索引即恢复）。

## What Changes

1. **迁移 `000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql`**：两条 `DROP INDEX CONCURRENTLY IF EXISTS`，逐条 rationale 注释（严格沿用 000041/000042 先例文体；`IF EXISTS` 同时承接 hermetic/CI 库中带外索引根本不存在的现实）。
2. **仓内证据对齐**：`packages/common/forecast_store.py` `_qhh_latest_query_indexes()` 自述 qhh-latest river 查询 `covered_by_mvt_identity_lookup_index`——删索引后该证据陈述变假，同步改为实测承接者陈述——保留的 `mvt_selected_identity_valid_time_discovery_idx`（predrop-baseline Q6 实测；pkey 无 `basin_version_id` 非承接者），连带 `tests/test_migrations.py` / `tests/test_forecast_api.py` 两处钉与反向钉。这是**证据元数据**修正，不是查询代码适配（M2 边界见 Non-Goals；对 issue "PR Boundary: db/migrations/" 的显式记录偏离，理由：不改则仓库自述与自己刚删的索引矛盾——#1255 刚消灭过的"仓库断言 bug 还在"缺陷类）。
3. **ADR 0002 amendment 注记**：ADR（Date: 2026-07-03）Context 的 2026-07-04 实测段写有 MVT identity lookup "functional … cannot be pruned further"（当时 32 GB；`docs/adr/0002:33-34`（amendment 头部插行后的现行行号），前瞻断言，非任何 Decision 依赖项）——按 ADR 既有 amendment 体例（`Policy amendment: 2026-07-21` 等先例）追加注记：2026-08-14 实测 162 GB / 5,571 scans vs pkey 7.96 亿，该 Context 断言被 #1338 依新证据 supersede；**不改 Status、不改正文原文**。

## Non-Goals

- **M2 schema/代码适配**（epic #1336）：不动任何查询代码路径（`apps/`、`services/`、`workers/` 零触碰）。
- 不动 `pkey`、`river_ts_segment_time_idx`（4.3M scans）、`valid_time_idx`、`mvt_selected_identity_valid_*`（issue 明文保留集）。
- `apps/api/routes/hydro_display.py` `_require_hydro_mvt_source_identity` 的列组合恰匹配 000042 **已删**索引——邻接既有事实，本 change 仅纳入 EXPLAIN 取证面，不改代码。
- node-27 `schema_migrations` 止于 000045、000046-48 生效未记账的部署漂移——**范围外报告**（沿 000047 手工 psql 先例应用 000049，不在本 change 内修漂移）。

## 待实测项（live receipt 期）

node-27：删除前后对 design D4 查询集（八查询，脚本原文保全于 `.workplans/issue-1338/predrop-queries.sql`，前后逐字节同一脚本）跑 `EXPLAIN (ANALYZE, BUFFERS)`（无 Seq Scan 回退、无显著慢化）；chunk 聚合尺寸前后对比（含 `_hyper_3_32_chunk` 单列）；`/` 与 `/ops` live 正常。
