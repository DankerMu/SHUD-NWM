# Spec Delta: scheduler-registry-refresh

## ADDED Requirements

### Requirement: Bootstrap classification receipts SHALL satisfy the empty-previous sum invariant

When a registry classification receipt with a non-`dry_run` outcome carries `previous_registry_sha256 = null` (bootstrap semantics: no previous canonical registry existed), the reconciliation validator SHALL reject the receipt with `receipt_classification_invalid` unless `unchanged.total + package_changed.total + removed.total == 0` — the dual of the non-bootstrap equality `unchanged + package_changed + removed == previous_model_count` — so a tampered on-disk bootstrap receipt cannot smuggle non-empty carry-over buckets past validation.

#### Scenario: Tampered bootstrap receipt with non-empty buckets is rejected

- **WHEN** a receipt with `previous_registry_sha256 = null` and
  `previous_model_count = null` carries a non-zero total in any of
  `removed`, `unchanged`, or `package_changed`, and the receipt `outcome`
  is not `dry_run` (the dry_run branch runs id-only reconciliation and
  returns before the previous-registry branch — its reconciliation gap is
  tracked separately, out of scope here)
- **THEN** receipt validation SHALL raise with
  `receipt_classification_invalid`, even when the
  `added + unchanged + package_changed == prospective_model_count` equality
  and the refused lower bound are satisfied by symmetric forgery

#### Scenario: Honest bootstrap receipt keeps validating

- **WHEN** a bootstrap receipt carries empty `removed`, `unchanged`, and
  `package_changed` buckets (the only shape `_classify_registry` can
  construct when no previous registry exists)
- **THEN** receipt validation SHALL pass unchanged
