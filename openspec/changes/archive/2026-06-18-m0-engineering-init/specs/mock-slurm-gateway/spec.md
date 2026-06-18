# Mock Slurm Gateway

Capability: `mock-slurm-gateway`
Status: draft
Parent: m0-engineering-init

## ADDED Requirements

### Requirement: Mock backend is activated via configuration

The Slurm Gateway service MUST support a `mock` backend mode that eliminates all dependencies on a real Slurm installation. The backend mode MUST be selected via configuration, not code changes.

#### Scenario: Mock mode is enabled by configuration

WHEN the configuration key `slurm_gateway.backend` is set to `mock`
THEN the gateway MUST use the in-process mock implementation for all operations
AND no Slurm CLI commands (`sbatch`, `scancel`, `squeue`, `sacct`) MUST be invoked
AND the gateway MUST log a startup message indicating mock mode is active

#### Scenario: Default configuration uses mock mode

WHEN no explicit `slurm_gateway.backend` configuration is provided in the development environment
THEN the gateway MUST default to `mock` mode
AND `make dev` MUST start the gateway without requiring Slurm to be installed

#### Scenario: Configuration accepts future backend values

WHEN the configuration key `slurm_gateway.backend` is set to `slurm`
THEN the gateway MUST raise `NotImplementedError` with the message "Real Slurm backend is not implemented; available in M3"
AND the gateway MUST NOT start or accept any job submissions
AND the error MUST be raised during service initialization, not on the first request

#### Scenario: Mock mode is identifiable at runtime

WHEN any API consumer queries the gateway health or status endpoint
THEN the response MUST include a field `backend` with value `mock`
AND the response MUST include a field `version` with the service version string

### Requirement: HTTP API paths are explicitly defined

The mock Slurm gateway MUST expose a RESTful HTTP API with well-defined paths for all operations.

#### Scenario: All HTTP API paths are defined

WHEN a developer inspects the gateway's route definitions
THEN the following HTTP endpoints MUST be registered:
  - `POST /api/v1/slurm/jobs` -- submit_job: accepts a job manifest and returns a mock job ID
  - `DELETE /api/v1/slurm/jobs/{job_id}` -- cancel_job: cancels a running or submitted job
  - `GET /api/v1/slurm/jobs/{job_id}` -- get_job_status: returns the current state of a specific job
  - `GET /api/v1/slurm/jobs` -- list_jobs: returns all jobs with pagination (`limit`, `offset`)
  - `GET /api/v1/slurm/jobs/{job_id}/logs` -- fetch_logs: returns mock log text for a job
  - `POST /api/v1/slurm/internal/reset` -- reset: clears the in-memory registry (mock mode only)
  - `GET /api/v1/slurm/health` -- health_check: returns backend type and version
AND all endpoints MUST use `application/json` for request and response bodies
AND all endpoints MUST return the standard response envelope on success

### Requirement: submit_job returns a mock job ID with configurable delay

The `submit_job` operation MUST accept a job submission request, validate it, store it in memory, and return a mock Slurm job ID. The mock job MUST transition through status states with configurable timing.

#### Scenario: Successful job submission returns a mock job ID

WHEN a client calls `submit_job` with a valid job manifest
THEN the gateway MUST return a response containing:
  - `job_id`: a unique string following the pattern `mock_<sequential_number>` (e.g., `mock_1001`)
  - `status`: `submitted`
  - `submitted_at`: current UTC timestamp in ISO 8601
AND the job MUST be stored in the in-memory job registry
AND the response HTTP status MUST be `201 Created`

#### Scenario: Job transitions to running and then succeeded after configurable delay

WHEN a job has been submitted successfully
THEN the mock backend MUST transition the job status through: `submitted` -> `running` -> `succeeded`
AND the delay between `submitted` and `running` MUST be configurable via `slurm_gateway.mock.delay_to_running_seconds` (default: 2 seconds)
AND the delay between `running` and `succeeded` MUST be configurable via `slurm_gateway.mock.delay_to_succeeded_seconds` (default: 5 seconds)
AND each transition MUST update the job's `updated_at` timestamp

#### Scenario: Job submission validates required fields

WHEN a client calls `submit_job` with a manifest missing required fields (e.g., missing `run_id` or `model_id`)
THEN the gateway MUST return HTTP 422 with an ErrorResponse
AND the `error.code` MUST be `INVALID_MANIFEST`
AND the `error.details` MUST list the missing fields
AND no job MUST be created in the registry

#### Scenario: Duplicate run_id submission is rejected for non-terminal jobs

WHEN a client calls `submit_job` with a `run_id` that already exists in the registry
AND the existing job is in a non-terminal state (`submitted` or `running`)
THEN the gateway MUST return HTTP 409 Conflict
AND the `error.code` MUST be `DUPLICATE_RUN`
AND the existing job MUST NOT be modified

#### Scenario: Resubmission with same run_id is allowed after terminal state

WHEN a client calls `submit_job` with a `run_id` that already exists in the registry
AND the existing job is in a terminal state (`succeeded`, `failed`, or `cancelled`)
THEN the gateway MUST accept the submission and create a new job entry
AND the new job MUST receive a new unique `job_id`
AND the previous job record MAY be retained in the registry for audit purposes

### Requirement: cancel_job immediately marks the job as cancelled

The `cancel_job` operation MUST immediately transition a job to the `cancelled` terminal state.

#### Scenario: Active job is cancelled successfully

WHEN a client calls `cancel_job` with the `job_id` of a job in `submitted` or `running` status
THEN the job status MUST immediately transition to `cancelled`
AND the `updated_at` timestamp MUST be set to the current UTC time
AND the response MUST include the updated job state
AND the response HTTP status MUST be `200 OK`

#### Scenario: Cancelling an already-terminated job returns an error

WHEN a client calls `cancel_job` with the `job_id` of a job in `succeeded`, `failed`, or `cancelled` status
THEN the gateway MUST return HTTP 409 Conflict
AND the `error.code` MUST be `JOB_ALREADY_TERMINAL`
AND the `error.message` MUST include the current terminal status
AND the job MUST NOT be modified

#### Scenario: Cancelling a non-existent job returns not found

WHEN a client calls `cancel_job` with a `job_id` that does not exist in the registry
THEN the gateway MUST return HTTP 404 Not Found
AND the `error.code` MUST be `JOB_NOT_FOUND`

### Requirement: get_job_status returns the current state of a job

The `get_job_status` operation MUST return the full current state of a job, including all state transition timestamps.

#### Scenario: Querying an existing job returns its current state

WHEN a client calls `get_job_status` with a valid `job_id`
THEN the response MUST include:
  - `job_id`: the mock job ID
  - `run_id`: the original run_id from submission
  - `status`: current status string (one of: `submitted`, `running`, `succeeded`, `failed`, `cancelled`)
  - `submitted_at`: ISO 8601 timestamp
  - `started_at`: ISO 8601 timestamp or null (set when transitioning to `running`)
  - `finished_at`: ISO 8601 timestamp or null (set when reaching a terminal state)
  - `updated_at`: ISO 8601 timestamp of last state change
AND the response HTTP status MUST be `200 OK`

#### Scenario: Status reflects time-based progression

WHEN a job was submitted 1 second ago and `delay_to_running_seconds` is 2
THEN `get_job_status` MUST return `status: submitted`
WHEN queried again after the delay has elapsed
THEN `get_job_status` MUST return `status: running`
AND `started_at` MUST be populated

#### Scenario: Querying a non-existent job returns not found

WHEN a client calls `get_job_status` with a `job_id` that does not exist
THEN the gateway MUST return HTTP 404 Not Found
AND the `error.code` MUST be `JOB_NOT_FOUND`

#### Scenario: Listing all jobs returns the full registry

WHEN a client calls `get_job_status` without a specific `job_id` (list mode)
THEN the response MUST return an array of all jobs in the registry
AND the array MUST be ordered by `submitted_at` descending
AND pagination parameters `limit` and `offset` MUST be supported

### Requirement: fetch_logs returns mock log text

The `fetch_logs` operation MUST return deterministic mock log content that resembles real Slurm job output.

#### Scenario: Fetching logs for a completed job returns mock output

WHEN a client calls `fetch_logs` with the `job_id` of a job in `succeeded` status
THEN the response MUST include a `logs` field containing a multi-line string
AND the log text MUST include:
  - a header line with the `job_id` and `run_id`
  - a line indicating job submission time
  - a line indicating job start time
  - a line indicating SHUD execution (mock)
  - a line indicating job completion with exit code 0
AND the response HTTP status MUST be `200 OK`

#### Scenario: Fetching logs for a running job returns partial output

WHEN a client calls `fetch_logs` with the `job_id` of a job in `running` status
THEN the response MUST include a `logs` field with partial log text
AND the log text MUST include submission and start lines
AND it MUST NOT include a completion line
AND a field `complete` MUST be `false`

#### Scenario: Fetching logs for a failed job includes error details

WHEN a client calls `fetch_logs` with the `job_id` of a job in `failed` status
THEN the log text MUST include an error line with the simulated error code and message
AND the final line MUST indicate a non-zero exit code
AND a field `complete` MUST be `true`

#### Scenario: Fetching logs for a non-existent job returns not found

WHEN a client calls `fetch_logs` with a `job_id` that does not exist
THEN the gateway MUST return HTTP 404 Not Found

### Requirement: State transitions match hydro.run_status ENUM semantics

The mock gateway's internal state machine MUST align with the `hydro.run_status` ENUM from the database design, ensuring that orchestrator logic developed against the mock is valid against the real system.

#### Scenario: Normal success path follows the correct state sequence

WHEN a job is submitted and completes normally
THEN the state sequence MUST be: `submitted` -> `running` -> `succeeded`
AND each transition MUST be one-directional (no backward transitions)
AND the terminal state `succeeded` MUST NOT transition to any other state

#### Scenario: Cancellation is valid from non-terminal states

WHEN a job is in `submitted` status
THEN `cancel_job` MUST transition it to `cancelled`
WHEN a job is in `running` status
THEN `cancel_job` MUST transition it to `cancelled`
AND `cancelled` is a terminal state with no further transitions allowed

#### Scenario: Failed state is terminal

WHEN a job has transitioned to `failed` (via failure simulation)
THEN the state MUST NOT change regardless of elapsed time
AND `cancel_job` MUST return HTTP 409
AND `submit_job` with the same `run_id` MUST be allowed (since `failed` is a terminal state)

#### Scenario: State values are a strict subset of hydro.run_status

WHEN a developer inspects all possible status values returned by the mock gateway
THEN every value MUST be a member of the `hydro.run_status` ENUM: `created`, `staged`, `submitted`, `running`, `succeeded`, `parsed`, `frequency_done`, `published`, `failed`, `cancelled`, `superseded`
AND the mock gateway MUST only use the subset relevant to HPC job lifecycle: `submitted`, `running`, `succeeded`, `failed`, `cancelled`

### Requirement: Deterministic behavior supports integration testing

The mock gateway MUST produce predictable, reproducible results so that integration tests can make assertions without timing-dependent flakiness.

#### Scenario: Job IDs are sequential and predictable

WHEN the mock gateway starts with a fresh in-memory registry
THEN the first submitted job MUST receive `job_id` = `mock_1001`
AND the second MUST receive `mock_1002`
AND so on, incrementing by 1

#### Scenario: Delay can be set to zero for synchronous testing

WHEN `slurm_gateway.mock.delay_to_running_seconds` is set to `0`
AND `slurm_gateway.mock.delay_to_succeeded_seconds` is set to `0`
THEN a submitted job MUST immediately be in `succeeded` status on the next `get_job_status` call
AND integration tests MUST NOT require `sleep` or polling loops

#### Scenario: Registry is isolated per test via reset endpoint

WHEN a client calls `POST /api/v1/slurm/internal/reset` on the mock gateway
THEN all jobs MUST be cleared from the in-memory registry
AND the job ID counter MUST reset to `mock_1001`
AND this endpoint MUST only be available when `backend` is `mock`

#### Scenario: Concurrent submissions produce distinct job IDs

WHEN multiple clients submit jobs concurrently
THEN each MUST receive a unique `job_id`
AND no two jobs MUST share the same `job_id`
AND the registry MUST be thread-safe

### Requirement: Configurable failure simulation supports error path testing

The mock gateway MUST support deterministic failure injection so that orchestrator and monitoring code can be tested against error scenarios without relying on real Slurm failures.

#### Scenario: Global failure rate can be configured with deterministic seed

WHEN the configuration key `slurm_gateway.mock.failure_rate` is set to a float between 0.0 and 1.0
AND the configuration key `slurm_gateway.mock.failure_seed` is set to an integer (default: 42)
THEN the gateway MUST use a seed-based pseudo-random number generator (e.g., `random.Random(seed)`) to decide failures
AND the N-th submitted job MUST deterministically fail or succeed based on the seed and failure_rate
AND the failure MUST occur during the `running` -> terminal transition (job goes to `running` first, then `failed`)
AND the failed job MUST include `error_code: SIMULATED_FAILURE` and `error_message: Mock failure for testing`
AND given the same seed and failure_rate, the same sequence of fail/succeed decisions MUST be reproduced across restarts

#### Scenario: Specific run_id can be forced to fail

WHEN the configuration key `slurm_gateway.mock.force_fail_run_ids` contains a list of run_id strings
AND a job is submitted with a `run_id` matching any entry in that list
THEN that job MUST always transition to `failed` regardless of the global failure rate
AND the `error_code` MUST be `FORCED_FAILURE`
AND the `error_message` MUST include the run_id

#### Scenario: Zero failure rate means all jobs succeed

WHEN `slurm_gateway.mock.failure_rate` is set to `0.0`
AND `slurm_gateway.mock.force_fail_run_ids` is empty or not set
THEN every submitted job MUST transition to `succeeded`
AND no job MUST ever reach `failed` status

#### Scenario: Failure simulation does not affect cancel behavior

WHEN a job is configured to fail (via `force_fail_run_ids`)
AND the job is still in `submitted` or `running` state (before the failure transition)
THEN `cancel_job` MUST still work, transitioning the job to `cancelled`
AND the configured failure MUST NOT override the explicit cancellation

#### Scenario: Failed job logs contain diagnostic information

WHEN a job transitions to `failed` via failure simulation
THEN `fetch_logs` MUST return log text that includes:
  - the original submission details
  - a line indicating the simulated error type (`SIMULATED_FAILURE` or `FORCED_FAILURE`)
  - a line indicating non-zero exit code
AND the log format MUST be consistent with the success case log format
