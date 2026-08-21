## ADDED Requirements

### Requirement: Duplicate-submission deferral has one cross-plane status meaning

The orchestrator SHALL preserve `skipped_duplicate_submission` as the raw reserve-gate terminal and SHALL translate that terminal to the existing production status `blocked` on every stage-evidence projection. The translator SHALL continue to map unrecognized statuses to `failed`.

#### Scenario: Duplicate-submission stage evidence is blocked

- **WHEN** either scheduler stage-evidence projection receives a stage whose status is `skipped_duplicate_submission`
- **THEN** the raw status SHALL remain `skipped_duplicate_submission`
- **THEN** its `production_status` SHALL be `blocked`
- **THEN** the value SHALL be a member of `PRODUCTION_STATUS_TAXONOMY`

#### Scenario: Unknown status still fails closed

- **WHEN** the production status translator receives an unrecognized status
- **THEN** it SHALL return `failed`
- **THEN** adding duplicate-submission support SHALL NOT broaden the unknown-status fallback
