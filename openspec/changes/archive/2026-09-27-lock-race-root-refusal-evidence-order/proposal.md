# Proposal — lock-race-root-refusal-evidence-order (#2540, #2582, #2596, #2563, #2564)

## Why

- **#2540.** Two openers that race to create the same lock file with `openat(dir_fd, name, O_CREAT)` can make the loser fail with `ENOENT` on macOS/APFS, instead of opening the file the winner created. This happens both across threads and across processes. The journal cycle lock folds that error into `FILE_JOURNAL_WRITE_FAILED`, so `tests/test_forcing_submit_ambiguity.py::test_forcing_member_reservation_is_atomic_across_keys_and_leaves_disjoint_work_eligible` always fails locally on macOS. `scheduler_lease._open_regular_guard_file` and `provider_atomic._provider_destination_file_lock` open their locks the same way. With `O_CREAT|O_EXCL` the loser always gets a clean `EEXIST` and never `ENOENT`.
- **#2582.** When `workspace_root` is a symlink loop, `scheduler_lease._open_lock_parent_directory` never returns its structured `UnsafeSchedulerLockError("unsafe_lock_parent_directory")`:
  - on the 3.11 pin, the non-strict `resolve()` raises an errno-less `RuntimeError`;
  - on 3.13+, `mkdir(exist_ok=True)` raises a bare `FileExistsError`.

  As a result, `FileSchedulerLease.acquire` and `open_evidence_directory` abort with an unclassified exception instead of returning their typed refusals.
- **#2596.** In object-store retention, `_sanitize_root_candidate` and the published protected root call `Path.expanduser()` with no guard. A `~nosuchuser/...` value therefore raises an errno-less `RuntimeError`:
  - the `cleanup` CLI prints a traceback and exits 1;
  - in the scheduler pass, a single bad additional root makes the whole object-store sweep end as `status:error` on every pass, including the valid primary root.
- **#2563.** The bounded evidence fallback empties `source_cycles` before the three candidate lists. Since #2402, `source_cycles` holds the only breaker-released projection, which is capped at 64 rows (about 23 KB). The candidate lists are unbounded. The early drop marks the projection `dropped` permanently, and the reader then falls back to `size_fallback_source_cycles_absent`.
- **#2564.** No test runs the heartbeat-refreshed reservation mtime and the evidence retention oldest-first policy on the same evidence root. The claim that "an inversion never degrades into a false `exit 0`" rests on reasoning alone.

## What changes

- **#2540.** A shared `safe_fs` lock-file opener tries `O_CREAT|O_EXCL` first. On `EEXIST` it falls back to a plain open without `O_CREAT`, keeping `O_NOFOLLOW`. If the entry vanishes between the two opens, it retries, up to a small fixed bound. The journal cycle lock, the scheduler lease guard file and the provider destination lock all use it. Regular-file checks, inode-identity checks and error mapping at the callers stay the same.
- **#2582.** Two changes in `_open_lock_parent_directory`:
  - the `workspace_root.resolve()` loop failure (a `RuntimeError`, or `OSError` with `ELOOP`/`ENOTDIR`) maps to `UnsafeSchedulerLockError("unsafe_lock_parent_directory")`;
  - `_ensure_workspace_directory` maps `EEXIST` (an existing non-directory entry, including a loop) to the same error.

  The result is the same on 3.11 and 3.13+. A workspace root that is a valid symlink to a directory is still accepted.
- **#2596.** `_sanitize_root_candidate` catches the expansion `RuntimeError` and reports new typed skip reasons: `primary_root_unexpandable` for the primary root, `extra_root_unexpandable` for an additional root. A bad additional root drops only itself.

  If the published protected root cannot be expanded, the plan records the typed skip `published_root_unexpandable` and plans no deletion at all. It never deletes without its protection root.
- **#2563.** `source_cycles` moves after `skipped_candidates` in `_DROPPABLE_BOUNDED_EVIDENCE_FIELDS`. It stays before `restart_reconcile` and `model_run_failures`. Both shedding tiers read this tuple, so both change.
- **#2564.** Tests only. They cover three orderings: the normal order, the inverted order caused by the heartbeat race, and an orphan under size pressure. In each case the retention policy runs on an evidence root, and the reader's exit code is asserted.

## Out of scope

- flock semantics, lock granularity, and the provider in-process registry, which is kept as is.
- The absolute-path lock opens in `state_manager.py` and `file_orchestration_migration.py`, which the reproduction shows do not trigger the race.
- The `.resolve()` loop failure in retention's `_sanitize_root_candidate` and published root (it is reported, not fixed).
- The three script siblings named in #2596 (`node27_mvt_cache_retention.py`, `node27_raw_retention.py`, `node22_scheduler_evidence_retention.py`), and `copyback_guard.py`.
- The projection cap, a cap on candidate-list rows, and the reader's `dropped` mapping.
- Moving `heartbeat.stop()` (#2564's alternative).
