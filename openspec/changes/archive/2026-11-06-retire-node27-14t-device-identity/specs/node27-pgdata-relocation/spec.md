# Spec Delta: node27-pgdata-relocation

## MODIFIED Requirements

### Requirement: Placement-aware observation and truthful evidence

Current PGDATA bytes MUST be attributed to the device that actually holds the
configured PGDATA root and charged to no other observed root; `/home`
placement, current since the 2026-11-05 rebuild, MUST remain supported.
Ordinary governance MUST preserve actual PGDATA device/available-space binding
and refuse unknown or conflicting placement without requiring cold-installer
behavior or importing retired cold modules. Governance MUST NOT sample the
retired 14T volume path as a separate filesystem. Disposable validation MUST
use isolated identities and MUST NOT be represented as production HDD
performance or rollout acceptance. Secrets MUST NOT appear in public receipts
or errors.

#### Scenario: PGDATA is relocated to the large volume

- **WHEN** current PGDATA is observed under its configured root (since the
  2026-11-05 rebuild `/home/nwm/nhms-primary/pgdata` on the `/home` LV; the 14T
  large volume formerly mounted at `/data/GHDC` is retired) and an old
  directory such as `/home/nwm/nhms-pgdata` remains retained
- **THEN** its current-PGDATA sample is assigned only to the device that holds
  the configured root and the old copy is not treated as the live cluster

#### Scenario: Retired volume path is not sampled

- **WHEN** governance collects filesystem observations after the 14T volume
  formerly mounted at `/data/GHDC` was retired
- **THEN** the report carries no separate `cold` filesystem entry for that
  path, which would only re-measure the root LV, while `root` and `home`
  observations remain

#### Scenario: Disposable evidence is presented as production approval

- **WHEN** an isolated rehearsal succeeds without the later live SQL/API/browser/ingest gates
- **THEN** its result proves migration mechanics only and cannot authorize production cutover or deletion of the old copy

#### Scenario: Device attribution cannot be established

- **WHEN** the configured PGDATA root and observed filesystem/device evidence
  are unknown or conflicting
- **THEN** governance refuses to invent available space or charge another root
  as the live PGDATA device, while preserving ordinary `/` and `/home`
  observations

## ADDED Requirements

### Requirement: Retired production relocation identity SHALL refuse until re-contracted

The #2240 production identity (source `/home/nwm/nhms-pgdata`, target on the
14T RAID formerly mounted at `/data/GHDC`) no longer exists on node-27 (ADR 0002
Revision 2026-11-05). Until a new source/target device identity is contracted,
the relocation CLI MUST refuse every production invocation, that is any
invocation whose effective configuration has no disposable root, with an
explicit reason naming the retirement and ADR 0002 Revision 2026-11-05. The
refusal MUST happen before any container, unit or database command, lifecycle
lock or state write; only the read-only workspace durability probe may run.
Unlisted state/action pairs MUST still refuse with the closed-table reason, and
disposable mechanics MUST remain runnable.

#### Scenario: Production planning reports the retirement

- **WHEN** an operator runs the default `plan` action without `--disposable-root`
- **THEN** the command stays read-only, issues no container, unit or database
  command, and reports the retirement as its only blocker

#### Scenario: A production mutating action is requested

- **WHEN** `prepare`, or an allowed `copy`/`activate`/`rollback`/`release`
  transition on a frozen production workspace, is requested with `--enforce`
- **THEN** the command exits non-zero with the retirement reason before taking
  the lifecycle lock, and the workspace state is unchanged

#### Scenario: Production hardware health is requested

- **WHEN** host health is requested for a configuration without a disposable root
- **THEN** it refuses with the retirement reason instead of checking the
  retired RAID filesystem
