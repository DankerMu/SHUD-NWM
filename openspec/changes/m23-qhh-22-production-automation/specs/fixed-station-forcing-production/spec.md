## ADDED Requirements

### Requirement: Per-cycle forcing targets fixed stations
The forcing producer SHALL generate meteorological forcing for fresh cycles by mapping canonical grids to the fixed SHUD forcing stations seeded from the processed QHH package.

#### Scenario: Fixed stations selected
- **WHEN** forcing generation starts for a QHH model/cycle
- **THEN** it loads active `met.met_station` rows for the model's basin version with `station_role="forcing_grid"`
- **AND** it uses their SHUD forcing index and forcing filename metadata as the target station contract.

#### Scenario: No fixed stations blocks forcing
- **WHEN** no active forcing-grid stations exist for the QHH model/basin version
- **THEN** forcing generation fails with a missing-stations blocker
- **AND** no `met.forcing_version` is marked ready for that cycle.

### Requirement: Dynamic station timeseries are persisted
The system SHALL persist generated forcing values and provenance for each accepted model/source/cycle.

#### Scenario: Forcing version created
- **WHEN** station forcing generation completes for a canonical product
- **THEN** it writes one `met.forcing_version` linked to model, basin, source, cycle, canonical product, station count, variable set, time range, and quality metadata
- **AND** it writes `met.forcing_station_timeseries` rows for each generated station/variable/time value.

#### Scenario: Idempotent forcing generation
- **WHEN** forcing generation reruns for the same model/source/cycle/canonical identity
- **THEN** it reuses or replaces according to a deterministic idempotency policy
- **AND** it does not create duplicate ready forcing versions for the same candidate identity.

#### Scenario: Bad interpolation coverage blocks readiness
- **WHEN** canonical grids cannot cover a station or required variable/time range
- **THEN** forcing generation records the affected station/variable/time coverage gap
- **AND** downstream SHUD submission is blocked unless the policy explicitly permits reduced scope.

### Requirement: SHUD forcing package is produced
The system SHALL materialize SHUD-ready forcing files from persisted station forcing using the processed basin's file contract.

#### Scenario: SHUD forcing files written
- **WHEN** forcing version is ready
- **THEN** the runtime package contains `qhh.tsd.forc` and per-station forcing CSV/text files expected by SHUD project mode
- **AND** file paths, checksums, station count, variable count, time range, and units are recorded in the runtime manifest.

#### Scenario: rSHUD contract honored without runtime dependency
- **WHEN** SHUD forcing files are created
- **THEN** their columns, units, station ordering, and filenames follow the existing rSHUD/AutoSHUD-informed processed basin contract
- **AND** the production cycle does not call rSHUD as the hydrologic runtime solver.
