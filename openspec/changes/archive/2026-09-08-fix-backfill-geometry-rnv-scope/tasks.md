Fixture level: expanded
Change surface:
- `workers/model_registry/basins_registry_import.py:1225-1274`：`updates` 元组追加第 5 列 `river_network_version_id`，`execute_values` 的 `template` 与 `VALUES` 子查询列表同步为 5 列，UPDATE 的 WHERE 增加 `AND target.river_network_version_id = source.river_network_version_id`
- 新文件 `tests/test_backfill_geometry_network_scope_integration.py`：integration 用例 `test_output_geometry_backfill_only_touches_the_target_network_version`（不能进 #1913 冻结的 7 个 partition 文件，design D3）
- `docs/runbooks/receipts/2026-09-08-issue-2158-backfill-rnv-scope-node27.md`：node-27 红→绿 receipt（orchestrator 产出）
Must preserve:
- 两条候选 SELECT `:1167-1179` / `:1195-1210` 逐字不变；`ST_Length(source.geom) > 0`、`RETURNING` + `fetch=True` 计数、`only_missing` 语义、`geometry_generation` bump 的 `updated_rows` 门控不变
- `tests/test_hhe_mvt_binding.py`（`rows[0][3]` 是 provenance）、`tests/test_river_segment_write_surface_scan.py`（恰一处 UPDATE 字面量）、`tests/test_basins_registry_import.py:277,302`、`tests/test_basins_registry_import_db.py` 零改动（#1913 冻结 partition，`tests/test_select_ci_tests.py` 四条守卫）；`tests/test_mvt_national_identity_probe_integration.py:1889-1984`（真实 DB：`updated == 1` / `stream_type == 4.0` / 二次 pass 0，同一 CI job 内执行）零改动；`basins_registry_import.py:1121` 与 `qhh_production_bootstrap.py:1576` 的 `template="(%s, %s, %s, %s)"` 保持 4 列（勿全局替换该字面量）；第四个调用方 `scripts/node27_autopipeline.py:1361-1382` 不动
Must add/change:
- 一处谓词（含 5 列 template 与列名列表）；一条真实 DB 用例；一份 receipt
Seams under test:
- `_backfill_output_segment_geometry(cursor, A)` 对真实 PostGIS 表的写作用域：A 输出行更新、B 同名输出行逐字不变（含 STORED `stream_type`）、`geometry_generation` 只 A +1、返回值 == 1
Risk packs:
- PostGIS / TimescaleDB domain behavior: selected - 复合主键 + STORED 生成列的行为只有真实 PostGIS 能验；用例在 CI real-db 与 node-27 两处真实库执行
- Geospatial / CRS / basin geometry: selected - 写的是 `core.river_segment.geom`；A/B 几何取不同坐标，断言按 `ST_AsText` 逐字比较
- Error Handling / Rollback / Partial Outputs: selected - 谓词收窄后 `RETURNING` 计数与 `geometry_generation` 门控必须仍只反映目标网络；无源 `Type` / 零更新路径由既有 unit 用例钉住
- Schema / columns / units / field names: not selected - 不改任何列或迁移
- File IO / path safety / overwrite: not selected - 无文件写入
- Public API / CLI / script entry: not selected - 函数签名与返回类型不变
- Other packs: not selected - 无 auth/并发/发布面（#2157 的锁序问题另单）
Required evidence:
- CI `real-db-integration`（"SQL Migration Dry Run"）在 PR 上真实执行新用例并通过（PR 非 draft）
- node-27 throwaway worktree：`NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=… TMPDIR=/home/nwm/tmp /home/nwm/NWM/.venv/bin/python -m pytest tests/test_backfill_geometry_network_scope_integration.py -q -p no:cacheprovider` 绿 → 替换为 `origin/master` 版本源文件红 → 恢复后绿；三段输出（DSN 脱敏）进 receipt
- `uv run pytest -q tests/test_hhe_mvt_binding.py tests/test_river_segment_write_surface_scan.py tests/test_basins_registry_import.py tests/test_basins_registry_import_qhh.py tests/test_qhh_production_bootstrap_state.py tests/test_select_ci_tests.py tests/test_basins_registry_import_db.py tests/test_backfill_geometry_network_scope_integration.py` 本地全绿（integration 用例本地按 marker 跳过）
- `uv run ruff check .`；`openspec validate fix-backfill-geometry-rnv-scope --strict --no-interactive`；`git diff --name-only origin/master` 只含上述三个文件 + `openspec/**`
Non-goals:
- 不收窄 `create_river_network` / qhh upsert 入口；不改 `_qhh_output_segment_geometry_counts`；不碰 #2154/#2155/#2156/#2157；不部署

## 1. Implementation

- [x] 1.1 `workers/model_registry/basins_registry_import.py`：`updates` 元组追加 `river_network_version_id` 为第 5 列（类型注解同步为 `tuple[str, str, float | None, str, str]`），`template="(%s, %s, %s, %s, %s)"`，`VALUES` 子查询列表 `AS value(river_segment_id, wkt, length_m, provenance, river_network_version_id)` 并暴露 `value.river_network_version_id::text AS river_network_version_id`，WHERE 增加 `AND target.river_network_version_id = source.river_network_version_id`；在 `:1247-1251` 注释块补一句为何网络 id 走 VALUES 列（`execute_values` 单占位符）而非第二个 bind，引用 #2158

## 2. Tests

- [x] 2.1 新文件 `tests/test_backfill_geometry_network_scope_integration.py`（模块级 `pytestmark = pytest.mark.integration`）中的用例 `test_output_geometry_backfill_only_touches_the_target_network_version(integration_database_url)`：`apply_migrations_from_zero`；`psycopg_connection` 内按前缀 `it2158_` DELETE（segment → rnv → basin_version → basin）后 seed：basin、basin_version（`ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490))`）、rnv A/B（`version_label` `v-a`/`v-b`，`segment_count` 2）、每网源 reach 行 `it2158_reach_000001`（`{"iRiv": "1", "Type": 3}` / `{"iRiv": "1", "Type": 4}`，A `LINESTRING(110.0 30.0, 110.6 30.6)` 1200.0，B `LINESTRING(116.0 36.0, 116.6 36.6)` 1800.0）与输出行 `it2158_shud_riv_000001`（`{"shud_output_river": "true", "shud_riv_index": "1"}`，`geom`/`length_m` NULL），A/B 输出行 id 文本相同；快照 B 输出行 `(ST_AsText(geom), length_m, properties_json, stream_type)` 与两网 `geometry_generation`；调用 `_backfill_output_segment_geometry(cursor, A)`；断言 (a) A 输出行 `ST_AsText(geom) == ST_AsText(A 源 reach geom)`、`length_m == 1200.0`、`properties_json["Type"] == 3`、`stream_type == 3.0`；(b) B 输出行四元组 == 快照（`geom` None、`stream_type` None）；(c) A `geometry_generation` == 前值 + 1，B == 前值；(d) 返回值 == 1
- [x] 2.2 红→绿：node-27 上按 design D4 先确认 `python -c "import workers.model_registry.basins_registry_import as m; print(m.__file__)"` 解析到 throwaway worktree，再用文件替换法跑三段（绿 → master 版红 → 绿），红段预期 (b) 或 (d) 断言失败并显示 B 被写成 A 的几何 / 返回 2；输出贴 PR body 与 receipt。CI `real-db-integration` 绿作为第二真实库证据
- [x] 2.3 node-27 receipt `docs/runbooks/receipts/2026-09-08-issue-2158-backfill-rnv-scope-node27.md`（orchestrator 写；DSN 脱敏、每行 ≤ 300 字符、无裸 URL；`npx --yes markdownlint-cli2 <file>` 0 issues）
- [x] 2.4 本地 `uv run pytest -q tests/test_hhe_mvt_binding.py tests/test_river_segment_write_surface_scan.py tests/test_basins_registry_import.py tests/test_basins_registry_import_qhh.py tests/test_qhh_production_bootstrap_state.py tests/test_select_ci_tests.py tests/test_basins_registry_import_db.py tests/test_backfill_geometry_network_scope_integration.py` 全绿，既有用例零改动

## 3. Verification

- [x] 3.1 `git diff --name-only origin/master` 只含 `workers/model_registry/basins_registry_import.py`、`tests/test_backfill_geometry_network_scope_integration.py`、`docs/runbooks/receipts/2026-09-08-issue-2158-backfill-rnv-scope-node27.md`、`openspec/changes/fix-backfill-geometry-rnv-scope/**`；两条候选 SELECT 在 diff 中无改动
- [x] 3.2 `uv run ruff check .`；`openspec validate fix-backfill-geometry-rnv-scope --strict --no-interactive`

## Evidence Floor

2.2 两处真实库红→绿（CI real-db 绿 + node-27 receipt 三段）+ 2.4 既有用例零改动全绿 + 3.1 diff 面 + 3.2 本地。
