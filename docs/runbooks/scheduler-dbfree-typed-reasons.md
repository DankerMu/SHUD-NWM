# Scheduler DB-Free Typed Reasons Runbook

适用范围：node-22 上以 DB-free 模式运行的 production scheduler
（`NHMS_SCHEDULER_DB_FREE_REQUIRED=true`，state / registry / journal /
state-index 全部走 file backend）。node-22 不连任何活 DB，所有判定证据只
存在于 pass evidence JSON 与 file state-index 里，因此 typed reason 是唯一
可靠的分诊入口。

本文按 typed reason 组织：每节写清"这个字符串是什么意思"、"哪几类现场会
产生它"、"证据里怎么把它们区分开"、"该怎么处置"。判定行为本身由 §8 gate
（`services/orchestrator/scheduler_generation_gate.py`）决定，本文不改变
任何判定，只解释它。

typed reason 出现在两个位置，含义相同：

- 候选级：pass evidence 的 `candidates[].reason` /
  `blocked[].reason` 与对应的 `state_evidence`。
- 排队级：被 §8.6 backfill 发出的 predecessor 会以自己的 typed reason
  落进 `blocked[]`，并在 successor 的
  `state_evidence.predecessor_backfill.summary.records[]` 里留下一条
  `status=blocked` 的发射记录。

## `state_snapshot_index_prior_checkpoint_missing_after_history`

### 含义

候选 cycle T 需要严格 warm start：它必须消费"上一 cycle（T − lead_hours）
产出的、同 generation 的 state checkpoint"。这个 typed reason 表示

1. generation-aware transition matrix 判定为 `block_predecessor_pending`
   （或 warm-continue 但对象校验没过），并且
2. state snapshot index 里**没有**位于精确 predecessor identity key 上的
   可用 checkpoint。

gate 因此 fail-closed：不提交、不冷启、不"就近凑一个" state。`failure`
块固定为 `retryable=True, permanent=False`——它描述的是"缺数据"，不是
"代码坏了"。

对应的 evidence 形状（节选）：

```json
{
  "status": "blocked",
  "reason": "state_snapshot_index_prior_checkpoint_missing_after_history",
  "mode": "db_free_state_continuity",
  "required_prior_cycle_time": "2026-05-21T00:00:00Z",
  "required_prior_cycle_id": "gfs_2026052100",
  "continuity_policy": {"decision": "block_or_backfill_prior_cycle"},
  "state_history": {"history_exists": false},
  "self_heal_expected": false,
  "operator_action_required": true,
  "operator_action": "backfill_predecessor_state",
  "runbook": "docs/runbooks/scheduler-dbfree-typed-reasons.md",
  "failure": {"retryable": true, "permanent": false}
}
```

### 两类群体：会自愈的 vs 不会自愈的

同一个 typed reason 覆盖两类完全不同的现场，处置相反：

|  | `state_history.history_exists = true` | `state_history.history_exists = false` |
|---|---|---|
| 几何 | state index 里存在严格早于 T 的同 generation 可用 checkpoint，只是缺 T − lead_hours 那一格 | state index 里**没有任何**严格早于 T 的可用 checkpoint（当代条目全部晚于 T，或压根没有） |
| §8.6 backfill 结局 | 发出的 predecessor 通常能被 gate 放行并真的跑起来，落地后缺口闭合 | 发出的 predecessor 自身再次评估为**同一个** typed reason（它的 predecessor 同样不存在），逐级后退永不落地 |
| 是否自愈 | 是——等 predecessor cycle 跑完即可 | 否——不做人工干预会永远 defer |
| evidence 信号 | `self_heal_expected=true`、`operator_action_required=false`；不带 `operator_action` / `runbook` | `self_heal_expected=false`、`operator_action_required=true`、`operator_action="backfill_predecessor_state"`、`runbook` 指向本文 |

分诊只看 `operator_action_required` 一个布尔即可，不必再自己解析
`state_history`；两个字段是 `state_history.history_exists` 的直接派生
（`self_heal_expected` 等于它，`operator_action_required` 是它的取反）。

注意：`operator_action_required=true` 不等于"系统坏了"。fail-closed 是
正确行为——它拒绝用错误 generation 或错误时刻的 state 静默启动一次
forecast。它只是说明**缺口不会自己长回来**。

### §8.6 stall 的识别特征

当 `operator_action_required=true` 的候选留在生产里，连续几个 scheduler
pass 会呈现这组稳定特征（这是"卡住"而不是"正在收敛"的判据）：

- successor cycle T 每 pass 都以该 typed reason 落 `blocked[]`，
  `submitted_count` 对它恒为 0，且从不变成 permanent failure。
- 每个 pass 的 `state_evidence.predecessor_backfill.summary` 里都多一条
  `status=blocked` 的 predecessor 发射记录，`reason` 与 successor 完全
  相同，`predecessor_cycle_time` 恒等于 `required_prior_cycle_time`。
- 被发出的 predecessor 自己那条 `blocked[]` 记录同样带
  `operator_action_required=true`——这是"再退一级也没用"的直接证据。
- `state_history.history_exists` 在多个 pass 之间保持 `false`：没有任何
  新 checkpoint 进入 index，说明没有任何一环在推进。

反例（不要按本节处置）：如果 summary 里的记录是
`status=skipped` 且 `reason` 为
`predecessor_raw_manifest_env_unwired` /
`predecessor_raw_manifest_not_ready` /
`predecessor_backfill_active_pipeline` /
`predecessor_already_present`，那么 §8 gate 根本没对 predecessor 执行，
问题在 raw manifest 或在途 pipeline，不是 state 缺口。

### 处置

1. **确认群体**。取该候选的 `state_evidence`，读
   `operator_action_required`。为 `false` 就停手——等下一个自然 pass，
   predecessor 落地后缺口自闭；此时任何手工补 state 都是在制造错误 lineage。
2. **定位缺哪一格**。为 `true` 时读 `required_prior_cycle_id` /
   `required_prior_cycle_time` 与 `registry_cutover_transition.generation`
   —— 这三个值唯一确定了需要补的 state identity（source、cycle、
   lead_hours、generation）。
3. **确认 index 里确实没有它**，而不是有但不可用：检查
   `NHMS_SCHEDULER_STATE_INDEX` 指向的 state snapshot index，看该
   identity 是否存在、`usable_flag` 是否为 `true`、
   `model_package_checksum` 是否属于当代 generation。若条目存在但
   `usable_flag=false` 或 checksum 属于旧 generation，那是另一类问题
   （wrong-generation / 不可用 checkpoint），不要用本节的补 state 方式绕过。
4. **补齐 predecessor state**。让 `required_prior_cycle_id` 那个 cycle 真
   正跑一次并把产出的 state checkpoint 发布进 state snapshot index
   （正常途径是调度该 cycle 的完整 chain，而不是手写 index 条目）。这是
   `operator_action="backfill_predecessor_state"` 指的动作。
5. **不要**用降低约束的方式"解决"它：
   - `NHMS_REQUIRE_FORECAST_WARM_START=false` **不会**放行——§8 gate 独立
     于该 env，env 只能弱化 warm-start 提示，不能承认缺失的 predecessor。
   - 不要手工往 state index 塞一个别的 cycle 的 checkpoint 冒充
     predecessor：identity key（cycle_id + lead_hours + generation）会被
     校验，塞错只会把 typed reason 换成 wrong-generation 一类，且污染
     lineage。
6. **验证收敛**。补齐后的下一个自然 pass，successor 的
   `state_history.history_exists` 应翻为 `true`，或直接进入 warm continue
   被提交；`predecessor_backfill.summary` 不再新增 `status=blocked` 记录。

### 边界说明

- 上述 operator signal 字段只出现在 §8 路径发出的 blocked evidence 上
  （即带 `registry_cutover_transition` 的那份）。pre-§8 的 legacy 路径
  （state index 不可信、无 declaration 且候选无 `package_checksum` 时走
  的那条）在 `history_exists=false` 几何下返回 passthrough，根本不发这份
  blocked evidence，所以那里没有也不需要这组字段。
- 跨 pass 的 no-progress circuit breaker（自动识别"连续 N 个 pass 零进展"
  并升级告警）尚未落地；在它落地之前，上面的 stall 识别特征需要人工
  按 pass evidence 判读。

## 相关文档

- [`current-production-ops.md`](current-production-ops.md) — 当前生产值守手册。
- [`failed-basin-retry.md`](failed-basin-retry.md) — 候选级 retry 预算与
  `blocked_strict_warm_start_init_state_mismatch` 的人工再入口径。
