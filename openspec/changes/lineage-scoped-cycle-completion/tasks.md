# Tasks — lineage-scoped cycle completion (#1735)

## Evidence Floor

- [ ] `uv run ruff check .` clean (local)
- [ ] `openspec validate lineage-scoped-cycle-completion --strict --no-interactive` clean (local)
- [ ] New/changed unit tests green, and each named trap below has its own test
- [ ] Full targeted suites green: `tests/test_scheduler_backfill.py`,
      `tests/test_scheduler_backfill_predecessor.py`,
      `tests/test_scheduler_generation.py`,
      `tests/test_state_manager_generation_history.py`,
      `tests/test_production_scheduler.py`
- [ ] node-27 real-DB pytest green (DB-plane resolver parity)
- [ ] **node-22 live receipt** matching `design.md` "Live receipt shape" — this is
      the production oracle and also closes `#1698`'s outstanding warm verification

## 1. Lineage resolver

- [ ] 1.1 Add a resolver that maps `(model_id, source_id)` to
      `(predecessor_model_id, t*)` or "no lineage".
- [ ] 1.2 db-free plane: read `cloned_from_model_id` / `clone_gate_kind` /
      `valid_time` from the already-loaded index snapshot entries
      (`packages/common/state_manager.py:2390-2452` pass-through). No extra IO.
- [ ] 1.3 DB plane: resolve via
      `PsycopgStateSnapshotRepository.get_latest_clone_row_for_model_source`
      (`packages/common/state_manager.py:798-841`).
- [ ] 1.4 `t*` is the **earliest** clone row under the model's own
      `(model_id, source_id)`, ordered `(valid_time, created_at)` ASC — its
      existence-start (D4). On the DB plane this needs its own read; it MUST NOT
      reuse `get_latest_clone_row_for_model_source`'s `DESC` ordering.
- [ ] 1.5 The resolver does **not** walk the ancestry chain. A chain ancestor's
      `t*` never participates in a scope decision, so there is no recursion and
      no visited-set guard on the critical path (D4).
- [ ] 1.6 Both `clone_gate_kind` values confer lineage; absent/unrecognised kind
      with `cloned_from_model_id` present still confers lineage (D2).
- [ ] 1.7 Absent provenance ⇒ "no lineage", never an error.

## 2. Completion scope filter

- [ ] 2.1 `_models_in_completion_scope`
      (`services/orchestrator/scheduler_discovery.py:158-166`) drops a model when
      its resolved `t*` for that source is strictly greater than the cycle time.
- [ ] 2.2 Empty-scope disambiguation (D5): distinguish "empty before the lineage
      filter" (⇒ `gap`, unchanged) from "emptied by the lineage filter"
      (⇒ not a gap).
- [ ] 2.3 Verify all three `_cycle_completion_verdict` tiers (`:170-284`) consume
      the filtered scope — Tier A's provider loop included, since it runs before
      `has_completed_pipeline` is ever reached.

## 3. Cohort / candidate suppression — primary correctness, not garnish

Scoping alone leaves a second deadlock: `exists_any_generation` is `True` for
`M1'` at every cycle, so a pre-`t*` submission lands on `block_predecessor_pending`
and `emit_predecessor_candidates` prepends an even earlier candidate — the
backward-recursion dead chain. See `design.md` D1.

- [ ] 3.1 Apply the same helper at cohort/candidate construction so a model is
      not submitted for cycles earlier than its `t*`.
- [ ] 3.2 Backfill predecessor emission
      (`services/orchestrator/scheduler_backfill_predecessor.py`,
      `_extract_pending_predecessors`) does not prepend a candidate for a model
      at a cycle earlier than that model's `t*`.

## 4. Evidence annotation

- [ ] 4.1 Record `lineage_scoped_out_pre_cutover` per excluded `(model, cycle)`
      with the predecessor `model_id` and the resolved `t*`.
- [ ] 4.2 The record is never read back as a decision input.

## 5. Tests — the named traps

Each of these is a deadlock re-entry path; each needs its own test.

- [ ] 5.1 Pre-`t*` cycle with a lineage-bearing model scores `complete`, and the
      model is not in the cohort.
- [ ] 5.2 `C == t*` still requires the model to genuinely complete (strict
      boundary, D3).
- [ ] 5.3 No lineage ⇒ unchanged scoring **and** unchanged first-cycle /
      cold-start branch (`scheduler_generation.py:1082`). Cover both surfaces.
      Note: for a model that DOES have lineage this branch is unreachable at any
      cycle (`exists_any_generation` has no `valid_time` bound), so do not write
      an assertion that claims suppression is what closes it.
- [ ] 5.4 **Trap 1 — empty scope.** Two tests: all-models-lineage-scoped-out ⇒
      not a gap; no-models-configured ⇒ `gap`
      (`tests/test_scheduler_backfill.py:1087` must still pass unmodified).
- [ ] 5.5 **Trap 2 — §8.7 breaker.** Pre-`t*` cycle + predecessor `M`'s journal
      rows carrying `M`'s identity tokens + `M'` in the current model set ⇒
      `complete`, breaker not engaged
      (`scheduler_discovery.py:287-357`, `:364-417`).
- [ ] 5.6 **Trap 3 — predecessor emission at the boundary.** Cycle `t*` selected
      ⇒ no prepend of `M'` at `t*−12h`; warm start resolves to the clone row at
      `valid_time == t*`.
- [ ] 5.7 **Second-recalibration regression (was the fixture-review P1).** Chain
      `M → M'` at `t1`, `M' → M''` at `t2`, `t1 < t2`, only `M''` active:
      `M''` is scoped out of `C < t2` and **in scope** for `[t1, t2)` is WRONG —
      assert `M''` is scoped out of `[t1, t2)` too, i.e. the boundary is `t2`
      (its own `t*`), never `t1`.
- [ ] 5.8 Multiple clone rows under one `model_id` (re-activation with a
      backdated `t*`): the boundary is the earliest, so cycles the identity
      actually ran are never scoped out.
- [ ] 5.9 Per-source `t*`: GFS and IFS cutting over at different instants are
      scoped independently.

## 6. Docs

- [ ] 6.1 Append a revisit note to `docs/adr/0005-recalibration-state-carryover.md`
      recording that its `:107` "Scheduler admission and lineage validation are
      not touched" is now qualified: completion scope and cohort membership do
      consume lineage; admission-side quarantine remains untouched.
- [ ] 6.2 Note the operational meaning in the production ops runbook: a
      recalibration rollout no longer re-opens the backfill window.
