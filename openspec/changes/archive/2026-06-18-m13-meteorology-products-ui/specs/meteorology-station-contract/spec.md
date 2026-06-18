## ADDED Requirements

### Requirement: Meteorology station contract
The system SHALL expose station inventory, basin association, stable station_id, lon/lat/elevation, completeness/QC status, latest data time, forcing version/source provenance, forcing series for PRCP/TEMP/RH/wind/Rn/Press where available, missing/anomalous intervals, and adjacent station relationships.

#### Scenario: Station list
WHEN station inventory is available
THEN UI can filter by basin, search by station_id/name, and sort by latest data time

#### Scenario: Partial QC
WHEN station has missing or anomalous variable intervals
THEN detail response marks missing periods and QC status per variable

#### Scenario: Forcing unavailable
WHEN a station exists but forcing series is unavailable for the selected time range or variable
THEN the response identifies the unavailable variables/reason and the UI omits charts for those variables without rendering synthetic samples

#### Scenario: Adjacent stations
WHEN station detail includes adjacent station relationships
THEN the UI can highlight adjacent stations and report their distance or relationship reason from the contract

#### Scenario: Bounded station inventory and series
WHEN station inventory, search results, or forcing series exceed advertised page-size, time-range, or sample-count limits
THEN the system returns paginated, truncated, or validation metadata and the UI renders a bounded empty/truncated/error state instead of unbounded rows or chart samples
