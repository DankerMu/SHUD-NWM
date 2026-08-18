# Proposal: scheduler-predecessor-pending-operator-signal

## Why

Issue #1152: 当 §8.6 predecessor-backfill 撞上 no-earlier-history 几何
（`state_snapshot_index_prior_checkpoint_missing_after_history` +
`state_history.history_exists=False`）时 gap 无法自愈——被发出的 predecessor
自身再次评估为同形状 blocked，逐级后退永不落地。这不是 bug（gate fail-closed
正确），但存在三个契约/可观测性缺口：

1. "会自愈"（单级 backfill 能闭合缺口）与"必须人工补 state"两类群体在
   blocked evidence 上不可区分。（注：issue 原文以 `history_exists` 作二分；
   round-1/round-2 审查证实该二分及其 valid_time 收紧版都会给假阴性，最终
   判据见 What Changes——`history_exists` 不是判据。）
2. emitter 该几何零真实门测试——`tests/test_scheduler_backfill_predecessor.py`
   全部用桩 gate。
3. 该 typed reason 在 `docs/` 零命中，无 runbook。

## What Changes

- **不改 §8 gate 判定行为、不改 `failure.retryable`**。在
  `scheduler_generation_gate.py` §8 路径的
  `state_snapshot_index_prior_checkpoint_missing_after_history` blocked
  evidence 上附加 additive operator signal 字段：
  - `self_heal_expected`: 当且仅当被发出的 §8.6 predecessor 自己的精确
    warm-start 验证**全量通过**——复用 provider 的
    `strict_warm_start_evidence(valid_time=required_prior_cycle_time, …)`
    要求 `ready=True`（覆盖 identity、generation/lineage、`usable_flag`、
    state 对象存在性与内容校验）。任何验证短缺（错代条目、对象丢失、条目
    缺失、evidence 畸形）→ `operator_action_required=True`（fail toward
    escalation）。另附 `self_heal_probe: {ready, reason}` 供运维看到判据依据。
    **修订史**：round-1 前为 `history_exists` 恒等（≥2 格缺口假阴性）；
    round-1 收紧为 `latest_usable_state.valid_time` 相等（generation-blind +
    对象丢失两类假阴性，round-2 双 reviewer CONFIRMED）；round-2 收紧为
    provider 全量验证。
  - `operator_action_required`: `not self_heal_expected`
  - **单级语义**：字段只回答"本候选的单级 backfill 会不会闭合"；运维分诊
    只看被发现 successor 的记录，emitted-predecessor 记录上的该字段不构成
    链式收敛证据。
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
- 不改 legacy（非 §8）路径。边界口径（与 spec delta 一致）：该路径在
  `history_exists=False` 几何下 passthrough 返回 `None`，不发 blocked
  evidence；但在 `history_exists=True` 几何下它**照样发出这个 typed
  reason**，只是那份 evidence **不带**本单新增的 signal 字段。因此
  **字段缺席 ≠ 会自愈**——运维在那里必须退回 runbook 记的人工判据
  （identity + generation/lineage + `usable_flag` + state 对象校验四件事
  做完），本单不给该站点补字段。
- 不改 emitter 的发射/去重/cap 逻辑。

## Impact

- `services/orchestrator/scheduler_generation_gate.py`（blocked evidence
  additive 字段）
- `tests/test_scheduler_backfill_predecessor.py`（env-wired 真实门测试）
- `tests/test_scheduler_generation.py`（operator signal 字段断言）
- `docs/runbooks/scheduler-dbfree-typed-reasons.md`（新建）
- `services/orchestrator/scheduler_evidence_payload.py`（bounded summarization
  保留 `operator_action_required`，round-2 C1）
- spec delta: `file-state-snapshot-index`
