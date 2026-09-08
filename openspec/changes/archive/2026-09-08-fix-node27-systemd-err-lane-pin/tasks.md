Fixture level: compact
Change surface:
- `docs/runbooks/tier-node27-timeseries-storage.md:3732-3733`（lane 名单运维副本，六→七）
- `tests/test_node27_timeseries_retention.py::test_sibling_units_keep_their_systemd_err_lane`（:3921-3964：docstring :3924-3936 五处数词 + 集合 :3953-3960）
- `scripts/select_ci_tests.py` `PATH_TEST_RULES` 中 `infra/systemd/nhms-node27-mvt-cache-retention.service` 一行（:2490-2498，target 元组 :2497；`.timer` 行 :2503 不动）
- `tests/test_select_ci_tests.py`（新增一条 systemd 规则用例，形态照 :1753 `test_node22_systemd_units_select_the_owner_without_collect_only`）
Must preserve:
- 集合相等形态与 :3964 负断言；`tests/test_node27_mvt_cache_retention.py` 零改动全绿；`.service` 规则仍选自家 suite；`.timer` 规则不动
Must add/change:
- 集合第七项 `nhms-node27-mvt-cache-retention.service`；docstring 9 兄弟 / `7 + 1 + 1 = 9` 并注明 #2032；规则目标追加 pin suite；规则用例
Seams under test:
- glob pin（unit 文件集合 → err lane 名集合）；`select_tests([unit], repo_root=Path("."))` 对该 `.service` 的输出
Risk packs:
- Public API / CLI / script entry: selected - `scripts/select_ci_tests.py` 是 CI 定向门的选择入口；用例钉住该 unit 的选择输出含两个 suite 且非空，既有规则用例全绿
- File IO / path safety / overwrite: not selected - 只读 unit 文件与规则表，无写入
- Schema / columns / units / field names: not selected - 无数据格式
- Legacy compatibility / examples: not selected - 无兼容面；unit 文件与模板不动
- Other packs: not selected - 无 auth/并发/发布/迁移面
Required evidence:
- `uv run pytest -q tests/test_node27_timeseries_retention.py -k sibling_units`（改前红：Extra `nhms-node27-mvt-cache-retention.service`；改后绿）
- `uv run pytest -q tests/test_node27_timeseries_retention.py tests/test_node27_mvt_cache_retention.py tests/test_select_ci_tests.py`
- `printf 'infra/systemd/nhms-node27-mvt-cache-retention.service\n' | uv run python scripts/select_ci_tests.py` 输出含两个 suite
- `uv run ruff check scripts tests`；`npx --yes markdownlint-cli2 docs/runbooks/tier-node27-timeseries-storage.md`；`openspec validate fix-node27-systemd-err-lane-pin --strict --no-interactive`
Non-goals:
- 不弱化集合相等、不删负断言、不改任何 unit 文件（含 `nhms-node27-timeseries-retention.service:24` 的「other six」陈旧注释，只报告）、不加 `infra/systemd/**` glob 通解（#2122 家族）、不动 timeseries-compression / resource-governance 两条既有 unit 规则（同缺口，顺延 #2122）、不处理 `OnFailure=` 观察项

## 1. Implementation

- [x] 1.1 `tests/test_node27_timeseries_retention.py:3953-3960` 集合加入 `nhms-node27-mvt-cache-retention.service`（保持 `==`）；docstring :3924-3936 全部数词同步：`Eight units`→nine、`Six of them`→Seven、`which six`→which seven、`a seventh … or a ninth`→an eighth … or a tenth、`6 + 1 + 1 = 8`→`7 + 1 + 1 = 9`；
      写明第七条 append lane 由 #2032 的 mvt-cache-retention unit 带入（有意保留，被 `tests/test_node27_mvt_cache_retention.py` 正向锁定）；:3964 负断言原样
- [x] 1.2 `scripts/select_ci_tests.py` 该 `.service` 规则目标改为 `("tests/test_node27_mvt_cache_retention.py", "tests/test_node27_timeseries_retention.py")`，注释补一句：pin 是 glob 读者，unit 路径精确规则若不指向它，PR 定向 CI 构造性跑不到（#2170）；`.timer` 行不动
- [x] 1.3 `docs/runbooks/tier-node27-timeseries-storage.md:3732-3733`：「The other six node-27 units (…)」改为 seven 并在括号名单加入 `mvt-cache-retention`（保持字母序），其余措辞不动；`npx --yes markdownlint-cli2 docs/runbooks/tier-node27-timeseries-storage.md` 0 issues

## 2. Tests

- [x] 2.1 `uv run pytest -q tests/test_node27_timeseries_retention.py -k sibling_units` 绿（改前红证 = issue 复现输出，贴入报告）
- [x] 2.2 `uv run pytest -q tests/test_node27_mvt_cache_retention.py` 绿，`git status` 显示该文件与 `infra/systemd/**` 未改
- [x] 2.3 `tests/test_select_ci_tests.py` 新增 `test_node27_mvt_cache_retention_unit_selects_the_sibling_lane_pin`：`select_tests(["infra/systemd/nhms-node27-mvt-cache-retention.service"], repo_root=Path("."))` 的集合 ⊇ {两个 suite} 且非空；改规则前跑一次证红（只含自家 suite）
- [x] 2.4 `uv run pytest -q tests/test_select_ci_tests.py` 全绿（含 mvt closure 精确集合 pin 与 systemd 既有用例）；oracle 用法 `printf '...service\n' | uv run python scripts/select_ci_tests.py` 输出含两个 suite

## 3. Verification

- [x] 3.1 `uv run ruff check scripts tests`
- [x] 3.2 `openspec validate fix-node27-systemd-err-lane-pin --strict --no-interactive`
- [x] 3.3 merge 后：master merge commit 的「Unit Tests (full)」0 failed（`gh run view`），记入 #2170；若仍红且失败项不是本 pin，作为独立观察上报——已核：merge commit 45d74ac8 的 run 34258294408「Unit Tests (full)」success，18021 passed / 0 failed / 16 skipped

## Evidence Floor

2.1 红→绿 + 2.2 零改动 + 2.3 红→绿 + 2.4 全绿 + 3.1/3.2 本地；3.3 为 merge 后核对项（本 PR 不改运行时，无 node-27 receipt 项）。
