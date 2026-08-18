# Proposal: scheduler-predecessor-pending-operator-signal

## Why

Issue #1152: 当 §8.6 predecessor-backfill 撞上 no-earlier-history 几何
（`state_snapshot_index_prior_checkpoint_missing_after_history` +
`state_history.history_exists=False`）时 gap 无法自愈——被发出的 predecessor
自身再次评估为同形状 blocked，逐级后退永不落地。这不是 bug（gate fail-closed
正确），但存在三个契约/可观测性缺口：

1. `history_exists=False`（必须人工补 state）与 `history_exists=True`
   （等 predecessor 落地即自愈）在 blocked evidence 上不可区分。
2. emitter 该几何零真实门测试——`tests/test_scheduler_backfill_predecessor.py`
   全部用桩 gate。
3. 该 typed reason 在 `docs/` 零命中，无 runbook。

## What Changes

- **不改 §8 gate 判定行为、不改 `failure.retryable`**。在
  `scheduler_generation_gate.py` §8 路径的
  `state_snapshot_index_prior_checkpoint_missing_after_history` blocked
  evidence 上附加 additive operator signal 字段：
  - `self_heal_expected`: 当且仅当
    `state_history.latest_usable_state.valid_time == required_prior_cycle_time`
    （被发出的 §8.6 predecessor 自己的精确 warm-start state 存在，单级
    backfill 能闭合缺口）。**Round-1 修订**：最初的 `history_exists` 恒等
    派生在 ≥2 格缺口几何下给假阴性（reviewer CONFIRMED），已收紧。
  - `operator_action_required`: `not self_heal_expected`
  - `operator_action_required=True` 时附
    `operator_action: "backfill_predecessor_state"` 与
    `runbook: "docs/runbooks/scheduler-dbfree-typed-reasons.md"`（字面值钉死，
    与本单新建 runbook 文件同源）。
- 新增 env-wired 集成测试（`NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true`）
  覆盖 `emit_predecessor_candidates` 跑真实 §8 gate 的 no-earlier-history
  几何：被发出的 predecessor 自身 blocked 同形状且携带
  `operator_action_required=True`。
- 新增 runbook `docs/runbooks/scheduler-dbfree-typed-reasons.md`，收录该
  typed reason 的含义、两类群体、处置方式与 §8.6 stall 识别特征。

## Non-Goals

- 不实现 #1118 的跨 pass no-progress circuit breaker（机制未落地，本单为
  该 typed reason 的 evidence 形状 + 测试 + runbook；#1118 落地时应消费本单
  新增的 `operator_action_required` 字段而非另起判据）。
- 不改 legacy（非 §8）路径——该路径在 `history_exists=False` 时 passthrough
  返回 None，不发 blocked evidence，无需信号。
- 不改 emitter 的发射/去重/cap 逻辑。

## Impact

- `services/orchestrator/scheduler_generation_gate.py`（blocked evidence
  additive 字段）
- `tests/test_scheduler_backfill_predecessor.py`（env-wired 真实门测试）
- `tests/test_scheduler_generation.py`（operator signal 字段断言）
- `docs/runbooks/scheduler-dbfree-typed-reasons.md`（新建）
- spec delta: `file-state-snapshot-index`
