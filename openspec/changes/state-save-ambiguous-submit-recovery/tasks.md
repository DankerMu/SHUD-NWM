# Tasks

## Risk packs

- [x] Concurrency / shared state / ordering — **selected**：不明确提交的原作业可能仍在跑，此时自动重试会并发执行。
  → 1.2 的安全依据注释；2.1
- [x] Error handling / rollback / partial outputs — **selected**：失败分类与永久失败标记。→ 2.1、2.2
- [x] Legacy compatibility — **selected**：forecast/forcing ambiguity 路径、选择器与 DB `RetryService` 不变、transient 覆盖守卫、
  脚本已有预览结果。→ 2.2、2.3、2.5
- [x] Public API / CLI / script entry — **selected**：manual-retry 脚本新增预览字段。→ 2.4、2.5
- [x] Documentation / migration notes — **selected**：runbook 决策表与 typed-reasons。→ 1.4
- [x] Config、File IO、Schema、Auth、Resource limits、Release — **not selected**：不改配置、文件格式与权限；提示只读。

## 1. 实现

- [x] 1.1 `services/orchestrator/retry.py`：`TRANSIENT_ERROR_CODES` 加入 `STATE_SAVE_SUBMIT_AMBIGUOUS`，`failure_classifier` 把它归为
      `transient_slurm_runtime`；`services/orchestrator/scheduler_state_types.py` 的 `TRANSIENT_RETRY_REASON_CODES` 同步加入。
- [x] 1.2 `services/orchestrator/chain_stage_execution.py` 通用提交失败分支：`state_save_qc`（按 `DOWNSTREAM_STAGE_ALIASES`
      规范化）且 `_submit_error_is_ambiguous(error, gateway_boundary_entered=gateway_boundary_entered)` 为真时，记为
      `STATE_SAVE_SUBMIT_AMBIGUOUS`，原始错误码写进事件 details 的 `origin_error_code`。写入点只能有一个。注释写明安全依据：
      `write_bytes_atomic`、同 checksum 幂等、index CAS（`atomic_replace_provider_bytes`）、copyback 以 run 为权威
      （`state_manager.py:2395-2407`）；最坏情况是落回 `permanently_failed`。
- [x] 1.3 `scripts/node22_manual_retry_failed_runs.py` `_preview`：按 design「Must add/change」加只读的 `cohort_candidates` 提示
      和 `warning`。成员关系：单模型看 `state_save_qc` 行的 `model_id`；多成员取同一 cohort run 下 forcing/forecast 行
      `cohort_members` 的并集，完整性规则复用 `_cohort_run_ids_excluding_model`（:14222-14261）。用 `query_pipeline_jobs_by_cycle`，
      识别 blocked 哨兵（`_is_blocked_query_job`），读受阻与其他异常都只给 `cohort_candidates_error`。
- [x] 1.4 文档：
      - `docs/runbooks/node22-control-plane-manual-recovery.md`：`permanent_failure` 行和小节加入"forecast 已成功、`state_save_qc`
        失败"的处置：传 cohort master id（取自脚本提示，或 blocked evidence 中失败行的 `run_id`）；标记会整 cohort 从 convert 重跑；
        cycle 已出窗口时接着用 `scripts/ops/node22-run-cycle-once.sh --cycle-time ... --source ... --basin-id ...`（先 `--plan` 再 `--submit`）。
        附 2026-09-23 HLJ 实例（标记后 55151–55154 全部完成）。
      - `docs/runbooks/scheduler-dbfree-typed-reasons.md:688` 附近：注明 `state_save_qc` 的人工出口，以及新错误码
        `STATE_SAVE_SUBMIT_AMBIGUOUS` 的含义（会自动重试；`origin_error_code` 记录原始错误码）。

## 2. 测试（新行为测试须在改动前的代码上变红，实现者贴出证据）

- [x] 2.1 第 1 部分正例（design evidence 1）：经 stage 执行入口与文件 journal repository，`state_save_qc` 提交抛出 gateway
      `SLURM_PARSE_ERROR`（ambiguous，已进边界）→ `submission_failed` / `STATE_SAVE_SUBMIT_AMBIGUOUS`，事件
      `origin_error_code == "SLURM_PARSE_ERROR"`，`FileJournalRetryService.should_auto_retry` 为真。
- [x] 2.2 第 1 部分反例（evidence 2）：REJECTED → 原始错误码且不自动重试；`convert` 同样的 ambiguous 错误 → 原始错误码；
      未进入 gateway 边界 → 原始错误码。
- [x] 2.3 登记（evidence 3）：两张 transient 表与 classifier；已有 transient 覆盖守卫测试按需更新，保持通过。
- [x] 2.4 脚本提示正例（evidence 4），journal 形态仿照生产（见 design「Seams under test」），行带真实时间戳。
- [x] 2.5 脚本提示反例（evidence 5 a-e）；多成员 cohort 的 `cohort_members` 放在 forcing/forecast 行上。
- [x] 2.6 已有测试保持通过：`tests/test_node22_manual_retry_failed_runs.py`、manual-retry 选择器与 DB `RetryService` 的拒绝测试。
- [x] 2.7 真实阶段循环（`orchestrate_cycle`，`FileJournalRetryService` + `RetryConfig(max_retries=1, backoff_schedule=[0])`）：
      gateway 先 502 `SLURM_PARSE_ERROR` 再 201 → 第二次 POST、`_state_save_qc_retry_1` 成功、retry 事件
      `previous_error == "STATE_SAVE_SUBMIT_AMBIGUOUS"`、cycle 完成。
- [x] 2.8 runbook 的耗尽口径：重试耗尽 → 行 `permanently_failed` / `STATE_SAVE_SUBMIT_AMBIGUOUS`，调度器判
      `permanent_failure` / `permanent_failure_guard`；行仍为 `submission_failed`（永久标记前中断）时才是 `retry_limit_exhausted`。
- [x] 2.9 脚本提示只看每个 cohort 最新的 `state_save_qc` 行：base 行失败、`_retry_1` 更晚且成功 → 不列出；反向 → 列出 `_retry_1`。

## 3. 验证

- [x] 3.1 `uv run pytest -q`，覆盖被改模块的直接测试文件、`tests/test_retry.py`、`tests/test_node22_manual_retry_failed_runs.py`，
      以及对每个被改文件执行 `uv run python scripts/select_ci_tests.py --changed-file <file>` 选出的集合
      （注意：该命令把结果写入 GitHub output 格式；要确认实际选中的列表非空后再跑）。
- [x] 3.2 `uv run ruff check` 被改文件。
- [x] 3.3 `openspec validate state-save-ambiguous-submit-recovery --strict --no-interactive`。
- [x] 3.4 `.large-file-guard.json`：确认被改文件没有新超出 1000 行上限又不在豁免名单中；若有，报告，不要擅自改。
      结果（偏离，已在 PR 偏离记录中说明）：`chain_stage_execution.py`（1482→1513）与 `chain_forecast_orchestrator_cycle.py`
      （1045→1047）在 master 上已超限，拆分超出本单范围；编排者按 guard 自身给出的处置在 `exclude` 中登记了这两项。
- [x] 3.5 CI 选择：`scripts/select_ci_tests.py` 把 `tests/test_state_save_submit_ambiguity.py` 挂到 `services/orchestrator/**`
      目录规则、`CHAIN_IMPORTER_TESTS`、`FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS`；对 `chain_stage_execution.py`、
      `chain_forecast_submission.py` 执行选择器，结果含该文件。
