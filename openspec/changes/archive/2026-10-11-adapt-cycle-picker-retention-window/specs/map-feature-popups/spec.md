## ADDED Requirements

### Requirement: Station popup issue-time picker handles retained-disk misses

The M11 station forcing popup SHALL treat station-series
`STATION_FORCING_FILE_NOT_FOUND` responses as retained-disk unavailable cycles,
not as generic chart failures and not as permission to fall back to DB history.

#### Scenario: retained-out cycle shows retention-specific empty state

- **WHEN** a user selects an issue time whose latest-product identity exists but
  whose station-series disk file has rotated out of the retained object-store
  window
- **AND** `/api/v1/met/stations/{station_id}/series` returns
  `STATION_FORCING_FILE_NOT_FOUND`
- **THEN** the station popup SHALL render a clear unavailable state for the
  selected retained-disk cycle
- **AND** it SHALL NOT draw station forcing charts for that response.

#### Scenario: a retained-out issue time stays selectable

- **WHEN** an issue time has failed with `STATION_FORCING_FILE_NOT_FOUND` for
  the current station/source popup session and is still among the available
  issue times
- **THEN** the issue-time picker SHALL keep that option selectable without a
  session-level unavailable mark, and selecting it again SHALL show the same
  retention-specific state
- **AND** selecting a different still-available issue time SHALL refetch
  latest-product and station-series normally.

#### Scenario: backend direct-disk contract is unchanged

- **WHEN** the frontend handles a retained-disk miss
- **THEN** it SHALL keep calling the existing direct-disk station-series route
- **AND** it SHALL NOT introduce DB fallback, archive/history reads, or synthetic
  station-series points.
