# Design: nested-retry-defer-propagation (run 2, skip-only defer)

## D1. Status routing table for `retry_aggregation is None`

Nested `_submit_and_wait_cycle_stage` returns `(StageRunResult, None)`
for the following non-`failed` status families (run-1 r2 F4 and run-2
r1 finding 2 corrected the enumeration; all share `aggregation=None`,
`task_results=()`, and differ ONLY in `.status` / `error_code`). A
plain `failed` also arrives with `aggregation=None` on the LEGACY
repo class when a nested poll times out
(`chain_stage_execution.py:1051-1059` + `:1102-1110` + `:613-618`) —
collapse-to-failed is the CORRECT semantics for that arrival and is
unchanged by this fix:

| nested status | producer | today (`:511-526`) | after this change |
|---|---|---|---|
| `succeeded` | unreachable-defensive for THIS helper (r2r2 P3-1: the helper resubmits the same array stage, so a scalar success with `aggregation=None` cannot arise; `:512`'s succeeded branch is defensive dead code) | all pending → succeeded, loop ends | unchanged |
| `skipped_duplicate_submission` | `chain_stage_execution.py:243-244` → `_skip_duplicate_submission` (`chain_forecast_submission.py:125-178`; `error_code=None`; writes only a `submission_skipped` pipeline event, never a failed status) | all pending → `failed`; helper's own terminal call re-writes durable failed (legacy-repo geometry); loop continues and, when the retry adjudicator grants attempt N+1 (permissive/legacy services), really resubmits; `partially_failed` aggregate admitted by allowlist | **defer**: no stamping, immediate return, propagate status; cycle → dedicated skip terminal (`:237-256` semantics) |
| `submit_result_ambiguous` | `chain_stage_execution.py:385-397`, reachable ONLY on the accepted-submit repo class (`supports_accepted_submit_reconcile` + forecast-cohort gate `:326-331`) whose projection suppression prevents the direct hydro write (`chain_forecast_execution.py:600-605`) | collapse stamps pending failed (evidence plane) and advances; resubmits under permissive services | **unchanged** (run-2 scope ruling; pinned by A3; routed → D6 follow-up) |
| `reconcile_unverified` | poll timeout → `aggregation=None` (`chain_stage_execution.py:613-618`) + accepted-submit transition (`:1089-1113`); top-level routes it with ambiguous to `reconciling` (`chain_forecast_execution.py:217`) and `_after_cycle_stage_terminal` deliberately no-ops for it (`:598`) | same collapse; additionally defeats the `:598` no-op by re-entering with `partially_failed` (`:624-633`) | **unchanged** (same ruling; pinned by A3; routed → D6) |
| `submission_failed` | `chain_stage_execution.py:437-451` (durable `_mark_staged_hydro_runs_failed` already fired inside the nested call; not suppressed by the projection gate, though skipped when the accepted-submit rejection batch commits, `:445`/`:397-436`) | stamp failed + loop continues (retry quota governs) | **unchanged** (source-issue boundary; pinned by anchor A4) |

Defer set constant: `NESTED_RETRY_DEFER_STATUSES =
{"skipped_duplicate_submission"}` — module level in
`chain_forecast_execution.py`, named as a SET deliberately: the D6
follow-up widens membership (reconciliation-pending family) with zero
extra control-flow surface once that family is evidence-plane
first-class. Referenced by both the helper and the call-site routing.

## D2. Helper-side change (`_retry_partial_array_stage`)

At the top of the `retry_aggregation is None` branch (`:511`):

```python
if latest_result.status in NESTED_RETRY_DEFER_STATUSES:
    return latest_result, None
```

- Returning `(latest_result, None)` — the raw nested defer result,
  aggregation `None`. **Type obligation** (task 2.1): the current
  annotation `-> tuple[StageRunResult, ArrayAggregation] | None` does
  NOT admit this shape — widen to
  `tuple[StageRunResult, ArrayAggregation | None] | None` at BOTH
  declaration sites (`chain_forecast_execution.py:473` and the
  delegating method `chain_forecast_orchestrator_cycle.py:244`); the
  defer routing at the call site (D3) breaks before any consumer that
  requires a non-None aggregation (`:336`, `:206-207`) can observe
  the `None`.
- No `task_results` mutation → `_aggregation_from_task_results` never
  sees fabricated failures; the helper's own
  `_after_cycle_stage_terminal` at `:574-586` is never reached
  (early return), so the DUPLICATE durable re-write disappears. The
  first-pass terminal's write for genuinely-failed tasks (before the
  helper ran, `chain_stage_execution.py:641` →
  `chain_forecast_execution.py:604-605`/`:647-677` on the legacy repo
  class) is untouched — on master and after the fix alike.
- No attempt consumption: the defer return precedes the next loop
  iteration's `_schedule_cycle_stage_retry` call, so no attempt-N+1
  job id or idempotency key is ever derived from the deferred
  submission.
- Terminal-processing parity: the skip producer early-returns BEFORE
  the `:641` call site (`chain_stage_execution.py:243-244`), and the
  top-level dedicated branch (`chain_forecast_execution.py:237-256`)
  likewise performs no terminal processing — the absence is
  deliberate parity, not double or missing processing.
- The `finally` basin-cohort restore around the nested call
  (`:551-552`) still runs on the defer return (return inside the
  loop body, restore in `finally`) — `context.active_basins` is not
  left reindexed.

## D3. Call-site routing (`_run_cycle_chain:258-270`)

Today the helper's return overwrites `stage_results[-1]` and falls
into the allowlist tail only (it re-enters NONE of the dedicated
branches at `:200-256`). Change:

```python
retried = self._retry_partial_array_stage(...)
if retried is not None:
    result, aggregation = retried                   # aggregation None on defer
    stage_results[-1] = result                      # overwrite, same as today
    result_slot = len(stage_results) - 1
    # defer status: route to the dedicated skip terminal instead of
    # falling into the allowlist tail
```

For the skip status, construct the SAME `PipelineResult` terminal the
dedicated top-level branch builds (`:237-256`:
`PipelineResult("skipped_duplicate_submission", …); break`; no
durable cycle-status write — the reservation-holding pass owns cycle
progress).

**Recommended shape**: extract the existing branch body into a tiny
private helper (`_skip_terminal_pipeline_result(...)`) called from
both the top-level branch and the post-retry routing, so the two
doors provably share one terminal construction. If extraction fights
the surrounding span/`result_slot` bookkeeping, a mirrored inline
construction is an acceptable recorded deviation — but the
constructed `PipelineResult` MUST be equivalent to the top-level
branch's in status, error fields, and absence of cycle-progress write
(NOT in `stages` content — a nested-defer cycle legitimately carries
earlier real stage entries a top-level-skip cycle would not), and
anchor A1 asserts the observable equivalence either way.

**Overwrite-not-append ruling** (run-1 r1 P2-5): the defer result
replaces `stage_results[-1]`, exactly the mechanics the merged-retry
path uses today. Appending was considered and REJECTED: a duplicate
same-stage entry ripples into `standard_chain_shape`
(`scheduler_candidate_execution_evidence.py:562` → `:743` would
report `[..., "forecast", "forecast"]` on a schema-versioned
artifact) and into the readiness status-union
(`readiness_scheduler_evidence.py:1097-1098`, where a retained
`partially_failed` entry sets `failed=True` feeding the
`failed_count` cardinality check `:944-958` — the very acceptance
error the #1202 scenario forbids manufacturing). Audit trail is NOT
lost by overwriting: the original `partially_failed` result already
had its terminal processing during the FIRST-PASS top-level submit —
before the retry helper was ever entered (`chain_stage_execution.py:
641` ran for it; durable pipeline events and task outcomes recorded);
only the in-memory summary entry is replaced, matching the
top-level-skip artifact shape the #1202 readiness recognizers were
built against.

## D4. Evidence and counter ripple (all riding #1202 machinery, no new
surface)

- **Span counters**: `_populate_stage_span_counters` (`:380-416`)
  computes from the post-retry `result` — the propagated skip status
  hits the #1202 skip arm (`:411-413`) → `submitted_count=0,
  failed_count=0`, replacing master's post-collapse
  `partially_failed` shape (`:408-410`,
  `submitted=basin_count/failed=0`). Disclosed as proposal delta 3.
  The skip scenario's `0/0` span clause thereby holds for nested
  skips too: a deferred cycle attributes zero submissions to the
  stage regardless of the pre-retry dispatch, whose accounting lives
  in the durable events written during the first pass.
- **Evidence plane**: the nested skip lands in
  `PipelineResult.stages` (the overwritten final forecast entry, D3)
  → cycle-derived evidence items surface it through the existing
  `_pipeline_result_duplicate_submission_skips` projection
  (`scheduler_candidate_execution_evidence.py:592-610`) — no schema
  change, no new key. Readiness vocabulary and recognizers (#1202)
  already treat the skip cycle terminal as review-blocked; the
  overwritten shape matches the top-level-skip artifact geometry
  those recognizers were anchored against.
- **Candidate-outcome accounting (r2r2 P1-1 corrected)**: forecast
  per-task failure signals reach `context.task_outcomes` ONLY via
  `_apply_array_progress` → `record_array_task_outcomes`
  (`chain_forecast_execution.py:336-337` →
  `chain_array_accounting.py:97-141`) or the retry-exhausted failed
  branch (`:206-207`); the defer path reaches NEITHER — it enters
  the helper only from `partially_failed` (past `:200`) and breaks
  at the skip terminal, returning at `:328-329` BEFORE `:336`
  (r3 P3-1 precision). A deferred cycle's
  `candidate_outcomes` therefore carry NO forecast-stage failure
  signal: every cohort candidate reads `active` and
  `_candidate_status_from_outcome`
  (`scheduler_candidate_execution_evidence.py:789-794`) maps it to
  the cycle terminal — the genuinely-failed basin's evidence item
  flips `failed` (master) → `skipped_duplicate_submission`
  (post-fix), losing the per-candidate failure reason. This EXACTLY
  matches the top-level-skip artifact geometry (which likewise never
  reaches `:336`), so the #1202 readiness recognizers face no new
  shape — but it IS a behavior delta, disclosed as proposal delta 4.
  Note for spec readers: the existing "count the skipped candidate
  consistently with the producer's partial accounting" THEN clause
  is satisfied by this cohort reporting no producer-partial signal
  at all (identical to a top-level skip) — recognizers must not be
  re-derived from the assumption that a partial signal survives.
  The `context.task_outcomes` pruning at `:555-560` is unreached on
  both sides in A1's geometry (gated on a succeeded final
  aggregation) — green-both-sides, no differential there.
- Nothing changes for `submission_failed` or the
  reconciliation-pending family (D1: unchanged rows).

## D5. Anchors

1. **A1 nested-skip defer (the fix)**: real `ForecastOrchestrator` +
   `FakeCycleRepository` wrapped in a **call-recorder subclass**
   (overrides `update_hydro_run_status` to append
   `(run_id, status, error_code)` then delegate — required because
   the base fake only mutates `self.hydro_runs`, making a duplicate
   same-value write invisible in final state; the subclass does not
   touch reserve-gate geometry). Legacy geometry: no
   `supports_accepted_submit_reconcile` attribute; its
   `reserve_pipeline_job` matches on `idempotency_key` across
   `repository.jobs` (`tests/test_orchestration_chain.py:341-351`),
   which is what the seed populates (`StoreBackedCycleRepository`
   routes through `store.reserve_job` and would ignore the dict
   seed). Geometry: first pass array forecast → `partially_failed`
   (`:4074` pattern) with the **escape submission pinned
   non-succeeding** (e.g. `array_results_by_stage` second attempt →
   `["failed"]`) so the master-side duplicate write is a `failed`
   write, not a `succeeded` one (r3 P2-1); competing reservation
   seeded under the **deterministic retry job id**
   `job_{run_id}_forecast_retry_1` with idempotency key
   `{run_id}:forecast:retry_1`, foreign `cycle_id` (which is what
   hides the seed from `query_pipeline_jobs_by_cycle`, `:476-480`)
   and `model_id=None` (which keeps the `PIPELINE_ALREADY_ACTIVE`
   preflight quiet, `:307-316`) — the true production geometry: run
   ids are deterministic across passes, so the competing pass's row
   genuinely carries the same job id, which is what lets
   `_retry_job_for_stage_result`'s `repository.get_pipeline_job`
   (`chain_forecast_execution.py:437-444`) find a record and grant
   attempt N+1. Retry service: `FakeRetryService(max_retries=2)` —
   quota ≥ 2 AND a permissive (no `should_auto_retry`) service are
   BOTH required for the resubmission arm to be red on master; a
   real `RetryService` classifies the skip's `error_code=None` as
   `UNKNOWN_FAILURE` ∉ transient (`retry.py:23-35`, `:134-142`) and
   never grants the attempt. Assert: (a) no submit call for the
   pending task ids after the defer (recorded submit count), (b)
   cycle terminal `skipped_duplicate_submission`, (c) no
   parse/state_save_qc/publish stage ran, (d) the recorded
   `update_hydro_run_status` failed-write list gains NO entry after
   the helper is entered (snapshot before). RED on master in this
   geometry: (a) real second submission, (b) non-skip terminal +
   advance, (c) downstream ran, (d) second failed-write batch from
   the helper's `:574-586` terminal call.
2. **A2 no attempt-N+1 derivation**: same geometry — assert no
   job/reservation exists beyond the seeded `…_retry_1` row and the
   first-pass rows (shape assertion, NOT a literal escape id: on
   this legacy path `FakeRetryService.handle_failed_job` APPENDS the
   suffix, so master's escape id is `…_forecast_retry_1_retry_2`
   with key `{run_id}:forecast:retry_1_retry_2` — the
   suffix-stripping `…_retry_2` form applies only on the
   accepted-submit master-row path, r3 P2-2). RED on master.
3. **A3 reconciliation-family pins (GREEN both sides, THREE arms —
   r2r1 finding 1)**: guards the defer set from silent widening
   ahead of the D6 follow-up, exactly as A4 guards
   `submission_failed`. Arms:
   (i) nested `submit_result_ambiguous` on the accepted-submit repo
   class keeps today's collapse semantics (pending stamped failed on
   the evidence plane, cycle advances/`partially_failed`, no
   dedicated `reconciling` terminal from the nested path) —
   producer: a raise inside `submit_job_array` after the gateway
   boundary via `fail_next_array_submission_stage`
   (`tests/test_orchestration_chain.py:96-97`), ambiguity-classified
   by `chain_stage_execution.py:62-72` under the `:326-331` gate
   (NOT `never_terminal_stage`, which is the poll-timeout knob —
   r2r1 finding 5);
   (ii) nested `reconcile_unverified` (poll timeout on the
   accepted-submit class via a per-attempt `never_terminal_stage`
   variant) keeps today's collapse semantics INCLUDING the defeat of
   the `:598` no-op (the helper's terminal re-enters with
   `partially_failed` → `:624-633` cycle-status write) — this arm is
   behaviourally distinct from (i) and needs its own pin;
   (iii) set-membership pin: `NESTED_RETRY_DEFER_STATUSES ==
   {"skipped_duplicate_submission"}` — a widening lands red here
   even if an arm's geometry rots. Both client variants need
   attempt-scoped subclasses (the stock knobs match per stage and
   would kill the first pass too). Retry-service pinning (r2r2
   P3-2): arm (ii) MUST pin quota to a single nested attempt
   (`max_retries=1` equivalent) — on the accepted-submit class
   `SLURM_JOB_TIMEOUT` is transient, so a second nested submission
   would be granted and trip the stock client's
   previous-stage-not-terminal guard
   (`tests/test_orchestration_chain.py:173-175`); arm (i) states its
   service/quota explicitly too.
4. **A4 submission_failed pin (GREEN both sides)**: nested
   `submission_failed` keeps today's semantics — pending stamped
   failed, retry loop continues under quota, durable
   `_mark_staged_hydro_runs_failed` fired inside the nested call.
5. **A5 evidence visibility**: driven from a REAL deferred
   orchestrator cycle (A1's geometry), not a hand-built
   `PipelineResult` (the projection already surfaces any skip-status
   stage entry, so a fabricated input is green on master and
   vacuous). Assert: the deferred cycle's `PipelineResult.stages`
   final forecast entry carries the skip status (overwrite ruling),
   `duplicate_submission_skips` projection on the cycle-derived
   evidence item surfaces the nested skip, AND (3, r2r2 P1-1/P2-1
   corrected) the candidate-outcome differential: post-fix every
   cohort candidate outcome reads `active` and the genuinely-failed
   basin's `model_run_evidence` item status is
   `skipped_duplicate_submission`; on master that basin's item reads
   `failed` with `reason: "forecast_task_failed"` (the collapsed
   aggregation reached `_apply_array_progress`). Task-results
   differential stated presence-vs-absence: master's final forecast
   stage entry carries fabricated per-task `task_results` for the
   pending basins; the deferred entry carries `task_results == ()`
   (raw skip result shape). RED on master overall (the real cycle
   produces no skip entry — it ends on a partial terminal,
   `context.last_partial_status`, never the skip).
6. **A6 regression**: `:4074` and `:4093` existing retry tests
   unchanged and green (the `retry_aggregation is not None` merge
   path and genuine-success path untouched).

Known limits (recorded, not anchored):
- `_next_current_master_retry_identity`
  (`file_orchestration_journal.py:7406-7410`) derives retry identity
  from PIPELINE-JOB rows (`job_id`/`retry_count`), not from the
  hydro-run durable statuses this change stops re-writing (r2r1
  finding 4 corrected the characterization); the relevant effect of
  this change on that consumer is indirect — no attempt-N+1 job row
  is minted for a deferred submission — and the consumer itself is
  unaudited here (pre-existing, out of scope).
- **Deferred retry job row state**: under a real `RetryService`,
  master's next loop iteration marks the deferred `…_retry_1` job
  `permanently_failed` (`retry.py:339-341`, `:408-415`); after the
  fix that row remains in its prior state and could be observed by
  `_find_existing_stage_job` / `_job_needs_submission` on a later
  pass — resume-compatible with the reservation-holder-owns-progress
  model (the row IS the record of a scheduled-but-deferred attempt),
  but a changed row-state outcome; recorded, not verified (with the
  A1 fake-repo geometry the row lives in the retry store only).

## D6. Follow-up routing (recorded)

The reconciliation-pending family (`submit_result_ambiguous`,
`reconcile_unverified`) needs: blocked-vocabulary membership +
partial recognizers + quality-predicate treatment for the
`reconciling` terminal (the #1202 template — today `reconciling`
appears in ZERO recognizers, so even top-level reconciling cycles
read `final_candidate_success: True`), THEN defer-set widening via
`NESTED_RETRY_DEFER_STATUSES`. Tracked as issue #1326 (filed during
this fixture's review; scribe-verified, including the cross-plane
inconsistency `production_status_for("reconciling") == "failed"` vs
top-level `final_candidate_success: True` in the same evidence file).
