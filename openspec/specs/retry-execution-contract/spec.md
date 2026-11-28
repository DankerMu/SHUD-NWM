# retry-execution-contract Specification

## Purpose
Pin retry-execution edge cases that survive the manual-retry submit lanes: ambiguous state_save_qc submits,
manual-retry preview routing to the cohort master, and the manual-repair durable-success set.

## Requirements

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

When the manual-retry script of the compute role previews a hydro run id and the answer is `no_retryable_failed_job`, the preview SHALL list, read-only, every cohort master row of the same cycle whose `state_save_qc` job is in a failed status and whose recorded cohort membership includes the run's model, with its run id, job id, status, error code, and member count, together with a warning that marking it re-runs the whole cohort from convert. A failed master SHALL NOT be listed when a later `state_save_qc` row of a `cycle_<source>_<stamp>_*` cohort run of the same cycle has succeeded and provably covers the same model under the same membership rule that decides listing (a single-model row by its `model_id`; a model-less row only through complete recorded `cohort_members`); "later" is ordered by the journal's retry truth sort key, and a row whose coverage cannot be proven supersedes nothing. The selector's refusal, the script's exit codes, and every other preview answer SHALL be unchanged, and a failure of the hint query SHALL NOT change the preview decision.

#### Scenario: forecast succeeded and the cohort master's state_save_qc permanently failed

- **WHEN** the operator previews `fcst_ifs_2026092212_<model>`, whose hydro run succeeded, and the single-model cohort master `cycle_ifs_2026092212_convert_<model>` has a `permanently_failed` `state_save_qc` row while a multi-model cohort of the same cycle has a succeeded one
- **THEN** the preview is refused with `no_retryable_failed_job` and lists exactly the single-model cohort master with `member_count` 1 and the whole-cohort warning
- **THEN** previewing the listed cohort run id yields `would_mark` for its `state_save_qc` job

#### Scenario: no failed cohort covers the model

- **WHEN** no cohort master of the cycle covering the model has a failed `state_save_qc` row
- **THEN** the preview is refused as before and lists no cohort candidates

#### Scenario: a master recovered under a new cohort run id is not listed

- **WHEN** the failed master `cycle_ifs_2026092212_convert_<model>` has a `permanently_failed` `state_save_qc` row and a later `cycle_ifs_2026092212_full_<model>` cohort has a succeeded `state_save_qc` row carrying the same `model_id`
- **THEN** previewing `fcst_ifs_2026092212_<model>` is still refused with `no_retryable_failed_job`
- **AND** `cohort_candidates` is empty and no `warning` is emitted

#### Scenario: a failure after the last success is still listed

- **WHEN** the covering succeeded `state_save_qc` row is earlier than the failed master's latest `state_save_qc` row
- **THEN** the failed master is listed in `cohort_candidates`

#### Scenario: a later success whose coverage cannot be proven supersedes nothing

- **WHEN** a later model-less succeeded `state_save_qc` row belongs to a cohort whose recorded membership is incomplete (for example a blank member `model_id`)
- **THEN** the failed master is still listed with its `member_count` and the whole-cohort warning

### Requirement: The manual-repair durable-success set is named distinctly from the pipeline durable-success set

The manual-retry refusal predicate SHALL consume a status set — on the file-journal manual-repair path (the shared manual-retry source selector used by `FileJournalRetryService.record_manual_repair`) — whose name is distinct from
`scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`, because the two
sets deliberately differ in membership (`"complete"` counts as durable
success for scheduler decisions but does not block a manual retry) and a
shared name invites an accidental merge that would silently change one
predicate's behavior. The membership relationship between the two sets
SHALL be pinned by a test so that any drift on either side — or a rename
back into collision — fails loudly. This change is naming-only: neither
predicate's behavior, membership, exception shape, nor caller surface
changes.

#### Scenario: the membership divergence is explicit and locked

WHEN the manual-retry set and the scheduler durable-success set are
compared
THEN the manual-retry set equals exactly `{"succeeded", "parsed",
"published"}`, the scheduler set equals exactly `{"succeeded", "parsed",
"published", "complete"}` (pinned separately, so collapsing the scheduler
set down to three members — the one merge direction that would change
behavior — also fails), the manual-retry set equals the scheduler set
minus `"complete"`, and a regression test asserts all three relationships

#### Scenario: manual-repair behavior is unchanged

WHEN a run's durable hydro status is one of the three manual-retry
members
THEN the file-journal manual-retry source selection refuses (no retry source; `record_manual_repair` raises `RetryNotFoundError`) exactly as before the rename, pinned by the file-journal refusal-arm test; and `"complete"` — absent from the manual-retry set and not a `hydro.run_status` enum value but representable on the file-journal lane —
continues not to trigger the refusal, asserted both on the file-journal lane and at the constant level
