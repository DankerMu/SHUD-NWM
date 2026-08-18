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
  `blocked_candidates[].reason` 与对应的 `state_evidence`。
- 排队级：被 §8.6 backfill 发出的 predecessor 会以自己的 typed reason
  落进 `blocked_candidates[]`，并在 successor 的
  `state_evidence.predecessor_backfill.summary.records[]` 里留下一条
  `status=blocked` 的发射记录。

## `state_snapshot_index_prior_checkpoint_missing_after_history`

### 含义

候选 cycle T 需要严格 warm start：它必须消费"上一 cycle（T − lead_hours）
产出的、同 generation 的 state checkpoint"。这个 typed reason 表示

1. generation-aware transition matrix 判定为 `block_predecessor_pending`，
   并且
2. state snapshot index 里**没有**位于精确 predecessor identity key 上的
   条目（"存在但不可用"是另外几个 typed reason：`usable_flag=false` 走
   `state_snapshot_index_checkpoint_unusable`，checksum / lineage 不匹配、
   对象读不出来各有自己的 reason，都在到达本 reason 之前就返回了）。

gate 因此 fail-closed：不提交、不冷启、不"就近凑一个" state。`failure`
块固定为 `retryable=True, permanent=False`——它描述的是"缺数据"，不是
"代码坏了"。

前置条件：本 typed reason 只在**非严格** warm-start 模式下出现。若
`NHMS_REQUIRE_FORECAST_WARM_START=true`（或候选 cycle 已越过
`NHMS_FORECAST_WARM_START_REQUIRED_FROM`），gate 在更早一步短路直接返回精确
warm-start 证据，reason 视具体失败而定
（`state_snapshot_index_exact_checkpoint_missing` /
`state_snapshot_index_checkpoint_unusable` /
`state_snapshot_index_object_missing` / lineage 与 checksum 不匹配一类），
下文的 operator signal 字段一个都不会出现。

对应的 evidence 形状（节选；这份是"缺口 ≥2 格"的现场——注意
`history_exists` 为 `true` 却仍然需要人工介入）：

```json
{
  "status": "blocked",
  "reason": "state_snapshot_index_prior_checkpoint_missing_after_history",
  "mode": "db_free_state_continuity",
  "required_prior_cycle_time": "2026-05-21T00:00:00Z",
  "required_prior_cycle_id": "gfs_2026052100",
  "continuity_policy": {"decision": "block_or_backfill_prior_cycle"},
  "state_history": {
    "history_exists": true,
    "latest_usable_state": {"valid_time": "2026-05-20T12:00:00Z"}
  },
  "self_heal_expected": false,
  "operator_action_required": true,
  "operator_action": "backfill_predecessor_state",
  "runbook": "docs/runbooks/scheduler-dbfree-typed-reasons.md",
  "self_heal_probe": {
    "ready": false,
    "reason": "state_snapshot_index_exact_checkpoint_missing"
  },
  "failure": {"retryable": true, "permanent": false}
}
```

### 两类群体：会自愈的 vs 不会自愈的

同一个 typed reason 覆盖两类完全不同的现场，处置相反。**判别标准是"被发出的
那个 §8.6 predecessor（cycle = T − lead_hours）自己的精确 warm-start 验证会不会
全量通过"**，不是 `history_exists`，也不是 `latest_usable_state.valid_time`
是否相等——gate 直接复用 provider 的
`strict_warm_start_evidence(valid_time=required_prior_cycle_time, …)`（即
predecessor 自己那道门要跑的同一次验证）并要求 `ready=true`，因此 identity、
generation/lineage、`usable_flag`、state 对象存在性与内容校验全部计入。
§8.6 每个 pass 只后退一级，只有被发出的那个 predecessor 自己能被放行时，单级
backfill 才能闭合缺口：

|  | 探测 `ready=true` | 探测 `ready=false` |
|---|---|---|
| 几何 | `valid_time == T − lead_hours` 上有当代、`usable_flag=true`、lineage 相符、对象在位且 checksum 正确的 checkpoint——那正是被发出的 predecessor cycle 自己的 warm-start 入参，它跑完就会产出 T 需要的那一格 | 缺口 ≥2 格（最新可用 state 早于 T − lead_hours，此时 `history_exists` 仍是 `true`）、完全没有严格早于 T 的可用 checkpoint（`history_exists=false`）、该格上坐着**旧 generation** 条目、或条目在但 state 对象丢失/读不出/checksum 不符 |
| §8.6 backfill 结局 | 发出的 predecessor 自己的 warm start 已就绪，被 gate 放行并真的跑起来，落地后缺口闭合 | 发出的 predecessor 自身再次被 gate 拦下（typed reason 视失败面而定：缺口几何下是**同一个** reason，错代/对象丢失下是 `state_snapshot_index_*` 的对应 reason），逐级后退永不落地 |
| 是否自愈 | 是——等 predecessor cycle 跑完即可 | 否——不做人工干预会永远 defer |
| evidence 信号 | `self_heal_expected=true`、`operator_action_required=false`、`self_heal_probe={"ready": true, "reason": null}`；不带 `operator_action` / `runbook` | `self_heal_expected=false`、`operator_action_required=true`、`operator_action="backfill_predecessor_state"`、`runbook` 指向本文、`self_heal_probe.reason` 给出被判死的具体原因 |

分诊只看 `operator_action_required` 一个布尔即可，不必再自己解析
`state_history`；两个字段是上面那次 probe 的直接派生（`self_heal_expected`
等于 `self_heal_probe.ready`，`operator_action_required` 是它的取反）。
`self_heal_probe.reason` 只作解释用，不参与判定。任何验证短缺（条目缺失、
错代、对象丢失、evidence 畸形）一律取 `false`，即倒向"需要人工介入"，
绝不倒向"会自愈"。

**单级语义（重要）**：这组字段只回答"**本条记录所属候选**的单级 backfill 会不
会闭合它自己的缺口"，不描述整条 backfill 链。因此：

- 运维分诊只读**被发现的 successor**（主循环 discover 出来的那个 cycle T）
  的记录。
- 被 §8.6 发出的 predecessor 落进 `blocked_candidates[]` 的那条记录同样带这组
  字段，语义同样是单级的：在缺口 ≥2 格的链里，predecessor 自己的上一格可能
  是在位的，于是**它那条记录会显示 `self_heal_expected=true`**——那只说明"再
  发一次 backfill 能救 predecessor 自己"，**不构成整条链已收敛的证据**。链是否
  卡住只看 successor 的 `operator_action_required`。
- 若被发出的 predecessor 撞上 declaration 级或 wrong-generation 一类的 block
  （`block_declaration_missing` / `block_wrong_generation` 等），它那条记录
  **完全没有**这组字段——这同样是永久 stall 信号，别把"字段缺席"读成"会自愈"。

注意：`operator_action_required=true` 不等于"系统坏了"。fail-closed 是
正确行为——它拒绝用错误 generation 或错误时刻的 state 静默启动一次
forecast。它只是说明**缺口不会自己长回来**。

### §8.6 stall 的识别特征

当 `operator_action_required=true` 的候选留在生产里，连续几个 scheduler
pass 会呈现这组稳定特征（这是"卡住"而不是"正在收敛"的判据）：

- successor cycle T 每 pass 都以该 typed reason 落 `blocked_candidates[]`，
  且从不变成 permanent failure。（`counts.submitted_count` 是**整个 pass**
  的聚合计数，不是这个候选的字段；它为 0 只说明本 pass 一个都没提交，
  别拿它当该候选的 per-candidate 证据。）
- 每个 pass 的 `state_evidence.predecessor_backfill.summary` 里都多一条
  `status=blocked` 的 predecessor 发射记录，`reason` 与 successor 完全
  相同，`predecessor_cycle_time` 与 `required_prior_cycle_time` 指向同一时刻
  （注意两者由不同 formatter 序列化，写法可能是 `+00:00` 与 `Z` 之别，
  **按时间戳比对，不要按字符串比对**）。
- successor 那条记录的 `operator_action_required` 连续多个 pass 恒为 `true`，
  且 `self_heal_probe.reason` 不变——这是"再退一级也没用"的直接证据。
  **不要**去读被发出的 predecessor 那条记录来判断链是否收敛：那组字段是单级
  语义（见上节），缺口 ≥2 格时它可能显示 `self_heal_expected=true`。
- successor 的 `state_history.latest_usable_state.valid_time` 在多个 pass
  之间纹丝不动：没有任何新 checkpoint 进入 index，说明没有任何一环在推进。
  **不要**拿 `history_exists` 当 stall 判据——缺口 ≥2 格的 stall 里它一直是
  `true`；也**不要**拿 `latest_usable_state.valid_time == required_prior_cycle_time`
  当收敛判据——错代条目或对象丢失时它同样相等。可靠特征是连续 pass 上的
  `operator_action_required=true`。

反例（不要按本节处置）：如果 summary 里的记录是
`status=skipped` 且 `reason` 为
`predecessor_raw_manifest_env_unwired` /
`predecessor_raw_manifest_not_ready` /
`predecessor_backfill_active_pipeline` /
`predecessor_already_present`，那么 §8 gate 根本没对 predecessor 执行，
问题在 raw manifest 或在途 pipeline，不是 state 缺口。

### 处置

1. **确认群体**。取**被发现的 successor** 候选的 `state_evidence`，读
   `operator_action_required`。为 `false` 就停手——它意味着 gate 已用
   predecessor 自己那道门的全量验证探测过（`self_heal_probe.ready=true`），
   被发出的 predecessor 的 warm start 已就绪，等下一个自然 pass 它跑完缺口
   即自闭；此时任何手工补 state 都是在制造错误 lineage。
2. **定位缺哪一格**。为 `true` 时读 `required_prior_cycle_id` /
   `required_prior_cycle_time` 与 `registry_cutover_transition.generation`
   —— 这三个值唯一确定了需要补的 state identity（source、cycle、
   lead_hours、generation）。
3. **看 `self_heal_probe.reason` 定性**——它就是 predecessor 那道门给出的
   typed 失败原因，省掉自己翻 index 的一步：
   `state_snapshot_index_exact_checkpoint_missing` = 该 identity 压根不在
   index 里（走第 4 步补 state）；`*_model_package_checksum_mismatch` /
   `*_cycle_id_mismatch` / `*_lead_hours_mismatch` = 该格坐着**错代或错
   lineage** 的条目；`*_checkpoint_unusable` = 条目在但 `usable_flag=false`；
   `state_snapshot_index_object_missing` / `*_object_unreadable` /
   `*_object_checksum_mismatch` = index 条目在但 state 对象丢了/坏了。
   后三类不是"缺一格"，别用第 4 步的补 state 方式绕过——先修 index 条目或
   对象本身。必要时对照 `NHMS_SCHEDULER_STATE_INDEX` 指向的 state snapshot
   index 复核。
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
6. **验证收敛**。补齐后的下一个自然 pass，**successor** 的
   `operator_action_required` 应翻为 `false`（`self_heal_probe.ready=true`），
   或直接进入 warm continue 被提交；`predecessor_backfill.summary` 不再新增
   `status=blocked` 记录。缺口原本 ≥2 格时，每补一格只前进一格，需要按上述
   判据逐 pass 复核直到 successor 的 `operator_action_required` 翻转——中途
   predecessor 记录上出现的 `self_heal_expected=true` 不算收敛（单级语义）。

### 边界说明

- 上述 operator signal 字段只出现在 §8 路径发出的 blocked evidence 上
  （即带 `registry_cutover_transition` 的那份）。pre-§8 的 legacy 路径
  （state index 不可信、无 declaration 且候选无 `package_checksum` 时走
  的那条）在 `history_exists=false` 几何下返回 passthrough，根本不发这份
  blocked evidence，所以那里没有也不需要这组字段。
- 但 legacy 路径在 `history_exists=true` 时**同样会发出这个 typed
  reason**，且那份 evidence 里没有 operator signal 字段。**字段缺席不等于
  "会自愈"**：遇到不带 `registry_cutover_transition` / 不带
  `operator_action_required` 的该 reason，退回人工判据——去 index 里核
  `required_prior_cycle_time` 那一格，且必须把 §8 probe 做的四件事都做完：
  identity（cycle_id + lead_hours）、`model_package_checksum` 属于当代
  generation、`usable_flag=true`、state 对象存在且 checksum 相符。只比
  `latest_usable_state.valid_time` 是不够的。
- 判据不只看 `valid_time`：该格上坐着**错误 generation** 的 checkpoint 时，
  successor 自己的精确查找发生在 `valid_time == T`（不是该格），因此**并不会**
  被改判成 wrong-generation 一类 reason——它照样落在本 reason 上。真正兜住
  这一类的是判据本身：probe 会跑完 generation/lineage 与对象校验，
  `self_heal_probe.reason` 会点名具体失败（如
  `state_snapshot_index_model_package_checksum_mismatch`、
  `state_snapshot_index_object_missing`），`operator_action_required` 因此
  仍为 `true`。
- 跨 pass 的 no-progress circuit breaker（自动识别"连续 N 个 pass 零进展"
  并升级告警）尚未落地；在它落地之前，上面的 stall 识别特征需要人工
  按 pass evidence 判读。

## 相关文档

- [`current-production-ops.md`](current-production-ops.md) — 当前生产值守手册。
- [`failed-basin-retry.md`](failed-basin-retry.md) — 候选级 retry 预算与
  `blocked_strict_warm_start_init_state_mismatch` 的人工再入口径。
