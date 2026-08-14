# Proposal: drop-redundant-river-ts-indexes（#1338，epic #1336 M1）

## Why

`hydro.river_timeseries` 上两条索引对查询几乎零贡献、对存储与写放大是纯成本（2026-08-14 node-27 实测，8 chunk 聚合）：

- `river_timeseries_mvt_identity_lookup_idx`（000019 创建；列 `(run_id, variable, valid_time, river_network_version_id, river_segment_id)`）——**162 GB / idx_scan 5,571**。其列**集合**与 pkey `(run_id, river_network_version_id, river_segment_id, variable, valid_time)` 完全相同、仅顺序不同，同期 pkey 被扫 **7.96 亿**次。`variable` 全表单值零选择性，解释了 planner 几乎不选它。
- `river_timeseries_valid_time_discovery_idx`（列 `(run_id, variable, valid_time DESC)`，**仓内无迁移出处——node-27 带外创建**）——4663 MB / idx_scan 10,864；列是上者的严格前缀（方向差异 btree 反向扫描等价），上者删除后其残余用途由 pkey 前缀 + 单值 `variable` 过滤承接（EXPLAIN 取证为准）。

issue 开票时（单 chunk 3_32：95 GB / 999 scans）到今日已膨胀至全表 162 GB——前提只增不减。合计预计回收 **~167 GB**（活库 `/home` 1.7 TB 卷，DB 已 389 GB+增长）。删除可逆（重建索引即恢复）。

## What Changes

1. **迁移 `000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql`**：两条 `DROP INDEX CONCURRENTLY IF EXISTS`，逐条 rationale 注释（严格沿用 000041/000042 先例文体；`IF EXISTS` 同时承接 hermetic/CI 库中带外索引根本不存在的现实）。
2. **仓内证据对齐**：`packages/common/forecast_store.py` `_qhh_latest_query_indexes()` 自述 qhh-latest river 查询 `covered_by_mvt_identity_lookup_index`——删索引后该证据陈述变假，同步改为 pkey 承接陈述（连带 `tests/test_migrations.py` / `tests/test_forecast_api.py` 两处钉）。这是**证据元数据**修正，不是查询代码适配（M2 边界见 Non-Goals；对 issue "PR Boundary: db/migrations/" 的显式记录偏离，理由：不改则仓库自述与自己刚删的索引矛盾——#1255 刚消灭过的"仓库断言 bug 还在"缺陷类）。
3. **ADR 0002 amendment 注记**：ADR（Date: 2026-07-03）Context 的 2026-07-04 实测段写有 MVT identity lookup "functional … cannot be pruned further"（当时 32 GB；`docs/adr/0002:29-30`，前瞻断言，非任何 Decision 依赖项）——按 ADR 既有 amendment 体例（`Policy amendment: 2026-07-21` 等先例）追加注记：2026-08-14 实测 162 GB / 5,571 scans vs pkey 7.96 亿，该 Context 断言被 #1338 依新证据 supersede；**不改 Status、不改正文原文**。

## Non-Goals

- **M2 schema/代码适配**（epic #1336）：不动任何查询代码路径（`apps/`、`services/`、`workers/` 零触碰）。
- 不动 `pkey`、`river_ts_segment_time_idx`（4.3M scans）、`valid_time_idx`、`mvt_selected_identity_valid_*`（issue 明文保留集）。
- `apps/api/routes/hydro_display.py` `_require_hydro_mvt_source_identity` 的列组合恰匹配 000042 **已删**索引——邻接既有事实，本 change 仅纳入 EXPLAIN 取证面，不改代码。
- node-27 `schema_migrations` 止于 000045、000046-48 生效未记账的部署漂移——**范围外报告**（沿 000047 手工 psql 先例应用 000049，不在本 change 内修漂移）。

## 待实测项（live receipt 期）

node-27：删除前后对 design D4 查询集跑 `EXPLAIN (ANALYZE, BUFFERS)`（无 Seq Scan 回退、无显著慢化）；`pg_total_relation_size` 前后对比（含 `_hyper_3_32_chunk` 单列）；`/` 与 `/ops` live 正常。
