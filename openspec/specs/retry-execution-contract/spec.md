# retry-execution-contract Specification

## Purpose
TBD - created by archiving change m8-fourth-review-remediation. Update Purpose after archive.

## Requirements

### Requirement: Manual retry creates executable work

Manual retry SHALL not stop at a stranded pending pipeline job.

#### Scenario: Retry submission path is available

WHEN an operator calls `POST /api/v1/runs/{run_id}/retry` for a retryable failed run
THEN the system MUST either submit retry work to Slurm before returning success or enqueue it for a durable consumer
AND the response MUST expose whether execution is `queued`, `submitted`, or `running`
AND a submitted retry MUST include `slurm_job_id`.

#### Scenario: Pending retry is consumed

WHEN a retry job is queued as `pending`
THEN a documented consumer MUST pick it up
AND update `slurm_job_id`, `submitted_at`, status, and pipeline events after submission
AND record enough ownership or lease metadata to prevent duplicate consumers from submitting the same retry simultaneously
AND concurrent consumers MUST NOT submit the same retry job twice.

#### Scenario: Retry cannot execute

WHEN the retry execution path is unavailable
THEN the API MUST return an error instead of a success envelope
AND it MUST NOT leave a pending job that blocks future retries indefinitely.

#### Scenario: Retry response exposes execution state

WHEN retry succeeds
THEN the response MUST expose an execution status of `queued`, `submitted`, or `running`
AND submitted or running responses MUST include `slurm_job_id`
AND queued responses MUST identify the consumer or queue path responsible for later submission.

### Requirement: Retry active guards do not deadlock

Pending retry jobs SHALL not permanently block operational recovery.

#### Scenario: Stale pending retry is detected

WHEN a pending retry exceeds the configured lease or submission timeout
THEN it MUST transition to a failed retry state with a stable error code
AND a later retry attempt MUST be possible if retry policy allows it.

### Requirement: The manual-retry durable-success set is named distinctly from the pipeline durable-success set

The manual-retry refusal predicate SHALL consume a status set — on both
the DB-backed path and its file-journal twin — whose name is distinct from
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

#### Scenario: manual retry behavior is unchanged

WHEN a run's durable hydro status is one of the three manual-retry
members
THEN both manual-retry paths continue to refuse the retry exactly as
before the rename — the DB lane pinned by its existing parametrized
refusal test, the file-journal lane pinned by a new refusal-arm test
(that arm had no coverage before this change); and `"complete"` — absent
from the manual-retry set and unreachable on the DB lane (it is not a
`hydro.run_status` enum value) but representable on the file-journal
lane — continues not to trigger the refusal, asserted both on the
file-journal lane and at the constant level

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
