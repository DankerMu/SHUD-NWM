# Capability Spec: slurm-job-chain

## Context

The NHMS M1 milestone requires a 5-stage linear Slurm job chain to orchestrate the end-to-end forecast pipeline: download → canonical → forcing → forecast → parse. The chain uses LAZY submission: the orchestrator submits only the first stage initially, then submits the next stage only after the current stage succeeds. In M1 scope, all jobs are submitted through the Mock Slurm Gateway (`slurm_gateway.backend = mock`). The chain is strictly linear — no job arrays, no parallel fan-out, no partial success handling. On stage failure, subsequent stages are NOT submitted and the pipeline is marked as failed with the failing stage info. Job tracking is recorded in `ops.pipeline_job` and status transition events in `ops.pipeline_event`.

**Ownership**: The orchestrator creates the `hydro.hydro_run` record and updates stage-level status. Each worker (Slurm callback) updates its own completion status. The Slurm gateway updates `submitted` → `running`, and the runtime/parser updates `running` → `succeeded`/`failed`.

**Status ENUMs**:
- `hydro.run_status`: created, staged, submitted, running, succeeded, parsed, frequency_done, published, failed, cancelled, superseded
- `met.cycle_status`: discovered, downloading, raw_complete, canonical_ready, forcing_ready_partial, forcing_ready, forecast_running, parsed_partial, complete, published, failed_download, failed_convert, failed_forcing, failed_run, failed_parse, failed_publish

---

## ADDED Requirements

### Requirement: Stage template definition

The job chain orchestrator SHALL define exactly 5 sbatch templates, one per pipeline stage. Each template MUST specify the script path, resource requirements, environment variables, and output/error log paths. Templates MUST be parameterized by cycle_time, basin_id, and run_id.

#### Scenario: All five sbatch templates are defined

- **WHEN** the orchestrator initializes for a forecast cycle
- **THEN** exactly 5 sbatch templates MUST be available: `download_gfs.sbatch`, `convert_canonical.sbatch`, `produce_forcing.sbatch`, `run_shud_forecast.sbatch`, `parse_output.sbatch`
- **THEN** each template MUST be a valid sbatch script with `#!/bin/bash` header and `#SBATCH` directives

#### Scenario: Templates are parameterized with cycle and run context

- **WHEN** a template is rendered for a specific cycle
- **THEN** the template MUST accept parameters: `cycle_time`, `basin_id`, `run_id`, and `workspace_dir`
- **THEN** the rendered script MUST substitute these parameters into environment variables and file paths
- **THEN** the output log path MUST follow the pattern `logs/{run_id}/{stage_name}.out`

#### Scenario: Templates define stage-specific resource requirements

- **WHEN** a template is rendered
- **THEN** each template MUST include `#SBATCH --job-name={stage_name}_{run_id}`
- **THEN** each template MUST include `#SBATCH --output` and `#SBATCH --error` directives pointing to distinct log files
- **THEN** the Mock Slurm Gateway MUST accept these directives without validation errors

---

### Requirement: Dependency chain orchestration

The orchestrator SHALL use LAZY submission to execute the 5 stages in strict sequential order: only the first stage is submitted initially, and each subsequent stage is submitted only after the previous stage succeeds. The chain MUST be linear with no branching or parallel execution. The orchestrator MUST NOT pre-submit all stages with `--dependency=afterok`.

#### Scenario: Stages are submitted lazily in correct order

- **WHEN** the orchestrator triggers a forecast cycle
- **THEN** stage 1 (`download_gfs`) is submitted immediately with no dependency
- **THEN** stage 2 (`convert_canonical`) is submitted ONLY after stage 1 reaches `succeeded` status
- **THEN** stage 3 (`produce_forcing`) is submitted ONLY after stage 2 reaches `succeeded` status
- **THEN** stage 4 (`run_shud_forecast`) is submitted ONLY after stage 3 reaches `succeeded` status
- **THEN** stage 5 (`parse_output`) is submitted ONLY after stage 4 reaches `succeeded` status
- **THEN** the orchestrator MUST NOT use `--dependency=afterok` to chain jobs

#### Scenario: Chain halts when a stage fails

- **WHEN** any stage in the chain transitions to `failed` status
- **THEN** the orchestrator MUST NOT submit any subsequent stages
- **THEN** the `hydro.hydro_run` status MUST be set to `failed`
- **THEN** the failure MUST be recorded in `ops.pipeline_event` with `entity_type='pipeline_job'`, the failing stage's `entity_id`, `event_type='status_change'`, `status_from` (previous status), `status_to='failed'`, and error details
- **THEN** the `hydro.hydro_run` record MUST include `error_code` and `error_message` identifying the failing stage

#### Scenario: No parallel fan-out or job arrays in M1

- **WHEN** the orchestrator builds the job chain
- **THEN** it MUST NOT use `--array` or any array job syntax
- **THEN** it MUST NOT submit multiple jobs for the same stage
- **THEN** exactly 5 job submissions MUST occur for a successful pipeline (one per stage, submitted lazily)

---

### Requirement: Pipeline job tracking

The orchestrator SHALL write one `ops.pipeline_job` record per stage per cycle. Each record MUST track the `slurm_job_id`, `job_type`, status, log URI, and timing information (`submitted_at`, `started_at`, `finished_at`) to enable monitoring and debugging.

#### Scenario: Pipeline job record is created on submission

- **WHEN** a stage job is submitted to the Slurm Gateway
- **THEN** an `ops.pipeline_job` record MUST be inserted with:
  - `job_type`: one of `download`, `canonical`, `forcing`, `forecast`, `parse`
  - `slurm_job_id`: the mock job ID returned by the gateway (e.g., `mock_1001`)
  - `status`: `submitted`
  - `log_uri`: S3 URI to the expected log file
  - `submitted_at`: UTC timestamp of submission
  - `started_at`: NULL (not yet started)
  - `finished_at`: NULL (not yet finished)

#### Scenario: Pipeline job record is updated on status change

- **WHEN** the gateway reports a job status change (e.g., `submitted` → `running`)
- **THEN** the corresponding `ops.pipeline_job` record MUST be updated with the new `status`
- **THEN** `started_at` MUST be set when status transitions to `running`
- **THEN** `finished_at` MUST be set when status reaches a terminal state (`succeeded`, `failed`, `cancelled`)

#### Scenario: All five stages produce pipeline job records for a complete run

- **WHEN** a forecast cycle completes successfully end-to-end
- **THEN** exactly 5 `ops.pipeline_job` records MUST exist for that pipeline
- **THEN** all 5 records MUST have `status = succeeded`
- **THEN** each record MUST have non-NULL `submitted_at`, `started_at`, and `finished_at`

---

### Requirement: Pipeline event logging

The orchestrator SHALL write an `ops.pipeline_event` record for every status transition of every stage. Events provide an immutable audit trail of pipeline execution.

#### Scenario: Status transition generates an event

- **WHEN** a pipeline job transitions from one status to another (e.g., `submitted` → `running`)
- **THEN** an `ops.pipeline_event` record MUST be inserted with:
  - `entity_type`: the type of entity (e.g., `pipeline_job` or `hydro_run`)
  - `entity_id`: the identifier of the entity (e.g., the pipeline_job ID or the hydro_run ID)
  - `event_type`: the type of event (e.g., `status_change`)
  - `status_from`: the previous status (NULL for initial submission)
  - `status_to`: the new status
  - `created_at`: UTC timestamp of the transition
  - `message`: optional human-readable description

#### Scenario: Complete successful pipeline generates correct event count

- **WHEN** a 5-stage pipeline completes successfully
- **THEN** at least 10 `ops.pipeline_event` records MUST exist (2 transitions per stage: `submitted→running`, `running→succeeded`)
- **THEN** events MUST be ordered by `created_at` ascending

#### Scenario: Failure event includes diagnostic information

- **WHEN** a stage transitions to `failed`
- **THEN** the `ops.pipeline_event` record MUST include a `message` field with the error description
- **THEN** the `message` MUST include the stage name and the mock gateway error code (if available)

---

### Requirement: End-to-end cycle trigger

The orchestrator SHALL provide a single entry point that accepts a cycle_time and basin_id and triggers the full 5-stage chain from cycle discovery through `river_timeseries` ingestion. This is the primary interface for M1 forecast execution.

#### Scenario: Single trigger starts the full pipeline

- **WHEN** `trigger_forecast(cycle_time="2026050700", basin_id="changjiang_demo")` is called
- **THEN** the orchestrator MUST create a `hydro.hydro_run` record with `status="created"` (run_id e.g., `fcst_gfs_2026050700_changjiang_demo_shud_v12`)
- **THEN** the orchestrator MUST prepare the workspace and update status to `staged`
- **THEN** the orchestrator MUST render the stage 1 sbatch template and submit it
- **THEN** the `hydro.hydro_run` status MUST transition to `submitted` after stage 1 is submitted
- **THEN** the orchestrator MUST lazily submit each subsequent stage only after the previous stage succeeds
- **THEN** as each stage runs, the Slurm gateway updates status to `running`

#### Scenario: Pipeline completion updates hydro_run to parsed

- **WHEN** the final stage (`parse_output`) completes with `succeeded` status
- **THEN** the `hydro.hydro_run` record MUST be updated to `status="parsed"`
- **THEN** `hydro.river_timeseries` MUST contain forecast flow data (variable `q_down`) for the cycle's river segments

#### Scenario: Duplicate trigger for same cycle is rejected

- **WHEN** `trigger_forecast()` is called with a cycle_time and basin_id that already has an active (non-terminal) pipeline
- **THEN** the orchestrator MUST return an error indicating the pipeline is already in progress
- **THEN** no duplicate jobs MUST be submitted

---

### Requirement: Mock gateway integration

The orchestrator SHALL submit all jobs through the Mock Slurm Gateway when `slurm_gateway.backend = mock`. The integration MUST use the gateway's HTTP API for job submission, status polling, and log retrieval.

#### Scenario: Jobs are submitted via Mock Gateway HTTP API

- **WHEN** the orchestrator submits a stage job
- **THEN** it MUST call `POST /api/v1/slurm/jobs` on the Mock Slurm Gateway
- **THEN** the request body MUST include `run_id`, `model_id`, and the rendered sbatch script content
- **THEN** the returned job ID (e.g., `mock_1001`) MUST be stored as `slurm_job_id` in `ops.pipeline_job`

#### Scenario: Status polling uses Mock Gateway endpoint

- **WHEN** the orchestrator checks job progress
- **THEN** it MUST call `GET /api/v1/slurm/jobs/{job_id}` on the Mock Slurm Gateway
- **THEN** the returned status MUST be used to update `ops.pipeline_job` and generate `ops.pipeline_event` records

#### Scenario: Mock gateway delays are respected

- **WHEN** the Mock Gateway is configured with `delay_to_running_seconds=2` and `delay_to_succeeded_seconds=5`
- **THEN** the orchestrator MUST poll at a configurable interval (default 1 second) until the job reaches a terminal state
- **THEN** the orchestrator MUST NOT assume instant completion
- **THEN** the total pipeline duration MUST reflect the sum of mock delays across all 5 stages

#### Scenario: Log retrieval uses Mock Gateway endpoint

- **WHEN** a stage job reaches a terminal state
- **THEN** the orchestrator MUST call `GET /api/v1/slurm/jobs/{job_id}/logs` to retrieve mock log output
- **THEN** the log content MUST be uploaded to the `log_uri` specified in `ops.pipeline_job`
