# Tasks

## Implementation

- [ ] Add cleanup command/script with dry-run default and explicit apply mode.
- [ ] Implement filters for `run_id`, `basin_version_id`, `source_id`, and
  `cycle_time` range.
- [ ] Implement manifest generation with aggregate counts, quality coverage,
  affected runs, size statistics, and resumable batch cursor fields.
- [ ] Implement bounded apply deletion with per-batch commit records and optional
  sleep interval.
- [ ] Add non-overridable apply guardrail for missing
  `flood.run_product_quality` summaries.
- [ ] Implement stable row-identity batching by `(run_id,
  river_network_version_id, river_segment_id, duration, valid_time,
  max_over_window)` and recheck the no-curve predicate plus filters at delete
  time.
- [ ] Implement graceful fallback when Timescale chunk metadata is unavailable.
- [ ] Ensure manifest/log output redacts DB credentials.
- [ ] Keep index/vacuum/repack/schema DDL out of this change.

## Tests / Evidence

- [ ] Unit test: dry-run emits manifest and does not delete candidates.
- [ ] Unit test: filters scope candidate rows consistently across summary and
  delete paths.
- [ ] Unit test: apply deletes only rows matching the no-curve null predicate and
  preserves rows with return-period or warning data.
- [ ] Unit test: missing explicit quality blocks apply before deletion and has
  no force override.
- [ ] Unit test: batching records per-batch deleted rows and resumable cursor
  using the stable row-identity key tuple.
- [ ] Unit test: manifest output path is safe/no-clobber or explicit overwrite is
  required.
- [ ] Unit test: database URL/password is redacted from manifest/errors.
- [ ] Unit test: Timescale chunk metadata absence does not fail manifest
  generation and records chunk distribution as unavailable.
- [ ] Static/unit test: cleanup implementation contains no `DROP INDEX`,
  `REINDEX`, `VACUUM FULL`, object-store deletion, or `hydro.river_timeseries`
  delete path.
- [ ] Unit test or CLI help assertion: operator notes state DELETE does not
  immediately reclaim disk and #491 owns index/vacuum/repack work.
- [ ] Unit test: summary queries do not materialize every candidate row in
  Python; apply reads at most `batch_size` row identities per batch.
- [ ] Regression: affected run `run_product_quality` remains present and
  unavailable after cleanup.
- [ ] Existing API regression commands for #489 remain passing or are cited as
  unchanged evidence.

## Verification Commands

- [ ] `uv run --no-sync pytest -q <new cleanup tests>`
- [ ] `uv run --no-sync ruff check <changed python files>`
- [ ] `openspec validate issue-490-no-curve-cleanup --strict --no-interactive`
- [ ] If frontend/API contracts are touched: run the relevant #489 API/OpenAPI
  regression tests.
