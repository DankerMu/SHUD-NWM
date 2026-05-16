## ADDED Requirements

### Requirement: Valid Basins models are published as immutable packages

The system SHALL publish each validated Basins SHUD model into an immutable object-store package containing runtime input files, selected calibration metadata, GIS sidecars, and a package manifest with checksums.

#### Scenario: Package publication writes manifest and checksum

- **WHEN** a validated Basins model is published to `OBJECT_STORE_ROOT` and `OBJECT_STORE_PREFIX`
- **THEN** the output includes a stable `model_package_uri`, a package checksum, a manifest JSON, and per-file checksums for all included runtime input and GIS files

#### Scenario: Package publication is idempotent

- **WHEN** publication is run again for unchanged source files and the same target version
- **THEN** it returns `already_done` or equivalent status without rewriting a different package checksum

#### Scenario: Source checksum changes for same target version

- **WHEN** publication is run for the same target version but source file checksums differ from the existing package manifest
- **THEN** it refuses to overwrite the package unless an explicit new version or force option is supplied, and reports the checksum conflict

### Requirement: Historical forcing is represented without accidental bulk duplication

The system SHALL record historical forcing CSV metadata separately from the runtime model input package and SHALL only copy forcing CSV payloads when explicitly requested.

#### Scenario: Forcing metadata inventory

- **WHEN** a model has CMFD forcing CSV files under `forcing/` or `focing/`
- **THEN** the package manifest records the forcing directory, CSV count, time coverage when parsable from file headers, and aggregate checksum metadata

#### Scenario: Runtime package excludes bulk forcing by default

- **WHEN** publication runs without an explicit historical forcing copy option
- **THEN** the runtime model package excludes the full forcing CSV payloads but retains forcing metadata needed for migration planning

#### Scenario: Historical forcing copy is explicit

- **WHEN** publication runs with an explicit option to copy historical forcing payloads
- **THEN** forcing CSV files are written under a separate object-store prefix and the manifest records forcing payload URI, file count, and checksum evidence

### Requirement: Production migration rejects symlink-only evidence

The system SHALL provide a migration report command that distinguishes development symlinks from production data copies and rejects symlink-only production migration evidence.

#### Scenario: Symlink target fails production migration evidence

- **WHEN** production migration evidence is generated for `/volume/data/nwm/Basins`
- **THEN** it states that target environments must contain actual copied data and fails if the target `Basins` path is a symlink

#### Scenario: Copied target passes production migration evidence

- **WHEN** production migration evidence is generated for a real copied `Basins` directory
- **THEN** it exits successfully and records file count, byte count, inventory checksum, and source-to-target copy metadata
