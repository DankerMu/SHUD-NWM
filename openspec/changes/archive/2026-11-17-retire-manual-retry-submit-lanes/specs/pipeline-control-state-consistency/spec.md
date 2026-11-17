## REMOVED Requirements

### Requirement: Manual retry submits or explicitly queues executable work

**Reason**: All three scenarios (no fake running state, response `queued` vs `submitted`, duplicate-request conflict) specify the retry route success path / retired pending manual row. The route no longer submits (issue #3157).

**Migration**: Route contract: ops-monitoring-api ADDED "POST /api/v1/runs/{run_id}/retry is a fail-closed manual-action endpoint".

### Requirement: Control-plane tests cover database state alignment

**Reason**: Scenario "Retry integration test detects pending deadlock" requires a test of a retry that "creates a pending job without submission"; that test target was the retired manual route. The cancel scenario is carried over unchanged. The retry scenario is dropped because no surviving lane creates a pending job without submission in-process; the marker lane writes a marker and the scheduler mints and submits in one pass.

**Migration**: See ADDED "Control-plane tests cover cancel state alignment".

## MODIFIED Requirements

### Requirement: State transitions are auditable
Retry and cancel operations SHALL write `ops.pipeline_event` records that include actor context when available, trigger type, previous status, next status, retry count, and previous error details.

#### Scenario: Retry event records prior failure
- **WHEN** a manual repair marker is recorded through `FileJournalRetryService.record_manual_repair`
- **THEN** a pipeline event MUST record trigger `manual`, previous job id, previous error code, and next retry count

#### Scenario: Cancel event records Slurm action
- **WHEN** a cancel request reaches Slurm or is treated idempotently
- **THEN** a pipeline event MUST record the Slurm job id and the local status transition

## ADDED Requirements

### Requirement: Control-plane tests cover cancel state alignment
The test suite SHALL verify cancel behavior across `ops.pipeline_job`, `hydro.hydro_run`, `met.forecast_cycle`, and returned API payloads.

#### Scenario: Cancel integration test detects split-brain state
- **WHEN** cancel updates only pipeline jobs and leaves hydro runs active
- **THEN** an integration test MUST fail
