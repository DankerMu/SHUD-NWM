# Tasks: unresolvable-marker-evidence-equivalence

## Risk Triage

```text
Issue type: bugfix
Project profile: NHMS/NWM (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent (issue predates the Suggested-fixture-level field; triaged here — mandatory expanded triggers: retry semantics, orchestrator state machine, persisted event field addition)
Why:
- Pin-gate decides whether an operator-pinned attempt number survives row absence; wrong verdict is silent under- or over-pin at the reservation boundary
- Two of three defects are regressions vs master in the under-pin direction; one over-pin defect falsifies the function's own docstring promise
- Latent today (zero non-test writers) but arms the moment #1186 wires a caller — fix-before-exposure ordering
Selected risk packs:
- Concurrency / shared state / ordering
- Schema / columns / units / field names (additive journal event details field)
OpenSpec change: unresolvable-marker-evidence-equivalence (generated)
Evidence floor:
- uv run pytest -q tests/test_production_scheduler.py tests/test_file_orchestration_journal.py
- uv run ruff check .
- openspec validate unresolvable-marker-evidence-equivalence --strict --no-interactive
```

## Recorded deviations from issue #1292 (fixture-level, none silent)

1. **Validation target**: the issue's AC cites `openspec validate
   manual-retry-marker-attribution`; that change was archived when PR #1286
   merged. This change is the validation target; the spec-wording AC lands
   on the main `job-retry-mechanism` spec via this change's delta (the "id
   ends with the state's failed stage" clause now lives in the main spec's
   cycle-scope pin scenario).
2. **Archived design.md F5′ NOT edited**: the issue asks to correct the
   F5′ residue description in `manual-retry-marker-attribution/design.md`,
   but archived change dirs are frozen historical records. The F5′ shape
   (model_id-bearing `job_cycle_*` row, row-absent ∧ cross-stage) is
   restated in this change's design.md Residues section with its
   post-change verdict — the under-pin residue SURVIVES this change — and
   is tracked in the follow-up residue issue (task 5.0), so the disclosure
   stays visible in live documents.
3. **Sequencing**: issue says "与 #1186 同批交付或阻塞于其后"; delivered
   AHEAD of #1186 instead — the reachability note gates production
   exposure, not implementability; landing the hardening first means the
   defects are dead before the caller exists. #1186 remains open and is not
   part of this run's pre-authorized ledger.

## 1. Fixture

- [x] 1.1 Author proposal/design/tasks + spec delta (symbol anchors only for
  PR-touched files); record the sequencing decision and the evidence-source
  ruling (marker record primary, loop-stripped id-text backstop)
- [x] 1.2 Reviewer fixture review (read-only) passes — round 1 NOT CLEAN
  (2 P1, 4 P2, 1 Note), round 2 NOT CLEAN (1 P2, 4 wording notes; all 7
  prior findings verified fixed), round 3 CLEAN (all deltas verified, delta
  discipline exact); `openspec validate
  unresolvable-marker-evidence-equivalence --strict --no-interactive` green

## 2. Implementation (implementer subagent)

- [x] 2.1 Writer: `record_manual_repair` marker event `details` gains
  `failed_stage` (failed job's stage — same value the API return namespace
  already carries as `stage`); journal test asserts the persisted field;
  consumer non-drop test asserts the marker event still enters the
  candidate state under the terminal-stage setting; identity-filter
  sanitizer retry-event whitelist gains `failed_stage` with a preservation
  test
- [x] 2.2 Router: `_marker_event_pins_attempt` passes the event into the
  unresolvable arm (private helper, single call site)
- [x] 2.3 Rewrite `_unresolvable_marker_entity_pins_attempt` per design.md:
  fail-open (non-cycle grammar) and foreign-cycle refusal unchanged;
  staleness conjunctions first (state-level `repaired_stage_evidence`
  whose `original_failed_job_id` exactly matches the marker entity id;
  round 1 added the sibling `completed_stage_evidence.job_id` exact-match
  conjunct);
  stage evidence = event `details.failed_stage` primary, loop-stripped id
  token backstop; same-stage → pin; mismatch → arm-2 fall-through
- [x] 2.4 Defect-1 regressions (red pre-change): single-suffix
  `..._<stage>_retry_1` AND three-layer `..._retry_1_retry_2_retry_3`
  stacked-suffix geometry (grammar motivated by the node-27 receipt), on
  synthesized state mappings with submission-stage cohort ids, both
  row-absence mechanisms (identity-filter cohort deletion; row-window
  truncation with a newer same-stage row)
- [x] 2.5 Defect-2 regression (red pre-change): mapping-named targets —
  row-present and row-absent give the SAME refusal; each identity fixture
  carries BOTH halves (row-borne flag/status on the target row AND the
  state mapping naming that job — design D2 canonical domain); the
  same-stage residue matrix's four cells, verbatim from its
  parametrization: `unsubmitted_placeholder` and
  `repaired_flag_not_named_by_the_state` (paired-verdict divergence
  disclosures), `non_failed_named_by_completed_stage_evidence` (delivered
  identity), `plain_failed_control` (delivered identity) — round 1
  replaced the inert cross-stage arm-2-anchor version; unnamed non-failed
  shapes have no matrix cell and are disclosed in Residue 1 only, residue
  tracked via task 5.0
- [x] 2.6 Defect-3 regression (red pre-change): model-less cohort truncation
  + cross-stage failure — unresolvable arm lands on the same arm-2 verdict
  as the resolved arm
- [x] 2.7 Non-regression anchors: non-cycle-grammar fail-open (covers SQL
  RetryService `{run_id}_retry_active` id shape); foreign cycle never pins;
  stage-less legacy marker backstop path (the stage-less three-layer case
  is the binding kill for the single-strip mutant — design D4.6); #1205
  committed anchor subset green (M6 grammar anchor, T7/T8, same-cycle
  cohort anchor, truncation anchor, T9/T10, V-E 4-cell)
- [x] 2.8 Red-proof protocol: new discriminating tests run against pre-change
  source, red output recorded in the brief

## 3. Verification (orchestrator)

- [x] 3.1 Evidence floor commands green (triage block above) — pytest
  1340 passed (1341 collected after the round-1 additions) + 1
  pre-existing env failure
  (`test_db_free_slurm_storage_root_check_masks_symlink_loop_path`,
  macOS /private/tmp symlink loop; confirmed identically red on the
  pristine d567e098 tree); ruff clean; validate green
- [x] 3.2 Production diff confined to the rewritten gate function, its four
  module-private helpers (loop-stripper, two staleness-evidence lookups,
  stage-evidence reader — the "one function" of the original wording was
  delivered decomposed; recorded implementation departure), and the router
  pass-through in `scheduler_state_manual_retry.py`, the one details field in
  `file_orchestration_journal.record_manual_repair`, and the one whitelist
  line in `scheduler_state_identity_filter`; tests-only elsewhere

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; findings verified before fix
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [ ] 5.0 Residue issue filed (design.md Residues, full re-derived scope:
  row-absent staleness gap for unsubmitted placeholders, repaired-flagged
  targets the repaired mapping does not name, and non-failed targets the
  completed mapping does not name — including ALL download/state_save_qc/
  publish cohort targets, which the completed-stage producer can never
  name — population enlarged on suffixed ids by the loop-strip fix;
  id-token backstop stage-inference limit; Residue 2 both cells: surviving
  cross-stage F5′ under-pin AND the new same-stage mapping-named under-pin
  for model-bearing `job_cycle` rows; missing test anchor for the
  completion-stage compaction domain note) and its number recorded here
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final head
- [ ] 5.2 Merge; archive change; loop-log line + audit; close issue #1292
