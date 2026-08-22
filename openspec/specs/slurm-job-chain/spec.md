# slurm-job-chain Specification

## Purpose
TBD - created by archiving change m1-gfs-forecast-loop. Update Purpose after archive.
## Requirements
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

### Requirement: Cycle-stage terminal handling is fail-closed

The per-stage cycle executor for `orchestrate_cycle` SHALL advance to
a downstream stage only when the current stage's terminal status is in
the pipeline success set or is the partial-success status handled by
the partial-capture mechanics; every other stage terminal SHALL end the
cycle with an explicit non-success cycle terminal, and a cycle whose
work was skipped or unrecognized SHALL NOT report success on the cycle
result or on the scheduler evidence plane. The duplicate-submission
deferral SHALL be honored wherever a stage submission is issued —
including nested resubmissions inside the partial-array retry helper —
not only at the top-level stage result.

#### Scenario: Duplicate-submission skip defers the cycle

- **WHEN** a cycle stage submission returns
  `skipped_duplicate_submission` because the reserve gate found
  another pass holding the in-flight reservation — whether from the
  top-level stage submission or from a nested resubmission issued by
  the partial-array retry helper
- **THEN** the executor SHALL NOT run any downstream stage of that
  cycle in the same pass
- **THEN** the cycle SHALL terminate with the dedicated non-success
  terminal `skipped_duplicate_submission` — not a success status, and
  not a failure terminal that would trigger failure-retry adjudication
  or resubmission against the reservation-holding pass's active row
- **THEN** the executor SHALL NOT rewrite pending retry tasks as
  failed, SHALL NOT issue a further durable failed status write for
  the deferred tasks' runs as a consequence of the deferral, and
  SHALL NOT derive a further retry attempt (no fresh retry-suffixed
  idempotency key) from the deferred submission
- **THEN** the skipped stage's span counters SHALL record zero
  submissions and zero failures

#### Scenario: Skipped candidate is non-success on the evidence plane

- **WHEN** scheduler evidence is built for a candidate whose cohort
  cycle terminated with `skipped_duplicate_submission`
- **THEN** the candidate's evidence SHALL NOT report final candidate
  success and SHALL surface a not-successful residual signal
- **THEN** the candidate's evidence item SHALL carry retrievable
  duplicate-skip evidence derived from the cohort's cycle result
  (cohort-scoped, matching the existing stage-status fan-out
  semantics)
- **THEN** a pass that submitted other work before the skip SHALL
  surface as a partial, review-visible pass rather than a fully
  successful one
- **THEN** readiness validation SHALL recognize the skip status in
  its pass-status vocabulary as a review-visible (blocked) state and
  SHALL count the skipped candidate consistently with the producer's
  partial accounting — without manufacturing a status-vocabulary or
  partial-cardinality acceptance error, and without loosening the
  compatibility rules that infer submission from model-run statuses

#### Scenario: Unrecognized stage terminal fails closed

- **WHEN** a cycle stage returns a terminal status that is neither in
  the pipeline success set, nor the partial-success status, nor a
  status with a dedicated break branch
- **THEN** the executor SHALL terminate the cycle as failed with an
  explicit error code identifying the unrecognized status rather than
  silently advancing downstream

#### Scenario: Stage-versus-cycle consistency invariant

- **WHEN** a cycle result is returned and any stage span with a
  positive entering basin count records every entering basin as failed
- **THEN** the cycle terminal SHALL NOT be a member of the pipeline
  success set

### Requirement: Duplicate-submission deferral has one cross-plane status meaning

The orchestrator SHALL preserve `skipped_duplicate_submission` as the raw reserve-gate terminal and SHALL translate that terminal to the existing production status `blocked` on every stage-evidence projection. The translator SHALL continue to map unrecognized statuses to `failed`.

#### Scenario: Duplicate-submission stage evidence is blocked

- **WHEN** either scheduler stage-evidence projection receives a stage whose status is `skipped_duplicate_submission`
- **THEN** the raw status SHALL remain `skipped_duplicate_submission`
- **THEN** its `production_status` SHALL be `blocked`
- **THEN** the value SHALL be a member of `PRODUCTION_STATUS_TAXONOMY`

#### Scenario: Unknown status still fails closed

- **WHEN** the production status translator receives an unrecognized status
- **THEN** it SHALL return `failed`
- **THEN** adding duplicate-submission support SHALL NOT broaden the unknown-status fallback

### Requirement: Reconciliation-pending nested submissions defer without manufacturing failure

The partial-array retry helper SHALL preserve a nested `submit_result_ambiguous` or `reconcile_unverified` result as a reconciliation deferral. It SHALL map either stage terminal to cycle terminal `reconciling`, while preserving the distinct duplicate-submission skip terminal.

#### Scenario: Nested ambiguous submission stops on reconciling

- **WHEN** a nested partial-array resubmission returns `submit_result_ambiguous` without an aggregation
- **THEN** the pending tasks SHALL NOT be rewritten as failed
- **THEN** the cycle SHALL terminate as `reconciling` with the raw ambiguous stage result
- **THEN** no downstream stage SHALL run and no further retry attempt SHALL be derived

#### Scenario: Nested unverified reconciliation preserves durable no-op

- **WHEN** a nested partial-array resubmission returns `reconcile_unverified` without an aggregation
- **THEN** the pending tasks SHALL NOT be rewritten as failed
- **THEN** the cycle SHALL terminate as `reconciling`
- **THEN** the existing reconciliation event or row MAY remain, but the executor SHALL NOT add a second partial or failed cycle-status write
- **THEN** no downstream stage or further retry attempt SHALL run

#### Scenario: Nested pending replacement preserves confirmed dispatch identity

- **GIVEN** the prior partial stage contains a non-empty Slurm master job identity proving that the full array was dispatched
- **WHEN** a nested resubmission returns a raw reconciliation-pending result with no Slurm identity or task outcomes
- **THEN** the returned pending stage SHALL retain the prior non-empty Slurm master job identity
- **THEN** it SHALL retain the raw pending terminal and empty task outcomes
- **THEN** it SHALL NOT reconstruct stale per-task outcomes or infer any additional submission

#### Scenario: Outer retry pending replacement preserves confirmed dispatch identity

- **GIVEN** the current cycle's prior whole-array stage contains a non-empty Slurm master job identity
- **WHEN** a same-stage outer retry returns a raw reconciliation-pending result with no Slurm identity or task outcomes
- **THEN** the replacement stage SHALL retain the prior non-empty Slurm master job identity
- **THEN** it SHALL retain the raw retry pipeline identity, pending terminal, error fields, and empty task outcomes
- **THEN** a non-empty raw retry master identity SHALL remain authoritative instead of being overwritten
- **THEN** no further retry or downstream stage SHALL be derived from the pending result

#### Scenario: Intermediate empty-ID failures cannot erase confirmed dispatch identity

- **GIVEN** the current stage has already returned a non-empty confirmed Slurm master identity
- **AND** one or more later same-stage retry results carry no Slurm identity, including a retryable `submission_failed`
- **WHEN** a subsequent same-stage retry returns an empty-ID reconciliation-pending result
- **THEN** the returned pending stage SHALL retain the earlier confirmed master identity
- **THEN** every intermediate durable retry row and the final raw retry metadata SHALL remain unchanged
- **THEN** no retry after the pending result or downstream stage SHALL run

#### Scenario: Normal-start indexed replacement preserves confirmed dispatch identity

- **GIVEN** a normal full-chain start has already completed stages before the current array stage
- **AND** the current stage has a confirmed Slurm master identity
- **WHEN** its outer retry returns an empty-ID reconciliation-pending result through the indexed result slot
- **THEN** the same confirmed-master preservation rule SHALL apply as on the restart-at-stage trailing slot
- **THEN** the returned result SHALL retain raw retry metadata and SHALL NOT derive another retry or downstream work

#### Scenario: Bare pending result does not manufacture dispatch identity

- **GIVEN** the current stage loop has never observed a confirmed Slurm submission identity
- **WHEN** a reconciliation-pending stage result is returned
- **THEN** the result SHALL remain without Slurm submission identity
- **THEN** the pending status alone SHALL NOT prove that Slurm submission occurred

#### Scenario: Reconciliation defer timing is neither submitted nor failed

- **WHEN** either governed nested reconciliation-pending terminal closes a stage span entered with `N` basins
- **THEN** the final span SHALL report `basin_count=N`
- **THEN** it SHALL report `submitted_count=0` and `failed_count=0`
- **THEN** ordinary failed or `submission_failed` terminals SHALL retain their existing failure attribution

#### Scenario: Unrelated nested terminals retain existing behavior

- **WHEN** a nested resubmission returns `skipped_duplicate_submission`, `submission_failed`, success, or an ordinary failed aggregation
- **THEN** duplicate skip SHALL retain its dedicated skip terminal
- **THEN** `submission_failed` and ordinary success/failure retry semantics SHALL remain unchanged

