# Spec Delta

## Purpose
One definition per time or identity helper concept, with a ceiling test so duplicates cannot grow back.

## ADDED Requirements

### Requirement: Canonical helpers SHALL exist in the contract module
`packages/common/contracts/time_normalization.py` SHALL export `utc_now()`, `ensure_utc(dt)`, `parse_iso_utc(text)`, `format_iso_utc(dt)`, `parse_cycle_time(text)`, `format_cycle_time(dt)`; `packages/common/source_identity.py::normalize_source_id` SHALL remain the only source-id normalizer. Each function SHALL carry a docstring stating the accepted input forms and the exact output format.

#### Scenario: Round trip
- **WHEN** `parse_iso_utc(format_iso_utc(dt))` is evaluated for a timezone-aware `dt`
- **THEN** the result equals `ensure_utc(dt)` to the second

### Requirement: Local duplicates SHALL be replaced one top-level package at a time
The match rule is `re.fullmatch` of a module-level function name against `_?utc_?now|_?now_iso|_?iso_now|_?parse_(iso|utc|time|iso8601|iso_utc)|_?coerce_datetime|_?format_time|_?format_iso_utc|_?normalize_source(_id)?` (design D4 uses the same list). For each of `packages/`, `services/`, `workers/`, `apps/`, `scripts/`, every matching function SHALL be removed and its callers SHALL import the canonical helper, unless the local function has documented behaviour the canonical one does not (then it is either renamed to state that behaviour, which takes it out of the rule and needs no entry, or kept under its name and listed in `tests/time_helper_allowlist.json` as `{file, name, reason}`; allowlisted definitions are not counted). Existing test suites of the touched modules SHALL pass unchanged; tests that monkeypatch a removed local name SHALL be updated to patch the canonical name at its use site in the same PR.

#### Scenario: Batch lowers the count
- **WHEN** the `services/` batch merges
- **THEN** no module under `services/` defines a function matching the rule except allowlisted ones

### Requirement: A ceiling test SHALL forbid regrowth
`tests/test_time_helper_consolidation.py` SHALL count functions defined directly in a module body (`ast.Module.body`) whose name matches, excluding the files `packages/common/contracts/time_normalization.py` and `packages/common/source_identity.py` and allowlisted entries, over `apps/ packages/ services/ workers/ scripts/`, compare to `tests/time_helper_ceiling.json`, fail above the ceiling, and fail when the count is below the ceiling by more than 5 % (so each batch lowers the ceiling in its PR). The PR introducing the ceiling SHALL attach the hit list. The test SHALL be routed by `scripts/select_ci_tests.py` for any changed `*.py` under `apps/ packages/ services/ workers/ scripts/` through an additive loop in `select_tests` in the #1656/#2185 shape (not a `PATH_TEST_RULES` row, which a `stop_on_match` owner would shadow).

#### Scenario: New local helper fails
- **WHEN** a new `_utcnow` is defined in a non-canonical module without lowering another
- **THEN** the ceiling test fails
