# Proposal: fix-backfill-geometry-rnv-scope (#2158)

## Why

`workers/model_registry/basins_registry_import.py:1128` `_backfill_output_segment_geometry(cursor, river_network_version_id, *, only_missing)`
的两条候选 SELECT（`:1167-1179` 目标行、`:1195-1210` 源 reach 行）都带 `WHERE river_network_version_id = %s`，但写侧 UPDATE（`:1252-1274`）
的匹配谓词只有 `target.river_segment_id = source.river_segment_id`。`core.river_segment` 的主键是复合的
`(river_segment_id, river_network_version_id)`（`db/migrations/000004_core.sql:42`），`river_segment_id` 单列无唯一约束，而 id 前缀只含
`model_id`（`:1308` `{model_id}_shud_riv_{N:06d}`），网络版本轴由 `basin_version_id + version_label` 另行生成
（`packages/common/model_registry.py:912-916`），`create_river_network`（`:895-980`）又原样接受调用方 payload 里的 `river_segment_id`。
因此同一 `river_segment_id` 可以合法地存在于网络 A 与 B；一次针对 A 的回填会把 B 的同名行一并改写（`geom`、`length_m`、
`properties_json || provenance` 带入的 `Type` → STORED `stream_type`），`RETURNING` 把 B 的行也计入返回值
（经 `qhh_production_bootstrap.py:694` / `:1076` 进 receipt 字段 `geometry_backfilled_count`），而 #2031 加的 `geometry_generation` 自增
（`:1289-1296`）只打在 A 上，B 的瓦片缓存键永不轮转、永久渲染被污染的几何。node-27 只读实测今日 0 碰撞（43 网络 / 555096 行），
本 change 是结构性加固，不是现网故障修复。

## What Changes

1. `workers/model_registry/basins_registry_import.py:1252-1274`：UPDATE 改为按复合主键匹配。`execute_values` 只允许一个 `%s` 占位
   （`psycopg2.extras._split_sql`），所以不能再加一个独立 bind；改为把 `river_network_version_id` 作为 `VALUES` 的**第 5 列**（追加在
   `provenance` 之后，`template="(%s, %s, %s, %s, %s)"`，每条 `updates` 元组末尾带上入参 `river_network_version_id`），
   子查询 `AS value(river_segment_id, wkt, length_m, provenance, river_network_version_id)` 暴露
   `value.river_network_version_id::text AS river_network_version_id`，WHERE 增加
   `AND target.river_network_version_id = source.river_network_version_id`。列追加在末尾，`tests/test_hhe_mvt_binding.py:71`
   的 `captured["rows"][0][3]`（provenance 下标）不受影响。`ST_Length(source.geom) > 0`、`RETURNING target.river_segment_id`、
   `fetch=True`、`only_missing` 语义、`geometry_generation` 自增（仍 gated on `updated_rows`，现在只可能来自目标网络）一律不变。
2. 新文件 `tests/test_backfill_geometry_network_scope_integration.py`（`pytestmark = pytest.mark.integration`；不能进 7 个 #1913 冻结 partition，见 design D3）：用例
   `test_output_geometry_backfill_only_touches_the_target_network_version`：
   `apply_migrations_from_zero` 后用 `psycopg_connection` 直接 INSERT（不走 CLI）`core.basin` → `core.basin_version` →
   两条 `core.river_network_version`（A/B，同一 `basin_version_id`，`version_label` `'v-a'`/`'v-b'`）→ 每个网络两行
   `core.river_segment`：源 reach 行 `{prefix}_reach_000001`（`properties_json` `{"iRiv": "1", "Type": <A 用 3, B 用 4>}`，
   `geom` 用 `ST_Multi(ST_GeomFromText(<A/B 各自不同的 LINESTRING>, 4490))`，`length_m` A 1200.0 / B 1800.0）与输出行
   `{prefix}_shud_riv_000001`（`{"shud_output_river": "true", "shud_riv_index": "1"}`，`geom` NULL，`length_m` NULL）——**A 与 B 的输出行
   `river_segment_id` 文本完全相同**。快照 B 的输出行（`ST_AsText(geom)`, `length_m`, `properties_json`, `stream_type`）与两网的
   `geometry_generation`；对 A 调 `_backfill_output_segment_geometry(cursor, A)`；断言 (a) A 输出行 `geom` = A 源 reach 的
   `ST_AsText`，`length_m == 1200.0`，`properties_json->>'Type'` 为 `3`，`stream_type == 3.0`；(b) B 输出行四项与快照逐字相等
   （`geom` 仍 NULL、`stream_type` 仍 NULL）；(c) A 的 `geometry_generation` +1，B 的不变；(d) 返回值 `== 1`。
   id 前缀 `it2158_`，用例开头按前缀 DELETE（`core.river_segment` → `core.river_network_version` → `core.basin_version` → `core.basin`）
   保证重跑幂等（与 `tests/integration_helpers.py::_clear_issue_126_rows` 同法）。
3. 红→绿证据：CI `real-db-integration`（"SQL Migration Dry Run"，`workers/model_registry/**` 在 `database` 过滤器内）在真实
   PostGIS 上执行该用例；node-27 oracle 上用文件替换法（`git show origin/master:<file> > <file>` → 用例红 → `git checkout -- <file>` → 绿）
   产出 receipt `docs/runbooks/receipts/2026-09-08-issue-2158-backfill-rnv-scope-node27.md`。

## Non-Goals

- 不给 `create_river_network` / `qhh_production_bootstrap.py:1561-1578` 加"拒绝复用他网 segment id"的入口收窄——另一条不变量，issue 明列另开单。
- 不改 `_qhh_output_segment_geometry_counts`（`:1399-1420`）的完备性检查形态；不改 `geometry_backfilled_count` 的 receipt 语义。
- 不碰 #2154（同一 WHERE 缺"值确有变化"谓词的过度轮转）、#2156（兄弟 digest）、#2157（ABBA 锁序；本修复只收窄行锁足迹，正向）。
- 不修 `openspec/specs/basins-registry-import/spec.md:200-222` 断言 `_backfill_output_segment_geometry` 已删除的陈旧需求（#2155 owner）；
  本 change 的 delta 作为独立 ADDED 需求挂在同一 spec 下，描述现行代码的真实写作用域，不改写 #2155 的那段。
- 不修订 #1913 的 partition 冻结基线（`tests/fixtures/basins_registry_partition_oracle.json` 及其守卫）——它没有加测试的修订通道，作为范围外发现另立单。
- 不动 `tests/test_river_segment_write_surface_scan.py`（它按 `\bUPDATE\s+core\.river_segment\b` 子串扫描，谓词增加不改变"恰一处"结论）。
- 不部署：本函数只在导入 / QHH bootstrap 时执行，活动树在 `hotfix/node27-rollback-pre-2073`（#2162/#2145），生产不 pull。

## Risk triage

- Fixture level: expanded。强制触发词命中：`geometry` / `PostGIS` / `iRiv` / `.sp.riv`（profile 域触发）与"persisted/shared state
  transitions"（生产写路径的作用域）。Upstream suggested level: absent（issue 由 PR #2149 Phase 7 复审的 issue-scribe 立单，无该字段）。
- Repair intensity: normal（一条谓词 + 一条真实 DB 用例；无迁移、无 API、无部署）。
- Risk packs 与 evidence 见 `tasks.md`；`design.md` 见 D1-D5。

## Must preserve

- `tests/test_hhe_mvt_binding.py:54-73`：`only_missing=True` 返回 1、`rows[0][3]` 是 provenance JSON（`Type == 5.0`）；`:76-89`
  无源 `Type` 时零 UPDATE、零 `geometry_generation` 写。
- `tests/test_river_segment_write_surface_scan.py:126-161`：恰一处 `UPDATE core.river_segment` 字面量且位于本函数；bump 字面量恰一处。
- `tests/test_mvt_national_identity_probe_integration.py:1889-1984`（`test_geometry_backfill_rotates_both_national_digests_exactly_once`，
  真实 DB：`updated == 1`、`stream_type == 4.0`、`length_m == 1234.5`、二次 pass `== 0`；与新用例同在 CI `real-db-integration` 内执行）。
- `scripts/node27_autopipeline.py:1361-1382` 第四个调用方（tick receipt `output_geometry_backfilled`）与
  `tests/test_node27_connection_attribution_delegated.py:96-103` 的 cursor 交接契约不变。
- `tests/test_basins_registry_import.py:277,302`：call_count 契约；`tests/test_basins_registry_import_db.py` 零改动（#1913 冻结 partition）；`tests/test_select_ci_tests.py` 全绿。
- `openspec/specs/mvt-tile-contract/spec.md:58-78`：`geometry_generation` 只在实际更新 ≥1 行时 +1，同事务同 cursor。
- 两条候选 SELECT（`:1167-1179`、`:1195-1210`）逐字不变；`execute_values` 的 `fetch=True` + `RETURNING` 计数语义不变。

## Seams under test

- `_backfill_output_segment_geometry(cursor, rnv_id)` 对真实 PostGIS 表的写作用域（复合主键 + STORED `stream_type` 只有真实 DB 能验）；
  oracle：node-27 `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=… pytest tests/test_backfill_geometry_network_scope_integration.py`
  与 CI `real-db-integration`。
- `psycopg2.extras.execute_values` 的 5 列 template 与 `VALUES` 子查询列名对齐（unit 层由 `test_hhe_mvt_binding.py` 的 fake 观测行元组）。

## Evidence mapping

- 验收 1（UPDATE 带 `river_network_version_id` 谓词、SELECT 作用域不变）→ tasks 1.1、3.1（diff 面）。
- 验收 2（真实 DB 用例 (a)-(d)）→ tasks 2.1、2.3（node-27 receipt）。
- 验收 3（修复前红、修复后绿）→ tasks 2.2（CI real-db 绿 + node-27 文件替换法红→绿）。
- 验收 4（既有 `test_basins_registry_import.py` 与 write-surface scan 绿）→ tasks 2.4。
