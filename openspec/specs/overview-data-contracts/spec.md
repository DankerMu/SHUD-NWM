# overview-data-contracts Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.
## Requirements
### Requirement: Overview pages consume typed view models

The system SHALL isolate national overview and basin drill-down pages from raw API response shapes through typed adapters or stores.

#### Scenario: Raw API data is normalized before page rendering
- **WHEN** overview or basin page components render basin, summary, segment, forecast, or lineage data
- **THEN** those components MUST consume typed frontend view models
- **AND** normalization of nullable fields, units, quality flags, timestamps, and display names MUST occur in adapters/stores rather than in leaf UI components

#### Scenario: Adapter tests cover required view models
- **WHEN** frontend tests run
- **THEN** they MUST cover normalization for overview basins, overview summaries, layer state, basin detail, segment rows, and selected segment detail

### Requirement: Existing API contracts are reused first

The system SHALL compose current backend APIs before adding new aggregation endpoints, and SHALL avoid fetching endpoints whose results are not consumed by the rendered view models on the default path.

#### Scenario: Overview data loads from existing endpoints
- **WHEN** the overview page fetches data
- **THEN** it MUST use existing basins, model asset, pipeline, tile, and river segment APIs where sufficient
- **AND** new endpoints MUST NOT be added solely for convenience if frontend adapters can satisfy the requirement within acceptable complexity

#### Scenario: Aggregation endpoint is justified
- **WHEN** an implementation adds `GET /api/v1/overview/summary`, `GET /api/v1/basins/{basin_id}/summary`, or another M11 aggregation endpoint
- **THEN** the PR MUST include OpenAPI updates, generated frontend types, backend route/schema tests, and frontend adapter tests
- **AND** the endpoint MUST be read-only and scoped to fields required by the M11 pages

#### Scenario: Unused-response fetches are removed
- **WHEN** a default-path fetch returns a payload that no rendered view model consumes
- **THEN** the call MUST be removed from the default path
- **AND** moved to the on-demand trigger (panel mount or layer change) that actually consumes it
- **AND** removal MUST include the corresponding `normalize*` argument cleanup so callers cannot silently re-introduce the dead call

### Requirement: ID and version fields remain explicit

The system SHALL preserve domain IDs and version identifiers across view models, routes, and handoff links.

#### Scenario: Basin version is selected
- **WHEN** a basin detail page chooses a basin version
- **THEN** the selected `basin_version_id` MUST be visible in state and in the UI where the design calls for it
- **AND** segment API calls MUST use the selected basin version rather than an implicit global version

#### Scenario: Segment ID is selected
- **WHEN** a segment is selected from the map, list, or URL query
- **THEN** the same `river_segment_id` or API-required segment identifier MUST be used consistently for detail, forecast series, lineage, and handoff links

### Requirement: Data freshness and unavailable states are represented

The system SHALL distinguish current data, stale data, unavailable data, and partial failures in the view models.

#### Scenario: Latest update is available
- **WHEN** a summary or layer payload includes latest update, cycle, run, or valid-time metadata
- **THEN** the view model MUST expose that freshness metadata to the summary panel or timeline

#### Scenario: Data is unavailable
- **WHEN** a required field or endpoint is unavailable
- **THEN** the view model MUST expose an unavailable reason or quality note
- **AND** UI components MUST show a scoped empty/disabled/error state instead of fabricating values

#### Scenario: Compare detail surfaces need aggregation
- **WHEN** an overview or basin detail query requests `source=compare`
- **THEN** selected-segment comparison surfaces and lineage MUST NOT be populated from a single run
- **AND** until a GFS+IFS aggregation/composition endpoint exists, those surfaces MUST expose a scoped unavailable or aggregation-needed state while source availability may still reflect the run set

#### Scenario: URL segment is not in filtered rows
- **WHEN** a basin detail URL supplies `segmentId`
- **THEN** selected-segment API identities MUST resolve from a matching filtered row or the selected basin-version feature collection
- **AND** the resolver MUST NOT fall back to the first filtered row for a supplied but unresolvable segment ID

### Requirement: Map interactivity is decoupled from enrichment loading
The system SHALL split the single `loading` flag in `useOverviewDataStore` into two independent flags so that map interactivity (MVT hit-layer registration) is not gated on non-essential enrichment requests. The flags are `mapBootstrapLoading` and `enrichmentLoading`.

#### Scenario: Initial state before loadOverview
- **WHEN** the store is constructed and `loadOverview` has not yet been called
- **THEN** both `mapBootstrapLoading` and `enrichmentLoading` MUST be `false`
- **AND** `overview` MUST be `null`
- **AND** callers MUST treat the (false, false, null) tuple as "not yet bootstrapped", not as "ready / empty"

#### Scenario: Map bootstrap completes before enrichment
- **WHEN** `loadOverview` runs and the bootstrap critical path settles
- **THEN** the store MUST set `mapBootstrapLoading=false` once basins, runless layers catalog, and the selected layer's valid_time are settled
- **AND** the store MUST keep `enrichmentLoading=true` until pipeline status, queue depth, per-basin versions, and any other non-bootstrap fetch settle
- **AND** the OverviewPage `surfaceSettling` indicator MUST react only to `mapBootstrapLoading || !overview?.bootstrap`, not to `enrichmentLoading`

#### Scenario: Map bootstrap rejection
- **WHEN** the mapBootstrap critical-path fetch (basins or runless layers) rejects
- **THEN** `mapBootstrapLoading` MUST settle to `false` with a scoped bootstrap-error state
- **AND** `enrichmentLoading` MUST NOT block on the failed bootstrap promise
- **AND** OverviewPage MUST render a truthful "bootstrap failed" state rather than an indefinite spinner

#### Scenario: Enrichment failure does not block map
- **WHEN** any enrichment fetch (pipeline, queue, summary, per-basin versions) rejects or yields partialError
- **THEN** the map MUST remain interactive
- **AND** the failure MUST surface as a scoped enrichment error or unavailable badge in the affected panel only

#### Scenario: Bootstrap minimal request set
- **WHEN** the default `best+discharge` overview is opened
- **THEN** the mapBootstrap critical path MUST consist of: `fetchBasins`, `fetchLayers(null)` (runless catalog), and resolution of the current layer's valid_time from `metadata.valid_times`
- **AND** the bootstrap MUST NOT depend on `fetchRuns`, `fetchPipelineStatus`, `fetchQueueDepth`, `fetchBasinVersions`, or `fetchLayerValidTimes`

### Requirement: Overview bootstrap cold latency budget
The system SHALL keep the default `best+discharge` overview cold first-paint within a defined latency budget so the receipt under `docs/runbooks/receipts/display-bootstrap-decoupling-<date>.md` is a regression contract, not a one-shot artifact.

#### Scenario: Cold `/api/v1/layers` budget
- **WHEN** a force-refresh load issues `GET /api/v1/layers` (runless) and `GET /api/v1/layers?run_id=<latest>` on a cold cache
- **THEN** each response MUST return within ≤ 200 ms p95 on node-27 production hardware
- **AND** no other bootstrap-critical endpoint MUST exceed 500 ms p95

#### Scenario: Cold first-paint interactivity budget
- **WHEN** the default `best+discharge` overview is opened on a cold cache and `loadOverview` is invoked
- **THEN** `mapBootstrapLoading` MUST settle to `false` within 1 s of `loadOverview` invocation on node-27 production hardware
- **AND** at least one MVT hit-layer MUST be registered with MapLibre by that point so a river segment is clickable

### Requirement: Default discharge run selection uses display readiness
The system SHALL select the latest run for the default `discharge` overview path using the layer's own display readiness gate, without any retired supplemental-product filter.

#### Scenario: Discharge layer is active
- **WHEN** `query.layer === 'discharge'` (default)
- **THEN** `fetchRuns(query)` MUST NOT append retired supplemental-product filters to the request
- **AND** latest run selection MUST follow the backend's display-ready ordering.

### Requirement: Default discharge tile URL is national across all `/api/v1/layers` callers

The backend `/api/v1/layers` catalog SHALL return the national `discharge` tile URL template (`/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf`) regardless of whether the caller passes a `run_id` query parameter. The `river-network` layer SHALL retain its basin-scoped template and MUST NOT be affected by this requirement.

This guarantees that the default `best+discharge` overview renders **every basin's river segments** simultaneously (via per-basin latest display-ready run selected server-side inside `postgis_tile_sql("hydro-national")`), not just the basin whose latest run happened to win the global `latestPublishedRun` tiebreak. It also makes the `loadOverview` two-phase fetch sequence (mapBootstrap `fetchLayers(null)` followed by enrichment `fetchLayers(latestRun?.run_id)`) idempotent for the discharge layer: both phases observe the same tile URL template, the same `metadata.maplibre_source_layer`, the same `metadata.properties` set, the same `source_refs={}`, and therefore the same `metadata.version` (ETag hash input). The enrichment phase MUST NOT silently downgrade the discharge layer to a single-basin view.

#### Scenario: Runless `/api/v1/layers` catalog
- **WHEN** `GET /api/v1/layers` is issued without a `run_id` query parameter
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf'`
- **AND** that item MUST have `metadata.required_placeholders === ['valid_time', 'z', 'x', 'y']` (no `run_id` placeholder)
- **AND** that item's `metadata.valid_times` MUST be sourced from `national_discharge_valid_times(session)` (union across each basin's latest display-ready run)
- **AND** that item's `metadata.maplibre_source_layer` MUST equal `'hydro'`
- **AND** that item's `metadata.properties` MUST include `basin_id` (so click-to-curve resolves basin without an N+1 round-trip)

#### Scenario: Run-scoped `/api/v1/layers?run_id=<X>` catalog
- **WHEN** `GET /api/v1/layers?run_id=<concrete display-ready run>` is issued
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf'` — byte-identical to the runless case
- **AND** that item MUST NOT contain a `{run_id}` placeholder in its tile URL template or its `required_placeholders` array
- **AND** that item's `metadata.valid_times` MUST be the **same** `national_discharge_valid_times(session)` value as the runless call would return, intentionally including valid_times from runs other than `<X>` because the discharge entry is always national-union
- **AND** that item's `metadata.maplibre_source_layer` MUST equal `'hydro'` (so MapLibre source identity is stable across the two-phase fetch and the browser does not drop and re-fetch tiles when bootstrap → enrichment transitions)

#### Scenario: Discharge catalog cache identity is run-agnostic
- **WHEN** `GET /api/v1/layers` and `GET /api/v1/layers?run_id=<X>` are both issued in succession
- **THEN** the response item with `layer_id === 'discharge'` from BOTH responses MUST have `metadata.source_refs === {}` (empty object)
- **AND** the discharge entry's `metadata.version` hash input MUST be byte-identical across the two responses, so the ETag is identical and CDN-level cache need not partition on `run_id` for the discharge entry
- **AND** this MUST hold even though the surrounding `/api/v1/layers` route may key its in-process `display_catalog_cached` entry on `f"layers:{run_id}:{limit}:{offset}"`.

#### Scenario: River-network remains basin-scoped
- **WHEN** `GET /api/v1/layers?run_id=<X>` is issued
- **THEN** the `river-network` layer MUST have `metadata.tile_url_template === '/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf'` AND `metadata.required_placeholders === ['basin_version_id', 'z', 'x', 'y']`.

#### Scenario: Frontend enrichment phase does not downgrade discharge
- **WHEN** `loadOverview` completes its enrichment phase, which calls `fetchLayers(latestRun?.run_id ?? null)`
- **THEN** the resulting `layers[].layer_id === 'discharge'` entry MUST have the national tile URL template — matching the value already observed during mapBootstrap, regardless of whether `latestRun` is null (which collapses to `fetchLayers(null)`) or a concrete run (which now also returns the national template because the backend ignores `run_id` for discharge layer URL selection)
- **AND** the MapLibre `hydro` source registered from the enrichment snapshot MUST consume the same national tile URL as the bootstrap snapshot, so MapLibre does NOT re-create the source layer and every basin's latest published-run river segments stay rendered on the map
- **AND** every basin with ≥1 display-ready published run MUST appear as clickable river segments at zoom ≥9, including basins that did NOT win the global `latestPublishedRun` tiebreak

#### Scenario: Unknown or non-ready `run_id` rejects the whole catalog
- **WHEN** `GET /api/v1/layers?run_id=<unknown-id>` is issued (no such run exists)
- **THEN** the response MUST be `404 RUN_NOT_FOUND`
- **AND** the discharge entry MUST NOT be returned as a side-channel — failure of the catalog gate MUST block the entire response, including discharge
- **WHEN** `GET /api/v1/layers?run_id=<exists-but-not-display-ready>` is issued
- **THEN** the response MUST be an explicit not-ready error envelope
- **AND** the discharge entry MUST NOT be returned as a side-channel — display-ready gate applies to the catalog as a whole

#### Scenario: No display-ready runs available
- **WHEN** `GET /api/v1/layers` is issued (runless) AND the database contains zero display-ready published runs across all basins
- **THEN** the response `data` MUST be `[]`
- **AND** the `discharge` entry MUST NOT be synthesized with an empty `metadata.valid_times`; the entire catalog stays empty until at least one basin has a display-ready run, so the frontend layer panel can render an honest "no layers available" state instead of an empty-discharge ghost
