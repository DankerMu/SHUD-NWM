# mvt-tile-contract Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.

## Requirements

### Requirement: MVT tile API contract
The backend SHALL expose hydrology vector tile endpoints with `application/x-protobuf`, stable layer IDs, bounded z/x/y parameters, and documented feature properties. The hydrology `discharge` layer SHALL surface the **national** tile endpoint as its canonical URL in the public `/api/v1/layers` catalog. The single-run hydro endpoint remains a supported direct-deeplink route but is NOT a canonical layer URL. The internal `_layer_source_refs` helper SHALL NEVER be reached for `layer_id == "discharge"` — the call site in `layer_metadata` short-circuits to `source_refs={}` whenever `national_discharge=True`, and the helper itself MUST guard the invariant at its entry boundary so any future refactor that wires discharge back through this path fails loudly at development/CI time rather than silently re-introducing run_id into the ETag hash input.

#### Scenario: Canonical endpoint disposition
WHEN M16 is implemented
THEN `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (canonical discharge layer URL), and `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (direct-deeplink only, NOT exposed via the `/api/v1/layers` `discharge` entry) have explicit OpenAPI/runtime behavior

#### Scenario: Tile success
WHEN a published layer/run/valid_time has features in a tile
THEN endpoint returns PBF with required properties and cacheable headers

#### Scenario: Invalid tile
WHEN z/x/y or query parameters are out of bounds
THEN endpoint returns stable validation error without running expensive SQL

#### Scenario: Contract freshness
WHEN the public tile contract changes
THEN OpenAPI, generated frontend API types, and drift allowlists are updated together or the unchanged legacy path remains explicitly documented

#### Scenario: Stable feature properties
WHEN a hydrology MVT feature is encoded
THEN properties include stable segment/network/source/time/value metadata and reject missing or non-finite required values

#### Scenario: Layer metadata discovery
WHEN frontend requests MVT-capable layer metadata
THEN metadata includes `layer_id`, `tile_format`, URL template placeholders, MapLibre source-layer id, property schema/version, min/max zoom, Web Mercator bounds, valid_time/source references, cache etag/version, and fallback/release-blocking flags

#### Scenario: Discharge canonical URL is national across all callers
WHEN `/api/v1/layers` is called with OR without a `run_id` query parameter
THEN the `discharge` entry's `tile_url_template` MUST be `/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf` AND MUST NOT contain a `{run_id}` placeholder
AND the single-run `/api/v1/tiles/hydro/{run_id}/q_down/...` route continues to serve direct GET requests but MUST NOT appear in the canonical catalog's discharge entry (see `overview-data-contracts` Requirement *Default discharge tile URL is national across all `/api/v1/layers` callers* for full scenarios)

#### Scenario: Discharge layer never reaches `_layer_source_refs`
WHEN `_layer_source_refs(layer_id, ...)` is invoked in `services/tiles/mvt.py`
THEN `layer_id` MUST NOT equal `"discharge"` — the function MUST raise an assertion error if called with `layer_id == "discharge"`, because the canonical short-circuit at `layer_metadata` ensures `national_discharge=True` collapses to `source_refs={}` before this helper would otherwise be reached
AND a unit test MUST exist that calls `_layer_source_refs(layer_id="discharge", ...)` and asserts the `AssertionError` is raised, locking the invariant against a future refactor that silently wires discharge back through this path and reintroduces `run_id` into the cache ETag input

### Requirement: Frontend M11Shell mock fixture mirrors canonical discharge shape
The frontend unit-test mock fixture `m11MvtMetadataByLayer['discharge']` in `apps/frontend/src/pages/__tests__/M11Shell.test.tsx` SHALL reference the national-shape fixture (`dischargeNationalMvtMetadata` — `tile_url_template = "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"`, `required_placeholders` without `{run_id}`, `source_refs` absent) and not the legacy single-run fixture (`dischargeMvtMetadata` — `tile_url_template` containing `{run_id}`, `source_refs` keyed by `run_id`). The mock fixture's `min_zoom` SHALL equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` (currently `3`).

The legacy `dischargeMvtMetadata` constant MAY remain in the file as a deeplink-only test fixture (the single-run `/api/v1/tiles/hydro/{run_id}/...` deeplink route still exists) but MUST NOT be the default-discharge fixture consumed by `m11MvtMetadataByLayer`.

#### Scenario: M11Shell unit-test default-discharge fixture uses national shape
WHEN the frontend M11Shell unit tests reference `m11MvtMetadataByLayer['discharge']`
THEN the resolved metadata MUST have `tile_url_template` containing `/api/v1/tiles/hydro-national/` and NOT containing `{run_id}` placeholder
AND `required_placeholders` MUST NOT contain `'run_id'`
AND `source_refs` MUST NOT contain a `run_id` key
AND `min_zoom` MUST equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` value (currently `3`)

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

### Requirement: A user-supplied tile instant never produces a 5xx

`services/tiles/mvt.py::canonical_mvt_time` SHALL NOT let a bare `OverflowError` escape from either
of its UTC-normalization branches (the `datetime` branch and the parsed-string branch): a value whose
normalization to UTC leaves `datetime`'s representable range SHALL raise the typed
`MvtTimeOutOfRangeError` (a `ValueError` subclass) instead.

Every MVT tile route that accepts a path-parameter instant — `hydro_mvt_tile`,
`hydro_national_mvt_tile`, and `hydro_national_source_cycle_mvt_tile` — SHALL reject such an instant
with HTTP **422** and code `VALIDATION_ERROR` **before issuing any SQL statement**, using one shared
validator so the routes cannot drift on the error body. That shared validator is also reached by
`/api/v1/layers/{layer_id}/valid-times` and by the two precip overlay routes, whose status, code,
message and details for every already-rejected instant SHALL be unchanged. The rejection is a range
check only:
the legacy routes SHALL keep accepting an in-range sub-second instant exactly as before, and the
`{source}/{cycle}` route SHALL keep rejecting sub-second instants with its existing 422.

Canonicalization of in-range instants SHALL be unchanged: the same canonical string, the same
`cache_key`, the same `_read_cache` identity comparison, the same SQL binds, the same tile bytes,
ETag and cache headers, and no `*_QUERY_VERSION` bump — for all five tile layers.

Server-sourced values reaching `canonical_mvt_time` (`created_at`, `cycle_time`, `updated_at`, cached
`valid_time` rows) are deliberately NOT translated to 422: an unnormalizable stored instant is a
server-data defect and SHALL surface as the typed error, not as a client-fault status.

#### Scenario: Out-of-range instant on the legacy national tile route
WHEN `GET /api/v1/tiles/hydro-national/q_down/9999-12-31T23:59:59-08:00/6/50/25.pbf` is requested
THEN the response is HTTP 422 with code `VALIDATION_ERROR` AND zero SQL statements were executed on
the request session

#### Scenario: Out-of-range instant on the legacy single-run tile route
WHEN `GET /api/v1/tiles/hydro/{run_id}/q_down/0001-01-01T00:00:00+08:00/6/50/25.pbf` is requested
THEN the response is HTTP 422 with code `VALIDATION_ERROR` AND zero SQL statements were executed on
the request session — in particular `_require_display_ready` and `_require_hydro_mvt_source_identity`
are never reached

#### Scenario: In-range instants are unshifted
WHEN the same instant is requested as `...T12:00:00Z`, `...T12:00:00+00:00`, `...T12:00:00.000Z`, or
`...T20:00:00+08:00`
THEN every spelling yields the identical canonical `valid_time`, the identical `cache_key`, the
identical SQL bind, and identical tile bytes and ETag

#### Scenario: Legacy sub-second acceptance is preserved
WHEN a legacy tile route is requested with an in-range sub-second instant such as `...T12:00:00.500Z`
THEN the route does NOT return 422 and canonicalizes the instant to `...T12:00:00.500000Z`, exactly as
before this change

#### Scenario: Helper raises a typed error rather than OverflowError
WHEN `canonical_mvt_time` is called with `datetime.fromisoformat("9999-12-31T23:59:59-08:00")` or with
the equivalent string `"9999-12-31T23:59:59-08:00"`
THEN it raises `MvtTimeOutOfRangeError` and not a bare `OverflowError`

#### Scenario: Routes sharing the validator are behavior-identical
WHEN `/api/v1/layers/{layer_id}/valid-times`, `/api/v1/precip/{source}/{cycle}/index`, or the precip PNG
route is requested with an out-of-range instant
THEN each still returns its existing 422 `VALIDATION_ERROR` with an unchanged body, and each in-range
request returns an unchanged response body, cache key and rendered bytes

#### Scenario: Unparseable text is still passed through
WHEN `canonical_mvt_time` is called with text that is not an ISO instant at all
THEN it returns that text unchanged, as before this change

### Requirement: Canonical national tile refuses a partially covered identity
The canonical source/cycle national discharge tile route `GET /api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` SHALL, on a tile-cache miss, refuse a `(source, cycle)` identity whose set of covering active river networks is non-empty but not equal to the set of active river networks, responding HTTP 424 with error code `MVT_NATIONAL_IDENTITY_INCOMPLETE` and the covered and active network counts. The coverage rule MUST be the one the per-cycle `/api/v1/layers/discharge/valid-times` branch applies, evaluated by the same helper, as a set comparison. An identity covered by no active network MUST keep the existing HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, and a fully covered identity MUST keep its response bytes, cache key and headers. The legacy source-less national route MUST NOT evaluate this rule.

#### Scenario: Partially covered cycle is refused
- **WHEN** live PostGIS MVT is enabled, the tile is not cached, and active networks `{rn-a, rn-b, rn-c}` exist while only `rn-a` and `rn-b` have a display-ready run for `(gfs, C)`
- **THEN** the route responds HTTP 424 with `error.code = MVT_NATIONAL_IDENTITY_INCOMPLETE`, `details.covered_network_count = 2` and `details.active_network_count = 3`
- **AND** the tile SQL is not executed and no cache entry is written

#### Scenario: Fully covered cycle is unchanged
- **WHEN** every active network has a display-ready run for `(gfs, C)`
- **THEN** the route responds 200 with the same bytes, cache key and headers as before this requirement, regardless of whether `C` is inside the `/cycles` lookback window

#### Scenario: Uncovered cycle keeps the no-run verdict
- **WHEN** no active network has a display-ready run for `(ifs, C)`
- **THEN** the route responds HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` exactly as before this requirement

#### Scenario: Membership mismatch at equal size fails closed
- **WHEN** the active set is `{rn-b, rn-c1, rn-c2}` and the covering set is `{rn-a, rn-c1, rn-c2}`
- **THEN** the route responds HTTP 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE`

#### Scenario: Cache hit and disabled live PostGIS issue no coverage statement
- **WHEN** the tile is already cached, or live PostGIS MVT is disabled
- **THEN** no coverage statement is executed, and the response is the cached tile or the existing `MVT_LIVE_POSTGIS_UNAVAILABLE` 424 respectively

### Requirement: The national discharge intersection's denominator is never older than its numerator

The helper that answers "which active river networks cover this `(source, cycle)`" SHALL read the
active-network set (the denominator) from a snapshot that is at least as fresh as the snapshot the
display-ready coverage rows (the numerator) come from. A river network activated after the coverage
rows were read and before the active set was read MUST therefore make the set comparison unequal, so
every cycle judged by that pair of reads fails closed, regardless of whether the newly active network
contributed any coverage row. Without any concurrent write to `core.model_instance`, `hydro.hydro_run` or
`hydro.run_display_coverage` between the two reads, this rule MUST NOT change any result: the coverage
rows are already filtered on `active_flag`, so the covered set is a subset of the active set and the
comparison is unchanged. The rule is about read ORDER alone. It makes no claim about a network
deactivated between the reads, and none about a coverage row that ceases to be display-ready between
them — that case is a known fail-open residual of reading the numerator first, not something this
requirement governs. The rule applies to every consumer of that helper — the
`GET /api/v1/layers/discharge/cycles` list, the per-cycle branch of
`GET /api/v1/layers/discharge/valid-times`, the no-argument branch of
`GET /api/v1/layers/discharge/valid-times`, and the canonical national tile route's coverage check —
because all four read the intersection through it. This requirement constrains the ORDER of the two
reads only; it does not make them atomic, and it does not govern a coverage row that ceases to be
display-ready between them.

#### Scenario: A network activated with zero coverage rows empties the cycles list

- **WHEN** the coverage rows are read while the active networks are `{rn-b, rn-c}`, network `rn-a` is
  then activated with no display-ready run at all, and the active-network set is read afterwards as
  `{rn-a, rn-b, rn-c}`
- **THEN** `GET /api/v1/layers/discharge/cycles?source=gfs` returns `cycles: []` and
  `default_cycle: null`

#### Scenario: A network activated with zero coverage rows empties the per-cycle valid times

- **WHEN** the same activation happens around the reads for one requested `(source, cycle)`
- **THEN** `GET /api/v1/layers/discharge/valid-times?source=gfs&cycle=C` returns an empty
  `valid_times` list with an observed count of `0`

#### Scenario: A network activated with coverage for only one cycle closes the other cycle too

- **WHEN** the coverage rows are read while the active networks are `{rn-b, rn-c}`, network `rn-a` is
  then activated holding a display-ready run for cycle `K` but none for the older cycle `J`, and the
  active-network set is read afterwards as `{rn-a, rn-b, rn-c}`
- **THEN** neither `K` nor `J` is listed by `GET /api/v1/layers/discharge/cycles?source=gfs`
- **AND** `GET /api/v1/layers/discharge/valid-times?source=gfs&cycle=J` returns an empty
  `valid_times` list

#### Scenario: Without a concurrent write every result is unchanged

- **WHEN** no write to `core.model_instance`, `hydro.hydro_run` or `hydro.run_display_coverage` lands
  between the two reads
- **THEN** the listed cycles, their `valid_time_start` / `valid_time_end` bounds, the per-cycle valid
  times, and the canonical national tile route's coverage verdict are exactly what they were before
  this requirement

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

Both entry points that run `qhh_production_bootstrap.py::_seed_output_segment_rows` (whose `ON CONFLICT DO UPDATE` rewrites `properties_json` and so erases the STORED `stream_type`) SHALL, after their trailing `_backfill_output_segment_geometry` call on the same cursor, fail with `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE` and roll the transaction back when any `shud_output_river` row of the network lacks a `Type` while its matching source reach (same network, not an output row, `iRiv` equal to the row's `shud_riv_index`, non-NULL geometry) carries a non-null one. A source `Type` that is absent or JSON `null` counts as no `Type`, exactly as the backfill (which copies `Type` only when it is not null) treats it.

`_backfill_output_segment_geometry(..., only_missing=False)`, which both entry points run, rewrites and bumps even when the values are unchanged; this over-rotation — one extra cold miss per network for each operator-run bootstrap or seed — is accepted, because the seed path must keep rewriting unconditionally. Row-level `INSERT`/`DELETE` of river segments and imports run with `backfill_output_segment_geometry=False` are outside the `geometry_generation` counter: they refresh the network's `segment_count`/`checksum`, which the per-basin and national river-network digests include; the run-painted `hydro`/`hydro-national` layers are driven by run identity and are not rotated by those row-level paths.

#### Scenario: Trailing backfill that restores nothing fails the seed closed
- **WHEN** a network's output rows carry `Type` from an earlier backfill, their source reach geometry is degenerate so the trailing backfill updates 0 rows, and `seed_qhh_output_segments` runs
- **THEN** it raises `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE`, and afterwards every output row still carries its `Type` and `geometry_generation` is unchanged

#### Scenario: Trailing backfill that restores nothing fails the bootstrap closed
- **WHEN** the same degenerate-source state is reached through the bootstrap main path (`_bootstrap_database`)
- **THEN** it raises `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE` after its trailing backfill on the same cursor and the transaction rolls back

#### Scenario: A source reach whose Type is JSON null does not block the seed
- **WHEN** a source reach carries `"Type": null` (a blank numeric dbf cell) and the seed runs
- **THEN** the seed commits, the matching output row has no `Type` and a NULL `stream_type`, and no `QHH_OUTPUT_SEGMENT_STREAM_TYPE_INCOMPLETE` is raised

#### Scenario: A healthy seed passes
- **WHEN** every output row whose source reach has a `Type` carries it after the trailing backfill
- **THEN** the seed completes and reports its counts as before

### Requirement: The no-argument national valid-times list fails closed on an incomplete intersection

`GET /api/v1/layers/discharge/valid-times` called with no `run_id`, no `source` and no `cycle` SHALL intersect each active river network's newest display-ready run only when the SET of networks holding a display-ready run EQUALS the set of active river networks, where the active set is read from `core.model_instance` (`active_flag` and a non-null `river_network_version_id`) by the same helper the per-cycle branch uses — never derived from the coverage rows. When the two sets differ in any direction (an active network without a display-ready run, equal size with different members, or a covering network no longer active), the branch MUST return an empty `valid_times` list with an observed count of `0`. When the sets are equal the result MUST be unchanged, including for a network whose newest display-ready run predates the `/cycles` lookback window. This branch is a fourth consumer of the helper governed by "The national discharge intersection's denominator is never older than its numerator": its verdict under a race between the two reads follows that requirement's read order, and its residuals (deactivation, a coverage row ceasing to be display-ready) are the ones that requirement disclaims.

#### Scenario: An active network without a display-ready run empties the list

- **WHEN** the active networks are `{rn-a, rn-b, rn-c}` and only `rn-a` and `rn-b` hold display-ready runs
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0`

#### Scenario: Equal-size membership mismatch empties the list

- **WHEN** the active networks are `{rn-b, rn-c1, rn-c2}` and display-ready runs exist for `{rn-a, rn-c1, rn-c2}`
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0`

#### Scenario: A covering network that is no longer active empties the list

- **WHEN** display-ready runs are read for `{rn-a, rn-b, rn-c}` and the active set is read as `{rn-a, rn-b}`
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0`

#### Scenario: A complete intersection is unchanged

- **WHEN** every active network holds a display-ready run, including one whose newest run predates the `/cycles` lookback window
- **THEN** the no-argument request returns the same `valid_times`, `observed_count` and `truncated` as before this requirement

#### Scenario: No active network and no coverage row returns an empty list

- **WHEN** there are no active networks and no display-ready runs
- **THEN** the no-argument request returns `valid_times: []` with `observed_count = 0` and does not error
