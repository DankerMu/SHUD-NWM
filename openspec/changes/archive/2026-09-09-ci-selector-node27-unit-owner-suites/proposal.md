# Proposal: ci-selector-node27-unit-owner-suites (#2180)

## Why

PR #2179 (#2173) 的通配行 `scripts/select_ci_tests.py:2536` 让每个 `infra/systemd/nhms-node27-*.service` 都选中兄弟 lane pin
`tests/test_node27_timeseries_retention.py`，但对 autopipe / download / frontier-alert / raw-retention / timeseries-compression-replay
五个 unit 这是**唯一**选择结果（实测 `printf '<unit>\n' | uv run python scripts/select_ci_tests.py` 五者各只输出该 pin suite）。
pin（`:3921` `test_sibling_units_keep_their_systemd_err_lane`）只断言 `StandardError=append:…systemd.err` 的 lane 集合；真正 `read_text`
这五个 unit 并断言其指令的自家 suite 全部不在选择结果里：

| unit | 自家 suite（读者 + 断言） |
|---|---|
| `nhms-node27-autopipe.service` | `tests/test_node27_autopipeline_preflight.py:19` + `:1111-1119`（`scripts/node27_autopipe_cron.sh`、`NODE27_AUTOPIPE_BOOTSTRAP_LOG=…`、`infra/env/display.env` not in） |
| `nhms-node27-download.service` | `tests/test_node27_download_cycles.py:18` + `:481-495`（`scripts/node27_download_once.sh` in service） |
| `nhms-node27-frontier-alert.service` | `tests/test_node27_frontier_stall_alert.py:1427` + `:1553-1562`（`Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1`、`EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env`、`TimeoutStartSec=900`） |
| `nhms-node27-raw-retention.service` | `tests/test_node27_raw_retention.py:317-322` + `:325-341`（`ExecStartPre=/usr/bin/mkdir -p …`、`StandardOutput=append:…`、`ExecStartPre` 先于 `ExecStart`） |
| `nhms-node27-timeseries-compression-replay.service` | `tests/test_node27_timeseries_compression_supervisor.py:1233-1238`（`EnvironmentFile=/home/nwm/NWM/infra/env/node27-timeseries-compression-replay.env`）与 `tests/test_node27_timeseries_compression.py:31` + `:1964-1976`（`--run-plan-path`/`--ledger-path`/`--receipt-path`/`--wall-seconds 900`/`ExecStopPost=` + `--finalize-only`/`TimeoutStartSec=920`） |

改错 `download.service` 的 `ExecStart`、删掉 frontier-alert 的 `Environment=` 行、把 raw-retention 的 `ExecStartPre` 挪到 `ExecStart` 之后，
只要 lane 行不动，定向 CI 全绿合入，唯一会红的是 merge 后 master 的 "Unit Tests (full)"（#2032 → #2170 失效模式在「自家 suite」维度的残留）。
而且 #2179 让 `count` 从 0 变 1 后 #1182 的零断言告警不再触发（这是正确行为——断言确实跑了），PR 页面上再无任何提示。

同形 `.timer` 缺口（issue 可选项，本 change 一并做，零新增 suite）：`infra/systemd/nhms-node27-download.timer` 与
`nhms-node27-timeseries-compression.timer` 实测**零选择**（不匹配任何规则，CI 降级 `--collect-only`），但确有内容断言读者：
`tests/test_node27_download_cycles.py:19` + `:485/:496`（`OnUnitActiveSec=30min`）；`tests/test_node27_timeseries_compression.py:32` + `:1982-1985`
（`OnCalendar=*-*-* 04:25:00 UTC`、`Unit=nhms-node27-timeseries-compression.service`）与 `tests/test_node27_cold_residency.py:118-119`
（同一 `OnCalendar`）。`tests/test_node27_timeseries_compression_live_evidence.py:712-716` 与 `_capture.py:134-139` 只 `read_bytes` 复制该 timer
作 fixture、不断言内容，故不是目标。`frontier-alert.timer` / `raw-retention.timer` 全树无 `read_text` 读者，零选择是正确的，不补。

## What Changes

1. `scripts/select_ci_tests.py` `PATH_TEST_RULES`：
   - 在通配行 `infra/systemd/nhms-node27-*.service`（:2524-2538）的 `),`（:2538）之后、mvt-cache-retention `.timer` 行（:2539）之前，新增五条
     **path-exact** `PathTestRule`（无 `stop_on_match`、无 `only_when_any_changed`），目标为各自自家 suite（replay 行两个目标）：
     - `infra/systemd/nhms-node27-autopipe.service` → `("tests/test_node27_autopipeline_preflight.py",)`
     - `infra/systemd/nhms-node27-download.service` → `("tests/test_node27_download_cycles.py",)`
     - `infra/systemd/nhms-node27-frontier-alert.service` → `("tests/test_node27_frontier_stall_alert.py",)`
     - `infra/systemd/nhms-node27-raw-retention.service` → `("tests/test_node27_raw_retention.py",)`
     - `infra/systemd/nhms-node27-timeseries-compression-replay.service` → `("tests/test_node27_timeseries_compression.py", "tests/test_node27_timeseries_compression_supervisor.py")`
     每条带 `#2180` 注释，写明该 suite 真的 `read_text` 这个 unit 并断言哪几条指令（沿用 `:2483-2491` 的注释纪律）；pin suite 由通配行累加提供，
     行内不重复列出。
   - 在 mvt-cache-retention `.timer` 行（:2539-2545）的 `),` 之后新增两条 `.timer` 行：
     - `infra/systemd/nhms-node27-download.timer` → `("tests/test_node27_download_cycles.py",)`
     - `infra/systemd/nhms-node27-timeseries-compression.timer` → `("tests/test_node27_cold_residency.py", "tests/test_node27_timeseries_compression.py")`
2. `tests/test_select_ci_tests.py`：紧跟 `test_a_future_node27_service_unit_selects_the_sibling_lane_pin`（:341-361）之后、
   `test_select_tests_keeps_new_node27_cold_tablespace_consumers_self_selecting`（:363）之前新增**一条**参数化表驱动用例
   `test_node27_unit_files_select_their_owner_suites`：模块级一张 `{unit_path: {owner suites}}` 表（五个 `.service` + 两个 `.timer`，共七行），
   形状对齐 `:237` 起的 resource-governance 生产者→消费者子集表，但以 `@pytest.mark.parametrize` 逐行展开（issue 原文即「参数化 owner 表」；
   单函数循环会在第一行断言失败处停住，红跑输出只能显示一个 unit）；每个参数化实例 `select_tests([unit], repo_root=Path("."))`，
   断言 `owner_suites <= selected`，失败信息带 unit 名与缺失 suite；对五个 `.service` 行额外断言 `"tests/test_node27_timeseries_retention.py" in selected`（pin 由通配行累加，
   本 PR 不得让它掉）；对两个 `.timer` 行额外断言 `"tests/test_node27_timeseries_retention.py" not in selected`（`.timer` 不在 pin glob 内，
   钉住新行没有把 pin 误挂到 timer 上）。不写七份复制粘贴的函数。
   改规则前七个参数化实例全红，承重的红只来自 `owner_suites <= selected`（五个 `.service` 缺自家 suite、两个 `.timer` 零选择）；
   pin-containment（通配行已提供）与 pin-absence（timer 基线零选择）两个子句改前即绿，是回归护栏而非红→绿证据。报告贴红→绿输出。

## Non-Goals

- 不改 `.github/workflows/ci.yml`（#1182 的 `::warning` / step summary 行为是正确的，本 change 不是「恢复告警」）。
- 不动 `tests/test_node27_timeseries_retention.py:3921` 的 pin 形态，不动 PR #2179 的通配行（:2524-2538）与它的两条元测试（:318-361）。
- 不动任何 `infra/systemd/*.service` / `*.timer` 内容；不改任何被选中的自家 suite。
- 不碰 `services/precip/**` 规则（#2122）；不给 `nhms-node27-unit-failure-alert@.service` 加行（唯一读者 `tests/test_node27_timeseries_retention.py:2832`
  已被通配行选中）；不给 `frontier-alert.timer` / `raw-retention.timer` 加行（无内容读者）；不为 `river_segment` 写面扫描补行（#2185）。
- 不把六个 suite 追加到通配行——已验证会红：retention `.service` 也匹配该 glob，`tests/test_select_ci_tests.py:274-276` 的精确相等立即失败，
  且每个 unit 会拖上无关 suite。

## Risk triage

- Fixture level: compact（七条 path-exact 规则 + 一条表驱动元测试；无运行时行为变化。不取 `none`：`scripts/select_ci_tests.py` 是 CI 定向门的
  共享选择入口，与 #2173 同级）。Upstream suggested level: absent（issue 来自 PR #2179 round-1 CONFIRMED/P2/DEFER 的 issue-scribe 立单，
  无 `Suggested fixture level` 字段）。
- Repair intensity: low。
- Risk packs 与 evidence 见 `tasks.md`。`design.md` 按 compact 级豁免。

## Must preserve

- `tests/test_select_ci_tests.py:274-276` retention `.service` **精确相等** `== ["tests/test_node27_timeseries_retention.py"]`（新行不匹配该路径）；
  `:278-283` retention `.timer` 精确相等；`:299-302` autopipe `.timer` 精确相等 `== [preflight, mvt_prewarm]`（本 change 不动 autopipe `.timer` 行）；
  `:305-315` mvt-cache-retention 双 suite；`:318-339` 树上派生 + `:341-361` 虚构 unit 两条通配元测试（`.timer` 负断言仍成立：新 `.timer` 行的目标
  不含 pin suite）；`:237` 起 resource-governance 子集表。
- `tests/test_node27_cold_residency.py:371` compression `.service` 成员断言；`scripts/select_ci_tests.py:2465-2474` compression `.service` 五目标不变。
- `tests/test_node27_timeseries_retention.py`、`infra/systemd/**`、`.github/workflows/ci.yml` 零改动（`git diff --name-only origin/master` 佐证）。

## Seams under test

- `scripts/select_ci_tests.py::select_tests` 对七个 `infra/systemd/nhms-node27-*` 路径的输出（规则表是 CI 定向门唯一选择面；`:3402-3403`
  fnmatch + `selected.update` 累加，无 `stop_on_match` 时 path-exact 行与通配行的目标并集）。
  oracle 用法：`printf 'infra/systemd/nhms-node27-download.service\n' | uv run python scripts/select_ci_tests.py`。

## Evidence mapping

- 验收 1（autopipe / download / frontier-alert / raw-retention 各含自家 suite + pin）→ tasks 1.1、2.1、2.2、3.3。
- 验收 2（replay 含 supervisor + compression + pin）→ tasks 1.1、2.1、2.2、3.3。
- 验收 3（owner 表元测试加规则前红、后绿，PR 贴 red-before）→ tasks 2.2。
- 验收 4（`tests/test_select_ci_tests.py` + `tests/test_node27_cold_residency.py` 绿，:274-276 与 :371 未回归）→ tasks 2.3。
- 验收 5（六个自家 suite 全绿）→ tasks 2.4。
- 验收 6（`ci.yml`、pin suite、`infra/systemd/**` 零改动）→ tasks 3.1。
- 验收 7（可选 `.timer` 项）→ tasks 1.2、2.1、3.3（本 change 采纳，不作偏离）。
- `uv run ruff check .` → tasks 3.2（全仓，不缩窄）。
