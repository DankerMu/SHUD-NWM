# display-control-mutation-guard Specification

## Purpose
TBD - created by archiving change m22-two-node-docker-readonly-display. Update Purpose after archive.
## Requirements
### Requirement: Display retry and cancel fail closed

In `display_readonly` mode, retry and cancel endpoints SHALL return a stable manual-action error instead of executing control-plane behavior.

#### Scenario: Display retry request
- **WHEN** an otherwise-authorized caller posts to `/api/v1/runs/{run_id}/retry` on a display readonly API
- **THEN** the API returns HTTP `409` with code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`
- **AND** the response uses the standard API error envelope
- **AND** details include safe `run_id`, `display_mode=display_readonly`, `suggested_action`, and `recovery_runbook`
- **AND** the response does not claim a new job was submitted.

#### Scenario: Display cancel request
- **WHEN** an otherwise-authorized caller posts to `/api/v1/runs/{run_id}/cancel` on a display readonly API
- **THEN** the API returns HTTP `409` with code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`
- **AND** the response uses the standard API error envelope
- **AND** details include safe `run_id`, `display_mode=display_readonly`, `suggested_action`, and `recovery_runbook`
- **AND** the response does not claim any job was cancelled.

#### Scenario: Display retry and cancel still honor auth
- **WHEN** an unauthenticated or unauthorized caller posts retry or cancel on a display readonly API
- **THEN** the API returns the existing `401` or `403` auth/RBAC error
- **AND** it does not reveal manual recovery details intended only for authorized operators.

### Requirement: Display mutation guard has no side effects

Display-mode retry and cancel guards SHALL complete without calling gateway dependencies or writing pipeline, hydro, or met terminal state.

#### Scenario: Retry does not call gateway
- **WHEN** retry is requested in display readonly mode
- **THEN** the request does not construct or call `get_slurm_gateway()`
- **AND** no `submit_job` call is made even if mock gateway configuration exists.

#### Scenario: Cancel does not call gateway
- **WHEN** cancel is requested in display readonly mode
- **THEN** the request does not construct or call `get_slurm_gateway()`
- **AND** no `cancel_job` call is made even if a job has a `slurm_job_id`.

#### Scenario: Display mutation guard does not write terminal state
- **WHEN** retry or cancel is requested in display readonly mode
- **THEN** the API does not create a new pipeline job
- **AND** it does not insert pipeline events for submitted/cancelled state
- **AND** it does not update hydro, met, forecast cycle, or pipeline terminal status.

### Requirement: Compute control preserves existing behavior

The compute control and dev monolith roles SHALL preserve the existing retry/cancel behavior and authorization checks.

#### Scenario: Compute retry still submits
- **WHEN** an authorized caller posts retry on a compute-control API and the retry target is valid
- **THEN** the existing retry service can submit through the configured gateway
- **AND** the returned job and retry metadata preserve the current API contract.

#### Scenario: Compute cancel still records proven cancellation
- **WHEN** an authorized caller posts cancel on a compute-control API and the gateway returns proven cancellation
- **THEN** active jobs can transition to cancelled according to the existing cancellation contract
- **AND** unproven cancellation gaps still preserve local state.

### Requirement: Display queue depth does not use gateway

Display readonly queue-depth behavior SHALL not construct or call the Slurm gateway.

#### Scenario: Display queue depth unavailable
- **WHEN** `/api/v1/queue/depth` is requested in display readonly mode and no DB-derived queue summary is implemented
- **THEN** the API returns a stable read-only unavailable error such as `CONTROL_PLANE_QUEUE_UNAVAILABLE`
- **AND** it does not construct or call `get_slurm_gateway()`.

#### Scenario: Display queue depth from DB
- **WHEN** `/api/v1/queue/depth` is implemented for display readonly mode from persisted pipeline/job records
- **THEN** the response is explicitly marked as DB-derived or display-derived
- **AND** it does not call Slurm Gateway, mock gateway, `queue_depth()`, or `list_jobs()`.

#### Scenario: Compute queue depth unchanged
- **WHEN** `/api/v1/queue/depth` is requested in compute-control or dev monolith mode
- **THEN** existing gateway-backed queue behavior can remain available according to configuration.

