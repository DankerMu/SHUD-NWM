## Why

M10 closed the production-like backend loop, but the current React frontend still starts from a forecast/monitoring workflow instead of the design-specified national situational overview. Operators need a default map-first view that answers "where is risk now?" and a direct drill-down path from national basin context to basin-level river segment analysis.

This change turns the existing production data surfaces into the first operator experience: a national overview page and a basin detail view that reuse current API contracts, flood alert data, model asset metadata, and MapLibre components while preserving the already delivered forecast, flood alert, and monitoring pages.

## What Changes

- Add a national overview as the default entry experience with global navigation, left basin/layer panel, central national map, right operational summary panel, and bottom valid-time timeline.
- Add basin drill-down from the overview and hydrologic forecast navigation into a basin-scoped map/list/detail workflow.
- Add shared overview data adapters and view models that compose existing basins, basin versions, river segments, forecast series, flood alert, pipeline, and model asset APIs.
- Add reusable map layer, legend, basemap, and timeline controls driven by API-provided `valid_times[]` rather than fixed generated hour sequences.
- Update frontend routing and URL state so selected basin, basin version, segment, source, cycle, valid time, active layer, warning filter, and basemap can be restored or shared.
- Add visual conformance requirements so M11 pages match the effect-image intent and the UI design spec for map-first layout, panel sizing, navigation, colors, spacing, typography, states, and responsive breakpoints.
- Add focused unit/component tests and Playwright coverage for overview loading, map/list interaction, drill-down navigation, query restoration, and loading/error/empty states; capture visual evidence with `agent-browser`.

## Capabilities

### New Capabilities

- `national-overview-page`: Default national map experience with basin tree, layer toggles, basin popups, right-side summaries, and operational cross-links.
- `basin-drilldown-page`: Basin-scoped river segment map/list/detail workflow with search, warning filters, hover/click interactions, trend sparkline, and handoff actions.
- `overview-data-contracts`: Frontend view-model adapters and, only if required, thin aggregation endpoints for overview and basin summary data.
- `map-layer-timeline-controls`: Shared MapLibre layer controls, legends, basemap switching, and valid-time timeline behavior.
- `frontend-navigation-state`: Route migration, navigation labels, URL query state, compatibility with existing pages, and testable state restoration.
- `frontend-visual-conformance`: Effect-image/UI-spec alignment for layout, design tokens, responsive behavior, states, and `agent-browser` screenshot evidence.

### Modified Capabilities

- None. Existing OpenSpec specs are not present under `openspec/specs/`; this change introduces new capability specs for M11 frontend behavior.

## Impact

- Frontend routes: `apps/frontend/src/App.tsx`, layout navigation, and new pages/components/stores under `apps/frontend/src`.
- Frontend data layer: generated OpenAPI types, API client helpers, overview/basin stores, and normalization utilities.
- Existing pages: `ForecastPage`, `FloodAlertPage`, and `MonitoringPage` remain available; current forecast functionality moves behind a stable route or route alias if `/` becomes the national overview.
- Existing APIs reused first: `/api/v1/basins`, `/api/v1/basins/{basin_id}/versions`, `/api/v1/models`, `/api/v1/models/{model_id}`, `/api/v1/basin-versions/{basin_version_id}/river-segments`, `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}`, `/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}/forecast-series`, `/api/v1/flood-alerts/summary`, `/api/v1/flood-alerts/ranking`, `/api/v1/flood-alerts/segments`, `/api/v1/flood-alerts/timeline`, `/api/v1/tiles/flood-return-period`, `/api/v1/layers`, `/api/v1/layers/{layer_id}/valid-times`, `/api/v1/pipeline/status`, `/api/v1/pipeline/stages`, `/api/v1/jobs`, `/api/v1/queue/depth`, `/api/v1/metrics/stage-duration`, `/api/v1/metrics/success-rate`, and `/api/v1/lineage/river-point`.
- Optional API additions are limited to lightweight read-only aggregation endpoints if frontend-only composition proves too slow or too brittle; any addition must update `openapi/nhms.v1.yaml`, generated TypeScript types, backend tests, and frontend tests in the same implementation issue.
- Design references: `docs/spec/06_frontend_gis_design.md`, `docs/spec/06B_frontend_ui_design_spec.md`, `docs/modules/15_frontend_application_design.md`, `docs/spec/04_api_design.md`, and `docs/appendices/A_id_and_versioning_convention.md`.

## Non-Goals

- Full-screen river forecast detail page from `docs/spec/06_frontend_gis_design.md` section 7.6 is not implemented in this change; M11 exposes a stable "查看详情" handoff route/link or disabled placeholder with copy-safe state.
- Full model asset management page from section 14 is not implemented; M11 links basin "查看详情" to the existing or placeholder system-management/model-assets destination.
- Full meteorology grid/station pages from sections 8/8B are not implemented; meteorology layer toggles may be disabled or marked unavailable until their data contracts exist.
- Production-grade MVT/PBF implementation is not added; M11 may use current GeoJSON flood return period data and existing river segment sources per `docs/spec/04_api_design.md`.
- Authentication/authorization redesign, live ingestion changes, and backend production operations changes are out of scope.
