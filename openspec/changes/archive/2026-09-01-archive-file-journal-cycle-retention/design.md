## Context

#1734 established that hot single-row reads need only one cycle and measured a 316x smaller read slice, so this change is not a query-performance fix. The remaining concern is the unbounded disk/inode footprint and the bounded whole-tree fallback surfaces. A 2026-08-31 read-only node-22 probe found the live root at `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`, 5,574 `latest` files (116 MiB), 268 `journal` files (22 MiB), no `pipeline-events` files, and 48 MiB of out-of-scope `pipeline-jobs`; the volume had about 18 TiB free. The same probe established production lookback/lag values of 96h/16h and availability of GNU tar, zstd, and sha256sum.

`latest/` and `journal/` are scheduler authority, not disposable cache. They can contain unbound reservations, accepted masters with incomplete projections, and #1748 released identity-blocked rows whose reconcile-inventory anchor has already been removed. The journal also has source-specific casing (`gfs`, `IFS`), bounded continuation segments, cycle-scoped locks, and a canonical replay merge. `pipeline-events/` is currently empty but is a sibling cycle log consumed by that merge.

Issue #1775 narrowed history existence to usable state-index entries at or before a candidate cutoff. This change chooses its safe option: it never edits the state index. Any future state-index retention must preserve at least one usable history anchor early enough for each existing `(model_id, source_id)`; treating pruned history as cold-start evidence is not accepted here.

Fixture level: **expanded**. Repair intensity: **high**, because enforcement deletes scheduler authority and must preserve recovery and rollback invariants.

## Goals / Non-Goals

**Goals:**

- Bound the hot `latest/`, `journal/`, and `pipeline-events/` cycle set with a 90-day default while retaining every cycle that can still affect scheduler recovery.
- Produce a deterministic cold archive with a manifest and SHA-256 identities, then remove hot members only after archive verification.
- Make dry-run the default, enforcement explicitly opt-in, every decision receipted, and recovery a documented stop/verify/extract/query drill.
- Run independently of scheduler-wide idle state while serializing each candidate against its writers with the existing cycle flock.

**Non-Goals:**

- Pruning or rewriting `pipeline-jobs/`, `reconcile-inventory/`, `.locks/`, or `state-index/index-last.json`.
- Serving reads directly from cold archives, automatic restore, changing journal schemas/merge semantics, or narrowing remaining whole-tree callers.
- Enabling production enforcement or deleting a live production cycle in this PR. Node-22 evidence uses the real tree for dry-run and an isolated copy for enforce/restore.
- Addressing non-journal scheduler I/O, node-27 Timescale retention, or artifact retention owned by other services.

## Decisions

### D1. Retain 90 days hot, bounded by scheduler safety inputs and one discovery budget

Candidate age is derived from the cycle token `%Y%m%d%H`, never file mtime. The default is 90 days. Candidate `(source_id, cycle_time)` pairs are the union discovered from exactly three hot roots: `latest/`, `journal/`, and `pipeline-events/`. Discovery uses the journal owner's existing containment and shape validation, a single aggregate `MAX_FILE_JOURNAL_DISCOVERED_FILES` budget across all three roots, and `MAX_FILE_JOURNAL_SCAN_DEPTH`; it never walks `pipeline-jobs/`, archive output, or the state index. Exhausting either bound, encountering an unrecognized cycle-shaped entry, or failing any root walk blocks the entire enforce invocation rather than returning a partial candidate set. Tests squeeze the injectable discovery bound to prove both a boundary-sized valid set is complete and one entry over budget removes nothing.

Enforcement requires all configured values to be valid and requires:

`retention_days * 24 > lookback_hours + cycle_lag_hours + 2 * largest_allowed_cycle_gap_hours`.

The production probe's 96h lookback and 16h lag are far below this bound. Invalid/missing settings block enforcement rather than falling back to permissive defaults. Dry-run records the blocker and candidate plan. This keeps the default deliberately conservative; the purpose is a finite tree, not aggressive capacity recovery.

### D2. Archive the whole `(source_id, cycle_time)` authority slice

One candidate contains all recognized regular files in:

- `latest/<source>/<cycle>/*.json`
- `journal/<source>/<cycle>.jsonl` and valid bounded continuation segments
- `pipeline-events/<source>/<cycle>.jsonl` and valid bounded continuation segments

It becomes `<archive-root>/<source>/<cycle>/journal-cycle.tar.zst` plus `manifest.json`. The manifest records schema version, source/cycle, creation time, archive SHA-256, every member's relative path/size/SHA-256, row counts where applicable, mode (`dry-run` or `enforce`), and the frontier used. Paths are deterministic and relative to the trusted journal root. The archive is written to a temporary sibling, verified by reading its member listing and digest, and atomically published before any hot member is unlinked. A pre-existing matching archive makes the operation idempotent; a conflicting archive blocks the cycle. No reader is added for tar files.

A plain delete was rejected because operator recovery is a requirement. Per-file archives were rejected because partial restoration could split the last-write-wins authority. Adding a Python compression dependency was rejected because node-22 already supplies GNU tar and zstd; subprocess execution is bounded and checked.

### D3. Reuse canonical journal ownership and live-row semantics

The retention service imports a narrow public retention inspection/lock seam from the file-journal owner rather than copying private predicates into the script. Under a non-blocking form of the existing `(source, cycle)` flock it replays the exact cycle with the same source normalization, continuation handling, latest/journal/direct merge, and validation. A cycle is retained if any canonical pipeline job:

- blocks rollback quiescence via the existing `_job_blocks_rollback_quiescence` semantics, including unbound reservations and incomplete accepted-master projections; or
- matches the existing released identity-blocked admission predicate, even though that terminal row no longer has a reconcile-inventory anchor.

The seam returns a typed inspection result and recognized member inventory; it does not expose a second replay implementation. Lock contention records `in-flight` and skips immediately so a long-running scheduler pass cannot starve behind a second scheduler-wide idle gate. Any unreadable, malformed, symlinked, non-regular, unrecognized, gapped, over-segment, disappearing, or identity-conflicting member fails closed and leaves the whole cycle untouched.

### D4. Require a fresh pipeline frontier outside scheduler passes

The command reads the newest scheduler evidence using `retention_frontier.read_latest_pass_frontier`. Missing, stale, malformed, or retention-disabled evidence blocks enforcement. Every candidate at or after a non-null `active_lower_bound` is retained as `pipeline_frontier_exempt`. The receipt records the evidence file, timestamp, bound, and reason. No bypass flag is provided. A null bound from an otherwise valid receipt is accepted only as the upstream frontier contract's explicit `receipt:none` state; D1 still protects the configured scheduler window.

### D5. Configuration and deployment are fail-safe

New environment settings are:

- `NHMS_SCHEDULER_JOURNAL_RETENTION_ENABLED=false`
- `NHMS_SCHEDULER_JOURNAL_RETENTION_DRY_RUN=true`
- `NHMS_SCHEDULER_JOURNAL_RETENTION_DAYS=90`
- `NHMS_SCHEDULER_JOURNAL_ARCHIVE_ROOT=<scheduler workspace>/journal-archive`

The archive root must be absolute, existing or safely creatable below an allowed non-root parent, distinct from and outside the hot journal root, and not a symlink. Enforcement additionally requires `ENABLED=true` and `DRY_RUN=false`; every other combination is non-destructive. The user systemd service uses the pinned active interpreter directly (never `uv run` on node-22 before its maintenance window), runs daily at 04:45 UTC with randomized delay, and ships disabled until operators explicitly install/enable it. Every invocation writes a bounded JSON receipt under `<archive-root>/retention/`, including blockers, candidate/member counts, bytes, skipped reasons, archive identity, and removed paths.

### D6. Recovery is explicit and offline for the selected cycle

The runbook requires operators to stop or otherwise exclude writers for the target cycle, verify the manifest and archive digest, extract to a staging directory, validate every member digest/path, then restore the original relative paths without clobbering existing files. A cycle query must match its pre-archive captured result before normal operation resumes. Recovery never edits state-index or direct records. A failed archive operation leaves the hot slice intact; a failed post-publish unlink leaves a verified archive plus remaining hot files and is safe to rerun.

## Risk Packs

### Selected

- **File IO / path safety / overwrite**: trusted-root containment, no symlinks/non-regular files, bounded discovery, atomic no-clobber publication, scoped removal, verified restore.
- **Concurrency / shared state / ordering**: non-blocking cycle flock covers inspect, archive, verify, and removal; archive publication precedes unlink.
- **Resource limits / large input / discovery**: union exactly `latest/`, `journal/`, and `pipeline-events/` under one aggregate existing journal file/depth budget; fail the invocation on truncation; bound cycle/member/byte counts and subprocess time; test boundary-sized valid and over-budget sets.
- **Legacy compatibility / examples**: preserve `gfs`/`IFS` normalization, continuation segments, direct records, released reservation recovery, and existing query results after restore.
- **Error handling / rollback / partial outputs**: fail closed per cycle; temporary/conflicting archives and partial unlink are explicit receipt states; retry is idempotent.
- **Release / packaging / dependency compatibility**: use node-22's existing GNU tar/zstd tools and pinned active interpreter; unit remains disabled by default.
- **Documentation / migration notes**: install, dry-run, enforce activation, receipt inspection, and restore drill are required.
- **Slurm production lifecycle / mock-vs-real parity**: node-22 live-tree dry-run and isolated enforce/restore receipts validate operational topology without changing jobs.
- **Run manifest / QC provenance**: archive manifest binds cold bytes to exact hot member identities and the frontier receipt used.
- **Public API / CLI / script entry**: the operator script is a destructive entrypoint; defaults, flags, exit codes, receipt output, and systemd invocation are tested even though it is not a product API.
- **Config / project setup**: four retention/archive settings plus scheduler window/frontier inputs are validated fail-closed, documented in the EnvironmentFile template, and exercised through valid, missing, malformed, disabled, dry-run, and enforce cases.

### Not selected
- **Schema / columns / units / field names**: no scheduler row or public evidence schema changes; the private archive receipt/manifest is versioned and tested under provenance.
- **Auth / permissions / secrets**: no network/auth boundary or secret is introduced; owner-mode filesystem operation and redacted receipts are covered by path safety.
- **Geospatial / CRS / basin geometry**: no geospatial data is read or changed.
- **Hydro-met time series / forcing windows**: no forcing objects are removed; scheduler lookback is used only as a deletion guard.
- **SHUD numerical runtime / conservation / NaN**: no model execution or numerical data changes.
- **PostGIS / TimescaleDB domain behavior**: node-22 remains DB-free.
- **External hydro-met providers / snapshot reproducibility**: provider data and snapshots are out of scope.
- **Published NHMS artifacts / display identity**: published artifacts and node-27 display data are untouched.

## Invariant Matrix

**Governing invariant:** Retention may remove a hot cycle member only after the complete recognized cycle authority is locked, proven outside every active/recovery boundary, archived and byte-verified, while all other scheduler authority remains byte-identical.

**Source-of-truth identity/contract:** normalized `(source_id, cycle_time)` plus the canonical file-journal replay, existing live-row predicates, current frontier receipt, and per-member/archive SHA-256 identities.

**Surfaces:**

- **Producers:** file-journal append/latest materialization in `services/orchestrator/file_orchestration_journal.py`; retention creates only cold bundle/manifest/receipt.
- **Validators/preflight:** retention config/root/window/frontier/member validation and canonical cycle inspection seam.
- **Storage/cache/query:** hot `latest/`, `journal/`, `pipeline-events/`; cold archive/manifest; existing cycle caches must be invalidated or never survive mutation.
- **Public routes/entrypoints:** new operator script and systemd oneshot/timer; no HTTP/API change.
- **Frontend/downstream consumers:** scheduler cycle queries, released-reservation operator query, rollback/reconcile; node-27/display unaffected.
- **Failure paths/rollback/stale state:** lock contention, malformed member, unavailable frontier, live row, archive conflict, subprocess failure, partial unlink, restore no-clobber.
- **Evidence/audit/readiness:** dry-run/enforce receipts, manifest/member digests, local tests, node-22 real-tree dry-run, isolated enforce/restore drill.

**Regression rows:**

- Expired terminal-only `gfs` or `IFS` cycle plus a fresh frontier -> dry-run plans all recognized members; enforce creates and verifies one archive, removes only those hot members, and leaves direct/state-index bytes unchanged.
- Reserved/unbound, accepted-master projection-incomplete, released identity-blocked, active-frontier, malformed/symlink/non-regular/gapped, or lock-busy cycle -> stable skipped/blocker receipt and zero hot removals.
- Archive then restore an eligible cycle -> restored member digests and cycle-scoped query results equal the captured pre-archive values.
- Unchanged sibling authority (`pipeline-jobs/`, reconcile inventory, lock files, state index) -> byte identity is preserved across enforce and restore.

## Boundary-Surface Checklist

- **Shared helper roots:** canonical file-journal inspection/lock/predicate owner; frontier reader; safe filesystem helpers.
- **Public entrypoints:** retention script and systemd service/timer.
- **Read surfaces:** three recognized hot cycle surfaces, latest pass frontier, configuration.
- **Write/delete/overwrite surfaces:** archive temp/final paths, manifests/receipts, exact recognized hot members only; no-clobber restoration.
- **Staging/publish/rollback surfaces:** temporary archive -> verified atomic publish -> scoped unlink; staged restore -> digest validation -> no-clobber publish.
- **Producer/consumer evidence boundaries:** manifest SHA/member list bound to archive and frontier receipt; cycle query parity after restore.
- **Stale-state/idempotency boundaries:** pre-existing archive match/conflict, partial unlink rerun, stale frontier rejection, cycle cache invalidation.
- **Unchanged downstream consumers:** scheduler replay/query, released reservation recovery, rollback/reconcile, state history admission.

## Risks / Trade-offs

- **A terminal-looking released reservation is still operator-recoverable** -> retain it through the existing dedicated predicate, not reconcile inventory.
- **Direct state can make a replay look settled after hot history disappears** -> inspect while all hot members exist and retain the whole cycle on any live canonical row; direct records themselves are never deleted.
- **A writer races archive/removal** -> hold the same cross-process cycle flock for the full transaction and skip instead of block when busy.
- **A bundle is published but hot cleanup is partial** -> retain the verified archive, report exact residual paths, and make rerun idempotent; never roll back by deleting the archive.
- **Cold storage doubles disk before unlink** -> one cycle at a time and conservative limits; the live volume has ample space.
- **90 days may be more than operationally needed** -> finite and safe is preferred; later tightening requires fresh measurements and the same invariant, not code changes.
- **A future developer extends retention to state-index** -> explicit non-goal and regression checksum pin preserve #1775's historical-anchor invariant.

## Migration Plan

1. Merge with enforcement disabled and dry-run true.
2. Deploy to node-22 using the checked-in service and active `.venv/bin/python`; do not run `uv sync` before the maintenance window.
3. Run the command against the live root in dry-run mode and retain its receipt; confirm live/active/frontier exemptions and unchanged hot/state-index checksums.
4. Run enforce and restore only against an isolated copy of one known eligible production-shaped cycle; verify archive/member hashes and query parity.
5. A later explicit operations change may set `ENABLED=true`, keep dry-run for an observation interval, then set `DRY_RUN=false` and enable the timer. Rollback disables the timer and restores any needed cycle from its verified archive.

## Open Questions

None for implementation. Live production enforcement timing is deliberately an operator decision outside this PR; the PR's node-22 oracle is non-destructive on the real journal.
