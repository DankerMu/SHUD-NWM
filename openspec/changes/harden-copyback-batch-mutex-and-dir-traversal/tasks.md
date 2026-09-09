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
        `0o600`, effective-uid owner, path/fd `(st_dev, st_ino)` match), bounded
        `LOCK_EX|LOCK_NB` poll loop with a **default 300 s** deadline overridable
        by `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS`, never unlinks the
        lock file. **Never reentrant**: `flock` is per open-file-description, so a
        second acquisition on the same file from the same process blocks itself
        until the deadline. Acquire at batch-owner level only; `copyback_batch_lock`
        must never be called from inside `_copyback_object_tree_with_rollback`
        (`publisher.py:1593`) or any helper it calls, because
        `forcing_copyback_backfill._copy_package` (`:744`) calls that helper
        directly while already holding the lock itself (T3).
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
      loss.
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
Each new-behavior test must be shown red against pre-change source first.

- [ ] E1 `copyback_guard` lock unit tests: two threads on one root serialize (the
      second observes the first's completion); two distinct roots do not block each
      other; timeout raises the distinct error and performs no promote; a
      symlinked / `0o644` / foreign-owned lock file fails closed; a killed holder's
      lock is released by the kernel and the next writer proceeds.
- [ ] E2 publisher × publisher race: competitor injected in the `:2380`→`:2382`
      window. **Red proof (pre-change)**: winner reports `ok`, its tree is absent,
      destination holds pre-race content. **Green**: the competitor blocks; both
      writers report truthfully; the destination holds one writer's complete tree.
- [ ] E3 publisher × `canonical_precip_copyback_backfill` race, same window.
      **Red**: a file the script counted as `copied` is removed by the publisher's
      rollback. **Green**: serialized; every counted file survives.
- [ ] E4 **batch-scope row** (the per-tree-lock discriminator): A promotes `prcp`
      into an empty slot, B commits into `prcp`, A then fails on `grid`. Expected:
      A's batch rollback does not delete B's tree. Must fail if the lock is
      narrowed to per-tree scope.
- [ ] E5 `run_tree_copyback._replace_tree` × publisher batch: serialized; loser
      reports failure; winner's tree intact; no `rmtree` of a competitor's tree.
- [ ] E6 `umask 027` publisher canonical copyback: assert the copyback root,
      `canonical/`, `canonical/<S>/`, `canonical/<S>/<cycle>/`,
      `canonical/<S>/grid/` each land `0o755` **individually**, including the case
      where this run is what creates the root. `os.umask` is process-global — set and restore in a
      fixture and keep the test off xdist, or run it in a subprocess.
- [ ] E7 `umask 002` and `umask 022`: same levels land `0o755`; no group/other
      write bit on any level.
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
- [ ] E13 Lock timeout in the run-tree lane raises `RunTreeCopybackError`, is
      caught by `_copyback_stage_run_trees` (`chain_forecast_execution.py:953`),
      records an `object_store_copyback` / `failed` pipeline event, and then
      **propagates as `_chain.OrchestratorError`** (`:971`). Assert the event and
      the propagated type — not "no exception escapes". Accepted consequence,
      recorded here because it is the one lane where a timeout is not free: the
      call sits at `_after_cycle_stage_terminal:859` inside the
      `result_status == "succeeded"` branch, so the raise skips
      `update_forecast_cycle_status` (`:861`) and aborts the stage's success path.
      The 300 s default was sized against the canonical hook's position; if this
      lane proves to need a different budget, that is a follow-up, not a silent
      retune.
- [ ] E14 Lock timeout in the q_down lane raises `PublishError` and it
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
- [ ] E12 `grep -rn "DEBUG-" ` clean before commit; `git stash list` shows no
      leftover `red-proof` entry.

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
