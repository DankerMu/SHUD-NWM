# pipeline-job-persistence Specification

## Purpose
TBD - created by archiving change m3-slurm-nationalization. Update Purpose after archive.
## Requirements
### Requirement: pipeline_job Record Creation

The system SHALL create a `pipeline_job` record in the `ops.pipeline_job` table whenever the Orchestrator submits a stage job to Slurm.

#### Scenario: Orchestrator submits a single-basin stage job

- **WHEN** the Orchestrator submits a stage job (e.g., `run_shud_forecast_array`) for a single basin via `sbatch`
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

#### Scenario: Orchestrator submits a cycle-level stage job (no per-basin scope)

- **WHEN** the Orchestrator submits a cycle-level stage job (e.g., `convert_canonical`, `publish_tiles`) that is not scoped to a specific basin
- **THEN** a new `pipeline_job` row SHALL be inserted with `run_id` set to NULL and `model_id` set to NULL; all other fields populated as above

#### Scenario: sbatch submission fails

- **WHEN** `sbatch` returns a non-zero exit code during submission
- **THEN** a `pipeline_job` record SHALL still be created with `status` set to `submission_failed`, `slurm_job_id` set to NULL, and `error_message` populated with the sbatch stderr output

---

### Requirement: pipeline_job Schema and Fields

The `ops.pipeline_job` table MUST match the upstream schema (`docs/appendices/C_database_schema_draft.md` §5) with `model_id` as an M3 addition.

#### Scenario: Table schema validation

- **WHEN** the `ops.pipeline_job` table is created or migrated
- **THEN** the table MUST contain exactly these columns:
  - `job_id` — TEXT, PRIMARY KEY
  - `job_type` — TEXT, NOT NULL
  - `slurm_job_id` — TEXT, NULLABLE (NULL when submission fails)
  - `cycle_id` — TEXT, NULLABLE (no FK constraint — upstream has no FK)
  - `stage` — TEXT, NULLABLE
  - `run_id` — TEXT, NULLABLE (NULL for cycle-level stages)
  - `model_id` — TEXT, NULLABLE (M3 addition, NULL for cycle-level stages)
  - `status` — TEXT, NOT NULL, DEFAULT `pending`
  - `submitted_at` — TIMESTAMPTZ, NULLABLE (NULL until job is submitted to Slurm)
  - `started_at` — TIMESTAMPTZ, NULLABLE
  - `finished_at` — TIMESTAMPTZ, NULLABLE
  - `exit_code` — INTEGER, NULLABLE
  - `error_code` — TEXT, NULLABLE
  - `error_message` — TEXT, NULLABLE
  - `log_uri` — TEXT, NULLABLE
  - `retry_count` — INTEGER, NOT NULL, DEFAULT 0
  - `created_at` — TIMESTAMPTZ, NOT NULL, DEFAULT NOW()
  - `updated_at` — TIMESTAMPTZ, NOT NULL, DEFAULT NOW()

---

### Requirement: Status Synchronization via sacct

The Orchestrator SHALL update the `pipeline_job` status when `sacct` returns a new status for the corresponding Slurm job.

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

---

### Requirement: pipeline_event Append-Only Event Log

The system SHALL maintain an append-only `ops.pipeline_event` table that records every status transition for a `pipeline_job`, matching upstream schema.

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

---

### Requirement: Database Indexes

The `ops.pipeline_job` and `ops.pipeline_event` tables MUST have indexes matching upstream schema to support efficient query patterns.

#### Scenario: Index definitions

- **WHEN** the `ops.pipeline_job` table is created or migrated
- **THEN** the following indexes MUST exist:
  - `pipeline_job_run_idx` on `(run_id)` — supports run-to-job lookup
  - `pipeline_job_cycle_idx` on `(cycle_id)` — supports cycle-to-job lookup

- **WHEN** the `ops.pipeline_event` table is created or migrated
- **THEN** the following index MUST exist:
  - `pipeline_event_entity_idx` on `(entity_type, entity_id, created_at DESC)` — supports entity event history queries

---

### Requirement: Bidirectional Queryability

The system SHALL support bidirectional lookup between Slurm job IDs and internal run IDs.

#### Scenario: Query by slurm_job_id

- **WHEN** a user or system queries `ops.pipeline_job` by `slurm_job_id`
- **THEN** the query SHALL return the corresponding `job_id`, `run_id`, `cycle_id`, `stage`, and `status`

#### Scenario: Query by run_id

- **WHEN** a user or system queries `ops.pipeline_job` by `run_id`
- **THEN** the query SHALL return all `pipeline_job` records for that run across all stages, ordered by `submitted_at ASC`

### Requirement: Per-cycle journal event logs SHALL rotate into bounded segments and journal capacity faults SHALL NOT fail-close unrelated scheduler work

When appending a record (or record batch) to a per-cycle journal event log would exceed the configured per-file byte limit, the journal SHALL roll the write over to a new continuation segment of the same cycle instead of failing, keeping every segment within the limit; readers SHALL replay all segments of a cycle in segment order with a globally monotonic replay order equivalent to a single concatenated log. A record or batch that exceeds the byte limit by itself SHALL still fail exactly as before. During restart reconciliation, a journal capacity or integrity error raised while resolving one reserved-unbound row SHALL quarantine that row with recorded evidence (reason and offending file) and SHALL NOT abort resolution of the remaining rows or the scheduler pass.

#### Scenario: Append near the byte limit rolls over to a continuation segment

- **WHEN** a cycle's newest journal segment cannot fit the next event
  line within the per-file byte limit
- **THEN** the line is written to a new continuation segment of the same
  cycle, both segments remain within the limit, and a subsequent replay
  of the cycle yields the same rows and ordering as if all lines lived
  in one file

#### Scenario: Single-segment cycles read byte-identically to today

- **WHEN** a cycle's event log never overflowed
- **THEN** reads, replay order, and computed event ids are identical to
  the pre-rotation behavior

#### Scenario: Oversized single record still fails closed

- **WHEN** one record (or one batch) alone exceeds the per-file byte
  limit
- **THEN** the append fails with the existing byte-limit error and no
  partial content is written

#### Scenario: Reconcile quarantines a poisoned cycle instead of aborting the pass

- **WHEN** restart reconciliation hits a journal error while resolving
  one reserved-unbound row
- **THEN** that row is recorded as quarantined in the reconcile evidence
  with the error reason and offending file, the remaining reserved rows
  are still resolved, and the scheduler pass proceeds past restart
  reconcile

#### Scenario: Journal error evidence names the offending file

- **WHEN** a journal byte-limit or integrity error surfaces in
  restart-reconcile evidence
- **THEN** the evidence message includes the redacted offending file
  reference, not only the bare reason string

#### Scenario: Existing enumeration readers tolerate continuation segments

- **WHEN** a cycle has continuation segments and a journal-wide
  enumeration reader (pipeline-job queries by cycle/run/slurm-id,
  rollback-scope iteration, reconcile-inventory backfill, cycle source
  discovery) walks the journal surface
- **THEN** continuation segments resolve to their base cycle — no
  invalid-cycle-time error and no silently skipped segment records —
  replay and inventory backfill arbitrate records in segment order
  (never lexicographic path order), genuinely unparseable file names
  keep today's behavior, and an orphan (gapped) segment WITHIN the
  bounded probe window (indices up to the segment cap) fails closed
  with the same answer from every reader; an orphan BEYOND the probe
  window — unreachable by the writer, producible only by external
  corruption — still fails closed in the enumeration walkers and
  inventory backfill, while the cycle-level exact-path reader cannot
  observe it (no-globbing hot-path constraint), an asymmetry that is
  pinned by test and documented where operators diagnose it

#### Scenario: Latest-view precedence survives segmented replay order

- **WHEN** a latest-view row and a continuation-segment journal record
  tie on the same sequence
- **THEN** the latest view still wins, exactly as it does for
  single-segment cycles

#### Scenario: Segments per cycle are bounded

- **WHEN** a cycle has reached the configured maximum number of
  segments and another rollover would be required
- **THEN** the append fails closed with a distinct
  segment-limit-exceeded reason naming the cycle file, bounding a
  cycle's total journal capacity and keeping segment exhaustion
  distinguishable from an oversized single record

### Requirement: Reserved-unbound identity-mismatch outcomes SHALL converge instead of wedging the pipeline

The journal SHALL persist, on each versioned accepted-submit master row, a consecutive-outcome counter that increments each time restart reconciliation records an `identity_mismatch_blocked` outcome for that reserved-unbound row, saturates once it reaches the configured limit (and does not increment while the exit is disabled), and resets to zero whenever the row's accounting state is replaced by any other transition — including a bind, an absence-path release, or the start of a new submission attempt after a reclaim. When the counter reaches the configured limit and the row is past the accepted-submit grace period — anchored to the submission attempt start time, never to a timestamp refreshed by the counter's own writes — reconciliation SHALL migrate the row out of `reserved` into `reservation_lost` through a dedicated compare-and-swap journal transition (expected attempt, attempt anchor, expected `reserved` status, unbound required) recording the typed decision `identity_mismatch_released` and preserving the counter's final value. The released row is a deliberately non-reclaimable terminal: its idempotency key SHALL NOT be revivable through reservation reclaim; liveness is preserved because, when the retry budget still allows, new attempts mint new retry-suffixed keys. A disabled or non-positive limit SHALL preserve today's behavior (no release). The closed master-status vocabulary SHALL NOT gain new members for this exit, and the generic evidence-transition API's decision whitelist SHALL NOT be widened.

#### Scenario: Consecutive identity-mismatch outcomes release the reservation

- **WHEN** a reserved-unbound row records `identity_mismatch_blocked` on N consecutive reconcile passes, N reaches the configured limit, and the row is past the accepted-submit grace
- **THEN** the row transitions `reserved` → `reservation_lost` with reconciliation decision `identity_mismatch_released`, the counter's final value is preserved on the row, and subsequent passes no longer surface the row as reserved-unbound — unwedging cycle-level orchestration that previously failed with `PIPELINE_ALREADY_ACTIVE`

#### Scenario: A non-blocked outcome resets the streak

- **WHEN** a reserved-unbound row records `identity_mismatch_blocked` outcomes followed by any different reconcile outcome before the limit is reached
- **THEN** the counter resets to zero and the release exit does not trigger until a fresh consecutive run reaches the limit

#### Scenario: A reclaimed reservation starts a fresh streak

- **WHEN** a row accumulates blocked outcomes, exits through the absence path, is reclaimed into a new submission attempt, and then records its first `identity_mismatch_blocked` outcome
- **THEN** the counter has restarted from zero — the stale pre-reclaim streak does not make the first post-reclaim blocked outcome trigger the release

#### Scenario: Guards hold the release closed

- **WHEN** the counter reaches the limit but the row is within the accepted-submit grace, or the limit is disabled (unset, zero, or negative), or the release compare-and-swap fails because the row's attempt state moved concurrently
- **THEN** no status migration occurs and the pass records the ordinary `identity_mismatch_blocked` outcome

#### Scenario: The streak and release invariants are test-anchored

- **WHEN** the invariant guards for this counter and this decision are exercised — a negative or
  non-integer streak, a pre-outcome transition carrying a non-zero streak, an
  `identity_mismatch_released` decision whose status is not `reservation_lost`, and a
  non-identity-mismatch decision carrying a non-zero streak
- **THEN** each guard rejects the transition with its typed error and leaves the journal row
  unchanged, and each guard has a negative test that fails when that guard alone is removed.

### Requirement: Accepted-submit cohort forecast terminal rows SHALL record init-state identity forward-only

The accepted-submit cohort forecast path SHALL persist the init-state identity (`init_state_id`, `checksum`, `uri`, `valid_time`) **at reservation time**, where the planning context is available, as a per-model identity mapping keyed by `array_task_id`/`model_id` on the cohort master row, outside the cohort-digest input set; terminal per-model row construction SHALL read each row's identity from the master-row mapping by its own `array_task_id` rather than from cohort-member projection, and a scalar single-identity field SHALL NOT be used. The recording SHALL NOT alter the ordinary-upsert frozen-field semantics and SHALL NOT enter the cohort-digest member field set — historical rows' `forecast_cohort_digest` validation results SHALL be unchanged. The identity's value SHALL be stable from the **first** reservation onward: reclaiming a dead reservation into a new submission attempt SHALL NOT refresh the mapping, and derived per-model rows SHALL reject a divergent ordinary-upsert write to the mapping exactly as the master row does. That rejection SHALL apply only to writes that explicitly carry the mapping — an ordinary upsert that omits the field SHALL continue to keep the persisted value silently, as it does today. The keep-first reclaim boundary SHALL be re-adjudicated if this mapping ever becomes an input to completion verdicts: while it stays invisible to them, a stale first-attempt mapping can only make a reader refuse to skip work, never permit a wrong skip. Invalid or partial identity payloads SHALL be rejected by accepted-submit normalization. Existing journal rows without these fields SHALL remain readable unchanged — no migration, no backfill, no rewrite of historical rows.

#### Scenario: New cohort terminal rows carry the identity

- **WHEN** a cohort forecast job reserved after this change reaches a terminal status through the accepted-submit path
- **THEN** its journal row records the init-state identity captured at reservation time

#### Scenario: Historical cohort digests are untouched

- **WHEN** normalization validates a pre-change cohort row's `forecast_cohort_digest` after this change is deployed
- **THEN** the validation result is identical to before this change

#### Scenario: Legacy rows stay untouched and readable

- **WHEN** the journal contains pre-change cohort rows without init-state fields
- **THEN** readers treat the record as absent without error and no writer mutates those rows

#### Scenario: Invalid identity is rejected by the invariant gates

- **WHEN** an upsert presents an init-state identity payload with a malformed or partial field set
- **THEN** accepted-submit normalization rejects the transition rather than persisting a partial record

#### Scenario: An upsert that omits the mapping keeps the persisted value

- **WHEN** an ordinary upsert targets a derived per-model accepted-submit row without carrying an
  init-state identity mapping at all
- **THEN** the write succeeds and the persisted mapping is kept unchanged — the freeze SHALL NOT
  fail closed on the row-constructor's default empty value.

#### Scenario: Derived per-model rows freeze the mapping like the master row

- **WHEN** an ordinary upsert targets a derived per-model accepted-submit row and carries an
  init-state identity mapping that differs from the persisted one — including a public-view
  round-trip whose object URI has been replaced by a display placeholder, an explicitly empty
  mapping, and a structurally valid mapping with different content
- **THEN** the write is rejected with an evidence-invariant error and the durable journal payload
  retains the value captured at reservation time.

#### Scenario: A reclaimed reservation keeps the first attempt's mapping

- **WHEN** a dead reservation is reclaimed into a new submission attempt and the reclaim request
  row carries a freshly recomputed init-state identity mapping that differs from the persisted one
- **THEN** the persisted mapping remains the first attempt's value, the reclaim still succeeds with
  its submission attempt incremented and its attempt anchor restamped, and terminal per-model rows
  projected afterwards carry that same first-attempt mapping.

#### Scenario: A public-view snapshot is not a valid write payload

- **WHEN** a caller replays an unmodified public-view snapshot of an accepted-submit master row
  back through the ordinary upsert path, where the public view has replaced object URIs with
  display placeholders
- **THEN** the write is rejected rather than laundering the placeholder into durable state.

### Requirement: Reconcile sacct scan windows are rendered in the host's local wall clock

The restart-reconciliation comment scan SHALL render its sacct page
boundaries in the host's local wall-clock representation of the
UTC-computed instants, because sacct interprets bare timestamps in the
host's local timezone: page arithmetic stays on the monotonic UTC axis,
and only the rendered `--starttime`/`--endtime` strings are converted, so
the scanned interval equals the intended interval on every host timezone
instead of being silently translated by the host offset (which on an
east-of-UTC host shifted the whole window into the past and made every
absence verdict for a job younger than the offset vacuous). The
per-session page freeze and the page-cache identity keyed by the rendered
strings keep their existing semantics, and on a UTC host the rendered
strings are byte-for-byte what they were before. On DST-observing hosts
the once-yearly fall-back hour renders an ambiguous local timestamp that
sacct's timezone-less interface may resolve up to an hour off, so the
coverage-complete gate can over-claim its scanned interval by at most one
hour once a year (inherent to sacct, not to this conversion;
spring-forward is safe because a UTC-to-local conversion never emits a
skipped wall clock, and adjacent page boundaries sit twelve hours apart
so rendered page keys can never collide).

#### Scenario: an east-of-UTC host scans the intended window

WHEN the reconcile comment scan runs on a host east of UTC
THEN the rendered page boundaries are the local wall-clock forms of the
UTC page instants, so a job submitted minutes ago falls inside the newest
page instead of beyond it

#### Scenario: a UTC host renders the same strings as before

WHEN the host timezone is UTC
THEN every rendered page boundary string is byte-for-byte identical to
the pre-change output

#### Scenario: page freeze and cache identity are unchanged

WHEN a querier session renders its pages under any host timezone that
stays stable for the session's duration
THEN the page set is frozen once per session and the page-cache keys
deduplicate exactly as before (a mid-session timezone change re-renders
the same frozen UTC page to a different key, costing only a redundant
sacct re-query — never a wrong verdict)

### Requirement: Comment-based absence proof requires proven comment accounting capability

The restart-reconciliation comment querier SHALL refuse to answer — raising
its transient query-unavailable error with reason class
`comment_accounting_unproven` before issuing any sacct command — unless a
once-per-querier-instance probe of `scontrol show config` proves that
`AccountingStoreFlags` includes the `job_comment` flag, because on a
cluster whose accounting never stores the sbatch comment a comment search
can never find a genuinely in-flight job, so treating its empty answer as
a confirmed absence falsely demotes live reservations to
`reservation_lost` and re-submits their cohorts. A probe that cannot run,
a missing `AccountingStoreFlags` line, and a `(null)` or
`job_comment`-less flag value all count as unproven (fail-closed toward
refusing, never toward trusting the search), with a warning that
distinguishes probe-execution failure from a cluster whose flags provably
lack `job_comment`. Refusal keeps the existing transient-deny semantics:
reserved rows stay reserved past the grace window and no absence
conclusion is recorded. This outcome class deliberately does not
converge on its own: it does not increment the identity-mismatch streak
counter (whose convergence requirement covers only the
`identity_mismatch_blocked` outcome family), adds no automatic release
exit, and leaves disposition to the documented runbook procedure (which
today may terminate in escalation rather than repair) — on
such clusters no reliable automatic absence proof exists, so any
automatic exit would trade duplicate submission against abandoning a
live job. On clusters where the probe proves the capability, sacct query
behavior is unchanged and the querier's raise-priority order is
preserved: the accepted-submit contract-version check still raises first,
and the global-visibility gate still applies to the queries it guarded
before.

#### Scenario: a cluster that does not store comments never confirms absence

WHEN the probe reads `AccountingStoreFlags = (null)` (or the flag list
lacks `job_comment`, or the line is absent, or scontrol fails)
THEN every comment query — owner-scoped, global, and legacy — raises the
transient query-unavailable error with reason class
`comment_accounting_unproven` without invoking sacct, and a reserved row
past its grace window stays reserved instead of being demoted to
`reservation_lost`

#### Scenario: a comment-storing cluster is unchanged

WHEN the probe proves `AccountingStoreFlags` includes `job_comment`
THEN owner-scope and global-scope comment queries page sacct exactly as
before, owned matches still bind, and a coverage-complete confirmed
absence older than the grace window still demotes to `reservation_lost`

#### Scenario: the probe runs once per querier instance

WHEN one querier instance serves multiple queries in a session
THEN the capability probe executes at most once and its verdict is
reused, and because the querier is rebuilt each reconcile pass a
transient scontrol failure denies only that pass — the next pass probes
again

### Requirement: Journal existence probes SHALL enforce filesystem containment before declaring absence

Every existence probe over the file orchestration journal tree (segment slot probes, sequence-floor file probes, and latest-directory probes) SHALL resolve the probed path with the same no-follow containment discipline as the hardened readers: a symlink in any parent component, or a symlink occupying the probed slot itself, SHALL fail loud as `file_journal_unreadable` instead of being reported as "absent" (or being silently skipped) — on every public surface, read or write, that reaches the probes, with exactly the exception type and fate that a hardened-reader fault (such as a corrupt journal file) already has on that same lane: a probe-detected containment fault is never softer than a reader fault and never introduces a new exception type at any public boundary. Genuine absence — the probed entry missing under a chain of real directories, including a wholly uninitialized journal tree — SHALL still be reported as absent, and failed writes SHALL leave zero bytes written. Known pre-existing limit, out of scope here and tracked as follow-up: a warm cycle-rows cache entry may keep serving a previously legal empty read after a tamper, because the pre-existing `_stat_signature` fingerprint cannot distinguish a real empty directory from a symlinked one; write surfaces still fail loud under a warm cache.

#### Scenario: Symlinked parent with missing cycle file fails loud on read

- **WHEN** `journal/<source>` (or any parent component) is a symlink whose
  target directory does not contain the requested cycle's segment files
- **THEN** the public cycle read raises `file_journal_unreadable` instead of
  returning an empty record list

#### Scenario: Sequence floor probe under a symlinked parent fails loud

- **WHEN** the next-sequence computation probes segment or latest paths whose
  parent component is a symlink
- **THEN** the enclosing public write method fails loud with
  `file_journal_unreadable` instead of silently skipping the slot and
  underestimating the sequence floor

#### Scenario: Symlink occupying a probed slot fails loud

- **WHEN** a segment slot or sequence-floor path is itself a symlink
- **THEN** the operation fails loud with `file_journal_unreadable` (the error
  may originate in the probe or the hardened reader — the reason token, not
  the origin or message, is the contract)

#### Scenario: A write that silently no-opped under a symlinked parent now fails loud

- **WHEN** a public journal write method whose empty probe result previously
  made it succeed as a silent no-op (for example marking a job permanently
  failed) runs against a symlinked journal parent
- **THEN** it fails loud with `file_journal_unreadable` instead of reporting
  success

#### Scenario: Probe faults are exactly as loud as reader faults on swallow lanes

- **WHEN** an internal lane that already absorbs hardened-reader journal
  errors (returning a partial or empty result) encounters a probe-detected
  containment fault
- **THEN** the observable result is identical to that lane's existing
  behavior for an unreadable journal file — the fault is neither swallowed
  earlier than a reader fault would be, nor does it introduce a new silent
  hole

#### Scenario: Genuine absence under real directories stays a legal empty read

- **WHEN** every path component up to the missing entry is a real directory,
  or the journal tree for the source is wholly uninitialized (cold start)
- **THEN** the cycle read returns an empty list and the next-sequence
  computation returns the base sequence, exactly as before

#### Scenario: Write surfaces fail in parity with reader faults, writing nothing

- **WHEN** an append or any other public journal write targets a path whose
  parent component is a symlink (the probes now detect the fault upstream of
  the actual write)
- **THEN** the write fails closed with `file_journal_unreadable`, carried by
  the same exception type a reader fault already surfaces on that lane, with
  zero bytes written — and pre-existing reader-raised errors on the write
  path keep their current propagation unchanged

### Requirement: Cohort terminal projection receives the master Slurm identity explicitly from its caller

The forecast-cohort terminal projection SHALL receive the master Slurm
identity as an explicit caller-supplied argument — the submit/poll leg
supplies the gateway job's Slurm id and the resume leg supplies the
pipeline row's recorded `slurm_job_id` — never by sniffing an id off
whatever dictionary shape the caller happens to hold, because the resume
leg's terminal dictionary is a pipeline row whose `job_id` is the
pipeline job id, and sniffing it fed the identity-mismatch guard a value
that can never equal the stored Slurm id: every already-terminal
resume-path projection was silently skipped (an idempotent no-op) or
mis-recorded as an identity-pollution event (the non-terminal resume
sub-path polls first and already produced the correct value — the poll
echoes the requested id back, so the sniffed value equalled the bound id;
the fix unifies that sub-path onto the same explicit bound argument with
identical results). An empty or non-numeric
supplied identity on the projection path SHALL fail closed with a
distinct, attributable error instead of degrading into an
unattributable failure. The identity-mismatch guard itself is
unchanged — it was correct and was being fed the wrong value.

#### Scenario: resume reconciles with the real Slurm identity

WHEN an already-terminal forecast-cohort pipeline row that still owes
its projection is resumed
THEN the projection receives the row's recorded Slurm id (not the
pipeline job id) and reconciles as matched-bound against the stored
identity — no identity-mismatch defer, no pollution event

#### Scenario: a settled master is never re-projected

WHEN a resumed forecast-cohort master is already terminal and its
recorded projections fully cover the cohort with terminal outcomes
THEN the resume pass does not re-enter the projection at all: the
durable row's outcome prevails over whatever this pass re-aggregates,
and the row's semantic fields stay byte-identical even when the fresh
aggregation disagrees with the stored one — and when the settled row has
no published log, the pass still publishes the log file to its
deterministic location (the row's log pointer stays unset until a typed
write for it exists)

#### Scenario: the submit leg is unchanged

WHEN the submit/poll leg records a cohort terminal outcome
THEN the projection receives the same gateway Slurm id as before the
change

#### Scenario: a missing identity fails loudly

WHEN the projection path is entered with an empty or non-numeric master
Slurm identity
THEN the call raises a distinct attributable error naming the pipeline
job and stage, rather than degrading into an unattributable downstream
failure

### Requirement: Journal read caches are safe under concurrent orchestration threads sharing one repository instance

The file journal repository's read-side caches SHALL tolerate concurrent
readers and writers on a single shared repository instance without
raising or corrupting cached values, because the production scheduler
hands one repository instance to every per-cohort orchestrator and fans
them out across a thread pool: cache population, lookup, and eviction
are mutually exclusive critical sections, and taking the cache mutex
never acquires the journal write mutex inside it (single lock order).
Single-threaded cache semantics are unchanged: identical keys observe
identical values and the eviction policy is untouched — only mutual
exclusion is added.

#### Scenario: concurrent cohorts hammer the shared caches

WHEN multiple orchestration threads concurrently read cycle rows and
journal files through one repository instance — including a concurrent
journal writer applying records — while cache eviction is continuously
triggered
THEN no thread observes a runtime iteration error or a torn cache
entry (a value not produced by any single store), and single-threaded
cache semantics are unchanged; cross-thread read freshness beyond this
is explicitly out of scope (the pre-existing ownership-blind
write-window staleness is declared, not fixed, by this change)

#### Scenario: single-threaded behavior is unchanged

WHEN the repository is used from a single thread
THEN cache hits, misses, and evictions behave byte-for-byte as before
the change

### Requirement: Display redaction placeholders SHALL NOT reach durable journal state through any write path

Object-URI display placeholders (`[object-uri]`, `[uri]`) are produced by public query projection so that callers never see raw object URIs, and a caller that round-trips a public row back into a write SHALL NOT launder those placeholders into durable state. The anti-laundering strip SHALL therefore be applied inside the single function through which journal records are constructed, rather than at individual write call sites, so that a write path added later inherits the guarantee instead of having to re-declare it. The strip SHALL act only on payloads supplied by a caller and SHALL NOT re-process the journal's own public rendering: the pipeline-event lane deliberately renders real paths and URIs into display placeholders as the durable public value, so that lane is stripped at its caller boundary — before the rendering — and the record constructor SHALL leave its already-rendered output intact. Consequently the event lane's structural guarantee is the weaker one, and an event write path that bypasses the caller boundary would carry no strip; that is a declared limit, not a covered case. A placeholder SHALL never be persisted as the literal placeholder text. Where the write path can decline — a guard that only overwrites when the caller supplied a value — a withheld value SHALL leave the persisted value untouched; where the write is unconditional, a withheld value SHALL resolve to the value already persisted on the row; and only where no value was previously persisted SHALL the durable result be `None`. The governing rule is single and uniform: **a withheld value never changes durable state**. The strip SHALL match a placeholder only as a whole value, never as a substring, so that placeholder text embedded inside a longer message survives. The strip SHALL remain narrowly scoped to the object-URI placeholder set: `[local-path]` and `[redacted]` are deliberately persisted evidence for runtime-root and secret redaction and SHALL continue to be stored verbatim. Where a durable write is already governed by a stricter remedy — the accepted-submit master row's frozen identity evidence, which rejects a divergent write loudly — that remedy SHALL continue to take precedence; silent withholding applies to the non-frozen evidence fields. Because the strip is idempotent, applying it both inside the record constructor and at a pre-existing outer call site is permitted and SHALL NOT change semantics — this mirrors the existing placement of the sibling durable-error-message sanitizer.

#### Scenario: A cohort projection write cannot launder a placeholder

- **WHEN** the accepted-submit cohort terminal projection writes a row whose evidence field carries an object-URI placeholder, through either the batched projection path or the deferred single-row path
- **THEN** the durable journal record and the direct row file store `None` for that field — the row held no real value for it beforehand — and no literal placeholder text is persisted by either path. Where the row did already hold a real value, the withheld-value rule below governs and the real value survives instead, whether the write path declines the overwrite or resolves the withheld value against the row

#### Scenario: Deliberately persisted placeholders survive

- **WHEN** a durable payload carries `[local-path]` or `[redacted]`
- **THEN** those values are persisted verbatim, unchanged by the anti-laundering strip

#### Scenario: Stored literals are normalized only by ordinary writes, never by a sweep

- **WHEN** the journal already contains rows in which a literal placeholder was persisted before this change
- **THEN** no migration, backfill, or sweep rewrites them, and reading them is unchanged; a later ordinary write to such a row normalizes the stored literal to `None`, which is the intended remedy rather than a rewrite pass

#### Scenario: The journal's own public rendering is not stripped away

- **WHEN** the pipeline-event lane deliberately renders a real object URI into a display placeholder as the durable public value of an event record
- **THEN** that rendered placeholder is persisted intact, because the strip runs on the caller's payload upstream of the rendering and never re-processes the rendering's output

#### Scenario: Placeholder text inside a longer message survives

- **WHEN** a durable field's value merely contains placeholder text as a substring of a longer message
- **THEN** the value is stored verbatim, because the strip matches whole values only

### Requirement: A cohort master's explicit terminal mark SHALL keep its attribution while observational evidence keeps refreshing

`permanently_failed` is an externally applied terminal mark, not a value the cohort projection can derive, and when the projection encounters a master already carrying that mark it SHALL preserve both the mark and the error code and error message the row already carries, instead of overwriting them with the values derived by the current pass. A row whose status says "permanently failed" while its error code is rewritten on every reconcile pass is self-contradictory attribution and SHALL NOT be produced. Observational evidence about the master Slurm job — completion time, exit code, log location — SHALL continue to refresh under the mark, because refreshing evidence under a sticky status is the projection's intended behavior and does not contradict the mark. The stickiness SHALL be triggered only by a status that is both externally applied — not derivable by the projection, which yields only `succeeded`, `partially_failed`, and `failed` — and already protected from status overwrite today; `permanently_failed` is currently the only such status, and pinning a derived value on its first computation would disable the projection rather than protect it. Terminal statuses that are neither derived nor currently status-protected are a pre-existing gap that this requirement does not address. An evidence field SHALL be treated as supplied by the caller only when it carries a real value: a display placeholder is a withheld value, not an instruction to overwrite, and SHALL NOT displace a real value already persisted on the row. That rule SHALL hold on every durable write path that admits caller-supplied evidence — not only the cohort projection paths — because a placeholder that displaces a real value and is then withheld destroys evidence that survived before. A write path MAY be exempt only where the rule does not apply to it — where it never reads the persisted row, where its outcome comparison does not consider the field, or where the caller's value is by contract the authoritative one — and each such exemption SHALL be enumerated with its reason at the resolution helper, so that the exempt set is readable in one place rather than inferred from omissions.

#### Scenario: Attribution survives a later reprojection

- **WHEN** a cohort master marked `permanently_failed` is reprojected by a later resume or reconcile pass that derives a different error code
- **THEN** the persisted status, error code, and error message are all unchanged

#### Scenario: Observational evidence still refreshes under the mark

- **WHEN** that same reprojection carries a real completion time, exit code, or log URI
- **THEN** those fields are updated on the durable row

#### Scenario: A withheld value does not displace a real one

- **WHEN** a reprojection carries an object-URI placeholder for a field whose durable row already holds a real object URI
- **THEN** the durable row still holds the real object URI afterwards — neither the placeholder nor `None` replaces it

#### Scenario: The deferred path protects a real value the same way

- **WHEN** a deferred cohort projection has already recorded a real log URI on a non-terminal row, and a later deferred pass for the same row carries an object-URI placeholder
- **THEN** the durable row still holds the real log URI, because the deferred path guards its evidence overwrite by the same rule as the batched path

#### Scenario: Derived terminal statuses are not made sticky

- **WHEN** a cohort master whose status is any of the three projection-derived terminal values is reprojected with a different derived error code
- **THEN** the error code is overwritten as before, because stickiness applies only to the externally applied mark

#### Scenario: Stickiness produces no empty write

- **WHEN** stickiness suppresses the only field that would otherwise have changed — a unit-constructed geometry, since a production row reaching this path always carries a changed per-task projection alongside
- **THEN** the projection detects no change and writes no record

### Requirement: A write path SHALL compare the same value it persists, so replaying an identical call converges

Several durable write paths decide their outcome by comparing the row they are about to write against the row already persisted, answering "already recorded" when the two agree. That comparison is only meaningful if the value compared is the value that will be persisted. Because caller-supplied evidence is normalized on its way to durable state, the normalization SHALL be applied before the comparison, not only before the write — otherwise an unchanged replay compares a raw caller value against a normalized durable value, the two can never agree, and the path reports a change on every call.

Replaying an identical call SHALL therefore converge: the second and later calls SHALL report that the state is already recorded, SHALL NOT append a further durable record, and SHALL NOT consume a further sequence number. A path that reports an applied change on every replay grows the journal without bound and drives it toward its segment and size limits. A path whose caller treats "already recorded" as success SHALL NOT instead report a conflict on replay, because a caller that reads a conflict as "not committed" will skip the follow-on work the first call earned.

#### Scenario: An identical replay carrying a withheld value converges

- **WHEN** a durable write path **that decides its outcome by comparison** is called twice with identical arguments in which an evidence field carries a display placeholder
- **THEN** the second call reports the state as already recorded, appends no further durable record, and consumes no further sequence number. A path that deliberately has no such comparison and appends on every call is outside this scenario

#### Scenario: A cancellation receipt replay stays committed

- **WHEN** a cancellation completion receipt carrying a withheld log location is delivered a second time for a row that already recorded it
- **THEN** the path answers "already recorded" rather than "conflict", so the caller still treats the cancellation as committed and does not drop its follow-on event

### Requirement: The journal's cache fast path SHALL be granted by cycle-write-window ownership, never by the mere fact that some thread holds the write lock

The cycle-rows cache serves a hit without revalidating its source files only inside a cycle write window. That fast path is safe because two rules hold **only for the thread that owns the window, and only for the cycle that window covers**: the cycle flock excludes other writers for that cycle, and every append invalidates every reachable cache key for that source/cycle so the next read recomputes from the newly committed journal bytes. The append hook SHALL NOT be understood as updating a reachable base cache entry in place; no such base key is produced by current readers, and invalidation followed by recomputation is the governing mechanism. The fast-path predicate SHALL therefore be true when and only when the calling thread is itself inside a write window for the very cycle being read, so that a thread which merely observes another thread's write in progress — or which reads a different cycle from inside a window — falls back to full source-file revalidation, exactly as it would in a single-threaded run. The predicate SHALL NOT be satisfied by holding a write lock taken for work that is not a cycle write: a lock held for reconcile-inventory maintenance takes no cycle flock and performs no cycle-cache invalidation, so it establishes neither rule and SHALL grant no fast path. Because the ownership marker is what grants the fast path, its lifetime SHALL be bounded by the same construct that opens the window and SHALL be cleared on every exit path including exceptions — including a failure raised while the window is being established, not only one raised from the work inside it — because a marker leaked past the window would hand a fast path to whatever unrelated task next reuses that thread identity. Only one construct SHALL set the marker, so that pairing is a structural property of that construct rather than a convention repeated across call sites. The window-entry wipe SHALL be treated as a correctness precondition rather than as a performance measure: the owner bypasses fingerprint validation for every hit, so without the wipe it could serve a pre-window entry that another process has already invalidated. Reads after the wipe may cache fingerprint-free entries during the window; a subsequent append invalidates them before the next read. The fast path narrows the tamper exposure that fingerprint validation would otherwise detect from any thread down to the window owner alone; the owner's own fast path still performs no tamper detection, which is a separate pre-existing concern this requirement does not address.

#### Scenario: A non-owner thread revalidates instead of trusting a hit

- **WHEN** one thread is inside a cycle write window and another thread, sharing the same repository instance, reads cycle rows for a different cohort whose cached entry is stale
- **THEN** the reading thread revalidates the source files and returns freshly recomputed rows, never the stale cached rows

#### Scenario: The owner keeps its fast path

- **WHEN** the thread that owns a cycle write window reads cycle rows inside that window
- **THEN** the cached rows are served without computing a source fingerprint at all

#### Scenario: A window for one cycle grants nothing for another

- **WHEN** the thread that owns a write window for one cycle reads cycle rows for a different cycle
- **THEN** the read revalidates its source files, because the window's flock protects only its own cycle

#### Scenario: A lock held for non-cycle work grants nothing

- **WHEN** a thread holds the repository write lock for work that takes no cycle flock and runs no append hook
- **THEN** cycle-rows reads on any thread, including that one, still revalidate their source files

#### Scenario: The marker does not survive an exception in the window's body

- **WHEN** the body of a cycle write window raises
- **THEN** the ownership marker is cleared before the exception propagates, so a later task reusing the same thread identity gets no fast path

#### Scenario: The marker does not survive a failure while the window is opening

- **WHEN** establishing the window fails before its body is ever entered
- **THEN** the ownership marker is cleared just the same, because the thread identity is released back to the pool either way

#### Scenario: A non-owner read does not depend on cache-clearing granularity

- **WHEN** the cycle write window's cache clearing is disabled entirely and a thread that owns no window reads a different cycle
- **THEN** that read still returns correct values, because a non-owner read rests on revalidation rather than on eviction

#### Scenario: An owner read does depend on the window-entry wipe

- **WHEN** the same clearing is disabled and the window owner reads its own cycle, for which a pre-window entry was cached and then invalidated by another process
- **THEN** the owner would serve that stale entry — which is why the window-entry wipe is a correctness precondition and not a tunable

### Requirement: A journal read SHALL absorb a concurrent atomic replacement of the file it is reading, within a bounded number of attempts

The journal's durable writes replace files atomically, which changes the target inode; the hardened reader rejects an inode change observed between its pre-open stat and its post-open fstat. Those are the same event at that layer, so a perfectly normal concurrent write is otherwise reported as a containment failure and then fans out into inconsistent outcomes — a silently skipped submission on one read path, a fabricated running status on another, a whole-source submission failure on a third. A read issued through the repository's cached read chokepoint — the single helper every journal document and event-log read passes through — SHALL therefore retry when it fails solely because the target was replaced mid-open, because an inode change means a writer just finished and re-reading yields the newly committed content. The guarantee is scoped to that chokepoint deliberately, and SHALL NOT be read as a promise about a journal read that bypasses it: the retry is inherited by routing through the chokepoint, not by being a journal read, and a future read path that opens the primitive directly carries no retry. The retry SHALL be bounded by a named constant and SHALL carry no sleep, and once the attempts are exhausted the read SHALL fail exactly as it does today rather than degrading to an empty or default result. The retry SHALL be selected on a structured discriminator carried by the error, never by matching its message text. Every other refusal the hardened reader can raise — symlinked target, non-regular target, containment violation, a symlink appearing inside the open window — SHALL NOT be retried even once, so the reader's fail-closed behavior is unchanged for every case except the one that was never an attack signal.

#### Scenario: A replacement landing inside the open window is absorbed

- **WHEN** the target file is atomically replaced between the reader's pre-open stat and its open
- **THEN** the read retries and returns the content committed by the replacement

#### Scenario: A relentless writer still fails closed

- **WHEN** every attempt observes a fresh replacement
- **THEN** the read makes exactly the bounded number of attempts and then raises, rather than retrying without limit or returning a default

#### Scenario: Safety refusals are never retried

- **WHEN** the read fails because the target is a symlink, is not a regular file, escapes the containment root, or becomes a symlink inside the open window
- **THEN** exactly one attempt is made and the refusal propagates unchanged

#### Scenario: Retry selection reads a field, not a message

- **WHEN** a read failure carries the same human-readable message but not the structured replacement discriminator
- **THEN** it is not retried

#### Scenario: Two threads read and write the same cycle

- **WHEN** two threads share one repository instance and concurrently read and write the same cycle
- **THEN** the reads complete without a containment failure, so an end-to-end test no longer has to separate reader and writer onto different cycles to stay green

### Requirement: Cycle-scoped single-row journal lookups with fall-open on underivable keys

A single-row journal lookup SHALL read only the cycle that owns the row.
Concretely: the file journal's single-row lookup entrypoints whose argument
carries a derivable `(source_id, cycle)` — lookup by cycle id, by run id, by
idempotency key, and by job id — SHALL resolve that pair from the argument and
read only that cycle's record sources: that cycle's `latest/<source>/<cycle>` views, that
cycle's `journal/<source>/<cycle>` segments, and the direct records. That
narrowed replay SHALL NOT read any other cycle's files.

This requirement is scoped to that narrowed replay deliberately, and SHALL NOT
be read as a promise about every read reachable from these entrypoints. It does
bind every reader of the unpartitioned flat direct directory: the filename rule
stated below SHALL have exactly one definition, shared by reference, so that a
second reader of that directory cannot be corrected independently of the first
or left uncorrected. A reader that establishes a row's identity from record
**content** SHALL retain that content check; the filename rule is a prefilter
ahead of it, not a replacement for it.

The narrowed read SHALL be a restriction of the input set only. Its result
SHALL be identical to the result of the whole-tree scan filtered by the same
key: the same rows, resolved by the same last-write-wins merge, in the same
order, and raising the same error for a blocked or unreadable row. Any flag
that governs whether direct records participate SHALL retain its meaning
unchanged in the narrowed read.

The narrowing SHALL derive the on-disk source directory by normalising the
source token parsed from the key, because run identifiers spell the source in
lower case while the on-disk directory casing is the normalised casing and
differs per source. A lookup whose key spells the source in a different case
from its directory SHALL still resolve the row.

When the `(source_id, cycle)` pair cannot be derived with certainty — an
unrecognised identifier shape, an unparseable cycle token, or an unknown source
— the entrypoint SHALL fall back to the whole-tree scan and return its answer.
It SHALL NOT return "not found" on a derivation failure. A narrowed lookup that
misses an existing row is silent and unsafe, whereas the fallback is merely as
slow as the prior behaviour.

An entrypoint whose argument carries no derivable cycle SHALL keep the
whole-tree scan, with its semantics unchanged.

The by-cycle direct partition SHALL NOT be used as the sole record source for
any of these lookups, because it holds only the subset of rows that are current
accepted-submit candidate rows; every other row, including cohort master rows
and rows from non-forecast stages, is written outside it, in an unpartitioned
flat directory.

When a cycle-scoped read of that flat direct directory happens, it SHALL filter
by file name rather than read the directory in full, because the directory
retains a row per job for all retained history and reading it whole would leave
the lookup's cost growing without bound. This obligation binds every
cycle-scoped reader of that directory, not only the narrowed replay, and the
filter SHALL be a single shared definition rather than a per-reader copy. The
comparison SHALL normalise the source token before comparing, because the
callers spell the source in both the run-identifier casing and the on-disk
casing, and a raw comparison would skip every file of a source passed in the
other case. A file SHALL be skipped only when its name resolves to a
`(source_id, cycle)` other than the one being looked up. A file whose name does not resolve to a `(source_id,
cycle)` at all SHALL be read, so that the filename filter fails toward reading
too much rather than toward missing a row.

The filename rule above and the whole-tree parity guarantee stated earlier are
in tension for exactly one input: a flat direct file whose name resolves to a
`(source_id, cycle)` that contradicts the row's own content. The filename rule
governs that case — such a file SHALL be skipped — and the parity guarantee is
correspondingly read as holding for rows whose file name agrees with their
content. This residual is declared rather than closed: no write path produces a
contradicting row, because every job identifier is derived from a run identifier
that is itself pinned to the row's own source and cycle, and the source token is
drawn from a closed allowlist containing no separator character. Nothing at the
write boundary *enforces* that agreement, so a file introduced onto disk by any
means other than these writers is outside the parity guarantee, with the
whole-tree scan as the recovery path.

#### Scenario: A lookup by cycle id reads only that cycle's files

- **WHEN** a single-row lookup is issued with a key from which
  `(source_id, cycle)` is derivable, against a journal holding records for many
  cycles and both sources
- **THEN** the lookup opens no file belonging to any other cycle
- **THEN** the rows it returns are identical — in content, merge resolution, and
  order — to those the whole-tree scan returns when filtered by that key.

#### Scenario: A cohort master row is still found after narrowing

- **WHEN** the row that answers the lookup is a cohort master row or a row from
  a non-forecast stage, which is not written into the by-cycle direct partition
- **THEN** the narrowed lookup still returns it, because it reads that cycle's
  view and journal record sources and not the direct partition alone.

#### Scenario: A lookup by job id reads only that cycle's files

- **WHEN** a lookup is issued by a job id whose shape encodes a source and a
  cycle, and the direct record for it is absent so the lookup must fall through
  to a record replay
- **THEN** the replay reads only that cycle's record sources
- **THEN** it returns the same row the whole-tree replay would have returned
- **THEN** whether direct records participate in that replay is governed by the
  same flag, with the same meaning, as before this change.

#### Scenario: An unrecognised flat direct file name is read, not skipped

- **WHEN** the flat direct directory holds a file whose name does not resolve to
  any `(source_id, cycle)`
- **THEN** the lookup reads that file rather than skipping it
- **THEN** a file whose name resolves to a different `(source_id, cycle)` than
  the one being looked up is skipped.

#### Scenario: A malformed flat direct file of another cycle does not block this one

- **WHEN** the flat direct directory holds an unreadable or malformed file whose
  name resolves to a `(source_id, cycle)` other than the one being looked up
- **THEN** no cycle-scoped reader of that directory opens it, so the lookup for
  this cycle succeeds
- **THEN** a malformed file whose name resolves to the cycle being looked up
  still fails the lookup closed, with its existing error.

#### Scenario: A source spelled in the other case still resolves

- **WHEN** the key spells the source in lower case while the journal's directory
  for that source is normalised to upper case
- **THEN** the lookup resolves the correct directory and returns the row.

#### Scenario: An underivable key falls open to the whole-tree scan

- **WHEN** the key does not match any recognised identifier shape, or its cycle
  token is not a valid cycle time, or its source is unknown
- **THEN** the entrypoint performs the whole-tree scan and returns that answer
- **THEN** it does not report the row as absent on account of the derivation
  having failed.

#### Scenario: A lookup whose argument carries no cycle is unchanged

- **WHEN** a single-row lookup is issued by an identifier that carries no
  derivable cycle
- **THEN** the entrypoint behaves exactly as it did before this change,
  returning the same row for the same journal state.

### Requirement: The cycle-scoped replay is memoized with a cycle-scoped invalidation signature

The cycle-scoped replay SHALL be memoized per `(source_id, cycle)` and per the
flag governing whether direct records participate, so that repeated lookups of
the same cycle within one orchestration pass do not re-read that cycle's files
once per lookup.

The memo's invalidation signature SHALL be derived exclusively from the files
that cycle's replay would itself open. Where a record source lives in a
directory shared across cycles, the signature SHALL be taken over the matched
file set rather than over the shared directory, so that a write belonging to a
different cycle does not invalidate this cycle's entry. A record source that
cannot be scoped to the cycle SHALL be recorded as a stated limitation of the
memo rather than covered by a shared directory's stat.

The memo SHALL be bounded in entry count and SHALL be safe under concurrent
orchestration threads sharing one repository instance, preserving the existing
single lock order: the signature is computed outside the cache mutex, and no
code holding the cache mutex acquires the write mutex.

#### Scenario: A write to another cycle does not evict this cycle's memo entry

- **WHEN** a cycle's replay has been memoized, and a row belonging to a
  different cycle is then written — into the same shared flat direct directory
  and the same shared per-source journal directory
- **THEN** a repeat replay of the first cycle opens no file at all
- **THEN** it returns the same rows as the first replay.

#### Scenario: A write to this cycle invalidates its memo entry

- **WHEN** a row belonging to the memoized cycle is written
- **THEN** the next replay of that cycle re-reads its files
- **THEN** it returns the newly written row rather than the stale one.

### Requirement: Journal reads are attributed to their entrypoint in pass evidence

Every read the file journal performs SHALL be counted against the entrypoint
and the reader lane that drove it, and the per-pass totals SHALL be merged into
the scheduler pass evidence artifact.

The counter SHALL be always on and SHALL ship in the repository, because the
production node from which the measurement is taken deploys by pulling the
repository and has no local-patch path, and because a probe that only runs on a
planning pass would not observe the writes that drive cache invalidation.

The counter SHALL distinguish bytes actually read from the filesystem from
reads satisfied by an in-process byte cache, so that its totals can be
reconciled against the operating system's own read accounting. It SHALL be safe
under concurrent orchestration threads sharing one repository instance, and it
SHALL NOT introduce a new lock ordering: its own mutex guards only counter
increments and is never held while any other lock is acquired.

The counters SHALL be reset at pass entry so their totals are per-pass, and the
merge into evidence SHALL be idempotent with respect to being invoked more than
once on a return path.

#### Scenario: A pass artifact carries the per-entrypoint read totals

- **WHEN** a scheduler pass completes, by any return path that writes an
  evidence artifact
- **THEN** the artifact carries a read attribution block naming, per
  entrypoint and reader lane, the number of reads and the number of bytes read
- **THEN** the totals reconcile against the sum of the per-tag rows.

#### Scenario: The counter is proven accurate, not merely self-consistent

- **WHEN** concurrent orchestration threads sharing one repository instance
  each perform a known number of reads
- **THEN** the recorded call count SHALL equal that independently known number,
  and SHALL NOT be asserted only against a total derived from the same rows it
  is being compared with — an assertion of the form
  `totals == sum(per_tag_rows)` is satisfied by construction for any counter
  content, including one that has silently lost updates under a race, and
  therefore SHALL NOT stand as the concurrency oracle for this requirement.

#### Scenario: No read escapes attribution

- **WHEN** a pass performs reads through any public journal API, including the
  cycle-status predicates and the write-path methods that read before they write
- **THEN** every counted byte SHALL carry both an entrypoint and a lane; a
  residual bucket for reads that reached no entrypoint SHALL NOT carry a
  material share of a pass's bytes, because a residual that dominates cannot
  separate baseline cost from the growth this change exists to measure.

#### Scenario: The by-cycle partition is not attributed to the flat directory

- **WHEN** a direct-record read for one cycle draws from both the unpartitioned
  flat directory and the already-partitioned by-cycle directory
- **THEN** the two SHALL be attributed to distinct lanes, so that bytes read
  from the partitioned tree are never graded against the flat directory's size.

#### Scenario: A narrowed lookup and a whole-tree lookup are told apart

- **WHEN** one lookup is answered by the cycle-scoped replay and another falls
  open to the whole-tree replay
- **THEN** the two are attributed to distinct tags, so the cost of the
  fall-open path is separable from the cost of the narrowed path.

### Requirement: Cohort runtime identity cross-check SHALL treat absent hydro_run identity fields as not-stored, not as mismatched

The file-journal runtime identity cross-check SHALL, for accepted-submit
forecast cohorts (`forecast_cohort_runtime_identity_matches`) and for each
cohort member, continue to require a per-model `hydro_run` row in the same
source and cycle that strictly matches on `run_id`, `model_id`,
`scenario_id`, `source_id`, `cycle_time`, and `submission_attempt`. For
`candidate_id`, `basin_id`, and `array_task_id` the check SHALL compare
strictly when the `hydro_run` row carries a value, and SHALL skip the field —
without failing the check — when the row's value is absent (`None`), because
some file-journal per-model writer paths do not persist these fields. A
present-but-different value SHALL remain fatal. The cohort-member side SHALL
remain fully strict, and the reconcile-side gates (exact master slurm id,
ownership user/account, stage-family job name, comment-when-stored, and the
array-task-id bijection against `cohort_members`) SHALL be unchanged.

#### Scenario: Production-shaped hydro_run rows reconcile to matched_bound

- **WHEN** an inflight forecast cohort's per-model `hydro_run` rows carry
  `None` for `candidate_id`, `basin_id`, and `array_task_id` (the shape
  written by `create_hydro_run`; array-shaped cohorts written by
  `create_hydro_run_from_basin` carry all three) and sacct returns a terminal master
  record passing all reconcile-side identity gates with a complete task
  bijection
- **THEN** restart reconcile SHALL record a `terminal` outcome with
  reconciliation decision `matched_bound` and project the per-task outcomes,
  instead of recording `identity_mismatch_blocked`

#### Scenario: Present-but-different identity fields still block

- **WHEN** a per-model `hydro_run` row carries a non-absent `candidate_id`,
  `basin_id`, or `array_task_id` that differs from the cohort member's value
- **THEN** the runtime identity cross-check SHALL fail and restart reconcile
  SHALL record `identity_mismatch_blocked` with zero durable writes

#### Scenario: Strict fields stay strict when degradable fields are absent

- **WHEN** a per-model `hydro_run` row has absent `candidate_id`,
  `basin_id`, and `array_task_id` but disagrees with the cohort member on
  `run_id`, `model_id`, `scenario_id`, `source_id`, `cycle_time`, or
  `submission_attempt` — or the row is missing entirely
- **THEN** the runtime identity cross-check SHALL fail and restart reconcile
  SHALL record `identity_mismatch_blocked` with zero durable writes

