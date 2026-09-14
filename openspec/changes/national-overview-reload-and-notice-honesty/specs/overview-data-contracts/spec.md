## ADDED Requirements

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
