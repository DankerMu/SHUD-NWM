# Tasks: fail-closed-cycle-stage-terminal

## Risk Triage

```text
Issue type: bugfix (silent data-correctness: skipped submission
  advances the chain and reports success on BOTH the cycle and the
  evidence plane; #1164 live poisoning)
Project profile: NHMS/NWM (openspec/project-profile.md)
Blast radius: medium-high (cycle terminal semantics of the
  orchestrate_cycle executor + evidence/readiness planes; run_chain
  trigger loop NOT affected — separate, already fail-closed)
Fixture level: expanded
Upstream suggested level: absent (issue implementation-ready with one
  triage point — terminal mapping — ruled in proposal)
Why:
- Terminal-status semantics change on the cycle executor; the rejected
  :196 alternative is measurably harmful (double submission against a
  live job / direct permanent-fail of the other pass's row)
- The evidence plane independently re-opens the silent-success hole
  unless the quality predicate (five consumers) and the readiness
  partial recognizers learn the new terminal (fixture-review r1 P1-1,
  re-derived r2 P1-1/P1-2)
- One disclosed second behavior delta: reserved-unbound resume rows
  now fail closed instead of silently advancing (P2-6)
Selected risk packs:
- Terminal-state/verdict semantics (cycle + evidence + readiness)
- Evidence-chain wiring (dead in-memory list → pass artifact)
Minimal mergeable slice: D2 loop inversion + D3 counters + D4 items
  1/3/4/5 + anchors 1-7 (one PR; the evidence plane is the issue's
  AC4 and P1-1 makes it inseparable)
Evidence floor:
- uv run pytest -q tests/test_orchestration_chain.py tests/test_production_scheduler.py tests/test_scheduler_timing.py tests/test_production_readiness_validation.py
  (known pre-existing local red: test_db_free_slurm_storage_root_check_
  masks_symlink_loop_path — macOS env, disclose)
- uv run ruff check .
- openspec validate fail-closed-cycle-stage-terminal --strict --no-interactive
- grep sweep: every "skipped_duplicate_submission" hit → disposition
```

## Recorded deviations from issue #1202

1. **Terminal name**: cycle-level terminal reuses the stage vocabulary
   `"skipped_duplicate_submission"` (not `skipped`/`deferred`) — one
   term end-to-end; consumer audit in proposal/design D4.
2. **Issue's consumer claims corrected**: journal and
   `run_qhh_continuous.py` do not consume `PipelineResult.status`;
   the AC4 evidence lands in the scheduler pass artifact +
   quality/readiness planes — the real consumers.
3. **The issue's minimal alternative (:196 membership) rejected as
   harmful** on corrected mechanism (fixture-review P2-4): retry mints
   a retry-suffixed idempotency key → NEW reservation → duplicate
   REAL submission racing the live job; and the legacy exhaustion
   branch routes the other pass's active row directly to
   `mark_permanently_failed` (`retry.py:338-341`).
4. **Scope beyond the issue's three-file list**: evidence-plane
   consumer alignment (`scheduler_candidate_quality.py` predicate,
   `readiness_scheduler_evidence.py` partial recognizers `:1158-1174`,
   evidence projection wiring) — without it the silent-success hole
   relocates to the artifact plane (fixture-review r1 P1-1; readiness
   target re-derived r2 P1-2 and completed r3 P1-1: pass-status
   vocabulary membership + recognizers, NOT the compatibility map,
   whose extension would weaken the `:1070` submitted-inference
   guard).
5. **Disclosed second behavior delta** (P2-6): reserved-unbound resume
   rows fail closed (`UNRECOGNIZED_STAGE_STATUS`) instead of silently
   advancing — intended, conservative-correct.
6. **Disclosed third behavior delta** (r2 P1-1): mixed-geometry passes
   (submitted work + skipped terminal stage) project
   `submitted_partial` / `partial_count ≥ 1` and enter the
   review-blocked set — intended operator visibility for the #1164
   shape.
7. **Issue AC4 satisfied via the artifact, not the in-memory list**
   (r2 P2-3/P2-4): `duplicate_submission_skips` evidence is projected
   from the returned `PipelineResult` stage results (thread-safe;
   cohort-scoped like `stage_statuses`); the orchestrator's in-memory
   list stays append-only/uncleared with no new production reader —
   a per-cycle clear would reintroduce the #861 shared-instance race,
   and per-candidate attribution is not claimable
   (`orchestrate_cycle` runs once per cohort).
8. **large-file-guard delta (recorded, hook escape hatch + repo
   precedent)**: five touched files exceeded the 1000-line guard at
   merge base `e3122746` — `tests/test_orchestration_chain.py`
   (12494), `tests/test_production_readiness_validation.py` (6206),
   `services/orchestrator/chain_forecast_execution.py` (1135),
   `services/orchestrator/scheduler_candidate_execution_evidence.py`
   (1002), `services/production_closure/readiness_scheduler_evidence.
   py` (1284) — added to `.large-file-guard.json` exclude; no file
   was pushed over the limit by this change alone.

## 0. Run ledger (upstream-contract escalation and re-entry)

- Run 1 (issue #1202 as originally written): fixture reviews r1/r2/r3
  each NOT CLEAN → two-iteration repair bound tripped → issue
  reclassified upstream-contract-defective; concrete gaps + corrected
  implementation-ready contract reported on the source issue
  (https://github.com/DankerMu/SHUD-NWM/issues/1202#issuecomment-5229221252).
  Run 1 is TERMINAL; its review history is preserved in 1.2 below.
- Run 2 (this fixture, current): re-entry against the REPAIRED issue
  contract (the comment above is the authoritative scope). Fixture
  re-authored to that contract — includes the readiness pass-status
  vocabulary ruling (D4.3) that run 1's issue text lacked. Fixture
  review restarts at round 1 for this run (fresh contract, fresh
  fixture-review ledger; run-1 history retained for audit).

## 1. Fixture

- [x] 1.1 Author proposal/design/tasks + spec delta (slurm-job-chain
  +1 ADDED requirement); ruling recorded (terminal mapping); run 2:
  re-authored to the repaired issue contract
- [x] 1.2 Reviewer fixture review (read-only): round 1 NOT CLEAN
  (P1-1 evidence-plane hole, P1-2 anchor geometry, P1-3 counter
  observability; P2-4..P2-9, P3-10) — repaired (iteration 1/2).
  Round 2 NOT CLEAN on the r1 repairs (P1-1 five-consumer/pass-plane
  ripple, P1-2 readiness mis-target + guard weakening, P2-3 clear
  race vs #861, P2-4 cohort-vs-candidate attribution; r1 P1-2/P1-3
  repairs probe-CONFIRMED good) — repaired (iteration 2/2, the bound;
  D4 re-derived, anchors 5(c) re-keyed, transport moved to
  `PipelineResult`). Round 3 NOT CLEAN — one probe-confirmed P1: the
  readiness repair does not clear the WHOLLY-skipped geometry
  (`partial_count_exceeds_model_run_evidence` is a CAPACITY error,
  `readiness_scheduler_evidence.py:974-995` — recognizers are never
  consulted past the `:937` continue; and the new pass-level status
  trips undisclosed `status_not_allowed` at `:486-490`); remedy shape
  probe-verified by the reviewer = pass-status VOCABULARY membership
  (`SCHEDULER_REVIEW_BLOCKED_STATUSES` + ripples `:1012-1017`/`:513`).
  **Third revise-class verdict → two-iteration repair bound tripped:
  issue #1202 reclassified upstream-contract-defective per
  subagent-workflow phase-flow §0.5.11; concrete contract gaps
  reported on the source issue; run 1 TERMINAL.**
- [x] 1.3 Run 2 fixture review: round 1 NOT CLEAN (P1-1 evidence
  floor cited the wrong readiness suite — `test_production_slurm_
  validation.py` never touches `readiness_scheduler_evidence`; the
  guard suite is `test_production_readiness_validation.py`; P2-2
  governance ledger update mandated; P2-3 live-proof ripple
  undisclosed; P2-4 historical-artifact failure mode understated;
  P2-5 transport file-set/key-scope contradiction. Vocabulary-ruling
  mechanics, ripple completeness, five-consumer audit, anti-weakening
  anchor all probe-CONFIRMED good.) — all five repaired in-fixture
  (iteration 1/2). Round 2 NOT CLEAN: one P1 — the r1 repair-5
  "always-present across four item shapes" ruling was wrong (real
  inventory ~10 shapes / 3 files; forcing results have no `stages`
  field); repairs 1-4 probe-CONFIRMED good. Repaired (iteration 2/2,
  the bound): key re-scoped to cycle-derived items only, single edit
  site, `scheduler_execution.py` removed from the affected set (four
  production files), scoped-absence anchored in D5.5(b); notes fixed
  (live-count cite → `readiness_scheduler_evidence.py:1028`, both
  governance rows named, `chain_array_accounting` cites reverted to
  `:157`/`:158-159` per r2 probe). Round 3 CLEAN (single-edit-site
  claim triple-probed: one production `orchestrate_cycle` caller, no
  other cycle-derived shape, skip always lands in `result.stages`
  incl. the partial-retry write-back; scoped-absence anchor
  constructible — blocked and cycle items share one evidence list;
  ripple grep clean; one cosmetic note fixed in-fixture). Fixture
  APPROVED for implementation

## 2. Implementation (implementer subagent)

- [x] 2.1 `_run_cycle_chain` fail-closed inversion (D2): skip →
  dedicated terminal via `pipeline_result = …; break` shape; success
  allowlist explicit; backstop `UNRECOGNIZED_STAGE_STATUS`; existing
  break/partial branches behavior-identical
- [x] 2.2 `_populate_stage_span_counters` skip arm (D3, 0/0)
- [x] 2.3 Quality predicate (D4.1, five consumers audited) +
  readiness vocabulary membership and partial recognizers (D4.3;
  compatibility map untouched)
- [x] 2.4 Skip-evidence projection from the returned `PipelineResult`
  (D4.4/D4.5: cohort-scoped, key on cycle-derived items only —
  single edit site in the evidence callee, no schema-version bump,
  NO instance-list clear, NO `scheduler_execution.py` edit)
- [x] 2.5 Anchors 1-5 (D5, re-keyed geometries incl. 5(c)(iii)
  anti-weakening) + keep 6/7 green
- [x] 2.6 Red-proof protocol per D5 (extraction + fresh uv sync;
  counter red under bound collector)

## 3. Verification (orchestrator)

- [x] 3.1 Evidence floor commands green; counts recorded with head SHA
  (06cbc564: 1 known macOS red / 1794 passed / 2 skipped; post-fix
  4a6ac0f0: readiness 342/2, chain 285, combined 627/2; ruff clean;
  openspec valid)
- [x] 3.2 Sweep table in PR body: every `skipped_duplicate_submission`
  hit → disposition
- [x] 3.3 Production diff confinement verified and recorded (four
  production files + the READINESS_VALIDATION_LANE_INVENTORY
  governance rows per proposal Impact; tests + fixture elsewhere;
  fix commit 4a6ac0f0 = fixture + tests only, zero production)

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; findings verified before fix
  - Round 1 (06cbc564, 3 lenses → 4 candidates → 2 verifier batches,
    4/4 CONFIRMED): A-P1-1 nested `_retry_partial_array_stage` skip
    collapse (P1, pre-existing `:511-512`, outside every hunk, retry
    machinery in NOT-changed; literal spec WHEN unviolated) → DEFER,
    routed → issue #1322; B-P2-1 live-proof disclosure named an
    unreachable error code → fixture corrected
    (`missing_scheduler_evidence_binding`); C-P2-1 `:1166` recognizer
    arm zero-covered + D4.3(b) rationale mismatch → anchor 5(d) added
    + rationale corrected; C-P2-2 scenario-4 invariant vacuous →
    anchor 5(e) added. Fix pass bought by the coverage gaps.
  - Round 2 (4a6ac0f0, focused post-fix): CLEAN — all four closures
    probe-verified (5(d) mutation-kill re-run; 5(e) real geometry
    forecast 2/0/2; live-proof corrected text matches code; #1322
    consistent incl. the requirement-prose check: the deferred
    geometry yields `partially_failed`, not a success terminal); no
    fix-regression; diff scope exact. P2 record-hygiene items
    (stale PR body, +3 line-cite drift, #1322 pointer, D4.2 mode
    nuance) closed in the terminal-state commit without code changes.
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [ ] 5.0 Follow-ups routed with numbers: (a) `production_status_for`
  lacks a `skipped_duplicate_submission` mapping — stage evidence
  misreports `production_status: "failed"` (pre-existing,
  `production_contract.py:354-390` /
  `scheduler_candidate_execution_evidence.py:881`); (b) stale
  `manual_retry` marker root cause = existing #1205 (reference only);
  (c) `state_save_qc` publishes successor state from a KILLED run
  with missing output dir (issue's out-of-scope; dedup before
  filing); (d) `multibasin-state-idempotency` spec prose
  (`spec.md:45-49`) lists out-of-memory as transient-retryable,
  contradicting post-#1161 `job-retry-mechanism` — prose escaped the
  #1161 literal sweep; (e) DONE during review: #1322 filed for the
  nested `_retry_partial_array_stage` skip/ambiguous collapse
  (round-1 A-P1-1, verifier-CONFIRMED P1, pre-existing)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final head
- [ ] 5.2 Merge; archive change (delta folds); loop-log line + audit;
  close issue #1202
