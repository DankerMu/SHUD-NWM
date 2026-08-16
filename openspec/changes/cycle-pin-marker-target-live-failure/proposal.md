# Proposal: cycle-pin-marker-target-live-failure

## Why

Issue #1294（#1287 的镜像残留，master parity）：`_cycle_scope_marker_pins_attempt`
的 marker 目标行状态臂仍用裸 `FAILED_PIPELINE_STATUSES`（不含 `cancelled`），
而 `cancelled` 的人工重试是一等流程（`retry.MANUAL_RETRY_SOURCE_STATUSES`、
blocker 谓词均含 `cancelled`）。运维对 `cancelled` 的 cycle-scope 行钉
`retry_count` 时，该行被误判为「陈旧 marker 目标」，两条钉值臂都不再求值，
`new_attempt` 静默落回 `previous_attempt + 1`（TERMINAL），运维钉的号被吞——
欠钉会让重投复用已被终态行占据的 attempt 号（#1201 已兑现过的
`skipped_duplicate_submission` 静默失效形）。

同时 `_cycle_scope_marker_pins_attempt` 的 docstring 明确记录了这一不对称并
指向本 issue；修复后两侧域同宽，docstring 需随之改写为同源事实。

## What Changes

- `services/orchestrator/scheduler_state_manual_retry.py`：抽一个共享的行级
  live-failure 谓词（blocking ∧ ¬ACTIVE ∧ ¬repaired-stage-evidence ∧
  ¬unsubmitted-placeholder），`_cycle_scope_marker_pins_attempt` 的 marker
  目标臂与 `_job_is_live_candidate_scope_failure` 的行级分支由构造同源；
  docstring 同步改写。
- 判别测试：marker 目标行 `cancelled` 时臂 1（同 stage）/臂 2（唯一失败）均
  钉 `retry_count`；既有回归护栏保持绿。
- spec 措辞：本 change 的 MODIFIED delta 承载全部措辞变更（「still in a
  failed status」/「no longer failed (stale)」等改为 live-failure 口径）；
  主 spec 落库在 merge 后 `openspec archive`，不进本 PR diff。

## Risk Triage

- Fixture level: **expanded**（强制触发词命中：retry / persisted state
  transitions / orchestrator state machine，与兄弟 change
  `cycle-pin-live-failure-domain` 同口径）。Upstream suggested level: 缺省
  （scribe 手写 issue，无该字段）。
- Repair intensity: **medium**（retry attempt 记账语义，但改动面窄、判别
  期望值已由 issue 给出实测表；生产今天不可达——唯一写入方
  `record_manual_repair` 零非测试调用方，随 #1186 接线才上线）。
- Risk packs:
  - state-machine/attempt-accounting: **selected** —— 钉值臂语义变更，判别
    测试锚定四个 failed 域状态 + cancelled + succeeded + 两个排除形。
  - compatibility/regression: **selected** —— #1287 的判别对与
    placeholder-shaped cancelled 口径必须保持绿。
  - spec-compliance: **selected** —— delta 措辞与最终谓词语义需逐句对读
    一致（tasks 3.4）。
  - file IO/path safety: not selected —— 无文件面。
  - security/auth: not selected —— 无权限面。
  - performance: not selected —— 纯谓词，无热路径变化。

## Non-Goals

- `_state_has_candidate_scope_failed_job` 一侧（#1287 已合入，勿回改）。
- `_unresolvable_marker_entity_pins_attempt` 的残留分歧（#1292 交付后由
  #1308 跟踪）。
- 最新 adopted marker 无 `retry_count` 的终止性（#1289）。
- `_job_matches_candidate` 的 cycle-run_id 跨 model 采纳（#1288）。
- hydro 语义进本臂（本臂判一行 job，hydro run 不是 job 行）。
- manual-retry 执行入口接线（#1186）。

## Impact

- `services/orchestrator/scheduler_state_manual_retry.py`（唯一实现点）
- `tests/test_production_scheduler.py`（新增判别测试，复用
  `_decision_path_cycle_download_job` / `_decision_path_cycle_download_marker`）
- `openspec/specs/job-retry-mechanism/spec.md`（merge 后 `openspec archive`
  时由 delta 回写，不在 PR diff 内）
- 只读下游 `scheduler_state_failure.py`（消费 `new_attempt`，不改）
