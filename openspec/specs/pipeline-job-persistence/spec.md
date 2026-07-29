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

### Requirement: Accepted-submit cohort forecast terminal rows SHALL record init-state identity forward-only

The accepted-submit cohort forecast path SHALL persist the init-state identity (`init_state_id`, `checksum`, `uri`, `valid_time`) **at reservation time**, where the planning context is available, as a per-model identity mapping keyed by `array_task_id`/`model_id` on the cohort master row, outside the cohort-digest input set; terminal per-model row construction SHALL read each row's identity from the master-row mapping by its own `array_task_id` rather than from cohort-member projection, and a scalar single-identity field SHALL NOT be used. The recording SHALL NOT alter the ordinary-upsert frozen-field semantics (the identity's value is stable from reservation onward) and SHALL NOT enter the cohort-digest member field set — historical rows' `forecast_cohort_digest` validation results SHALL be unchanged. Invalid or partial identity payloads SHALL be rejected by accepted-submit normalization. Existing journal rows without these fields SHALL remain readable unchanged — no migration, no backfill, no rewrite of historical rows.

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

