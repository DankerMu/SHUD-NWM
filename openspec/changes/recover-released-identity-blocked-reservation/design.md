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
| Run manifest / QC provenance | the recovered attempt must be built from the **then-current** cohort like any ordinary retry; carrying the released row's stale member set forward would silently re-run a superseded basin manifest (17 basins vs. the current 24) |

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
2. **The two door predicates stay byte-identical**: the reservation reclaim
   predicate (`file_orchestration_journal.py:2117-2170`) and
   `_verified_accepted_submit_forecast_retry`
   (`chain_forecast_orchestrator_cycle.py:923-931`) keep hard-pinning
   `absence_retry_permitted`. This is **not** the same as "the call sites are
   untouched": the operator attestation is admitted as an **additional disjunct**
   at `_terminal_stage_needs_manual_retry`
   (`chain_forecast_orchestrator_cycle.py:171-183`), whose first arm currently
   `return`s the door verdict unconditionally for this shape. Reviewers will read
   that diff; it is stated here rather than glossed. Neither predicate may be
   widened, weakened, or reordered.
3. `absence_retry_permitted` semantics, the comment-capability fail-closed gate
   (`reconcile.py:416-422`), and the identity-released invariant
   (`accepted_submit_identity.py:267`, `:646`, "identity released transition
   must abandon the reservation") are unchanged.
4. No automatic path may reach the new minting call.

## D3. Why not the three rejected designs

- **Stamp a transient `error_code` at release.** Directly contradicts
  `file_orchestration_journal.py:3358-3361` and reddens both pins. Rejected.
- **Open a reclaim door to `identity_mismatch_released`.** Contradicts the
  contract at `:3312-3314` and would fabricate an identity proof the system does
  not have. Rejected.
- **Auto-mint at the release site under an `identity_blocked_streak` cap.**
  Reverses the deliberate "permanent wedge over duplicate submission" choice.
  With `AccountingStoreFlags = (null)` absence is unprovable, so a cap bounds how
  many duplicates, not whether. Also checked and rejected as an enabler: the
  sbatch templates set `--job-name=nhms_{{stage_name}}`
  (`infra/sbatch/run_shud_forecast_array.sbatch:2`, 14 templates identical), so
  `JobName` carries no cohort/candidate identity and cannot support an
  sacct-side absence proof either. Rejected.

## D4. Chosen design (revised after the first implementation proved inert)

**The invariant.** The recovery's only durable output must be an *input the
ordinary submission path already consumes* — never a competing artifact *on* that
path.

The first design violated it by pre-materializing the successor row, and was
verified INERT and self-blocking. The trace, each predicate opened and checked:

- `chain_forecast_cycle.py:503` filters to non-terminal statuses;
  `reservation_lost` is in `TERMINAL_JOB_STATUSES`
  (`chain_runtime_utils.py:32-40`) and `pending` is not, so the successor becomes
  the stage's job.
- `job_needs_submission` (`chain_forecast_cycle.py:527-528`) is true, so the pass
  tries to submit it.
- `_pipeline_job_conflicts_unlocked`'s master branch
  (`file_orchestration_journal.py:7376-7389`) sees a row already under that
  `job_id` and refuses the reserve; `reclaim_pipeline_job_reservation` then
  refuses at `:2135` because the row is `pending`, not `reservation_lost`.
- `_reservation_already_inflight` therefore fires and the pass calls
  `_skip_duplicate_submission`, which writes no row; durable cycle status is
  deliberately not written (`chain_forecast_execution.py:272-285`), so every
  later pass repeats it. Permanently inert.
- Self-blocking: `_next_current_master_retry_identity` mints exactly the identity
  an ordinary retry would have used, so the eager write consumes the one
  submittable slot.

**The revised design.** The recovery API records a durable operator attestation
on the released row and writes no successor. The consuming call site
`_terminal_stage_needs_manual_retry` (`chain_forecast_orchestrator_cycle.py:171-183`)
gains an additive disjunct for that attestation, after which the ordinary path
mints `_retry_<n>` via `_retry_cycle_stage_job_id` and
`_submit_and_wait_cycle_stage` creates a clean reservation on a **free**
identity, reaching sbatch.

**Why no existing channel was reused (pre-flight, recorded).**
`_terminal_stage_needs_manual_retry`'s first arm `return`s for this shape, so
`_terminal_stage_needs_forced_resubmit` (`:895-920`) is unreachable; and
`manual_retry` is not in `_FORCE_TERMINAL_RESUBMIT_DECISIONS` (`:27-41`), so
adding it there would change behaviour for every terminal status. The additive
disjunct is forced, not preferred.

**Attestation carrier.** The carrier must survive
`normalize_accepted_submit_evidence` and the shape pins at
`tests/test_production_scheduler.py:48632`/`:48681`. Candidates, in order:
(a) a field the accepted-submit validators already admit — note the production
wedged row already carries `manual_retry_marker: False`, so that field is
admitted on this row kind; (b) a side journal record keyed by job id that the
chain reads at stage selection. Red-first will settle which survives.

**No member-set carry-forward.** The recovered attempt participates in ordinary
candidate selection like any retry. `chain_forecast_orchestrator_cycle.py:228-230`
states the next submit builds a clean reservation from the **then-current** basin
cohort, and the July production journal shows `cohort_digest` churning per attempt
(`cf0bba44…` → `0b32b13f…`). With the manifest now at 24 basins instead of 17,
carrying a stale member set forward would silently re-run a superseded manifest.

**Granularity.** The wedged row's own `run_id` is the cohort pseudo-id
(`cycle_gfs_2026080712_forecast_cohort_3e066f456290`, verified on node-22), so
one operator invocation recovers one wedged cohort.

**Why the generic manual channel is not reused.** `_create_pending_manual_retry_job`
(`file_orchestration_journal.py:8556-8600`) clones the failed row with
`candidate_id: None` and key `manual_retry:{run_id}:{n}`, writing through
`_pipeline_job_row` + `_write_pipeline_job_unlocked` (`:8590`, `:8594-8598`).
The clone carries `reconciliation_decision = identity_mismatch_released` over
unchanged while overriding `status` to `"pending"` (`:8573`), tripping the
accepted-submit invariant at `accepted_submit_identity.py:646` — surfaced as
`file_journal_evidence_invariant_invalid`. (The typed-API guard at
`file_orchestration_journal.py:1914-1922` is **not** what blocks this: it lives
in `upsert_pipeline_job`, which this path never calls.) It would also
pre-materialize a row, which the invariant above forbids outright.

## D5. The signal

Today the wedge is silent: the row freezes and each pass rewrites the same
`RUN_SHUD_FORECAST_ARRAY_RESERVATION_LOST` cycle terminal, with nothing saying
"this is permanent and needs a human". The single release write point SHALL emit
a queryable operator-visible record with a searchable token naming the job id,
cohort digest and streak. This is what makes the recovery path reachable in
practice — the 2026-08-22 wedge was found only by accident while investigating
a different issue.

**The signal may not be able to fail the release.** The release write is durable
with no rollback, and the release path is never re-entered for an already-released
row (the reconcile loop iterates reserved-unbound jobs, which never yields a
`reservation_lost` row), so a raising emission would strand the row released and
unannounced — the very silent terminal this section exists to end. The emission
is therefore best-effort with respect to the release, and **its own failure is
not silent**: it records a durable failure trace, degrading to a log only if that
too fails, and never to a raise. Precedent: `_record_permanent_failure_mark_failure`
(#1312 C-P2, "the fallback must not be silent"). One deliberate difference from
that precedent: the fallback here catches the filesystem/OS class as well, because
this path writes through the unlocked journal helper that can raise it — catching
only the precedent's two classes would let a filesystem fault escape the fallback
and abort the whole reconcile pass.

## D6. Seams under test

- `retry.RetryService.should_auto_retry` — asserted still false for the shape.
- `file_orchestration_journal` typed recovery API — asserted to write **no**
  pipeline-job row, to leave the `_retry_<n>` identity free, and to refuse every
  non-released shape.
- `_terminal_stage_needs_manual_retry` (`chain_forecast_orchestrator_cycle.py:171-183`)
  — asserted to admit the attestation and to be unchanged without it.
- The ordinary pass, end to end — asserted to reach the stage's submission call
  after recovery. This is the oracle whose absence let an inert no-op go green.
- **The operator, end to end** — asserted to be able to FIND a wedged row and ACT
  on it through a supported entry point, without source access or a Python shell.
  This seam was missing from the first three drafts of this design, and its
  absence is why five reviewers reading against this fixture could not see that
  the recovery API shipped with zero production callers. **Standing rule for this
  change and its successors: for every mechanism added, name its intended invoker
  and show the path.** "The mechanism is correct" and "the mechanism is reachable
  by the party it exists for" are different claims; list both, or neither is under
  test. (Recorded in `.workplans/pr-1802/review/retro-round-3.md`.)
- The single release write point — asserted to emit the signal exactly once for
  both prior-state shapes, and asserted to keep the release durable, to not raise,
  and to leave a trace when the emission itself fails.

## D7. Evidence mapping

| claim | evidence |
|---|---|
| released rows stay non-auto-retriable | `tests/test_production_scheduler.py:48632`, `:48681` pass unweakened |
| **an ordinary pass submits after recovery** (the oracle whose absence let an inert no-op go green) | new test, red-first, driving a real pass to the submission call |
| recovery writes no row and leaves the `_retry_<n>` identity free | new test |
| without the attestation nothing changes | new test |
| the single release write point signals, for both prior-state shapes | new test driving a fresh reservation and a reclaim re-seeded one |
| a repeat recovery is refused | new test invoking recovery twice on one row |
| no lint/spec regression | `uv run ruff check .`, `openspec validate ... --strict` |
| production behavior | node-22 runtime receipt (scheduler behavior changed) |

## D8. Corrected premise (fixture review, round 1)

The first draft of this design asserted **two release write points** and told the
implementer to instrument both. That premise is false, and the correction is
recorded rather than silently rewritten because it changes what the tasks mean:

- `IDENTITY_MISMATCH_RELEASED_DECISION` is constructed in exactly one place,
  `file_orchestration_journal.py:3365`, inside
  `release_identity_blocked_reservation` (`:3294-3400`). Its only production
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
