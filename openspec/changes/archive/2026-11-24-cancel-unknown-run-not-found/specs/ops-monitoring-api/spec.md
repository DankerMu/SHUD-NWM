## MODIFIED Requirements

### Requirement: POST /api/v1/runs/{run_id}/cancel

The API SHALL provide a `POST /api/v1/runs/{run_id}/cancel` endpoint that cancels running jobs for a given run. When the run id has no `pipeline_job` row of any status and no hydro run row, the endpoint SHALL answer HTTP 404 with the stable error envelope and `error.code` `RUN_NOT_FOUND`, SHALL NOT write any record or event, and SHALL NOT call the Slurm gateway. A run id that has at least one `pipeline_job` row or a hydro run row SHALL keep the cancel behaviour, including the idempotent HTTP 200 result when no job is active. Refusals decided before the route body runs (authentication, authorization, release block, the display-readonly manual-action refusal) SHALL take precedence over the not-found answer.

#### Scenario: Successful cancellation

- **WHEN** an authenticated user with `operator` role or above sends `POST /api/v1/runs/{run_id}/cancel`
- **THEN** the API SHALL:
  1. Identify all `pipeline_job` records for the given `run_id` with `status` IN (`submitted`, `running`)
  2. For each active job with a valid `slurm_job_id`, invoke `scancel <slurm_job_id>`
  3. Update each job's `status` to `cancelled`
  4. Return HTTP 200 with `{"request_id": "...", "status": "ok", "data": {"cancelled_jobs": [<job_ids>]}}`

#### Scenario: Unauthorized user

- **WHEN** a user without `operator`, `model_admin`, or `sys_admin` role sends `POST /api/v1/runs/{run_id}/cancel`
- **THEN** the API SHALL return HTTP 403 with `{"request_id": "...", "status": "error", "error": {"code": "FORBIDDEN", "message": "operator+ role required"}}`

#### Scenario: No active jobs to cancel

- **WHEN** a user sends cancel for a `run_id` whose jobs are all in terminal status
- **THEN** the API SHALL return HTTP 200 with `status` `ok` and an empty `cancelled_jobs` list

#### Scenario: Unknown run id

- **GIVEN** a run id with no `pipeline_job` row and no hydro run row
- **WHEN** an authorized user sends cancel for it on an API that is not display-readonly
- **THEN** the API SHALL return HTTP 404 with `{"request_id": "...", "status": "error", "error": {"code": "RUN_NOT_FOUND", "message": "...", "details": {"run_id": "<run_id>"}}}`
- **AND** no pipeline event is written, no record changes, and the Slurm gateway receives no call

#### Scenario: Hydro run row without jobs

- **GIVEN** a run id with a hydro run row and no `pipeline_job` row
- **WHEN** an authorized user sends cancel for it
- **THEN** the API SHALL return HTTP 200 and handle the hydro run as it does for a run with jobs

#### Scenario: Earlier refusals come first

- **GIVEN** a run id that names nothing
- **WHEN** the request is unauthenticated, lacks the role, or reaches a display-readonly API
- **THEN** the API SHALL return the 401, 403 or manual-action 409 answer it returns for any run id, not 404

#### Scenario: The contract declares the not-found answer

- **WHEN** the published OpenAPI document and the runtime schema are read
- **THEN** the cancel operation SHALL declare a 404 response whose error code is `RUN_NOT_FOUND`
