# Tasks — safe-fs-split-retire-durable-absence (#3155, PR 1 of 2)

- [x] 1.1 Re-verify the patch audit. Search `tests/`, `services/`, `packages/`, `scripts/`, `workers/` and `apps/` for
  any patch, reassignment or string target of `SafeFilesystemError`, `_anchor_for`, `_expand_path`, `_absolute_parts`,
  `_relative_parts_under_root`, or `safe_fs.Path` / `safe_fs.os` used to steer those helpers. Do not move a helper
  that is patched; report it instead.
- [x] 1.2 Retire `prove_named_entry_absent_durable` and its two tests in `tests/test_safe_fs.py`, plus the import.
- [x] 1.3 Create `packages/common/safe_fs_paths.py` with the moved names, byte-identical bodies and the imports they
  need. Re-import them in `safe_fs.py` with redundant-alias imports. The leaf does not import `safe_fs`.
- [x] 1.4 Update `tests/test_node27_timeseries_compression_wrappers.py` so its fake package also contains
  `safe_fs_paths.py`.
- [x] 1.5 Selector: add the standalone literal `PathTestRule` for `packages/common/safe_fs_paths.py` beside
  `scripts/select_ci_tests_rules.py:1197-1204`, per proposal What 4. Do **not** edit `FILE_JOURNAL_READ_STATE_PATH_PATTERNS`.
  Add one assertion in `tests/test_select_ci_tests.py` (red before the rule edit), and add the path to the C4 producers
  loop (`:23618-23629`). Update any frozen list that the new route changes.
- [x] 1.6 Add one identity test in `tests/test_safe_fs.py`: `safe_fs.SafeFilesystemError is safe_fs_paths.SafeFilesystemError`, and an error raised
  by a moved helper is caught by `except safe_fs.SafeFilesystemError` and keeps its `kind`.
- [x] 1.7 Rewrite `scripts/node27_timeseries_compression_prearm.py:436` from `packages/common/safe_fs.py:10` to the symbol
  reference `packages.common.safe_fs_paths.SafeFilesystemError`. Add no new `file:line` reference and no baseline key.
- [x] 1.8 Write the AST move-proof script to the scratchpad (not the repo) and record its output for E2.
