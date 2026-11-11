# Proposal: state-index capacity watch 按 cycle lag 推导 retention 窗口，capacity WARNING 能分清是哪份 index（#3118）

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent。fixture review 指出三个 expanded 触发条件：
- 1.1 重构的 `_state_index_retention_window` 是 enforce 删除路径的闸门；
- receipt 新增字段属于格式变更；
- WARNING 文本是 runbook 引用的观测契约。
Blast radius:
- floor helper 重构若改变了边界，`prune-retention --enforce` 可能接受过短的窗口，误删 warm-start 仍需要的 index 条目（只从 index 移除，state 对象不删，但会让 warm-start 找不到 state）；
- watch 推导错误：告警继续失明，或者 `capacity_after` 数字失真；
- WARNING 格式变化：运维按日志 grep 的习惯要跟着 runbook 更新；
- 日志和 evidence 不能出现路径。
Selected risk packs: Config / project setup；Error handling / rollback / partial outputs；Documentation / migration notes；Legacy compatibility / examples；Schema / Release（receipt 新增字段）
Evidence floor: `uv run pytest -q tests/test_node22_state_index_capacity_watch.py tests/test_state_index_retention.py tests/test_scheduler_state_index_repair.py` + selector 选集 + `uv run ruff check .`；node-22 手动触发一次 watch 的 receipt（按当时的 lag 判定预期，见 tasks 2.2）

## Why

追赶期间 node-22 的 `NHMS_SCHEDULER_CYCLE_LAG_HOURS=294`。`_state_index_retention_window` 要求 `retention_days*24 > 336 + lag + 48`（即 678h），但 watch 写死使用默认的 21 天（504h），所以从 2026-11-09 起每天都以 exit 2 `repair_retention_window_too_short` 失败，容量告警完全失明。现有 watch 在 lag ≥ 120 时就会拒绝（issue 里写的 "lag > 120" 差了一）。

2026-11-11 人工 dry-run（29 天）显示 reference（private）lane 已到 0.8407，离硬上限约 2–3 天。这个数字从没出现在调度日志里：日志里的 capacity WARNING 用的是脱敏标签 `[local-path]`，两份 index 无法区分，而且只出现过约 77% 的那一份。

## What Changes

- `state_manager` 暴露 floor 和最小天数的唯一 helper，`_state_index_retention_window` 改为调用它，边界行为不变。
- watch 读取 lag 并推导窗口：`max(21, minimum_days)`；lag 不合法时保持原来的行为（用 21 天交给 repair，由 repair 拒绝）。receipt 顶层新增 `retention_days_source`。
- capacity WARNING 日志行在原有脱敏标签之外，追加该 index 位置的不可逆短指纹（`index_fp=<8 hex>`），用来区分两份 index；不改任何函数签名。runbook 给出计算两份 index 指纹的命令。
- 查清 private（reference）lane 的 index 在正常调度中是否会经过容量评估，结论写进 PR 和 runbook。
- runbook 8.12 同步：窗口推导、人工 enforce 最小天数的算法、`retention_days_source`、WARNING 新格式，以及 `capacity_warning_unprunable` 的处置按 `retention_days_source` 分流。

## Non-goals

- 不改 `_state_index_retention_window` 的拒绝条件和错误码，不改 repair CLI 的 `--retention-days` 默认值，不改 watch 的退出码表和 `schema_version`。
- 不改 evidence、publish 结果里的 `index` 值和 `capacity` 的结构（pass evidence 的字节预算本来就紧）。
- 不给 reader/publisher 加 role 参数（指纹方案不需要）。
- 不实现自动 enforce（#2653 Stage 2 决策保持人工）。
- reference 与 destination 的 json_nodes 差异（252205 vs 227897，条目数相同）只查原因、记录在 PR，不在本 change 修。
