## Context

Issue #874 started with a missing persisted forcing package for
`gfs_2026062912/basins_hetianhe_shud`. Online mitigation restored artifacts and
proved individual Slurm jobs can complete, but later evidence showed four
code-level scheduler defects that still block normal node-22 operation:

- retry/submitted manifests can lose DB-free runtime flags, causing forecast
  retries to require `DATABASE_URL`;
- Slurm reconcile can mark a completed array task as
  `SLURM_RECONCILE_UNVERIFIED` because generic job names do not prove identity;
- stale `hydro_run.status=created` can cause an already completed cycle/basin to
  be selected again;
- a live scheduler pass can spin while holding the scheduler lock without
  submitting work or advancing journal evidence.

The active project profile is NHMS. This is an expanded, high-risk fixture
because it touches Slurm/SHUD runtime, DB-free file journal state, retry,
concurrency, terminal-state idempotency, and production service configuration.

## Goals / Non-Goals

**Goals:**

- Restore node-22 automatic scheduler business operation from the latest code.
- Keep node-22 compute-only and DB-free; do not connect to local `:55433`.
- Make retry/reconcile/state transitions identity-safe under concurrent submit.
- Convert missing forcing package and missing copyback source into stable
  recoverable blockers before downstream forecast submission.
- Add local tests plus node-22 live receipts proving bounded concurrent
  operation.

**Non-Goals:**

- No node-27 display/API changes.
- No schema migration or new database enum.
- No change to SHUD solver numerical behavior.
- No broad rewrite of the scheduler facade or file-journal repository.
- No resurrection of the archived node-22 PostgreSQL listener.

## Decisions

1. **Carry DB-free flags in the submission manifest, not only process env.**
   The sbatch templates export runtime mode from manifest fields. Retry paths
   must therefore set the canonical DB-free fields on the manifest, and may also
   pass `NHMS_SHUD_DB_FREE=true` through allowed `slurm_env` as belt-and-suspenders
   runtime evidence.

2. **Use durable manifest/task identity for Slurm reconcile.** Reconcile should
   consider submitted manifest identity, array task id, run id, stage, model id,
   and stdout/runtime evidence before accepting a terminal Slurm status. Generic
   Slurm job names are insufficient for array task proof.

3. **Terminal candidate state is derived from the strongest completed evidence.**
   A completed `forecast_cycle` plus terminal stage/copyback evidence should
   prevent resubmission even if a stale hydro row still says `created`. If the
   implementation can safely repair the stale hydro status, it may do so, but the
   scheduler selection guard must not depend on that repair.

4. **Live passes need explicit progress bounds.** The pass should either submit,
   skip/block with evidence, or stop with a stable bounded-progress blocker before
   lock lease harm. This is a guard for production safety, not a replacement for
   fixing root state bugs.

5. **Concurrency proof must use a real node-22 service run with work.** Local
   tests are necessary for contracts, but business readiness requires node-22
   receipts with `NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND` greater than `1`, at
   least two eligible candidates or array tasks, scheduler service restarted on
   merged/latest code, Slurm/file-journal progress, and no stale lock. A no-work
   pass can prove the daemon is safe, but it is a blocked/deferred business
   receipt, not issue completion.

## Risks / Trade-offs

- Broad scheduler state interactions can hide sibling-surface regressions ->
  mitigate with invariant/state cross-review and focused retry/reconcile/state
  tests.
- Bounded progress guard could mask useful work if too aggressive -> default to
  conservative env-configured limits and evidence explaining the blocker.
- Reconcile identity may be incomplete for old persisted manifests -> preserve
  backward compatibility by accepting legacy proof only when identity can still
  be constructed from durable journal/stdout evidence, otherwise leave
  `SLURM_RECONCILE_UNVERIFIED`.
- Live node-22 validation depends on current Slurm/GFS availability -> record
  exact dependency state and use existing queued candidates where available.

## Risk Fixture

Fixture level: expanded
Project profile: NHMS
Repair intensity: high

Change surface:
- `services/orchestrator/retry.py`
- `services/orchestrator/reconcile.py`
- `services/orchestrator/slurm_gateway.py`
- `services/orchestrator/scheduler_runtime.py`
- `services/orchestrator/scheduler_state_*`
- `infra/sbatch/run_shud_forecast_array.sbatch`
- focused scheduler/retry/reconcile tests and node-22 systemd runtime config

Must preserve:
- Non-DB-free legacy scheduler mode remains available where tests intentionally
  cover it.
- `services.orchestrator.scheduler` remains a compatibility facade.
- File journal writes stay append-only, bounded, redacted, and current-run bound.
- Missing identity proof must not be silently marked as success.

Must add/change:
- DB-free retry manifest propagation.
- Missing forcing/copyback source pre-resume blocker with stable code.
- Slurm reconcile identity proof for array tasks.
- Terminal skip based on completed cycle/stage/copyback evidence despite stale
  hydro status.
- Bounded live pass progress guard.
- Node-22 concurrent service receipt.

Risk packs considered (core):
- Public API / CLI / script entry: selected - scheduler CLI/systemd entrypoint
  semantics change.
- Config / project setup: selected - node-22 service env and concurrent submit
  bounds are part of readiness.
- File IO / path safety / overwrite: selected - file journal/object-store
  artifact existence and copyback roots are read before mutation.
- Schema / columns / units / field names: selected - manifest/journal fields
  carry identity and DB-free flags; no DB schema change.
- Auth / permissions / secrets: not selected - no auth surface, but evidence must
  remain redacted.
- Concurrency / shared state / ordering: selected - scheduler lock, retry, Slurm
  array tasks, and concurrent submit are central.
- Resource limits / large input / discovery: selected - pass progress must be
  bounded under production journal/object roots.
- Legacy compatibility / examples: selected - old journal/manifests must not be
  broken.
- Error handling / rollback / partial outputs: selected - missing artifacts and
  unverified reconcile outcomes need stable blockers.
- Release / packaging / dependency compatibility: not selected - no new
  dependency or packaging change.
- Documentation / migration notes: selected - runbook/issue evidence must record
  safe online mitigation and node-22 restart proof.

Domain packs:
- Slurm production lifecycle / mock-vs-real parity: selected.
- SHUD numerical runtime / conservation / NaN: not selected - solver output math
  is unchanged.
- Hydro-met time series / forcing windows: selected - forcing package identity
  gates forecast retry.
- Run manifest / QC provenance: selected.
- Published NHMS artifacts / display identity: selected only for copyback/source
  existence; display UI remains out of scope.
- Geospatial / CRS / basin geometry: not selected.
- PostGIS / TimescaleDB domain behavior: not selected; node-22 stays DB-free.
- External hydro-met providers / snapshot reproducibility: not selected beyond
  existing cycle identity.

Invariant Matrix:
- Governing invariant: A node-22 scheduler pass may submit or reconcile work
  only when DB-free runtime identity, source/cycle/model/task identity, and
  required upstream artifact evidence all match the candidate being advanced.
- Source-of-truth identity/contract: source id, cycle time, model id, run id,
  stage, array task id, submitted manifest path/content, forcing package URI,
  file-journal event identity, Slurm job id and sacct/stdout evidence.
- Producers: retry manifest builder, chain stage submission, Slurm gateway,
  SHUD runtime stdout, file journal.
- Validators/preflight: scheduler candidate/state decision, artifact existence
  checks, reconcile identity proof, DB-free preflight.
- Storage/cache/query: file orchestration journal, latest snapshots,
  object-store roots, NFS copyback roots.
- Public routes/entrypoints: scheduler CLI/systemd service/timer; manual retry
  API remains compatible.
- Frontend/downstream consumers: node-27 display reads copyback artifacts;
  unchanged.
- Failure paths/rollback/stale state: missing forcing/copyback source, stale
  hydro status, unverified Slurm identity, progress timeout, stale lock.
- Evidence/audit/readiness: scheduler pass evidence, pipeline events, Slurm
  receipts, node-22 live receipt, issue/PR comments.
- Regression rows:
  - DB-free automatic retry for forecast candidate -> submitted manifest exports
    file repository flags and runtime runs without `DATABASE_URL`.
  - DB-free manual/operator retry for forecast candidate -> submitted manifest
    exports the same file repository flags and preserves prior/new job evidence.
  - Completed cycle with stale hydro `created` -> candidate skipped as terminal
    with explicit evidence and no Slurm submission.
  - Missing `forcing_package_uri` on downstream resume -> stable artifact
    blocker/restart-from-upstream decision, not generic `NODE_FAILURE`.
  - Slurm `COMPLETED|0:0` for matching array task identity -> reconcile marks
    task succeeded.
  - Slurm terminal status with only generic job name and no identity proof ->
    remain unverified, no fabricated success.
  - Live pass with no progress beyond configured bound -> exits/blocks with
    progress evidence and releases lock.
  - Concurrent node-22 service run with bound >1 -> multiple candidates can be
    planned/submitted/reconciled without duplicate submissions or stale lock.
  - Node-22 service run with no eligible candidates -> records safe no-work
    evidence but does not satisfy the business-readiness task.
  - Legacy non-DB-free scheduler/reconcile path -> remains compatible or is
    explicitly reported as out-of-scope before merge.

Review focus:
- Runtime mode propagation across retry/submission/sbatch/runtime.
- Identity binding across Slurm reconcile and file journal.
- Terminal-state/stale-state idempotency.
- Bounded progress and lock cleanup under concurrent scheduler operation.
