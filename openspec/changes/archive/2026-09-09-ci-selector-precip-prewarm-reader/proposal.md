# Proposal: ci-selector-precip-prewarm-reader (#2122)

## Why

PR #2117（#2013）给 `scripts/node27_mvt_prewarm.py:57` 加了 `from services.precip.mirror import horizon_valid_times`（在 `:276` 消费），
而 `tests/test_node27_mvt_prewarm.py:17` 模块级 `from scripts import node27_mvt_prewarm as prewarm`，所以 prewarm suite 成了
`services/precip/**` 的 one-hop importer suite。但 `scripts/select_ci_tests.py:1914-1916` 的 `"services/precip/**"` 规则只路由
`PRECIP_SURFACE_TESTS`（`:1203-1208`，四条：`test_precip_overlay`、`test_openapi_drift`、`test_openapi_31_contract`、`test_api_contract`）。
实测（d113edca）只含 `services/precip/constants.py` 或只含 `services/precip/mirror.py` 的清单各输出这四条，均无 `tests/test_node27_mvt_prewarm.py`；
树内 `cache/errors/field/render/__init__.py` 同样只有四条。

有真实 oracle 可漏：`PRECIP_STEP_HOURS`（`services/precip/constants.py:28`，值 3）经 `mirror.py:114-115` 的 `horizon_valid_times` 决定 prewarm 的
valid-time 网格；clamped-first-valid-time / PNG-horizon 契约**只**由 `tests/test_node27_mvt_prewarm.py` 的
`test_a_clamped_first_valid_time_is_warmed_from_that_entry_not_from_the_cycle` 断言（该文件按用例名引用、不写行段，见
`openspec/changes/display-v2-national-timeline-precip-overlay/tasks.md:548`），改动前定向 CI（`unit-test-targeted`）构造性跑不到这个 oracle。
**更正 issue 前提**：issue 称该 flip「唯一会红的 suite」是 prewarm，此说不成立——它的变异探针在 `pytest_configure` 里 rebind 常量且只跑了 prewarm 一个 suite。
本次在 PR head 上实测把常量改成 6：`tests/test_precip_overlay.py` → `31 failed, 65 passed`，`tests/test_node27_mvt_prewarm.py` → `25 failed, 30 passed`。
`tests/test_precip_overlay.py` 本就是 `PRECIP_SURFACE_TESTS` 第一条、改动前即被选中，所以该 flip 从来不会绿着过 PR 门。本 change 补的是缺失的 oracle 覆盖面，不是「唯一红点」。

不对称参照：只含 `services/tiles/mvt.py` 的清单会选到 prewarm suite（`scripts/select_ci_tests.py:1817` 已是 mvt 规则的 DIRECT importer 目标），
同一个 suite 的另一个上游常量路由是通的。

`services/precip` 不在 `tests/test_select_ci_tests.py:7675-7689` 的 `DIRECTORY_RULE_AUDIT_PATHS` 内，`services/precip/mirror.py` 也不在
`GUARDED_MODULE_CLOSURES`（`:2979`，`:10025` 钉 `len == 4`），所以既有的 importer-gap / closure 守卫都不会自动派生这条边，也不会因本 change 而变动。

本 change 只补 issue 点名的 prewarm 腿，**不代表该树的一跳 importer 闭包已完整**：`scripts/node27_raw_retention.py:44` 模块级
`from services.precip.constants import FILE_CACHE_DIR_ENV`（`:219` 消费），`tests/test_node27_raw_retention.py:11` 模块级 `from scripts import node27_raw_retention`，
是与 prewarm 同构的非 gated 一跳边，实测只含 `services/precip/constants.py` 的清单同样不选它；这是 fixture review 发现的同类相邻缺口，已立单 #2191（见 Non-Goals）。

## What Changes

1. `scripts/select_ci_tests.py` `PATH_TEST_RULES`：把 `"services/precip/**"` 规则（`:1909-1916`）的目标从 `PRECIP_SURFACE_TESTS` 改为
   就地并集 `(*PRECIP_SURFACE_TESTS, "tests/test_node27_mvt_prewarm.py")`，带 `#2122` 注释说明 import 边（`scripts/node27_mvt_prewarm.py:57`
   → `tests/test_node27_mvt_prewarm.py:17`）与判别输入（`PRECIP_STEP_HOURS`）。**不动**共享元组 `PRECIP_SURFACE_TESTS`（`:1203-1208`），
   **不动** `apps/api/routes/precip.py` 规则（`:1917-1924`）——路由改动不该拖上 prewarm suite。不加 `stop_on_match` / `only_when_any_changed`。
   该 glob 覆盖树内全部 7 个模块（`__init__/cache/constants/errors/field/mirror/render.py`），改后 `cache/errors/field/render/__init__.py` 的单文件清单同样从四条变五条、
   多选 prewarm（它只读 `mirror.horizon_valid_times`）——按 issue 推荐的 glob 方案接受此过选；spec 的两个 parametrize 实例是 7 模块的抽样，不是穷举。
2. `tests/test_select_ci_tests.py`：在 `test_select_tests_maps_sh_only_wrapper_change_to_its_guard_suite`（`:1242`）之前新增两条用例，
   全部用**显式字面 expected list**、`==` 精确相等（#1827 教训：不得引用 `PRECIP_SURFACE_TESTS` 自证）：
   - `test_precip_tree_module_selects_the_prewarm_reader_suite`：`@pytest.mark.parametrize("module", ["services/precip/constants.py", "services/precip/mirror.py"])`，
     `assert Path(module).exists()`，`select_tests([module], repo_root=Path(".")) == ["tests/test_api_contract.py", "tests/test_node27_mvt_prewarm.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py"]`。
   - `test_precip_route_rule_stays_without_the_prewarm_suite`：`assert Path("apps/api/routes/precip.py").exists()`（规则匹配是纯 `fnmatch`，不校验路径存在，同文件 `:297`/`:1231`/`:1245` 的先例都先断存在，否则路由改名后 pin 变僵尸），`select_tests(["apps/api/routes/precip.py"], repo_root=Path(".")) == ["tests/test_api.py", "tests/test_api_contract.py", "tests/test_monitoring_api.py", "tests/test_openapi_31_contract.py", "tests/test_openapi_drift.py", "tests/test_precip_overlay.py"]`，
     注释写明它是"共享元组未被改"的反向护栏。
   改规则前第一条的两个参数化实例红（缺 prewarm suite），第二条改前改后都绿；第二条的承重证据是 mutant：把 prewarm suite 临时追加进
   `PRECIP_SURFACE_TESTS` 后它必须红（多出第 7 条），实现报告贴红→绿与 mutant 输出。

## Non-Goals

- 不改 `PRECIP_SURFACE_TESTS` 元组（`:1203-1208`）与 `apps/api/routes/precip.py` 规则；不改任何 `services/precip/**`、`scripts/node27_mvt_prewarm.py`、
  `tests/test_node27_mvt_prewarm.py` 的行为。
- 不把 `services/precip/mirror.py` 加进 `GUARDED_MODULE_CLOSURES`（要动 `:10025` 的 `len == 4`，且一跳边界覆盖不到 `constants.py`，issue 已评估为收益更低的备选）；
  不把 `services/precip` 加进 `DIRECTORY_RULE_AUDIT_PATHS`（会要求逐对 disposition 整棵树的 importer gap，是另一个 issue 的量级）。
- 不处理 `NATIONAL_DISCHARGE_VALID_TIME_STRIDE_HOURS`（`services/tiles/mvt.py:123`）与 `PRECIP_STEP_HOURS` 两个独立 3 的耦合（`tests/test_node27_mvt_prewarm.py` 的 `test_a_clamped_first_valid_time_is_warmed_from_that_entry_not_from_the_cycle` 内已有注释；该文件按用例名引用、不写行段，见 `openspec/changes/display-v2-national-timeline-precip-overlay/tasks.md:548`）。
- 不处理 `services/precip/constants.py` → `tests/test_node27_raw_retention.py` 的同构一跳边（`scripts/node27_raw_retention.py:44` → `tests/test_node27_raw_retention.py:11`，同样非 gated、同样未被选中）；本单只补 prewarm 腿，该缺口已立单 #2191，届时 2.1 的精确五条清单需同步改为六条。
- 不碰 #2098（应用组合 owner → `PRECIP_SURFACE_TESTS`，相邻不同边；后落地者 rebase）、#2188 / #2185（其他 selector 缺口）。
- 不改 `.github/workflows/ci.yml`。

## Risk triage

- Fixture level: compact（一条规则目标并集 + 两条显式 expected-set 用例；无运行时行为变化。不取 `none`：`scripts/select_ci_tests.py` 是 CI 定向门的
  共享选择入口，与 #2173 / #2180 同级）。Upstream suggested level: absent（issue 来自 PR #2117 round-5 CONFIRMED/DEFER 的 issue-scribe 立单，
  无 `Suggested fixture level` 字段；`预估规模 S`）。
- Repair intensity: low。
- Risk packs 与 evidence 见 `tasks.md`。`design.md` 按 compact 级豁免。
- Issue 行号漂移（写 issue 时 vs d113edca）：规则 `:1900`→`:1909`、元组 `:1199`→`:1203`、prewarm import `:56`→`:57`；语义未变（prewarm 用例按名引用，不记行号）。

## Must preserve

- `scripts/select_ci_tests.py:1817` mvt 规则里的 prewarm 目标、`:2389` / `:2495` 两处既有 prewarm 目标不动。
- `tests/test_select_ci_tests.py:299-302`（autopipe `.timer` 精确 `== [preflight, mvt_prewarm]`）、`:1235-1239`（autopipe cron wrapper 精确相等）、
  `:965-1034` mvt 规则目标清单（`test_select_tests_maps_mvt_tiles_without_core_smoke_fallback`，`:961`；prewarm 条目在 `:1010`）、`:4858-4929` duplicate-pattern / stop-on-match 守卫、`:8200-8222` importer-gap disposition 守卫全部照旧绿。
- 只含 `apps/api/routes/precip.py` 的清单仍恰为六条（`apps/api/**` 的三条 + `PRECIP_SURFACE_TESTS` 四条去重）。

## Seams under test

- `select_tests([path], repo_root=Path("."))` 对 `services/precip/constants.py`、`services/precip/mirror.py` 的输出（精确五条）与对
  `apps/api/routes/precip.py` 的输出（精确六条，不含 prewarm）。

## Evidence mapping

见 `tasks.md` Evidence Floor；本地即可闭环（issue `Verification:` 明示无需 node-22 / node-27 oracle）。
