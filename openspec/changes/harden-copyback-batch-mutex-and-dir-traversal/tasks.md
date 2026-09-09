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

- [ ] T1 Add `packages/common/copyback_guard.py`:
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
        (`publisher.py:1593`) or any helper it calls, because
        `forcing_copyback_backfill._copy_package` (`:744`) calls that helper
        directly while already holding the lock itself (T3).
        The euid comparison alone only closes the **pre-existing-file** direction:
        a foreign uid that creates the lock file first passes its own euid check
        and, because the file is never unlinked, poisons the mutex permanently.
        So the lock file's owner is additionally compared to the copyback root's
        owner, the **create** branch is refused before `O_CREAT|O_EXCL` runs so no
        orphan is left behind, and every such refusal names both uids plus the
        lock path. It raises `CopybackLockError` (→ each lane's
        `OBJECT_STORE_COPYBACK_LOCK_UNSAFE`), never a timeout.
        The timeout error must be **raised as each lane's own type**
        (`RunTreeCopybackError` in the run-tree lane; `PublishError` with code
        `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` in the q_down/run-products lane) —
        `chain_forecast_execution.py:953` catches only `RunTreeCopybackError` and
        `publisher.py:199-202` only `PublishError | SQLAlchemyError | OSError |
        ValueError`, so a foreign type escapes both. **Ordering: prepare the root →
        run its identity/overlap guards → open the lock → acquire**; a lane that
        returns `skipped` because the copyback root is the object-store root
        (canonical `publisher.py:1227-1228`, q_down `:933-948`, run-products
        `:786-792`, run-tree `run_tree_copyback.py:56-61`) must create no lock
        file at all.
      - `ensure_traversable_copyback_directory(path, *, containment_root=None)` —
        level-by-level create through `safe_fs.ensure_directory_no_follow`,
        `chmod 0o755` **unconditionally** on the levels this call created and on
        no others (the ACL probe of the earlier draft is dead code; see
        `design.md` "Why the widening is unconditional"). Determine "levels this
        call created" by probing upward for missing components **before** creating
        them (the `canonical_precip_copyback_backfill._ensure_target_directory`
        idiom, `:189-225`), **not** by walking `relative.parts` against a
        containment root as `state_manager._ensure_copyback_state_parent:2472`
        does — that parts list is empty when the path *is* the root, a silent
        no-op. `containment_root` is optional and passes through to `safe_fs` for
        symlink containment only, so the two root-creating sites
        (`publisher.py:1403`, `run_tree_copyback.py:49`) work without one. Use
        `os.chmod(..., follow_symlinks=False)`, as `state_manager.py:2481` does.
        Note that `0o755`'s `r-x` group bits make this mask-neutral under an
        inherited ACL; do **not** generalize that to other modes —
        `_ensure_copyback_state_parent`'s `0o775` genuinely restores `mask::rwx`
        and must not be narrowed.
- [ ] T2 Hold `copyback_batch_lock` across the whole batch in
      `publisher._copyback_run_products` (def `publisher.py:721`; non-batch loop
      at `:823`), `publisher._copyback_qdown_products` (def `:873`; batch region
      `:982-1133`) and `publisher._copyback_canonical_precip` (def `:1157`; batch
      region `:1209-1335`) — acquired after the copyback root's identity/overlap
      guards and before planning, released only after commit or rollback returns.
      **Placement relative to each lane's `except Exception`**: in
      `_copyback_qdown_products` the root guards already sit before the batch
      `try:` at `:983`, so acquire there too — *outside* that try, because its
      handler (`:1093`) rewraps any non-`PublishError` as
      `OBJECT_STORE_COPYBACK_FAILED` and would erase the distinct timeout code. In
      `_copyback_canonical_precip` the guards are *inside* the `try:` at `:1209`,
      so the acquire is inside it as well; its handler (`:1301`) returns the
      `failed` receipt carrying `error_type`, and `rollback_log` is still empty on
      a timeout so no rollback runs. `_copyback_run_products` has no production
      caller today
      (`publish_cycle:181` delegates to `publish_qdown_cycle`; only
      `tests/test_tile_publisher.py:1192`/`:1264` call it); lock it anyway so the
      two siblings cannot diverge.
- [ ] T3 Hold the same lock in
      `services/tile_publisher/forcing_copyback_backfill._copy_package` (def
      `:734`) **per package** — wrap the whole `rollback_log` lifetime `:742-772`,
      not just the `:744` copyback call: the lock must still be held when
      `_commit_qdown_copyback_batch` (`:755`) or, on the `except` path,
      `_rollback_qdown_copyback_batch` (`:758`) returns. Releasing at `:754` is
      exactly the E4 defect shape. Per package, because `_copy_package` runs in a
      loop at `:649`. And in `scripts/canonical_precip_copyback_backfill.py`
      **per mirrored tree** — the cycle loop mirrors one tree per cycle, and the
      grid loop mirrors one per grid id, so both go through a single locked
      mirror helper. Never once for the whole run, which would hold the lock past
      the publisher's deadline. `--dry-run` takes no lock: it provably writes
      nothing, and acquiring would create the lock file, which is a write.
- [ ] T4 Hold the same lock in
      `services/orchestrator/run_tree_copyback.copyback_run_trees` (def `:36`),
      and add a comment at `_replace_tree` (`:374-392`) recording that its guarded
      recovery branch (`:388`) always produced a spurious failure rather than data
      loss. The `STATE_INDEX_OBJECT_KEY` merge runs **outside** the mutex:
      `merge_state_snapshot_index_copyback` takes `provider_destination_lock`
      (`provider_atomic.py:219-222`) with `blocking=True` and no deadline, so
      nesting it would make the mutex's own *hold* time unbounded. It is the
      per-file provider-atomic writer the spec delta already exempts; the other
      `extra_object_keys` entries are cheap `_replace_file` copies and stay
      inside.
- [ ] T5 Route copyback directory creation through
      `ensure_traversable_copyback_directory` at `publisher.py:1403`, `:1625`,
      `:1630`, `:1633`, `:2305`, `:2371` and `run_tree_copyback.py:49`, `:375`,
      `:396`. `publisher.py:1403` and `run_tree_copyback.py:49` create the
      **copyback root itself**, which is in scope: issue #2035's `umask 027`
      measurement lists `0o750 .` first, and a `0o750` root defeats traversal
      regardless of the levels below it.
- [ ] T6 Document `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` and the fixed
      lock path in `infra/env/README.md` and the copyback runbook; assert
      `packages/common/safe_fs.py`, `run_tree_copyback.py:419-440` and
      `packages/common/state_manager.py` are unmodified by this diff.

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
`tests/test_run_tree_copyback.py:1097-1140`: a `threading.Event` gate injected by
`monkeypatch` so the competing writer runs inside the target window, two threads,
and joins with timeouts. `flock` is per open file description, so two threads that
each open the lock file do contend. The umask idiom is
`tests/test_canonical_precip_copyback_backfill.py:426-430` (save/restore). The
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
that harness — E6 because `safe_fs.py:91`'s `os.mkdir(part, 0o755, dir_fd=fd)`
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

### Red-proof record (run 2026-09-09, local, macOS / Python 3.11.14)

Harness: the changed source files are replaced in place with
`git show <rev>:<path> > <path>` and restored with `git checkout HEAD -- <path>`
— **not** `git stash`, because the stash stack is shared across worktrees.
Tests and `packages/common/copyback_guard.py` stay on the tree throughout.
Baseline is `master` unless a row names another revision.

| Row | Baseline | Recorded failing assertion |
|---|---|---|
| E2 (gate) | master | `assert competitor_ran_inside_window == [False], "the competitor entered the promote window"` → `AssertionError: assert [True] == [False]` |
| E2 (terminal, gate + `summary_a` assertions removed) | master | writer B reports `ok` and then `(copyback_root / key).read_bytes()` → `FileNotFoundError: .../canonical/gfs/2024060112/prcp_rate_or_amount/gfs_2024060112_prcp_rate_or_amount_f003.nc` |
| E3 (gate) | master | `assert script_ran_inside_the_batch == [False], "the script committed inside the publisher's batch"` → `assert [True] == [False]` |
| E3 (terminal, gate bypassed) | master | `script_summary[0]["totals"]["failed"] == 0` passes, then `(copyback_root / key).read_bytes()` → `FileNotFoundError: .../gfs_2026090200_prcp_rate_or_amount_f003.nc` |
| E4 (gate 1) | master | `assert competitor_ran_between_trees == [False], "the competitor committed inside A's batch"` → `assert [True] == [False]` |
| E4 (gate 2, the extended discriminator) | HEAD with the canonical lane's batch acquire replaced by a per-tree acquire around `publisher.py:1357-1362` | `assert competitor_ran_before_rollback == [False], "the competitor committed before A's batch rollback"` → `assert [True] == [False]`; gate 1 reads `[False]` under that placement, which is exactly why gate 2 exists |
| E4 (terminal, both gates bypassed) | same per-tree build | `(copyback_root / key).read_bytes()` → `FileNotFoundError: .../gfs_2024060112_prcp_rate_or_amount_f003.nc` — A's batch rollback deleted B's committed tree |
| E5 | master | `assert competitor_ran_inside_the_region == [False], "a competitor entered the promote region"` → `assert [True] == [False]` |
| E6 (`umask 027`) | master | `assert landed == {str(path): "0o755" for path in levels}` → `.../shared-object-store/canonical: '0o750' != '0o755'` (and the same for `canonical/gfs`) |
| E7 (`umask 022`, `umask 002`) | master | **green pre-change, by construction** — the masked `mkdir` already lands `0o755` at those umasks. Recorded as a no-regression row with no red proof, not as a discriminator. |
| E13 | master | `with pytest.raises(OrchestratorError)` → `Failed: DID NOT RAISE <class 'services.orchestrator.chain_types.OrchestratorError'>` |
| E14 | master | `with pytest.raises(PublishError)` → `Failed: DID NOT RAISE <class 'services.tile_publisher.publisher.PublishError'>` |
| E17 (state-index merge outside the mutex) | `40cf8ed9` (this branch, pre-fix) | `assert batch_lock_free_during_merge == [True], "the state-index merge ran inside the batch mutex"` → `assert [False] == [True]` |
| E18 (lock-owner poisoning) | `40cf8ed9` | create direction: `assert not (root / COPYBACK_BATCH_LOCK_NAME).exists()` → `assert not True`; root-owner row: `AttributeError: module 'packages.common.copyback_guard' has no attribute 'copyback_root_owner_uid'`; unstattable root: `'copyback root owner is unavailable' not in "cannot acquire copyback batch lock ..."`; deadline: `assert 300.0 == 900.0` |
| E19 (`..._LOCK_UNSAFE` per lane) | master | four lanes `Failed: DID NOT RAISE`, canonical `assert 'ok' == 'failed'`, forcing `assert 1 == 0` |
| E19 (narrowing variant — the defect the row actually guards) | HEAD with each `except CopybackLockError` arm narrowed to `CopybackLockTimeout` | `packages.common.copyback_guard.CopybackLockError: copyback batch lock must have mode 0600` escapes uncaught out of all four publisher/run-tree entry points, and the forcing report reads `assert 'failed' == 'copyback_lock_unavailable'` |
| E20 (import-closure includes package `__init__`s) | the pre-fix test body from `40cf8ed9`, with `import numpy` planted in `packages/common/__init__.py` | pre-fix test: **1 passed** (vacuous); fixed test: `AssertionError: .../packages/common/__init__.py pulls in a third-party dependency: ['numpy']`. Both the plant and the pre-fix test body were reverted immediately. |

- [ ] E1 `copyback_guard` lock unit tests: two threads on one root serialize (the
      second observes the first's completion); two distinct roots do not block each
      other; timeout raises the distinct error and performs no promote; a
      symlinked / `0o644` / foreign-owned lock file fails closed; a killed holder's
      lock is released by the kernel and the next writer proceeds.
- [x] E2 publisher × publisher race: competitor injected in the `:2380`→`:2382`
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
      (`publisher.py:1357-1362`) instead puts the gate's own wait inside the lock,
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
- [ ] E8 ACL boundary row: create a level beneath a parent carrying
      `default:user:X:rwx` / `default:mask::rwx` and assert the mask is `r-x`
      **both before and after** the widening — i.e. the caller-side `chmod` changes
      nothing, because `safe_fs`'s `mkdir` already clamped it. Assert separately
      that a mode-less `mkdir` under the same parent yields `mask::rwx`, pinning
      the `run_tree_copyback.py:440` boundary. Skip with an explicit reason on a
      platform without ACL support — never a silent pass.
- [ ] E9 Pre-existing intermediate directory at `0o700` → left unchanged (#1513).
- [ ] E10 `provider_lock_parent_unsafe` and the `filesystem-permission-determinism`
      umask regression tests stay green; `state_manager` copyback writers still
      succeed without the mutex.
- [ ] E11 Lock timeout inside `_mirror_canonical_precip`: the cycle records a
      `failed` `canonical_precip_mirror` receipt and does not raise.
- [x] E13 Lock timeout in the run-tree lane raises `RunTreeCopybackError`, is
      caught by `_copyback_stage_run_trees` (`chain_forecast_execution.py:953`),
      records an `object_store_copyback` / `failed` pipeline event, and then
      **propagates as `_chain.OrchestratorError`** (`:971`). Assert the event and
      the propagated type — not "no exception escapes". Accepted consequence,
      recorded here because it is the one lane where a timeout is not free: the
      call sits at `_after_cycle_stage_terminal:859` inside the
      `result_status == "succeeded"` branch, so the raise skips
      `update_forecast_cycle_status` (`:861`) and aborts the stage's success path.
      The 900 s default is sized against the measured hold (2.2 GB per
      acquisition at 62 MB/s ≈ 36 s, twice per cycle) rather than against the
      canonical hook's position; see `design.md` "Cost accepted, deliberately".
      A copyback timeout must stay fatal here: `resume_cycle_stage`
      (`chain_stage_execution.py:974`) re-enters `_after_cycle_stage_terminal`
      unconditionally, so the failure is retried on the next pass, and making it
      non-fatal would mark the cycle succeeded over absent data.
- [x] E14 Lock timeout in the q_down lane raises `PublishError` and it
      **propagates out of `publish_qdown_cycle` still carrying** the
      `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` code — `publisher.py:199-200` re-raises
      `PublishError` unchanged; the point of the distinct type is that it is not
      swallowed by the `SQLAlchemyError | OSError | ValueError` arm at `:201-202`
      nor rewrapped as `OBJECT_STORE_COPYBACK_FAILED`.
- [ ] E15 Copyback root identical to the object-store root: the lane returns
      `skipped` and **no lock file exists** anywhere under the object-store root.
- [ ] E16 `state_manager._ensure_copyback_state_parent` still chmods `0o775` and
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
      is refused *before* `O_CREAT` so it leaves no orphan. Every message names
      both uids and the lock path; none of them is a `CopybackLockTimeout`.
- [x] E19 A raised base `CopybackLockError` surfaces as each lane's *unsafe* code,
      distinct from its timeout code: q_down, run-products and the public
      `publish_qdown_cycle` handler
      (`PublishError`/`OBJECT_STORE_COPYBACK_LOCK_UNSAFE`), the canonical receipt
      (`error_type: CopybackLockError`), the run-tree lane
      (`RunTreeCopybackError`/`OBJECT_STORE_COPYBACK_LOCK_UNSAFE`) and the forcing
      backfill (`copyback_lock_unavailable`).
- [x] E20 The import-closure assertion parses the package `__init__.py` files on
      the path to each allowed in-tree module, not just the module itself.
- [x] E12 `grep -rn "DEBUG-" ` clean before commit; `git stash list` shows no
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

- [ ] AC1 Concurrent regression tests exist for both writer combinations
      (publisher × publisher, publisher × backfill script) and neither terminal
      state has a writer reporting `ok`/`copied` for content another writer's
      rollback removed. → E2, E3, E4.
- [ ] AC2 `run_tree_copyback.py:374 _replace_tree` is covered by the same
      adjudication: brought under the mutex **and** commented with why its own
      terminal state was benign. → T4, E5.
- [ ] AC3 Under `umask 027` a full publisher-side copyback leaves every level it
      created — the copyback root included — traversable, asserted level by level.
      → E6.
- [ ] AC4 node-27 oracle: the display API read account actually reads bytes
      through `canonical/**`. → the AC4 receipt procedure above.
- [ ] AC5 Fact-check recorded: `getfacl` on the copyback root and each lane.
      **Collected 2026-09-08 on node-27** — root and `canonical/`: no default ACL;
      `runs/`, `forcing/`, `states/`: `default:user:nwm:rwx`, `default:mask::rwx`.
      Recorded in `proposal.md`/`design.md`. It bounds where B bites; it does not
      gate the fix, which is unconditional.
- [ ] AC6 No #1513 regression (`provider_lock_parent_unsafe` green at node-27's
      default umask) and no #1631 regression (`run_tree_copyback.py:440`'s
      `mask::rwx` preservation pinned by E8). → E8, E10, T6.

## Non-goals

- Relaxing `provider_atomic`'s `0o022` gate.
- `chmod`-ing an already-existing path in `safe_fs`.
- Changing `run_tree_copyback.py:440`'s mode-less `mkdir`, or recovering the
  `mask::rwx` that `safe_fs`'s `mkdir` clamps (#1631's open question).
- Bringing `state_manager`'s per-file copyback writers (`:2194`, `:2405`) under
  this mutex, or narrowing `_ensure_copyback_state_parent`'s `0o775` (`:2481`) to
  the `0o755` this change uses — that `0o775` is what restores `mask::rwx` on the
  state lane.
- Copy-all-then-promote-all batch restructuring.
- Cross-host mutual exclusion (all writers are node-22 local processes; recorded
  as a known limit).
