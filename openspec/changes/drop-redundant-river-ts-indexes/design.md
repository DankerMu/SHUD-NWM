# Design: drop-redundant-river-ts-indexes（#1338）

## 风险三角与 fixture level

- 风险：**查询计划退化**（删错致 Seq Scan/显著慢化——issue 自点名的主风险，可逆但伤 display 延迟）×**证据陈旧**（仓内自述引用已删索引）×**迁移执行面**（CONCURRENTLY 与 harness/手工路径交互）。
- fixture level：**compact**（S 规模纯迁移 + 证据对齐；无 schema/行为变更；删除可逆）。

## 现状基线（2026-08-14 实测 + 仓内核实）

- 六条索引全量清单、尺寸、扫描数：`.workplans/issue-1338/pre-inventory.md`（PR 开出后同步为 PR 评论）。
- 创建出处：`mvt_identity_lookup_idx` = `db/migrations/000019`；`valid_time_discovery_idx` **无仓内出处**（带外）；先例 drop 文体 = `000041` / `000042`（单条 `DROP INDEX CONCURRENTLY IF EXISTS` + rationale 注释，经 migrate.py 在 node-27 成功应用并记账，schema_migrations 顶行 000042 首见于 2026-07-07 loop-log——CONCURRENTLY 在本 hypertable 拓扑可行的实证；**caveat（F4）**：该先例发生在 000047 启用 TimescaleDB 压缩**之前**，今日 `hydro.river_timeseries` 已有 compressed chunk（`parser.py:734` 守卫证明生产生效），3.4 pre-flight 必须记录 `timescaledb_information.chunks` 的 `is_compressed` 分布）。
- harness：`packages/common/migrate.py` 全局 autocommit、无事务包裹（`:161`），`CONCURRENTLY` 天然可行；`schema_migrations(version, applied_at)` 记账跳过。
- node-27 记账漂移：schema_migrations 止于 `000045`；`000046-48` 生效未记账（000047 系 runbook 手工双跑先例）。
- 仓内引用面：`packages/common/forecast_store.py:3420-3446` `_qhh_latest_query_indexes()` 陈述 `covered_by_mvt_identity_lookup_index`，被 `tests/test_migrations.py:370` 与 `tests/test_forecast_api.py:1735` 钉住；`tests/test_migrations.py:253-283` 钉的是 `mvt_selected_identity_valid_time_discovery_idx`（保留集，不受影响）。
- ADR 0002 `docs/adr/0002-node27-timeseries-hot-cold-tiering.md:29-30` "cannot be pruned further"（ADR Date: 2026-07-03；该句在 **Context** 的 2026-07-04 实测段，非任何 Decision 依赖项）。

## 决策

### D1 — 删除集与 SQL 形态

- 恰好两条，名字逐字：`hydro.river_timeseries_mvt_identity_lookup_idx`、`hydro.river_timeseries_valid_time_discovery_idx`。
- `DROP INDEX CONCURRENTLY IF EXISTS`（先例文体）：`CONCURRENTLY` 避免锁 display 读路径；`IF EXISTS` 双重必要——(a) 幂等重放，(b) `valid_time_discovery_idx` 在任何从迁移链重建的库（CI/hermetic/未来节点）**根本不存在**，无 `IF EXISTS` 则迁移在干净库上必炸。
- 迁移注释必须写明 `valid_time_discovery_idx` 系带外索引、仓内无创建出处——这是迁移文件对"删一个自己没建过的东西"的自证。
- **回滚 DDL 保全（fixture review F2）**：两条索引的重建 `CREATE INDEX` 语句原文写进 000049 注释（`mvt_identity_lookup` 取自 000019；`valid_time_discovery` 取自 node-27 实机 `pg_get_indexdef`——带外索引仓内无 DDL，不落盘则"可逆"变凭记忆重建）。
- 保留集（pkey / segment_time / valid_time_idx / selected_identity_valid_*）逐字不触碰。

### D2 — node-27 应用路径（AC2）

- 沿 **000047 手工 psql 先例**：`docker exec nhms-db psql -U nhms -d nhms -v ON_ERROR_STOP=1 -f`（逐条语句；`CONCURRENTLY` 不能进事务块，psql 默认 autocommit 满足）。二次重放验证幂等（`IF EXISTS` → no-op）。
- **不做** schema_migrations 补记或漂移修复（范围外报告；migrate.py 未来全量跑到 000049 时 `IF EXISTS` 保证安全重放）。
- 顺序硬约束：**pre-drop EXPLAIN/尺寸取证必须发生在 apply 之前**；且整条 3.4 实机腿**在 merge 前执行**（前置 = round 复审 clean + CI SQL dry run 绿）——使 1.2 的归因措辞在 merge 前由实测定稿，不留"pending 值被测试钉死、post-merge 实测翻案需二次 PR"的洞（round-1 C1/C2/C4 裁决）。

### D3 — 证据对齐范围（对 issue PR Boundary 的记录偏离）

- issue 说"不做任何 schema 或代码适配"，PR Boundary 限 `db/migrations/`。但 `_qhh_latest_query_indexes()` 是**证据自述函数**（introspection metadata，不在任何查询执行路径上）：删索引后不改，仓库将自述"该查询由 mvt_identity_lookup 覆盖"——与本 change 自己的迁移直接矛盾。
- 裁定：改**恰好这一处**状态陈述（`covered_by_mvt_identity_lookup_index` → 实测承接者的如实陈述，列元组同步）+ 两处测试钉同步。不碰任何执行路径代码。此为偏离记录第 1 条，PR body 显式声明（注：`query_indexes` 是公开 API 响应字段 `openapi/nhms.v1.yaml:3407-3428`，`index`/`status` 为无 enum 自由字符串、前端不渲染，改值不破契约——PR body 写明）。
- 承接索引（pre-drop 实测已定，round-2 修正）：`river_sample_rows` **已由保留的 `mvt_selected_identity_valid_time_discovery_idx` 服务**（predrop-baseline Q6，被删索引不在计划中）——pkey 不是承接者（无 `basin_version_id`，可用前缀止于 2 列）。post-drop EXPLAIN（D4 第 6 条）验证该计划不变即定稿。1.2 的最终措辞在 3.4（已前移 merge 前）实测后定稿、merge 前提交；在 receipt 产出前 forecast_store 注释必须用 pending 措辞，不得以肯定语气记载未产出的证据。

### D4 — EXPLAIN 取证查询集（AC3）

删除前后各跑一次 `EXPLAIN (ANALYZE, BUFFERS)`，同参数同库：

1. `services/tiles/mvt.py` national tile `typed_values`/`untyped_ranked` CTE（`:603-651`）——被删 `mvt_identity_lookup_idx` 的**唯一在册消费面**，主取证对象。
2. `services/tiles/mvt.py` `valid_times_for_layer` 具名身份分支（`:1220-1234`，走保留集 `selected_identity_valid_time_discovery_idx`，应零变化）。
3. `services/tiles/mvt.py` `valid_times_for_layer` 无 basin 分支（`:1236-1242`，列匹配被删 `valid_time_discovery_idx`，删后承接者取证）。
4. `services/tiles/mvt.py` hydro 层 basin/segment tile CTE（`:454-474`）。
5. `apps/api/routes/hydro_display.py` `_require_hydro_mvt_source_identity`（`:749-769`，邻接面基线记录）。
6. **（F1）**`packages/common/forecast_store.py:1633-1650` `river_sample_rows`（qhh-latest display product 主路径）——谓词 `(run_id, …, variable, valid_time BETWEEN)` 正是被删 `mvt_identity_lookup_idx` 的三列前缀形态，D3 的"pkey 承接"陈述**由这条的 post-drop EXPLAIN 实测决定**，不得推断。
7. **（F3）**`workers/output_parser/parser.py:745-755` ingest 窗口 `DELETE` 谓词（对已存在 run 的窗口做只读 `EXPLAIN`，不真删）——**pre-drop 实测（baseline Q7）已在 pkey 上、四谓词全推入 Index Cond，写路径非被删索引消费面**；保留在判定集作廉价回归锚。
8. **（F3）**`services/tiles/mvt.py:530-553` `source_identity_stats_sql`（national identity 探针）。

判定：**八条**无一退化为 Seq Scan，且**八条全部**执行时间无数量级劣化——其中 **1 与 8 是被删索引的实测消费面**（predrop-baseline Q1/Q8 Index Only Scan），是 post-drop 退化风险的焦点；3/6/7 的 pre-drop 计划本就在保留索引/pkey 上（Q3=valid_time_idx、Q6=selected_identity、Q7=pkey），作廉价回归锚保留在判定集内。**若出现退化：立即重建被删索引回滚（可逆性即回滚方案），change 终止并回 upstream**。

### D5 — 测试形态（B 锚）

- B1（迁移文本钉，`tests/test_migrations.py` 既有文体）：`000049` 文件存在、恰含两条 `DROP INDEX CONCURRENTLY IF EXISTS`、目标名逐字、不含任何保留集索引名、编号接 `000048` 之后无冲突。**硬约束（fixture review F5）**：`tests/test_migrations.py:52-60` 的迁移体检门要求文本（lower）子串命中 `create`/`select`/`do`/`alter` 之一，两个目标名与自然 rationale 措辞一个都不含——解法**规定为**在注释中写入两条回滚用 `CREATE INDEX` 语句原文（同时满足 F2 的回滚 DDL 保全），**禁止**以削体检门过关。
- B2（证据对齐钉）：`_qhh_latest_query_indexes()` 不再陈述 `mvt_identity_lookup`，新陈述与既有测试断言同步（`test_migrations.py:370` / `test_forecast_api.py:1735` 改后仍绿且仍在断言实质内容，不得删钉了事）。
- B3（保留集回归）：`test_selected_run_valid_time_discovery_migration_matches_strict_identity_predicates`（`:253-283`）等既有迁移测试不动仍绿。
- 红证：B1 在迁移文件缺失/名字打错时红；B2 在 forecast_store 未同步时红（先跑旧断言证红再改）。

## Invariant Matrix（本 change 触及）

- 不变式：display 主查询路径删除前后**无计划退化**；迁移链在"索引从未存在"的库上可完整重放；仓内索引证据自述与迁移链终态一致。
- 兄弟面自查：`grep -rn "mvt_identity_lookup\|valid_time_discovery_idx"` 全仓扫尾——**carve-out（F7）**：`db/migrations/000019`（不可变历史）、`tests/test_migrations.py` 000019 相关文本钉（:187-205 等）、`openspec/changes/archive/**` 时点记录均豁免；其中 `test_hydro_mvt_identity_index_protects_public_valid_time_lookup_contract` 函数名断言已删索引 "protects contract"——**范围外 follow-up 记一笔，本 change 不动**（与 D3 "改恰好一处" 一致）。

## Evidence Mapping

- AC1（迁移落地 + pytest）↔ B1/B2/B3 + tasks 3.1。
- AC2（node-27 实机应用 + `_hyper_3_32_chunk` 前后尺寸）↔ tasks 3.4（D2 路径）。
- AC3（EXPLAIN 前后对比无退化）↔ tasks 3.4（D4 查询集）。
- AC4（`/` 与 `/ops` live 正常）↔ tasks 3.5。
- ADR 0002 supersession ↔ tasks 1.3；带外索引现实 ↔ D1 注释要求 + B1。
