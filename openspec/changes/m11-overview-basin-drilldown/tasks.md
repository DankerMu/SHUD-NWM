## 1. Navigation, visual foundation, and route state

- [ ] 1.1 Update `App.tsx` routing so `/` and `/overview` render the national overview page, `/basins/:basinId` renders basin drill-down, and existing forecast functionality remains reachable through `/forecast`.
- [ ] 1.2 Update `NavBar.tsx` and app-shell labels/icons to expose 全国总览, 水文预报, 洪水预警, and 产品监控 while keeping unimplemented 气象数据/系统管理 entries hidden, disabled, or explicit placeholders.
- [ ] 1.3 Map the UI design tokens from `06B_frontend_ui_design_spec.md` into existing CSS/Tailwind tokens for navigation height, panel widths, timeline height, colors, typography, spacing, radii, shadows, warning levels, and status states.
- [ ] 1.4 Add shared URL query parsing/serialization helpers for `source`, `cycle`, `validTime`, `layer`, `basemap`, `basinVersionId`, `segmentId`, `warningLevel`, and `q`; keep basin visibility store-only unless an explicit tested `visibleBasins` parameter is added.
- [ ] 1.5 Add route/query tests for `/`, `/overview`, `/forecast`, `/flood-alerts`, `/monitoring`, representative `/basins/:basinId` links, invalid query fallback, and preserved forecast route behavior.
- [ ] 1.6 Add initial layout smoke coverage for the app shell at 1920x1080, 1440x900, and 1280x900 using `agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>` followed by `agent-browser screenshot <path>`; keep Playwright for route/interaction tests.

### Issue #160 Evidence Matrix

Selected risk packs:

- Public API / CLI / script entry -> 1.1, 1.2, 1.5.
- Time series / forcing / temporal boundaries -> 1.4, query tests for `cycle` and `validTime`.
- Legacy compatibility / examples -> 1.1, 1.5, preserved route tests for existing pages.
- Error handling / rollback / partial outputs -> 1.4, invalid-query fallback tests.
- Release / packaging / dependency compatibility -> frontend test/build commands.
- Documentation / migration notes -> 1.3 code notes or developer notes for token mapping.

Required test inputs and expected outputs:

- Route input `/` -> renders the national overview shell and marks 全国总览 active.
- Route input `/overview?source=gfs&layer=flood-return-period&basemap=terrain` -> renders the
  same overview shell with normalized query state `{source: "gfs", layer: "flood-return-period",
  basemap: "terrain"}`.
- Route input `/forecast` -> renders existing hydrologic forecast workflow and marks 水文预报
  active.
- Route input `/flood-alerts?warningLevel=major` -> renders existing flood alert workflow; the
  route remains reachable after the overview migration.
- Route input `/monitoring` with the test role override allowed -> renders the existing product
  monitoring workflow through the same RBAC wrapper.
- Route input `/basins/basin-demo?basinVersionId=bv-001&segmentId=seg-009&source=best&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&warningLevel=orange&q=main` -> renders the basin
  drill-down shell and exposes normalized basin/query state for later data loading.
- Query helper input `{source: "ifs", cycle: "2026-05-18T00:00:00Z", validTime:
  "2026-05-18T06:00:00Z", layer: "warning-level", basemap: "satellite", basinVersionId:
  "bv-001", segmentId: "seg-009", warningLevel: "red", q: "干流"}` -> serializes and parses back
  to the same supported values.
- Query helper input `source=unknown&basemap=bad&warningLevel=invalid` -> returns documented
  defaults without throwing and without emitting repeated URL updates.
- Navigation render -> shows exactly the implemented workflow entries 全国总览, 水文预报, 洪水预警,
  产品监控; unimplemented 气象数据/系统管理 is hidden, disabled, or explicit placeholder.
- Token/layout assertion -> app shell header is 56px, M11 shell exposes 280px left panel,
  320-360px right panel, 64px timeline, compact radii, and warning/status tokens matching the
  documented mapping.
- Agent-browser smoke inputs `1920x1080`, `1440x900`, and `1280x900` for `/overview` and one
  `/basins/:basinId` shell -> screenshots show navigation, shell panels, map placeholder area,
  and timeline region without overlap. If the browser tool is unavailable in CI, store the
  attempted command and local screenshot/evidence path in the PR notes.

Verification commands for #160:

- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm test:e2e`
- `cd apps/frontend && corepack pnpm run test:e2e:preview`
- `cd apps/frontend && corepack pnpm build`
- `openspec validate m11-overview-basin-drilldown --strict --no-interactive`

Non-goals for #160:

- No real basin inventory adapter, river segment adapter, MapLibre river rendering, layer
  valid-time API integration, selected segment forecast data, or backend/OpenAPI aggregation
  endpoint unless required to keep existing routes compiling.

## 2. Overview and basin data contracts

- [ ] 2.1 Create overview/basin view-model types for `OverviewBasin`, `OverviewSummary`, `LayerState`, `BasinDetail`, `BasinSegmentRow`, `SelectedSegmentDetail`, and source/scenario selection state.
- [ ] 2.2 Implement adapters/stores that compose existing APIs for basins, basin versions, models, river segments, flood alerts, forecast series, `/api/v1/layers`, `/api/v1/layers/{layer_id}/valid-times`, pipeline status/stages, jobs, queue depth, metrics, flood return-period map data, and lineage.
  - #161 closure scope: page-facing M11 adapters compose basins, bounded basin versions,
    models, river segments, flood summary/ranking/timeline, forecast series, layers,
    layer valid-times, pipeline status, queue depth, and lineage status. Pipeline stages,
    jobs, metrics, flood return-period map features/tiles, flood-alert segment-list UI,
    and full lineage graph rendering are typed downstream surfaces or existing
    monitoring/flood-alert route surfaces, not first-class overview/basin page widgets in
    this issue; they must remain unavailable/placeholder-backed rather than fabricated.
- [ ] 2.3 Normalize IDs, `basin_version_id`, `river_segment_id`, warning levels, quality flags, units, timestamps, null fields, unavailable reasons, source/scenario provenance, valid-time metadata, and freshness metadata in adapters rather than leaf components.
- [ ] 2.4 Add caching or request de-duplication for shared overview resources so route changes, source changes, layer changes, and filter changes do not trigger unnecessary repeated calls.
- [ ] 2.5 Add unit tests for adapter normalization, partial failures, unavailable fields, freshness metadata, source/scenario provenance, layer valid-times, and query-driven request parameters.
- [ ] 2.6 Add a measurable aggregation-endpoint decision rule: only add read-only endpoint(s) when existing composition requires more than 8 initial overview requests, creates per-basin N+1 calls for required fields, or cannot provide a required field from current APIs; if added, update `openapi/nhms.v1.yaml`, regenerate types, run `check:api-types`, and add backend/frontend tests in the same issue.
  - #161 closure invariant: overview request counts are computed from the actual plan,
    including pipeline eligibility and each distinct layer valid-time request. Real
    basin-version/bbox data is fetched only when bounded; otherwise the view model reports
    aggregation-needed and leaves required composite fields unavailable.

### Issue #161 Evidence Matrix

Selected risk packs:

- Public API / CLI / script entry -> typed view models consumed by overview/basin shell pages
  or exported for later page issues.
- Schema / columns / units / field names -> adapter tests for snake_case API fields, camelCase
  view models, units, nullable values, and warning levels.
- Geospatial / CRS / shapefile sidecars -> bbox/geometry availability and missing-geometry
  states are normalized without fabricating coordinates.
- Time series / forcing / temporal boundaries -> source/cycle/validTime/layer valid-time and
  forecast point tests.
- Resource limits / large input / discovery -> request de-duplication/caching and aggregation
  endpoint decision rule tests.
- Legacy compatibility / examples -> existing forecast, flood alert, monitoring, and M11 route
  tests keep passing.
- Error handling / rollback / partial outputs -> partial endpoint failure and unavailable
  field tests.
- Release / packaging / dependency compatibility -> frontend test/build commands and no
  unvetted dependency.
- Documentation / migration notes -> endpoint decision rule and non-goals documented here.

Required test inputs and expected outputs:

- Basin list payload with nested hierarchy, bbox, area, null latest forecast, and multiple
  basin versions -> `OverviewBasin[]` preserves `basinId`, `basinVersionId`, display name,
  bbox/fallback extent, hierarchy, warning counts, and unavailable latest-forecast reason.
- Model and basin-version payloads with active/inactive models -> overview/basin adapters expose
  active model/version counts without hiding `basin_version_id`.
- Flood summary/ranking payloads with mixed warning levels, null Q, missing unit, and quality
  note -> `OverviewSummary` and `BasinSegmentRow` normalize warning level, `m3/s` fallback,
  quality state, and freshness metadata.
- `/api/v1/layers` plus `/api/v1/layers/{layer_id}/valid-times` payloads with empty and non-empty
  valid-time arrays -> `LayerState` marks available/unavailable, current valid time, source, and
  disabled reason.
- Forecast series payload with GFS, IFS, analysis, empty points, and explicit `cycle_time` ->
  `SelectedSegmentDetail` exposes source/scenario provenance, trend points, current value, and
  comparison availability without fabricating missing series.
- Lineage success and failure/unavailable cases -> selected segment detail exposes lineage status
  or unavailable reason instead of throwing in leaf UI.
- Partial failure where one optional endpoint rejects -> adapter returns partial view model with
  scoped error/unavailable state; required identity fields remain stable.
- Repeated identical overview load requests -> shared resource calls are de-duplicated or cached
  according to the chosen implementation.
- Aggregation endpoint decision inputs: `initialRequestCount <= 8`, `initialRequestCount > 8`,
  per-basin N+1 true, and missing required field true -> helper returns reuse-existing or
  aggregation-needed with reason.

Verification commands for #161:

- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
- `cd apps/frontend && corepack pnpm test:e2e`
- `cd apps/frontend && corepack pnpm run test:e2e:preview`
- `openspec validate m11-overview-basin-drilldown --strict --no-interactive`
- If OpenAPI/backend changes are added: `cd apps/frontend && corepack pnpm check:api-types`,
  `uv run ruff check .`, and focused `uv run pytest -q` for affected API tests.

Non-goals for #161:

- No full map layer controls, national overview UI beyond consuming initial view models, basin
  segment list UI, selected segment panel UI, or refreshed visual screenshot evidence.

## 3. Shared map, source, layer, legend, and timeline controls

- [ ] 3.1 Extract or extend shared MapLibre primitives for source/layer registration, hover/click callbacks, overlays, fit/fly-to behavior, and restoration of active layers after basemap switches.
- [ ] 3.2 Implement required terrain, satellite, and vector basemap switching for overview and basin maps.
- [ ] 3.3 Implement grouped layer controls for hydrology, meteorology, and base layers; mark unimplemented precipitation, temperature, station, or DEM layers unavailable rather than rendering fake data.
- [ ] 3.4 Implement source/scenario controls for GFS, IFS, GFS + IFS 对比, and Best Available; make source changes refresh map layers, summaries, segment detail, valid times, comparison availability, URL query, and provenance labels.
- [ ] 3.5 Implement discharge, return-period, and warning-level legends using the same warning/status color semantics as the flood alert page and `06B` warning-level palette.
- [ ] 3.6 Implement a shared timeline using `/api/v1/layers/{layer_id}/valid-times` as the primary source, payload-derived times only for non-layer detail payloads, current time display, draggable slider, previous/play/pause/next, speed, native-resolution ticks, current data-source label, and Analysis/Forecast divider.
- [ ] 3.7 Ensure layer/source switching replaces the active valid-time list, corrects invalid stale valid times, disables playback when no valid times exist, and updates map/data requests with the selected valid time.
- [ ] 3.8 Add unit/component tests for basemap switching, layer availability, source/scenario behavior, legend selection, valid-times API use, timeline drag/boundaries/playback, and stale valid-time correction.

### Issue #162 Evidence Matrix

Selected risk packs:

- Public API / CLI / script entry -> reusable map/control components consumed by `/overview`
  and `/basins/:basinId`, URL query updates for `source`, `layer`, `basemap`, and
  `validTime`.
- Schema / columns / units / field names -> layer ids, valid-time arrays, source/scenario
  provenance labels, warning/return-period legend bins, and disabled reasons.
- Geospatial / CRS / shapefile sidecars -> MapLibre source/layer registration and basemap
  restoration without fabricating unavailable basin/river/meteorology data.
- Time series / forcing / temporal boundaries -> valid-time selection, stale valid-time
  correction, native-resolution ticks, playback boundaries, and Analysis/Forecast divider.
- Resource limits / large input / discovery -> no unbounded animation timers, playback loops,
  or repeated valid-time refreshes when controls change.
- Legacy compatibility / examples -> existing forecast, flood alert, monitoring, and M11
  route tests keep passing.
- Error handling / rollback / partial outputs -> unavailable layers, missing valid times,
  unsupported source/layer combinations, and basemap/source switch failures show scoped disabled
  states rather than fake rendered data.
- Release / packaging / dependency compatibility -> no new mapping/timeline dependency unless
  justified; frontend build/test path remains stable.
- Documentation / migration notes -> component contracts and documented playback end behavior
  are discoverable in code/OpenSpec or tests.

Required test inputs and expected outputs:

- Basemap input `terrain`, `satellite`, and `vector` -> the shared map surface exposes the
  selected basemap state, updates the URL query, and preserves active overlay/layer state after
  switching.
- Layer controls with hydrology, meteorology, and base groups -> implemented hydrology/base
  layers render toggles; precipitation, temperature, station, and DEM placeholders are disabled
  or marked unavailable and do not claim rendered data.
- Source input `gfs`, `ifs`, `compare`, and `best` -> controls update shareable query state,
  propagate to page data reload hooks, show comparison availability/provenance, and never emit
  unsupported backend `best_available` or `forecast_best_available` values.
- Active layer valid times `["2026-05-18T00:00:00Z", "2026-05-18T06:00:00Z"]` with stale
  `validTime=2026-05-17T00:00:00Z` -> timeline selects the documented fallback valid time and
  updates map/data request state without rendering stale time.
- Empty valid-time list -> timeline shows an empty/disabled state and disables previous, next,
  drag, and playback actions.
- Payload-derived detail times from forecast/timeline payloads -> timeline labels the source as
  derived and uses only those exact times when no layer valid-time contract applies.
- Timeline playback at first and last ticks -> previous/next respect boundaries and play either
  pauses/stops/loops according to the documented behavior without selecting invalid times.
- Active layer `discharge`, `flood-return-period`, and `warning-level` -> legend displays units,
  bins, and colors consistent with `06B` and flood alert warning semantics.

Verification commands for #162:

- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
- `cd apps/frontend && corepack pnpm test:e2e`
- `cd apps/frontend && corepack pnpm run test:e2e:preview`
- `openspec validate m11-overview-basin-drilldown --strict --no-interactive`
- If OpenAPI/backend changes are added: `cd apps/frontend && corepack pnpm check:api-types`,
  `uv run ruff check .`, and focused `uv run pytest -q` for affected API tests.

Non-goals for #162:

- No production MVT pipeline, new backend aggregation endpoints, full national overview basin
  popup workflow, basin segment list UX, selected segment detail panel, or visual screenshot
  evidence refresh unless needed to keep controls testable.
- No fabricated meteorology/DEM/station data; unavailable controls must remain disabled or
  explicitly unavailable.

## 4. National overview page

- [ ] 4.1 Build `OverviewPage` with effect-image-1 map-first layout: 56px top nav, left basin/layer panel, central national map, right summary panel, and 64px bottom timeline; support the documented 1920/1440/1280 viewport behaviors.
- [ ] 4.2 Implement basin tree grouped by available hierarchy with all-select/none-select actions and map visibility state.
- [ ] 4.3 Render national map at China extent with available basin boundaries, basin labels, river network, hydrologic risk layers, and scoped loading/error overlays.
- [ ] 4.4 Implement basin click popup matching the UI spec popup-card pattern with basin name, area, model river segment count, active model/version count, latest forecast time, "查看详情" handoff behavior, and "进入分析" drill-down action.
- [ ] 4.5 Implement right panel basemap selector, active-layer legend, forecast run summary, warning summary, source/scenario provenance, and links to monitoring/flood-alert routes with context query parameters.
- [ ] 4.6 Implement overview empty/degraded states for no basin inventory, no published basin version, invalid source/cycle/valid-time data, partial summary failures, unavailable meteorology layers, and map source failure.
- [ ] 4.7 Add component and Playwright interaction tests for overview default render, basin visibility toggle, layer/source changes, popup drill-down, summary links, and partial data rendering; capture overview visual screenshots at supported viewports with `agent-browser`.

### Issue #163 Evidence Matrix

Selected risk packs:

- Public API / CLI / script entry -> 4.1, 4.4, 4.5, route and popup/link Playwright tests.
- Schema / columns / units / field names -> 4.2, 4.4, 4.5, component tests using normalized
  basin/version/model/warning/layer/source fields.
- Geospatial / CRS / shapefile sidecars -> 4.3, map component tests for national extent,
  basin boundary/label/river layer availability, and unavailable geometry states.
- Time series / forcing / temporal boundaries -> 4.1, 4.5, 4.6, tests for layer/source changes,
  valid-time correction, latest forecast labels, and context query links.
- Resource limits / large input / discovery -> 4.2, 4.3, 4.7, tests or assertions showing
  visibility/source/layer changes do not create unbounded timers, request loops, or map layers.
- Legacy compatibility / examples -> 4.1, 4.5, 4.7, preserved tests for `/forecast`,
  `/flood-alerts`, `/monitoring`, `/basins/:basinId`, map controls, and adapters.
- Error handling / rollback / partial outputs -> 4.3, 4.6, partial failure/empty/unavailable
  component and Playwright coverage.
- Release / packaging / dependency compatibility -> frontend test/e2e/build commands and no new
  unvetted runtime dependency.
- Documentation / migration notes -> PR/developer notes or test fixture names documenting
  unsupported meteorology/MVT/model-asset handoff limits and screenshot evidence paths.

Required test inputs and expected outputs:

- Route input `/` and `/overview` -> renders the full national overview page, marks 全国总览
  active, preserves 56px nav, exposes left basin/layer panel, central map, right summaries, and
  64px timeline without overlap.
- Viewport inputs `1920x1080`, `1440x900`, and `1280x900` -> overview remains map-first; side
  panels use documented widths/collapse behavior; no panel, popup, timeline, or map control text
  overlaps.
- Basin inventory with hierarchy, bbox/boundary, area, active version/model count, segment count,
  latest forecast time, and warning counts -> basin tree groups rows, all-select/none-select
  toggles visibility, map layers reflect visible basins, and popup shows the same normalized
  fields without fabricated values.
- Basin inventory empty list -> left panel shows empty basin state; map remains usable for any
  available non-basin layers; page does not redirect or crash.
- Basin with no published/active version -> row and popup show version-unavailable state; "进入分析"
  is disabled or navigates to a basin detail empty state according to the implemented route
  contract.
- Map source success for available basin boundary/label/river/risk data -> overview initializes
  to China extent and renders available layers; toggling basin/layer visibility hides/shows only
  the intended layer.
- Map source failure or unavailable geometry -> only the affected layer shows scoped error or
  unavailable state; other controls, summaries, and map interactions remain usable.
- Source inputs `gfs`, `ifs`, `compare`, and `best` plus layer/valid-time changes -> overview
  updates provenance labels, active legend/timeline state, stale valid-time fallback, and URL
  query without emitting unsupported backend source values.
- Popup action input "进入分析" on a basin with routeable id -> navigates to
  `/basins/<encoded-basin-id>` with relevant source/cycle/validTime/layer/basemap context.
- Popup action input "查看详情" -> links to the existing/placeholder model asset destination or
  disabled handoff state; it does not claim a full model asset page exists.
- Right summary click for forecast run status -> navigates to `/monitoring` with available
  source/cycle context; warning summary click -> navigates to `/flood-alerts` with available
  source/cycle/warning context.
- Partial summary failure where flood alert or pipeline data rejects but basin inventory succeeds
  -> successful sections render and the failed card shows scoped unavailable/error state.
- Unavailable meteorology/DEM/station layers -> controls remain disabled or explicitly
  unavailable and do not render fake map data.

Verification commands for #163:

- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm build`
- `cd apps/frontend && corepack pnpm test:e2e`
- `cd apps/frontend && corepack pnpm run test:e2e:preview`
- `openspec validate m11-overview-basin-drilldown --strict --no-interactive`
- `git diff --check`

Visual evidence for #163:

- Start the local frontend server using the existing project command selected by the implementer
  or Playwright preview server.
- Capture overview screenshots for `/overview` at `1920x1080`, `1440x900`, and `1280x900` with
  `agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>`
  followed by `agent-browser screenshot <path>`.
- Store evidence under an issue-scoped path such as `.codex/screenshots/issue-163/` and record
  any unavailable browser-tool limitation in PR evidence rather than omitting the attempt.

Non-goals for #163:

- No basin detail segment list/search/filter, selected segment detail panel, sparkline, basin
  river coloring, or forecast-to-basin handoff beyond the popup/summary route links.
- No production MVT/PBF implementation, new backend aggregation endpoint, full model asset
  management page, meteorology page, or full-screen forecast detail page.
- Basin visibility persistence may stay local to the overview page unless a tested
  `visibleBasins` URL parameter is intentionally added.

## 5. Basin drill-down shell, segment discovery, and forecast handoff

- [ ] 5.1 Build `BasinDetailPage` route that loads basin identity, active or selected basin version, bbox/fallback extent, warning distribution, latest run metadata, source/scenario state, and basin-scoped segment data.
- [ ] 5.2 Implement fly-to/fit-bounds behavior from national overview and direct links, including invalid basin id and missing-bbox states.
- [ ] 5.3 Update 水文预报 navigation/selection so choosing a basin opens the same `/basins/:basinId` workflow rather than a separate incompatible basin detail flow.
- [ ] 5.4 Implement left segment list with basin name, `basin_version_id`, segment rows, search, warning-level filter, selected row state, result count, and empty/no-data states.
- [ ] 5.5 Implement row selection that syncs `segmentId` into URL state, highlights the same map segment when geometry exists, and loads the detail panel.
- [ ] 5.6 Add component and Playwright tests for basin deep link, forecast-to-basin handoff, invalid basin id, missing bbox, search/filter, row selection, query restoration, and no-segment empty state.

## 6. Basin map, segment detail, visual evidence, and delivery validation

- [ ] 6.1 Render basin-scoped river network colored by discharge, return period, or warning level with basin boundary highlight and available city/station labels.
- [ ] 6.2 Implement river segment hover tooltip/highlight with segment name/ID, current flow, return period, and warning level when available.
- [ ] 6.3 Implement segment click selection that syncs `segmentId` into URL state, selects or scrolls the matching row, loads detail/forecast/timeline/lineage data, and keeps unavailable source/time states scoped.
- [ ] 6.4 Implement selected segment detail panel with IDs, basin/model metadata, current Q, optional water-level delta, return-period level, forecast valid time, source, cycle, quality/lineage status, "查看详情" handoff, and "对比预报" availability/overlay behavior.
- [ ] 6.5 Implement right-side trend sparkline for selected segment with current value and direction when trend points exist.
- [ ] 6.6 Add `agent-browser` screenshots for overview and basin detail at 1920x1080, 1440x900, and 1280x900 using the global-argument command form `agent-browser --args "--no-sandbox,--disable-dev-shm-usage,--disable-gpu" open <url>`; verify navigation, side panels, map area, timeline, popup/detail panel, controls, and state components do not overlap and match the design spec intent.
- [ ] 6.7 Run `cd apps/frontend && corepack pnpm test`, `cd apps/frontend && corepack pnpm test:e2e`, `cd apps/frontend && corepack pnpm run test:e2e:preview`, and `cd apps/frontend && corepack pnpm build`; if OpenAPI changed, also run `cd apps/frontend && corepack pnpm check:api-types`, `uv run ruff check .`, and focused `uv run pytest -q` for affected API tests.
- [ ] 6.8 Add or update frontend developer notes for overview/basin routes, required data contracts, source/scenario behavior, map layer limitations, visual evidence location, and future handoff destinations.
- [ ] 6.9 Update progress documentation after implementation to record completed M11 scope and deferred follow-ups: full-screen segment detail, meteorology pages, model asset management page, and production MVT.
