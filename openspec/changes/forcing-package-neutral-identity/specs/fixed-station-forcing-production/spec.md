# fixed-station-forcing-production (delta)

## MODIFIED Requirements

### Requirement: SHUD forcing package is produced

The system SHALL materialize SHUD-ready forcing files from persisted station forcing using the processed basin's file contract, publishing the main station-index member under the basin-neutral canonical identity.

#### Scenario: SHUD forcing files written

- **WHEN** forcing version is ready
- **THEN** the runtime package contains the canonical basin-neutral station-index member `shud/stations.tsd.forc` and per-station forcing CSV/text files expected by SHUD project mode
- **AND** file paths, checksums, station count, variable count, time range, and units are recorded in the runtime manifest.

#### Scenario: rSHUD contract honored without runtime dependency

- **WHEN** SHUD forcing files are created
- **THEN** their columns, units, station ordering, and filenames follow the existing rSHUD/AutoSHUD-informed processed basin contract
- **AND** the production cycle does not call rSHUD as the hydrologic runtime solver.

## ADDED Requirements

### Requirement: Forcing package station-index identity is basin-neutral and fails closed

The direct-grid SHUD forcing package main station-index member SHALL carry the fixed basin-neutral canonical identity `shud/stations.tsd.forc`, with the legacy identity `shud/qhh.tsd.forc` accepted read-only for historical packages. Producers SHALL emit only the canonical member. Consumers SHALL resolve the station index by requiring exactly one member from the {canonical, legacy} set in both the package manifest and the staged filesystem, failing closed on ambiguity or absence, and SHALL NOT treat the member filename as evidence of the forcing data's basin identity.

#### Scenario: canonical member published for every basin

- **WHEN** a direct-grid forcing package is produced for any basin, QHH included
- **THEN** the main station-index member is `shud/stations.tsd.forc` with manifest role `shud_forcing`
- **AND** the station rows and per-station CSVs derive from that basin's own inputs, not from the member filename.

#### Scenario: legacy package remains consumable

- **WHEN** a historical package whose only station-index member is `shud/qhh.tsd.forc` is staged
- **THEN** the runtime resolves, checksums, and stages it exactly as before the migration
- **AND** existing object-store packages are not rewritten.

#### Scenario: ambiguous index membership fails closed

- **WHEN** the package manifest lists more than one station-index member from the {canonical, legacy} set, or the staged filesystem contains both files
- **THEN** the runtime raises the fail-closed error `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS` naming the conflicting members
- **AND** no fallback member selection is attempted.

#### Scenario: missing index membership fails closed for direct-grid

- **WHEN** a direct-grid package contains no station-index member from the {canonical, legacy} set
- **THEN** the existing missing-member errors are raised and their messages name both accepted identities.

#### Scenario: real QHH model asset keeps its identity

- **WHEN** the real QHH model asset `data/Basins/qhh/input/qhh/qhh.tsd.forc` or its bootstrap tooling is exercised
- **THEN** its QHH-specific naming and semantics are unchanged by this contract.
