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

### Finding the rows

- Scheduler pass evidence: `restart_reconcile.reserved_unbound.outcomes[]` carries
  `action`, `identity_blocked_streak` and `durable_write_count`. The counter survives the
  bounded (size-limited) evidence compaction, so it is readable even on a pass that hit
  the evidence byte limit.
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
   `.retry_limit`. `attempt` is derived stage-scoped from the `*_forecast_retry_<N>` job
   ids, so a `reserved` master with `retry_count=0` still reports the real attempt count.
2. Decide whether re-running is actually correct. If the init-state identity mismatch is a
   data defect, fix the data first — the budget is protecting you from re-submitting the
   same mismatch forever.
3. To re-open the ladder, raise `NHMS_SCHEDULER_RETRY_LIMIT` above the recorded `attempt`
   and restart the scheduler service. Below-budget behaviour is byte-identical to the old
   retry decision, so the candidate is selected again and the chain mints the next
   `*_retry_<N+1>` job id / idempotency key.
4. Never lower `NHMS_SCHEDULER_IDENTITY_BLOCKED_STREAK_LIMIT` to `0` as a "fix": that only
   restores the wedge (no release, `PIPELINE_ALREADY_ACTIVE` every pass).

Verified by `tests/test_gateway_reconcile.py::test_identity_mismatch_released_row_is_a_non_reclaimable_terminal`
(non-reclaimable terminal + retry-suffixed key still reserves),
`tests/test_production_scheduler.py::test_identity_blocked_release_unwedges_pipeline_already_active`
(the released row leaves the active set) and
`tests/test_production_scheduler.py::test_db_free_strict_warm_start_terminal_mismatch_blocks_when_budget_exhausted`
(below-budget vs exhausted decisions).

### Cycle `2026072000` disposition

The wedged `2026072000` forecast pair needed no manual action: after the fix is deployed,
three natural passes drive the streak to the limit and release both rows, the cycle stops
recording `submission_failed`, and the 36 already-completed candidates settle on
`blocked_strict_warm_start_init_state_mismatch` (their forecast-stage attempts, 87 and
117, are far past the retry limit) instead of being re-selected every pass. The residual
risk accepted here is that a released reservation whose Slurm job was in fact alive would
have been abandoned; that is bounded by the three consecutive-pass requirement, the grace
window, the unbound compare-and-swap, and by the retry budget refusing to mint a competing
submission for the same family.

## Residual Risks

Repeated basin failures may indicate model input defects. Do not publish affected downstream frequency or tile outputs until QC is accepted.
