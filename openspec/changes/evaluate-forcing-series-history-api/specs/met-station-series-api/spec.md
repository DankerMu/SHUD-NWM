## ADDED Requirements

### Requirement: Historical station forcing access is explicit archive surface

Long-term station forcing history, if implemented, SHALL be exposed through a
separate archive/history API surface or an explicit opt-in mode. It SHALL NOT be
a silent fallback from the current retained-disk station-series route.

#### Scenario: retained disk route keeps disk miss semantics

- **WHEN** the current public station-series route resolves a disk path outside
  the retained object-store window
- **THEN** it SHALL return `STATION_FORCING_FILE_NOT_FOUND`
- **AND** it SHALL NOT read DB/archive rows to mask the missing disk artifact

#### Scenario: future archive surface exposes provenance

- **WHEN** a future archive/history station-series API returns samples
- **THEN** it SHALL expose provenance that distinguishes archive/DB data from
  retained display CSV data
- **AND** that provenance SHALL include source id, cycle time, model id,
  forcing version id, storage source, and freshness or retention class

#### Scenario: future archive errors remain distinct from disk errors

- **WHEN** a future archive/history selector cannot resolve DB/archive data
- **THEN** it SHALL use DB/archive-specific errors such as
  `FORCING_VERSION_NOT_FOUND`, `FORCING_VERSION_NOT_FINALIZED`,
  `FORCING_VERSION_FILTER_CONFLICT`, or `STATION_NOT_IN_FORCING_VERSION`
- **AND** it SHALL NOT reuse `STATION_FORCING_FILE_NOT_FOUND` for DB/archive
  selector failures
