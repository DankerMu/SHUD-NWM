## ADDED Requirements

### Requirement: Meteorology navigation and tabs
The frontend SHALL expose meteorology navigation only when minimum contracts exist, with spatial grid and station query sub-tabs. The bundled frontend fixture contracts count as the minimum contract for M13 route exposure; missing live backend products are rendered as restricted/unavailable states within the route.

#### Scenario: Route available
WHEN metadata contracts are enabled
THEN `/meteorology?tab=grid` and `/meteorology?tab=stations` restore selected tab and source state

#### Scenario: Contracts absent
WHEN metadata contracts are absent
THEN navigation remains hidden/disabled or shows an explicit unavailable page

#### Scenario: Existing route compatibility
WHEN the meteorology route is added
THEN existing overview, basin, forecast, flood-alert, segment detail, and monitoring routes keep their current navigation behavior and access control

#### Scenario: Tab query correction
WHEN the user opens `/meteorology` with a missing or unsupported tab query value
THEN the route selects a supported default tab and updates or renders state without crashing
