# Tasks — journal-walk-dirfd-scan (#3138)

- [x] 1.1 `packages/common/safe_fs.py`: add `ScannedEntry` + `scan_directory_no_follow_limited` exactly per design D1
  (returned per-entry outcomes, whole-directory raises, sentinel incl. vanished, final `_verify_fd_matches_path`,
  fd closed on every path, materialized list).
- [x] 1.2 `services/orchestrator/file_journal_paths.py` `_iter_discovered_files.walk`: non-strict walks only (D0) use the
  primitive in place of the listing call and per-entry `stat_no_follow`, mapped per D2; strict walks unchanged.
  Facade import + `__all__` in `file_orchestration_journal.py`, call-time lookup, cluster-map fixture (D3).
- [x] 1.3 Tests: `os.open` count equal for two N on the non-strict path and growing on the strict path (D4); symlink
  entry (suffix match, non-matching name, link to a directory) -> `file_journal_unsafe_scanned_entry` with
  `error_type == "SafeFilesystemError"`; per-entry non-FNF error -> same code/evidence; non-regular suffix match ->
  `not_regular_file`; vanished entry skipped on the non-strict path; limit/sentinel incl. vanished names; sorted
  order; primitive unit tests in `tests/test_safe_fs.py` (symlinked ancestor refused, symlink entry returned as
  link, containment root enforced, FNF vs error outcomes, identity-mismatch raise, fd not leaked).
- [x] 1.4 Seam tests per D3 (non-strict guards wrap the new seam, assert called, pin `.reason`); green:
  `tests/test_safe_fs*.py tests/test_file_journal_*.py tests/test_file_orchestration_journal*.py
  tests/test_gateway_reconcile*.py`.
- [x] 1.5 `scripts/select_ci_tests.py` routing selects the walk/safe_fs suites for both changed files (verify; add
  rows only if missing).
- [x] 2.1 Post-merge (operator): node-22 ff-pull at a pass boundary; next pass receipt vs 2026110200 (qc → provenance
  IFS 12.8 / gfs 15.7 min; convert `dispatch_ms` ~434 s; pass entry → convert start ~25 min; wall 143 min).

  Receipt 2026-11-13T16:40Z: node-22 ff-pull to 2ec644614 at the 14:43:39Z pass boundary; first pass on it,
  cycle 2026110312 (`scheduler_2026111314_c1f355627cbb`), 14:43:39Z-16:34:41Z = 111 min (2026110200: 143 min).
  - qc aggregated -> provenance published: IFS 27 s, gfs 23 s (2026110200: 12.8 min / 15.7 min).
  - pass entry -> convert start 19.7 min (25.8 min); convert dispatch 53-81 s per stage (434 s); first forcing at
    20.6 min (34 min).
  - 0 failed / 0 ambiguous; no failed nhms unit.
