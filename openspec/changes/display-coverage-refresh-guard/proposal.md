## Why

- #1446: `packages/common/display_coverage.py::refresh_run_display_coverage` (and `refresh_all_run_display_coverage`, same `_REFRESH_SQL` write point) scans `hydro.river_timeseries` by surrogate keys since #1341. Legacy runs whose key columns are all NULL scan to 0 segments, so a refresh silently overwrites a populated `hydro.run_display_coverage` row with `segment_count = 0` — destructive, and only a runbook prohibition (`current-production-ops.md:126-128`) stands between an operator and it. The pin test at `tests/test_river_ts_read_path_surrogate_keys_integration.py:739` currently locks the destructive behaviour in.
- #1725: `refresh_all_run_display_coverage` isolates per-run failures (`failed` counter) on two independent code paths — a list comprehension for `workers == 1` and `ThreadPoolExecutor.map` for `workers > 1` — and neither path has a regression test proving that one failing run leaves the others refreshed.

## What Changes

- #1446: the guard lives in the shared `_refresh` upsert itself — `_REFRESH_SQL` gains a `force` parameter and a conditional `DO UPDATE … WHERE force OR new_count > 0 OR existing_count = 0`, so a populated row is never zeroed by an empty key scan on any path (single-run, batch, all-runs); `_refresh` returns `RefreshOutcome(refreshed, refused)` with `refused = candidates − returned`, so a skip is distinguished from the existing non-candidate early return; single-run callers raise `DisplayCoverageRefreshRefused(run_id, existing_segment_count, advice)` when their run was skipped, the batch counts it; `force=True` (CLI `--force` on `scripts/node27_refresh_coverage.py`) performs the zeroing; the CLI reports a refusal as exit 3 with one structured stderr line instead of a traceback. First refreshes and genuinely empty runs (no row, or existing `segment_count = 0`) are unaffected. `refresh_all_run_display_coverage` counts refusals under a new additive `refused` key and never aborts the batch. The pin test flips to "guard holds"; the runbook prohibition (`current-production-ops.md:126-128`) becomes "guard-backed" and names the standing cost of a refused run (rescanned every cron tick until backfilled or forced).
- #1725: parametrized (`workers=1`, `workers=2`) isolation tests with N=3 runs where exactly one run's injected `connect` raises, asserting `{"refreshed": 2, "skipped": 0, "failed": 1, "refused": 0}`, commit/close on the survivors and rollback/close on the failure; red-proof by removing the `except Exception` arm recorded in the PR body.

## Capabilities

**Modified Capabilities**
- `display-coverage-freshness` — refusal guard with explicit force; per-run failure isolation across both worker paths.

## Impact

- Code: `packages/common/display_coverage.py` (guard, exception, `force` kwarg, `refused` counter), `scripts/node27_refresh_coverage.py` (`--force`).
- Tests: `tests/test_river_ts_read_path_surrogate_keys_integration.py` (real-DB, node-27 oracle), `tests/test_display_coverage_parallel.py` (`:54` exact-dict assertion rewritten), `tests/test_display_coverage_refresh.py` (`:82,:101,:104` statement-sequence pins updated to the new `_REFRESH_SQL` shape), CLI tests for the refusal exit code/line.
- Docs: `docs/runbooks/current-production-ops.md:126-128`, the CLI docstring.
- Unchanged: `scripts/node27_autopipe_cron.sh:229-230` (`--all --skip-fresh` loop), `scripts/node27_autopipeline.py` (consumes neither the dict nor the new exit code as fatal), #1719 attribution injection.
