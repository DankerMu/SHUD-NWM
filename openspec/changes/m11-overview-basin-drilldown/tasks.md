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
- `cd apps/frontend && corepack pnpm build`
- `openspec validate m11-overview-basin-drilldown --strict --no-interactive`

Non-goals for #160:

- No real basin inventory adapter, river segment adapter, MapLibre river rendering, layer
  valid-time API integration, selected segment forecast data, or backend/OpenAPI aggregation
  endpoint unless required to keep existing routes compiling.

## 2. Overview and basin data contracts

- [ ] 2.1 Create overview/basin view-model types for `OverviewBasin`, `OverviewSummary`, `LayerState`, `BasinDetail`, `BasinSegmentRow`, `SelectedSegmentDetail`, and source/scenario selection state.
- [ ] 2.2 Implement adapters/stores that compose existing APIs for basins, basin versions, models, river segments, flood alerts, forecast series, `/api/v1/layers`, `/api/v1/layers/{layer_id}/valid-times`, pipeline status/stages, jobs, queue depth, metrics, flood return-period map data, and lineage.
- [ ] 2.3 Normalize IDs, `basin_version_id`, `river_segment_id`, warning levels, quality flags, units, timestamps, null fields, unavailable reasons, source/scenario provenance, valid-time metadata, and freshness metadata in adapters rather than leaf components.
- [ ] 2.4 Add caching or request de-duplication for shared overview resources so route changes, source changes, layer changes, and filter changes do not trigger unnecessary repeated calls.
- [ ] 2.5 Add unit tests for adapter normalization, partial failures, unavailable fields, freshness metadata, source/scenario provenance, layer valid-times, and query-driven request parameters.
- [ ] 2.6 Add a measurable aggregation-endpoint decision rule: only add read-only endpoint(s) when existing composition requires more than 8 initial overview requests, creates per-basin N+1 calls for required fields, or cannot provide a required field from current APIs; if added, update `openapi/nhms.v1.yaml`, regenerate types, run `check:api-types`, and add backend/frontend tests in the same issue.

## 3. Shared map, source, layer, legend, and timeline controls

- [ ] 3.1 Extract or extend shared MapLibre primitives for source/layer registration, hover/click callbacks, overlays, fit/fly-to behavior, and restoration of active layers after basemap switches.
- [ ] 3.2 Implement required terrain, satellite, and vector basemap switching for overview and basin maps.
- [ ] 3.3 Implement grouped layer controls for hydrology, meteorology, and base layers; mark unimplemented precipitation, temperature, station, or DEM layers unavailable rather than rendering fake data.
- [ ] 3.4 Implement source/scenario controls for GFS, IFS, GFS + IFS 对比, and Best Available; make source changes refresh map layers, summaries, segment detail, valid times, comparison availability, URL query, and provenance labels.
- [ ] 3.5 Implement discharge, return-period, and warning-level legends using the same warning/status color semantics as the flood alert page and `06B` warning-level palette.
- [ ] 3.6 Implement a shared timeline using `/api/v1/layers/{layer_id}/valid-times` as the primary source, payload-derived times only for non-layer detail payloads, current time display, draggable slider, previous/play/pause/next, speed, native-resolution ticks, current data-source label, and Analysis/Forecast divider.
- [ ] 3.7 Ensure layer/source switching replaces the active valid-time list, corrects invalid stale valid times, disables playback when no valid times exist, and updates map/data requests with the selected valid time.
- [ ] 3.8 Add unit/component tests for basemap switching, layer availability, source/scenario behavior, legend selection, valid-times API use, timeline drag/boundaries/playback, and stale valid-time correction.

## 4. National overview page

- [ ] 4.1 Build `OverviewPage` with effect-image-1 map-first layout: 56px top nav, left basin/layer panel, central national map, right summary panel, and 64px bottom timeline; support the documented 1920/1440/1280 viewport behaviors.
- [ ] 4.2 Implement basin tree grouped by available hierarchy with all-select/none-select actions and map visibility state.
- [ ] 4.3 Render national map at China extent with available basin boundaries, basin labels, river network, hydrologic risk layers, and scoped loading/error overlays.
- [ ] 4.4 Implement basin click popup matching the UI spec popup-card pattern with basin name, area, model river segment count, active model/version count, latest forecast time, "查看详情" handoff behavior, and "进入分析" drill-down action.
- [ ] 4.5 Implement right panel basemap selector, active-layer legend, forecast run summary, warning summary, source/scenario provenance, and links to monitoring/flood-alert routes with context query parameters.
- [ ] 4.6 Implement overview empty/degraded states for no basin inventory, no published basin version, invalid source/cycle/valid-time data, partial summary failures, unavailable meteorology layers, and map source failure.
- [ ] 4.7 Add component and Playwright interaction tests for overview default render, basin visibility toggle, layer/source changes, popup drill-down, summary links, and partial data rendering; capture overview visual screenshots at supported viewports with `agent-browser`.

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
- [ ] 6.7 Run `cd apps/frontend && corepack pnpm test`, `cd apps/frontend && corepack pnpm test:e2e`, and `cd apps/frontend && corepack pnpm build`; if OpenAPI changed, also run `cd apps/frontend && corepack pnpm check:api-types`, `uv run ruff check .`, and focused `uv run pytest -q` for affected API tests.
- [ ] 6.8 Add or update frontend developer notes for overview/basin routes, required data contracts, source/scenario behavior, map layer limitations, visual evidence location, and future handoff destinations.
- [ ] 6.9 Update progress documentation after implementation to record completed M11 scope and deferred follow-ups: full-screen segment detail, meteorology pages, model asset management page, and production MVT.
