# timeseries-db-retention Specification (delta)

## ADDED Requirements

### Requirement: Retention enforcement is hard-gated on archive receipts

The retention runner SHALL refuse enforce mode unless both (a) the
**archive-completeness receipt** emitted by the inventory audit (defined in
`timeseries-product-archive`) and (b) an **archive-rebuild-drill PASS
receipt** (defined in `archive-rebuild-drill`) exist, are fresh within a
configurable validity window, and cover the time window being dropped. These
two receipts are the complete gate set: compression state is not a retention
gate. Coverage is defined as: the completeness receipt covers the drop
window when every `hydro_run` cycle, `forcing_version` window, and
`state_snapshot` reference with rows or products in the drop window carries
a `complete` verdict (checksum-verified product archive or verified
`db-export` salvage object — salvage completion is thereby folded into
completeness); the drill receipt covers the drop window when its declared
(source, window) tuples satisfy the coverage rule in `archive-rebuild-drill`.

#### Scenario: Missing or stale gate receipts

- **WHEN** enforce mode starts and either gate receipt is missing, stale, or
  does not cover the drop window
- **THEN** the runner MUST refuse to drop anything, exit non-zero, and emit a
  receipt with the refusal reason

#### Scenario: Gates satisfied

- **WHEN** both gate receipts are fresh and cover the drop window
- **THEN** the runner MAY execute `drop_chunks` for that window

#### Scenario: Salvage-covered window is droppable with a manual recovery path

- **WHEN** the drop window includes a window whose only durable copy is
  verified `db-export` salvage objects (verdict `complete` via salvage)
- **THEN** enforce MAY drop that window's chunks
- **AND** the receipt MUST record the salvage-backed windows included, whose
  post-drop recovery path is the documented manual `COPY FROM` procedure
  (see `db-export-salvage`)

### Requirement: Window and mechanism

Retention SHALL use TimescaleDB `drop_chunks` with a 30-day default window,
targeting exactly `hydro.river_timeseries` and
`met.forcing_station_timeseries`; chunks are dropped only when their entire
range is older than the window. Metadata and coverage tables (`hydro_run`,
`run_display_coverage`, `forcing_version`, `state_snapshot`, QC/lineage)
SHALL never be retention targets.

#### Scenario: Chunk fully outside the window is dropped

- **WHEN** a chunk's `range_end` is older than the retention window at
  enforce time
- **THEN** the chunk MUST be dropped and the receipt MUST record its name and
  freed bytes

#### Scenario: Metadata tables are untouched

- **WHEN** retention enforce completes
- **THEN** row counts of the metadata/coverage tables MUST be unchanged by
  the run

### Requirement: Safety bounds

The retention runner SHALL default to dry-run, require an explicit enforce
flag, hold a flock, drop at most a configurable number of chunks per tick,
and set a statement timeout.

#### Scenario: Candidate count exceeds the per-tick bound

- **WHEN** more chunks are eligible than the per-tick maximum
- **THEN** only the maximum count MUST be dropped and the receipt MUST list
  the deferred remainder

### Requirement: Timer integration and governance visibility

Retention SHALL run from a user-level systemd timer following the existing
node-27 governance family patterns, and its units SHALL be registered in the
resource-governance audit unit list.

#### Scenario: Governance audit reports retention units

- **WHEN** the resource-governance audit runs
- **THEN** its receipt MUST include the retention service/timer states
