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
   performed under durable per-cycle file locks, commit journal truth before
   direct cache snapshots, and use temporary files plus atomic rename.

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
- File journal write side, retry service replacement, historical migration, and
  live node-22 deployment remain later issue scope.
- Non-DB-free postgres repository behavior and public scheduler facade imports
  remain stable.
- DB-free default mutation still blocks until the file journal write side lands.

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
- Write/delete/overwrite surfaces: none in #833; task 4 replaces the
  temporary fail-not-implemented write methods with file-journal writes.
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
- Producers: #834 write side and historical migration; #833 tests created
  read-side fixtures only.
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
