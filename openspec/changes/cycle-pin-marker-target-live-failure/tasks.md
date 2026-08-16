# Tasks: cycle-pin-marker-target-live-failure

## 1. 实现

- [x] 1.1 在 `services/orchestrator/scheduler_state_manual_retry.py` 抽共享行级
      谓词 `_job_row_is_live_failure(job)`：
      `¬_pipeline_job_is_repaired_stage_evidence(job)` ∧
      `¬_job_is_unsubmitted_auto_retry_placeholder(job)` ∧
      `_manual_retry_blocking_pipeline_status(status)` ∧
      `status ∉ ACTIVE_PIPELINE_STATUSES`；
      `_job_is_live_candidate_scope_failure` 改为
      `¬cycle-scope ∧ _job_row_is_live_failure`，
      `_cycle_scope_marker_pins_attempt` 的状态臂改为
      `not _job_row_is_live_failure(job)` —— 两侧由构造同源。
- [x] 1.2 改写 `_cycle_scope_marker_pins_attempt` docstring：删除「The two sides
      of this rule do NOT share one status domain … tracked separately by
      #1294」段，改为记录两侧同源于共享谓词的事实；
      `_unresolvable_marker_entity_pins_attempt` docstring 中引用 #1294 的
      「narrower bare FAILED」句同步修正。
- [x] 1.3 spec 措辞：本 change 的 MODIFIED delta 已含主 spec 的全部措辞变更
      （「still in a failed status」、两处「no longer failed (stale)」、
      scenario WHEN 与新增 cancelled 判别 AND 分支）；主 spec
      `openspec/specs/job-retry-mechanism/spec.md` 的落库发生在 merge 后
      `openspec archive`，**不在本 PR diff 内**。实现时核对 delta 与最终
      谓词语义一致即可。

## 2. 测试（判别锚，当前 1084 例零判别力）

- [x] 2.1 臂 1 判别：`failed_stage="download"` +
      `_decision_path_cycle_download_job(status="cancelled")` + own forecast
      `failed` + marker `retry_count=5` →
      `_manual_retry_new_attempt(prev=0) == 5` 且
      `_manual_retry_payload(...)["new_attempt"] == 5`（修复前为 1 / 缺键，
      需 red-proof）。
- [x] 2.2 臂 2 判别：无 `failed_stage` + cancelled 目标行 + own jobs 全
      succeeded → `new_attempt == 5`（修复前为 1）。
- [x] 2.3 回归护栏（参数化或逐例）：目标行
      `failed`/`permanently_failed`/`partially_failed`/`submission_failed`
      仍 `== 5`；目标行为 repaired stage evidence 或 unsubmitted auto-retry
      placeholder 时仍不钉；`succeeded` 目标行仍不钉；
      `test_cancelled_own_forecast_blocks_cross_stage_cycle_marker_pin`、
      `test_failed_hydro_run_blocks_cycle_marker_pin_beside_succeeded_jobs`、
      `test_cancelled_placeholder_shaped_row_blocks_the_pin_*`、
      `test_same_stage_cycle_marker_still_pins_when_the_candidates_own_row_is_cancelled`
      保持绿。

## 3. 验证（Evidence Floor）

- [x] 3.1 `uv run pytest -q tests/test_production_scheduler.py` 通过（允许
      deselect 既有 macOS 失败
      `test_db_free_slurm_storage_root_check_masks_symlink_loop_path`）。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate cycle-pin-marker-target-live-failure --strict
      --no-interactive` 通过。
- [x] 3.4 spec-compliance 人工证据：最终谓词定义（共享行级 live-failure
      谓词及两侧消费点）与 delta 的 live-failure 口径逐句对读一致（域 =
      failed-pipeline ∪ {cancelled}，排除 ACTIVE、repaired stage evidence、
      unsubmitted placeholder；row-absent 臂不在本 change 域内）。
