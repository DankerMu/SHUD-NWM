## 1. Runtime Guard And File Lock

- [x] 1.1 Add canonical DB-free scheduler backend env parsing.
  Evidence floor: scheduler config parses the canonical env matrix from
  `design.md`: state, lock, registry, canonical readiness, journal, state
  index, all required paths, and `NHMS_SCHEDULER_DB_FREE_REQUIRED`.
  Test rows:
  - Input: `NHMS_SCHEDULER_DB_FREE_REQUIRED=true`, every scheduler backend
    selector set to `file`, required manifest/index/journal paths configured
    safely, and no `DATABASE_URL`.
    Expected: config exposes DB-free mode, backend names, and configured path
    fields without raw secrets.
  - Input: DB-free required is absent/false and legacy postgres config is used.
    Expected: legacy config remains valid where existing postgres-mode tests
    expect it.
- [x] 1.2 Fail closed on DB-free mixed backend or scheduler `DATABASE_URL`.
  Evidence floor: DB-free preflight blocks before lock acquisition/submission
  when `DATABASE_URL` is present, any selector is unset/non-file/postgres-like,
  or a required file path is missing/unsafe; evidence records the exact field.
  Test rows:
  - Input: DB-free env plus scheduler `DATABASE_URL`.
    Expected: pre-lock blocker `database_url_forbidden`; no lock acquisition,
    no model discovery, no DB-backed factory, no Slurm submission.
  - Input: each selector one at a time unset, blank, `postgres`, `psycopg`, or
    another non-file value.
    Expected: pre-lock blocker names the exact selector field; no mutation.
  - Input: each required manifest/index/journal path missing, blank, outside
    allowed workspace/object boundary, or unsafe.
    Expected: pre-lock blocker names the exact path field; evidence remains
    bounded and redacted.
- [x] 1.3 Move DB-free scheduler locking to `FileSchedulerLease`.
  Evidence floor: focused local tests show `lock_type=file`; concurrent passes
  cannot both mutate and contention evidence is bounded. Node-22 bounded pass
  proof remains part of deployment task 6.4 and post-stop task 7.4.
  Test rows:
  - Input: valid DB-free config.
    Expected: `_build_scheduler_lease()` returns/uses `FileSchedulerLease` and
    lock evidence has `lock_type=file`.
  - Input: two DB-free scheduler passes compete for the same file lock.
    Expected: at most one acquires mutation rights; the contended pass records
    bounded file-lock contention and submits nothing.
- [x] 1.4 Add DB-free scheduler evidence fields and factory guardrails.
  Evidence floor: evidence records backend names and configured manifest/index
  paths; tests fail if DB-free factories call `PsycopgModelRegistryStore`,
  `PsycopgMetStore`, `PsycopgOrchestratorRepository`,
  `PsycopgStateSnapshotRepository`, DB-backed `_retry_service_from_env`,
  SQLAlchemy `PipelineStore`, DB-backed `ForcingProducer.from_env`, or
  equivalent PostgreSQL paths.
  Test rows:
  - Input: valid DB-free config with every listed DB-backed factory monkeypatch
    set to raise.
    Expected: DB-free construction/preflight still succeeds or returns the
    intended file-mode blocker without invoking any patched DB-backed factory.
  - Input: DB-free pass evidence.
    Expected: evidence includes `database_url_configured=false`, selected
    backend names, canonical DB-free field names, redacted configured paths,
    and no PostgreSQL host/port/advisory-lock/psycopg dependency.
  - Input: non-DB-free legacy mode.
    Expected: existing postgres-backed registry/repository/readiness/reconcile
    reachability remains available where current tests intentionally cover it.

## 2. File Model Registry And Canonical Readiness

- [x] 2.1 Define the production scheduler registry manifest schema.
  Evidence floor: schema validates version, model identity, basin identity,
  package URI, checksum, resource profile, capabilities, segment counts, source
  policy, duplicate IDs, and bounded evidence.
- [x] 2.2 Add a registry publisher for production scheduler manifests.
  Evidence floor: publisher verifies referenced model manifests/checksums,
  writes manifest-last, records checksum/generated_at/source evidence, and can
  produce a real node-22-consumable registry fixture.
- [x] 2.3 Wire scheduler model discovery to file registry in DB-free mode.
  Evidence floor: `ProductionScheduler.from_env()` does not call
  `PsycopgModelRegistryStore.from_env()` in DB-free mode; discovery evidence
  lists selected model IDs and registry checksum.
- [x] 2.4 Define the canonical readiness index schema and publisher.
  Evidence floor: publisher produces schema version, checksum, generated_at,
  source/cycle/model/basin identity, forecast-hour coverage, product counts,
  product URIs, and object existence evidence using authoritative canonical
  product artifacts or node-27 data-plane state.
- [x] 2.5 Wire scheduler canonical readiness to file index in DB-free mode.
  Evidence floor: scheduler uses file/object-store product evidence and
  existing canonical readiness semantics without `PsycopgMetStore`; missing,
  stale, checksum mismatch, object-missing, and identity mismatch cases fail
  closed.
- [x] 2.6 Preserve node-27 raw handoff and no-download fallback semantics.
  Evidence floor: focused tests prove missing/invalid NFS raw blocks, ready raw
  plus canonical-zero starts at `restart_stage=convert`, staging is
  manifest-last, and Slurm requests do not include `download_source_cycle`.

## 3. File Orchestration Journal Contract And Read State

- [x] 3.1 Define the DB-free orchestration repository method contract.
  Evidence floor: contract lists scheduler-required read methods for active
  orchestration, active pipeline, completed pipeline, active Slurm jobs,
  candidate state, model/forcing context reads, and query helpers. Lifecycle,
  pipeline write, retry/cancel/status-sync write methods are named in the
  interface but explicitly fail with `FILE_JOURNAL_WRITE_NOT_IMPLEMENTED` in
  this read-side slice until later write-side tasks land.
- [x] 3.2 Define append-only journal and materialized latest schemas.
  Evidence floor: schemas cover candidate/job/event state, active Slurm job
  evidence, reservation, binding, retry attempt, Slurm job ID, stage, status,
  error code, redacted runtime roots, sequence IDs, and replay metadata.
- [x] 3.3 Implement file-backed read-side scheduler state.
  Evidence floor: file repository answers active orchestration, active
  pipeline, completed pipeline, active Slurm jobs, candidate state, and
  model/forcing context reads without PostgreSQL. Lifecycle and pipeline write
  methods remain out of scope for this slice and fail-not-implemented. Trusted
  replay also validates nested latest/journal identity, strict direct
  `pipeline-jobs` schema, sidecar `pipeline-events` source/cycle schema,
  no-follow scanned entries, file/depth/JSON complexity limits, source alias
  canonicalization, envelope/payload/run identity consistency, append-only
  sequence ordering, valid direct-only `pipeline-jobs` snapshots, directory
  entry bounds, and blocked query redaction before state is trusted.
- [x] 3.4 Add read-side repository contract tests.
  Evidence floor: shared fixtures cover duplicate prevention, active Slurm job
  skip/cancel evidence, completed skip, candidate identity filtering, and
  model/forcing context reads. Regression fixtures also cover malformed
  embedded rows, invalid cycle timestamps, newer terminal direct job masking,
  sidecar-event schema/cycle mismatch, resource limits, DB-compatible
  equal-timestamp job/event tie-breaks, scheduler-level active Slurm evidence,
  public-safe blocked query sentinels, source alias/casing consistency,
  envelope/payload/run mismatch blockers, append-only sequence precedence,
  valid direct-only pipeline-job reads, JSONL record limits, unknown record
  types, non-matching directory-entry limits, and direct
  `FILE_JOURNAL_WRITE_NOT_IMPLEMENTED` method evidence. Write-side lifecycle,
  retry, and migration behavior is intentionally reserved for section 4 tasks.

## 4. File Orchestration Journal Writes, Retry, And Migration

- [ ] 4.1 Implement file-backed lifecycle and pipeline writes.
  Evidence floor: file repository supports `ensure_forecast_cycle`,
  `create_hydro_run`, hydro/forecast status updates, reservation/bind,
  pipeline job upsert/status update, and pipeline event insertion with
  atomic writes.
- [ ] 4.2 Replace DB-backed retry service in DB-free mode.
  Evidence floor: DB-free orchestrator construction does not call
  `_retry_service_from_env` or `PipelineStore`; retry attempts, retry-limit
  exhaustion, manual repair, and permanent-failure state are represented in the
  file journal.
- [ ] 4.3 Add historical scheduler-state export/import migration.
  Evidence floor: exporter reads active/completed/candidate/job/event/retry and
  permanent-failure rows from node-22 `:55433`, writes append-only journal
  events, records cutoff time, row counts, checksums, replay status, and stale
  `download_source_cycle` supersession evidence.
- [ ] 4.4 Add write-side and migration contract tests.
  Evidence floor: fixtures for active, completed, permanent failure, manual
  repair, retry exhaustion, stale `download_source_cycle`, and migrated journal
  replay produce decisions equivalent to DB-backed state.
- [ ] 4.5 Wire scheduler submission path to file journal in DB-free mode.
  Evidence floor: fake Slurm submission with no `DATABASE_URL` writes file
  reservation/job/event evidence and does not call
  `PsycopgOrchestratorRepository.from_env()`.

## 5. File State Snapshot Index And DB-Free State Save

- [ ] 5.1 Define the file-backed state snapshot index contract.
  Evidence floor: index maps `model_id + source_id + valid_time` to usable
  state URI, checksum, source identity, schema version, generated_at, and
  object existence evidence.
- [ ] 5.2 Add exact-match strict warm-start lookup.
  Evidence floor: exact successor checkpoint succeeds; missing, stale, checksum
  mismatch, wrong source/model/time, and unusable state fail closed without
  latest-state fallback or `PsycopgStateSnapshotRepository`.
- [ ] 5.3 Make `state_save_qc` produce DB-free state-index records.
  Evidence floor: state-save command runs without `DATABASE_URL`, writes or
  stages index records with checksum/identity evidence, and does not instantiate
  `StateRunRepository.from_env()` or `PsycopgStateSnapshotRepository`.
- [ ] 5.4 Add scheduler warm-start integration tests.
  Evidence floor: scheduler candidate construction carries file-index state
  evidence and blocks candidates when strict state is unavailable.

## 6. DB-Free Runtime Integration And Deployment Compatibility

- [x] 6.1 Update DB-free Slurm preflight policy.
  Evidence floor: with DB-free mode and Slurm enabled, missing `DATABASE_URL`
  does not produce `SLURM_PREFLIGHT_DATABASE_URL_MISSING`; non-DB root,
  manifest, secret, and template safety checks still run.
- [ ] 6.2 Update compute deployment entrypoints for DB-free scheduler mode.
  Evidence floor: `infra/env/compute.example`, DB-free env template/runbook,
  systemd guidance, and `infra/compose.compute.yml` support scheduler runtime
  without mandatory `DATABASE_URL` while preserving DB requirements for
  non-DB-free lanes.
- [ ] 6.3 Run local focused no-DB integration tests.
  Evidence floor: with `DATABASE_URL` absent, scheduler planning and fake
  submission use file lock, registry, readiness, raw handoff, journal, retry,
  and state index; static guardrails prove no psycopg factories are called.
- [ ] 6.4 Freeze node-22 scheduler timer and deploy DB-free env with `:55433`
  still online.
  Evidence floor: receipt records stopped/frozen timer state, env/unit backup,
  DB-free env, migration artifacts, rollback commands, and a bounded scheduler
  pass with no scheduler `DATABASE_URL` and `lock_type=file`.
- [ ] 6.5 Observe live GFS and IFS cycles through `convert-or-later`.
  Evidence floor: one GFS and one IFS cycle use node-27 raw, node-22 file-backed
  scheduler state, downstream Slurm submission, no scheduler PostgreSQL, and no
  `download_source_cycle` submission.

## 7. Archive And Stop Node-22 Historical PostgreSQL

- [ ] 7.1 Capture pre-stop listener and session attribution.
  Evidence floor: receipt records `ss -ltnp`, PID, process owner, unit/service
  metadata where available, command line, active client/session snapshot, and
  proof `:55433` is historical rollback state rather than scheduler-owned.
- [ ] 7.2 Archive node-22 `:55433`.
  Evidence floor: receipt records dump/archive path, checksum, service/unit
  metadata, env backup, process owner, listener snapshot, and rollback commands
  without secrets.
- [ ] 7.3 Stop and disable node-22 historical PostgreSQL.
  Evidence floor: stop action is performed by the owning account or an
  administrator; `ss -ltnp | grep 55433` is empty.
- [ ] 7.4 Run post-stop scheduler and service health verification.
  Evidence floor: bounded no-DB scheduler pass succeeds, compute API and Slurm
  gateway are healthy, and latest evidence still shows `lock_type=file`.
- [ ] 7.5 Update topology guardrails and receipts.
  Evidence floor: active docs/env examples no longer present node-22 `:55433`
  as business DB; historical references are explicitly marked archive/rollback.
