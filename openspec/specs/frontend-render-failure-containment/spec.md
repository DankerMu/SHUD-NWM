# frontend-render-failure-containment Specification

## Purpose
TBD - created by archiving change add-frontend-region-error-boundaries. Update Purpose after archive.
## Requirements
### Requirement: Render failures are contained to a route or region
The frontend SHALL contain a render-time exception to the smallest enclosing boundary instead of unmounting the application. A route-level boundary MUST wrap the routed pages, keep the site header mounted, show a page fallback with retry and reload actions, catch lazy page chunk load failures, and reset when the location changes. The national overview page MUST give its map surface, floating map controls, forecast panels, bottom control bar and legend separate region boundaries, with the control bar model derived inside the control bar region. The monitoring/ops page MUST give its summary, stage list, jobs table and trend panel separate region boundaries. A region fallback MUST offer a retry action that re-renders the region, MUST reset when the region's reset keys change, and MUST NOT display the raw error message.

#### Scenario: Control bar derivation throws
- **WHEN** the national overview renders with a discharge cycles payload whose `cycles` array contains `null`
- **THEN** the control bar region shows its fallback
- **AND** the map section, map surface, floating controls and legend remain rendered, and the site header is present

#### Scenario: A routed page throws
- **WHEN** a routed page component throws during render
- **THEN** the site header remains rendered and the page area shows the page fallback with retry and reload actions
- **AND** navigating to another route renders that route normally

#### Scenario: Retry re-renders a recovered region
- **WHEN** a region has caught an error and the underlying cause no longer throws
- **THEN** activating retry renders the region's content again

#### Scenario: One monitoring panel throws
- **WHEN** one of the summary, stage list, jobs table or trend panel throws during render
- **THEN** that panel shows its fallback and the other three render normally

