## 1. Route and Handoff
- [x] 1.1 Add the segment detail route and update basin, forecast, and flood-alert handoff links with `source`, `cycle`, `validTime`, `basinVersionId`, `riverNetworkVersionId`, and `segmentId`.
  - Evidence: basin fixture starts at `/basins/basin-demo?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009`, clicks `查看河段详情`, and expects `/segments/seg-009` with the same six query values.
  - Evidence: flood fixture selects ranking row `seg-1` from `run-flood-1` and expects `/segments/seg-1?source=gfs&cycle=2026-05-12T00:00:00Z&validTime=2026-05-12T03:00:00Z&basinVersionId=basin-v1&riverNetworkVersionId=rivnet-v1`.
- [x] 1.2 Add route/query tests for reload, invalid stale segment, missing river network version, and preserved existing `/forecast` behavior.
  - Evidence: reload of `/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1` requests only `/api/v1/basin-versions/bv-001/river-segments/seg-009/forecast-series?river_network_version_id=rn-v1...` and restores the same segment heading.
  - Evidence: `/segments/missing?basinVersionId=bv-001&riverNetworkVersionId=rn-v1` returns a 404/empty segment fixture and renders `未找到河段 missing`; the test asserts no request is made for `seg-001` or any other sibling.
  - Evidence: `/segments/seg-009?basinVersionId=bv-001` renders `缺少 riverNetworkVersionId` and does not call forecast-series.
  - Evidence: existing `/forecast?source=gfs&basinVersionId=bv-001&riverNetworkVersionId=rn-v1&segmentId=seg-009` test still opens the forecast map workflow.

## 2. Data Contract
- [x] 2.1 Build the normalized segment detail view model from existing APIs and document the endpoint decision if a new aggregation endpoint is required.
  - Evidence: unit fixture with `river_segment_id=seg-009`, `basin_version_id=bv-001`, `river_network_version_id=rn-v1`, `issue_time=2026-05-18T00:00:00Z`, `unit=m3/s`, GFS/IFS/analysis series, `frequency_thresholds={Q2:100,Q5:200,Q10:300,Q20:400,Q50:500,Q100:600}`, and null station fields normalizes to camelCase identity, KPI peak/current values, thresholds, lineage, and `stationStatus=unavailable`.
- [x] 2.2 Add tests for identity normalization, forecast/threshold/lineage availability, geometry budgets, and unavailable station/forcing fields.
  - Evidence: API payload with missing thresholds renders all threshold rows as unavailable; payload with `NaN`, `Infinity`, or non-numeric Q values omits those points and renders `暂无有效流量`.
  - Evidence: geometry payload with no coordinates renders `位置缩略图不可用`; geometry payload above the configured coordinate budget renders `河段几何超出缩略图预算`.
  - Evidence: station/forcing absent fixture renders `站点与强迫数据暂不可用` and contains no synthetic station rows or PRCP/TEMP chart series.

## 3. Detail UI
- [x] 3.1 Implement the KPI strip and 120x90 location thumbnail.
  - Evidence: component test with current Q 3225, peak 3460, Q20 400, and no water-level delta renders KPI values from data and renders water-level delta as `暂无水位变化` instead of a fabricated number.
  - Evidence: thumbnail test asserts the SVG/canvas container is exactly 120x90, missing geometry shows unavailable copy, and over-budget geometry does not render a partial misleading segment.
- [x] 3.2 Implement the multi-source forecast chart with analysis/forecast divider, scenario toggles, IFS short-horizon labeling, tooltip, and Q2/Q5/Q10/Q20/Q50/Q100 threshold overlays.
  - Evidence: chart fixture with analysis points before issue time, GFS points after issue time, IFS `available_lead_hours=144`, and Q2/Q5/Q10/Q20/Q50/Q100 thresholds asserts mark lines named `Q2`...`Q100`, `起报时间`, and `IFS 6d`.
  - Evidence: user toggles Analysis/GFS/IFS controls and expected output is series visibility changing while `window.location.search` remains unchanged.
- [x] 3.3 Implement station/forcing panel with PRCP/TEMP charts and no-fake-data unavailable state.
  - Evidence: station fixture with `station_id=S001`, PRCP/TEMP series renders station metadata and two charts; empty fixture renders unavailable copy and no station row.
- [x] 3.4 Implement frequency curve and weather driver panels with partial-data states.
  - Evidence: weather fixture containing PRCP and TEMP only renders those charts and explicit missing labels for RH/wind/Press; no placeholder numeric values appear.
- [x] 3.5 Implement bottom timeline sync and stale valid-time correction.
  - Evidence: URL with stale `validTime=2026-05-17T00:00:00Z` and fixture valid times `2026-05-18T00:00:00Z,2026-05-18T06:00:00Z` replaces only `validTime` with the closest valid scoped time and leaves source/cycle/basinVersionId/riverNetworkVersionId/segmentId unchanged.
  - Evidence: loading fixture delays forecast response and expects `河段详情加载中`; restricted fixture with station source `restricted_reason=CLDAS unavailable in this environment` renders restricted copy and no fake station/forcing values.

## 4. Validation
- [x] 4.1 Run OpenSpec strict validation, frontend unit tests, `tsc --noEmit`, build, and focused Playwright handoff/reload tests.
- [x] 4.2 Capture desktop screenshot evidence for the route at 1920x1080 and 1440x900.
  - Evidence: local screenshots captured at `.codex/screenshots/issue-173/segment-detail-1920x1080.png` and `.codex/screenshots/issue-173/segment-detail-1440x900.png`.
- [x] 4.3 Update `progress.md` with completed scope and remaining segment-detail limitations.

## Evidence Floor

- `openspec validate m12-segment-forecast-detail --strict --no-interactive`
- `cd apps/frontend && corepack pnpm test`
- `cd apps/frontend && corepack pnpm exec tsc --noEmit`
- `cd apps/frontend && corepack pnpm build`
- Focused Playwright route/handoff coverage for basin -> segment detail, flood ranking -> segment detail, reload query restoration, invalid stale segment, and preserved `/forecast`.

## Non-Goals

- Adding meteorology browse pages or model asset management pages.
- Replacing the existing `/forecast` map workflow.
- Introducing backend aggregation endpoints unless the endpoint decision note proves existing calls cannot provide required fields within bounded request count.
- Fabricating station, forcing, weather, threshold, or geometry values when unavailable.
