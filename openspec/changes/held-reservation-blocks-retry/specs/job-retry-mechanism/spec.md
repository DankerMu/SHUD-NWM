## ADDED Requirements

### Requirement: A held reservation SHALL block the candidate-state retry decision

The DB-free candidate-state decision SHALL return `skip` with reason `active_duplicate_pipeline` whenever any pipeline row present in the candidate's provider-filtered state is a held reservation, whatever the row's stage and whatever its cohort membership annotation (`member`, `unwitnessed`, or `incomplete`). Attribution SHALL NOT be restricted to the stages that the cohort-member attribution gate recognizes. A held reservation is a row whose status is `reserved` and which has no real Slurm binding. The block SHALL be evaluated before any hydro-placeholder supersession, completed-upstream-stage resume, terminal-success, failure-policy, or manual-retry branch. It SHALL apply regardless of the reservation's reconcile reason class, because an ambiguous submit may already have been accepted by Slurm, and resubmitting it would run the same runs twice in the same run directories. The decision evidence SHALL carry `decision: skip_active`, `active_status: reserved`, and the held rows. Rows excluded from the candidate by recorded cohort membership (`non_member`) SHALL NOT block. Once restart reconcile binds the row or releases it, the block SHALL no longer apply and the pre-existing decisions SHALL be unchanged.

#### Scenario: Ambiguous forecast submit held while forcing already succeeded
- **WHEN** a candidate's forcing stage succeeded, its forecast cohort row is `reserved` with `submit_outcome` `submit_result_ambiguous` and no Slurm id, its hydro run is `created`, and the cycle carries no failure rows
- **THEN** the decision SHALL be `skip` / `active_duplicate_pipeline` with `active_status` `reserved`
- **AND** the decision SHALL NOT be `retry_after_completed_stage`

#### Scenario: Held state_save_qc reservation
- **WHEN** a candidate's forecast stage succeeded and its state_save_qc cohort row is `reserved` with an ambiguous submit and no Slurm id
- **THEN** the decision SHALL be `skip` / `active_duplicate_pipeline`
- **AND** the decision SHALL NOT be `resume_after_completed_stage`

#### Scenario: Unwitnessed or incomplete membership still blocks
- **WHEN** the held reserved row's cohort membership annotation is `unwitnessed` or `incomplete`
- **THEN** the decision SHALL be `skip` / `active_duplicate_pipeline`

#### Scenario: Reconcile reason class does not matter
- **WHEN** the held row's reconcile outcome is transient `query_unavailable`, `comment_accounting_unproven`, or `ambiguous_fallback_match`
- **THEN** the decision SHALL be `skip` / `active_duplicate_pipeline`

#### Scenario: A manual retry marker does not override a held reservation
- **WHEN** a manual-retry marker targets a candidate that still has a held reservation
- **THEN** the decision SHALL be `skip` / `active_duplicate_pipeline`

#### Scenario: Sibling cohort reservations do not block
- **WHEN** the only reserved row in the cycle belongs to a sibling cohort whose recorded membership excludes the candidate
- **THEN** the decision SHALL be unchanged from the behavior without that row

#### Scenario: Bound or released reservations resume normal decisions
- **WHEN** restart reconcile binds the held row and its inflight projection is `succeeded`
- **THEN** the candidate SHALL resume after the completed stage as before
- **WHEN** the held row is released to `reservation_lost`
- **THEN** the decision SHALL equal the pre-change decision for that released row
