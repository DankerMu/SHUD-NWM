## ADDED Requirements

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
- **AND** the store MUST keep `enrichmentLoading=true` until pipeline status, queue depth, flood summary, per-basin versions, and any other non-bootstrap fetch settle
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
- **AND** the bootstrap MUST NOT depend on `fetchRuns`, `fetchPipelineStatus`, `fetchQueueDepth`, `fetchFloodSummary`, `fetchFloodRanking`, `fetchBasinVersions`, or `fetchLayerValidTimes`

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

### Requirement: Default discharge run selection is independent of flood readiness
The system SHALL select the latest run for the default `discharge` overview path using only the layer's own readiness gate (frequency-ready selector on the backend), without forcing `flood_product_ready=true` on the `/api/v1/runs` query.

#### Scenario: Discharge layer is active
- **WHEN** `query.layer === 'discharge'` (default)
- **THEN** `fetchRuns(query)` MUST NOT append `flood_product_ready=true` to the request
- **AND** latest run selection MUST follow the backend's frequency-ready ordering

#### Scenario: Flood-return-period or warning-level layer is active
- **WHEN** `query.layer` ∈ {`flood-return-period`, `warning-level`}
- **THEN** `fetchRuns(query)` MUST append `flood_product_ready=true`
- **AND** ranking/summary panels for those layers MUST require the flood product readiness gate

#### Scenario: Layer toggle re-evaluates flood_product_ready filter
- **WHEN** `query.layer` transitions between `discharge` and `flood-return-period`/`warning-level` at runtime
- **THEN** the next `fetchRuns(query)` invocation MUST recompute the `flood_product_ready` query string for the new layer
- **AND** the latest-run selection MUST be re-resolved against the new run set rather than reused from the previous layer's cached result

### Requirement: Flood ranking is fetched on demand, not on overview bootstrap
The system SHALL NOT request `/api/v1/flood-alerts/ranking` as part of the default overview bootstrap. The request SHALL be issued only when the ranking panel is mounted or the active layer requires it (flood-return-period or warning-level).

#### Scenario: Default overview bootstrap omits ranking
- **WHEN** the default `best+discharge` overview loads
- **THEN** `loadOverview` MUST NOT call `fetchFloodRanking`
- **AND** `normalizeOverviewSummary` MUST NOT require ranking as input
- **AND** `normalizeOverviewBasins` MUST NOT require ranking to map basins to alerts
- **AND** `BasinDetailPanels` MUST tolerate an empty / `pending` `warningDistribution` until lazy ranking settles, without rendering a misleading "all zero warnings" state

#### Scenario: Ranking panel mounted
- **WHEN** the ranking panel component mounts, or `query.layer` switches to `flood-return-period` / `warning-level`
- **THEN** ranking MUST be fetched on demand
- **AND** an in-flight cache MUST coalesce concurrent panel mounts to one network round-trip

#### Scenario: Ranking fetch is cancelled on unmount or layer change
- **WHEN** the ranking panel unmounts, or `query.layer` toggles away from `flood-return-period` / `warning-level` while a ranking request is still in flight
- **THEN** the implementation MUST EITHER cancel the in-flight fetch OR discard its resolution
- **AND** no `setState` MUST occur on the unmounted/irrelevant context
- **AND** the in-flight cache entry MUST be cleared so the next mount / layer-switch issues a fresh request

## MODIFIED Requirements

### Requirement: Existing API contracts are reused first

The system SHALL compose current backend APIs before adding new aggregation endpoints, and SHALL avoid fetching endpoints whose results are not consumed by the rendered view models on the default path.

#### Scenario: Overview data loads from existing endpoints
- **WHEN** the overview page fetches data
- **THEN** it MUST use existing basins, model asset, flood alert, pipeline, tile, and river segment APIs where sufficient
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
