## Context

Node-22 has already stopped owning production downloads, but it still owns the
production scheduler and Slurm/SHUD control surface. The historical node-22
PostgreSQL `:55433` listener remains online only because the scheduler still
uses it for lock/state/model reads. Latest live evidence shows:

```text
DATABASE_URL=postgresql://REDACTED@10.0.2.100:55433/nhms
NHMS_SCHEDULER_LOCK_BACKEND=postgres
latest scheduler evidence lock_type=postgres_advisory
```

The stopping gate is documented in
`docs/runbooks/node22-db-retirement-runbook.md`. This change implements the
software work needed to satisfy that gate.

## Goals

- Run node-22 production scheduler passes with no `DATABASE_URL` in scheduler
  runtime env.
- Make scheduler evidence prove DB-free operation through file lock, file
  registry, file readiness, file journal, and file state-index evidence.
- Preserve existing scheduler semantics for duplicate prevention, retry,
  permanent-failure guard, strict warm-start, and downstream Slurm submission.
- Keep node-27 as the active DB/display/ingest owner; do not move scheduler to
  node-27.
- Stop node-22 `:55433` only after archive, rollback, and live GFS/IFS receipts
  prove the DB-free scheduler path.

## Non-Goals

- No display/frontend behavior changes.
- No migration of scheduler/control plane from node-22 to node-27.
- No deletion of historical database files before archive and checksum receipt.
- No new business download behavior; node-27 raw download remains the source
  acquisition owner.
- No weakening of strict forecast warm-start policy.

## Decisions

1. **DB-free mode is explicit and fail-closed.** The scheduler must not infer
   DB-free operation from missing env alone. `NHMS_SCHEDULER_STATE_BACKEND=file`
   and related backend settings declare intent; any remaining scheduler
   `DATABASE_URL` in that mode is a blocker.

2. **File lock first, state later.** File locking is already implemented by
   `FileSchedulerLease`, so the first cutover slice should prove
   `lock_type=file` with no DB. This reduces risk before replacing larger state
   surfaces.

3. **Use manifest-shaped sources, not ad hoc config.** Model inventory,
   canonical product readiness, and state snapshot lookup must come from
   versioned JSON manifests or indexes with checksums, schema versions, and
   bounded evidence. Runtime env should point at those files, not embed large
   model/state data.

4. **Journal is append-only with materialized latest views.** The file-backed
   orchestration state uses append-only JSONL records for auditability and
   atomic materialized `state.json` files for fast scheduler reads. Writes are
   performed under the scheduler file lock and use temporary files plus atomic
   rename.

5. **Compatibility is contract-tested against existing repository semantics.**
   The file journal must preserve behavior currently supplied by
   `PsycopgOrchestratorRepository`: active/completed checks, candidate state,
   reservation binding, job status updates, pipeline events, retry repair, and
   permanent-failure decisions.

6. **Cutover keeps DB online until proven.** The first live deployment runs with
   `:55433` still listening but unused. Only after no-DB scheduler evidence and
   GFS/IFS live receipts pass do operators archive and stop the listener.

7. **Runtime names are fixed by this change.** Implementations must not invent
   backend env keys per issue. The canonical matrix below is the contract used
   by env templates, preflight, evidence, runbooks, and static guardrails.

## Data Contracts

### Canonical DB-Free Runtime Env Matrix

DB-free scheduler mode uses these canonical keys:

| Purpose | Env key | DB-free value |
|---|---|---|
| Mode selector | `NHMS_SCHEDULER_STATE_BACKEND` | `file` |
| Lock selector | `NHMS_SCHEDULER_LOCK_BACKEND` | `file` |
| Registry selector | `NHMS_SCHEDULER_REGISTRY_BACKEND` | `file` |
| Registry manifest | `NHMS_SCHEDULER_REGISTRY_MANIFEST` | absolute path or supported object URI |
| Canonical readiness selector | `NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND` | `file` |
| Canonical readiness index | `NHMS_SCHEDULER_CANONICAL_READINESS_INDEX` | absolute path or supported object URI |
| Journal selector | `NHMS_SCHEDULER_JOURNAL_BACKEND` | `file` |
| Journal root | `NHMS_SCHEDULER_JOURNAL_ROOT` | absolute path |
| State index selector | `NHMS_SCHEDULER_STATE_INDEX_BACKEND` | `file` |
| State index path | `NHMS_SCHEDULER_STATE_INDEX` | absolute path or supported object URI |
| DB-free preflight | `NHMS_SCHEDULER_DB_FREE_REQUIRED` | `true` |

When `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, every selector above must be
`file`, every required path must be configured and safe, and scheduler
`DATABASE_URL` must be absent. Any `postgres`, `psycopg`, blank, or implicit
fallback backend is a pre-lock blocker.

DB-free scheduler construction must also prove DB-backed factory paths are not
reachable. This includes model/met/orchestrator/state factories, DB-backed retry
or pipeline-store construction, and the forcing producer path
`ForcingProducer.from_env()` while it constructs `PsycopgMetStore` internally.
Forcing production remains outside node-22's DB-free scheduler runtime unless a
separate DB-free forcing producer is implemented and contract-tested.

### Scheduler Registry Manifest

The DB-free registry manifest contains:

- `schema_version`
- `generated_at`
- `models[]`
- per model: `model_id`, `basin_id`, `basin_version_id`,
  `river_network_version_id`, `model_package_uri`, `manifest_uri`,
  `package_checksum`, `resource_profile`, `display_capabilities`,
  `frequency_capabilities`, `output_segment_count`, and source policy metadata.

The manifest is produced by a separate publisher task. It is written
manifest-last, includes checksum and `generated_at`, and is the only registry
source node-22 may read in DB-free mode.

### Canonical Readiness Index

The DB-free canonical readiness index maps source/cycle identity to canonical
product evidence sufficient for `evaluate_canonical_readiness`, including
product IDs, forecast hours, product counts, object-store URI, checksum, and
source/cycle/model/basin identity.

The index is produced from authoritative canonical product artifacts or the
node-27 data plane, then atomically published for node-22. It must include
schema version, checksum, `generated_at`, source/cycle/model/basin identity,
forecast-hour coverage, and object existence evidence. Stale or mismatched
index files fail closed.

### Orchestration Journal

The file-backed journal uses:

```text
<state-root>/journal/<source>/<cycle>.jsonl
<state-root>/latest/<source>/<cycle>/<model_id>.json
<state-root>/pipeline-jobs/<job_id>.json
<state-root>/pipeline-events/<source>/<cycle>.jsonl
```

Records include `schema_version`, `record_type`, `event_id` or monotonic
sequence, `created_at`, candidate identity, run/job identity, status, stage,
retry attempt, Slurm job ID, error code, and redacted runtime root evidence.

The scheduler-required repository interface includes read-only state
(`has_active_orchestration`, `has_active_pipeline`, `has_completed_pipeline`,
`candidate_state`, `active_slurm_jobs`), context reads (`load_model_context`,
`find_forcing_context`, canonical-ready listings where still needed), lifecycle
writes (`ensure_forecast_cycle`, `create_hydro_run`,
`update_forecast_cycle_status`, `update_hydro_run_status`), pipeline writes
(`reserve_pipeline_job`, `bind_pipeline_job_reservation`,
`upsert_pipeline_job`, `update_pipeline_job_status`, `insert_pipeline_event`),
and status/query helpers used by retry, cancellation, and reconcile paths.
DB-free mode must also avoid DB-backed `_retry_service_from_env()` and
`PipelineStore`; retry/permanent-failure events are represented in the file
journal.

Historical node-22 DB rows that affect active/completed/job/event/retry or
permanent-failure decisions are migrated into append-only journal records before
live cutover. The migration receipt records cutoff time, row counts, checksums,
idempotent replay status, and supersession evidence for stale
`download_source_cycle` rows.

### State Snapshot Index

The file-backed state index maps exact strict warm-start lookup keys:

```text
model_id + source_id + valid_time -> state_uri + checksum + usable_flag
```

The scheduler may not fall back to latest usable state when strict successor
checkpoint lookup fails.

## Migration Plan

1. Add DB-free runtime preflight and file-lock live proof while state remains
   read-only/fake or blocked.
2. Add file-backed model registry and canonical readiness providers.
3. Add file-backed orchestration journal contract and implementation.
4. Add file-backed state snapshot index for strict warm-start.
5. Wire scheduler factory to DB-free providers under explicit env toggles.
6. Freeze scheduler timer, back up node-22 scheduler env/unit, and migrate
   scheduler-relevant `:55433` state into the file journal.
7. Run node-22 no-DB bounded scheduler passes with `:55433` still online.
8. Run live GFS and IFS cycles to `convert-or-later` with no scheduler DB env.
9. Capture pre-stop listener/process/session attribution, archive the DB, stop
   `:55433`, then run a final no-DB scheduler pass.

## Risks

- File journal semantics may drift from DB repository behavior and allow double
  submission. Mitigation: contract tests and live file-lock contention tests.
- Canonical readiness may be stale if indexes are not generated atomically.
  Mitigation: schema version, checksum, generated time, and manifest-last write.
- Warm-start index mistakes can violate strict no-cold-start policy. Mitigation:
  fail closed and test exact successor only.
- Existing historical DB rows may encode permanent failures that need migration
  semantics. Mitigation: migration receipt and explicit supersession rules for
  old `download_source_cycle` state.

## Rollback

Before stopping `:55433`, rollback is switching scheduler env back to
PostgreSQL-backed mode and re-enabling the old `DATABASE_URL`. After stopping
the DB, rollback requires restarting the archived PostgreSQL service first,
then restoring the previous scheduler env and timer. Every live cutover receipt
must include the exact env backup and archive path used for rollback.
