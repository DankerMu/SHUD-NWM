## Context

Issues #1796 and #1568 expose the same scheduler invariant at two adjacent boundaries: once authority state or a no-submit decision is established, optional derived evidence must not reverse the business outcome. In the file journal, `_write_pipeline_job_unlocked` appends authority before writing direct/inventory/latest projections. Current master contains those post-append faults only for the #1564 operator old-ID reclaim; automatic `absence_retry_permitted`, plain reserve, and other generic reservation calls retain the escaping-failure window. In the submission layer, duplicate-skip evidence catches `OrchestratorError` but not the sibling `FileOrchestrationJournalError` emitted by the db-free repository.

Fixture level: expanded. Repair intensity: high. The issues have no upstream `Suggested fixture level` or `Minimal mergeable slice`; the minimal mergeable slice is the shared committed-decision containment invariant across both named entrances.

## Goals / Non-Goals

**Goals:**

- Preserve authority-journal append as the commit point for reservation and reclaim writes.
- Contain every reachable post-append direct/inventory/latest projection fault at the explicit reserve/reclaim boundary, return the committed row, attempt independent projections, and expose a bounded non-secret committed-warning signal.
- Prove automatic-absence recovery cannot become a wedged `reserved` row or double-submit after a post-append projection fault.
- Preserve duplicate-skip results and zero sbatch when event emission raises either scheduler exception family.
- Keep pre-append failures fail-closed and normal reserve/reclaim/bind behavior unchanged.

**Non-Goals:**

- Changing #1116 reconcile decisions, Slurm ambiguous-submit handling, operator old-ID routing, #1592 public projection stripping, PostgreSQL repository behavior, or status vocabulary.
- Making every pipeline-job write best-effort or changing the cross-layer exception hierarchy.
- Requiring a live node-22 Slurm receipt for deterministic local-only file-journal fault injection.

## Decisions

### D1: Containment is opt-in at reservation/reclaim write boundaries

Use an explicit committed-projection policy/result at `reserve_pipeline_job` and `reclaim_pipeline_job_reservation`, reusing and generalizing the existing #1564 mechanism. Do not silently enable containment for status transitions, bind, terminal writes, or arbitrary callers of `_write_pipeline_job_unlocked`; those operations can have different completion semantics and are outside these issues.

Alternative rejected: catch projection errors globally after every pipeline-job append. It is shorter but changes unrelated writer contracts and can hide failures whose caller has not accepted committed-warning semantics.

### D2: Authority replay decides commitment; projection failures cannot roll it back

After a successful authority append, each independent derived projection is attempted under the existing lock. A failure returns the public projection of the committed row rather than raising `FILE_JOURNAL_WRITE_FAILED`. A fresh repository replay must observe the new reservation attempt even if direct/latest materialization failed. A failure before append still raises and leaves authority bytes unchanged.

For a plain new reservation, the same rule prevents a committed row from being falsely reported as an insert failure. For automatic reclaim, it lets the winning pass continue the unique submit/bind path; competing passes still lose the authority CAS/reservation gate and must not submit.

Alternative rejected: append a compensating `submission_failed`/`reservation_lost` state after a projection fault. That invents a second transition, can itself fail, and creates unnecessary retry semantics when the committed reservation is valid and replayable.

### D3: Warning evidence is bounded, non-secret, and subordinate to the committed result

Emit one stable warning per failed projection using fixed code/reason tokens and validated identity only; never include exception text, class, path, `error_code`, `reason`, or secret-derived hashes. Where a durable journal event can be appended without recursively invoking the failed projection, record it; if that secondary evidence append fails, retain the process warning and committed result. Observability failure never reclassifies committed success as failure.

Alternative rejected: echo the original fault detail requested by the issue. Raw filesystem errors can contain secrets and paths; the safe contract is fault class/site observability, not arbitrary exception content.

### D4: Duplicate-skip evidence catches only the two repository-domain families

Align `_skip_duplicate_submission` with the established sibling handler and catch `(OrchestratorError, FileOrchestrationJournalError)`. Do not catch `Exception`: programming errors must still surface. The skip is already determined by the reservation gate, so either expected evidence-write failure leaves the returned status `skipped_duplicate_submission`, records the in-memory bounded skip evidence, and performs zero sbatch.

Alternative rejected: wrap all file-journal errors as `OrchestratorError` in `insert_pipeline_event`. That changes a shared exception contract and every caller, far beyond #1568.

## Invariant Matrix

- Governing invariant: once a db-free authority append or duplicate-skip decision is established, downstream projection/evidence failure cannot reverse it or authorize an additional Slurm submission.
- Source-of-truth identity/contract: cycle authority journal record keyed by source/cycle/job/idempotency plus submission attempt and attempt anchor; reservation-gate `created` result for submit ownership.
- Producers: `FileOrchestrationJournalRepository.reserve_pipeline_job`, `reclaim_pipeline_job_reservation`, `_write_pipeline_job_unlocked`; forecast `_skip_duplicate_submission`.
- Validators/preflight: clean-reservation validation, current-master reclaim CAS, immutable cohort identity, exact attempt/anchor, `_reservation_already_inflight`.
- Storage/cache/query: append-only cycle journal is authority; direct job, reconcile inventory, and latest files are derived/rebuildable projections.
- Public routes/entrypoints: `ForecastOrchestrator.orchestrate_cycle` and `_submit_and_wait_cycle_stage`.
- Frontend/downstream consumers: `StageRunResult`, `PipelineResult`, scheduler candidate evidence; no frontend change.
- Failure paths/rollback/stale state: append failure is zero-commit; post-append projection failure is committed-warning; duplicate event failure preserves skip; next pass must not wedge or double-submit.
- Evidence/audit/readiness: focused pytest fault injection, warning/event assertions, fresh repository replay, second public cycle, ruff, strict OpenSpec.
- Regression rows:
  - automatic `absence_retry_permitted` + one post-append direct/inventory fault -> committed new attempt, one submit at most, next cycle no `PIPELINE_ALREADY_ACTIVE` and zero duplicate submit.
  - plain clean reservation + post-append projection fault -> committed reservation returned/replayed; pre-append append fault -> exception and byte-identical authority.
  - active competing reservation + event write raises either `OrchestratorError` or `FileOrchestrationJournalError` -> typed duplicate skip and zero sbatch.
  - normal reserve/reclaim/bind and operator old-ID containment -> existing behavior and warning contract remain compatible.

## Boundary-Surface Checklist

- Shared helper roots: pipeline-job append/direct/latest helpers and bounded projection-warning helper.
- Public entrypoints: forecast public cycle and real submit gate.
- Read surfaces: authority replay, accepted-submit current-master lookup, candidate/idempotency lookup.
- Write surfaces: plain reserve, generic reclaim, event append; bind/status writers intentionally unchanged.
- Producer/consumer evidence: reservation result -> submit ownership; duplicate-skip event -> typed stage result.
- Stale/idempotency: concurrent winner/loser, second public pass, fresh repository replay.
- Unchanged downstream consumers: operator reclaim, PostgreSQL repository, Slurm gateway ambiguity, scheduler evidence projection.

## Risk Packs Considered

- Public API / CLI / script entry: selected — public forecast cycle and typed stage result are observable boundaries.
- Config / project setup: not selected — no configuration or setup change.
- File IO / path safety / overwrite: selected — post-append direct/inventory/latest writes and authority replay are the defect boundary; existing safe-filesystem primitives remain mandatory.
- Schema / columns / units / field names: selected — warning/event fields and reservation attempt identity must remain stable; no new external schema.
- Auth / permissions / secrets: selected — filesystem faults and warnings must not leak paths, credentials, exception text, or secret-derived data.
- Concurrency / shared state / ordering: selected — append-before-projection ordering and single submit ownership are central.
- Resource limits / large input / discovery: not selected — no new discovery, polling, or unbounded reads.
- Legacy compatibility / examples: selected — operator-only containment, generic/legacy rows, normal reserve/bind, and PostgreSQL behavior must not regress.
- Error handling / rollback / partial outputs: selected — pre-commit failure versus committed partial projection is the core contract.
- Release / packaging / dependency compatibility: not selected — no dependency or packaging change.
- Documentation / migration notes: selected — OpenSpec records the new durable behavior; no operator migration/runbook needed.
- Geospatial / CRS / basin geometry: not selected — no geometry/data surface.
- Hydro-met time series / forcing windows: not selected — no forcing/time-window semantics.
- SHUD numerical runtime / conservation / NaN: not selected — no solver behavior.
- PostGIS / TimescaleDB domain behavior: not selected — db-free local journal only.
- Slurm production lifecycle / mock-vs-real parity: selected — prove zero double submission and preserve submit ownership without changing gateway behavior.
- External hydro-met providers / snapshot reproducibility: not selected — no provider boundary.
- Run manifest / QC provenance: not selected — no manifest/QC content change.
- Published NHMS artifacts / display identity: not selected — no publish/display surface.

## Risks / Trade-offs

- [A committed row may outlive stale projections] -> authority replay remains canonical; later writes/materialization repair derived views, and tests reopen from the journal.
- [A warning event append can encounter the same filesystem fault] -> make it best-effort and non-recursive; process warning plus committed return remains the final fallback.
- [Overbroad containment could hide unrelated defects] -> opt in only from the named reservation/reclaim boundaries and catch projection work only after the append commit point.
- [Continuing after commit could double-submit under concurrency] -> ownership remains the locked authority write/CAS and `ReservationResult.created`; deterministic winner/loser and second-pass tests pin zero duplicate sbatch.

## Migration Plan

No data migration. Deploy as a backward-compatible scheduler code change. Rollback restores the prior escaping-failure behavior but does not require journal conversion because no authority schema or status changes. No live production fault injection is permitted or required.

## Open Questions

None. #1568's product-semantics question is resolved by the existing sibling handler and identical comment contract: expected evidence-write failures do not overturn a correct skip decision.
