# Design: cycle-pin-live-failure-domain

## Context

PR #1286 (#1205) 新立的 cycle-scope 钉值规则有两臂：arm 1（`failed_stage` 同
stage 显式修复）与 arm 2（cycle 失败是候选唯一 repair target）。arm 2 由
`_state_has_candidate_scope_failed_job` 承载，其失败域直接复用了
`FAILED_PIPELINE_STATUSES`，未对齐同文件 blocker 扫描的更宽域。master 上无此规则
（一律钉），PR #1286 只修好了 `failed` 系；`cancelled` / hydro 两类残留旧行为。

## Goals / Non-Goals

- Goal: arm 2 的"活失败"域与 blocker 域的失败半边同宽；两个消费臂由构造同域；
  spec 措辞与实现一致。
- Non-goal: 改动 `_cycle_scope_marker_pins_attempt:210` 状态臂对 marker 目标行
  自身 cancelled 的判定（独立缺口，本 issue 边界外）；改动 blocker 扫描；改动
  adoption（刀 1）语义。

## Decisions

### D1: 共享谓词，不复制常量

抽一个行级"live failure"判据供 arm 2 使用，语义 =
`_manual_retry_blocking_pipeline_status` 的失败半边
（`FAILED_PIPELINE_STATUSES ∪ {"cancelled"}`，**不含** ACTIVE）：直接在
`_state_has_candidate_scope_failed_job` 内改判 status 的分支即可（该函数已是唯一
承载点，两个消费者都调它），不为一行判断新增顶层 helper —— 但判断本身必须写成
与 blocker 谓词可见的同源形（引用同一常量集合），不得手抄状态字符串列表。

### D2: hydro 腿加在 state 级，不在 job 循环里

hydro run 不是 job 行；在 `_state_has_candidate_scope_failed_job` 的 job 循环后
追加 state 级检查：`hydro_status ∈ {"failed", "cancelled", "permanently_failed"}`
（= `_manual_retry_blocking_hydro_status` 的失败半边，不含 ACTIVE）。hydro 状态
字段读取要与本模块 blocker 扫描读 hydro 的字段口径一致（同名字段、同 fallback
链），不得另起口径。

### D3: 既有排除与语义保留

repaired stage evidence、unsubmitted auto-retry placeholder 两项排除保留在 job
循环内原位。注意 placeholder 的 status 门只认 `{pending, submission_failed}`
（`scheduler_state_rows.py:619-621`）：cancelled 的 placeholder 形状行**不在**
排除内、在 blocker 扫描里本来就是 blocker，因此在本谓词里同样计为活失败——
同域不是措辞，是两侧共享同一排除口径。arm 1（同 stage 优先）与终止性回落语义
不动。

### D4: spec 措辞同步

主 spec `openspec/specs/job-retry-mechanism/spec.md` 的钉值规则中两处
"no failed model-scoped job row (of its own)" 字面（:330、:379-380）及相邻
live-failure 子句改为活失败域表述（cancelled + hydro 失败域，含两项排除的字面
保留）。经由本 change 的 MODIFIED delta 落库（merge 后 archive 应用）。

### D5: fallback 兜底 floor——restarted-stage-family 轴（round-2 引入，round-4 重设计）

域扩宽把 cancelled / hydro 形状从钉值路由进 prev+1 回落，而这些形状恰好无法解析
canonical failed stage——stage-scoped 派生短路回顶层 `retry_count`（journal
clean-reservation 不变量下 master 行重置为 0），durable `_retry_<n>` 后缀被丢，
`new_attempt` 重铸已消耗身份 → reservation ON CONFLICT 落败 → 静默跳过提交
（round-2 CONFIRMED P1，cand-INT1）。round-2 的第一版 floor 对候选域行取
**stage-blind max**，round-4 复审证实其为 CONFIRMED P1（F-R4-A）：gate-open
分支把其他 stage 的已消耗后缀计入 forecast 预算——实测 r1-head 派生 1 的形状
HEAD 派生 5/7/8（幅度不受 marker 值限制），且单 basin cycle 会给
download/forcing/convert 行盖 `model_id`（`chain_runtime_utils.py:65-68`；仅
forecast cohort stage 强制 model-less，`accepted_submit_identity.py:317-328`），
cohort 自己的计数器经 clamp 回流。round-4 重设计为**两分支同轴**：

- **gate 不变**：`_canonical_downstream_stage(_failed_stage(state))` 可解析时
  原样返回 previous_attempt（gate-closed 分支保持 master 的 per-stage 预算
  语义；函数内 deferred import 规避循环依赖）。
- **gate-open 分支改为 restarted-stage-family floor**（`_restarted_stage_family`
  + `_fallback_previous_attempt`）：family = 候选自身活失败行的 stage 集合
  （行级活失败判据与 `_state_has_candidate_scope_failed_job` 共享同一 helper
  `_job_is_live_candidate_scope_failure`，两消费者不可漂移；repaired evidence /
  placeholder 排除作用于 family **成员资格**——被排除行不贡献 stage），hydro 腿
  是活失败时并入 canonical forecast stage（`_HYDRO_RUN_STAGE_FAMILY`，由
  `NATIVE_SHUD_STAGE_ALIASES` 推导，非字面量）。floor =
  `max(_state_retry_attempt(state, stage=s) for s in family)`——**复用
  gate-closed 分支已信任的同一条生产派生**：stage 内 status-blind、
  `max(recorded, suffix)`（`scheduler_state_rows.py:462-479`），repaired
  `_retry_3` 行在 family stage 内仍证明身份已消耗（deviation-6 语义在
  family 内保留）。空 family（stale-marker 回落、无活失败）不 clamp。
- 实测（fix head，红先行）：cancelled forecast 无后缀 + forcing `_retry_7`
  8→1；cancelled forecast `_retry_2` + forcing `_retry_7` 8→**3**（关键判别形：
  stage-blind 世界 8、无 floor 世界 1）；hydro failed + forcing `_retry_7`
  8→1；stale marker 无活失败 5→1；单 basin 盖章 download`_retry_4` 5→1、
  convert`_retry_6` 7→1；repaired 行不贡献 stage 4→2。
- 残留（有记录、未修，follow-up issue #1298 跟踪）：活失败行自身的 stage 非
  canonical 时（例：唯一活失败是 cancelled 的 model-scoped
  `download_retry_4` 行），`_state_retry_attempt(state, stage="download")`
  短路回 flat count（`scheduler_state_rows.py:449-452`，`download` 不在
  `DOWNSTREAM_RESTART_STAGES`），family floor 退化为 previous_attempt——
  同一 replay 风险在该窄形状上仍在；修复需改 `_state_retry_attempt`，超出
  本 change 单文件边界。
- round-5 追加两笔残留（均 DEFER，非本 change 回归）：候选自身 failed stage
  解析为 canonical downstream stage 时，同 stage 的多 basin cohort 行经
  `previous_attempt = _state_retry_attempt(state, stage=_failed_stage(state))`
  （`scheduler_state_failure.py:1088`）计入候选预算——`_failed_stage` 的
  cycle-scope 盲区为 pre-existing（cross-SHA 实测 head==master），跟踪
  #1300；`_state_jobs` 在无 job 行 state 上把 state 自身合成为 job 行、
  顶层 `pipeline_status` 可流入本谓词——两条生产读路径由投影形状闭合
  （见谓词 docstring 逐句引证），模块级硬化跟踪 #1299。
- 发出的 `previous_attempt` 证据字段保持未 clamp 的 stage-scoped 派生
  （`scheduler_state_failure.py:1088`），floor 只进 `new_attempt`
  （`failure.new_attempt` / `manual_retry.new_attempt` /
  `retry_policy.attempt` 同值）——其中 `manual_retry.new_attempt` 经
  manifest `retry_attempt` 消费（`scheduler_candidate_manifest.py:263-279`
  → `:239-242`），不是"仅内部"值。

## Risks / Trade-offs

- 方向性风险：扩宽 arm 2 的失败域会让更多形状走 fallback（prev+1）而非钉值——
  这正是意图（候选自身有 repair target 时不吃跨 stage cycle 计数）；但须护栏
  确认 arm 1 命中（同 stage）与"真无失败"钉值形状不回归。
- `_unresolvable_marker_entity_pins_attempt` 的无 failed_stage 臂共享该谓词，
  域扩宽后其"only failure left"判定同步收紧——两臂同域正是 #1286 round-5 审核
  确立的不变量，视为收益而非风险；判别测试须覆盖该臂至少一形。
