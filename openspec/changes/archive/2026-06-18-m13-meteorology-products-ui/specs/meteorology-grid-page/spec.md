## ADDED Requirements

### Requirement: Meteorology grid page
The grid page SHALL render raster/grid products with variable/source controls, opacity, contours, station overlay, timeline, cell query, area stats, and comparison widgets.

#### Scenario: Route and state restore
WHEN the user opens `/meteorology?tab=grid` with variable, source, and validTime query state
THEN the grid tab restores supported state and corrects unsupported or stale state to a visible contract-backed value or unavailable state

#### Scenario: Cell query
WHEN user clicks a rendered grid cell
THEN popup shows lon/lat, source, cycle, validTime, value, unit, time resolution, and spatial resolution

#### Scenario: No tile
WHEN tile request fails or product is missing
THEN map shows scoped unavailable overlay without stale tile data

#### Scenario: Unsupported comparison
WHEN the user selects comparison sources that lack the same variable and valid time
THEN the comparison panel reports unsupported comparison without computing a fake difference

#### Scenario: Source switch cleanup
WHEN the selected variable or source changes
THEN stale tiles, popups, area statistics, and playback state from the previous product are cleared or visibly superseded before new data is shown
