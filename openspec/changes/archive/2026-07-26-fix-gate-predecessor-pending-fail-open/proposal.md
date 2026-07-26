# Fix §8 gate fail-open: block_predecessor_pending must not return None (#1150)

## Why

`services/orchestrator/scheduler_generation_gate.py:452-455` returns `None`
(cold-seed passthrough) whenever the strictly-earlier history probe reports
`history_exists=False` — but that branch is reached by BOTH `warm_continue`
and `block_predecessor_pending` transition decisions. The inline comment only
argues safety for `warm_continue`. Under `NHMS_REQUIRE_FORECAST_WARM_START=false`
(env=true short-circuits at `:427` and never reaches this branch), a candidate
the §8 transition matrix has already decided to BLOCK (`block_predecessor_pending`)
is translated into `None`, which `scheduler_candidates.py:338-342` treats as
"no warm gate" → the candidate is ADMITTED with empty `state_evidence`, no
blocker, no typed reason. This is a p1 fail-open: the env flag alone flips
admit/block on the same fixture, directly contradicting the merged spec
requirement "The warm-start env override SHALL NOT admit candidates blocked
by state-lineage invariants" (`openspec/specs/file-state-snapshot-index/spec.md`)
and the D8.9 contract comment at `scheduler_core.py:736-748`.

Root cause (traced in issue #1150, introduced by `821af66c` #1081/#1105): two
inconsistent history predicates. The transition matrix's generation-scoped
signal counts ANY usable current-generation entry regardless of `valid_time`
(`state_manager.py:1402-1411`), while the gate's fallback probe
`usable_state_history_evidence` only counts entries strictly earlier than the
candidate cycle (`state_manager.py:1296-1304`). When the index holds only
same-or-later entries (real triggers: retention pruned earlier entries, or a
backfill of an earlier cycle), the matrix says "history exists → predecessor
pending → BLOCK" while the probe says "no history → cold-seed passthrough".

## What Changes

- `scheduler_generation_gate.py:452-455`: split the `history_exists=False`
  branch by transition decision, POSITIVE predicate — `None` cold-seed
  passthrough only when `transition.decision == WARM_CONTINUE` (unchanged
  design intent; any future unenumerated decision fails closed).
  `block_predecessor_pending`
  falls through to the EXISTING blocked-evidence construction directly below
  (typed reason `state_snapshot_index_prior_checkpoint_missing_after_history`,
  classifier `file_state_snapshot_index_unavailable`, `retryable=True`) — the
  evidence already carries `state_history` (showing `history_exists=False`)
  and `registry_cutover_transition` (showing `block_predecessor_pending`), so
  operators can discriminate this shape from the history-exists sibling
  without any new reason string.
- Red-provable integration test at the `_build_candidates` seam
  (`tests/test_scheduler_generation.py`): env=false + valid in-window
  declaration + single current-generation entry at `valid_time` LATER than
  the candidate cycle → must block; env=true same fixture unchanged
  (`state_snapshot_index_exact_checkpoint_missing`).
- Spec delta: MODIFIED requirement in `file-state-snapshot-index` — drop the
  "usable history exists strictly earlier" precondition from the
  missing-predecessor arm (block now fires whether or not such history
  exists) and add a scenario for the no-earlier-history shape.

## Design decisions

- **Chosen: decision-based split, reuse existing reason.** The block decision
  was already made by the §8 matrix; the gate's job is to add field-level
  detail, not to overturn it. Reusing
  `state_snapshot_index_prior_checkpoint_missing_after_history` keeps the
  public reason vocabulary and failure-classifier mapping (`retryable=True` —
  remedy is backfilling the predecessor) stable; no classifier change needed.
- **Not chosen: widen the gate's history probe** to the generation-scoped
  wide predicate. That would require a new/changed `state_manager` query
  entry point, and `usable_state_history_evidence`'s narrow semantics has an
  independent consumer at `scheduler_core.py:790` — touching it risks a
  second semantic drift for no additional safety.
- **Legacy sibling `scheduler_generation_gate.py:239-240` untouched**: that
  pre-§8 path has NO transition decision — "no history → cold seed" is its
  original design intent, and there is no matrix block being overturned. The
  PR body records this disposition (issue AC requires it).

## Impact

- Affected specs: `file-state-snapshot-index` (MODIFIED requirement, +1 scenario)
- Affected code: `services/orchestrator/scheduler_generation_gate.py` (one
  branch, ~4 lines), `tests/test_scheduler_generation.py` (+1 test)
- Must preserve: `warm_continue` cold-seed passthrough (`None`) when
  `history_exists=False`; the history-exists block branch guarded by
  `tests/test_scheduler_generation.py:1120` (T15(b)); env=true behavior;
  COLD_DECLARED_CUTOVER admits; legacy no-checksum/no-declaration path
  (`:231-240`); D8.9 preflight cases (`tests/test_scheduler_generation.py:1721+`, T17 section).
- Downstream surface: the §8.6 predecessor-backfill emitter
  (`scheduler_backfill_predecessor.py`) now sees the newly-blocked
  population (blocked evidence carries `selected_predecessor`); for the
  no-earlier-history geometry the emitted predecessor itself re-blocks
  under the same rule, so the gap cannot self-heal — operator-signal
  decision, env-wired emitter test, and runbook entry deferred to #1152
  (verifier-confirmed P3, pre-existing in kind).
- Non-goals: §8.7 journal-side quarantine (#1107); generic
  `usable_state_history_evidence` refactor; legacy path hardening.
