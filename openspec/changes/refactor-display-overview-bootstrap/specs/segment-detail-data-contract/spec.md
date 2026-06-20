<!--
  Modification rationale (2026-06-20): the product decision removes `water-level`
  as a supported hydro variable. `hydro.river_timeseries` never carried
  `water_level` rows; the dead layer caused a 22s cold SQL on `/api/v1/layers`.
  The MODIFIED `Normalized segment detail view model` requirement below drops
  the prior `water-level` mention from the non-finite-value scenario; no
  standalone `## REMOVED Requirements` block is used because the dropped clause
  was a phrase inside an existing scenario, not its own `### Requirement:`
  header in the live spec (and a REMOVED block here would fail
  `openspec validate --strict`).
-->

## MODIFIED Requirements

### Requirement: Normalized segment detail view model
The system SHALL normalize segment identity, basin/model metadata, forecast series, return-period thresholds, lineage, and availability flags before rendering leaf components.

#### Scenario: Existing API composition
WHEN existing APIs provide all required fields
THEN the view model includes currentQ, qUnit, peak forecast, returnPeriodBand, forecast valid time, source provenance, lineage, basinVersionId, riverNetworkVersionId, and geometry budget status

#### Scenario: Non-finite or unavailable numeric values
WHEN forecast, threshold, station, or weather values are missing, non-numeric, NaN, or infinite
THEN the view model omits those values from charts/KPIs and exposes explicit unavailable flags for the UI

#### Scenario: Endpoint decision required
WHEN existing APIs cannot provide a required design field within bounded request count
THEN the implementation records an endpoint decision note before adding an OpenAPI-backed read-only endpoint
