## MODIFIED Requirements

### Requirement: POST /api/v1/runs/{run_id}/retry is a fail-closed manual-action endpoint

The API SHALL keep the `POST /api/v1/runs/{run_id}/retry` endpoint but it SHALL NOT submit, enqueue, or record anything. After run-id validation (400 `INVALID_RUN_ID`) and RBAC (401/403), and after the existing `display_readonly` 409, every request SHALL be answered with HTTP 409 and error code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` in the standard error envelope, regardless of service role (`compute_control`, `dev_monolith`, `display_readonly`). The error details SHALL include the safe `run_id`, a `suggested_action` that names the supported operator path (the manual-repair marker lane of the compute role, `FileJournalRetryService.record_manual_repair`, driven by `scripts/node22_manual_retry_failed_runs.py`), and `recovery_runbook` equal to `node22-control-plane-manual-recovery`. The endpoint SHALL NOT construct a database session, a retry service, or a Slurm gateway. The published OpenAPI operation `retryRun` SHALL declare no success (2xx) response and no `RetryRunResult` schema, because none can be returned; it keeps its path, security and error responses so existing callers and deny-write probes see the same 401/403/409.

#### Scenario: Compute-control retry returns the stable manual-action error

- **WHEN** an authorized caller (`operator`, `model_admin`, or `sys_admin`) posts to `/api/v1/runs/{run_id}/retry` on a `compute_control` or `dev_monolith` API, whether or not the run has failed jobs and whether or not the run exists
- **THEN** the API returns HTTP `409` with code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` in the standard API error envelope
- **AND** details include safe `run_id`, `suggested_action` naming `scripts/node22_manual_retry_failed_runs.py`, and `recovery_runbook=node22-control-plane-manual-recovery`
- **AND** the response does not claim a new job was submitted.

#### Scenario: Compute-control retry without DATABASE_URL is not a 500

- **WHEN** an authorized caller posts to the retry endpoint on a `compute_control` API that has no `DATABASE_URL` configured
- **THEN** the API returns the same stable `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED`, not an unclassified `500` or `503`.

#### Scenario: Unauthorized user

- **WHEN** a user without `operator`, `model_admin`, or `sys_admin` role sends `POST /api/v1/runs/{run_id}/retry`
- **THEN** the API SHALL return HTTP 403 with error code `RBAC_FORBIDDEN` (the shared RBAC refusal; 401 `AUTH_REQUIRED` when unauthenticated)
- **AND** it does not reveal the manual recovery details intended only for authorized operators.

#### Scenario: Authentication and authorization precede the manual-action error

- **WHEN** an unauthenticated or unauthorized caller posts to the retry endpoint in any service role
- **THEN** the API returns the existing `401` or `403` auth/RBAC error and never the `409`.

#### Scenario: Display readonly keeps its existing 409 first

- **WHEN** an otherwise-authorized caller posts to the retry endpoint on a `display_readonly` API
- **THEN** the API returns `409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED` with details including `display_mode=display_readonly`, exactly as specified by display-control-mutation-guard
- **AND** no gateway, database session, or retry service is constructed.

#### Scenario: Retry endpoint has no side effects

- **WHEN** the retry endpoint is requested in any service role
- **THEN** no `pipeline_job` row, `pipeline_event`, file-journal record, or hydro run status change is written
- **AND** no Slurm `submit_job` call is made.

#### Scenario: The contract declares no success response

- **WHEN** the published OpenAPI document (`openapi/nhms.v1.yaml`, the runtime `app.openapi()` and the generated `apps/frontend/src/api/types.ts`) is inspected
- **THEN** `POST /api/v1/runs/{run_id}/retry` (`retryRun`) lists no 2xx response, and no `RetryRunResult` component exists
- **AND** the operation keeps its path, `security` requirement and its `409 ControlPlaneManualActionRequired` response
