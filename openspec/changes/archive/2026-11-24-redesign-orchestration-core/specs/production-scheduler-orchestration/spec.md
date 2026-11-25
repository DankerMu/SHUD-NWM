## MODIFIED Requirements

### Requirement: Operators can atomically demote a manually verified-dead comment-unobservable reservation

The file-journal scheduler SHALL expose a row-scoped operator CLI that converts a current accepted-submit cohort master from the exact held state (`status=reserved`, no bound or matched Slurm job id, `submit_outcome=submit_result_ambiguous`, `reconciliation_source=slurm_exact_comment`, `reconciliation_decision=accounting_unavailable`, and `reconciliation_reason_class=comment_accounting_unproven`) to `status=reservation_lost` with the distinct `operator_verified_absence` decision only when the operator supplies explicit confirmation, operator identity, a timezone-aware check time, a bounded non-empty verification note, and exact persisted submission-attempt and attempt-anchor expectations. The transition SHALL execute under the cycle lock, reject every stale or mismatched request without changing journal bytes, clear the post-state reason class, and atomically append the cohort master, eligible active member failure projections, and a durable audit event containing the operator evidence and prior accounting blocker. That authority append is the operation's commit point. A later direct/latest derived-projection failure SHALL NOT turn the committed demotion into a reported failure: the command SHALL return committed success with bounded non-secret projection warnings, while journal replay remains authoritative and a repeated request remains a zero-write CAS refusal. The command SHALL be file-journal-only and SHALL behave identically through the click and argparse entrypoints.

#### Scenario: Exact confirmed request records one audited demotion

- **WHEN** an operator has independently verified absence and invokes `demote-reserved-job` with all required confirmation, operator, attempt, and anchor values matching the exact held master
- **THEN** the command exits zero with a stable JSON receipt, the master becomes `reservation_lost/operator_verified_absence` with a null reason class, matching active member rows are projected to `failed/SLURM_RESERVATION_LOST`, and one operator audit event records the prior blocker and verification evidence in the same durable append

#### Scenario: Missing confirmation or evidence is rejected before writing

- **WHEN** `--confirm`, operator identity, timezone-aware check time, or the bounded verification note is missing or invalid
- **THEN** both CLI entrypoints exit non-zero, report the validation error, and leave the journal byte-identical

#### Scenario: Stale or wrong durable state loses the compare-and-swap

- **WHEN** the job id does not name a current master, or any current status, binding, outcome, source, decision, reason class, submission attempt, or attempt anchor differs from the supplied held-row expectation
- **THEN** the command exits non-zero and writes no master, member, event, sequence, or materialized-latest record

#### Scenario: Concurrent successor cannot be demoted

- **WHEN** another actor binds, permits, releases, demotes, or reclaims the reservation before the operator transition obtains the cycle lock
- **THEN** the locked re-read detects the changed authority state and the stale operator request writes nothing

#### Scenario: State and audit evidence fail together before commit

- **WHEN** validation or append of any master, member, or audit-event record fails before the authority batch commit
- **THEN** neither the operator decision nor any partial member/event evidence becomes durable

#### Scenario: Derived projection failure after commit is reported as committed

- **WHEN** the authority batch commits and a later direct-job or latest materialization write fails
- **THEN** the command still reports the demotion as committed, carries a bounded non-secret warning naming each failed projection, attempts the remaining independent projections, and does not append another operator decision when the same request is retried

#### Scenario: Automatic fail-closed behavior remains unchanged

- **WHEN** reconcile runs on a cluster that does not store job comments and no operator command is invoked
- **THEN** it continues to record `accounting_unavailable/comment_accounting_unproven`, keeps the row `reserved`, and never infers absence from the empty comment query

#### Scenario: PostgreSQL and manual retry surfaces do not gain this authority

- **WHEN** a caller uses the shared `OrchestratorRepository` protocol or the chain facade, the HTTP/manual retry API, or a generic file-journal evidence transition
- **THEN** no `operator_verified_absence` demotion capability is exposed and `reserved` remains outside the manual-retry source statuses

#### Scenario: Release validation does not manufacture a production incident

- **WHEN** a read-only census of the active production file journal finds no naturally occurring master in the exact held pre-state
- **THEN** release evidence SHALL use the deterministic held-to-reclaim chain, fault/refusal matrix, final-head CI, and recorded census; it SHALL NOT stop the production scheduler, force gateway unavailability, inject or rewrite journal authority, or submit a real cohort merely to create a live receipt

#### Scenario: A natural held-row incident retains an in-situ receipt

- **WHEN** a naturally occurring exact held master is independently confirmed dead with name/time/user/account `sacct` and `squeue` evidence and the guarded command is used operationally
- **THEN** the incident record SHALL retain the success receipt and durable audit event, a stale or repeated zero-write refusal, the fresh reclaim attempt and anchor, exactly one cohort resubmission, and the cleanup boundary

#### Scenario: Non-dedicated accepted-submit writers cannot persist the operator decision

- **WHEN** the submit-attempt commit writer receives an accepted transition carrying `operator_verified_absence`, the cohort defer or cohort task-projection writer receives the raw decision token, ordinary pipeline-job upsert receives the token while creating or upgrading a current-contract row, the legacy `transition_pipeline_job_submit_evidence` path receives a transition carrying the token, or `record_pipeline_job_reconciliation` receives the token
- **THEN** every non-dedicated writer rejects it with `file_journal_authority_transition_requires_typed_api` before row construction, lock acquisition, durable mutation, or event, the journal stays byte-identical, and existing legitimate current-contract decisions, legacy decisions, and non-token legacy upgrades still apply unchanged

#### Scenario: Committed reclaim never strands a pre-sbatch live reservation

- **WHEN** the public old-ID operator recovery path commits the reclaim authority append and a derived direct or inventory projection write then fails before any submission
- **THEN** the failure SHALL NOT be reported as an uncommitted failure that leaves a live `reserved` row: the flow either completes the single submission path or transitions the row to a non-live retryable authority state under the lock, and the next public pass does not fail with `PIPELINE_ALREADY_ACTIVE`

#### Scenario: The receipt locator uses the one safe journal-root authority

- **WHEN** `--journal-root` is a symlink loop or a literal unexpanded tilde path
- **THEN** the loop root fails through the typed operational error path before the authority append with no traceback and zero journal bytes, and the tilde root's success receipt locator equals the expanded authority root actually used by repository reads and writes

### Requirement: A terminal run-manifest-missing retry SHALL consult the per-model forcing witness

The scheduler SHALL consult the per-model forcing witness for run-manifest-missing
retries: when a terminal-success skip (`terminal_hydro_success` or
`terminal_pipeline_success`) that lacks a run-manifest initial state is replaced
on the non-strict lane by the `retry_terminal_run_manifest_missing` forced
resubmit that restarts at `forecast`, it SHALL consult the per-model
forcing witness for the candidate's own `(source, cycle, basin_version_id,
model_id)` before the retry can be emitted. When no witness is found, the
decision SHALL be the stable missing-forcing blocker (reason
`missing_forcing_package_uri` or `forcing_version_row_absent`), returned
verbatim from the guard, and no forecast work SHALL be submitted. When the
witness is found, the retry decision, its reason, and its `restart_stage` SHALL
be unchanged and its evidence SHALL carry the forcing provenance.

Before that replacement, the scheduler SHALL first evaluate the §8.7
journal-predecessor identity quarantine for the same skip. A quarantine retry or
breaker-blocked decision SHALL replace the skip instead, carry quarantine
provenance, count toward the §8.7 breaker, and consult the per-model forcing
witness exactly as the quarantine leg does for a manifest-present skip. Only when
the quarantine yields no decision SHALL the `retry_terminal_run_manifest_missing`
retry be emitted. This ordering applies in the file-journal lane, which is the only
candidate-state lane: the DB lane, which recorded no journal init-state token,
was deleted with `PsycopgOrchestratorRepository` in R3 (#3260). A manifest-missing loop whose lineage matches still has no retry
budget of its own; that residual risk is not addressed here.

#### Scenario: Run-manifest-missing retry without own forcing blocks

- **WHEN** a non-strict-lane candidate is terminal success, its evidence lacks a
  run-manifest initial state, the quarantine yields no decision, and no forcing
  witness exists for its own model
- **THEN** the candidate SHALL be `blocked` with reason `missing_forcing_package_uri`
  or `forcing_version_row_absent`, satisfying the stable missing-forcing blocker
  contract
- **AND** no forecast work SHALL be submitted

#### Scenario: Run-manifest-missing retry with own forcing is unchanged

- **WHEN** the same candidate's own forcing package is witnessed and the
  quarantine yields no decision (the journal token matches, no id is recorded,
  or the base key differs)
- **THEN** the decision SHALL remain `retry_terminal_run_manifest_missing` with
  `restart_stage: "forecast"` and its evidence SHALL carry the forcing provenance,
  identical to the pre-change behavior

#### Scenario: Stale lineage with a missing manifest is quarantined

- **WHEN** a non-strict-lane `terminal_hydro_success` skip lacks a run-manifest
  initial state and its journal init-state token is stale for the cycle's
  expected lineage
- **THEN** the decision is the journal-predecessor quarantine retry, the model is
  counted by `canonical_quarantine_rerun_model_ids`, and the forcing witness is
  consulted

#### Scenario: The breaker stops a repeating manifest-missing rerun

- **WHEN** the same shape recurs until the §8.7 breaker threshold
- **THEN** the decision is `blocked_journal_predecessor_identity_quarantine` and
  no forecast rerun is submitted

## REMOVED Requirements

### Requirement: Hydro run status sets have one definition and a parity lock

**Reason**: R3 (#3260) deletes `PsycopgOrchestratorRepository` and `services/orchestrator/chain_repository.py`, so the SQL completed-pipeline and active probes, the `chain_repository` aliases and the "SQL active probe counts a pending hydro run" scenario no longer exist. A MODIFIED block cannot drop that scenario.

**Migration**: replaced by the ADDED requirement "Hydro run status sets have one definition shared by chain and the file journal", which keeps every surviving clause and scenario unchanged and names only `chain` and the file journal as alias sites.

## ADDED Requirements

### Requirement: Hydro run status sets have one definition shared by chain and the file journal

The hydro-run durable-success status set SHALL be one shared object defined in `scheduler_state_types`, consulted by the scheduler's candidate decision, by the file journal's completed-pipeline probes, by the forecast trigger's completion check, and by the durable-output predicate; the name `COMPLETED_HYDRO_STATUSES` on `chain` SHALL remain importable and SHALL bind to that same object. The hydro-run error-code-clearing status set consulted by the file journal's status write and by the recorded-failure-code reader SHALL likewise be one shared `frozenset` defined in `scheduler_state_types`, with the reader's private name kept as an alias to it.

The hydro-run active status set SHALL be one shared object defined in `scheduler_state_types` holding exactly `created`, `staged`, `pending`, `submitted` and `running`; the name `ACTIVE_HYDRO_STATUSES` on `chain` SHALL remain importable and SHALL bind to that same object, and the file journal SHALL import the name from `scheduler_state_types`. `pending` is an active status because it is the value manual retry writes to `hydro_run` once the retry job is submitted, and the scheduler candidate decision and the manual-retry blocker lane already treat it so. The file journal's active-pipeline probe SHALL match `pending` under its existing candidate-scoped terminal-completion suppression (#1472), which is unchanged; and the file journal's attempt-scoped write paths — submit-attempt rejection, retry permission and operator-verified demotion — SHALL treat a `pending` member row of the affected attempt exactly as they treat `created`, `staged`, `submitted` and `running` rows (rewrite to `failed` with the path's error code), and the cohort task projection SHALL treat a `pending` row as retryable so a reconciled succeeded task rewrites it to `succeeded` and a reconciled failed task rewrites it to `failed` with the task's error code. Repairing a stale `pending` row on the database lane is not part of this requirement.

Every member of the hydro-run active, durable-success and code-clearing sets other than `"complete"` SHALL be a member of the `hydro.run_status` enum as declared by the migrations, where the parity lock SHALL derive that member table by sweeping every migration file for the enum's `CREATE TYPE` and `ADD VALUE` statements with the type identifier written bare or double-quoted on either segment, SHALL fail closed on any `RENAME VALUE` or `RENAME TO` statement for the type because it does not model renames, and a migration change SHALL select the parity lock in CI. `"complete"` SHALL stay in the durable-success and code-clearing sets as the one named exception: it is unreachable on the database lane (closed enum) and is not produced by any production writer on the file-journal lane, but that lane does not validate `hydro_run.status` and its test construction face uses it, so removing it would change file-journal decisions. The membership of the durable-success and code-clearing sets SHALL be unchanged by this consolidation.

#### Scenario: Aliases are the same object

- **WHEN** `chain.COMPLETED_HYDRO_STATUSES`, the file journal's imported `COMPLETED_HYDRO_STATUSES`, the scheduler decision module's imported `DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain_forecast_trigger._completed_hydro_statuses()` are compared by identity with `scheduler_state_types.DURABLE_HYDRO_SUCCESS_STATUSES`, and `chain.ACTIVE_HYDRO_STATUSES` and the file journal's imported `ACTIVE_HYDRO_STATUSES` are compared by identity with `scheduler_state_types.ACTIVE_HYDRO_STATUSES`
- **THEN** each `is` the same object, and `scheduler_state_failure._HYDRO_RUN_CODE_CLEARING_STATUSES` and the file journal's imported name `is` `scheduler_state_types.HYDRO_RUN_CODE_CLEARING_STATUSES`, which is a `frozenset` of six members

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
- **THEN** every existing assertion passes with no assertion edits, including the fixtures that write `hydro_status="complete"` on the file-journal lane
