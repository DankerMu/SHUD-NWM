## Context

The React frontend already has a Vite/TypeScript structure, AppShell navigation, MapLibre forecast map components, flood alert pages, monitoring pages, generated OpenAPI types, and stores for forecast, flood alert, model assets, and monitoring. M11 should extend those foundations rather than start a new frontend architecture.

The design documents define two linked operator workflows:

1. National overview: default homepage with basin/layer controls, national map, right-side operational summaries, and bottom timeline.
2. Basin detail: entered from a basin popup or hydrologic forecast navigation, with basin-scoped river list, map interactions, segment detail, and trend sparkline.

M11 intentionally stops at the overview-to-basin workflow. It keeps handoff points for full-screen segment detail, meteorology pages, and model asset management without implementing those larger pages in this stage.

## Decisions

### D1: Route shape and compatibility

Use the national overview as the default user-facing entry while keeping existing workflows reachable:

- `/` renders `OverviewPage`.
- `/overview` is an explicit alias for the same page.
- `/basins/:basinId` renders `BasinDetailPage`.
- Existing forecast functionality remains reachable through `/forecast` or a backward-compatible alias decided during implementation.
- Existing `/flood-alerts` and `/monitoring` routes remain unchanged.

The route migration must include tests for default render, direct deep links, and existing route availability. If existing tests assume `/` is `ForecastPage`, update them to assert the new route contract and add `/forecast` coverage for the preserved forecast page.

### D2: Reuse current API contracts before adding aggregation endpoints

The first implementation should compose existing endpoints in frontend adapters:

- Basin inventory: `/api/v1/basins` and `/api/v1/basins/{basin_id}/versions`.
- Model/version context: `/api/v1/models` and `/api/v1/models/{model_id}`.
- River segments and selected segment details: `/api/v1/basin-versions/{basin_version_id}/river-segments` and `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}`.
- Forecast series: `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`.
- Flood alert summaries, ranking, segments, and timeline: `/api/v1/flood-alerts/summary`, `/ranking`, `/segments`, and `/timeline`.
- Current map risk layer: `/api/v1/tiles/flood-return-period`.
- Pipeline and operational summaries: `/api/v1/pipeline/*`, jobs, metrics, and queue endpoints already used by monitoring.
- Lineage and quality drill-through: `/api/v1/lineage/river-point`.

Add read-only aggregation endpoints only if adapter composition creates repeated N+1 calls or cannot satisfy the page within the design performance targets. Any new endpoint must be OpenAPI-first, covered by backend tests, and generated into frontend types before page code consumes it. Candidate endpoints, if justified, are:

- `GET /api/v1/overview/summary`
- `GET /api/v1/basins/{basin_id}/summary`

Issue #161 closure note: the overview adapter may fetch real basin-version/bbox data only
when the measured request plan stays inside the documented threshold and does not create
per-basin N+1 fan-out. When basin-version/bbox composition would require N+1 or exceed the
request budget, page-facing links and summaries must mark that surface aggregation-needed
instead of fabricating basin-version IDs or claiming full reuse of existing APIs.

### D3: View-model adapters as the contract between pages and APIs

Pages and components should consume typed view models rather than raw API responses. Adapters should normalize nullable fields, IDs, warning levels, units, time strings, and empty collections.

Suggested view-model groups:

- `OverviewBasin`: basin id/name/level/parent, bbox, area, river count, active model count, latest forecast time, warning counts, available basin versions.
- `OverviewSummary`: completed cycles today, currently running jobs, warning segment count, latest update, data source/cycle labels, quality notes.
- `LayerState`: layer id, group, availability, valid times, current valid time, legend, disabled reason.
- `BasinDetail`: basin identity, selected basin version, bbox, segment count, warning distribution, latest run metadata.
- `BasinSegmentRow`: river segment id, display name, current Q, return period, warning level, quality flag, source, cycle time.
- `SelectedSegmentDetail`: IDs, basin/model metadata, current forecast values, data source, lineage/quality status, trend points, and handoff URLs.

For M11 #161, the page-facing view models include typed placeholders and unavailable states
for surfaces that are required by the larger OpenSpec but not rendered as first-class page
components in this issue: pipeline stages, job tables, stage-duration/success-rate metrics,
flood return-period map features/tiles, flood-alert segment lists, and full lineage graph
visualization. The adapter composes pipeline status, queue depth, flood summary/ranking,
layer valid-times, forecast series, timeline, and lineage status; richer monitoring tables,
metrics charts, flood map rendering, and lineage graph UI remain downstream page/component
work and must not be silently represented with fake values.

### D4: Shared map primitives, not one-off map implementations

Reuse and extend existing MapLibre components under `components/map`. Shared primitives should handle map container lifecycle, fit/fly-to behavior, hover/click callbacks, empty/error overlays, and source/layer registration. Overview and basin pages can provide different layer definitions and style expressions.

M11 should not require production MVT. For this stage, it can use available GeoJSON and existing river segment endpoints. The implementation must make the data-source choice explicit so future MVT migration is contained to map source adapters.

### D5: Timeline is data-driven

The timeline must use `GET /api/v1/layers` and `GET /api/v1/layers/{layer_id}/valid-times` as the primary layer-time contract. It must not synthesize a fixed hourly range when data is unavailable. If a non-layer payload such as a segment forecast or flood alert timeline is the active data source, the adapter may derive valid times from that payload, mark the source as derived, and expose a disabled/empty state when no times exist.

Layer switching must update the timeline's valid-time list and must prevent stale valid-time selections from rendering mismatched map data.

### D6: Source/scenario controls drive data selection

M11 must expose the source/scenario choices required by the GIS design: `GFS`, `IFS`, `GFS + IFS 对比`, and `Best Available`. These choices are not cosmetic; they drive map layers, summaries, selected segment forecast data, timeline valid times, and comparison availability.

If one source is unavailable for the selected basin/segment/cycle, the UI should show a disabled or unavailable state with provenance rather than silently falling back. `Best Available` must expose which source/run was chosen so operators can interpret the map and detail data.

### D7: URL query owns shareable operator state

Persist shareable state in the URL query:

- `source`
- `cycle`
- `validTime`
- `layer`
- `basemap`
- `basinVersionId`
- `segmentId`
- `warningLevel`
- `q`

Per-basin visibility selection can stay in store state for M11 unless implementation explicitly chooses a compact `visibleBasins` query parameter and tests it.

Local UI-only state such as panel collapse or hover state can stay in component/store state. Invalid query values should fall back to the nearest valid option and surface a non-blocking toast or inline notice only when the correction affects visible data.

### D8: Dense operational UI and effect-image conformance

Follow `docs/spec/06B_frontend_ui_design_spec.md`:

- Map remains the main subject; panels are compact and information-dense.
- Side panels use the documented widths where viewport allows: left around 280px, right around 320-360px.
- Cards and panels use restrained radius and existing design tokens.
- Status and warning colors are consistent with flood alert levels and existing monitoring status colors.
- Icon buttons use existing UI/icon patterns; visible text should not explain controls that are already standard.
- The pages must visually match the effect-image intent in `docs/spec/06_frontend_gis_design.md`: national overview corresponds to effect image 1, basin detail corresponds to effect images 2/3 handoff scope, flood alert and monitoring links remain consistent with their existing pages.
- Top navigation height, tab styling, side-panel widths, popup card structure, timeline height, timeline controls, fonts, spacing, radii, shadows, state colors, loading/empty/error states, and responsive breakpoint behavior are acceptance criteria, not optional styling guidance.
- `agent-browser` screenshots must be captured for overview and basin detail at the documented viewport classes. The required launch pattern is `agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>`; `--args` is a global parameter and must appear before the `open` subcommand. Playwright remains responsible for route and interaction E2E coverage.

### D9: Explicit loading, error, and empty states

Both pages must handle:

- no basin inventory
- basin exists but has no published basin version
- basin version exists but has no river segment data
- forecast/flood alert data unavailable for the selected source/cycle/valid time
- map source load failure
- partial backend/API failure where summary cards can still render
- disabled meteorology layers until their data contracts exist

### D10: Performance targets are measured during implementation

M11 should track the design performance targets as implementation acceptance criteria:

- national initial map load P95 target < 5 seconds
- layer switch P95 target < 2 seconds
- time-step switch P95 target < 1 second when cache/data is available
- river segment click detail P95 target < 2 seconds

Automated tests do not need to prove production P95, but implementation issues must include local interaction/performance evidence or an explicit note when a target cannot be measured with mocked data.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Overview page creates many API calls on load | Batch through adapters, cache shared resources in stores, and add aggregation endpoints only after measuring the composition problem. |
| Existing `/` forecast tests or bookmarks break | Preserve `/forecast`, update route tests, and optionally add a compatibility redirect if old user flows require it. |
| Map rendering becomes page-specific and duplicated | Extract shared map source/layer primitives before building the second page. |
| Valid-time behavior diverges between pages | Implement a shared timeline state/helper with tests for layer switching and stale valid-time correction. |
| Design scope expands into full forecast detail or asset management | Keep only handoff links/placeholders in M11 and track full pages as future work outside this change. |
| Current APIs lack some national aggregate fields | Display degraded but honest values from available data, or add narrowly scoped read-only aggregation endpoints with OpenAPI/test coverage. |
| Visual implementation drifts from effect-image intent | Treat the UI design spec as normative, add screenshot/viewport checks, and block issue completion when map-first layout, panel proportions, or state colors diverge. |

## Issue #160 Fixture Slice

Fixture level: expanded. Issue #160 touches shared frontend entrypoints, route migration,
URL query parsing, app-shell navigation, CSS/Tailwind design tokens, and visual evidence.
The mandatory expanded triggers are public route entrypoints, query-state compatibility,
layout token changes, existing workflow compatibility, and browser screenshot validation.

Repair intensity: medium. The work is frontend-only and does not touch backend
authorization, file IO, evidence ingestion, data loss, or production publish/delete paths.

Change surface:

- `apps/frontend/src/App.tsx`, `apps/frontend/src/components/layout/AppShell.tsx`,
  `apps/frontend/src/components/layout/NavBar.tsx`, and initial M11 placeholder shell pages.
- Shared query parsing/serialization helpers for `source`, `cycle`, `validTime`, `layer`,
  `basemap`, `basinVersionId`, `segmentId`, `warningLevel`, and `q`.
- `apps/frontend/src/index.css` or equivalent Tailwind/CSS token mapping.
- Frontend unit/component tests, Playwright route smoke tests, and issue-scoped
  `agent-browser` layout screenshots.

Must preserve:

- `/forecast`, `/flood-alerts`, and `/monitoring` remain reachable and keep their existing
  page-level behavior.
- RBAC wrapping for `/monitoring` remains intact.
- Existing forecast, flood alert, monitoring, API, and formatting tests continue to pass.
- CSS token changes do not break existing UI consumers of `background`, `panel`,
  `foreground`, `muted`, `border`, `accent`, `danger`, or warning/status colors.
- Invalid URL queries fall back deterministically and do not create repeated navigation
  update loops.

Must add/change for #160:

- `/` and `/overview` render the national overview shell.
- `/basins/:basinId` renders the basin drill-down shell with query restoration hooks.
- Navigation exposes 全国总览, 水文预报, 洪水预警, and 产品监控.
- The app shell maps the 56px navigation height, 280px left panel width, 320-360px right
  panel width, 64px timeline height, compact radii, spacing, typography, and warning/status
  tokens from `06B_frontend_ui_design_spec.md`.
- Query helpers round-trip valid values, omit unsupported/empty values, and normalize invalid
  values to documented defaults.

Risk packs considered for #160:

- Public API / CLI / script entry: selected - frontend route URLs and navigation are public
  operator entrypoints.
- Config / project setup: not selected - no runtime deployment or project configuration
  contract changes.
- File IO / path safety / overwrite: not selected - no file reads/writes outside normal
  screenshot artifact generation.
- Schema / columns / units / field names: not selected - no backend or data schema changes.
- Geospatial / CRS / shapefile sidecars: not selected - #160 only provides shell routes and
  state helpers, not map data rendering.
- Time series / forcing / temporal boundaries: selected - `cycle` and `validTime` query
  parsing must be stable for later timeline data.
- Numerical stability / conservation / NaN: not selected - no solver or numeric model logic.
- Solver runtime / performance / threading: not selected - no SHUD runtime changes.
- Resource limits / large input / discovery: not selected - no discovery or large input paths.
- Legacy compatibility / examples: selected - existing `/forecast`, `/flood-alerts`, and
  `/monitoring` workflows and tests must remain reachable.
- Error handling / rollback / partial outputs: selected - invalid query values and unknown
  basin shell links must render stable fallback states without route loops.
- Release / packaging / dependency compatibility: selected - frontend build/test output must
  remain compatible without adding unvetted dependencies.
- Documentation / migration notes: selected - token mapping and route migration decisions must
  be discoverable in code or developer notes.

Issue #160 non-goals:

- Real overview data adapters, basin segment data, MapLibre layer rendering, source/scenario
  data refresh, legends, full basin segment detail, and production MVT remain in issues #161-#165.
- Backend/OpenAPI changes are not part of #160 unless implementation proves route/query tests
  cannot be completed without a contract fix.

## Issue #161 Fixture Slice

Fixture level: expanded. Issue #161 introduces shared frontend data contracts and adapters
that compose multiple backend APIs and normalize IDs, timestamps, units, warning states,
freshness, partial failures, and unavailable reasons for later overview and basin pages.
Mandatory expanded triggers are public frontend data contracts, schema/field normalization,
time-series valid-time metadata, existing API reuse, and legacy workflow compatibility.

Repair intensity: high. The work touches shared adapter/helper behavior that later route,
map, timeline, and detail pages will consume. It must not add backend endpoints unless the
documented aggregation decision rule is met and fully evidenced.

Change surface:

- New frontend view-model types and adapter/store modules for M11 overview and basin data.
- API composition over existing `/api/v1/basins`, basin version, model, river segment,
  flood alert, forecast series, layer valid-time, pipeline, jobs/metrics, tile, and lineage
  contracts where already available.
- Unit tests for normal, partial, unavailable, invalid-query, freshness, and source/scenario
  provenance cases.
- No OpenAPI/backend changes unless adapter composition exceeds the endpoint decision rule.

Must preserve:

- Existing forecast, flood alert, monitoring, model asset, API type, and M11 route tests keep
  passing.
- Existing raw API response types remain generated from `openapi/nhms.v1.yaml`; adapters sit
  between these raw contracts and pages/components.
- `basin_id`, `basin_version_id`, `river_segment_id`, `model_id`, `run_id`, `source`, `cycle`,
  and `validTime` remain explicit in view models and handoff data.
- Unavailable data is represented as explicit `unavailableReason`, `qualityNote`, or partial
  state rather than fabricated default business values.

Must add/change for #161:

- Typed view models for `OverviewBasin`, `OverviewSummary`, `LayerState`, `BasinDetail`,
  `BasinSegmentRow`, `SelectedSegmentDetail`, and source/scenario selection state.
- Adapter functions or a store that compose existing APIs and normalize IDs, units, warning
  levels, timestamps, nullable fields, freshness metadata, source/scenario provenance, and
  unavailable reasons.
- Request de-duplication or caching for shared overview resources so repeated route/source/
  layer/filter changes do not blindly issue duplicate requests.
- A measurable aggregation endpoint decision helper/result: add no endpoint when existing
  composition is sufficient; if the rule is met, include OpenAPI/backend/frontend coverage in
  the same issue.

Risk packs considered for #161:

- Public API / CLI / script entry: selected - view models are public frontend contracts for
  overview/basin page entrypoints and handoff links.
- Config / project setup: not selected - no deployment or project configuration change.
- File IO / path safety / overwrite: not selected - no filesystem reads/writes.
- Schema / columns / units / field names: selected - adapter normalization depends on API
  schema field names, units, IDs, and optional/null values.
- Geospatial / CRS / shapefile sidecars: selected - basin bbox, river segment geometry, and
  map-feature availability must be passed through honestly without CRS fabrication.
- Time series / forcing / temporal boundaries: selected - source/cycle/validTime/layer
  valid-time metadata and forecast/timeline points must stay explicit and canonical.
- Numerical stability / conservation / NaN: not selected - no solver/numerical computation.
- Solver runtime / performance / threading: not selected - no SHUD runtime change.
- Resource limits / large input / discovery: selected - adapter composition must avoid
  unbounded per-basin N+1 calls and document the aggregation endpoint threshold.
- Legacy compatibility / examples: selected - existing forecast/flood/monitoring/model asset
  consumers and tests must continue to pass.
- Error handling / rollback / partial outputs: selected - partial endpoint failures and
  unavailable data must produce stable view-model states.
- Release / packaging / dependency compatibility: selected - no new dependency unless
  justified; frontend build/test path remains stable.
- Documentation / migration notes: selected - the endpoint decision rule and non-goals must
  be visible in OpenSpec/tasks or developer notes.

Issue #161 non-goals:

- Full map rendering, basin popup interaction, segment list UI, timeline controls, selected
  segment panel UI, visual evidence refresh, and production MVT remain in issues #162-#165.
- Backend aggregation endpoints are non-goals unless the decision rule is actually met by
  measured request count, per-basin N+1 behavior, or a missing required field.

## Issue #162 Fixture Slice

Fixture level: expanded. Issue #162 introduces shared MapLibre-facing primitives and
page-level controls for basemaps, layer groups, source/scenario selection, legends, valid-time
timeline, and playback. Mandatory expanded triggers are public map/control components consumed
by multiple M11 routes, time-series valid-time boundaries, geospatial source/layer registration,
legacy route compatibility, and resource-limit handling for playback/timer behavior.

Repair intensity: high. The work touches shared map/timeline/control behavior that later
overview, basin detail, segment detail, visual evidence, and Playwright workflows will consume.
It must keep unavailable layers honest and must not fabricate map data or introduce unbounded
timers/request refreshes.

Change surface:

- Shared components/helpers under `apps/frontend/src/components/map`,
  `apps/frontend/src/pages/m11`, `apps/frontend/src/lib/m11`, or a similarly scoped M11 module.
- Overview and basin detail shell wiring for source, layer, basemap, legend, and timeline
  controls.
- Query-state updates for `source`, `layer`, `basemap`, and `validTime`.
- Unit/component tests for basemap switching, layer grouping, source/scenario controls, legends,
  valid-time fallback, playback boundaries, and unavailable states.
- Playwright route coverage may be updated when controls become visible in the M11 shell.

Must preserve:

- Existing `/forecast`, `/flood-alerts`, `/monitoring`, `/`, `/overview`, and
  `/basins/:basinId` tests and behavior.
- Existing M11 data adapters remain the source of truth for `LayerState`,
  source/scenario provenance, and valid-time metadata.
- Best Available resolves to concrete GFS/IFS context or remains unavailable; frontend control
  changes must not emit unsupported `best_available` or `forecast_best_available` backend
  request values.
- Compare mode must remain explicit: comparison availability is surfaced, and missing compare
  data is unavailable rather than partially unlabeled.
- Timeline controls must use valid-time arrays from API or explicit payload-derived arrays; they
  must not synthesize a fixed hourly sequence.
- Missing layer data, meteorology data, station data, DEM data, map source failures, and empty
  valid-time lists produce scoped disabled/empty states.
- Playback timers are cleaned up and bounded by available valid times; route/control changes do
  not leave stale timers or stale valid-time selections.

Must add/change for #162:

- Reusable basemap state/control with terrain, satellite, and vector choices.
- Grouped layer controls for hydrology, meteorology, and base layers with disabled/unavailable
  placeholders for unimplemented meteorology/station/DEM surfaces.
- Source/scenario controls for GFS, IFS, GFS + IFS 对比, and Best Available with provenance and
  query-state updates.
- Active-layer legend for discharge, flood return period, and warning level using `06B` and
  flood-alert warning colors.
- Shared valid-time timeline with current time display, draggable/selectable valid-time control,
  previous/play/pause/next, speed selection, native-resolution ticks where available,
  data-source label, Analysis/Forecast divider, empty state, stale-time correction, and
  documented playback end behavior.

Risk packs considered for #162:

- Public API / CLI / script entry: selected - shared map/source/layer/timeline controls are
  public operator entrypoints and mutate shareable URL query state.
- Config / project setup: not selected - no deployment or project configuration changes are
  expected.
- File IO / path safety / overwrite: not selected - no filesystem operations beyond normal
  test artifacts.
- Schema / columns / units / field names: selected - controls consume layer ids, valid-time
  arrays, warning/return-period bins, units, and source/scenario labels.
- Geospatial / CRS / shapefile sidecars: selected - MapLibre source/layer registration,
  basemap switching, overlay restoration, and missing geospatial data states must be safe.
- Time series / forcing / temporal boundaries: selected - timeline valid times, stale-time
  correction, playback boundaries, analysis/forecast divider, and source/cycle propagation are
  core behavior.
- Numerical stability / conservation / NaN: not selected - no solver or numeric hydrology
  computation is changed.
- Solver runtime / performance / threading: not selected - no SHUD runtime changes.
- Resource limits / large input / discovery: selected - playback timers, valid-time lists, map
  layer toggles, and repeated source/layer switches must be bounded and cleaned up.
- Legacy compatibility / examples: selected - existing forecast/flood/monitoring/M11 route
  workflows must continue to pass.
- Error handling / rollback / partial outputs: selected - missing layers, empty valid times,
  unsupported source/layer combinations, and map source failures must remain scoped and honest.
- Release / packaging / dependency compatibility: selected - avoid new dependencies unless
  justified by existing project patterns and covered by build/tests.
- Documentation / migration notes: selected - playback end behavior, unavailable placeholders,
  and shared component contracts must be visible in code/OpenSpec/tests.

Issue #162 non-goals:

- Production vector-tile/MVT generation, backend tile endpoint work, full overview basin popup
  UX, basin segment list UX, selected-segment detail panel, full-screen forecast detail, and
  refreshed `agent-browser` visual evidence remain in issues #163-#165 unless a minimal hook is
  needed for shared controls.
- Implementing real meteorology/temperature/precipitation/station/DEM data contracts is out of
  scope; controls must show these as unavailable rather than rendering fake layers.

## Issue #163 Fixture Slice

Fixture level: expanded. Issue #163 turns the M11 shell, adapters, map primitives, source/layer
controls, and timeline into the default national overview operator page. Mandatory expanded
triggers are public route entrypoints, geospatial map rendering, time-series valid-time state,
schema/view-model consumption, legacy route compatibility, partial-failure behavior, and visual
evidence across supported viewports.

Repair intensity: high. The page combines shared M11 adapters, query state, MapLibre layer
registration, UI controls, and existing forecast/flood/monitoring navigation surfaces. Review
and fixes must treat stale source/time/layer state, unavailable geospatial data, partial summary
failures, and existing route compatibility as cross-surface invariants rather than isolated
widget bugs.

Change surface:

- `OverviewPage` and overview-scoped components under the M11 frontend module.
- Basin tree visibility state, basin popup actions, summary panels, and overview empty/degraded
  states.
- Overview map wiring that reuses shared MapLibre, basemap, layer, legend, source/scenario, and
  timeline controls from issues #160-#162.
- Links from overview summaries and basin popups to `/basins/:basinId`, `/monitoring`, and
  `/flood-alerts` with context query parameters.
- Component tests, Playwright interaction tests, and issue-scoped `agent-browser` screenshot
  evidence for 1920, 1440, and 1280 viewport classes.

Must preserve:

- `/`, `/overview`, `/forecast`, `/flood-alerts`, `/monitoring`, and `/basins/:basinId` remain
  reachable with their issue #160 query-state behavior.
- M11 adapters remain the source of truth for basin identity, active basin version, model counts,
  warning summaries, source/scenario provenance, layer valid times, and unavailable reasons.
- Shared map controls from #162 continue to correct stale valid times, bound playback timers,
  preserve active overlays across basemap changes, and mark unsupported meteorology/station/DEM
  layers unavailable.
- Existing forecast, flood alert, monitoring, route, adapter, map-control, and build tests keep
  passing.
- No fake basin boundaries, river geometry, warning values, forecast times, model counts, or
  meteorology layers are introduced when the underlying adapter/source marks data unavailable.

Must add/change for #163:

- National overview page matching effect-image-1 map-first layout with 56px nav, approximately
  280px left panel, 320-360px right panel, central China map extent, and 64px bottom timeline at
  the documented desktop viewport classes.
- Basin tree grouped by available hierarchy with all-select, none-select, per-basin visibility,
  empty inventory state, and map visibility synchronization.
- Overview map rendering for available basin boundaries/labels, river network, and active
  hydrologic risk layer, with scoped loading/error/unavailable overlays.
- Basin click popup with basin name, area, model river segment count, active model/version count,
  latest forecast time, "查看详情" handoff, and "进入分析" drill-down.
- Right panel basemap selector, active-layer legend, forecast run summary, warning summary,
  source/scenario provenance, and context-preserving links to monitoring/flood-alert routes.
- Degraded states for no basin inventory, no published basin version, invalid source/cycle/
  valid-time data, partial summary failures, unavailable meteorology layers, and map source
  failure.

Risk packs considered for #163:

- Public API / CLI / script entry: selected - `/` and `/overview` are public operator
  entrypoints; popup and summary actions navigate to public app routes with query context.
- Config / project setup: not selected - no deployment, environment, or project configuration
  contract changes are expected.
- File IO / path safety / overwrite: not selected - no application filesystem operations beyond
  test/screenshot artifacts.
- Schema / columns / units / field names: selected - page UI consumes normalized basin/version,
  model, river count, warning summary, layer, valid-time, source, and provenance fields.
- Geospatial / CRS / shapefile sidecars: selected - overview map uses basin bbox/boundary,
  national extent, labels, river network, and hydrologic layers without fabricating geometry.
- Time series / forcing / temporal boundaries: selected - source/cycle/validTime, layer valid
  times, latest forecast time, stale-time correction, and summary context links are page-visible.
- Numerical stability / conservation / NaN: not selected - no solver or hydrologic numerical
  computation is changed.
- Solver runtime / performance / threading: not selected - no SHUD runtime or parallel solver
  behavior is changed.
- Resource limits / large input / discovery: selected - map source registration, visibility
  toggles, summary refreshes, and screenshot/e2e flows must avoid unbounded requests, timers, or
  render loops.
- Legacy compatibility / examples: selected - existing forecast, flood alert, monitoring, and
  M11 shell routes/tests must remain stable after overview becomes the real default page.
- Error handling / rollback / partial outputs: selected - partial API failures, invalid query
  values, missing basin versions, unavailable layers, and map source failures must stay scoped
  and recoverable.
- Release / packaging / dependency compatibility: selected - frontend tests/build/e2e must pass
  without adding unvetted dependencies or browser-only assumptions that break CI.
- Documentation / migration notes: selected - overview limitations, visual evidence location,
  and unavailable data behavior must be discoverable in OpenSpec/tests or developer notes.

Issue #163 non-goals:

- Basin drill-down segment discovery/list workflow, forecast-to-basin handoff, selected segment
  detail, trend sparkline, and basin map segment coloring remain issues #164-#165.
- Production MVT/PBF, backend aggregation endpoints, authentication changes, model asset
  management pages, meteorology pages, and full-screen forecast detail are out of scope unless a
  narrowly scoped compatibility fix is required by local verification.
- Basin visibility persistence may remain page/store-local unless an explicit tested
  `visibleBasins` URL parameter is implemented.

## Verification Strategy

- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
- Component/store tests for adapters, query restoration, timeline valid-time behavior, and warning filters.
- Playwright tests for overview default route, basin popup to drill-down, basin search/filter, segment click detail, and existing `/forecast`, `/flood-alerts`, `/monitoring` availability.
- `agent-browser` screenshots for overview and basin detail at 1920x1080, 1440x900, and 1280x900 where supported, using the global-argument form: `agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>` followed by `agent-browser screenshot <path>`.
- Backend checks only if aggregation endpoints are added: `uv run ruff check .` and focused `uv run pytest -q` for new API tests.
