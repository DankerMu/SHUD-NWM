## MODIFIED Requirements

### Requirement: A journal-predecessor identity quarantine retry SHALL consult the per-model forcing witness

The scheduler SHALL consult the per-model forcing witness for quarantine retries:
when the journal predecessor identity quarantine replaces a completed-type skip
with a retry that restarts at `forecast`, the scheduler SHALL consult the same
upstream-artifact guard the strict warm-start lane uses for the candidate's own
`(source, cycle, basin_version_id, model_id)` before that retry can be emitted,
on every lane where no later consultation runs. When no forcing witness is
found, the decision SHALL be the existing stable missing-forcing blocker (reason
`missing_forcing_package_uri` or `forcing_version_row_absent`), which satisfies
the stable missing-forcing blocker contract; the guard's existing copyback leg
MAY instead yield its own named copyback blocker. The quarantine
circuit-breaker `blocked` exit submits no work and SHALL keep its reason absent
from every forced-resubmit whitelist. A `manual_retry_requested` decision SHALL
NOT be rewritten by this consultation.

Automatic re-entry of the forcing stage for a candidate that landed in the
stable missing-forcing blocker SHALL NOT be emitted by any unattended scheduler
lane; such candidates are drained by operator action (forcing backfill for the
renamed model identities, and — on the strict warm-start lane only — the explicit
single-cycle repair authorization, except for a blocker that descends from a
quarantine retry).

On the strict warm-start lane the quarantine retry SHALL keep its decision
literal `retry_journal_predecessor_identity_mismatch` through the strict
warm-start retry upgrade, so the forecast-cohort reservation can stamp its
quarantine provenance. The explicit single-cycle missing-forcing repair
authorization SHALL refuse to reclassify a missing-forcing blocker that
descends from a quarantine retry. Descent is recognised by the surviving
`journal_predecessor_identity` evidence block or by
`artifact_guard.planned_retry_decision` naming the quarantine retry. For an
unconfirmed candidate the refusal reason is
`journal_predecessor_quarantine_present`. A candidate carrying an operator
re-entry confirmation keeps the earlier `operator_reentry_confirmation_present`
refusal, which is evaluated first. Such a reclassification
would restart at `forcing`, where no quarantine provenance can be stamped. A
real re-run of the stale lineage could then submit without moving the breaker
count. A refused candidate SHALL stay in the stable missing-forcing blocker,
drained by restoring the model's own forcing. Missing-forcing blockers that do not descend from
a quarantine retry SHALL keep the existing repair behaviour.

#### Scenario: Quarantine retry without own forcing blocks

- **WHEN** a candidate's completed-type skip is quarantined for a stale journal
  predecessor identity, no strict warm-start evidence is present, and no forcing
  witness exists for the candidate's own model
- **THEN** the candidate SHALL be `blocked` with reason
  `missing_forcing_package_uri` or `forcing_version_row_absent`
- **AND** the evidence SHALL carry the forcing provenance
- **AND** no forecast work SHALL be submitted

#### Scenario: Quarantine retry with own forcing is unchanged

- **WHEN** the same quarantine fires and the candidate's own forcing package is
  witnessed
- **THEN** the retry decision, its reason, its `restart_stage: "forecast"`, and
  the submission SHALL be unchanged

#### Scenario: The quarantine blocker is drainable

- **WHEN** a quarantine retry is replaced by the missing-forcing blocker
- **THEN** the decision SHALL satisfy the stable missing-forcing blocker contract,
  so the forcing backfill for renamed model identities drains it

#### Scenario: Strict-lane quarantine retry keeps its literal

- **WHEN** a `terminal_completed_cycle` skip is quarantined on the strict
  warm-start lane, the candidate's own forcing is witnessed, and the terminal
  run manifest does not match the strict warm-start evidence
- **THEN** the emitted decision SHALL be `retry_journal_predecessor_identity_mismatch`
  and SHALL NOT be rewritten to `retry_strict_warm_start_retry_run_manifest_mismatch`

#### Scenario: Repair authorization refuses a quarantine-descended blocker

- **WHEN** an unconfirmed strict-lane quarantine retry is replaced by the stable
  missing-forcing blocker and the single-cycle repair authorization is open for
  that exact cycle with every other repair precondition satisfied
- **THEN** the candidate SHALL stay `blocked` on the stable missing-forcing
  blocker with repair rejection reason `journal_predecessor_quarantine_present`
- **AND** no `retry_repair_missing_forcing` decision SHALL be emitted and no work
  SHALL be submitted

#### Scenario: Repair of a non-quarantine blocker is unchanged

- **WHEN** a missing-forcing blocker that does not descend from a quarantine
  retry meets every repair precondition
- **THEN** it SHALL still be reclassified to `retry_repair_missing_forcing`

## ADDED Requirements

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

#### Scenario: Run-manifest-missing retry without own forcing blocks

- **WHEN** a non-strict-lane candidate is terminal success, its evidence lacks a
  run-manifest initial state, and no forcing witness exists for its own model
- **THEN** the candidate SHALL be `blocked` with reason `missing_forcing_package_uri`
  or `forcing_version_row_absent`, satisfying the stable missing-forcing blocker
  contract
- **AND** no forecast work SHALL be submitted

#### Scenario: Run-manifest-missing retry with own forcing is unchanged

- **WHEN** the same candidate's own forcing package is witnessed
- **THEN** the decision SHALL remain `retry_terminal_run_manifest_missing` with
  `restart_stage: "forecast"` and its evidence SHALL carry the forcing provenance
