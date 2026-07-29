# Proposal: scheduler-completion-verdict-absence-tolerance

## Why

Issue #1183(p1)。node-22 生产 backfill 链卡死在 cycle 2026072000:该 cycle 的 cohort 运行实际全链成功(forcing→convert→forecast→state_save_qc 终态 `succeeded`,+12h 产出状态已入 state-index 且 `usable_flag=True`),但 cycle completion verdict(`services/orchestrator/scheduler_discovery.py:180-223`)要求终态行携带与 strict warm-start checkpoint 匹配的 init-state 身份,而 **accepted-submit cohort 终态行 schema 根本不记录 init-state 身份**(实机验证:payload 43 键,`init_state*`/`state_id`/`hydro_run` 全 ABSENT)。判定把"缺账"(absence)与"错账"(conflict)同罪 → 072000 永判 `gap` → backfill `available_gaps[:1]`(`scheduler_discovery.py:478`)永远指向它 → 后继 072012-072900 尽管 NFS raw/manifest/warm checkpoint 全部就绪,连候选都不生成,生产断产。该缺陷同时是 7/20-26 重试风暴与 #1173 wedge 的第一因。

## What Changes

- **completion verdict 容缺严冲突**:终态成功 **且** successor state ready(index 在册且 usable)时,init-state 记录**缺失**不再阻断 `complete`;记录**存在但冲突**仍判 `gap`(严格性零回退)。
- **cohort 行前向补账(预约期)**:accepted-submit cohort forecast 在**预约期**把 init-state 身份落账为 master 行新字段(digest 输入集之外),终态构行时从 master 行读取;旧行不回填、不改写(零 migration)。
- **三态比对 helper(verdict 侧专用,cross-review C1 修订)**:verdict 侧消费单一**逐在场字段**helper(absent/match/conflict 三态);candidate 侧 wrapper 的 `hydro_run` 腿**保留 selected-驱动严格比对逐字节不变**(observed-驱动会把 legacy id-only 行翻成 match,绕过 #1173 预算路由),特例分支保留在其 wrapper 内不上提;candidate 准入梯子其余段不动。
- 运维文档:`docs/runbooks/failed-basin-retry.md` 增"缺账 vs 错账"判读与本次 072000 处置结论。

## Impact

- Affected specs: `strict-warm-start`(verdict 语义)、`cross-cycle-warm-start-chaining`(链推进)、`pipeline-job-persistence`(cohort 行记账)。
- Affected code: `services/orchestrator/scheduler_discovery.py`、`scheduler_candidates.py`、accepted-submit 写侧(`accepted_submit_identity.py` / `file_orchestration_journal.py`)、`tests/`、runbook。
- 部署预期(node-22 live oracle):下一自然 pass 072000 verdict→`complete`,072012 候选通过 warm 准入并提交,链逐格自动追平;非目标:不重跑 072000、不动历史行、不改 lookback/backfill 语义。
