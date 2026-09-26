## ADDED Requirements

### Requirement: A same-run_id forecast rerun SHALL carry the current submission attempt onto the durable hydro_run row

In the file-journal lane, a forecast staging that finds an existing non-retriable `hydro_run` row for the same `run_id` SHALL raise that row's `submission_attempt` (and `run_manifest_uri`, when the manifest carries one) to the current reservation's attempt when the current attempt is greater, reading and appending the row inside one journal lock so a concurrent status write is never reverted. It SHALL NOT lower the attempt, change the row's status, or change its init-state identity. The accepted-submit release entrypoints (submit-attempt rejection, absence retry permit, operator-verified-absence demotion) SHALL keep releasing only hydro rows whose attempt equals the master's. When the stale state is shown unreachable on the production write path, this requirement is satisfied by a pinning test that proves it, with no code change.

#### Scenario: A rerun releases its own active hydro row

- **WHEN** a same-`run_id` rerun stages attempt 2 over a hydro row left at attempt 1 in an active status, and the attempt-2 master is then rejected, permitted for absence retry, or demoted as operator-verified absent
- **THEN** the durable hydro row carries `submission_attempt=2` and is released to `failed`

#### Scenario: Another attempt's row is never released

- **WHEN** a release entrypoint runs for an attempt that does not equal the hydro row's attempt
- **THEN** the hydro row is left unchanged

### Requirement: The model-less cohort convert row SHALL record its members

In repositories supporting accepted-submit reconcile, the model-less cohort `convert` row SHALL record `cohort_members` at reservation, like the downstream-of-forecast rows, adding no accepted-submit master marker. Active-pipeline detection SHALL then exclude a sibling cohort's convert row by the existing recorded-membership rule, and a true duplicate SHALL still be refused.

#### Scenario: A split cohort's override unit is not blocked by the default unit's convert row

- **WHEN** only the default cohort's convert row is active and the override cohort unit (not a member) checks for conflicts
- **THEN** no `PIPELINE_ALREADY_ACTIVE` conflict is raised and the override unit proceeds in the same pass

#### Scenario: A convert row with members is not read as accepted-submit identity

- **WHEN** a convert row records `cohort_members`
- **THEN** it is not classified as an accepted-submit master and is not read as forcing member identity

## MODIFIED Requirements

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
retry be emitted. This ordering applies in the file-journal lane; the DB lane
records no journal init-state token (`completed_pipeline_init_state_id` returns
none there), so the quarantine yields no decision and DB-lane behavior is
unchanged. A manifest-missing loop whose lineage matches still has no retry
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
