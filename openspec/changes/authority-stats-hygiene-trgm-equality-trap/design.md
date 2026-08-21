# Design: authority-stats-hygiene-trgm-equality-trap

## 风险三元组

- **Fixture level**：expanded——生产 DDL（并发重建索引 + reloptions）、ingest
  定时器行为变更、search 查询文本变更、planner 行为断言；必须有 node-27 live receipt。
- **Must-preserve**：(a) search 语义——`GET /api/v1/models/{id}/river-segments?search=`
  的命中集合逐条不变（ILIKE → lower/LIKE 在 ASCII id 上等价；name/segment_name
  两臂原样）；(b) 索引名 `river_segment_id_trgm_idx` 不变（集成测试索引清单钉、
  000050 预算注释）；(c) autopipe tick rc 语义——stats guard 任一腿失败不改 rc
  （#1378 spec）；(d) 压缩 chunk 统计——修复腿绝不触碰 hypertable/chunk；
  (e) 迁移幂等：删除 `schema_migrations` 中 000052 行后由
  `packages/common/migrate.apply_migration()` 重放一遍，indexdef / `indisvalid` /
  reloptions / `_legacy` 缺席均不变（CI 的 `apply_migrations_from_zero` 是账本门控，
  第二次调用零重放——不能当两遍证据）。
- **Seams under test**（上游声明，本 change 消费）：`_STATS_GUARD_*` SQL 文本钉 +
  summary 结构；`_list_river_segments` 生成语句文本钉（`tests/test_list_search_contract.py`）；
  真库集成测试（`tests/test_real_database_integration.py`，CI "SQL Migration Dry
  Run" + node-27）对索引定义与 planner 可选性断言。
- **Risk packs**：selected = correctness-sql（planner 可选性、LIKE/ILIKE 等价、
  ESCAPE 保留）、migration-safety（CONCURRENTLY 非事务、幂等可重跑、CIC 失败残留 invalid
  索引的处置、CIC 秒级窗口内改写后的 lower/LIKE 走 Filter 可接受）、ops-runtime（tick 预算：修复腿 ≤3 表 × 120 s；常态一条
  catalog 查询）、security-identifier-interpolation（ANALYZE 不可绑参，复用
  `_STATS_GUARD_IDENT_RE` 白名单）。not selected = frontend（无 UI 变更）、
  slurm/compute（无 node-22 面）、schema-json（summary 无 JSON Schema 约束，
  grep 证实 `schemas/` 无 `stats_guard`）。

## D1: 两种失效、两个机制，缺一不可

| 失效 | 触发 | 任何 autovacuum 阈值能否救 | 修法 |
|---|---|---|---|
| 清零型 | 崩溃恢复丢弃累计统计（2026-08-07T05:49:17Z 实证），计数器归零 | 否——`n_mod_since_analyze=0` 永不过槛 | guard 修复腿：`relpages>0 AND last_analyze IS NULL AND last_autoanalyze IS NULL` → ANALYZE |
| churn 型 | 新增 network（~5k 行 < 209k×10%）、20 行表单行变更（< 50） | 能，但需 per-table 参数 | 000052 reloptions |

issue 建议的"per-table scale factor"只覆盖第二行；只做它，下一次容器崩溃恢复后
权威表照样零统计。修复腿放在 autopipe guard 而非新 timer：复用连接/ANALYZE/回读/
隔离全套机制（#1378 已在产），且 ingest DSN 的角色实测为 `nhms`（superuser，且是 `core.*`/`met.*`/`hydro.*` 表 owner——
2026-08-21 只读诊断 `select current_user, rolsuper` + `relowner::regrole`）；PG15 非 owner
ANALYZE 静默跳过的情形由回读自检兜底记 `warning`。修复腿**不**受 `ingested_runs` 门控：它的
触发条件是统计缺席而非前沿移动，catalog 查询成本可忽略；`NODE27_AUTOPIPE_STATS_GUARD=off`
仍一并关闭两腿。候选集限定 `core`/`met`/`hydro` 中 `relkind='r'` 且不在
`timescaledb_information.hypertables` 的表——hypertable 根表 ANALYZE 在 TSDB 2.10
下会递归到 chunk，压缩 chunk 的 origin 统计会被清零（#1378 D3 实证），必须排除。

## D2: 等值陷阱的结构性修法，而不是成本博弈

候选：(a) 删 `river_segment_id_trgm_idx`——search 在 basin 作用域内退化为
bitmap(network) + Filter，实测 9 → 20 ms（只读诊断第二轮），但 OR 三臂中一臂失去
索引后整个 BitmapOr 失效，两条 name 索引随之变成死重；(b) 表达式索引
`lower(river_segment_id)` + search 改 `lower(col) LIKE lower(pattern)`——等值查找
在结构上不匹配索引表达式，与统计/成本无关；search 计划不变；(c) 把
`enable_bitmapscan=off` 固化进回填脚本——只救一个消费者，掩盖根因。选 (b)。
`lower()` 而非 `col || ''`：PG 大小写不敏感表达式索引的标准写法，读者不需要
背景知识；ILIKE 在 trgm GIN 上本就按小写三元组匹配，语义一致。

`met.met_station`：同机制（pg_trgm 1.6 `=` 支持）；partial 谓词
`active_flag = true` 只保护不带该谓词的等值查找。带谓词的等值 join 诊断时走 partial
btree Hash Join（22 ms/500），E4 receipt 时（统计刷新后）翻为 `met_station_id_trgm_idx`
Bitmap（174 ms/500，~8×）——陷阱真实且随统计翻转。本 change 不扩 scope，结论入账，
对齐（`lower(station_id)` partial 表达式索引 + `forecast_store` search 臂）路由 #1669。

## D3: 迁移形态与 CI/node-27 施加

- 幂等置换（清残骸 → DO 块改名 → CIC → 并发删旧/残骸 → reloptions），全部在事务外（`CREATE/DROP INDEX CONCURRENTLY` 不能在事务内；
  `packages/common/migrate.apply_migration()` 在 `autocommit=True` 连接上经
  `split_sql_statements` 逐语句执行，该切分器维护 `$$` 状态，DO 块不会被 `;`
  切碎；000031 是 CONCURRENTLY 的在产先例，DO 块在 000003/000011/…/000050 共 11 个迁移
  已有先例——000052 的新形态是 DO 块与两条 CONCURRENTLY 混排且全文无事务）：
  1. `DO $$ … $$`（`SET LOCAL lock_timeout = '2s'`，000050 先例）：若
     `pg_index.indisvalid = false` 的同名索引存在（上一次 CIC 中断的残骸）则
     `ALTER INDEX … RENAME TO river_segment_id_trgm_idx_invalid`（SHARE UPDATE
     EXCLUSIVE；**不能**用非并发 `DROP INDEX`——它对**表**取 ACCESS EXCLUSIVE，会在
     MVT 读流量下排队阻塞读者再被 lock_timeout 打断，round-1 审查 C1）；若 `pg_indexes` 中
     `schemaname='core' AND indexname='river_segment_id_trgm_idx' AND indexdef NOT
     LIKE '%lower(%'` 则 `ALTER INDEX core.river_segment_id_trgm_idx RENAME TO
     river_segment_id_trgm_idx_legacy`（SHARE UPDATE EXCLUSIVE，不阻塞读者）。
  2. `CREATE INDEX CONCURRENTLY IF NOT EXISTS river_segment_id_trgm_idx ON
     core.river_segment USING GIN (lower(river_segment_id) gin_trgm_ops);`
  3. `DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_legacy;`
     与 `DROP INDEX CONCURRENTLY IF EXISTS core.river_segment_id_trgm_idx_invalid;`
     （并发删除对 invalid 普通索引合法；全文件唯一可能取表级 AEL 的语句因此不存在）。
  4. 四张表 `ALTER TABLE … SET (autovacuum_*)`（幂等）。
- RENAME 步骤的收益是**幂等判别**（第二遍能区分"裸列旧索引"与"已置换的表达式
  索引"，不会误删刚建好的索引），不是可用性：`_legacy` 是裸列 GIN，服务不了改写后
  的 `lower(river_segment_id) LIKE`，CIC 的秒级窗口（209k 行、11 MB）内该查询走
  bitmap(network) + Filter（只读诊断实测 20 ms 量级），可接受。
- 中断态全部可重跑：步 1 后中断 → 第二遍步 1 判别不触发、步 2 建新、步 3 删旧；
  步 2 中断留下 invalid 同名索引 → 第二遍步 1 先把残骸改名为 `_invalid`（若上上次
  已留下 `_invalid`，步 1 之前先并发删除它，避免改名撞名）再建；该分支由真库用例
  覆盖（用 case-dup 行让 `CREATE UNIQUE INDEX CONCURRENTLY` 同名失败制造 invalid
  残骸，再删账本行重放）；步 3 前中断 → 第二遍
  只剩步 3/4。`IF NOT EXISTS` 单独不够（它只按名字判存在，会跳过 invalid 残骸），
  故 invalid 清理是必需步骤，E4(i) 与集成断言都要查 `indisvalid = true`。
- 修复腿候选 SQL 需要**行为** oracle（round-1 审查 C2：字符串钉下三种语义中和突变
  全绿）：真库用例在 throwaway DB 造 core/met 普通表（先 ANALYZE 使 `relpages > 0`，
  再 `pg_stat_reset_single_table_counters`）与同样处理的 hypertable chunk，直接调用
  `_analyze_unanalyzed_authority_tables(dsn)`，断言普通表入选且被 ANALYZE、
  hypertable/chunk 不入选。
- node-27 首遍以 `python -m packages.common.migrate` 施加（写 `schema_migrations` 账本，
  否则后续 bring-up 会静默重放），第二遍以 `psql -v ON_ERROR_STOP=1 -f` 真重放；CI 只施加一遍
  （账本门控），幂等证据来自集成测试的"删账本行后 `apply_migration()` 重放"。

## D4: search 查询改写的边界

仅 `packages/common/model_registry.py` 列表路径（`_list_river_segments` 过滤器
拼装处）的 id 臂：`lower(rs.river_segment_id) LIKE %s ESCAPE '\\'`，参数
`like_pattern.lower()`（`_escape_like` 在前、`lower` 在后，`\`/`%`/`_` 不受
影响）。COUNT 语句与分页语句共用同一 filters 列表，自然同步。slice 路径在内存中
`needle.lower() in river_segment_id.lower()` 已等价，不动。`forecast_store` 的
station search 不动（D2）。

## D5: 测试策略

- mock DB 单测（扩展 `tests/test_node27_autopipeline_handoff.py` 既有 fixture：
  `_FakeCursor.execute` 新增 authority 候选 SQL 分派分支且排在通用
  `pg_stat_user_tables` 回读分支之前；`_prepare_autopipe` 同时 stub 修复腿，否则 16 个
  tick 用例会去拨真 socket；:622 用例改名为 `…_frontier_leg_never_touches_the_database`
  并断言修复腿仍被调用）：修复腿候选 SQL 文本钉（排除 hypertable / internal schema、三条件）、不受 ingest 门控、
  与前沿腿共享上限与开关、失败隔离、summary 结构 `stats_guard.authority`。
- `tests/test_list_search_contract.py`：id 臂语句文本钉改为 `lower(rs.river_segment_id) LIKE %s ESCAPE`，
  参数小写化钉、name 臂仍 ILIKE 钉。
- 真库集成（`tests/test_real_database_integration.py`，CI 真 PG + node-27）：
  (i) 索引清单钉不变；(ii) `indexdef` 含 `lower(river_segment_id)`；(iii) 造
  共享长前缀 id 数据后 `EXPLAIN` 等值 join **不含** `river_segment_id_trgm_idx`
  （结构性，不依赖成本），正向可选性用 catalog 断言（索引表达式
  `lower(river_segment_id)` + opclass `gin_trgm_ops` + 该 opfamily 的 `pg_amop` 含
  `~~(text,text)`），不用计划断言——E3 node-27 实跑证明小样本上 `enable_seqscan=off`
  仍会被无条件 bitmap 全索引扫描抢走，planner 实选归 E4(iii) 真数据 receipt；(iv) reloptions
  回读；(v) 幂等：`DELETE FROM schema_migrations WHERE version LIKE '000052%'` 后
  `apply_migration()` 重放，indexdef / `indisvalid` / reloptions / `_legacy` 缺席不变。
  索引断言一律含 `pg_index.indisvalid = true`。
- node-27 live（E4）：E3 的真库 pytest 只打 throwaway DB（superuser DSN 建/删临时库，
  项目既定做法），绝不指向生产库 `nhms`；迁移施加 receipt + 等值 join 无 pin 计划（预期 ≈17 ms 形态，
  `river_segment_pkey`）+ search 计划仍用 trgm + 一次 autopipe tick 的
  `stats_guard.authority` 修复 crosswalk/canonical_grid_cell + 复核 SQL 返回 0 行。

## 残余风险（记录，不在本 change 解决）

- 修复腿只认"双 NULL"。若崩溃恢复发生在一次 autoanalyze 之后、且表此后零 churn，
  计数器清零同时 `last_autoanalyze` 也清零——仍命中条件，覆盖。但若运维手工
  `pg_stat_reset_single_table_counters` 则同样命中，属预期。
- 其他 schema（`ops` 等）的普通表未纳入候选集——诊断清单里没有受害者；扩展只需
  改一个 schema 元组。
- `public.spatial_ref_sys`（PostGIS 自带，8,500 行，零统计）不在候选集，等值查找
  走 pkey，无影响。
- `relpages` 只由 VACUUM/ANALYZE/CREATE INDEX 维护：一张有行但从未被 analyze 过的
  小表 `relpages = 0`，不在修复腿候选集内；这类表有 churn（行是写进去的），50 行
  阈值由 autoanalyze 兜住，且 <50 行的表 planner 默认估计无害。判据保持 `relpages > 0`。
