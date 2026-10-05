# Tasks

Risk packs:
- Error handling / retry policy: selected -> 1.1, 1.2, 1.3.
- Legacy compatibility: selected - the #1161 / #1313 D2 "zero semantic change" line -> 1.2 (both sets unchanged), 1.3.
- Concurrency / shared state / ordering: not selected - a pure function of (candidate, state); no new state is written.
- Public API / CLI, Config, File IO, Schema, Auth: not selected - no such surface is touched.

## 1. Refresh-retry channel

- [x] 1.1 Reproduce first, on unmodified source: forecast-stage failure + forcing-input code
      (`FORCING_PACKAGE_CHECKSUM_MISMATCH`, `FORCING_CHECKSUM_READ_FAILED`, `SHUD_FORCING_CSV_MISSING`,
      `FORCING_FAILED`) + changed `run_manifest_model_package`; one group on the `None` lane and one on the strict
      warm-start lane. Expected red on the `None` lane: action `retry`,
      reason `retry_after_model_package_refresh`, `restart_stage == "forecast"`. Expected red on the strict lane:
      action `retry` and `restart_stage == "forecast"`; the reason may be `retry_after_model_package_refresh` or
      `strict_warm_start_retry_run_manifest_mismatch` (the escalator may rewrite it). Inputs: the forcing package
      is witnessed present (otherwise `_missing_forcing_block()` answers first); on the strict lane
      `run_manifest_model_package` only arrives through the journal reading the run manifest at
      `hydro_run.run_manifest_uri`, so seed a run manifest carrying the OLD model-package identity at that URI
      (the existing `_strict_forecast_failure_pass` helper asserts the manifest is absent and cannot trigger the
      channel as is). Record the red output.
- [x] 1.2 `_model_package_refresh_retry_evidence` returns `None` for that case; the candidate is `blocked` by the
      permanent-failure guard on both lanes. The two `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_*` sets are unchanged.
- [x] 1.3 Preserve tests: non-forcing-input forecast failure (`SHUD_FAILED`, `INVALID_MANIFEST`) with a changed
      package still refresh-retries at `forecast`; `failed_stage="forcing"` + `FORCING_FAILED` keeps its refresh retry with `restart_stage == "forcing"`;
      `forecast_run` / `analysis_run` aliases are refused like `forecast`; a top-level non-forcing `error_code`
      with a stale forcing code elsewhere in the state keeps the refresh retry; `DIRECT_GRID_TSD_FORC_TOO_LARGE` keeps today's decision; `OUT_OF_MEMORY` stays blocked.
- [x] 1.4 Sibling check, recorded in the PR: for each other pre-guard evidence channel that returns before the
      permanent guard in `scheduler_state_decision.py`, state whether a forcing-input forecast failure can leave it
      with a `forecast` restart. Report only; fix nothing outside this channel.
- [x] 1.5 Reachability note, recorded in the PR: when a readable prior run manifest exists at `run_manifest_uri`
      for a run whose forecast failed on forcing validation (read the code path that writes the run manifest and
      the one that reads it; no production access needed).

## 2. Verification

- [x] 2.1 `uv run ruff check .`; targeted pytest for the touched test files plus `tests/test_production_scheduler.py -k "model_package"`.
- [x] 2.2 `scripts/select_ci_tests.py` routes any new test file; `openspec validate refresh-retry-refuses-forcing-input-forecast-failure --strict --no-interactive`.
- [ ] 2.3 node-27: targeted pytest of the touched test files (no database; `TMPDIR=/home/nwm/tmp`).
