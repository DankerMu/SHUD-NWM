# split-replay-lineage-tests

Issue: #3210
Fixture level: compact
Risk packs: test-evidence
Evidence floor: E1-E5 below

## Why

`tests/test_replay_lineage.py` is 1015 lines, over the 1000-line large-file guard, which blocks any edit to it.
Its docstring at :423-431 (`test_audit_without_clone_rows_raises_replay_lineage_error`) still says "Under the SUB-4
hook clone rows are written in the same transaction as activation". #3189 (PR #3205) deleted that hook: activation no
longer clones state, and clone rows come from the explicit clone tooling
(`scripts/node22_clone_direct_grid_cutover_states.py`, `scripts/model_succession/`). The edit was deferred from
#3189 because of the guard. #3177 set the precedent: partition, don't exempt.

## What

1. Partition along the existing seam:
   - `tests/test_replay_lineage.py` keeps the pure resolver tests: interval classification/straddle/t*/ambiguity/
     no-history/audit-without-clone/invalid-interval (:141-464) plus naive-datetime and frozen-plan (:973-1015). These
     last two use `_fake_readers_with_cutover`, so they are resolver tests. **Deviation from the issue Approach**, which
     assigned :973-1015 to the lifecycle half; recorded in the PR.
   - New `tests/test_replay_lineage_lifecycle_guard.py` takes the lifecycle-guard half (:466-971):
     `_valid_direct_grid_forcing_block`, `_model_row`, `_legacy_model`, `_direct_grid_model`, `_RecordingCursor`,
     `_FakeTransaction`, `_LifecycleCallRecorder`, `_decision`, `_seed_basin_with_direct_grid_activation_history`, and
     tests (e) `test_pre_cutover_m0_replay_makes_no_lifecycle_call_not_intercepted_by_guard` and (f)
     `test_actual_activate_m0_for_same_basin_still_refused_by_guard`.
   - Only symbols used by BOTH partitions move to the non-collectible support module `tests/replay_lineage_helpers.py`:
     `BASIN_VERSION_ID`, `CUTOVER_VALID_TIME`, `_FakeAuditReader`, `_FakeCloneReader` (the helper imports
     `ReplayLineageAmbiguityError` from `services.orchestrator.replay_lineage` for `_FakeCloneReader`). Single-partition
     symbols stay in their partition (#3177 rule): `BASIN_ID` → lifecycle; `M0_MODEL_ID`, `M1_MODEL_ID`,
     `_fake_readers_with_cutover` → base. Neither partition imports the other.
   - Split the module docstring so each file describes its own tests ((e)/(f) go with the lifecycle-guard file).
2. Rewrite retired-hook narrative, comments/docstrings only (no assertion, decorator or test-body code change):
   - :423-431 docstring: clone rows are written by the explicit clone tooling
     (`scripts/node22_clone_direct_grid_cutover_states.py`, `scripts/model_succession/`), not by activation; an
     audit-recorded activation without clone rows is still refused with `ReplayLineageError`.
   - :433-434 comment "out-of-band delete after the atomic-cutover transaction committed": reword to the same current
     truth (clone rows absent although the audit records an activation).
3. Selector routing (the routes live in `scripts/select_ci_tests_rules.py` / `scripts/select_ci_tests_tables.py`, not
   `scripts/select_ci_tests.py`):
   - New `REPLAY_LINEAGE_TESTS` tuple in tables.py (both partitions, sorted; precedent #3165 `tables.py:1703-1717`).
     Use it at `rules.py:2345` (the `services/orchestrator/**` directory-rule list) and inside
     `MODEL_REGISTRY_IMPORTER_TESTS` (`tables.py:1739`).
   - New `REPLAY_LINEAGE_HELPERS_PATH` constant; a `SUPPORT_MODULE_TEST_RULES` row routing it to both partitions
     (precedent `VARIANT_ACTIVATION_CUTOVER_HELPERS_PATH`, `rules.py:601-606`); a matching entry in
     `SUPPORT_MODULE_ROUTING_ANCHORS` (`tests/test_select_ci_tests.py:13580`).
   - Test expectations: `tests/test_select_ci_tests.py:2112` (exact ordered list; new path directly after
     `tests/test_replay_lineage.py`), `:12546` (`POSITIVE_SELECTION_FLOOR`), `:16723` (subset).
   - Register in the #3177 repartition registries: `REPARTITIONED_SUITES` (:16848), `REPARTITION_ROUTE_SOURCES`
     (:16892) with at least `services/orchestrator/replay_lineage.py`, `packages/common/model_registry.py`,
     `packages/common/model_registry_lifecycle_support.py` and one directory-rule-only orchestrator module (e.g.
     `services/orchestrator/retention.py`), and `REPARTITION_SUPPORT_MODULE_ROUTES` (:16867). Do not add changed-test-file mutual-pull rows
     (`CHANGED_TEST_FILE_RULES`): the registries do not require them and the spec covers production paths only.
   - `scripts/ci/full_test_durations.json`: split the 0.198 s entry across both partitions.
   - `pyproject.toml` per-file ruff ignores only if a moved block needs one.

## Must preserve

- Collection identity: the sorted `::test_name` suffix set before and after is byte-identical (14 cases in total today;
  re-count at implementation).
- `services/orchestrator/replay_lineage.py` and `packages/common/model_registry.py` are untouched.

## Out of scope

Any behavior change; other oversized suites.

## Evidence Floor

- [x] E1 Collection identity: `uv run pytest --collect-only -q tests/test_replay_lineage.py` on master vs
  `tests/test_replay_lineage.py tests/test_replay_lineage_lifecycle_guard.py` on the branch; the sorted suffixes after
  `::` diff to empty (14 cases).
- [x] E2 After `git add` of the new files (the selector guards derive from `git ls-files` and silently pass on untracked
  files): `uv run pytest -q tests/test_replay_lineage.py tests/test_replay_lineage_lifecycle_guard.py tests/test_select_ci_tests.py tests/test_ci_shard_tests.py`
  green; `uv run ruff check --no-cache .`; no new format drift: `uv run ruff format --diff` on the moved code matches
  master's output (the repo has no format gate and the master file already fails `ruff format --check`; reformatting
  would break byte-equivalence of moved code).
- [x] E3 `wc -l tests/test_replay_lineage*.py tests/replay_lineage_helpers.py` all ≤ 1000; `.large-file-guard.json`
  has no replay-lineage entry; `git grep -n -e "SUB-4 hook" -e "atomic-cutover transaction committed" tests/` empty.
- [x] E4 For each of `services/orchestrator/replay_lineage.py`, `packages/common/model_registry.py`,
  `packages/common/model_registry_lifecycle_support.py`, `tests/replay_lineage_helpers.py`:
  `printf '%s\n' <path> > "$TMPDIR/c" && uv run python scripts/select_ci_tests.py --changed-file "$TMPDIR/c"` lists both
  partitions; for the helper the output is exactly the two partitions plus `tests/test_select_ci_tests.py`.
- [x] E5 `openspec validate split-replay-lineage-tests --strict --no-interactive`. No node-27 run needed: the moved
  tests use in-memory fakes only (no real DB); recorded in the PR.
