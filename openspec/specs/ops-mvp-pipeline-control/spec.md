# ops-mvp-pipeline-control Specification

## Purpose
TBD - created by archiving change m21-qhh-hydro-met-ops-mvp. Update Purpose after archive.
## Requirements
### Requirement: Ops MVP entry

The frontend SHALL expose an operations MVP entry focused on QHH pipeline status, jobs, logs, and retry controls.

#### Scenario: Ops navigation entry
- **WHEN** the app shell renders for an authorized operator
- **THEN** the visible workflow includes an operations entry at `/ops` or an equivalent route alias
- **AND** it can reuse `/monitoring` implementation details without exposing non-MVP clutter in the primary workflow.

#### Scenario: Source and cycle selection
- **WHEN** an operator selects a source and cycle
- **THEN** the operations page filters stage status and jobs to that QHH product context
- **AND** it preserves source/cycle query state
- **AND** if the backend cannot provide that filtered context it renders an explicit unsupported/unavailable state instead of mixing jobs from other cycles.

### Requirement: Formal pipeline-backed stage and job status

The operations MVP SHALL read stage, job, log, and retry state from formal backend pipeline/orchestrator APIs and persisted job records.

#### Scenario: Stage cards
- **WHEN** QHH pipeline data exists for a selected cycle
- **THEN** the page displays persisted `download`, `convert`, `forcing`, `forecast`, `parse`, `frequency`, and `publish` stage states from API responses
- **AND** UI labels may describe the `forecast` stage as SHUD execution where helpful, but persisted stage names remain canonical and consistent with pipeline job/event records.

#### Scenario: Jobs table
- **WHEN** jobs are returned for the selected context
- **THEN** the table includes job id, run id, stage, status, Slurm job id when available, started time, finished time, duration, retry count, and log availability.

#### Scenario: Log modal
- **WHEN** an operator opens a job log
- **THEN** the page requests the backend log route and displays bounded stdout/stderr content or an explicit unavailable/error reason
- **AND** log paths remain server-side and are not exposed as local filesystem access instructions.

#### Scenario: Retry failed run
- **WHEN** a job or run is in `failed`, `submission_failed`, `partially_failed`, or `permanently_failed` state and the operator is authorized
- **THEN** the page shows a restart action that calls `POST /api/v1/runs/{run_id}/retry`
- **AND** after success it refreshes status, stages, and jobs.

#### Scenario: Authorization gate
- **WHEN** a non-operator opens `/ops`
- **THEN** mutating controls such as retry and cancel are hidden or disabled according to existing RBAC behavior
- **AND** direct retry or cancel API calls by a non-operator are rejected by the backend according to existing authorization rules
- **AND** the page does not bypass production authentication requirements beyond documented internal MVP dev-role overrides.

### Requirement: Orchestrator boundary

The operations MVP SHALL treat qhh diagnostic scripts as reproduction evidence only and use the formal backend scheduler/orchestrator path for operational status.

#### Scenario: Formal scheduler source
- **WHEN** the MVP operations page displays run status or retry actions
- **THEN** its data comes from backend pipeline status, jobs, logs, and retry APIs tied to `ops.pipeline_job` or equivalent orchestrator persistence
- **AND** it does not depend on `.nhms-runs/qhh-continuous` state JSON files as the production control source.

#### Scenario: Controlled failure evidence
- **WHEN** MVP readiness is validated
- **THEN** a controlled failed QHH run or stage is visible as failed in `/ops`
- **AND** retry creates new job/pipeline evidence and transitions through submitted/running to succeeded or a documented terminal failure.

