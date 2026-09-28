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

- `reclaim_pipeline_job_reservation` revives exactly two decisions:
  `absence_retry_permitted` (automatic) and `operator_verified_absence` (the
  #1564 operator demotion); every other shape — including
  `identity_mismatch_released`, `identity_mismatch_blocked`, and any
  `matched_bound` row — returns `None` and writes nothing.
- The cycle chain's reclaim shortcut (`_verified_accepted_submit_forecast_retry`) is
  likewise `False` for the identity decisions, so the stage resumes the terminal instead of
  re-submitting the spent idempotency key.

That is the intended anti-duplicate-submission direction: a reservation whose identity was
never verifiable must not be revived under the same key. A released row that has been
independently assessed by an operator now has a separate supported recovery door:

```bash
/scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli \
  recover-released-identity-blocked-reservation \
  --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
  --job-id "<released_master_job_id>" \
  --attest
```

Run the same command without `--attest` first to inspect eligibility. `--attest` records an
operator judgement, not a Slurm-side absence proof; confirm the cohort is gone before using
it. This command does **not** reclaim or revive the released key. It adds the attestation
consumed by the ordinary scheduler path, which mints a **new** retry-suffixed idempotency key
(`<run_id>:forecast:retry_<N>` / `job_..._forecast_retry_<N>`) and reserves that successor
alongside the released row.

If any recovery door above is refused with `file_journal_job_id_scope_mismatch`, the row's
`job_id` encodes a different `(source, cycle)` than the row itself carries: no API can
transition it, and its `reconcile-inventory` anchor can abort the whole reconcile scan. See
[`current-production-ops.md`](current-production-ops.md) §8.11 for the census and the recovery
path, and §8.10 for the journal-root realpath precondition both depend on.

### Operator-action decisions (`manual_retry_required: true`)

Four DB-free decisions wait on an operator. Until the read-only operator-action listing
CLI lands (#1186, follow-up PR), list them by reading the newest `scheduler_*.json` pass
evidence file under the evidence root and filtering `blocked_candidates` by the decision
literal (plus the not-selected `source_cycles` entries for breaker-released cycles); the
exact commands, the evidence-root source and node-22 execution discipline are in
[`node22-control-plane-manual-recovery.md`](node22-control-plane-manual-recovery.md).

- `permanent_failure` — the failure classifier called the error permanent (for example
  `ARTIFACT_NOT_FOUND`), so the run never retries by itself. Fix the cause, then mark the
  run once with `scripts/node22_manual_retry_failed_runs.py` (preview, then `--execute`).
- `cancelled_manual_retry_required` — a cancelled run is not retried automatically. Confirm
  the cancellation was not intentional, then use the same manual-retry script.
- `blocked_journal_predecessor_identity_quarantine` — the §8.7 breaker. The journal row is
  terminal-success, so a manual-retry marker never reaches it. Re-enter once with
  `confirm-operator-reentry --pin <quarantine_rerun_count> --recorded-init-state-id <token>`; triage
  first per [`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md).
  The breaker pin is the model's quarantine rerun count (cohort masters whose quarantine
  provenance names the model, whatever their terminal status or token), not `occurrences`;
  read it from the dry-run receipt's `live.quarantine_rerun_count`. The confirmation is
  consumed when the rerun is accepted for submission: a Slurm failure does not restore it, so
  re-confirm with the new live count if another re-entry is needed.
  **A candidate still showing blocked does not mean the confirmation did not take effect:**
  first check whether the dry-run receipt's live count already moved +1 (the file journal does
  not update the `hydro_run` row on a same-`run_id` rerun, #2397, so even a rerun with the
  correct lineage still shows as breaker-blocked). If it moved, do not confirm again.
- `blocked_cohort_membership_unprovable` — a `permanently_failed` multi-member cohort row
  whose recorded `cohort_members` is incomplete, so it can not be attributed (#2603). Fix the
  cause, then mark the cohort run once with the same manual-retry script; see
  [`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md).
- `blocked_strict_warm_start_init_state_mismatch` — the strict warm-start retry budget; see
  the next section (`confirm-operator-reentry --pin <budget_reentry_count>`).
- `blocked_operator_reentry_restart_stage_refused` — the sink refusal (#1555). The
  confirmation matched, but the candidate would have restarted somewhere other than
  `forecast` (typically a `convert` rewrite provoked by incomplete canonical readiness),
  and the re-entry provenance is stamped only at a forecast-cohort reservation. Nothing
  submitted, nothing consumed, the confirmation stays armed. **Do not confirm again.**
  Read `state_evidence.operator_reentry_sink_refusal.refused_restart_stage`, repair the
  pre-forecast input out of band, and let the next pass restart the re-entry at `forecast`.
  If the pass evidence came back bounded-summarized, only `refused_restart_stage` survives
  (as a top-level `refused_restart_stage` on the summary row) and every other field of the
  block is dropped — `refused_restart_from_stage`, `effective_restart_stage`,
  `fresh_full_chain`, `confirmation`, and, on a corrupted-confirmation refusal, the
  `malformed_confirmation` / `confirmation_type` discriminator. Read the unsummarized pass
  evidence if you need any of them. A refusal that named no restart stage at all keeps the
  field absent rather than null, so identify that row by its blocked decision.
  Field-by-field triage is in
  [`scheduler-dbfree-typed-reasons.md`](scheduler-dbfree-typed-reasons.md).

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
   attempt of the stage-matching job rows)` (`scheduler_state_rows._state_retry_attempt`): only
   rows whose authoritative `stage` matches contribute their `*_retry_<N>` suffix, so a
   `reserved` master with `retry_count=0` still reports the real attempt count as long as
   a `*_forecast_retry_<N>` row survives the candidate-state job-limit truncation.
   Since #1179 that last clause no longer applies to the STAGE-MATCHING ROW SCAN half of
   that `max`: before truncating, the file-journal projection records each canonical
   stage's maximum attempt in the state key `stage_retry_attempt_floors`, and a
   stage-scoped read maxes the row scan against that floor. So the budget also binds in
   the reverse geometry where the `*_forecast_retry_<N>` row is OLDER than `job_limit`
   fresher rows of other stages (it used to be dropped and the attempt silently read
   `0`). The flat `retry_count` half is still aggregated over the surviving rows and so is
   still window-sensitive, exactly as it was before #1179 — including the case where
   another stage's persisted count reaches this number through the flat channel (#1579).
   The row selection itself is untouched — still pure freshness — so `pipeline_status`,
   `failed_stage`, `restart_stage`, the active-job scan and the flat `retry_count` all
   read exactly what they read before. Three consequences to know about: a stage-LESS
   attempt read (no `stage` argument) still answers with the flat candidate-scoped count
   and never sees the floors; a floor whose every contributing row is judged
   non-authoritative for the candidate leaves with those rows, so a cycle cohort row's
   attempt cannot become this candidate's — since #1586 that narrowing covers the
   strict-warm-start budget read's ROW-SCAN channel too, so an in-window suffixed
   execution-cohort row no longer spends the candidate's budget; and under the geometry
   where the failed row is outside the window AND a succeeded terminal-stage row empties
   the stage keys, `_failed_stage` is `None` — since #1577 the manual-retry mint recovers
   the canonical stage from the newest adopted marker's exact target lineage against the
   carried floor sources and mints `_retry_<N+1>` instead of re-deriving the consumed
   `_retry_1` identity (the recovery fails closed when lineage is foreign, absent, stale,
   non-canonical, or maps to disagreeing stages). When the stage IS
   nameable the mint moves with the floor — it mints `_retry_<N+1>` off the true attempt
   rather than the window-local one. The DB-backed read path truncates in SQL upstream of
   the projection, so an attempt outside that window never reaches the floors either
   (#1572).
2. Decide whether re-running is actually correct. If the init-state identity mismatch is a
   data defect, fix the data first — the budget is protecting you from re-submitting the
   same mismatch forever.
3. To re-open the ladder for ONE candidate, record a pinned one-shot confirmation (#1768):

   ```bash
   /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli \
     confirm-operator-reentry \
     --journal-root "$NHMS_SCHEDULER_JOURNAL_ROOT" \
     --source-id <source_id> --cycle-time <cycle_time> --model-id <model_id> \
     --decision blocked_strict_warm_start_init_state_mismatch \
     --pin <budget_reentry_count> \
     --operator "<operator>" --reason "<why>"
   # dry run first; append --attest once the receipt checks out
   ```

   The blocked evidence names this channel in `retry_policy.operator_reentry_command` /
   `retry_policy.recovery_runbook`. The pin is the model's **budget re-entry count** (cohort
   masters whose `strict_warm_start_budget_reentry_model_ids` provenance names the model,
   whatever their terminal status, job id or retry suffix), not `retry_policy.attempt`: read
   it from the dry-run receipt's `live.budget_reentry_count`. The writer refuses any other
   pin with `pin_mismatch` (exit 2).
   **The writer can not see whether the budget is exhausted** (#2400 residual, not closed by
   this change: right pin, wrong time). A confirmation written before exhaustion stays armed
   until it is consumed, so once the budget is exhausted it releases one re-entry with no new
   signature. Only confirm a target that the NEWEST pass evidence file currently lists in
   `blocked_candidates` as `blocked_strict_warm_start_init_state_mismatch`, checking `source_id`,
   `cycle_time` and `model_id` verbatim, and never while that model's rerun is in flight. If
   a confirmation was written in error (target, pin or timing), stop and escalate; do NOT
   write another one over it (the earlier one stays armed). While the confirmation's `pin` equals that live count,
   the next pass emits `retry_strict_warm_start_terminal_init_state_mismatch` once (with an
   `operator_reentry_confirmation` block in `state_evidence`). The confirmation is consumed
   when that rerun is accepted for submission: the reservation stamps the provenance on the
   cohort master, the count moves +1 whatever the rerun's outcome or the job-id prefix it
   mints under, and the candidate is not re-entered again. A Slurm failure does not restore
   it; re-confirm with the new live count if another re-entry is needed.
   `NHMS_SCHEDULER_RETRY_LIMIT` is not touched; see
   [`node22-control-plane-manual-recovery.md`](node22-control-plane-manual-recovery.md) for
   the receipt, refusals and known limitations. The same precondition below (a
   higher-attempt retry row must outrank a released base row) applies.

   Raising the global budget is the fallback only when the confirmation channel is not
   available (a DB-backed repository without the confirmation accessor keeps the budget
   fail-stop): raise `NHMS_SCHEDULER_RETRY_LIMIT` above the recorded `attempt` and restart
   the scheduler service. Below-budget behaviour is byte-identical to the old retry
   decision, so the candidate is selected again.
   **`NHMS_SCHEDULER_RETRY_LIMIT` is one GLOBAL budget shared by every retry decision
   family in the deployment** (`scheduler_config.py` `retry_limit` is injected into every
   `scheduler_candidates.py` state provider, not per-decision), so raise it only
   temporarily and restore the previous value immediately after the re-entry lands —
   otherwise every failure family gains the inflated budget, which is exactly the
   unbounded-spin class #1160 guarded against.
   **Precondition — this only produces a new submission when a higher-attempt
   `*_forecast_retry_<N>` row outranks the released base row for the stage.** For a
   released master (`reservation_lost` + unbound),
   `ForecastOrchestratorCycleMixin._terminal_stage_needs_manual_retry` short-circuits to the
   reclaim shortcut, which is `False` for `identity_mismatch_released`; which row represents the
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

- `tests/test_gateway_reconcile_identity_release.py::test_identity_mismatch_released_row_is_a_non_reclaimable_terminal`
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
`AccountingStoreFlags` contains the `job_comment` flag. Since #1565 the probe is tri-state
(present-with-`job_comment` / present-without / unknown): a probe failure or a missing
`AccountingStoreFlags` line is **unknown** and stays query-free, while an explicitly
comment-less cluster may enter the § automatic unique fallback below. **This cluster does
not store it** (`AccountingStoreFlags = (null)`, Slurm 23.11.4, measured on node-22
2026-08-18), so each reserved-unbound cohort master that the fallback cannot uniquely prove
is recorded `accounting_unavailable` / `comment_accounting_unproven` and **stays
`reserved`** instead of being demoted.

The refusal is deliberate: a comment search on a cluster that never stored the comment can
only ever answer "not found", so reading that answer as a confirmed absence demotes a live
reservation to `reservation_lost`, and the reclaim path then re-`sbatch`es the same cohort
— a silent double submission. The price of refusing is real and must be paid by hand: a
`reserved` row is not terminal, so **every later pass of that cycle keeps failing with
`PIPELINE_ALREADY_ACTIVE`** and records `submission_failed` with zero Slurm submissions
until the row is disposed of. This durable outcome class has no automatic exit: only a
durable exact-comment `reconciliation_decision=identity_mismatch_blocked` feeds
`identity_blocked_streak`. An unsuccessful comment-less fallback may expose the same
action name in pass evidence, but its durable row stays `accounting_unavailable` /
`comment_accounting_unproven`, keeps streak zero, and never self-releases.

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
- Scheduler log, once per pass, tells all three capability verdicts apart:
  `comment storage probe could not execute: …` means `scontrol` failed or was unreachable
  (unknown; fix the deployment fault first because the cluster may store comments), while
  `comment storage capability unknown: AccountingStoreFlags line is absent` means the
  command ran but omitted the authoritative config line (also unknown; no fallback query
  issues). Only `accounting does not store job comments: AccountingStoreFlags lacks
  job_comment` proves the cluster explicitly comment-less and opens the conservative
  fallback. While either unknown condition persists, each durable
  `accounting_unavailable` transition resets any accumulated `identity_blocked_streak`, so
  the § `identity_mismatch_released` ladder cannot fire. A legacy
  `AccountingStoreJobComment` key does not substitute for `AccountingStoreFlags`; inspect
  `scontrol show config | grep -i AccountingStore` before changing cluster configuration.

### Deciding whether the job is actually in flight

`sacct -a --comment "nhms_idem:<idempotency_key>"` is useless here by construction. Use the
same name-scoped fallback as the identity-blocked triage above (§ `identity_mismatch_released`,
"To triage an abandoned-but-alive suspicion"):

```bash
sacct -a --name nhms_forecast \
  --starttime <submission_attempt_started_at> --endtime now \
  --format=JobID,JobName,State,User,Account,Submit,SubmitLine
squeue -a --name nhms_forecast
```

Match by submit time inside the reservation's attempt window and by user/account (the row's
`expected_slurm_user` / `expected_slurm_account`, when `slurm_ownership_required` is set).
`squeue` covers the still-queued/running half without the accounting propagation lag.
`SubmitLine` still carries the sbatch argv even though `Comment` is empty: an array-task row
(`<master>_<task>`) whose `SubmitLine` contains `--comment=nhms_idem:<idempotency_key>` is this
reservation's own job (#2655); `.batch`/`.extern` step rows have an empty `SubmitLine`.

### Automatic unique fallback on an explicitly comment-less cluster (#1565)

Since #1565 the comment capability probe is **tri-state**: a present
`AccountingStoreFlags` line containing `job_comment` is comment-storing;
a present line explicitly lacking it — including `(null)` — is explicitly
comment-less; a probe failure or an absent `AccountingStoreFlags` line is
**unknown**. Only an explicitly comment-less cluster and a current
accepted-submit forecast cohort with a strict UTC `submission_attempt_started_at`
plus non-empty `expected_slurm_user`/`expected_slurm_account` may enter the
**conservative name-window fallback**:

- One bounded `sacct --name nhms_forecast` query per reservation, from the
  immutable attempt anchor through the querier's frozen `now`, with
  `--user=<expected_slurm_user> --accounts=<expected_slurm_account>` and
  `--format=JobID,JobName,State,ExitCode,Comment,User,Account,Submit,SubmitLine`
  (`SubmitLine` since #2655; parsed as the rejoined remainder of the row, and
  requested **only** by this fallback query — the exact-comment lane keeps its
  seven-field format).
- Both bounds are rendered as host-local wall-clock strings (same rule as the
  exact-comment query). A timezone-less `Submit` value is interpreted in the
  host-local timezone and converted to UTC. Missing/unparsable `Submit` is
  transient denial only for an otherwise eligible forecast/owner row; an
  out-of-window row is ineligible.
- Accepted forecast array/step ids normalize to their bare numeric master id;
  at most two distinct masters are retained (zero / unique / ambiguous is all
  that is needed). Forcing, batch/extern, and unrelated job names are discarded
  before candidate classification, so they contribute to `fallback_no_match`,
  not `identity_mismatch_blocked` or malformed-Submit evidence.
- A candidate must have an in-window submit instant, exact user/account, and a
  forecast-family job name before it reaches the existing identity gates. A
  candidate with an **empty** comment may pass both comment gates (the cluster
  never stored it); a present-but-different comment remains fatal.

**SubmitLine exact key (#2655).** The scheduler submits the gfs and IFS
forecast cohorts seconds apart, so a `now`-ended window routinely contains the
sibling source's array too; counting masters alone would hold both
reservations as `ambiguous_fallback_match` forever. Each eligible row's
**submit-line key** is the single distinct `--comment=<value>` token in its
`SubmitLine` (missing/empty line, no token, or two distinct values → no key); a
master's key is known only when all its eligible rows agree. Classification
then picks one **match basis**:

- `submitline_exact` — every eligible master in the window has a known key.
  Masters whose key differs from this reservation's `nhms_idem:<idempotency_key>`
  are provably another submission and are **excluded before** the two-master
  cap. Exactly one remaining master is the unique candidate; two or more
  remaining masters are a genuine double submission and stay
  `ambiguous_fallback_match`. Zero remaining is `fallback_no_match`.
- `name_window_count` — at least one eligible master has an unknown key
  (e.g. an older sacct without `SubmitLine`, a key-less or inconsistent line).
  Nothing is excluded and the pre-#2655 count-only rules apply to every
  eligible master, so a known-foreign master plus an unknown-key master is
  still `ambiguous_fallback_match`.

The key is never consulted on comment-storing or unknown-capability clusters.

“Unique” means one durable claimant, not one result in one query. Exactly one durable
reserved claimant must admit the candidate's Submit instant for that user/account,
and no other current accepted-submit master may own the same accounting
incarnation `(bare Slurm id, canonical Submit)`. Under `submitline_exact` the
durable claimants are only the reserved-unbound attempts whose own idempotency
comment equals the candidate's key — an overlapping sibling with a different key
is not a claimant — and the typed commit re-verifies the key against the
committing row, refusing a foreign key with zero journal bytes (#2655). Under
`name_window_count` every overlapping reserved attempt is a claimant, as before. An active same-id owner always
blocks; a settled same-id row blocks only when its canonical Submit is identical,
while a different Submit proves legitimate numeric-id reuse. Canonical cycle
journal authority decides this check: stale, damaged, or missing flat projections
cannot fabricate or hide an owner. Inventory cleanup and first migration preserve
an anchor-to-flat locator handoff, so a projection crash or concurrent cleanup
cannot create a temporary vacancy.

**Two distinct timestamps, never conflated.** The durable `submitted_at` on a
normal/exact-comment bind is the **gateway acceptance/commit time**; it is never
canonical Slurm accounting `Submit` evidence and therefore cannot prove numeric-id
recycling. Only the attempt-scoped `slurm_accounting_submitted_at` — populated
exclusively from the parsed sacct `Submit` of a successful name-window fallback
bind — is incarnation proof. A settled same-id sibling blocks fail-closed when its
canonical accounting Submit is absent, malformed, gateway-sourced, or
exact-comment-sourced without a canonical Submit, even if the legacy `submitted_at`
differs (including microsecond-only differences). The `slurm_binding_source`
(`gateway_submit` / `slurm_exact_comment` / `slurm_name_window_unique`) and
`slurm_accounting_submitted_at` are immutable for one bound attempt, survive every
defer and terminal projection (which restore the legal current
`reconciliation_source` from binding provenance on `matched_bound`), and clear
only when reclaim starts a new attempt.

Only that claimant-exclusive, fully validated candidate binds — once, with
`reconciliation_source=slurm_name_window_unique` and
`reconciliation_decision=matched_bound` for **either** basis (no new durable
token, so an older checkout still reads the bound row). Every other outcome is
fail-closed and **never** binds, demotes, retries, or increments the streak.
Every fallback outcome that classified at least one remaining master — including
a successful bind — also carries the pass-evidence-only
`fallback_match_basis` (`submitline_exact` | `name_window_count`) in
`restart_reconcile.reserved_unbound.outcomes[]`; it is never persisted:

| Outcome | Pass evidence | Row state |
|---|---|---|
| unique claimant-exclusive candidate | `action=bound`, `reconciliation_decision=matched_bound`, `match_count=1`, `fallback_match_basis` | submitted/bound |
| zero eligible masters (including only non-forecast names, or every master excluded by a foreign key) | `action=fallback_no_match`, `match_count=0` (no basis) | reserved/unbound |
| two or more remaining masters, or more than one durable claimant | `action=ambiguous_fallback_match`, `match_count=2`, `fallback_match_basis` | reserved/unbound |
| one candidate fails identity or same-incarnation occupancy | `action=identity_mismatch_blocked`, `match_count=1`, `fallback_match_basis` | reserved/unbound |
| missing/unparsable `Submit` | `action=query_unavailable`, `reconciliation_reason_class=fallback_submit_unparsable` (pass-only) | reserved/unbound |
| process/timeout/byte/row failure | `action=query_unavailable`, existing bounded-query reason | reserved/unbound |
| non-`.json` residue under `<journal>/pipeline-jobs/` hit by the commit-time claimant scan | `action=journal_quarantined`, `quarantine_reason=file_journal_reconcile_inventory_migration_invalid`, `quarantine_field=pipeline_jobs`, `fallback_match_basis` | reserved/unbound |

Operating notes for the fallback (#2655):

- **Bind throughput is about one held row per pass.** The querier's shared
  whole-query time budget (one deadline per querier session) is consumed by the
  first bind's journal inventory scan, so the other held rows in the same pass
  report transient `action=query_unavailable` /
  `reconciliation_reason_class=process_unavailable` and bind on later passes
  (four held rows converge over about four passes). This is expected
  convergence, **never** a reason to demote.
- **`journal_quarantined` with `file_journal_reconcile_inventory_migration_invalid`
  / field `pipeline_jobs`** means a non-`.json` file (for example a
  `*.json.bak-zombie-<date>` backup) sits under `<journal>/pipeline-jobs/`; the
  commit-time scan refuses it and every bind fails closed with the held tuple
  intact. Operator rule: no backup/residue files under the scheduler journal
  root (precedent #1925). Move any such file out of the journal root, keeping a
  backup; **never** demote for this.
- **An all-foreign-key window is `fallback_no_match`**, not
  `ambiguous_fallback_match`: every master in the window provably belongs to
  another reservation. The node-22 stall probe suppresses only
  `ambiguous_fallback_match:comment_accounting_unproven`, so this outcome is
  not suppressed and raises the stall alert; treat it as "no job of this
  reservation is visible" and route it to the confirmed-dead checks of
  Disposition case 3 below.
- **`SubmitLine` roughly doubles the fallback query's bytes**, so a long-held
  (widening, `now`-ended) window reaches the shared 2 MiB byte budget earlier;
  past it the query fails closed as `query_unavailable` (held, transient).

Every unsuccessful fallback preserves the durable #1564 held tuple byte-for-byte:
`status=reserved`, no `slurm_job_id`, `reconciliation_source=slurm_exact_comment`,
`reconciliation_decision=accounting_unavailable`, and
`reconciliation_reason_class=comment_accounting_unproven`. If that tuple is not
yet present on the first pass, establishing it is the only permitted durable
write. The guarded `nhms-pipeline demote-reserved-job` CAS therefore stays valid
on every fallback-failed row. Fallback failures never enter the
`identity_blocked_streak` release ladder and never create an automatic
absence/release exit.

Zero candidates is **not** an absence proof, and ambiguity never changes
disposal authority: on a comment-less cluster only a uniquely proven live job
binds; row-scoped confirmed-dead disposal remains the documented guarded
operator action.

Terminal inflight identity blocks additionally carry an additive
`reconciliation_reason_class` in
`restart_reconcile.inflight.outcomes[]` (#1795) — one stable clause token
(`cohort_identity_invalid`, `runtime_identity_mismatch`, `master_id_mismatch`,
`comment_mismatch`, `stage_family_mismatch`, `ownership_unproven`,
`ownership_user_mismatch`, `ownership_account_mismatch`,
`cohort_members_unparsable`, `task_identity_values_mismatch`,
`task_identity_values_unparsable`, `task_id_unparsable`, `task_mapping_mismatch`,
`task_job_name_mismatch`, `task_comment_mismatch`) with `action` unchanged as
`identity_mismatch_blocked` and zero durable/status/event writes. The token is
pass evidence only — it is never written to the accepted-submit durable
accounting tuple by the inflight leg.

### Production-safe validation rule

A held `reserved` row is a natural production state, not a release-validation fixture —
it must never be manufactured to produce a receipt. Follow this order, and stop at the
first step that is not satisfied:

1. **Read-only census first.** Use the evidence and journal reads above (pass evidence,
   journal replay, `sacct`/`squeue`) to locate held rows. None of these writes anything.
2. **No natural candidate: stop.** If the census finds no exact held row, there is
   nothing to validate against a live row. Release then relies on the deterministic
   highest-seam chain plus the fault/refusal matrix plus final-head CI — a held row is
   not required.
3. **Never manufacture one.** For a receipt, do not stop the production scheduler, do
   not force the gateway or its endpoint unreachable, do not inject or rewrite held
   authority into the production journal, and do not submit a real cohort. All four are
   forbidden production mutations.
4. **Natural accidents proceed normally.** When a held row does appear naturally, run
   Disposition case 3 below in full — `sacct`/`squeue` proof, typed CAS, receipt, and
   next-pass verification — and keep the complete receipt.

**#1565 node-22 receipt rule.** The automatic-fallback validation on node-22 is
**read-only live accounting plus scratch journal only**: prove the live
capability is explicit `False` (`scontrol show config | grep -i
AccountingStoreFlags`), query historical `nhms_forecast` accounting rows over a
narrow frozen interval to produce one unique result and a wider interval to
produce ambiguity, and exercise the bind only against a **scratch** file journal.
Never `sbatch`, never `scancel`, never change a service, and never write to the
production journal. If no suitable historical rows exist, deterministic tests
stand in instead of manufacturing a held row.

### Disposition — confirmed dead rows: the guarded operator demotion

1. **Fix the cluster — but only after every held row has been through the in-flight
   check above and confirmed dead.** Set `AccountingStoreFlags=job_comment` in
   `slurm.conf` and `scontrol reconfigure` (a cluster-admin action, not a repo command).
   **Warning — ordering matters:** the moment the probe turns True the gate opens for
   **all** held rows, not just future attempts. Jobs submitted while the flag was off
   carry an empty accounting `Comment` forever (the flag applies at submission time and
   is not retroactive), so the comment search still cannot see them: a held row whose
   job is **still running** on a just-fixed cluster is immediately judged a confirmed
   absence past grace, demoted to `reservation_lost` with `absence_retry_permitted`,
   and the next pass reclaims and re-`sbatch`es the cohort — the exact double
   submission this gate exists to prevent. With every held row confirmed dead first,
   reconfiguring is safe and doubles as the demotion mechanism: the next pass demotes
   each dead row through the normal absence path and the retry is minted legitimately.
2. **In flight or completed but unbound:** do not touch the row. On an explicitly
   comment-less cluster whose `sacct` returns `SubmitLine`, reconcile binds it
   automatically once the job is visible (#2655): a later pass (about one held row binds
   per pass; the others report transient `query_unavailable` meanwhile) reports
   `action=bound` with `fallback_match_basis=submitline_exact`, and the following inflight reconcile
   projects the cohort to its terminal status — a `COMPLETED` array is **not**
   recomputed, its members stop reading as `active_duplicate_pipeline`, and the
   scheduler resumes after the completed forecast stage. If the row instead keeps
   reporting `ambiguous_fallback_match` with `fallback_match_basis=name_window_count`,
   some eligible master in the window has no provable key (check the `SubmitLine`
   column above); with `submitline_exact` and `match_count=2`, two masters carry this
   reservation's key — a genuine double submission. A row whose fallback window has grown
   past the scan budget reports `query_unavailable` on every pass instead (its
   `reconciliation_reason_class` in pass evidence is `bounded_output_*`). For all three the
   supported exit is the guarded operator bind `nhms-pipeline bind-reserved-job` (#2668),
   see § "Disposition — completed or running but unbound: the guarded operator bind"
   below; `list-operator-actions` lists such a row as `held_reservation_unresolved`.
   Never demote while a matching job is alive or completed: demotion re-`sbatch`es the
   cohort. Only a row with **no** matching job (case 3) is demoted.
3. **Confirmed dead** (no matching job in `sacct`/`squeue` for the attempt window), when
   reconfiguring the cluster is not an option (or any other held row may still be alive,
   making the cluster-wide gate flip unsafe): use the **row-scoped guarded operator
   command** `nhms-pipeline demote-reserved-job` (#1564). It is the only supported way to
   convert one held row to the reclaimable `reservation_lost` /
   `operator_verified_absence` shape — hand-editing the journal is unsupported and remains
   the exact write this gate exists to prevent.

   The command never asks for interactive confirmation; it requires the explicit
   non-interactive `--confirm` flag and exact compare-and-swap inputs (the persisted
   `submission_attempt` and `submission_attempt_started_at`), plus named operator evidence.
   On node-22:

   ```bash
   # 1. Independently prove the job dead for THIS attempt window (name/user/account/submit):
   sacct -a --name nhms_forecast \
     --starttime <submission_attempt_started_at> --endtime now \
     --format=JobID,JobName,State,User,Account,Submit,SubmitLine
   squeue -a --name nhms_forecast
   #    The row is safe to demote only when no matching job survives both queries.
   #    A row whose SubmitLine carries --comment=nhms_idem:<this row's idempotency_key>
   #    is this reservation's own job: never demote while it exists (#2655).

   # 2. Read the exact persisted attempt and anchor off the HELD master row.
   #    The scheduler pass evidence is NOT authoritative for the pre-state: it is
   #    only a point-in-time locator/diagnostic. Its
   #    restart_reconcile.reserved_unbound.outcomes[] entry can help locate the
   #    held tuple (and its recorded submission_attempt and
   #    submission_attempt_started_at are a useful cross-check), but the current
   #    authoritative state — including the attempt and anchor the CAS will
   #    compare against — must be confirmed by a read-only journal replay / the
   #    current master row before the command runs. Do NOT treat
   #    pipeline-jobs/<job_id>.json as authoritative on its own: it is a derived
   #    direct projection, and after a post-commit projection warning the
   #    demotion succeeded while that file was left stale. Authority lives in
   #    the journal batch (journal replay), not in the flat direct file. Take
   #    submission_attempt and submission_attempt_started_at from the journal
   #    replay / current master (or a read-only journal inspection), and if a
   #    previous receipt carried warnings, verify against the journal replay
   #    before believing any flat file.

   # 3. Demote (both entrypoints behave identically; missing --confirm exits 2 with no write).
   #    node-22 active checkout: exact interpreter, not bare uv (pre-maintenance rule):
   /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli demote-reserved-job \
     --journal-root <journal-root> \
     --job-id job_cycle_<source>_<cycle>_forecast[...] \
     --expected-attempt <attempt> \
     --expected-attempt-started-at <anchor, timezone-aware> \
     --checked-by <operator> \
     --checked-at <now, timezone-aware> \
     --verification-note "<bounded note: what was verified and how>" \
     --confirm
   ```

   Success prints a stable sorted JSON receipt with the job id, prior/new status,
   `reconciliation_decision=operator_verified_absence`, attempt/anchor, operator fields,
   and `written_record_count`. The receipt always carries `committed=true` and a
   `warnings` array. On a clean projection pass the receipt reports
   `status=demoted` with `warnings=[]`; when a post-commit direct/latest projection
   fault occurred, it reports `status=demoted_with_warnings` with `warnings=[...]` —
   the authority append already committed in both cases, so **exit 0 means the demotion
   is durable and must not be retried as a failure**: verify it by journal replay (the
   master row / audit event) and the next scheduler pass evidence, and treat any
   `pipeline-jobs/<job_id>.json` mentioned by a warning as a stale derived hint, not
   authority. A CAS refusal or validation error prints to stderr and exits 2 **and
   writes zero journal bytes** — the row keeps its exact held state.

   After demotion, run one scheduler pass. The next pass's reconcile
   (`query_unavailable` action) is gone from `restart_reconcile.reserved_unbound.outcomes[]`,
   the forecast-cycle verified-retry shortcut (`_verified_accepted_submit_forecast_retry`)
   treats the row as retryable, and `reclaim_pipeline_job_reservation` mints one new
   `submission_attempt` with a fresh lock-owned anchor and resubmits the cohort exactly
   once — `PIPELINE_ALREADY_ACTIVE` no longer appears for this cycle.

   Never invoke the command on a row that is not confirmed dead, and never reuse it as a
   replacement for the cluster reconfigure in case 1: a false positive re-`sbatch`es a
   live cohort.

Verified by:

- `tests/test_gateway_reconcile_comment_capability.py::test_reserved_row_stays_reserved_on_a_cluster_that_does_not_store_comments`
  — a genuinely in-flight job with an empty `Comment` no longer demotes the reservation.
- `tests/test_gateway_reconcile_comment_capability.py::test_comment_storage_probe_requires_job_comment_in_accounting_store_flags`
  — the `(null)` / `job_comment` / missing-line parse vectors and the two distinct warnings.
- `tests/test_orchestrator_demote_core_cas.py`, `tests/test_orchestrator_demote_projection_faults.py`,
  `tests/test_orchestrator_demote_reclaim_lifecycle.py`, `tests/test_orchestrator_demote_cli_security.py`
  — typed CAS demotion, byte-identical
  refusals, atomic master/member/event append, cycle retry shortcut, reclaim chain, and
  both CLI entrypoints.

### Disposition — completed or running but unbound: the guarded operator bind (#2668)

Since #2667 a held forecast master skips every member `active_duplicate_pipeline`, so the
cycle's completion verdict reads `gap` and the source's single backfill slot stays on it:
the whole forward lane of that source is frozen until the row is bound, demoted, or
released. Automatic reconcile cannot bind three held shapes. Those, and every other held
action restart reconcile could not resolve, are now listed by `list-operator-actions` as
decision `held_reservation_unresolved`:

| reconcile outcome | listed | `operator_command` | exit |
|---|---|---|---|
| `ambiguous_fallback_match` (either `fallback_match_basis`) | every pass | `bind-reserved-job` | this section |
| `query_unavailable` / `fallback_no_match` / `absence_unconfirmed` | once the attempt anchor is at least 6h old (or unknown) | `triage` | this section if `sacct` shows the job; Disposition case 3 if it is confirmed dead |
| `multiple_matches_blocked` | every pass | `escalate` | none: its durable decision is not the held tuple, so both the bind and the demote refuse it (`not_held`); escalate to the scheduler owner and never hand-edit the journal |
| `identity_mismatch_blocked` / `stale_attempt_blocked` | once the attempt anchor is at least 6h old (or unknown) | `escalate` | none: no operator command resolves them; escalate to the scheduler owner |
| `journal_quarantined` | once the attempt anchor is at least 6h old (or unknown) | `escalate` | the residue-file fix below when `quarantine_reason` is `file_journal_reconcile_inventory_migration_invalid`; any other reason: escalate |
| `legacy_unversioned_read_only` (shape (c)) | every pass | `escalate` | none: since #2674 no current writer produces the shape (every journal writer refuses it, and the historical import fails closed on it before creating the journal root), so a listed row predates that change; the bind refuses it (`legacy_unversioned_unsupported`); escalate to the scheduler owner and never hand-edit the journal. The node-22 journal held 0 such rows on 2026-09-28 |
| a held forcing master (job id ends in a forcing stage) | `multiple_matches_blocked` every pass; the rest as tabled | `bind-reserved-job` for `multiple_matches_blocked` / `query_unavailable`, otherwise `escalate` | the forcing disposition below (#2675) |
| any other held row that is not a forecast cohort master | as tabled | `escalate` | none: escalate to the scheduler owner |
| any other action (outside this table) | every pass, whatever the anchor age | `escalate` | none: a new reconcile action the listing does not know; escalate |

For `journal_quarantined`, read the outcome's `quarantine_reason` / `quarantine_field` in the
pass evidence. `file_journal_reconcile_inventory_migration_invalid` with field `pipeline_jobs`
means a non-`.json` residue (for example `*.json.bak-zombie-<date>`) sits under
`<journal>/pipeline-jobs/` and every bind scan fails closed on it. Move it out of the journal
root and keep a backup, as in
[node22-control-plane-manual-recovery.md](node22-control-plane-manual-recovery.md) (section
"`active_duplicate_pipeline` + reserved-held forecast master (#2655)"), then wait for the next
pass. Never demote such a row.

`bind-reserved-job` binds **one** held current-contract forecast cohort master to the Slurm
master the operator matched in `sacct` (a held forcing master: the next section). It writes exactly the durable tuple that the
automatic #2655 name-window bind writes (`status=submitted`, `submit_outcome=accepted`,
`reconciliation_source=slurm_name_window_unique`, `reconciliation_decision=matched_bound`,
`matched_slurm_job_id`, `slurm_accounting_submitted_at`) plus one `operator_verified_bind`
audit event in the same durable append. It introduces no new durable token, so rolling the
code back leaves a row every older checkout already reads. It never queries `sacct` and
never `sbatch`es. The next pass's inflight reconcile projects the bound master to its
terminal status as usual.

1. **Find the master in `sacct`** (read-only). Use the row's own owner and attempt anchor.
   For a `query_unavailable` row whose window saturated the scan budget, narrow `--endtime`
   to shortly after the anchor. The forecast array is accepted within minutes of the
   reservation.

   ```bash
   sacct --name nhms_forecast --user <expected_slurm_user> --accounts <expected_slurm_account> \
     --starttime <submission_attempt_started_at> --endtime <anchor + 1h, or now> \
     --format=JobID,JobName,State,Submit,End,SubmitLine
   squeue -a --name nhms_forecast
   ```

   This row's master is the bare numeric id (`56839`, never `56839_0` or `56839.batch`)
   whose array-task rows carry a `SubmitLine` with exactly one
   `--comment=nhms_idem:<this row's idempotency_key>`, and whose `Submit` falls inside
   `[submission_attempt_started_at, now]`. A master submitted before the anchor belongs to
   an earlier attempt of the same key: never bind it (the command refuses it with
   `submit_time_outside_attempt_window`). If no row matches, this is not a bind case: go to
   Disposition case 3.
2. **Genuine double submission** (`fallback_match_basis=submitline_exact`, `match_count=2`):
   both masters ran the same runs in the same run directories. Before binding, confirm in
   `sacct`/`squeue` that the **other** master is terminal or cancelled. If it is still
   pending or running, do not bind yet: binding lets the next stage start while that master
   still writes the same run outputs. Wait for it, or escalate. Bind the master whose writes
   are on disk, which is the one that finished **last** (`End`). Record the other master's
   id and final `State` in `--verification-note`.
3. **Preview the CAS inputs** from the journal replay, never from pass evidence or the flat
   `pipeline-jobs/<job_id>.json` (a derived projection). The row must read
   `status=reserved`, `slurm_job_id=null`, `submit_outcome=submit_result_ambiguous`,
   `reconciliation_source=slurm_exact_comment`,
   `reconciliation_decision=accounting_unavailable`,
   `reconciliation_reason_class=comment_accounting_unproven`:

   ```bash
   /scratch/frd_muziyao/NWM/.venv/bin/python -c 'import json, sys
   from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
   row = FileOrchestrationJournalRepository(sys.argv[1]).get_accepted_submit_pipeline_job(sys.argv[2])
   keys = ("status", "slurm_job_id", "submit_outcome", "reconciliation_source", "reconciliation_decision",
           "reconciliation_reason_class", "idempotency_key", "submission_attempt", "submission_attempt_started_at")
   print(json.dumps({key: row.get(key) for key in keys}, sort_keys=True))' <journal-root> <job_id>
   ```

4. **Bind.** First re-read the exact master id you are about to pass (mandatory):

   ```bash
   sacct --jobs=<bare master id> --parsable2 --format=JobID,JobName,State,Submit,End,SubmitLine
   ```

   Its `<master>_<task>` rows must read `JobName=nhms_forecast`, carry a `SubmitLine` with
   exactly one `--comment=nhms_idem:<this row's idempotency_key>`, and show the `Submit` you
   pass. Take `--slurm-job-id`, `--slurm-submit-time` and `--submit-line` from **this**
   output only, never from the step 1 listing or by retyping: the command cannot detect a
   mistyped id whose job is a legacy unversioned forecast master of another cycle, a
   non-master row of another cycle, or an `nhms_forecast` job outside this journal (none of
   them is checked, and the `SubmitLine` carries no job id), and inflight reconcile would
   then project that job's terminal state onto this row.

   `sacct` prints `Submit` as local naive time: append the cluster's UTC offset
   (`date +%z` on the login node) yourself. The command refuses a timestamp without a
   timezone. Pass one array-task row's `SubmitLine` verbatim; only its `--comment=` value is
   used and recorded, never the script path.

   ```bash
   /scratch/frd_muziyao/NWM/.venv/bin/python -m services.orchestrator.cli bind-reserved-job \
     --journal-root <journal-root> \
     --job-id job_cycle_<source>_<cycle>_..._forecast \
     --slurm-job-id <bare master id> \
     --slurm-submit-time <sacct Submit with offset, e.g. 2026-09-25T20:42:57+08:00> \
     --submit-line "<SubmitLine of one <master>_<task> row>" \
     --expected-attempt <submission_attempt> \
     --expected-attempt-started-at <submission_attempt_started_at> \
     --checked-by <operator> \
     --checked-at <now, timezone-aware> \
     --verification-note "<sacct query and what matched; for a double submission: other master <id> <State>>" \
     --confirm
   ```

   Missing `--confirm`, blank evidence, or a timestamp without a timezone exit 2 before the
   journal is opened. Every compare-and-swap refusal exits 2 with
   `bind-reserved-job: refused: <token>; no journal bytes were written`:

   | token | meaning |
   |---|---|
   | `not_held` | not the exact held shape (already bound, not `reserved`, outcome not ambiguous, an exact-comment reason such as `coverage_incomplete`, `multiple_matches_blocked`); a repeated request after a successful bind also lands here |
   | `stale_attempt` | `--expected-attempt` / `--expected-attempt-started-at` differ from the durable row |
   | `submit_time_outside_attempt_window` | `--slurm-submit-time` is before the attempt anchor or after `--checked-at` |
   | `submitline_key_mismatch` | the `SubmitLine` has no `--comment=`, two distinct ones, or a key that is not this row's |
   | `slurm_id_invalid` | `--slurm-job-id` is not a canonical bare decimal master id (a leading zero such as `0123`, an array task, or a step suffix) |
   | `slurm_id_claimed` | a same-cycle row of any kind (forcing, legacy, member task `<id>_<n>`) or a current forecast master of any other cycle (settled or active) already claims that master id, even a recycled id with another `Submit` (stricter than #1850) |
   | `slurm_submit_time_invalid` | `--slurm-submit-time` is not a timezone-aware instant |
   | `legacy_unversioned_unsupported` | shape (c), a row that predates #2674 (no current writer produces it): escalate, never hand-edit the journal |
   | `not_found` | no such forecast master |

   On the production-sized journal a bind takes about a minute (53 s in the #2668 rehearsal): it holds the
   cycle lock and the journal-global reconcile-inventory lock while it scans claimants, so scheduler journal
   writes wait behind it. Do not interrupt it. Refusals decided before the claimant scan (`stale_attempt`,
   `not_held`, `submitline_key_mismatch`, `submit_time_outside_attempt_window`) return within seconds;
   `slurm_id_claimed` may take as long as a bind.

5. **After the bind.** Exit 0 prints a sorted JSON receipt (`status=bound`, or
   `bound_with_warnings` when a derived direct/latest projection failed after the durable
   append; the bind is committed in both cases, so do not retry it). The next scheduler pass's
   inflight reconcile projects the cohort from `sacct`. A `COMPLETED` array is not recomputed:
   its members leave the #2667 freeze and resume at the next stage (`state_save_qc` under
   production's `forecast_state_save_qc` terminal stage). The cycle's verdict stays `gap` until
   that stage succeeds, which is expected, not a missed bind; then it turns `complete` and the
   next cycle enters backfill selection. The `held_reservation_unresolved` entry drops out of
   `list-operator-actions` once a newer reconciling pass no longer reports the row. If one
   array task `FAILED`, only that member reads `permanent_failure_guard` and succeeded members
   stay terminal. Its exit is the manual-retry marker on the cohort master
   (`scripts/node22_manual_retry_failed_runs.py --run-id <cohort master run id>` previews
   `would_mark`), which re-runs only that member.

Verified by `tests/test_orchestrator_bind_reserved_job_cas.py`,
`tests/test_orchestrator_bind_reserved_job_cli.py`,
`tests/test_orchestrator_bind_reserved_job_lane.py` (real journal → bind → inflight
reconcile → completion verdict, and the one-failed-task marker path) and
`tests/test_operator_action_listing_held_reservations.py`.

### Disposition — held forcing master: the forcing bind (#2675)

A held forcing master is `reserved`, unbound, `submit_result_ambiguous`, with no
`reconciliation_decision` and a complete forcing identity: `slurm_comment` is the attempt
comment `nhms_forcing_attempt:<idempotency_key>:a<submission_attempt>`, `cohort_members`
covers tasks `0..n-1`, and the owner is recorded when `slurm_ownership_required` is set. It
freezes its members like a held forecast master. `bind-reserved-job` binds it and writes
exactly the automatic forcing bind tuple (`status=submitted`, `submit_outcome=accepted`,
`reconciliation_source=slurm_exact_comment`, `reconciliation_decision=matched_bound`, the id
as `slurm_job_id` and `matched_slurm_job_id`; no `slurm_binding_source`) plus one
`operator_verified_bind` event (`lane=forcing`). Forcing entries carry no `follow_up_issue`:

| forcing outcome | listed | `operator_command` |
|---|---|---|
| `multiple_matches_blocked` | every pass | `bind-reserved-job`: a real double submission, step 2 |
| `query_unavailable` | anchor at least 6h old (or unknown) | `bind-reserved-job`; no master with this attempt's comment in `sacct`: escalate, the dead-job exit is #2682 |
| `identity_mismatch_blocked`, `absence_unconfirmed`, any other | as tabled above | `escalate`: a foreign owner or comment collision needs owner judgement; absence is #2682 |

1. **Find the master** (read-only). It is the bare id whose task rows carry exactly one
   `--comment=` equal to the row's `slurm_comment`, one `--array=0-<n-1>` (optionally
   `%<k>`) for its `n` members, and a `Submit` inside `[submission_attempt_started_at, now]`.
   A comment ending in another `:a<m>` belongs to another attempt of the same key.

   ```bash
   sacct --name nhms_forcing --user <expected_slurm_user> --accounts <expected_slurm_account> \
     --starttime <submission_attempt_started_at> --endtime now \
     --format=JobID,JobName,State,User,Account,Submit,SubmitLine
   ```

2. **Double submission** (`multiple_matches_blocked`): both masters wrote the same forcing
   products. Bind only once the **other** master is terminal or cancelled in `sacct`/`squeue`;
   bind the one that finished last and put the other's id and `State` in `--verification-note`.
3. **Preview** as in the forecast step 3, but read `get_pipeline_job(<job_id>)` and also print
   `slurm_comment`, `cohort_members` (count), `expected_slurm_user`, `expected_slurm_account`
   and `slurm_ownership_required`.
4. **Bind.** Re-read the exact id first (mandatory):
   `sacct --jobs=<id> --parsable2 -o JobID,JobName,User,Account,Submit,SubmitLine`. Its rows
   must read `nhms_forcing` with this attempt's comment and array spec. Take `--slurm-job-id`,
   `--slurm-submit-time` (append the UTC offset), `--submit-line`, `--slurm-user` and
   `--slurm-account` from **this** output only. Inflight reconcile does not catch a mistyped
   forcing id: it matches forcing accounting without an exact comment, so another cycle's
   `nhms_forcing` job of the same owner would be projected onto this row. Run the forecast
   step 4 command with the forcing `--job-id` plus `--slurm-user <User> --slurm-account
   <Account>`; the receipt adds `lane`, `array_spec` and `slurm_submit_time` (the verified
   `--slurm-submit-time`), and its `slurm_accounting_submitted_at` is null (the durable forcing
   row never stores it, as in the automatic forcing bind). Forcing-specific refusals (exit 2,
   zero bytes; the rest as in the forecast table):

   | token | meaning |
   |---|---|
   | `not_held` | not the held forcing shape (identity incomplete, bound, not `reserved`, outcome not ambiguous, a reconciliation decision present); a repeated bind lands here |
   | `submitline_key_mismatch` | the `--comment=` is not this attempt's `slurm_comment` (another `:a<m>`, another key, a forecast `nhms_idem:` comment, none or two) |
   | `array_spec_mismatch` | no `--array=`, more than one array option, or a value other than `0-<n-1>[%k]` for the row's `n` members |
   | `slurm_owner_mismatch` | ownership required and `--slurm-user`/`--slurm-account` missing, or either differs from a recorded owner (the inflight owner rule); a wrongly recorded owner stays `escalate` |
   | `slurm_id_claimed` | a same-cycle row of any kind or a current forecast master of any cycle holds the id; forcing and other non-master rows of other cycles are not scanned, hence the re-read |

5. **After the bind** the next inflight reconcile projects the array. A `COMPLETED` array
   releases the members, which resume at the forecast stage without a new forcing `sbatch`.
   Verified by `tests/test_orchestrator_bind_reserved_job_forcing.py` and
   `tests/test_orchestrator_bind_reserved_job_forcing_lane.py`.

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
