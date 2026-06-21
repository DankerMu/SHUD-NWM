## ADDED Requirements

### Requirement: Default discharge tile URL is national across all `/api/v1/layers` callers

The backend `/api/v1/layers` catalog SHALL return the national `discharge` tile URL template (`/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf`) regardless of whether the caller passes a `run_id` query parameter. The `flood-return-period`, `warning-level`, and `river-network` layers SHALL retain their existing run-scoped or basin-scoped templates and MUST NOT be affected by this requirement.

This guarantees that the default `best+discharge` overview renders **every basin's river segments** simultaneously (via per-basin latest frequency-ready run selected server-side inside `postgis_tile_sql("hydro-national")`), not just the basin whose latest run happened to win the global `latestPublishedRun` tiebreak. It also makes the `loadOverview` two-phase fetch sequence (mapBootstrap `fetchLayers(null)` followed by enrichment `fetchLayers(latestRun?.run_id)`) idempotent for the discharge layer: both phases observe the same tile URL template, the same `metadata.maplibre_source_layer`, the same `metadata.properties` set, the same `source_refs={}`, and therefore the same `metadata.version` (ETag hash input). The enrichment phase MUST NOT silently downgrade the discharge layer to a single-basin view.

#### Scenario: Runless `/api/v1/layers` catalog
- **WHEN** `GET /api/v1/layers` is issued without a `run_id` query parameter
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf'`
- **AND** that item MUST have `metadata.required_placeholders === ['valid_time', 'z', 'x', 'y']` (no `run_id` placeholder)
- **AND** that item's `metadata.valid_times` MUST be sourced from `national_discharge_valid_times(session)` (union across each basin's latest frequency-ready run)
- **AND** that item's `metadata.maplibre_source_layer` MUST equal `'hydro'`
- **AND** that item's `metadata.properties` MUST include `basin_id` (so click-to-curve resolves basin without an N+1 round-trip)

#### Scenario: Run-scoped `/api/v1/layers?run_id=<X>` catalog
- **WHEN** `GET /api/v1/layers?run_id=<concrete frequency-ready run>` is issued
- **THEN** the response item with `layer_id === 'discharge'` MUST have `metadata.tile_url_template === '/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf'` — byte-identical to the runless case
- **AND** that item MUST NOT contain a `{run_id}` placeholder in its tile URL template or its `required_placeholders` array
- **AND** that item's `metadata.valid_times` MUST be the **same** `national_discharge_valid_times(session)` value as the runless call would return, intentionally including valid_times from runs other than `<X>` (this is the deliberate semantic departure — the caller's `run_id` scopes flood/warning per-run valid_times but the discharge entry is always national-union; this is by design, not a leak)
- **AND** that item's `metadata.maplibre_source_layer` MUST equal `'hydro'` (so MapLibre source identity is stable across the two-phase fetch and the browser does not drop and re-fetch tiles when bootstrap → enrichment transitions)

#### Scenario: Discharge catalog cache identity is run-agnostic
- **WHEN** `GET /api/v1/layers` and `GET /api/v1/layers?run_id=<X>` are both issued in succession
- **THEN** the response item with `layer_id === 'discharge'` from BOTH responses MUST have `metadata.source_refs === {}` (empty object)
- **AND** the discharge entry's `metadata.version` hash input MUST be byte-identical across the two responses, so the ETag is identical and CDN-level cache need not partition on `run_id` for the discharge entry
- **AND** this MUST hold even though the surrounding `/api/v1/layers` route may key its in-process `display_catalog_cached` entry on `f"layers:{run_id}:{limit}:{offset}"` (the per-layer ETag is the binding contract; the route-level cache key only affects flood/warning portions which DO differ by run)

#### Scenario: Flood-return-period and warning-level remain run-scoped
- **WHEN** `GET /api/v1/layers?run_id=<X>` is issued
- **THEN** the response items with `layer_id ∈ {'flood-return-period', 'warning-level'}` MUST have `metadata.tile_url_template` containing the `{run_id}` placeholder (`/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf`)
- **AND** their `metadata.valid_times` MUST continue to be sourced from `valid_times_for_layer(session, layer_id, run_id=<X>, ...)` (per-run valid-time discovery)
- **AND** the `river-network` layer MUST have `metadata.tile_url_template === '/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf'` AND `metadata.required_placeholders === ['basin_version_id', 'z', 'x', 'y']`
- **AND** no flood/warning/river-network field MUST be regressed by the discharge-specific change

#### Scenario: Frontend enrichment phase does not downgrade discharge
- **WHEN** `loadOverview` completes its enrichment phase, which calls `fetchLayers(useSingleRunFloodSurfaces ? latestRun?.run_id : null)` (see [apps/frontend/src/stores/overviewData.ts:1331](apps/frontend/src/stores/overviewData.ts:1331) and [:1511](apps/frontend/src/stores/overviewData.ts:1511))
- **THEN** the resulting `layers[].layer_id === 'discharge'` entry MUST have the national tile URL template — matching the value already observed during mapBootstrap, regardless of whether `latestRun` is null (which collapses to `fetchLayers(null)`) or a concrete run (which now also returns the national template because the backend ignores `run_id` for discharge layer URL selection)
- **AND** the MapLibre `hydro` source registered from the enrichment snapshot MUST consume the same national tile URL as the bootstrap snapshot, so MapLibre does NOT re-create the source layer and every basin's latest published-run river segments stay rendered on the map
- **AND** every basin with ≥1 frequency-ready published run MUST appear as clickable river segments at zoom ≥9, including basins that did NOT win the global `latestPublishedRun` tiebreak

#### Scenario: Unknown or non-ready `run_id` rejects the whole catalog
- **WHEN** `GET /api/v1/layers?run_id=<unknown-id>` is issued (no such run exists)
- **THEN** the response MUST be `404 RUN_NOT_FOUND` (existing contract via `_require_frequency_ready`)
- **AND** the discharge entry MUST NOT be returned as a side-channel — failure of the catalog gate MUST block the entire response, including discharge
- **WHEN** `GET /api/v1/layers?run_id=<exists-but-not-frequency-ready>` is issued
- **THEN** the response MUST be `409 FREQUENCY_NOT_COMPUTED` (existing contract)
- **AND** the discharge entry MUST NOT be returned as a side-channel — frequency-ready gate applies to the catalog as a whole

#### Scenario: No frequency-ready runs available
- **WHEN** `GET /api/v1/layers` is issued (runless) AND the database contains zero frequency-ready published runs across all basins
- **THEN** the response `data` MUST be `[]` (empty array — existing contract from `latest_frequency_ready_run(session) is None` branch)
- **AND** the `discharge` entry MUST NOT be synthesized with an empty `metadata.valid_times`; the entire catalog stays empty until at least one basin has a frequency-ready run, so the frontend layer panel can render an honest "no layers available" state instead of an empty-discharge ghost
