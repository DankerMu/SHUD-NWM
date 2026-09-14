# basin-drilldown-page Specification

## Purpose
TBD - created by archiving change m11-overview-basin-drilldown. Update Purpose after archive.
## Requirements
### Requirement: Basin drill-down page is retired

The system SHALL NOT provide a basin drill-down page or basin-detail mode (#2109 decision B, 2026-09-13). A basin click on the national overview SHALL only fit the camera to that basin, and the legacy `/basins/:basinId` path SHALL redirect with `replace` to `/`, dropping the path parameter and preserving every other query key, so the national overview renders with no `basinId` in the URL.

#### Scenario: Legacy basin link lands on the national overview
- **WHEN** an operator opens `/basins/basins_qhh?source=ifs`
- **THEN** the final URL MUST have pathname `/`, keep `source=ifs`, and contain no `basinId` key (the national valid-time correction may add `validTime`)
- **AND** the national overview map MUST render with no back-to-overview control and no basin-scoped segment list or panel

#### Scenario: Basin click does not drill in
- **WHEN** an operator clicks a visible basin on the national overview
- **THEN** the camera MUST fit that basin and the route and query MUST stay unchanged

