# Enforce bootstrap-branch sum invariant in reconciliation validator (#1096)

## Why

`_enforce_registry_classification_reconciliation`
(`scripts/scheduler_file_provider_refresh.py:1905`) validates on-disk
classification self-consistency. The non-bootstrap branch (R2-N1) enforces
`unchanged + package_changed + removed == previous_count` (`:2008-2009`),
but the bootstrap branch (`previous_sha is None`, `:1995-2000`) only checks
shape (`previous_count is None`) — not the dual equality
`unchanged + package_changed + removed == 0`.

The runtime path cannot trigger this: `_classify_registry` guarantees
`previous_by_id = {}` on bootstrap so all three buckets are empty by
construction. But the validator's contract is to verify UNTRUSTED on-disk
receipts. A tampered bootstrap receipt (`previous_sha=null,
previous_count=null, removed=[d]` — or the symmetric forgery `added=0,
unchanged=N, prospective_count=N`) passes today: the downstream
`added + unchanged + package_changed == prospective_count` check (`:2011-2012`)
constrains nothing about `removed` and accepts the symmetric forgery when
totals are filled consistently. Defense-in-depth gap, issue-verified.

## What Changes

- `scripts/scheduler_file_provider_refresh.py`, bootstrap branch only: after
  the existing `if previous_count is not None: raise` (`:1999-2000`), add
  `if unchanged_total + package_changed_total + removed_total != 0:
  raise ValueError("receipt_classification_invalid")` — the exact dual of
  the non-bootstrap equality. One production line (plus comment); no other
  branch touched.
- `tests/test_scheduler_file_provider_refresh.py`: 3 negative
  receipt-tampering tests (bootstrap receipt with non-empty `removed` /
  `unchanged` / `package_changed` respectively → `receipt_classification_invalid`),
  mirroring the existing validator-negative pattern
  (`test_receipt_validator_rejects_unsafe_classification_item_model_id`
  `:3538` — build receipt, `pytest.raises(ValueError)`, assert message).
  Red-proof: all 3 must FAIL on pre-change code (tampered receipts validate
  clean today), pass after.
- Bootstrap happy-path (all three buckets zero) must keep passing — covered
  by existing bootstrap-publishing tests; identify and re-run them.

## Out of Scope

- Non-bootstrap branch equality (R2-N1, already covered and tested).
- `_classify_registry` construction-side behavior.
- Other receipt fields/invariants (`declared_cutovers ⊆ package_changed`,
  refused lower bound, etc. — independently checked).
- The `dry_run` early-return branch (`:1978-1990`): it skips ALL
  previous-registry reconciliation (bootstrap and non-bootstrap alike), so
  the new check is unreachable for `outcome="dry_run"` receipts. That
  pre-existing defense-in-depth gap is routed to its own tracked issue;
  the spec scenario here is scoped to non-dry_run outcomes.
- The rejected alternative (treating bootstrap as `previous_count == 0` in
  the same branch): changes the null-semantics contract of
  `previous_model_count` and the receipt schema — cross-node compatibility
  risk, explicitly declined by the issue.

## Impact

- Affected specs: `scheduler-registry-refresh` (ADDED requirement).
- Affected code: `scripts/scheduler_file_provider_refresh.py` (bootstrap
  branch of `_enforce_registry_classification_reconciliation`),
  `tests/test_scheduler_file_provider_refresh.py`.
- Behavior change surface: fail-closed only for receipts that are today
  invalid-by-construction; every in-tree writer produces empty buckets on
  bootstrap, so no green path changes. Callers of `_validate_receipt`
  (publish path `:1900`, installer-side validation) all get the stricter
  check via the single helper — no sibling copies. (`:1900` is the sole call
site of the reconciliation helper, inside
`_validate_registry_classification_field` `:1804`; the publish path reaches
it via `_publish_primary_receipt` → `_validate_receipt`.)
