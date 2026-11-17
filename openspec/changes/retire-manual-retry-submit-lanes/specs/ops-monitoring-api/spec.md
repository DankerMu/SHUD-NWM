## REMOVED Requirements

### Requirement: POST /api/v1/runs/{run_id}/retry

**Reason**: Owner decision (b) on issue #3157: the retry route no longer re-submits failed runs. Scenario "Successful retry submission" (202, `retried_jobs`), "No failed jobs for run" (409 `NO_FAILED_JOBS`) and "Run not found" (404 `RUN_NOT_FOUND`) are submit-lane / route-success outcomes that can no longer occur; "Unauthorized user" is carried over to the new requirement.

**Migration**: See ADDED "POST /api/v1/runs/{run_id}/retry is a fail-closed manual-action endpoint".

## MODIFIED Requirements

### Requirement: Slurm gateway error evidence SHALL render host paths on ops responses while persisted events keep the raw text

When a Slurm gateway error reaches an ops API response body, its `message` and its `details` SHALL be rendered through the shared public evidence renderer, so that absolute host paths become `[local-path]` and URIs become `[uri]`/`[object-uri]`, including inside nested values and list elements such as the executed command's argument vector and the captured stderr snippet. Secret redaction SHALL keep its existing precedence.

This SHALL apply to the cancel endpoint's 200 body — every gateway error carried under its failed and blocked job entries — and to the queue-depth endpoint's upstream-error body. The response SHALL contain no configured scheduler binary path and no workspace root text.

The pipeline events persisted beside those responses SHALL keep the gateway's raw, secrets-redacted text, because operators diagnose from them and because the durable event is the system's record of what the scheduler actually reported. It is NOT an anti-laundering rule — `[local-path]` is deliberately persisted evidence elsewhere. A single rendered payload SHALL NOT be shared between a response and a persisted event: the persisted copy SHALL remain raw.

The upstream-error response schema's field names SHALL NOT change; only the values are rendered. Errors that carry no gateway text, such as the control-plane manual-action error, SHALL keep their current rendering.

#### Scenario: Cancel response renders a failing scancel's command and stderr

- **WHEN** `scancel` fails for an active job and its error carries the configured scheduler binary path in the command vector and an absolute workspace path in the stderr snippet
- **THEN** the cancel response's failed-job entry SHALL carry the rendered command vector and a stderr snippet in which the workspace path is `[local-path]`
- **THEN** the response body SHALL contain neither the scheduler binary path nor the workspace root text

#### Scenario: The unproven-cancellation branch renders the same way

- **WHEN** the gateway's error is one the route classifies as an unproven cancellation, so the entry lands under blocked jobs
- **THEN** the same rendering SHALL apply to that entry's error details and message

#### Scenario: Queue depth upstream error renders the same way

- **WHEN** the queue-depth query fails with a gateway command error carrying the same two kinds of path
- **THEN** the upstream-error response body SHALL carry the rendered details, with the same field names as before

#### Scenario: An unproven cancellation's gateway response is rendered on the wire and raw in the event

- **WHEN** a cancellation returns a record whose manifest carries a workspace directory, a manifest index path or an array log directory, and the route reports it as an unproven cancellation
- **THEN** the response's gateway-response payload SHALL be rendered like any other gateway evidence
- **THEN** the persisted event's copy SHALL keep the raw values, as real values, not placeholders

#### Scenario: Persisted cancellation events keep the raw evidence

- **WHEN** the same failure is recorded as a cancellation-gap or cancel-failed pipeline event
- **THEN** that event's persisted error details SHALL still carry the raw command vector and stderr text, with secrets redacted as before, and SHALL NOT carry any public placeholder introduced by the response rendering

## ADDED Requirements

### Requirement: POST /api/v1/runs/{run_id}/retry is a fail-closed manual-action endpoint

The API SHALL keep the `POST /api/v1/runs/{run_id}/retry` endpoint but it SHALL NOT submit, enqueue, or record anything. After run-id validation (400 `INVALID_RUN_ID`) and RBAC (401/403), and after the existing `display_readonly` 409, every request SHALL be answered with HTTP 409 and error code `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` in the standard error envelope, regardless of service role (`compute_control`, `dev_monolith`, `display_readonly`). The error details SHALL include the safe `run_id`, a `suggested_action` that names the supported operator path (the node-22 manual-repair marker lane `FileJournalRetryService.record_manual_repair`, driven by `scripts/node22_manual_retry_failed_runs.py`), and `recovery_runbook` equal to `node22-control-plane-manual-recovery`. The endpoint SHALL NOT construct a database session, a retry service, or a Slurm gateway.

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
- **THEN** the API SHALL return HTTP 403 with `{"request_id": "...", "status": "error", "error": {"code": "FORBIDDEN", "message": "operator+ role required"}}`
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
