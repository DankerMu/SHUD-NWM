# frontend-mvt-layer-consumption Specification

## Purpose
TBD - created by archiving change m16-production-mvt-performance. Update Purpose after archive.
## Requirements
### Requirement: Frontend MVT layer consumption
MapLibre hydrology layers SHALL consume vector tile sources for national rendering when layer metadata advertises MVT. The supported hydrology layer set is `discharge` and `river-network`; retired supplemental layers and `water-level` are no longer supported.

#### Scenario: Metadata-driven selection
WHEN layer metadata exposes `tile_format=mvt`, URL template, source-layer id, zoom/bounds, schema/version, and valid-time/source references
THEN frontend derives MapLibre vector source/layer configuration from that metadata instead of hard-coding hidden tile URLs

#### Scenario: MVT available
WHEN layer metadata has MVT template
THEN frontend registers vector source/layers and does not request full national GeoJSON

#### Scenario: MVT unavailable
WHEN only bounded GeoJSON compatibility is available
THEN frontend labels fallback mode and limits bbox/feature requests

#### Scenario: National MVT required but unavailable
WHEN user opens a national hydrology view and MVT metadata is unavailable
THEN frontend shows a truthful unavailable/release-blocking state instead of silently requesting full-national GeoJSON

#### Scenario: State compatibility
WHEN MVT source selection changes valid_time, run, layer, basin, or restored URL state
THEN MapLibre source identity and visible status update without breaking existing timeline/selection behavior

#### Scenario: water-level layer is rejected at compile time
WHEN any frontend code path attempts to consume `water-level` as a layer id or `water_level` as a hydro MVT variable
THEN the layer enum / `M11Layer` union MUST NOT include `water-level`
AND build-time type checking MUST reject the value
AND no tests, fixtures, or runtime selectors MUST register a `water-level` source

#### Scenario: water-level layer id is rejected at the URL/query boundary
WHEN a restored URL/query parameter sets `layer=water-level` (e.g. bookmark, shared link, stale router state)
THEN the layer parser MUST reject the value and fall back to the default supported layer (`discharge`)
AND no MVT source registration MUST occur for `water-level`

#### Scenario: water_level variable is rejected at the backend boundary
WHEN a client requests `GET /api/v1/layers/water-level/valid-times` or any tile/MVT endpoint with `variable=water_level`
THEN the backend MUST respond with HTTP 422 (FastAPI enum validation)
AND the OpenAPI `HydroMvtVariable` enum MUST NOT include `water_level`

### Requirement: Layer valid_times are consumed from `metadata.valid_times` first
The frontend SHALL consume `apiLayer.metadata.valid_times` returned by `GET /api/v1/layers` as the primary source of per-layer valid-time discovery, and SHALL only call `GET /api/v1/layers/{layer_id}/valid-times` as a fallback when the metadata payload is missing or `null`.

#### Scenario: Metadata carries valid_times
- **WHEN** `/api/v1/layers` (or `/api/v1/layers?run_id=...`) returns a layer whose `metadata.valid_times` is a non-empty array
- **THEN** `normalizeLayerStates` MUST use that array directly
- **AND** the frontend MUST NOT issue a separate `/api/v1/layers/<layer_id>/valid-times` request for that layer during the same overview load

#### Scenario: Metadata.valid_times is intentionally empty (time-less layer)
- **WHEN** `apiLayer.metadata.valid_times === []` (e.g. `river-network` is a topology layer with no time dimension)
- **THEN** the frontend MUST treat the layer as having no time dimension
- **AND** the frontend MUST NOT issue a fallback `/api/v1/layers/<layer_id>/valid-times` request
- **AND** unit tests MUST cover the empty-array primary path explicitly

#### Scenario: Metadata.valid_times is missing or null (schema gap)
- **WHEN** `apiLayer.metadata.valid_times` is `undefined` or `null`
- **THEN** the frontend MAY fetch `/api/v1/layers/<layer_id>/valid-times` as a fallback
- **AND** unit tests MUST cover both the primary and fallback paths (a dedicated `normalizeLayerStates` unit test pair in `apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`)

### Requirement: A cycle the source does not list is named, not reported as timeless
When the active national discharge pair is `(source, cycle)` with `cycle` absent from that source's arrived cycle list and the per-cycle valid-times list is empty, the discharge layer SHALL be disabled with a reason that names the source and the cycle. The reason MUST differ from `'Layer has no valid times.'`, from the fail-closed reason, and from the pending and error reasons for the active cycle's valid times.

#### Scenario: GFS-only cycle requested for IFS
- **WHEN** the URL is `?source=ifs&cycle=<C>` and the IFS cycle list has arrived, is non-empty and does not contain `<C>`
- **AND** valid times for `(ifs, <C>)` are an empty list
- **THEN** the discharge layer is unavailable and its `disabledReason` names `IFS` and `<C>`
- **AND** no national overlay is registered

#### Scenario: Symmetric and fabricated cycles
- **WHEN** the URL is `?source=gfs&cycle=<C>` with `<C>` listed only for IFS, or `?cycle=1999-01-01T00:00:00Z`
- **AND** valid times for the pair are empty
- **THEN** the same cycle-not-listed reason is used

#### Scenario: Listed cycle without coverage keeps the generic reason
- **WHEN** `<C>` is listed for the source but its valid-times list is empty
- **THEN** `disabledReason` stays `'Layer has no valid times.'`

#### Scenario: Unlisted cycle that still has valid times renders
- **WHEN** `<C>` is not listed for the source but its valid-times list is non-empty
- **THEN** the layer is available exactly as before this requirement

#### Scenario: Membership not yet known
- **WHEN** valid times for a non-default pair arrive empty before the source's cycle list has arrived
- **THEN** the layer shows the pending reason until the cycle list arrives and the reason is re-derived

