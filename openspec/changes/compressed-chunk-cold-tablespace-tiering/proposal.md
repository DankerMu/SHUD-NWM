## Why

Node-27 currently keeps both active rows and terminal compressed TimescaleDB
chunks on `/home`, while the recovered `/data/GHDC` RAID has ample capacity.
The retired archive lane cannot simply be restored: a narrower DB-only tier
must prove that the actual compressed relations and every index move together,
not merely the near-empty origin chunk shell.

## What Changes

- Introduce a cold-residency contract for compressed chunk groups: eligibility
  derives only from the display business watermark and compression lag, and a
  group includes the origin chunk, its compressed relation, and all physical
  index/TOAST storage reachable from both.
- Require an isolated PostgreSQL 15.2 / TimescaleDB 2.10.2 cluster experiment
  before freezing the supported move, rollback, lock, decompression,
  recompression, and retention sequence. A throwaway database inside the live
  cluster is insufficient because tablespaces are cluster-scoped.
- Add a dry-run-default, bounded, idempotent, receipted convergence runner and
  a fail-closed fresh-tablespace installation/governance contract.
- Re-admit `/dev/md0` only for terminal compressed DB storage after root-level
  RAID and two-member SMART evidence; this does not revive product archive,
  salvage, or rebuild lanes retired by #1309/#1370.
- Forbid attaching the cold tablespace to either business hypertable, moving
  active/uncompressed chunks, or moving PGDATA, WAL, or object-store data.

## Capabilities

### New Capabilities

- `compressed-chunk-cold-residency`: eligibility, complete physical residency
  groups, atomic migration/recovery, lifecycle convergence, runner receipts,
  tablespace installation/governance, and live rollout evidence.

### Modified Capabilities

- None. Existing compression eligibility, write guards, lag defaults,
  retention windows, and display contracts remain unchanged.

## Impact

- #1892: this OpenSpec fixture, a pinned isolated-cluster integration probe,
  ADR 0002 amendment, and the operator residency/decompression contract.
- #1893: a node-27 cold-residency runner, receipt schema/example, configuration,
  tests, and serialized systemd integration.
- #1894: fresh tablespace/container installation and rollback tooling,
  dual-device governance, backup-readiness checks, receipts, and tests.
- #1895: controlled node-27 deployment and live receipts for migration,
  automatic convergence, hot/cold reads, timers, and display performance.
- No row-schema migration, public API change, TimescaleDB/PostgreSQL upgrade,
  node-22 scheduling change, or archive-lane restoration.
