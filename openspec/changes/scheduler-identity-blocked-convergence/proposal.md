# Proposal: scheduler-identity-blocked-convergence

Issue: #1173 · Fixture level: expanded · Risk triage: **high**(生产调度器 journal 状态机 + reconcile 终态 + 决策白名单;错误的放行会造成重复 Slurm 提交/记录污染,错误的收敛会永久封死可自愈的 reservation)

## Why

node-22 生产调度器被一对 reserved-unbound forecast cohort 楔死:reconcile 对它们逐 pass 写 `identity_mismatch_blocked` 但**不做任何状态迁移**,row 永远停在 `reserved`;而 `reserved ∉ TERMINAL_JOB_STATUSES`(`chain_runtime_utils.py:28-36`)使 `_active_orchestration_conflicts` 每 pass 抛 `PIPELINE_ALREADY_ACTIVE` → `scheduler_execution.py:730` 记 `submission_failed`——自 2026-07-27T01:43:08Z 起 6h 零 Slurm 提交,timer 100% 占空比空转。同时决策侧 `retry_strict_warm_start_terminal_init_state_mismatch` 每 pass 重选目标 cycle 全部 36 个**已完成**流域(该 decision 只在 terminal-success skip 分支上发出,`_STRICT_WARM_START_TERMINAL_SKIP_REASONS`,`scheduler_candidates.py:61`),无任何预算约束(该文件对 `_state_retry_attempt` 零引用)。两层缺陷互为放大器:楔死期间自旋不可见;解楔后若无预算,重试会恢复为**真实重复提交**(journal 已有 `retry_87`/`retry_117` 共 204 条记录)。

## What Changes

- **L1(活性出口,reconcile/journal)**:versioned accepted-submit master 增加 durable 计数字段 `identity_blocked_streak`;三个 writer site 的递增+阈值+outcome 产出收敛到共享 blocked-outcome helper(落盘经 `_record_file_reconciliation`),blocked 结局 +1(达 limit/禁用即饱和停增),清零随 accounting tuple 原子替换(bind/absence-retry/`begin_attempt` 天然写 0)。当 streak(含本次)≥ 配置阈值且已过 `accepted_submit_grace`,经**新的专用 typed journal API**(镜像 `permit_pipeline_job_retry` 的 CAS 纪律)把 row 从 `reserved` 迁移到 **`reservation_lost`**,`reconciliation_decision="identity_mismatch_released"`(新 token)。复用 `reservation_lost` 而非新增状态:它已在全部三个封闭状态集内(`ACCEPTED_SUBMIT_MASTER_STATUSES`/`TERMINAL_PIPELINE_STATUSES`/`TERMINAL_JOB_STATUSES`),语义(放弃不可验证的 reservation)吻合,避免为一个出口撑大三处封闭词表。
- **L2(决策预算,scheduler_candidates)**:`retry_strict_warm_start_terminal_init_state_mismatch` 发出点(`scheduler_candidates.py:446-451` else 分支)前置 stage-scoped 预算检查(复用 #1160 的 `_state_retry_attempt(state, stage=<restart stage>)` 与 `_state_retry_limit`):attempt ≥ limit 时改发**稳定阻断决策** `blocked_strict_warm_start_init_state_mismatch`,evidence 携带 `retry_policy{automatic_retry_allowed: false, manual_retry_required: true, attempt, retry_limit}`(镜像 #1160 `_artifact_blocker_evidence` 的形状)。新 decision 字符串**自动**掉出两处 force 白名单(`_FORCE_TERMINAL_RESUBMIT_DECISIONS`、`force_replacement_decisions` 均按字面量匹配)——此为刻意行为,用测试钉死。
- **证据面**:`restart_reconcile` outcome 行新增 `identity_blocked_streak` 与新 action `identity_mismatch_released`;`_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS` 同步扩(否则证据压力下被静默丢弃);`restart_reconcile_proof` 按显式 `pipeline_status_write_count` 口径计入 release 状态写。
- **runbook**:`docs/runbooks/failed-basin-retry.md` 新增该 blocked 终态与 released row 的人工处置流程,含本次 `2026072000` 的处置结论。

## Impact

- Affected specs: `pipeline-job-persistence`(L1 状态迁移 + streak 字段)、`job-retry-mechanism`(L2 预算降级)、`runtime-evidence-and-operations`(证据行)
- Affected code: `services/orchestrator/reconcile.py`、`file_orchestration_journal.py`、`accepted_submit_identity.py`、`scheduler_candidates.py`、`scheduler_config.py`、`scheduler_runtime.py`(evidence 行生产)、`scheduler_evidence_payload.py`、`scheduler_evidence_proofs.py`、`docs/runbooks/failed-basin-retry.md`
- 测试面:`tests/test_gateway_reconcile.py`、`tests/test_production_scheduler.py`、`tests/test_warm_start_chaining.py`(既有断言迁移见 tasks 2.0)

## Non-goals

- 不做 #1116(comment accounting 能力探测/保守匹配放行绑定)——本 change 的出口是放弃 reservation,不是替它找回绑定。
- 不做 #1118(跨 reason 的 no-progress 断路器/告警)——streak 是 per-(job,outcome) 收敛计数,不是全局断路器;两者字段不共享、不互斥。
- 不修 cycle `2026072000` 的 init-state 身份不一致(数据侧)。
- 不动 bounded-evidence 摘要行为本身(#1171/#1172)。
- 不动另外三处同名 decision 的写入路径(`reconcile.py:1018-1029` in-flight 零写、`file_orchestration_journal.py:2554-2569` projection 延迟、`chain_array_accounting.py:389-404` in-band 延迟)。
- 不改 `_STRICT_WARM_START_TERMINAL_SKIP_REASONS` 语义、不弱化 strict warm-start 判定本身。

## Must-preserve

- `ACCEPTED_SUBMIT_MASTER_STATUSES` / `TERMINAL_PIPELINE_STATUSES` / `TERMINAL_JOB_STATUSES` 三个封闭集**成员不变**(不新增状态)。
- 通用 `transition_pipeline_job_submit_evidence` 的 decision 白名单 `_GENERIC_VERSIONED_RECONCILIATION_DECISIONS` 不扩——release 走专用 API。
- reserved-unbound 各分支的**判定条件与检查顺序**(A `:1387-1401` / B `:1499-1514` / C `:1520-1539`、absence 路径、quarantine 路径)不变;三个 site 的 outcome 产出收敛到共享 helper 属刻意结构改动(design F4),判定语义零变化。
- 未达阈值时 repeat pass 的写行为:除 streak +1 外,其余 durable 字段写集合与现状 byte 级一致;直调 `transition_pipeline_job_submit_evidence` 的既有零写钉子(`tests/test_gateway_reconcile.py:870-921`)**原样保持绿**(它不携带 streak,幂等判定不变)。
- `_terminal_decision_matches_strict_warm_start` 谓词零字节改动;预算未耗尽时 retry decision 的 evidence 形状不变。
- 强制替换白名单两处的**既有成员**不变(新 blocked decision 不加入)。

## Seams under test(上游声明,实现消费)

- `_record_file_reconciliation`(`reconcile.py:1992-2007`)——三个 writer site 的共同落盘点;递增/阈值/outcome 产出收敛于共享 helper(design F4)。
- `permit_pipeline_job_retry`(`file_orchestration_journal.py:2356-2513`)——release API 的 CAS 纪律模板(attempt + anchor + status + unbound)。
- `_state_retry_attempt` / `_state_retry_limit`(`scheduler_state_rows.py:424-455` / `:488-496`;后者兜底 `DEFAULT_RETRY_LIMIT` 永不为 None)+ `retry_identity.effective_retry_attempt`——L2 预算读数。
- `_bounded_limit_block` / `_compact_bounded_restart_reconcile` key 白名单(`scheduler_evidence_payload.py:36-43`)。
- 已知风险缝(flag 6,具体化为 F10):真实楔死几何里 master row 是 reserved、`retry_count=0`,attempt 读数依赖 journal 中 `*_retry_N` 后缀 job rows 的 stage-scoped 推导,且 state provider 按 `candidate_state_job_limit` 截断 job 行(`scheduler_candidates.py:249-268`)——tasks 2.6 用行数超过截断限的真实几何 fixture 证明预算真的会绑定;若推导/截断不命中,修复属 in-scope。

## Evidence mapping(AC → tasks)

| Issue 验收标准 | tasks |
|---|---|
| red-before:连续 N pass blocked 后迁出 reserved,提交通道解封 | 2.1/2.2/2.8 |
| red-before:36/36 已完成 cycle 预算耗尽后降级 blocked,不再逐 pass 重选 | 2.5/2.6/2.8 |
| 证据档可读出无进展(blocked 原因 + 连续 pass 计数) | 1.4 + 2.7 |
| node-22 live receipt(≥3 自然 pass 无 submission_failed、pair 不复现) | 4.1 |
| runbook 人工处置流程(含 2026072000 处置结论) | 1.5 |
| pytest 三文件 + ruff | 3.x |
