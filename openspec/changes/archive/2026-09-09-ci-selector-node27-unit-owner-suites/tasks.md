Fixture level: compact
Change surface:
- `scripts/select_ci_tests.py` `PATH_TEST_RULES`：通配行 `infra/systemd/nhms-node27-*.service`（:2524-2538）之后新增五条 path-exact `.service` 行；mvt-cache-retention `.timer` 行（:2539-2545）之后新增两条 `.timer` 行
- `tests/test_select_ci_tests.py`：在 `test_a_future_node27_service_unit_selects_the_sibling_lane_pin`（:341-361）之后新增一条 `@pytest.mark.parametrize` 表驱动用例 `test_node27_unit_files_select_their_owner_suites`
Must preserve:
- `:274-276` retention `.service` 精确相等、`:278-283` retention `.timer` 精确相等、`:299-302` autopipe `.timer` 精确相等、`:305-315` mvt 双 suite、`:318-361` 两条通配元测试（含 `.timer` 负断言）、`:237` resource-governance 子集表；`tests/test_node27_cold_residency.py:371`；通配行与四条既有字面 unit 规则不删不改；`tests/test_node27_timeseries_retention.py`、`infra/systemd/**`、`.github/workflows/ci.yml` 零改动
Must add/change:
- 七条 path-exact `PathTestRule`（无 `stop_on_match`、无 `only_when_any_changed`）+ `#2180` 注释；一条表驱动元测试
Seams under test:
- `select_tests([path], repo_root=Path("."))` 对五个 `.service` 与两个 `.timer` 路径的输出：`.service` 行为自家 suite ∪ pin suite，`.timer` 行为自家 suite 且不含 pin suite
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门的选择入口；表驱动元测试钉住七个路径的 owner 子集，既有规则用例全绿
- File IO / path safety / overwrite: not selected - 只读规则表，无写入
- Schema / columns / units / field names: not selected - 无数据格式
- Legacy compatibility / examples: not selected - unit / timer 文件不动
- Other packs: not selected - 无 auth/并发/发布/迁移面
Required evidence:
- `uv run pytest -q tests/test_select_ci_tests.py -k "owner_suites"`（改规则前七个参数化实例全红，红由 `owner_suites <= selected` 触发：五个 `.service` 缺自家 suite、两个 `.timer` 零选择；pin-containment / pin-absence 子句改前即绿，不计入红证据；改后七个全绿），红→绿输出贴 PR body
- `uv run pytest -q tests/test_select_ci_tests.py tests/test_node27_cold_residency.py` 全绿（:274-276 精确相等与 :371 成员断言未回归）
- `uv run pytest -q tests/test_node27_autopipeline_preflight.py tests/test_node27_download_cycles.py tests/test_node27_frontier_stall_alert.py tests/test_node27_raw_retention.py tests/test_node27_timeseries_compression_supervisor.py tests/test_node27_timeseries_compression.py` 全绿
- 七个路径逐个 `printf '<path>\n' | uv run python scripts/select_ci_tests.py` 的选择矩阵贴 PR body（`.service` 行含自家 suite 与 pin；`.timer` 行含自家 suite、不含 pin；retention `.service` 仍恰为单一 pin suite）
- `uv run ruff check .`；`openspec validate ci-selector-node27-unit-owner-suites --strict --no-interactive`；`git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/**`
Non-goals:
- 不改 pin 形态与通配行、不动 unit / timer 文件、不改 ci.yml（#1182）、不碰 `services/precip/**`（#2122）与写面扫描（#2185）、不给 unit-failure-alert@ / frontier-alert.timer / raw-retention.timer 加行、不把 suite 追加到通配行

## 1. Implementation

- [x] 1.1 `scripts/select_ci_tests.py`：通配行之后新增五条 `.service` path-exact 行（autopipe → preflight；download → download_cycles；frontier-alert → frontier_stall_alert；raw-retention → raw_retention；timeseries-compression-replay → compression + compression_supervisor），每条 `#2180` 注释写明该 suite 真读该 unit 并断言的指令（`ExecStart`/`Environment`/`EnvironmentFile`/`ExecStartPre` 行序等），pin 由通配行累加、行内不重复
- [x] 1.2 `scripts/select_ci_tests.py`：mvt-cache-retention `.timer` 行之后新增两条 `.timer` 行（download.timer → download_cycles，注释 `OnUnitActiveSec=30min`；timeseries-compression.timer → cold_residency + compression，注释 `OnCalendar=*-*-* 04:25:00 UTC` 与 `Unit=` 断言），注释注明 live_evidence / capture 只复制字节不断言、故非目标

## 2. Tests

- [x] 2.1 `tests/test_select_ci_tests.py` 新增 `test_node27_unit_files_select_their_owner_suites`：模块级一张 `{unit_path: {owner suites}}` 表（五个 `.service` + 两个 `.timer`），`@pytest.mark.parametrize` 逐行展开为七个实例（`ids` 用 unit 文件名）；每个实例 `select_tests([unit], repo_root=Path("."))`，断言 `owner_suites <= selected`（失败信息带 unit 名与缺失 suite）；`.service` 行额外断言含 `tests/test_node27_timeseries_retention.py`，`.timer` 行额外断言不含它；docstring 引 #2180 与 pin 只断言 lane 集合的事实
- [x] 2.2 红→绿：改规则前 `uv run pytest -q tests/test_select_ci_tests.py -k "owner_suites"` 七个实例全红（贴输出，每个实例须能看到缺失的 suite 名）；改后七个全绿
- [x] 2.3 `uv run pytest -q tests/test_select_ci_tests.py tests/test_node27_cold_residency.py` 全绿（既有 :274 / :278 / :299 / :305 / :318 / :341 / :237 用例零改动）
- [x] 2.4 `uv run pytest -q tests/test_node27_autopipeline_preflight.py tests/test_node27_download_cycles.py tests/test_node27_frontier_stall_alert.py tests/test_node27_raw_retention.py tests/test_node27_timeseries_compression_supervisor.py tests/test_node27_timeseries_compression.py` 全绿

## 3. Verification

- [x] 3.1 `git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/changes/ci-selector-node27-unit-owner-suites/**`；`tests/test_node27_timeseries_retention.py`、`infra/systemd/**`、`.github/workflows/ci.yml` 未改
- [x] 3.2 `uv run ruff check .`；`openspec validate ci-selector-node27-unit-owner-suites --strict --no-interactive`
- [x] 3.3 七个路径 + retention `.service` 的 `printf ... | uv run python scripts/select_ci_tests.py` 选择矩阵贴 PR body

## Evidence Floor

2.2 红→绿 + 2.3/2.4 既有用例零改动全绿 + 3.1 diff 面 + 3.2 本地 + 3.3 选择矩阵；本 PR 不改运行时，无 node-27 receipt 项。
