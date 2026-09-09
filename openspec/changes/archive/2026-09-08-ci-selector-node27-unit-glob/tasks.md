Fixture level: compact
Change surface:
- `scripts/select_ci_tests.py` `PATH_TEST_RULES`：在 `infra/systemd/nhms-node27-mvt-cache-retention.service` 的 `PathTestRule(...)`（:2508-2523，即在 :2523 的 `),` 之后、`.timer` 行之前）新增一条通配行 `infra/systemd/nhms-node27-*.service` → `tests/test_node27_timeseries_retention.py`
- `tests/test_select_ci_tests.py`：在 `test_node27_mvt_cache_retention_unit_selects_the_sibling_lane_pin`（:305-315）之后新增两条用例（树上 glob 派生 + 虚构 unit）
Must preserve:
- `:274-276` retention `.service` 精确相等、`:278-283` `.timer` 精确相等、`:299-302` autopipe `.timer` 精确相等、`:305-315` mvt 双 suite、`:237` resource-governance 子集表；四条既有字面 unit 规则不删不改；`tests/test_node27_timeseries_retention.py`、`infra/systemd/**`、`.github/workflows/ci.yml` 零改动
Must add/change:
- 一条通配 `PathTestRule`（无 `stop_on_match`、无 `only_when_any_changed`）+ 注释；两条元测试
Seams under test:
- `select_tests([unit], repo_root=Path("."))` 对每个 `nhms-node27-*.service`（含虚构名）的输出；同名 `.timer` 不被通配行覆盖
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门的选择入口；元测试从树上派生 unit 列表并覆盖未来 unit，既有规则用例全绿
- File IO / path safety / overwrite: not selected - 只读规则表与 unit 文件名，无写入
- Schema / columns / units / field names: not selected - 无数据格式
- Legacy compatibility / examples: not selected - unit 文件与模板不动
- Other packs: not selected - 无 auth/并发/发布/迁移面
Required evidence:
- `uv run pytest -q tests/test_select_ci_tests.py -k "node27_service_unit"`（改规则前红：autopipe 零选择 / brand-new 零选择；改后绿），红→绿输出贴 PR body
- `uv run pytest -q tests/test_select_ci_tests.py tests/test_node27_timeseries_retention.py` 全绿
- 对 10 个 unit 逐个 `printf '<unit>\n' | uv run python scripts/select_ci_tests.py`，输出均含 `tests/test_node27_timeseries_retention.py`；`nhms-node27-brand-new.service` 同样
- `uv run ruff check .`；`openspec validate ci-selector-node27-unit-glob --strict --no-interactive`；`git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/**`
Non-goals:
- 不改 pin 形态、不动 unit / timer 文件、不改 ci.yml 降级策略、不补自家 suite、不碰 `.timer` 行与 `services/precip/**`（#2122）、不删改既有字面 unit 规则

## 1. Implementation

- [x] 1.1 `scripts/select_ci_tests.py`：在 mvt-cache-retention `.service` 行之后新增 `PathTestRule("infra/systemd/nhms-node27-*.service", ("tests/test_node27_timeseries_retention.py",))`，注释注明 #2173：pin 是 `nhms-node27-*.service` 的 glob 读者，规则按同一 glob 对齐，规则累加不替换既有 per-unit 目标，未来新 unit 自动覆盖；`.timer` 不在 pin glob 内故不加

## 2. Tests

- [x] 2.1 `tests/test_select_ci_tests.py` 新增 `test_every_node27_service_unit_selects_the_sibling_lane_pin`：`sorted(Path("infra/systemd").glob("nhms-node27-*.service"))` 非空且 `>= 10`；逐个 `select_tests([str(unit)], repo_root=Path("."))` 非空且含 `tests/test_node27_timeseries_retention.py`；失败信息带 unit 名
- [x] 2.2 新增 `test_a_future_node27_service_unit_selects_the_sibling_lane_pin`：`Path("infra/systemd/nhms-node27-brand-new.service").exists()` 为 False；`select_tests([该路径])` 非空且含 pin suite；`select_tests(["infra/systemd/nhms-node27-brand-new.timer"])` 不含 pin suite
- [x] 2.3 红→绿：改规则前 `uv run pytest -q tests/test_select_ci_tests.py -k "node27_service_unit"` 两条均红（贴输出）；改后 `uv run pytest -q tests/test_select_ci_tests.py tests/test_node27_timeseries_retention.py` 全绿（既有 :274 / :278 / :299 / :305 / :237 用例零改动）

## 3. Verification

- [x] 3.1 `git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/changes/ci-selector-node27-unit-glob/**`；`tests/test_node27_timeseries_retention.py` 未改
- [x] 3.2 `uv run ruff check .`；`openspec validate ci-selector-node27-unit-glob --strict --no-interactive`
- [x] 3.3 10 个 unit + brand-new 的 `printf ... | uv run python scripts/select_ci_tests.py` 输出表贴 PR body（每行含 pin suite；compression 行仍含原五目标；retention `.service` 行仍恰为单一 pin suite）

## Evidence Floor

2.3 红→绿 + 既有用例零改动全绿 + 3.1 diff 面 + 3.2 本地 + 3.3 选择矩阵；本 PR 不改运行时，无 node-27 receipt 项。
