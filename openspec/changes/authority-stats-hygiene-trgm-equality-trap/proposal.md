# Proposal: authority-stats-hygiene-trgm-equality-trap

## Why

Issue #1468：node-27 生产库的 `core.river_segment` / `core.river_network_version` /
`core.basin_version` 三张身份权威表长期零 planner 统计，且 `core.river_segment`
上挂着 `river_segment_id_trgm_idx`（GIN `gin_trgm_ops`）；两者叠加使 #1341 回填
战役的等值 UPDATE 选中 trigram bitmap 而非主键 btree，单次查找劣化 ~2900x，整条
战役停摆到 `PGOPTIONS='-c enable_bitmapscan=off'` 钉进会话才恢复。

2026-08-21 node-27 只读诊断（受理评论 receipt）把两件事分开钉死：

1. **统计为什么是零**：`pg_stat_database.stats_reset` 为 NULL（从未显式 reset），
   autovacuum 开启且阈值为默认值；但 `core.river_segment_crosswalk`（reltuples
   154,630）、`met.canonical_grid_cell`（296,100）至今 `last_analyze`/
   `last_autoanalyze` 双 NULL 且计数器全零，而 `pg_class.reltuples` 仍保有旧值——
   只有"累计统计被整体清零、pg_class 不受影响"能解释。`docker logs nhms-db`
   第 12 行：`2026-08-07 05:49:17 UTC … database system was not properly shut down;
   automatic recovery in progress`——容器重建（ADR 0002 裸 `docker run`）时走了
   崩溃恢复，**PG15 在崩溃恢复时丢弃全部累计统计且不写 `stats_reset`**。此后
   零 churn 的表 `n_mod_since_analyze` 永远是 0，默认阈值（50 + 10%）永远达不到
   ——三张权威表直到 2026-08-16/08-19 两次手工 ANALYZE 才有统计；crosswalk 与
   canonical_grid_cell 仍是零。
2. **统计新鲜了为什么还错**：08-19 ANALYZE 之后，2,000 次
   `rs.river_segment_id = t.river_segment_id AND rs.river_network_version_id = …`
   等值查找在今天仍选 `river_segment_id_trgm_idx`（cost 0.72 vs pkey 2.28）：
   **51,029 ms vs `enable_bitmapscan=off` 17 ms**（buffers 2.56M vs 9.7k）。机制是
   pg_trgm 1.6（node-27 实装版本）起 `gin_trgm_ops` 支持 `=` 运算符，planner 因而
   把等值查找也纳入 trigram 候选；在共享 ~34 字符前缀的 id 家族
   （`basins_jialingjiang_shud_shud_riv_` 14,673 条、`basins_zhaochen_*` 9k 条）上
   posting list 几乎全表重合，GIN 成本模型严重低估。**新鲜统计不是修法**。

`met.met_station` 的同形态索引 `met_station_id_trgm_idx` 是 partial 索引
（`WHERE active_flag = true`）：不带该谓词的等值查找根本不可选它；带谓词的实测
计划走 `met_station_active_basin_station_idx` Hash Join（22 ms/500 行），未中招。

## What Changes

1. **迁移 `db/migrations/000052_authority_stats_hygiene_trgm_expression_index.sql`**：
   - `river_segment_id_trgm_idx` 由裸列改为表达式索引 `GIN (lower(river_segment_id)
     gin_trgm_ops)`，三步幂等置换：DO 块内条件 `ALTER INDEX … RENAME TO …_legacy`
     （仅当 `schemaname='core'` 的现有 indexdef 不含 `lower(`；同块先清理同名
     invalid 残骸）→ `CREATE INDEX CONCURRENTLY IF NOT EXISTS` → `DROP INDEX
     CONCURRENTLY IF EXISTS …_legacy`。等值查找 `river_segment_id = $1` 在结构上
     不再匹配该索引——不是成本博弈，是不可选。
   - 四张 core 身份表 per-table autovacuum analyze 参数：`river_segment` /
     `river_segment_crosswalk` `(autovacuum_analyze_scale_factor=0.01,
     autovacuum_analyze_threshold=500)`；`river_network_version` / `basin_version`
     `(autovacuum_analyze_scale_factor=0, autovacuum_analyze_threshold=1)`——覆盖
     **churn 型**失效（新增一个 5k 段的 network 不到 209k×10% 阈值，20 行表的单行
     变更不到 50）。
2. **autopipe phase 3.5 stats guard 增加"统计清零修复"腿**
   （`scripts/node27_autopipeline.py`）：每 tick **不论是否 ingest** 都查一次
   `core`/`met`/`hydro` schema 下 **非 hypertable 普通表** 中 `relpages > 0 AND
   last_analyze IS NULL AND last_autoanalyze IS NULL` 者，逐表 ANALYZE（复用既有
   上限 3 / 120 s timeout / 回读自检 / 两级失败隔离），写入 summary
   `stats_guard.authority`。覆盖 **清零型**失效（崩溃恢复后计数器归零，任何阈值
   都不再触发）。hypertable 与 `_timescaledb_internal` 明确排除（#1378 D3：裸
   chunk 名 ANALYZE 会清零压缩 chunk 的 origin 统计）。
3. **search 查询对齐表达式索引**：`packages/common/model_registry.py` 列表路径的
   `rs.river_segment_id ILIKE %s` 改为 `lower(rs.river_segment_id) LIKE %s`
   （pattern 在 Python 侧 `lower()`；ASCII slug id 上语义等价于 ILIKE），
   name/segment_name 两臂不变；slice 路径本就在内存里 `.lower()` 比较，不改。
4. **记录**：ADR 0004（标识列上的 trigram GIN 等值陷阱与表达式索引约定）、
   `docs/runbooks/tier-node27-timeseries-storage.md` §4.8（清零机制、修复腿、
   复核 SQL、已施加缓解的作用域：08-16/08-19 一次性 ANALYZE；
   `enable_bitmapscan=off` 仅存在于 node-27 的 `run-campaign-v3.sh` 会话级，
   随 #1341 战役结束消亡，迁移后不再需要）。
5. **#1378 数据点**：在 #1378 留一条评论链接 08-19 ANALYZE 之后的计划（其关闭
   依据 E4(iii) Q2 = 2.296 ms，SkipScan → selected_identity 键索引，采样于
   08-20，即 ANALYZE 之后）。

## Non-Goals

- 不改 `met.met_station` 的两条 partial trgm 索引（实测无陷阱；partial 谓词已结构性
  保护绝大多数等值查找；记录结论与复核 SQL）。
- 不删 `river_segment_name_trgm_idx` / `river_segment_segment_name_trgm_idx`
  （idx_scan=0，但与 search 的 OR 三臂绑定；去留另议）。
- 不动簇级 planner 旋钮；不在回填脚本内固化 `enable_bitmapscan` 开关
  （结构性修法使其不再需要）。
- 不处理 #1123 重复 seed 行（仅作放大器记录）。
- 不改 `run-campaign-v3.sh`（node-27 本地、不在仓库内、战役已结束）。
