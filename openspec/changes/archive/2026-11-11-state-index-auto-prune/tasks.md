# Tasks: state-index-auto-prune (#3121)

Fixture level: expanded
Must preserve：见 design.md。

## Risk packs
- Concurrency / shared state / ordering — selected：冻结顺序、两次 pgrep、闸门复查、排队 job、pgrep fail-closed、预闸门（evidence 2/3/8/12/17/18/19）
- Error handling / rollback / partial outputs — selected：所有失败路径都恢复 timer、exit 3 partial、exit 5 restore 失败、SIGTERM、enforce 期间延迟信号、收尾时忽略二次信号（evidence 4–7/9–11/15/16）
- Config / project setup — selected：lag 推导复用 #3118 的 helper，env 注入（evidence 2）
- Public API / CLI / script entry — selected：新 unit 与模块入口（evidence 14）
- Documentation / migration notes — selected：runbook 8.12 和 `stuck-detection.md`（1.5）
- Resource limits — selected：5h 等待上限、`TimeoutStartSec` 大于等待上限、`TimeoutStopSec` 预算、60s 子进程超时、8h dwell 约束（evidence 4/14/17）
- Legacy compatibility / examples — selected：watch、wrapper、人工流程不变（不改它们的文件；现有测试保持绿）
- Auth / permissions / secrets — not selected：不加载 gateway secret；receipt 根 owner-private 由 evidence 13 覆盖
- File IO / path safety — selected：receipt 根的 owner-private 校验（evidence 13）
- Schema / Release — selected：新 receipt schema v1（evidence 13）

## 1. 实现
- [x] 1.1 `scripts/node22_state_index_auto_prune.py`：按 design.md 的流程（1–7）实现，lag 解析和窗口推导与 watch 共用
- [x] 1.2 `infra/systemd/nhms-scheduler-state-index-prune.service` / `.timer`
- [x] 1.3 把新 unit 登记进 design.md "Sibling surfaces" 列出的全部清单：MANIFEST、entrypoint invariant、unit_tracking、unit_failure_alert（六改七）、python_scan、select_ci_tests_rules 及其测试
- [x] 1.4 `tests/test_node22_state_index_auto_prune.py`：design.md Required evidence 1–19
- [x] 1.5 runbook：
  - 8.12：Stage 2 改为自动；安装/启用；退出码表；人工流程作兜底，含 exit 3 之后、以及 SIGKILL 打断 enforce 之后的人工处置命令；`enforced_unprunable` 的处置按 `retention_days_source` 分流；clone 不要与自动剪枝同时跑。
  - `stuck-detection.md` 的 `timer_stopped` 处置：自动剪枝持有 timer 时属预期。

## 2. 上线（合并后）
- [x] 2.1 node-22：先 `git status --porcelain`，再 `git pull --ff-only`（禁止 `uv sync` 和裸 `uv run`）
- [x] 2.2 `install -d -m 0700 /scratch/frd_muziyao/nhms-prod/workspace/state-index-repair/auto-prune`；复制 unit/timer；`daemon-reload`；`enable --now` timer；再跑 `scripts/node22_unit_drift_check.py`（精确解释器）确认没有漂移
- [x] 2.3 手动 `systemctl --user start nhms-scheduler-state-index-prune.service` 跑一次。预期：
  - 当时利用率低于 0.70 → `action=skip`，exit 0；
  - 否则 → `action=enforced`，exit 0；
  - 或者 `action=enforced_unprunable`，exit 1。这在 lag 294 时可能出现，因为 29 天的稳态就接近 0.70。按 runbook 视为 lag 撑大了窗口，等 lag 回落后再评估。
  
  跑完两个 timer 都应为 active。把 receipt 摘要贴到 #3121。

## Evidence Floor
- `uv run pytest -q tests/test_node22_state_index_auto_prune.py tests/test_node22_entrypoint_invariant.py tests/test_node22_unit_tracking.py tests/test_node22_unit_failure_alert.py tests/test_node22_entrypoint_invariant_python_scan.py tests/test_select_ci_tests.py tests/test_node22_state_index_capacity_watch.py`，加上 selector 选出的测试集
- `uv run ruff check .`
- 2.3 的 node-22 receipt

## 上线记录（2026-11-11）
- 2.1：node-22 部署到 `c73b4ade5`。
- 2.2：unit/timer 已安装并启用；drift check 显示新增的两个文件为 `same`。部署前就存在的 drift（6 个旧 oneshot 未接告警、模板缺失）不属于本 change。
- 2.3：首跑结果为 `enforced_unprunable`，exit 1：29 天窗口，每条 lane 删 206 条，剪完 0.7004；两个 timer 都已恢复。证据见 #3121 的评论。
