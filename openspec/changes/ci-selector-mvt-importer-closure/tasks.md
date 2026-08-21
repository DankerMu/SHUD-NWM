# Tasks: ci-selector-mvt-importer-closure (#1597)

## 1. Implementation

- [x] 1.1 `tests/test_select_ci_tests.py`：`GUARDED_MODULE_CLOSURES` 加入
      `("services/tiles/mvt.py", "services.tiles.mvt", "tests/test_hydro_display_mvt_scaling.py")`；
      先于 1.2 执行并记录守卫红证（missing 7 个）；同步把 `:2235` / `:2944` 注释里的
      "two … GUARDED_MODULE_CLOSURES modules" 改为 three。
- [x] 1.2 `scripts/select_ci_tests.py`：`services/tiles/mvt.py` 规则补 7 个套件
      （4 direct + 3 one-hop），注释说明来源与 #1597。
- [x] 1.3 `tests/test_select_ci_tests.py`：`test_select_tests_maps_mvt_tiles_without_core_smoke_fallback`
      等值 pin 由 5 条更新为 12 条（按 selector 输出顺序），保留无 core-smoke 兜底断言。

## 2. Tests

- [x] 2.1 `uv run pytest -q tests/test_select_ci_tests.py` 全绿（含既有两条闭包断言不倒退）。
- [x] 2.2 新选中 7 个套件实跑绿并记录墙钟。

## Evidence Floor

- [x] E1 `uv run python scripts/select_ci_tests.py --changed-file <mvt-only> --repo-root .`
      输出含 7 个新增套件；修复前输出（5 个）留档。
- [x] E2 守卫 anti-vacuity：1.1 后 1.2 前守卫红（missing 列表）；1.2 后绿。
- [x] E3 `uv run pytest -q tests/test_select_ci_tests.py`；7 个套件实跑墙钟；`uv run ruff check .`。
- [ ] E4 CI：PR 自身的 "Unit Tests" 绿（selector 改动会自选 `tests/test_select_ci_tests.py`）。
