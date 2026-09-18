## MODIFIED Requirements

### Requirement: National tile cache identity describes the data the tile SQL reads
The `source_version` digest that feeds the `hydro-national` discharge tile's cache key (`national_discharge_source_version`) SHALL, when called with the request's `valid_time`, rank each active river network's candidate runs under the same display-ready, active-instance, `(source, cycle)` and coverage-window (`river_valid_time_start <= valid_time <= river_valid_time_end`) predicates the tile SQL's `latest_runs` CTE applies, so the digest and the tile select the same run. Called without `valid_time` it SHALL keep answering each network's overall-latest question for the layer catalog. Both national tile routes SHALL pass their `valid_time`; the `/api/v1/layers` catalog call SHALL NOT.

Both national digests (`national_discharge_source_version` and `national_river_network_source_version`) SHALL include `core.river_network_version.geometry_generation` in their digest basis, and `_backfill_output_segment_geometry` — which holds the only `UPDATE core.river_segment` statement in production code (exactly one under `apps/`, `services/`, `workers/`, `packages/`, `scripts/` and `db/` — `.py` files by their string literals, `db/**/*.sql` by statement text — pinned by a source-scan test that also pins the single `INSERT … ON CONFLICT DO UPDATE` on `core.river_segment`, `qhh_production_bootstrap.py::_seed_output_segment_rows`, which is outside this requirement: it rewrites `properties_json` only and is covered today solely by the trailing `_backfill_output_segment_geometry` call both of its callers run on the same cursor) — SHALL increment that network's `geometry_generation` in the same transaction, only when it actually updated at least one row. That increment SHALL be the only write of `geometry_generation` anywhere in that scanned tree, whatever its spelling (`= geometry_generation + 1`, a constant, another expression, or the column named in an `INSERT` column list); the `ADD COLUMN` of `db/migrations/000057_river_network_version_geometry_generation.sql` is the one named exception, and reads of the column are not writes. `NATIONAL_DISCHARGE_QUERY_VERSION` and `NATIONAL_RIVER_NETWORK_QUERY_VERSION` are not bumped by this requirement.

#### Scenario: Digest ranks the run the tile reads at the requested instant
- **WHEN** one network has two display-ready `segment_count > 0` runs at the same `(gfs, cycle)`, the run with the greater `run_id` covering `[cycle, cycle+1h]` and the other covering `[cycle, cycle+2h]`
- **THEN** `national_discharge_source_version(session, source="gfs", cycle=cycle, valid_time=cycle+2h)` equals the value it had before the second run existed
- **AND** `national_discharge_source_version(session, source="gfs", cycle=cycle, valid_time=cycle+1h)` differs from the value it had before the second run existed
- **AND** the tile at `cycle+2h` still returns 200 with the same bytes as before the second run existed

#### Scenario: Unbound digest keeps the overall-latest question
- **WHEN** the same second run is present
- **THEN** `national_discharge_source_version(session)` differs from its pre-rival value
- **AND** the `/api/v1/layers` catalog call passes `source`/`cycle` only, never `valid_time`

#### Scenario: Geometry backfill that updates rows rotates both national digests
- **WHEN** `_backfill_output_segment_geometry` updates at least one `shud_output_river` row of a network
- **THEN** `core.river_network_version.geometry_generation` for that network increments by one in the same transaction
- **AND** both `national_discharge_source_version(session)` and `national_river_network_source_version(session)` differ from their values before the backfill

#### Scenario: Geometry backfill that updates no row leaves the digests unchanged
- **WHEN** `_backfill_output_segment_geometry(..., only_missing=True)` runs on a network whose output rows already carry geometry and `Type`, or its candidate update set is emptied by the `ST_Length > 0` filter
- **THEN** it returns 0, issues no `geometry_generation` update, and both national digests are byte-identical to their values before the call

#### Scenario: Legacy alias keeps bytes and verdict
- **WHEN** the legacy `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` route is called
- **THEN** it passes `valid_time` (and NULL `source`/`cycle`) to the digest and its response bytes and 200/424 verdict are unchanged from before this requirement; only its cache key may rotate once for instants outside the overall-latest run's window

## ADDED Requirements

### Requirement: Per-basin and run-scoped tile identity sees in-place geometry rewrites

The per-basin `river-network` tile's `source_version` (`_river_network_source_version`) SHALL digest, for every `core.river_network_version` row of the requested `basin_version_id`, its `river_network_version_id`, `segment_count`, `checksum` and `geometry_generation`. The run-scoped `hydro` tile's `source_version` (`_run_source_version`) SHALL include the run's network `geometry_generation` in its revision basis, read through `LEFT JOIN core.river_network_version` in BOTH run readers that feed it — `_run_row` (tile routes and `/api/v1/layers?run_id=`) and `display_ready_run` (the `/api/v1/layers` default) — so the catalog and the tile route compute the same value for the same run. The one-time cache-key rotation this causes on deploy is expected. National digests are not changed by this requirement.

#### Scenario: Geometry backfill rotates the per-basin and run-scoped identities
- **WHEN** `_backfill_output_segment_geometry` updates at least one row of the network a basin version and a display-ready run point at
- **THEN** `_river_network_source_version(session, basin_version_id)` and `_run_source_version(run)` both differ from their values before the backfill
- **AND** the `river-network` and `hydro` tile responses for the same `z/x/y` carry a different `X-Tile-Cache-Key` and the new geometry's bytes

#### Scenario: No-op backfill leaves both identities unchanged
- **WHEN** `_backfill_output_segment_geometry(..., only_missing=True)` returns 0
- **THEN** both values are byte-identical to their values before the call

#### Scenario: Inventory refresh rotates the per-basin identity
- **WHEN** a network's `segment_count` or `checksum` changes under an unchanged `river_network_version_id`
- **THEN** `_river_network_source_version` for its basin version changes

#### Scenario: Catalog and tile route agree on the run identity
- **WHEN** the same display-ready run is read through `display_ready_run` and through `_run_row`
- **THEN** `_run_source_version` of both rows is equal

### Requirement: River-segment geometry writers lock the parent network row first

Every production path that rewrites EXISTING `core.river_segment` rows in place — the `UPDATE` in `_backfill_output_segment_geometry` and the `ON CONFLICT DO UPDATE` upsert in `qhh_production_bootstrap.py::_seed_output_segment_rows` — SHALL hold the network's `core.river_network_version` row lock (`SELECT … FOR NO KEY UPDATE`) before its first statement that touches those rows, so every in-place writer acquires parent then children. `_backfill_output_segment_geometry` SHALL take it as its first statement, before reading candidates, and when it then finds nothing to update it SHALL return 0 and bump nothing; both entry points of the upsert (`seed_qhh_output_segments` and the bootstrap path) SHALL take it before the upsert. Row-level `INSERT` of new segments and the deletion of legacy `<model>_seg_*` rows are outside this requirement: they never take a lock on a row either in-place writer targets.

#### Scenario: Import transaction and a concurrent backfill serialize instead of deadlocking
- **WHEN** session A holds the network row lock through `UPDATE core.river_network_version`, session B then calls `_backfill_output_segment_geometry` with at least one fillable row of that network, and A then updates those segment rows and commits
- **THEN** B waits on the parent row lock without holding any segment row lock, and both transactions commit with no `deadlock detected`

#### Scenario: The autopipeline backfill and the seed serialize on the parent row
- **WHEN** session A runs `_backfill_output_segment_geometry(..., only_missing=True)` with fillable rows of a network and has not committed, and session B runs `seed_qhh_output_segments` on the same network
- **THEN** B waits on the parent row lock before its upsert, and both transactions commit with no `deadlock detected`

#### Scenario: A geometry-complete tick locks the parent row and bumps nothing
- **WHEN** `_backfill_output_segment_geometry(..., only_missing=True)` finds nothing to update
- **THEN** its first statement is the parent-row `FOR NO KEY UPDATE`, it returns 0, and it issues no `geometry_generation` update

### Requirement: The output-segment upsert cannot commit a silently erased stream type

Both entry points that run `qhh_production_bootstrap.py::_seed_output_segment_rows` (whose `ON CONFLICT DO UPDATE` rewrites `properties_json` and so erases the STORED `stream_type`) SHALL, after their trailing `_backfill_output_segment_geometry` call on the same cursor, fail with `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE` and roll the transaction back when any `shud_output_river` row of the network lacks a `Type` while its matching source reach (same network, not an output row, `iRiv` equal to the row's `shud_riv_index`, non-NULL geometry) carries one.

`_backfill_output_segment_geometry(..., only_missing=False)`, which both entry points run, rewrites and bumps even when the values are unchanged; this over-rotation — one extra cold miss per network for each operator-run bootstrap or seed — is accepted, because the seed path must keep rewriting unconditionally. Row-level `INSERT`/`DELETE` of river segments and imports run with `backfill_output_segment_geometry=False` are outside the `geometry_generation` counter: they refresh the network's `segment_count`/`checksum`, which the per-basin and national river-network digests include; the run-painted `hydro`/`hydro-national` layers are driven by run identity and are not rotated by those row-level paths.

#### Scenario: Trailing backfill that restores nothing fails the seed closed
- **WHEN** a network's output rows carry `Type` from an earlier backfill, their source reach geometry is degenerate so the trailing backfill updates 0 rows, and `seed_qhh_output_segments` runs
- **THEN** it raises `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE`, and afterwards every output row still carries its `Type` and `geometry_generation` is unchanged

#### Scenario: A healthy seed passes
- **WHEN** every output row whose source reach has a `Type` carries it after the trailing backfill
- **THEN** the seed completes and reports its counts as before
