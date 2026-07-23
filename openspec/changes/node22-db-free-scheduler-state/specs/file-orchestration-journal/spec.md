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
- **AND** scheduler-pass lease configuration and journal-transaction guard
  configuration have distinct, unambiguous semantics; a mode that omits
  `flock` for the lease MUST NOT silently become `flock` for the journal
- **AND** DB-free startup fails closed when the configured shared filesystem has
  no supported cross-process journal transaction guard
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

Before invoking the Gateway, every reservation write MUST use strict durable
replacement semantics: file and parent-directory durability plus parent
identity verification are mandatory, and an indeterminate result MUST fail
closed rather than report a usable reservation.

Persisted and emitted reconciliation evidence MUST use `submit_outcome` in
`accepted|submit_result_ambiguous|rejected`,
`reconciliation_source=slurm_exact_comment`, and `reconciliation_decision` in
`matched_bound|absence_deferred|absence_retry_permitted|multiple_matches_blocked|identity_mismatch_blocked|accounting_unavailable`.
`matched_slurm_job_id` MUST remain null until an exact unique identity is
proven. Candidate projection MUST use `array_task_id`, `array_task_outcome` in
`succeeded|failed|unverified`, `restart_stage`, and
`native_shud_resubmitted`.

A reservation MAY temporarily omit `submit_outcome` only between its durable
pre-submit write and durable Gateway-result classification. Restart recovery
MUST atomically classify that state as `submit_result_ambiguous` before it
persists any reconciliation decision. Task-accounting completeness MUST use
pipeline status/error/projection fields and MUST NOT add values to the closed
`reconciliation_decision` enum above.

Reclaiming a reservation for a new submission attempt MUST atomically increment
the attempt and clear `submit_outcome`, `reconciliation_source`,
`reconciliation_decision`, and `matched_slurm_job_id` before the next Gateway
call. Reopening the journal in that pre-Gateway window MUST NOT expose evidence
from the prior attempt.

Every submit/reconciliation transition MUST compare the durable submission
attempt and expected state under the same cycle lock. Gateway success MUST
atomically bind its Slurm ID and `accepted` outcome; accounting adoption MUST
atomically bind the ID and `matched_bound` tuple. Repeating the same ID is
idempotent, while a stale transition or different-ID collision MUST NOT mutate
the winning row. Every versioned master MUST carry a valid aware-UTC immutable
`submission_attempt_started_at`; a retry reclaim MUST create its next anchor
under the cycle lock, and retry permission MUST compare the expected attempt
number and anchor. Persisted current-version master classification and authority
identity MUST be sticky: an ordinary upsert MUST NOT downgrade it to a
non-master/candidate or mutate any master identity field, and only typed reclaim
MAY advance attempt and anchor together. Ordinary upsert also MUST NOT change a
versioned master's Slurm binding, status, outcome, reconciliation tuple/reason,
projection, runtime/retry/error/log state; an exact replay MUST perform no
authority write. Each legal master transition MUST use its typed commit,
reconciliation, rejection, retry-permission, reclaim, or projection boundary.
Generic status and reconciliation compatibility APIs MUST fail before writing
when they would change any current-version master authority field, including a
retryable status or absence permission. Reclaim MUST independently require the
complete current-attempt typed absence proof and immutable attempt anchor; a
generic compatibility write cannot manufacture retry authority.
The same restriction applies to generic reserve, bind, and unmarked submit
transition APIs: a current-version reservation MUST start clean and unbound,
accepted binding MUST use the attempt-aware commit boundary, and
`absence_retry_permitted` MUST be produced only by the authoritative typed
retry-permission boundary. Marker-free rows retain their legacy API behavior.

A Gateway timeout MUST persist only the ambiguous submit outcome and MUST leave
`reconciliation_source`, `reconciliation_decision`, and
`matched_slurm_job_id` null until accounting has actually been queried. The
closed `submit_outcome` enum applies to both cohort-master and candidate-task
rows.

Only a typed, proven pre-acceptance rejection MAY persist `rejected`. Once a
submit request may have reached `sbatch`, any transport error, parse failure,
malformed success response, or unclassified Gateway failure MUST persist
`submit_result_ambiguous` and reconcile by exact comment. A proven rejection
MUST atomically terminalize the master and all matching-attempt hydro members;
partial member terminalization MUST NOT be observable after reopen.

Every repeated typed transition with the same normalized current-attempt state
and evidence MUST be a zero-write replay: it MUST NOT append the cycle journal,
rewrite direct state, or advance `updated_at`. A real typed evidence change MUST
still append exactly once under the cycle lock.

New accepted-submit master and candidate rows MUST carry an explicit contract
version. Marker-free historical cohort-shaped rows MUST retain the legacy
read/replay contract and MUST NOT be rejected for missing accepted-submit
fields. Global accounting-visibility proof MUST be applied only to versioned
accepted-submit cohorts, not to generic or non-DB-free reconciliation.

#### Scenario: Forecast cohort reservation precedes the Gateway call

- **WHEN** scheduler submits a source/cycle/restart-stage forecast cohort
- **THEN** the file journal durably records its deterministic cohort identity,
  ordered candidate/task member map, idempotency key, and exact Slurm comment
  before the Gateway call
- **AND** a real file-journal test reopens and observes that reservation inside
  the fake Gateway boundary, before the external side effect
- **AND** a Gateway response timeout records an ambiguous non-terminal submit
  result rather than a permanent hydro or candidate failure.

#### Scenario: Pre-outcome interruption and explicit rejection remain recoverable

- **WHEN** a process restarts after the reservation write but before the Gateway
  result was durably classified
- **THEN** reconciliation first records `submit_result_ambiguous` and continues
  exact-comment recovery, whether runtime member rows already exist or not
- **AND** an exact-comment match without independently persisted runtime member
  rows remains `identity_mismatch_blocked` rather than being bound
- **AND WHEN** the Gateway explicitly rejects a forecast submission
- **THEN** the journal accepts `submit_outcome=rejected`, terminalizes affected
  hydro rows, and returns the ordinary submission-failure result without a
  secondary evidence-validation failure.

#### Scenario: Retry reclaim starts a clean attempt

- **WHEN** authoritative accounting permits retry of an ambiguous attempt and
  the scheduler reclaims its reservation
- **THEN** the incremented attempt is durably `reserved` with no submit outcome
  or accounting tuple before the next Gateway call
- **AND** an immediate process restart reads the same clean pre-outcome state,
  without inheriting the prior attempt's absence proof.

#### Scenario: Versioned master authority cannot escape validation

- **WHEN** an ordinary upsert attempts to change a persisted versioned master's
  stage/job type, model/task class, cohort/digest, idempotency/comment,
  ownership, restart/native-SHUD, attempt, or anchor identity
- **THEN** the first illegal mutation fails closed and the original durable
  identity remains unchanged after reopen
- **AND** a multi-step master-to-non-master-to-master classification detour
  cannot bypass the same invariant
- **AND** valid candidate rows and marker-free generic/legacy rows retain their
  documented compatibility behavior.

#### Scenario: Generic upsert cannot forge a typed master transition

- **WHEN** an accepted, rejected, retryable, or terminal versioned master is
  presented to ordinary upsert with a changed Slurm ID, status, submit outcome,
  reconciliation proof/reason, projection, runtime timestamp, retry, error, or
  log field
- **THEN** the mutation fails before append/direct materialization and reopen
  preserves the original master exactly
- **AND** clearing a bound Slurm ID or writing
  `absence_retry_permitted` cannot make typed reclaim submit a second attempt
- **AND** an exact same-value ordinary replay appends no journal record, while
  the corresponding valid typed transition remains available.

#### Scenario: Unique exact-comment match binds the accepted array

- **WHEN** a later pass or process restart reconciles a reserved-unbound cohort
  and authoritative accounting returns exactly one array with the exact comment
  and matching source/cycle/stage/cohort identity
- **THEN** the file journal binds that array master job ID and continues task
  status reconciliation
- **AND** scheduler neither submits nor cancels another forecast array.

#### Scenario: Confirmed absence permits one bounded idempotent retry

- **WHEN** authoritative exact-comment accounting returns zero matches from a
  frozen query window that covers the current attempt anchor through query end,
  before the configured reconciliation window expires
- **THEN** the cohort remains in a bounded reconciling state and is not
  resubmitted
- **AND WHEN** authoritative zero-match evidence persists after the window
- **THEN** the file journal permits exactly one idempotent submission attempt,
  including under concurrent scheduler passes.
- **AND WHEN** the current attempt predates the bounded accounting lookback, or
  an adapter cannot prove full attempt coverage
- **THEN** zero matches remain `accounting_unavailable` with bounded
  `coverage_incomplete` evidence and MUST NOT permit retry.
- **AND** reconcile MUST recompute coverage from valid aware
  `coverage_start`/`coverage_end` bounds and the durable current-attempt anchor;
  a completeness flag with missing, reversed, malformed, naive, or non-covering
  bounds MUST NOT authorize absence.
- **AND** an independently proven exact identity match MAY bind even when
  absence coverage is incomplete.

#### Scenario: Ownership mismatch is not authoritative absence

- **WHEN** bounded exact-comment discovery finds a job owned by a different
  Slurm user or account
- **THEN** reconciliation records `identity_mismatch_blocked` and does not bind,
  cancel, or submit another array
- **AND** an owner-scoped zero result alone cannot become
  `absence_retry_permitted`; retry requires bounded global zero-match proof.

#### Scenario: Global accounting authority is proven and scale-bounded

- **WHEN** scheduler preflight cannot prove that its principal sees jobs across
  all users/accounts
- **THEN** a successful empty `sacct --allusers` result is non-authoritative and
  cannot permit retry
- **AND WHEN** exact-comment discovery runs at the supported 256-member cadence
- **THEN** it queries bounded time pages, counts an unterminated final row, and
  aggregates at most the bounded zero/one/multiple proof without silently
  discarding a row at the byte or row limit
- **AND** all cohort queries in that reconcile session use the same frozen page
  boundaries, so wall-clock advance cannot invalidate the page cache or starve
  later GFS/IFS cohorts under the shared deadline.

#### Scenario: Ambiguous or unavailable accounting fails closed

- **WHEN** exact-comment accounting returns multiple matches, a mismatched
  comment/cohort/stage/member identity, or an unavailable/non-authoritative
  result
- **THEN** scheduler does not bind, cancel, or submit another array
- **AND** bounded evidence distinguishes multiple, mismatch, and unavailable
  decisions so a later pass or operator can reconcile safely.

#### Scenario: Accounting discovery and evidence are bounded and redacted

- **WHEN** indexed exact-comment discovery proves multiple matches
- **THEN** scheduler records `multiple_matches_blocked` with bounded count
  evidence and no matched Slurm identity
- **AND WHEN** a raw accounting page exceeds its configured byte/row limit
- **THEN** scheduler records `accounting_unavailable` with a closed bounded
  saturation reason class and MUST NOT bind, cancel, or permit retry
- **AND** public evidence omits raw comments, credentials, local/shared-NFS
  roots, and unbounded accounting payloads.

#### Scenario: Inflight task accounting is bounded before materialization

- **WHEN** master/task accounting exceeds its byte, row, or time limit
- **THEN** reconciliation treats accounting as unavailable/incomplete and keeps
  the cohort fail closed
- **AND** the control process does not capture or split an unbounded output.
- **AND** executable process-boundary tests cover byte, row, and wall-time
  limits, including termination and reap behavior.

#### Scenario: Terminal array tasks project to exact candidates

- **WHEN** an adopted array has authoritative terminal task results
- **THEN** each result is projected only to its exact reserved candidate/task
  identity
- **AND** the physical Slurm task ID suffix, reported task ID, canonical member
  order, and reserved member identity all agree before any terminal projection
- **AND** malformed, swapped, duplicate, or incomplete task identity remains
  fail-closed without candidate or hydro mutation
- **AND** foreground polling, immediate-terminal submit responses, and restart
  reconciliation use the same typed cycle-lock transaction to persist terminal
  master state, task projections, candidate/hydro rows, and events together
- **AND** restart discovery includes terminal current-version masters only when
  their terminal projection is incomplete, while replay of a complete
  projection is zero-write and cannot move an already progressed hydro backward
- **AND** successful forecast tasks clear their own stale
  `SLURM_GATEWAY_UNAVAILABLE` hydro failure and resume at `state_save_qc`
- **AND** failed or unverified tasks remain failed or reconciling without
  relabelling/recomputing successful siblings.

#### Scenario: Scheduler restart evidence proves the recovery result

- **WHEN** reserved or inflight restart reconciliation changes a cohort
- **THEN** public scheduler evidence includes the bounded submit/accounting
  tuple, matched identity, cohort restart stage, native-SHUD resubmission flag,
  and bounded candidate/task outcomes
- **AND** it excludes raw comments, credentials, runtime roots, and raw
  accounting rows.

#### Scenario: Accepted-submit storage remains bounded over operational history

- **WHEN** two daily forecast cycles each project one GFS and one IFS cohort
  (four cohort masters per day) with up to 256 terminal members over long-term
  history
- **THEN** cycle reads and restart discovery do not enumerate every historical
  member direct file or approach a global 100,000-file discovery limit
- **AND** a durable active-reconcile index is atomically maintained by every
  typed current-version master transition, so restart discovery cost and public
  outcome materialization are bounded independently of terminal history size
- **AND** a new active discovery anchor is durable before its journal side
  effect; an orphan pre-journal anchor is removed by canonical replay, while a
  journaled active row remains discoverable across a crash before direct/index
  materialization and arbitrary later history
- **AND** terminal journal truth commits before its discovery anchor is removed,
  and stale terminal anchors are repaired on reopen
- **AND** an idempotent, crash-resumable one-time backfill inventories existing
  current-version and marker-free active rows under a durable completion marker;
  steady-state restart queries do not recursively enumerate terminal master or
  candidate history
- **AND** the oldest active cohort remains discoverable after reopen while a
  terminal cohort is removed from the active index only in the same durable
  transition that makes it ineligible for reconciliation
- **AND** canonical journal evidence remains auditable under the documented
  partition/index and retention contract.
- **AND** versioned master reserve, accepted bind, rejection, retry permission,
  and accounting adoption resolve their deterministic job only from exact
  direct plus the corresponding cycle journal, so unrelated malformed or
  over-limit history cannot block the current mutation.

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

#### Scenario: Accepted-submit retry and cycle control remain typed

- **WHEN** a current-version forecast cohort has whole or partial terminal task
  failure, status synchronization, or a cancellation request
- **THEN** it does not create a marker-free retry clone or use a generic master
  status mutation
- **AND** retry stays within the attempt-aware accepted-submit lifecycle
- **AND** non-terminal synchronization uses the typed runtime transition while
  terminal truth is finalized only by exact task accounting/projection
- **AND** cancellation intent is durable before the external cancel call, so a
  process or Gateway failure remains recoverable on reopen
- **AND** proven rejection without a real Slurm master ID is not retained as
  terminal task-projection work in the active-reconcile inventory.

#### Scenario: Accounting visibility probes are process-bounded

- **WHEN** scheduler probes controller/accounting visibility with local Slurm
  commands
- **THEN** stdout, stderr, rows, wall time, termination, and process reap are
  bounded before output materialization
- **AND** saturation or timeout leaves accounting authority unproven and cannot
  release retry permission.

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
