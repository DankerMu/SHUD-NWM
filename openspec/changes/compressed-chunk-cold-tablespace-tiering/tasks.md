Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Upstream suggested level: absent

Seams under test:

- Isolated-cluster probe CLI/test boundary: pinned image + isolated paths ->
  measured PG 15.2 / TimescaleDB 2.10.2 movement and lifecycle verdicts.
- Shared residency-group module: catalog snapshot -> complete group or stable
  blocker; group + target -> transactional move/reconciliation result.
- Runner CLI/wrapper: dry-run/enforce config, including required numeric runtime
  UID/GID -> schema-valid receipt and bounded DB effects.
- Target inspector: one bounded Mounts + strict numeric `Config.User` observation
  -> exact `uid:gid` writable probe -> observed schema-1.1 target evidence.
- Installer/governance CLI: host/container/catalog evidence -> NO-GO or exact
  topology receipt.
- Node-27 rollout: frozen reviewed SHA + approved maintenance inputs -> live
  parity/performance/timer receipt.

## 1. #1892 — Freeze the TimescaleDB 2.10.2 contract

- [x] 1.1 Create the expanded OpenSpec fixture, complete risk-pack selection, invariant matrix, boundary checklist, and pass one read-only fixture review plus strict validation.
- [x] 1.2 Add an automated isolated-cluster integration probe that refuses the live container/port/paths, pins the exact node-27 image digest and PG/TimescaleDB versions, creates its own PGDATA/cold/hot storage, and always cleans up its container and directories.
- [x] 1.3 Probe and record `timescaledb_experimental.move_chunk`, direct
  compressed-member ALTER, decompress-first, internal attach, two-transaction,
  and shell-first alternatives; freeze the single accepted shell-first
  transaction (lock/revalidate -> move origin shell/indexes -> decompress ->
  prove expanded cold -> recompress -> prove new complete cold group/parity ->
  commit/fresh readback), with every rejection and transient state evidenced.
- [x] 1.4 Prove normal lifecycle behavior in the isolated cluster: hot compression, complete cold move, cold read, cold decompression, replay write, recompression, repeated convergence, move-back, and `drop_chunks`, with row/value/checksum and member residency parity.
- [x] 1.5 Prove boundary behavior: exact cutoff contract, empty chunk, no user index, multiple/quoted indexes, owned TOAST, already-target no-op, and same-window chunks in both business hypertables.
- [x] 1.6 Prove failures and concurrency at shell-move, post-decompress and
  post-recompress stages: pinned image/server/extension drift, missing/wrong
  target, a safely injected catalog/path mismatch before mutation, bounded
  full-filesystem fault, cold expansion plus hot PGDATA/WAL headroom refusal,
  permission error, lock conflict, statement timeout, process/connection
  interruption at pre-commit and post-commit acknowledgement boundaries,
  relation disappearance, and injected mid-group failure; fresh readback plus
  target-window parity must prove original-sibling rollback or new-sibling
  committed target, otherwise yield an explicit mixed/unknown blocker without
  false success.
- [x] 1.7 Commit `probe-1892-throwaway.md` with exact image/server/extension
  identity and digest, commands, target-window count/aggregate/all-business-column
  checksum, before/intermediate/after relation/index/TOAST residency and bytes,
  query/lifecycle results, lock/timeout/WAL observations, cleanup proof, accepted
  sequence and rejected alternatives; the probe's PASS predicate must
  machine-check every required row rather than merely record it.
- [x] 1.8 Amend ADR 0002 and the tiering runbook: retire stale live-`ghdc`
  wording, distinguish the new DB-only tier from #1309/#1370 archive retirement,
  freeze the probe-supported sequence, require root `mdadm --detail` plus
  both-member SMART evidence, forbid hypertable attach, and state that
  PGDATA-only backup is incomplete.
- [x] 1.9 Run `openspec validate compressed-chunk-cold-tablespace-tiering
  --strict --no-interactive`, focused local collection/contract tests, the pinned
  2.10.2 integration suite on the isolated node-27 cluster, and `uv run ruff
  check .`; record exact results and no stranded container/directory/red-proof
  stash.

## 2. #1893 — Implement bounded cold-residency convergence

- [x] 2.1 Implement the production catalog/parity/transaction owner in
  `packages/common/compressed_chunk_cold_runtime.py`, consuming the #1892 pure
  contract and its sole shell-first sequence. It must resolve complete OID/member
  mappings, perform read-only validation of the fixed target catalog/container/
  host-path/device identity, lock in stable order, revalidate under finite local
  timeouts, reconcile source/target/mixed/unknown outcomes, and derive every
  non-dropped user column in physical order from both live hypertables before
  any mutation.
  It must validate that `valid_time` is the sole open Timescale dimension and has PostgreSQL type `timestamptz`, bind the inventory
  descriptor/digest to window count/non-null counts/checksum, and never import
  the probe-private four-column helper. Production parity must be a database-side
  bounded single-row aggregate; client code must not fetch/materialize all rows.
  After heap locks and before movement SQL, re-derive both inventories and the
  target-window parity in the moving transaction and require exact preflight
  equality.
- [x] 2.2 Implement `scripts/node27_cold_residency.py` plus
  `scripts/node27_cold_residency_once.sh`, dry-run by default, using the existing
  display business watermark and compression lag. Scan bounded per-hypertable
  catalog input, assign oldest-first rank within each hypertable, and merge all
  catch-up candidates by stable `(per_hypertable_rank, range_end, hypertable,
  origin_oid)` order. Record no-write `already_cold` observations without
  consuming the mutation bound, enforce a positive per-tick mutation
  bound and maximum member count, and apply finite statement/wrapper budgets.
  Require positive `NODE27_COLD_RESIDENCY_COLD_RESERVE_BYTES` and
  `NODE27_COLD_RESIDENCY_WAL_RESERVE_BYTES` with no implicit Python, shell, or
  example-template defaults; Issue #1895 supplies measured live values before
  deployment. Freshly sample cold/hot free bytes immediately before every group;
  never reuse one sample across multiple rewrites.
- [x] 2.3 Define `schemas/timeseries_cold_residency_receipt.schema.json` and
  normal/no-op/intent/partial/error examples. Bind exact head/config/cluster/
  target identity from a required real inspector (expected config cannot be its
  own observation), validated business-column inventory and per-window parity,
  complete before/intermediate/after member residency and bytes, capacity
  inputs/decision, durations, result/deferred/error/recovery fields, and stable
  redaction. Before enforce mutation, atomically write a same-directory mode-0600
  intent sidecar and replace the public receipt with the same schema-valid
  `in_progress` payload. For every planned mutation, intent must include the
  original compressed sibling/member snapshot, source residency, preflight
  inventory/parity and actual per-group capacity decision so startup can prove
  complete-source rollback rather than comparing an after-state to itself. The sidecar is authoritative until a freshly reconciled
  terminal receipt is durably published and the sidecar is durably removed with
  parent-directory fsync and identity verification. On
  startup, an existing sidecar must be fresh-reconciled and terminally published
  before new selection; mixed/unknown blocks the tick. Publication failure is
  non-success, never triggers mutation replay, and cannot leave an older success
  looking current.
- [x] 2.4 Add `packages/common/node27_timeseries_lifecycle_lock.py` with fixed
  mutex `/tmp/nhms-node27-timeseries-lifecycle.lock`. Compression, cold
  residency, retention, and manual decompression/replay must acquire it before
  any existing lane-local or database relation lock, assert the fixed file is a
  no-follow regular file owned by the effective user with mode 0600, and release
  it on every terminal path; autopipe remains outside the flock because it cannot write an eligible
  compressed group and is fenced by transactional revalidation. Add cold
  residency as the second sequential `ExecStart` of the existing compression
  oneshot, after compression, using the existing 04:25 timer and no new timer.
  Assert each wrapper wall exceeds its statement wall plus cleanup margin and the
  one service wall exceeds both sequential wrapper walls plus the systemd margin.
  Move the existing retention timer after that worst-case service window and
  retain lifecycle-lock refusal as the runtime backstop. Do not change the
  retention window and do not attach `nhms_cold` to a hypertable.
- [x] 2.5 Test normal migration, already-cold no-op, catch-up, exact cutoff,
  empty selection, bound/fairness, maximum member count, all legal states,
  selection races, partial recovery, capacity/lock/timeout/disappearance errors,
  multi-group free-space shrinkage, bounded single-row parity, locked inventory/
  parity drift, real target-inspector failure, pre-movement SQL event identity,
  unresolved-intent source/target startup, durable unlink failure, every early
  error replacing stale success, and receipt-publication failure.
- [x] 2.6 Run focused unit/schema tests, the isolated PG 15.2 / TimescaleDB 2.10.2
  integration suite, strict OpenSpec validation, and `uv run ruff check .`;
  attach normal/no-op/intent/partial/error receipt examples.

## 2A. #1929 — Bind target writability to numeric runtime identity

- [x] 2A.1 Require explicit non-root
  `NODE27_COLD_RESIDENCY_CONTAINER_EXEC_UID/GID` integers for dry-run and enforce
  before any database connection; propagate them through `RunnerConfig` and
  `RuntimeConfig`. Require each decimal component in `1..4294967294`; reject
  missing/empty/whitespace/named/non-integral/Python-bool/negative/above-bound/
  either-zero/one-component-only input with no `postgres`, root, image-default,
  UID-only, or implicit fallback. Expose both keys unassigned
  in the public env example for #1895 to fill after fresh measurement.
- [x] 2A.2 Replace the mount-only production observation with one bounded inert,
  small Docker inspect projection that stays inside the existing 5-second/64-KiB
  ceilings and parses exactly one cold bind plus strict numeric `Config.User`;
  reject missing/empty/named/UID-only/malformed/either-root/mismatched identity
  before running `test -w`, then execute that check as the same `<uid>:<gid>`.
- [x] 2A.3 Carry observed `container_exec_uid/gid` through `TargetIdentity` and
  target receipt evidence. New writers/examples use schema `1.1`; the shipping
  schema/readers accept historical `1.0` and current `1.1`; `1.0` target objects
  omit the fields, observed `1.1` requires both non-root integers, and unobserved
  `1.1` requires both present as null without expected-config echo.
- [x] 2A.4 Test dry-run and enforce preflight for the discriminating
  image-`postgres=1000:1000` / expected+observed runtime `1005:1005` /
  owner-matched mode-0700 path case; assert exact numeric argv, the complete
  env/Python and inspect refusal matrices before writable/SQL, config tombstone/
  redaction, 1.0-omit/1.1-observed-required/1.1-unobserved-null schema
  compatibility, shipping examples, fixed inspect ceilings, and selector
  producer-consumer closure.
- [ ] 2A.5 Run focused target/runtime/CLI/schema/selector tests, full pytest, Ruff,
  strict OpenSpec, and a node-27 live read-only/disposable receipt proving current
  `Config.User`, numeric writable success, named `postgres` failure, unchanged
  live container identity, and zero DDL/chunk movement.

## 3. #1894 — Install and govern the fresh cold tablespace

- [x] 3.1 Implement dry-run-default installation/preflight for `nhms_cold` with fixed host/container paths, empty non-symlink directory, exact owner/mode/device, root RAID/SMART evidence freshness, capacity/rollback budget, and backup coverage gates.
- [x] 3.2 Implement exact raw-container config snapshot/diff/recreate/ready/rollback handling that preserves image, env, ports, mounts, limits and restart policy while adding only the cold bind and refusing empty-directory shadowing.
- [x] 3.3 Implement `CREATE TABLESPACE` and readback validation for catalog location, current container bind source, host device and writability; prove neither business hypertable is attached and new chunks remain in `pg_default`.
- [x] 3.4 Extend governance to sample `/home` and `/data/GHDC` together and separately report filesystem capacity, PGDATA, cold relation bytes, object-store and shared residual use, plus trend/threshold evidence.
- [x] 3.5 Detect dangling catalog/bind/filesystem identities, stopped-container stale mounts, degraded/rebuilding/unknown RAID fixtures, SMART failures, permission/capacity faults, and PGDATA-only backup gaps; all live-precondition failures are NO-GO.
- [x] 3.6 Document and test rollback that stops writers/timers, restores the prior container, verifies catalog/read paths, never deletes a referenced path, and never binds an empty directory over valid data.
- [x] 3.7 Run disposable install/rollback tests with this minimum checked-in fixture matrix and verification set:
  - pinned-image synthetic-mount container;
  - `mdadm --detail` healthy `[UU]`, degraded, rebuilding, recovering/reshaping, missing/substituted-member, and unknown cases;
  - two-member SMART PASS, one-member FAIL, and one-member unknown;
  - correct/wrong/missing mount, symlink, nonempty, and wrong owner/mode/device paths;
  - catalog absent, expected, drifted, and dangling `pg_tblspc` targets;
  - PGDATA-only and PGDATA-plus-all-target backup inventories;
  - stopped-container stale mount, rollback empty-shadow, and referenced-path deletion attempts;
  - exact container config diff, installer normal/already-ready/NO-GO/progress/rollback/error receipts, governance healthy/drift receipts, selector ownership, strict OpenSpec, focused pytest, and `uv run ruff check .`.

## 4. #1895 — Controlled node-27 rollout and closure

- [ ] 4.1 Freeze exact reviewed HEAD and capture preflight: clean worktree,
  container config/image, cluster/catalog, every candidate/hot group identity/
  member residency/rows/checksum, both filesystems, timer/writer/lock state,
  backup readiness, root RAID/SMART evidence, API valid-times/publication and
  #1342 plans/latencies.
- [ ] 4.2 Stop and drain autopipe/compression/residency/retention writers under the documented mutex/lock order; any active writer, conflicting lock, unknown health, insufficient worst-case rollback space, or identity mismatch is NO-GO.
- [ ] 4.3 Establish the fresh `nhms_cold` bind/tablespace from #1894 and deploy #1893 at the exact reviewed SHA; prove catalog/bind/device identity, no hypertable attach, and new-chunk `pg_default` placement before moving data.
- [ ] 4.4 Run a dry-run preview and bounded enforce to migrate the six baseline eligible compressed groups one at a time; after each group prove complete origin/compressed/index/TOAST residency, row/identity/checksum/query parity, duration/wait/bytes, and hot/cold filesystem reconciliation.
- [ ] 4.5 Prove active/uncompressed groups remain wholly in `pg_default`, current ingest can resume, and a controlled move-back returns one complete group hot without data drift or orphaned paths.
- [ ] 4.6 Restore timers and observe at least one natural serialized tick plus one newly terminal group's automatic convergence or a catalog-proven no-op; require active timers and no issue-owned failed unit.
- [ ] 4.7 Validate hot/cold SQL plans and #1342 thresholds (buffers <= 5000, SQL P95 <= 300 ms, local API P95 <= 500 ms, frontend click P95 < 2 s), public hot/cold curves/MVT/click flow, non-regressing valid-times, and complete GFS/IFS publication counts.
- [ ] 4.8 Post schema-valid live receipts and GO/NO-GO with exact deviations/rollback triggers, pass node-27 C1-C4 plus cold-residency gates and real-DB pytest, close #1891 only after #1892-#1895 acceptance is complete, then archive this shared OpenSpec change.

## Risk-pack evidence mapping

- Public API / CLI / config: tasks 2.2, 2.4, 2A.1-2A.2, 3.1-3.3;
  invalid/missing values -> pre-connect/pre-mutation refusal.
- File IO / path / permissions / secrets: tasks 1.2, 2.3, 2A.1-2A.4,
  3.1-3.2; symlink/alias/mode/credential/principal cases -> stable refusal,
  redaction, exact numeric execution and no unsafe overwrite.
- Schema / evidence identity: tasks 1.7, 2.3, 2A.2-2A.5, 3.1-3.7, 4.8;
  historical 1.0 + current 1.1 residency target evidence, installer private
  recovery authority and public installer/governance receipts -> schema
  validation, redaction/secret rejection, durable publication, and independent
  semantic readback.
- Concurrency / resources / rollback: tasks 1.6, 2.1-2.5, 3.5-3.6, 4.2-4.6;
  lock/timeout/full/interruption/race -> rollback or explicit recovery state.
- Legacy/display compatibility: tasks 2.4, 2A.3, 3.3, 4.5-4.7; historical
  receipt recovery plus unchanged hot/new chunks, ingest, retention and display
  -> existing behavior and performance.
- TimescaleDB/time-series domain: tasks 1.3-1.6, 2.1-2.5, 4.4-4.7; exact 2.10.2 catalog/lifecycle and business-time boundaries -> real isolated/live oracle evidence.
- Documentation/migration/backup: tasks 1.8, 3.6, 4.1-4.8; ADR/runbook/readiness and rollback evidence -> no production mutation without all gates.

Non-goals:

- No TimescaleDB/PostgreSQL upgrade, row-schema change, archive-lane restoration,
  active chunk/PGDATA/WAL/object-store move, node-22/Slurm change, automatic
  decompression, or compression-lag/retention/display-contract change.
