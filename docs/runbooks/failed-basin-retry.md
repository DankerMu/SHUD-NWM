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

## Reserved rows held by `comment_accounting_unproven` (#1116)

Since #1116 the comment querier probes `scontrol show config` once per reconcile pass and
refuses every comment query — owner-scoped, global and legacy — unless
`AccountingStoreFlags` contains the `job_comment` flag. **This cluster does not store it**
(`AccountingStoreFlags = (null)`, Slurm 23.11.4, measured on node-22 2026-08-18), so each
reserved-unbound cohort master is recorded `accounting_unavailable` /
`comment_accounting_unproven` and **stays `reserved`** instead of being demoted.

The refusal is deliberate: a comment search on a cluster that never stored the comment can
only ever answer "not found", so reading that answer as a confirmed absence demotes a live
reservation to `reservation_lost`, and the reclaim path then re-`sbatch`es the same cohort
— a silent double submission. The price of refusing is real and must be paid by hand: a
`reserved` row is not terminal, so **every later pass of that cycle keeps failing with
`PIPELINE_ALREADY_ACTIVE`** and records `submission_failed` with zero Slurm submissions
until the row is disposed of. This outcome class has no automatic exit — it does not feed
`identity_blocked_streak` (that counter only counts `identity_mismatch_blocked`) and never
self-releases.

### Finding the held rows

- Pass evidence: `restart_reconcile.reserved_unbound.outcomes[]` entries with
  `action=query_unavailable`, `reconciliation_decision=accounting_unavailable` and
  `reconciliation_reason_class=comment_accounting_unproven`. Take `job_id` and
  `idempotency_key` from the entry. On a pass that hit the evidence byte limit the
  bounded compaction keeps only `job_id` / `action` / `status` /
  `reconciliation_reason_class` (plus streak/quarantine fields) — the
  `reconciliation_decision` and `idempotency_key` keys may be absent; filter by the
  reason-class token alone and recover the `idempotency_key` from the journal master
  row below.
- Journal: the same `reconciliation_reason_class` is persisted on the cohort master row,
  whose `status` is still `reserved` and whose `slurm_job_id` is still null.
- Scheduler log, once per pass, tells the two unproven causes apart: `comment storage probe
  could not execute: …` means `scontrol` failed or was unreachable (a deployment fault —
  fix that first, the cluster may well store comments; while the fault persists, each
  pass's `accounting_unavailable` write also resets any accumulated
  `identity_blocked_streak`, so the § `identity_mismatch_released` ladder cannot fire
  either). `accounting does not store job comments: AccountingStoreFlags lacks
  job_comment` normally means the cluster is provably comment-less — but the same message
  also fires when the config output has no `AccountingStoreFlags` line at all (for
  example a pre-20.11 Slurm using the legacy `AccountingStoreJobComment` key, which the
  probe deliberately does not read); when in doubt, run `scontrol show config | grep -i
  AccountingStore` by hand before concluding the capability is absent.

### Deciding whether the job is actually in flight

`sacct -a --comment "nhms_idem:<idempotency_key>"` is useless here by construction. Use the
same name-scoped fallback as the identity-blocked triage above (§ `identity_mismatch_released`,
"To triage an abandoned-but-alive suspicion"):

```bash
sacct -a --name nhms_forecast \
  --starttime <submission_attempt_started_at> --endtime now \
  --format=JobID,JobName,State,User,Account,Submit
squeue -a --name nhms_forecast
```

Match by submit time inside the reservation's attempt window and by user/account (the row's
`expected_slurm_user` / `expected_slurm_account`, when `slurm_ownership_required` is set).
`squeue` covers the still-queued/running half without the accounting propagation lag.

### Disposition — what exists today

1. **Fix the cluster, which is the only automatic exit.** Set `AccountingStoreFlags=job_comment`
   in `slurm.conf` and `scontrol reconfigure` (a cluster-admin action, not a repo command).
   The probe then passes and reconcile resumes binding and demoting normally. Note the flag
   is applied at submission time: it does **not** retroactively attach comments to jobs
   already submitted, so it unwedges future attempts, not the rows already held.
2. **In flight:** do not touch the row. Let the job reach its terminal state — but be aware
   reconcile still cannot bind it, because binding needs the comment match that this cluster
   cannot serve. The row remains held, so it still ends up in case 3.
3. **Confirmed dead** (no matching job in `sacct`/`squeue` for the attempt window): **there
   is no safe operator mechanism to demote the row today.** Verified against the code:
   - The manual-retry surface (`POST /api/v1/runs/{run_id}/retry`, `RetryService` /
     the file journal's `record_manual_repair`) only accepts a latest job whose status is
     `failed` / `submission_failed` / `partially_failed` / `permanently_failed` /
     `cancelled` (`retry.py:73-74`); `reserved` is neither that nor an active status, so the
     call raises `RetryNotFoundError` and writes nothing.
   - `reclaim_pipeline_job_reservation` requires `status=reservation_lost` with
     `reconciliation_decision=absence_retry_permitted`
     (`file_orchestration_journal.py:1711-1720`); it returns `None` for a `reserved` row.
   - The `nhms-pipeline` CLI has no reservation/status subcommand (`cli.py:742-800`).

   The only physical option left is hand-editing the journal, which is unsupported here and
   is exactly the write this gate exists to prevent. Escalate instead: the missing
   operator-facing demotion tool is tracked as the #1116 follow-up, and until it lands the
   affected cycle stays wedged on `PIPELINE_ALREADY_ACTIVE`. A new cycle (new candidate
   identity) is unaffected and keeps running.

Verified by:

- `tests/test_gateway_reconcile.py::test_reserved_row_stays_reserved_on_a_cluster_that_does_not_store_comments`
  — a genuinely in-flight job with an empty `Comment` no longer demotes the reservation.
- `tests/test_gateway_reconcile.py::test_comment_storage_probe_requires_job_comment_in_accounting_store_flags`
  — the `(null)` / `job_comment` / missing-line parse vectors and the two distinct warnings.

## Missing accounting vs wrong accounting (缺账 vs 错账)

A cycle whose whole cohort ran to `succeeded` can still be judged `gap` forever. When the
backfill audit keeps naming the same oldest cycle pass after pass while its successor
cycles never even produce candidates, read the completion verdict inputs before touching
anything — the two failure classes look identical in the pass evidence and have opposite
dispositions.

| | 缺账 (missing accounting / `absent`) | 错账 (wrong accounting / `conflict`) |
|---|---|---|
| Terminal row | records **no** `init_state*` / `hydro_run` identity at all | records an identity that **disagrees** with the strict warm-start resolution |
| Typical producer | accepted-submit cohort rows reserved before #1183 | a run genuinely started from a stale or repaired checkpoint |
| Verdict since #1183 | `complete` **iff** the successor state is in the snapshot index with `usable_flag=true`; otherwise still `gap` | `gap`, always — strictness is not relaxed here |
| Disposition | none: the chain converges by itself on the next natural pass | investigate the lineage; re-run the cycle, never widen the judgement |

How to tell them apart:

1. Take the oldest gap from `backfill.audit[]` / `source_cycles[]` in the pass evidence.
2. Read that cycle's terminal journal rows (`query_pipeline_jobs_by_cycle`, or the
   `latest/<source>/<cycle>/<model>.json` view). No `init_state_id` anywhere in the
   terminal row or its `hydro_run` means 缺账; a present-but-different `init_state_id` /
   checksum / `valid_time` means 错账.
3. Confirm the continuity proof independently: the successor cycle's state must be in the
   state snapshot index with `usable_flag=true`. Absence tolerance requires that proof —
   a missing or unusable successor state keeps the verdict at `gap`, and a successor
   evaluation that reached no verdict at all (outside the strict window, no next allowed
   cycle) is silence, not proof.

Cycles reserved after #1183 book their per-model init-state identity on the cohort master
row at reservation time, and each per-model terminal row copies its own entry, so new
cycles land in the 匹配 or 冲突 columns rather than in 缺账. Historical rows are never
rewritten — no migration, no backfill.

### Cycle `2026072000` disposition (accounting-absence class)

`2026072000` ran its whole chain to `succeeded` (state_save_qc terminal, +12h checkpoint in
the index with `usable_flag=true`), but its accepted-submit cohort terminal rows carry no
init-state identity, so the pre-#1183 verdict judged the unaccounted rows exactly like
conflicting ones. `available_gaps[:1]` therefore pointed at `2026072000` on every pass and
`2026072012`…`2026072900` never became candidates even though their raw/manifest/warm
inputs were ready.

**Disposition: no manual action.** The change
`openspec/changes/scheduler-completion-verdict-absence-tolerance` (issue #1183) makes
absence-with-proven-continuity `complete`, so after deploying it the next natural pass
retires `2026072000` from the oldest-gap slot and the chain advances cycle by cycle on its
own. Do **not** hand-edit the journal, do not re-run `2026072000`, and do not raise
`NHMS_SCHEDULER_RETRY_LIMIT` for it: the retry budget is not what is holding this cycle.
If a cycle still gaps after the deploy, it is 错账 (or its successor state is missing) —
re-read the two inputs above rather than relaxing the judgement.

## Residual Risks

Repeated basin failures may indicate model input defects. Do not publish affected downstream frequency or tile outputs until QC is accepted.

Absence tolerance accepts one named lineage risk: the successor-state proof shows that a
usable checkpoint exists for the next cycle, not that the run which produced it used a
canonical warm init. A cold-seed-admitted run registers under the same cycle id, so that
lineage break is now judged `complete` where it used to gap. The conflict path is
unchanged, so a wrong recorded identity is still caught.
