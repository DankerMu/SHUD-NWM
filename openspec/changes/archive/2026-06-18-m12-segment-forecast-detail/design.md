## Context

M12 segment forecast detail follows the completed M11 overview/basin drill-down delivery and turns a documented product gap into implementable, testable work. Existing production-like closure and M11 behavior must remain stable.

## Design Decisions

- Canonical route state uses frontend query keys `source`, `cycle`, `validTime`, `basinVersionId`, `riverNetworkVersionId`, `segmentId`; API calls use existing snake_case parameters such as `cycle_time`, `valid_time`, `basin_version_id`, `river_network_version_id`, and `river_segment_id`.
- The implementation must write an endpoint decision note before adding any backend aggregation endpoint, listing existing calls and missing required fields.
- KPI and chart values must be derived from forecast/analysis series and return-period curves; unavailable fields render explicit degraded states.
- The 120x90 location thumbnail is non-interactive and may use simplified basin/segment geometry within a fixed geometry budget.
- Station/forcing rendering must use only safe existing contracts. The segment detail route does not carry a trusted `station_id`, `forcing_version_id`, or `run_id`, so it must not call live station/forcing endpoints by guessing identifiers; optional `properties_json.station_forcing` data on the scoped segment may render real station/forcing values, restricted reasons render as restricted states, and absent/incomplete payloads render unavailable states.

## OpenSpec Fixture

Fixture level: expanded
Project profile: other
Repair intensity: high

Change surface:
- Frontend route table and navigation handoffs from basin detail, forecast, and flood alert pages.
- Segment detail view model, forecast chart composition, location thumbnail, station/forcing, frequency/weather panels, and route reload behavior.
- Existing `/forecast` page and M11 basin/flood data contracts remain downstream consumers.

Must preserve:
- Existing `/forecast`, `/basins/:basinId`, and `/flood-alerts` tests and query behavior.
- Segment identity must stay bound to `basinVersionId` and `riverNetworkVersionId`; a stale or invalid segment must not fall back to a sibling network.
- The UI must not fabricate station, forcing, threshold, weather, or geometry values when the current contracts do not provide them.

Must add/change:
- A full-screen restorable segment detail route with canonical query state.
- Handoff links that carry source, cycle, valid time, basin version, river network version, and segment identity.
- Explicit unavailable/partial states for missing station/forcing/frequency/weather data.

Risk packs considered:
- Public API / CLI / script entry: selected - adds a public SPA route and cross-page navigation contract.
- Config / project setup: not selected - no runtime config or project setup change is expected.
- File IO / path safety / overwrite: not selected - no local file read/write, upload, delete, or publish surface is expected.
- Schema / columns / units / field names: selected - normalizes existing API fields, URL query keys, units, thresholds, and availability flags.
- Geospatial / CRS / shapefile sidecars: selected - renders bounded basin/segment geometry in a location thumbnail and must handle unavailable geometry.
- Time series / forcing / temporal boundaries: selected - preserves cycle/valid time route state and renders analysis/forecast/forcing series.
- Numerical stability / conservation / NaN: selected - charts/KPIs must reject missing or non-finite numeric values rather than displaying misleading values.
- Solver runtime / performance / threading: not selected - no solver/runtime execution path changes.
- Resource limits / large input / discovery: selected - geometry and chart series rendering must stay bounded and avoid unbounded full-national data assumptions.
- Legacy compatibility / examples: selected - existing M11 forecast, basin, and flood workflows must keep working.
- Error handling / rollback / partial outputs: selected - invalid stale segment, missing river network, failed data load, and partial panels must render stable states.
- Release / packaging / dependency compatibility: not selected - no dependency or release packaging change is expected.
- Documentation / migration notes: selected - `progress.md` and any endpoint decision note must document delivered scope and limitations.

Boundary-surface checklist:
- Public entrypoints: `/segments/:segmentId`, `/forecast`, `/basins/:basinId`, `/flood-alerts`.
- Read surfaces: existing frontend API/store readers for basin detail, forecast series, flood alert ranking/timeline, and route query parsing.
- Producer/consumer boundaries: basin/flood handoff links produce route state; segment detail consumes it and binds API calls to the same basin/network/segment identity.
- Stale-state/idempotency boundaries: reload, repeated navigation to the same segment, invalid segment, missing river network version, and stale valid time correction.
- Unchanged downstream consumers: existing forecast panel/chart, M11 basin shell, flood alert detail, App route tests, and E2E forecast/flood routes.

## Dependency Order

- Route/query handoffs before data model.
- Data model before KPI/chart/panels.
- Panel/chart implementation before Playwright screenshot evidence.

## Risks and Mitigations

- Risk: stale segment identity leaks across basin versions. Mitigation: route restoration includes basinVersionId and riverNetworkVersionId and clears invalid segment state.
- Risk: chart hides missing frequency thresholds. Mitigation: threshold rows render unavailable labels and tests.
- Risk: station/forcing data not available. Mitigation: explicit restricted/unavailable panel, no fake data.

## Verification

- `openspec validate m12-segment-forecast-detail --strict`
- `cd apps/frontend && corepack pnpm test && corepack pnpm exec tsc --noEmit && corepack pnpm build`
- Focused Playwright route/handoff tests for basin -> segment detail, flood ranking -> segment detail, and reload query restoration.
