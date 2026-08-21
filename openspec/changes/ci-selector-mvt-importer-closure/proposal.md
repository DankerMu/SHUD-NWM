# Proposal: ci-selector-mvt-importer-closure

## Why

Issue #1597：`scripts/select_ci_tests.py` 里 `services/tiles/mvt.py` 的
`PathTestRule` 只列 5 个套件，其中没有一个断言 `postgis_tile_sql()` 的输出形状；
真正把它当 oracle 的非门控直系 importer 套件全部落选。在 master `f664a21e` 上
复现（`select_tests(["services/tiles/mvt.py"])` vs 守卫推导的 direct ∪ one-hop
闭包）缺 **7** 个：

- direct：`tests/test_hhe_mvt_binding.py`、`tests/test_hydro_display_mvt_scaling.py`、
  `tests/test_node27_timeseries_compression_benchmark.py`、
  `tests/test_node27_timeseries_compression_live_evidence.py`
- one-hop（经 `apps/api/routes/hydro_display.py`）：
  `tests/test_direct_grid_display_cutover_flip.py`、
  `tests/test_direct_grid_display_cutover_history.py`（issue 手工枚举时漏计）、
  `tests/test_direct_grid_display_cutover_model_resolution.py`

实跑代价：7 个套件 521 passed / 29.2 s。同家族第六次（#1191/#1247/#1283/#1447/#1455）：
每次只补当次那条边；#1455 交付的 AST importer 闭包守卫
`test_guarded_module_rules_cover_their_non_gated_importer_closure` 本可机械化，却被
限定在 `GUARDED_MODULE_CLOSURES` 两条白名单里，`services/tiles/mvt.py` 不在其中。

## What Changes

1. `scripts/select_ci_tests.py`：`services/tiles/mvt.py` 规则补 7 个套件（4 direct +
   3 one-hop），注释写明来源是守卫推导的闭包而非手挑。
2. `tests/test_select_ci_tests.py`：`GUARDED_MODULE_CLOSURES` 加入
   `("services/tiles/mvt.py", "services.tiles.mvt", "tests/test_hydro_display_mvt_scaling.py")`
   ——此后任何新的 mvt importer 会把守卫点红而不是静默落选；
   `test_select_tests_maps_mvt_tiles_without_core_smoke_fallback` 的等值 pin 更新为
   新集合并保留"无 core-smoke 兜底泄漏"断言。
3. Anti-vacuity 证据：白名单加入后、规则补边前，守卫必须红（列出 7 个 missing）。

## Non-Goals

- 不把 `GUARDED_MODULE_CLOSURES` 改成全树推导（更大的治本改造，后续独立立项）。
- 不动 `packages/**` 同名套件路由（#1587）、suite→suite 边（#1561）、collect-only 零断言
  兜底（#1182）。
- 不改 `mvt.py` 自身；不改两个带 `integration` marker 的门控 importer
  （`tests/test_mvt_national_identity_probe_integration.py`、
  `tests/test_river_ts_read_path_surrogate_keys_integration.py`）——守卫定义按 #1447 裁定
  排除 file-level 门控套件，它们在 "SQL Migration Dry Run" lane 跑。
