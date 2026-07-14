## Context

Issue #1076 was discovered while PR #1075 verified real writer ACL
inheritance. Node-22 pass `scheduler_2026071403_b44ab3b785f4` proved its runtime
DB-free, but the 13-model registry had not been republished since 2026-06-30
and failed the intentional 168-hour limit. Canonical readiness is also expired;
the state index has the same expiring-file lifecycle. Existing bounded entries
and referenced objects are the renewal truth: a producer may bypass only age
while rechecking every other invariant, then invoke the existing publisher.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS.

## Goals / Non-Goals

**Goals:**

- Close all three file-provider lifecycles with one daily runner, shared writer
  lock, bounded receipt/history, systemd deployment, and rollback.
- Preserve consumer freshness/checksum/identity/object gates and node-22's
  no-database boundary.
- Prove recovery with actual scheduler stage job(s), new three-lane NFS leaves,
  and node-27 ACL observation.

**Non-Goals:**

- No node-22 DB/`:55433`, DB-backed forcing/model/readiness fallback, TTL
  extension, timestamp-only edit, or model lifecycle change.
- No #1065 product-archive enforce, #856, #1069-#1072, frontend/display, or
  unrelated scheduler refactor.

## Decisions

### D1. Producer lifecycle remains separate from the scheduler consumer

The refresh service wraps existing publishers; `ProductionScheduler.from_env()`
stays read-only and fail closed. A failed consumer tick cannot renew the
evidence it judges. Extending TTL was rejected because it hides stale object
references.

### D2. Every provider writer uses destination lock plus expected-preimage CAS

The common low-level atomic writer for registry/readiness/state acquires one
destination-derived lock, optionally verifies an expected preimage digest and
inode, then replaces. Refresh passes its snapshot preimage; other authoritative
writers use the same locked write seam. If another writer committed during
validation, refresh aborts `provider_preimage_changed` rather than losing new
entries. Writers hold only one destination lock at a time, avoiding cross-
provider lock ordering/deadlock. The manual interface stays compatible.
Content-addressed packages created before manifest commit are permitted
immutable side effects, capped/classified in evidence, and never auto-deleted.

### D3. Publication failure semantics follow the commit phase

Before replace, the complete old stat/digest tuple is invariant. Atomic replace
legitimately changes inode. A certain invalid replacement may atomically
restore captured validated previous bytes (`restored_previous`) without
claiming inode/mtime preservation. Replace uncertainty and receipt-only failure
are non-zero indeterminate outcomes; the consumer still sees complete old/new
bytes. Cleanup touches only certain current-run temp identities.

### D4. Readiness and state renewal revalidate indexed truth

The runner reads each bounded payload with schema/checksum/complexity/path
checks and disables only age inside the publisher. Readiness entries pass
`_validate_readiness_index` plus catalog/object identity/existence/checksum
checks before `publish_canonical_readiness_index`. State entries are loaded by
`FileStateSnapshotIndexRepository._load_index_snapshot` with freshness disabled
and object verification enabled before `publish_state_snapshot_index`. Missing
or semantically invalid input fails; no empty synthesis or DB fallback exists.

### D5. Receipt/resource contract is fixed and bounded

Schema version is `nhms.scheduler.file_provider_refresh_receipt.v1`; outcomes,
reason tokens, byte/list/string/residue caps, atomic latest, 32-file history,
64 GiB/250,000-entry/depth-32 workspace caps, 4,096 orphan cap, workspace
ownership and two-hour service timeout are normative. Evidence lists at most
256 orphan paths with total/truncation. Before provider commit, the runner
reserves an exclusive mode-0600 emergency receipt slot on a separately
preflighted local path. If primary publication fails after commit, the slot is
fsynced with `published_receipt_failed` and committed digests; recovery rebuilds
latest/history without republishing data. Journal is diagnostic only. If both
primary and the reserved slot fail, the outcome is `replace_uncertain` and
direct provider validation is required.

### D6. Live proof follows actual stage topology

One scheduler pass/candidate/run may bind multiple Slurm stage jobs. Success
requires terminal accounting and genuinely new forcing/runs/states leaves;
reused forcing is not evidence. Node-27 observes the same NFS identities and
ACLs. On failure the new refresh units roll back; on success its timer remains
enabled/active. Scheduler state and issue-owned jobs always restore.

## Risk Packs Considered

- Public API / CLI / script entry: selected - refresh CLI/wrapper and compatible
  manual publisher.
- Config / project setup: selected - env and user-systemd deployment.
- File IO / path safety / overwrite: selected - three canonical artifacts,
  temp/replace/fsync/rollback, immutable packages, receipts.
- Schema / columns / units / field names: selected - registry/index schemas and
  fixed v1 receipt.
- Auth / permissions / secrets: selected - DB-free env, modes, containment,
  redaction.
- Concurrency / shared state / ordering: selected - all three provider writers,
  destination lock + preimage CAS, old/new readers, no multi-lock deadlock,
  provider order and timer state.
- Resource limits / large input / discovery: selected - exact 13-model live
  inventory; 1 MiB receipt, 256 collection, 512-char string, 64 residue,
  32-history, 64 GiB/250k-entry/depth-32 workspace, 4,096-orphan and two-hour
  bounds.
- Legacy compatibility / examples: selected - unchanged manual arguments/output
  and scheduler consumer behavior.
- Error handling / rollback / partial outputs: selected - phase outcomes,
  immutable orphan classification, certain cleanup, unit/job restoration.
- Release / packaging / dependency compatibility: selected - Linux systemd,
  repo venv, no expected dependency addition.
- Documentation / migration notes: selected - install, monitor, manual run,
  failure, success steady state, rollback.
- Geospatial / CRS / basin geometry: not selected - existing Basins validation
  is unchanged; exact inventory identities are compatibility evidence.
- Hydro-met time series / forcing windows: selected - readiness identity and new
  forcing/run/state leaves.
- SHUD numerical runtime / conservation / NaN: not selected - no numerical
  contract change; only job/artifact identity is evaluated.
- PostGIS / TimescaleDB domain behavior: not selected - DB access is prohibited.
- Slurm production lifecycle / mock-vs-real parity: selected - actual stage jobs,
  terminal accounting, queue cleanup.
- External hydro-met providers / snapshot reproducibility: selected - GFS/IFS
  source-cycle/checksum identity.
- Run manifest / QC provenance: selected - pass/candidate/run/stage/artifact
  chain.
- Published NHMS artifacts / display identity: selected - shared-NFS identity
  and node-27 reader ACL, not UI rendering.

## Invariant Matrix

Governing invariant: no expiring scheduler provider is renewed unless its
authoritative bounded content and every reference validate, and no registry
writer replaces canonical bytes outside the one shared transaction lock.

Source-of-truth identity/contract:

- `/volume/nwm/Basins` ->
  `/ghdc/data/nwm/object-store/scheduler/registry/manifest-last.json` with 13
  model/package identities.
- Validated canonical catalog/object entries ->
  `scheduler/canonical-readiness/index-last.json`.
- Validated checkpoint objects -> `scheduler/state-index/index-last.json`.

Surfaces:

- Producers: `publish_all_basin_scheduler_registry`,
  `publish_scheduler_registry_manifest`, `publish_canonical_readiness_index`,
  `FileStateSnapshotIndexRepository._load_index_snapshot`,
  `publish_state_snapshot_index`.
- Validators/preflight: runner path/env/receipt checks,
  `_validate_registry_manifest`, `_validate_readiness_index`,
  `_validate_state_snapshot_index` with age-only producer bypass.
- Storage/cache/query: three exact NFS paths above; per-destination lock and
  expected preimage; private run workspace, primary receipt history/latest and
  separately preflighted/reserved local emergency receipt.
- Public entrypoints: manual publisher CLI, refresh CLI/wrapper,
  `nhms-scheduler-file-provider-refresh.service/.timer`,
  `ProductionScheduler.from_env()`.
- Downstream: unchanged scheduler file consumers, Slurm gateway/stage jobs,
  node-27 NFS reader.
- Failure/rollback/stale state: `provider_preimage_changed`, pre-commit failure,
  replace uncertainty, restored old bytes, primary/emergency receipt failure,
  contention, provider invalidity, capped/truncated package orphans, temp
  residue, unit/job restore.
- Evidence: v1 refresh receipt with three before/after digests; scheduler pass
  JSON; `squeue/sacct`; pass/candidate/run/stage-job map; three leaf identities;
  node-27 ACL/access output.

Regression rows:

- Valid 13-model inventory + three valid-except-age provider inputs -> fully
  revalidated atomic outputs and published receipt.
- Any provider writer overlap -> destination lock + expected-preimage CAS; new
  authoritative entries are never overwritten and no multi-lock deadlock.
- Pre-commit invalid/path/limit/provider failure -> complete old stat tuple;
  capped immutable orphan evidence only.
- Replace/fsync/post-read/receipt failure -> phase-correct restored/indeterminate
  outcome; reader sees complete old/new; primary failure writes reserved fsynced
  emergency record or becomes replace-uncertain.
- Workspace >64 GiB/250k/depth32 or orphan candidates >4,096 -> fail before
  canonical commit; evidence contains first 256, total and truncation flag.
- Invalid readiness/state reference -> no renewal, no DB/timestamp bypass,
  scheduler stays fail closed.
- Unchanged manual CLI/consumer -> existing successful output and fail-closed
  tests remain compatible.
- Refreshed providers -> one pass/run across actual stage job(s), terminal Slurm
  evidence, three new leaves, node-27 access, scheduler/job restoration and
  successful refresh-timer steady state.

## Boundary-Surface Checklist

- Shared helpers: scheduler file-provider writers/validators, safe filesystem,
  Basins full publisher.
- Read: DB-free env, Basins/packages, three provider files and referenced
  catalogs/objects.
- Write/overwrite: three provider files, immutable packages, destination lock,
  private workspace/receipt/history.
- Stage/publish/rollback: atomic temp/fsync/replace, old/new reader, phase
  rollback/indeterminate, capped orphans/residue.
- Evidence boundary: digest/generation/model/entry identity and pass/run/job/
  artifact chain.
- Stale/idempotency: daily replay, valid-except-age renewal, concurrent
  authoritative update/preimage change, repeated success, failed retry and
  emergency-receipt reconstruction.
- Unchanged: manual publisher interface, consumer, node-27 ingest/display, #856.

## Risks / Trade-offs

- [Full Basins validation cost] -> daily two-hour bound, immutable package reuse,
  timer jitter and bounded receipts.
- [Lifecycle publication race] -> shared destination lock under every writer.
- [Post-commit receipt failure] -> explicit non-zero indeterminate outcome and
  canonical old/new validator, never false rollback claims.
- [Registry reveals next gate] -> same run revalidates all expiring providers
  before scheduler.
- [Long real job] -> one bounded pass, explicit stage-job identity/accounting,
  no forced cancellation without recorded failure.

## Migration Plan

1. Deploy frozen SHA to node-22 by ff-only pull; record provider hashes, unit
   states, DB-free env proof and queue.
2. Install byte-identical refresh units stopped; route all registry writers
   through the shared lock.
3. Run dry-run then one manual refresh; validate v1 receipt, 13-model registry,
   renewed readiness/state and identical node-22/node-27 NFS views.
4. Run one bounded scheduler pass through actual stage job(s), terminal
   accounting, three new leaves and node-27 ACL verification.
5. On success enable/start refresh timer and restore scheduler state; on failure
   roll refresh units back. Always prove no issue-owned job remains.

## Open Questions

None. Existing validated index entries and their referenced objects are the
renewal authority; inability to revalidate blocks instead of permitting empty,
DB-derived, or timestamp-only output.
