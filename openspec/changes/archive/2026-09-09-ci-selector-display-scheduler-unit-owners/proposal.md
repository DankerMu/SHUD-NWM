# Proposal: ci-selector-display-scheduler-unit-owners (#2188)

## Why

三个 systemd unit 各有**唯一**的 content-asserting owner suite，却在选择器里零命中。实测（87551f41，逐个跑
`printf '<unit>' | uv run python scripts/select_ci_tests.py`，三者输出均为空 0 行）：

| unit | owner suite（读者 + 断言） |
|---|---|
| `infra/systemd/nhms-display-api.service` | `tests/test_hydro_display_mvt_scaling.py` 的 `test_systemd_workers_receive_shared_file_cache_default`：`read_text` 该路径后断言 `export NHMS_MVT_FILE_CACHE_DIR="${NHMS_MVT_FILE_CACHE_DIR:-/home/nwm/.cache/nhms/mvt}"` 与 `--workers "${NHMS_DISPLAY_WORKERS:-2}"` |
| `infra/systemd/nhms-scheduler-file-provider-refresh.service` | `tests/test_scheduler_file_provider_refresh.py` 的 `test_systemd_refresh_contract_is_db_free_daily_and_scheduler_independent`：`read_text` 该路径并断言 `ExecStart` 指向 wrapper、`TimeoutStartSec=7200`、`PrivateTmp=true` 不在其中等 |
| `infra/systemd/nhms-scheduler-file-provider-refresh.timer` | 同一用例同时 `read_text` 该 timer 并断言其调度语义 |

`.github/workflows/ci.yml` 的 backend filter 含 `infra/**`，所以 unit-only diff **会**启动定向后端 job，但选中零个文件后
降级为 `--collect-only` 冒烟（零断言执行）。被跳过的断言直接覆盖公网 display 入口的缓存/worker 契约与 node-22 每日续期 timer 的调度语义。

减轻因素（也是本条不定 high 的理由）：`count == 0` 会触发 `#1182` 的 `::warning title=Unit Tests executed 0 assertions`，
PR 页面上有提示——不像 #2180 那种 `count==1` 的静默假绿。

成因与 #2180 / #2173 同源：规则表长期是「被咬一次补一行」的反应式补丁，而这三个 unit 的读者与 unit 本体由不同 PR 在不同时间落地。

## What Changes

1. `scripts/select_ci_tests.py` `PATH_TEST_RULES` 新增三条 **path-exact** 行（无 `stop_on_match`、无 `only_when_any_changed`），
   各带 `#2188` 注释写明该 suite 真的 `read_text` 这个 unit 并断言哪几条指令：
   - `infra/systemd/nhms-display-api.service` → `("tests/test_hydro_display_mvt_scaling.py",)`，放在 node-27 unit 行之后
   - `infra/systemd/nhms-scheduler-file-provider-refresh.service` → `("tests/test_scheduler_file_provider_refresh.py",)`
   - `infra/systemd/nhms-scheduler-file-provider-refresh.timer` → `("tests/test_scheduler_file_provider_refresh.py",)`
     后两条紧贴既有的 `scripts/scheduler_file_provider_refresh_once.sh`（`:2415-2418`）与
     `scripts/install_node22_scheduler_file_provider_refresh.sh`（`:2419-2422`）两行——同一 wrapper/installer/unit 家族，目标 suite 相同。
2. `tests/test_select_ci_tests.py`：
   - `nhms-display-api.service` 接入既有 `NODE27_UNIT_OWNER_SUITES`（`:368`）。它语义上就是 node-27 的 unit（display API 跑在 node-27，
     默认缓存目录 `/home/nwm/.cache/nhms/mvt` 即该机路径）。
   - **必须同时修正该表元测试的 pin 判据**：`test_node27_unit_files_select_their_owner_suites`（`:398`）当前用
     `if unit.endswith(".service")` 断言 sibling lane pin 在选择结果里，但 pin 的来源是 `#2173` 的 glob
     `infra/systemd/nhms-node27-*.service`，而 `nhms-display-api.service` **不匹配**该 glob（实测 `fnmatch` 为 False）。
     `.endswith(".service")` 至今成立只是因为表内七行恰好全是 `nhms-node27-` 前缀。判据改为按该 glob 匹配，
     display-api 因此落进 else 分支、断言它**不**带 pin——这是真断言而非放宽：若有人把 display-api 挂到 glob 行上，它会红。
   - 新建兄弟表 `NODE22_UNIT_OWNER_SUITES: dict[str, frozenset[str]]`（两行，`.service` 与 `.timer` 各指向
     `tests/test_scheduler_file_provider_refresh.py`）+ 同形参数化元测试 `test_node22_unit_files_select_their_owner_suites`，
     断言 `owners <= selected` 且选择结果不含任何 node-27 lane pin。不写复制粘贴的函数。
   - **不**把两张表合并改名为通用 `SYSTEMD_UNIT_OWNER_SUITES`：纯改名无功能收益，且要动 PR #2187 刚落地的常量名与 docstring。

## Non-Goals

- 不动 `.github/workflows/ci.yml`（`#1182` 的 `::warning` 行为正确，本 change 不是「改告警」）。
- 不动任何 `infra/systemd/*.service` / `*.timer` 内容，不动任何被选中的 owner suite。
- **不**给 `nhms-compute-compose.service`、`nhms-display-compose.service`（全树无真实路径读者，命中只是 README 字符串断言）、
  `nhms-node27-frontier-alert.timer`、`nhms-node27-raw-retention.timer`、`nhms-scheduler-evidence-retention.timer`
  （全树无 `read_text` 读者）加行——零选择对它们是正确结果。
- 不重复已由 `:384-387` 常量覆盖的四个 node-22 unit（slurm-gateway、evidence-retention、journal-retention `.service`/`.timer`）。
- 不碰 `services/precip/**`（#2122 已合）、river_segment 写面（#2185）、raw-retention 一跳边（#2191）——同族不同表面，各自 owner。
- 不触碰 #2041 / #2146 的现网 timer 事故本身；本 change 只管 CI 路由。
- 不为 `infra/env/compute.scheduler-provider-refresh.env.example` 加行——见下方「范围外发现」，另行立单。

## Risk triage

- Fixture level: compact（三条 path-exact 规则 + 一张兄弟表与一条参数化元测试 + 一处元测试判据修正；无运行时行为变化。
  不取 `none`：`scripts/select_ci_tests.py` 是 CI 定向门的共享选择入口，与 #2173 / #2180 / #2122 同级）。
  Upstream suggested level: absent（issue 由 PR #2187 round-1 seat 2 的 pre-existing 发现经 issue-scribe 立单，无 `Suggested fixture level` 字段；`预估规模 S`）。
- Repair intensity: low。
- Risk packs 与 evidence 见 `tasks.md`。`design.md` 按 compact 级豁免。

## Must preserve

- `NODE27_UNIT_OWNER_SUITES` 既有七行与它们的 pin 断言：改判据后，七行全部匹配 `nhms-node27-*.service` glob 的仍走 pin 分支
  （五条 `.service`），两条 `.timer` 仍走 else 分支——行为逐条不变。
- `tests/test_select_ci_tests.py` 的两条通配元测试（树上派生 + 虚构 unit）、`:274-276` retention `.service` 精确相等、
  `:278-283` retention `.timer` 精确相等、`:299-302` autopipe `.timer` 精确相等、duplicate-pattern / stop-on-match 守卫、
  规则目标存在性守卫，全部照旧绿。
- `scripts/select_ci_tests.py` 既有四条 node-22 unit 规则与 `:2415-2422` 的 wrapper/installer 两行不动。

## Seams under test

- `select_tests([unit], repo_root=Path("."))` 对三个新路径的输出（各含其 owner suite），
  对 `nhms-display-api.service` 额外断言不含 node-27 lane pin，对两个 scheduler unit 同理。

## Evidence mapping

见 `tasks.md` Required evidence；本地即可闭环（issue `验收标准` 明示无需 node-22 / node-27 oracle）。

## 范围外发现（报告，不修）

`tests/test_scheduler_file_provider_refresh.py` 的同一用例还 `read_text` 了
`infra/env/compute.scheduler-provider-refresh.env.example`，而实测该路径只选中 `tests/test_two_node_docker_runtime.py`，
不含它自己的 owner suite——同一失效类的第四个实例，落在 `infra/env/**` 表面而非 `infra/systemd/**`，本 change 不修。
