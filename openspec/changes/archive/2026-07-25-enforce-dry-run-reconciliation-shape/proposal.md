# Enforce dry_run reconciliation shape constraints in the receipt validator (#1135)

## Why

`_enforce_registry_classification_reconciliation`
(`scripts/scheduler_file_provider_refresh.py:1905`) is the tamper-resistance
validator for on-disk refresh receipts (same threat model as #1096). Its
`outcome == "dry_run"` branch (`:1977-1987`) returns before the
previous-registry section, so a dry_run receipt's `removed.total`,
`previous_model_count`, and `previous_registry_sha256` are bound by NO
constraint at all: probe-tested tampering (removed=1 under bootstrap,
previous_count=999 vs unchanged=2+removed=1, previous_sha=null with
previous_count=7) is ACCEPTED for dry_run while identical tampering under
`published` is rejected. #1096 explicitly scoped this branch out
(`openspec/changes/archive/*enforce-bootstrap-reconciliation-sum/specs/.../spec.md`);
after #1096 landed, dry_run is the only remaining freely-forgeable path
through this validator. The runtime writer (`_classify_registry`, dry_run
block `:2517-2525`) is honest — this is defense-in-depth, not a live-data
bug.

## What Changes

Recommended route from #1135 (adopted): add the constraints that DO hold in
id-only mode inside the dry_run branch, instead of skipping the whole
section. Per the writer's construction semantics:

- `removed.total == 0` — hard invariant: the dry_run classify path never
  evaluates removals (`:2523-2524` comment + `:2542-2544` loop is after the
  dry_run return at `:2525`).
- Shape pairing: `previous_registry_sha256 is None` ⇔
  `previous_model_count is None` (same source: `result.previous_model_count
  = None if previous is None else len(previous_by_id)` at `:2510-2512`);
  when previous exists, `previous_model_count` must be a non-bool int >= 0.
- Upper bound when previous exists: `unchanged_total <= previous_count`
  (dry_run `unchanged` collects only prospective ∩ previous, `:2518-2521`).
- `new_registry_sha256 is None` — the writer pins it to None in dry_run
  (`:2485` `new_registry_sha256=None if dry_run else new_sha256`); today
  only the non-null 64-hex format is checked (`:1834-1840`), so a dry_run
  receipt can forge a "published" registry sha (fixture-review P2-4).
- Explicitly NOT added: the full equality
  `unchanged + package_changed + removed == previous_count` — it does NOT
  hold in dry_run (removals are never computed), and adding it would reject
  honest receipts (issue acceptance criterion 4).

Plus negative receipt-tampering tests (bootstrap + non-bootstrap dry_run)
and one honest dry_run happy-path regression pinning the legal shape
(prospective has additions AND previous has models absent from prospective).

## Out of Scope

- #1096's own bootstrap sum invariant (already merged; different branch of
  the same validator).
- `_classify_registry` writer behavior — honest, untouched.
- Existing dry_run checks (package_changed/refused/declared == 0;
  added+unchanged+package_changed == prospective_count) — already correct.
- The #1098/#1099/#1101 large-file splits.
- The stale forward-reference in the merged capability spec
  (`openspec/specs/scheduler-registry-refresh/spec.md:115-117`, "tracked
  separately, out of scope here") — accepted as known doc drift; archiving
  this change adds the new requirement alongside it, and the governance
  doc-alignment stream (#368/#369 class) owns prose cleanup of merged
  specs.

## Impact

- Affected specs: `scheduler-registry-refresh` (ADDED requirement: dry_run
  receipt reconciliation shape).
- Affected code: `scripts/scheduler_file_provider_refresh.py`
  (`_enforce_registry_classification_reconciliation` dry_run branch, ~6-10
  lines), `tests/test_scheduler_file_provider_refresh.py` (appended tests
  following the `test_receipt_validator_rejects_bootstrap_receipt_*`
  pattern at `:3833+`).
- No sibling validator copies exist (repo grep: single definition, single
  test file).
