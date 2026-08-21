# Tasks: authority-stats-hygiene-trgm-equality-trap (#1468)

## 1. Implementation

- [ ] 1.1 `db/migrations/000052_authority_stats_hygiene_trgm_expression_index.sql`：
      三步幂等索引置换（DO 块带 `SET LOCAL lock_timeout = '2s'`：先清理同名
      `indisvalid = false` 残骸，再条件 RENAME `schemaname='core'` 的裸列索引为
      `_legacy` → `CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx …
      GIN (lower(river_segment_id) gin_trgm_ops)` → `DROP INDEX CONCURRENTLY IF EXISTS
      …_legacy`）+ 四张 core 身份表 reloptions（design D3）。全文无 BEGIN/COMMIT。
      文件头注释写明陷阱机制、#1468、CIC 中断后 invalid 索引的恢复步骤（重跑即可）。
- [ ] 1.2 `scripts/node27_autopipeline.py`：stats guard 修复腿
      `_analyze_unanalyzed_authority_tables`（候选 SQL 限 `core`/`met`/`hydro`、
      `relkind='r'`、排除 hypertable；`relpages>0 AND last_analyze IS NULL AND
      last_autoanalyze IS NULL`；复用上限 3 / 120 s / `_STATS_GUARD_IDENT_RE` /
      回读自检 / 两级隔离），不受 `ingested_runs` 门控，summary 新增
      `stats_guard.authority = {status, analyzed:[…], deferred:[…]}`；
      `NODE27_AUTOPIPE_STATS_GUARD=off` 一并跳过；`--progress` 行追加 authority 计数，
      且打印条件放宽为"任一腿有非空 analyzed/deferred 或任一腿 failed"（现条件在
      not_triggered 时整行抑制，会吞掉修复腿的可观测性）。
- [ ] 1.3 `packages/common/model_registry.py` 列表路径 id 臂改
      `lower(rs.river_segment_id) LIKE %s ESCAPE '\\'`，参数 `like_pattern.lower()`
      （design D4）；name/segment_name 臂与 slice 路径不动。
- [ ] 1.4 `docs/adr/0004-identifier-trgm-gin-equality-trap.md`（机制、实测、约定：
      标识列的 trigram GIN 一律建在 `lower(col)` 表达式上并由查询显式使用该表达式）
      + `docs/runbooks/tier-node27-timeseries-storage.md` §4.8（清零机制与实证、
      修复腿、复核 SQL、两项既有缓解的作用域）。

## 2. Tests（requirement-driven）

- [ ] 2.1 `tests/test_node27_autopipeline_handoff.py`：扩展 `_FakeCursor.execute` 分派
      （authority 候选 SQL 分支须先于通用 `pg_stat_user_tables` 回读分支）；
      `_prepare_autopipe` 增加修复腿 stub（否则 16 个 tick 用例拨真 socket）；
      `test_stats_guard_without_ingest_never_touches_the_database` 改名为
      `…_frontier_leg_never_touches_the_database` 并断言修复腿仍被调用。修复腿五场景
      ——候选 SQL 文本钉（三条件 + relkind 'r' + hypertable NOT EXISTS + schema 元组）/
      无 ingest 也执行 / 上限 deferred / 单表失败隔离 / 开关 off 两腿皆 skipped；
      summary `authority` 结构钉。既有用例全部保持绿。
- [ ] 2.2 `tests/test_list_search_contract.py`：id 臂 `lower(... ) LIKE` 钉 + 参数
      小写化钉 + name 臂仍 `ILIKE` 钉 + COUNT/分页语句同步钉。
- [ ] 2.3 `tests/test_real_database_integration.py`：`indexdef` 含
      `lower(river_segment_id)` 且 `pg_index.indisvalid = true`；共享长前缀样本上
      等值 join EXPLAIN 不含 `river_segment_id_trgm_idx`；`enable_seqscan=off` 下
      lower/LIKE 计划含之；四表 reloptions 回读；幂等——`DELETE FROM
      public.schema_migrations WHERE version LIKE '000052%'` 后
      `apply_migration()` 重放，indexdef / `indisvalid` / reloptions / `_legacy`
      缺席均不变。

## Evidence Floor

- [ ] E1 本地：`uv run ruff check .`；`uv run pytest -q tests/test_node27_autopipeline_handoff.py
      tests/test_list_search_contract.py`；`openspec validate
      authority-stats-hygiene-trgm-equality-trap --strict --no-interactive`。
- [ ] E2 CI：`SQL Migration Dry Run`（真 PG；000052 经 `apply_migrations_from_zero`
      施加一遍 + 2.3 的删账本行重放幂等断言 + 其余集成断言）绿。
- [ ] E3 node-27 真库 pytest：`tests/test_real_database_integration.py` 相关用例绿——
      目标为 superuser DSN 建/删的 throwaway DB（项目既定做法），绝不指向生产库。
- [ ] E4 node-27 live receipt（只读诊断 + 迁移施加 + 看护一次 tick）：
      (i) 迁移 `psql -v ON_ERROR_STOP=1 -f` 施加两遍均 exit 0，`pg_indexes` 显示
      表达式定义且 `pg_index.indisvalid = true`、`_legacy` 不存在、reloptions 在位；
      (ii) 等值 join（诊断同形态，2,000 查找）**无任何 session pin** 的
      `EXPLAIN (ANALYZE, BUFFERS)`：计划走 `river_segment_pkey`、不含 trgm、
      wall 与 buffers 同量级于 08-21 `enable_bitmapscan=off` 基线（17 ms / 9.7k）；
      (iii) search 形态（OR 三臂，basin 作用域）计划仍含 `river_segment_id_trgm_idx`
      且命中计数与迁移前相同；
      (iv) 下一次 autopipe tick summary `stats_guard.authority.analyzed` 含
      `core.river_segment_crosswalk` 与 `met.canonical_grid_cell`（status ok、
      `last_analyze` 非空），随后复核 SQL（双 NULL 候选）返回 0 行；
      (v) `met.met_station` 复核 SQL（带 `active_flag` 的等值计划）记录结论。
- [ ] E5 #1378 评论：链接 08-19 ANALYZE 后的 Q2 数据点；PR body 引用。
