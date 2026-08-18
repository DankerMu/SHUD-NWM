## ADDED Requirements

### Requirement: Terminal state map covers the terminal-state vocabulary and empty states normalize safely

`SLURM_STATE_MAP` SHALL contain every state that
`services/production_closure/slurm_validation.py` `TERMINAL_SLURM_STATES`
enumerates (including `REVOKED` and `SPECIAL_EXIT`, mapped to FAILED), so the
default-less file-cohort task projection cannot strand a cohort in
`task_accounting_incomplete` on a terminal state; this registration is
orthogonal to `map_slurm_error_code`, whose deliberately-unmapped verdict for
these states (falling to `SLURM_JOB_FAILED`) is unchanged. State
normalization SHALL treat empty or whitespace-only raw states as the existing
`UNKNOWN` fallback instead of raising, in both the gateway and the
production-closure sibling copy.

#### Scenario: REVOKED or SPECIAL_EXIT array task projects failed, cohort stays accountable

WHEN a file-cohort array task's sacct raw state is REVOKED or SPECIAL_EXIT
THEN the task projection reports outcome failed with accounting complete
AND the cohort outcome action is terminal, not task_accounting_incomplete

#### Scenario: empty sacct State field converges to UNKNOWN, not IndexError

WHEN a sacct row passes field-count validation but carries an empty or
whitespace-only State field
THEN state normalization returns UNKNOWN on every parse leg (status, list,
array-member aggregation) and no bare IndexError escapes the gateway contract

#### Scenario: terminal-state vocabulary cannot drift apart again

WHEN TERMINAL_SLURM_STATES gains a state absent from SLURM_STATE_MAP
THEN a committed meta assertion fails

## MODIFIED Requirements

### Requirement: Retryable Slurm errors are stable

RealSlurmGateway SHALL map raw Slurm terminal states to stable control-plane error codes while preserving raw state details, and the retry classification of every emitted code is an explicit contract: transient infrastructure and scheduling failures receive codes registered on every transient classification surface (eligible for automatic retry and downstream resume, subject to retry limits), while terminal states without a specific mapping receive the generic unknown code, which stays deliberately unregistered and therefore defaults to non-transient as the job-retry-mechanism unknown-code clause prescribes — automatic downstream resume is refused and the failure waits for operator adjudication, with raw Slurm state metadata preserved in every case.

#### Scenario: Timeout becomes retryable

- **WHEN** `sacct` reports `TIMEOUT`
- **THEN** the pipeline job MUST persist `status=failed`, `error_code=SLURM_TIMEOUT`, and raw state metadata
- **AND** RetryService MUST treat the job as eligible for retry subject to retry limits

#### Scenario: Deadline termination becomes retryable

- **WHEN** `sacct` reports `DEADLINE`
- **THEN** the pipeline job MUST persist `error_code=SLURM_DEADLINE` and raw state metadata
- **AND** RetryService MUST treat the job as eligible for retry subject to retry limits, and automatic downstream resume MUST treat the failure as non-permanent

#### Scenario: Node failure becomes retryable

- **WHEN** `sacct` reports `NODE_FAIL`, `PREEMPTED`, or `BOOT_FAIL`
- **THEN** the pipeline job MUST persist `error_code=NODE_FAILURE`
- **AND** RetryService MUST treat the job as eligible for retry subject to retry limits

#### Scenario: Out of memory preserves a stable error code

- **WHEN** `sacct` reports `OUT_OF_MEMORY`
- **THEN** the pipeline job MUST persist `error_code=OUT_OF_MEMORY`
- **AND** raw Slurm state metadata MUST be preserved for operator diagnosis

#### Scenario: Unknown terminal failure preserves raw state and refuses automatic resume

- **WHEN** `sacct` reports a terminal failure state without a specific mapping (including bare `FAILED`, states normalized to `UNKNOWN` — an empty or whitespace-only State field among them — and states `map_slurm_error_code` deliberately leaves unmapped, such as `REVOKED` or `SPECIAL_EXIT`; both are registered in `SLURM_STATE_MAP` for terminal accounting, which is orthogonal to this error-code verdict)
- **THEN** the pipeline job MUST persist the stable generic error code `SLURM_JOB_FAILED`
- **AND** that code is deliberately registered on no classification surface — neither transient set, nor the non-transient set: failure classification reports it as an unknown failure, and automatic retry and downstream resume refuse it as the job-retry-mechanism unknown-code default prescribes (the skip-reason and refusal-reason literals live in that spec's requirements, not here), so the cycle waits for operator adjudication instead of burning retry budget on a possibly-deterministic failure
- **AND** raw Slurm state metadata MUST be preserved for that adjudication

#### Scenario: Poll timeout is persisted

- **WHEN** orchestrator polling exceeds `job_timeout_seconds`
- **THEN** the pipeline job, related run or cycle, and event log MUST be updated to a terminal failed state with `SLURM_JOB_TIMEOUT`
- **AND** retry scheduling MUST evaluate that failure through the same retry policy as Slurm-reported failures
