# Proposal: fail-closed cycle-stage terminal handling — duplicate-submission skip must not advance the chain

## Why

When a cycle stage's submission hits the reserve gate (another pass
holds the in-flight reservation), `_skip_duplicate_submission` returns
`status="skipped_duplicate_submission"`
(`services/orchestrator/chain_forecast_submission.py:125-176`, returned
via `chain_stage_execution.py:238-244`). That status is in NONE of
`_run_cycle_chain`'s break sets
(`chain_forecast_execution.py:196/:213/:223`), so the loop advances to
the next stage; `_populate_stage_span_counters` (`:322-350`) records
the skipped stage as `submitted_count=0, failed_count=basin_count`
(all-failed), and with no partial marker the cycle closes as the
orchestrator's `final_pipeline_status` — `"complete"` by default
(`chain.py:524`; `"succeeded"` only under the
`NHMS_ORCHESTRATOR_TERMINAL_STAGE` override,
`chain_forecast_orchestrator_cycle.py:59` — both members of
`TERMINAL_PIPELINE_SUCCESS_STATUSES`). Issue
#1202; materialized live in the #1164 campaign: a fully-skipped
forecast submission advanced into `state_save_qc`, and
`state_save_qc_retry_1` published a successor state from a KILLED
run's stale checkpoint — poisoning warm-start lineage while the
control plane reported success.

The hole is TWO-plane (fixture-review round 1, P1-1): even with the
cycle terminal fixed, the evidence plane re-opens it — the new status
matches none of `_is_non_submitted_terminal_or_unavailable_status`'s
patterns (`scheduler_candidate_quality.py:374-386`), so
`model_run_evidence[].final_candidate_success` would read `True`
(`scheduler_candidate_execution_evidence.py:714-716`), `quality_flag`
would read `ok`, and the `candidate_not_successful` residual blocker
would be suppressed. Both planes are in scope.

## Ruling (triage point from the issue: which terminal the skip maps to)

**Dedicated cycle terminal `"skipped_duplicate_submission"` + fail-closed
loop inversion + evidence-plane consumer alignment**:

1. **NOT the `:196` failed set** (the issue's minimal alternative is
   rejected as actively harmful; mechanism corrected per fixture-review
   P2-4): a retry against the skip's `pipeline_job_id` mints a
   retry-suffixed id, and `_cycle_stage_idempotency_key`
   (`chain_runtime_utils.py:426-431`) then derives a NEW key
   (`{run_id}:{stage}:retry_{n}`) — so the retry does NOT re-collide
   with the reserve gate; it creates a fresh reservation and REALLY
   SUBMITS, i.e. a duplicate submission racing the other pass's live
   job. And on the retry-exhaustion/legacy branch,
   `handle_failed_job` (`chain_forecast_orchestrator_cycle.py:206` →
   `retry.py:338-341`) routes the OTHER pass's active row directly to
   `mark_permanently_failed`. Either way a healthy overlapping pass is
   corrupted — worse than the noise the issue predicted.
2. **Reuse the stage vocabulary at cycle level** rather than minting
   `"deferred"`: one term end-to-end, self-describing, grep-able. The
   raw string is safe where it lands (no consumer throws on an unknown
   string), but the evidence and readiness planes must LEARN it — see
   What Changes — or the silent-success hole relocates instead of
   closing (fixture-review r1 P1-1, re-derived in r2: the quality
   predicate has FIVE consumers, and teaching it ripples the pass
   status to `submitted_partial` in mixed geometries — accepted and
   disclosed as behavior delta 3).
3. **Fail-closed inversion with a disclosed, measured regression
   surface**: statuses reaching the loop tail today are
   {`succeeded`,`complete`,`published`} (allowlist; the latter two have
   no current producer — superset by design), `partially_failed`
   (existing `had_partial` mechanics, preserved verbatim), the leaking
   `skipped_duplicate_submission`, and — via the resume path reading
   persisted rows back verbatim (`chain_stage_execution.py:820`) — a
   `reserved`-and-unbound row inside the reconcile grace window
   (`reconcile.py:1324-1356`), which today ALSO silently advances.
   Post-change that last geometry hits the fail-closed backstop and
   terminates the cycle as failed with `UNRECOGNIZED_STAGE_STATUS`
   instead of silently advancing past a stage whose submission state
   is unknown — an intended, disclosed second behavior delta
   (fixture-review P2-6), conservative-correct: the next pass retries
   after reconcile resolves the row.

## What Changes

- `services/orchestrator/chain_forecast_execution.py`
  `_run_cycle_chain`: stage-terminal handling inverted to fail-closed —
  advancement only for the success allowlist and `partially_failed`
  (existing mechanics preserved); `skipped_duplicate_submission` sets a
  terminal `PipelineResult(status="skipped_duplicate_submission")` via
  the existing `pipeline_result = …; break` shape (NOT an inner
  `return` — the span-counter and dispatch-ms backfill at `:255-268`
  must still run; fixture-review P2-7) that runs NO downstream stage
  and never reports success; any other status breaks with terminal
  `failed` + error code `UNRECOGNIZED_STAGE_STATUS`.
- `_populate_stage_span_counters`: skipped stage records
  `submitted_count=0, failed_count=0`.
- **Evidence-plane consumer alignment (r1 P1-1, re-derived r2)**:
  - `_is_non_submitted_terminal_or_unavailable_status`
    (`scheduler_candidate_quality.py:374-386`) gains
    `skipped_duplicate_submission`. The predicate has FIVE consumers,
    all audited in design D4.1: final-candidate-success, quality flag,
    residual blocker, `_is_partial_candidate_evidence`
    (`scheduler_candidate_runtime.py:644-648`), and the evidence-item
    projection at `scheduler_candidate_execution_evidence.py:743`.
  - **Behavior delta 3 (disclosed, r2 P1-1)**: via
    `_is_partial_candidate_evidence`, a MIXED-geometry pass (earlier
    stages submitted, forecast skipped — the #1164 shape) now reports
    pass status `submitted_partial` with `partial_count ≥ 1`;
    `submitted_partial` is a member of
    `SCHEDULER_REVIEW_BLOCKED_STATUSES`
    (`readiness_scheduler_evidence.py:69-85`), so such passes become
    operator-visible instead of silently green. Intended: a pass that
    submitted real work and then deferred its terminal stage IS
    partial, and closure review should look at it. A wholly-skipped
    pass reports `skipped_duplicate_submission` at pass level
    (`_scheduler_pass_status_from_execution`,
    `scheduler_candidate_runtime.py:609-620`, unchanged) and drops out
    of `SCHEDULER_LIVE_WORK_STATUSES` — it did no work.
  - **Readiness pass-status vocabulary + partial recognizers, NOT the
    compatibility map (r2 P1-2, completed per r3 P1-1)**: the new
    terminal manufactures three readiness errors —
    `status_not_allowed` (`readiness_scheduler_evidence.py:486-490`,
    new pass-level status outside both review vocabularies) and
    `partial_count_exceeds_model_run_evidence` (a CAPACITY error:
    `:974-995` keys capacity off `SCHEDULER_REVIEW_BLOCKED_STATUSES`
    membership, giving a skip-status pass capacity 0), both rooted in
    vocabulary; and `partial_count_status_cardinality_mismatch`
    (mixed geometry), rooted in the recognizers. Fix (r3
    probe-verified to clear all three): (a)
    `SCHEDULER_REVIEW_BLOCKED_STATUSES` (`:69-85`) gains
    `skipped_duplicate_submission` — skip-carrying passes are
    review-visible, with ripples audited in design D4.3; (b) the
    partial recognizers (`:1158-1174`) learn the status.
    `SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY` (`:126-145`) is
    deliberately NOT touched: its derived set feeds the
    submitted-inference at `:1070`, and adding the skip status there
    would make a bare skip row infer `submitted=True` — weakening a
    real guard (probe-verified r2, re-confirmed r3; anti-weakening
    anchor pins it).
- Retrievable skip evidence (issue AC4), thread-safe transport (r2
  P2-3/P2-4; key scope corrected run-2 r2 P1-1): every CYCLE-DERIVED
  candidate evidence item gains a `duplicate_submission_skips` key
  (list, empty when no skips); the non-cycle item shapes
  (preflight/evidence-write/forcing-blocked, output-uri, exceptions)
  omit it — a scoped-optional additive key on the v1 item shape,
  WITHOUT bumping `MODEL_RUN_EVIDENCE_SCHEMA_VERSION` (readiness
  validators check named fields, never `additionalProperties` —
  recorded contract decision; no consumer requires the key). It is projected FROM THE RETURNED
  `PipelineResult`'s stage results (stages with the skip status;
  fields: stage, job_type, pipeline_job_id), NOT from the
  orchestrator's `duplicate_submission_skips` instance attribute — the
  orchestrator instance is shared across `ThreadPoolExecutor` workers
  and a clear-at-entry design would reintroduce the documented #861
  attribute-stash race (`scheduler_execution.py:518-527`). The
  in-memory list stays append-only and unclear (no production reads;
  existing single-point test unaffected); scope of the evidence key is
  the COHORT's cycle result, identical in fan-out semantics to the
  existing `stage_statuses` duplication across the cohort's candidates
  (`orchestrate_cycle` runs once per cohort — r2 P2-4). The existing
  `submission_skipped` pipeline event and its swallow-on-write-failure
  stay as-is.
- Spec delta: `slurm-job-chain` gains one ADDED requirement (scoped to
  the per-stage cycle executor behind `orchestrate_cycle`; the
  `run_chain` trigger path is a DIFFERENT loop that is already
  fail-closed — `chain_forecast_orchestrator_runtime.py:63-76` — and
  is NOT covered or touched; fixture-review P2-5).
- Governance ledger: `docs/governance/READINESS_VALIDATION_LANE_
  INVENTORY.md` — BOTH scheduler-evidence rows (`:94` lane table and
  `:257` guard-hook seed) updated for the new blocked pass status
  (the mandated trigger per
  `services/production_closure/AGENTS.md:29-32`); the new
  producer-artifact item key is noted there for completeness (run-2
  r1 P2-2, rows disambiguated r2 note).
- Durable cycle-row status: deliberately NOT written by the skip
  terminal — the reservation-holding pass owns the cycle's durable
  progress; writing a failure status from the deferring pass would
  fight the active pass (recorded ruling, fixture-review P2-9).

## Impact

- Affected specs: `slurm-job-chain` (+1 requirement).
- Affected code (FOUR production files — run-2 r2 P1-1 removed
  `scheduler_execution.py`, the key is scoped to the cycle-derived
  item shape built inside the evidence callee):
  `chain_forecast_execution.py` (loop + counters),
  `scheduler_candidate_quality.py` (predicate),
  `scheduler_candidate_execution_evidence.py` (evidence projection
  from the returned `PipelineResult`),
  `services/production_closure/readiness_scheduler_evidence.py`
  (vocabulary + partial recognizers); governance ledger
  `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md`
  (both scheduler-evidence rows, `:94` and `:257`: new blocked pass
  status + new item output field — mandated by
  `services/production_closure/AGENTS.md:29-32`);
  tests `test_orchestration_chain.py`, `test_production_scheduler.py`,
  `test_scheduler_timing.py`,
  `test_production_readiness_validation.py` (the readiness guard
  suite; anchors 5(c) live there).
- Behavior delta 1 (the fix): a reserve-gate-skipped stage terminates
  the cycle immediately with the dedicated non-success terminal; no
  downstream stage runs; no successor state publishes from that pass;
  evidence plane reports non-success with retrievable skip evidence.
- Behavior delta 2 (disclosed): a `reserved`-unbound row read back by
  the resume path now fails the cycle closed
  (`UNRECOGNIZED_STAGE_STATUS`) instead of silently advancing.
- Behavior delta 3 (disclosed): skip-carrying passes become
  review-visible instead of silently green — mixed geometry
  (submitted work + skipped terminal stage) reports
  `submitted_partial` with `partial_count ≥ 1`; wholly-skipped passes
  report `skipped_duplicate_submission`, now a
  `SCHEDULER_REVIEW_BLOCKED_STATUSES` member (readiness maps them to
  the blocked/review state rather than rejecting the vocabulary).
  Includes the live-proof channel (design D4.2, corrected review r1
  B-P2-1): a skip-carrying pass's scheduler item is `blocked`, so
  `_scheduler_bindings` harvests no binding from it and live proof
  fails with `missing_scheduler_evidence_binding` (a receipt bound to
  such a pass can never validate — regenerate a skip-free pass); the
  live-count channel goes silent for them. A deferring pass is not
  live-green evidence.
- Explicitly NOT changed: reserve-gate logic, retry machinery,
  `TERMINAL_PIPELINE_SUCCESS_STATUSES` (all five copies),
  `_scheduler_pass_status_from_execution`,
  `SCHEDULER_LIVE_MODEL_RUN_STATUS_COMPATIBILITY` (guard at `:1070`
  preserved), the orchestrator's `duplicate_submission_skips` list
  semantics (append-only, never cleared), the `submission_skipped`
  event write/swallow, `run_chain`/trigger paths.
- Issue-claim corrections recorded: journal and
  `run_qhh_continuous.py` do NOT consume `PipelineResult.status`;
  pass-artifact filename suffix is `uuid4().hex[:12]`, not a hash;
  the analysis chain does NOT ride `_run_cycle_chain`
  (fixture-review P2-5 — earlier draft of this fixture overclaimed).
- Out of scope, routed at tasks 5.0: `production_status_for` stage
  alias gap (pre-existing); #1205 (stale manual_retry root cause);
  `state_save_qc` missing-output-dir publishing gap;
  `multibasin-state-idempotency` OOM-prose contradiction with
  post-#1161 `job-retry-mechanism`.
