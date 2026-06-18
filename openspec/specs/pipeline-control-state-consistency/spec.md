# pipeline-control-state-consistency Specification

## Purpose
TBD - created by archiving change m6-system-hardening-alignment. Update Purpose after archive.
## Requirements
### Requirement: Cancel transitions all affected control-plane records
Canceling a run SHALL update active pipeline jobs, the corresponding hydro run, forecast cycle state when applicable, and pipeline events consistently.

#### Scenario: Active run cancellation is reflected in run listing
- **WHEN** an operator cancels a run with active pipeline jobs
- **THEN** `ops.pipeline_job` records MUST become `cancelled` and `hydro.hydro_run.status` MUST become `cancelled`

#### Scenario: Existing forecast cycle transitions on cancel
- **WHEN** an operator cancels a run that belongs to an existing non-terminal forecast cycle
- **THEN** the related forecast cycle MUST transition to the documented cancelled or partially-cancelled status in the same control-plane action

#### Scenario: Missing forecast cycle is handled explicitly
- **WHEN** an operator cancels a run whose forecast cycle record does not exist
- **THEN** the API MUST complete the job and run transition or return a clear non-retryable error without leaving partial state

#### Scenario: Published forecast cycle is not silently rolled back
- **WHEN** an operator cancels a run for a forecast cycle that is already published or otherwise terminal
- **THEN** the API MUST preserve the terminal publication state or write a documented compensating status rather than silently overwriting it

#### Scenario: Partial Slurm cancel failure is reflected locally
- **WHEN** one Slurm job cancel succeeds and another active Slurm job cancel fails for the same run
- **THEN** local job, run, cycle, and event records MUST represent the partial failure without reporting full cancellation

#### Scenario: Cancelled run does not block re-run
- **WHEN** a run has been cancelled successfully
- **THEN** active-pipeline guards MUST NOT treat that run as active for the same source, cycle, and model

#### Scenario: Already-terminal Slurm job cancellation is idempotent
- **WHEN** cancel is requested for a Slurm job that has already reached a terminal state
- **THEN** the API MUST return an idempotent terminal response or a clear non-retryable error without corrupting local state

### Requirement: Manual retry submits or explicitly queues executable work
Manual retry SHALL either submit a new Slurm job through the orchestrator path or create an explicit queued state that a worker is guaranteed to consume.

#### Scenario: Manual retry does not fake running state
- **WHEN** a failed run is manually retried but no Slurm job has been submitted yet
- **THEN** the hydro run MUST NOT be marked `running`

#### Scenario: Manual retry response states execution status
- **WHEN** the retry API returns success
- **THEN** the response MUST distinguish `queued` from `submitted` and include the pipeline job id and Slurm job id when available

#### Scenario: Duplicate manual retries are rejected
- **WHEN** a retry is already pending, queued, submitted, or running for the same run
- **THEN** a second manual retry request MUST return a conflict response and MUST NOT create another active retry job

### Requirement: State transitions are auditable
Retry and cancel operations SHALL write `ops.pipeline_event` records that include actor context when available, trigger type, previous status, next status, retry count, and previous error details.

#### Scenario: Retry event records prior failure
- **WHEN** a manual retry is accepted
- **THEN** a pipeline event MUST record trigger `manual`, previous job id, previous error code, and next retry count

#### Scenario: Cancel event records Slurm action
- **WHEN** a cancel request reaches Slurm or is treated idempotently
- **THEN** a pipeline event MUST record the Slurm job id and the local status transition

### Requirement: Control-plane tests cover database state alignment
The test suite SHALL verify retry and cancel behavior across `ops.pipeline_job`, `hydro.hydro_run`, `met.forecast_cycle`, and returned API payloads.

#### Scenario: Cancel integration test detects split-brain state
- **WHEN** cancel updates only pipeline jobs and leaves hydro runs active
- **THEN** an integration test MUST fail

#### Scenario: Retry integration test detects pending deadlock
- **WHEN** retry creates a pending job without submission or queue-consumer semantics
- **THEN** an integration test MUST fail

