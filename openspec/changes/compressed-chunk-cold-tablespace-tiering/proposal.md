## Why

The initial cold-tier proposal addressed active rows and terminal compressed
TimescaleDB chunks sharing hot placement. Current devices and capacity require
fresh deployment evidence; historical placement or chunk counts are not admission
authority. A DB-only tier must prove that the actual compressed relations and
every index move together, not merely the near-empty origin chunk shell.

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
- Bind cold eligibility to physical parent identities and the narrow river schema
  through child #2290; exclude legacy stores and invalidate stale name-reuse
  evidence without changing receipt wire schemas. Child #2291 then carries an
  explicitly reviewed bounded count through all cold rollout gates. Both children
  and separate external readiness must precede any new production G0.

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
- #1929: bind the #1893 target writability probe to an explicitly configured,
  observed numeric container runtime UID/GID and carry that principal in receipt
  evidence; no image-user-name or root fallback is allowed.
- #1895: controlled node-27 deployment and live receipts for migration,
  automatic convergence, hot/cold reads, timers, and display performance. Its
  reviewed executable runbook and Python readiness owners land first through
  dedicated child #2137, before node-27 access; six is a historical preflight
  count rather than reusable identity, and rollback acceptance uses the exact-SHA
  disposable move-back branch plus live read-only compatibility. The first G1
  observation at merged SHA `a8db554d6402bec642e9a05627eae64b2b79aec3`
  ended as NO-GO when production parity read the parent hypertable and reached its
  finite 3600-second statement timeout before publishing a census. Child #2224
  must bind mandatory, no-default parity input to each current durable physical
  origin relation, retain the half-open window as an identity fence, route every
  production caller through that owner, and merge before G1 is retried at a new
  reviewed SHA; this is an interpretation correction to the older D4 wording that
  allowed a parent-plus-window implementation, and raising or removing the timeout
  is not acceptance. The independent C4
  producer/validator/publisher/binder has already merged through #2123, and its
  input-classification precedence was clarified by #2130; #1895 consumes that
  promoted capability for live evidence rather than reimplementing it. The
  installer may reconcile/roll back only an in-progress install whose private
  authority still exists; terminal `installed` closes that authority, so every
  later trigger preserves the installed topology. After movement, reversal also
  waits for a reviewed live move-back entrypoint from its owning implementation
  issue.
- #2290: physical-parent admission, all catalog/runtime/census/post-target callers,
  distinct wide/narrow fixtures, identity-bound inventory digests and isolated
  rename/replacement proof; detailed fixture in `fixtures/issue-2290.md`.
- #2291: dependent reviewed-count/baseline cutover and G3 population alignment,
  without per-tick/time-budget expansion or production execution.
- No row-schema migration, public API change, TimescaleDB/PostgreSQL upgrade,
  node-22 scheduling change, or archive-lane restoration.
