## Why

The node-22 production scheduler starts every timer tick but fails closed at
`db_free_registry_blocked` because its independently published file registry is
older than the mandatory 168-hour freshness bound.  Canonical readiness is
also expired, so the manual-only file-provider lifecycle must become a durable,
validated producer lifecycle before #1065 can obtain real writer evidence.

## What Changes

- Add one auditable node-22 file-provider refresh runner which serializes every
  canonical registry writer, reuses the full-Basins publisher, and renews the
  canonical-readiness/state indexes only after revalidating all indexed
  identities, referenced objects, and checksums.
- Add phase-specific atomic publication, failure, rollback, immutable-package
  orphan, bounded receipt/history, lock, and cleanup contracts.
- Add a user-systemd service/timer whose cadence is below the existing 168-hour
  limit, with install, monitoring, failure rollback, and success steady-state
  procedures.
- Capture a real scheduler pass, its actual Slurm stage job(s), newly created
  forcing/runs/states leaves, and node-27 ACL inheritance evidence.

## Capabilities

### New Capabilities

- `scheduler-registry-refresh`: Steady-state transaction and deployment
  semantics for the node-22 DB-free registry, canonical-readiness index, state
  index, and the real-writer proof they gate.

### Modified Capabilities

None.

## Impact

- Runtime: node-22 user-systemd, existing registry/readiness/state publishers,
  a new bounded wrapper/receipt, and the unchanged scheduler consumer.
- Contracts: all freshness/checksum/identity/object gates remain fail closed;
  node-22 remains database-free.
- Oracles: node-22 proves scheduling/Slurm; node-27 proves the same shared-NFS
  artifact identities and inherited `nwm` ACLs.
- Dependency: blocks #1065 / PR #1075; excludes #856 and #1069-#1072.
