## MODIFIED Requirements

### Requirement: MVT tile API contract
The backend SHALL expose hydrology vector tile endpoints with `application/x-protobuf`, stable layer IDs, bounded z/x/y parameters, and documented feature properties. The hydrology `discharge` layer SHALL surface the **national source/cycle** tile endpoint `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` as its canonical URL in the public `/api/v1/layers` catalog, with `source ∈ {gfs, ifs}` (lower-case path segment matched case-insensitively against `hydro.hydro_run.source_id`) and RFC3339 `cycle`. The legacy national route `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` SHALL remain served with unchanged behavior as a non-canonical alias. The single-run hydro endpoint remains a supported direct-deeplink route but is NOT a canonical layer URL. The internal `_layer_source_refs` helper SHALL NEVER be reached for `layer_id == "discharge"` — the call site in `layer_metadata` short-circuits to `source_refs={}` whenever `national_discharge=True`, and the helper itself MUST guard the invariant at its entry boundary so any future refactor that wires discharge back through this path fails loudly at development/CI time rather than silently re-introducing run_id into the ETag hash input.

#### Scenario: Canonical endpoint disposition
WHEN this change is implemented
THEN `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (canonical discharge layer URL), `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (legacy alias, unchanged), and `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (direct-deeplink only) have explicit OpenAPI/runtime behavior

#### Scenario: Source and cycle bind the national run selection
WHEN the source/cycle national tile SQL is generated
THEN the `latest_runs` CTE filters `lower(h.source_id) = :source` and `h.cycle_time = :cycle` in addition to the display-ready and coverage predicates
AND the tile cache key and `source_version` include `source` and `cycle`
AND `NATIONAL_DISCHARGE_QUERY_VERSION` equals `fair-network-budget-v5`

#### Scenario: Tile success
WHEN a published layer/run/valid_time has features in a tile
THEN endpoint returns PBF with required properties and cacheable headers

#### Scenario: Invalid tile
WHEN z/x/y or query parameters are out of bounds, or `source` is not `gfs`/`ifs`, or `cycle` is not RFC3339
THEN endpoint returns stable validation error without running expensive SQL

#### Scenario: Contract freshness
WHEN the public tile contract changes
THEN OpenAPI, generated frontend API types, and drift allowlists are updated together or the unchanged legacy path remains explicitly documented

#### Scenario: Stable feature properties
WHEN a hydrology MVT feature is encoded
THEN properties include stable segment/network/source/time/value metadata and reject missing or non-finite required values

#### Scenario: Layer metadata discovery
WHEN frontend requests MVT-capable layer metadata
THEN metadata includes `layer_id`, `tile_format`, URL template placeholders, MapLibre source-layer id, property schema/version, min/max zoom, Web Mercator bounds, valid_time/source references, cache etag/version, fallback/release-blocking flags, and for `discharge` additionally `default_source`, `default_cycle`, `cycles_url_template`, and `valid_times_url_template`

#### Scenario: Discharge canonical URL is national across all callers
WHEN `/api/v1/layers` is called with OR without a `run_id` query parameter
THEN the `discharge` entry's `tile_url_template` MUST be `/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf` with `required_placeholders = ["source", "cycle", "valid_time"]` AND MUST NOT contain a `{run_id}` placeholder
AND `metadata.valid_times` MUST be the list for `default_source`/`default_cycle`
AND the single-run `/api/v1/tiles/hydro/{run_id}/q_down/...` route continues to serve direct GET requests but MUST NOT appear in the canonical catalog's discharge entry

#### Scenario: Discharge layer never reaches `_layer_source_refs`
WHEN `_layer_source_refs(layer_id, ...)` is invoked in `services/tiles/mvt.py`
THEN `layer_id` MUST NOT equal `"discharge"` — the function MUST raise an assertion error if called with `layer_id == "discharge"`, because the canonical short-circuit at `layer_metadata` ensures `national_discharge=True` collapses to `source_refs={}` before this helper would otherwise be reached
AND a unit test MUST exist that calls `_layer_source_refs(layer_id="discharge", ...)` and asserts the `AssertionError` is raised, locking the invariant against a future refactor that silently wires discharge back through this path and reintroduces `run_id` into the cache ETag input

### Requirement: Frontend M11Shell mock fixture mirrors canonical discharge shape
The frontend unit-test mock fixture `m11MvtMetadataByLayer['discharge']` in `apps/frontend/src/pages/__tests__/M11Shell.test.tsx` SHALL reference the national-shape fixture (`dischargeNationalMvtMetadata` — `tile_url_template = "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"`, `required_placeholders = ["source", "cycle", "valid_time"]`, `source_refs` absent, `default_source = "gfs"`, `default_cycle` set) and not the legacy single-run fixture (`dischargeMvtMetadata` — `tile_url_template` containing `{run_id}`, `source_refs` keyed by `run_id`). The mock fixture's `min_zoom` SHALL equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` (currently `3`).

The legacy `dischargeMvtMetadata` constant MAY remain in the file as a deeplink-only test fixture (the single-run `/api/v1/tiles/hydro/{run_id}/...` deeplink route still exists) but MUST NOT be the default-discharge fixture consumed by `m11MvtMetadataByLayer`.

#### Scenario: M11Shell unit-test default-discharge fixture uses national shape
WHEN the frontend M11Shell unit tests reference `m11MvtMetadataByLayer['discharge']`
THEN the resolved metadata MUST have `tile_url_template` containing `/api/v1/tiles/hydro-national/{source}/{cycle}/` and NOT containing `{run_id}` placeholder
AND `required_placeholders` MUST equal `['source', 'cycle', 'valid_time']`
AND `source_refs` MUST NOT contain a `run_id` key
AND `min_zoom` MUST equal the real backend `_NATIONAL_DISCHARGE_METADATA.min_zoom` value (currently `3`)

## ADDED Requirements

### Requirement: National discharge cycles and per-cycle valid times
The backend SHALL expose `GET /api/v1/layers/discharge/cycles?source=gfs|ifs` returning `{source, cycles: [{cycle_time, valid_time_start, valid_time_end}], default_cycle}` where a cycle is listed only if **every** active river network has a display-ready run (`segment_count > 0`) for that source and cycle (intersection, fail-closed: an empty list when any network has no run). `GET /api/v1/layers/discharge/valid-times` SHALL accept optional `source` and `cycle` query parameters and, when both are given, return valid times from `cycle` at 3-hour stride up to the minimum `river_valid_time_end` across active networks for that source/cycle; without them the existing default-window behavior is preserved.

#### Scenario: Intersection excludes a partially covered cycle
- **WHEN** 38 networks have gfs runs for cycle A but only 37 have runs for cycle B
- **THEN** `cycles` contains A and not B

#### Scenario: Fail-closed on a network without runs
- **WHEN** one active network has no display-ready gfs run at all
- **THEN** `cycles` is empty and `default_cycle` is null

#### Scenario: Per-cycle valid times at 3h stride
- **WHEN** `valid-times?source=gfs&cycle=2026-09-02T12:00:00Z` is requested and every network covers through `cycle + 168h`
- **THEN** the response has 57 entries from `cycle` to `cycle + 168h` at 3-hour spacing

#### Scenario: Unknown source or cycle
- **WHEN** `source` is not `gfs`/`ifs`, or `cycle` is given without `source`
- **THEN** the route returns HTTP 422
