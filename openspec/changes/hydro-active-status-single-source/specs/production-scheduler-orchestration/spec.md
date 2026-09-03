## MODIFIED Requirements

### Requirement: Hydro run status sets have one definition and a parity lock

The hydro-run durable-success status set SHALL be one shared object defined in `scheduler_state_types`, consulted by the scheduler's candidate decision, by the SQL completed-pipeline probe, by the file journal's completed-pipeline probes, by the forecast trigger's completion check, and by the durable-output predicate; the names `COMPLETED_HYDRO_STATUSES` on `chain` and `chain_repository` SHALL remain importable and SHALL bind to that same object. The hydro-run error-code-clearing status set consulted by the file journal's status write and by the recorded-failure-code reader SHALL likewise be one shared `frozenset` defined in `scheduler_state_types`, with the reader's private name kept as an alias to it.

The hydro-run active status set SHALL be one shared object defined in `scheduler_state_types` holding exactly `created`, `staged`, `pending`, `submitted` and `running`; the names `ACTIVE_HYDRO_STATUSES` on `chain` and `chain_repository` SHALL remain importable and SHALL bind to that same object, and the file journal SHALL import the name from `scheduler_state_types`. `pending` is an active status because it is the value manual retry writes to `hydro_run` once the retry job is submitted, and the scheduler candidate decision and the manual-retry blocker lane already treat it so. The SQL active-pipeline probe's hydro arm SHALL therefore match `pending`, so that a `hydro_run` row at `pending` whose cycle has no `pipeline_job` row answers "active" on the database lane as it does on the decision lane. The file journal's active-pipeline probe SHALL match `pending` under its existing candidate-scoped terminal-completion suppression (#1472), which is NOT mirrored by the SQL probe and is unchanged; and the file journal's attempt-scoped write paths — submit-attempt rejection, retry permission and operator-verified demotion — SHALL treat a `pending` member row of the affected attempt exactly as they treat `created`, `staged`, `submitted` and `running` rows (rewrite to `failed` with the path's error code), and the cohort task projection SHALL treat a `pending` row as retryable so a reconciled succeeded task rewrites it to `succeeded` and a reconciled failed task rewrites it to `failed` with the task's error code. Repairing a stale `pending` row on the database lane is not part of this requirement.

Every member of the hydro-run active, durable-success and code-clearing sets other than `"complete"` SHALL be a member of the `hydro.run_status` enum as declared by the migrations, where the parity lock SHALL derive that member table by sweeping every migration file for the enum's `CREATE TYPE` and `ADD VALUE` statements with the type identifier written bare or double-quoted on either segment, SHALL fail closed on any `RENAME VALUE` or `RENAME TO` statement for the type because it does not model renames, and a migration change SHALL select the parity lock in CI. `"complete"` SHALL stay in the durable-success and code-clearing sets as the one named exception: it is unreachable on the database lane (closed enum) and is not produced by any production writer on the file-journal lane, but that lane does not validate `hydro_run.status` and its test construction face uses it, so removing it would change file-journal decisions. The membership of the durable-success and code-clearing sets SHALL be unchanged by this consolidation.

#### Scenario: Aliases are the same object

- **WHEN** `chain.COMPLETED_HYDRO_STATUSES`, `chain_repository.COMPLETED_HYDRO_STATUSES`, the file journal's imported `COMPLETED_HYDRO_STATUSES`, the scheduler decision module's imported `DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain_forecast_trigger._completed_hydro_statuses()` are compared by identity with `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain.ACTIVE_HYDRO_STATUSES`, `chain_repository.ACTIVE_HYDRO_STATUSES` and the file journal's imported `ACTIVE_HYDRO_STATUSES` are compared by identity with `scheduler_state_types.ACTIVE_HYDRO_STATUSES`
- **THEN** each `is` the same object, and `scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES` and the file journal's imported name `is` `scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES`, which is a `frozenset` of six members

#### Scenario: SQL active probe counts a pending hydro run

- **WHEN** `PsycopgOrchestratorRepository.has_active_pipeline` is called for a source, cycle and model whose `hydro.hydro_run` row has `status = 'pending'` and no `ops.pipeline_job` row exists for the cycle at all
- **THEN** the bound hydro-status parameter contains `pending` and, against a real Postgres, the probe returns `True`

#### Scenario: Inline consumer consults the shared durable set

- **WHEN** a sentinel status is added to the shared durable-success set for the duration of a probe
- **THEN** `_durable_shud_output_exists({"hydro_status": sentinel})` returns `True`, and after the sentinel is discarded it returns `False`

#### Scenario: A fake member turns the lock red

- **WHEN** any alias site is rebound to a fresh literal, or a status outside the `hydro.run_status` enum other than `"complete"` is added to the durable-success set
- **THEN** the parity test fails

#### Scenario: Quoted identifiers and renames in migrations

- **WHEN** a migration adds a value with the type written as `hydro."run_status"` or `"hydro"."run_status"`
- **THEN** the sweep counts the value exactly as for the bare identifier
- **WHEN** a migration renames a value of the type or renames the type
- **THEN** the parity lock fails with a message naming that migration file and stating that the oracle does not model renames

#### Scenario: Journal lane counts a pending hydro run

- **WHEN** a file-journal latest view carries a candidate-matching `hydro_run` at `pending` and no pipeline-job rows
- **THEN** the journal's `has_active_pipeline` returns `True`
- **WHEN** a cohort member's hydro row is `pending` at the attempt that `permit_pipeline_job_retry` marks lost, or a `pending` row meets a reconciled `succeeded` task in `project_forecast_cohort_tasks`
- **THEN** the row is rewritten to `failed` / `SLURM_RESERVATION_LOST` in the first case and to `succeeded` in the second (a reconciled `failed` task would rewrite it to `failed` with the task's error code), exactly as a `running` row would be

#### Scenario: Decision-lane answers and every non-pending answer are unchanged

- **WHEN** the scheduler, chain, retry and journal suites run against the consolidated sets
- **THEN** every existing assertion passes with no assertion edits other than the SQL-parameter pin that gains `pending`, including the fixtures that write `hydro_status="complete"` on the file-journal lane
