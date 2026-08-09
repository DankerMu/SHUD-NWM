# Proposal: nested-retry-defer-propagation

## Why

Issue #1322 (P1, verifier-CONFIRMED during PR #1321 round-1 review;
re-entered here as run 2 against the repaired contract posted on the
issue after run 1's fixture reviews tripped the two-iteration repair
bound): the partial-array retry helper `_retry_partial_array_stage`
(`services/orchestrator/chain_forecast_execution.py:511-526`) collapses
every `aggregation is None` nested-resubmission outcome into per-task
`failed`. For `skipped_duplicate_submission` (reserve gate: another
pass holds the in-flight reservation; this pass did no work) that
re-opens the #1164 failure family the fail-closed terminal (#1202 /
PR #1321) was built to stop, through a second door: pending tasks are
stamped failed, the helper's own terminal call re-writes durable
`update_hydro_run_status(run_id, "failed", "FORECAST_TASK_FAILED")`
over run rows the reservation-holding pass is actively working (run
ids are deterministic across passes), and the `partially_failed`
aggregate is admitted by the #1202 advance allowlist — so
parse/state_save_qc/publish run on output this pass never produced. A
third arm — the next loop iteration deriving attempt N+1 and **really
resubmitting** while the other pass's job is alive — fires when the
retry adjudicator grants the attempt (permissive/legacy retry
services; the production `RetryService` classifies the skip's missing
error code as non-retryable and breaks instead). The stamping, the
duplicate durable write, and the allowlist advance fire under EVERY
retry service. Trigger geometry is reachable and production-shaped:
two same-cohort passes both landing `partially_failed` on the array
forecast stage derive the identical deterministic retry job id
`job_{run_id}_forecast_retry_1` and idempotency key
`{run_id}:forecast:retry_1` (`chain_runtime_utils.py:413-431`;
`chain_forecast_orchestrator_cycle.py:201-203`).

## Ruling

`skipped_duplicate_submission` from a NESTED retry submission is a
**defer status, not a task failure**: the retry helper stops
immediately (no task stamping, no duplicate failed write, no next
attempt) and propagates the nested status upward so the cycle lands on
the SAME dedicated terminal the top-level path already has
(`chain_forecast_execution.py:237-256`). The defer set is
**skip-only** in this change (run-2 scope ruling): the
reconciliation-pending family (`submit_result_ambiguous`,
`reconcile_unverified` — the latter a fourth `aggregation is None`
arrival the source issue missed) shares the collapse defect but
CANNOT be deferred safely until that family is first-class on the
evidence/readiness planes (its `reconciling` terminal appears in ZERO
recognizers today, and a defer would flip cohort rows from
partial-recognized to nothing-recognized with unanalyzed recount
sign) — routed to the follow-up issue filed from this fixture's
review (see design D6), and pinned here so the exclusion is
deliberate. `submission_failed` likewise keeps its current
collapse-and-continue retry semantics (source-issue boundary), pinned
by anchor.

## What Changes

1. **`_retry_partial_array_stage` status split**
   (`services/orchestrator/chain_forecast_execution.py:511-526`): at
   the top of the `retry_aggregation is None` branch, a nested
   `skipped_duplicate_submission` returns `(latest_result, None)`
   immediately — `task_results` pending entries untouched, no further
   `_schedule_cycle_stage_retry` call, no attempt N+1. Every other
   status through that branch: byte-identical current behavior.
   Return annotation widened to
   `tuple[StageRunResult, ArrayAggregation | None] | None` at BOTH
   declaration sites (`chain_forecast_execution.py:473`,
   `chain_forecast_orchestrator_cycle.py:244`).
2. **Call-site defer routing**
   (`chain_forecast_execution.py:258-270`): when the retry helper
   returns the skip status, `_run_cycle_chain` routes it through the
   same terminal semantics as the top-level dedicated branch
   (`:237-256`) instead of falling into the advance-allowlist tail.
   The defer result overwrites `stage_results[-1]` exactly as the
   merged-retry path does today (design D3 overwrite ruling).
3. **Spec delta** (`openspec/specs/slurm-job-chain/spec.md`, MODIFIED
   requirement "Cycle-stage terminal handling is fail-closed"): the
   "Duplicate-submission skip defers the cycle" scenario's WHEN
   clause is extended to cover skips surfacing from nested retry
   submissions, with new THEN clauses: no pending-task rewrite, no
   FURTHER durable failed write as a consequence of the deferral
   (the first-pass terminal's write for genuinely-failed tasks is
   legitimate and precedes the helper), no further retry attempt
   derived from the deferred submission.

## Impact

- Production code: `services/orchestrator/chain_forecast_execution.py`
  (the retry helper branch + the call-site routing) plus the one-line
  return-annotation widening on the delegating method
  `services/orchestrator/chain_forecast_orchestrator_cycle.py:244`.
  The nested return points (`chain_stage_execution.py:243-244` skip,
  `:385-397` ambiguous) already produce correct result shapes and are
  NOT changed. The reserve gate, idempotency-key derivation
  (`chain_runtime_utils.py:413-431`), `_after_cycle_stage_terminal`
  projection suppression, and the #1202 main-loop branch structure
  are NOT changed.
- Tests: `tests/test_orchestration_chain.py` (new anchors + the two
  existing retry regressions stay green:
  `test_partial_array_retry_only_resubmits_failed_basin_tasks`
  (:4074), `test_partial_array_retry_persists_submission_under_retry_
  job_id_with_real_retry_service` (:4093)). Evidence-plane visibility
  of the nested skip rides the existing `duplicate_submission_skips`
  projection — anchored where the projection's tests live
  (`tests/test_production_scheduler.py`).
- Spec: `openspec/specs/slurm-job-chain/spec.md` (1 MODIFIED
  requirement).

## Behavior deltas (disclosed)

1. **The fix**: a nested retry submission deferred by the reserve gate
   now terminates the cycle with the dedicated
   `skipped_duplicate_submission` terminal — previously it stamped
   pending tasks failed, re-wrote their durable statuses, and
   advanced; under a permissive retry adjudicator it additionally
   derived attempt N+1 and really resubmitted. Operationally: the
   losing pass stops; the reservation-holding pass owns cycle
   progress (same operator model as #1202 delta 1).
2. Pending tasks of a deferred retry are no longer rewritten to
   `failed`, and the cycle no longer reports `partially_failed` for
   work that was never attempted. The durable write this removes is
   the helper's DUPLICATE re-write (its own terminal call at
   `:574-586` re-marking rows) — the first-pass terminal already
   wrote failed for the genuinely-failed tasks before the retry
   helper ran, on master and after the fix alike; the fix removes the
   second write over rows the reservation-holding pass owns, not a
   first-time poisoning. (Legacy-repo geometry; accepted-submit repos
   suppress the direct write on both sides.)
3. Span counters of the deferring pass shift to the top-level skip
   convention: `0/0` (skip arm) — replacing master's post-collapse
   `submitted=basin_count/failed=0` `partially_failed` shape (design
   D4 ruling). The pre-retry dispatch's accounting remains in the
   durable events written during the first pass.
4. Candidate-outcome accounting (r2r2 P1-1): a deferred cycle breaks
   before `_apply_array_progress` runs for the forecast stage, so
   `candidate_outcomes` carry no forecast failure signal — every
   cohort candidate reads `active` and the genuinely-failed basin's
   evidence item status flips `failed` (master, with
   `reason: "forecast_task_failed"`) → `skipped_duplicate_submission`
   (post-fix, reason absent). Both are review-blocked non-success
   (`final_candidate_success` stays False on both sides), and the
   shape is identical to a top-level skip's artifact geometry — no
   new readiness surface, but a real per-candidate delta.

## Non-goals

- Nested `submit_result_ambiguous` / `reconcile_unverified` defer —
  requires the reconciliation-family evidence-plane work first;
  routed to the follow-up issue recorded in design D6 and pinned by
  anchor A3 so the current collapse behavior for those statuses is
  deliberately retained until that issue lands.
- `submission_failed` semantics (collapse-to-failed + retry loop +
  `_mark_staged_hydro_runs_failed`) — unchanged, pinned by anchor A4.
- Reserve-gate adjudication (`_reservation_already_inflight`) and
  retry-key derivation rules — unchanged.
- The file-journal retry-identity minter
  (`file_orchestration_journal.py:7406-7410`) — derives identity
  from pipeline-job rows, not the hydro statuses this change stops
  re-writing; second-order consumer, unaudited here (known limit in
  design D5).
- Pre-retry first-pass `partially_failed` handling and the #1202
  terminal/allowlist structure — receivers of this fix, not modified.
