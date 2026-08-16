## MODIFIED Requirements

### Requirement: Retry Guard — Non-Transient Error Exclusion

The Orchestrator SHALL NOT automatically retry jobs that failed with non-transient error codes.

#### Scenario: Non-transient error codes block auto-retry

- **WHEN** a `pipeline_job` fails with one of the following error codes:
  - `INVALID_MANIFEST` — manifest file is malformed or missing required fields
  - `PERMISSION_DENIED` — insufficient permissions to access resources
  - `OUTPUT_INCOMPLETE` — output schema validation failed (data integrity error, not retryable)
  - `TEMPLATE_NOT_ALLOWED` — sbatch template rejected by security policy
  - `MANIFEST_SCHEMA_INVALID` — manifest file fails JSON schema validation
  - `OUT_OF_MEMORY` — Slurm OOM kill (configuration error: memory_gb too low for workload, not transient)
- **THEN** the Orchestrator MUST NOT schedule an automatic retry
- **THEN** the Orchestrator SHALL mark the job as permanently failed immediately
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"auto_retry_skipped": true, "reason": "non_transient_error", "error_code": "<code>"}`

#### Scenario: Transient error codes allow auto-retry

- **WHEN** a `pipeline_job` fails with one of the following error codes:
  - `SLURM_TIMEOUT` — Slurm walltime exceeded
  - `NODE_FAILURE` — compute node crashed or became unreachable
  - `STORAGE_WRITE_FAILED` — transient storage I/O error
  - `SBATCH_SUBMISSION_FAILED` — sbatch command returned non-zero (transient Slurm scheduler issue)
  - `SLURM_UNAVAILABLE` — Slurm controller unreachable at submission time
- **THEN** the Orchestrator SHALL proceed with automatic retry (subject to `max_retries` and backoff)

#### Scenario: Unknown error code defaults to non-transient

- **WHEN** a `pipeline_job` fails with an error code not listed in the non-transient or transient lists
- **THEN** the Orchestrator SHALL treat it as non-transient and MUST NOT schedule an automatic retry
- **THEN** a `pipeline_event` SHALL be appended with `details_json` containing `{"auto_retry_skipped": true, "reason": "unknown_error_code_defaulted_non_transient", "error_code": "<code>"}`
- **THEN** the Orchestrator SHALL log a warning: `"unknown error_code '<code>' defaulted to non-transient — add to classification list"`

#### Scenario: Classification parity between this requirement and code is test-anchored

- **WHEN** the repository test suite runs
- **THEN** a test SHALL read this requirement's non-transient code list from the spec text and assert that `OUT_OF_MEMORY` appears there, is a member of the orchestrator's non-transient classification set, and is absent from every transient classification surface (`TRANSIENT_ERROR_CODES` and the scheduler-state transient retry-reason set), so that a reopened spec-code drift on this code fails the suite rather than surviving to review

#### Scenario: auto_retry_skipped payload rides the permanently_failed event

- **WHEN** the guard blocks automatic retry for a non-transient or unknown error code and marks the job permanently failed
- **THEN** the `auto_retry_skipped` payload is merged into the existing `permanently_failed` pipeline event's `details_json` (no separate event type), constructed by a single shared helper consumed by both the DB plane and the file-journal plane so the reason literals have exactly one source

#### Scenario: the auto_retry_skipped key appears iff a recorded error code is classification-blocked

- **WHEN** a job is marked permanently failed because its TRANSIENT error code exhausted the retry budget, or because it reached permanent failure with no recorded error code
- **THEN** the `permanently_failed` event's `details_json` does NOT contain the `auto_retry_skipped` key — the key appears if and only if a recorded error code was blocked by classification; the keyless cases (no recorded code vs. retries exhausted) are distinguished from each other by the existing `failure.limit_exhausted` / `failure.reason_code` fields
- **AND** a job with a NON-TRANSIENT recorded error code carries the key even when its attempt count also exceeds the retry limit (classification blocks first)

#### Scenario: db-free scheduler plane disposition

- **WHEN** the db-free scheduler plane handles a guard-blocked failure
- **THEN** the portion flowing through `FileJournalRetryService` emits the payload via the file-journal sink
- **AND** the pure-adjudication path (`scheduler_state_failure._failure_policy_payload`) remains sink-free per its `pipeline_event_writes_proven_absent` evidence contract and is NOT required to write pipeline events
- **AND** the unknown-code warning obligation is likewise exempted on the pure-adjudication path (it is re-evaluated every scheduler pass with no append anchor; the single warning is emitted where the mark event lands, at the file-journal sink)
