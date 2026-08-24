## Context

Fixture level: expanded. Repair intensity: broad-expanded. Project profile: NHMS.

The three issues share one state-integrity root: a private write path must merge from durable truth, while public redaction belongs only at an explicit read/return boundary. `update_hydro_run_status` currently calls `_hydro_run_for`, which public-renders the row before the merge. `FileJournalRetryService._mark_master_permanently_failed` receives two public snapshots and forwards their redacted `error_message` into a typed durable transition. `project_forecast_cohort_tasks` derives only `succeeded`, `partially_failed`, or `failed`, but currently protects only `permanently_failed` from that derivation.

## Goals / Non-Goals

**Goals:**

- Preserve exact durable hydro-run error attribution on non-clearing status updates.
- Preserve exact durable master attribution when permanent failure is marked from an unchanged public snapshot, without suppressing genuinely new caller text.
- Preserve `cancelled` and `permanently_failed` status across task reprojection while still refreshing task projections and observational evidence.
- Keep public redaction and all existing typed-transition/idempotency/error contracts unchanged.

**Non-Goals:**

- No migration or repair sweep for already-corrupted rows.
- No change to `_sanitize_public_*`, `_strip_redaction_placeholders`, successful hydro-run clearing states, or event redaction.
- No new writer that produces a contract-current `cancelled` master; #1629 remains a defensive read/write-boundary fix for schema-valid persisted rows.
- No projection of `submission_failed` or `reservation_lost`: their existing submit-outcome/inventory gates remain the owners, and `reservation_lost` accounting decisions must not be rewritten as `matched_bound`.
- No DB backend, API route, Slurm scheduler, node-22, or node-27 behavior change.

## Decisions

### D1: Make the private hydro-run lookup durable

`_hydro_run_for` SHALL return `dict(rows.hydro_run)`, not `_public_scheduler_row(rows.hydro_run)`. Every production caller is internal and uses it as durable state; explicit public methods already render at return/query boundaries. This fixes the invariant at the source instead of adding a second point-read only for `update_hydro_run_status`.

Rejected: add `_durable_hydro_run_for` while retaining the misleading public behavior of the private method. It creates two private names for one row and leaves existing internal merge/retry callers exposed to the same category.

`update_hydro_run_status` continues to return `_public_scheduler_row(row)`. Non-clearing states preserve absent error arguments from the durable base; the existing clearing set `{pending, created, succeeded, complete, parsed, published}` still writes the provided error values, including `None`.

### D2: Distinguish a round-trip snapshot from new permanent-failure text

The retry service SHALL pass `error_message=None` to `mark_pipeline_job_permanently_failed` when the selected source message equals the current public message: both values came through the same public projection and therefore carry no new authoritative text. The typed transition's existing `None` semantics preserve the durable value. If `source.error_message` is non-empty and differs from `current.error_message`, it is caller-provided new text and SHALL still be forwarded and durably sanitized.

Rejected: always drop the message. That breaks the acceptance criterion for a genuinely new decline message. Rejected: substring-unredact `[local-path]`/`[object-uri]`; redaction is irreversible and guessing a path would fabricate evidence.

`status`, `error_code`, `finished_at`, event details, and missing/stale/idempotent outcomes remain byte-for-byte compatible.

### D3: Name the projection-owned and sticky terminal domains

Define one local constant for exactly the statuses the projection can derive: `{succeeded, partially_failed, failed}`. A persisted terminal status is sticky in this function when it is projection-reachable and outside that derived set. Under the current routed domain, this means `permanently_failed` and `cancelled`.

The implementation SHALL preserve the existing attribution family only for `permanently_failed`; `cancelled` preserves its status but still takes the current task-derived `error_code`/`error_message` and refreshes `candidate_projections`, `finished_at`, `exit_code`, and `log_uri`. This is the minimal #1629 contract: do not turn cancellation into failure, but do not invent frozen cancellation attribution that the issue did not request.

`submission_failed` is rejected before this projection by its submit-outcome/inventory gate. `reservation_lost` exits reconcile inventory and may carry an accounting decision that is valid only with that literal status; it must never be fed through a `matched_bound` projection. Tests keep those routing guards intact rather than widening this write path to accept them.

Rejected: `TERMINAL_PIPELINE_STATUSES - derived`. It accidentally includes statuses whose accounting tuple cannot legally pass this function. Rejected: `{permanently_failed, cancelled}` inline at the branch; naming the derived domain makes the ownership rule reviewable while the routing exclusions remain explicit.

## Invariant Matrix

- Governing invariant: a write that claims to preserve existing state must merge from durable truth, and task projection must not overwrite a terminal truth it cannot derive.
- Source-of-truth identity/contract: durable `hydro_run`/accepted-submit master rows under one cycle lock; public projections are display-only snapshots.
- Producers: `_write_hydro_run`, accepted-submit typed transitions, `FileJournalRetryService`.
- Validators/preflight: `_validate_outgoing_record`, accepted-submit evidence validation and accounting transition validation.
- Storage/cache/query: `_cycle_rows`, journal/direct rows, `_hydro_run_for`, `_pipeline_job_for_id_unlocked`.
- Public routes/entrypoints: repository status methods and retry service; all return public-rendered rows.
- Frontend/downstream consumers: scheduler/retry callers consume public rows; no shape changes.
- Failure paths/rollback/stale state: missing/stale/idempotent permanent-failure outcomes and successful hydro status clearing remain unchanged; no new partial write.
- Evidence/audit/readiness: master JSONL/direct assertions, existing per-model `latest/` projection compatibility (cohort masters remain excluded), public return assertions, and focused pytest.
- Regression rows:
  - durable URI error + non-clearing hydro update without errors -> exact durable value survives, public return remains redacted.
  - public master snapshot + permanent-failure mark -> exact durable message survives; distinct new source text overrides.
  - cancelled master + complete task projection -> status stays cancelled while task/observational evidence refreshes.
  - derived master status + later projection -> status/error remain projection-owned; reservation-lost/submission-failed remain unrouted to this projection.

## Boundary-Surface Checklist

- Shared helper roots: `_hydro_run_for`, public/durable redaction helpers.
- Public entrypoints: `update_hydro_run_status`, `FileJournalRetryService.mark_permanently_failed`, `project_forecast_cohort_tasks`.
- Read surfaces: cycle replay and private single-row lookups.
- Write surfaces: validated hydro append; permanent-failure typed payload loop; cohort projection payload loop/direct row.
- Producer/consumer evidence boundary: public snapshots must never become durable merge bases.
- Stale/idempotency boundary: permanent-failure missing/stale/idempotent outcomes unchanged.
- Unchanged downstream consumers: retry reset/source selection, historical repair script, candidate state/public projection.

## Risk Packs Considered

- Public API / CLI / script entry: selected — internal method semantics and an ops script caller are audited; public row output must remain redacted.
- Config / project setup: not selected — no config or setup changes.
- File IO / path safety / overwrite: selected — durable JSONL/direct/latest records carry attribution; no path-opening behavior changes.
- Schema / columns / units / field names: selected — existing status/error fields and accepted-submit enum semantics change, with no shape change.
- Auth / permissions / secrets: selected — public redaction must not regress or expose raw paths/URIs.
- Concurrency / shared state / ordering: selected — merges remain under the existing cycle lock; no extra unlocked read.
- Resource limits / large input / discovery: not selected — no discovery or input-size behavior changes.
- Legacy compatibility / examples: selected — schema-valid historical `cancelled` rows and existing public/private consumers must continue to load.
- Error handling / rollback / partial outputs: selected — missing/stale/idempotent and zero-write failure exits remain unchanged.
- Release / packaging / dependency compatibility: not selected — no dependency or packaging change.
- Documentation / migration notes: selected — OpenSpec must replace the prior declared-gap wording; no migration is required.
- Geospatial / CRS / basin geometry: not selected — no geometry.
- Hydro-met time series / forcing windows: not selected — no time-series/forcing logic.
- SHUD numerical runtime / conservation / NaN: not selected — no solver behavior.
- PostGIS / TimescaleDB domain behavior: not selected — DB-free.
- Slurm production lifecycle / mock-vs-real parity: not selected — only persisted accounting interpretation changes; no sbatch/gateway/scheduler behavior.
- External hydro-met providers / snapshot reproducibility: not selected — no provider data.
- Run manifest / QC provenance: selected — durable attribution/provenance must remain exact rather than redacted.
- Published NHMS artifacts / display identity: not selected — no publish/display artifact identity change.

## Risks / Trade-offs

- [Private hydro lookup exposes raw values to internal callers] -> Keep it private, audit all callers, and assert public return/query surfaces remain redacted.
- [A genuinely new message could equal the redacted public text by coincidence] -> Equality means no distinguishable new evidence exists; preserving durable truth is the fail-safe direction. Distinct source text remains supported.
- [A future non-derived status reaches projection] -> The derived-domain predicate preserves its status, but accepted-submit validation/routing tests must still establish that its accounting tuple is legal before adding a route.
- [Already corrupted durable rows remain corrupt] -> Explicit non-goal; no safe inverse exists for redacted paths.

## Migration Plan

No data migration. Deploy as a normal code change; rollback is the single PR revert. Existing rows remain readable in both directions.

## Open Questions

None. `reservation_lost` is explicitly excluded by current routing/accounting invariants, not left for implementer discretion.
