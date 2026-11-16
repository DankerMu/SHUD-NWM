# safe-fs-split-retire-durable-absence (#3155, PR 1 of 2)

This PR refers to #3155 and does not close it; PR 2 (the reconcile split) closes it.

## Why
`packages/common/safe_fs.py` is 1072 lines and is not excluded from the large-file guard
(`.large-file-guard.json`, `maxLines: 1000`). The guard counts staged line totals, so even a deletion-only commit to the
file is blocked. That is why #3148 had to descope retiring the test-only helper `prove_named_entry_absent_durable`
(`safe_fs.py:298-322`): it has no production caller, and only `tests/test_safe_fs.py:42,473-506` exercises it.

#3155 covers this file and `services/orchestrator/reconcile.py`. The reconcile split is a separate PR, because it needs
about 60 monkeypatch retargets and an AST-scanning test rewrite. This PR does `safe_fs` alone.

## What
1. **Retire** `prove_named_entry_absent_durable` and its two tests
   (`test_durable_absence_proof_fsyncs_and_rechecks_parent`,
   `test_durable_absence_proof_parent_recheck_failure_is_indeterminate`) and the import at `tests/test_safe_fs.py:42`.
   `_fsync_and_verify_parent` and `_indeterminate_proof_error` stay, because `read_bytes_durable_no_follow` still
   calls them.
2. **Pure move** of the error type and the path-expansion helpers into a new leaf
   `packages/common/safe_fs_paths.py`:
   - `SafeFilesystemError` (`:11-36`)
   - `_anchor_for`, `_expand_path`, `_absolute_parts`, `_relative_parts_under_root` (`:992-1055`)

   The leaf imports nothing from `safe_fs`. `safe_fs` imports these names back with redundant-alias imports
   (`from packages.common.safe_fs_paths import SafeFilesystemError as SafeFilesystemError`, and so on), so every
   existing `safe_fs.X` access, `from packages.common.safe_fs import X` import, and string patch target still
   resolves. The import side includes `safe_fs_publication.py:15-24`, which from-imports `_expand_path`.
3. **Patch targets stay put.** Every name that is patched as a `safe_fs` module attribute stays in `safe_fs.py`,
   together with all of its in-module callers:
   - `_verify_fd_matches_path`, `_open_parent_dir`, `_open_child_dir`
   - `ensure_directory_no_follow`, `atomic_write_bytes_no_follow`

   None of the moved names is patched anywhere (explorer audit, AST scan of `tests/`, `services/`, `packages/`,
   `scripts/`, `workers/`, `apps/`). The implementer re-verifies this before moving (task 1.1).
4. **Consumers keyed on the file path:**
   - `tests/test_node27_timeseries_compression_wrappers.py:22,109` copies `safe_fs.py` into a fake `packages/common`;
     it must also copy `safe_fs_paths.py`.
   - Selector: `safe_fs_paths.py` gets its own literal rule beside the `safe_fs.py` route at
     `scripts/select_ci_tests_rules.py:1197-1204`:
     `PathTestRule("packages/common/safe_fs_paths.py", (*FILE_JOURNAL_READ_STATE_TESTS, "tests/test_safe_fs.py",
     *C4_PRODUCTION_ACCEPTANCE_TESTS), stop_on_match=True)`.
     This follows the `safe_fs_publication.py` / `safe_fs_lock.py` precedent (`:1208-1233`).
     `FILE_JOURNAL_READ_STATE_PATH_PATTERNS` is **not** touched, because rules and the `ci-contract-baseline` spec index
     it positionally (`[0]..[11]`). `tests/test_select_ci_tests.py` gets one assertion, and the C4 producers loop
     (`:23618-23629`) gains the new path.
   - Line references: `scripts/node27_timeseries_compression_prearm.py:436` cites `packages/common/safe_fs.py:10` for
     `SafeFilesystemError`, which becomes wrong after the move. It is rewritten as the symbol reference
     `packages.common.safe_fs_paths.SafeFilesystemError`. The `line_reference_baseline.json:93` key is not replaced
     with a new line key: the checker keys on text, so a new `file:line` key would fail `hard-line-reference`, while
     removing a reference never fails it. `scripts/scheduler_state_index_copyback_replay.py:95` (`safe_fs.py:138`) was
     already stale before this change and is out of scope; it is reported only.
   - Not applicable: `facade_guard` governs only `scheduler.py` and `chain.py`
     (`scripts/governance/entropy_audit/schema.py:208-214`), and `pyproject.toml` has no per-file ignore keyed on
     `safe_fs.py`.

Result: `safe_fs.py` is about 958 lines and `safe_fs_paths.py` about 110. Neither gets a guard exclusion.

**Non-goals:**
- No logic edits. `_expand_path`'s docstring says "every public entry point of this module". After the byte-identical
  move, "this module" means the leaf; that is recorded as a deviation and not edited.
- `SafeFilesystemError.__module__` becomes `packages.common.safe_fs_paths`. A grep found no code that compares module
  names or pickles the type. The class object stays the same under both import paths.

## Must preserve
- Every public and private name reachable as `packages.common.safe_fs.X` on `origin/master` stays reachable, except
  the retired helper.
- `SafeFilesystemError` is a single class object, so `safe_fs.SafeFilesystemError is safe_fs_paths.SafeFilesystemError`.
- No-follow, containment and durability semantics.
- Every existing monkeypatch on `safe_fs` still governs the code it governed before.

## Fixture level
standard. Risk packs: refactor-move, test-only-retire.

## Evidence Floor
- [ ] E1 `uv run ruff check --no-cache .`
- [ ] E2 AST move proof against `origin/master`. The proof script, its output and the command are written to a
  file and the result goes in the PR body.
  - Every top-level def, class and assignment of the old `safe_fs.py` appears with an identical AST dump in exactly one
    of `safe_fs.py` and `safe_fs_paths.py`.
  - The only allowed differences are the retired `prove_named_entry_absent_durable` and the import lines.
  - The set of names reachable as `safe_fs.X` equals the old set minus the retired one.
- [ ] E3 Tests, all green locally:
  - `select_tests` over the union of `packages/common/safe_fs.py`, `packages/common/safe_fs_paths.py` and
    `packages/common/safe_fs_publication.py`, plus the selector set for the diff;
  - `tests/test_safe_fs.py`, `tests/test_safe_fs_lock.py` and `tests/test_select_ci_tests.py`;
  - `tests/test_node27_timeseries_compression_wrappers.py` explicitly. It copies `safe_fs.py` by path into a fake
    package and is not on the `safe_fs.py` route, so missing task 1.4 would not show red on the PR lane.

  New files are added with `git add -N` before the local selector run. The new selector assertion is red before the
  rule edit.
- [ ] E4 The same set passes in a node-27 temporary worktree.
- [ ] E5 `wc -l` shows both files at or under 1000 lines, `.large-file-guard.json` is unchanged, and the entropy hard
  gate has `hard_gate_failing_count 0`.
- [ ] E6 Outside `openspec/changes/archive/`, a grep for `prove_named_entry_absent_durable` has zero hits in code, tests and
  `openspec/specs`.
- [ ] E7 `openspec validate safe-fs-split-retire-durable-absence --strict --no-interactive`.
