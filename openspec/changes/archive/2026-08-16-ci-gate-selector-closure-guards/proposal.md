# Proposal: ci-gate-selector-closure-guards

## Why

The `select_ci_tests.py` mapping family has now failed four times
(#1191 → #1247 → #1283 → #1447): a `PathTestRule` is written (or omitted)
without a "who imports this module" closure scan, the PR-lane `Unit Tests`
job goes green on an incomplete selection, and the regression only surfaces
on the post-merge master full run. Two adjacent CI-gate defects share the
delivery window: the `database` paths-filter in `ci.yml` carries a dead
pattern for a file deleted in `b97c16e2` (#1362), and the master full-run
`Unit Tests (full)` job is structurally red because the entropy hard gate
flags a prose comment in `workers/shud_runtime/runtime.py:973` that names a
QHH diagnostic token (#1372).

## What Changes

One PR closing five local-only issues:

- **#1447**: add a `PathTestRule` for `packages/common/display_coverage.py`
  covering its non-gated top-level importer suites
  (`tests/test_display_coverage_refresh.py`,
  `tests/test_display_coverage_parallel.py`, `tests/test_forecast_api.py`),
  with a deliberate, commented exclusion of the single `integration`-marked
  importer suite that exists on master
  (`tests/test_display_coverage_residual_debt_integration.py`; the sibling
  `tests/test_river_ts_read_path_surrogate_keys_integration.py` named by the
  issue exists only on unmerged PR #1443's branch — the rule comment must
  name only files present in the tree). Note: master has NO rule for this
  module today (the incomplete
  rule and the false `packages/common/**` comment live on unmerged PR #1443);
  on master the module silently falls through to the CORE_SMOKE fallback,
  which never imports it.
- **#1283**: extend the `services/slurm_gateway/**` rule with
  `tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py`,
  `tests/test_job_array.py` (310 currently-unselected cases, including the
  sbatch injection-defense assertions).
- **Mechanical guard against a 5th recurrence** (#1447/#1283 shared): a
  traversal-based selector test that derives, from the tracked tree, the
  non-gated top-level importer test set for each guarded module
  (`packages.common.display_coverage`,
  `services.slurm_gateway.real_backend`) and asserts the corresponding rule
  selects every member — new importer suites redden the guard instead of
  silently falling out of the PR lane.
- **#1254**: the changed-test early-continue branch additionally selects
  `tests/test_select_ci_tests.py` for any changed `tests/test_*.py`, so the
  tree-derived meta-guards (#1191/#1247 and the new ones above) run exactly
  on the PR class that can invalidate them. Self-selection and the existing
  orchestrator/file-journal redirect semantics are preserved.
- **#1362**: delete the dead `tests/test_worker_chain_smoke.py` line from
  the `database` paths-filter in `.github/workflows/ci.yml`; audit that
  every remaining `database` pattern expands to ≥1 tracked file.
- **#1372**: reword the `runtime.py:973` comment so it no longer contains
  the literal token `create_qhh_shud_manifest` (or the script path form);
  zero executable-logic change; entropy hard gate returns to `pass`.

Non-goals:

- No change to `packages/common/display_coverage.py`,
  `services/slurm_gateway/real_backend.py`, or any production module logic
  (the #1372 comment rewording in `runtime.py` is prose-only).
- No change to `ci.yml` job trigger conditions or the draft/ready gating.
- No collect-only/empty-selection policy change (#1182 lane, closed).
- No `scripts/**/*.sh` filter coverage change (#1138).
- No generic auto-derived closure guard for every directory rule: the
  sibling audit for other `services/**`/`workers/**` rules is documented in
  the PR body (fix or recorded deliberate-exclusion reason), not mechanized
  wholesale — the broad scan is noisy (e2e/integration hits).

## Capabilities

### Modified Capabilities

- `ci-contract-baseline`: adds importer-closure coverage requirements for
  guarded selector modules, meta-guard selection on changed-test PRs, a
  no-dead-filter-pattern rule for `ci.yml` paths-filters, and a green
  entropy hard gate on master without checker weakening.

## Impact

- `scripts/select_ci_tests.py` (rules + changed-test branch)
- `tests/test_select_ci_tests.py` (traversal guards + expectation updates)
- `.github/workflows/ci.yml` (one deleted dead filter line)
- `workers/shud_runtime/runtime.py` (comment prose only)
