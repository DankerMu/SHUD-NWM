# segment-detail-route-state Specification

## Purpose
TBD - created by archiving change m12-segment-forecast-detail. Update Purpose after archive.
## Requirements
### Requirement: Full-screen segment detail route
The frontend SHALL expose a full-screen segment detail route for a selected river segment and preserve cross-page source/basin identity.

#### Scenario: Basin handoff
WHEN a user clicks 查看详情 from `/basins/:basinId` with `source`, `cycle`, `validTime`, `basinVersionId`, `riverNetworkVersionId`, and `segmentId` in state
THEN the destination URL contains equivalent route/query state and reload restores the same segment without falling back to another river network

#### Scenario: Missing river network version
WHEN the segment detail URL includes `segmentId` and `basinVersionId` but omits `riverNetworkVersionId`
THEN the page shows a stable missing-identity state and does not request forecast series data

#### Scenario: Invalid stale segment
WHEN the URL references a segment that is not present in the scoped basin/river network
THEN the page shows an invalid segment state and does not request forecast data for a sibling segment

