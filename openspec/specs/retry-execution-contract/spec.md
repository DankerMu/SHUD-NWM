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

