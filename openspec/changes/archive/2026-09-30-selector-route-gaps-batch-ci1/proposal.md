# selector-route-gaps-batch-ci1

## Why

A single-file change to any of the owners below makes `scripts/select_ci_tests.py` skip suites that directly assert that owner's behaviour. The PR's targeted "Unit Tests" lane stays green, and a regression only turns red in the post-merge full run on master. Measured on master 86e6a17d5 with `echo <path> | uv run python scripts/select_ci_tests.py`:

- **#2568:** `apps/api/routes/pipeline.py` (15 selected) does not select `tests/test_retry.py`, which holds the only HTTP-level oracle for `POST /runs/{run_id}/retry`.
- **#2576:** `pipeline.py` is in neither `GUARDED_MODULE_CLOSURES` nor `DIRECTORY_RULE_AUDIT_PATHS`, so a new importer of it silently falls out of the PR lane.
- **#2575:** every `infra/sbatch/*.sbatch` template selects 0 tests, so the lane degrades to a zero-assertion `--collect-only` run.
- **#2612:** `packages/common/station_set_flip.py` (9 selected) and `packages/common/model_registry.py` (13 selected) miss their direct non-gated importer suites. For `station_set_flip.py` these include both parts of the cutover-flip split and `test_direct_grid_display_cutover_history`. For `model_registry.py`, 13 of its 14 direct importers are missed.
- **#2624:** `services/orchestrator/{persistence,retry,public_evidence}.py` and `packages/common/{redaction,source_identity}.py` do not select the response-model preservation oracle (`tests/test_response_model_preservation{,_pipeline}.py`).
- **Batch-plan extra (no issue):** `packages/common/forecast_store.py` (36 selected) does not select `tests/test_river_ts_stats_harness_offline.py`, a non-gated suite that imports `forecast_store` at module level.

## What Changes

Add the missing routes and pin each one in `tests/test_select_ci_tests.py`. Put `pipeline.py`, `station_set_flip.py` and `model_registry.py` under a mechanical closure guard. No other files change.

## Impact

- Files: `scripts/select_ci_tests.py`, `tests/test_select_ci_tests.py`, and this change.
- The PR lane grows for these owners. The growth is measured and recorded in pins and the PR body.
- Runtime behaviour is untouched.
