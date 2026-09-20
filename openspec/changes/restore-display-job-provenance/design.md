## Context

The node-22 active scheduler uses `/scratch/frd_muziyao/nhms-prod/workspace/scheduler/journal`, not the stale roots in compute.env. Its actual EnvironmentFile is compute.scheduler-dbfree.env. Parent read-only observations are under `.workplans/2420/`; no service/env mutation occurred during reconnaissance.

Two current QHH 12Z snapshots contain genuine forecast rows: GFS job `job_fcst_gfs_2026091612_dg_0883c7e9c1006c6fd347df500315e9df_forecast_reconciled_50025_0`; IFS job `job_fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6_forecast_reconciled_49999_23`. Both have real created/updated times and task IDs, but null submitted/started/finished/exit_code/log_uri. These unknown values must stay unknown. Their containing snapshots also carry cycle-scoped convert/forcing/state-save rows: never relabel those rows as model jobs.

The live gateway origin is 127.0.0.1:8090. Direct child log reads have metadata_complete=false and empty logs. Parent-array reads return 38 task entries. Exact QHH matches are the *task entries*, not the parent envelope: GFS parent 50025 / task 0 matches the forecast run; IFS parent 49999 envelope `run_id` is a sibling array member (`dg_0e611766f8d1edb6e99a2ba1892e48e1`) while task 23 is the selected run (`dg_9ccb261a39d51c24f4de9173fb4461b6`, 426 stdout bytes, identity_complete=true, no truncation). Matching the parent envelope `run_id` would fail-closed the live IFS case. Mutable `runs/<run>/logs/shud_stdout.log` has no Slurm task identity and is not an acceptable fallback.

Four display routes have a second defect independent of missing data: the validator requires the complete tuple in one response object, but status/stage/job/log payloads fragment it. `_ok()` currently emits only `request_id`/`status`/`data`; the binder already accepts a top-level envelope `identity` sibling of `data`. No validator weakening is needed. `ops.pipeline_job` is owned by `nhms_ingest_rw` on node-27; `nhms_display_ro` remains readonly.


## Goals / Non-Goals

Goals: truthful current job/log visibility through existing Ops APIs; durable automated transport for subsequent runs; a safe explicit backfill for already-published runs; coherent identity; unchanged control and scientific-data authority boundaries; genuine dual-source readonly/C4 PASS.

Non-goals: retire Ops routes, synthesize jobs from filenames or hydro status, infer missing times/exit codes, copy private journal trees into display, add a second scheduler/DB authority, restore node-22 PostgreSQL, change Slurm scheduling/retry/reconcile decisions, repopulate `pipeline_event` as fabricated history, reparse published hydro facts, alter the held cold-admission/prewarm implementation, introduce generic replication/retry infrastructure, or require cycle-level-only sidecars for C4 (C4 binds the selected forecast run's job+log).

## Change surface / must preserve / must add

Change surface: DB-free journal publication-safe read; `_copyback_stage_run_trees` pre-copyback hook; new publisher/importer; autopipeline catch-up after the already-ingested skip; four Ops success envelopes; OpenAPI/generated types; bounded backfill CLI.

Must preserve: existing `_ok` `data` shapes, request-id, 404/409/422, non-strict browsing, display denial of retry/cancel/Slurm; PostgreSQL-backed `ops.pipeline_job` writers; copyback mutex/cleanup/error ownership and hydro terminal truth; published-artifact log URI form `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out|.err`; `nhms_ingest_rw` / `nhms_display_ro` grants; node-22 3.12 interpreter (no `uv sync`).

Must add: per-run `runs/<run_id>/input/pipeline_jobs.json`; verified task-log publication; idempotent node-27 projection including already-ingested runs; top-level envelope `identity` on the four strict Ops successes.


## Decisions

### 1. Preserve existing DB-backed display reads; project real authority

Use the existing node-27 `ops.pipeline_job` as a derived read model. Alternative direct filesystem queries would replace SQL/filter/pagination behavior across all routes and require an exposed private journal or another display provider; reject that larger boundary change. Retiring routes would contradict the requested unwaived C4 and existing public capability. Keep node-22 journal authority and node-27 ingest-only writes; display remains SELECT-only.

Add a small versioned per-run sidecar, `runs/<run_id>/input/pipeline_jobs.json`, separate from the immutable scientific manifest. That path is already a valid object-store prefix (`runs/{run_id}/input/...`). Its closed schema includes the parent source/cycle/run/model identity, source provenance/version, allowlisted job fields and verified published-log metadata. Use source timestamps, not import time, for job state ordering. Unknown lifecycle fields stay null. Job identity must never change under an existing job_id.

A forecast-run sidecar MUST include the selected run's bound jobs (forecast and any other journal jobs whose `run_id`/`model_id` equal the selected run). It MAY also echo cycle-scoped convert/forcing/state-save rows from the same journal snapshot, but those rows MUST keep their original cycle `run_id` and null `model_id`; they MUST NOT be rewritten onto the enclosing forecast identity. Omitting cycle-scoped rows is allowed; C4 binds the forecast job+log, not convert/forcing. If the same cycle-level `job_id` appears in multiple forecast sidecars, projection is identical-replay.

The producer reads through the existing validated journal machinery. Existing public queries sanitize some object URIs; `[object-uri]` and other redaction sentinels are not valid provenance. If needed add a narrow source-owned publication view exposing only allowlisted fields and validated `published://` URIs, without weakening existing public sanitization or using ad-hoc raw latest-file parsing. Never take journal write locks, mkdir the journal, mutate source rows or call control actions while exporting.

### 2. Attach to the existing terminal copyback lifecycle

`chain_forecast_execution._copyback_stage_run_trees` already runs for parse success or state_save_qc in the DB-free terminal profile, with concrete active run IDs, before existing copyback. Produce each run's sidecar and any verified log publication immediately before the existing run-tree copyback so metadata uses the established transport. Do not add a periodic full-journal scan or a second daemon. Preserve legacy PostgreSQL-mode behavior and the existing copyback mutex/cleanup/error ownership.

Publication is diagnostic display evidence, not hydro terminal truth. A publisher failure MUST be recorded in a bounded per-run summary and MUST NOT raise into `_copyback_stage_run_trees`, skip copyback, roll back scheduler state, or mark valid scientific products unavailable. Once a sidecar/log is durably written, it remains usable; a failed publication is not advertised as complete. The owning copyback stage's hydro/copyback error semantics stay unchanged.

A checked-in bounded backfill CLI invokes the SAME publisher for explicit run IDs and configured roots; no alternate fabricated import path. It must validate all requested identities and source root before writes, record per-run outcomes, and never start/retry/cancel Slurm work. Live apply MUST re-resolve current source identities rather than hard-code the observed 12Z examples.


### 3. Prove log identity before advertising it

Keep a canonical already-published URI only after validating containment and the row's source/cycle/run/job binding. For a journal array task with no published log, fetch its actual parent Slurm array logs with the existing gateway client, cache the parent response once per publication batch, and select exactly the authoritative `array_task_id`. Require parent job ID match, complete binding metadata, `identity_complete=true`, exact run/model equality against the *task entry* (not the parent envelope `run_id`/`model_id`), and consistency between `<master>_<task>` and the source row. Ambiguous/missing/mismatched task entries fail closed for log publication; they do not license a nearby job, the parent envelope, or an arbitrary local file. Respect the gateway's existing bounded read/truncation contract and preserve truncation provenance.

Publish actual returned stream bytes via existing safe atomic artifact primitives. Canonical URI is the existing published-artifact form `published://logs/<source>/<cycle_time>/<run_id>/<job_id>.out` and `.err` when stderr is present — not a new `.<stream>` suffix. Advertise only after verified publication. No master-level log relabeled as a task log, no synthetic explanatory text as evidence, no credential/private path export. A legitimate missing log remains explicitly unavailable; do not create a success log. Parent-array read budgets must not be widened to scan unlimited task logs.


### 4. Separate metadata catch-up from scientific ingest

Node-27 projection validates the complete bounded sidecar and expected run manifest/hydro identity before a transaction. Require known schema, normalized source/cycle consistency, unique job IDs, allowed statuses/types, exact job/run/model bindings, bounded strings/rows/bytes and safe approved published-log paths. No following symlinks, traversal, arbitrary URI/DSN or archive paths. Missing sidecars on historical runs mean unavailable provenance, not synthesized jobs or failure of already-valid scientific data.

Integrate a lightweight projection phase AFTER the already-ingested skip (same catch-up posture as phase 3 publish), covering discovered eligible runs INCLUDING the `done` set. Do not force `register -> forcing -> parse` again or bump hydro.updated_at/coverage state merely to attach jobs. The same importer is callable explicitly for backfill and must be reusable by the normal tick. Batch DB lookups/transactions where existing patterns permit; no full river-fact scan and no directory walk outside selected run roots. Missing sidecars on historical runs mean unavailable provenance, not synthesized jobs or failure of already-valid scientific data.

Within the DB transaction, existing job_id must retain immutable identity. Newer authoritative updates may advance the projection; older timestamps are ignored/reported, identical replay is a no-op, equal-version conflicting payloads fail without partial updates. A previously missing log may be monotonically enriched only with verified canonical log evidence for that same row; never overwrite an existing conflicting log at equal authority version. If an extra projection checkpoint is necessary, justify it; do not invent import timestamps to outrank journal truth. No new grants are expected: verify existing ingest owner and display denial live; do not silently use superuser credentials in the importer.

### 5. Bind resolved identity without changing data containers

For strict requests on status, stages, jobs and job logs, emit one coherent top-level `identity` object as a sibling of `data` in `_ok()`. This is already accepted by the canonical validator (`body.get("identity")` first). It contains resolved `source`, `cycle_time`, `run_id`, and `model_id`, plus the actual `job_id` for logs after mismatch checks. Keep all existing `data` shapes (including stages array), request-id behavior, strict filters, 404/409/422 errors, non-strict browsing and readonly retry/cancel/Slurm refusal unchanged. No identity echo before authoritative resolution. Empty jobs may honestly carry resolved request identity but C4 still requires a real job and log.

Reuse the existing identity payload helper/definitions; update runtime OpenAPI, checked-in YAML, generated frontend types and relevant consumers/contracts normally. Do not modify the C4 or readonly validator to accept fragmented/missing identity or skip routes.


## Governing invariant, sibling surfaces, seams, evidence

Governing invariant: every displayed job/log traces to one real source journal job and, for task logs, one matching real gateway *task entry*; source/cycle/run/model/job identity survives publication, copyback, ingest transaction, SQL filters, envelope and browser. Missing or conflicting evidence cannot become success. One host owns each mutation: node-22 journal/producer, node-27 ingest DB, display none.

Sibling surfaces: four Ops endpoints; GFS/IFS casing; duplicate source-cycle runs; cycle-level versus model-level jobs; parent-array envelope versus task-entry identity; old/new attempts; nullable times; cold backfill versus normal future copyback; already-ingested skip path; failure/partial batch; redaction and safe file IO; PostgreSQL-backed writers; published-artifact log reader; hydro status / no-control exits.

Seams under test: publisher export of a real journal snapshot (forecast job + optional cycle rows); parent-array task selection including envelope/task run_id mismatch; copyback continues after publisher failure; importer replay/stale/conflict/log-enrichment; autopipeline catch-up of an already-ingested run; `_ok` envelope identity on all four strict routes; readonly denial; base-red identity/data absence on pre-change source.

Required evidence:
- Publisher given the live IFS parent-array shape (envelope run_id sibling, task 23 matching) publishes that task's 426-byte stdout and not the envelope sibling.
- Copyback hook still copies the run tree when the publisher raises.
- Importer of an already-published hydro run inserts job rows and leaves hydro status/coverage unchanged; identical replay is a no-op; older snapshot cannot roll back; identity conflict rolls back the run transaction.
- Strict status/stages/jobs/logs 200 bodies carry top-level `identity`; pre-change source fails that assertion.
- Node-27: `nhms_ingest_rw` can write `ops.pipeline_job`; `nhms_display_ro` cannot; existing 23-deny matrix still passes.
- Node-22 rehearsal: active `/scratch/frd_muziyao/NWM/.venv/bin/python` or checked-in wrapper, `--no-sync`, journal and gateway reads only.
- Dual-source readonly + formal public C4 PASS on the reviewed SHA; original #2420 red receipt retained as baseline.

Review focus: identity survival across the three hosts; fail-closed logs; copyback non-interference; no fabricated jobs; envelope identity without validator weakening.

## Risks / Trade-offs

- Replication lag → explicit missing provenance, normal lightweight catch-up and explicit same-code backfill; do not claim every historical cycle is backfilled.
- Published scientific runs can precede job metadata → separate phase; do not reparse or hide valid maps.
- Journal read sentinels/redaction → fail-closed source-owned export contract, not raw-file bypass.
- Parent-array envelope `run_id` can name a sibling task → match the task entry only.
- Retries reuse run directories → only exact task-bound gateway logs; never attach mutable native stdout to historical jobs.
- NFS partial writes → safe atomic sidecar/log publication, final validation before DB transaction; no partial logical batch.
- Replay/conflicting state → source-version ordering, immutable identity and rollback tests.
- Copyback is scheduler-adjacent → publication errors must not become hydro/copyback errors.
- Active node-22 Python 3.12 environment → exact verified interpreter/checked-in wrapper only; no uv sync or implicit 3.11 environment rebuild.

## Migration Plan

1. Implement and prove real-journal producer→sidecar→real-DB importer→API roundtrip, existing consumers and strict negative cases. Real failing base tests must expose the missing data/identity, not only absent new symbols.
2. Ship source via GitHub to isolated candidate checkouts; node-27 backend/DB tests, node-22 publication-only smoke with active interpreter. Do not alter scheduler service while a pass is active, submit work or connect an archived DB.
3. Back up exact source/config/read-model state; validate deployment/source ownership and existing roles. Deploy producer integration and explicit backfill for real GFS/IFS selected runs, then importer and display together under controlled quiet windows. Missing provider/log evidence blocks rather than seeding rows by hand. Live backfill re-resolves current identities at apply time.
4. Run production readonly validator for both exact identities, with all nine read surfaces and 23 denial probes per source, then formal public C4 freeze/run/bind/verify on reviewed SHA. Preserve original #2420 red receipt as baseline.
5. Roll back code/config and only the read-model changes owned by this deployment if a gate fails; reject foreign changes and preserve source journal/scientific products. Never restore stale scientific DB state or purge shared artifacts. Record row-level projection backup/diff for safe rollback.
6. Merge/close #2420 only after required evidence, cross-review and CI; archive fixture. Then update/review held PR #2450 against the merged base and rerun its joint A+#2346 deployment/C4 gates. No C4 waiver.

## Not yet specified

Exact publisher/importer helper module names and sidecar JSON field list beyond the closed allowlist (identity, allowlisted job columns, provenance version, verified log metadata) are implementation choices constrained by existing repository patterns. Byte/row caps should reuse existing artifact/journal bounds rather than invent a second budget. No remaining product-scope decision.

## Open Questions

None. Existing Ops functionality and strict acceptance are retained. Live backfill must re-resolve current source identities at apply time rather than assume the observed 12Z examples remain default.
