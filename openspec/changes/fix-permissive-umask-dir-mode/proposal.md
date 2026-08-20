# Fix permissive-umask directory mode in safe_fs

## Why

`packages/common/safe_fs.py:68` creates directories with a mode-less
`os.mkdir(part, dir_fd=fd)`. The kernel then applies the implicit base `0o777`,
so the landed permission is `0o777 & ~umask` — a function of the ambient
environment rather than of the code.

`packages/common/provider_atomic.py:209` guards every provider lock with a
fail-closed check on the lock's **direct parent** directory:

```python
if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
    raise ProviderAtomicError("provider_lock_parent_unsafe", phase="precommit")
```

Under umask `0002` a mode-less `mkdir` lands `0o775`; `0o775 & 0o022 == 0o020`,
so the gate refuses. node-27 — the project's designated backend pytest oracle —
runs at umask `0002`, which puts **76 tests in `tests/test_production_scheduler.py`
into a permanent pre-existing red state on master** (measured locally at
`29892932`: `76 failed, 1588 passed`, reproducible with
`(umask 002; uv run pytest -q tests/test_production_scheduler.py)`).

Two consequences: every issue whose Verification runs this family on node-27 sees
red unrelated to its own diff, and — the expensive one — 76 lines of standing
noise are enough to hide a real regression in exactly the db-free state machine
and file-provider fail-closed semantics that the suite exists to protect.

The failure is **umask-conditional, not host-conditional**. node-22 (`0022`),
local macOS (`022`), and GitHub runners (`022`) all happen to sit on the safe
side of the gate, which is why this survived undetected: the repository's only
umask tests pin the **strict** side (`os.umask(0o077)` at
`tests/test_scheduler_file_provider_refresh.py:823`/`:920` and
`tests/test_run_tree_copyback.py:212`). The permissive side has zero coverage.

## What Changes

Two halves, both required. Measured: `(a)` alone leaves 74 of the 76 red, because
the directory that actually trips the gate is created by a test helper that never
touches `safe_fs`.

- **(a) Production-side determinism.** `safe_fs`'s directory creation pins an
  explicit base mode `0o755` instead of relying on the implicit `0o777`. This
  mirrors the helper's own file path, which already passes an explicit `0o666`
  base (`safe_fs.py:119`). The gate at `provider_atomic.py:209` is **not**
  relaxed — it is a fail-closed security property and must not yield to the
  environment.
- **(b) Test-side directory hygiene.** The test helpers that pre-create provider
  lock parents (`_scheduler_env_roots`, `_set_db_free_scheduler_env`, and the
  per-test sites the umask-`002` run enumerates) create those directories with a
  mode that satisfies the gate, instead of inheriting the ambient umask.
- **(c) Permissive-side regression coverage**, symmetric with the existing
  `0o077` strict-side tests, so the gap that hid this cannot silently reopen.
- **(d) A recorded ruling** that the gate stays fail-closed on *pre-existing*
  group/world-writable lock parents.

Non-goals: relaxing the `0o022` gate; changing node-27's system umask; touching
`scheduler_lease`'s own lock channel (`scheduler_lease.py:565`), which does not
route through this gate and is not claimed without a trace; the other
`provider_atomic` fail-closed branches.

## Impact

- Affected code: `packages/common/safe_fs.py` (shared helper, **119 production
  call sites** of `ensure_directory_no_follow`), the six `provider_atomic`
  importers as unchanged consumers, and the test helpers in `(b)`.
- Affected specs: new capability `filesystem-permission-determinism`.
- On hosts **without** POSIX ACLs the behavior delta is confined to umask
  `0002`/`0000`, where safe_fs-created directories tighten `0o775 -> 0o755`.
  Under umask `0022` and `0077` the landed mode is byte-identical to today,
  because the kernel still applies the umask to an explicit mode and a umask can
  only clear bits, never set them.
- On a parent carrying a **default POSIX ACL** the umask is ignored and the mode
  argument instead clamps the ACL **mask**. The node-22 -> node-27 NFS handoff is
  built on such an ACL (`default:user:nwm:rwx` on `forcing/`, `runs/`, `states/`),
  so this is a real boundary, enumerated in design D7. Only `states/` is
  structurally immune (its post-create `chmod 0o775` restores the mask);
  `forcing/` and `runs/` are exposed in mechanism but inert in fact — `find`
  reports zero `nwm`-owned entries in any of the three, node-27 only reads there.
  The genuinely two-uid subtrees, `raw/` (`777`) and `models/direct_grid_variants/`
  (`1777` sticky), share through the parent mode rather than group bits, and
  their children sit in empty groups, so tightening the group bit grants and
  removes nothing. Recorded as a boundary with a follow-up issue, not designed
  around.
