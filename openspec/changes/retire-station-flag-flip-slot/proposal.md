# Proposal — retire-station-flag-flip-slot (#3165)

Closes #3165.

## Why

#3164 deleted `packages/common/station_set_flip.py`, the only consumer of the `station_flag_flip` pre-activation mount
point (no production code ever registered it). The slot was kept as a default no-op, because dropping it means editing
`tests/test_variant_activation_cutover.py` (1770 lines, not excluded by `.large-file-guard.json`, so the guard blocks
any commit that stages it). The slot is dead configuration:
- `PRE_ACTIVATION_HOOK_MOUNT_POINTS = ("state_clone", "station_flag_flip")` (`packages/common/model_registry_contracts.py:266`);
- seeded as a no-op in `packages/common/model_registry.py:196`, accepted by `register_pre_activation_hook`
  (`packages/common/model_registry_lifecycle_support.py:42-51`) and dispatched in declared order (`:66-76`);
- reserved in `openspec/specs/variant-activation-cutover/spec.md:47-51`;
- `fixed-station-forcing-production` still names "the Change 8 flip" as the owner of `active_flag=true` (~:95, ~:106,
  ~:113).

## What

1. **Split `tests/test_variant_activation_cutover.py`** into partitions under 1000 lines each, following the #2527
   precedent (`openspec/changes/archive/2026-09-24-split-oversized-surfaces-batch-2/design.md:52`):
   - the shared harness (`_model_row`, `_RecordingCursor`, `_FakeTransaction`, `_HarnessStore`, `_decision`, the
     `variant_direct_grid_profile` / `two_models` fixtures, ~:57-327) moves to a support module
     `tests/variant_activation_cutover_helpers.py`;
   - the base path keeps the pre-activation hook and helper cases (~:328-716) and stays a module-scope importer of
     `packages.common.model_registry`, because it is the `GUARDED_MODULE_CLOSURES` known member
     (`tests/test_select_ci_tests.py:6259`);
   - the manifest re-publish cases (~:717-1365) go to `tests/test_variant_activation_cutover_manifest.py`;
   - the dispatch-set, retirement and concurrency cases (~:1366-end) go to
     `tests/test_variant_activation_cutover_dispatch.py`.

   Every test keeps its name and body except the tests rewritten in item 2. The collected node count stays the same.
   Every selector rule, table, pin and reference that names the base path also names each new partition (and the
   helper module gets a support-module route): `scripts/select_ci_tests_rules.py:2306`,
   `scripts/select_ci_tests_tables.py:1717`, `tests/test_select_ci_tests.py:2194,6259,16670`, `pyproject.toml:221`
   (per-file ruff ignore), `scripts/ci/full_test_durations.json`, and the docstring references in
   `tests/test_state_clone_index_publish.py:462,488,533` and `tests/test_legacy_reactivation_guard.py:13`.
2. **Drop `"station_flag_flip"`** from `PRE_ACTIVATION_HOOK_MOUNT_POINTS`, so the tuple becomes `("state_clone",)`.
   - Registration on `"station_flag_flip"` is then rejected as an unknown mount point by the existing validation.
   - Rewrite the tests that register on it (~:349, :434, :485, :536, :577, :619, :1714). Ordered dispatch and
     abort-on-raise (a raising first hook stops later hooks and rolls back the swap and the audit row) stay covered:
     the tests monkeypatch a local test-only two-entry tuple into the dispatch module's binding, or use the single
     mount where ordering is not the subject.
   - Add one case: registering on `"station_flag_flip"` raises.
   - The PR names every rewritten or deleted test.
   - Update the comments that point at #3165: `model_registry_contracts.py:259-265`, `model_registry_lifecycle.py:333-335`,
     `model_registry.py:186-191`, `tests/test_direct_grid_display_cutover_history.py:41-46`, and
     `packages/common/state_clone_hook.py:16` if it names the second slot.
3. **Spec `variant-activation-cutover`.** REMOVE "Activation runs ordered pre-activation extension-point hooks inside the
   transaction" and ADD "Activation runs the ordered pre-activation hook chain inside the transaction" without the
   reservation scenario. The validator rejects a MODIFIED that drops a scenario, so this takes REMOVED plus ADDED. The
   hook contract, the declared-order dispatch, abort-on-raise and the already-current skip are restated unchanged. The
   chain has one mount point, `state_clone`, and an unknown mount point is rejected.
4. **Spec `fixed-station-forcing-production`.** MODIFY "Direct-grid mirror maintenance preserves the registration-owned
   active_flag" so it no longer names the retired Change 8 flip as the owner of `active_flag=true`. Mirror activation
   is outside the producer: no production code path sets it today. The producer's never-escalate guard, the
   insert-inactive rule and the fail-closed collision predicate are unchanged, and so is every scenario's assertion.
5. **Report only: state-clone wiring.** `register_pre_activation_hook` has no non-test call site
   (`git grep register_pre_activation_hook` hits only `model_registry_lifecycle_support.py:42` and a docstring at
   `state_clone_hook.py:248`), so the `state_clone` hook is not wired in production either. The PR records this, and a
   separate issue is filed for the decision (wire it or retire it). This change does not wire or delete it.

## Non-goals

- No change to the lifecycle transaction, the dispatch gate, the manifest publisher or `state_clone_hook.py` behaviour.
- No change to migration 000033 or any DB object.
- No `.large-file-guard.json` exclusion is added.

## Must preserve

- Every pre-existing test node in the split file still exists by name (except the tests rewritten in item 2, which are
  named in the PR), and the collected count does not drop.
- Activation behaviour is byte-identical: the dispatch iterates the tuple, and the removed entry was a no-op.
- Selection: a diff to any partition, to the helper module, or to the modules that route to the base path today selects
  every partition. Each new partition has a selector pin that is red before its rule is added.

## Fixture level

standard. Risk packs: legacy compatibility (test split, node-count preservation), CI routing (partition registration),
public surface (the mount-point tuple, the unknown-mount rejection), docs/spec.

## Evidence Floor

- [ ] E1 `uv run ruff check --no-cache .`; `git diff --check`; entropy hard gate `hard_gate_failing_count 0`; every
  touched or created test file is under 1000 lines, and `.large-file-guard.json` is unchanged.
- [ ] E2 Node-count parity: `pytest --collect-only -q` over the base path before the split equals the collected count
  over the three partitions after the split (± the item-2 delta, stated); the node-id diff lists only the renamed or
  added item-2 tests.
- [ ] E3 Tests, including the selector set for the diff, `tests/test_variant_activation_cutover*.py`,
  `tests/test_select_ci_tests.py`, `tests/test_state_clone_cutover_hook.py`, `tests/test_legacy_reactivation_guard.py` and
  `tests/test_state_clone_index_publish.py`, pass locally and in a node-27 temporary worktree. The ordering and
  abort-on-raise cases still fail when the dispatch order is reversed or the raise is swallowed (mutation evidence),
  and the unknown-mount case fails on the old tuple.
- [ ] E4 `openspec validate retire-station-flag-flip-slot --strict --no-interactive`.
- [ ] E5 The state-clone wiring finding is recorded in the PR, and its issue is filed.
