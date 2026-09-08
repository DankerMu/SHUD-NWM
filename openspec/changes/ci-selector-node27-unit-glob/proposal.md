# Proposal: ci-selector-node27-unit-glob (#2173)

## Why

`tests/test_node27_timeseries_retention.py:3921` `test_sibling_units_keep_their_systemd_err_lane` 用 glob `infra/systemd/nhms-node27-*.service`
收集判据对象并做集合相等，任何 node-27 unit 的增改都会改变它的输入。`scripts/select_ci_tests.py` 里对应的规则却全是字面路径：
`:2465` timeseries-compression、`:2498` timeseries-retention、`:2508` mvt-cache-retention、`:3223` resource-governance（均为 `PathTestRule(` 起始行）；其余六个 unit
（autopipe / download / frontier-alert / raw-retention / timeseries-compression-replay / unit-failure-alert@）无任何规则。实测 10 个 unit 里只有
retention 与 mvt-cache-retention 两个 unit-only diff 触达 pin；6 个零选择降级 `--collect-only`（零断言绿灯），2 个只选自家 suite。
`#2032 → #2170` 的失效模式（PR 定向 CI 绿、master「Unit Tests (full)」在 glob pin 上红）对 8/10 现有 unit 与所有未来 unit 仍可达。
#2170 只补了一行 unit（PR #2172），并在 spec delta 里把通解显式移交本 issue。

## What Changes

1. `scripts/select_ci_tests.py`：在 `infra/systemd/nhms-node27-mvt-cache-retention.service` 规则之后新增一条通配规则
   `PathTestRule("infra/systemd/nhms-node27-*.service", ("tests/test_node27_timeseries_retention.py",))`（不设 `stop_on_match`，
   不设 `only_when_any_changed`），注释说明：pin 是 glob 读者，规则按同一 glob 对齐；规则累加（`:3387-3391` `selected.update`），
   既有 per-unit 目标一律保留。`.timer` 不在 pin 的 glob 内，不加。
2. `tests/test_select_ci_tests.py`：紧跟 `test_node27_mvt_cache_retention_unit_selects_the_sibling_lane_pin`（:305-315）新增两条用例：
   - `test_every_node27_service_unit_selects_the_sibling_lane_pin`：用 `Path("infra/systemd").glob("nhms-node27-*.service")` 从树上派生 unit 列表
     （断言非空且 ≥ 10），对每个 unit 单独 `select_tests([unit], repo_root=Path("."))`，断言非空且含 `tests/test_node27_timeseries_retention.py`。
   - `test_a_future_node27_service_unit_selects_the_sibling_lane_pin`：对不存在的 `infra/systemd/nhms-node27-brand-new.service`
     （先断言该路径不存在）做同样断言，并断言同名 `.timer` 路径 `infra/systemd/nhms-node27-brand-new.timer` 的选择结果**不含**该 suite
     （钉住 glob 只覆盖 `*.service`）。
   两条用例改规则前均红（前者在 autopipe 处零选择，后者零选择），报告贴红→绿输出。

## Non-Goals

- 不改 `tests/test_node27_timeseries_retention.py:3921` 的 pin 形态（集合相等 + `resource-governance` 负断言原样）。
- 不动任何 `infra/systemd/*.service` / `*.timer` 内容与日志 lane；不改 `.github/workflows/ci.yml` 的 `--collect-only` 降级策略（#1182）。
- 不为其余 unit 补"自家 suite"目标；不碰 `.timer` 行；不碰 `services/precip/**` 规则（#2122 自己 owner）。
- 不删或改写四条既有字面 unit 规则（它们的自家目标在通配行之外仍需保留；重复列出 retention suite 无害——集合语义）。

## Risk triage

- Fixture level: compact（一条通配规则 + 两条元测试；无运行时行为变化。不取 `none`：`scripts/select_ci_tests.py` 是 CI 定向门的共享选择入口）。
  Upstream suggested level: absent（issue 来自 PR #2172 最终复审的 issue-scribe 立单，无 `Suggested fixture level` 字段）。
- Repair intensity: low。
- Risk packs 与 evidence 见 `tasks.md`。`design.md` 按 compact 级豁免。

## Must preserve

- `tests/test_select_ci_tests.py:274-276` retention `.service` **精确相等** `== ["tests/test_node27_timeseries_retention.py"]`（通配行加的是同一文件，集合不变）；
  `:278-283` `.timer` 精确相等不变（`.timer` 不匹配 `*.service`）；`:299-302` autopipe `.timer` 精确相等不变。
- `:305-315` mvt-cache-retention 双 suite 断言；`:237` 起 resource-governance 生产者→消费者子集表。
- `scripts/select_ci_tests.py:2465` compression 规则的五个目标仍被选中——树上无既有断言钉它，唯一 oracle 是 tasks 3.3 的选择矩阵。
- `tests/test_node27_timeseries_retention.py` 零改动；`infra/systemd/**` 与 `.github/workflows/ci.yml` 零改动（`git diff --name-only` 佐证）。

## Seams under test

- `scripts/select_ci_tests.py::select_tests` 对每个 `infra/systemd/nhms-node27-*.service` 路径的输出（规则表是 CI 定向门唯一选择面）；
  oracle 用法：`printf 'infra/systemd/nhms-node27-autopipe.service\n' | uv run python scripts/select_ci_tests.py`。
- 选择器对生产者路径不 stat（`:3387` 只做 fnmatch；`_test_target_exists` 只校验测试目标 `:3441`），故虚构 unit 的断言是对规则形态的直接观测。

## Evidence mapping

- 验收 1（10 个 unit 经 CLI 均非空且含 pin suite）→ tasks 1.1、2.1、2.3、3.3（3.3 是 AC 字面所指的 CLI 矩阵）。
- 验收 2（虚构 unit 同样选中）→ tasks 2.2、2.3、3.3。
- 验收 3/4（:274-276 精确相等未弱化、:305-315 仍绿）→ tasks 2.3（既有用例零改动全绿）。
- 验收 5（pin 未改动）与验收 7（`infra/systemd/**`、`ci.yml` 零改动）→ tasks 3.1。
- 验收 6（两套 suite 全绿）→ tasks 2.3；验收 7 的 `uv run ruff check .` → tasks 3.2（全仓，不缩窄）。
