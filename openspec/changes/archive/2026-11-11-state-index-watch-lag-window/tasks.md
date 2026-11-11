# Tasks: state-index-watch-lag-window (#3118)

Fixture level: expanded
Must preserve：见 design.md "Must preserve"。

## Risk packs
- Config / project setup — selected：lag 由 env 读入并推导窗口（1.4 的 lag 边界表）
- Error handling / rollback / partial outputs — selected：未设置 / 非整数 / 负数的 lag 仍然 exit 2，reason 和 field 精确保持；不新增 exit 4（1.4）
- Documentation / migration notes — selected：runbook 8.12 的两处同步（1.5）
- Legacy compatibility / examples — selected：现有断言（watch :281-282、:472，retention :1067-1069）保持不变，WARNING 仍含 8.12 指引（1.4）
- Schema / Release — selected：receipt 顶层新增 `retention_days_source`，`schema_version` 保持 v1（1.4 的 receipt 断言）
- Public API / CLI / script entry — not selected：不改 reader/publisher 的签名，也不改 CLI 参数；指纹在内部计算（design.md §3）
- Concurrency / shared state / ordering — not selected：watch 只做 dry-run，不加锁；enforce 的锁序不变
- Auth / permissions / secrets — not selected：不涉及凭据；不泄露路径由 1.4 的 WARNING 用例覆盖
- File IO / path safety — not selected：receipt 根的校验不变
- Resource limits — not selected：硬上限、告警阈值和 evidence 结构都不变

## 1. 实现
- [x] 1.1 `state_manager`：新增 `state_index_retention_floor_hours` 和 `minimum_state_index_retention_days`，`_state_index_retention_window` 改为调用它们（design.md §1）
- [x] 1.2 watch 推导窗口，receipt 顶层写 `retention_days_source`，传给 repair 的 `cycle_lag_hours` 仍为 `None`（design.md §2）
- [x] 1.3 WARNING 加 `index_fp` 指纹：纯字符串哈希，只在 warning 分支里计算，不碰文件系统；对外暴露 `state_index_log_label`；三个调用点改为传原始 uri；evidence 的 `index` 不变（design.md §3）。同时追踪 private 和 destination 两个 uri 的确切字符串形式；完成 private lane 覆盖调查（design.md §4），结论交给 orchestrator 写进 PR
- [x] 1.4 测试：design.md "Required evidence" 中的全部用例，每个新行为先红后绿
- [x] 1.5 runbook `docs/runbooks/production-ops/known-issues-state-index.md` §8.12：
  - 约 :483 的 WARNING 引文改为新格式，附上 design.md §3 的指纹命令（调用 `state_index_log_label`，node-22 精确解释器），并写明 private 和 destination 两个 uri 的确切形式；
  - "定时 capacity watch" 段：窗口推导公式和边界表、`retention_days_source`；
  - 约 :553 `capacity_warning_unprunable` 的处置：`retention_days_source=default` 时才升级 #2541；`derived_from_cycle_lag` 时说明是 lag 撑大了窗口，等 lag 回落后再评估，必要时按推导出的天数人工 enforce；
  - "窗口下限"段：给出人工 enforce 最小天数的算法（`floor_hours // 24 + 1`）；
  - private lane 的覆盖结论。

## 2. 上线（合并后）
- [x] 2.1 node-22 先查 `git status --porcelain`，再 `git pull --ff-only`（禁止 `uv sync` 和裸 `uv run`）
- [x] 2.2 记下 `compute.scheduler-dbfree.env` 里当时的 `NHMS_SCHEDULER_CYCLE_LAG_HOURS`，然后 `systemctl --user start nhms-scheduler-state-index-capacity.service`：
  - lag 294：receipt 中 `retention_days=29`，`retention_days_source=derived_from_cycle_lag`；
  - lag 回到 16：`21`，`default`；
  - 两种情况 exit 都是 0 或 1，不能是 2。
  - lag 值和 receipt 摘要贴到 #3118。

## Evidence Floor
- `uv run pytest -q tests/test_node22_state_index_capacity_watch.py tests/test_state_index_retention.py tests/test_scheduler_state_index_repair.py`，加上 selector 选出的测试集
- `uv run ruff check .`
- 2.2 的 node-22 receipt（附 lag 值）

## 上线记录（2026-11-11）
- 2.1：node-22 部署到 `f487b30ae`。
- 2.2：当时 lag 294。watch receipt 为 `healthy` / exit 0 / `retention_days_source=derived_from_cycle_lag`，两条 lane 都是 `retention_days=29`、utilization 0.696659，证据见 #3118 的评论。
