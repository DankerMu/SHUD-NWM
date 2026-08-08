# Tasks: align-oom-retry-classification

## Risk Triage

```text
Issue type: bugfix (spec-code drift, MUST NOT clause)
Project profile: NHMS/NWM (openspec/project-profile.md)
Blast radius: medium (retry semantics — auto-retry eligibility flips for
  one error code across DB and db-free paths)
Fixture level: expanded
Upstream suggested level: absent (issue is Readiness: needs-triage on the
  ruling only; triaged here — mandatory expanded trigger applies: retry
  semantics)
Why:
- Behavior change on the retry decision surface: OOM jobs stop
  self-healing by design; wrong delivery direction would silently
  disable recovery for genuinely transient siblings (guarded by the
  surface-disposition table D2 and the recompute-boundary anchor)
- Five surfaces (the issue's four sibling copies + the P1-1-discovered
  downstream-resume guard, which needs a production edit); one
  (recompute set) is ruled a non-copy — the boundary needs a two-sided
  test anchor
Selected risk packs:
- Retry/backoff semantics (auto vs manual, permanent-failure marking)
- Sibling-copy consistency (5 surfaces: 3 edits, 1 justified keep,
  1 guard block)
OpenSpec change: align-oom-retry-classification
Evidence floor:
- uv run pytest -q tests/test_retry.py tests/test_real_slurm_gateway.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_production_slurm_validation.py
  (known pre-existing local red: test_db_free_slurm_storage_root_check_...
  macOS symlink-loop env issue — disclose, don't chase)
- uv run ruff check .
- openspec validate align-oom-retry-classification --strict --no-interactive
- Dual sweep table in PR body (every hit → D2 row or non-goals):
  grep -rn "OUT_OF_MEMORY" AND grep -rn "is_transient_error\|is_retryable_failure\|classify_failure\|TRANSIENT_ERROR_CODES\|TRANSIENT_RETRY_REASON_CODES"
```

## Recorded deviations from issue #1161

1. **Ruling made in-fixture** (AC1 allows design.md): direction A with
   grounds in proposal.md/D1 — following the #1289 precedent of
   recording the semantic ruling in the fixture rather than a separate
   maintainer comment; the merge itself is the maintainer act.
2. **Evidence floor adds `tests/test_orchestration_chain.py`** beyond
   the issue's three-file pytest command: it hosts the OOM state-mapping
   fixtures the issue claims unaffected (currently `:6598,:6634`; the
   issue's `:6278,:6314` were `3d6d3b92`-era lines) — running it is
   the proof of that claim, not scope creep.
3. **`_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES` keeps OOM** via AC3's
   justified-non-change branch (design D2 row 4, two-sided anchor
   D4 "D2-row-4").
4. **New classifier label `resource_configuration`** — a vocabulary
   addition beyond pure "alignment" (fixture review noted it as an
   undisclosed-deviation candidate); grounds in D2 row 2: no production
   consumer branches on classifier strings, and letting OOM fall to
   `unknown_failure` would misstate a spec-classified code.
5. **Main spec gains one normative scenario** (parity anchor) the issue
   did not ask for — it is both the AC4 anti-drift lock and what gives
   this OpenSpec change a validatable delta.
6. **Production diff includes `scheduler_state_failure.py`** (one guard
   edit, D2 row 5) — the issue's four-surface list missed the
   downstream-resume channel entirely; without this edit the MUST NOT
   clause stays violated in the durable-output geometry (fixture review
   P1-1).

## 1. Fixture

- [x] 1.1 Author proposal/design/tasks + spec delta (one added scenario;
  classification lists unchanged — spec was already correct); ruling
  recorded (D1)
- [x] 1.2 Reviewer fixture review (read-only): round 1 NOT CLEAN
  (1 P1 — the P1-1 fifth surface, measured live; 4 P2, 3 P3), all fixed
  in-fixture; round 2 CLEAN (all 8 fixes verified by in-memory
  simulation incl. the row-5 guard: OOM → blocked/
  permanent_failure_guard, PARSE_FAILED channel untouched, zero
  collateral across 6 test modules) + 6 P3 doc-consistency items fixed;
  `openspec validate align-oom-retry-classification --strict
  --no-interactive` green

## 2. Implementation (implementer subagent)

- [ ] 2.1 `retry.py`: set membership move (TRANSIENT → NON_TRANSIENT);
  `failure_classifier` gains `resource_configuration` branch for OOM
- [ ] 2.2 `scheduler_state_types.py`: drop OOM from
  `TRANSIENT_RETRY_REASON_CODES`
- [ ] 2.3 `scheduler_state_failure.py` `_downstream_failure_restartable`:
  refusal sets gain `OUT_OF_MEMORY` + `resource_configuration` (D2 row 5)
- [ ] 2.4 Confirm-and-leave: `_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES`
  keeps OOM (D2 row 4); `scheduler_state_compat` re-export untouched;
  Slurm raw-state mapping surfaces untouched (non-goals list);
  `slurm_validation.py:1721` evidence flip disclosed, no edit
- [ ] 2.5 Rewrite the 5 pinning tests to spec behavior (D4.1-D4.5;
  anchor the two production_scheduler targets by test NAME)
- [ ] 2.6 New parity anchor test (spec-text ↔ code sets, bullet-token
  parsing per D4) — red pre-change
- [ ] 2.7 New D2-row-4 boundary anchor: half 1 (recompute preserved WITH
  missing-output geometry) green pre- and post-change; half 2
  (manual_retry_required WITHOUT that geometry) RED pre-change — both
  halves' pre-change outputs stated in the brief
- [ ] 2.8 New D2-row-5 anchor: OOM + durable output present → NOT
  retry_downstream, no automatic_retry_allowed:True — red pre-change
- [ ] 2.9 Red-proof protocol: rewritten tests + parity + row-5 + row-4
  half 2 red against pre-change source, red output recorded in the brief

## 3. Verification (orchestrator)

- [ ] 3.1 Evidence floor commands green
- [ ] 3.2 Dual full-repo sweep (literal OOM grep + indirect-consumer
  grep per triage block): every hit mapped to a D2 disposition row or
  the non-goals list (AC2's 无残余矛盾点)
- [ ] 3.3 Production diff confined to `retry.py`,
  `scheduler_state_types.py`, and the one
  `_downstream_failure_restartable` guard in
  `scheduler_state_failure.py`; tests-only elsewhere

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; findings verified before fix
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [ ] 5.0 Follow-ups routed and numbers recorded here: (a)
  `auto_retry_skipped` pipeline_event payload (`spec.md:154,170`)
  implemented for NO non-transient code repo-wide (issue #1161 flagged
  it out-of-scope); (b) the downstream-resume channel
  (`_downstream_failure_restartable`) still resumes unknown-default
  non-transient codes (e.g. `PARSE_FAILED`) against the spec's
  unknown-defaults clause — pre-existing, broader than this one-code
  alignment (fixture review P1-1 corollary)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final head
- [ ] 5.2 Merge; archive change (delta folds into main spec); loop-log
  line + audit; close issue #1161
