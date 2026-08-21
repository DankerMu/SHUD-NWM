## ADDED Requirements

### Requirement: Readiness recount recognizes reconciliation-pending evidence without accepting it

The scheduler readiness validator SHALL recognize `reconciling`, `submit_result_ambiguous`, and `reconcile_unverified` model-run rows as partial and producer-partial, not failed and not submitted-compatible. Pass status `reconciling` SHALL be an allowed review-blocked status. Recognition SHALL keep producer counts and readiness cardinality in agreement without allowing final readiness.

#### Scenario: Zero-submission reconciling pass has consistent cardinality

- **WHEN** a scheduler pass has status `reconciling`, submitted count zero, and one model-run row carrying a governed reconciliation status
- **THEN** the row SHALL have `partial=true`, `producer_partial=true`, and `failed=false`
- **THEN** producer and readiness `partial_count` SHALL both equal one
- **THEN** validation SHALL NOT emit `status_not_allowed`, `partial_count_exceeds_model_run_evidence`, or `partial_count_status_cardinality_mismatch`
- **THEN** the scheduler readiness item SHALL remain `blocked`

#### Scenario: Reconciliation status does not imply submission or failure

- **WHEN** readiness evaluates a bare reconciliation-pending model-run row
- **THEN** it SHALL NOT infer `submitted=true`
- **THEN** it SHALL NOT count the row as failed
- **THEN** it SHALL count the row on both partial recount predicates
