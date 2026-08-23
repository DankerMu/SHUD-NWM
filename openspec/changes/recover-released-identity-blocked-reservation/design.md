# Design: operator-gated recovery for released identity-blocked reservations

## D1. Risk triage

- **Fixture level**: `full`. This touches the accepted-submit master row's typed
  write surface and the retry classification boundary — the exact seam that
  #1116, #1173/#1178 and #1312 each fought over. A silent regression here is a
  duplicate Slurm submission against live output directories.
- **Blast radius**: production scheduler on node-22; no DB, no frontend.

### Selected risk packs

| pack | why selected |
|---|---|
| Slurm production lifecycle / mock-vs-real parity | the whole subject is what happens when Slurm-side truth and our accounting diverge; a wrong recovery re-submits against a possibly-live array |
| Run manifest / QC provenance | the recovered attempt must inherit the cohort's identity (`cohort_digest`, `cohort_members`) or it silently runs a different member set |

### Not selected

| pack | why not |
|---|---|
| Geospatial / CRS / basin geometry | no geometry code path is touched |
| Hydro-met time series / forcing windows | no forcing/window arithmetic changes |
| SHUD numerical runtime | no SHUD invocation or numerics change |
| PostGIS / TimescaleDB | db-free path; node-22 connects to no live DB |
| External providers / snapshot reproducibility | no provider call changes |
| Published artifacts / display identity | nothing reaches display selection |

## D2. Must-preserve behavior

1. `should_auto_retry` stays **false** for this shape, and the release writes
   keep `error_code` null. `tests/test_production_scheduler.py:48632` and
   `:48681` SHALL pass **unweakened** — they are the anti-regression pin for the
   duplicate-submission class, not incidental coverage.
2. The two reclaim doors are untouched: the reservation reclaim predicate
   (`file_orchestration_journal.py:2126-2140`) and
   `_verified_accepted_submit_forecast_retry`
   (`chain_forecast_orchestrator_cycle.py:911-919`) keep hard-pinning
   `absence_retry_permitted`.
3. `absence_retry_permitted` semantics, the comment-capability fail-closed gate
   (`reconcile.py:416-422`), and the identity-released invariant
   (`accepted_submit_identity.py:267`, `:646`, "identity released transition
   must abandon the reservation") are unchanged.
4. No automatic path may reach the new minting call.

## D3. Why not the three rejected designs

- **Stamp a transient `error_code` at release.** Directly contradicts
  `file_orchestration_journal.py:3331-3334` and reddens both pins. Rejected.
- **Open a reclaim door to `identity_mismatch_released`.** Contradicts the
  contract at `:3285-3288` and would fabricate an identity proof the system does
  not have. Rejected.
- **Auto-mint at the release site under an `identity_blocked_streak` cap.**
  Reverses the deliberate "permanent wedge over duplicate submission" choice.
  With `AccountingStoreFlags = (null)` absence is unprovable, so a cap bounds how
  many duplicates, not whether. Also checked and rejected as an enabler: the
  sbatch templates set `--job-name=nhms_{{stage_name}}`
  (`infra/sbatch/run_shud_forecast_array.sbatch:2`, 14 templates identical), so
  `JobName` carries no cohort/candidate identity and cannot support an
  sacct-side absence proof either. Rejected.

## D4. Chosen design

A typed, operator-gated recovery API on the journal that:

1. accepts only the released shape (`status == "reservation_lost"`,
   `reconciliation_decision == "identity_mismatch_released"`,
   `slurm_job_id is None`, `matched_slurm_job_id is None`, current-contract
   cohort **master**);
2. mints the next identity through the **existing**
   `_next_current_master_retry_identity` helper
   (`file_orchestration_journal.py:8878-8882`), so the retry-suffix derivation
   stays single-sourced;
3. carries the cohort identity (`cohort_digest`, `cohort_members`) onto the new
   row unchanged — a recovered attempt is the same cohort, not a re-picked one;
4. never consults `should_auto_retry` and writes no `error_code`;
5. is CAS-guarded on expected attempt + attempt anchor, mirroring
   `release_identity_blocked_reservation`
   (`file_orchestration_journal.py:3267-3346`), so a concurrently advanced
   attempt loses the race rather than producing two keys;
6. refuses a **repeat** invocation on a row it has already recovered. This is not
   covered by the CAS guard: minting a successor through
   `_next_current_master_retry_identity` does not mutate the source row's own
   `submission_attempt` / `submission_attempt_started_at` (see
   `file_orchestration_journal.py:7918` and `:7984`, which leave the source row
   untouched), so a second identical call would pass every shape check and the
   CAS check and mint a **second** successor for the same cohort — precisely the
   duplicate-submission class D3 rejects. The implementation SHALL make the
   second call refuse (the mint collides on the successor `job_id` /
   idempotency key, or the source row is marked consumed);
7. performs **no** Slurm-side liveness or absence check. It cannot: see D3. The
   call is an operator attestation, and this boundary is a stated non-goal, not
   an oversight.

**Why the generic manual channel is not reused.** `_create_pending_manual_retry_job`
(`file_orchestration_journal.py:8264-8305`) clones the failed row with
`candidate_id: None` and key `manual_retry:{run_id}:{n}`, then writes through
`_pipeline_job_row` + `_write_pipeline_job_unlocked` (`:8298`, `:8302-8304`).
The clone carries `reconciliation_decision = identity_mismatch_released` over
unchanged while overriding `status` to `"pending"` (`:8281`), which trips the
accepted-submit invariant at `accepted_submit_identity.py:646`
(`if decision == IDENTITY_MISMATCH_RELEASED_DECISION and status !=
"reservation_lost": raise`) — surfaced as
`file_journal_evidence_invariant_invalid`. Widening
`MANUAL_RETRY_SOURCE_STATUSES` alone would therefore route the operator into a
raise, not a recovery. (The typed-API guard at
`file_orchestration_journal.py:1913-1918` is **not** what blocks this: it lives
in `upsert_pipeline_job`, which this path never calls.) The clone would also
lose the cohort identity.

**Granularity.** The wedged row's own `run_id` is the cohort pseudo-id
(`cycle_gfs_2026080712_forecast_cohort_3e066f456290`, verified on node-22), so
one operator invocation recovers one wedged cohort.

## D5. The signal

Today the wedge is silent: the row freezes and each pass rewrites the same
`RUN_SHUD_FORECAST_ARRAY_RESERVATION_LOST` cycle terminal, with nothing saying
"this is permanent and needs a human". Both release write points SHALL emit a
queryable operator-visible record with a searchable token naming the job id,
cohort digest and streak. This is what makes the recovery path reachable in
practice — the 2026-08-22 wedge was found only by accident while investigating
a different issue.

## D6. Seams under test

- `retry.RetryService.should_auto_retry` — asserted still false for the shape.
- `file_orchestration_journal` typed recovery API — asserted to mint exactly one
  successor, to preserve `cohort_digest`/`cohort_members`, and to refuse every
  non-released shape.
- The release write points — asserted to emit the signal at both sites.

## D7. Evidence mapping

| claim | evidence |
|---|---|
| released rows stay non-auto-retriable | `tests/test_production_scheduler.py:48632`, `:48681` pass unweakened |
| recovery mints exactly one successor, cohort identity preserved | new test, red-first against current code |
| the single release write point signals, for both prior-state shapes | new test driving a fresh reservation and a reclaim re-seeded one |
| a repeat recovery is refused | new test invoking recovery twice on one row |
| no lint/spec regression | `uv run ruff check .`, `openspec validate ... --strict` |
| production behavior | node-22 runtime receipt (scheduler behavior changed) |

## D8. Corrected premise (fixture review, round 1)

The first draft of this design asserted **two release write points** and told the
implementer to instrument both. That premise is false, and the correction is
recorded rather than silently rewritten because it changes what the tasks mean:

- `IDENTITY_MISMATCH_RELEASED_DECISION` is constructed in exactly one place,
  `file_orchestration_journal.py:3338`, inside
  `release_identity_blocked_reservation` (`:3267-3346`). Its only production
  caller is `reconcile.py:2135`.
- A function named `release_pipeline_job_reservation` does **not exist**; the
  first draft cited it twice.
- What `tests/test_production_scheduler.py:48632` and `:48681` distinguish is two
  **reservation** write points — the fresh reserve and the
  `reclaim_pipeline_job_reservation` re-seed — as `:48681`'s own docstring says
  ("the SECOND reservation write point"). That is a difference in the released
  row's *prior state*, not a second release-writing function.

Consequence: the signal is emitted **once**, at the single release write point.
Instrumenting the `reconcile.py` caller as well would double-emit on 100% of
production releases, since it is the sole caller. The `incomplete-fix` risk from
PR #1793's retro still applies, but to the *test matrix* (both prior-state
shapes must be driven), not to the emission sites.
