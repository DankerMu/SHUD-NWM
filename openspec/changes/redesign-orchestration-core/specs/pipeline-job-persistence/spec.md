## MODIFIED Requirements

### Requirement: pipeline_job Record Creation

The scheduler SHALL record every stage job it submits to Slurm in the node-22
file orchestration journal, and that journal record SHALL be the authoritative
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

- **WHEN** the DB-free node-22 Orchestrator submits a stage job
- **THEN** the authoritative job record SHALL be created in the file orchestration journal with the same identity and lifecycle fields
- **AND** `ops.pipeline_job` SHALL not be populated by the node-22 journal lane
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

### Requirement: Status Synchronization via sacct

The scheduler SHALL update the file journal job record when `sacct` returns a
new status for the corresponding Slurm job; the file journal is the only
store the scheduler synchronizes (DB-free is its only mode). All status writes
go through `FileOrchestrationJournalRepository.update_pipeline_job_status`;
a field a caller does not pass is left unchanged, except that a success status
clears an unpassed `error_code` / `error_message`. The in-pass Gateway poll
(`services/orchestrator/chain_stage_execution.py` `_update_runtime_pipeline_status`)
records, with the status, each of `started_at`, `finished_at`, `exit_code`,
`error_code`, `error_message` and `log_uri` that the Gateway reports. The
terminal status of a non-cohort array stage is written from its task
aggregation instead (`services/orchestrator/chain_array_accounting.py`), with
the aggregation's error code and message, and with `finished_at` falling back
to the write time when the Gateway reports none. Restart reconcile
(`services/orchestrator/reconcile_inflight.py` `reconcile_inflight_jobs`) goes
through `update_job_status` and records the status and, for a failure, the
mapped `error_code` only; it does not record timestamps, exit codes or
`log_uri`. An accepted-submit forecast
cohort instead takes the typed runtime transition and exact task accounting of
the file-orchestration-journal requirement "Accepted forecast cohort
submission is reconciled exactly once". The node-27 derived projection SHALL
advance only from validated published provenance whose source authority and
ordering match the journal record (`services/orchestrator/pipeline_job_provenance.py`;
`tests/test_pipeline_job_provenance_importer.py`
`test_importer_older_snapshot_cannot_roll_back_existing_job`).

#### Scenario: Slurm job transitions to RUNNING

- **WHEN** `sacct` reports status `RUNNING` for a `slurm_job_id` whose journal job record is still in flight (`pending` or `submitted`)
- **THEN** the scheduler SHALL set the journal job record's `status` to `running` (status transition: `tests/test_file_orchestration_journal.py` `test_file_orchestration_journal_exposes_restart_reconcile_store_interface`) and advance `updated_at`; on the Gateway-poll path it also records the Gateway-reported start time as `started_at`

#### Scenario: Slurm job completes successfully

- **WHEN** `sacct` reports status `COMPLETED` with exit code 0 for a `slurm_job_id`
- **THEN** the scheduler SHALL set the journal job record's `status` to `succeeded` (status transition: `tests/test_file_orchestration_journal.py` `test_file_orchestration_journal_pipeline_reservation_bind_event_and_terminal_guards`); on the Gateway-poll path it also records the Gateway-reported `finished_at` and exit code 0

#### Scenario: Slurm job fails

- **WHEN** `sacct` reports status `FAILED`, `TIMEOUT`, or `NODE_FAIL` for a `slurm_job_id`
- **THEN** the scheduler SHALL set the journal job record's `status` to `failed` with the mapped error code (e.g., `SLURM_TIMEOUT`, `NODE_FAILURE`); on the Gateway-poll path it also records the Gateway-reported end time, exit code and error message
- **AND** on the accepted-submit cohort route the same mapping comes from exact task accounting (`tests/test_gateway_reconcile_file_cohort_projection.py` `test_file_cohort_boot_fail_task_projects_failed_with_node_failure`)

#### Scenario: Slurm job cancelled

- **WHEN** `sacct` reports status `CANCELLED` for a `slurm_job_id`
- **THEN** the scheduler SHALL set the journal job record's `status` to `cancelled` and advance `updated_at`; on the Gateway-poll path it also records the Gateway-reported `finished_at`

#### Scenario: log_uri populated on job completion

- **WHEN** a Slurm job reaches a terminal status (`succeeded`, `failed`, `cancelled`)
- **THEN** on the Gateway-poll path the scheduler SHALL record the Slurm output log location reported for the job (derived from the sbatch `--output` directive) as the journal job record's `log_uri`, and a placeholder SHALL NOT displace a real `log_uri` already recorded (`tests/test_file_orchestration_journal.py` `test_update_pipeline_job_status_placeholder_does_not_displace_a_real_log_uri`)
- **AND** in the DB-free display projection, a previously null `log_uri` MAY be enriched monotonically only when the source journal proves that exact task/log binding and the published artifact verifies (`tests/test_pipeline_job_provenance_importer.py` `test_importer_enriches_null_log_at_equal_source_version`).
