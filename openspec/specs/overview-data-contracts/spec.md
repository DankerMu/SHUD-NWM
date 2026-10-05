# overview-data-contracts Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.

## Requirements

### Requirement: Overview pages consume typed view models

The system SHALL isolate the national overview page from raw API response shapes through typed adapters or stores.

#### Scenario: Raw API data is normalized before page rendering
- **WHEN** overview page components or overview popups render basin, summary, segment, or forecast data
- **THEN** those components MUST consume typed frontend view models
- **AND** normalization of nullable fields, units, quality flags, timestamps, and display names MUST occur in adapters/stores rather than in leaf UI components

#### Scenario: Adapter tests cover required view models
- **WHEN** frontend tests run
- **THEN** they MUST cover normalization for overview basins, overview summaries, and layer state

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

#### Scenario: Segment ID is selected
- **WHEN** a segment is selected from the map
- **THEN** the same `river_segment_id` or API-required segment identifier MUST be used consistently for the forecast popup's series requests

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
- **WHEN** an overview query requests `source=compare`
- **THEN** selected-segment comparison surfaces MUST NOT be populated from a single run
- **AND** until a GFS+IFS aggregation/composition endpoint exists, those surfaces MUST expose a scoped unavailable or aggregation-needed state while source availability may still reflect the run set

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
- **AND** the store MUST keep `enrichmentLoading=true` until pipeline status, queue depth, per-basin versions, and any other non-bootstrap fetch settle, **except** the layer-time enrichment chain — the discharge cycles request, the per-cycle valid-times request, and the precipitation index request — which settles independently: each of the three carries its own scoped state, and `enrichmentLoading` MUST NOT wait on them. Callers needing to know whether the active cycle's valid-time list has arrived MUST read that layer's own state, not this flag
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
- **THEN** each response MUST return within ≤ 500 ms p95 on node-27 production hardware
- **AND** no other bootstrap-critical endpoint MUST exceed 500 ms p95
- **AND** the node-27 receipt for this change MUST record the measured cold p95 of `GET /api/v1/layers` both before and after the change, each computed from at least 10 cold samples taken in the same session by the same method — a 3-sample figure is a maximum, not a p95, and does not satisfy this clause
- **AND** the after-measurement MUST NOT exceed the before-measurement by more than 50 ms within that same receipt
- **AND** the receipt MUST also record, for the after-measurement, the coverage-query row count and `len(cycles)` returned by the runless catalog, so a later budget regression can be attributed to row growth rather than re-measured blind

> Budget note (user decision, i5-2009 round 2; supersedes the round-1 note). The threshold in this
> scenario was ≤ 200 ms when the change was authored. Three node-27 receipts measure master's own
> pre-change cold latency for `GET /api/v1/layers`:
> `docs/runbooks/receipts/issue-612-cold-waterfall-rerun-2026-06-21.md:42` (three cold TTFB samples,
> `Median` 392 ms, `Max` 405 ms), `docs/runbooks/receipts/display-bootstrap-decoupling-20260620.md:100`
> (three cold TTFB samples, `Median` 413 ms, `Max` 418 ms), and
> `docs/runbooks/receipts/2026-07-20-node27-display-scaling.md:181` (a single public sample at 0.331 s).
> **No existing receipt reports a p95 for `GET /api/v1/layers`** — the two tables carry `Median` and
> `Max` columns over three runs, which is why the ≥ 10-sample clause above exists. The 200 ms figure was
> therefore already unmet before any code in this change existed — a mis-set budget, not a regression
> introduced here.
>
> A round-1 note in this position claimed master sits at "331–392 ms" and set the threshold to 400 ms
> on that basis. That premise was wrong: 392 and 413 are medians, their own tables report maxima of
> 405 ms and 418 ms, and 0.331 s is one sample. Both three-sample tables exceed 400 ms at their maximum,
> so a 400 ms absolute threshold would have failed on master under any percentile reading of them. The user's corrected decision is a
> **500 ms absolute ceiling** — the same tier this scenario already applies to every other
> bootstrap-critical endpoint — **plus a regression clause**: within one receipt, measured by one
> method, `after − before` MUST NOT exceed 50 ms. The ceiling stops absolute drift; the regression
> clause is what stays red-capable against this change's own cost (the 38-network intersection plus
> the per-cycle valid-time query), which a 500 ms ceiling alone would not catch given master already
> measures 405–418 ms. Epic #2003's acceptance item 2 and issue #2009's acceptance criteria still
> spell 200 ms and are amended separately — see the PR's 偏离记录.

#### Scenario: Cold first-paint interactivity budget
- **WHEN** the default `gfs+discharge` overview is opened on a cold cache with the precipitation overlay enabled and `loadOverview` is invoked
- **THEN** `mapBootstrapLoading` MUST settle to `false` within 1 s of `loadOverview` invocation on node-27 production hardware
- **AND** at least one MVT hit-layer MUST be registered with MapLibre by that point so a river segment is clickable
- **AND** the precipitation index fetch and the first PNG request MUST NOT be awaited before `mapBootstrapLoading` settles

### Requirement: Default discharge run selection uses display readiness
The system SHALL select the latest run for the default `discharge` overview path using the layer's own display readiness gate, without any retired supplemental-product filter.

#### Scenario: Discharge layer is active
- **WHEN** `query.layer === 'discharge'` (default)
- **THEN** `fetchRuns(query)` MUST NOT append retired supplemental-product filters to the request
- **AND** latest run selection MUST follow the backend's display-ready ordering.

### Requirement: Default discharge tile URL is national across all `/api/v1/layers` callers

The backend `/api/v1/layers` catalog SHALL return the national source/cycle `discharge` tile URL template (`/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf`) regardless of whether the caller passes a `run_id` query parameter. This is a BREAKING change to the previous run-agnostic-but-source-agnostic template `/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf`, which stays served as a non-canonical alias route but is no longer the catalog value. The `river-network` layer SHALL retain its basin-scoped template and MUST NOT be affected by this requirement.

This guarantees that the default `discharge` overview renders **every basin's river segments** simultaneously (via the per-basin display-ready run for the requested `(source, cycle)`, selected server-side inside the `latest_runs` CTE of `postgis_tile_sql("hydro-national")` with the `:source` / `:cycle` binds), not just the basin whose latest run happened to win the global `latestPublishedRun` tiebreak. Source/cycle selection is explicit rather than implicit: the catalog advertises `metadata.default_source` and `metadata.default_cycle`, and every caller substitutes them (or the operator's selection) into the template. It also makes the `loadOverview` two-phase fetch sequence (mapBootstrap `fetchLayers(null)` followed by enrichment `fetchLayers(latestRun?.run_id)`) idempotent for the discharge layer: both phases observe the same tile URL template, the same `metadata.maplibre_source_layer`, the same `metadata.properties` set, the same `source_refs={}`, and therefore the same `metadata.version` (ETag hash input). The enrichment phase MUST NOT silently downgrade the discharge layer to a single-basin view.

#### Scenario: Runless `/api/v1/layers` catalog
- **WHEN** `GET /api/v1/layers` is issued without a `run_id` query parameter
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf'`
- **AND** that item MUST have `metadata.required_placeholders === ['source', 'cycle', 'valid_time', 'z', 'x', 'y']` (no `run_id` placeholder)
- **AND** that item MUST carry `metadata.default_source === 'gfs'`, a `metadata.default_cycle` equal to the newest cycle of `national_discharge_cycles(session, source='gfs')`, and `metadata.cycles_url_template` / `metadata.valid_times_url_template`
- **AND** `metadata.default_cycle` MUST instead be `null` whenever the resolved `metadata.valid_times` is empty, even if `national_discharge_cycles` returned a non-empty list: a cycle the catalog cannot prove a valid-time list for MUST NOT be advertised as the default, because the frontend would substitute it into the tile template and request tiles the backend refuses to serve. This is the fail-closed pairing of `default_cycle` and `valid_times` — the two are advertised together or not at all
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

### Requirement: A validTime-only query change re-derives the overview without reloading
`useOverviewDataStore.loadOverview` SHALL treat `validTime` as a derivation input, not as request identity. When the query differs from the active overview request only in `validTime` (after the existing `precip` normalisation), the store MUST NOT issue any HTTP request, MUST NOT start a new request generation, and MUST re-derive the validTime-dependent parts of the snapshot from the inputs the active request already holds.

#### Scenario: Timeline step issues no request
- **WHEN** an overview load for a query has settled
- **AND** `loadOverview` is called again with the same query except `validTime`
- **THEN** no request is issued
- **AND** `mapBootstrapLoading`, `enrichmentLoading`, `bootstrapError`, `error`, `cyclesBySource`, `validTimesByCycle` and `precipIndexByCycle` keep their values

#### Scenario: In-flight enrichment survives a timeline step
- **WHEN** a load is still waiting for cycles, per-cycle valid times or the precipitation index
- **AND** `loadOverview` is called with the same query except `validTime`
- **THEN** the pending responses are still written to the store when they arrive
- **AND** the layer states they re-derive use the latest `validTime`

#### Scenario: Consumers follow the new validTime
- **WHEN** a validTime-only call has been made
- **THEN** each layer state's `currentValidTime`, the snapshot `requestScope` (so `overviewSnapshotMatchesQuery` matches the new query) and the summary freshness and provenance reflect the new `validTime`
- **AND** the store state equals what a fresh load of the new query would produce from the same responses

#### Scenario: Identity change still reloads
- **WHEN** `loadOverview` is called with a query that differs in `source`, `cycle`, `layer`, `basinVersionId`, `riverNetworkVersionId`, `segmentId` or `q`
- **THEN** a new request generation starts exactly as before this requirement

#### Scenario: Identical query after settle still reloads
- **WHEN** a load has settled and `loadOverview` is called again with an identical query (for example after `OverviewPage` remounts)
- **THEN** a new request generation starts as before this requirement, so expired cache entries are refetched and a failed bootstrap is retried

### Requirement: Map bootstrap failure is surfaced regardless of the basin list
OverviewPage SHALL render the scoped bootstrap error as soon as the map surface has settled, whether or not enrichment recovered a non-empty basin list, and the bootstrap-failure notice SHALL take precedence over any precipitation overlay notice.

#### Scenario: Bootstrap failed but basins recovered
- **WHEN** the runless layer catalog rejects during bootstrap
- **AND** enrichment later yields a non-empty basin list and a usable catalog
- **THEN** the overview notice shows the `bootstrapError` text
- **AND** no precipitation notice is shown at the same time

### Requirement: One discharge catalog identity per overview load
Within one `loadOverview` request generation whose map bootstrap succeeded with a `discharge` catalog entry, every resolution of the national discharge `(source, cycle)` pair SHALL read the `discharge` catalog entry of the bootstrap (runless) snapshot, so the keys written by layer-time enrichment and the keys read when deriving layer states and the precipitation overlay are the same. A run-scoped catalog fetched later in the same generation MUST NOT replace that entry.

#### Scenario: Default cycle flips between the two catalog fetches (no URL cycle)
- **WHEN** the runless catalog advertises discharge `default_cycle` C1 and the run-scoped catalog fetched in the same generation advertises C2 ≠ C1
- **AND** the URL carries no `cycle`
- **THEN** the discharge layer's `activeNationalCycle` is C1 and its valid times are the runless catalog's list
- **AND** the precipitation index is requested for C1 and the overlay resolves against C1 instead of staying hidden as pending

#### Scenario: Deep link to the runless default cycle
- **WHEN** the same flip happens and the URL carries `cycle=C1`
- **THEN** the discharge layer is available with the runless catalog's list and never shows the pending reason while no valid-times request is in flight

#### Scenario: Runless catalog has no discharge entry
- **WHEN** the runless catalog succeeds without a `discharge` entry and the run-scoped catalog carries one
- **THEN** the run-scoped discharge entry is used, as before this requirement

#### Scenario: Bootstrap failed
- **WHEN** the map bootstrap fails (for example the basin list rejects) and the run-scoped catalog succeeds
- **THEN** the run-scoped discharge entry is used, as before this requirement

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
