# Tasks: ci-selector-display-scheduler-unit-owners (#2188)

Fixture level: compact
Change surface:
- `scripts/select_ci_tests.py`：三条 path-exact `PathTestRule`（display-api 一条放在 node-27 unit 行之后；两条 scheduler-refresh 放在 `:2415-2422` 的 wrapper/installer 行之后、另起注释块）
- `tests/test_select_ci_tests.py`：`NODE27_UNIT_OWNER_SUITES` 增 display-api 行；`test_node27_unit_files_select_their_owner_suites` 的 pin 判据由 `.endswith(".service")` 改为按 `infra/systemd/nhms-node27-*.service` glob 匹配；新增 `NODE22_UNIT_OWNER_SUITES`（2 行）与同形参数化元测试
Must preserve:
- `NODE27_UNIT_OWNER_SUITES` 既有 7 行的 pin/无 pin 分支归属逐条不变（5 条 `.service` 仍走 pin 分支，2 条 `.timer` 仍走 else）
- `:274-276`、`:278-283`、`:299-302` 三处精确相等；两条通配元测试；duplicate-pattern / stop-on-match / 规则目标存在性守卫；既有四条 node-22 unit 规则与 `:2415-2422` 两行
- `.github/workflows/ci.yml`、`infra/systemd/**`、任何 owner suite 本体零改动
Must add/change:
- 三条 path-exact 规则 + `#2188` 注释；一行表项；一处判据修正；一张兄弟表 + 一条参数化元测试
Seams under test:
- `select_tests([unit], repo_root=Path("."))` 对三个新路径：各含自家 owner suite，且均不含 node-27 lane pin（三者都不在 `#2173` 的 glob 内）
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门的选择入口；两张表驱动元测试钉住三个路径的 owner 子集与 pin 归属，既有规则用例全绿
- File IO / path safety / overwrite: not selected - 只读规则表，无写入
- Schema / columns / units / field names: not selected - 无数据格式
- Legacy compatibility / examples: not selected - unit / timer 文件不动
- Other packs: not selected - 无 auth/并发/发布/迁移面
Required evidence:
- `uv run pytest -q tests/test_select_ci_tests.py -k "owner_suites"`：改规则前 display-api 与两个 node-22 参数化实例全红（红由 `owners <= selected` 触发：三者零选择），改后全绿；红→绿输出贴 PR body
- 判据修正的承重证据：把 display-api 行**保留**而把判据改回 `.endswith(".service")`，该实例必须红（display-api 不在 `#2173` glob 内、拿不到 pin）——证明新判据不是放宽而是收紧；输出贴 PR body
- 三条清单实测贴 PR body：三个 unit 各自 `printf '<unit>' | uv run python scripts/select_ci_tests.py` 的输出（各含自家 owner suite、均无 lane pin）
- `uv run pytest -q tests/test_select_ci_tests.py tests/test_hydro_display_mvt_scaling.py tests/test_scheduler_file_provider_refresh.py` 全绿
- `uv run ruff check .`；`openspec validate ci-selector-display-scheduler-unit-owners --strict --no-interactive`；`git diff --name-only origin/master` 只含 `scripts/select_ci_tests.py`、`tests/test_select_ci_tests.py`、`openspec/**`
Non-goals:
- 不改 ci.yml、不动 unit 文件与 owner suite 本体、不给 5 个「无读者」unit 加行、不重复 4 个已覆盖的 node-22 unit、不合并改名两张表、不修 `infra/env/**` 的同类缺口（另行立单）

## 1. 选择规则

- [x] 1.1 `scripts/select_ci_tests.py` 新增 `infra/systemd/nhms-display-api.service` → `("tests/test_hydro_display_mvt_scaling.py",)`，注释写明读者用例名与它断言的两条指令（`NHMS_MVT_FILE_CACHE_DIR` 默认值、`--workers` 默认值），并写明该 unit 不在 `#2173` 的 `nhms-node27-*.service` glob 内故不带 lane pin。
- [x] 1.2 新增 `infra/systemd/nhms-scheduler-file-provider-refresh.service` 与 `.timer` 两条，目标同为 `("tests/test_scheduler_file_provider_refresh.py",)`，放在既有 wrapper/installer 两行**之后**（`:2422` 之下，**不要插在两行之间**——`:2409-2414` 的 `#1138` 注释块自述作用域是 shell wrapper 与 `grep -rln '*.sh' tests/` 派生的目标，unit 文件不属其列），另起 `#2188` 注释块写明读者用例名与被断言的指令。

## 2. 元测试

- [x] 2.1 `NODE27_UNIT_OWNER_SUITES` 增 `infra/systemd/nhms-display-api.service` 行。
- [x] 2.2 把 `test_node27_unit_files_select_their_owner_suites` 的 pin 判据从 `unit.endswith(".service")` 改为按 `infra/systemd/nhms-node27-*.service` 的 `fnmatch` 匹配，注释说明 `.endswith` 至今成立只因表内七行恰为同一前缀，display-api 是第一个反例。
- [x] 2.3 新增 `NODE22_UNIT_OWNER_SUITES` 与 `test_node22_unit_files_select_their_owner_suites`（同形 parametrize，`owners <= selected` + 不含 node-27 lane pin）。
- [x] 2.4 红→绿与判据回退变异证据按 Required evidence 产出。

## 3. 验证

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py tests/test_hydro_display_mvt_scaling.py tests/test_scheduler_file_provider_refresh.py` 全绿；`uv run ruff check .` 清洁；三条清单实测；`openspec validate --strict` 通过。
