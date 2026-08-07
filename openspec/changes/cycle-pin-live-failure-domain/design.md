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

### D5: fallback 兜底 clamp（round-2 审核修复，cand-INT1）

域扩宽把 cancelled / hydro 形状从钉值路由进 prev+1 回落，而这些形状恰好无法解析
canonical failed stage——stage-scoped 派生短路回顶层 `retry_count`（journal
clean-reservation 不变量下 master 行重置为 0），durable `_retry_<n>` 后缀被丢，
`new_attempt` 重铸已消耗身份 → reservation ON CONFLICT 落败 → 静默跳过提交。
因此回落端加兜底 clamp（`_candidate_scope_consumed_attempt` +
`_fallback_previous_attempt`，仍仅本文件）：

- **clamp 域走 identity-consumption 轴**，对候选域（非 cycle-scope）行取
  `effective_retry_attempt`（recorded count 与 id 后缀取 max）的最大值——
  live-failure 域的两项排除（repaired evidence / placeholder）**故意不适用**：
  生产铸号规则 `_next_retry_attempt_for_stage` 扫 `{base}_retry_` 前缀不看
  status，repaired `_retry_3` 行同样证明身份已消耗。floor 只会更安全（更高）。
- **gate 在"无 canonical failed stage"上**而非无条件：无条件 clamp 会把其他
  stage 的后缀计费进已正确派生的 forecast 预算（跨 stage 计费 + retry-limit
  提前触顶）。gate 复用生产解析器 `_canonical_downstream_stage(_failed_stage)`
  （函数内 deferred import 规避循环依赖，两侧不可漂移）。
- 残留（有记录、不修）：无法命名 stage 的分支里 floor 是候选域行的
  stage-blind max——理论上会把候选另一 stage 的后缀计入，但实践不可达
  （parse/state_save_qc/publish 是 cohort stage，download/forcing/convert 是
  cycle-scope，候选域 stage 实际只有 forecast）；且 clamp 只影响内部 floor，
  发出的 `previous_attempt` 证据字段保持未 clamp 的 stage-scoped 派生。

## Risks / Trade-offs

- 方向性风险：扩宽 arm 2 的失败域会让更多形状走 fallback（prev+1）而非钉值——
  这正是意图（候选自身有 repair target 时不吃跨 stage cycle 计数）；但须护栏
  确认 arm 1 命中（同 stage）与"真无失败"钉值形状不回归。
- `_unresolvable_marker_entity_pins_attempt` 的无 failed_stage 臂共享该谓词，
  域扩宽后其"only failure left"判定同步收紧——两臂同域正是 #1286 round-5 审核
  确立的不变量，视为收益而非风险；判别测试须覆盖该臂至少一形。
