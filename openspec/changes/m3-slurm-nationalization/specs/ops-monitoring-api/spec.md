# Spec: Operations Monitoring API

**Change:** m3-slurm-nationalization  
**Spec:** 6 of 8 — ops-monitoring-api  
**Status:** draft  

---

## ADDED Requirements

### Requirement: GET /api/v1/pipeline/status

The API SHALL provide a `GET /api/v1/pipeline/status` endpoint that returns the overall status of a single forecast cycle.

#### Scenario: Successful status query

- **WHEN** a client sends `GET /api/v1/pipeline/status?source=gfs&cycle_time=2026-05-08T00:00:00Z`
- **THEN** the API SHALL return HTTP 200 with:
  - `request_id` — unique request identifier
  - `status` — `"ok"`
  - `data` — object containing:
    - `source` — the forecast source (e.g., `gfs`)
    - `cycle_time` — TIMESTAMPTZ of the cycle
    - `state` — one of: `pending`, `running`, `succeeded`, `partially_failed`, `failed`
    - `started_at` — TIMESTAMPTZ when the first stage began (NULL if not started)
    - `updated_at` — TIMESTAMPTZ of the most recent status change

#### Scenario: Unknown cycle

- **WHEN** a client queries with a `cycle_time` that does not exist for the given `source`
- **THEN** the API SHALL return HTTP 404 with `{"request_id": "...", "status": "error", "error": {"code": "CYCLE_NOT_FOUND", "message": "..."}}`

---

### Requirement: GET /api/v1/pipeline/stages

The API SHALL provide a `GET /api/v1/pipeline/stages` endpoint that returns the status, duration, and basin progress of all 7 pipeline stages for a given forecast cycle.

#### Scenario: Successful stages query

- **WHEN** a client sends `GET /api/v1/pipeline/stages?source=gfs&cycle_time=2026-05-08T00:00:00Z`
- **THEN** the API SHALL return HTTP 200 with a response body containing:
  - `request_id` — unique request identifier
  - `status` — `"ok"`
  - `data` — an ordered array of 7 stage objects (`download`, `canonical`, `forcing`, `shud_forecast`, `parse`, `frequency`, `publish`), each containing:
    - `stage` — stage name
    - `status` — one of: `pending`, `running`, `succeeded`, `partially_failed`, `failed`, `skipped`
    - `duration_seconds` — total elapsed seconds for the stage (NULL if not started)
    - `basin_progress` — object `{"completed": <int>, "total": <int>, "failed": <int>}` (NULL for cycle-level stages without per-basin scope)
    - `basin_results` — array of per-basin result objects (only present for stages that are array jobs, NULL otherwise), each containing:
      - `model_id` — basin model identifier
      - `status` — one of: `succeeded`, `failed`, `running`, `submitted`
      - `error_code` — error code if failed (NULL otherwise)
      - `error_message` — human-readable error message if failed (NULL otherwise)
    - `started_at` — TIMESTAMPTZ (NULL if not started)
    - `finished_at` — TIMESTAMPTZ (NULL if not finished)

#### Scenario: status mapping

- **WHEN** the API computes `status` for a stage
- **THEN** the mapping SHALL follow these rules:
  - `pending` — no jobs submitted yet for this stage
  - `running` — at least one job is in `submitted` or `running` status
  - `succeeded` — all jobs completed with `succeeded` status
  - `partially_failed` — some jobs `succeeded` and some `failed`, with no jobs still running
  - `failed` — all jobs `failed`
  - `skipped` — stage was not executed (e.g., due to upstream failure blocking all basins)

#### Scenario: Unknown cycle

- **WHEN** a client queries with a `cycle_time` that does not exist for the given `source`
- **THEN** the API SHALL return HTTP 404 with `{"request_id": "...", "status": "error", "error": {"code": "CYCLE_NOT_FOUND", "message": "..."}}`

---

### Requirement: GET /api/v1/jobs

The API SHALL provide a `GET /api/v1/jobs` endpoint that returns a paginated, filterable list of pipeline jobs.

#### Scenario: Paginated job listing

- **WHEN** a client sends `GET /api/v1/jobs?source=gfs&cycle_time=2026-05-08T00:00:00Z&limit=20&offset=0`
- **THEN** the API SHALL return HTTP 200 with:
  - `request_id` — unique request identifier
  - `status` — `"ok"`
  - `data` — object containing:
    - `items` — array of job objects, each containing: `job_id`, `slurm_job_id`, `cycle_id`, `source`, `stage`, `run_id`, `model_id`, `run_type`, `scenario`, `status`, `submitted_at`, `started_at`, `finished_at`, `duration_seconds`, `exit_code`, `error_code`, `error_message`, `retry_count`, `log_uri`
    - `total` — total count of matching jobs
    - `limit` — the applied limit
    - `offset` — the applied offset

#### Scenario: Filter by status

- **WHEN** a client sends `GET /api/v1/jobs?source=gfs&cycle_time=2026-05-08T00:00:00Z&status=failed`
- **THEN** the API SHALL return only jobs whose `status` equals `failed`

#### Scenario: Filter by model_id

- **WHEN** a client sends `GET /api/v1/jobs?source=gfs&cycle_time=2026-05-08T00:00:00Z&model_id=yangtze_shud_v12`
- **THEN** the API SHALL return only jobs whose `model_id` equals `yangtze_shud_v12`

#### Scenario: Filter by stage

- **WHEN** a client sends `GET /api/v1/jobs?source=gfs&cycle_time=2026-05-08T00:00:00Z&stage=shud_forecast`
- **THEN** the API SHALL return only jobs whose `stage` equals `shud_forecast`

#### Scenario: Filter by run_type

- **WHEN** a client sends `GET /api/v1/jobs?source=gfs&cycle_time=2026-05-08T00:00:00Z&run_type=forecast`
- **THEN** the API SHALL return only jobs whose `run_type` equals `forecast`

#### Scenario: Filter by scenario

- **WHEN** a client sends `GET /api/v1/jobs?source=gfs&cycle_time=2026-05-08T00:00:00Z&scenario=GFS`
- **THEN** the API SHALL return only jobs whose `scenario` equals `GFS`

#### Scenario: Default pagination

- **WHEN** a client omits `limit` and `offset`
- **THEN** the API SHALL default to `limit=50` and `offset=0`

---

### Requirement: GET /api/v1/jobs/{job_id}/logs

The API SHALL provide a `GET /api/v1/jobs/{job_id}/logs` endpoint that returns the log content for a specific pipeline job.

#### Scenario: Logs available from log_uri

- **WHEN** a client sends `GET /api/v1/jobs/{job_id}/logs` and the `pipeline_job.log_uri` is populated
- **THEN** the API SHALL read the log file at `log_uri` and return HTTP 200 with:
  - `request_id` — unique request identifier
  - `status` — `"ok"`
  - `data` — object containing `log_content` (TEXT, truncated to last 10,000 lines) and `truncated` (BOOLEAN)

#### Scenario: Logs not yet available

- **WHEN** a client queries logs for a job whose `log_uri` is NULL (job still running or submission failed without log)
- **THEN** the API SHALL return HTTP 200 with `data.log_content` set to an empty string and `data.truncated` set to false

#### Scenario: Job not found

- **WHEN** a client queries logs for a `job_id` that does not exist
- **THEN** the API SHALL return HTTP 404 with `{"request_id": "...", "status": "error", "error": {"code": "JOB_NOT_FOUND", "message": "..."}}`

---

### Requirement: POST /api/v1/runs/{run_id}/retry

The API SHALL provide a `POST /api/v1/runs/{run_id}/retry` endpoint that triggers re-submission of a failed run.

#### Scenario: Successful retry submission

- **WHEN** an authenticated user with `operator` role or above sends `POST /api/v1/runs/{run_id}/retry`
- **THEN** the API SHALL:
  1. Identify all `pipeline_job` records for the given `run_id` with `status` = `failed`
  2. For each failed job, create a new `pipeline_job` record with a new `job_id`, incremented `retry_count`, and `status` = `submitted`
  3. Submit the new jobs to Slurm
  4. Return HTTP 202 with `{"request_id": "...", "status": "ok", "data": {"retried_jobs": [<new_job_ids>]}}`

#### Scenario: Unauthorized user

- **WHEN** a user without `operator`, `model_admin`, or `sys_admin` role sends `POST /api/v1/runs/{run_id}/retry`
- **THEN** the API SHALL return HTTP 403 with `{"request_id": "...", "status": "error", "error": {"code": "FORBIDDEN", "message": "operator+ role required"}}`

#### Scenario: No failed jobs for run

- **WHEN** a user sends retry for a `run_id` that has no jobs in `failed` status
- **THEN** the API SHALL return HTTP 409 with `{"request_id": "...", "status": "error", "error": {"code": "NO_FAILED_JOBS", "message": "..."}}`

#### Scenario: Run not found

- **WHEN** a user sends retry for a `run_id` that does not exist
- **THEN** the API SHALL return HTTP 404 with `{"request_id": "...", "status": "error", "error": {"code": "RUN_NOT_FOUND", "message": "..."}}`

---

### Requirement: POST /api/v1/runs/{run_id}/cancel

The API SHALL provide a `POST /api/v1/runs/{run_id}/cancel` endpoint that cancels running jobs for a given run.

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
- **THEN** the API SHALL return HTTP 409 with `{"request_id": "...", "status": "error", "error": {"code": "NO_ACTIVE_JOBS", "message": "..."}}`

---

### Requirement: GET /api/v1/metrics/stage-duration

The API SHALL provide a `GET /api/v1/metrics/stage-duration` endpoint that returns average stage durations over a configurable time window.

#### Scenario: Default 7-day window

- **WHEN** a client sends `GET /api/v1/metrics/stage-duration?source=gfs`
- **THEN** the API SHALL return HTTP 200 with:
  - `data` — array of 7 objects (one per stage), each containing:
    - `stage` — stage name
    - `avg_duration_seconds` — mean `duration_seconds` across all completed jobs for this stage in the last 7 days
    - `p95_duration_seconds` — 95th percentile duration
    - `sample_count` — number of completed jobs in the window

#### Scenario: Custom day window

- **WHEN** a client sends `GET /api/v1/metrics/stage-duration?source=gfs&days=30`
- **THEN** the API SHALL compute averages over the last 30 days

---

### Requirement: GET /api/v1/metrics/success-rate

The API SHALL provide a `GET /api/v1/metrics/success-rate` endpoint that returns per-cycle success rates over a configurable time window.

#### Scenario: Default 7-day success rate

- **WHEN** a client sends `GET /api/v1/metrics/success-rate?source=gfs`
- **THEN** the API SHALL return HTTP 200 with:
  - `data` — array of objects ordered by `cycle_time ASC`, each containing:
    - `cycle_time` — the cycle timestamp
    - `total_jobs` — total pipeline jobs for this cycle
    - `succeeded_jobs` — count of jobs with `status` = `succeeded`
    - `success_rate` — `succeeded_jobs / total_jobs` as a decimal (0.0 to 1.0)

#### Scenario: Custom day window

- **WHEN** a client sends `GET /api/v1/metrics/success-rate?source=gfs&days=14`
- **THEN** the API SHALL compute success rates for cycles within the last 14 days

---

### Requirement: GET /api/v1/queue/depth

The API SHALL provide a `GET /api/v1/queue/depth` endpoint that returns the current Slurm queue depth.

#### Scenario: Queue depth query

- **WHEN** a client sends `GET /api/v1/queue/depth`
- **THEN** the API SHALL query Slurm (via `squeue` or equivalent) and return HTTP 200 with:
  - `data` — object containing:
    - `running` — count of currently running jobs
    - `pending` — count of pending/queued jobs
    - `idle` — count of idle nodes (from `sinfo`)
    - `queried_at` — TIMESTAMPTZ of when the Slurm query was executed

#### Scenario: Slurm query failure

- **WHEN** the `squeue`/`sinfo` command fails or times out
- **THEN** the API SHALL return HTTP 503 with `{"request_id": "...", "status": "error", "error": {"code": "SLURM_UNAVAILABLE", "message": "..."}}`

---

### Requirement: Standard Response Wrapper

All monitoring API endpoints MUST return responses in a standard wrapper format.

#### Scenario: Successful response structure

- **WHEN** any monitoring API endpoint returns a successful response
- **THEN** the response body MUST conform to: `{"request_id": "<uuid>", "status": "ok", "data": <payload>}`

#### Scenario: Error response structure

- **WHEN** any monitoring API endpoint returns an error response
- **THEN** the response body MUST conform to: `{"request_id": "<uuid>", "status": "error", "error": {"code": "<ERROR_CODE>", "message": "<human-readable message>"}}`
