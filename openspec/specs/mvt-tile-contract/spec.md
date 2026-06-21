# mvt-tile-contract Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: MVT tile API contract
The backend SHALL expose hydrology vector tile endpoints with `application/x-protobuf`, stable layer IDs, bounded z/x/y parameters, and documented feature properties. The hydrology `discharge` layer SHALL surface the **national** tile endpoint as its canonical URL in the public `/api/v1/layers` catalog. The single-run hydro endpoint remains a supported direct-deeplink route but is NOT a canonical layer URL. The internal `_layer_source_refs` helper SHALL NEVER be reached for `layer_id == "discharge"` — the call site in `layer_metadata` short-circuits to `source_refs={}` whenever `national_discharge=True`, and the helper itself MUST guard the invariant at its entry boundary so any future refactor that wires discharge back through this path fails loudly at development/CI time rather than silently re-introducing run_id into the ETag hash input.

#### Scenario: Canonical endpoint disposition
WHEN M16 is implemented
THEN `/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf`, `/api/v1/tiles/hydro-national/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (canonical discharge layer URL), `/api/v1/tiles/hydro/{run_id}/{variable}/{valid_time}/{z}/{x}/{y}.pbf` (direct-deeplink only, NOT exposed via the `/api/v1/layers` `discharge` entry), and true `/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf` have explicit OpenAPI/runtime behavior, while `/api/v1/tiles/flood-return-period` remains bounded GeoJSON compatibility

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
WHEN a hydrology or flood-return-period MVT feature is encoded
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

