# Pin both-sides-None identity-field equality semantics with regression tests (#1093)

## Why

`_rows_have_identical_identity` (`scripts/scheduler_file_provider_refresh.py:2443-2460`)
is the classifier deciding `unchanged` vs `package_changed`. Its safe fallback
— "both sides None (flat) or both sides missing (nested) is identical" — rides
on two different mechanisms with zero test coverage:

- Flat fields (`REGISTRY_MODEL_IDENTITY_FIELDS`, `:119-129`): `row.get(field)
  != previous_row.get(field)` — both-missing and both-None collapse to
  `None == None`.
- Nested fields (`REGISTRY_MODEL_NESTED_IDENTITY_FIELDS`, `:135-137`):
  `_extract_nested_identity` (`:2428-2440`) returns a `_MISSING_IDENTITY`
  sentinel (`:2425`) on any path gap, deliberately distinguishing "missing
  key" from "explicit JSON null" (its docstring states the rationale).

No test in the repo calls `_rows_have_identical_identity` directly
(`grep -rn "_rows_have_identical_identity" tests/` → no hits, verified
2026-07-25), and every one of the 40+ `_registry_row` helper call sites fills
all identity fields with non-None values. Any refactor — truthiness compare
(`bool(a) != bool(b)`), None-as-missing conflation — silently changes
classification semantics with CI green. Notably a truthiness compare would
misclassify `0 vs None` / `"" vs None` as identical, masking real drift.

This is a test gap, not an implementation bug: current behavior is safe and
is the contract to pin.

## What Changes

- `tests/test_scheduler_file_provider_refresh.py` only: new parametrized unit
  tests calling `_rows_have_identical_identity` directly with minimal dicts
  (issue's recommended option — no `_registry_row` extension, keeping the
  shape-realistic helper single-purpose):
  1. flat identity field both sides `None` → `True`
  2. nested `resource_profile.source_inventory_checksum` both sides `None` →
     `True`
  3. both sides missing the top-level `resource_profile` key → `True`
     (sentinel == sentinel path)
  4. asymmetric guard: one side `None`, other side a **falsy non-None** value
     (`0` for `segment_count`, `""` for `lifecycle_state`) → `False` — falsy
     values chosen so the truthiness-mutation acceptance check actually goes
     red (a truthy value would pass under `bool()` compare too)
  5. asymmetric nested guard: one side missing `resource_profile`, other side
     explicit `None` value → `False` — pins the sentinel-vs-null distinction
     `_extract_nested_identity`'s docstring defends
- MANDATORY mutation-check (issue acceptance criterion 2): flat compare
  locally mutated to `bool(row.get(f)) != bool(previous_row.get(f))` must turn
  scenario 4 red; restore and record both outputs.

## Out of Scope

- Any change to `_rows_have_identical_identity`, `_extract_nested_identity`,
  or their semantics (unifying flat/nested paths is a separate design issue
  per the source issue).
- Extending `REGISTRY_MODEL_IDENTITY_FIELDS` / `REGISTRY_MODEL_NESTED_IDENTITY_FIELDS`.
- Extending the `_registry_row` helper (`tests/test_scheduler_file_provider_refresh.py:2630`)
  — minimal dicts are sufficient and keep identity-equality tests decoupled
  from the shape-realistic helper.

## Impact

- Affected specs: `scheduler-registry-refresh` (ADDED requirement pinning the
  None/missing equality semantics).
- Affected code: `tests/test_scheduler_file_provider_refresh.py` only; zero
  production-code change (issue acceptance criterion 5).
- `_registry_row` is file-local with no sibling copies (issue-verified grep).
