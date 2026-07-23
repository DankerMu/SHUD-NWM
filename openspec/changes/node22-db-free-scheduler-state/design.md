## Context

Node-22 has already stopped owning production downloads, but it still owns the
production scheduler and Slurm/SHUD control surface.

Initial context before #836/#837: the historical do-not-connect node-22
PostgreSQL `:55433` rollback listener was not yet archived/stopped only because
the scheduler still used it for lock/state/model reads. Pre-cutover live
evidence showed:

```text
# Historical pre-cutover evidence only; do not connect or reuse.
DATABASE_URL=postgresql://REDACTED@10.0.2.100:55433/nhms
NHMS_SCHEDULER_LOCK_BACKEND=postgres
pre-cutover scheduler evidence lock_type=postgres_advisory
```

The stopping gate is documented in
`docs/runbooks/node22-db-retirement-runbook.md`. This change implements the
software work needed to satisfy that gate.

Current status after #837: node-22 `:55433` is historical do-not-connect,
archived/stopped rollback-only state. The authoritative stop receipt is
`docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`.

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
   performed under durable per-cycle file locks, commit journal truth before
   direct cache snapshots, and use temporary files plus atomic rename.

5. **Compatibility is contract-tested against existing repository semantics.**
   The file journal must preserve behavior currently supplied by
   `PsycopgOrchestratorRepository`: active/completed checks, candidate state,
   reservation binding, job status updates, pipeline events, retry repair, and
   permanent-failure decisions.

6. **Cutover kept historical DB online until proven, then stopped it.** The
   first live deployment ran with the historical do-not-connect `:55433`
   rollback listener still listening but unused. After no-DB scheduler evidence
   and GFS/IFS live receipts passed, #837 archived and stopped the listener.

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
model_id + source_id + valid_time + cycle_id + lead_hours -> state_uri + checksum + usable_flag
```

The scheduler may not fall back to latest usable state when strict successor
checkpoint lookup fails.

## Issue #831 Fixture

Fixture level: expanded
Repair intensity: high

Mandatory expanded triggers:

- Production scheduler config and env matrix parsing.
- Fail-closed pre-lock and pre-submission behavior.
- File-lock concurrency and bounded contention evidence.
- Scheduler evidence schema fields proving no PostgreSQL dependency.
- Shared scheduler factory reachability across registry, readiness, repository,
  retry, state, forcing, and reconcile construction.
- Node-22 no-DB production configuration.

Must preserve:

- Non-DB-free legacy scheduler mode may still use `DATABASE_URL`, postgres lock,
  and DB-backed factories where existing tests intend that behavior.
- Existing workspace/object-store/runtime/temp/evidence root preflight remains
  strict and still blocks unsafe roots before mutation.
- Slurm submission remains gated behind root, lock, evidence, SHUD, template,
  gateway, allowed-root, and env checks.
- `services.orchestrator.scheduler` compatibility facade import and monkeypatch
  paths remain stable; new ownership stays in scheduler owner modules.
- Evidence remains credential-safe and never records raw database URLs or
  secrets.

Must add/change:

- `NHMS_SCHEDULER_DB_FREE_REQUIRED=true` enables the explicit DB-free runtime
  guard.
- DB-free mode requires every canonical scheduler backend selector to be
  `file` and every required manifest/index/journal path to be configured and
  safe.
- DB-free mode rejects scheduler `DATABASE_URL` before acquiring a scheduler
  lease, discovering models, building factories, or submitting Slurm jobs.
- DB-free mode always constructs `FileSchedulerLease` and records
  `lock_type=file`.
- DB-free scheduler evidence records selected backend names and redacted
  configured manifest/index/journal paths.

Risk packs considered for #831:

- Public API / CLI / script entry: selected - `plan-production` and
  `ProductionScheduler.from_env()` are active scheduler entrypoints.
- Config / project setup: selected - canonical env matrix controls the runtime
  mode.
- File IO / path safety / overwrite: selected - file lock, evidence dir,
  manifest/index paths, and journal root are filesystem/object boundaries.
- Schema / columns / units / field names: selected - scheduler evidence gains
  named backend/path fields.
- Auth / permissions / secrets: selected - `DATABASE_URL` rejection and evidence
  redaction are required.
- Concurrency / shared state / ordering: selected - two DB-free passes must not
  both mutate.
- Resource limits / large input / discovery: selected - preflight evidence and
  configured path evidence stay bounded; registry/readiness content parsing is
  deferred.
- Legacy compatibility / examples: selected - postgres-backed legacy mode
  remains reachable outside DB-free mode.
- Error handling / rollback / partial outputs: selected - DB-free blockers are
  stable pre-lock/pre-mutation blockers.
- Release / packaging / dependency compatibility: not selected - no package or
  dependency change in #831.
- Documentation / migration notes: selected - OpenSpec evidence defines the
  operator-facing DB-free contract for later deployment issues.
- Geospatial / CRS / basin geometry: not selected - #831 does not inspect model
  geometry.
- Hydro-met time series / forcing windows: not selected - no canonical product
  selection change in #831.
- SHUD numerical runtime / conservation / NaN: not selected - no solver runtime
  behavior changes in #831.
- PostGIS / TimescaleDB domain behavior: not selected - #831 avoids DB
  mutation/schema behavior rather than changing it.
- Slurm production lifecycle / mock-vs-real parity: selected - DB-free
  submission gating must still work with Slurm enabled.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider discovery or download behavior changes in #831.
- Run manifest / QC provenance: not selected - model run manifest generation is
  unchanged.
- Published NHMS artifacts / display identity: not selected - display published
  artifacts are outside #831.

Boundary-surface checklist:

- Shared helper roots: `services/orchestrator/scheduler_config.py`,
  `scheduler_runtime_roots.py`, `scheduler_preflight.py`.
- Public entrypoints: `services/orchestrator/cli.py` `plan-production`,
  `ProductionScheduler.from_env()`, and `ProductionScheduler.run_once()`.
- Read surfaces: env vars, manifest/index path config, root preflight helpers,
  and scheduler evidence reads.
- Write/delete/overwrite surfaces: scheduler lock file and scheduler evidence
  files only; DB-free mode must not write DB rows in #831.
- Producer/consumer evidence boundaries: `_base_evidence()`,
  `_scheduler_runtime_config_evidence()`, and lock evidence.
- Stale-state/idempotency boundaries: concurrent file lock contention and
  pre-lock DB-free blockers.
- Unchanged downstream consumers: existing postgres-mode scheduler tests,
  Slurm preflight tests, and scheduler compatibility inventory guards.

Invariant Matrix:

- Governing invariant: when `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, the
  scheduler either proves an all-file runtime before mutation or stops with a
  credential-safe blocker before any DB-backed factory, lock, or Slurm
  submission is reachable.
- Source-of-truth identity/contract: the canonical DB-free runtime env matrix
  plus `ProductionSchedulerConfig.database_url` and
  `ProductionSchedulerConfig.scheduler_lock_backend`.
- Producers: `ProductionSchedulerConfig` parses `DATABASE_URL`,
  `NHMS_SCHEDULER_STATE_BACKEND`, `NHMS_SCHEDULER_LOCK_BACKEND`,
  `NHMS_SCHEDULER_REGISTRY_BACKEND`,
  `NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND`,
  `NHMS_SCHEDULER_JOURNAL_BACKEND`,
  `NHMS_SCHEDULER_STATE_INDEX_BACKEND`, and required path keys.
- Validators/preflight: DB-free runtime preflight in scheduler config/runtime,
  root/path safety helpers, `_slurm_preflight()`, and
  `_scheduler_lock_evidence_root_preflight()`.
- Storage/cache/query: no DB-backed registry/readiness/orchestrator/state/retry
  store or reconcile `PipelineStore` may be constructed in DB-free mode; file
  registry/journal/state implementations are later issue scope.
- Public routes/entrypoints: `ProductionScheduler.from_env()` and
  `ProductionScheduler.run_once()` stop before model discovery or lock when
  DB-free config is unsafe.
- Frontend/downstream consumers: none - scheduler evidence is the downstream
  consumer for #831.
- Failure paths/rollback/stale state: `DATABASE_URL` present, blank/unset or
  postgres-like selectors, unsafe/missing paths, and lock contention all produce
  bounded evidence without mutation.
- Evidence/audit/readiness: pass evidence records `database_url_configured`,
  selected backend names, configured path field names, `lock_type=file`, and
  redacted blocker fields.
- Regression rows:
  - All selectors `file`, required paths safe, no `DATABASE_URL` -> config
    parses, file lease is used, and evidence records all DB-free backend names.
  - `DATABASE_URL` present in DB-free mode -> pre-lock
    `database_url_forbidden` blocker, no lease acquisition, no DB factory.
  - Any selector unset, blank, `postgres`, `psycopg`, or non-file in DB-free
    mode -> blocker names the exact field, no lease acquisition, no DB factory.
  - Required manifest/index/journal path missing or unsafe -> blocker names the
    exact path field before mutation.
  - Two DB-free passes on the same lock -> at most one acquires the file lock;
    the other records bounded contention.
  - DB-backed factory monkeypatches raise in DB-free mode -> tests still pass
    because those paths are unreachable.
  - Non-DB-free postgres mode -> existing postgres lock/backend tests still
    pass.

## Issue #832 Fixture

Fixture level: expanded
Repair intensity: high

Mandatory expanded triggers:

- Versioned JSON registry/readiness schemas consumed by production scheduler.
- Manifest-last publication, checksum binding, and object existence evidence.
- File/object URI reads under DB-free production config.
- Scheduler planning semantics for raw handoff, canonical-zero reuse, and no
  retired `download_source_cycle` submission.
- DB-backed factory reachability for model registry and canonical readiness.

Must preserve:

- Existing postgres-backed registry/readiness remains reachable outside
  DB-free mode.
- Existing canonical readiness semantics continue to come from
  `evaluate_canonical_readiness`; file mode only changes the source of product
  rows and validation evidence.
- Node-27 remains source acquisition owner; node-22 consumes raw readiness and
  starts at `convert` when raw is already ready.
- File journal and state snapshot index remain later issue scope; #832 must not
  claim those write/read contracts are implemented.
- Existing scheduler compatibility facade imports and monkeypatch paths remain
  stable.

Must add/change:

- DB-free `ProductionScheduler.from_env()` builds a file-backed model registry
  from `NHMS_SCHEDULER_REGISTRY_MANIFEST` without
  `PsycopgModelRegistryStore.from_env()`.
- DB-free canonical readiness uses `NHMS_SCHEDULER_CANONICAL_READINESS_INDEX`
  without `PsycopgMetStore`.
- Registry and readiness manifests validate schema version, generated time,
  checksum, source/cycle/model/basin identity, and bounded counts before trust.
- Missing, stale, checksum-mismatched, unsupported, object-missing, duplicate,
  or identity-mismatched files fail closed with redacted evidence.
- Registry/readiness publishers write content and checksum evidence before
  atomically publishing the final manifest/index.

Risk packs considered for #832:

- Public API / CLI / script entry: selected - `ProductionScheduler.from_env()`
  and `run_once()` consume the new file providers.
- Config / project setup: selected - canonical DB-free env paths select the
  manifest/index sources.
- File IO / path safety / overwrite: selected - loader and publisher read and
  atomically publish trusted JSON/object evidence.
- Schema / columns / units / field names: selected - registry/readiness JSON
  schemas are new scheduler contracts.
- Auth / permissions / secrets: selected - evidence must redact local paths,
  object buckets, and secret-bearing URI fragments.
- Concurrency / shared state / ordering: selected - manifest-last publication
  prevents readers from trusting partial refreshes.
- Resource limits / large input / discovery: selected - JSON and product/model
  discovery are bounded by bytes, entry counts, and evidence size.
- Legacy compatibility / examples: selected - non-DB-free postgres mode and
  existing model coercion semantics are preserved.
- Error handling / rollback / partial outputs: selected - every invalid file or
  stale artifact becomes a stable planning blocker before submission.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change in #832.
- Documentation / migration notes: selected - OpenSpec records the new file
  contracts used by later deployment issues.
- Geospatial / CRS / basin geometry: not selected - #832 preserves basin IDs
  and segment counts but does not inspect geometry.
- Hydro-met time series / forcing windows: selected - canonical readiness
  forecast-hour coverage and product rows gate forcing reuse.
- SHUD numerical runtime / conservation / NaN: not selected - no solver
  numerical behavior changes.
- PostGIS / TimescaleDB domain behavior: not selected - #832 removes
  scheduler reads from those stores in DB-free mode rather than changing DB
  schema semantics.
- Slurm production lifecycle / mock-vs-real parity: selected - raw-ready
  canonical-zero candidates must submit downstream stages without retired
  download jobs.
- External hydro-met providers / snapshot reproducibility: selected -
  source/cycle/policy/object identity from provider evidence must match the
  readiness index.
- Run manifest / QC provenance: selected - candidate manifests must carry
  canonical/readiness/raw evidence and `restart_stage=convert`.
- Published NHMS artifacts / display identity: not selected - display products
  are outside #832.

Boundary-surface checklist:

- Shared helper roots: new file provider module, `scheduler_core.py`,
  `scheduler_models.py`, `scheduler_candidates.py`, and raw manifest helpers.
- Public entrypoints: `ProductionScheduler.from_env()` and scheduler
  `run_once()` in DB-free mode.
- Read surfaces: registry manifest, canonical readiness index, referenced
  model manifests, canonical product objects, and node-27 raw manifest evidence.
- Write/delete/overwrite surfaces: registry/readiness publisher staging files
  and final manifest/index publish only; no journal/state-index writes in #832.
- Staging/publish/rollback surfaces: manifest-last atomic writes and cleanup of
  publisher temporary files.
- Producer/consumer evidence boundaries: publisher receipts, model discovery
  evidence, canonical readiness evidence, candidate state evidence, and model
  run manifest construction.
- Stale-state/idempotency boundaries: stale generated times, checksum mismatch,
  duplicate IDs, missing objects, and repeated publisher refreshes.
- Unchanged downstream consumers: postgres-mode scheduler tests, existing
  canonical readiness evaluator tests, and future file journal/state-index
  tasks.

Invariant Matrix:

- Governing invariant: in DB-free scheduler mode, model and canonical
  readiness truth is trusted only when a versioned file/object manifest binds
  identity, checksum, freshness, and object existence; otherwise planning fails
  closed before any retired download or DB fallback can occur.
- Source-of-truth identity/contract: registry `schema_version`, manifest
  checksum, model/basin/package identity, readiness `schema_version`,
  source/cycle/model/basin/policy/object identity, product checksums, and raw
  manifest source/cycle identity.
- Producers: registry publisher, readiness publisher, and node-27 raw manifest
  publisher/stager.
- Validators/preflight: file loader byte/schema/depth/count checks, checksum
  verifier, object existence verifier, stale generated-time verifier, and
  DB-free runtime preflight.
- Storage/cache/query: file registry `list_models()/get_model()`, file
  canonical readiness provider, `LocalObjectStore`/safe filesystem reads; DB
  registry/met stores are forbidden in DB-free mode.
- Public routes/entrypoints: `ProductionScheduler.from_env()` wires file
  providers and `run_once()` uses them for discovery/readiness before
  candidate submission.
- Frontend/downstream consumers: none directly - scheduler evidence and model
  run manifests are the downstream contract in #832.
- Failure paths/rollback/stale state: missing/malformed/oversized JSON,
  unsupported schema, duplicate model IDs, checksum mismatch, stale generated
  time, object missing, identity mismatch, invalid raw manifest, and incomplete
  canonical coverage.
- Evidence/audit/readiness: bounded publisher receipts, model discovery
  registry evidence, canonical readiness evidence, raw handoff evidence, and
  no-DB factory guard assertions.
- Regression rows:
  - Valid registry manifest with verified package manifest -> DB-free model
    discovery selects the model and records registry checksum without DB calls.
  - Duplicate/missing/checksum-invalid registry manifest -> planning blocks
    before submission with stable redacted evidence.
  - Valid readiness index with matching source/cycle/model/basin and products
    -> existing canonical evaluator receives product rows and records ready or
    canonical-zero evidence.
  - Missing/stale/schema-invalid/checksum-invalid/object-missing readiness
    index -> canonical readiness fails closed without `PsycopgMetStore`.
  - Ready node-27 raw evidence plus canonical-zero rows -> candidate carries
    `restart_stage=convert` and Slurm request does not include
    `download_source_cycle`.
  - Missing/invalid node-27 raw evidence plus canonical-zero rows -> candidate
    blocks and no Slurm submission occurs.
  - Non-DB-free postgres mode -> legacy registry/readiness factories remain
    available where existing tests cover them.

## Issue #833 Fixture

Fixture level: expanded
Repair intensity: high

Mandatory expanded triggers:

- File-backed replacement for scheduler planning reads currently supplied by
  `PsycopgOrchestratorRepository`.
- Append-only journal replay and materialized latest views affect duplicate
  prevention and active Slurm behavior.
- DB-free `ProductionScheduler.from_env()` changes its active repository wiring.
- Candidate-state evidence can trigger active skip, completed skip, retry,
  permanent block, raw handoff restart, and cancellation/status-sync paths.

Must preserve:

- #832 registry, canonical readiness, and node-27 raw handoff evidence stay
  bounded and redacted.
- File journal write side, retry service replacement, and historical migration
  were intentionally deferred by #833 at that time. That deferral is now
  superseded by #834 / section 4, which implements those surfaces while live
  node-22 deployment remains a later cutover step.
- Non-DB-free postgres repository behavior and public scheduler facade imports
  remain stable.
- DB-free default mutation blocked during #833 because the file journal write
  side had not landed. That blocker is historical; #834 closes the file-backed
  write/retry/migration contract without weakening the DB-free preflight gate.

Must add/change:

- Add a versioned append-only record schema
  `nhms.scheduler.file_orchestration_journal.v1` and materialized latest schema
  `nhms.scheduler.file_orchestration_latest.v1`.
- Implement a file-backed read repository for active orchestration, active
  pipeline, completed pipeline, active Slurm jobs, candidate state, model
  context, forcing context, pipeline job lookup, and stage-status query helpers.
- Reuse the existing candidate-state row materialization algorithm so DB-backed
  and file-backed rows produce the same scheduler decisions.
- Wire DB-free scheduler construction to the file journal repository from
  `NHMS_SCHEDULER_JOURNAL_ROOT` without DB-backed repository factories.
- Fail closed on malformed journal/latest state, schema-less direct job
  snapshots, sidecar event schema/cycle mismatches, nested identity mismatches,
  unsafe scanned entries, discovery/file/JSON complexity limits, and invalid
  scalar fields; redact runtime roots, local paths, object URIs, and blocked
  query sentinels in public candidate/job evidence.

Risk packs considered for #833:

- Public API / CLI / script entry: selected - `ProductionScheduler.from_env()`
  uses the new read repository in DB-free mode.
- Config / project setup: selected - `NHMS_SCHEDULER_JOURNAL_ROOT` becomes a
  live planning input.
- File IO / path safety / overwrite: selected - reader consumes latest JSON,
  append-only JSONL, pipeline-job files, and pipeline-event files.
- Schema / columns / units / field names: selected - new journal/latest schemas
  encode scheduler row contracts and replay metadata.
- Auth / permissions / secrets: selected - runtime roots and local paths must
  not leak through scheduler evidence.
- Concurrency / shared state / ordering: selected - append-only replay and
  materialized latest views must not allow duplicate submission.
- Resource limits / large input / discovery: selected - file reads, scanned
  file discovery, recursion depth, replay records, and JSON node/depth
  complexity are bounded.
- Legacy compatibility / examples: selected - postgres-mode repository methods
  and scheduler facade imports remain compatible.
- Error handling / rollback / partial outputs: selected - malformed state fails
  closed instead of being treated as absent.
- Slurm production lifecycle / mock-vs-real parity: selected - active Slurm job
  evidence controls skip/cancel/status-sync paths.

Boundary-surface checklist:

- Shared helper roots: `chain_repository_state.py`, new file journal reader,
  `scheduler_core.py`, and scheduler facade exports.
- Public entrypoints: `ProductionScheduler.from_env()`, scheduler planning
  `run_once()`, and repository read/query methods.
- Read surfaces: `<journal-root>/latest`, `<journal-root>/journal`,
  `<journal-root>/pipeline-jobs`, `<journal-root>/pipeline-events`,
  `<journal-root>/models`, and `<journal-root>/forcing`.
- Write/delete/overwrite surfaces: none in #833. Section 4 / #834 replaces the
  temporary fail-not-implemented write methods with file-journal writes,
  retry-source parity, forecast-cycle events, and historical migration import.
- Staging/publish/rollback surfaces: replay metadata, latest schema, and task
  4 historical migration/import receipts.
- Producer/consumer evidence boundaries: candidate state, active Slurm jobs,
  scheduler skipped/blocked candidates, and model/forcing context reads.
- Stale-state/idempotency boundaries: active/completed statuses, candidate
  identity filters, append-only sequence replay, malformed JSON, and missing
  optional latest views.
- Unchanged downstream consumers: #832 file providers, raw handoff staging,
  postgres-mode repository tests, and future write/migration tasks.

Invariant Matrix:

- Governing invariant: DB-free scheduler planning must treat file journal rows
  as the orchestration-state source of truth only when schema and identity are
  valid; malformed state blocks rather than permitting duplicate submission.
- Source-of-truth identity/contract: applicable schema version,
  source/cycle/model/run identity, candidate ID, forcing version ID, job ID,
  Slurm job ID, stage, status, error code, sequence/event ID, replay metadata,
  context field contracts, and redacted runtime roots.
- Producers: #833 tests created read-side fixtures only. #834 now owns the file
  write side, retry replacement, and historical migration producer/import
  contract.
- Validators/preflight: file schema validation, source/cycle/model/run/job
  identity checks, path segment checks, no-follow scanned entry validation,
  safe bounded reads, file/depth/JSON complexity limits, and existing DB-free
  runtime preflight.
- Storage/cache/query: materialized latest views, append-only JSONL replay,
  pipeline job JSON files, pipeline event JSONL files, model context JSON, and
  forcing context JSON.
- Public routes/entrypoints: DB-free scheduler construction and read/query
  repository methods.
- Frontend/downstream consumers: none directly - scheduler evidence and Slurm
  planning outcomes are the downstream contract in #833.
- Failure paths/rollback/stale state: malformed JSON, schema mismatch, source
  mismatch, cycle mismatch, missing optional latest view, unknown record type,
  reservation conflicts, retry exhaustion, permanent failures, and migration
  replay blockers.
- Evidence/audit/readiness: repository contract tests, scheduler no-DB planning
  test, raw handoff regression selector, ruff, and OpenSpec strict validation.
- Regression rows:
  - Active job in file latest or append-only journal -> scheduler sees active
    orchestration/pipeline and active Slurm evidence.
  - Valid source alias/casing at caller or row boundary -> canonical file
    source identity is used consistently for path lookup, cycle IDs, row
    matching, and context reads; accepted rows cannot later disappear because
    of raw source-string casing.
  - Active job in file latest or append-only journal plus newer terminal direct
    `pipeline-jobs` snapshot for the same `job_id` -> active replay remains
    authoritative for scheduler planning and query evidence.
  - Append-only journal records for the same `job_id` with increasing sequence
    -> the later valid sequence/replay row is authoritative even when timestamp
    fields are absent or older.
  - Journal envelope/payload/run identity disagreement -> file replay fails
    closed for the affected model instead of treating the row as a sibling
    absence.
  - Valid direct-only `pipeline-jobs/<job_id>.json` snapshot with matching
    payload identity -> scheduler active/candidate/Slurm/query reads can
    consume it without relying on undocumented filename substrings.
  - Completed hydro run in file latest -> scheduler skips completed duplicate.
  - Candidate-state rows from file latest/journal -> existing candidate-state
    decision code sees the same row shape as DB-backed reads.
  - Model/forcing context file rows -> context methods return typed
    `ModelContext`/`ForcingContext` without PostgreSQL.
  - Malformed latest view, unknown journal record type, JSONL record over-limit,
    or over-limit non-matching directory entries -> active detection, query
    helpers, and candidate state fail closed with bounded public evidence.
  - File lifecycle/reservation/status/event write -> append-only journal record
    plus latest/query materialization without DB-backed repository calls.
  - Retry exhaustion or manual repair -> file journal records permanent-failure
    or manual retry marker evidence consumable by existing candidate-state
    decision helpers.
  - DB-free scheduler from_env -> file journal repository is constructed and
    DB-backed active/orchestrator repository factories are not called.

## Issue #835 Fixture

Fixture level: expanded
Repair intensity: high

Mandatory expanded triggers:

- File-backed state snapshot index is a versioned JSON evidence contract with
  checksums, object existence checks, freshness, and bounded reads.
- Strict forecast warm-start is a production scheduler state-machine gate; it
  must fail closed before Slurm/orchestrator mutation when the exact successor
  checkpoint is unavailable.
- `state_save_qc` becomes a DB-free producer of state-index records while
  preserving legacy non-DB-free CLI behavior.
- Shared `packages/common.state_manager` and `state_cli` helper behavior is
  consumed by scheduler candidate construction, Slurm array jobs, and existing
  warm-start tests.

Must preserve:

- Non-DB-free scheduler and state-save paths may still use
  `PsycopgStateSnapshotRepository` and `StateRunRepository.from_env()` where
  existing tests intend that behavior.
- Legacy `state_cli save --manifest-index --task-id` entries that only contain
  `run_id` remain valid outside DB-free mode.
- Strict warm-start never falls back to latest usable state when a keyed
  successor checkpoint is missing or invalid.
- Candidate state evidence still flows into basin manifests through the
  existing `candidate_state` contract.
- Evidence remains credential-safe and does not reveal local runtime roots,
  raw object-store roots, or database connection details.

Must add/change:

- Add `FileStateSnapshotIndexRepository` and publisher behavior for
  `nhms.scheduler.file_state_snapshot_index.v1`.
- Strict exact lookups use `model_id + source_id + valid_time + expected
  cycle_id + required lead_hours`; source-less, wrong-cycle, wrong-lead, or
  latest-state lookup is not an accepted DB-free substitute.
- Index validation checks schema version, generated time, checksum, entry
  count, JSON complexity, usable flag, object existence, object checksum,
  source/model/time identity, and model package identity.
- State object references are supported object URIs (`s3` or `published`) with
  configured object-store boundaries; legacy relative object-store keys remain
  accepted only after safe containment validation under `OBJECT_STORE_ROOT`.
- DB-free `state_save_qc` obtains run context from manifest-index or explicit
  `NHMS_*` runtime env and writes state-index entries without constructing DB
  repositories.
- DB-free scheduler candidate construction attaches ready state-index evidence
  or blocks the candidate with a stable state-index reason before submission.

Risk packs considered for #835:

- Public API / CLI / script entry: selected - `state_cli save`,
  `ProductionScheduler.from_env()`, and scheduler candidate construction are
  active entrypoints.
- Config / project setup: selected - `NHMS_SCHEDULER_STATE_INDEX_BACKEND`,
  `NHMS_SCHEDULER_STATE_INDEX`, `OBJECT_STORE_ROOT`, `OBJECT_STORE_PREFIX`, and
  strict warm-start env decide runtime behavior.
- File IO / path safety / overwrite: selected - index reads/writes and state
  object verification cross local path and object URI boundaries.
- Schema / columns / units / field names: selected - state-index schema,
  evidence keys, source/model/time identity, checksum fields, and lineage fields
  are public contracts.
- Auth / permissions / secrets: selected - DB-free mode must avoid database
  factories and redact local/object roots in evidence.
- Concurrency / shared state / ordering: selected - state-index publish is
  manifest-last and scheduler must block before mutation when evidence is not
  ready.
- Resource limits / large input / discovery: selected - index bytes, entry
  count, JSON depth/node count, and state object reads are bounded.
- Legacy compatibility / examples: selected - postgres-mode state save and old
  manifest-index CLI entries remain compatible.
- Error handling / rollback / partial outputs: selected - malformed, stale,
  mismatched, missing, and unusable state evidence produce stable blockers
  without partial scheduler mutation.
- Release / packaging / dependency compatibility: not selected - no package or
  dependency change in #835.
- Documentation / migration notes: selected - OpenSpec records the operator
  contract for later live node-22 deployment.
- Geospatial / CRS / basin geometry: not selected - #835 does not inspect basin
  geometry.
- Hydro-met time series / forcing windows: selected - strict successor
  checkpoint identity is tied to source/cycle/valid-time alignment.
- SHUD numerical runtime / conservation / NaN: not selected - #835 verifies IC
  file existence/checksum but does not change solver numerics.
- PostGIS / TimescaleDB domain behavior: not selected - DB-free mode avoids DB
  access rather than changing DB schema or query semantics.
- Slurm production lifecycle / mock-vs-real parity: selected - strict
  warm-start blocking occurs before Slurm/orchestrator mutation.
- External hydro-met providers / snapshot reproducibility: selected - source
  identity and package checksum prevent mixing stale provider/model snapshots.
- Run manifest / QC provenance: selected - `state_save_qc` records checksum,
  package, source, cycle, and valid-time lineage.
- Published NHMS artifacts / display identity: not selected - display artifacts
  are outside #835.

Boundary-surface checklist:

- Shared helper roots: `packages/common/state_manager.py`,
  `packages/common/state_cli.py`, and object-store/safe-fs helpers.
- Public entrypoints: `state_cli save`, Slurm array `state_save_qc`, DB-free
  `ProductionScheduler.from_env()`, and scheduler candidate construction.
- Read surfaces: `NHMS_SCHEDULER_STATE_INDEX`, object-store state URI, manifest
  index rows, `NHMS_*` runtime env, and existing file journal candidate state.
- Write/delete/overwrite surfaces: state-index publish/update only; no delete
  or cleanup behavior is introduced by #835. File-backed state-index
  read/modify/publish is serialized by an adjacent local lock for local and
  LocalObjectStore-backed object indexes; unsafe/unlockable backends fail
  closed.
- Staging/publish/rollback surfaces: state-index publisher validates referenced
  state objects before publishing the index as the last artifact.
- Producer/consumer evidence boundaries: `state_save_qc` produces state-index
  entries; scheduler strict warm-start consumes them and carries
  `candidate_state` into basin manifests.
- Stale-state/idempotency boundaries: stale generated time, wrong
  source/model/time, unusable flag, package mismatch, checksum mismatch, and
  missing object all block rather than falling back.
- Unchanged downstream consumers: legacy state-manager tests, Slurm array CLI
  manifest-index tests, postgres-mode scheduler/state paths, and basin manifest
  warm-start field copying.

Invariant Matrix:

- Governing invariant: in DB-free strict warm-start mode, a scheduler candidate
  may proceed only when the file state index proves the exact
  `model_id + source_id + valid_time + expected cycle_id + required lead_hours`
  checkpoint, its object checksum, usable flag, and model/source/package
  lineage; otherwise the candidate blocks before any DB fallback, latest-state
  fallback, Slurm submission, or orchestrator mutation.
- Source-of-truth identity/contract: state-index schema version,
  `model_id`, normalized `source_id`, `valid_time`, `state_uri`, state object
  checksum, `usable_flag`, expected `cycle_id`, `lead_hours`, model package
  URI/checksum, generated_at, and index checksum.
- Producers: `state_cli.save_state_for_run()` and
  `publish_state_snapshot_index()` write index records after QC/object evidence.
- Validators/preflight: `FileStateSnapshotIndexRepository` schema/checksum/time
  validation, object existence/checksum verification, safe/bounded reads, and
  DB-free runtime preflight.
- Storage/cache/query: file/object-backed state index, state object store, and
  cached scheduler state-index repository per scheduler pass.
- Public routes/entrypoints: `state_cli save`, `ProductionScheduler.run_once()`,
  and scheduler candidate construction.
- Frontend/downstream consumers: none directly - basin manifests and scheduler
  evidence are the downstream contracts in #835.
- Failure paths/rollback/stale state: missing index, malformed JSON, unsupported
  schema, stale/future generated_at, checksum mismatch, missing object, object
  checksum mismatch, unusable flag, wrong source/model/time/package, missing run
  context, and legacy manifest-index compatibility.
- Evidence/audit/readiness: focused state-manager/state-save tests, scheduler
  strict warm-start ready/block tests, legacy Slurm array CLI compatibility test,
  ruff, git diff check, and OpenSpec strict validation.
- Regression rows:
  - Valid exact entry and object checksum -> strict lookup returns ready
    `candidate_state` evidence with lineage and no DB factory.
  - Missing exact entry for the key -> scheduler blocks
    `state_snapshot_index_exact_checkpoint_missing` and does not query latest
    state.
  - Missing object -> blocks `state_snapshot_index_object_missing`.
  - Object checksum mismatch -> blocks
    `state_snapshot_index_object_checksum_mismatch`.
  - `usable_flag=false` -> blocks
    `state_snapshot_index_checkpoint_unusable`.
  - Non-boolean `usable_flag` -> fail-closed
    `state_snapshot_index_usable_flag_invalid` with no `candidate_state`.
  - Unsafe or cross-prefix state object URI -> fail-closed
    state-index object URI blocker with no local root leakage.
  - Concurrent DB-free state-index upserts for distinct keys -> serialized
    update preserves both entries.
  - Wrong source/model/time/package or stale/unsupported index -> fail-closed
    state-index blocker, no DB fallback.
  - Missing or wrong expected `cycle_id`, or wrong `lead_hours` for a matching
    `valid_time` -> fail-closed lineage blocker, no DB/latest fallback.
  - Two state checkpoints share `model_id + source_id + valid_time` but have
    different producing cycles/leads -> strict lookup selects the checkpoint
    matching the requested lead and expected producer cycle.
  - Same-checksum DB-free state save rerun with missing older lineage/package
    metadata -> repairs metadata before strict readiness can pass.
  - DB-free `state_cli save` with manifest-index and no `DATABASE_URL` ->
    writes usable state-index record without `StateRunRepository.from_env()` or
    `PsycopgStateSnapshotRepository`.
  - Legacy non-DB-free `state_cli save --manifest-index --task-id` with only
    `run_id` -> still resolves the run ID and delegates to legacy repository
    loading.
  - Ready strict state evidence on scheduler candidate -> basin manifest can
    receive `init_state_*` through the existing `candidate_state` contract.

## Issue #836 Fixture

Fixture level: expanded/live-deployment
Repair intensity: high

Mandatory expanded triggers:

- Deployment entrypoints and static guardrails decide whether node-22 scheduler
  receives `DATABASE_URL` at runtime.
- `infra/env/compute.example`, `infra/compose.compute.yml`, systemd comments,
  and runbooks are operator-facing contracts for the DB-free cutover.
- Local no-DB tests must cover file lock, registry, canonical readiness, raw
  handoff, file journal, retry, state index, and Slurm preflight without
  PostgreSQL factory reachability.
- Live evidence must prove one GFS and one IFS cycle progress through
  `convert-or-later` while `:55433` remains online only as rollback.

Must preserve:

- `compute-api` may still receive a writer-capable `DATABASE_URL`; this issue
  removes scheduler runtime dependency, not every compute-side DB credential.
- Non-DB runtime roots, manifest paths, secret handling, template roots,
  workspace/object-store/temp/evidence roots, and allowed-root checks stay
  strict.
- During #836, the historical do-not-connect `:55433` rollback listener stayed
  online as rollback state; archive/stop is #837.
- Existing display/node-27 readonly and ingest responsibilities do not move to
  node-22.
- Scheduler evidence remains bounded and must not reveal raw DB URLs, tokens,
  local private roots, or object-store secrets.

Must add/change:

- `scheduler-once` compose runtime omits `DATABASE_URL` and carries the full
  canonical DB-free env matrix.
- Static compose/env checks fail if DB-free `scheduler-once` receives
  `DATABASE_URL`, if selectors drift from `file`, or if required
  registry/readiness/journal/state-index paths are missing or empty.
- Compute env/runbook/systemd guidance distinguishes `compute-api` DB/rollback
  credentials from DB-free scheduler runtime.
- Node-22 cutover receipts record frozen timer state, env/unit backups,
  migration artifacts, rollback commands, bounded no-DB scheduler evidence, and
  live GFS/IFS `convert-or-later` proof.

Risk packs considered for #836:

- Public API / CLI / script entry: selected - `docker compose run
  scheduler-once`, systemd units, and scheduler CLI are production entrypoints.
- Config / project setup: selected - env files, compose interpolation, and
  systemd comments govern runtime shape.
- File IO / path safety / overwrite: selected - deployment paths, journal root,
  manifest/index paths, and evidence roots cross host/container boundaries.
- Schema / field names: selected - DB-free env matrix field names and evidence
  keys are operator contracts.
- Auth / permissions / secrets: selected - `DATABASE_URL` must be absent from
  scheduler env while still redacted/allowed for compute-api rollback.
- Concurrency / shared state / ordering: selected - timer freeze and file lock
  avoid concurrent migration/live scheduler mutation.
- Resource limits / discovery: selected - static and runtime evidence remain
  bounded; local no-DB tests cover discovery providers.
- Legacy compatibility / rollback: selected - the historical do-not-connect
  node-22 `:55433` rollback listener stayed online during #836 and was
  archived/stopped by #837.
- Error handling / partial outputs: selected - failed preflight/live evidence
  blocks merge rather than claiming readiness.
- Slurm production lifecycle / mock-vs-real parity: selected - live receipts
  must show downstream Slurm submission without `download_source_cycle`.
- External hydro-met providers / snapshot reproducibility: selected - GFS and
  IFS receipts must bind source/cycle identity and node-27 raw handoff.

Boundary-surface checklist:

- Shared helper roots: `scripts/validate_two_node_docker_runtime.py`,
  scheduler DB-free preflight helpers, and file provider tests.
- Public entrypoints: `infra/compose.compute.yml` `scheduler-once`,
  `infra/env/compute.example`, `infra/systemd/nhms-compute-compose.service`,
  and node-22 scheduler timer/service.
- Read surfaces: env files, compose interpolation, registry/readiness/state
  index files, file journal, raw manifest handoff, and live scheduler evidence.
- Write surfaces: local docs/specs/tests, node-22 env backup/deploy files,
  scheduler evidence root, migration receipts, and file journal writes.
- Staging/rollback surfaces: timer freeze, env/unit backup, DB-free env deploy,
  migration artifacts, rollback commands, and `:55433` left online.
- Unchanged downstream consumers: compute-api DB env, node-27 display/ingest,
  and #837 DB archive/stop runbook.

Invariant Matrix:

- Governing invariant: during #836, node-22 scheduler runtime either proves a
  full file-backed DB-free environment with `DATABASE_URL` absent or blocks
  before mutation; `:55433` remains online solely as rollback/historical state.
- Source-of-truth contract: canonical DB-free env matrix, compose service env,
  node-22 timer/env/unit receipts, scheduler evidence `database_url_configured`
  and `lock_type=file`, and source/cycle live receipts.
- Producers: compose/static checker, local no-DB tests, node-22 deployment
  commands, scheduler evidence writer, file journal/state-index providers, and
  live GFS/IFS scheduler passes.
- Validators/preflight: static compose/env checker, source-trust preflight,
  DB-free scheduler runtime preflight, Slurm preflight, and OpenSpec validate.
- Failure paths/rollback: missing/forbidden DB-free env, stale migration,
  raw handoff missing/invalid, Slurm preflight blockers, no live GFS/IFS cycle,
  or timer/env backup missing all block merge and keep `:55433` online.
- Evidence/audit/readiness: focused local no-DB pytest, static compose/env PASS,
  ruff, OpenSpec strict validation, bounded scheduler pass receipt, live GFS
  receipt, live IFS receipt, and rollback receipt.
- Regression rows:
  - Checked-in compute compose renders `compute-api` with `DATABASE_URL` but
    renders `scheduler-once` without it.
  - Adding `DATABASE_URL` to DB-free `scheduler-once` fails static validation.
  - Changing any scheduler backend selector away from `file` fails static
    validation.
  - Empty registry/readiness/journal/state-index paths fail static validation.
  - Local DB-free scheduler pass uses file lock/registry/readiness/journal/state
    index with DB-backed factories patched to fail.
  - DB-free Slurm preflight with missing `DATABASE_URL` does not emit
    `SLURM_PREFLIGHT_DATABASE_URL_MISSING`.
  - Live GFS and IFS cycles reach `convert-or-later` with node-27 raw handoff,
    file-backed scheduler state, no scheduler PostgreSQL, and no
    `download_source_cycle` submission.

## Issue #837 Fixture

Fixture level: expanded/live-deployment
Repair intensity: high

Mandatory expanded triggers:

- The change stops a production rollback database listener and must preserve a
  restorable archive with checksums.
- Operator-facing docs and env examples decide whether node-22 `:55433` is
  treated as active business DB, archive, or rollback state.
- Post-stop scheduler evidence must prove the DB-free runtime still plans with
  file backends and no scheduler `DATABASE_URL`.

Must preserve:

- Active NHMS business DB ownership remains node-27 `:55432`.
- Node-22 scheduler runtime remains DB-free; old `compute.env` credentials are
  not reintroduced into scheduler env.
- The archive and rollback commands contain no secrets.
- Historical receipts and governance inventories may keep old `:55433`
  references when clearly marked historical, archived, rollback, or
  compatibility context.

Must add/change:

- A dated retirement receipt records pre-stop listener/session attribution,
  archive path, checksums, redacted env backups, stop evidence, rollback
  commands, and post-stop health checks.
- Active topology/runbook/env guidance states node-22 `:55433` is historical
  do-not-connect archived/stopped rollback-only state.
- OpenSpec tasks 7.1-7.5 are closed with exact evidence paths.

Risk packs considered for #837:

- Public API / CLI / script entry: selected - operator commands stop Docker,
  run scheduler dry-run, and probe service health.
- Config / project setup: selected - env examples and systemd/runtime state
  must not point scheduler back at the historical do-not-connect archived/stopped
  `:55433` rollback listener.
- File IO / path safety / overwrite: selected - archive, checksum, and evidence
  paths live on shared NFS and node-22 workspace roots.
- Auth / permissions / secrets: selected - env backups are redacted and
  rollback commands omit credentials.
- Resource limits / discovery: selected - archive verification uses checksums
  and `pg_restore -l` instead of unbounded manual inspection.
- Legacy compatibility / rollback: selected - `nhms-22-e2e-db` remains a stopped
  rollback container with restart policy `no`.
- Error handling / rollback / partial outputs: selected - failed archive,
  checksum, stop, empty-listener, or health checks block completion.
- Documentation / migration notes: selected - active docs/env examples must
  describe `:55433` as historical do-not-connect archived/stopped rollback-only
  state.

Invariant Matrix:

- Governing invariant: after #837, node-22 `:55433` is not an active scheduler
  or business database dependency; it is historical do-not-connect
  archived/stopped rollback-only state, either absent from listeners or
  deliberately restarted only as an archived rollback target.
- Source-of-truth contract: the 2026-06-29 retirement receipt, archive
  `SHA256SUMS`, post-stop `ss_55433_after`, DB-free scheduler evidence
  `database_url_configured=false` and `lock_type=file`, and active topology docs.
- Producers: Docker stop command, pg_dump archive, checksum verification,
  scheduler dry-run evidence writer, health probes, and documentation updates.
- Validators/preflight: pre-stop listener/session attribution, `sha256sum -c`,
  `pg_restore -l`, post-stop `ss`, compute API health, Slurm gateway health,
  OpenSpec validation, and topology guard tests.
- Failure paths/rollback/stale state: missing archive/checksum/env backup,
  listener still present, scheduler evidence showing DB dependency, failed
  health probes, or active docs presenting `:55433` as business DB all block
  completion.
- Evidence/audit/readiness: dated receipt, remote evidence root, archive root,
  local OpenSpec strict validation, markdown lint, and static topology guard.
- Regression rows:
  - Pre-stop listener and sessions identify `nhms-22-e2e-db` as historical
    rollback DB, not scheduler runtime.
  - Archive root contains dump directory, globals without role passwords,
    `SHA256SUMS`, and `sha256sum -c` passes.
  - Post-stop `ss -ltnp | grep 55433` is empty and Docker state is exited with
    restart policy `no`.
  - Post-stop scheduler dry-run has no `DATABASE_URL`, all scheduler backends
    `file`, `lock_type=file`, root preflight ready, and no mutation.
  - Compute API and `/api/v1/slurm/health` pass after stop.
  - Active docs/env examples mark `:55433` only as archived/stopped rollback
    context.

## Issue #1081 Fixture

Fixture level: expanded/high
Repair intensity: high

Mandatory expanded triggers:

- The change ties the registry declared-cutover contract (landed by #1080) to
  scheduler cold-start policy, state-index continuity, and backfill selection
  in one transition. A partial implementation would either (a) admit a
  declaration-less cutover, or (b) let old-generation history block a
  legitimate declared cold start, or (c) submit a successor cycle whose own
  readiness policy will inevitably block it — each of which is a governance
  hole equivalent to the one #1080 just closed.
- Scheduler decides which Slurm workloads run. Getting the cutover consumer
  wrong causes wasted compute, silently-invalid state chains, or missed
  raw-manifest predecessor cycles.
- The old-generation state entries must remain audit-visible without being
  treated as current-generation history — a boundary that only becomes
  visible during the transition and must not be inferred from local reads.

Must preserve:

- Existing warm-start strictness within one generation: an unchanged model
  generation still requires an exact predecessor checkpoint; no fallback to
  latest-usable-state.
- Old-generation state objects are immutable and remain readable for audit /
  rollback.
- Manifest-last write ordering and DB-free constraint from §§1-6.
- `NHMS_REQUIRE_FORECAST_WARM_START=false` continues to affect only optional
  warm-start hints and NEVER admits a declaration-less cutover, missing
  predecessor, or wrong-generation checkpoint.

Must add / change:

- Scheduler planning consumes the `nhms.scheduler.registry_package_cutover.v1`
  declaration channel emitted by the registry publisher (schema landed by
  #1080). Each declared cutover is bound to a target `(model_id, old_checksum,
  new_checksum, generation, effective_cycle_utc, transition_mode)` before
  candidate submission.
- A `generation` token derived deterministically from the registry
  `package_checksum` threads through candidate, state lookup, backfill
  selection, and evidence.
- Cold-start is now decidable by a single `transition_decision` enum over the
  full matrix: `warm_continue`, `cold_new_model`, `cold_declared_cutover`, and
  five block reasons (`block_predecessor_pending`, `block_declaration_missing`,
  `block_declaration_stale`, `block_cold_start_out_of_window`,
  `block_wrong_generation`).
- Backfill is predecessor-aware within one generation: a successor cycle is
  never attempted while its raw-available predecessor is missing.
- Stale journal / output entries whose lineage lacks a required intermediate
  predecessor OR belongs to a different generation are quarantined from
  canonical readiness scoring.
- Evidence records every decision path with model_id, generation,
  transition_decision, selected_predecessor identity, cold-start reason, and
  any typed block reason. Bounded — no unbounded log/state inlining.

Risk packs considered for #1081:

- State machine / invariants: SELECTED — the transition is a state-space
  extension (`warm_continue` -> `cold_declared_cutover` / `cold_new_model` /
  block reasons), and the boundary between old-generation audit history and
  current-generation warm-start history must be a strict invariant.
- Registry-boundary contract: SELECTED — consumes the declaration schema
  landed by #1080; a mismatch in old/new_checksum/generation semantics is a
  cross-PR governance hole.
- Concurrency / bounded submission: SELECTED — retries and concurrent bounded
  submission across a cutover boundary must not corrupt generation lineage.
- Predecessor / backfill selection: SELECTED — the 2026070600 <-> 2026070612
  regression documented in the issue is a real cycle-selector defect that
  scheduler must not repeat.
- Legacy compatibility / rollback: SELECTED — old-generation state history
  must remain audit-visible; nothing in this change deletes it.
- Public API / CLI / script entry: NOT SELECTED — no CLI surface changes.
- Config / project setup: NOT SELECTED — no new env variables introduced
  beyond the declaration path already added by #1080.
- File IO / path safety / overwrite: NOT SELECTED — no new file writes
  outside the existing journal / state-index / candidate evidence sinks.
- Auth / permissions / secrets: NOT SELECTED — DB-free; no credentials
  crossed.
- Resource limits / discovery: partially SELECTED — evidence must remain
  bounded.

Invariant Matrix:

- Governing invariant: within one model generation, forecast state continuity
  is strict (exact predecessor checkpoint required); across an explicit
  declared package cutover, cold start is admitted ONLY at the declared
  `effective_cycle_utc`, and old-generation history is audit-visible but does
  NOT count as current-generation warm-start history.
- Source-of-truth contract: the registry `manifest-last.json`
  `package_checksum` plus the `nhms.scheduler.registry_package_cutover.v1`
  declaration are jointly authoritative for the generation identity and the
  effective cold-start cycle.
- Producers: registry publisher writes declaration + manifest; scheduler
  consumer derives generation token, matches declaration, and emits candidate
  + evidence.
- Validators / preflight: declaration `(model_id, old_checksum, new_checksum,
  generation, effective_cycle_utc)` matches current registry state; candidate
  cycle within the declared window; predecessor identity matches required
  generation; journal lineage matches required predecessors.
- Failure paths / rollback / stale state: any of
  {declaration_missing, declaration_stale, cold_start_out_of_window,
  wrong_generation, predecessor_pending} blocks submission with a typed
  reason; a `predecessor_pending` is retryable, others require operator
  action.
- Evidence / audit / readiness: candidate evidence records generation,
  transition_decision, selected_predecessor, cold-start reason, and typed
  block reason (bounded); journal quarantine of stale-lineage entries is
  itself an audit event; Slurm-oracle dry-run proves the cycle selector
  respects the window.
- Regression rows:
  - 13 continuing generation-unchanged models select `warm_continue` with
    exact predecessor checkpoint.
  - 6 new models with no prior history cold-start once at earliest selected
    cycle (`cold_new_model`).
  - Existing model + declared cutover admits `cold_declared_cutover` ONLY
    at `effective_cycle_utc`; earlier cycles keep old-generation warm
    requirement; later cycles require new-generation exact predecessor.
  - Old-generation state objects remain readable for audit but never satisfy
    current-generation warm-start.
  - With raw manifests for 2026070600 AND 2026070612 and no 2026070600
    journal, scheduler selects 2026070600 first; 2026070612 is deferred with
    `predecessor_pending`, not submitted or permanently failed.
  - Completed / failed entries from an invalid later-cycle lineage do not
    suppress correct backfill.
  - `NHMS_REQUIRE_FORECAST_WARM_START=false` never admits declaration-less
    cutover, missing predecessor, or wrong-generation checkpoint.
  - Concurrent bounded submission across a cutover boundary preserves
    generation lineage per candidate.

Boundary-Surface Checklist:

- Registry consumer: reads declaration channel; matches (model_id, old_checksum,
  new_checksum, generation); rejects mismatch.
- Candidate construction: derives generation token; carries transition_decision
  and selected_predecessor forward.
- State-index lookup: filters by generation; refuses wrong-generation
  checkpoint as usable.
- Backfill selector: predecessor-aware within one generation; refuses
  cross-generation predecessor with `generation_mismatch`.
- Journal quarantine: stale-lineage entries are audit-readable but excluded
  from canonical readiness scoring.
- Evidence sink: every decision path (8 in enum: 3 admit + 5 block) captured
  with bounded identity fields.
- Env override: `NHMS_REQUIRE_FORECAST_WARM_START=false` still gated at
  hint-only granularity.
- Slurm oracle: dry-run proves the cycle selector defers `predecessor_pending`
  and refuses cold-start outside window.

Decisions (D8):

D8.1 Declaration loading is at scheduler-planning time (not runtime submit
time), so a mid-plan declaration change cannot corrupt an in-flight candidate.

D8.2 Generation token = deterministic function of `package_checksum` (short
form recommended: first 12 hex chars, mirroring #1080's `manifest-<12hex>`
convention). Scheduler evidence records the full checksum + short generation.
Invariant: the declaration's top-level `generation` field (from
`nhms.scheduler.registry_package_cutover.v1`) MUST equal
`derived_generation(entry.new_checksum)` for the declaration to bind. A
mismatch is a `block_declaration_stale` — the 12-hex short form derived from
`new_checksum` is the authoritative match key.

D8.3 A `cold_declared_cutover` candidate at `effective_cycle_utc` explicitly
IGNORES old-generation state history for warm-start; old-generation entries
remain readable but not usable-as-predecessor.

D8.4 A cycle earlier than `effective_cycle_utc` uses the OLD generation's
warm-start rules — it is not a cutover-cold-start, it is a legacy cycle. This
preserves audit and rollback semantics.

D8.5 Predecessor cadence: 12h for GFS/IFS 00/12; if a future source uses 6h,
the same predecessor-aware rule applies with cadence = 6h. Cadence is derived
from source metadata, not hardcoded.

D8.6 A `predecessor_pending` block is transient (retryable next scheduler
tick). All other block reasons are terminal until operator action (a new
declaration, an operator ack, or a manual state repair).

D8.7 Journal quarantine is applied at readiness-scoring time. The journal
itself is immutable; quarantined entries stay readable via
`scheduler_state_snapshot.jsonl` and are excluded ONLY from the readiness
score that drives candidate selection.

D8.8 The eight `transition_decision` enum values — three admit (`warm_continue`,
`cold_new_model`, `cold_declared_cutover`) and five block (`block_predecessor_pending`,
`block_declaration_missing`, `block_declaration_stale`,
`block_cold_start_out_of_window`, `block_wrong_generation`) — are the closed
contract. Adding a new enum value is an OpenSpec change, not a scheduler-local
decision. Each `block_*` enum value maps 1:1 to a typed-reason string used in
evidence: `block_declaration_missing` → `registry_cutover_declaration_missing`,
`block_declaration_stale` → `registry_cutover_declaration_stale`,
`block_cold_start_out_of_window` → `registry_cutover_cold_start_out_of_window`,
`block_predecessor_pending` → `state_snapshot_index_prior_checkpoint_missing_after_history`,
`block_wrong_generation` → `state_snapshot_index_generation_mismatch`. The
`transition_decision` is the coarse gate; the typed reason surfaces the
specific field that failed.

## Issue #1112 Fixture

Fixture level: expanded/live-deployment
Repair intensity: high

Mandatory expanded triggers:

- The failure sits in the accepted-submit/bind crash window: Slurm may own a
  live 18-task array while the DB-free file journal still has no
  `slurm_job_id`. Treating a transport timeout as rejection can either create
  a duplicate array or permanently suppress a successful one.
- Reconciliation mutates durable cohort, candidate, hydro-run, and array-task
  state. Incorrect projection can clear real task failures or poison all
  successful siblings with one stale cohort-level Gateway error.
- `restart_stage` is a scheduler state-machine input. Losing
  `state_save_qc` during grouping or run-context construction repeats native
  SHUD forecast work and breaks verified warm-state continuity.
- The node-22 live oracle changes production scheduler behavior and must prove
  recovery on shared NFS while `DATABASE_URL` remains absent.

Must preserve:

- Reservation/comment identity is durable before every Gateway submission;
  PostgreSQL and node-27 ingest/display remain outside this repair.
- Exact identity is required before bind, cancellation, task projection, or
  retry eligibility. An unverified or multiply matched Slurm job is never
  adopted or cancelled.
- File-journal append-first, per-cycle locking, materialized-latest ordering,
  bounded reads, credential-safe evidence, and immutable operational receipts
  remain unchanged.
- Successful candidate siblings are never recomputed because another array
  task failed; failed tasks are never relabelled successful.
- Strict warm-start and model-generation gates remain fail closed.

Must add/change:

- Persist one forecast-cohort reservation carrying the exact idempotency key and Slurm
  comment before the Gateway call. A transport timeout after submission is
  recorded as `submit_result_ambiguous`, not as a permanent hydro/candidate
  failure.
- On restart, reconcile every reserved-unbound DB-free cohort by exact comment:
  one identity binds the array; zero identities retain a bounded reconciling
  state during the absence window and permit one idempotent submit attempt only
  after that window; multiple identities, comment mismatch, or task/cohort
  identity mismatch fail closed; unavailable accounting retains state for a
  later retry.
- Reconcile terminal array tasks against the reserved cohort member map and
  project status per candidate. A successful forecast task clears only its
  stale `SLURM_GATEWAY_UNAVAILABLE` hydro failure and resumes at
  `state_save_qc`; a failed task remains failed/retry-eligible according to its
  own identity.
- Carry canonical `restart_stage` from candidate evidence through
  restart-compatible grouping, deterministic cohort identity, basin manifest,
  run context, and stage selection. Mixed stages form distinct durable
  cohorts; `state_save_qc` cohorts cannot submit `run_shud_forecast_array`.
- Emit bounded evidence containing reconciliation source, exact matched Slurm
  identity, decision, candidate/task outcome, restart stage, and
  `native_shud_resubmitted`.
- Preserve pre-#1112 failure/retry behavior for non-forecast array stages;
  accepted-submit member projection in this issue is limited to the canonical
  forecast stage family.

Persisted reconciliation/evidence contract for #1112:

- `submit_outcome` is one of `accepted`, `submit_result_ambiguous`, or
  `rejected`.
- `reconciliation_source` is `slurm_exact_comment` when Slurm accounting is the
  authority.
- `reconciliation_decision` is one of `matched_bound`, `absence_deferred`,
  `absence_retry_permitted`, `multiple_matches_blocked`,
  `identity_mismatch_blocked`, or `accounting_unavailable`.
- `matched_slurm_job_id` is present only after one exact identity is proven;
  absence and blocked decisions persist `null`.
- Candidate projections persist `array_task_id`, `array_task_outcome` in
  `succeeded|failed|unverified`, `restart_stage`, and
  `native_shud_resubmitted`.
- Comment/accounting discovery is capped before materialization; an over-limit
  result maps to `multiple_matches_blocked` with a count/category only, not an
  unbounded row list.

Risk packs considered for #1112:

- Public API / CLI / script entry: selected - `ProductionScheduler.run_once`,
  production scheduler service/timer, and Slurm submission are active entries.
- Config / project setup: selected - reconciliation/absence windows and
  scheduler/Gateway runtime settings govern duplicate-prevention behavior.
- File IO / path safety / overwrite: selected - the file journal is shared-NFS
  durable truth and must preserve append-first/atomic materialization ordering.
- Schema / columns / units / field names: selected - reservation, cohort member,
  reconciliation, restart-stage, and task-result evidence are persisted public
  contracts.
- Auth / permissions / secrets: selected - the repair must remain DB-free and
  keep Gateway/runtime roots and credentials out of evidence.
- Concurrency / shared state / ordering: selected - timeout, restart, concurrent
  scheduler pass, bind, and task projection race on one cohort identity.
- Resource limits / large input / discovery: selected - comment/accounting
  queries, evidence, and the 18-task GFS/IFS matrix must remain bounded.
- Legacy compatibility / examples: selected - generic repository reconcile and
  non-DB-free callers retain their current contract.
- Error handling / rollback / partial outputs: selected - zero, unique,
  multiple, mismatch, accounting-unavailable, partial failure, and process
  restart are first-class branches.
- Release / packaging / dependency compatibility: not selected - no dependency
  or package-format change is required.
- Documentation / migration notes: selected - node-22 live injection and
  rollback receipts are merge evidence.
- Geospatial / CRS / basin geometry: not selected - no geometry is read or
  written.
- Hydro-met time series / forcing windows: not selected - source/cycle identity
  is preserved but forcing data semantics do not change.
- SHUD numerical runtime / conservation / NaN: selected only at dispatch
  boundary - native forecast must not be repeated for a downstream restart;
  solver numerics are unchanged.
- PostGIS / TimescaleDB domain behavior: not selected - node-22 remains DB-free.
- Slurm production lifecycle / mock-vs-real parity: selected - exact comment
  adoption, array task accounting, no duplicate/cancel, and live timeout
  injection are the core repair.
- External hydro-met providers / snapshot reproducibility: not selected - no
  provider acquisition changes.
- Run manifest / QC provenance: selected - recovered forecast success must
  retain lineage and continue at state-save/QC.
- Published NHMS artifacts / display identity: not selected - node-27 products
  are outside the PR.

Boundary-surface checklist:

- Producers: cohort construction and reservation write the member map,
  idempotency key, exact Slurm comment, restart stage, and submission-attempt
  evidence before the external call through
  `scheduler_execution.execute_candidate_cohort()` and
  `chain_stage_execution.submit_array_stage()`.
- Validators/preflight: exact-comment reconcile validates one master identity,
  source/cycle/stage/cohort membership, array task identity, and accounting
  authority before mutation through
  `reconcile.reconcile_reserved_unbound_jobs()` and the stage/accounting
  identity validators it calls.
- Storage/cache/query: `FileOrchestrationJournalRepository` append-only records,
  `reserve_pipeline_job()`, `bind_pipeline_job_reservation()`, job/hydro status
  updates, direct job snapshots, latest views, pipeline events, and bounded
  comment/accounting results.
- Public entrypoints: `ProductionScheduler.run_once`, restart reconcile, cohort
  execution, and `ForecastOrchestrator.orchestrate_cycle`.
- External boundary: fake Gateway/Slurm injects acceptance followed by response
  timeout; real node-22 `sbatch`/`sacct` is the live oracle.
- Downstream consumers: candidate-state decision, permanent-failure guard,
  `scheduler_execution.restart_compatible_candidate_cohorts()`,
  `candidate_restart_stage()`, basin manifest/run context construction,
  `chain_forecast_execution` stage dispatch, state-save/QC, and scheduler
  candidate-state/evidence consumers.
- Failure/rollback/stale state: zero/unique/multiple/mismatch/unavailable
  accounting, partial tasks, process restart, stale Gateway failure, and an
  existing accepted array with no durable bind.
- Unchanged boundaries: node-27 ingest/display, PostgreSQL repositories, forcing
  values, SHUD numerics, and published product identity.

Invariant Matrix:

- Governing invariant: for one deterministic source/cycle/restart-stage/member
  forecast-cohort identity, at most one Slurm array may be submitted or adopted; an
  ambiguous accepted-submit outcome remains non-terminal until exact
  reconciliation proves ownership or a bounded, authoritative absence permits
  exactly one idempotent retry.
- Durable identity contract: source, cycle, stage, deterministic cohort run ID,
  ordered member candidate/task identities, idempotency key, exact Slurm
  comment, submission attempt, reconciliation decision, and bound master job
  ID.
- Durability contract: a file-journal write may return success only after the
  replaced file and its directory entry are durably committed and the parent
  identity remains the one validated before replacement. Any indeterminate
  durability result fails closed before an external Gateway call.
- Submission-state contract: the durable reservation may temporarily have no
  `submit_outcome` only before the Gateway result is recorded. Restart recovery
  atomically classifies that state as `submit_result_ambiguous` before writing a
  reconciliation decision. An explicit Gateway rejection records the normative
  `rejected` outcome and terminalizes the affected hydro rows; neither state may
  make the journal validator fail while handling the original failure.
- Submit-disposition contract: only a response that proves the external submit
  was rejected before acceptance may write `rejected`. Transport loss,
  post-`sbatch` parse failure, malformed success bodies, and unknown Gateway
  failures are acceptance-unknown and enter exact-comment reconciliation.
- Transition-truth contract: a Gateway timeout proves only
  `submit_result_ambiguous`; it clears/leaves absent the reconciliation source,
  decision, and matched ID. Those fields are written only by a completed
  accounting query. Common evidence fields such as `submit_outcome` are
  validated before master/candidate specialization so neither row kind can
  bypass the closed enum.
- Attempt-boundary contract: reclaiming a retry initializes the new submission
  attempt atomically while holding the cycle lock. Before its Gateway result,
  the new attempt has no `submit_outcome` and no reconciliation source,
  decision, or matched ID; evidence proved for the prior attempt cannot cross
  this boundary, including when the process stops immediately after reclaim.
  Every versioned master carries one valid aware-UTC immutable
  `submission_attempt_started_at`; reclaim creates the next anchor while
  holding the lock and never trusts a lock-external request timestamp.
- Attempt-CAS contract: timeout/accounting transitions compare the durable
  submission attempt and expected reserved-unbound state under the cycle lock.
  Normal Gateway success and accounting adoption atomically bind the accepted
  Slurm ID with their complete evidence tuple; a same-ID repeat is idempotent,
  while a different-ID collision never overwrites the winner.
- Rejection-atomicity contract: a proven rejection commits the attempt master,
  every matching member hydro failure, and required event evidence as one
  cycle-lock journal batch. A failed batch leaves the reservation recoverable;
  it cannot leave a terminal master with active member state.
- Accounting-proof contract: an owner-scoped match identifies the bind
  candidate but is not by itself proof of global uniqueness, and an owner-scoped
  zero result is not authoritative global absence. Any bounded exact-comment
  collision with a different owner/account is `identity_mismatch_blocked`;
  binding requires one globally unique owned match, while retry eligibility
  requires a bounded, authoritative proof that no exact-comment job exists under
  any ownership.
- Accounting-coverage contract: a zero-match result is authoritative only when
  the frozen query window covers the current
  `submission_attempt_started_at` through the query end. An older attempt or a
  legacy/custom adapter that cannot prove that coverage remains
  `accounting_unavailable` and cannot release retry permission. The reconcile
  consumer recalculates coverage from valid aware start/end bounds and the
  durable anchor; an adapter's completeness boolean alone has no authority.
- Accounting-authority contract: successful command execution is not by itself
  global visibility. Runtime preflight proves the scheduler principal's Slurm
  accounting visibility (including job privacy), otherwise zero-match evidence
  remains unavailable. Discovery pages a bounded time range before byte/row
  materialization, freezes one page-window snapshot for the whole reconcile
  session, and aggregates only the bounded exact-comment matches needed to
  prove zero, one, or multiple results at the supported 256-member cadence.
- Accounting-saturation contract: raw page byte/row saturation is not evidence
  of multiple exact-comment matches. It remains fail-closed
  `accounting_unavailable` with a closed, bounded reason class; only an indexed
  proof of multiple exact matches becomes `multiple_matches_blocked`.
- Independent-runtime contract: because runtime member rows are durably prepared
  before the Gateway call, a pre-outcome reservation with no runtime rows cannot
  have produced an accepted array. A later exact-comment match without those
  rows is unverifiable and remains identity-mismatch blocked; the pre-outcome
  recovery allowance applies to safe ambiguity/absence handling, not to
  weakening bind identity.
- Restart invariant: the earliest incomplete canonical stage is candidate
  state. Grouping never lowers it, mixed stages never share a cohort, and a
  `state_save_qc` cohort cannot enter native forecast.
- Candidate outcome invariant: terminal array results are projected only to the
  exact member identity; successful members clear stale transport failure and
  advance, failed members remain failed, and absent/unverified members remain
  reconciling.
- Projection-schema invariant: every persisted projection maps by
  `array_task_id` to exactly one canonical durable member and repeats that
  member's candidate/run/model identity. Cohort digest and projection mapping
  are validated on outgoing write and journal/latest/direct replay; missing or
  malformed accepted-submit members fail closed instead of falling back to the
  legacy reservation path.
- Evidence invariant: every branch records bounded reconciliation source,
  matched identity or safe absence/mismatch class, decision, restart stage,
  and whether native SHUD was resubmitted; raw comments, local/shared-NFS roots,
  credentials, and unbounded accounting rows never enter public evidence.
- Version/compatibility invariant: the accepted-submit contract has an explicit
  persisted version marker. Marker-free historical cohort-shaped rows remain
  legacy read-only state and never acquire accepted-submit authority or become
  invalid merely because additive fields are absent. Global visibility proof
  is required only for versioned accepted-submit reconciliation.
- Anchor-validation invariant: direct, journal, and latest replay reject a
  versioned master whose attempt anchor is missing, malformed, or naive, while
  marker-free historical rows retain their legacy read contract. Ordinary
  same-attempt updates cannot change the anchor, and retry CAS compares both
  attempt number and anchor.
- Sticky-authority invariant: once persisted, a current-version master remains
  a master independently of mutable stage classification. Ordinary upsert
  cannot change its contract/job/run/cycle/source, stage/job type,
  candidate/idempotency/comment, cohort/digest, ownership, restart/native-SHUD,
  attempt, or anchor identity; only the typed reclaim boundary may advance
  attempt and anchor together under the cycle lock. A current-version row that
  is neither a valid master nor a valid candidate fails closed.
- Typed-transition invariant: ordinary `upsert_pipeline_job` cannot change any
  current-version master authority state, including Slurm binding, status,
  submit outcome, reconciliation tuple/reason, projections, runtime timestamps,
  retry fields, errors, or logs. An exact same-value replay is a zero-write
  read. Accepted bind, ambiguity/reconciliation, proven rejection, retry
  permission, next-attempt reclaim, and terminal projection occur only through
  their typed cycle-lock APIs.
- Closed-enum invariant: task-accounting completeness is represented by
  pipeline status/error/projection fields and never adds values to the six-value
  `reconciliation_decision` contract. Reconciliation API inputs are normalized
  to the same bounded projection allowlist before persistence.
- Resource invariant: both exact-comment discovery and ordinary inflight
  master/task accounting apply byte, row, and time limits before full
  materialization. Model-less cohort truth remains in journal/direct storage
  and is not copied into every model latest view; aggregate model-latest bytes
  grow approximately linearly with cohort size.
- Direct-storage invariant: terminal member projection does not amplify a
  globally scanned direct-job namespace. Direct lookup/index/partition and
  audit retention keep per-cycle reads and restart discovery bounded at the
  supported 256-member cadence without deleting canonical journal truth.
- Accepted-submit lookup invariant: versioned master reserve, transition,
  accepted bind, rejection, retry permission, and accounting adoption resolve
  the deterministic `pipeline_job_id` from exact direct plus its one cycle
  journal. Unrelated historical latest/journal/direct records and their global
  file/record limits cannot block the current attempt.
- Compatibility invariant: generic repository and non-DB-free reconcile callers
  keep their existing inputs/status behavior, while the new cohort/member fields
  are additive for file-journal DB-free execution.
- Provenance invariant: recovered task success preserves source/cycle/model,
  initial-state, output/checkpoint, run-manifest, and QC lineage when clearing a
  stale transport failure; reconcile does not synthesize a replacement run.
- Highest feasible seam under test: drive `ProductionScheduler.run_once()` and
  `ForecastOrchestrator.orchestrate_cycle()` with the real file-journal
  repository and a fake Gateway/Slurm only at the external boundary; repository
  unit tests supplement but do not replace this end-to-end seam.
- Live proof boundary: node-22 bounded response-timeout injection on shared NFS,
  exact `sacct` comment recovery, one array for 18 members, downstream
  copyback/state-save progress, no scheduler `DATABASE_URL`, and a rollback
  receipt.

Regression rows:

- 18-member cohort; Slurm accepts `sbatch`; Gateway response times out -> one
  durable reserved-unbound cohort, `submit_result_ambiguous`, no permanent
  candidate failure, and exactly one Slurm array.
- Process restart; exact comment has one matching array -> bind master ID,
  reconcile tasks, and do not submit or cancel another forecast array.
- Exact comment has zero matches before absence window -> remain reconciling;
  zero matches after the window -> one idempotent retry attempt, with concurrent
  passes unable to produce a second attempt.
- Exact-comment accounting returns more than the bounded match limit ->
  `multiple_matches_blocked` with bounded/redacted evidence and no adoption,
  cancellation, or submission.
- Exact comment has multiple matches, wrong comment, wrong cohort/stage/member
  identity, or accounting is unavailable -> fail closed/retain for retry as
  specified, with no bind, cancel, or submission.
- 18 successful task rows plus stale `SLURM_GATEWAY_UNAVAILABLE` -> each exact
  candidate becomes forecast-successful, stale hydro failure clears, and each
  restarts at `state_save_qc` without native SHUD resubmission.
- Partial terminal array -> successful siblings advance; only failed eligible
  identities remain failed/retryable; no successful sibling is recomputed.
- Recovered `state_save_qc` candidate -> only state-save/QC is submitted.
- Mixed `forecast` and `state_save_qc` candidates -> separate deterministic
  cohort identities and stage submissions.
- GFS and IFS each carry the live 18-model file-journal shape -> no duplicate
  forecast arrays across timeout, restart, and reconciliation.
- Generic repository and non-DB-free reconcile fixtures -> unchanged behavior;
  additive file-journal cohort fields do not become required for legacy rows.
- Non-forecast array Gateway failure or stale row carrying cohort-like fields ->
  preserves its prior stage behavior and never creates forecast projection or
  `state_save_qc` restart evidence.

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

Before #837 stopped the historical do-not-connect `:55433` rollback listener,
rollback meant switching scheduler env back to PostgreSQL-backed mode and
re-enabling the old `DATABASE_URL`. After #837, rollback is an explicit
archived-DB recovery path only: restart the archived/stopped PostgreSQL
container deliberately, verify the listener, record the decision, and stop it
again after the rollback drill unless an operator has explicitly accepted a
temporary rollback window. Every live cutover receipt must include
the exact env backup and archive path used for rollback.
