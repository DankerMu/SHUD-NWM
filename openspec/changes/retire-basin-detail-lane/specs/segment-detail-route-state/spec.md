## MODIFIED Requirements

### Requirement: Full-screen segment detail route
The frontend SHALL expose a full-screen segment detail route for a selected river segment and preserve cross-page source/basin identity. The retired basin-detail page (#2109 decision B) is no longer a handoff origin.

#### Scenario: Missing river network version
WHEN the segment detail URL includes `segmentId` and `basinVersionId` but omits `riverNetworkVersionId`
THEN the page shows a stable missing-identity state and does not request forecast series data

#### Scenario: Invalid stale segment
WHEN the URL references a segment that is not present in the scoped basin/river network
THEN the page shows an invalid segment state and does not request forecast data for a sibling segment
