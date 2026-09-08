## Why

The `hydro-national` discharge tile's cache key is driven by `national_discharge_source_version`, but
that digest ranks runs over a wider domain than the tile SQL's `latest_runs` CTE (no coverage-window
predicate) and carries no river-network geometry identity at all, while neither `map.tile_cache` nor
the file cache expires. Since PR #2027 pinned a `(source, cycle)` identity's digest to that cycle's run
set, a divergence no longer self-heals: the user keeps an old tile until someone rotates a query version
by hand. node-27 measurement (`docs/runbooks/receipts/2026-09-08-issue-2031-digest-precondition.md`)
shows the run-side precondition is structurally reachable today (20 double-run identity groups via a
retired direct-grid variant on `basins_lh_ylj`; legacy-route divergence on all 38 networks) and the
geometry-side path is latent (fires on the next bootstrap/re-import). Issue #2031.

## What Changes

- `national_discharge_source_version` gains a keyword-only optional `valid_time`; when bound, its ranked
  sub-query applies the same coverage-window predicate the tile's `latest_runs` CTE applies, so digest
  and tile rank the same run. Both national tile routes pass their `valid_time`; the layer catalog call
  stays without it (it has no instant). Unbound, the digest keeps answering the overall-latest question.
- New migration `db/migrations/000057_river_network_version_geometry_generation.sql`:
  `core.river_network_version.geometry_generation INTEGER NOT NULL DEFAULT 0`.
- `_backfill_output_segment_geometry` increments `geometry_generation` for the network **only when it
  actually updated ≥1 row**, in the same transaction.
- Both national digests (`national_discharge_source_version`, `national_river_network_source_version`)
  project `geometry_generation`, so an in-place geometry/`stream_type` backfill rotates the discharge and
  river-network national cache keys. The projection change rotates every national key once on deploy;
  `NATIONAL_DISCHARGE_QUERY_VERSION` / `NATIONAL_RIVER_NETWORK_QUERY_VERSION` are deliberately untouched
  (both literals are pinned by the sibling change's spec and tests).
- Ruling and measurement written back to `invariant-matrix-i4-2007.md` (appendix) as the issue requires.
- **Deploy order constraint (documented, not executed here)**: `000057` must be applied on node-27 before
  the display API that references the column is restarted. `ADD COLUMN IF NOT EXISTS … DEFAULT 0` is safe
  to apply ahead of code.

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `mvt-tile-contract`: ADDED requirement — the national tile cache identity SHALL describe the run and
  the network geometry the tile SQL actually reads (valid_time-aware ranking; geometry generation in both
  national digests; zero-row backfill does not rotate).

## Impact

- `services/tiles/mvt.py` (`national_discharge_source_version`, `national_river_network_source_version`)
- `apps/api/routes/hydro_display.py` (two national tile routes pass `valid_time`; catalog call unchanged)
- `workers/model_registry/basins_registry_import.py` (`_backfill_output_segment_geometry`)
- `db/migrations/000057_river_network_version_geometry_generation.sql` (trips the CI `database` filter →
  "SQL Migration Dry Run" runs; open the PR non-draft)
- Tests: `tests/test_hydro_display_mvt_scaling.py`, `tests/test_hhe_mvt_binding.py`,
  `tests/test_mvt_national_identity_probe_integration.py` (node-27 real DB), migration test
- Not touched: `postgis_tile_sql` run selection, 424 probe, `(source, cycle)` binding, frontend, cache
  TTL/purge, `national_discharge_valid_times`.
