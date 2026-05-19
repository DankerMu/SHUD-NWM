## ADDED Requirements

### Requirement: Frequency and weather context panels
The right side SHALL show return-period frequency context and weather driver charts when data exists.

#### Scenario: Frequency curve available
WHEN frequency curve parameters and forecast peak are available
THEN the page renders a return-period curve with current forecast peak marker

#### Scenario: Weather variables partial
WHEN only some PRCP/TEMP/RH/wind/Press variables are available
THEN available variables render while missing variables show explicit partial-data status

#### Scenario: Loading and partial states
WHEN segment detail data is still loading or a downstream weather/frequency request fails
THEN the page renders stable loading or partial-data states without clearing the scoped segment identity from the URL
