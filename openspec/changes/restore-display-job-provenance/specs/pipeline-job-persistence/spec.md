## MODIFIED Requirements

### Requirement: pipeline_job Record Creation

In PostgreSQL-backed scheduler mode, the system SHALL create a `pipeline_job` record in the `ops.pipeline_job` table whenever the Orchestrator submits a stage job to Slurm. In DB-free scheduler mode, the node-22 file journal SHALL be the authoritative job record at submission, and node-27's `ops.pipeline_job` SHALL be a derived projection created from a validated published job-provenance record. Under no deployment SHALL node-27 display or ingest fabricate a job solely to satisfy evidence collection.

#### Scenario: PostgreSQL orchestrator submits a single-basin stage job

- **WHEN** the PostgreSQL-backed Orchestrator submits a stage job (e.g., `run_shud_forecast_array`) for a single basin via `sbatch`
- **THEN** a new row SHALL be inserted into `ops.pipeline_job` with:
  - `job_id` (PK, TEXT) generated before submission
  - `job_type` set to the upstream stage name (e.g., `run_shud_forecast_array`)
  - `slurm_job_id` populated with the Slurm-assigned job ID returned by `sbatch`
  - `cycle_id` (TEXT) set to the current forecast cycle ID
  - `stage` set to one of: `convert_canonical`, `produce_forcing_array`, `run_shud_forecast_array`, `parse_output_array`, `publish_tiles`
  - `run_id` (TEXT, nullable) set to the basin run identifier
  - `model_id` set to the basin model identifier (M3 addition, not in upstream draft)
  - `status` set to `pending`
  - `submitted_at` set to the current UTC timestamp
  - `started_at`, `finished_at` set to NULL
  - `exit_code` set to NULL
  - `error_code` set to NULL
  - `error_message` set to NULL
  - `log_uri` set to NULL (populated later)
  - `retry_count` set to 0
  - `created_at` and `updated_at` set to the current UTC timestamp

#### Scenario: DB-free orchestrator submits a stage job

- **WHEN** the DB-free node-22 Orchestrator submits a stage job
- **THEN** the authoritative job record SHALL be created in the file orchestration journal with the same identity and lifecycle fields
- **AND** `ops.pipeline_job` SHALL not be populated by the node-22 journal lane
- **AND** a node-27 projection SHALL be created only from the validated published job-provenance record
- **AND** forecast-scoped rows SHALL match source, cycle, run, model, and job identity
- **AND** cycle-scoped rows SHALL keep null `model_id` and their original cycle `run_id` rather than inherit an enclosing forecast identity


#### Scenario: PostgreSQL orchestrator submits a cycle-level stage job (no per-basin scope)

- **WHEN** the PostgreSQL-backed Orchestrator submits a cycle-level stage job (e.g., `convert_canonical`, `publish_tiles`) that is not scoped to a specific basin
- **THEN** a new `pipeline_job` row SHALL be inserted with `run_id` set to NULL and `model_id` set to NULL; all other fields populated as above

#### Scenario: sbatch submission fails

- **WHEN** `sbatch` returns a non-zero exit code during submission
- **THEN** a `pipeline_job` record SHALL still be created with `status` set to `submission_failed`, `slurm_job_id` set to NULL, and `error_message` populated with the sbatch stderr output

### Requirement: Status Synchronization via sacct

In PostgreSQL-backed scheduler mode, the Orchestrator SHALL update the `pipeline_job` status when `sacct` returns a new status for the corresponding Slurm job. In DB-free scheduler mode, the file journal SHALL perform status synchronization; the node-27 derived projection SHALL advance only from validated published provenance whose source authority and ordering match the journal record.

#### Scenario: Slurm job transitions to RUNNING

- **WHEN** `sacct` reports status `RUNNING` for a `slurm_job_id` whose current `pipeline_job.status` is `pending`
- **THEN** the Orchestrator SHALL update the row: set `status` to `running`, `started_at` to the Slurm-reported start time, and `updated_at` to the current UTC timestamp

#### Scenario: Slurm job completes successfully

- **WHEN** `sacct` reports status `COMPLETED` with exit code 0 for a `slurm_job_id`
- **THEN** the Orchestrator SHALL update the row: set `status` to `succeeded`, `finished_at` to the Slurm-reported end time, `exit_code` to 0, and `updated_at` to the current UTC timestamp

#### Scenario: Slurm job fails

- **WHEN** `sacct` reports status `FAILED`, `TIMEOUT`, or `NODE_FAIL` for a `slurm_job_id`
- **THEN** the Orchestrator SHALL update the row: set `status` to `failed`, `finished_at` to the Slurm-reported end time, `exit_code` to the reported exit code, `error_code` to the mapped error code (e.g., `SLURM_TIMEOUT`, `NODE_FAILURE`), `error_message` to a human-readable description, and `updated_at` to the current UTC timestamp

#### Scenario: Slurm job cancelled

- **WHEN** `sacct` reports status `CANCELLED` for a `slurm_job_id`
- **THEN** the Orchestrator SHALL update the row: set `status` to `cancelled`, `finished_at` to the current UTC timestamp, and `updated_at` to the current UTC timestamp

#### Scenario: log_uri populated on job completion

- **WHEN** a Slurm job reaches a terminal status (`succeeded`, `failed`, `cancelled`)
- **THEN** the Orchestrator SHALL set `log_uri` to the path of the Slurm output log file (derived from the sbatch `--output` directive)
- **AND** in the DB-free display projection, a previously null `log_uri` MAY be enriched monotonically only when the source journal proves that exact task/log binding and the published artifact verifies.

### Requirement: pipeline_event Append-Only Event Log

The system SHALL maintain an append-only `ops.pipeline_event` table that records every status transition for a `pipeline_job`, matching upstream schema. In the DB-free scheduler lane the file journal's own append-only events SHALL remain the authority; node-27 SHALL not fabricate historical status-transition events merely to reconstruct unavailable authority history.

#### Scenario: Status transition event recorded

- **WHEN** a `pipeline_job` status changes from one value to another (e.g., `pending` to `running`)
- **THEN** a new row SHALL be appended to `ops.pipeline_event` with:
  - `event_id` — BIGSERIAL, PRIMARY KEY
  - `entity_type` — TEXT, set to `'pipeline_job'`
  - `entity_id` — TEXT, referencing the `pipeline_job.job_id`
  - `event_type` — TEXT, set to `'status_change'`
  - `status_from` — TEXT (the previous status)
  - `status_to` — TEXT (the new status)
  - `message` — TEXT, optional human-readable description
  - `details` — JSONB, containing contextual data (e.g., `{"exit_code": 1, "slurm_state": "FAILED", "error_code": "SLURM_TIMEOUT"}`)
  - `created_at` — TIMESTAMPTZ, set to the current UTC timestamp

#### Scenario: Initial submission event

- **WHEN** a `pipeline_job` is first created with status `pending`
- **THEN** an event SHALL be appended with `event_type` set to `'submission'`, `status_from` set to NULL, and `status_to` set to `'pending'`

#### Scenario: Retry event

- **WHEN** a `pipeline_job` is retried after a failure
- **THEN** an event SHALL be appended with `event_type` set to `'retry'`, `status_from` set to `'failed'`, and `status_to` set to `'pending'`

#### Scenario: Event table is immutable

- **WHEN** any attempt is made to UPDATE or DELETE a row in `ops.pipeline_event`
- **THEN** the operation MUST be rejected (the table is append-only by application-level constraint)
