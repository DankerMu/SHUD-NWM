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

#### Scenario: Write-side submission lifecycle is reserved for later slices

- **WHEN** #833 read-side file repository write methods are called before the
  write-side slice lands
- **THEN** they fail with `FILE_JOURNAL_WRITE_NOT_IMPLEMENTED`
- **AND** atomic reservation, binding, pipeline-job writes, pipeline-event
  writes, Slurm job ID persistence, retry attempt writes, and materialized
  latest writes remain explicitly out of scope for the read-side slice.

#### Scenario: Read-side journal schemas are explicit

- **WHEN** node-22 scheduler reads orchestration state in DB-free mode
- **THEN** append-only records use schema
  `nhms.scheduler.file_orchestration_journal.v1`
- **AND** materialized latest views use schema
  `nhms.scheduler.file_orchestration_latest.v1`
- **AND** records include source/cycle/model/run/candidate identity, job ID,
  Slurm job ID, stage, status, error code, sequence or event ID, redacted
  runtime-root evidence, and replay metadata.

#### Scenario: Read-side journal replays without writes

- **WHEN** only append-only journal records exist for a source/cycle
- **THEN** the DB-free file repository can replay active pipeline jobs and
  pipeline events into candidate state
- **AND** missing materialized latest views do not block replay.

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

#### Scenario: Read-side slice does not claim write support

- **WHEN** #833 file repository write methods are called before the write-side
  slice lands
- **THEN** they fail with `FILE_JOURNAL_WRITE_NOT_IMPLEMENTED`
- **AND** scheduler default DB-free mutation remains blocked until the write
  side is implemented.

#### Scenario: Malformed file state fails closed

- **WHEN** a DB-free scheduler read sees malformed JSON, unsupported schema, or
  source/cycle identity mismatch in file journal state
- **THEN** duplicate-prevention reads fail closed as active/blocking evidence
- **AND** malformed state is not treated as an absent row.

### Requirement: File journal is contract-tested against DB semantics

The system SHALL include repository contract tests that verify file-backed
orchestration state behavior against existing scheduler semantics.

#### Scenario: Contract fixtures cover critical repository methods

- **WHEN** repository contract tests run
- **THEN** the #833 read-side fixture covers active orchestration, active
  pipeline, completed pipeline, active Slurm jobs, candidate state,
  model/forcing context reads, and query helpers
- **AND** write-side lifecycle, reservation/bind, job status updates, event
  insertion, retry supersession, and permanent failure guards are explicitly
  deferred to later write/retry/migration slices.

#### Scenario: Read-side contract fixtures cover scheduler planning

- **WHEN** #833 focused tests run without `DATABASE_URL`
- **THEN** they prove file-backed active/completed/candidate/active-Slurm
  decisions are visible to scheduler planning
- **AND** DB-backed active/orchestrator repository factories are not called in
  DB-free read-side construction.
