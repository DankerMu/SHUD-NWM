# display-control-mutation-guard Specification

## Purpose
Keep the API from mutating pipeline state through run actions: retry fails closed in every service role without side
effects or gateway calls, and the run cancel path is not a route in any role (#3394).

## Requirements

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

### Requirement: Display retry SHALL fail closed and the cancel path SHALL NOT be a route

In `display_readonly` mode, the retry endpoint SHALL return a stable manual-action error instead of executing control-plane behavior. `POST /api/v1/runs/{run_id}/cancel` SHALL NOT be a route of the API in any service role: no handler, no OpenAPI operation, no RBAC action. Cancelling work stays with the scheduler on the compute host and with the Slurm gateway's own job route, neither of which the display API serves.

#### Scenario: Display retry request
- **WHEN** an otherwise-authorized caller posts to `/api/v1/runs/{run_id}/retry` on a display readonly API
- **THEN** the API returns HTTP `409` with code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED`
- **AND** the response uses the standard API error envelope
- **AND** details include safe `run_id`, `display_mode=display_readonly`, `suggested_action`, and `recovery_runbook`
- **AND** the response does not claim a new job was submitted.

#### Scenario: Display retry still honors auth
- **WHEN** an unauthenticated or unauthorized caller posts retry on a display readonly API
- **THEN** the API returns the existing `401` or `403` auth/RBAC error
- **AND** it does not reveal manual recovery details intended only for authorized operators.

#### Scenario: The cancel path answers like any unregistered POST path
- **WHEN** a caller posts to `/api/v1/runs/{run_id}/cancel` on an API running as `display_readonly`, `compute_control` or `dev_monolith`, with operator credentials, with viewer credentials and with none
- **THEN** every answer equals the answer of the same request to an unregistered path under `/api/v1/`: HTTP `405` with `Allow: GET` and the same body, because the one route that matches the path is the GET fallback that serves the single-page app
- **AND** no answer is `200` or `409`, and none carries a manual-action body or names a cancelled job
- **AND** no Slurm gateway is constructed, no `cancel_job` call is made and no pipeline, hydro, met or forecast-cycle state is written.

### Requirement: The display retry guard SHALL have no side effects

The display-mode retry guard SHALL complete without calling gateway dependencies or writing pipeline, hydro, or met terminal state.

#### Scenario: Retry does not call gateway
- **WHEN** retry is requested in display readonly mode
- **THEN** the request does not construct or call `get_slurm_gateway()`
- **AND** no `submit_job` call is made even if mock gateway configuration exists.

#### Scenario: Display retry guard does not write terminal state
- **WHEN** retry is requested in display readonly mode
- **THEN** the API does not create a new pipeline job
- **AND** it does not insert pipeline events for submitted state
- **AND** it does not update hydro, met, forecast cycle, or pipeline terminal status.

### Requirement: Compute control SHALL fail retry closed

The compute control and dev monolith roles SHALL answer retry with the same stable manual-action error as display mode instead of submitting work.

#### Scenario: Compute retry fails closed
- **WHEN** an authorized caller posts retry on a compute-control or dev-monolith API
- **THEN** the API returns HTTP `409` with code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` in the standard API error envelope, including when no `DATABASE_URL` is configured
- **AND** no retry service, database session, or Slurm gateway is constructed and no `submit_job` call is made
- **AND** the response does not claim a new job was submitted.
