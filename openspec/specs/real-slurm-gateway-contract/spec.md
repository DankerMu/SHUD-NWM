# real-slurm-gateway-contract Specification

## Purpose
TBD - created by archiving change m7-second-review-remediation. Update Purpose after archive.
## Requirements
### Requirement: Structured real Slurm submit contract
Orchestrator and RealSlurmGateway SHALL share one explicit structured submit contract for single jobs and array jobs.

#### Scenario: Single-job manifest survives the FastAPI route boundary
- **WHEN** the orchestrator submits a single job through `/api/v1/slurm/jobs`
- **THEN** `run_id`, `model_id`, `job_type`, and manifest fields MUST be available to RealSlurmGateway template rendering
- **AND** top-level request fields MUST override same-named nested manifest fields

#### Scenario: Array manifest survives the FastAPI route boundary
- **WHEN** the orchestrator submits an array job through `/api/v1/slurm/job-arrays`
- **THEN** `job_type`, `cycle_id`, `stage_name`, `tasks`, and nested `manifest` fields MUST be available to RealSlurmGateway template rendering
- **AND** top-level request fields MUST override same-named nested manifest fields

#### Scenario: Object store roots are rendered into real Slurm templates
- **WHEN** an array job is submitted with `object_store_root` and `object_store_prefix` in the orchestrator manifest
- **THEN** the rendered sbatch script MUST export those values to worker processes as `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX`
- **AND** the script MUST NOT silently fall back to `WORKSPACE_ROOT` for durable artifacts

### Requirement: Production jobs use fixed templates or constrained script mode
Real Slurm execution SHALL use fixed configured templates unless a constrained script mode is explicitly implemented and tested.

#### Scenario: Legacy single-job path submits to real Slurm
- **WHEN** a legacy or analysis orchestration path submits a single job to RealSlurmGateway
- **THEN** the job MUST resolve to an available configured template or a validated constrained script mode
- **AND** unsupported legacy `job_type` values MUST fail before submission with a clear validation error

#### Scenario: Template ownership is documented
- **WHEN** developers inspect Slurm template documentation
- **THEN** the docs MUST state which paths are canonical for real Slurm, which are legacy, and which orchestrator paths still use them

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

### Requirement: Durable Slurm log lookup
Real Slurm log retrieval SHALL work after gateway restart, SHALL support array task logs, and SHALL never label one member's logs with another member's run or model identity. Production array scheduler logs SHALL be written to a cohort-neutral, submission-specific directory bound to the immutable manifest index; the directory SHALL exist before `sbatch` is invoked. Array log identity joins and restart discovery SHALL be bounded and SHALL fail safe: readable log content remains available when identity metadata is absent or invalid, but unproven identity MUST NOT be returned as complete.

#### Scenario: Master array job log request aggregates task logs with identity
- **WHEN** logs are requested for an array master job id whose exact manifest index is available
- **THEN** the gateway MUST return available `%A_%a.out` and `%A_%a.err` content grouped by task id and annotated with that entry's `model_id` and `run_id`
- **AND** missing task logs MUST be reported without replacing existing task logs with an empty payload

#### Scenario: Array templates use a cohort-neutral directory
- **WHEN** any production array template is rendered for members with different run ids
- **THEN** its output, error, and directory-creation paths MUST use one submission-specific neutral log directory and MUST NOT contain the first or any other member's run id
- **AND** submission MUST safely create that directory before invoking `sbatch`

#### Scenario: Direct array-template submitters preserve the rendered log binding
- **WHEN** a supported production acceptance or validation lane renders a production array template and invokes `sbatch` directly instead of `submit_job_array`
- **THEN** it MUST derive the same neutral directory from the workspace, cycle, and immutable manifest index through the canonical path contract and safely create it before invoking `sbatch`
- **AND** every post-submit task-log check and emitted evidence path MUST read that exact directory rather than a legacy member-run directory
- **AND** no-submit, blocked-preflight, and fake-validation lanes MUST NOT create the shared scheduler-log directory

#### Scenario: Gateway restart recovers exact array identity
- **WHEN** the gateway process restarts before logs are fetched
- **THEN** deterministic neutral-path discovery MUST locate the task logs and derive the one exact immutable manifest index for that submission without choosing a newest or otherwise guessed index
- **AND** the API response MUST indicate when record or identity metadata is incomplete

#### Scenario: Historical leader-run logs remain readable
- **WHEN** an array log exists only at the legacy `workspace/<leader-run-id>/logs/%A_%a` location
- **THEN** new log lookup MUST still return its available content
- **AND** it MUST annotate member identity only when the exact corresponding manifest index is proven

#### Scenario: Invalid identity metadata does not mislabel or erase logs
- **WHEN** the associated manifest index is missing, unsafe, oversized, malformed, ambiguous, or does not contain the requested task id
- **THEN** bounded lookup MUST return available log content with identity marked incomplete
- **AND** it MUST NOT guess a `model_id` or `run_id`, block on a non-regular file, or raise an unclassified exception

### Requirement: sacct timestamps enter job records as timezone-aware UTC

Naive sacct timestamps SHALL be interpreted in the gateway process's
local timezone and converted to UTC before entering
`SlurmJobRecord` time fields. `sacct` prints `Start`/`End` as bare
ISO wall-clock strings in the invoking environment's local timezone;
the gateway and its `sacct` subprocess share one TZ environment, so
local-time interpretation is exact. Values carrying an explicit
offset or "Z" suffix are converted (not relabeled) to UTC. The
parser's sentinel semantics (empty/"Unknown"/"None"/"N/A" → absent;
unparseable or unconvertible → the same parse error) are unchanged
in kind.

#### Scenario: Naive local timestamp is converted, not relabeled

- **WHEN** the gateway host runs in a non-UTC timezone (e.g. CST,
  UTC+8) and `sacct` reports a terminal job's `End` as a bare local
  wall-clock string
- **THEN** the parsed `finished_at` is the same instant expressed in
  UTC (local minus the host offset), never the local wall-clock
  digits with a UTC label

#### Scenario: Offset-carrying timestamp is timezone-independent

- **WHEN** a timestamp carries an explicit "Z" or offset suffix
- **THEN** parsing yields the same UTC instant regardless of the
  gateway host's local timezone

#### Scenario: Records never carry naive datetimes

- **WHEN** any sacct-sourced time field is populated on a
  `SlurmJobRecord`
- **THEN** the value is timezone-aware with zero UTC offset, so
  downstream `_ensure_utc`-style consumers take their aware branch
  and the journal's "Z"-suffixed serialization is truthful

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

### Requirement: Default sacct lookback windows are rendered in the host's local wall clock

The gateway SHALL render the default `--starttime` lookback boundary it
passes to sacct in the host's local wall-clock representation of the
UTC-computed instant, because sacct interprets bare timestamps in the
host's local timezone: the default window is computed as UTC now minus
the configured lookback and converted with the host's local timezone
before formatting, so the effective window is the configured width on
every host timezone instead of silently widening east of UTC and
narrowing west of it — except across the once-yearly ambiguous local
hour on DST-observing hosts, where a bare local timestamp is irreducibly
ambiguous to sacct's timezone-less interface and the boundary may land
up to an hour off (inherent to sacct, not to this conversion;
spring-forward is safe because a UTC-to-local conversion never emits a
skipped wall clock). Explicitly supplied start or end times keep their
existing caller-owned semantics without re-conversion, and on a UTC host
the rendered value is byte-for-byte what it was before.

#### Scenario: a negative-offset host keeps the full lookback window

WHEN the host timezone is west of UTC and list_jobs runs with no explicit
start time
THEN the rendered `--starttime` equals the local wall-clock form of UTC
now minus the configured lookback (the window is not narrowed)

#### Scenario: a positive-offset host stops silently widening the window

WHEN the host timezone is east of UTC (the production node-22 case) and
list_jobs runs with no explicit start time
THEN the rendered `--starttime` equals the local wall-clock form of UTC
now minus the configured lookback — the effective window narrows from
the accidental lookback-plus-offset width to the declared lookback width

#### Scenario: a UTC host renders the same value as before

WHEN the host timezone is UTC
THEN the rendered `--starttime` string is byte-for-byte identical to the
pre-change output

#### Scenario: explicit caller times are not re-converted

WHEN a caller supplies an explicit start time
THEN it is passed through byte-for-byte unchanged

