# Proposal: Evidence-equivalent rewrite of the unresolvable-marker pin gate

## Why

`_unresolvable_marker_entity_pins_attempt`
(`services/orchestrator/scheduler_state_manual_retry.py`) promises to pin a
marker whose target row is absent "only with evidence equivalent to the
resolved-row rule". Three CONFIRMED defects from PR #1286 round-5 (issue
#1292) break that promise, all rooted in one structural choice — using entity
id TEXT as evidence instead of the marker's own recorded stage:

1. **Retry-suffix-blind stage oracle (regression vs master, under-pin)**: the
   `endswith(f"_{failed_stage}")` check fails on production ids that stack
   `_retry_<n>` suffixes (`..._retry_1_retry_2_retry_3` — real node-27
   archive row cited in the issue), so an operator's own-stage manual retry
   loses its pinned attempt whenever the target row is deleted by the
   identity filter or truncated out of the row window.
2. **Missing staleness conjunction (over-pin)**: the resolved-row twin
   (`_cycle_scope_marker_pins_attempt`) refuses repaired-stage-evidence rows,
   unsubmitted auto-retry placeholders, and non-failed targets; the
   unresolvable arm has no equivalent, so a stale marker regains pinning
   power the moment its row disappears — the round-5 verifier measured the
   same state flipping pin 1 (row present) → pin 5 (row absent).
3. **Missing arm-2 fall-through (under-pin)**: on stage mismatch the twin
   falls through to the only-failure-left arm; the unresolvable arm returns
   False outright, so the model-less cohort truncation shape is under-pinned.

All three are unreachable today (`record_manual_repair` — the only writer
that can mint cycle-grammar marker entities — has zero non-test callers), and
were DEFERRED out of PR #1286 on exactly that ground. They arm the moment
#1186 wires the db-free manual-retry execution entry.

## What Changes

- **Evidence source flips from id text to the marker's own record**: the
  journal marker event gains `details["failed_stage"]` (the failed job's
  stage, already present on the API return namespace — one-line port in
  `record_manual_repair`), and the unresolvable arm consumes it as the
  primary stage evidence: pin requires same cycle AND marker stage == failed
  stage, decoupled from id suffix shapes for good. The key is `failed_stage`
  rather than `stage` because the candidate-state record-stage reader
  consumes `details.stage` from events and would drop the marker event
  itself under the production terminal-stage setting (design.md D1); the
  identity-filter event sanitizer's retry-event whitelist gains
  `failed_stage` (one line) so the evidence survives the identity-filter
  rewrite that creates the row-absent shape.
- **Legacy/stage-less markers keep an id-text backstop** that loop-strips ALL
  `_retry_<n>` suffixes via `retry_identity.split_retry_job_identity`
  (single-strip is proven insufficient by the three-layer node-27 receipt)
  before comparing the stage token.
- **Staleness conjunction added** (defect 2): mirroring the twin's order, the
  arm refuses to pin when state-level `repaired_stage_evidence` names the
  marker's target as its original failed job, or when state-level
  `completed_stage_evidence` names it as its completed job (both exact
  entity-id comparisons; the second conjunct was added by the round-1
  review). Scope: those two mapping-named sub-shapes are the staleness
  classes with row-absent evidence and are delivered in full; unsubmitted
  placeholders, repaired-flagged targets the repaired mapping does not name,
  and non-failed targets the completed mapping does not name live on the
  row's own state and remain an accepted, issue-tracked residue (design.md
  Residues — enlarged in reachable population by the loop-strip fix itself,
  recorded there).
- **Arm-2 fall-through added** (defect 3): stage mismatch now falls through
  to `not _state_has_candidate_scope_failed_job(state)` exactly as the twin
  does, instead of returning False.
- Router `_marker_event_pins_attempt` passes the event (not just the entity
  id) to the arm — private helper, single call site.
- **Spec**: the job-retry-mechanism requirement's "the id ends with the
  state's failed stage" evidence clause is rewritten to the marker-record
  evidence rule (id-text wording would otherwise freeze defect 1 into
  contract); scenario clauses added for the three defect shapes.
- **Sequencing decision recorded**: delivered AHEAD of #1186 — the hardening
  lands before the entry point creates a caller (fix-before-exposure); all
  three defects are test-discriminable today via synthesized states.

## Impact

- Affected specs: `job-retry-mechanism` (the same cycle-granularity marker
  requirement modified — fourth consecutive change; one scenario's evidence
  clause rewritten, defect-shape scenarios added).
- Affected code: `services/orchestrator/scheduler_state_manual_retry.py`
  (gate rewrite delivered as the function plus four module-private helpers,
  + router pass-through),
  `services/orchestrator/file_orchestration_journal.py` (`record_manual_repair`
  details gains `failed_stage` — additive journal event field),
  `services/orchestrator/scheduler_state_identity_filter.py` (retry-event
  sanitizer whitelist gains `failed_stage` — one line), tests in
  `tests/test_production_scheduler.py` (which also hosts the sanitizer
  preservation test — the module that exercises the decision-state
  sanitizer) / `tests/test_file_orchestration_journal.py`.
- Behavior delta confined to cycle-grammar unresolvable marker entities:
  non-cycle-grammar fail-open, foreign-cycle refusal, and the resolved-row
  twin are all anchored unchanged; the #1205 committed anchor subset (design
  D4.5) must stay green.
- Consumer analysis (fixture review): `details.stage` on events IS consumed
  — the candidate-state builder's record-stage reader feeds terminal-stage
  gating from it. The `failed_stage` key name avoids that reader entirely
  (marker events keep entering the candidate state; a consumer non-drop
  test anchors this), and the sanitizer whitelist addition keeps the field
  alive through the identity-filter rewrite. No DB schema change (journal
  event details are schemaless JSON — additive field).
