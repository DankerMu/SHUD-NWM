Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Upstream suggested level: absent

Seams under test:

- Isolated-cluster probe CLI/test boundary: pinned image + isolated paths ->
  measured PG 15.2 / TimescaleDB 2.10.2 movement and lifecycle verdicts.
- Shared residency-group module: catalog snapshot -> complete group or stable
  blocker; group + target -> transactional move/reconciliation result.
- Runner CLI/wrapper: dry-run/enforce config -> schema-valid receipt and bounded
  DB effects.
- Installer/governance CLI: host/container/catalog evidence -> NO-GO or exact
  topology receipt.
- Node-27 rollout: frozen reviewed SHA + approved maintenance inputs -> live
  parity/performance/timer receipt.

## 1. #1892 — Freeze the TimescaleDB 2.10.2 contract

- [x] 1.1 Create the expanded OpenSpec fixture, complete risk-pack selection, invariant matrix, boundary checklist, and pass one read-only fixture review plus strict validation.
- [x] 1.2 Add an automated isolated-cluster integration probe that refuses the live container/port/paths, pins the exact node-27 image digest and PG/TimescaleDB versions, creates its own PGDATA/cold/hot storage, and always cleans up its container and directories.
- [x] 1.3 Probe and record `timescaledb_experimental.move_chunk`, direct compressed-member ALTER, decompress-first, internal attach, two-transaction, and shell-first alternatives; freeze the single accepted shell-first transaction (lock/revalidate -> move origin shell/indexes -> decompress -> prove expanded cold -> recompress -> prove new complete cold group/parity -> commit/fresh readback), with every rejection and transient state evidenced.
- [x] 1.4 Prove normal lifecycle behavior in the isolated cluster: hot compression, complete cold move, cold read, cold decompression, replay write, recompression, repeated convergence, move-back, and `drop_chunks`, with row/value/checksum and member residency parity.
- [x] 1.5 Prove boundary behavior: exact cutoff contract, empty chunk, no user index, multiple/quoted indexes, owned TOAST, already-target no-op, and same-window chunks in both business hypertables.
- [x] 1.6 Prove failures and concurrency at shell-move, post-decompress and post-recompress stages: pinned image/server/extension drift, missing/wrong target, a safely injected catalog/path mismatch before mutation, bounded full-filesystem fault, cold expansion plus hot PGDATA/WAL headroom refusal, permission error, lock conflict, statement timeout, process/connection interruption at pre-commit and post-commit acknowledgement boundaries, relation disappearance, and injected mid-group failure; fresh readback plus target-window parity must prove original-sibling rollback or new-sibling committed target, otherwise yield an explicit mixed/unknown blocker without false success.
- [x] 1.7 Commit `probe-1892-throwaway.md` with exact image/server/extension identity and digest, commands, target-window count/aggregate/all-business-column checksum, before/intermediate/after relation/index/TOAST residency and bytes, query/lifecycle results, lock/timeout/WAL observations, cleanup proof, accepted sequence and rejected alternatives; the probe's PASS predicate must machine-check every required row rather than merely record it.
- [x] 1.8 Amend ADR 0002 and the tiering runbook: retire stale live-`ghdc` wording, distinguish the new DB-only tier from #1309/#1370 archive retirement, freeze the probe-supported sequence, require root `mdadm --detail` plus both-member SMART evidence, forbid hypertable attach, and state that PGDATA-only backup is incomplete.
- [x] 1.9 Run `openspec validate compressed-chunk-cold-tablespace-tiering --strict --no-interactive`, focused local collection/contract tests, the pinned 2.10.2 integration suite on the isolated node-27 cluster, and `uv run ruff check .`; record exact results and no stranded container/directory/red-proof stash.

## 2. #1893 — Implement bounded cold-residency convergence

- [ ] 2.1 Implement one shared residency-group resolver and transactional move/reconciliation primitive using the #1892 sequence, complete OID/member mapping, stable lock order, finite local timeouts, and source/target/mixed/unknown outcomes. Before any production mutation, derive and validate the complete live business-column inventory for both hypertables from the production schema; the #1892 four-column fixture helper is not that inventory and must not be reused as a production parity API.
- [ ] 2.2 Implement a dry-run-default runner and wrapper using the existing display business watermark and compression lag, catch-up selection, per-tick bound, deterministic cross-hypertable fairness, whole-run wall budget, and the existing lifecycle mutex/order.
- [ ] 2.3 Define the receipt JSON Schema/example with exact head/config/cluster/target identity, complete before/after member residency and bytes, durations, result/deferred/error/recovery fields, atomic mode-0600 publication, redaction, and honest post-commit publication-failure handling.
- [ ] 2.4 Add config and serialized systemd integration without adding a second unlocked lane or attaching the cold tablespace to a hypertable; assert systemd wall > wrapper wall > per-statement wall.
- [ ] 2.5 Test normal migration, already-cold no-op, catch-up, exact cutoff, empty selection, bound/fairness, maximum member count, all legal states, selection races, partial recovery, capacity/lock/timeout/disappearance errors, and receipt-publication failure.
- [ ] 2.6 Run focused unit/schema tests, the isolated PG 15.2 / TimescaleDB 2.10.2 integration suite, strict OpenSpec validation, and `uv run ruff check .`; attach normal/no-op/partial/error receipt examples.

## 3. #1894 — Install and govern the fresh cold tablespace

- [ ] 3.1 Implement dry-run-default installation/preflight for `nhms_cold` with fixed host/container paths, empty non-symlink directory, exact owner/mode/device, root RAID/SMART evidence freshness, capacity/rollback budget, and backup coverage gates.
- [ ] 3.2 Implement exact raw-container config snapshot/diff/recreate/ready/rollback handling that preserves image, env, ports, mounts, limits and restart policy while adding only the cold bind and refusing empty-directory shadowing.
- [ ] 3.3 Implement `CREATE TABLESPACE` and readback validation for catalog location, current container bind source, host device and writability; prove neither business hypertable is attached and new chunks remain in `pg_default`.
- [ ] 3.4 Extend governance to sample `/home` and `/data/GHDC` together and separately report filesystem capacity, PGDATA, cold relation bytes, object-store and shared residual use, plus trend/threshold evidence.
- [ ] 3.5 Detect dangling catalog/bind/filesystem identities, stopped-container stale mounts, degraded/rebuilding/unknown RAID fixtures, SMART failures, permission/capacity faults, and PGDATA-only backup gaps; all live-precondition failures are NO-GO.
- [ ] 3.6 Document and test rollback that stops writers/timers, restores the prior container, verifies catalog/read paths, never deletes a referenced path, and never binds an empty directory over valid data.
- [ ] 3.7 Run disposable install/rollback tests, healthy/degraded/rebuilding/unknown RAID/SMART fixtures, exact container identity diff tests, governance reconciliation tests, strict OpenSpec validation, focused pytest, and `uv run ruff check .`.

## 4. #1895 — Controlled node-27 rollout and closure

- [ ] 4.1 Freeze exact reviewed HEAD and capture preflight: clean worktree, container config/image, cluster/catalog, every candidate/hot group identity/member residency/rows/checksum, both filesystems, timer/writer/lock state, backup readiness, root RAID/SMART evidence, API valid-times/publication and #1342 plans/latencies.
- [ ] 4.2 Stop and drain autopipe/compression/residency/retention writers under the documented mutex/lock order; any active writer, conflicting lock, unknown health, insufficient worst-case rollback space, or identity mismatch is NO-GO.
- [ ] 4.3 Establish the fresh `nhms_cold` bind/tablespace from #1894 and deploy #1893 at the exact reviewed SHA; prove catalog/bind/device identity, no hypertable attach, and new-chunk `pg_default` placement before moving data.
- [ ] 4.4 Run a dry-run preview and bounded enforce to migrate the six baseline eligible compressed groups one at a time; after each group prove complete origin/compressed/index/TOAST residency, row/identity/checksum/query parity, duration/wait/bytes, and hot/cold filesystem reconciliation.
- [ ] 4.5 Prove active/uncompressed groups remain wholly in `pg_default`, current ingest can resume, and a controlled move-back returns one complete group hot without data drift or orphaned paths.
- [ ] 4.6 Restore timers and observe at least one natural serialized tick plus one newly terminal group's automatic convergence or a catalog-proven no-op; require active timers and no issue-owned failed unit.
- [ ] 4.7 Validate hot/cold SQL plans and #1342 thresholds (buffers <= 5000, SQL P95 <= 300 ms, local API P95 <= 500 ms, frontend click P95 < 2 s), public hot/cold curves/MVT/click flow, non-regressing valid-times, and complete GFS/IFS publication counts.
- [ ] 4.8 Post schema-valid live receipts and GO/NO-GO with exact deviations/rollback triggers, pass node-27 C1-C4 plus cold-residency gates and real-DB pytest, close #1891 only after #1892-#1895 acceptance is complete, then archive this shared OpenSpec change.

## Risk-pack evidence mapping

- Public API / CLI / config: tasks 2.2, 2.4, 3.1-3.3; invalid/missing values -> pre-connect/pre-mutation refusal.
- File IO / path / permissions / secrets: tasks 1.2, 2.3, 3.1-3.2; symlink/alias/mode/credential cases -> stable refusal/redaction and no unsafe overwrite.
- Schema / evidence identity: tasks 1.7, 2.3, 4.8; each producer output -> schema validation plus independent semantic readback.
- Concurrency / resources / rollback: tasks 1.6, 2.1-2.5, 3.5-3.6, 4.2-4.6; lock/timeout/full/interruption/race -> rollback or explicit recovery state.
- Legacy/display compatibility: tasks 2.4, 3.3, 4.5-4.7; unchanged hot/new chunks, ingest, retention and display -> existing behavior and performance.
- TimescaleDB/time-series domain: tasks 1.3-1.6, 2.1-2.5, 4.4-4.7; exact 2.10.2 catalog/lifecycle and business-time boundaries -> real isolated/live oracle evidence.
- Documentation/migration/backup: tasks 1.8, 3.6, 4.1-4.8; ADR/runbook/readiness and rollback evidence -> no production mutation without all gates.

Non-goals:

- No TimescaleDB/PostgreSQL upgrade, row-schema change, archive-lane restoration,
  active chunk/PGDATA/WAL/object-store move, node-22/Slurm change, automatic
  decompression, or compression-lag/retention/display-contract change.
