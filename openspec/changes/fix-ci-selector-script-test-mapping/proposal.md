# Mechanize scripts→tests same-name mapping in the CI selector (#1191)

## Why

`scripts/select_ci_tests.py` maps `scripts/**` changes to test files through
7 hand-added `PathTestRule` literals only. 21 existing script/test pairs
(`scripts/<name>.py` ↔ `tests/test_<name>.py`, ~1057 test functions, table
in issue #1191) have no rule: a PR that edits only such a script falls into
the `unknown_backend_python` fallback (`:408-412`, `_is_backend_python_path`
`:431-432` includes `"scripts/"`) and runs `CORE_SMOKE_TESTS` — 5 unrelated
files including the ~20-min heavyweights — with ZERO assertions over the
changed code. The convention "add a rule when you add a script" was never
mechanized; none of the 22 existing selector tests guard mapping
completeness. Verified repro: piping
`scripts/scheduler_state_index_copyback_replay.py` into the selector emits
the 5 CORE_SMOKE files, not its own 8-test suite.

## What Changes

1. `scripts/select_ci_tests.py`: a generic same-name mapping — a changed
   `scripts/**/*.py` whose `tests/test_<basename>.py` exists selects that
   test file, and a same-name hit counts as KNOWN (no CORE_SMOKE fallback
   for that path). Explicit `PathTestRule`s keep precedence/additivity;
   redundant `scripts/**` literals may be pruned only if behavior is
   byte-equal for their paths.
2. `tests/test_select_ci_tests.py`: (a) behavior cases for the new mapping
   (same-name selected; subdirectory script uses basename; script without a
   same-name test still falls back; no-regression case for the explicit
   DIFFERENT-named rule `scripts/validate_readonly_db_boundary.py` →
   `tests/test_readonly_db_validation.py`, currently uncovered by any test;
   scope guard: a non-`scripts/` backend path with a same-name test — e.g.
   `packages/common/state_qc.py` — still falls back to CORE_SMOKE, pinning
   that the derivation applies to `scripts/**/*.py` ONLY), (b) a mechanized
   completeness guard: every tracked `scripts/**/*.py` with an existing
   `tests/test_<basename>.py` must have `select_tests([that_path])` both
   include that test file AND share no member with `CORE_SMOKE_TESTS`
   (unless the same-name test itself is core-smoke — none today), so an
   implementation that appends the same-name test without marking the path
   known cannot pass while still dragging the ~20-min smoke set.

Spec: ADDED requirement in `ci-contract-baseline` pinning the same-name
selection contract.

## Non-goals

- #1182's empty-selection fallback policy (collect-only branch) — different
  axis, explicitly out of scope there and here.
- #1138's `scripts/**/*.sh` paths-filter blindness.
- Gate strength (full unit-test on PRs), CORE_SMOKE_TESTS membership/cost.
- No changes to any mapped script or to test content of mapped suites.
