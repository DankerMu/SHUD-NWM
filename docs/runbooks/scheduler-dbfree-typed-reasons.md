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
- 排队级：被 §8.6 backfill 发出的 predecessor 一律在 successor 的
  `state_evidence.predecessor_backfill.summary.records[]` 里留下一条发射
  记录——它自己被门放行时是 `status=emitted`（并进 `candidates[]`），被自己
  那道门拦下时是 `status=blocked`（并以自己的 typed reason 进
  `blocked_candidates[]`）。`status=skipped` 表示门根本没跑（见"处置"第 1 步）。

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
| §8.6 backfill 结局 | 发出的 predecessor 自己的 warm start 已就绪，被它自己那道 §8 门放行并进入本 pass 的 `candidates[]`（`status=emitted`；真实门端到端已钉在 `tests/test_scheduler_backfill_predecessor.py::test_emitted_predecessor_admitted_when_self_heal_expected`），跑完落地后缺口闭合 | 发出的 predecessor 自身**通常**再次被 gate 拦下（typed reason 视失败面而定：缺口几何下是**同一个** reason，错代/对象丢失下是 `state_snapshot_index_*` 的对应 reason），逐级后退不落地；**例外见表下"declared-cutover 边界"**——该边界上 predecessor 反而被放行 |
| 是否自愈 | 是——等 predecessor cycle 跑完即可，**前提是 §8.6 本 pass 真的把 predecessor 发出去了**（发射记录校验见表下） | 否——不做人工干预会永远 defer；**典型例外**是 declared-cutover 边界（见表下），那里 predecessor 本 pass 已被发出，等它落地即可（原则上任何让 predecessor 落在 matrix admit 分支的几何都会产生同样的保守假阳性——判据以发射记录 `status` 为准，不以分支名为准） |
| evidence 信号 | `self_heal_expected=true`、`operator_action_required=false`、`self_heal_probe={"ready": true, "reason": null}`；不带 `operator_action` / `runbook` | `self_heal_expected=false`、`operator_action_required=true`、`operator_action="backfill_predecessor_state"`、`runbook` 指向本文、`self_heal_probe.reason` 给出被判死的具体原因 |

分诊的第一跳是 `operator_action_required`，不必再自己解析 `state_history`；
两个字段是上面那次 probe 的直接派生（`self_heal_expected` 等于
`self_heal_probe.ready`，`operator_action_required` 是它的取反）。
`self_heal_probe.reason` 只作解释用，不参与判定。任何验证短缺（条目缺失、
错代、对象丢失、evidence 畸形）一律取 `false`，即倒向"需要人工介入"，
绝不倒向"会自愈"。

**declared-cutover 边界：`operator_action_required=true` 的已知假阳性（安全方向）**。
当 `required_prior_cycle_time == declaration.effective_cycle_utc`——即 T 是
cutover 之后的第一个 successor，被发出的 predecessor 正好坐在声明的生效
cycle 上——probe 仍然读 `ready=false`（那一格上确实没有当代 checkpoint，也不
该有：cutover 那一 cycle 本来就是冷启的），于是 successor 的
`operator_action_required` 为 `true`；但该 predecessor 自己那道 §8 门走的是
transition matrix 的 `cold_declared_cutover` 分支，**被放行**（其
`state_evidence.predecessor_backfill_gate.mode = db_free_cold_declared_cutover`，
successor 的发射记录为 `status=emitted`，且它进了本 pass 的 `candidates[]`）。
这是设计上的保守取值——signal 只探 warm-start 那一格，不复算 transition
matrix，倒向"需要人工介入"而不是"会自愈"。**运维影响**：这时按布尔手工调度
`required_prior_cycle_id` 会重复触发同一个本 pass 已经发出去的 cycle。因此下面
的两步分诊对 `true` 分支同样必须做完（真实门端到端已钉在
`tests/test_scheduler_backfill_predecessor.py::test_cutover_boundary_predecessor_admitted_despite_operator_action_flag`）。

**但只有这个布尔不足以停手**：它只回答"state 那一格齐不齐"，不回答"§8.6
本 pass 到底有没有把 predecessor 发出去"——两者互相独立（raw manifest 未就
绪时 §8.6 根本不发，state 却可能完好）。要按"会自愈"停手，还必须在**同一条
successor 记录**的 `state_evidence.predecessor_backfill.summary.records[]` 里
找到该 predecessor 的发射记录（按 `predecessor_cycle_time` 时间戳比对），且其
`status` ∈ {`emitted`, `blocked`}——即 §8 gate 真的对 predecessor 执行过。
注意：`predecessor_model_not_available` /
`predecessor_candidate_construction_failed` / `predecessor_gate_failed` 三类
记录**不带** `predecessor_cycle_time`（后两类连 predecessor 身份都不带）——按
时间戳比对会误读成"记录缺席"。每个 blocked successor 的 `records[]` 恒为单条，
直接读那唯一一条的 `reason` 即可；判 cap 截断前必须先确认其他 successor 的
`pass_totals.truncated` 存在。
不满足时缺口**不会**自己闭合，且要修的东西也不是 state：

- `status=skipped` 且 `reason` 为 `predecessor_raw_manifest_not_ready` /
  `predecessor_raw_manifest_env_unwired` → 修 predecessor cycle 的 raw
  manifest（或补上 `NHMS_SCHEDULER_NFS_RAW_MANIFEST_*` env 接线），不是补
  state。
- `status=skipped` 且 `reason=predecessor_model_not_available` → 该 model 本
  pass 不在 registry 可用集里，修模型可用性。
- `status=skipped` 且 `reason` 为 `predecessor_candidate_construction_failed` /
  `predecessor_gate_failed` → 是构造/门执行本身出错，按 pass 日志排查，别当
  数据缺口处置。
- `records[]` **完全为空** → 才考虑本 pass 的发射撞到 256 条上限被截断；且
  只有在其他 successor 的 `summary.pass_totals` 里确实出现 `truncated` 之后
  才能这么判（截断记录不挂在任何 successor 上），确认后先降 pending 规模或
  分批。`records[]` 里有记录但按 `predecessor_cycle_time` 比对不上，**不是**
  缺席——上面那三类无时间戳 reason 就长这样，直接读那条记录的 `reason`。
- `status=skipped` 且 `reason=predecessor_already_present` → predecessor 已在
  本 pass 的列表里：落在 `candidates[]` 是良性的（它这轮就跑），落在
  `blocked_candidates[]` 则要对**它那条记录**再走一遍本节分诊。
- `status=skipped` 且 `reason=predecessor_backfill_active_pipeline` → 上一轮
  pipeline 还在飞，良性，等下一个自然 pass。

**summarized pass 的例外**：当 pass evidence 的 `limit.candidate_lists` 为
`summarized` 或 `dropped` 时，bounded 摘要只保留 `operator_action_required`
（`scheduler_evidence_payload.py` 的
`_BOUNDED_CANDIDATE_STATE_EVIDENCE_KEYS`），`predecessor_backfill.summary`
的 records 已被丢掉。此时**光凭这个布尔不能停手**——先去找未摘要的完整证据
（journal / 未截断的 pass 日志）拿到发射记录再判。

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
  （transition 决策 `block_declaration_missing` / `block_wrong_generation`
  等，落到 `reason` 上是 `registry_cutover_declaration_missing` /
  `registry_cutover_declaration_stale` /
  `state_snapshot_index_generation_mismatch` 一类——运维要 grep 的是后者），
  它那条记录**完全没有**这组字段：这组字段只挂在本 typed reason 上。这同样
  是永久 stall 信号，别把"字段缺席"读成"会自愈"。

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
  且 `self_heal_probe.reason` 不变——**并且**同 pass 的发射记录是
  `status=blocked`：两者同时成立才是"再退一级也没用"的证据。发射记录为
  `status=emitted` 时不是 stall（典型是上面的 declared-cutover 边界，
  predecessor 已被放行、正在跑）。连续多个 pass 都是 `status=emitted` 而
  `state_history.latest_usable_state.valid_time` 始终不动 ⇒ 问题在 predecessor
  的执行/发布侧（Slurm 提交、SHUD 运行或 state 发布链路），不是 state 缺口，
  按对应链路排查而非补 state。
  **不要**去读被发出的 predecessor 那条记录来判断链是否收敛：那组字段是单级
  语义（见上节），缺口 ≥2 格时它可能显示 `self_heal_expected=true`。
- successor 的 `state_history.latest_usable_state.valid_time` 在多个 pass
  之间纹丝不动：没有任何新 checkpoint 进入 index，说明没有任何一环在推进。
  **不要**拿 `history_exists` 当 stall 判据——缺口 ≥2 格的 stall 里它一直是
  `true`；也**不要**拿 `latest_usable_state.valid_time == required_prior_cycle_time`
  当收敛判据——错代条目或对象丢失时它同样相等。可靠特征是连续 pass 上的
  `operator_action_required=true`。

反例（不要按本节处置）：如果 summary 里的记录是
`status=skipped`（`predecessor_raw_manifest_env_unwired` /
`predecessor_raw_manifest_not_ready` /
`predecessor_backfill_active_pipeline` /
`predecessor_already_present` /
`predecessor_model_not_available` /
`predecessor_candidate_construction_failed` /
`predecessor_gate_failed`），那么 §8 gate 根本没对 predecessor 执行，问题在
raw manifest / 模型可用性 / 在途 pipeline / 发射本身，不是 state 缺口——逐
reason 的处置见"处置"第 1 步的分支表。

**另一类 stall：`operator_action_required=false` 也会卡住。** state 那一格是
齐的，但 §8.6 每个 pass 都因上面那些 `skipped` 原因（典型是 raw manifest 长
期 not-ready，或 env 未接线）发不出 predecessor，于是 successor 每 pass 照样
blocked 且永不推进。识别特征：连续多个 pass 上 `operator_action_required`
恒为 `false`、`self_heal_probe.ready=true`，而 `predecessor_backfill.summary`
里同一个 `predecessor_cycle_time` 的记录始终是 `status=skipped`（或该
predecessor 始终没有记录）。这类 stall **不能**用补 state 解决。

### 处置

1. **确认群体**（两步，缺一不可）。取**被发现的 successor** 候选的
   `state_evidence`：
   - 一、读 `operator_action_required`。为 `true` **也不能直接动手**：先做
     第二步核对发射记录——`status=emitted` 说明 §8.6 本 pass 已经把 predecessor
     发出去且它被自己那道门放行了（典型是 declared-cutover 边界上的假阳性），
     此时**不要**手工调度它，等它跑完落地即可；但连续多个 pass 都是
     `status=emitted` 而 `state_history.latest_usable_state.valid_time` 始终
     不动 ⇒ 问题在 predecessor 的执行/发布侧（Slurm 提交、SHUD 运行或 state
     发布链路），不是 state 缺口，按对应链路排查而非补 state。只有
     `status=blocked`（或非 transient 的 skip）才继续进第 2 步定位缺格。为 `false`
     说明 gate 已用 predecessor 自己那道门的全量验证探测过
     （`self_heal_probe.ready=true`），被发出的 predecessor 的 warm start 已
     就绪——但先别停手。
   - 二、在同一份 `state_evidence` 的
     `predecessor_backfill.summary.records[]` 里核对该 predecessor 的发射
     记录：`status` ∈ {`emitted`, `blocked`} 才说明 §8.6 真的把它推给了 gate，
     这时才可以停手等下一个自然 pass（此时任何手工补 state 都是在制造错误
     lineage）。`status=skipped`（`predecessor_raw_manifest_not_ready` /
     `predecessor_model_not_available` 等）或 `records[]` **为空**时，缺口
     不会自己闭合，但要修的是 raw manifest / 模型可用性 / 发射上限，**不是**
     补 state——按上一节"只有这个布尔不足以停手"的分支表处置，别进第 4 步。
     空 `records[]` 只有在其他 successor 的 `summary.pass_totals` 里确认了
     `truncated` 之后才可判为 cap 截断；而按 `predecessor_cycle_time` 比对不
     上的记录是**在场但对不上**（`predecessor_model_not_available` /
     `predecessor_candidate_construction_failed` / `predecessor_gate_failed`
     不带时间戳），不是缺席——`records[]` 恒为单条，直接读它的 `reason`。
   - 若本 pass evidence 是 `limit.candidate_lists=summarized` / `dropped` 的
     摘要，records 已被丢掉，第二步做不了：先取未摘要的完整证据再判，不要只
     凭布尔停手。
2. **定位缺哪一格**。为 `true` 时读 `required_prior_cycle_id` /
   `required_prior_cycle_time` 与 `registry_cutover_transition.generation`
   —— 这三个值唯一确定了需要补的 state identity（source、cycle、
   lead_hours、generation）。
3. **看 `self_heal_probe.reason` 定性**——省掉自己翻 index 的一步。注意口径：
   它是 **provider 级**的 reason，即
   `strict_warm_start_evidence(valid_time=required_prior_cycle_time, …)` 这一
   次查找的失败原因，**不是**被发出的 predecessor 那条 blocked 记录上的
   `reason`。两者可以不同：predecessor 自己那道门要先过 §8 transition
   matrix，可能在到达 provider 之前就以别的 typed reason 拦下（例如 probe 给
   `state_snapshot_index_model_package_checksum_mismatch`，而 predecessor 落
   `blocked_candidates[]` 时带的是 `state_snapshot_index_generation_mismatch`
   或 `registry_cutover_declaration_missing`）。**不要**拿 probe 字符串去 grep
   `blocked_candidates[].reason`，那会漏。各 reason 的含义：
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
   `operator_action="backfill_predecessor_state"` 指的动作。**手工调度前再确认
   一次**：`predecessor_backfill.summary.records[]` 里没有该 predecessor 的
   `status=emitted` 记录（有就说明本 pass 已经调度过同一个 cycle，重复触发是
   多余的）。
5. **不要**用降低约束的方式"解决"它：
   - `NHMS_REQUIRE_FORECAST_WARM_START=false` **不会**放行——§8 gate 独立
     于该 env，env 只能弱化 warm-start 提示，不能承认缺失的 predecessor。
   - 不要手工往 state index 塞一个别的 cycle 的 checkpoint 冒充
     predecessor：identity key（cycle_id + lead_hours + generation）会被
     校验，塞错只会把 typed reason 换成 wrong-generation 一类，且污染
     lineage。
6. **验证收敛**。补齐后的下一个自然 pass，**successor** 的
   `operator_action_required` 应翻为 `false`（`self_heal_probe.ready=true`），
   或直接进入 warm continue 被提交；同时 `predecessor_backfill.summary` 里该
   predecessor 的记录应从 `status=blocked` 变为 `status=emitted`（它被自己那
   道门放行进了 `candidates[]`）——**两者都要看**：只翻布尔而记录仍是
   `skipped`，说明卡在发射侧而不是 state 侧。缺口原本 ≥2 格时，每补一格只前进一格，需要按上述
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
  被改判成 wrong-generation 一类 reason——它照样落在本 reason 上（只有当错代
  条目坐在 `valid_time == T`——successor 自己那一格——matrix 才判
  wrong-generation；坐在 `T − lead` 上的错代条目**永远不会**改道 successor 的
  typed reason，无论 index 里有没有别的当代 history，本节都适用）。真正兜住
  这一类的是判据本身：probe 会跑完 generation/lineage 与对象校验，
  `self_heal_probe.reason` 会点名具体失败（如
  `state_snapshot_index_model_package_checksum_mismatch`、
  `state_snapshot_index_object_missing`），`operator_action_required` 因此
  仍为 `true`。
- 跨 pass 的 no-progress circuit breaker（自动识别"连续 N 个 pass 零进展"
  并升级告警）尚未落地；在它落地之前，上面的 stall 识别特征需要人工
  按 pass evidence 判读。

## `journal_predecessor_identity_quarantine_breaker_engaged`

### 含义

§8.7 journal predecessor identity quarantine（#1107）会把"journal 记录的
`init_state_id` 与 T 的期望 predecessor token 同 base key、异 lineage 后缀"的
completed-skip 降级为 `retry_journal_predecessor_identity_mismatch` 重跑。这个
reason 表示该重跑已被证明**不收敛**：同一 cycle+model 上**已有一次 quarantine
重跑**回来仍记录同一个 stale token。计数口径 = journal 里 terminal-success
cohort **master** 行中，`journal_predecessor_quarantine_rerun_model_ids`（提交
预留时按 basin decision 落下的 provenance 戳）含本 model **且**记录了该 token
的那些；reconcile 复制到 per-model terminal 行的那份不计，所以一次 submission
的 master+terminal 算 1。首犯那一次不进计数——调用方自己那次 positive mismatch
就是它的证据——所以阈值是"带戳计数 ≥1"。**不带戳的 master 一律不计**：
`retry_terminal_run_manifest_missing` / `retry_missing_forecast_output` 这类与
§8.7 无关的白名单重提交也会重录同一个 token，把它们计进来会在第一次 quarantine
判定前就预充断路器、直接 fail-stop 掉本该重跑的那一轮。#1157 之前写的旧 journal
行没有这个字段，一律计 0（断路器保持断开）。断路器随即接管（#1157）：

- 候选侧 decision 从 retry 降为
  `blocked_journal_predecessor_identity_quarantine`（进 `blocked_candidates[]`），
  evidence 带 `journal_predecessor_identity.{recorded_init_state_id,
  expected_init_state_id, occurrences}` 与
  `retry_policy.manual_retry_required=true`；
- discovery 侧该 cycle **不再占用** source 唯一的 oldest-first backfill 执行槽
  （pass evidence 里是一条 `selection_status=not_selected` +
  `selection_reason=journal_predecessor_identity_quarantine_breaker_engaged` 的
  source-cycle 条目，带两个 token）。

该 cycle 的完成度**仍然是 gap**，绝不会因断路器变成 complete；journal 不被写入
也不被删除。

### 两步核对

1. **确认是断路器而不是普通 quarantine**。候选侧读 `blocked_candidates[].reason`：
   是本 reason 才是断路器；仍是 `journal_predecessor_identity_mismatch`
   （decision `retry_journal_predecessor_identity_mismatch`）说明还在重跑收敛
   窗口内，**不要**人工干预，等下一个自然 pass。计数读不出来（repository 无
   accessor、行不可读）时口径是 fail toward liveness——断路器保持断开、decision
   保持 retry，所以看到 retry 也可能是"数不出来"，判据以 evidence 里的
   `journal_predecessor_identity.occurrences` 是否存在为准。
   该 cycle **整个不在** `blocked_candidates[]` 里（连 candidate 都没有）时**不是**
   "断路器没生效"，而是 discovery 侧已经在本 pass 释放了执行槽：去
   `predecessor_backfill` / source-cycle 选择证据里读那条
   `selection_status=not_selected` +
   `selection_reason=journal_predecessor_identity_quarantine_breaker_engaged` 的
   条目，两个 token 在那里。两个面**每个 pass 互斥**：cycle 拿到槽才会构造候选
   （于是出现在 `blocked_candidates[]`），槽被释放就不构造候选（于是只在
   not_selected 条目里可见）。
2. **核对两个 token 的差异面**。比 `recorded_init_state_id` 与
   `expected_init_state_id`：两者 base key（source / model / `valid_time=T`）必然
   相同，差的是 lineage 后缀（`_<predecessor_cycle_id>_f<lead>`）。后缀里
   predecessor cycle 与 lead 哪一项对不上，决定了下一步查的是 cadence 配置还是
   state index 的那一格。

### 处置

1. **看 state index 有没有期望那一格**。用 `expected_init_state_id` 的后缀去
   `NHMS_SCHEDULER_STATE_INDEX` 里找 `cycle_id` + `lead_hours` 相符、
   `usable_flag=true` 的条目。**有**：说明 quarantine 重跑本该收敛（重跑会优先
   按期望 lineage 查找），却仍记录了 stale token ⇒ 问题在写侧（run manifest /
   state 记录链路），按该链路排查。**没有**：这就是断路器存在的那一类——
   期望的 predecessor state 根本不存在，重跑只能反复选中同一个 wrong-lineage
   state。
2. **补齐期望的 predecessor state**：让 `expected_init_state_id` 后缀指向的那个
   predecessor cycle 真正跑一次并把 checkpoint 发布进 state snapshot index
   （与上一节第 4 步同法——正常调度整条 chain，不要手写 index 条目）。
3. **不要**把 `blocked_journal_predecessor_identity_quarantine` 加进两处
   forced-resubmit 白名单（`_FORCE_TERMINAL_RESUBMIT_DECISIONS` /
   `force_replacement_decisions`）来"放行"：那正是断路器要防的复活，会把
   fail-stop 变回无限重跑。
4. **恢复只能靠"新的提交身份"，补状态本身不会自己再入**。断路器是 fail-stop：
   journal 里那条 completed 行既不改写也不删除，所以即使第 2 步把期望的
   predecessor state 补齐了，下一个 pass 读到的仍是同一个 stale token、同一个
   带戳计数——候选侧继续 blocked、discovery 侧继续释放执行槽，**不会**自动重跑。
   要让该 cycle+model 重新进入调度，必须在 §8.7 之外产生一次**新的 forecast 提交
   身份**（新的 run / cohort 身份，其 journal 行记录期望 token）；此后 §8.7 不再
   判定，cycle 才可能转 complete。
   **特别注意：给该行打 `manual_retry_marker` 是无效的**——
   `manual_retry_requested` 要到 `scheduler_state_decision.py:269` 才被评估，而
   terminal-success 系列 skip 在 `:220`（`terminal_hydro_success`）/
   `:235`（`terminal_completed_cycle`）/ `:257`（`terminal_pipeline_success`）
   就已返回，一条 completed 行永远走不到 manual retry 那一支；打了标记只会看到
   同一个 blocked reason 原样再来一遍。
5. **验证收敛**：新提交身份完成后的下一个自然 pass，该 cycle+model 的 journal 行
   应记录 `expected_init_state_id`，§8.7 随即不再判定，cycle 转 complete；
   `blocked_candidates[]` 条目与 backfill `not_selected` 条目同时从 pass evidence
   中消失。

## 相关文档

- [`current-production-ops.md`](current-production-ops.md) — 当前生产值守手册。
- [`failed-basin-retry.md`](failed-basin-retry.md) — 候选级 retry 预算与
  `blocked_strict_warm_start_init_state_mismatch` 的人工再入口径。
