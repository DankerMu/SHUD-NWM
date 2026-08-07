# Proposal: cycle-pin-live-failure-domain

## Why

Issue #1287（PR #1286 round-3 DEFER，verifier CONFIRMED）：cycle-scope 钉值规则
arm 2 的"候选自身无 repair target"判据 `_state_has_candidate_scope_failed_job`
只数 `status ∈ FAILED_PIPELINE_STATUSES` 的 job 行，看不见 `cancelled` 的
model-scoped 行、也看不见失败/取消的 hydro run。同文件的 blocker 域早已放宽到
`cancelled` + hydro 失败（`_manual_retry_blocking_pipeline_status` /
`_manual_retry_blocking_hydro_status`）。后果：自身明明有活 repair target 的候选
（own forecast cancelled，或 jobs 全 succeeded 但 hydro failed）被读成"无候选域
失败"，交叉 stage 的 cycle-download 计数器被钉进候选 forecast 级 `new_attempt`
（实测 1→5 / 3→5 跳号），经 `scheduler_state_failure.py:1089` 写进决策证据并投影
到 manifest `retry_attempt`，可越过 `retry_limit`。cancelled 的人工重试是一等流程
（`retry.py:50` `MANUAL_RETRY_SOURCE_STATUSES` 含 `cancelled`），可达性已实测。

## What Changes

- `_state_has_candidate_scope_failed_job` 的活失败域扩到与 blocker 域的失败半边
  同宽：model-scoped job 行 `status ∈ FAILED_PIPELINE_STATUSES ∪ {"cancelled"}`，
  并追加 hydro 腿（`hydro_status ∈ {failed, cancelled, permanently_failed}`）。
  既有排除（repaired stage evidence、unsubmitted auto-retry placeholder）保留。
- 该谓词的两个消费者（`_cycle_scope_marker_pins_attempt` arm 2、
  `_unresolvable_marker_entity_pins_attempt` 无 failed_stage 臂）由构造自动同域。
- 同步收紧主 spec `job-retry-mechanism` 中两处 "no failed model-scoped job
  row" 字面（:330、:379-380）及相邻 live-failure 子句为活失败域（cancelled +
  hydro）。
- 判别对测试两条（issue 验收标准）+ 回归护栏。
- round-2 引入、round-4 重设计的回落 floor 随行（design D5）：prev+1 回落在
  无法解析 canonical failed stage 时，以候选自身活失败行的
  restarted-stage-family 内的 stage-scoped 已消耗 attempt 记录为下界
  （`_restarted_stage_family` + `_fallback_previous_attempt`，复用
  `_state_retry_attempt(stage=s)` 同轴派生），防止域扩宽后新可达的回落路径
  重铸已消耗身份（reservation 静默跳过），同时跨 stage / cohort 计数器绝不
  计入候选预算（round-4 F-R4-A 修复）。

## Impact

- Affected specs: `job-retry-mechanism`（钉值规则 arm 2 措辞）
- Affected code: `services/orchestrator/scheduler_state_manual_retry.py`
  （唯一实现点）；只读下游 `scheduler_state_failure.py:1089`（消费值，不改）；
  tests `tests/test_production_scheduler.py`
- Out of scope（issue 边界，逐条）：`_cycle_scope_marker_pins_attempt:210` 状态臂
  自身对 cancelled 的 marker 目标行的缺口；journal 候选 state 不按 model 过滤；
  最新 adopted marker 无 `retry_count` 时的终止性（#1289 域）；#1292 的三项
  unresolvable-gate 残留。
- design.md 全套提供（expanded fixture，domain trigger: orchestrator state
  machine）。
