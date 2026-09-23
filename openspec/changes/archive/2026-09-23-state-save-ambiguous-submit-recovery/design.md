# Design

Change surface:
- 第 1 部分：`services/orchestrator/chain_stage_execution.py` 的通用提交失败分支（`durable_submit_ambiguity` 为假时，
  约 :549-600 调 `orchestrator._record_submission_failure` 处）；错误码登记在 `services/orchestrator/retry.py`
  （`TRANSIENT_ERROR_CODES`、`failure_classifier`）与 `services/orchestrator/scheduler_state_types.py`
  （`TRANSIENT_RETRY_REASON_CODES`）。
- 第 2 部分：`scripts/node22_manual_retry_failed_runs.py` 的 `_preview`（只读提示）。需要的话在
  `FileOrchestrationJournalRepository` 上用已有的只读查询（如 `query_pipeline_jobs_by_cycle`，:2101）。
  **不改** `_manual_retry_source_for_run` 与 `record_manual_repair`。
- 文档：`docs/runbooks/node22-control-plane-manual-recovery.md`（决策表 `permanent_failure` 行及其小节）、
  `docs/runbooks/scheduler-dbfree-typed-reasons.md:688` 附近。

Must preserve:
- forecast cohort / forcing array 的 durable ambiguity 路径（`submission_ambiguous` 事件、exact-comment reconcile）。
- 其他阶段（convert、parse、publish 等）提交失败的错误码和分类不变；`state_save_qc` 在"已证明拒绝"
  （`submit_disposition == REJECTED`）或尚未进入 gateway 边界时，照旧保留原始错误码。
- manual-retry 选择器与 DB `RetryService` 的全部行为不变，包括 `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES` 的成员与命名
  （spec `retry-execution-contract` 的成员锁定场景，以及已有的 refusal-arm 测试）。
- 脚本的 CLI 参数、退出码语义、`--execute` 行为不变；`would_mark` / `run_active` / `journal_read_blocked` 三种预览结果的
  字段不变。提示只在 `no_retryable_failed_job` 时出现，是新增字段。
- `SLURM_JOB_FAILED` 等已有的 transient / non-transient 归类不变（`slurm-error-code-transient-coverage` 的守卫测试）。

Must add/change:
- 新错误码 `STATE_SAVE_SUBMIT_AMBIGUOUS`：登记进 `TRANSIENT_ERROR_CODES` 与 `TRANSIENT_RETRY_REASON_CODES`，
  `failure_classifier` 返回 `transient_slurm_runtime`。
- 通用失败分支：`stage.stage` 按 `DOWNSTREAM_STAGE_ALIASES` 规范化为 `state_save_qc`，且
  `_submit_error_is_ambiguous(error, gateway_boundary_entered=...)` 为真时，以 `STATE_SAVE_SUBMIT_AMBIGUOUS` 记录
  `error_code`，原始错误码写进提交失败事件 details 的 `origin_error_code`，error_message 保持原文。写入点只能有这一个
  （例如给 `_record_submission_failure` 加一个 keyword 参数）。
- 脚本 `_preview`：结果为 `no_retryable_failed_job`，且 run id 能解析出 (source, cycle, model)（按
  `fcst_<source>_<YYYYMMDDHH>_<model_id>` 解析，解析不了就不提示）时，只读地用 `query_pipeline_jobs_by_cycle`（:2101，
  `_public_scheduler_row` 投影，保留 `cohort_members`；**不能**用 `iter_publication_pipeline_jobs_by_cycle`，它的白名单投影会丢掉
  这个字段）取同一 cycle 的行，找出 stage 规范化为 `state_save_qc`、状态属于 `MANUAL_RETRY_SOURCE_STATUSES` 的 cohort master 行，
  再判断成员关系：
  - 单模型 cohort：`state_save_qc` 行自带的 `model_id` 等于该模型（生产样本中 HLJ 那一行带 `model_id`）；
  - 多成员 cohort：`cohort_members` 只写在 forcing/forecast 行上（`file_orchestration_journal.py:14227` docstring）。成员关系 =
    同一 cohort `run_id` 下各行 `cohort_members[].model_id` 的并集，完整性规则与 `_cohort_run_ids_excluding_model`
    （:14222-14261，#2543）一致：列表截断、长度不一致、达到 `MAX_FORECAST_COHORT_MEMBERS`、出现空 `model_id` 都视为无法证明，
    这类行不列出。能复用该函数或其判定就复用，不要另写一套规则；
  - 输出 `cohort_candidates: [{run_id, job_id, stage, status, error_code, member_count}]`（`member_count` 为成员并集大小，单模型为 1），
    以及固定的 `warning`：标记会让整个 cohort 从 convert 重跑，member_count 个模型的 forecast 都会重算。
  - 读受阻：`query_pipeline_jobs_by_cycle` 读失败时不抛异常，而是返回 blocked 哨兵行（`_blocked_query_job`，:2111-2112）。
    必须用 `_is_blocked_query_job` 识别，识别到就给出 `cohort_candidates_error`（带 journal reason/field），**不**输出
    `cohort_candidates: []`。其他异常同样只给 `cohort_candidates_error`。两种情况下原预览结果和退出码都不变。

Governing invariant: `state_save_qc` 一次已越过 gateway 边界、未被证明拒绝的提交，不能直接落成永久失败；
值守人员对 hydro run id 预览被拒时，必须能看到应标记的 cohort master id，以及标记的代价（整 cohort 重跑）。

Sibling surfaces:
- `scheduler_state_failure.py` 读取时的分类（:265 `classify_failure`）：按错误码判断，新码登记进 transient 表后自然一致。
- `FileJournalRetryService.retry_policy_for_job` / `should_auto_retry`（`file_orchestration_journal.py:11158-11171`）与 DB 侧
  `RetryService.retry_policy_for_job`（`retry.py:430`）：都基于错误码，新码登记后两边一致。
- `_schedule_cycle_stage_retry`（`chain_forecast_orchestrator_cycle.py:247`）：master 行可重试时，按退避等待后在阶段内重提交；
  不可重试时走 `mark_pipeline_job_permanently_failed`。
- `reconcile.py` 的两个读循环：属于 #2587，不在本单范围。

Seams under test:
- 第 1 部分：经真实的 stage 执行入口（提交函数，配合抛出 gateway 错误的 slurm client 替身和文件 journal repository），
  断言落盘行与事件；以及 `FileJournalRetryService.retry_policy_for_job` / `should_auto_retry`。
- 第 2 部分：`scripts/node22_manual_retry_failed_runs.py` 的 `main`（预览模式），基于真实文件 journal repository（tmp 根），
  journal 形态仿照生产：hydro run `fcst_…` 已 succeeded；cohort master `cycle_…_convert_dg_<model>` 的 `state_save_qc` 行为
  `permanently_failed` / `SLURM_PARSE_ERROR`；另一个多成员 cohort 的 `state_save_qc` 行为 succeeded。

Required evidence:
1. `state_save_qc` 提交抛出 gateway `SLURM_PARSE_ERROR`，disposition 为 ambiguous，已进入 gateway 边界：落盘行为
   `submission_failed` / `STATE_SAVE_SUBMIT_AMBIGUOUS`；事件 details `origin_error_code == "SLURM_PARSE_ERROR"`；
   `should_auto_retry(...) is True`。
2. 反例：同一阶段 disposition 为 REJECTED → 原始错误码且 `should_auto_retry` 为假；`convert` 阶段在同样的 ambiguous 错误下
   → 原始错误码；未进入 gateway 边界 → 原始错误码。forecast 路径由已有测试守住。
3. 登记：`STATE_SAVE_SUBMIT_AMBIGUOUS` 在两张 transient 表中，classifier 为 `transient_slurm_runtime`，不在
   `NON_TRANSIENT_ERROR_CODES` 中；transient 覆盖守卫测试按需更新并保持通过。
4. 脚本提示正例：对 `fcst_ifs_2026092212_<model>` 预览 → `decision: refused`、`reason: no_retryable_failed_job`、
   `cohort_candidates` 恰好含单模型 cohort master（`run_id == "cycle_ifs_2026092212_convert_<model>"`、
   `status == "permanently_failed"`、`member_count == 1`），不含已 succeeded 的多成员 cohort；带 `warning`；退出码与改动前一致。
   再对提示给出的 cohort run id 预览 → `would_mark`、`stage == "state_save_qc"`（证明提示指向的 id 可用）。
5. 脚本提示反例：(a) 该 cycle 没有覆盖该模型的失败 `state_save_qc` 行 → `cohort_candidates == []`；(b) run id 无法解析 →
   不提示；(c) 多成员 cohort 失败且覆盖该模型（`cohort_members` 放在该 cohort 的 forcing/forecast 行上，**不在** `state_save_qc`
   行上）→ 列出，`member_count` 为成员并集大小；成员列表不完整（例如出现空 `model_id`）的 cohort → 不列出；(d) `would_mark` 与
   `run_active` 的预览输出与改动前逐字段一致；(e) cycle 读受阻（blocked 哨兵）→ 给出 `cohort_candidates_error`，不输出
   `cohort_candidates`，原预览结果与退出码不变。
6. 新行为测试（1、3、4、5c）在改动前的代码上变红，实现者贴出证据。

Non-goals:
- 让 `state_save_qc` 走 `durable_submit_ambiguity` / exact-comment reconcile：本集群 sacct 不存 comment（#1116），结果只会是"永远不明确"。
- 改选择器或 `record_manual_repair`，让 hydro run id 直接可标记；自动把 hydro run id 换成 cohort id。代价（整 cohort 重跑）
  应由值守人员看清后自己决定。
- cohort 标记只从失败阶段重启：另开 issue。
- 把外部 Slurm job id 绑定回失败行；DB `RetryService` 的任何改动；`reconcile.py` 读循环（#2587）。

Review focus:
1. 第 1 部分只影响"`state_save_qc` + 已进入 gateway 边界 + 未被证明拒绝"这一组合。
2. 安全依据成立：`write_bytes_atomic`、同 checksum 幂等、index CAS，最坏结果是落回 `permanently_failed`，没有更坏的情况。
   代码注释里要写明。
3. 提示是只读的，只列成员关系能确定的行，查询异常不影响原预览结果和退出码。
4. runbook 写清：用哪个 id、如何从提示或 blocked evidence 取得它、会整 cohort 重跑，以及 IFS 等 cycle 已出窗口时接着用
   `node22-run-cycle-once.sh`。
