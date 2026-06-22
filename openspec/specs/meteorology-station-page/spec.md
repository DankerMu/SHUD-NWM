# meteorology-station-page Specification

## Purpose
TBD - created by archiving change m13-meteorology-products-ui. Update Purpose after archive.
## Requirements
### Requirement: Meteorology station page
The station query page SHALL render station map markers, popup, forcing charts, QC indicators, and adjacent-station interactions.

#### Scenario: Route and filter restore
WHEN the user opens `/meteorology?tab=stations` with basin, search, sort, or station query state
THEN the station tab restores supported state and displays explicit empty or unavailable states for unsupported filters

#### Scenario: Station select
WHEN user selects a station row or marker
THEN map pans to marker, popup opens, and right panel shows PRCP/TEMP/RH/wind/Rn charts from the current station-series route where available
AND Press is shown only as unavailable/omitted unless a future route explicitly provides it

#### Scenario: Deep-linked station selection
WHEN a supported `stationId` exists in the filtered station collection but is outside the default bounded page
THEN selected popup/detail remains on that station and the UI either includes the selected row or renders an explicit selected-out-of-page state without falling back to another station

#### Scenario: No stations
WHEN filter returns no stations
THEN page displays 搜索无结果 or no-station empty state without fake rows

#### Scenario: Partial QC markers
WHEN selected station series includes missing or anomalous intervals
THEN charts and variable summaries mark the affected intervals and show completeness/QC status from the contract

#### Scenario: Selection cleanup
WHEN the selected basin, search filter, station, or tab changes
THEN stale popups, adjacent highlights, and forcing/QC charts from the previous station are cleared or visibly superseded
