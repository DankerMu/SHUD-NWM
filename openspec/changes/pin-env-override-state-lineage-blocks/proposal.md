# Reshape T15(b)/(c) fixtures to reach the promised state-lineage branches (#1109)

## Why

PR #1105 round-3 verifier judged F2(b)/(c) PARTIAL: both tests honor the
"env=false does not bypass §8" invariant, but their fixtures short-circuit
at declaration validation (fixture review corrected the mechanism: T15(b)'s
declaration is actually in-window — the stale classification comes from the
declaration `generation` field mismatch, not window math), so the branches
the test NAMES promise —
`state_snapshot_index_prior_checkpoint_missing_after_history` (T15(b),
the AC5 defect branch #1081 exists to keep closed) and
`state_snapshot_index_generation_mismatch` (T15(c), core §8 invariant) —
have NO CI assertion protection. Both docstrings honestly record the
drift. Meanwhile the peer
`test_env_override_does_not_admit_declaration_less_cutover` already covers
the declaration-layer block, so T15(b)/(c) currently triplicate that
semantics while the two real branches sit unguarded: a future rename /
merge / removal of either typed reason upstream would keep CI green and
silently revive the AC5 defect.

## Decision (route recorded)

Adopt the issue's recommended route: **reshape the two fixtures in place**
(valid in-window declaration so the gate passes the declaration layer,
then seed state-index history to trigger the promised branch), keeping the
round-1 A4 single-value pin (no OR-set). The alternative (rename the two
tests + add two new ones) is rejected per the issue's own tradeoff: four
tests instead of two, with the declaration-layer semantics then asserted
in triplicate. Test-only: `services/orchestrator/` is untouched by
explicit prohibition.

## What Changes

- `tests/test_scheduler_generation.py:1119-1252` (T15(b)): loadable
  declaration + one current-generation state entry strictly earlier than
  the candidate cycle (checksum matching the candidate) so
  `history_exists=True` while the expected predecessor identity key
  (`valid_time` = candidate cycle, producing `cycle_id` = cycle − 12h,
  `lead_hours` = 12) stays empty → assert
  `blocked[0].reason == "state_snapshot_index_prior_checkpoint_missing_after_history"`
  and `TransitionDecision.BLOCK_PREDECESSOR_PENDING`. Exact coordinates
  in tasks 1.1 (fixture review corrected the key geometry and the
  COLD_DECLARED_CUTOVER trap on `effective_cycle_utc`).
- `tests/test_scheduler_generation.py:1258-1370` (T15(c)): (e)-branch
  route — an OLD-generation entry AT the expected predecessor identity
  key plus a second current-generation entry elsewhere (so
  `exists_current_generation=True` and declaration binding does not
  participate) → assert
  `blocked[0].reason == "state_snapshot_index_generation_mismatch"` and
  `TransitionDecision.BLOCK_WRONG_GENERATION`. (d)-branch checksum
  plumbing kept only as a reported-deviation fallback (tasks 1.2).
- Both docstrings rewritten to match the actual asserted branch; the
  "would need a fixture ... tracked separately" drift notes removed.
- Spec delta: one ADDED requirement on `file-state-snapshot-index`
  pinning that the warm-start env override cannot admit candidates
  blocked by the two state-lineage invariants (making the guarded
  behavior normative so the coverage cannot silently regress again).

## Out of Scope

- Any change under `services/orchestrator/` (explicitly prohibited by the
  issue — no §8 gate, `_strict_warm_start_for_candidate`, or
  `scheduler_candidates.py` edits).
- The peer `test_env_override_does_not_admit_declaration_less_cutover`
  (:531-619) — correctly wired, untouched.
- Test renames; #1107 (journal quarantine) and #1108 (orphaned A4
  fixture) — confirmed distinct.

## Impact

- Affected specs: `file-state-snapshot-index` — one ADDED requirement
  (env-override does not admit state-lineage-blocked candidates; capability
  is still owned by the live `node22-db-free-scheduler-state` change, so
  this lands as a sibling delta on the same capability). Overlap note: the
  sibling delta's scenario "Broad env override does not loosen generation
  semantics" (:168-176) covers the same axis without typed
  reasons/decisions — this requirement REFINES it with the two typed
  pins; on archive the two coexist without contradiction.
- Affected code: `tests/test_scheduler_generation.py` ONLY (acceptance
  criterion: `git diff --name-only origin/master...HEAD` shows the test
  file plus this openspec change and nothing else).
- Reference patterns (read-only; line numbers corrected by fixture
  review): `_build_registry_state`
  (`tests/test_scheduler_generation.py:812`),
  `test_transition_blocks_wrong_generation_at_expected_predecessor_key`
  (`tests/test_scheduler_generation.py:627-679`, direct-call style only —
  no state-index seeding), `_old_generation_state_entry`
  (`tests/test_production_scheduler.py:20134`),
  `_write_db_free_state_index_fixture` (:20175).
