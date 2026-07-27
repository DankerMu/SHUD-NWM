# Design: scheduler-identity-blocked-convergence

Issue: #1173。证据基础:read-only 代码勘察(2026-07-27,master @ 132e748d)+ node-22 生产观测(issue 正文)。

## 楔死机理(为什么是 `PIPELINE_ALREADY_ACTIVE`,不是 reserve gate)

issue 原文说 reservation "楔死提交通道",实际闭环是:

1. 每 pass 调度器为同一 36 流域 cohort 生成**确定性相同**的 `orchestration_run_id`(`scheduler_execution.py:882-895`,digest of source/cycle/stage/members)。
2. `chain_forecast_control.py:118-130` 的 `_active_orchestration_conflicts` 扫描该 run_id 下所有 pipeline job;楔死的 master row(`status="reserved"`)因 `reserved ∉ TERMINAL_JOB_STATUSES` 被 `_is_active_pipeline_job`(`chain_runtime_utils.py:240-245`)判为 active。
3. `_is_unsubmitted_retry_placeholder`(`:266-282`)要求 `status ∈ {pending,queued,submitted}` 且 `retry_count>0`——reserved row 永不满足,replacement-retry 旁路救不了它。
4. → `OrchestratorError("PIPELINE_ALREADY_ACTIVE")` → `scheduler_execution.py:716-766` 记 `status="submission_failed"`。

推论:**解楔的充要条件是把 row 迁入 `TERMINAL_JOB_STATUSES` 的任一成员**,不需要新状态。

## D1(L1):streak 计数 + `reservation_lost` 出口

### 为什么要计数(而不是立即放行 / 只处理确定性分支)

- Writer site B(foreign_collision)/ C(owned-mismatch)依赖 accounting 仍返回记录,记录老化出 `sacct` 窗口后可经 absence 路径自愈(`absence_retry_permitted`)——立即放行会在"真实任务可能还在跑"时制造重复提交风险。
- Writer site A(`:1387-1401`,file cohort 且 `not accepted_submit_reconcile`)是确定性终态:master 的 `cohort_members` 对比**当前** latest hydro rows(`file_orchestration_journal.py:1279-1329`,含 run_id/candidate_id/submission_attempt),后续 pass 改写这些行后永不可能再匹配。
- 生产观测未定论命中哪个 site(issue 明言需加日志才知)。streak 阈值对三个 site 统一生效:确定性 site 恰好 N 个 pass 后收敛,可自愈 site 若在 N 个 pass 内自愈则 streak 被清零、出口不触发。**一个机制覆盖全部三个 site,无需在线上定论分支归属**。
- 结构(F4):三个 site 各自独立构造 outcome 后 `continue`(`reconcile.py:1389-1400`/`1501-1513`/`1526-1538`)——递增、阈值判定与 released/blocked outcome 的产出收敛到**一个共享 blocked-outcome helper**,三个 site 改为调用它;site 的分支**判定条件与顺序**不变,outcome 产出结构性收拢是本 change 的刻意重构面。

### durable 字段与写语义

- 新字段 `identity_blocked_streak`(int ≥ 0),加入 `ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS`(`accepted_submit_identity.py:98-119`)。**贯通面(fixture review F5)**:`AcceptedSubmitTransition` dataclass(含 `__post_init__` 校验,`accepted_submit_identity.py:172-182`)、`apply_accepted_submit_transition`、`file_orchestration_journal.py:1926-1941` 的 `changed_fields` 幂等闭集必须同步扩,否则 streak 递增被静默判 idempotent 不落盘;`normalize_accepted_submit_evidence`(`accepted_submit_identity.py:507-545`)补 `identity_blocked_streak: int ≥ 0` 类型校验与 `identity_mismatch_released` 的 decision 不变量(`matched_slurm_job_id` 保持 None、`reconciliation_source` 保持 `slurm_exact_comment`)。
- 递增点:`_record_file_reconciliation`(`reconcile.py:1992-2007`)——三个 reserved-unbound writer site 的共同落盘 choke point;decision 为 `identity_mismatch_blocked` 时读当前值 +1。**饱和(F6)**:streak 达到 limit 后、或出口禁用(limit None/≤0)时**停止递增**——禁用稳态下 repeat pass 保持现状零写,不产生无上限的 journal 追加(#1165 刚治理过的增长面)。
- 清零语义(F3,**不是**在各结局散点补清零):streak 随 accounting tuple **原子替换**——任何非 blocked 的 accounting transition(`matched_bound` 走 `commit_pipeline_job_submit_attempt`、absence-retry 走 `permit_pipeline_job_retry`)与 `begin_attempt`(reclaim/新 attempt,`file_orchestration_journal.py:1616-1619`)默认把 streak 写 0;只有 blocked 递增与 release 终值携带显式非零值。这样三条绕过 choke point 的出路(bind / absence-retry / reclaim)天然清零,不存在"陈旧 streak 使新 attempt 一次 blocked 即放行"的击穿序列。
- 阈值:`scheduler_config` 新字段 `identity_blocked_streak_limit`,env `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT`,默认 **3**,`≤0 视为禁用出口`(fail-safe:配置坏值不会变成"立即放行")。经 `scheduler_runtime` 传入 `reconcile_reserved_unbound_jobs`(新关键字参数,默认 None=禁用,保持既有调用方零改动兼容)。

### 出口迁移(专用 typed API)

- 通用 `transition_pipeline_job_submit_evidence` 的 decision 白名单是封闭的(`_GENERIC_VERSIONED_RECONCILIATION_DECISIONS`,`file_orchestration_journal.py:255-263`)且**不改状态**——release 必须走新的专用 API(命名建议 `release_identity_blocked_reservation`),镜像 `permit_pipeline_job_retry`(`:2356-2513`)的 CAS 纪律:expected `submission_attempt` + attempt anchor + expected status `reserved` + `require_unbound`;CAS 失败返回 blocked 结果、不重试(下一 pass 重来)。
- 迁移写:`status="reservation_lost"`、`reconciliation_decision="identity_mismatch_released"`(新 token,加入 `accepted_submit_identity.py:22-31` 的 decision 词表)、`submit_outcome` 保持 `submit_result_ambiguous`、streak 保留终值(取证用,不清零)。
- 触发条件:本 pass 递增后 streak ≥ limit **且** row 已过 `accepted_submit_grace`。**grace 锚点固定为 `submission_attempt_started_at`(F2)**:site A 分支的 `_job_attempt_anchor(job, accepted_submit_reconcile=False)` 返回 `updated_at`/`created_at`(`reconcile.py:1980-1989`),而 streak 落盘每 pass 刷新 `updated_at`(`file_orchestration_journal.py:1943`)——若沿用该锚点,出口条件会被自己的计数写**永久推迟**。release 判定必须直接用 row 的 `submission_attempt_started_at`(缺失时 fallback `created_at`,绝不用 `updated_at`)。
- outcome:`action="identity_mismatch_released"`、`status="reservation_lost"`、`durable_write_kind/count` 如实计。
- **为什么复用 `reservation_lost`**:已是三个封闭状态集成员(改集合要动 `accepted_submit_identity.py:43-60` + `file_orchestration_journal.py:238-246` + `chain_runtime_utils.py:28-36` + 封闭集钉子 `tests/test_gateway_reconcile.py:11031-11061` 四处);operator 可经 `reconciliation_decision` 精确检索。新状态 `blocked_needs_operator` 的额外表达力(阻止一切后续自动重试)由 **L2 预算**在决策层提供,状态层不重复表达。
- **released row 是不可 reclaim 的终态(F1,刻意行为)**:versioned master 的 reclaim 硬性要求 `reconciliation_decision == "absence_retry_permitted"`(`file_orchestration_journal.py:1561-1570`),`_verified_accepted_submit_forecast_retry`(`chain_forecast_orchestrator_cycle.py:798-806`)同样只认该 token——写入 `identity_mismatch_released` 后,该 idempotency_key 的 reserve 永远走不通(`created=False` → `_reservation_already_inflight`)。这**不损害活性**:当重试预算仍允许时,`_schedule_cycle_stage_retry`/`_retry_cycle_stage_job_id` 以 `*_retry_N` 后缀铸新 idempotency key(注意 `_cycle_stage_idempotency_key` 默认形态跨 pass 恒定,新 key 只来自 retry 后缀),ghost 只封死自己的 key;`reservation_lost ∈ TERMINAL_JOB_STATUSES` 保证它不再触发 `PIPELINE_ALREADY_ACTIVE`。它同时是**防重复提交的正确方向**:身份不可验证的 reservation 不应被 reclaim 复活。本次生产几何下 auto-retry 预算(attempt 87/117)不放行新 key——released row 停在收敛终态而非再提交,这正是期望结局。人工重入不依赖 reclaim,具体可执行步骤由实现验证后写入 runbook(tasks 1.5,复审 P2)。
- **#1116 交界**:release 不试图为 row 找回绑定;真正的重复提交防线是 (a) released key 不可 reclaim(上一条)、(b) 本 family 被 L2 预算挡住重选(生产几何 attempt 87/117 ≫ 3)、(c) 其它 decision family 有各自 retry limit(`retry.py:127-148`)。风险残量记入 runbook 处置节。

## D2(L2):strict-warm-start 终态重试预算

- 位置:`scheduler_candidates.py:404-451` 的 else 分支(发出 `retry_strict_warm_start_terminal_init_state_mismatch` 处)。`raw_candidate_state` 在 `:246-271` 已就位(state provider 已注入 `retry_limit=context.config.retry_limit`,`:263`)。
- 判定:`attempt = _state_retry_attempt(raw_candidate_state, stage=<restart stage,即 "forecast">)`,`limit = _state_retry_limit(raw_candidate_state)`(注:该函数兜底 `DEFAULT_RETRY_LIMIT`,**永不返回 None**,`scheduler_state_rows.py:488-496`);`attempt >= limit` → 改发降级决策;否则维持现 decision 与 evidence 形状**零改动**。
- 降级决策**必须是 `CandidateStateDecision("blocked", "strict_warm_start_retry_budget_exhausted", evidence)`(F8)**——`action="blocked"` 才会被下游归入 blocked 列表而不进入提交候选;只换 decision 字符串而保持 `action="retry"` 会照常提交,spec 场景落空。
- 降级 evidence 形状(镜像 #1160 `_artifact_blocker_evidence`,`scheduler_state_failure.py:418-459`):`decision="blocked_strict_warm_start_init_state_mismatch"`、`reason="strict_warm_start_retry_budget_exhausted"`、`retry_policy={automatic_retry_allowed: False, manual_retry_required: True, attempt, retry_limit}`、`native_shud_resubmitted=False`、`replacement_submitted=False`、保留 `strict_warm_start` 与 `candidate_state` 上下文。
- **白名单自动退出是刻意行为**:`_FORCE_TERMINAL_RESUBMIT_DECISIONS`(`chain_forecast_orchestrator_cycle.py:17-24`)与 `force_replacement_decisions`(`chain_runtime_utils.py:171-177`)按字面量匹配 decision 字符串——两处**零编辑**,新 blocked decision 天然不享受强制替换/重提。测试钉死"两个集合成员未变 + blocked decision 不触发 force 路径"。
- **预算读数在真实几何下必须绑定**(勘察 flag 6,失效模式已具体化 F10):楔死 master 是 `reserved`、`retry_count=0`,attempt 依赖 journal 中 `*_forecast_retry_N` 后缀 job rows 的 stage-scoped 推导(`_state_job_retry_attempt` → `effective_retry_attempt` 后缀解析)。**真正的失效缝是 state provider 按 `candidate_state_job_limit` 截断 pipeline_jobs(`scheduler_candidates.py:249-268`)——`retry_87` 行被截掉时 attempt 读到 0**。tasks 2.6 的真实几何 fixture 必须包含超过 `candidate_state_job_limit` 的 job 行数,证明 attempt 仍 ≥ limit;若需修截断/推导,修 `scheduler_state_rows.py`/provider 截断策略属 in-scope,且必须保持 #1160 既有测试(`test_state_retry_attempt_honors_forecast_retry_suffix_and_exhausts_the_limit` 等)零迁移。

## D3:证据面

- `scheduler_runtime.py:1505-1539`(restart_reconcile outcome 全保真行):新增 `identity_blocked_streak`(来自 outcome)与新 action token;dataclass `ReservationReconcileOutcome` 增 `identity_blocked_streak: int | None = None`。
- `scheduler_evidence_payload.py:36-43` `_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS` 增 `identity_blocked_streak`——否则 bounded 压缩把它静默丢掉,AC"证据档一眼读出无进展"在证据压力场景(正是本次事故的常态)不成立。
- `scheduler_evidence_proofs.py:426-500` `restart_reconcile_proof`:release 的状态写计入 proof。注意(F9):`reserved_status_update_count` 的 `:460-465` 分支只在 legacy fallback(无 `has_explicit_write_kinds`)参与;新 release outcome 带 `durable_write_count`,走显式路径——计数断言落在显式 `pipeline_status_write_count` 口径上,legacy 分支仅当 outcome 无显式写计数时兜底(tasks 2.7 按两种形状分别断言,避免空转断言)。
- L2 降级决策经既有 `decision` key 进入 bounded candidate summary(`_BOUNDED_CANDIDATE_SUMMARY_KEYS` 已含 `decision`,零扩)。
- 与 #1118 的边界:本 change 只提供 per-job streak 与 per-decision 预算的**事实字段**;跨 reason 聚合、`no_progress_circuit_open` 告警升级仍归 #1118,不在此实现。

## 风险与保护

| 风险 | 保护 |
|---|---|
| release 造成重复提交(真实任务仍在跑) | streak≥3(连续,任何异质结局清零)+ 过 grace(锚 `submission_attempt_started_at`)才放行;require_unbound CAS;released key **不可 reclaim**(F1);本 family 被 L2 预算挡住重选 |
| released ghost 封死后续合法提交 | 每次 attempt 铸新 idempotency key,ghost 只封自己的 key;`reservation_lost` 终态不再触发 `PIPELINE_ALREADY_ACTIVE`;人工重入走 manual retry API(runbook) |
| streak 写被幂等闭集静默吞掉(假绿) | `changed_fields`/`AcceptedSubmitTransition`/normalize 三处贯通列入 tasks 1.1;tasks 2.0 新增 reconcile 路径写断言 |
| 出口不可达时 journal 无上限增长 | streak 饱和(≥limit 或禁用即停增);禁用稳态零写断言(tasks 2.3) |
| 陈旧 streak 击穿"连续"语义(absence-retry→reclaim→一次即放行) | streak 随 accounting tuple 原子替换,`begin_attempt` 强制清零;tasks 2.2 场景钉死 |
| 配置坏值(0/负数)导致立即放行 | ≤0 = 禁用出口(fail-safe 方向是"维持现状楔死",不是"乱放行") |
| 预算读数在真实几何下为 0(F10:`candidate_state_job_limit` 截断) | tasks 2.6 fixture 行数超过截断限;推导/截断修复 in-scope |
| 三个封闭状态集被扩 | Must-preserve 明令禁止;评审 checklist 项 |
| bounded 压缩丢掉新证据字段 | key 白名单同步扩 + tasks 2.7 双档(全保真/bounded)断言 |
