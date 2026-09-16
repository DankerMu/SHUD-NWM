# PGDATA survivor delta

Applied to canonical alongside the matching R1 owner transfers. Production
relocation and effective deployment remain separately authorized gates.

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

### Requirement: Retained SQL performance evidence SHALL keep query identity

The PGDATA workload owner SHALL retain the minimum capture, canonical-parameter
validation, native named EXPLAIN binding and deterministic query identity needed
by its explicit-cycle before/after performance evidence before deleting the old
G7 recorder. The owner SHALL reject binding drift before execution or acceptance.
Equivalent mapping order SHALL preserve identity; changed semantic bindings
SHALL change identity or refuse. Shipping forecast SQL alone SHALL NOT substitute
for the workload's evidence producer. Synthetic positional compatibility and the
four-lane cold rollout protocol are not required survivors.

#### Scenario: A manual workload still needs the retiring recorder

- **WHEN** R3 removes the old recorder used by retained PGDATA SQL measurements
- **THEN** its necessary evidence behavior, real entrypoint and assertion-bearing tests already belong to the PGDATA workload owner, with no old exports or dangling manual instructions

#### Scenario: Captured SQL has drifted from the frozen workload

- **WHEN** a required cycle/run/model/segment binding differs from canonical workload inputs
- **THEN** the retained owner refuses before live EXPLAIN or PASS; mapping insertion order alone does not change the accepted query identity
