# Tasks

Fixture level: **broad-expanded**. Repair intensity: **broad-expanded** (shared
helper root + file IO/path safety + publish/rollback + data loss + production
config, spanning the `runs/`, `forcing/` and `canonical/` lanes).

## Risk pack selection

| Pack | Status | Reason |
|---|---|---|
| file/path IO safety | **selected** | Every changed site is a no-follow directory create, rename, or `rmtree` under a shared NFS root. |
| publish/delete/rollback | **selected** | The defect is in the promote/commit/rollback batch itself. |
| shared helper behavior | **selected** | New `copyback_guard` helper consumed by 6 writers across 3 lanes. |
| concurrency/idempotency | **selected** | The whole change is a cross-process mutex. |
| permissions/auth boundary | **selected** | Cross-uid traversal (node-22 writer, node-27 reader) and the `provider_atomic` `0o022` gate. |
| production config | **selected** | New `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS`; fixed lock path under the copyback root. |
| evidence chain | **selected** | A writer reporting `ok` for a destroyed tree is an evidence-integrity defect. |
| data schema / migration | not selected | No DB schema, payload schema, or migration touched. |
| frontend contract | not selected | No `apps/frontend` or OpenAPI surface changes; display API read path is unchanged code. |
| numerical/scientific | not selected | No GRIB/NetCDF/CRS/unit-conversion logic touched. |
| Slurm scheduling | not selected | No sbatch, gateway, or scheduler-behavior change; node-22 Slurm receipt not required. |

## Implementation

- [x] T1 Add `packages/common/copyback_guard.py`:
      - `copyback_batch_lock(copyback_root, *, timeout_seconds=None)` — exclusive
        `flock` on the fixed `<copyback_root>/.nhms-copyback-batch.lock` (no env
        override), `node27_timeseries_lifecycle_lock`-style no-follow open and
        identity assertions (regular file, not a symlink, one hard link, mode
        `0o600`, effective-uid owner, **copyback-root-owner uid**, path/fd
        `(st_dev, st_ino)` match), bounded
        `LOCK_EX|LOCK_NB` poll loop with a **default 900 s** deadline overridable
        by `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS`, never unlinks the
        lock file. **Never reentrant**: `flock` is per open-file-description, so a
        second acquisition on the same file from the same process blocks itself
        until the deadline. Acquire at batch-owner level only; `copyback_batch_lock`
        must never be called from inside `_copyback_object_tree_with_rollback`
        (`publisher.py`) or any helper it calls, because
        `forcing_copyback_backfill._copy_package` calls that helper
        directly while already holding the lock itself (T3).
        The euid comparison alone only closes the **pre-existing-file** direction:
        a foreign uid that creates the lock file first passes its own euid check
        and, because the file is never unlinked, poisons the mutex permanently.
        So the lock file's owner is additionally compared to the copyback root's
        owner, the **create** branch is refused before `O_CREAT|O_EXCL` runs so no
        orphan is left behind. That create-branch refusal names both uids plus
        the lock path; the pre-existing-file direction does not, for a non-root
        writer — see E18 for what the operator actually sees there. It raises
        `CopybackLockError` (→ each lane's `OBJECT_STORE_COPYBACK_LOCK_UNSAFE`),
        never a timeout.
        The timeout error must be **raised as each lane's own type**
        (`RunTreeCopybackError` in the run-tree lane; `PublishError` with code
        `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` in the q_down/run-products lane) —
        `chain_forecast_execution.py` catches only `RunTreeCopybackError` and
        `publisher.py` only `PublishError | SQLAlchemyError | OSError |
        ValueError`, so a foreign type escapes both. **Ordering: prepare the root →
        run its identity/overlap guards → open the lock → acquire**; a lane that
        returns `skipped` because the copyback root is the object-store root
        (the `same_root` skip in each of `publisher._copyback_canonical_precip`,
        `_copyback_qdown_products` and `_copyback_run_products`, and in
        `run_tree_copyback`) must create no lock
        file at all.
      - `ensure_traversable_copyback_directory(path, *, containment_root=None)` —
        level-by-level create through `safe_fs.ensure_directory_no_follow`,
        `chmod 0o755` **unconditionally** on the levels this call created and on
        no others (the ACL probe of the earlier draft is dead code; see
        `design.md` "Why the widening is unconditional"). Determine "levels this
        call created" by probing upward for missing components **before** creating
        them (the `canonical_precip_copyback_backfill._ensure_target_directory`
        idiom), **not** by walking `relative.parts` against a
        containment root as `state_manager._ensure_copyback_state_parent`
        does — that parts list is empty when the path *is* the root, a silent
        no-op. `containment_root` is optional and passes through to `safe_fs` for
        symlink containment only, so the two root-creating sites
        (`publisher.py`, `run_tree_copyback.py`) work without one. Use
        `os.chmod(..., follow_symlinks=False)`, as `state_manager.py` does.
        Note that `0o755`'s `r-x` group bits make this mask-neutral under an
        inherited ACL; do **not** generalize that to other modes —
        `_ensure_copyback_state_parent`'s `0o775` genuinely restores `mask::rwx`
        and must not be narrowed.
- [x] T2 Hold `copyback_batch_lock` across the whole batch in
      `publisher._copyback_run_products` (a non-batch loop),
      `publisher._copyback_qdown_products` (batch region opened by
      `with self._copyback_batch_mutex(...)`) and
      `publisher._copyback_canonical_precip` (batch region opened by
      `acquire_copyback_batch_lock`) — acquired after the copyback root's identity/overlap
      guards and before planning, released only after commit or rollback returns.
      **Placement relative to each lane's `except Exception`**: in
      `_copyback_qdown_products` the root guards already sit before the batch's own `try:`, so acquire there too — *outside* that try, because its
      handler rewraps any non-`PublishError` as
      `OBJECT_STORE_COPYBACK_FAILED` and would erase the distinct timeout code. In
      `_copyback_canonical_precip` the guards are *inside* its `try:`,
      so the acquire is inside it as well; its `except Exception` handler returns the
      `failed` receipt carrying `error_type`, and `rollback_log` is still empty on
      a timeout so no rollback runs. `_copyback_run_products` has no production
      caller today
      (`publish_cycle` delegates to `publish_qdown_cycle`; only
      `tests/test_tile_publisher.py` calls it, at six sites); lock it anyway so the
      two siblings cannot diverge.
- [x] T3 Hold the same lock in
      `services/tile_publisher/forcing_copyback_backfill._copy_package`
      **per package** — wrap the whole `rollback_log` lifetime, not just the
      `_copyback_object_tree_with_rollback` call: the lock must still be held when
      `_commit_qdown_copyback_batch` or, on the `except` path,
      `_rollback_qdown_copyback_batch` returns. Releasing before them is
      exactly the E4 defect shape. Per package, because `_copy_package` runs in a
      loop that calls it. And in `scripts/canonical_precip_copyback_backfill.py`
      **per mirrored tree** — the cycle loop mirrors one tree per cycle, and the
      grid loop mirrors one per grid id, so both go through a single locked
      mirror helper. Never once for the whole run, which would hold the lock past
      the publisher's deadline. `--dry-run` takes no lock: it provably writes
      nothing, and acquiring would create the lock file, which is a write.
- [x] T4 Hold the same lock in
      `services/orchestrator/run_tree_copyback.copyback_run_trees`,
      and add a comment at `_replace_tree` recording that its guarded
      recovery branch always produced a spurious failure rather than data
      loss. The `STATE_INDEX_OBJECT_KEY` merge runs **outside** the mutex:
      `merge_state_snapshot_index_copyback` takes `provider_destination_lock`
      (`provider_atomic.py`) with `blocking=True` and no deadline, so
      nesting it would make the mutex's own *hold* time unbounded. It is the
      per-file provider-atomic writer the spec delta already exempts; the other
      `extra_object_keys` entries are cheap `_replace_file` copies and stay
      inside.
- [x] T5 Route copyback directory creation through
      `ensure_traversable_copyback_directory` at every call site in `publisher.py`
      and `run_tree_copyback.py`. `publisher.py` and `run_tree_copyback.py` create the
      **copyback root itself**, which is in scope: issue #2035's `umask 027`
      measurement lists `0o750 .` first, and a `0o750` root defeats traversal
      regardless of the levels below it.
- [x] T6 Document `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` and the fixed
      lock path in `infra/env/README.md` and the copyback runbook; assert
      `packages/common/safe_fs.py` and `packages/common/state_manager.py` are
      unmodified by this diff, and that inside `run_tree_copyback.py` — which
      this change *does* modify — the function `_copy_tree_no_symlinks` is
      byte-identical to the merge-base, since its mode-less `mkdir` is the deliberate
      #1631 ACL-mask-preserving site. Restated at symbol level post-ceiling
      (round-5 H3): `a09f7fd0` stripped the `:514-535` range that had scoped
      this to that one function, widening it into a whole-file assertion that
      the file's 285 changed lines (192 added, 93 deleted) make false. Checkable:
      `git diff $(git merge-base master HEAD)..HEAD -- packages/common/safe_fs.py
      packages/common/state_manager.py` is empty, and extracting
      `_copy_tree_no_symlinks` from both revisions of `run_tree_copyback.py`
      yields identical text.

## Verification matrix

| Surface | Command | Expected evidence | Oracle |
|---|---|---|---|
| Lint | `uv run ruff check .` | clean | local |
| Contract | `openspec validate harden-copyback-batch-mutex-and-dir-traversal --strict --no-interactive` | pass | local |
| Backend targeted | `uv run pytest -q tests/test_copyback_guard.py tests/test_tile_publisher.py tests/test_run_tree_copyback.py tests/test_canonical_precip_copyback_backfill.py tests/test_forcing_copyback_backfill.py tests/test_safe_fs.py` | all pass | node-27 |
| #1513 / #1631 no-regression | `uv run pytest -q tests/test_safe_fs.py tests/test_scheduler_file_provider_refresh.py tests/test_state_manager.py -k "umask or provider_lock_parent or acl or copyback_state"` | green at node-27's default umask | node-27 |
| Full backend regression | `uv run pytest -q` | pass | node-27 |
| Cross-uid traversal | AC4 receipt procedure below | `cat` output is non-empty | node-22 write + node-27 read |

On node-27, `export TMPDIR=/home/nwm/tmp` before any pytest run (project rule).

## Test/evidence plan

Every row below names input and expected output. Concurrency tests must be
**deterministic** — reuse the repo's proven idiom at
`tests/test_run_tree_copyback.py`: a `threading.Event` gate injected by
`monkeypatch` so the competing writer runs inside the target window, two threads,
and joins with timeouts. `flock` is per open file description, so two threads that
each open the lock file do contend. The umask idiom is
`tests/test_canonical_precip_copyback_backfill.py` (save/restore). The
per-open-file-description `flock` semantics the thread tests rely on hold on the
local filesystem the suite runs on; on an NFS export that emulates `flock` with
POSIX byte-range locks the granularity is per process instead. That is harmless in
production, where the contending writers are separate processes.
Each new-behavior test must be shown red against pre-change source first, using
the harness that stashes only the changed source files and leaves the tests and
`packages/common/copyback_guard.py` on the tree (that module does not exist on
master and is imported at module level by two test files, so removing it would
make every row fail with `ImportError` and prove nothing).
**Two things this rule demands that a gate-assertion failure does not give.**
First, the rule is unconditional: E5, E6, E13 and E14 go red non-vacuously under
that harness — E6 because `safe_fs.py`'s `os.mkdir(part, 0o755, dir_fd=fd)`
is umask-masked and the assertion is an exact `0o755` compare; E13/E14 because
pre-change no lane acquires, so the `pytest.raises` never fires; E5 because
`run_tree_copyback` had no lock — so each must have its failing assertion
recorded, not just E2/E3/E4. **E7 is the exception and is recorded as such**: at
`umask 002` and `umask 022` the masked `mkdir` already lands `0o755`, so those
two parameters are green pre-change. E7 is a no-regression row, not a
defect-discriminating one, and must not claim a red proof.
Second, E2's and E3's red states below name *terminal* outcomes ("its tree is
absent", "a file the script counted as `copied` is removed"), and those are what
make the defect destructive. A red run that stops at the gate assertion proves
only that the promote window is enterable. Observing the specified terminal state
requires a red run with the gate assertion bypassed; record that run, or amend
these rows to claim only what the gate proves. Reporting "red-proof exists" while
the fixture asks for the terminal state is the one option that is not honest.

### Red-proof record

Local, macOS / Python 3.11.14. Rows E1-E20 run 2026-09-09; the two E22 rows
2026-09-10; E23 and E24 2026-09-10 after the round-4 retro; E25 and E26
2026-09-10 in the post-ceiling fix pass.

Harness: the changed source files are replaced in place with
`git show <rev>:<path> > <path>` and restored with `git checkout HEAD -- <path>`
— **not** `git stash`, because the stash stack is shared across worktrees. The
one exception is E26's "pre-fix test file" measurement, where the *test* file
had to be reverted while a source mutation stayed applied: that used
`git stash push -u -m <unique-tag>` / `git stash apply <sha>` / drop-by-tag,
never a bare `stash`/`pop`.
Tests and `packages/common/copyback_guard.py` stay on the tree throughout.
Baseline is `master` unless a row names another revision.

| Row | Baseline | Recorded failing assertion |
|---|---|---|
| E2 (gate) | master | `assert competitor_ran_inside_window == [False], "the competitor entered the promote window"` → `AssertionError: assert [True] == [False]` |
| E2 (terminal, gate + `summary_a` assertions removed) | master | writer B reports `ok` and then `(copyback_root / key).read_bytes()` → `FileNotFoundError: .../canonical/gfs/2024060112/prcp_rate_or_amount/gfs_2024060112_prcp_rate_or_amount_f003.nc` |
| E3 (gate) | master | `assert script_ran_inside_the_batch == [False], "the script committed inside the publisher's batch"` → `assert [True] == [False]` |
| E3 (terminal, gate bypassed) | master | `script_summary[0]["totals"]["failed"] == 0` passes, then `(copyback_root / key).read_bytes()` → `FileNotFoundError: .../gfs_2026090200_prcp_rate_or_amount_f003.nc` |
| E4 (gate 1) | master | `assert competitor_ran_between_trees == [False], "the competitor committed inside A's batch"` → `assert [True] == [False]` |
| E4 (gate 2, the extended discriminator) | HEAD with the canonical lane's batch acquire replaced by a per-tree acquire around `publisher.py` | `assert competitor_ran_before_rollback == [False], "the competitor committed before A's batch rollback"` → `assert [True] == [False]`; gate 1 reads `[False]` under that placement, which is exactly why gate 2 exists |
| E4 (terminal, both gates bypassed) | same per-tree build | `(copyback_root / key).read_bytes()` → `FileNotFoundError: .../gfs_2024060112_prcp_rate_or_amount_f003.nc` — A's batch rollback deleted B's committed tree |
| E5 | master | `assert competitor_ran_inside_the_region == [False], "a competitor entered the promote region"` → `assert [True] == [False]` (the assertion has since been widened to one sample per promote — see the round-3 row below, which is the form now in the file) |
| E5 (round 3: every promote, not just the first) | `df747077` with the referenced-tree loop (`run_tree_copyback.py`) moved outside `with _run_tree_batch_lock(target_root):` | pre-change test body (single-sample gate): **1 passed** — vacuously green, because the gate fired only on the first promote, which is still inside the mutex, and the two writers' key spaces are disjoint (`runs/`+`forcing/`+`models/` vs `canonical/`) so no terminal assertion notices either. Widened test: `AssertionError: a competitor entered the promote region` / `assert [False, True, True] == [False, False, False]` / `At index 1 diff: True != False`. Production restored with `git checkout -- services/orchestrator/run_tree_copyback.py`; the widened test is green (6.33s) on the unmutated tree. |
| E6 (`umask 027`) | master | `assert landed == {str(path): "0o755" for path in levels}` → `.../shared-object-store/canonical: '0o750' != '0o755'` (and the same for `canonical/gfs`) |
| E7 (`umask 022`, `umask 002`) | master | **green pre-change, by construction** — the masked `mkdir` already lands `0o755` at those umasks. Recorded as a no-regression row with no red proof, not as a discriminator. |
| E13 | master | `with pytest.raises(OrchestratorError)` → `Failed: DID NOT RAISE <class 'services.orchestrator.chain_types.OrchestratorError'>` |
| E14 | master | `with pytest.raises(PublishError)` → `Failed: DID NOT RAISE <class 'services.tile_publisher.publisher.PublishError'>` |
| E17 (state-index merge outside the mutex) | `40cf8ed9` (this branch, pre-fix) | `assert batch_lock_free_during_merge == [True], "the state-index merge ran inside the batch mutex"` → `assert [False] == [True]` |
| E18 (lock-owner poisoning) | `40cf8ed9` | create direction: `assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()` → `assert not True`; root-owner row: `AttributeError: module 'packages.common.copyback_guard' has no attribute 'copyback_root_owner_uid'`; unstattable root: `'copyback root owner is unavailable' not in "cannot acquire copyback batch lock ..."`; deadline: `assert 300.0 == 900.0` |
| E18 (euid-mismatch message names uid + path) | HEAD `b8b4c46b` (pre-fix message) | `assert str(foreign_euid) in message` → `AssertionError: assert '4743' in 'copyback batch lock must be owned by the effective user'` |
| E19 (`..._LOCK_UNSAFE` per lane) | master | four lanes `Failed: DID NOT RAISE`, canonical `assert 'ok' == 'failed'`, forcing `assert 1 == 0` |
| E19 (narrowing variant — the defect the row actually guards) | HEAD with each `except CopybackLockError` arm narrowed to `CopybackLockTimeout` | `packages.common.copyback_guard.CopybackLockError: copyback batch lock must have mode 0600` escapes uncaught out of all four publisher/run-tree entry points, and the forcing report reads `assert 'failed' == 'copyback_lock_unavailable'` |
| E19 (run-products × timeout code) | master | `with pytest.raises(PublishError)` → `Failed: DID NOT RAISE <class 'services.tile_publisher.publisher.PublishError'>` |
| E19 (run-products × traversal widening, `umask 027`) | master | `assert stat.S_IMODE(level.stat().st_mode) == 0o755` on the copyback root → `assert 488 == 493` (`0o750` vs `0o755`) |
| E19 (q_down × held through commit **and** rollback) | master | the probe cannot even open a lock file: `FileNotFoundError: [Errno 2] No such file or directory: '.../shared-object-store/.nhms-copyback-batch.lock'` — pre-change no lane acquires |
| E19 (q_down × held through commit/rollback — the release-early variant, the defect the row actually guards) | HEAD with `_copyback_batch_mutex` calling `release_copyback_batch_lock(fd)` before its `yield` | `assert {'commit': False, 'rollback': False} == {'commit': True, 'rollback': True}` |
| E19 (canonical × held through commit) | master | same absent-lock-file `FileNotFoundError`, surfacing through the lane's own summary as `assert ... and 'failed' == 'ok'` |
| E19 (canonical × held through commit — release-early variant) | HEAD with the canonical lane releasing after its last promote and before `_commit_qdown_copyback_batch` (`publisher.py`) | `assert {'commit': False} == {'commit': True}` |
| E19 (forcing × held through rollback — round-2 A1) | HEAD with the `ExitStack` at `forcing_copyback_backfill.py` replaced by a plain `with copyback_batch_lock(...)` inside the `try:` | `assert {'rollback': False} == {'rollback': True}` |
| E19 (forcing × traversal widening, `umask 027`) | master | `assert {... '.../shared-object-store/forcing': '0o750', ...} == {... '0o755', ...}` |
| E19 (script × unsafe lock, distinct from timeout — round-2 A2) | HEAD with `_mirror_tree_under_batch_lock`'s arm (`scripts/canonical_precip_copyback_backfill.py`) narrowed to `CopybackLockTimeout` | `packages.common.copyback_guard.CopybackLockError: copyback batch lock must have mode 0600` raised at `packages/common/copyback_guard.py` and escaping `backfill.main` uncaught — no JSON summary is printed at all, which is why the row asserts on stdout rather than the exit code |
| E22 (run-tree lane: a lock timeout leaves the stage un-advanced — round-4 claims audit B8) | HEAD with the `_copyback_stage_run_trees` call at `chain_forecast_execution.py` wrapped in `try/except: pass` | `with pytest.raises(OrchestratorError)` → `Failed: DID NOT RAISE <class 'services.orchestrator.chain_types.OrchestratorError'>`; **2 failed** (both parametrisations), canonical test stayed green |
| E22 (canonical lane: a lock timeout still advances the stage — same audit row) | first attempt mutated only `chain_forecast_execution.py` → **3 passed, did not bite**, which is itself the evidence: `_copyback_canonical_precip`'s own `except Exception` (`publisher.py`) swallows first, so the outer net never sees the timeout. Second attempt mutated **both** layers to re-raise | `packages.common.copyback_guard.CopybackLockTimeout: copyback batch lock .../.nhms-copyback-batch.lock was still held after the configured deadline`; **1 failed**, both run-tree parametrisations stayed green |
| E20 (import-closure includes package `__init__`s) | the pre-fix test body from `40cf8ed9`, with `import numpy` planted in `packages/common/__init__.py` | pre-fix test: **1 passed** (vacuous); fixed test: `AssertionError: .../packages/common/__init__.py pulls in a third-party dependency: ['numpy']`. Both the plant and the pre-fix test body were reverted immediately. |
| E23 (a failing lock release must not replace the outcome the caller already computed — round-4 findings E1/E3) | HEAD before the fix, with `fcntl.flock(LOCK_UN)` and `os.close` each injected to raise `OSError` in turn | `OSError: [Errno 5] injected unlock failure` at `release_copyback_batch_lock`'s unlock, and `injected close failure` at its close, escaping both the success path and an in-flight exception; **4 failed** across the two injection points × the two paths. Green after the fix. **Corrected post-ceiling (round-5 G2)**: the original row went on to say the same tests "assert the lock really was released", which the injection could not actually establish — `_fail_the_release`'s `fake_flock` ran the real `LOCK_UN` *before* raising, so the later 1 s `acquire` succeeded whether or not the close backstop existed. The injection no longer unlocks on the `unlock` parametrisation, and an `os.fstat(fd)` → `EBADF` assertion sits immediately after the first release, before any later acquire (fd numbers are reused, so placed later it is vacuous) |
| E24 (script lane: the mirror runs *inside* the mutex, not merely one acquisition per tree — round-4 finding C1) | HEAD with `_mirror_tree_under_batch_lock` mutated to acquire, release immediately, then mirror outside the lock | `assert [False, False, False, False, False, False] == [True] * 6` — **1 failed, 42 passed**: the new test is the only one of the 43 in the file that the mutation reddens, which is the finding restated as a receipt. Mutation reverted, then **43 passed** |
| E25 (run-products lane: the mutex spans every tree it promotes — round-5 finding G1) | HEAD with `_copyback_run_products`'s `with self._copyback_batch_mutex(...)` reduced to an acquire/release pair immediately before the promote loop, so both trees are promoted unlocked | `assert [False, False] == [True, True]` — **1 failed, 164 passed**: the new test is the only one of the 165 in the file that the mutation reddens. The same mutation against the pre-fix file left **164 passed** with nothing red, which is round-5 G1 restated as a receipt. Mutation reverted, then **165 passed** |
| E26 (the close backstop really runs after a failed unlock — round-5 finding G2) | HEAD with `release_copyback_batch_lock`'s two independent `try/except OSError` blocks merged back into one, so an injected unlock `OSError` skips the `os.close` | `Failed: DID NOT RAISE <class 'OSError'>` at the `os.fstat(fd)` → `EBADF` assertion — **1 failed, 40 passed, 3 skipped**, only the `[unlock]` parametrisation. Against the pre-fix test file the identical mutation left **41 passed, 3 skipped** — fully green — because the injection performed the real `LOCK_UN` before raising and made the scenario unreachable. Mutation reverted, then **41 passed, 3 skipped** (44 collected) |

- [x] E1 `copyback_guard` lock unit tests: two threads on one root serialize (the
      second observes the first's completion); two distinct roots do not block each
      other; timeout raises the distinct error and performs no promote; a
      symlinked / `0o644` / foreign-owned lock file fails closed; a killed holder's
      lock is released by the kernel and the next writer proceeds.
- [x] E2 publisher × publisher race: competitor injected in the rename-to-backup → promote
      window. **Red proof (pre-change), as actually observed**: the *competitor*
      (B) completes a whole batch inside the window and reports `ok`, the first
      writer (A) then hits `ENOTEMPTY` and its `_restore_copyback_backup`
      `rmtree`s B's just-promoted tree — so B's `read_bytes()` raises
      `FileNotFoundError` for content it already reported as copied, and A
      reports `failed` rather than `ok`. (The earlier wording of this row put the
      `ok` on the wrong writer.) **Green**: the competitor blocks; both writers
      report truthfully; the destination holds one writer's complete tree.
- [x] E3 publisher × `canonical_precip_copyback_backfill` race, same window.
      **Red**: a file the script counted as `copied` is removed by the publisher's
      rollback. **Green**: serialized; every counted file survives.
- [x] E4 **batch-scope row** (the per-tree-lock discriminator): A promotes `prcp`
      into an empty slot, B commits into `prcp`, A then fails on `grid`. Expected:
      A's batch rollback does not delete B's tree.
      **Scope of the discriminator, stated precisely** — the gate assertion fails
      only for a per-tree lock placed *inside*
      `_replace_directory_tree_for_qdown_batch`, i.e. released when that call
      returns, which is the narrowing `design.md`'s four-step counterexample
      describes. A per-tree lock wrapping the loop-body call site
      (`publisher.py`) instead puts the gate's own wait inside the lock,
      so the gate reads `[False]` and stops discriminating; what would remain is a
      race between A's rollback (in the `except` handler, outside any per-tree
      lock) and B's promote, which is timing-dependent rather than deterministically
      red. Closed by additionally gating A's rollback — `_rollback_qdown_copyback_batch`
      is monkeypatched to wait on the competitor first — so the terminal
      `read_bytes()` assertion is deterministic under either placement.
- [x] E5 `run_tree_copyback._replace_tree` × publisher batch: serialized; loser
      reports failure; winner's tree intact; no `rmtree` of a competitor's tree.
- [x] E6 `umask 027` publisher canonical copyback: assert the copyback root,
      `canonical/`, `canonical/<S>/`, `canonical/<S>/<cycle>/`,
      `canonical/<S>/grid/` each land `0o755` **individually**, including the case
      where this run is what creates the root. `os.umask` is process-global — set and restore in a
      fixture and keep the test off xdist, or run it in a subprocess.
- [x] E7 `umask 002` and `umask 022`: same levels land `0o755`; no group/other
      write bit on any level. No-regression row: green pre-change too (see the
      red-proof record), so it claims no discrimination.
- [x] E8 ACL boundary row: create a level beneath a parent carrying
      `default:user:X:rwx` / `default:mask::rwx` and assert the mask is `r-x`
      **both before and after** the widening — i.e. the caller-side `chmod` changes
      nothing, because `safe_fs`'s `mkdir` already clamped it. Assert separately
      that a mode-less `mkdir` under the same parent yields `mask::rwx`, pinning
      the `run_tree_copyback.py` boundary. Skip with an explicit reason on a
      platform without ACL support — never a silent pass.
- [x] E9 Pre-existing intermediate directory at `0o700` → left unchanged (#1513).
- [x] E10 `provider_lock_parent_unsafe` and the `filesystem-permission-determinism`
      umask regression tests stay green; `state_manager` copyback writers still
      succeed without the mutex.
- [x] E11 Lock timeout inside `_mirror_canonical_precip`: the cycle records a
      `failed` `canonical_precip_mirror` receipt and does not raise.
- [x] E13 Lock timeout in the run-tree lane raises `RunTreeCopybackError`, is
      caught by `_copyback_stage_run_trees` (`chain_forecast_execution.py`),
      records an `object_store_copyback` / `failed` pipeline event, and then
      **propagates as `_chain.OrchestratorError`**. Assert the event and
      the propagated type — not "no exception escapes". Accepted consequence,
      recorded here because it is the one lane where a timeout is not free: the
      call sits at `_after_cycle_stage_terminal` inside the
      `result_status == "succeeded"` branch, so the raise skips
      `update_forecast_cycle_status` and aborts the stage's success path.
      The 900 s default is sized against the measured hold, not against the
      canonical hook's position; the acquisition count and its qualifier live
      only at `DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS`, and the arithmetic is in
      `design.md` "Cost accepted, deliberately".
      A copyback timeout must stay fatal here: `resume_cycle_stage`
      (`chain_stage_execution.py`) re-enters `_after_cycle_stage_terminal`
      unconditionally, so the failure is retried on the next pass, and making it
      non-fatal would mark the cycle succeeded over absent data.
- [x] E14 Lock timeout in the q_down lane raises `PublishError` and it
      **propagates out of `publish_qdown_cycle` still carrying** the
      `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` code — `publisher.py` re-raises
      `PublishError` unchanged; the point of the distinct type is that it is not
      swallowed by the `SQLAlchemyError | OSError | ValueError` arm in `publish_qdown_cycle`
      nor rewrapped as `OBJECT_STORE_COPYBACK_FAILED`.
- [x] E15 Copyback root identical to the object-store root: the lane returns
      `skipped` and **no lock file exists** anywhere under the object-store root.
- [x] E16 `state_manager._ensure_copyback_state_parent` still chmods `0o775` and
      still restores `mask::rwx` under an ACL'd parent — the `0o755` rule of this
      change must not have leaked onto it.
- [x] E17 `copyback_run_trees` runs the `STATE_INDEX_OBJECT_KEY` merge with the
      batch mutex **released** — probed by a non-blocking `flock` from a second
      fd in the same process while the merge runs — and every other
      `extra_object_keys` entry still copies inside it. The unbounded
      `provider_destination_lock` must never nest inside the bounded mutex.
- [x] E18 The lock refuses a foreign uid in **both** directions: an existing lock
      file whose owner is not the copyback root's owner fails closed (separately
      from the euid mismatch), and a foreign uid at a root with no lock file yet
      is refused *before* `O_CREAT` so it leaves no orphan. None of them is a
      `CopybackLockTimeout`. Message content is **not** uniform and the evidence
      says so: the pre-`O_CREAT` refusal names both uids and the lock path, and
      so does the ownership assertion — but a non-root writer meeting an existing
      foreign-owned `0o600` lock file never reaches that assertion (the `O_RDWR`
      reopen fails `EACCES` first) and gets `Permission denied` with the path
      only. The euid-mismatch case is covered by a test that patches
      `os.geteuid`, which is what makes that branch reachable at all.
- [x] E19 **Lane × named-behaviour coverage matrix.** Two review rounds lost the
      same way — an enumeration done from memory missed a cell — so the
      enumeration is now the artifact, not a prose list. Round 1 finding B3 was
      "`LOCK_UNSAFE` × every lane untested" and its fix brief enumerated lanes by
      hand, omitting `scripts/canonical_precip_copyback_backfill.py`; round 2
      finding A2 is exactly that omission. Round 2 finding A1 is a cell nobody
      enumerated at all ("held through rollback" × forcing backfill). The
      invariant this table exists to enforce: **every (lane, named behaviour)
      cell holds either a discriminating `tests/<file>::<test>` reference or an
      explicit `N/A — <structural reason>`.** An empty cell is a defect, and a
      cell whose test would stay green under the behaviour's removal is an empty
      cell.

      **Column legend** (without it "timeout code" reads as false for the two
      backfills, which report rather than raise):

      - **timeout code** — a `CopybackLockTimeout` reaches this lane's caller as
        *this lane's own* documented outcome (error type, error code, or recorded
        failure entry), never as a foreign exception type.
      - **unsafe code, distinct from timeout** — a non-timeout `CopybackLockError`
        (tampered, foreign-owned or wrong-mode lock file) is caught by the same
        arm and is distinguishable at the caller from a timeout. The two publisher
        raising lanes and the run-tree lane carry a distinct *code*; the two
        backfills carry one shared failure bucket by design and distinguish in the
        recorded `reason` — stated per cell, not glossed.
      - **lock held through commit** — the mutex is still held while this lane's
        batch commit runs.
      - **lock held through rollback** — still held while this lane's batch
        rollback runs. This is the E4 defect shape: the `backup_dir is None`
        branch removes whatever now sits at the target.
      - **zero-write skip creates no lock file** — a path that returns without
        writing creates no lock file under the root it skipped.
      - **traversal widening** — every directory level this lane creates, the
        copyback root included, lands `0o755` whatever the process umask.

      | lane | timeout code | unsafe code, distinct from timeout | lock held through commit | lock held through rollback | zero-write skip creates no lock file | traversal widening |
      |---|---|---|---|---|---|---|
      | `publisher` run-products | `tests/test_tile_publisher.py::test_run_products_copyback_lock_timeout_raises_the_distinct_timeout_code` | `tests/test_tile_publisher.py::test_run_products_copyback_unsafe_lock_file_raises_the_distinct_unsafe_code` | N/A — this lane has no batch commit phase: it passes `rollback_log=None` (`publisher.py` → `_copyback_object_tree`), so each promote settles on its own. Span is a separate claim from commit and now has its own test: `tests/test_tile_publisher.py::test_run_products_copyback_holds_the_batch_mutex_across_every_tree_it_promotes` probes at `_replace_directory_tree_no_follow` once per seeded run and asserts `[True, True]`. **Round-5 finding G1**: this cell previously carried no span citation at all and instead asserted inline that `_replace_directory_tree_no_follow` "restores its own backup inline, inside the same `with`" — an untested claim in a cell whose job is to name a test. Span was pinned lane by lane and this one was missed: run-tree in `d4de6cb2`, q_down and canonical in `fe779674`, the canonical backfill script in `a09f7fd0` (round-4 finding C1). | N/A — same reason: no batch rollback exists to hold the lock through; `_replace_directory_tree_no_follow`'s own inline backup restore is lexically inside a call the span test above observes running under the lock. The span test injects no failure, so the restore branch itself is not executed by it — containment is what is measured, not the restore. | `tests/test_tile_publisher.py::test_a_zero_write_skip_path_creates_no_lock_file[run_products]` | `tests/test_tile_publisher.py::test_run_products_copyback_leaves_every_level_it_created_traversable_under_umask_027` |
      | `publisher` q_down | `tests/test_tile_publisher.py::test_qdown_copyback_lock_timeout_propagates_out_of_publish_qdown_cycle` (+ `::test_qdown_copyback_lock_timeout_survives_the_public_publish_entry_points_handler`) | `tests/test_tile_publisher.py::test_qdown_copyback_unsafe_lock_file_raises_the_distinct_unsafe_code` (+ `::test_qdown_copyback_unsafe_lock_survives_the_public_publish_entry_points_handler`) | `tests/test_tile_publisher.py::test_qdown_copyback_holds_the_batch_mutex_through_commit_and_rollback` | `tests/test_tile_publisher.py::test_qdown_copyback_holds_the_batch_mutex_through_commit_and_rollback` (one run drives both: the probing commit fails, so the same call also enters the `except` handler's rollback) | `tests/test_tile_publisher.py::test_a_zero_write_skip_path_creates_no_lock_file[qdown]` | `tests/test_tile_publisher.py::test_qdown_copyback_leaves_every_level_it_created_traversable_under_umask_027` |
      | `publisher` canonical mirror | `tests/test_tile_publisher.py::test_canonical_copyback_lock_timeout_is_reported_through_the_summary` (+ `tests/test_orchestration_chain.py::test_canonical_precip_mirror_lock_timeout_records_a_failed_receipt_and_does_not_raise`) | `tests/test_tile_publisher.py::test_canonical_copyback_unsafe_lock_file_is_reported_through_the_summary` | `tests/test_tile_publisher.py::test_canonical_copyback_holds_the_batch_mutex_through_its_commit` | `tests/test_tile_publisher.py::test_copyback_batch_rollback_never_removes_another_writers_committed_tree` — gate 2 (`competitor_ran_before_rollback`) waits for the competitor *inside* A's rollback, so a build that released before the rollback lets B commit and the terminal `read_bytes()` fails | `tests/test_tile_publisher.py::test_a_zero_write_skip_path_creates_no_lock_file[canonical]` | `tests/test_tile_publisher.py::test_canonical_copyback_leaves_every_level_it_created_traversable` |
      | `run_tree_copyback` | `tests/test_run_tree_copyback.py::test_run_tree_copyback_lock_timeout_raises_this_lanes_own_error_type` (+ `tests/test_orchestration_chain.py::test_run_tree_copyback_lock_timeout_records_a_failed_event_and_propagates`) | `tests/test_run_tree_copyback.py::test_run_tree_copyback_unsafe_lock_file_raises_the_distinct_unsafe_code` | N/A — no batch commit phase: `_replace_tree` promotes and settles one tree at a time. That **every** promote — the run tree plus both referenced trees from the loop that follows it, three in that fixture — is inside the region is pinned by `tests/test_run_tree_copyback.py::test_run_tree_copyback_holds_the_shared_batch_mutex_for_its_whole_promote_region`, which takes one blocking sample per main-thread promote and asserts `[False, False, False]` (round-3 C1/TE-2: the earlier single-sample gate probed only the first promote and stayed green with the referenced-tree loop moved out of the mutex); the one thing deliberately outside is the state-index merge, pinned in both directions by `::test_the_state_index_merge_runs_with_the_batch_mutex_released` and `::test_a_non_state_index_extra_object_still_copies_inside_the_batch_mutex`. | N/A — no batch rollback: `_replace_tree`'s guarded per-tree restore (`run_tree_copyback.py`, commented in place per T4/AC2) runs inside the same `with _run_tree_batch_lock(...)`, and its terminal state is a benign spurious failure rather than a lost update. | `tests/test_run_tree_copyback.py::test_run_tree_copyback_skip_path_creates_no_lock_file` | `tests/test_run_tree_copyback.py::test_run_tree_copyback_widens_every_level_it_creates_under_umask_027` |
      | `forcing_copyback_backfill` | `tests/test_forcing_copyback_backfill.py::test_apply_records_a_lock_timeout_as_its_own_failure_category` | `tests/test_forcing_copyback_backfill.py::test_apply_records_an_unsafe_lock_file_as_the_same_failure_category` — one `copyback_lock_unavailable` bucket **by design** (both verdicts mean "the target tree is intact, rerun once the contender is gone"), so the discriminator is the recorded `reason`: `0600` present and `deadline` absent. That is what a narrowing of `_classify_tree_error` to `CopybackLockTimeout` flips. | `tests/test_forcing_copyback_backfill.py::test_apply_holds_the_batch_mutex_across_the_whole_rollback_log_lifetime` | `tests/test_forcing_copyback_backfill.py::test_apply_holds_the_batch_mutex_while_the_rollback_runs` — round-2 A1's cell; the `ExitStack` at `forcing_copyback_backfill.py` exists only so the lock outlives `_rollback_qdown_copyback_batch`, and nothing asserted it | `tests/test_forcing_copyback_backfill.py::test_cli_rejects_copyback_root_equal_object_store_root_without_already_present` (both `args` params) — the identity refusal is upstream of the only acquire site (`_copy_package`) | `tests/test_forcing_copyback_backfill.py::test_apply_leaves_every_level_it_created_traversable_under_umask_027` |
      | `scripts/canonical_precip_copyback_backfill` | `tests/test_canonical_precip_copyback_backfill.py::test_a_lock_the_backfill_cannot_take_is_a_recorded_failure_not_a_crash` | `tests/test_canonical_precip_copyback_backfill.py::test_an_unsafe_lock_file_is_a_recorded_failure_not_an_escaped_exception` — round-2 A2's cell. Same single per-tree bucket as the forcing lane, distinguished in the recorded `reason`; without the base-class `except CopybackLockError` arm the error escapes `main` before the `print(json.dumps(...))` that is this script's entire report. Not asserted on the exit code: `EXIT_FAILURES = 1` is indistinguishable from the interpreter's uncaught-exception code (pre-existing, out of scope). | N/A — no promote batch at all: `mirror_tree` copies file by file into the target with no cross-tree commit and no rollback of its own (design.md, "Backfill granularity"). Span and granularity are separate claims and now have separate tests: the span — the mirror actually runs *inside* the `with` — is pinned by `::test_the_mutex_is_still_held_while_each_tree_is_mirrored`, which opens a second fd on the lock path from inside `mirror_tree` and asserts `BlockingIOError` for every tree; the granularity — one acquisition per tree, not one per run — stays with `::test_the_backfill_takes_the_batch_mutex_per_tree_not_once_per_run`. **Round-4 finding C1**: this cell previously cited the granularity test for the span, and a release-before-mirror mutation survived all 42 other tests in the file, leaving this lane's `MUST hold the same mutex for its own critical section` with no test at all. The new test is the only one in the file that fails under that mutation. | N/A — same reason: there is no batch rollback here to outlive. This is precisely why per-tree acquisition is sufficient rather than merely convenient. | `tests/test_canonical_precip_copyback_backfill.py::test_dry_run_takes_no_lock_and_creates_no_lock_file`; the other zero-write path (copyback root == source root) is a `resolve_roots` usage refusal upstream of the only acquire site, pinned by `::test_backfill_overlapping_roots_exit_two` | `tests/test_canonical_precip_copyback_backfill.py::test_backfill_created_directories_stay_readable_under_a_restrictive_umask` (+ `::test_backfill_partially_created_directory_chain_stays_readable`) — this lane does **not** route through `ensure_traversable_copyback_directory`: its import closure must stay stdlib-plus-`copyback_guard` for node-22's frozen checkout, so it keeps its own `_ensure_target_directory`, which #2008 already built to the same rule |

      Filled 2026-09-09. No cell needed a production change to become fillable.
      All six `N/A` cells say the same thing — **this lane has no batch
      commit/rollback phase to hold the lock through** — and that is a property
      of the lane's design (per-tree settle in run-products and
      `run_tree_copyback`, file-by-file mirror in the canonical script), not of
      where a `with` statement happens to sit. Where the enclosure *is* merely
      lexical — q_down and the canonical mirror, whose commit and rollback both
      sit inside the locked region — a probe test was added rather than an N/A,
      because "the structure guarantees it and nothing asserts it" is exactly
      what round 2 finding A1 was.
- [x] E20 The import-closure assertion parses the package `__init__.py` files on
      the path to each allowed in-tree module, not just the module itself.
- [x] E21 **Claims audit — the prose counterpart of E19.** Registered corrective
      action of the round-3 Review Failure Retro (`shape: depth`). Rounds 1-3 kept
      returning the same defect in a second guise: a sentence that was true when
      written and false after the code moved, because the fix pass changed the
      sentences its brief named and nothing re-derived the sentences describing
      the same behaviour elsewhere. E19 fixed that for coverage claims by turning
      an enumeration into a matrix; nothing did it for prose. `claims-audit.md`
      is that mechanism: every behaviour-asserting sentence of this change — both
      spec deltas' WHEN/THEN clauses, the behaviour assertions in `design.md` /
      `tasks.md` / `proposal.md`, every operator-visible string, default and
      recovery step in the three docs, and the two new code comments — gets one
      row, and each row records **the falsifier that was constructed and
      checked**, never a happy-path confirmation. Invariant for the artifact:
      *a row whose "falsifier constructed" cell is empty, or says "none" without
      a structural reason, is an unchecked row.* Row counts, dispositions and
      receipts are in `claims-audit.md`, which is a **frozen review record**: it
      is not re-derived here, and it is not extended for later commits.

      **Round 4 judged this mechanism insufficient, and the record says so.**
      Auditing prose by adding prose grew the failing surface: the corrective
      commit added ~480 lines and the next round's verified-finding count went
      5 → 12, including 30 stale `file:line` citations and two spec clauses the
      audit's own declared scope covered but its rows did not. The round-4 retro
      (`.workplans/pr-2201/review/review-failure-retro-round4.md`) therefore
      replaced the mechanism with substrate reduction rather than a fifth
      section: **every `file:line` citation is deleted from `design.md`,
      `tasks.md` and `proposal.md`** — symbol references only, verified by a grep
      that must return nothing rather than by a checker (the checker that ran in
      round 4 missed the bare `:NNN` form it was never tested against) —
      and the acquisition-count claim, previously restated at seven sites, now
      exists at one.
- [x] E22 **The one coverage gap the claims audit exposed** (`claims-audit.md`
      row B8): `design.md` claims a run-tree copyback lock timeout is retried on
      the next scheduler pass while the canonical-mirror lane's is not. The
      mechanism is real and traced — the run-tree failure propagates out of
      `_after_cycle_stage_terminal` before `update_forecast_cycle_status`
      (`chain_forecast_execution.py`) so the stage stays un-advanced,
      while the canonical lane's own `except Exception` swallows into a
      `failed` receipt and the stage advances — but no test asserted the
      contrast; E13 stopped at "propagates as `OrchestratorError`". A
      discriminating test per lane at that seam, with a double-sided red proof
      recorded in the red-proof table. Landed: two tests in
      `tests/test_orchestration_chain.py` — the run-tree one parametrised over
      both stage configurations — with the two-sided proof above. The canonical
      side needed a **two-layer** mutation, and the single-layer attempt that
      stayed green is kept in the record: it is the discovery that
      `publisher.py` swallows first and the orchestrator hook's
      `except Exception` never sees a copyback timeout at all.
- [x] E12 `grep -rn "DEBUG-"` clean before commit; `git stash list` shows no
      leftover `red-proof` entry — this pass created no stash entry at all: the
      red-proof harness swapped source with `git show <rev>:<path>` and restored
      with `git checkout HEAD -- <path>`, because the stash stack is shared
      across worktrees.

## AC4 receipt procedure (cross-uid traversal, must be run, not asserted)

The reader must be node-27's display account (`nwm`) and the writer must be a
different uid (`frd_muziyao` on node-22) running **this branch's code** under
`umask 027`. node-22's active checkout is venv-frozen (no `uv sync`, no bare
`uv run` before the maintenance window), and `copyback_guard` needs only stdlib:

1. node-22: clone the PR branch to a scratch path **outside**
   `/scratch/frd_muziyao/NWM`.
2. node-22, `umask 027`, using the pinned active interpreter and the scratch
   clone on `PYTHONPATH`:
   `/scratch/frd_muziyao/NWM/.venv/bin/python` driving a canonical copyback into a
   scratch root under `/ghdc/data/nwm/` (NFS, so node-27 sees it). No `uv sync`,
   no environment rebuild.
3. node-22: record `stat -c '%a %n'` for each created level.
4. node-27 **as `nwm`**:
   `f=$(find <scratch-root>/canonical -type f | head -1); cat "$f" | wc -c`
   → must print a non-zero byte count. A `stat`/`ls` is explicitly not the
   receipt; the bytes are.

## Evidence Floor (issue #2035 acceptance criteria)

- [x] AC1 Concurrent regression tests exist for both writer combinations
      (publisher × publisher, publisher × backfill script) and neither terminal
      state has a writer reporting `ok`/`copied` for content another writer's
      rollback removed. → E2, E3, E4.
- [x] AC2 `run_tree_copyback.py _replace_tree` is covered by the same
      adjudication: brought under the mutex **and** commented with why its own
      terminal state was benign. → T4, E5.
- [x] AC3 Under `umask 027` a full publisher-side copyback leaves every level it
      created — the copyback root included — traversable, asserted level by level.
      → E6.
- [x] AC4 node-27 oracle: the display API read account actually reads bytes
      through `canonical/**`. → the AC4 receipt procedure above.
- [x] AC5 Fact-check recorded: `getfacl` on the copyback root and each lane.
      **Collected 2026-09-08 on node-27** — root and `canonical/`: no default ACL;
      `runs/`, `forcing/`, `states/`: `default:user:nwm:rwx`, `default:mask::rwx`.
      Recorded in `proposal.md`/`design.md`. It bounds where B bites; it does not
      gate the fix, which is unconditional.
- [x] AC6 No #1513 regression (`provider_lock_parent_unsafe` green at node-27's
      default umask) and no #1631 regression (`run_tree_copyback.py`'s
      `mask::rwx` preservation pinned by E8). → E8, E10, T6.

### node-27 / node-22 oracle receipts (this change's final head, 2026-09-10)

E8, E10, E16, AC1-AC4 and AC6 are checked on measured receipts taken at this
change's final head, not on the local macOS run. The exact SHA is in the PR's
`Agent Review` section and in the evidence bundle
(`.workplans/pr-2201/review/node27-node22-receipts-*.md`), which is where a SHA
belongs — a commit cannot name itself. The load-bearing lines:

| Row | Where | Result |
|---|---|---|
| Backend targeted | node-27 `/home/nwm/tmp/issue2035-wt`, `TMPDIR=/home/nwm/tmp` | **366 passed, 0 skipped**. The same row on macOS is 363 passed / 3 skipped — the three ACL tests that skip there ran here |
| #1513 / #1631 no-regression | node-27, default umask `0002` | **8 passed** (macOS: 7 passed, 1 skipped) |
| E8 | `test_the_0o755_widening_is_mask_neutral_under_an_inherited_default_acl`, `test_a_mode_less_mkdir_under_the_same_parent_keeps_mask_rwx`, `test_a_0o775_widening_still_restores_mask_rwx` | all passed on ext4 — the mask boundary measured, not skipped |
| E16 | `test_ensure_copyback_state_parent_still_chmods_0o775`, `test_ensure_copyback_state_parent_restores_mask_rwx_under_an_acl_parent` | both passed; `state_manager.py` is zero-diff against the merge-base, so this is a no-leak assertion |
| AC6 | `test_run_tree_copyback_widens_every_level_it_creates_under_umask_027` | 1 passed. This is the row that closes the glibc `fchmodat(AT_SYMLINK_NOFOLLOW)` risk the correctness seat raised: macOS never exercises it |
| AC4 | node-22 uid **1103** writes under `umask 027` with this head's `copyback_guard`, using the frozen `/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7, no `uv sync`); node-27 uid **1005** reads | Every level created `0o755`, the copyback root included, lock file `0o600`. The reader `cat`s **37 bytes** across the uid boundary. Control: the pre-fix `0o750` tree on the same mount, same reader → `Permission denied` |

The AC4 control is what makes it a receipt rather than a decoration: the same
account that reads 37 bytes out of the fixed tree cannot traverse the unfixed
one.

## Deferral routing (every out-of-scope finding: an issue or a stated reason)

The pre-merge gate treats an unrouted deferral as a failure, not a silent drop.
Each row below is a finding this PR's review surfaced and deliberately did not
fix.

| Finding | Routing |
|---|---|
| `forcing_copyback_backfill._inspect_existing_target` reads the destination before acquiring, so `already_present` is decided outside the mutex | **#2236**. Verified staleness/mis-report, not lost update: the skip branch writes nothing and the copy branch is fully inside the lock and re-validates after promoting. The spec delta's accepted-limit paragraph cites it |
| `run_tree_copyback._replace_tree`'s `finally` deletes the backup unconditionally, including after the inline restore failed or was skipped | **#2237**. Both destructive sequences reproduced by injection. Structural since `73b9718b` (2026-06-29); this change touched only the docstring and the `ensure_traversable_copyback_directory` call. This change's mutex makes sequence (a) the only reachable path rather than one of two, so it removes an accidental guard without adding a real one |
| `services/orchestrator/retention.py` `rmtree`s trees under the copyback root without the mutex | **#2238**. Not latent: node-22's production env sets `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true` and 294 deletions under the copyback root appear across six passes of the receipt log. Not a regression from this change either — today no writer holds the mutex, so the race already exists — but this change makes the gap structural. `design.md` previously mis-filed this module as a downstream *consumer*; corrected there |
| `scripts/node27_raw_retention.py` `rmtree`s `canonical/**` without the mutex, the same trees the canonical backfill writes while holding it | **#2239**, the sibling of #2238 |
| `infra/env/compute.example` templates `NHMS_CONTAINER_UID=1000` against a root owned by 1103 | Reason, no issue: a template default that every real deployment overrides, unrelated to this change's mutex or traversal surface, and correcting it needs the deployment owner's call on which uid the template should presume |
| `EXIT_FAILURES = 1` in the canonical backfill is indistinguishable from the interpreter's uncaught-exception code | Reason, no issue: pre-existing and already recorded in the E19 matrix cell that depends on it, which is why that row asserts on stdout rather than the exit code |
| `.large-file-guard.json` threshold bypass | Reason, no issue: a repo tooling threshold, not a behaviour of this change's surface; no finding in five rounds turned on it |
| `_run_tree_batch_lock` puts its `yield` inside the `except CopybackLockError` `try`, asymmetric with `publisher._copyback_batch_mutex` | Reason, no issue: unreachable today — nothing in the `with` body raises `CopybackLockError` — and the asymmetry is cosmetic. Recorded rather than filed so a future reader does not read the asymmetry as intent |
| node-27's tracked flake `test_file_journal_post_window_concurrent_public_cycles_submit_one_retry[IFS]` | Already tracked as #1356; not observed in this change's node-27 runs |
| `openspec/specs/multi-source-comparison-ui/spec.md` MD007 list-indent errors | Reason, no issue: pre-existing on lines this branch did not author (it changed one word on line 134), and CI's markdownlint globs `docs/**/*.md` only |

## Non-goals

- Relaxing `provider_atomic`'s `0o022` gate.
- `chmod`-ing an already-existing path in `safe_fs`.
- Changing `run_tree_copyback.py`'s mode-less `mkdir`, or recovering the
  `mask::rwx` that `safe_fs`'s `mkdir` clamps (#1631's open question).
- Bringing `state_manager`'s per-file copyback writers under
  this mutex, or narrowing `_ensure_copyback_state_parent`'s `0o775` to
  the `0o755` this change uses — that `0o775` is what restores `mask::rwx` on the
  state lane.
- Copy-all-then-promote-all batch restructuring.
- Cross-host mutual exclusion (all writers are node-22 local processes; recorded
  as a known limit).
