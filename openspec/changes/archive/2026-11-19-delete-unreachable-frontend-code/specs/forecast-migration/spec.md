## REMOVED Requirements

### Requirement: Forecast panel
**Reason**: The side panel (`components/forecast/ForecastPanel.tsx`) has not been routed since the single-map page replaced the forecast page; it was unreachable from the app entry point and is deleted (#3222). The river popup that replaced it is specified by `map-feature-popups` and `mobile-curve-sheet`.
**Migration**: None. No user-visible surface is removed.

### Requirement: Forecast state management
**Reason**: The Zustand forecast store (`stores/forecast.ts`) had one non-test user, a component that was itself unreachable; it is deleted (#3222). The river popup loads its series through `lib/hydroMet/riverForecast.ts`.
**Migration**: None. The type `ForecastData` consumed by `ForecastChart` moves to `src/lib/forecastData.ts`.

## MODIFIED Requirements

### Requirement: River segment layer
The system SHALL render GeoJSON river segments on the map with click-to-select interaction.

#### Scenario: Segment click triggers forecast load
- **WHEN** the user clicks on a river segment on the map
- **THEN** the river curve window specified by `map-feature-popups` MUST open and load the forecast data for the selected segment
