# segment-detail-station-forcing Specification

## Purpose
TBD - created by archiving change m12-segment-forecast-detail. Update Purpose after archive.
## Requirements
### Requirement: Station forcing side panel
The segment detail page SHALL show station and forcing context when station/forcing contracts are available and explicit unavailable states otherwise.

#### Scenario: Station data available
WHEN station_id, name, location, source, and forcing series are available
THEN the left panel lists stations and renders PRCP and TEMP forcing charts for the selected station

#### Scenario: Station contract absent
WHEN station APIs or forcing series are absent
THEN the panel displays station/forcing unavailable copy and no synthetic station rows

#### Scenario: Restricted station or forcing source
WHEN station or forcing metadata includes a restricted reason
THEN the panel displays the restricted reason and does not render fabricated station rows or forcing series

