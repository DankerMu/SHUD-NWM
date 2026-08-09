# Tasks: nested-retry-defer-propagation

Issue: #1322 (P1) — `_retry_partial_array_stage` collapses nested
`skipped_duplicate_submission` into per-task failed, bypassing the
#1202 fail-closed terminal.

Fixture level: expanded (proposal + design + tasks + spec delta).
Origin: issue-scribe from PR #1321 round-1 A-P1-1 (verifier-CONFIRMED
with live probe); no `Suggested fixture level` field (not a
stage-change-pipeline issue); level chosen by orchestrator triage:
single-file control-flow fix but P1 blast radius and a spec-delta
obligation ⇒ expanded.

Evidence floor:
- `uv run pytest -q tests/test_orchestration_chain.py`
- `uv run pytest -q tests/test_production_scheduler.py` (A5 projection
  anchor lives with the `duplicate_submission_skips` tests)
- `uv run ruff check .`
- `openspec validate nested-retry-defer-propagation --strict
  --no-interactive`

Deviations (recorded):
1. Defer-terminal construction MAY be an extracted helper or mirrored
   inline (design D3); either way A1 asserts observable equivalence
   with the top-level branch. Implementer records which.
2. `.large-file-guard.json`: `chain_forecast_execution.py` and
   `tests/test_orchestration_chain.py` are already excluded (recorded
   in #1202's guard delta); no new exclusions expected — if the fix
   touches another >1000-line file, record it here.
3. Run-2 scope narrowing vs the source issue's recommendation: the
   issue recommended deferring `submit_result_ambiguous` alongside
   the skip; run-2 defers the SKIP ONLY and routes the
   reconciliation-pending family (incl. `reconcile_unverified`,
   which the issue missed) to the D6 follow-up — rationale in the
   corrected contract posted on #1322 (run-1 terminal report).

## 0. Run ledger (upstream-contract escalation and re-entry)

- Run 1 (issue #1322 as originally written, defer set =
  {skip, ambiguous} per the issue's recommendation): fixture reviews
  r1/r2/r3 each NOT CLEAN (r1: 1 P1 + 6 P2; r2: 1 P1 + 4 P2; r3:
  4 P2) → two-iteration repair bound tripped → issue reclassified
  upstream-contract-defective. Root contract defects: (a) the
  acceptance criterion "no `update_hydro_run_status(..., 'failed')`
  durable write for deferred basins" is literally unsatisfiable —
  the FIRST-pass terminal legitimately writes failed for
  genuinely-failed tasks before the retry helper runs, on master and
  after any fix (r2 F2 / r3 P2-3); (b) the "no second sbatch"
  acceptance criterion is retry-service- and quota-dependent
  (permissive fake + quota ≥ 2 required; production `RetryService`
  never grants the attempt for the skip's `error_code=None`), which
  the issue never specifies (r2 F1); (c) the recommended
  ambiguous-defer arm carries an unanalyzed evidence/readiness
  ripple — `reconciling` appears in ZERO recognizers, so deferring
  flips cohort rows from partial-recognized to nothing-recognized
  with unanalyzed recount sign (r3 P2-4) — and the issue missed the
  fourth `aggregation is None` family `reconcile_unverified`
  entirely (r2 F4). Gap report + corrected implementation-ready
  contract posted on the source issue
  (https://github.com/DankerMu/SHUD-NWM/issues/1322#issuecomment-5230637731
  — the authoritative run-2 scope). Run 1 TERMINAL; review history
  preserved in 1.2.
- Run 2 (this fixture, current): re-entry against the REPAIRED
  contract — defer set narrowed to `{skipped_duplicate_submission}`
  (the fully-paved #1202 evidence plane); reconciliation-pending
  family pinned unchanged (A3) and routed to the follow-up issue
  (number recorded here when the scribe reports:
  #1326). Fixture review restarts at round 1
  (fresh contract, fresh fixture-review ledger; run-1 history
  retained for audit).

## 1. Fixture

- [x] 1.1 Author proposal/design/tasks + spec delta (slurm-job-chain,
  1 MODIFIED requirement: skip-defer scenario WHEN extended to nested
  retry submissions, new pending-task / no-further-durable-write /
  no-attempt-derivation THEN clauses); run 2: re-authored to the
  repaired contract (skip-only defer)
- [x] 1.2 Reviewer fixture review (read-only) until clean
  (two-iteration repair bound per workflow contract)
  - RUN-1 HISTORY (terminal): Round 1 NOT CLEAN (1 P1, 6 P2, 3 P3) —
    P1-1 ambiguous producer is accepted-submit-gated so
    durable-failed harm is skip-only; P2-1 return-annotation
    widening; P2-2 `_after_cycle_stage_terminal` parity mechanism;
    P2-3 quota ≥ 2; P2-4 FakeCycleRepository seeding; P2-5 append
    ruling reversed to overwrite; P2-6 counter-shift disclosure;
    P3-1/2/3 wording+vacuity+atomicity — all repaired (iteration
    1/2). Round 2 NOT CLEAN (1 P1, 4 P2) — F1 A1/A2 geometry
    re-based on deterministic retry job id + permissive-fake
    requirement; F2 A1(d) re-scoped to no-ADDITIONAL-write; F3 A3
    retry-service pinning; F4 `reconcile_unverified` fourth family
    added to defer set; F5 second annotation site — all repaired
    (iteration 2/2, the bound). Round 3 NOT CLEAN (4 P2): P2-1 A1's
    four RED components mutually unsatisfiable in the pinned
    geometry + duplicate write invisible on the base fake; P2-2
    escape-key note wrong for the legacy path (`retry_1_retry_2`,
    not `retry_2`); P2-3 spec delta's absolute durable-write clause
    false by construction; P2-4 reconciling-arm evidence/readiness
    accounting asserted-away, arms not symmetric. **Third
    revise-class verdict → bound tripped: issue #1322 reclassified
    upstream-contract-defective per subagent-workflow phase-flow
    §0.5.11; gaps reported on the source issue; run 1 TERMINAL.**
  - Run 2 fixture review: Round 1 NOT CLEAN (1 P2, 4 P3) — repaired
    (iteration 1/2): P2 `reconcile_unverified` had no pin while
    three files claimed A3 covered it → A3 rebuilt with THREE arms
    (ambiguous behavior pin via `fail_next_array_submission_stage`;
    `reconcile_unverified` behavior pin incl. the `:598`-no-op
    defeat, via per-attempt `never_terminal_stage` variant;
    set-membership pin on `NESTED_RETRY_DEFER_STATUSES`); P3s: D1
    enumeration notes the legacy-timeout plain-`failed` arrival; A5
    pruning clause relabeled green-both-sides pin + differential
    re-pointed at the `:513-523` outcome-metadata overwrite;
    retry-identity known limit recharacterized (pipeline-job rows,
    not hydro statuses); A3 constructibility cite fixed. Reviewer
    verified: A1/A2 anchor geometry survives end-to-end master
    code-walk (all four RED components genuinely red), spec delta
    clean vs base, requirement sentence watertight under skip-only
    scope, consumer sweep clean.
  - Round 2 NOT CLEAN (1 P1, 1 P2, 3 P3) — repaired (iteration 2/2,
    the bound): P1-1 "producer-partial survival" claim was FALSE —
    `context.task_outcomes` gets forecast signals only via
    `_apply_array_progress` (:336-337), which the defer return
    precedes (:328-329) → D4 bullet rewritten, per-candidate
    `failed`→`skipped_duplicate_submission` flip disclosed as
    proposal delta 4, A5 assertion (3) replaced with the true
    differential + spec-reader note that "consistent with producer's
    partial accounting" is satisfied by a no-partial-signal cohort
    (identical to top-level skip); P2-1 A5's `:513-523` payload
    differential unobservable in A1's quota-2 geometry (attempt-2
    re-overwrites) → restated presence-vs-absence (fabricated
    task_results on master vs `()` on defer); P3-1 D1 succeeded row
    marked unreachable-defensive; P3-2 A3 arm (ii) pins
    single-nested-attempt quota (transient SLURM_JOB_TIMEOUT would
    otherwise trip the stock client guard); P3-3 A5 master-terminal
    wording → partial terminal (`context.last_partial_status`).
  - Round 3 CLEAN (P3 cosmetic only, closed in the approval commit:
    D4 "ONLY via" precision — `:206-207` retry-exhausted branch is a
    second `task_outcomes` writer, defer reaches neither; cite
    drift `:562`/`:134-142`/`:4074`/`:4093`). Reviewer re-walked the
    P1-1/P2-1 repairs end-to-end (defer path returns at `:328-329`
    before `:336`; top-level skip likewise; per-candidate flip via
    `:789-794`; `final_candidate_success` False both sides;
    presence-vs-absence task_results differential robust), confirmed
    A3 arm mechanics and spec delta purely additive. Fixture
    APPROVED for implementation. Implementer note carried from r3:
    A3 arm (ii) must pick a retry service that enforces quota on
    whichever branch its fake repo selects (a `should_auto_retry`-
    free permissive fake on the accepted-submit master-row branch
    would grant unbounded nested attempts and trip the stock client
    guard).
- [x] 1.3 `openspec validate nested-retry-defer-propagation --strict
  --no-interactive` green (re-run after every repair round and after
  the approval-commit cosmetic fixes)

## 2. Implementation (implementer subagent)

- [ ] 2.1 `NESTED_RETRY_DEFER_STATUSES = {"skipped_duplicate_
  submission"}` + helper-side early return in
  `_retry_partial_array_stage` (design D2): no task stamping, no
  attempt N+1, `(latest_result, None)` propagated; return annotation
  widened at BOTH declaration sites
  (`chain_forecast_execution.py:473`,
  `chain_forecast_orchestrator_cycle.py:244`)
- [ ] 2.2 Call-site defer routing in `_run_cycle_chain` (design D3):
  overwrite `stage_results[-1]`, route the skip status to the
  dedicated skip terminal semantics. **2.1 and 2.2 land atomically**
  — 2.1 alone would send the defer status into the allowlist tail's
  `UNRECOGNIZED_STAGE_STATUS` backstop (cycle `failed`), strictly
  worse than master
- [ ] 2.3 Anchors A1/A2/A5 red-proofed on the pre-change tree
  (`git archive` extraction protocol) + A3 pins (three arms:
  ambiguous, reconcile_unverified, set-membership) and A4 pin green
  both sides + A6 existing retry regressions green
- [ ] 2.4 Evidence floor suites + ruff green; implementer reports
  deviations explicitly ("no deviations" stated if none)

## 3. PR

- [ ] 3.1 Commit + push branch `feat/issue-1322-nested-retry-defer`;
  PR with 变更摘要 / 偏离记录 / 测试证据 / Evidence-Floor 声明
- [ ] 3.2 CI green (targeted Unit Tests)

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; candidates → dedup →
  per-class verifier batches; findings verified before fix
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [ ] 5.0 Follow-ups routed with numbers: (a) reconciliation-family
  evidence-plane + nested-defer widening (D6; scribe issue number →
  #1326)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final
  head
- [ ] 5.2 Merge; archive change (delta folds); loop-log line + audit;
  close issue #1322
