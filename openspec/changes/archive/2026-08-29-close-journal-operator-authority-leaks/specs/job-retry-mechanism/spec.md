## ADDED Requirements

### Requirement: Operator recovery attestations do not cross retry-attempt boundaries

The file-journal manual-retry service SHALL treat `operator_recovery_attested_at` as authority bound to the exact released source row. A newly created manual-retry row SHALL NOT inherit that attestation, even if a future caller or test explicitly selects an attested `reservation_lost/identity_mismatch_released` source. `reservation_lost` SHALL remain outside `MANUAL_RETRY_SOURCE_STATUSES`, and the downstream operator-recovery predicate SHALL independently require the released status and exact authority tuple. The dedicated typed recovery API, the attested source row, ordinary failed-row manual retry, and bounded retry lineage SHALL remain unchanged.

#### Scenario: Released reservation is not a generic manual-retry source

- **WHEN** manual-retry source selection observes an attested row with `status=reservation_lost` and `reconciliation_decision=identity_mismatch_released`
- **THEN** it SHALL NOT select that row as a manual-retry source, because `reservation_lost` remains outside `MANUAL_RETRY_SOURCE_STATUSES`

#### Scenario: Forced source cannot transfer the attestation

- **WHEN** an attested released row is explicitly supplied to the manual-retry clone seam independent of normal source selection
- **THEN** the new persisted retry row SHALL have `operator_recovery_attested_at` cleared, SHALL identify the original through `previous_job_id`, and SHALL preserve the existing bounded retry lineage contract
- **AND** the original source row SHALL retain its attestation unchanged

#### Scenario: Retry successor cannot satisfy the operator-recovery predicate

- **WHEN** the downstream `_operator_recovery_attested` predicate evaluates the new manual-retry successor
- **THEN** it SHALL return false because the successor is an unattested distinct attempt and is not a released row

#### Scenario: Dedicated recovery remains the sole attestation writer

- **WHEN** `recover_released_identity_blocked_reservation` receives an eligible current-contract released master and exact attempt expectations
- **THEN** it SHALL retain its existing typed behavior and remain the sole writer of `operator_recovery_attested_at`
