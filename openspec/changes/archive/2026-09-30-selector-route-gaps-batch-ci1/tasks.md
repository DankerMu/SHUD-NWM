# Tasks — selector-route-gaps-batch-ci1

Fixture level: standard. Risk pack: ci-gate (PR-lane coverage). Local-only oracle; the selector and meta-suite need no DB.

## Must preserve
- Only `scripts/select_ci_tests.py` and `tests/test_select_ci_tests.py` change, plus this change directory.
- Existing selections for the owners do not lose any suite.
- Expected, intended selection changes, which must be listed in the PR:
  - every module that falls to the `services/orchestrator/**` broad rule gains the two preservation halves, so `services/orchestrator/retry.py`'s count grows by 2, deliberately overriding #2568's "count unchanged" wording;
  - every `model_registry_*.py` owner gains the model_registry importer set (facade parity).
- Any other owner's selection is unchanged.
- `integration`/`e2e`-gated suites stay out of the PR lane (#1447 semantics).

## 1. Baseline
- [x] 1.1 Record the before-selection for every owner path in design.md (count plus list) under `.workplans/ci1/`.

## 2. Routes
- [x] 2.0 D0: `DIRECT_ONLY_GUARDED_MODULES` plus guard branch; len pin 6→9; stale comments refreshed.
- [x] 2.1 D1: `pipeline.py` → `tests/test_retry.py`; `pipeline.py` in `GUARDED_MODULE_CLOSURES` and in `DIRECT_ONLY_GUARDED_MODULES`; the precip peer-pin conflict resolved as design D1 describes.
- [x] 2.2 D2: glob rule `infra/sbatch/*.sbatch`; per-template ≥1 suite; `run_qhh_cycle.sbatch` and the autopipe timer unchanged.
- [x] 2.3 D3: exact rules for `station_set_flip.py` and `model_registry.py` (the shared tuple includes both flip parts); both in `GUARDED_MODULE_CLOSURES` with anchors; fn-gated dispositions recorded.
- [x] 2.4 D4: five owners select both preservation halves; broad-orchestrator pin literal updated with measured wall time; `RESPONSE_MODEL_ORACLE_INDIRECT_BUILDERS` and single-leg-removal extended; meta-guard docstring narrowed.
- [x] 2.5 D5: `forecast_store.py` → `tests/test_river_ts_stats_harness_offline.py`, pinned.

## 3. Verification
- [x] 3.1 After-selection table; mutation-red evidence per new leg.
- [x] 3.2 `uv run pytest -q tests/test_select_ci_tests.py` green; `uv run ruff check .`; added suites green locally with wall time.
- [x] 3.3 `openspec validate selector-route-gaps-batch-ci1 --strict --no-interactive`.

## Evidence Floor
- Every acceptance command in #2568 #2575 #2576 #2612 #2624 produces the expected selection.
- Each new rule leg has a pin that turns red when the leg is removed.
- `tests/test_select_ci_tests.py` passes in full; ruff is clean.
