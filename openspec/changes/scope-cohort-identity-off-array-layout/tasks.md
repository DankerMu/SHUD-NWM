# Tasks

## Risk triage

- **Fixture level: high.** The change edits a production identity gate on the
  path of a live P0 stall (#1748). A gate that wrongly passes is worse than one
  that wrongly fails, so the review must check the *removal* for over-reach, not
  only the fix for correctness.
- **Must-preserve behaviour**: `run_id` / `model_id` / `scenario_id` /
  `source_id` / `cycle_time` / `submission_attempt` stay strict; `candidate_id`
  and `basin_id` keep present-but-different-is-fatal; every reconcile-side gate
  unchanged.
- **Seam under test**: `forecast_cohort_runtime_identity_matches`
  (`services/orchestrator/file_orchestration_journal.py:1784`), sole caller
  `services/orchestrator/reconcile.py:1174`.
- **Risk packs selected**: correctness-silent-miss (a gate that passes wrongly
  is silent by construction); spec-conformance (this change MODIFIES a
  requirement and archives a corrected donor — the class that fired twice on
  PR #1759); test-oracle-integrity (both new tests must be shown red first).
- **Not selected**: concurrency-perf (no locking or read-path change);
  security (no auth surface).
- **Reviewer lens names MUST be recorded** in the round ledger and the loop-log
  line. PR #1788 lost them and its rotation attribution is unusable as a result.

## 1. Fixture

- [x] Correct the donor change's falsified premise (design + delta rationale),
      clause-to-code check it, and archive it **before** this change's delta, so
      the false rationale never lands in `openspec/specs/`.
- [x] Author proposal / design / delta; `openspec validate --strict` green.
- [x] Read-only fixture review by a reviewer subagent.

## 2. Implementation

- [x] Remove the `array_task_id` comparison from
      `forecast_cohort_runtime_identity_matches`. Nothing else in the loop moves.
- [x] Correct the comment above the comparison (`~:1813-1818`), **in place** —
      it asserts the per-model writers never persist the three fields, which is
      false for `create_hydro_run_from_basin`. Record the correction, do not
      silently rewrite it.

## 3. Evidence floor

- [x] **Regression test: renumbered member set.** Same `(source, cycle, model)`
      `hydro_run` rows carrying an **old layout's** `array_task_id`; a new cohort
      with a different member set and renumbered indices. Identity must pass.
      **Must be shown red against today's code** — paste the red run in the PR
      before the fix commit.
- [x] **Regression test: multi-submission layout churn, not field absence.** The
      donor change already covers absence. This test must pin the
      *present-but-stale* case, which is the production failure. Also red first.
- [x] **Sibling legs still bite.** A present-but-different `candidate_id` or
      `basin_id` still fails. Must be shown to bite (mutate the value, watch red).
- [x] **Retained strict fields still bite**, `submission_attempt` included —
      see D4: it is a known defect of the same class (#1792) and is NOT fixed
      here, but its comparison must still be *present and biting* after this
      change, or the removal has over-reached.
- [x] **Delta scenario 4 pinned explicitly, not transitively.** The
      `matched_bound` scenario (rows absent `candidate_id`/`basin_id`) is today
      covered only by folding
      `tests/test_gateway_reconcile.py::test_file_cohort_terminal_projects_when_hydro_run_rows_lack_planning_identity`
      into the generic suite run. Name it in the PR evidence as the pin for that
      scenario and confirm it still passes, so all four scenarios have an
      itemised obligation rather than three itemised and one implicit.
- [x] `uv run pytest` over the journal and reconcile suites; `uv run ruff check .`.

## 4. node-22 receipt — criterion corrected up front, before the fixture exists

The issue's acceptance criterion reads "`gfs_2026080712`-class historical cycles
no longer end in `identity_mismatch_released`". **That criterion is
unsatisfiable and is corrected here rather than at receipt time**, which is the
#1734 lesson applied prospectively. Reason, from the #1749 triage comment: those
cohorts sit in a non-reclaimable terminal, and fixing the gate does not revive
them — their exit is #1748's scope. A receipt written against them would fail
for a reason that has nothing to do with this change.

Corrected criterion:

- [ ] Across **N >= 3** post-deploy passes on node-22: **zero newly minted**
      `identity_mismatch_blocked` / `identity_mismatch_released` records.
      Denominator and pass ids recorded; counted from journal, not inferred.
- [ ] **If a member-set change occurs naturally in the window**: that cohort
      passes identity and submits. Record the two layouts and the member counts.
- [ ] **Pre-declared fallback, so the receipt cannot be quietly restated later**:
      if no churn occurs in the window, the behaviour proof is the deterministic
      red -> green regression test above and the production proof is the clean
      passes; "churn not observed in window" is recorded as such, and the receipt
      is NOT presented as having demonstrated the churn path in production.
- [ ] Record whether any cohort in the window is at `submission_attempt >= 2`.
      Per D4 those stay blocked by `:1829` and that is **expected, not a
      regression of this change** — it is #1792.

## Implementation record (2026-08-23)

Production change is 3 deleted lines plus an in-place comment correction
(`764a1275`). Verified independently at Phase 2: zero remaining production
readers of `hydro_run.array_task_id`, 37 targeted tests pass, ruff clean.

Red-first honoured: both new identity tests failed against unmodified
production code for the right reason (`identity_mismatch_blocked` instead of
`terminal`; `forecast_cohort_runtime_identity_matches(...) is False`). Three
mutation bite proofs, each clearing `__pycache__` around mutate/restore:
deleting the sibling legs reddens 4 tests, deleting the `submission_attempt`
comparison reddens exactly the `[submission_attempt]` parameter, deleting the
`run_id`/`model_id`/`scenario_id` loop reddens `[run_id]` and `[scenario_id]`.

### Deviations accepted, with reasons

1. **An existing test parameter was deleted**:
   `{"array_task_id": 99}` from
   `test_file_cohort_present_but_different_runtime_identity_still_blocks`. It
   pinned exactly the behaviour this change removes, so it must go — delta
   scenario 1 requires that shape to pass now. This is oracle **narrowing to
   the new spec**, not weakening: the same input is now asserted by the new
   tests to pass, so the case is still covered, with the opposite expectation.
2. `_append_cohort_placeholders` now returns the written rows. Test 2's whole
   discriminating power is asserting the persisted `array_task_id` is
   **non-`None` and different** before asserting the gate passes — without that
   guard it cannot distinguish present-but-stale from absent, which is the
   distinction the Evidence Floor asks for. Existing callers ignore the return.
3. Test 4's `run_id` leg mutates the **member** side, not the row side. Row-side
   is structurally impossible: the journal binds row `run_id` to `model_id` and
   raises `file_journal_run_mismatch` on write
   (`file_orchestration_journal.py:10961`). Same comparison, operands reversed.

### Known limit, recorded rather than papered over

`model_id` / `source_id` / `cycle_time` have **no direct pin**. Producing a
disagreement through the public write seam is either unrepresentable or makes
the row unfindable (lookup is by model), which surfaces as the row-missing
branch. Scenario 3 accepts that failure too, but it is not the comparison leg
itself. The three reachable legs (`run_id`, `scenario_id`, `submission_attempt`)
are pinned directly; `model_id`'s loop is shown to bite via its two siblings.

### Third same-class field: searched for, none found

`run_id`/`model_id`/`scenario_id`/`source_id`/`cycle_time`/`candidate_id`/
`basin_id` are all layout-independent and stable across submissions per D2's
measurements. `_reset_hydro_run_after_retry_submission` (`:8816-8823`) only
touches `failed`/`cancelled` rows, consistent with D1/D4's freeze semantics and
introducing no new asymmetry. `submission_attempt` (#1792) remains the only
known sibling defect.
