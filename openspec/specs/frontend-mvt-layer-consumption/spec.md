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
The frontend SHALL consume `apiLayer.metadata.valid_times` returned by `GET /api/v1/layers` as the valid-time list for the **default** `(default_source, default_cycle)` of the `discharge` layer, and SHALL fetch `GET /api/v1/layers/discharge/valid-times?source=&cycle=` whenever the active `(source, cycle)` differs from the metadata defaults. `buildM11RegisteredOverlay` SHALL validate the requested `validTime` against the list held in the store for the active `(source, cycle)`, never solely against `metadata.valid_times`, and SHALL substitute `{source}` and `{cycle}` placeholders in the national template.

#### Scenario: Metadata carries valid_times
- **WHEN** `/api/v1/layers` returns `discharge` with non-empty `metadata.valid_times` and the active `(source, cycle)` equals `(default_source, default_cycle)`
- **THEN** `normalizeLayerStates` MUST use that array directly
- **AND** the frontend MUST NOT issue a separate `/api/v1/layers/discharge/valid-times` request during the same overview load

#### Scenario: Non-default cycle fetches its own list
- **WHEN** the operator selects a cycle other than `default_cycle` (or a source other than `default_source`)
- **THEN** the frontend MUST fetch `/api/v1/layers/discharge/valid-times?source=<source>&cycle=<cycle>` and store it keyed by `(source, cycle)`
- **AND** `buildM11RegisteredOverlay` MUST resolve the overlay using that stored list, producing a non-null overlay when `validTime` is in it

#### Scenario: The active cycle's list is unresolved
- **WHEN** the active `(source, cycle)` is not the metadata default and its `valid_times[]` has not been fetched yet, that fetch was rejected, or the layer-time enrichment chain was skipped because map bootstrap failed (so no per-cycle fetch will ever be issued)
- **THEN** the frontend MUST NOT fall back to `metadata.valid_times` (which describes the *default* cycle), MUST treat the layer as unavailable, and MUST NOT register an overlay or request a tile for that cycle
- **AND** the layer's `disabledReason` MUST be distinct from the time-less reason and from the fail-closed "no cycle covers every basin" reason, and the not-yet-fetched and rejected cases MUST be distinguishable from each other
- **AND** the frontend MUST NOT rewrite the URL's `validTime` while the list is unresolved, so a shared `?cycle=<cycle>&validTime=<t>` link survives the load
- **AND** the unresolved state MUST reach a terminal state: a rejected fetch, and equally a fetch that will never be issued because the enrichment chain was skipped, MUST be recorded as such (the skipped case resolving to the same rejected reason, since no further attempt is pending) and MUST re-render the layer states in place, so the transition out of "still loading" is observable rather than latched

#### Scenario: Metadata.valid_times is intentionally empty (time-less layer)
- **WHEN** `apiLayer.metadata.valid_times === []` for any layer other than `discharge` (e.g. `river-network` is a topology layer with no time dimension, and `precip` carries its times in its own index rather than in the catalog)
- **THEN** the frontend MUST treat the layer as having no time dimension
- **AND** the frontend MUST NOT issue a fallback `/api/v1/layers/<layer_id>/valid-times` request
- **AND** unit tests MUST cover the empty-array primary path explicitly

#### Scenario: Discharge with an empty list is fail-closed, not time-less
- **WHEN** `apiLayer.metadata.valid_times === []` for `layer_id === 'discharge'` together with `metadata.default_cycle === null` (the fail-closed intersection signal defined in `overview-data-contracts`)
- **THEN** the frontend MUST NOT classify `discharge` as a time-less layer
- **AND** it MUST render the disabled cycle selector, timeline and playback required by `map-layer-timeline-controls`, with the notice that no cycle covers every basin
- **AND** it MUST NOT issue a fallback `/api/v1/layers/discharge/valid-times` request and MUST NOT request tiles

#### Scenario: Metadata.valid_times is missing or null (schema gap)
- **WHEN** `apiLayer.metadata.valid_times` is `undefined` or `null`
- **THEN** the frontend MAY fetch `/api/v1/layers/<layer_id>/valid-times` as a fallback
- **AND** unit tests MUST cover both the primary and fallback paths (a dedicated `normalizeLayerStates` unit test pair in `apps/frontend/src/lib/__tests__/m11OverviewDataContracts.test.ts`)

#### Scenario: National template substitution
- **WHEN** the overlay is built for `source=ifs`, `cycle=2026-09-02T12:00:00Z`, `validTime=2026-09-02T15:00:00Z`
- **THEN** the tile URL's percent-decoded path is `/api/v1/tiles/hydro-national/ifs/2026-09-02T12:00:00Z/q_down/2026-09-02T15:00:00Z/{z}/{x}/{y}.pbf`
- **AND** the literal string carries the repository's existing substitution encoding: `buildMvtTileUrlTemplate` percent-encodes every substituted value, so the colons of the `{cycle}` and `{valid_time}` segments appear as `%3A` on the wire (the same encoding the single-run `/api/v1/tiles/hydro/{run_id}/...` route has always used, decoded back by the server before routing). Tests MUST assert the decoded path rather than weaken the assertion, and MUST NOT change `buildMvtTileUrlTemplate`'s encoding to make a literal comparison pass
- **AND** the MapLibre source key changes when any of source, cycle, or validTime changes

#### Scenario: Substituted instants are canonicalized to seconds precision
- **WHEN** the query state holds the millisecond spelling `parseM11QueryState` produces (`normalizeIsoInstant` returns `2026-09-02T12:00:00.000Z`) for `cycle` and `validTime`
- **THEN** the frontend MUST substitute the seconds-precision spelling `2026-09-02T12:00:00Z` into `{cycle}` and `{valid_time}`, matching the single spelling the backend serializes
- **AND** membership checks against the stored `valid_times[]` for `(source, cycle)` MUST compare on that same canonical spelling, so a `.000Z` state value never fails to match an API `...:00Z` entry
- **AND** the same canonicalization applies to the precipitation index and PNG URLs

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

### Requirement: MVT cold-generation busy tiles are retried per Retry-After
The M11 map SHALL load its MVT vector sources (the discharge overlay and the national river network) through a tile loader that retries a tile answered with `503` when that response carries a `Retry-After` header or an error body whose `error.code` is `MVT_COLD_GENERATION_BUSY`. The loader SHALL wait the `Retry-After` delay (clamped to a bounded range, with small jitter) and SHALL retry at most 3 times. Every other response status, and a busy tile whose retries are exhausted, SHALL fail exactly as a default MapLibre fetch would (an `AJAXError` preserving the HTTP status, so a `404` still reads as an empty tile). An aborted tile request SHALL cancel any pending wait and SHALL NOT issue further requests. The MVT URL template, `sourceKey` identity and cache-version query SHALL be unchanged.

#### Scenario: busy tile succeeds on retry
- **WHEN** a discharge tile request returns `503` with `Retry-After: 1` and body code `MVT_COLD_GENERATION_BUSY`, and the next attempt returns `200`
- **THEN** the loader waits about one second and resolves the tile with the second response's bytes
- **AND** no map error event is raised for that tile

#### Scenario: retries exhausted
- **WHEN** every attempt for a tile returns the busy `503`
- **THEN** the loader stops after 3 retries and fails the tile with an `AJAXError` whose status is 503

#### Scenario: non-busy failures are not retried
- **WHEN** a tile request returns `404`, `500`, or a `503` without `Retry-After` and without the busy code
- **THEN** the loader issues exactly one request and fails with an `AJAXError` carrying that status

#### Scenario: abandoned tile stops retrying
- **WHEN** MapLibre aborts a tile while its retry wait is pending
- **THEN** the wait is cancelled, the loader rejects with an abort error, and no further request is sent

### Requirement: Query state carries source, cycle, and precipitation toggle
`M11QueryState` SHALL carry `source` (`gfs|ifs|best|compare`; `defaultM11QueryState.source` becomes `'gfs'` and a parsed `best` is resolved to `gfs` at national scale), `cycle` (RFC3339 or null), `validTime`, and `precip: boolean` (default `true`). The constraint is on the exported surface of `apps/frontend/src/lib/m11/queryState.ts`: `parseM11QueryState` SHALL read `precip=0` as `false` and any other or absent value as `true`; `serializeM11QueryState` SHALL emit `precip=0` when the normalized state has `precip === false` and omit the parameter when it is `true`. Because `serializeM11QueryState` normalizes through `parseM11QueryState(queryParamsFromState(state))` and then rebuilds the query string from an explicit whitelist, the private `queryParamsFromState` helper — which today drops every `false` boolean — MUST be taught to carry `precip: false` through that internal round trip, and `precip` MUST be added to the whitelist; no other boolean's behaviour changes. The `M11Layer` union SHALL remain `'discharge'` only.

#### Scenario: Round trip with precipitation disabled
- **WHEN** `serializeM11QueryState(parseM11QueryState('precip=0'))` is evaluated
- **THEN** the returned query string still contains `precip=0`
- **AND** parsing that returned string yields `precip === false`
- **AND** `needsM11QueryReplacement('precip=0')` is `false`, so the URL is not rewritten away on load

#### Scenario: Serializing an explicit false state
- **WHEN** `serializeM11QueryState({ ...defaultM11QueryState, precip: false })` is evaluated
- **THEN** the result contains `precip=0`

#### Scenario: Default precipitation is on
- **WHEN** the URL has no `precip` parameter
- **THEN** `parseM11QueryState` returns `precip === true`
- **AND** `serializeM11QueryState` of that state omits the `precip` parameter

#### Scenario: Other booleans keep their existing serialization
- **WHEN** a state with `metStations: false` is serialized
- **THEN** the `metStations` parameter is omitted, unchanged from today's behaviour

#### Scenario: Layer enum unchanged
- **WHEN** any code path attempts `layer=precip`
- **THEN** the layer parser falls back to `discharge` and type checking rejects `'precip'` as an `M11Layer`
