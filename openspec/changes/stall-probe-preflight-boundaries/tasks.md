# Tasks: stall-probe-preflight-boundaries

## 约定

- **本地验证命令**：`uv run ruff check . && uv run pytest -q tests/test_node22_scheduler_stall_health.py tests/test_select_ci_tests.py`。
- **oracle**：本地 pytest + ruff + `openspec validate stall-probe-preflight-boundaries --strict --no-interactive`。node-22 实机 `--now` 回放是运维动作（下一个同步窗口），不在本地 evidence 内。
- **不改 writer**：`services/orchestrator/scheduler_runtime.py` 零改动，runbook / 探针里引用它的行号不移动。

## 1. 探针：guard-less preflight 边界全量分级（issue #2969）

- [x] 1.1 `scripts/node22_scheduler_stall_health.py`：`PREFLIGHT_BLOCKED_BOUNDARY_VERDICTS` 四边界表、三个新 verdict 与 runbook 锚点；`preflight_blocked_run`（按表的集合连续、最新一趟定处置）；第 10 档改为查表；receipt 新块 `preflight_blocked_run`，`root_blocked_run` 原样保留。
- [x] 1.2 `tests/test_node22_scheduler_stall_health.py`：j3 改写为新语义（root-exact run 被别的表内边界截断、分级 run 不截断；豁免边界与无边界旧产物两者都截断）；新增 k1–k11（表字面量、三个兄弟边界各自一小时触发 / 55 分钟不触发、`/ghdc` 翻转回放永不回 `ok`、反向翻转、`timer_stopped` / `pass_limit_blocked` 压过、压过 `submission_stalled`、`probe_failed` 时块仍写、谓词单元表、writer parity、parity 自检变红、runbook 节）。
- [x] 1.3 CI 路由：k9 parity node id 加入 `scripts/select_ci_tests_rules.py` 的 `scheduler_runtime.py` stop rule，并同步 `tests/test_select_ci_tests.py` 的三处镜像断言。

## 2. 文档与规格

- [x] 2.1 `docs/runbooks/production-ops/stuck-detection.md`：root-blocked 节改写为共用 run + 两个 receipt 块；新增 `stall-scheduler-db-free-runtime-blocked` / `stall-scheduler-rollback-fenced` / `stall-scheduler-registry-blocked` 三节；删去 "已知残留，见 #2969"。
- [x] 2.2 `scheduler-stall-alerting` spec delta（本 change），`openspec validate stall-probe-preflight-boundaries --strict --no-interactive` 通过。

## 3. 运维（不在本 PR 的 evidence 内）

- [ ] 3.1 node-22 ff-only 同步后，用 `/scratch/frd_muziyao/NWM/.venv/bin/python`（禁止 `uv sync` / 裸 `uv run`）对一份 `db_free_runtime_preflight_blocked` 产物副本做 `--now` 回放（receipt 根指到 scratch），留 receipt。

## Evidence Floor

- 三个边界各自连续超阈值时非 `ok`、exit 1，receipt 可由 `preflight_blocked_run` 独立复算：k2。
- `/ghdc` 未挂载回放：root -> `db_free_runtime_preflight_blocked` 翻转后每个 tick 都非 `ok`：k3。
- writer 源无 guard 的 `preflight_blocked` 边界集合 == 已处理 ∪ 豁免，新增边界即红：k9 + k10（另在 PR 里贴 writer 侧临时加字面量的红跑）。
- 优先级：`timer_stopped`、`pass_limit_blocked` 压过新 verdict（k4）；`scheduler_root_blocked` 行为与 `root_blocked_run` 字段不回退（j1/j2/j5–j14 不改期望值）。
- spec delta strict validate 通过；runbook 每个新 verdict 一节（k11、`test_r2`）。
- node-22 live 回放 receipt：3.1，未完成。
