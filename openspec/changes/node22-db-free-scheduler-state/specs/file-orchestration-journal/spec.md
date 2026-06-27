## ADDED Requirements

### Requirement: Scheduler can use a file-backed orchestration journal

The system SHALL provide a file-backed implementation of the scheduler's
active/completed/candidate/job/event state responsibilities currently supplied
by `PsycopgOrchestratorRepository`.

#### Scenario: File journal preserves active pipeline detection

- **WHEN** scheduler evaluates a source/cycle/model candidate
- **THEN** the file journal can answer whether an active orchestration or active
  pipeline already exists
- **AND** active statuses prevent duplicate submission.

#### Scenario: File journal preserves completed pipeline detection

- **WHEN** scheduler evaluates a source/cycle/model candidate
- **THEN** the file journal can answer whether the pipeline is already
  completed
- **AND** completed candidates are skipped with bounded evidence.

#### Scenario: File journal records submission lifecycle

- **WHEN** scheduler reserves and submits a pipeline job
- **THEN** the file journal atomically records reservation, binding,
  pipeline-job state, pipeline-event rows, Slurm job ID, stage, retry attempt,
  and runtime-root evidence
- **AND** materialized latest state can be replayed from append-only journal
  records.

#### Scenario: File journal preserves active Slurm job detection

- **WHEN** scheduler asks for active Slurm jobs for a source/cycle/model
- **THEN** the file journal returns bounded queued, pending, submitted, or
  running Slurm job evidence for that identity
- **AND** active Slurm jobs prevent duplicate submission and support
  cancel/status-sync evidence.

#### Scenario: File journal preserves retry and permanent-failure decisions

- **WHEN** scheduler sees failed, retried, manually repaired, or permanently
  failed state
- **THEN** file-backed candidate state produces the same retry/permanent guard
  decisions as the existing DB-backed repository for the same event history
- **AND** stale historical `download_source_cycle` failures do not block a
  node-27 raw-ready restart at `convert` when supersession evidence exists.

#### Scenario: File journal replaces DB-backed retry service

- **WHEN** a DB-free scheduler orchestrator is constructed
- **THEN** retry and permanent-failure state is written to and read from the
  file journal
- **AND** DB-backed `_retry_service_from_env`, SQLAlchemy `PipelineStore`, and
  PostgreSQL retry paths are not constructed.

#### Scenario: Historical scheduler rows migrate into the journal

- **WHEN** node-22 DB-free cutover imports scheduler-relevant rows from
  historical `:55433`
- **THEN** active/completed/candidate/job/event/retry/permanent-failure state is
  converted into append-only journal records
- **AND** the migration receipt records cutoff time, row counts, checksums,
  replay status, and stale failure supersession evidence.

#### Scenario: Journal writes are atomic and bounded

- **WHEN** file journal state is updated
- **THEN** writes use the scheduler file lock plus temporary files and atomic
  rename for materialized latest views
- **AND** evidence/log output remains bounded and credential-safe.

### Requirement: File journal is contract-tested against DB semantics

The system SHALL include repository contract tests that verify file-backed
orchestration state behavior against existing scheduler semantics.

#### Scenario: Contract fixtures cover critical repository methods

- **WHEN** repository contract tests run
- **THEN** they cover active orchestration, active pipeline, completed pipeline,
  active Slurm jobs, candidate state, model/forcing context reads,
  forecast-cycle and hydro-run lifecycle writes, reservation/bind, job status
  updates, event insertion, retry supersession, and permanent failure guards.
