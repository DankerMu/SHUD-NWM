## ADDED Requirements

### Requirement: Canonical readiness reports identity mismatch only for rows that exist

`evaluate_canonical_readiness` SHALL report `reason == "canonical_identity_mismatch"` only when at least one candidate canonical row exists for the cycle and the observed policy or source-object identity of those rows disagrees with the expected identity. When the candidate row set is empty (`candidate_row_count == 0`, hence `identity_rejected_row_count == 0`), the evaluation MUST NOT treat the absence of observed identities as a mismatch: it SHALL report `ready == False`, `status == "canonical_incomplete"`, and — because with zero rows every required variable is missing and the reason chain checks missing variables first — `reason == "missing_canonical_variables"` whenever the required variable set is non-empty. Readiness with rows present — mismatching policy, mismatching source object, or partially missing lineage — MUST keep its existing reason (`canonical_identity_mismatch` or `canonical_lineage_missing`).

#### Scenario: Fresh cycle with no canonical products and expected identity
- **WHEN** readiness is evaluated for `gfs` cycle `2026-09-03T12Z` with `products=[]`, `forecast_hours=(0, 3)`, and a non-empty expected policy identity and source-object identity
- **THEN** the evidence has `ready == False`, `status == "canonical_incomplete"`, `candidate_row_count == 0`, `identity_rejected_row_count == 0`
- **AND** `reason == "missing_canonical_variables"`, never `canonical_identity_mismatch`
- **AND** the scheduler's fresh-zero-row predicate still classifies that evidence as a fresh zero-row cycle

#### Scenario: Rows with a different policy identity still mismatch
- **WHEN** readiness is evaluated over canonical rows whose policy identity differs from the expected policy identity
- **THEN** `reason == "canonical_identity_mismatch"` as before
