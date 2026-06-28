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

#### Scenario: File journal writes lifecycle and pipeline state

- **WHEN** DB-free scheduler or orchestrator code creates lifecycle,
  reservation, pipeline-job, or pipeline-event state
- **THEN** the file journal writes append-only records with atomic/no-clobber
  file behavior and materializes latest/query views for the same source/cycle
  identity
- **AND** reservation, binding, job-status, event insertion, forecast/hydro
  status, retry, and permanent-failure writes preserve the existing DB-backed
  repository semantics.

#### Scenario: Read-side journal schemas are explicit

- **WHEN** node-22 scheduler reads orchestration state in DB-free mode
- **THEN** append-only records use schema
  `nhms.scheduler.file_orchestration_journal.v1`
- **AND** materialized latest views use schema
  `nhms.scheduler.file_orchestration_latest.v1`
- **AND** records include source/cycle/model/run/candidate identity, job ID,
  Slurm job ID, stage, status, error code, sequence or event ID, redacted
  runtime-root evidence, and replay metadata.

#### Scenario: Trusted read surfaces validate schema and identity before replay

- **WHEN** the reader consumes latest views, append-only records, sidecar
  pipeline events, direct pipeline-job snapshots, model contexts, or forcing
  contexts
- **THEN** every consumed row must pass its applicable schema, identity, and
  field contract before it can affect active, completed, candidate, query, or
  Slurm evidence
- **AND** non-object embedded job/event rows, missing required identity fields,
  invalid cycle timestamps, and mismatched source/cycle/model/run/job identity
  fail closed as file-journal blocking evidence.

#### Scenario: Direct pipeline-job snapshots cannot mask scoped journal truth

- **WHEN** `<journal-root>/pipeline-jobs/<job_id>.json` is read
- **THEN** it must be a journal-shaped `pipeline_job` record with schema
  `nhms.scheduler.file_orchestration_journal.v1` and matching source/cycle/
  model/job identity
- **AND** schema-less or mismatched direct snapshots fail closed
- **AND** terminal direct snapshots do not override active latest/journal rows
  for the same job even when the direct snapshot has a later `updated_at`.

#### Scenario: Sidecar pipeline events use the journal record contract

- **WHEN** `<journal-root>/pipeline-events/<source>/<cycle>.jsonl` is replayed
- **THEN** each line must use the append-only journal schema, `pipeline_event`
  record type, and matching source/cycle identity
- **AND** wrong schema, wrong cycle, or malformed event identity blocks replay
  instead of being treated as absent evidence.

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

#### Scenario: DB-free mode uses file-backed retry state

- **WHEN** DB-free mode constructs a production orchestrator
- **THEN** it uses a file-journal retry service instead of DB-backed
  `_retry_service_from_env()` or SQLAlchemy `PipelineStore`
- **AND** retry attempts, retry-limit exhaustion, manual repair markers, and
  permanent-failure state are represented in append-only file journal records.

#### Scenario: Historical scheduler state migrates into append-only journal

- **WHEN** operators export scheduler-relevant rows from historical node-22
  PostgreSQL `:55433`
- **THEN** the importer writes active/completed/candidate/job/event/retry and
  permanent-failure rows into the file journal
- **AND** the migration receipt records cutoff time, row counts, input
  checksums, replay status, and stale `download_source_cycle` supersession
  evidence.

#### Scenario: Malformed file state fails closed

- **WHEN** a DB-free scheduler read sees malformed JSON, unsupported schema, or
  source/cycle identity mismatch in file journal state
- **THEN** duplicate-prevention reads fail closed as active/blocking evidence
- **AND** malformed state is not treated as an absent row.

#### Scenario: File journal discovery and JSON parsing are bounded

- **WHEN** the file journal discovers JSON/JSONL surfaces or decodes JSON
  payloads
- **THEN** total discovered files, recursion depth, byte count, record count,
  JSON node count, and JSON depth are bounded
- **AND** symlinked/non-regular scanned entries and unsafe path segments fail
  closed with stable file-journal reasons.

#### Scenario: Candidate-state ordering matches DB tie-breaks before limits

- **WHEN** file-backed rows are materialized into candidate state with job or
  event limits
- **THEN** rows are pre-limited using the same DB ordering as
  `PsycopgOrchestratorRepository`, including `job_id DESC` for jobs and
  `event_id DESC` for events when timestamps tie
- **AND** file input order cannot decide equal-timestamp candidate state.

#### Scenario: Blocked query evidence is public-safe

- **WHEN** a query helper blocks on malformed or unsafe file-journal state
- **THEN** returned sentinel evidence redacts raw `job_id`, `idempotency_key`,
  `cycle_id`, `run_id`, and `slurm_job_id` values that look like local paths,
  `file://`, `s3://`, or `published://` URIs.

### Requirement: File journal is contract-tested against DB semantics

The system SHALL include repository contract tests that verify file-backed
orchestration state behavior against existing scheduler semantics.

#### Scenario: Contract fixtures cover critical repository methods

- **WHEN** repository contract tests run
- **THEN** fixtures cover active orchestration, active pipeline, completed
  pipeline, active Slurm jobs, candidate state, model/forcing context reads,
  lifecycle writes, reservation/bind, job status updates, event insertion,
  retry supersession, permanent failure guards, historical migration, and query
  helpers
- **AND** DB-backed repository semantics remain covered by existing
  `PsycopgOrchestratorRepository` tests.

#### Scenario: Read-side contract fixtures cover scheduler planning

- **WHEN** #833 focused tests run without `DATABASE_URL`
- **THEN** they prove file-backed active/completed/candidate/active-Slurm
  decisions are visible to scheduler planning
- **AND** DB-backed active/orchestrator repository factories are not called in
  DB-free read-side construction.
