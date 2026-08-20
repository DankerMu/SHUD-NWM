# Proposal: repair-evidence-projection-composition

## Why

PR #1449 round-4 review 在 master 上 CONFIRMED 两个 pre-existing 的 repaired-evidence 投影缺陷（#1460、#1461），同文件同投影面，合并一个 change 交付：

1. **#1460 single-winner 驱逐**：`_candidate_manual_stage_repair_state`
   （`services/orchestrator/chain_repository_state.py:359-400`）按 truth key 倒序遍历成功
   manual retry job，第一个产出非空修复目标集合即 `break`。同一候选两次跨 stage 人工修复时，
   较早那次的失败行完全失去 repaired 标注（无 `repair_status`/`repaired_by_job_id`，
   `active_blocker` 仍真值域），在全部下游消费者（blocker 扫描、`_restarted_stage_family`、
   `latest_job_repaired` 腿）里复活为活 blocker。运维连修两处后调度器仍报活失败，restart stage
   回退，重复重跑已成功的 stage。成因：`cde5967a` 的 `break` 把「标注面」与「latest_repair 选择」
   两件事耦合在一起。
2. **#1461 elif 抢占**：`chain_repository_state.py:875-902` 的 completed-stage 投影分支以
   `elif` 挂在外层 `if isinstance(repaired_stage_evidence, Mapping)` 上，而内层守卫要求
   `restart_stage` 非空才投影。source-cycle 变体 `_source_cycle_repaired_stage_evidence`
   恒不带 `restart_stage`（且在 `:814-816` 取值优先级上压过 manual-stage 变体），落进
   「内层不投影、外层 elif 又被抢占」的两不管：上游 download 被人工修复后，候选自身的
   `completed_stage_evidence`/`restart_stage` 被整体抑制，`_failed_stage(state)` 塌成
   `None`，`_manual_retry_new_attempt` 从 1 重铸为 5——#1201/#1298 attempt 记账族的又一静默入口。

## What Changes

- `_candidate_manual_stage_repair_state`：去掉 `break`，遍历全部 `successful_retry_jobs` 累积 `repaired_by_failed_job_id`/`repair_events`；同一 `failed_job_id` 被多个 retry job 认领时 first-write-wins（外层倒序即 newest-wins）。`latest_repair` 选择规则（`max` 按 retry/failed truth key）与 `restart_stage` 计算保持不变。
- `candidate_state_from_rows` 投影组合：completed-stage 分支从 `elif` 改为独立 `if`，仅当 repaired 分支**实际投影了** `restart_stage` 时跳过（而非 repaired 证据存在即跳过）。manual-stage 带 restart_stage 形状与无 repaired 证据形状 byte-identical；gap 形状（source-cycle 变体、以及 manual-stage 终段 stage `_stage_after` 为 None 时）恢复 completed-stage 投影。
- 回归测试锁两条缺陷的 AC 全项 + must-preserve 面。

## Non-Goals

- `REPAIRABLE_PIPELINE_STATUSES` 域、`_jobs_share_stage` 扩面**判据**、`latest_repair` 选择规则、`restart_stage` 计算——全部保持现状（#1460 边界原文）。注：判据不动但聚合标注面按设计扩张为各 retry job 扩面集合的并（见 design D1）。
- `_source_cycle_repaired_stage_evidence` 的 repair 判定逻辑与 13 键形状不动；**不**走「给 source-cycle 变体补 `restart_stage`」备选路线（对 `stage="download"` 恒 None，治不了根，见 design D2 rejected）。
- `_manual_retry_new_attempt`/`_fallback_previous_attempt` 记账算法不动（本 change 修的是其输入污染）。
- #1308 row-absent pin gate 的**算法**、#1451 `active_blocker is False` 臂 oracle 缺口——同源 review 已单独立项。但 gate 对本 change 新开输入形状（gap 形状扫描产 completed 证据带 `job_id`）的行为由本 change 的对照腿钉住（design must-preserve 8，E11）。
- marker 写入侧（`RetryService`）不动。

## Impact

- Affected specs: `retry-stage-evidence-supersession`（MODIFIED 标注审计需求 + ADDED 投影组合需求）
- Affected code: `services/orchestrator/chain_repository_state.py`（唯一生产改动文件）；下游消费面（`scheduler_state_failure.py`、`scheduler_state_manual_retry.py`、`scheduler_state_decision.py`、`scheduler_state_evidence_owner.py`）行为随输入修正而收敛，不改代码。
- Affected tests: `tests/test_production_scheduler.py`、`tests/test_orchestration_chain.py`
- Closes #1460, closes #1461。
