## ADDED Requirements

### Requirement: QHH processed basin package bootstrap
The system SHALL provide an idempotent production bootstrap path that imports the existing QHH processed Basins/SHUD package into database state before scheduler submission.

#### Scenario: Bootstrap imports QHH package identity
- **WHEN** the bootstrap runs with `NHMS_BASINS_ROOT` containing `qhh/input/qhh/qhh.tsd.forc`
- **THEN** it records or verifies the QHH basin package, basin version, model id, project name, manifest/checksum identity, and package root
- **AND** it does not rebuild the watershed package or require rSHUD during a production forecast cycle.

#### Scenario: Bootstrap is idempotent
- **WHEN** the bootstrap is run repeatedly against the same QHH package identity
- **THEN** it updates existing compatible records instead of creating duplicate active model, station, or output identity rows
- **AND** it reports unchanged, created, and updated counts.

#### Scenario: Missing package blocks scheduler readiness
- **WHEN** the QHH package root or required SHUD project files are missing
- **THEN** the bootstrap records a blocked result with the missing file names
- **AND** the scheduler does not submit QHH work for that model.

### Requirement: Active QHH model instance
The scheduler SHALL discover QHH only through an active production model instance persisted in the registry/database.

#### Scenario: Active model contains scheduler-required fields
- **WHEN** QHH bootstrap creates or verifies the active `core.model_instance`
- **THEN** the record exposes scheduler-required `model_id`, `basin_id`, `basin_version_id`, `river_network_version_id`, `model_package_uri`, and `shud_code_version`
- **AND** its `resource_profile` marks the model runnable and includes the minimum QHH runtime/display metadata needed by production planning.

#### Scenario: Active model candidate exists
- **WHEN** QHH bootstrap has created or activated the model instance
- **THEN** production scheduler candidate discovery can resolve model id, basin version, river network version, package URI/root, SHUD project name, and resource profile
- **AND** the candidate identity is stable across repeated scheduler scans.

#### Scenario: Plan production discovers bootstrapped QHH
- **WHEN** `nhms-pipeline plan-production --model-id <qhh_model_id>` runs after bootstrap
- **THEN** candidate evidence includes the QHH model
- **AND** the model is not excluded with `not_shud_model`, `not_runnable`, or `incomplete_model_metadata`.

#### Scenario: No active model is explicit blocker
- **WHEN** no active QHH model instance exists
- **THEN** scheduler evidence reports `active_model_count=0` and a no-candidate blocker
- **AND** the scheduler does not report the pass as production-ready.

#### Scenario: Duplicate active model rejected
- **WHEN** more than one active QHH model instance matches the same basin/project identity
- **THEN** scheduler discovery fails with a duplicate-active-model blocker
- **AND** no forecast, forcing, SHUD, parse, or publish stage is submitted for the ambiguous model.

### Requirement: Fixed SHUD forcing stations are seeded
The system SHALL seed fixed SHUD forcing stations from the processed basin's `qhh.tsd.forc` file into `met.met_station`.

#### Scenario: Forcing stations seeded from qhh.tsd.forc
- **WHEN** QHH bootstrap reads `qhh.tsd.forc`
- **THEN** it creates or updates one active `met.met_station` row per forcing station with `station_role="forcing_grid"`
- **AND** each row includes SHUD forcing index, forcing filename, project name, source file identity, coordinates, and elevation metadata.

#### Scenario: Station count mismatch blocks bootstrap
- **WHEN** the parsed station row count does not match the SHUD forcing file header
- **THEN** bootstrap fails with a station-count blocker
- **AND** it does not mark QHH model readiness as active for scheduling.

#### Scenario: Dynamic forecast values are not preseeded
- **WHEN** bootstrap completes
- **THEN** it has not created `met.forcing_version` or `met.forcing_station_timeseries` rows for future forecast cycles
- **AND** those rows are produced only by per-cycle forcing generation.

### Requirement: QHH output identities are seeded
The system SHALL seed or verify output river/segment identities needed to parse and publish SHUD results.

#### Scenario: Output segment identities available
- **WHEN** QHH bootstrap completes successfully
- **THEN** output parser and publisher can resolve SHUD output river ids to stable river segment identities
- **AND** the identity mapping is linked to the same model/basin version used by forcing generation.

#### Scenario: Missing output mapping blocks parse readiness
- **WHEN** output river/segment identities cannot be resolved
- **THEN** bootstrap or preflight records a parse-readiness blocker
- **AND** SHUD output is not reported as display-ready.
