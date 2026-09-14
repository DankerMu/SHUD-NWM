## ADDED Requirements

### Requirement: Basin drill-down page is retired

The system SHALL NOT provide a basin drill-down page or basin-detail mode (#2109 decision B, 2026-09-13). A basin click on the national overview SHALL only fit the camera to that basin, and the legacy `/basins/:basinId` path SHALL redirect with `replace` to `/`, dropping the path parameter and preserving every other query key, so the national overview renders with no `basinId` in the URL.

#### Scenario: Legacy basin link lands on the national overview
- **WHEN** an operator opens `/basins/basins_qhh?source=ifs`
- **THEN** the final URL MUST have pathname `/`, keep `source=ifs`, and contain no `basinId` key (the national valid-time correction may add `validTime`)
- **AND** the national overview map MUST render with no back-to-overview control and no basin-scoped segment list or panel

#### Scenario: Basin click does not drill in
- **WHEN** an operator clicks a visible basin on the national overview
- **THEN** the camera MUST fit that basin and the route and query MUST stay unchanged

## REMOVED Requirements

### Requirement: Basin drill-down route renders basin-scoped analysis
**Reason**: The basin drill-down page is retired (#2109 decision B). It was unreachable by construction, and the national overview already delivers its user value in place; a basin click fits the camera and stays on `/`.
**Migration**: Use the national overview `/`. Legacy `/basins/:basinId` links redirect to `/` without `basinId` (see `single-map-shell-routing` and `inplace-overview-basin-detail`).

### Requirement: Basin detail left panel supports segment discovery
**Reason**: The basin-scoped segment list and basin-scoped segment layer are retired with the basin drill-down page (#2109 decision B).
**Migration**: Discover segments on the national discharge layer: zoom to the basin (basin click fits the camera) and click a segment.

### Requirement: Basin map supports segment hover and click
**Reason**: The basin-scoped map mode is retired (#2109 decision B); the national map already supports segment hover (latest-product prefetch) and click.
**Migration**: Click a national discharge segment on `/` to open the in-place river forecast panel.

### Requirement: Selected segment detail provides forecast context
**Reason**: The basin-detail selected-segment panel is retired (#2109 decision B).
**Migration**: The national river forecast panel (`M11RiverForecastPanel`) shows the segment's GFS and IFS forecast series with its own source/cycle choice.

### Requirement: Basin detail includes lineage and quality context
**Reason**: The only frontend lineage consumer (`fetchLineage`, calling `/api/v1/lineage/river-point`) lived inside the retired basin-detail load, and that route was never registered in the backend (#2039); the requirement had no working implementation.
**Migration**: None in the frontend. Any future lineage display is specified afresh together with a registered backend route (#2039 tracks the backend/API-doc side).

### Requirement: Basin detail handles unavailable source/time data
**Reason**: Retired with the basin drill-down page (#2109 decision B).
**Migration**: Unavailable source/time states on `/` are governed by `map-layer-timeline-controls` and `overview-data-contracts`.
