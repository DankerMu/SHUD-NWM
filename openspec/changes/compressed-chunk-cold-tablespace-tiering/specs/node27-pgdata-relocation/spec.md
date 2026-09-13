# Proposed survivor delta

Target contract only; canonical implementation is unchanged by this revision.

## MODIFIED Requirements

### Requirement: Placement-aware observation and truthful evidence

Current PGDATA bytes MUST be attributed to their configured observed storage
root without also charging `/home`; existing `/home` placement MUST remain
compatible. Ordinary governance MUST preserve actual PGDATA
device/available-space binding and refuse unknown or conflicting placement
without requiring cold-installer behavior or importing retired cold modules.
Disposable validation MUST use isolated identities and MUST NOT be represented
as production HDD performance or rollout acceptance. Secrets MUST NOT appear in
public receipts or errors.

#### Scenario: PGDATA is relocated to the large volume

- **WHEN** current PGDATA is observed under `/data/GHDC` and the old directory
  remains retained
- **THEN** its current-PGDATA sample is assigned only to the large-volume
  observation and the old copy is not treated as the live cluster

#### Scenario: Disposable evidence is presented as production approval

- **WHEN** an isolated rehearsal succeeds without the later live
  SQL/API/browser/ingest gates
- **THEN** its result proves migration mechanics only and cannot authorize
  production cutover or deletion of the old copy

#### Scenario: Device attribution cannot be established

- **WHEN** the configured PGDATA root and observed filesystem/device evidence
  are unknown or conflicting
- **THEN** governance refuses to invent available space or charge another root
  as the live PGDATA device, while preserving ordinary `/home` and `/data/GHDC`
  observations

## ADDED Requirements

### Requirement: PGDATA relocation SHALL own its retained command, container and evidence behavior

The PGDATA command/host, container and evidence owners SHALL provide the minimal
bounded-command, snapshot normalization/serialization and descriptor/RAID/SMART
closure used by host/migrate without retired cold-family imports or
compatibility re-exports. Image pinning SHALL use the existing container
contract/snapshot owner. This ownership transfer SHALL preserve the closed
migration state machine, clean stop, whole-copy validation, exact rebind,
pre-write rollback and durable first-write boundary; negative guards against
incompatible old cold binds/services SHALL remain protective refusals, not
installer support.

#### Scenario: Retired packages are unavailable

- **WHEN** an isolated relocation runs after cold-family deletion
- **THEN** the PGDATA-owned command/container/evidence paths preserve bounded
  child/pipe cleanup, snapshot identity and descriptor/RAID/SMART safety without
  a cold module

#### Scenario: Write release has made the old copy stale

- **WHEN** rollback is requested after the durable writes-released marker
- **THEN** the surviving relocation owner refuses switchback to the stale
  original, including after an interrupted release
