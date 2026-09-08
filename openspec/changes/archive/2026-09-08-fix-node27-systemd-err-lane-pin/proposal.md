# Proposal: fix-node27-systemd-err-lane-pin (#2170)

## Why

master「Unit Tests (full)」自 `e0cfe40b`（PR #2151 / #2032 merge）起恒红，已跨 `5e3d2482`（PR #2164）。唯一红测是
`tests/test_node27_timeseries_retention.py::test_sibling_units_keep_their_systemd_err_lane`（:3921-3964）：它用 glob
`infra/systemd/nhms-node27-*.service` 收集兄弟单元并对「带 `StandardError=append:…/systemd.err` lane 的单元」做**集合相等**
断言（:3953-3960，六项）。#2032 新增的 `infra/systemd/nhms-node27-mvt-cache-retention.service:11` 带第七条 lane，pin 未随更新。
该 pin 抓到的正是它被写出来要抓的事（第七条 lane 无人注意地出现），故正解是扩 pin，不是弱化。

PR #2151 的定向 CI 绿着合进来，因为 `scripts/select_ci_tests.py:2490-2498`（路径字面量 :2496、target 元组 :2497；`.timer` 行 :2503）为该 unit 加的 `PathTestRule` 只选自家 suite
`tests/test_node27_mvt_cache_retention.py`；pin 是 glob 读者，按路径精确匹配的规则构造性跑不到它，只有 master 全量 run 才红。

## What Changes

1. `tests/test_node27_timeseries_retention.py:3953-3960` 集合加入 `nhms-node27-mvt-cache-retention.service`（保持 `set(...) == {…}`
   七项集合相等；:3964 的 `resource-governance` 负断言原样保留）；同函数 docstring（:3924-3936）全部数词同步：`Eight units`→nine、`Six of them`→Seven、
   `which six`→which seven、`a seventh … or a ninth`→an eighth … or a tenth、`6 + 1 + 1 = 8`→`7 + 1 + 1 = 9`，注明第七条 lane 属 #2032。
2. `scripts/select_ci_tests.py` 的 `infra/systemd/nhms-node27-mvt-cache-retention.service` 规则目标追加
   `tests/test_node27_timeseries_retention.py`（只改 `.service` 行；`.timer` 行不动——pin 的 glob 是 `*.service`）。
3. `tests/test_select_ci_tests.py` 新增一条用例：只含该 `.service` 的 diff 经 `select_tests([...], repo_root=Path("."))`
   选中两个 suite（自家 + pin 所在）且选择非空（不降级 `--collect-only`）。改规则前该用例红（只选到自家 suite）。
4. `docs/runbooks/tier-node27-timeseries-storage.md:3732-3733`「The other six node-27 units（六个名字）」是同一 lane 名单的运维副本
   （fixture review 发现；issue「无兄弟副本」结论只扫了 tests/），改为七个并加入 `mvt-cache-retention`，措辞其余不动。

## Non-Goals

- 不把集合相等弱化为子集/包含；不删 :3964 负断言。
- 不改 `nhms-node27-mvt-cache-retention.service` 本身，不动其余八个兄弟 unit 的日志 lane，不重新裁定 #2032 的 lane 设计。
- 不为其余无 `PathTestRule` 的 `infra/systemd/nhms-node27-*.service` 补规则，也不加 glob 通解（#2122 家族，另立）。
- 不改 `nhms-node27-timeseries-compression.service`（`scripts/select_ci_tests.py:2447-2456`）与 `nhms-node27-resource-governance.service`（:3198-3203）两条既有规则——它们同样不指向 pin suite，缺口同属 #2122 家族，本单只修 #2032 引入红的那一行。
- 不处理该 unit 缺 `OnFailure=` 的观察项（issue「受影响面」已记录，独立观测点）。
- 不改 `infra/systemd/nhms-node27-timeseries-retention.service:24` 注释里的「other six node-27 units」——unit 文件一律不动（部署面），
  该陈旧注释在 PR 中报告，不修。

## Risk triage

- Fixture level: compact（两处测试期望 + 一条选择规则 + 一条规则用例；无运行时行为变化。不取 `none`：改的是 CI 定向门的
  选择规则脚本 `scripts/select_ci_tests.py`，属共享工具入口）。Upstream suggested level: absent（issue 来自 issue-scribe，
  无 `Suggested fixture level` 字段）。
- Repair intensity: low。
- Risk packs 与 evidence 见 `tasks.md`。`design.md` 按 compact 级豁免。

## Must preserve

- pin 仍是七项 `set(...) == {...}` 集合相等；`nhms-node27-resource-governance.service` 负断言不动。
- `tests/test_node27_mvt_cache_retention.py` 全绿且**零改动**（它在 :1075-1090 正向锁死同一行 lane）。
- 只含该 `.service` 的 diff 仍选中 `tests/test_node27_mvt_cache_retention.py`；`.timer` 行的选择输出不变。
- `tests/test_select_ci_tests.py` 既有用例（含 mvt closure 精确集合 pin）全绿。

## Seams under test

- glob pin 本身（`infra/systemd/nhms-node27-*.service` → 带 err lane 的单元名集合）：修后本地绿，是 master 恒红的直接判据。
- `scripts/select_ci_tests.py::select_tests` 对该 unit 路径的输出（规则表是 CI 定向门的唯一选择面）；oracle 用法：
  `printf 'infra/systemd/nhms-node27-mvt-cache-retention.service\n' | uv run python scripts/select_ci_tests.py`。

## Evidence mapping

- 验收 1/2/3（pin 本地绿、七项集合相等、docstring 算术）→ tasks 1.1、2.1。
- 验收 4（mvt_cache_retention suite 全绿未改）→ tasks 2.2。
- 验收 5（选择规则 + 规则用例）→ tasks 1.2、2.3、2.4。
- 验收 6（master merge commit 全量回绿）→ tasks 3.3（merge 后核对，记入 issue）。
