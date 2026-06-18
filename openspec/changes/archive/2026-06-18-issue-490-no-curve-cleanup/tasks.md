# Tasks

## Implementation

- [x] Add cleanup command/script with dry-run default and explicit apply mode.
- [x] Implement filters for `run_id`, `basin_version_id`, `source_id`, and
  `cycle_time` range.
- [x] Implement manifest generation with aggregate counts, quality coverage,
  affected runs, size statistics, and resumable batch cursor fields.
- [x] Implement bounded apply deletion with per-batch commit records and optional
  sleep interval.
- [x] Add non-overridable apply guardrail for missing
  `flood.run_product_quality` summaries.
- [x] Implement stable row-identity batching by `(run_id,
  river_network_version_id, river_segment_id, duration, valid_time,
  max_over_window)` and recheck the no-curve predicate plus filters at delete
  time.
- [x] Implement graceful fallback when Timescale chunk metadata is unavailable.
- [x] Ensure manifest/log output redacts DB credentials.
- [x] Keep index/vacuum/repack/schema DDL out of this change.

## Tests / Evidence

- [x] Unit test: dry-run emits manifest and does not delete candidates.
- [x] Unit test: filters scope candidate rows consistently across summary and
  delete paths.
- [x] Unit test: apply deletes only rows matching the no-curve null predicate and
  preserves rows with return-period or warning data.
- [x] Unit test: missing explicit quality blocks apply before deletion and has
  no force override.
- [x] Unit test: batching records per-batch deleted rows and resumable cursor
  using the stable row-identity key tuple.
- [x] Unit test: manifest output path is safe/no-clobber or explicit overwrite is
  required.
- [x] Unit test: database URL/password is redacted from manifest/errors.
- [x] Unit test: Timescale chunk metadata absence does not fail manifest
  generation and records chunk distribution as unavailable.
- [x] Static/unit test: cleanup implementation contains no `DROP INDEX`,
  `REINDEX`, `VACUUM FULL`, object-store deletion, or `hydro.river_timeseries`
  delete path.
- [x] Unit test or CLI help assertion: operator notes state DELETE does not
  immediately reclaim disk and #491 owns index/vacuum/repack work.
- [x] Unit test: summary queries do not materialize every candidate row in
  Python; apply reads at most `batch_size` row identities per batch.
- [x] Regression: affected run `run_product_quality` remains present and
  unavailable after cleanup.
- [x] Existing API regression commands for #489 remain passing or are cited as
  unchanged evidence.

## Verification Commands

- [x] `uv run --no-sync pytest -q tests/test_flood_frequency.py tests/test_return_period.py tests/test_return_period_cleanup.py tests/test_select_ci_tests.py`
- [x] `uv run --no-sync ruff check workers/flood_frequency/return_period_cleanup.py workers/flood_frequency/cli.py scripts/select_ci_tests.py tests/test_return_period_cleanup.py tests/test_select_ci_tests.py`
- [x] `openspec validate issue-490-no-curve-cleanup --strict --no-interactive`
- [x] API/frontend contracts untouched; #489 API/OpenAPI regression not required
  for this ops-only PR.
