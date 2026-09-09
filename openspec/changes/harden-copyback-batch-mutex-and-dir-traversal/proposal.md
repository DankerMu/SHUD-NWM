## Why

Object-store copyback has two independent defects in its **shared** transport
mechanism (issue #2035). Both are pre-existing and were explicitly deferred out
of PR #2028 because the affected code serves the `runs/`, `forcing/` and
`canonical/` lanes at once.

**Weakness A — destructive promote race.** `_replace_directory_tree_for_qdown_batch`
(`services/tile_publisher/publisher.py:2357`) is a lock-free three-step
`exists → rename-to-backup → promote`: the existence check at `:2378`, the backup
rename at `:2380`, the promote at `:2382`. A second writer entering the
**`:2380`→`:2382` window** sees no target, takes no backup, promotes its own tree,
records `backup_dir=None`, and reports `ok`. The first writer's promote then hits
`ENOTEMPTY`, and `_restore_copyback_backup` (`:2676`) `rmtree`s the competitor's
just-promoted tree before restoring its own stale backup. Net: the destination
silently reverts to pre-race content while one writer reports success.

The issue filed this as latent because #2034 proved the canonical mirror hung off
a `publish_qdown_cycle` path that never executed in the node-22 DB-free topology.
**That escalation trigger has now fired**: #2034 and #2069 are both closed and the
mirror hook now sits on the `convert` terminal stage
(`services/orchestrator/chain_forecast_execution.py:996 _mirror_canonical_precip`,
calling `publisher.copyback_canonical_precip` at `:1045`), dispatched under
`run_concurrent_submissions` bound 4
(`infra/env/compute.scheduler-dbfree.env.example:109`). The issue's own text says
this makes A "现网静默数据丢失（届时按 p1 处理）".

A per-tree lock is **not** sufficient. `_rollback_qdown_copyback_batch`
(`publisher.py:2416`) walks the whole batch: an entry whose `backup_dir is None`
is undone with `rmtree_no_follow(entry.target_dir)` (`:2426`). If writer A
promotes `prcp` into an empty slot (`backup_dir=None`), releases a per-tree lock,
and later fails on the `grid` tree, its batch rollback deletes whatever now sits
at `prcp` — including a tree writer B committed in the interim. The mutex must
therefore span the entire batch: plan → copy → every promote → commit-or-rollback.

**Weakness B — copyback intermediate directories land at the process umask.**
`safe_fs.ensure_directory_no_follow` passes an explicit `0o755` to `os.mkdir`
(`packages/common/safe_fs.py:91`), which the umask masks. That explicit mode is a
deliberate #1513 decision ("lets umask restrict but never widen"), and the
`filesystem-permission-determinism` capability already states that cross-uid
sharing "SHALL be established by the caller after creation". No caller does so for
copyback's intermediate parents: `_chmod_tree_readable` (`publisher.py:2702`)
compensates only the copied tree, never `canonical/`, `canonical/<S>/`,
`canonical/<S>/grid/`. Under `umask 027` every such level lands `0o750` and
node-27's reader account loses traversal.

Live `getfacl` on the shared root (node-27, `/home/ghdc/nwm/object-store`, the
same NFS export as node-22's `/ghdc/data/nwm/object-store`) settles the scope the
issue could not check remotely:

| path | default ACL | consequence |
|---|---|---|
| `object-store/` | none | mode bits govern |
| `runs/`, `forcing/`, `states/` | `default:user:nwm:rwx`, `default:mask::rwx` | umask ignored; `nwm` reaches its descendants through the ACL |
| `canonical/` | **none** | umask fully applies; node-27's reader reaches it through `other::r-x`, which `umask 027` removes — B is real and exclusive to this lane today |

A follow-up `chmod 0o755` is safe on **both** sides of that table, which is why
the compensation is unconditional. Measured on node-27 under a parent carrying
`default:user:X:rwx` / `default:mask::rwx`:

| creation | resulting mask |
|---|---|
| `os.mkdir(path, 0o755)` — what `safe_fs` does | `mask::r-x`, grant shown `#effective:r-x` |
| mode-less `os.mkdir(path)` — `run_tree_copyback.py:440` | `mask::rwx` preserved |
| the `0o755`-created directory, then `chmod 0755` | `mask::r-x` — **unchanged** |

So `safe_fs`'s `mkdir` has already clamped the mask before any caller-side
`chmod` runs; the widening is mask-neutral under an ACL and corrective without
one. This is the same #1631 clamp that `run_tree_copyback.py:427-434` documents,
and it is why the mode-less `mkdir` at `:440` must stay exactly as it is.

## What Changes

- Add `packages/common/copyback_guard.py`: one home for the two shared copyback
  concerns.
  - `copyback_batch_lock(copyback_root, *, timeout_seconds=None)`: a
    deadline-bounded exclusive `flock` on `<copyback_root>/.nhms-copyback-batch.lock`,
    following the proven `packages/common/node27_timeseries_lifecycle_lock.py`
    idiom (no-follow open, mode/owner/nlink/`(st_dev, st_ino)` assertions, never
    unlinked).
  - `ensure_traversable_copyback_directory(path, *, containment_root=None)`: creates
    the chain one level at a time through `safe_fs.ensure_directory_no_follow`,
    and `chmod 0o755` **only** the levels this call created.
- Hold `copyback_batch_lock` across every whole-batch directory-tree
  promote-and-commit critical section under the shared copyback root:
  `publisher._copyback_run_products` (def `publisher.py:721`, non-batch loop at
  `:823`), `publisher._copyback_qdown_products` (def `:873`, batch region
  `:982-1133`), `publisher._copyback_canonical_precip` (def `:1157`, batch region
  `:1209-1335`), `services/tile_publisher/forcing_copyback_backfill.py:744`,
  `scripts/canonical_precip_copyback_backfill.py` (per cycle, at `_backfill_cycle`
  def `:388`), and `services/orchestrator/run_tree_copyback.copyback_run_trees`
  (def `:36`). The lock is acquired after each lane's copyback-root identity and
  overlap guards, so a zero-write `skipped` path creates no lock file, and the
  timeout surfaces as each lane's own error type.
- Route copyback directory creation through
  `ensure_traversable_copyback_directory` at `publisher.py:1403`, `:1625`,
  `:1630`, `:1633`, `:2305`, `:2371` and `run_tree_copyback.py:49`, `:375`, `:396`.
  The first and last of those create the copyback **root** itself, which is in
  scope — issue #2035's `umask 027` measurement lists `0o750 .` first.
- Leave `run_tree_copyback.py:_copy_tree_no_symlinks`'s mode-less `mkdir`
  (`:419-440`) exactly as it is — the deliberate #1631 ACL-mask-preserving site,
  and not an `ensure_directory_no_follow` call.
- Leave `packages/common/safe_fs.py` unchanged: the
  `filesystem-permission-determinism` capability requires the helper never to
  `chmod` after creating, and compensation belongs to the caller.

## Capabilities

### New Capabilities

- `object-store-copyback-mutual-exclusion`: directory-tree promote-and-commit
  copyback batches SHALL be serialized across processes, so no writer's rollback
  can destroy a tree another writer promoted and reported.

### Modified Capabilities

- `filesystem-permission-determinism`: adds the caller-side obligation for
  copyback directories, and reconciles the existing ACL clause with the measured
  fact that `safe_fs`'s explicit-mode `mkdir` performs the mask clamp before any
  caller-side `chmod` can. That reconciliation is scoped to a `chmod` whose group
  bits are `r-x`; a wider `chmod` (`state_manager`'s `0o775`) still restores the
  mask and is untouched. `safe_fs` behavior itself is unchanged.

### Removed Capabilities

None.

## Non-Goals

- Relaxing `provider_atomic`'s `0o022` gate (#1631 forbids it).
- Making `ensure_directory_no_follow` `chmod` an already-existing path (#1513).
- Changing `run_tree_copyback.py:440`'s mode-less `mkdir`, or recovering the
  `mask::rwx` that `safe_fs`'s `mkdir` clamps (that is #1631's open question, not
  this change's).
- Bringing per-file provider-atomic copyback writers
  (`packages/common/state_manager.py:2194 merge_state_snapshot_index_copyback`,
  `:2405 _copyback_state_checkpoint`) under this mutex — they promote no directory
  tree, write a disjoint subtree, and carry their own provider lock.
- Restructuring the copyback batch into copy-all-then-promote-all.
- Cross-host mutual exclusion.
- Re-litigating PR #2028's implementation or tests.
- Object-store mount-point ownership (#2034, closed).
