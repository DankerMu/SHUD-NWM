## Context

Fixture level: expanded

Repair intensity: high

Project profile: NHMS

Issue #386 is a production state/evidence bug. A source-cycle stage can have an
old terminal failure and a later successful manual retry that repairs the same
logical stage. Current candidate readiness can still surface the older failure as
active blocking evidence, even when `met.forecast_cycle` is `raw_complete` and
the linked retry succeeded.

## Goals / Non-Goals

Goals:
- Prefer the latest successful linked retry repair over older failed evidence for
  the same logical stage/run/cycle.
- Retain audit visibility of the original failed job as historical repaired or
  superseded evidence.
- Ensure scheduler candidate decisions do not keep emitting `CANDIDATE_FAILED`
  or `canonical_incomplete` solely from a repaired source-cycle failure.
- Bind source-cycle repair to the same `forecast_cycle.manifest_uri` /
  source-cycle identity so a successful unrelated retry cannot prove readiness.
- Cover the shared source-cycle path without regressing existing array task retry
  supersession behavior.
- Add operator runbook guidance for diagnosis and safe remediation.

Non-goals:
- No change to #384 runtime-root reconstruction or Slurm submission manifests.
- No automatic file movement or object-store repair.
- No database schema/enum migration.
- No frontend or node-27 display implementation.
- No broad rewrite of pipeline stage taxonomy or production status enums.

## Decisions

1. Bind repair evidence by durable retry provenance and logical stage identity.
   - The successful retry must link to the failed job/stage through retry event
     details, `previous_job_id`, retry naming, or existing stage/run identity.
   - A successful unrelated sibling stage/job must not hide an unrepaired
     failure.

2. Preserve both records in evidence.
   - Readiness may clear the active blocker, but audit evidence must identify
     the original failure, the repairing retry job, and the repaired stage.
   - Repair metadata is additive; existing monitoring/API envelopes and existing
     field names must stay compatible.

3. Keep selection bounded and deterministic.
   - Existing job/event limits and ordering must remain bounded; no unbounded DB
     scans or broad filesystem discovery are introduced.

4. Require manifest/source-cycle agreement for source download repair.
   - A successful retry can repair a failed `download_source_cycle` only when it
     belongs to the same source/cycle and the forecast cycle carries a ready raw
     manifest URI for that source/cycle.
   - Missing or mismatched manifest evidence leaves the old failure active.

## Risk Packs Considered

- Public API / CLI / script entry: selected - scheduler evidence is consumed by
  API/runbook/operator surfaces, though no route shape should change.
- Config / project setup: not selected - no new env/config is introduced.
- File IO / path safety / overwrite: not selected - no file movement or path
  write behavior changes.
- Schema / columns / units / field names: selected - evidence fields for
  repaired/superseded failures must be stable and redacted.
- Auth / permissions / secrets: not selected - no mutation/auth path change.
- Concurrency / shared state / ordering: selected - retry success vs older
  terminal failure ordering is the core state-machine invariant.
- Resource limits / large input / discovery: selected - job/event evidence reads
  must remain bounded by existing limits.
- Legacy compatibility / examples: selected - existing array retry supersession,
  non-source retry behavior, and candidate failure reporting must remain stable.
- Error handling / rollback / partial outputs: selected - stale failures must not
  be silently dropped; unrepaired failures still block with stable evidence.
- Release / packaging / dependency compatibility: not selected - no dependency or
  packaging change.
- Documentation / migration notes: selected - operator runbook must cover stale
  stage evidence diagnosis.

Domain packs:
- Geospatial / CRS / basin geometry: not selected - no geometry behavior.
- Hydro-met time series / forcing windows: selected - source-cycle readiness gates
  downstream forcing/model execution.
- SHUD numerical runtime / conservation / NaN: not selected - no SHUD runtime
  behavior.
- PostGIS / TimescaleDB domain behavior: not selected - no migration or spatial
  query change.
- Slurm production lifecycle / mock-vs-real parity: selected - Slurm job history
  and retry status ordering drive evidence.
- External hydro-met providers / snapshot reproducibility: selected - IFS/GFS
  source-cycle recovery must bind to the provider cycle identity.
- Run manifest / QC provenance: selected - `forecast_cycle.manifest_uri` and
  repaired stage evidence must agree.
- Published NHMS artifacts / display identity: not selected - no publish/display
  identity change.

## Invariant Matrix

Governing invariant: for a logical stage/cycle/run, the active candidate blocker
must be the latest unrepaired terminal failure; a later successful linked retry
turns the earlier failure into historical repaired evidence.

Source-of-truth identity/contract: `source_id`, `cycle_time`, `cycle_id`,
`run_id`, `stage`/`job_type`, original `job_id`, retry `previous_job_id` or retry
marker details, retry status, and `met.forecast_cycle.status`/`manifest_uri`.

Surfaces:
- Producers: `services/orchestrator/retry.py`, `services/orchestrator/chain.py`
  event/job records.
- Validators/preflight: scheduler candidate state decision helpers in
  `services/orchestrator/scheduler.py`.
- Storage/cache/query: `PsycopgOrchestratorRepository.candidate_state()` job/event
  selection and bounded evidence construction.
- Public routes/entrypoints: monitoring/pipeline APIs that expose candidate or
  stage evidence, if affected.
- Frontend/downstream consumers: no frontend changes; evidence shape must remain
  compatible with existing consumers.
- Failure paths/rollback/stale state: unrepaired failures, unrelated successful
  jobs, stale manual retry markers, and bounded/truncated state.
- Evidence/audit/readiness: scheduler evidence JSON, candidate outcomes,
  `stage_statuses`, `residual_blockers`, and operator runbook.

Regression rows:
- Original `download_source_cycle` permanently_failed + linked retry succeeded +
  forecast cycle `raw_complete` -> candidate is not blocked by the original
  failure; evidence marks original as repaired/superseded by the retry.
- Original failure + unrelated successful job or missing retry linkage -> failure
  remains active blocker with stable error evidence.
- Original failure + stale manual retry marker or retry row that is not
  `succeeded` -> failure remains active blocker.
- Original failure + linked successful retry but missing/mismatched
  `forecast_cycle.manifest_uri` -> failure remains active blocker.
- Existing partial array failure + later retry task success -> current array
  retry supersession behavior remains unchanged.
- Existing non-source/manual retry behavior -> retry API/service compatibility
  remains unchanged.
- Existing monitoring/pipeline API response envelope -> unchanged; repair
  metadata is additive and bounded when surfaced.
- Bounded job/event limits -> evidence indicates truncation instead of performing
  unbounded scans.

## Review Focus

- Retry provenance matching is strict enough not to hide unrelated failures.
- Candidate readiness and stage evidence use the same repaired-failure semantics.
- Evidence retains audit history without reintroducing stale blockers.
- Existing retry/runtime-root and array retry tests remain compatible.
