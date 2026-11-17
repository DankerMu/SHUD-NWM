# Proposal — selector-route-shud-preflight (#3167)

Closes #3167.

## Why

`scripts/select_ci_tests_rules.py` has no rule that routes `packages/common/shud_preflight.py` to the suites that assert
its behaviour, and there is no same-named `tests/test_shud_preflight.py`. A PR that changes only that module gets core
smoke plus the canonicalisation family guard (`tests/test_select_ci_tests.py:15134`), and misses:
- `tests/test_shud_runtime.py`, the `test_shared_preflight_*` family (realpath, stub, identity, silent/inconclusive
  cases);
- `tests/test_production_slurm_validation.py`, the stub-rejection cases through
  `services/production_closure/slurm_validation.py`, a caller of `check_shud_executable`.

The module's only importers are production modules (`workers/shud_runtime/runtime.py:44`,
`services/orchestrator/scheduler_preflight.py:13`, `services/production_closure/slurm_validation.py:31`). No test
imports it at module scope (`tests/test_production_scheduler.py:47446` imports it inside a function).

## What

1. Add a `PathTestRule` for `packages/common/shud_preflight.py` →
   `("tests/test_shud_runtime.py", "tests/test_production_slurm_validation.py")` without changing the existing family
   guard routing.
2. Pin it in `tests/test_select_ci_tests.py`: a routing test that is red before the rule is added.
3. **`GUARDED_MODULE_CLOSURES` decision: the module does not join.** It cannot join structurally:
   - for every entry, the guard first asserts that the derived direct importer set is non-empty and holds the
     anchor (`tests/test_select_ci_tests.py:6491-6495`), and only then branches on mode (`:6496`). Both the full mode
     and the direct-only mode go through that check;
   - every test that imports `shud_preflight` does so inside a function (`tests/test_shud_runtime.py` from ~:6940,
     `tests/test_production_slurm_validation.py:3263,3282`, `tests/test_production_scheduler.py:47446`). The direct set
     is empty, so either mode would fail the guard;
   - precedent: the four `hydro_display_*` owner modules stay out of the registry for the same reason
     (`tests/test_select_ci_tests.py:6215-6221`).

   The one-hop importer count is measured with the guard's own helpers and recorded for information only; it is not a
   criterion. Residual risk, recorded and not addressed (YAGNI): the importer derivations read only module-scope
   imports (~:6390-6393), so a future suite that imports the module inside a function stays invisible to any guard.
4. Spec: ADDED requirement in `ci-contract-baseline`.

## Non-goals

- No change to `shud_preflight.py` or its callers.
- No new `tests/test_shud_preflight.py`.

## Must preserve

- Every existing selection for `packages/common/shud_preflight.py`: core smoke (`scripts/select_ci_tests.py:466-467`,
  required by `openspec/specs/ci-contract-baseline/spec.md:726`; it carries `tests/test_production_scheduler.py`, whose
  preflight cases cover the `scheduler_preflight` caller) and the additive guards (family guard, resolve-surface guard,
  `scripts/select_ci_tests.py:504-507`). The new selection is a superset of the old one.
- The existing selector pins stay green, unless a pin that compares an exact selection set needs the two new suites
  added; each such pin is listed in the PR.

## Fixture level

minimal. Risk pack: CI routing (test selection).

## Evidence Floor

- [x] E1 `uv run ruff check --no-cache .`; `git diff --check`.
- [x] E2 `uv run pytest -q tests/test_select_ci_tests.py` passes, and the new routing pin is red before the rule is added.
- [x] E3 `uv run python scripts/select_ci_tests.py --changed-file <file containing packages/common/shud_preflight.py>`
  lists `tests/test_shud_runtime.py` and `tests/test_production_slurm_validation.py`, and its selection is a superset of
  the `origin/master` selection for the same input. The routing pin also asserts `CORE_SMOKE_TESTS ⊆ selection`.
- [x] E4 The "does not join `GUARDED_MODULE_CLOSURES`" decision is recorded, citing the structural reason, the
  precedent and the measured one-hop count (for information only).
- [x] E5 `openspec validate selector-route-shud-preflight --strict --no-interactive`.
