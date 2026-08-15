# Tasks: master-row-permanent-failure-marking (#1312)

## Risk packs (considered)

- Selected: terminal-state-semantics · idempotency · oracle-integrity ·
  authority-surface（理由见 design "Risk packs"）。
- Not selected: concurrency · performance · security · data-migration。

## Tasks

- [x] 0. 运行时探针（design D5/D9 复证，先于一切实现）：
  - (a) 真实 `FileOrchestrationJournalRepository` 构造 typed-reserve master
    行，**先断言** `accepted_submit_contract_is_current(row) and
    accepted_submit_row_kind(row) == "master"`（round-2 Note-1，防假阴
    性），再直调现 `mark_permanently_failed` → 复证
    `file_journal_authority_transition_requires_typed_api` raise。
  - (b) 同批复证 D9 抹回链：手工把 master 行写成 `permanently_failed`
    后驱动第二趟 resume/projection → 观察是否被
    `project_forecast_cohort_tasks` 抹回 `failed`。
  - 两探针结果记入 PR 偏离记录；(a) 不 raise 或 (b) 不抹回 → 停下报告
    orchestrator 重裁对应决策。
- [x] 1. 新增 typed authority transition（design D5 精确形状）：
  `mark_pipeline_job_permanently_failed`——只借 reject 的写序
  （`_locked_cycle_write`/`_journal_record_for_write`/
  `_validate_outgoing_record`/`_append_journal_records_unlocked`/
  `_write_pipeline_job_direct_unlocked`）；源状态前置=终态失败子集
  `{failed, submission_failed}`（task 11/14 裁决后两次收窄：
  `reservation_lost`、`partially_failed` 移出，见 design D5），非法
  源返回 stale 不 raise；accounting 元组
  （reconciliation_decision/submit_outcome/matched_slurm_job_id）逐字段保
  持；幂等前置以 job_id 重读持久行；事件
  `event_type="permanently_failed"` + 真实 `status_from`，仅随真实翻转追
  加。`:3240/:3251` 禁令不放宽。
- [x] 2. `FileJournalRetryService.mark_permanently_failed`（`:6704`）：
  master 行改走 task 1 API，且 master 分支**先按 job_id 重读持久行**再判
  幂等（对 master 绕开 `:6705-6707` 快照闸，design D4/round-2 P2-E）；非
  master 行为不变。
- [x] 3. 调用方臂 `chain_forecast_orchestrator_cycle.py:195-197`：
  capability 门控（`getattr(self.retry_service, "repository", None) is not
  None`，design D2），落标后仍 `return None`；store-less/DB 形状零变化。
- [x] 4. 休眠臂 `file_orchestration_journal.py:6607-6608`：False 臂经
  `self.mark_permanently_failed` 落标后返回（design D3）。
- [x] 5. cohort projection 终态粘性（design D9）：
  `project_forecast_cohort_tasks`（`:2993-3021` master 写面）对 existing
  `permanently_failed` 行保留状态、只更 projections/证据字段（与
  `:3173-3175` defer 终态短路同构）；两个入口
  （`chain_array_accounting.py:308-330`、`reconcile.py:1035-1050`）经同一
  函数覆盖。
- [x] 6. 测试（新增，既有测试零编辑；seam 编号对应 design）：
  - seam 1 调用方臂：master 行 `OUT_OF_MEMORY` → None + 读回
    `permanently_failed` + 单条事件（复用
    `tests/test_orchestration_chain.py:1607-1634` builder 族，service 换
    file-journal 形）。
  - seam 2 休眠臂：`handle_failed_job` 直连结构性 master 行 → namespace
    与持久行均 `permanently_failed` + 事件。
  - seam 3 store-less 负向：`RetryService(None,…)` + master 行 → None、不
    抛、零落标。
  - seam 4 新 API 单测：终态失败源合法写入 + accounting 元组逐字段相等 +
    `running` 源 → stale 零写入零事件 + `update_pipeline_job_status` 对
    master 仍 raise。
  - seam 5 幂等双向：stale 快照双驱动 → 事件计数 1；快照
    `permanently_failed`/持久行 `failed` → 仍落标。
  - seam 6 反向控制：master 行 `NODE_FAILURE`（低 retry_count）→ retry
    identity 不变、零落标。
  - seam 7 耗尽域：master 行 `NODE_FAILURE` + `retry_count >=
    max_retries` → 落标 + upstream refresh 不重投（design D6）。
  - seam 8 upstream-refresh：已落标行 + `refreshed_upstream_finished_at`
    → 不重投。
  - seam 9 e2e 两趟（端到端不变式钉，**非** D9 粘性红证——二趟走 defer
    分支，见 D9 偏离 (b)）：OOM master 行（经 `_write_task_outcome_receipt`
    `:1389` 注入，装配复用 `:8843-8853` 形）第一趟落标、第二趟后行仍
    `permanently_failed`、事件计数仍 1、两趟均无 raise、PipelineResult
    "failed"。
  - seam 10 参数化：两臂各覆盖 `OUT_OF_MEMORY` + `INVALID_MANIFEST`。
- [x] 7. 红证（design D8，S-2 更正后口径）：仅回退 caller 臂 → 仅 caller
  测试红；仅回退 journal 臂 → 仅 journal 测试红；回退 D9 粘性 →
  **journal 级粘性单测红**（二趟 e2e 走 defer 分支不参与该红证，见
  design D9 偏离 (b)）。三组 pytest 输出留存；`git stash list` 空核验。
- [x] 8. 回归：`uv run pytest -q tests/test_retry.py
  tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py
  tests/test_production_scheduler.py` 全绿；`uv run ruff check .`；
  `openspec validate master-row-permanent-failure-marking --strict
  --no-interactive`。
- [x] 9. 既有 anchor `tests/test_orchestration_chain.py:1637-1652` 原样通过
  （只断 None 返回；不编辑不弱化）。

## Round-1 fix tasks (Phase 5/6)

- [x] 10. C-1（P2 FIX_NOW）：mark 调用双臂窄捕获——caller 臂
  `chain_forecast_orchestrator_cycle.py` 仅捕
  `OrchestratorError`/`FileOrchestrationJournalError` 后仍 `return None`，
  并按 `chain_forecast_submission.py:157-166` 形发运维信号（自身再包一层
  不得抛）；休眠臂同两类异常回退 `_file_retry_namespace(current)`。回归测
  试：mark raise → `orchestrate_cycle` 仍 `PipelineResult("failed")`、零新
  行。
- [x] 11. C-2/R-1 搭车裁决：`PERMANENT_FAILURE_SOURCE_STATUSES` 移除
  `reservation_lost`；随之 `identity_mismatch_released` decision 特判守卫
  成死码一并删除；本 PR 自有的两条 reservation_lost 测试改为断言
  `stale` + 行不变 + 零事件 + reclaim 门仍开
  （`reclaim_pipeline_job_reservation` 仍可达）。
- [x] 12. T-1（P2 FIX_NOW，`partially_failed` 一侧随 task 14 翻到非法侧）：
  源状态域参数化——合法 ×3
  （`failed`/`submission_failed`/`partially_failed`，各断 `applied` + 真实
  `status_from` + 事件计数 1；`submission_failed` 经
  `reject_pipeline_job_submit_attempt` 构造，`partially_failed` 经混合
  outcome projection 构造）；非法补 `succeeded`/`cancelled`（+ 两个
  `reservation_lost` 子形，随 task 11）断 `stale` + 整行相等 + 零事件。
  验收：收窄/放宽两个源集突变体均被杀。
- [x] 13. C-3/S-3（P2 FIX_NOW）：粘性写穿测试——落标后带**变化证据**
  （`finished_at`/`log_uri`/`error_code`）重投影，断言持久行仍
  `permanently_failed`、accounting 元组不变、`permanently_failed` 事件计
  数仍 1（不得断 `total == 0`——写分支合法写 1 行 + 1 条 pf→pf
  `status_change`）。D9-revert 突变下必红。

## Round-3 fix tasks (Phase 7 终审 + verifier CONFIRMED；retro-round3 纠正动作)

- [x] 14. C-P1（P1 FIX_NOW）：`PERMANENT_FAILURE_SOURCE_STATUSES` 移除
  `partially_failed`（源集收束为 `{failed, submission_failed}`，理由见
  design D5——partial cohort 受 #1202 partial-advance 契约约束，落标使二
  趟 resume 把 `parsed_partial` 翻成 `failed_run` 并跳过下游 stage）。测
  试：(a) task 12 的 `partially_failed` 合法参数化翻到非法侧（stale + 整
  行相等 + 零事件）；(b) 新增混合 cohort 两趟 e2e——2 basin、task0
  succeeded / task1 `OUT_OF_MEMORY`，`orchestrate_cycle` 两趟，断言
  pass-1 与 pass-2 的 PipelineResult / master 行状态（保持
  `partially_failed`）/ 下游 stage 推进与 pre-PR 基线完全同构、全程零
  `permanently_failed` 事件。
- [x] 15. C-P2（P2 FIX_NOW）：休眠臂窄捕获回退前 best-effort 追加与调用方
  臂同形的 `permanent_failure_mark_failed` 事件（emission 自身不得抛；不
  以收窄 spec 文本代偿）；扩展既有休眠臂 raising-mark 测试断言该事件恰
  1 条。

## Required evidence (maps every selected pack)

- terminal-state-semantics：seams 6/7/8/9 + task 8 全量回归（不增重投 +
  D6/D7 裁决的减重投显式钉住 + D9 抹标振荡消除）。
- idempotency：seam 5 双向 + task 1 幂等前置。
- oracle-integrity：task 9 + task 7 三组红证（每面独立先红后绿）。
- authority-surface：seam 4（前置/元组保持/禁令保持）+ task 5 粘性。

## Non-goals

见 design "Non-goals"（#1313 store-less 平面 / #1314 / DB 平面 / 手动重试
路径 / 非 permanently_failed 终态的 projection 粘性语义）。
