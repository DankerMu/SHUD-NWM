# Tasks: slurm-error-code-transient-coverage

## 1. 实现

- [x] 1.1 `services/slurm_gateway/real_backend.py`（design D1）：
      `map_slurm_error_code` 增 `DEADLINE` → `SLURM_DEADLINE`、`BOOT_FAIL`
      并入 `NODE_FAILURE` 分支；`SLURM_STATE_MAP` 增 `BOOT_FAIL` →
      `SlurmJobStatus.FAILED`（具名承担 reconcile.py:1224 cohort 投影行为
      正确化，D1/P1-2）；`REVOKED`/`SPECIAL_EXIT` 具名不映（注释记裁决）；
      :1422-1423 raw state 入 manifest 行为不动。
- [x] 1.2 分类三面（design D2/P1-1）：`SLURM_DEADLINE` 入
      `retry.TRANSIENT_ERROR_CODES` + classifier `transient_slurm_runtime`
      集合 + `scheduler_state_types.TRANSIENT_RETRY_REASON_CODES`；
      `failure_classifier` 增 `SLURM_JOB_FAILED` 显式分支（尾部默认之前，
      行为等价，注释点名裁决与 resume 后果）；各集合既有成员不删不移。
- [x] 1.3 双 spec delta（design D3）：`real-slurm-gateway-contract`
      MODIFIED「Retryable Slurm errors are stable」（保留 RealSlurmGateway
      主语；新增 DEADLINE 场景；node-failure WHEN 扩 BOOT_FAIL；未知终态
      场景补显式契约、resume 本体引用 job-retry-mechanism；TIMEOUT/OOM/
      poll-timeout 原文不动）；`job-retry-mechanism` MODIFIED「Retry
      Guard — Non-Transient Error Exclusion」（瞬时清单加 SLURM_DEADLINE，
      其余场景逐字照抄）。

## 2. 测试（先红后绿；红证锚定 2.3 的 resume 放行方向）

- [x] 2.1 `tests/test_real_slurm_gateway.py`：`map_slurm_error_code` 逐格
      （DEADLINE/BOOT_FAIL/REVOKED/SPECIAL_EXIT/裸 FAILED/垃圾串）——纯函数
      直测与既有 sacct-fake 端到端并存不替换（N3）；
      `_record_from_sacct_fields` DEADLINE 终态端到端（error_code=
      SLURM_DEADLINE + manifest `slurm_raw_state="DEADLINE"`）；BOOT_FAIL
      经 `_map_slurm_state` 归 FAILED 且无 "Unmapped" warning（caplog）；
      **更新既有两处**（P2-3）：:967 参数化 BOOT_FAIL 格改 NODE_FAILURE、
      `test_unknown_terminal_produces_slurm_job_failed_error_code` 代表态
      BOOT_FAIL 换 REVOKED；**BOOT_FAIL cohort 投影方向测试**（P1-2/
      R2-N1，seam 具名）：克隆 `test_file_cohort_terminal_tasks_project_
      exact_success_failure_and_restart`（tests/test_gateway_reconcile.py
      :999，raw_state 换 BOOT_FAIL）⇒ outcome=failed +
      error_code=NODE_FAILURE（**今天必红**：outcome=unverified、
      action=task_accounting_incomplete——I7 天然红-绿锚）。
- [x] 2.2 `tests/test_retry.py` + 扩充既有
      `test_slurm_error_codes_align_with_retry_sets`
      （test_real_slurm_gateway.py:1029-1035）：
      `is_retryable_failure("SLURM_DEADLINE")` True + classifier
      `transient_slurm_runtime` + `classify_failure` 限额内
      permanent=False；`SLURM_JOB_FAILED` classifier ==
      `unknown_failure` + **全不入钉测**（not in TRANSIENT and not in
      NON_TRANSIENT and not in TRANSIENT_RETRY_REASON_CODES）+
      `auto_retry_skipped_details` reason 仍为
      `unknown_error_code_defaulted_non_transient`；
      `TRANSIENT_ERROR_CODES ∩ NON_TRANSIENT_ERROR_CODES == ∅` +
      **`TRANSIENT_ERROR_CODES == TRANSIENT_RETRY_REASON_CODES` 相等钉测**。
- [x] 2.3 resume 两方向主锚（红-绿，seam 具名）：
      `test_downstream_resume_keeps_recorded_transient_codes`
      （test_production_scheduler.py:22633）参数化**加格
      `SLURM_DEADLINE`**（**接线前红**：未知码 → permanent →
      action="blocked"）；**预算耗尽 reason 锚**（P1-1/R2-P2-3）：
      `test_downstream_resume_refuses_recorded_transient_code_with_
      exhausted_budget`（:22648，现用 NODE_FAILURE）克隆/参数化加
      `SLURM_DEADLINE` 一格，断 `reason == "retry_limit_exhausted"`
      （**接线前必红**：未登记第二面 → permanent_failure_guard）；
      **recompute 通道方向**（R2-N2）：
      `test_missing_forecast_output_recompute_channel_is_unchanged`
      （:22796 参数化）加格 `SLURM_DEADLINE`；反方向
      `test_downstream_resume_refuses_recorded_non_transient_codes`
      （:22600-22628，已含 SLURM_JOB_FAILED）保绿。
- [x] 2.4 既有映射回归：TIMEOUT/NODE_FAIL/PREEMPTED/OOM 四路既有格与
      CANCELLED 非 FAILED 态不发码，原断言保绿（BOOT_FAIL 一格按 2.1
      更新，为本 change 唯一既有断言改动）。
- [x] 2.5 anchor 保全：`test_repaired_raw_manifest_allows_stale_downstream_
      failure_retry`（tests/test_production_scheduler.py:22135）**零改动**
      且绿；`uv run pytest -q tests/test_production_scheduler.py -k
      "downstream or resume or raw_manifest"` 全绿。

## 3. 验证（Evidence Floor，per issue Verification）

- [x] 3.1 `uv run pytest -q tests/test_real_slurm_gateway.py
      tests/test_retry.py tests/test_reconcile_sacct_parse.py
      tests/test_gateway_reconcile.py tests/test_production_slurm_validation.py`
      通过（issue Verification 写的 `tests/test_reconcile.py` 不存在，以两
      个 reconcile 既有文件为准；补 slurm_validation——:1135 是
      map_slurm_error_code 调用点；偏离已记录）。
- [x] 3.2 `uv run pytest -q tests/test_production_scheduler.py -k
      "downstream or resume or raw_manifest"` 通过。
- [x] 3.3 `uv run ruff check .` 通过。
- [x] 3.4 `openspec validate slurm-error-code-transient-coverage --strict
      --no-interactive` 通过。
- [ ] 3.5 node-27 oracle（merge 后标准循环）：`ssh -p 32099
      nwm@210.77.77.27 'cd /home/nwm/NWM && git pull --ff-only && uv run
      pytest -q tests/test_real_slurm_gateway.py
      tests/test_production_scheduler.py'` 通过，结果记入 issue/PR；若失败
      立即 hotfix follow-up。
- [ ] 3.6 D4 受影响面四处硬写点 + 五处调用点核对结论记录（PR body）。
