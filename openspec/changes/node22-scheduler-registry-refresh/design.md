## Context

Issue #1076 was discovered while PR #1075 verified real writer ACL
inheritance. Node-22 pass `scheduler_2026071403_b44ab3b785f4` proved its runtime
DB-free, but the 13-model registry had not been republished since 2026-06-30
and failed the intentional 168-hour limit. Canonical readiness is also expired;
the state index has the same expiring-file lifecycle. State renewal uses its
bounded entries and referenced checkpoints. Readiness is instead rebuilt from
the newest private GFS/IFS canonical catalogs and the same prospective registry
generation, so legacy cached readiness identities are never re-signed.

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

- Node-22 historical local PostgreSQL `:55433` remains archived/stopped and
  do-not-connect; no DB-backed forcing/model/readiness fallback, TTL extension,
  timestamp-only edit, or model lifecycle change.
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
Package versions use a source identity planned by the Basins package publisher
itself: every required, optional runtime, CALIB, and forcing source that affects
the immutable package/forcing checksum semantics participates. Logical relative
identity participates, but machine-specific `source_path`, `resolved_source_path`,
`input_dir`, object URI, and repair-run workspace prefixes do not. The publisher
recomputes the expected identity before writing, so source mutation after
version planning fails closed instead of publishing conflicting bytes under an
old version.

The registry has one additional compute-plane binding: the shared-NFS
canonical manifest read by the scheduler and the private compute-visible
manifest read by Slurm workers are one generation, not independent providers.
Refresh publishes the worker mirror first and the shared canonical manifest
last from the same prospective rows and `generated_at`, requires identical
content SHA/model count, and restores the mirror by committed-preimage CAS if
the shared commit fails. A stage manifest compares the two complete byte
generations and fails closed with `SCHEDULER_REGISTRY_MIRROR_MISMATCH`; it never
submits work against a stale mirror.

### D3. Publication failure semantics follow the commit phase

Before replace, the complete old stat/digest tuple is invariant. Atomic replace
legitimately changes inode. A certain invalid replacement may atomically
restore captured validated previous bytes (`restored_previous`) without
claiming inode/mtime preservation. Replace uncertainty and receipt-only failure
are non-zero indeterminate outcomes; the consumer still sees complete old/new
bytes. Cleanup touches only certain current-run temp identities.
All four publication lanes form one rollback transaction: worker registry
mirror, shared registry, readiness, then state. A later-lane failure captures
the exact postimage returned by each successful atomic replace as its commit
token and restores owned lanes in reverse order by committed-preimage CAS. A
typed expected-preimage conflict never supplies a token and never enrolls the
concurrent authoritative generation in rollback; earlier owned lanes restore
and the original `provider_preimage_changed` remains the receipt reason. An
unknown write-after exception without a matching token is not guessed as owned
and reports `replace_uncertain`. Only complete restoration clears committed
evidence and reports `restored_previous`; any capture, CAS, or restore conflict
reports `replace_uncertain`, which receipt failure handling must not relabel as
`published_receipt_failed`.

### D4. Readiness is derived from current catalogs; state renews indexed truth

Before any canonical provider commit, the runner builds the prospective
registry generation, finds the newest bounded no-follow GFS and IFS catalog
under the private object root, and validates every catalog row, lineage
identity, object checksum, forecast-hour set, and canonical completeness. It
then creates exactly one readiness entry per source and prospective registry
model, with products externalized behind `catalog_uri + catalog_sha256 +
catalog_row_count`. The registry/readiness model sets must match exactly. An
invalid newest catalog fails closed without falling back to an older cycle.
Consumer identity mismatch recomputes against the still-bound catalog instead
of trusting cached entry identity. State entries remain loaded by
`FileStateSnapshotIndexRepository._load_index_snapshot` with freshness disabled
and object verification enabled before `publish_state_snapshot_index`. Missing
or semantically invalid input fails; no legacy-readiness renewal, empty
synthesis, huge product copy, or DB fallback exists.

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
The provider evidence list additionally binds `registry_worker_mirror` whenever
the required Slurm override is configured; success/current validation requires
its SHA and model count to equal the canonical registry evidence.

### D6. Live proof follows actual stage topology

One scheduler pass/candidate/run may bind multiple Slurm stage jobs. Success
requires terminal accounting and genuinely new forcing/runs/states leaves;
reused forcing is not evidence. Node-27 observes the same NFS identities and
ACLs. On failure the new refresh units roll back; on success its timer remains
enabled/active. Scheduler state and issue-owned jobs always restore.
The refresh unit runs only while the scheduler service is inactive and orders
before a concurrently requested scheduler start, so a pre-existing stage job
cannot observe the mirror transition.

### D7. Registry refresh is classified against the previous canonical registry (#1080 follow-up)

Adding a basin is not itself an input to another basin's `package_checksum`:
per-model package versions are derived from the model's own validated content
and source identity (`package_version_for_model`). The 2026-07-15 failure was
not a checksum-derivation drift; it was that the refresh publishes a full new
registry with no compatibility gate against the previous active rows, so a
mixed refresh silently activated 13 new package checksums for pre-existing
models and blocked scheduler continuity downstream.

The registry publisher gains a precommit compatibility gate:

1. **Load previous canonical** — before invoking
   `publish_scheduler_registry_manifest`, the refresh runner reads the
   currently canonical `manifest-last.json` under bounded no-follow limits
   (missing file is legitimate first-time publication, tracked separately).
2. **Diff by `model_id`** — the union of previous and prospective
   `model_id`s is classified into four disjoint primary buckets, then a
   decision layer produces `refused` and `declared_cutovers`:
   - `added` covers prospective `model_id`s absent from the previous
     canonical registry.
   - `unchanged` covers `model_id`s present in both with byte-for-byte
     equality on `model_package_uri`, `manifest_uri`, `package_checksum`,
     `source_inventory_checksum`, and the documented model identity fields;
     any deviation escalates.
   - `package_changed` covers `model_id`s present in both whose
     `package_checksum` differs.
   - `removed` covers previously canonical `model_id`s absent from the
     prospective registry (a previous-only row, not a prospective row).
     Deliberate decommission is out of scope for #1080 and is refused; it
     must go through a separate declared workflow.
   `declared_cutovers` is a subset of `package_changed` whose cutover
   declaration entry validates. `refused` records every `removed` entry,
   every `package_changed` entry not covered by `declared_cutovers`, and
   every entry rejected by declaration invalidity.
3. **Cutover declaration contract** — a `package_changed` row is admitted only
   when a `nhms.scheduler.registry_package_cutover.v1` declaration (path via
   env `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`, absent = no declaration)
   names the same `model_id` with matching `old_checksum` (= previous
   canonical), matching `new_checksum` (= prospective), the same `generation`
   as the prospective registry, an `effective_cycle_utc` aligned to 00:00 or
   12:00 UTC and within the bounded window (not more than 24h in the past nor
   more than 168h in the future), and a supported `transition_mode`
   (`replace`). Duplicates, unmatched entries, and schema-invalid declarations
   fail closed.
4. **Refusal semantics** — undeclared `package_changed`, any `removed`, or an
   invalid declaration exits non-zero with a closed reason token
   (`registry_cutover_undeclared`, `registry_cutover_removal_refused`,
   `registry_cutover_declaration_invalid`) before any canonical provider
   replace. The existing rollback transaction (D3) is not entered because no
   commit token exists; previous canonical bytes, inode, mtime, and identity
   remain unchanged.
5. **Receipt classification** — the v1 receipt gains a top-level
   `registry_classification` object bound to the previous and new canonical
   registry SHA-256, with bounded (256-cap + `total` + `truncated`) lists for
   each class exposing only `model_id`, `old_checksum`, `new_checksum`, and
   accepted declaration identity. No absolute paths, object URIs, or
   credentials are emitted.

The gate lives inside the existing precommit hook
(`_registry_precommit_gate`) so the classification, refusal decision, and
receipt payload are produced under the same destination lock and preimage
snapshot as the canonical registry write. Because refusal happens before
replace, the phase-explicit outcome semantics (D3) are unchanged — refusals
are pre-commit `failed` outcomes, never `replace_uncertain` or
`restored_previous`.

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
- Resource limits / large input / discovery: selected - exact current live
  inventory (19 models on 2026-07-15 after removing duplicate
  `HHe-MAIN-02`); 1 MiB receipt, 256 collection,
  512-char string, 64 residue,
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

Governing invariant: readiness is rebuilt only from the newest fully validated
private catalogs and the same prospective registry model set; every other
expiring provider is renewed only from validated bounded truth, and no registry
writer replaces canonical bytes outside the one shared transaction lock.

Source-of-truth identity/contract:

- `/volume/nwm/Basins` ->
  `/ghdc/data/nwm/object-store/scheduler/registry/manifest-last.json` with the
  exact current model/package identities (19 on 2026-07-15 after removing
  duplicate `HHe-MAIN-02`).
- Newest validated private GFS/IFS catalog/object entries + the same prospective
  registry model set -> catalog-bound
  `scheduler/canonical-readiness/index-last.json` entries.
- Validated checkpoint objects -> `scheduler/state-index/index-last.json`.

Surfaces:

- Producers: `publish_all_basin_scheduler_registry`,
  `publish_scheduler_registry_manifest`, `publish_canonical_readiness_index`,
  `FileStateSnapshotIndexRepository._load_index_snapshot`,
  `publish_state_snapshot_index`.
- Validators/preflight: runner path/env/receipt checks,
  `_validate_registry_manifest`, `_validate_readiness_index`,
  current-catalog derivation/model-set binding, and
  `_validate_state_snapshot_index` with state-only age bypass.
- Storage/cache/query: three exact NFS paths above; per-destination lock and
  expected preimage; private run workspace, primary receipt history/latest and
  separately preflighted/reserved local emergency receipt.
  The live topology has two explicit roots: shared NFS holds only the three
  canonical provider files, while node-22 private object storage holds registry
  packages and resolves every `s3://nhms` catalog/checkpoint reference consumed by compute.
  Neither root substitutes for the other and object verification stays enabled.
  The private root also holds the explicit Slurm registry mirror; it is
  generation-identical to the shared registry and is receipt/runtime gated.
- Public entrypoints: manual publisher CLI, refresh CLI/wrapper,
  `nhms-scheduler-file-provider-refresh.service/.timer`,
  `ProductionScheduler.from_env()`.
- Downstream: unchanged scheduler file consumers, Slurm gateway/stage jobs,
  node-27 NFS reader.
- Failure/rollback/stale state: `provider_preimage_changed`, pre-commit failure,
  replace uncertainty, restored old bytes, primary/emergency receipt failure,
  contention, provider invalidity, capped/truncated package orphans, temp
  residue, worker-mirror mismatch/rollback, unit/job restore.
- Evidence: v1 refresh receipt with three canonical before/after digests plus
  worker registry mirror binding; scheduler pass
  JSON; `squeue/sacct`; pass/candidate/run/stage-job map; three leaf identities;
  node-27 ACL/access output.

Regression rows:

- Prospective registry equals previous canonical registry plus new basins with
  every previously canonical model byte-identical -> `added` count equals new
  basins, `unchanged` count equals previous canonical model count,
  `package_changed = removed = refused = declared_cutovers = 0`, and canonical
  replacement proceeds under the shared lock and existing rollback transaction.
- Any previously canonical `model_id` whose prospective row has a different
  `package_checksum` with no matching cutover declaration -> non-zero
  `registry_cutover_undeclared`, previous canonical bytes/inode/mtime/identity
  unchanged, and receipt lists the refused model with previous and proposed
  checksums only.
- A previously canonical `model_id` missing from the prospective registry ->
  non-zero `registry_cutover_removal_refused`, previous canonical bytes
  unchanged, and receipt lists the removed model.
- Valid `nhms.scheduler.registry_package_cutover.v1` declaration whose entry
  matches the prospective `model_id + old_checksum + new_checksum + generation`
  with UTC-cycle-aligned `effective_cycle_utc` and a supported
  `transition_mode` -> classification records the row under
  `declared_cutovers`, canonical replacement proceeds, and receipt binds the
  declaration identity.
- Declaration whose schema is invalid, whose `generation` differs, whose
  old/new checksum mismatches the actual prospective/previous, whose
  `effective_cycle_utc` is non-cycle-aligned or outside the bounded window,
  which duplicates a `model_id`, which references a `model_id` absent from
  the prospective registry, or which is symlinked/non-regular/over-size ->
  non-zero `registry_cutover_declaration_invalid`, previous canonical bytes
  unchanged, and receipt records the specific invalidity token.
- Valid N-model inventory + newest valid GFS/IFS catalogs + valid-except-age
  state input -> 2N catalog-bound readiness entries (19 models/38 entries on
  2026-07-15 after removing duplicate `HHe-MAIN-02`), fully revalidated atomic
  outputs and published receipt; new registry packages are
  private-only, the canonical manifest is shared, and deleting a private
  package makes the unchanged scheduler consumer fail closed.
- Any provider writer overlap -> destination lock + expected-preimage CAS; new
  authoritative entries are never overwritten and no multi-lock deadlock.
- Pre-commit invalid/path/limit/provider failure -> complete old stat tuple;
  capped immutable orphan evidence only.
- Replace/fsync/post-read/receipt failure -> phase-correct restored/indeterminate
  outcome; reader sees complete old/new; primary failure writes reserved fsynced
  emergency record or becomes replace-uncertain. Reservation file+parent fsync
  precedes provider side effects; zero/short write and file/parent fsync faults
  leak neither descriptor nor reserved slot.
- Readiness/state failure after registry publication -> reverse-order rollback
  of every changed lane; all old bytes plus `restored_previous`, or explicit
  `replace_uncertain` on any committed-preimage conflict.
- Workspace >64 GiB/250k/depth32 or orphan candidates >4,096 -> fail before
  canonical commit; evidence contains first 256, total and truncation flag.
- Invalid latest readiness catalog/state reference -> no publication, older-
  catalog fallback, legacy-readiness renewal, DB/timestamp bypass; scheduler
  stays fail closed.
- Private state checkpoint copyback -> shared checkpoint is durable and
  checksum-valid before the merged shared index becomes visible; copy failure
  preserves the old index and concurrent refresh loses no entry.
- Installer repeated enable/transitional service state -> strict current
  receipt validation precedes mutation, invalid evidence changes no unit state,
  and each failure restores its own entry state.
- Unchanged manual CLI/consumer -> existing successful output and fail-closed
  tests remain compatible.
- Refreshed providers -> one pass/run across actual stage job(s), terminal Slurm
  evidence, three new leaves, node-27 access, scheduler/job restoration and
  successful refresh-timer steady state.

## Boundary-Surface Checklist

- Shared helpers: scheduler file-provider writers/validators, safe filesystem,
  Basins full publisher.
- Read: DB-free env, Basins/packages, three shared provider files and private
  compute-visible referenced catalogs/objects.
- Write/overwrite: three provider files, immutable packages, destination lock,
  private workspace/receipt/history.
- Stage/publish/rollback: atomic temp/fsync/replace, old/new reader, phase
  rollback/indeterminate, capped orphans/residue.
- Evidence boundary: digest/generation/model/entry identity and pass/run/job/
  artifact chain.
- Stale/idempotency: daily catalog-bound readiness derivation, state age-only
  renewal, concurrent authoritative update/preimage change, repeated success,
  failed retry and emergency-receipt reconstruction.
- Registry-classification boundary (#1080): previous canonical
  `manifest-last.json` no-follow read, cutover declaration schema
  (`nhms.scheduler.registry_package_cutover.v1`) loaded from
  `NHMS_REGISTRY_CUTOVER_DECLARATION_PATH`, refusal reason tokens
  (`registry_cutover_undeclared`,
  `registry_cutover_removal_refused`,
  `registry_cutover_declaration_invalid`), bounded per-class evidence lists in
  `registry_classification`.
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
3. Run dry-run then one manual refresh; validate v1 receipt, registry matching
   the exact current live inventory (19 models on 2026-07-15 after removing
   duplicate `HHe-MAIN-02`), renewed
   readiness/state and identical node-22/node-27 NFS views.
4. Run one bounded scheduler pass through actual stage job(s), terminal
   accounting, three new leaves and node-27 ACL verification.
5. On success enable/start refresh timer and restore scheduler state; on failure
   roll refresh units back. Always prove no issue-owned job remains.

## Open Questions

None. The newest private GFS/IFS catalogs plus the same prospective registry
model identities are readiness authority; state entries and checkpoints remain
state renewal authority. Inability to validate either blocks instead of
permitting an older catalog, legacy readiness renewal, empty, DB-derived, or
timestamp-only output.
