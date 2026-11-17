# pipeline-control-state-consistency Specification

## Purpose
Keep control-plane records consistent when a run is cancelled: every affected record transitions together, each
transition is auditable, and tests cover the alignment.

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

### Requirement: State transitions are auditable
Retry and cancel operations SHALL write `ops.pipeline_event` records that include actor context when available, trigger type, previous status, next status, retry count, and previous error details.

#### Scenario: Retry event records prior failure
- **WHEN** a manual repair marker is recorded through `FileJournalRetryService.record_manual_repair`
- **THEN** a pipeline event MUST record trigger `manual`, previous job id, previous error code, and next retry count

#### Scenario: Cancel event records Slurm action
- **WHEN** a cancel request reaches Slurm or is treated idempotently
- **THEN** a pipeline event MUST record the Slurm job id and the local status transition

### Requirement: Control-plane tests cover cancel state alignment
The test suite SHALL verify cancel behavior across `ops.pipeline_job`, `hydro.hydro_run`, `met.forecast_cycle`, and returned API payloads.

#### Scenario: Cancel integration test detects split-brain state
- **WHEN** cancel updates only pipeline jobs and leaves hydro runs active
- **THEN** an integration test MUST fail
