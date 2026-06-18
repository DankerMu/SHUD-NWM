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

