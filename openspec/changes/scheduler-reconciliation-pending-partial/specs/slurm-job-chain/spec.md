## ADDED Requirements

### Requirement: Reconciliation-pending nested submissions defer without manufacturing failure

The partial-array retry helper SHALL preserve a nested `submit_result_ambiguous` or `reconcile_unverified` result as a reconciliation deferral. It SHALL map either stage terminal to cycle terminal `reconciling`, while preserving the distinct duplicate-submission skip terminal.

#### Scenario: Nested ambiguous submission stops on reconciling

- **WHEN** a nested partial-array resubmission returns `submit_result_ambiguous` without an aggregation
- **THEN** the pending tasks SHALL NOT be rewritten as failed
- **THEN** the cycle SHALL terminate as `reconciling` with the raw ambiguous stage result
- **THEN** no downstream stage SHALL run and no further retry attempt SHALL be derived

#### Scenario: Nested unverified reconciliation preserves durable no-op

- **WHEN** a nested partial-array resubmission returns `reconcile_unverified` without an aggregation
- **THEN** the pending tasks SHALL NOT be rewritten as failed
- **THEN** the cycle SHALL terminate as `reconciling`
- **THEN** the existing reconciliation event or row MAY remain, but the executor SHALL NOT add a second partial or failed cycle-status write
- **THEN** no downstream stage or further retry attempt SHALL run

#### Scenario: Unrelated nested terminals retain existing behavior

- **WHEN** a nested resubmission returns `skipped_duplicate_submission`, `submission_failed`, success, or an ordinary failed aggregation
- **THEN** duplicate skip SHALL retain its dedicated skip terminal
- **THEN** `submission_failed` and ordinary success/failure retry semantics SHALL remain unchanged
