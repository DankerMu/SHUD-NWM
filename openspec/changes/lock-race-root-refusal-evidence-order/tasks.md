# Tasks — lock-race-root-refusal-evidence-order (#2540, #2582, #2596, #2563, #2564)

## Risk packs

- File IO / path safety / overwrite: **selected**. Lock creation, the lock parent, and the retention deletion surface. Covered by 1.x, 2.x, 3.x.
- Concurrency / shared state / ordering: **selected**. First creation of a lock under concurrency, and the mtime order under retention. Covered by 1.x, 5.x.
- Config / project setup: **selected**. Misconfigured root values (`~nosuchuser`, symlink loop). Covered by 2.x, 3.x.
- Error handling / rollback / partial outputs: **selected**. Typed refusals replace bare exceptions. Covered by 2.x, 3.x.
- Schema / columns / units / field names: **selected (light)**. New skip reason tokens and the evidence shedding order. Covered by 3.x, 4.x.
- Legacy compatibility / examples: **selected**. Symlinked workspace roots, and Linux lock behavior. Covered by 1.3, 2.3.
- Public API / CLI / script entry: **selected (light)**. The `cleanup` CLI no longer tracebacks. Covered by 3.3.
- Auth / permissions / secrets: not selected.
- Resource limits / large input: not selected.
- Release / packaging: not selected.
- Documentation / migration notes: **selected (light)**. Covered by 6.1.

## 1. #2540 lock-file opener

- [x] 1.1 Add `safe_fs.open_lock_file_no_follow(name, *, dir_fd, mode)`:
  - try `O_EXCL` first;
  - on `EEXIST`, fall back to a plain `O_NOFOLLOW` open;
  - retry a bounded number of times on an `ENOENT` from the fallback;
  - let every other `OSError` propagate unchanged.

  Deviation: the opener lives in the new sibling module `packages/common/safe_fs_lock.py`, not in `safe_fs.py`, which is at 990 of the large-file guard's 1000 lines and not excluded.
- [x] 1.2 Use it in the journal cycle lock, the scheduler lease guard file, and the provider destination lock. Keep modes, checks and error mapping. Keep the provider registry, and update its docstring.
- [x] 1.3 Tests. The platform-independent primitive tests are:
  - a concurrent winner (`EEXIST` on create) still yields an fd;
  - an `ENOENT` between the create and the plain open is retried;
  - a symlink raises `ELOOP` and is not followed;
  - a directory raises `EISDIR`;
  - retry exhaustion re-raises.

  Also add a threaded `Barrier` first-creation test that calls `scheduler_lease._open_regular_guard_file` directly. That site has no in-process registry, so on macOS origin/master reproduces the race. `tests/test_forcing_submit_ambiguity.py` must be fully green on macOS in 5 consecutive runs. Existing lease and provider lock tests must stay green.

## 2. #2582 lock parent loop root

- [x] 2.1 Map the `resolve()` loop failure (`RuntimeError`, or `OSError` with `ELOOP`/`ENOTDIR`) and the `mkdir` `EEXIST` to `UnsafeSchedulerLockError("unsafe_lock_parent_directory")`.
- [x] 2.2 Tests on the 3.11 pin, for a two-link loop and a self loop:
  - `_open_lock_parent_directory` raises the typed error;
  - `FileSchedulerLease.acquire` returns `acquired=False` with `reason="unsafe_lock_parent_directory"`;
  - `open_evidence_directory` raises `SchedulerEvidenceWriteError("unsafe_evidence_directory")`.

  Run the same file once with `uv run --python 3.14`.
- [x] 2.3 Pin: a workspace root that is a symlink to a real directory is still accepted.

## 3. #2596 retention unexpandable roots

- [x] 3.1 Add the `reason_unexpandable` parameter and the three tokens. When the published root cannot be expanded, record the typed skip and plan nothing.
- [x] 3.2 Tests. Unexpandable roots:
  - a `~nosuchuser` primary root gives a typed `primary_root_unexpandable` skip, and there is no exception;
  - a `~nosuchuser` additional root gives an `extra_root_unexpandable` skip, and the valid primary root's plan matches a control run key for key;
  - a `~nosuchuser` published root gives `published_root_unexpandable`, zero planned targets, and zero deletions under a non-dry run. When a `~nosuchuser` extra root is configured as well, its `extra_root_unexpandable` entry is still in the receipt;
  - with the extra-roots gate off, a non-dry run, and a `~nosuchuser` copyback root: no exception, and the primary root's deletions proceed.
- [x] 3.3 Tests at the entrypoints:
  - the click and argparse `cleanup` entrypoints exit without a traceback;
  - the scheduler pass retention block shows `status: completed` with the typed skip, not `status:error`. Use a db-free config with a raw `~nosuchuser` primary root, or a copyback root. Never the published root, which config normalizes.
- [x] 3.4 The existing root-admission suites stay green, with their tokens unchanged.

## 4. #2563 drop order

- [x] 4.1 Move `source_cycles` after `skipped_candidates`, and add the constant comment.
- [x] 4.2 Regression test: a bounded product with a non-empty breaker-released projection, and candidate lists much larger than it, under a budget that clearing `candidates` is enough to fit. Assert:
  - `limit.source_cycles.status == "summarized"`;
  - the projection rows survive;
  - `limit.candidate_lists == "dropped"`;
  - `list_operator_actions` reports `size_fallback_source_cycles_summarized` and lists the released models.
- [x] 4.3 The three band tests are unchanged, or any move is explained.

  Unchanged: all three pass at their existing budgets (their fixture's `source_cycles` projects to `[]`).

## 5. #2564 cross tests

- [x] 5.1 The three cases from the design, each setting mtimes with `os.utime`. Cases 2 and 3 assert the reader exits exactly `3`. Case 2 uses the fixed two-file root and also asserts `orphan_reservations == []`.

## 6. Docs

- [x] 6.1 Update `node22-control-plane-manual-recovery.md` under orphan reservations, only if the current text conflicts with the fact that a heartbeat inversion stays fail-closed. Retention skip tokens are not documented anywhere today (`git grep extra_root_not_absolute -- docs` is empty), so no token doc is added.

  Checked: the orphan-reservation text makes no claim the inverted order contradicts, so the runbook is unchanged.

## Evidence Floor

- **Local (macOS).**
  - Behavior-changing tests fail on origin/master source and pass afterwards. Pins are labelled.
  - `uv run pytest -q tests/test_forcing_submit_ambiguity.py` passes 5 times in a row.
  - `uv run ruff check .` passes.
  - `openspec validate lock-race-root-refusal-evidence-order --strict --no-interactive` passes.
  - Also run, and list: the new test files; `tests/test_forcing_submit_ambiguity.py`, `tests/test_file_orchestration_journal.py`, `tests/test_operator_action_reservation_lease.py`, `tests/test_production_scheduler.py`, `tests/test_scheduler_evidence_retention.py`, `tests/test_operator_action_listing.py`, `tests/test_scheduler_evidence_decidability.py`, `tests/test_retention_root_admission.py`, `tests/test_retention_extra_roots.py`, `tests/test_cli_cleanup_frontier.py`, `tests/test_retention_copyback_mutex_protocol.py`, `tests/test_select_ci_tests.py`, `tests/test_resolve_surface_guard.py`, `tests/test_path_canonicalization_family_guard.py`; every provider_atomic / safe_fs / scheduler_lease test file; and every other test file that imports a changed module.
- **Cross-version.** Run the #2582 test file once with `uv run --python 3.14`.
- **node-27** (`TMPDIR=/home/nwm/tmp`): the same suites (the Linux behavior is unchanged).
