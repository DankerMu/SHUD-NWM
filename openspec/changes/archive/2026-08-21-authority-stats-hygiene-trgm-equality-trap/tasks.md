# Tasks: authority-stats-hygiene-trgm-equality-trap (#1468)

## 1. Implementation

- [x] 1.1 `db/migrations/000052_authority_stats_hygiene_trgm_expression_index.sql`：
      幂等索引置换（先 `DROP INDEX CONCURRENTLY IF EXISTS …_invalid` 清上上次
      残骸；DO 块带 `SET LOCAL lock_timeout = '2s'`：同名 `indisvalid = false` 残骸
      RENAME 为 `_invalid`，`schemaname='core'` 的裸列索引 RENAME 为 `_legacy` →
      `CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx … GIN
      (lower(river_segment_id) gin_trgm_ops)` → `DROP INDEX CONCURRENTLY IF EXISTS
      …_legacy` 与 `…_invalid`）+ 四张 core 身份表 reloptions（design D3）。全文无
      BEGIN/COMMIT，无任何对表取 ACCESS EXCLUSIVE 的语句。
      文件头注释写明陷阱机制、#1468、CIC 中断后 invalid 索引的恢复步骤（重跑即可）。
- [x] 1.2 `scripts/node27_autopipeline.py`：stats guard 修复腿
      `_analyze_unanalyzed_authority_tables`（候选 SQL 限 `core`/`met`/`hydro`、
      `relkind='r'`、排除 hypertable；`relpages>0 AND last_analyze IS NULL AND
      last_autoanalyze IS NULL`；复用上限 3 / 120 s / `_STATS_GUARD_IDENT_RE` /
      回读自检 / 两级隔离），不受 `ingested_runs` 门控，summary 新增
      `stats_guard.authority = {status, analyzed:[…], deferred:[…]}`；
      `NODE27_AUTOPIPE_STATS_GUARD=off` 一并跳过；`--progress` 行追加 authority 段
      （与前沿段同形：腿级 status + ok / warning / failed / deferred 计数），
      且打印条件放宽为"任一腿有非空 analyzed/deferred 或任一腿 failed"（现条件在
      not_triggered 时整行抑制，会吞掉修复腿的可观测性）。
- [x] 1.3 `packages/common/model_registry.py` 列表路径 id 臂改
      `lower(rs.river_segment_id) LIKE %s ESCAPE '\\'`，参数 `like_pattern.lower()`
      （design D4）；name/segment_name 臂与 slice 路径不动。
- [x] 1.4 `docs/adr/0004-identifier-trgm-gin-equality-trap.md`（机制、实测、约定：
      标识列的 trigram GIN 一律建在 `lower(col)` 表达式上并由查询显式使用该表达式）
      + `docs/runbooks/tier-node27-timeseries-storage.md` §4.8（清零机制与实证、
      修复腿、复核 SQL、两项既有缓解的作用域）。

## 2. Tests（requirement-driven）

- [x] 2.1 `tests/test_node27_autopipeline_handoff.py`：扩展 `_FakeCursor.execute` 分派
      （authority 候选 SQL 分支须先于通用 `pg_stat_user_tables` 回读分支）；
      `_prepare_autopipe` 增加修复腿 stub（否则 16 个 tick 用例拨真 socket）；
      `test_stats_guard_without_ingest_never_touches_the_database` 改名为
      `…_frontier_leg_never_touches_the_database` 并断言修复腿仍被调用。修复腿五场景
      ——候选 SQL 文本钉（三条件 + relkind 'r' + hypertable NOT EXISTS + schema 元组）/
      无 ingest 也执行 / 上限 deferred / 单表失败隔离 / 开关 off 两腿皆 skipped；
      summary `authority` 结构钉。既有用例全部保持绿。
- [x] 2.2 `tests/test_list_search_contract.py`：id 臂 `lower(... ) LIKE` 钉 + 参数
      小写化钉 + name 臂仍 `ILIKE` 钉 + COUNT/分页语句同步钉。
- [x] 2.3 `tests/test_real_database_integration.py`：`indexdef` 含
      `lower(river_segment_id)` 且 `pg_index.indisvalid = true`；共享长前缀样本上
      等值 join EXPLAIN 不含 `river_segment_id_trgm_idx`（默认计划 + 仅剩 bitmap
      路径两种设置下均不含）；正向可选性用 catalog 断言而非计划断言——索引表达式为
      `lower(river_segment_id)`、opclass `gin_trgm_ops`，且该 opfamily 的 `pg_amop`
      含 `~~(text,text)`（LIKE）；planner 实际选用的证据归 E4(iii) 真数据 receipt
      （小样本上 `enable_seqscan=off` 仍会被"无条件 bitmap 全索引扫描"抢走，E3
      实跑已证明计划断言不可靠）；四表 reloptions 回读；幂等——`DELETE FROM
      public.schema_migrations WHERE version LIKE '000052%'` 后
      `apply_migration()` 重放，indexdef / `indisvalid` / reloptions / `_legacy`
      缺席均不变；invalid 残骸分支——用 case-dup 行让同名 `CREATE UNIQUE INDEX
      CONCURRENTLY` 失败制造 `indisvalid=false` 残骸后删账本行重放，终态为单个 valid
      表达式索引、无 `_legacy`/`_invalid`；修复腿候选 SQL 行为 oracle——core/met 普通表
      先 ANALYZE 再 `pg_stat_reset_single_table_counters`、hypertable chunk 同样处理，
      调 `_analyze_unanalyzed_authority_tables(dsn)` 断言入选/排除与 ANALYZE 落地；
      hypertable 根表须先被做成候选形态（根表 heap 无行、ANALYZE 不写 relpages，throwaway
      库内以 superuser 把其 `pg_class.relpages` 置 1 并断言），`NOT EXISTS` 子句才承重。

## Evidence Floor

- [x] E1 本地：`uv run ruff check .`；`uv run pytest -q tests/test_node27_autopipeline_handoff.py
      tests/test_list_search_contract.py`；`openspec validate
      authority-stats-hygiene-trgm-equality-trap --strict --no-interactive`。
- [x] E2 CI：`SQL Migration Dry Run`（真 PG；000052 经 `apply_migrations_from_zero`
      施加一遍 + 2.3 的删账本行重放幂等断言 + 其余集成断言）绿。
- [x] E3 node-27 真库 pytest：`tests/test_real_database_integration.py` 相关用例绿——
      目标为 superuser DSN 建/删的 throwaway DB（项目既定做法），绝不指向生产库。
- [x] E4 node-27 live receipt（只读诊断 + 迁移施加 + 看护一次 tick）——PR #1666 E4 评论，
      (iv) 的定时 tick 级 `stats_guard.authority` 为 post-merge 核对项：
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
      (v) `met.met_station` 复核 SQL（带 `active_flag` 的等值计划）记录结论——实测
      中招（174 ms vs 22 ms，随统计翻转），路由 #1669。
- [x] E5 #1378 评论：链接 08-19 ANALYZE 后的 Q2 数据点（issuecomment-5366736732）；PR body 引用。
