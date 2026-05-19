## Why

M11 basin drill-down can hand off a selected river segment into `/forecast`, but design §7.6 / effect image 3 requires a dedicated full-screen segment detail page. Operators need KPI, station forcing, multi-source forecast, return-period thresholds, frequency context, weather drivers, timeline, and provenance in one restorable route.

## What Changes

- Add `/segments/:segmentId` (or an equivalent route selected during implementation) as a full-screen segment forecast detail page reachable from basin detail, forecast, and flood-alert workflows.
- Define canonical URL state mapping for `source`, `cycle`, `validTime`, `basinVersionId`, `riverNetworkVersionId`, and `segmentId`, translating to API snake_case parameters without losing identity scope.
- Compose existing forecast-series, return-period/frequency, basin/model identity, segment geometry, lineage, and station/forcing metadata; add thin read-only endpoints only after an endpoint decision note proves existing composition is insufficient.
- Render KPI strip, 120x90 segment location thumbnail, station/forcing side panel, multi-source forecast chart with analysis/forecast divider and Q2/Q5/Q10/Q20/Q50/Q100 threshold lines, frequency/weather panels, and bottom timeline.
- Add loading, empty, partial, restricted, and stale-state tests plus Playwright route/handoff coverage.

## Capabilities

### New Capabilities

- `segment-detail-route-state`
- `segment-detail-data-contract`
- `segment-detail-forecast-chart`
- `segment-detail-station-forcing`
- `segment-detail-frequency-weather-panels`

## Impact

- Frontend routes, M11 basin/flood/forecast handoff links, forecast chart components, and possibly API/OpenAPI when new read-only aggregation endpoints are required.
- Existing `/forecast` remains available and must preserve current tests.

## Non-Goals

- Meteorology grid/station browse pages; those are M13.
- Replacing current `/forecast` map workflow.
- Fabricating station or forcing values when no contract/data exists.
