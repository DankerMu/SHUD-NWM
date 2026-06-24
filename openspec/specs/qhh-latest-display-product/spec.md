# qhh-latest-display-product Specification

## Purpose
TBD - created by archiving change m21-qhh-hydro-met-ops-mvp. Update Purpose after archive.
## Requirements
### Requirement: Latest QHH display product discovery

The system SHALL provide a stable contract for discovering the latest usable QHH display product for an MVP source.

#### Scenario: Latest product by source
- **WHEN** a client requests `GET /api/v1/mvp/qhh/latest-product?source=GFS` or `GET /api/v1/mvp/qhh/latest-product?source=IFS`
- **THEN** the response includes `basin_id`, `model_id`, `basin_version_id`, `river_network_version_id`, `source_id`, `cycle_time`, `run_id`, `forcing_version_id`, `station_count`, `expected_station_count` when known, `segment_count`, `expected_segment_count` when known, `status`, and available valid-time or horizon metadata.

#### Scenario: No usable product
- **WHEN** no QHH product is usable for the requested source
- **THEN** the response returns an explicit unavailable/not-found state with reasons
- **AND** the frontend can render an unavailable state without manual IDs or fallback dummy data.

#### Scenario: Product readiness filters
- **WHEN** multiple QHH runs exist for the same source
- **THEN** the latest-product logic selects the newest product that has a usable hydro run, forcing version, basin version, river network version, six-variable station forcing coverage, and displayable station/segment counts consistent with the product's expected coverage
- **AND** it does not select failed, cancelled, or incomplete products as ready.

#### Scenario: IFS horizon disclosure
- **WHEN** the selected IFS product has a shorter available horizon than seven days
- **THEN** the product metadata or associated series metadata exposes the available end time or horizon
- **AND** the frontend labels the actual horizon rather than padding or hiding the truncation.

### Requirement: Latest product supports downstream MVP requests

The latest product response SHALL contain enough identifiers for the hydro-met UI to request station inventory, station series, river segments, and river `q_down` forecasts without manual operator input.

#### Scenario: Display bootstrap
- **WHEN** the current `/` single-map display entrypoint or the `/hydro-met` legacy redirect alias loads for QHH and a selected source
- **THEN** it can use latest-product metadata to call station list, station series, river segment list, and forecast-series APIs
- **AND** no user-entered `run_id`, `forcing_version_id`, `basin_version_id`, or `river_network_version_id` is required.

#### Scenario: Contract validation
- **WHEN** backend tests seed a QHH-like product
- **THEN** latest-product tests assert the identity fields, counts, source/cycle normalization, and incomplete-product rejection rules.
