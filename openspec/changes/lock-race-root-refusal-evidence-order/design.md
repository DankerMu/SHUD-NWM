# Design — lock-race-root-refusal-evidence-order

Fixture level: **standard**. Five small, independent defects in the scheduler's filesystem and evidence layer. Each fix is fail-closed. The only intentional behavior change on Linux is the new typed refusals.

## #2540 shared lock-file opener

- **New primitive.** `packages/common/safe_fs_lock.py`, a sibling module of `safe_fs.py` (which is at the large-file guard's limit):

  ```
  open_lock_file_no_follow(name: str, *, dir_fd: int, mode: int) -> int
  ```

  - Try `os.open(name, O_RDWR|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC, mode, dir_fd=dir_fd)`.
  - On `FileExistsError`, run `os.open(name, O_RDWR|O_NOFOLLOW|O_CLOEXEC, dir_fd=dir_fd)`.
  - If that plain open raises `FileNotFoundError` (the entry was unlinked in between), start over. Stop after a small fixed number of attempts (3), then re-raise the last error.
  - Every other `OSError` propagates **unchanged**, as a raw `OSError`, not `SafeFilesystemError`, so each caller keeps its own errno mapping (`scheduler_lease.py` ~694-697). Precedent: `safe_fs.py` ~250 already lets a raw `FileExistsError` through.
- **Why this is safe.**
  - A symlink at `name` makes `O_EXCL` fail with `EEXIST`. The plain open then fails with `ELOOP` because of `O_NOFOLLOW`.
  - A directory at `name` makes the plain open fail with `EISDIR`.
  - Both are errnos the callers already map. The callers keep their `fstat`/`S_ISREG` check and, in provider, the inode-identity check.
- **Call sites.**
  1. `file_orchestration_journal._cycle_file_lock_unlocked`: mode `0o666`.
  2. `scheduler_lease._open_regular_guard_file`: mode `0o644`. The `{EEXIST, EISDIR, ELOOP, ENOTDIR}` mapping stays.
  3. `provider_atomic._provider_destination_file_lock`: mode `0o600`, and the `fchmod` stays. The `_process_destination_lock` registry also stays: it keeps the non-blocking contender semantics for callers in the same process. Its docstring is updated to say the open race is now closed in the primitive.

  The PR records the resolution for each site. This covers the #2540 acceptance criterion for sites 2 and 3.
- **Existing precedent.** `_FILE_FLAGS` in `safe_fs` already uses `O_CREAT|O_EXCL`.

## #2582 lock parent loop root

- In `_open_lock_parent_directory` (`scheduler_lease.py` ~635), wrap `workspace_root = workspace_root.resolve()` as follows:
  - `RuntimeError` (the 3.11 non-strict loop failure) raises `UnsafeSchedulerLockError("unsafe_lock_parent_directory")`;
  - `OSError` with `errno in {ELOOP, ENOTDIR}` raises the same error;
  - any other `OSError` propagates as today.
  - The `OSError{ELOOP, ENOTDIR}` arm is defensive and untested. The non-strict form never raises it on a supported interpreter: 3.11 turns `ELOOP` into `RuntimeError`, and 3.13+ `realpath` does not raise. Mark it with a comment.
- `_ensure_workspace_directory` adds `EEXIST` to the mapped errnos. After resolution, `mkdir(exist_ok=True)` raises `FileExistsError` only when an entry exists that `is_dir()` rejects: a regular file, or a loop in the last component. (A dangling symlink root is followed by `resolve()`, and `mkdir` then creates its target, so it is accepted as today.) That entry is exactly the non-directory the function already refuses a few lines later.
- **Rejected alternative.** An `lstat` symlink refusal before `resolve()`, as #2582 recommends. It buys nothing over the two-line mapping:
  - Production callers already pass a root canonicalized with `realpath` (`scheduler_config/config.py` ~288).
  - A pre-resolve refusal would only change direct callers that pass a symlink to a real directory. Today those are accepted (pinned by 2.3).
  - Loops still reach this function in production, because non-strict `realpath` folds them.
- **ADR 0009 note.** The call stays non-strict. The new `RuntimeError` arm does not make the interpreters disagree. On 3.13+ the folded loop ends at the `EEXIST` mapping, and on 3.11 it ends at the `RuntimeError` arm, so both reach the same typed refusal. A loop in a parent component ends at the existing `ELOOP` mapping of `mkdir` on 3.13+. No ADR 0009 marker is needed:
  - the family guard (`tests/test_path_canonicalization_family_guard.py`) only inspects `os.path.realpath`;
  - the resolve-surface guard (`tests/test_resolve_surface_guard.py`) only inspects the strict form;
  - this site-by-site adjudication of a non-strict call is what `safe-filesystem-primitive-contract` requires.

  The PR notes the correction from #2582: the claim "3.13+ reaches the structured channel" was wrong.

## #2596 retention unexpandable roots

- `_sanitize_root_candidate` gains a keyword `reason_unexpandable: str`. `Path(value).expanduser()` is wrapped in `except RuntimeError`, which returns `(str(value), None, reason_unexpandable)`.
- New tokens, defined next to the existing reason constants:
  - `PRIMARY_ROOT_UNEXPANDABLE_REASON = "primary_root_unexpandable"`
  - `EXTRA_ROOT_UNEXPANDABLE_REASON = "extra_root_unexpandable"`
  - `PUBLISHED_ROOT_UNEXPANDABLE_REASON = "published_root_unexpandable"`
- **Callers.**
  - The primary root caller passes the primary token.
  - `_resolve_runs_only_roots` passes the extra token. The existing skip-entry shape is recorded, and only that root is dropped.
  - `_resolve_copyback_lock_root` passes the extra token. It does not record skips, as today.
- **Published protected root** (`plan_retention` ~783). When expansion raises `RuntimeError`:
  - append `{"key": "", "root": <raw>, "reason": "published_root_unexpandable"}` to `result.skipped`;
  - in that early-return branch only, extend `result.skipped` with `extra_root_skipped`;
  - return the result without collecting any target on any root.

  The normal path keeps the extend where it is today, at ~799, so the order of skip entries in the normal receipt is unchanged.

  With no planned targets, `run_retention` deletes nothing, so there is never a deletion without the protection root. Both the primary and the extra skip entries stay in the receipt.
- **Visible outcomes.**
  - Both `cleanup` CLI entrypoints print the structured result and **exit 0**. This matches how today's `primary_root_not_absolute` rejection surfaces. There is no traceback. The PR records this as a deviation from the issue's "exit 2" wording.
  - The scheduler pass retention block reports `status: completed` with the typed skips, instead of `status:error`. The valid primary root is still swept.
- **Where the scheduler lane can hit this.** In db-free mode, `config.py` ~315-331 normalizes the published root at config time to `<workspace>/~nosuchuser/…`, so it never raises in retention. The scheduler-pass test must therefore use the raw primary root (`_object_store_root_raw`) or the copyback root.
- **Default-off copyback path.** `retention.py` ~926 calls `_resolve_copyback_lock_root` on every non-dry run, whatever the extra-roots gate says (it is off by default, ~191). So the `~nosuchuser` copyback value is reachable in the default configuration, contrary to the issue's "unreachable" note, and needs its own test.

## #2563 drop order

- The new tuple is:

  ```
  ("model_discovery", "candidates", "blocked_candidates", "skipped_candidates", "source_cycles", "restart_reconcile", "model_run_failures")
  ```

- Add a comment by the constant: since #2402 it holds the only breaker-released evidence, so it must be shed after the candidate details.
- Update the comment at the pop tier (`scheduler_evidence_payload.py` ~345) if its claim no longer holds.
- The three band tests in `tests/test_production_scheduler.py` are expected not to move. Their fixture `_incident_scheduler_evidence_payload` (~36351, derived from `_large_scheduler_evidence_payload`) has `source_cycles` without selection fields, so it projects to `[]`. The pop-tier comment (`scheduler_evidence_payload.py` ~371-373) stays true after the reorder. If any band moves, the implementer investigates and records why.

## #2564 heartbeat / retention cross tests

- New file `tests/test_scheduler_evidence_retention_reservation_mtime.py`.
- All mtimes are set with `os.utime`: no `sleep`, no patched clock.
- The file runs `scripts/node22_scheduler_evidence_retention.run_retention` and `operator_action_listing.list_operator_actions` on the same root.
- Three cases:
  1. **Normal order.** Both files are older than the safety window, and the reservation's mtime is just before its terminal file's. The byte budget allows exactly one deletion. The only size-pass deletion is the `.pre_execution.json`.
  2. **Inverted order.** The root is fixed to exactly two files:
     - `P.json`: evaluating and scope-complete, carrying at least one operator action;
     - `P.pre_execution.json`: mtime is `P.json`'s + 1 s, `lease.ttl_seconds=60`.

     There is no newer evaluating pass, and the budget forces one deletion. Assert:
     - the only size deletion is `P.json`;
     - the listing exits exactly `3`;
     - `orphan_reservations == []`, because the trigger is "no evaluating, scope-complete pass remains" (`operator_action_listing.py` ~460), not the orphan rule (~491 returns `[]` when no evaluating pass remains).

     A newer clean evaluating pass would filter the reservation (~518) and correctly answer `0`. That is not a defect, and this case does not build it.
  3. **Orphan under size pressure.** A crashed pass's reservation, with no terminal file, sits next to an older evaluating terminal file. Oldest-first deletes the older terminal file. The listing exits exactly `3`.
- If a case shows a consequence that is not fail-closed, stop and report it. Do not move `heartbeat.stop()`: that is out of scope.

## Must-preserve

- Linux lock acquisition is behaviorally identical: same modes, same refusals for a symlink, a directory, or a non-regular file, same flock semantics.
- Root admission for valid, blank and relative roots is byte-identical: same tokens, same skip shape.
- A valid symlinked workspace root is still accepted.
- The bounded evidence markers keep their semantics: `dropped` is never downgraded.
- The reader's exit-code contract is unchanged.
