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
- **AND** read-modify-write appends, event-id allocation, reservation duplicate
  checks, direct snapshot materialization, and latest materialization are
  linearized by a durable per-cycle file lock
- **AND** direct pipeline-job snapshots are materialized only after the
  append-only journal truth is committed, so append failure cannot leave a
  direct-only reservation blocker
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
- **AND** manual repair markers that unblock scheduler retry decisions require
  the same `pipeline.retry_run` policy evidence as manual retry execution
- **AND** manual retry submission preserves DB-compatible source selection,
  terminal-success guards, active-retry conflict guards, download-source-cycle
  manifest fields, runtime-root evidence, and hydro-run reset-to-pending
  semantics.

#### Scenario: Historical scheduler state migrates into append-only journal

- **WHEN** operators export scheduler-relevant rows from historical
  do-not-connect node-22 PostgreSQL `:55433` for the archived/stopped rollback
  state
- **THEN** the importer writes active/completed/candidate/job/event/retry and
  permanent-failure rows into the file journal
- **AND** migrated pipeline events preserve historical `event_id` and
  `created_at` ordering and repeated imports do not duplicate visible replay
  events
- **AND** the migration receipt records cutoff time, row counts, input
  checksums, replay status, and stale `download_source_cycle` supersession
  evidence
- **AND** receipt files are written with no-follow atomic writes under the
  configured journal/evidence root.

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

### Requirement: Accepted forecast cohort submission is reconciled exactly once

The system SHALL preserve and recover a DB-free forecast cohort across the window where
Slurm accepted an array but the Gateway response did not durably return, without
creating, adopting, or cancelling an array whose exact identity is unproven.

Persisted and emitted reconciliation evidence MUST use `submit_outcome` in
`accepted|submit_result_ambiguous|rejected`,
`reconciliation_source=slurm_exact_comment`, and `reconciliation_decision` in
`matched_bound|absence_deferred|absence_retry_permitted|multiple_matches_blocked|identity_mismatch_blocked|accounting_unavailable`.
`matched_slurm_job_id` MUST remain null until an exact unique identity is
proven. Candidate projection MUST use `array_task_id`, `array_task_outcome` in
`succeeded|failed|unverified`, `restart_stage`, and
`native_shud_resubmitted`.

#### Scenario: Forecast cohort reservation precedes the Gateway call

- **WHEN** scheduler submits a source/cycle/restart-stage forecast cohort
- **THEN** the file journal durably records its deterministic cohort identity,
  ordered candidate/task member map, idempotency key, and exact Slurm comment
  before the Gateway call
- **AND** a Gateway response timeout records an ambiguous non-terminal submit
  result rather than a permanent hydro or candidate failure.

#### Scenario: Unique exact-comment match binds the accepted array

- **WHEN** a later pass or process restart reconciles a reserved-unbound cohort
  and authoritative accounting returns exactly one array with the exact comment
  and matching source/cycle/stage/cohort identity
- **THEN** the file journal binds that array master job ID and continues task
  status reconciliation
- **AND** scheduler neither submits nor cancels another forecast array.

#### Scenario: Confirmed absence permits one bounded idempotent retry

- **WHEN** authoritative exact-comment accounting returns zero matches before
  the configured reconciliation window expires
- **THEN** the cohort remains in a bounded reconciling state and is not
  resubmitted
- **AND WHEN** authoritative zero-match evidence persists after the window
- **THEN** the file journal permits exactly one idempotent submission attempt,
  including under concurrent scheduler passes.

#### Scenario: Ambiguous or unavailable accounting fails closed

- **WHEN** exact-comment accounting returns multiple matches, a mismatched
  comment/cohort/stage/member identity, or an unavailable/non-authoritative
  result
- **THEN** scheduler does not bind, cancel, or submit another array
- **AND** bounded evidence distinguishes multiple, mismatch, and unavailable
  decisions so a later pass or operator can reconcile safely.

#### Scenario: Accounting discovery and evidence are bounded and redacted

- **WHEN** exact-comment discovery exceeds its configured match/row limit or
  returns fields containing runtime roots, credentials, or raw unbounded rows
- **THEN** scheduler records `multiple_matches_blocked` with bounded count/class
  evidence and no matched Slurm identity
- **AND** public evidence omits raw comments, credentials, local/shared-NFS
  roots, and unbounded accounting payloads.

#### Scenario: Terminal array tasks project to exact candidates

- **WHEN** an adopted array has authoritative terminal task results
- **THEN** each result is projected only to its exact reserved candidate/task
  identity
- **AND** successful forecast tasks clear their own stale
  `SLURM_GATEWAY_UNAVAILABLE` hydro failure and resume at `state_save_qc`
- **AND** failed or unverified tasks remain failed or reconciling without
  relabelling/recomputing successful siblings.

#### Scenario: Recovered success preserves run and QC provenance

- **WHEN** a terminal successful task clears a stale transport failure
- **THEN** its source/cycle/model, initial-state, output/checkpoint,
  run-manifest, and QC lineage remain attached to the same candidate/run
- **AND** reconcile does not synthesize a replacement forecast run.

#### Scenario: Existing reconcile callers remain compatible

- **WHEN** generic repository or non-DB-free reconcile processes a legacy row
  without the additive cohort/member fields
- **THEN** its existing status and identity contract remains valid
- **AND** DB-free-only cohort fields are not made mandatory for that caller.

#### Scenario: Non-forecast array stages retain their prior contract

- **WHEN** a non-forecast array stage receives a Gateway failure or a legacy
  row contains cohort-like member fields
- **THEN** #1112 does not project that stage as forecast success or attach
  `restart_stage=state_save_qc`
- **AND** the stage retains its pre-#1112 failure/retry behavior.

### Requirement: Candidate restart stage survives cohort dispatch

The system SHALL preserve each candidate's earliest incomplete canonical stage
through restart-compatible grouping, durable cohort identity, run-context
construction, and stage execution.

#### Scenario: Downstream restart never repeats forecast

- **WHEN** a recovered candidate has `restart_stage=state_save_qc`
- **THEN** its execution cohort starts at `state_save_qc`
- **AND** `run_shud_forecast_array` is not submitted
- **AND** evidence records `native_shud_resubmitted=false`.

#### Scenario: Mixed restart stages form distinct cohorts

- **WHEN** selected candidates for one source/cycle include both `forecast` and
  `state_save_qc` restart stages
- **THEN** scheduler creates distinct deterministic cohort identities and
  dispatches each from its own earliest incomplete stage
- **AND** it does not lower the downstream cohort to `forecast`.
