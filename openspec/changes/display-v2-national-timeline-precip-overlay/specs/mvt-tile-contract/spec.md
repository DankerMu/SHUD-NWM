## MODIFIED Requirements

### Requirement: MVT tile API contract
The backend SHALL expose hydrology vector tile endpoints with `application/x-protobuf`, stable layer IDs, bounded z/x/y parameters, and documented feature properties. The hydrology `discharge` layer SHALL surface the **national source/cycle** tile endpoint `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` as its canonical URL in the public `/api/v1/layers` catalog, with `source ∈ {gfs, ifs}` (lower-case path segment matched case-insensitively against `hydro.hydro_run.source_id`) and RFC3339 `cycle`. Every instant this contract serializes — the `cycle` and `valid_time` path segments, `metadata.valid_times[]`, and the `valid-times` / `cycles` response bodies — SHALL use the single spelling `YYYY-MM-DDTHH:MM:SSZ` (seconds precision, literal `Z`, no fractional seconds), the form `canonical_mvt_time` already emits; routes MAY accept an instant with fractional seconds or a `+00:00` offset but MUST canonicalize to that spelling before it reaches the SQL bind, the cache key, or the ETag input, and frontend callers MUST substitute the canonicalized spelling into `{cycle}`/`{valid_time}` rather than the millisecond form their own state normalization produces. The legacy national route `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` SHALL remain served with unchanged behavior as a non-canonical alias. The single-run hydro endpoint remains a supported direct-deeplink route but is NOT a canonical layer URL. The internal `_layer_source_refs` helper SHALL NEVER be reached for `layer_id == "discharge"` — the call site in `layer_metadata` short-circuits to `source_refs={}` whenever `national_discharge=True`, and the helper itself MUST guard the invariant at its entry boundary so any future refactor that wires discharge back through this path fails loudly at development/CI time rather than silently re-introducing run_id into the ETag hash input.

#### Scenario: Canonical endpoint disposition
WHEN this change is implemented
THEN `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (canonical discharge layer URL), `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (legacy alias, unchanged), and `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (direct-deeplink only) have explicit OpenAPI/runtime behavior

#### Scenario: Source and cycle bind the national run selection
WHEN the source/cycle national tile SQL is generated
THEN the `latest_runs` CTE filters `lower(h.source_id) = :source` and `h.cycle_time = :cycle` in addition to the display-ready and coverage predicates
AND the tile cache key and `source_version` include `source` and `cycle`
AND `NATIONAL_DISCHARGE_QUERY_VERSION` equals `fair-network-budget-v5`

#### Scenario: The identity probe binds the same source and cycle as the data CTE
WHEN the source/cycle national tile SQL is generated
THEN the `source_identity_stats_sql` probe's inline run-discovery sub-select (`services/tiles/mvt.py`, the `SELECT DISTINCT ON (mi.river_network_version_id)` block that feeds the `CROSS JOIN LATERAL` existence check) carries the SAME `lower(h.source_id) = :source AND h.cycle_time = :cycle` predicate as the `latest_runs` CTE
AND both run selections therefore agree on which runs exist for the requested identity, so the probe cannot answer "identity present" from another source's run
AND when the requested `(source, cycle)` has no display-ready run, `source_identity_count` is 0 and the route raises HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` (`apps/api/routes/hydro_display.py`), rather than returning an empty 200 tile

#### Scenario: One source has a run and the other does not
WHEN `gfs` has a display-ready run for cycle `2026-09-02T12:00:00Z` and `ifs` has none for that cycle
THEN the `gfs` tile request returns 200 with features
AND the `ifs` tile request for the same cycle, variable, valid_time and z/x/y returns HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE`, not an empty 200 tile

#### Scenario: Tile success
WHEN a published layer/run/valid_time has features in a tile
THEN endpoint returns PBF with required properties and cacheable headers

#### Scenario: Invalid tile
WHEN z/x/y or query parameters are out of bounds, or `source` is not `gfs`/`ifs`, or `cycle` is not RFC3339
THEN endpoint returns stable validation error without running expensive SQL

#### Scenario: Contract freshness
WHEN the public tile contract changes
THEN OpenAPI, generated frontend API types, and drift allowlists are updated together or the unchanged legacy path remains explicitly documented
AND because `openapi/nhms.v1.yaml` is hand-maintained (there is no generator) and `tests/test_openapi_drift.py::test_static_openapi_matches_runtime_schema` compares it to `app.openapi()` for equality, the new national tile route, the cycles route and the changed `valid-times` query parameters MUST be written into that YAML by hand; adding them to `INTERNAL_ROUTE_REASONS` is NOT a substitute, because that allowlist only relaxes the public-route parity test
AND `apps/frontend/src/api/types.ts` is refreshed with `pnpm generate:api` and verified with `pnpm check:api-types`

#### Scenario: Stable feature properties
WHEN a hydrology MVT feature is encoded
THEN properties include stable segment/network/source/time/value metadata and reject missing or non-finite required values

#### Scenario: Layer metadata discovery
WHEN frontend requests MVT-capable layer metadata
THEN metadata includes `layer_id`, `tile_format`, URL template placeholders, MapLibre source-layer id, property schema/version, min/max zoom, Web Mercator bounds, valid_time/source references, cache etag/version, fallback/release-blocking flags, and for `discharge` additionally `default_source`, `default_cycle`, `cycles_url_template`, and `valid_times_url_template`

#### Scenario: Discharge canonical URL is national across all callers
WHEN `/api/v1/layers` is called with OR without a `run_id` query parameter
THEN the `discharge` entry's `tile_url_template` MUST be `/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf` with `required_placeholders = ["source", "cycle", "valid_time", "z", "x", "y"]` AND MUST NOT contain a `{run_id}` placeholder
AND `metadata.valid_times` MUST be the list for `default_source`/`default_cycle`
AND the single-run `/api/v1/tiles/hydro/{run_id}/q_down/...` route continues to serve direct GET requests but MUST NOT appear in the canonical catalog's discharge entry

#### Scenario: Discharge layer never reaches `_layer_source_refs`
WHEN `_layer_source_refs(layer_id, ...)` is invoked in `services/tiles/mvt.py`
THEN `layer_id` MUST NOT equal `"discharge"` — the function MUST raise an assertion error if called with `layer_id == "discharge"`, because the canonical short-circuit at `layer_metadata` ensures `national_discharge=True` collapses to `source_refs={}` before this helper would otherwise be reached
AND a unit test MUST exist that calls `_layer_source_refs(layer_id="discharge", ...)` and asserts the `AssertionError` is raised, locking the invariant against a future refactor that silently wires discharge back through this path and reintroduces `run_id` into the cache ETag input

### Requirement: Frontend M11Shell mock fixture mirrors canonical discharge shape
The default-discharge mock metadata in `apps/frontend/src/pages/__tests__/M11Shell.test.tsx` is the `dischargeMetadata` constant declared at the top of that file and consumed by `dischargeLayer.metadata` (and by the `m11VectorSourceKey` case). It SHALL carry the national source/cycle shape: `url_template = "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"` (the frontend metadata field is `url_template`, per `apps/frontend/src/lib/mvtLayerMetadata.ts`; the backend emits the same string under both `url_template` and `tile_url_template`, so if the fixture also sets `tile_url_template` the two MUST be equal), `required_placeholders = ["source", "cycle", "valid_time", "z", "x", "y"]`, no `run_id` key in `source_refs`, and `default_source = "gfs"` plus a `default_cycle`. The fixture's `min_zoom` SHALL equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` (currently `3`); it is `0` today and MUST be corrected by this change.

There is no `m11MvtMetadataByLayer` map and no `dischargeNationalMvtMetadata` / `dischargeMvtMetadata` constant in this test file — the single `dischargeMetadata` constant is the whole fixture surface, and this requirement is written against it. A separate legacy single-run fixture MAY be introduced for the `/api/v1/tiles/hydro/{run_id}/...` deeplink route, but it MUST NOT be the constant that `dischargeLayer.metadata` consumes.

#### Scenario: M11Shell unit-test default-discharge fixture uses national shape
WHEN the frontend M11Shell unit tests build an overlay from `dischargeLayer` (whose `metadata` is `dischargeMetadata`)
THEN `dischargeMetadata.url_template` MUST contain `/api/v1/tiles/hydro-national/{source}/{cycle}/` and MUST NOT contain a `{run_id}` placeholder
AND `required_placeholders` MUST equal `['source', 'cycle', 'valid_time', 'z', 'x', 'y']`
AND `source_refs` MUST NOT contain a `run_id` key
AND `min_zoom` MUST equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` value (currently `3`)

#### Scenario: Existing assertions against the legacy single-run URL are updated with the fixture
WHEN the fixture is switched to the national source/cycle template
THEN the assertions in the same file that expect the built tile URL to contain `/api/v1/tiles/hydro/` MUST be updated to expect `/api/v1/tiles/hydro-national/gfs/<cycle>/q_down/<valid_time>/` for the fixture's `(source, cycle, validTime)`
AND the `m11VectorSourceKey` case MUST assert a key that distinguishes `(source, cycle, valid_time)` rather than `run_id`

## ADDED Requirements

### Requirement: National discharge cycles and per-cycle valid times
The backend SHALL expose `GET /api/v1/layers/discharge/cycles?source=gfs|ifs` returning `{source, cycles: [{cycle_time, valid_time_start, valid_time_end}], default_cycle}` where a cycle is listed only if **every** active river network has a display-ready run (`segment_count > 0`) for that source and cycle (intersection, fail-closed: an empty list when any network has no run). `GET /api/v1/layers/discharge/valid-times` SHALL accept optional `source` and `cycle` query parameters and, when both are given, return valid times from `cycle` at 3-hour stride restricted to the intersection coverage window `[max(river_valid_time_start), min(river_valid_time_end)]` across active networks for that source/cycle, so the list never advertises an instant some basin cannot render; and it SHALL return the empty list when any active network has no display-ready run for that `(source, cycle)`, the same fail-closed intersection rule the `cycles` list uses. Without those parameters the existing default-window behavior is preserved.

#### Scenario: Intersection excludes a partially covered cycle
- **WHEN** 38 networks have gfs runs for cycle A but only 37 have runs for cycle B
- **THEN** `cycles` contains A and not B

#### Scenario: Fail-closed on a network without runs
- **WHEN** one active network has no display-ready gfs run at all
- **THEN** `cycles` is empty and `default_cycle` is null

#### Scenario: Per-cycle valid times at 3h stride
- **WHEN** `valid-times?source=gfs&cycle=2026-09-02T12:00:00Z` is requested and every network covers through `cycle + 168h`
- **THEN** the response has 57 entries from `cycle` to `cycle + 168h` at 3-hour spacing

#### Scenario: Coverage that starts after the cycle clamps the first entry
- **WHEN** `valid-times?source=gfs&cycle=2026-09-02T12:00:00Z` is requested and one active network's `river_valid_time_start` is `2026-09-02T18:00:00Z`
- **THEN** the first entry is `2026-09-02T18:00:00Z`, not the cycle itself
- **AND** the matching `cycles[]` row for that cycle carries the same `valid_time_start`

#### Scenario: A cycle outside the intersection has no valid times
- **WHEN** `valid-times?source=gfs&cycle=<C>` is requested for a cycle that `cycles` does not list because some active network has no display-ready gfs run for it
- **THEN** the response `valid_times` is `[]`, not a partial list over the networks that do have a run

#### Scenario: Unknown source or cycle
- **WHEN** `source` is not `gfs`/`ifs`, or `cycle` is given without `source`
- **THEN** the route returns HTTP 422

#### Scenario: Cycle and valid time spelling is seconds-precision UTC
- **WHEN** `cycles` or `valid-times` responds
- **THEN** every `cycle_time`, `default_cycle`, `valid_time_start`, `valid_time_end` and `valid_times[]` entry matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`
- **AND** a tile request whose `cycle` path segment is spelled `2026-09-02T12:00:00.000Z` binds the same `:cycle` value, and hits the same cache entry, as `2026-09-02T12:00:00Z`
