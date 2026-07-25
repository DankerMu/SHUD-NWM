# Spec Delta: scheduler-registry-refresh

## ADDED Requirements

### Requirement: The receipt validator SHALL bind dry_run receipts to the reconciliation constraints that hold in id-only mode

`_enforce_registry_classification_reconciliation` SHALL reject an
`outcome="dry_run"` receipt whose classification violates a constraint
that the id-only classify path guarantees by construction, specifically:
`removed.total` must be zero, `previous_registry_sha256` and
`previous_model_count` must be null together or non-null together (with a
non-boolean integer count >= 0), `new_registry_sha256` must be null (a
dry_run never publishes a registry), and when a previous registry exists
`unchanged.total` must not exceed `previous_model_count`. The validator
SHALL NOT apply the full previous-side equality (`unchanged +
package_changed + removed == previous_model_count`) to dry_run receipts,
because dry_run classification never evaluates removals.

#### Scenario: Tampered dry_run receipt with removed entries is rejected

- **WHEN** a receipt has `outcome="dry_run"` and
  `classification.removed.total != 0`, whether or not a previous registry
  is recorded
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Contradictory previous-registry shape is rejected in dry_run

- **WHEN** a dry_run receipt carries `previous_registry_sha256 = null`
  together with a non-null `previous_model_count` (the pairing
  contradiction — the newly reachable discriminator at this validator;
  boolean/negative/non-integer counts are additionally rejected by the
  branch-local guard, mirroring the field-level validation that already
  runs on the receipt path)
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Forged new-registry sha on a dry_run receipt is rejected

- **WHEN** a dry_run receipt carries a non-null `new_registry_sha256`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: dry_run unchanged total exceeding the previous count is rejected

- **WHEN** a dry_run receipt records a previous registry with
  `previous_model_count = N` and `classification.unchanged.total > N`
- **THEN** validation SHALL raise `receipt_classification_invalid`

#### Scenario: Honest dry_run receipts keep passing

- **WHEN** a dry_run receipt matches the writer's id-only construction —
  zero removed/package_changed/refused/declared totals,
  `added + unchanged == prospective_model_count`, paired previous fields,
  `unchanged <= previous_model_count`, null `new_registry_sha256` —
  including the legal shape where the
  previous registry holds models absent from the prospective set (so the
  previous-side sum equality does not hold)
- **THEN** validation SHALL pass without raising
