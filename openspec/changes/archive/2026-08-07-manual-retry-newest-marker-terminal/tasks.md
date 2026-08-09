# Tasks: manual-retry-newest-marker-terminal

## Risk Triage

```text
Issue type: bugfix
Project profile: NHMS/NWM (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent (issue predates the Suggested-fixture-level field; triaged here — mandatory expanded triggers: retry semantics, orchestrator state machine, persisted/shared state transitions)
Why:
- Retry-attempt derivation feeds minted job identity; a wrong value silently no-ops operator retries at the reservation boundary
- Persisted scheduler-state event ordering is the deciding input
- The defective arm has zero test discrimination across 1442 existing tests
  (issue #1289's measurement at its filing HEAD; the collection has since
  grown)
Selected risk packs:
- Concurrency / shared state / ordering
OpenSpec change: manual-retry-newest-marker-terminal (generated)
Evidence floor:
- uv run pytest -q tests/test_file_orchestration_migration.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_retry.py
- uv run ruff check .
- openspec validate manual-retry-newest-marker-terminal --strict --no-interactive
```

## Recorded deviations from issue #1289 (fixture-level, none silent)

1. **Validation target**: the issue's AC cites
   `openspec validate manual-retry-marker-attribution`; that change was
   archived when PR #1286 merged. This change
   (`manual-retry-newest-marker-terminal`) is the validation target; the
   spec-wording AC lands on the main `job-retry-mechanism` spec via this
   change's delta.
2. **Fallback value**: the issue's literal "返回 `previous_attempt + 1`"
   is refined to `_fallback_previous_attempt(state, previous_attempt) + 1`
   (design.md D2) — the issue predates #1287's restarted-stage-family
   floor; a bare value would re-mint consumed identities on the
   unnameable-stage shape.
3. **Mutation-guard AC restated post-fix** (design.md D4.5): the issue's
   mutant is this fix modulo the floor; post-fix the guard inverts to
   "reintroducing the walk-back must turn the discriminating test red",
   demonstrated once by hand and reverted.

## 1. Semantic ruling and fixture

- [x] 1.1 Record the needs-triage ruling: newest adopted marker is the sole
  termination point (design.md D1; alternative clamp rejected with reasons)
- [x] 1.2 Author proposal/design/tasks + spec delta with symbol anchors only
  for the touched file
- [x] 1.3 Reviewer fixture review (read-only) passes; `openspec validate
  manual-retry-newest-marker-terminal --strict --no-interactive` green

## 2. Implementation (implementer subagent)

- [x] 2.1 Replace the walk-back `continue` arm in
  `_manual_retry_new_attempt` with the terminal floored fallback return;
  update the in-function comment (the "keeps walking back past them"
  wording is overturned by the ruling); verify `_manual_retry_payload`
  alignment read-only (design.md D3, no edit)
- [x] 2.2 Discriminating pair red at pre-change source, green post-change,
  parametrized over absent AND empty-string `retry_count` on the newest
  marker (design.md D4.1; red output recorded in the brief)
- [x] 2.3 Invariant test: never ≤ `previous_attempt` absent a pinning
  operator claim, domain scoped to states without a top-level
  `manual_retry`/`manual_retry_marker` attempt payload, with the
  documented pinning exemption (D4.2)
- [x] 2.4 Negative anchors: pinning newest marker unchanged; no-marker
  fallback unchanged; pin-refusal arm unchanged; newer UNADOPTED
  marker-shaped event does not terminate the scan — own pinning marker
  still decides (D4.3). Round-1 addition: sibling scan-loop test whose
  state-level `new_attempt: None` payload defeats the short-circuit, so
  the scan loop's own adoption guard has a discriminating oracle (hoist
  mutant: 5 → 1)
- [x] 2.5 Floor discrimination on the new arm: no-`retry_count` newest
  marker on the unnameable-stage consumed-suffix shape derives the
  floored value, not bare `previous_attempt + 1` (D4.4 — D2's oracle)
- [x] 2.6 Inverse-mutant demonstration recorded and reverted (D4.5)

## 3. Verification (orchestrator)

- [x] 3.1 Evidence floor commands green (triage block above; 4-suite
  1509 passed / 1 known macOS baseline failure — 1507 at 7a3b1f60 plus
  the round-1 sibling's two params — baseline reproduced on unmodified
  HEAD; ruff clean; openspec strict valid)
- [x] 3.2 Production diff confined to `_manual_retry_new_attempt` scan loop
  + comment in `services/orchestrator/scheduler_state_manual_retry.py`;
  tests-only elsewhere. (Recorded decision: the minimal one-arm edit is
  chosen deliberately; collapsing the scan loop into the payload-shaped
  break form is a foreclosed refactor — post-fix the loop's pin arm stays
  reachable only via exotic state-level payload shapes, an honest
  limitation noted in design.md D4.3, not a defect to fix here.)

## 4. Review loop

- [x] 4.1 Cross-review rounds per gate ledger; findings verified before fix
  (round 1 not-clean: 3 CONFIRMED FIX_NOW fixed at 86171b3a; round 2
  P2-only, verifier 3 CONFIRMED FIX_NOW routed into the close commit +
  1 PLAUSIBLE DISCARD — recorded clean. Ledger shows the clean round as
  `verified 0 / classes none` because the review_gate CLI normalizes
  clean rounds by design — the raw counts above were passed to
  `record-round` and the per-candidate verdicts are persisted in the
  workplan's verify records; had they been recorded raw, round 2 would
  repeat round 1's record-accuracy class — noted for any future
  gate-retro reading, no gate was approached)
- [x] 4.2 Phase 7 final review clean on final head (NOT CLEAN @ b2158550:
  1 P2 ledger-normalization reconciliation; fixed at 2ca9ed0d, re-adjudicated
  CLOSED / PR CLEAN)

## 5. Merge (pre-authorized) and closeout

- [x] 5.1 Chinese work summary + evidence posted; CI green on final head
  (Unit Tests pass 16m25s run 31197738498 @ 2ca9ed0d)
- [x] 5.2 Merged d21570ea; archived; loop-log line + audit; issue #1289 closed
