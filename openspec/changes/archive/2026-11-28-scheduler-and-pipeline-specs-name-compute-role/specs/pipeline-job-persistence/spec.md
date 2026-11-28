## MODIFIED Requirements

### Requirement: pipeline_job Record Creation

The scheduler SHALL record every stage job it submits to Slurm in the
file orchestration journal of the compute role, and that journal record SHALL be the authoritative
job record at submission. DB-free is the scheduler's only mode: no scheduler
path builds a PostgreSQL orchestration repository or writes `ops.pipeline_job`
(`ProductionSchedulerConfig` pins `scheduler_db_free_required` to `true`;
`services/orchestrator/scheduler_core.py` `ProductionScheduler.__init__` and
`_default_orchestrator_for` take the file journal repository and refuse with
`SchedulerModeInvariantError` when there is none). Node-27's `ops.pipeline_job`
SHALL be a derived projection created from a validated published job-provenance
record (`services/orchestrator/pipeline_job_provenance.py`). Under no deployment
SHALL node-27 display or ingest fabricate a job solely to satisfy evidence
collection.

#### Scenario: PostgreSQL orchestrator submits a single-basin stage job

- **WHEN** the scheduler submits a stage job (e.g., `run_shud_forecast_array`)
  for a single basin via `sbatch`
- **THEN** no PostgreSQL orchestrator inserts an `ops.pipeline_job` row for it,
  because the scheduler has no PostgreSQL orchestration repository to call
  (`tests/test_production_scheduler.py`
  `test_db_free_scheduler_fake_slurm_submission_writes_file_journal_without_database_url`)
- **AND** the file journal reserves the job record, with its `job_id`,
  `job_type`, `stage`, `cycle_id`, `run_id` and `model_id`, before the Gateway
  call, and binds the Slurm-assigned `slurm_job_id` after `sbatch` returns
  (`FileOrchestrationJournalRepository.reserve_pipeline_job` /
  `bind_pipeline_job_reservation`; `tests/test_file_orchestration_journal.py`
  `test_file_orchestration_journal_pipeline_reservation_bind_event_and_terminal_guards`)
- **AND** a node-27 `ops.pipeline_job` row for that job, when one exists, is the
  projection of the published job-provenance record and has the columns of
  "pipeline_job Schema and Fields".

#### Scenario: DB-free orchestrator submits a stage job

- **WHEN** the compute role's DB-free Orchestrator submits a stage job
- **THEN** the authoritative job record SHALL be created in the file orchestration journal with the same identity and lifecycle fields
- **AND** `ops.pipeline_job` SHALL not be populated by the journal lane of the compute role
- **AND** a node-27 projection SHALL be created only from the validated published job-provenance record
- **AND** forecast-scoped rows SHALL match source, cycle, run, model, and job identity
- **AND** cycle-scoped rows SHALL keep null `model_id` and their original cycle `run_id` rather than inherit an enclosing forecast identity

#### Scenario: PostgreSQL orchestrator submits a cycle-level stage job (no per-basin scope)

- **WHEN** the scheduler submits a cycle-level stage job (e.g.,
  `convert_canonical`, `publish_tiles`) that is not scoped to a specific basin
- **THEN** no PostgreSQL orchestrator inserts an `ops.pipeline_job` row for it
- **AND** for a multi-basin cycle the file journal record and any node-27
  projection of it keep a null `model_id` and the cycle's own `run_id`
  (`cycle_<source>_<cycle>` unless the basins carry exactly one distinct
  non-empty `orchestration_run_id`)
- **AND** for a single-basin cycle the record carries the sole basin's
  `model_id` and, when that basin has one, its `orchestration_run_id`, so the
  row counts as candidate scope rather than cycle scope (stamp:
  `services/orchestrator/chain_runtime_utils.py` `_cycle_pipeline_job_model_id`
  / `_cycle_orchestration_run_id`; candidate scope of a stamped row:
  `tests/test_production_scheduler.py`
  `test_gate_open_fallback_floor_is_scoped_to_the_restarted_stage_family`,
  `test_non_canonical_family_stage_fallback_floor_mints_the_next_attempt`).

#### Scenario: sbatch submission fails

- **WHEN** `sbatch` returns a non-zero exit code during submission
- **THEN** the file journal job record SHALL still be kept, with `status` set to
  `submission_failed`, no `slurm_job_id`, and the bounded sbatch error recorded
  as its error code and message
  (`FileOrchestrationJournalRepository.reject_pipeline_job_submit_attempt` for
  accepted-submit cohorts; `tests/test_forcing_submit_ambiguity_reservation.py`
  `test_real_http_forcing_rejection_keeps_rejected_semantics_without_ambiguity`)
- **AND** no `ops.pipeline_job` row is written by the scheduler for it.

### Requirement: Expired file-journal cycles SHALL be archived only as complete, inactive, recoverable authority slices

The file-journal retention entrypoint of the compute role SHALL derive age from a cycle's `%Y%m%d%H` identity and SHALL use a 90-day default hot-retention window. Its candidate set SHALL be the complete union discovered from exactly the hot `latest/`, `journal/`, and `pipeline-events/` roots under one aggregate existing journal file-count bound and the existing journal depth bound. A failed root walk or exhausted bound SHALL block the entire enforce invocation rather than return a partial candidate set. For one normalized `(source_id, cycle_time)` slice it SHALL consider only that cycle's recognized `latest` views, bounded `journal` segments, and bounded `pipeline-events` segments. It SHALL NOT scan for candidates in, remove, or rewrite `pipeline-jobs`, reconcile inventory, cycle lock files, archives, or the scheduler state index.

Before enforcement the entrypoint SHALL validate that the configured retention window exceeds scheduler lookback plus cycle lag plus twice the largest allowed-cycle gap. It SHALL require a fresh pipeline frontier and SHALL exempt every cycle at or after a non-null active lower bound. Missing, stale, malformed, or retention-disabled frontier evidence, and invalid or missing safety-window configuration, SHALL prevent all hot-file removal rather than degrade to wall-clock deletion.

For each candidate the entrypoint SHALL acquire the same cross-process cycle lock used by journal writers without waiting indefinitely, inspect the canonical cycle replay, and apply the journal owner's existing rollback/reconcile and released-identity-blocked predicates. Any live row, lock contention, malformed or unrecognized member, symlink or non-regular member, source/cycle mismatch, segment gap/overflow, disappearing file, or unreadable input SHALL leave the entire cycle untouched with a stable reason in the receipt. Retention SHALL NOT rely on reconcile inventory alone because a released identity-blocked row is intentionally absent from that inventory.

An eligible cycle SHALL be written as one `tar.zst` archive plus a versioned manifest that binds the normalized source/cycle, frontier evidence, archive SHA-256, and every member's relative path, size, and SHA-256. The implementation SHALL write and verify a temporary archive, atomically publish it without clobbering a conflicting bundle, and only then unlink the exact recognized hot members while still holding the cycle lock. Dry-run SHALL be the default, enforcement SHALL require explicit enablement and `dry_run=false`, and every invocation SHALL emit a bounded receipt. A matching existing archive and a partially removed hot slice SHALL be safely retryable; a conflicting archive SHALL block the cycle.

Cold archives SHALL NOT become a transparent read tier. Recovery SHALL be an explicit, documented operation that verifies archive and member identities, rejects path escape and overwrite, restores the original relative paths while the selected cycle has no writers, and proves cycle-query parity before normal operation resumes. Future retention of the scheduler state index is outside this capability and MUST preserve at least one usable state-index history anchor at or before relevant candidate cutoffs for each existing `(model_id, source_id)`; this retention entrypoint SHALL leave the index byte-identical.

#### Scenario: Dry-run plans an eligible complete cycle without mutation

- **WHEN** a recognized `gfs` or `IFS` cycle is older than 90 days, precedes a fresh active frontier, contains only quiescent terminal rows, and retention is in its default dry-run mode
- **THEN** the receipt lists the complete `latest`, `journal`, and `pipeline-events` member plan and byte count
- **THEN** no archive or hot-authority member is created, removed, or rewritten

#### Scenario: Enforce archives before removing only the hot cycle slice

- **WHEN** the same eligible cycle is processed with retention explicitly enabled and dry-run disabled
- **THEN** one verified `tar.zst` bundle and manifest are atomically published before any hot member is unlinked
- **THEN** only that cycle's recognized hot members are removed, while `pipeline-jobs`, reconcile inventory, locks, and state index remain byte-identical

#### Scenario: A recoverable row retains the whole cycle

- **WHEN** canonical replay contains an unbound reservation, a current accepted master with incomplete terminal projections, a rollback-blocking row, or a released identity-blocked reservation even though reconcile inventory has no anchor for it
- **THEN** retention records `live_row` for the candidate and leaves every member of the cycle untouched

#### Scenario: Candidate discovery is complete or enforcement stops

- **WHEN** all three hot roots contain a large-but-valid candidate set at the aggregate file/depth bounds
- **THEN** every normalized candidate is discovered exactly once regardless of which hot surface introduced it
- **WHEN** one more entry exceeds the aggregate bound or any root walk fails
- **THEN** enforcement archives and removes nothing rather than operating on a truncated candidate set

#### Scenario: Active or unprovable scheduler windows fail closed

- **WHEN** the candidate is at or after the fresh pipeline frontier, the configured hot window violates the lookback/lag/cycle-gap inequality, or frontier evidence is missing, stale, malformed, or from a pass whose retention did not run
- **THEN** enforcement removes no hot member and records the precise exemption or blocker
- **THEN** there is no override that falls back to pure wall-clock deletion

#### Scenario: Concurrent or malformed cycle authority is not partially archived

- **WHEN** the cycle lock is busy or any recognized slot is symlinked, non-regular, unreadable, gapped, over the segment cap, mismatched, replaced during inspection, or accompanied by an unrecognized cycle-shaped member
- **THEN** retention skips or blocks the entire cycle without waiting indefinitely
- **THEN** no archive is published and no hot member is removed

#### Scenario: Archive publication and cleanup are idempotent

- **WHEN** a prior attempt published a byte-identical verified archive but removed only a subset of its hot members
- **THEN** a retry verifies the existing archive and removes only remaining manifest-bound members
- **THEN** a pre-existing archive with a different manifest or digest blocks the cycle without overwriting either copy

#### Scenario: Restore reproduces the archived cycle without clobbering authority

- **WHEN** an operator verifies a cold bundle, stages every member under containment, and restores it while the selected cycle has no writers
- **THEN** path traversal, symlinks, unexpected members, digest mismatches, and existing destination files fail closed
- **THEN** a successful restore reproduces every member digest and the captured cycle-scoped query result

#### Scenario: State history semantics are unchanged

- **WHEN** retention dry-run, enforce, or restore is executed
- **THEN** the scheduler state-index file and its usable entries remain byte-identical
- **THEN** an existing model cannot be reclassified as `cold_new_model` because journal retention removed a state-index history anchor
