## MODIFIED Requirements

### Requirement: Overview map displays national basin context

The system SHALL display the national extent with basin boundaries, basin labels, and hydrologic risk layers where data is available.

#### Scenario: National map initializes at China extent
- **WHEN** the overview map loads
- **THEN** it MUST initialize to a national China extent matching approximately 73E-135E and 18N-53N
- **AND** it MUST render basin boundaries with translucent fill when boundary data is available
- **AND** it MUST render basin labels or an accessible substitute when label geometry is available

#### Scenario: National hydrologic risk layer renders
- **WHEN** river network or discharge data is available for the active layer/source/time
- **THEN** the overview map MUST render the national river or risk layer using the active hydrologic color scale
- **AND** changing the related layer toggle MUST update or hide the corresponding map layer
- **AND** unavailable river/risk data MUST show a scoped unavailable state rather than fake geometry or values

#### Scenario: Basin click fits the camera and stays on the overview
- **WHEN** an operator clicks a visible basin on the overview map
- **THEN** the map camera MUST fit that basin's bounding box
- **AND** the page MUST stay on the national overview: no basin drill-down mode, no route change, and no `basinId` written to the URL
- **AND** every other basin MUST remain rendered and clickable


### Requirement: Overview handles degraded data states

The system SHALL render useful degraded states when backend data is missing or partial.

#### Scenario: No basin inventory
- **WHEN** the basin inventory request returns an empty list
- **THEN** the overview MUST show an empty basin state in the left panel
- **AND** the map MUST remain usable with any available non-basin layers

#### Scenario: Basin has no published version
- **WHEN** a basin exists but has no published or active basin version
- **THEN** the basin row and popup MUST show a version-unavailable state

#### Scenario: Partial summary failure
- **WHEN** one summary request fails but basin inventory or map data succeeds
- **THEN** the page MUST render the successful sections
- **AND** the failed summary card MUST show a scoped error or unavailable state without replacing the whole page

#### Scenario: Overview map source fails
- **WHEN** a map source or layer request fails
- **THEN** the affected layer MUST show an inline map or panel error with retry affordance when possible
- **AND** other successful layers and controls MUST remain usable
