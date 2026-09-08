## ADDED Requirements

### Requirement: National tile cache identity describes the data the tile SQL reads
The `source_version` digest that feeds the `hydro-national` discharge tile's cache key (`national_discharge_source_version`) SHALL, when called with the request's `valid_time`, rank each active river network's candidate runs under the same display-ready, active-instance, `(source, cycle)` and coverage-window (`river_valid_time_start <= valid_time <= river_valid_time_end`) predicates the tile SQL's `latest_runs` CTE applies, so the digest and the tile select the same run. Called without `valid_time` it SHALL keep answering each network's overall-latest question for the layer catalog. Both national tile routes SHALL pass their `valid_time`; the `/api/v1/layers` catalog call SHALL NOT.

Both national digests (`national_discharge_source_version` and `national_river_network_source_version`) SHALL include `core.river_network_version.geometry_generation` in their digest basis, and `_backfill_output_segment_geometry` — which holds the only `UPDATE core.river_segment` statement in production code (exactly one under `apps/`, `services/`, `workers/`, `packages/`, `scripts/`, pinned by a source-scan test that also pins the single `INSERT … ON CONFLICT DO UPDATE` on `core.river_segment`, `qhh_production_bootstrap.py::_seed_output_segment_rows`, which is outside this requirement: it rewrites `properties_json` only and is covered today solely by the trailing `_backfill_output_segment_geometry` call both of its callers run on the same cursor) — SHALL increment that network's `geometry_generation` in the same transaction, only when it actually updated at least one row. `NATIONAL_DISCHARGE_QUERY_VERSION` and `NATIONAL_RIVER_NETWORK_QUERY_VERSION` are not bumped by this requirement.

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
