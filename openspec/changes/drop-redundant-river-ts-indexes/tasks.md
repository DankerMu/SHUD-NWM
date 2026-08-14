# Tasks: drop-redundant-river-ts-indexes（#1338）

## 0. 前置实测（已完成）

- [x] 0.1 node-27 read-only 六索引全量清单（尺寸/idx_scan/8 chunk 聚合）+ `valid_time_discovery_idx` 带外身份确认 + schema_migrations 漂移记录 → `.workplans/issue-1338/pre-inventory.md`

## 1. 实现

- [x] 1.1 `db/migrations/000049_drop_redundant_river_mvt_identity_and_valid_time_discovery_idx.sql`：两条 `DROP INDEX CONCURRENTLY IF EXISTS`（D1 名字逐字），先例 000041/000042 文体 rationale 注释，含带外索引自证注释
- [x] 1.2 `packages/common/forecast_store.py` `_qhh_latest_query_indexes()` 证据陈述对齐（D3：`covered_by_mvt_identity_lookup_index` → pkey 承接如实陈述），`tests/test_migrations.py:370` / `tests/test_forecast_api.py:1735` 两钉同步；**最终承接者措辞以 3.4 第 6 查询（`river_sample_rows`）post-drop EXPLAIN 实测为准，merge 前定稿**
- [x] 1.3 `docs/adr/0002-node27-timeseries-hot-cold-tiering.md` 追加 supersession 注记（2026-08-14 实测 162 GB / 5,571 scans，被 #1338 依新证据 supersede；不改原正文段落）

## 2. 测试

- [x] 2.1 B1：000049 文本钉（存在、恰两条 DROP CONCURRENTLY IF EXISTS、目标名逐字、零保留集名、编号无冲突）；红证=文件缺失/名字打错时红
- [x] 2.2 B2：证据对齐钉（forecast_store 不再陈述 mvt_identity_lookup；两既有钉改后仍断言实质内容）；红证=先跑旧断言对新 forecast_store 证红
- [x] 2.3 B3：保留集既有迁移测试（含 `test_selected_run_valid_time_discovery_migration_matches_strict_identity_predicates`）不动仍绿

## 3. Evidence Floor

- [x] 3.1 `uv run pytest -q tests/test_migrations.py tests/test_forecast_api.py`
- [x] 3.2 `uv run ruff check .`
- [x] 3.3 `openspec validate drop-redundant-river-ts-indexes --strict --no-interactive`
- [ ] 3.4 node-27 实机（D2 手工 psql 路径，merge 后），顺序硬约束：
  1. pre-flight（F2/F4）：两索引 `pg_get_indexdef`（hypertable 级 + 至少一个 chunk 级）落盘 `.workplans/issue-1338/`；`timescaledb_information.chunks` 的 `is_compressed` 分布记录
  2. pre-drop EXPLAIN（D4 **八**查询）+ 尺寸基线——**hypertable 上必须用 chunk 聚合/`hypertable_index_size()`，父表 `pg_total_relation_size` 恒近零会废掉 AC2**；`_hyper_3_32_chunk` 单列
  3. apply 000049：`screen`/`nohup` 内执行（F4：`DROP INDEX CONCURRENTLY` 中断可留 invalid 索引）→ 收尾 `SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid OR NOT indisready` 必须为空，非空则普通 `DROP INDEX` 清理残留后重跑
  4. 二次重放证幂等（`IF EXISTS` → no-op）
  5. post-drop EXPLAIN + 尺寸对比 → D4 判定；记录进 `.workplans/issue-1338/` 与 PR 评论；**退化即按 000049 注释中的 CREATE 原文重建回滚并终止**
- [ ] 3.5 `/` 与 `/ops` node-27 live 浏览器验证正常（AC4），受理证据入 PR 评论
- [ ] 3.6 CI "SQL Migration Dry Run"（`db/**` 命中 database filter，from-zero 全链重放含 000049）绿——即 spec "replay on a database where the index never existed" scenario 的免费证据，PR body 点名
