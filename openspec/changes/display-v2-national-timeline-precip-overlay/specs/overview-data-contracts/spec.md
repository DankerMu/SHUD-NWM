## MODIFIED Requirements

### Requirement: Default discharge tile URL is national across all `/api/v1/layers` callers

The backend `/api/v1/layers` catalog SHALL return the national source/cycle `discharge` tile URL template (`/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf`) regardless of whether the caller passes a `run_id` query parameter. This is a BREAKING change to the previous run-agnostic-but-source-agnostic template `/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf`, which stays served as a non-canonical alias route but is no longer the catalog value. The `river-network` layer SHALL retain its basin-scoped template and MUST NOT be affected by this requirement.

This guarantees that the default `discharge` overview renders **every basin's river segments** simultaneously (via the per-basin display-ready run for the requested `(source, cycle)`, selected server-side inside the `latest_runs` CTE of `postgis_tile_sql("hydro-national")` with the `:source` / `:cycle` binds), not just the basin whose latest run happened to win the global `latestPublishedRun` tiebreak. Source/cycle selection is explicit rather than implicit: the catalog advertises `metadata.default_source` and `metadata.default_cycle`, and every caller substitutes them (or the operator's selection) into the template. It also makes the `loadOverview` two-phase fetch sequence (mapBootstrap `fetchLayers(null)` followed by enrichment `fetchLayers(latestRun?.run_id)`) idempotent for the discharge layer: both phases observe the same tile URL template, the same `metadata.maplibre_source_layer`, the same `metadata.properties` set, the same `source_refs={}`, and therefore the same `metadata.version` (ETag hash input). The enrichment phase MUST NOT silently downgrade the discharge layer to a single-basin view.

#### Scenario: Runless `/api/v1/layers` catalog
- **WHEN** `GET /api/v1/layers` is issued without a `run_id` query parameter
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf'`
- **AND** that item MUST have `metadata.required_placeholders === ['source', 'cycle', 'valid_time', 'z', 'x', 'y']` (no `run_id` placeholder)
- **AND** that item MUST carry `metadata.default_source === 'gfs'`, a `metadata.default_cycle` equal to the newest cycle of `national_discharge_cycles(session, source='gfs')`, and `metadata.cycles_url_template` / `metadata.valid_times_url_template`
- **AND** that item's `metadata.valid_times` MUST be sourced from `national_discharge_valid_times(session, source=default_source, cycle=default_cycle)` — the 3-hour-stride list of that one `(default_source, default_cycle)`, NOT the previous union across each basin's latest display-ready run
- **AND** that item's `metadata.maplibre_source_layer` MUST equal `'hydro'`
- **AND** that item's `metadata.properties` MUST include `basin_id` (so click-to-curve resolves basin without an N+1 round-trip)

#### Scenario: Run-scoped `/api/v1/layers?run_id=<X>` catalog
- **WHEN** `GET /api/v1/layers?run_id=<concrete display-ready run>` is issued
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf'` — byte-identical to the runless case
- **AND** that item MUST NOT contain a `{run_id}` placeholder in its tile URL template or its `required_placeholders` array
- **AND** that item's `metadata.default_source`, `metadata.default_cycle` and `metadata.valid_times` MUST be the **same** `(default_source, default_cycle)` values as the runless call would return, intentionally ignoring the source and cycle of `<X>` because the discharge entry is always national and defaults-driven
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

#### Scenario: Runs exist but no cycle covers every basin
- **WHEN** `GET /api/v1/layers` is issued (runless) AND at least one basin has a display-ready run, but the fail-closed intersection in `national_discharge_cycles(session, source='gfs')` yields an empty cycle list
- **THEN** the `discharge` entry MUST still be returned, with `metadata.default_cycle === null` and `metadata.valid_times === []`
- **AND** this is NOT the "empty-discharge ghost" the previous scenario forbids: that scenario is about a catalog with zero display-ready runs anywhere, whereas here runs exist and the null cycle is the honest fail-closed signal the bottom control bar renders as the disabled cycle selector required by `map-layer-timeline-controls`
- **AND** the frontend MUST NOT request tiles with a fabricated or literal `{cycle}` segment while `default_cycle` is null

#### Scenario: Instants in the catalog use the seconds-precision spelling
- **WHEN** the `discharge` entry is returned by either caller shape
- **THEN** `metadata.default_cycle` and every `metadata.valid_times[]` entry match `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$`, the one spelling pinned by `mvt-tile-contract`

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
- **WHEN** the default `gfs+discharge` overview is opened (the national default source is `gfs`; a restored `source=best` resolves to `gfs` at national scale per `map-layer-timeline-controls`)
- **THEN** the mapBootstrap critical path MUST consist of: `fetchBasins`, `fetchLayers(null)` (runless catalog), and resolution of the current layer's valid_time from `metadata.valid_times`
- **AND** the bootstrap MUST NOT depend on `fetchRuns`, `fetchPipelineStatus`, `fetchQueueDepth`, `fetchBasinVersions`, `fetchLayerValidTimes`, the discharge cycles request, or the precipitation index request

### Requirement: Overview bootstrap cold latency budget
The system SHALL keep the default `gfs+discharge` overview cold first-paint within a defined latency budget so the receipt under `docs/runbooks/receipts/display-bootstrap-decoupling-<date>.md` (and, for this change, `docs/runbooks/receipts/<date>-display-v2.md`) is a regression contract, not a one-shot artifact. The budget is re-measured by this change because the runless catalog now evaluates the 38-network cycle intersection (`national_discharge_cycles`) and the per-cycle valid-time list inside `GET /api/v1/layers`, and because the precipitation overlay is on by default.

#### Scenario: Cold `/api/v1/layers` budget
- **WHEN** a force-refresh load issues `GET /api/v1/layers` (runless) and `GET /api/v1/layers?run_id=<latest>` on a cold cache, with the intersection and default-cycle valid-time queries included in the catalog computation
- **THEN** each response MUST return within ≤ 200 ms p95 on node-27 production hardware
- **AND** no other bootstrap-critical endpoint MUST exceed 500 ms p95
- **AND** the node-27 receipt for this change MUST record the measured cold p95 of `GET /api/v1/layers` before and after the change

#### Scenario: Cold first-paint interactivity budget
- **WHEN** the default `gfs+discharge` overview is opened on a cold cache with the precipitation overlay enabled and `loadOverview` is invoked
- **THEN** `mapBootstrapLoading` MUST settle to `false` within 1 s of `loadOverview` invocation on node-27 production hardware
- **AND** at least one MVT hit-layer MUST be registered with MapLibre by that point so a river segment is clickable
- **AND** the precipitation index fetch and the first PNG request MUST NOT be awaited before `mapBootstrapLoading` settles

## ADDED Requirements

### Requirement: Cycles and precipitation index requests stay off the bootstrap critical path
The bootstrap critical path SHALL remain `fetchBasins` + `fetchLayers(null)` + valid_time resolution from `metadata.valid_times` (the `Bootstrap minimal request set` scenario, restated above for the `gfs+discharge` national default). `GET /api/v1/layers/discharge/cycles`, `GET /api/v1/layers/discharge/valid-times?source=&cycle=` and `GET /api/v1/precip/{source}/{cycle}/index` SHALL be issued only after `mapBootstrapLoading` settles, as non-blocking enrichment fetches: the bottom control bar renders immediately from `metadata.default_source` / `metadata.default_cycle` / `metadata.valid_times`, the cycle selector shows only the default cycle until the cycles list arrives, and the precipitation raster is registered when its index arrives.

#### Scenario: Control bar renders from catalog metadata first
- **WHEN** `loadOverview` settles the bootstrap critical path
- **THEN** the timeline, source control and cycle selector are usable with the default cycle before any cycles or precip index response has arrived
- **AND** the store issues the cycles and precip index requests after `mapBootstrapLoading === false`, never as part of the awaited bootstrap promise

#### Scenario: Enrichment failure of cycles or precip index does not block the map
- **WHEN** the cycles or precip index request rejects
- **THEN** the map, discharge tiles and timeline for the default cycle stay interactive
- **AND** the failure surfaces as a scoped notice (cycle selector limited to the default cycle / precipitation unavailable), not as a bootstrap error
