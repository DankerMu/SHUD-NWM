## ADDED Requirements

### Requirement: An ambiguous state_save_qc submit is recorded as a transient failure

When a `state_save_qc` submission fails after the Slurm Gateway call boundary was entered and the failure is not a proven rejection, the orchestrator SHALL record the pipeline job with error code `STATE_SAVE_SUBMIT_AMBIGUOUS` (a registered transient code) and SHALL keep the gateway's original error code as `origin_error_code` in the submission-failure event details, so the row is retried automatically within the retry limit instead of being marked permanently failed. Proven rejections, submissions that never reached the gateway boundary, and every other stage SHALL keep their original error code and classification.

#### Scenario: gateway parse error on a state_save_qc submit

- **WHEN** a `state_save_qc` submit raises a gateway error with code `SLURM_PARSE_ERROR` and an ambiguous submit disposition after the gateway boundary was entered
- **THEN** the pipeline job is recorded as `submission_failed` with error code `STATE_SAVE_SUBMIT_AMBIGUOUS`
- **THEN** the submission-failure event details carry `origin_error_code: "SLURM_PARSE_ERROR"`
- **THEN** the retry service reports the job as eligible for automatic retry and does not mark it `permanently_failed`

#### Scenario: proven rejection keeps its original code

- **WHEN** the same `state_save_qc` submit fails with a `REJECTED` submit disposition
- **THEN** the job keeps the gateway's original error code and is not auto-retried, as before

### Requirement: The manual-retry preview points a refused hydro run at its cohort master

When the node-22 manual-retry script previews a hydro run id and the answer is `no_retryable_failed_job`, the preview SHALL list, read-only, every cohort master row of the same cycle whose `state_save_qc` job is in a failed status and whose recorded cohort membership includes the run's model, with its run id, job id, status, error code, and member count, together with a warning that marking it re-runs the whole cohort from convert. The selector's refusal, the script's exit codes, and every other preview answer SHALL be unchanged, and a failure of the hint query SHALL NOT change the preview decision.

#### Scenario: forecast succeeded and the cohort master's state_save_qc permanently failed

- **WHEN** the operator previews `fcst_ifs_2026092212_<model>`, whose hydro run succeeded, and the single-model cohort master `cycle_ifs_2026092212_convert_<model>` has a `permanently_failed` `state_save_qc` row while a multi-model cohort of the same cycle has a succeeded one
- **THEN** the preview is refused with `no_retryable_failed_job` and lists exactly the single-model cohort master with `member_count` 1 and the whole-cohort warning
- **THEN** previewing the listed cohort run id yields `would_mark` for its `state_save_qc` job

#### Scenario: no failed cohort covers the model

- **WHEN** no cohort master of the cycle covering the model has a failed `state_save_qc` row
- **THEN** the preview is refused as before and lists no cohort candidates
