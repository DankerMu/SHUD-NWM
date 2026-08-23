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
- [ ] Read-only fixture review by a reviewer subagent.

## 2. Implementation

- [ ] Remove the `array_task_id` comparison from
      `forecast_cohort_runtime_identity_matches`. Nothing else in the loop moves.
- [ ] Correct the comment above the comparison (`~:1813-1818`), **in place** —
      it asserts the per-model writers never persist the three fields, which is
      false for `create_hydro_run_from_basin`. Record the correction, do not
      silently rewrite it.

## 3. Evidence floor

- [ ] **Regression test: renumbered member set.** Same `(source, cycle, model)`
      `hydro_run` rows carrying an **old layout's** `array_task_id`; a new cohort
      with a different member set and renumbered indices. Identity must pass.
      **Must be shown red against today's code** — paste the red run in the PR
      before the fix commit.
- [ ] **Regression test: multi-submission layout churn, not field absence.** The
      donor change already covers absence. This test must pin the
      *present-but-stale* case, which is the production failure. Also red first.
- [ ] **Sibling legs still bite.** A present-but-different `candidate_id` or
      `basin_id` still fails. Must be shown to bite (mutate the value, watch red).
- [ ] **Retained strict fields still bite**, `submission_attempt` included —
      see D4: it is a known defect of the same class (#1792) and is NOT fixed
      here, but its comparison must still be *present and biting* after this
      change, or the removal has over-reached.
- [ ] **Delta scenario 4 pinned explicitly, not transitively.** The
      `matched_bound` scenario (rows absent `candidate_id`/`basin_id`) is today
      covered only by folding
      `tests/test_gateway_reconcile.py::test_file_cohort_terminal_projects_when_hydro_run_rows_lack_planning_identity`
      into the generic suite run. Name it in the PR evidence as the pin for that
      scenario and confirm it still passes, so all four scenarios have an
      itemised obligation rather than three itemised and one implicit.
- [ ] `uv run pytest` over the journal and reconcile suites; `uv run ruff check .`.

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
