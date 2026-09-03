## ADDED Requirements

### Requirement: Hydro run status sets have one definition and a parity lock

The hydro-run durable-success status set SHALL be one shared object defined in `scheduler_state_types`, consulted by the scheduler's candidate decision, by the SQL completed-pipeline probe, by the file journal's completed-pipeline probes, by the forecast trigger's completion check, and by the durable-output predicate; the names `COMPLETED_HYDRO_STATUSES` on `chain` and `chain_repository` SHALL remain importable and SHALL bind to that same object. The hydro-run error-code-clearing status set consulted by the file journal's status write and by the recorded-failure-code reader SHALL likewise be one shared `frozenset` defined in `scheduler_state_types`, with the reader's private name kept as an alias to it.

Every member of the hydro-run active set on every module, and every member of the durable-success and code-clearing sets other than `"complete"`, SHALL be a member of the `hydro.run_status` enum as declared by the migrations, where the parity lock SHALL derive that member table by sweeping every migration file for the enum's `CREATE TYPE` and `ADD VALUE` statements, and a migration change SHALL select the parity lock in CI. `"complete"` SHALL stay in the durable-success and code-clearing sets as the one named exception: it is unreachable on the database lane (closed enum) and is not produced by any production writer on the file-journal lane, but that lane does not validate `hydro_run.status` and its test construction face uses it, so removing it would change file-journal decisions. The membership of every set SHALL be unchanged by this consolidation.

The three `ACTIVE_HYDRO_STATUSES` copies SHALL be pinned as `chain == chain_repository == scheduler_state_types - {"pending"}` and labelled as an unadjudicated divergence; this requirement does not decide whether `"pending"` belongs in the SQL active probe.

#### Scenario: Aliases are the same object

- **WHEN** `chain.COMPLETED_HYDRO_STATUSES`, `chain_repository.COMPLETED_HYDRO_STATUSES`, the file journal's imported `COMPLETED_HYDRO_STATUSES`, the scheduler decision module's imported `DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain_forecast_trigger._completed_hydro_statuses()` are compared by identity with `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`
- **THEN** each `is` the same object, and `scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES` and the file journal's imported name `is` `scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES`, which is a `frozenset` of six members

#### Scenario: Inline consumer consults the shared durable set

- **WHEN** a sentinel status is added to the shared durable-success set for the duration of a probe
- **THEN** `_durable_shud_output_exists({"hydro_status": sentinel})` returns `True`, and after the sentinel is discarded it returns `False`

#### Scenario: A fake member turns the lock red

- **WHEN** any alias site is rebound to a fresh literal, or a status outside the `hydro.run_status` enum other than `"complete"` is added to the durable-success set
- **THEN** the parity test fails

#### Scenario: Decisions are unchanged

- **WHEN** the scheduler, chain, retry and journal suites run against the consolidated sets
- **THEN** every existing assertion passes with no assertion edits, including the fixtures that write `hydro_status="complete"` on the file-journal lane
