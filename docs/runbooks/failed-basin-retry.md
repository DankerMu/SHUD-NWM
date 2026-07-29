# Failed Basin Retry Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_failed_basin_retry_count`, basin/model ID, job ID, and run ID.
- Preserve stdout, stderr, manifest, and QC evidence before retrying.

## Commands

```bash
uv run nhms-production validate-slurm --evidence-root artifacts/production-closure --run-id failed-basin-retry-check --fake-slurm
uv run pytest -q tests/test_shud_runtime.py tests/test_production_slurm_validation.py
```

## Expected Evidence

- `slurm/array_partial_success.json` keeps successful sibling outputs immutable.
- `slurm/retry_cancel.json` records retry status and cancellation semantics.
- `ops/monitoring_alerts.json` records the breached retry threshold.

## Recovery Steps

1. Classify the failed basin error from stderr and QC evidence.
2. Retry failed-only tasks when the error is transient.
3. Quarantine failed outputs if retry is not safe, while retaining successful sibling artifacts.

## Identity-blocked reservation convergence (`identity_mismatch_released`)

Restart reconcile records `identity_mismatch_blocked` for a reserved-unbound forecast
cohort master whose Slurm identity cannot be verified. Before #1173 that outcome never
moved the row: it stayed `reserved`, `reserved` is not a terminal job status, so every
later pass raised `PIPELINE_ALREADY_ACTIVE` and the cycle recorded `submission_failed`
with zero Slurm submissions.

The journal now keeps a durable consecutive counter, `identity_blocked_streak`, on the
master row. Any other accounting transition (bind, absence-path release, a new reserved
attempt after reclaim) resets it to zero. Once it reaches
`NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT` (default `3`; `<= 0` disables the exit and
keeps the old wedging behaviour) **and** the row is past the accepted-submit grace —
measured from `submission_attempt_started_at`, never from `updated_at` — reconcile
migrates the row to `reservation_lost` with `reconciliation_decision =
identity_mismatch_released`.

When the exit is disabled (`<= 0`), the counter is not merely unused: it freezes at its
**current** value (`reconcile.py:2067` writes `streak = current` when the exit is off, so
it reads `0` only for rows that never counted — a row that reached `2` while the exit was
enabled keeps reporting `2` after disabling), so the streak is **not** a no-progress
diagnostic in that mode. Count the repeated `identity_mismatch_blocked` action rows across
passes instead.

### Finding the rows

- Scheduler pass evidence: `restart_reconcile.reserved_unbound.outcomes[]` carries
  `action`, `identity_blocked_streak` and `durable_write_count`. The counter survives the
  bounded (size-limited) evidence compaction, so it is readable even on a pass that hit
  the evidence byte limit. **With the exit disabled (`NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT
  <= 0`) `identity_blocked_streak` freezes at whatever value it already held** (`0` only
  for rows that never counted; a row that counted to `2` under an enabled exit keeps
  reporting `2`) instead of advancing; in that mode measure no-progress by counting
  consecutive passes whose outcome `action` is `identity_mismatch_blocked` for the same
  `job_id`, not by the counter.
- Journal: the master row of the wedged cycle
  (`job_cycle_<source>_<cycle>_forecast[...]`) shows `status=reservation_lost`,
  `reconciliation_decision=identity_mismatch_released`,
  `submit_outcome=submit_result_ambiguous`, `matched_slurm_job_id=null`, and the final
  `identity_blocked_streak`.

### A released row is a deliberate, non-reclaimable terminal

Do **not** try to reset, reclaim or hand-edit a released row:

- `reclaim_pipeline_job_reservation` only revives `absence_retry_permitted`; on an
  `identity_mismatch_released` row it returns `None` and writes nothing.
- The cycle chain's reclaim shortcut (`_verified_accepted_submit_forecast_retry`) is
  likewise `False` for that decision, so the stage resumes the terminal instead of
  re-submitting the spent idempotency key.

That is the intended anti-duplicate-submission direction: a reservation whose identity was
never verifiable must not be revived under the same key. Liveness is preserved because
each new attempt mints a **new** retry-suffixed idempotency key
(`<run_id>:forecast:retry_<N>` / `job_..._forecast_retry_<N>`), which reserves normally
alongside the released row.

### `blocked_strict_warm_start_init_state_mismatch` candidates

The candidate ladder now checks the stage-scoped retry budget before emitting
`retry_strict_warm_start_terminal_init_state_mismatch`. When the forecast-stage attempt
has reached `NHMS_SCHEDULER_RETRY_LIMIT` (default `3`), the candidate is emitted as
**blocked** with `state_evidence.decision =
blocked_strict_warm_start_init_state_mismatch` and a `retry_policy` block
(`automatic_retry_allowed: false`, `manual_retry_required: true`, `attempt`,
`retry_limit`). That decision is deliberately absent from both force-resubmit whitelists,
so it can never trigger a forced terminal resubmission or replacement retry.

Manual re-entry, in order:

1. Read the pass evidence: `candidates[].state_evidence.retry_policy.attempt` and
   `.retry_limit`. Stage-scoped `attempt` is `max(flat retry_count, suffix-derived
   attempt of the stage-matching job rows)` (`scheduler_state_rows.py:425-453`): only
   rows whose authoritative `stage` matches contribute their `*_retry_<N>` suffix, so a
   `reserved` master with `retry_count=0` still reports the real attempt count as long as
   a `*_forecast_retry_<N>` row survives the candidate-state job-limit truncation.
2. Decide whether re-running is actually correct. If the init-state identity mismatch is a
   data defect, fix the data first — the budget is protecting you from re-submitting the
   same mismatch forever.
3. To re-open the ladder, raise `NHMS_SCHEDULER_RETRY_LIMIT` above the recorded `attempt`
   and restart the scheduler service. Below-budget behaviour is byte-identical to the old
   retry decision, so the candidate is selected again.
   **`NHMS_SCHEDULER_RETRY_LIMIT` is one GLOBAL budget shared by every retry decision
   family in the deployment** (`scheduler_config.py` `retry_limit` is injected into every
   `scheduler_candidates.py` state provider, not per-decision), so raise it only
   temporarily and restore the previous value immediately after the re-entry lands —
   otherwise every failure family gains the inflated budget, which is exactly the
   unbounded-spin class #1160 guarded against.
   **Precondition — this only produces a new submission when a higher-attempt
   `*_forecast_retry_<N>` row outranks the released base row for the stage.** For a
   released master (`reservation_lost` + unbound), `_terminal_stage_needs_manual_retry`
   (`chain_forecast_orchestrator_cycle.py:154-158`) short-circuits to the reclaim
   shortcut, which is `False` for `identity_mismatch_released`; which row represents the
   stage is decided by `_stage_job_sort_key` (highest attempt among the non-active
   matches). So:
   - **Retry-row geometry** (the real `2026072000` journal, which holds `retry_87` /
     `retry_117` rows): the retry row wins the sort, the forced-resubmit path applies, and
     the chain mints `*_retry_<N+1>` with idempotency key `<run_id>:forecast:retry_<N+1>`.
   - **Flat geometry** (only the released base row, `retry_count=0`, no retry-suffixed
     row): the released row itself represents the stage, the chain resumes that terminal,
     and **no submission happens no matter how high the budget is raised**. Re-entry then
     needs a new candidate identity (a new cycle/run), not a budget change.
4. Never lower `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT` to `0` as a "fix": that only
   restores the wedge (no release, `PIPELINE_ALREADY_ACTIVE` every pass) and silently
   freezes `identity_blocked_streak` at its current value.

Verified by:

- `tests/test_gateway_reconcile.py::test_identity_mismatch_released_row_is_a_non_reclaimable_terminal`
  — the released row is not reclaimable and a retry-suffixed key still reserves.
- `tests/test_warm_start_chaining.py::test_released_reservation_reenters_only_through_a_higher_attempt_retry_row`
  — the step-3 precondition, both directions: with a higher-attempt retry row the chain
  mints `*_retry_<N+1>` and submits; in the flat geometry it resumes the released terminal
  and submits nothing.
- `tests/test_production_scheduler.py::test_identity_blocked_release_unwedges_pipeline_already_active`
  — three real scheduler passes: the first two record `submission_failed` /
  `PIPELINE_ALREADY_ACTIVE`, the release pass submits.
- `tests/test_production_scheduler.py::test_db_free_strict_warm_start_terminal_mismatch_blocks_when_budget_exhausted`
  — below-budget vs exhausted decisions.

### Cycle `2026072000` disposition (observed 2026-07-29 — tasks 4.1 receipt)

**Status: observed on the production node-22 scheduler.** Fix deployed at `5ed81a36`
(2026-07-29T12:19Z); four natural timer passes with zero manual scheduler action. The
prediction held on every clause: the streak climbed 1 → 2 → 3 (= limit) across three
passes with both jobs in lockstep, the release pass
(`scheduler_2026072913_d459e00da10b`, 13:02–13:09Z) recorded
`identity_mismatch_released, identity_blocked_streak=3, status=reservation_lost` for
both `…retry_87` and `…retry_117`, and the next pass
(`scheduler_2026072913_e2b9cd8dc6e0`) showed `reserved_unbound.outcomes: []` with
`timing.pass.status` flipping `restart_reconciled → planned`. No pass in the window
recorded `submission_failed` / `PIPELINE_ALREADY_ACTIVE`, and all 36 candidates settled
on `blocked_strict_warm_start_init_state_mismatch` every pass instead of being
re-selected for submission.

Receipt: `artifact path:
/scratch/frd_muziyao/nhms-prod/workspace/scheduler/evidence/scheduler_2026072913_d459e00da10b.json`
(release; predecessors `scheduler_2026072912_9cb232b2a0e4`, `…_c40f23d2454a`, successor
`…_e2b9cd8dc6e0`) · `observed on: 2026-07-29T12:41Z–13:19Z` · `released rows + streak
trajectory: …retry_87 (attempt 88, anchor submission_attempt_started_at
2026-07-27T01:39:13Z, streak 1→2→3, released 13:02:24Z) and …retry_117 (attempt 118,
anchor 2026-07-27T01:39:15Z, streak 1→2→3, released 13:02:33Z); anchors were never
self-refreshed by streak writes` · `post-release direction of the (run_id, forecast)
pair: stable stop — no new *_retry_N minted (max suffix unchanged at 87/117),
error_code=null invariant held, journal status distribution for the cycle
failed=202/succeeded=8/reservation_lost=4 (2 pre-fix supersession losses + these 2
releases)`. Full receipt: PR #1178 / issue #1173 comments (2026-07-29).

The residual risk accepted here is that a released reservation whose Slurm job was in fact
alive would have been abandoned; that is bounded by the three consecutive-pass
requirement, the grace window, the unbound compare-and-swap, and by the retry budget
refusing to mint a competing submission for the same family. To triage an
abandoned-but-alive suspicion, take the `idempotency_key` from the evidence row and query
accounting by the submission comment convention
(`services/orchestrator/reservation.py:40-43`):

```bash
sacct -a --comment "nhms_idem:<idempotency_key>"
```

On this cluster accounting does not store job comments (#1116), so that query normally
returns nothing; fall back to `sacct -a --name nhms_forecast` narrowed to the reservation's
`submission_attempt_started_at` window and match by user/account and submit time.

## Residual Risks

Repeated basin failures may indicate model input defects. Do not publish affected downstream frequency or tile outputs until QC is accepted.
