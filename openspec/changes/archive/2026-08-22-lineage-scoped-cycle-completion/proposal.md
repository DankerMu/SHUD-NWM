# Lineage-scoped cycle completion for recalibrated models

## Why

ADR 0005 ruled that a basin whose only change is calibration parameters SHALL
**continue** its computation rather than restart
(`docs/adr/0005-recalibration-state-carryover.md:36`). The clone mechanism
delivers half of that ruling: the state carries over, and the clone row records
`cloned_from_model_id` / `clone_gate_kind`
(`openspec/specs/fingerprint-gated-state-clone/spec.md`). The scheduler
delivers none of it — ADR 0005 explicitly says "Scheduler admission and lineage
validation are **not** touched" (`:107`).

The result wedged production on 2026-08-22. `#1698` activated `M1'` for
`basins_jialingjiang` and `Huai-MAIN`; the new packages mint new
content-derived `model_id`s. Every completion predicate in the scheduler is
keyed strictly by `model_id`:

- `cycle_completion_status` (`services/orchestrator/scheduler_discovery.py:134`)
  scores a cycle `complete` only if **every** model in scope completed it;
- `_models_in_completion_scope` (`:158-166`) filters by source scope only —
  there is no "did this model exist at cycle C" scoping;
- all three verdict tiers in `_cycle_completion_verdict` (`:170-284`) reduce to
  per-`model_id` lookups: `has_completed_pipeline(source_id, cycle_time,
  model_id)` — `services/orchestrator/chain_repository.py:97-109` (DB, no
  lineage join) and `services/orchestrator/file_orchestration_journal.py:604-636`
  (db-free, explicitly excluding "another model's job rows", #1302).

`M1'` has zero pipeline history before `t*`, so **every one of the 29 cycles in
the 336 h lookback flipped from `complete` to `gap`** across two adjacent passes
(07:07:58Z → 08:58:26Z). Backfill then trims `source_cycles` to the globally
earliest cycle time (`scheduler_discovery.py:535-551`) and takes
`remaining_gaps[:1]` per source (`:602`), so the scheduler pinned itself on
`gfs_2026080700` — a cycle it can never close, because closing it requires
`M1'` to have completed a cycle that predates `M1'`'s own existence. The
forward lane has been starved since 06:33Z and the public display is stalled.

The lineage data needed to answer this correctly is already loaded and already
unread: `cloned_from_model_id`, `cloned_from_state_id`,
`clone_gate_fingerprint` and `clone_gate_kind` survive into the file state-index
entries as pass-through fields (`packages/common/state_manager.py:2390-2452`
builds `{**row, ...}` with no projection whitelist), and the DB plane has
`get_latest_clone_row_for_model_source`
(`packages/common/state_manager.py:798-841`). No code anywhere in the tree walks
`cloned_from_model_id` at scheduling time.

This is not a one-off: more recalibration rollouts of the `jialingjiang` class
are queued, and each one would re-wedge the scheduler in the same way.

## What Changes

- A **lineage resolver** resolves, for a `(model_id, source_id)` pair, the
  model's own cutover instant `t*` and its immediate predecessor `model_id`, by
  reading the clone entry already present in the loaded state index (db-free
  plane) or an earliest-clone-row read on the DB plane. `t*` is the **earliest**
  clone row under the model's own id — its existence-start — and the resolver
  does **not** walk the ancestry chain (see `design.md` D4). Both
  `clone_gate_kind` values confer lineage.
- `_models_in_completion_scope` gains a **lineage-existence filter**: a model
  whose resolved `t*` for that source is strictly greater than the cycle time
  is not in completion scope for that cycle. The same helper is applied at
  cohort/candidate construction so the model is not submitted for cycles that
  predate its own existence.
- Evidence records `lineage_scoped_out_pre_cutover` with the predecessor
  `model_id` and `t*` per scoped-out `(model, cycle)` — an annotation, never a
  scoring input.
- An **empty-scope disambiguation**: a cycle whose scope is empty *because every
  model was lineage-scoped out* is not a gap; a cycle whose scope is empty
  *because no models were configured* keeps today's `gap` verdict
  (`tests/test_scheduler_backfill.py:1087`).

## Why scope, not delegate

Answering `has_completed_pipeline(C, M')` by delegating to the predecessor was
the first design. It does not work, for two independent reasons:

1. **Tier A never reaches the repositories.** `_cycle_completion_verdict`
   consults the candidate-state providers per model (`:186-232`) *before*
   Tier B's `has_completed_pipeline` (`:234-249`). Delegation inside the
   repositories leaves `M1'` producing non-terminal evidence in Tier A, and the
   cycle still scores `gap`.
2. **Delegation reintroduces the deadlock on its unhappy path.** If the
   predecessor also lacks completion at `C`, a delegating predicate scores
   `gap` — and nobody can fill it, because the predecessor is retired and
   absent from the manifest. Scoping has no such branch: the basin is simply
   not scored before it existed, which is what the completeness predicate
   ("every model's full pipeline is done for this cycle", `:134`) already means
   when read against the *current* model set.

Scoping also keeps the DB `ActiveCandidateRepository` unchanged; only the
resolver is per-plane.

## Non-Goals

- **`generation_scoped_history_signal` is not touched.** Its `entries_for_model`
  filter carries no `valid_time` bound (`state_manager.py:1418-1432`, deliberate
  per its docstring), so `M1'`'s clone row makes `exists_any_generation` `True`
  at **every** cycle — not only at `C >= t*`. The first-cycle branch
  (`services/orchestrator/scheduler_generation.py:1082`) is therefore already
  unreachable for `M1'` at any cycle, and was never gated on `C`; `M1'` lands on
  the predecessor-pending branch instead. Cohort suppression stops
  `evaluate_transition_decision` from being invoked for `M1'` at `C < t*` at
  all, so the non-goal holds — but it holds for that reason, not because
  suppression closes a cold-start branch. Changing the `regardless of
  valid_time` filter would alter cold-start semantics for models with no
  lineage — out of scope.
- **Models without lineage behave byte-for-byte as today.** The seven new basins
  from `#1699` have no clone rows; they keep `exists_any_generation == False`
  and the `packaged_ic_bootstrap` / cold-start path unchanged. Their backfill
  sweep cost is a separate decision, tracked with the held `#1699` refresh.
- Content-derived `model_id` derivation is unchanged; the cold-start window is
  not relaxed.
- D8.3 / D8.7 generation quarantine on the **admission** side is unchanged. This
  change touches completion scope and cohort membership only.
- `#1734` (unindexed full-tree journal replay in `_iter_pipeline_job_records`)
  is a separate PR.
- The `basins_xinanjiang_upstream` (gfs) / `basins_tailanhe` (ifs) full ~1 h
  recompute every pass is a separate open anomaly. The retention hypothesis is
  disproved: the 2026-08-22T04:24Z retention run deleted only 56 evidence JSONs
  dated 07-25, and both models still hold 97 usable state-index entries.
