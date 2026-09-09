# Tasks: ci-selector-precip-prewarm-reader (#2122)

Fixture level: compact
Change surface:
- `scripts/select_ci_tests.py`：`"services/precip/**"` 规则（:1909-1916）的目标改为 `(*PRECIP_SURFACE_TESTS, "tests/test_node27_mvt_prewarm.py")` + `#2122` 注释
- `tests/test_select_ci_tests.py`：`test_select_tests_maps_sh_only_wrapper_change_to_its_guard_suite`（:1242）之前新增两条显式 expected-list 用例（路由用例先 `assert Path(...).exists()`）与一条 flag 结构 pin（round-1 复审后补）
Must preserve:
- `PRECIP_SURFACE_TESTS`（:1203-1208）与 `apps/api/routes/precip.py` 规则（:1917-1924）零改动；`:1817` / `:2389` / `:2495` 既有 prewarm 目标不动
- `tests/test_select_ci_tests.py:299-302`、`:1235-1239`、`:965-1034`、`:4858-4929`、`:8200-8222` 既有用例照旧绿；`services/precip/**`、`scripts/node27_mvt_prewarm.py`、`tests/test_node27_mvt_prewarm.py`、`.github/workflows/ci.yml` 零改动
Must add/change:
- 一处规则目标并集；两条用例（一条 parametrize 两个 tree 模块的精确五条，一条路由精确六条反向护栏）
Seams under test:
- `select_tests([path], repo_root=Path("."))` 对 `services/precip/constants.py` / `services/precip/mirror.py`（精确五条，含 prewarm）与 `apps/api/routes/precip.py`（精确六条，不含 prewarm）
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门的选择入口；显式 expected-list 用例钉住三个路径的精确输出，既有规则用例全绿
- File IO / path safety / overwrite: not selected - 只读规则表，无写入
- Schema / columns / units / field names: not selected - 无数据格式
- Legacy compatibility / examples: not selected - 无兼容面
- Other packs: not selected - 无 auth/并发/发布/迁移面
Required evidence:
- `uv run pytest -q tests/test_select_ci_tests.py -k "precip_tree_module or precip_route_rule"`：改规则前 tree 用例两个实例红（expected 含 prewarm、actual 不含）、路由用例绿；改后三者全绿；红→绿输出贴 PR body
- mutant 证据：把 `"tests/test_node27_mvt_prewarm.py"` 临时追加进 `PRECIP_SURFACE_TESTS` 再跑 `-k precip_route_rule` 必须红（多出第 7 条），随后恢复；输出贴 PR body
- 三条清单实测贴 PR body：`printf 'services/precip/constants.py\n' | uv run python scripts/select_ci_tests.py`、同 `services/precip/mirror.py`（各恰五条含 prewarm）、`printf 'apps/api/routes/precip.py\n' | ...`（恰六条不含 prewarm）
- `uv run pytest -q tests/test_select_ci_tests.py tests/test_node27_mvt_prewarm.py` 全绿；`uv run ruff check .` 清洁
- `openspec validate ci-selector-precip-prewarm-reader --strict --no-interactive`；`git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/**`
Non-goals:
- 不改 `PRECIP_SURFACE_TESTS`、不动路由规则、不进 `GUARDED_MODULE_CLOSURES` / `DIRECTORY_RULE_AUDIT_PATHS`、不改任何生产行为、不碰 #2098 / #2188 / #2185、不改 ci.yml
- 不处理 `services/precip/constants.py` → `tests/test_node27_raw_retention.py` 的同构一跳边（`scripts/node27_raw_retention.py:44` → `tests/test_node27_raw_retention.py:11`，同样非 gated、同样未被选中）；本单只补 prewarm 腿，已立单 #2191；glob 加宽让树内其余 5 个模块也多选 prewarm，按 issue 推荐方案接受

## 1. 选择规则

- [x] 1.1 `scripts/select_ci_tests.py` `"services/precip/**"` 规则目标改为 `(*PRECIP_SURFACE_TESTS, "tests/test_node27_mvt_prewarm.py")`，紧贴既有 `# #2010` 注释追加 `# #2122` 注释（import 边 `scripts/node27_mvt_prewarm.py:57` → `tests/test_node27_mvt_prewarm.py:17`；判别输入 `PRECIP_STEP_HOURS`；就地并集、不动共享元组以免路由规则继承）。不加 `stop_on_match` / `only_when_any_changed`。

## 2. 显式 expected-set 用例

- [x] 2.1 `tests/test_select_ci_tests.py` 新增 `test_precip_tree_module_selects_the_prewarm_reader_suite`（parametrize `services/precip/constants.py`、`services/precip/mirror.py`；`assert Path(module).exists()`；`==` 字面五条列表）。
- [x] 2.2 同文件新增 `test_precip_route_rule_stays_without_the_prewarm_suite`（先 `assert Path("apps/api/routes/precip.py").exists()`，再 `apps/api/routes/precip.py` `==` 字面六条列表；注释写明是共享元组未改的反向护栏）。
- [x] 2.3 红→绿证据（file swap，不用 stash）与 mutant 证据按 Required evidence 产出。
- [x] 2.4 round-1 复审后补 `test_precip_tree_rule_carries_no_selection_flags`：spec delta 的「两个 flag 都不带」子句在行为面不可观测（该树之后无规则命中，加 `stop_on_match` 输出零变化），故直接钉在 rule 对象上，形状照 `test_basins_publication_helper_route_selects_exactly_eight_consumers_plus_the_rider` 的单规则 `next(...)` 先例（按用例名引用：合入前该用例的行段被本 change 自己的新增顶漂过一次）。mutant：加 `stop_on_match=True` 后只有这条结构 pin 变红、三条输出 pin 全绿。

## 3. 验证

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py tests/test_node27_mvt_prewarm.py` 全绿；`uv run ruff check .` 清洁；三条清单实测；`openspec validate --strict` 通过。
