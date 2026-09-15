# production-scheduler-orchestration — delta

## ADDED Requirements

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
single-cycle repair authorization).

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
