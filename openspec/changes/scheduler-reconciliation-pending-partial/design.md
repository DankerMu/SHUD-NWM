## Context

Top-level `submit_result_ambiguous` and `reconcile_unverified` already terminate a cycle as `reconciling`. The evidence path does not recognize that terminal and can set `final_candidate_success=True`. The nested partial-array retry helper instead collapses either status into failed task rows, builds a failed/partial aggregation, defeats the `reconcile_unverified` durable no-op, and can enter another retry under permissive services.

- Fixture level: expanded
- Repair intensity: high
- Project profile: NHMS
- Upstream suggested level: absent
- Minimal mergeable slice: Part A evidence/readiness first-classing and Part B nested defer in one PR; neither half is independently mergeable.

## Goals / Non-Goals

**Goals:**

- Give the three reconciliation status tokens one evidence meaning: partial, producer-partial, non-failed, non-successful.
- Keep readiness cardinality in agreement with producer `partial_count` for zero-submission and mixed passes.
- Propagate nested ambiguous/unverified results to cycle terminal `reconciling` without further task stamping, retry minting, downstream execution, or durable failure writes.
- Preserve the duplicate-skip terminal and all non-reconciliation retry behavior.

**Non-Goals:**

- Change `submission_failed` collapse/retry semantics.
- Change `skipped_duplicate_submission` behavior delivered by #1322/#1324.
- Add `reconciling` to `production_status_for`; stage production status remains fail-closed `failed` until separately governed.
- Change `forcing_ready_partial`, reserve-gate, idempotency keys, accepted-submit projection suppression, database schema, or Slurm gateway behavior.
- Claim public readiness `passed`: reconciliation remains review-blocked until exact outcome is known.

## Decisions

### D1: Internal partial, public review-blocked

The family joins the candidate non-success predicate and the readiness `partial` plus `producer_partial` predicates. It does not join failed or submitted-compatible sets. Pass status `reconciling` joins the review-blocked vocabulary so the public readiness item remains `blocked`, not `passed` and not `status_not_allowed`.

This separates two existing axes honestly: partial describes incomplete model-run accounting; blocked describes whether readiness can be accepted.

### D2: Closed status family with one shared definition per plane

The governed family is exactly:

- cycle terminal: `reconciling`
- stage terminals: `submit_result_ambiguous`, `reconcile_unverified`

Each plane must consume a single constant/helper rather than copy three literals into multiple predicates. Existing public constants may be extended when they already own the vocabulary; no new generic status framework is introduced.

### D3: Explicit nested status-to-terminal mapping

`NESTED_RETRY_DEFER_STATUSES` may include the two stage terminals, but the caller cannot keep routing every member through `_skip_terminal_pipeline_result`. A dedicated mapping/helper returns:

- `skipped_duplicate_submission` -> `skipped_duplicate_submission`
- `submit_result_ambiguous` -> `reconciling`
- `reconcile_unverified` -> `reconciling`

The helper constructs the existing top-level terminal shape and candidate outcomes. Unknown statuses never enter this mapping and continue through the fail-closed tail.

### D4: Defer before task mutation

When a nested submission returns one governed status with `aggregation is None`, `_retry_partial_array_stage` returns the raw result immediately. No pending task row is changed, no aggregation is synthesized, and the enclosing `finally` restores the full basin cohort. The caller stops the cycle on the mapped terminal before `_apply_array_progress` or downstream stages.

### D5: Durable no-op and attempt boundary

The nested `reconcile_unverified` path must retain the existing terminal-hook no-op: the defer must not re-enter `_after_cycle_stage_terminal` as `partially_failed`. The nested call may already have emitted its own `reconcile_unverified` event/row; this change adds no second cycle write. The accepted nested attempt that produced the pending result is observable, but no attempt N+1 is derived after it.

## Risk Packs Considered

- Public API / CLI / script entry: not selected — no external entrypoint change.
- Config / project setup: not selected — no configuration.
- File IO / path safety / overwrite: not selected — journal behavior is observed, but no file primitive changes.
- Schema / columns / units / field names: selected — evidence status vocabulary and count semantics change.
- Auth / permissions / secrets: not selected — no security boundary.
- Concurrency / shared state / ordering: selected — pending submissions may still represent live jobs; defer ordering is the core invariant.
- Resource limits / large input / discovery: not selected — bounded existing cohort geometry.
- Legacy compatibility / examples: selected — duplicate skip, submission failure, generic retry, and historical evidence must remain compatible.
- Error handling / rollback / partial outputs: selected — ambiguous and unverified outcomes must not become fabricated failure or success.
- Release / packaging / dependency compatibility: not selected — no dependency change.
- Documentation / migration notes: selected — three capability deltas record the new vocabulary and non-goals.
- Geospatial / CRS / basin geometry: not selected — unrelated.
- Hydro-met time series / forcing windows: not selected — unrelated.
- SHUD numerical runtime / conservation / NaN: not selected — no solver behavior.
- PostGIS / TimescaleDB domain behavior: not selected — no DB path.
- Slurm production lifecycle / mock-vs-real parity: selected — accepted-submit ambiguity and polling timeout are Slurm lifecycle states, tested at the orchestrator/gateway fake boundary.
- External hydro-met providers / snapshot reproducibility: not selected — unrelated.
- Run manifest / QC provenance: selected — model-run evidence and pass counts must reflect the true pending outcome.
- Published NHMS artifacts / display identity: not selected — downstream publication must not run in the defer scenario.

## Invariant Matrix

- Governing invariant: A submission whose exact result is pending reconciliation is incomplete and review-blocking; it is never fabricated as success or failure, and no further work is scheduled from that uncertainty.
- Source of truth: raw stage status (`submit_result_ambiguous` or `reconcile_unverified`) and mapped cycle terminal `reconciling`.
- Producers: `chain_stage_execution.py` ambiguous submit and polling-timeout paths; unchanged.
- Validators/preflight: candidate quality non-success predicate; readiness pass/model-run vocabularies.
- Storage/cache/query: file-journal accepted-submit rows/events; existing writers remain unchanged and `reconcile_unverified` no-op stays authoritative.
- Public routes/entrypoints: none; `orchestrate_cycle` is the public orchestration seam exercised by tests.
- Frontend/downstream consumers: scheduler candidate evidence, pass counts, readiness item, and stage progression.
- Failure/rollback/stale state: nested helper returns before task mutation; full basin cohort restored; no N+1 retry; unknown/submission-failed paths unchanged.
- Evidence/audit/readiness: candidate `final_candidate_success=False`; partial and producer-partial row counts agree; public readiness blocked with no vocabulary/cardinality error.
- Regression rows:
  - top-level cycle `reconciling` + active candidate -> non-success, partial evidence, failed false.
  - zero-submission reconciling pass + one row -> `partial_count=1`, readiness blocked, no status/cardinality errors.
  - nested ambiguous result -> raw stage result, no task failure stamp, cycle `reconciling`, no downstream or N+1 attempt.
  - nested unverified result -> same plus no second durable cycle-status write.
  - duplicate skip and nested `submission_failed` -> existing behavior unchanged.

## Boundary Surface Checklist

- Shared state-machine root: `chain_forecast_execution.py` defer set, mapping, caller, retry helper.
- Evidence producer/consumer boundary: cycle result -> candidate evidence -> pass counts -> readiness recount.
- Stale/idempotency boundary: accepted-submit ambiguity may still correspond to a live job; no new attempt may be minted.
- Durable boundary: existing nested event/write is retained; no fabricated partial/failure write.
- Unchanged sibling consumers: duplicate skip, submission failure, succeeded/failed partial retry, production status translator.

## Risks / Trade-offs

- A partial predicate added without producer-partial creates zero-submission cardinality mismatch -> paired predicate and end-to-end artifact test are mandatory.
- A defer-set-only edit maps reconciliation to the skip terminal -> explicit mapping oracle is mandatory.
- Treating reconciliation as blocked at row level obscures incomplete accounting -> row remains partial; public readiness alone is blocked.
- Tests could pass without proving no N+1 attempt -> submission attempt counts and downstream stage list are load-bearing assertions.
- Rollback is one PR revert; there is no migration or persisted-format conversion.
