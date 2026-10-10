## ADDED Requirements

### Requirement: The basemap-unavailable status SHALL NOT be cleared by overview data settling

Once a basemap tile failure has raised the map-source-error status with the basemap-unavailable notice, the status SHALL NOT be cleared because the layer catalogue arrives or the valid time settles or changes. The notice SHALL be cleared when the user switches to a different basemap. A business-layer source error SHALL still take precedence over the basemap notice and SHALL still be cleared when the layer inputs change, after which a basemap whose failure was recorded SHALL show its notice again.

#### Scenario: Overview data settles after the basemap tiles have failed

- **WHEN** basemap tile requests have failed and the status shows the basemap-unavailable notice
- **AND** the overview bootstrap data arrives afterwards and the valid time is corrected
- **THEN** the status shows the basemap-unavailable notice without having been removed in between

#### Scenario: The user switches basemap

- **WHEN** the basemap-unavailable notice is shown and the user selects a different basemap
- **THEN** the notice is cleared until a tile of the newly selected basemap fails
