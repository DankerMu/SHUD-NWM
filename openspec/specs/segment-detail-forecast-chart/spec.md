# segment-detail-forecast-chart Specification

## Purpose
TBD - created by archiving change m12-segment-forecast-detail. Update Purpose after archive.
## Requirements
### Requirement: Multi-source forecast chart
The segment detail page SHALL render analysis, GFS forecast, and IFS forecast series with design-specified styling and controls.

#### Scenario: Threshold overlay
WHEN Q2/Q5/Q10/Q20/Q50/Q100 values are available
THEN the chart displays labeled threshold lines and highlights the peak return-period band

#### Scenario: Scenario toggle
WHEN a user toggles Analysis, GFS, or IFS
THEN the corresponding series visibility changes without mutating URL identity state

#### Scenario: Insufficient IFS horizon
WHEN IFS 06/18 cycle has shorter horizon
THEN the chart truncates the IFS series and labels the available horizon

