# Design

## Triage record (issue #2035 was `Readiness: needs-triage`)

Issue #2035 deliberately listed candidates without choosing. All three owner
calls are recorded here with the evidence that forced them. The user
pre-authorized this run and asked for advisor consultation at key nodes; a
`决策记录` line in the PR body carries these decisions for veto.

| Decision | Choice | Why |
|---|---|---|
| A: advisory lock vs documented ops discipline | **advisory `flock`** | The issue scoped candidate 2 to "只覆盖 #2016 这一次"; #2016 is closed and #2069 put the mirror on a recurring `convert`-terminal hook, so a one-shot ops assertion protects nothing going forward, and discipline is not CI-verifiable. |
| B: call-site compensation vs `umask 022` wrapper assertion | **call-site compensation** | The issue notes bring-up shells bypass wrappers, which is exactly the environment the defect targets. The `filesystem-permission-determinism` capability already assigns cross-uid widening to the caller. |
| `run_tree_copyback._replace_tree` inclusion | **same mutex** | It writes under the same `NHMS_OBJECT_STORE_COPYBACK_ROOT`. Its own terminal state is benign (spurious failure, competitor's tree intact) but a shared root with one lock-free writer leaves the publisher's window open regardless. |

**Priority escalation.** #2035 filed as p2 with an explicit upgrade trigger:
"#2034 一旦把镜像接到真会跑的缝上 … A 立刻变成现网静默数据丢失（届时按 p1 处理）".
#2034 and #2069 are closed; the trigger has fired. Treated as p1.

## Weakness A: batch-scoped mutex

### Why the lock must be batch-scoped, not per-tree

`_rollback_qdown_copyback_batch` (`publisher.py:2416-2451`) is the reason. Its
`backup_dir is None` branch calls `rmtree_no_follow(entry.target_dir)` (`:2426`) —
"I found nothing here, so removing what is here restores the world". That claim is
only true while no other writer can commit into that slot. Sequence with a
per-tree lock:

1. A locks `prcp`, finds it absent, promotes, records `backup_dir=None`, unlocks.
2. B locks `prcp`, backs A's tree up, promotes, commits (removing the backup of
   A's tree), unlocks. B reports `ok`.
3. A locks `grid`, fails for any reason.
4. A's batch rollback hits the `prcp` entry: `backup_dir is None` →
   `rmtree_no_follow` → **B's committed tree is deleted**, B already reported ok.

So the mutex is acquired where `rollback_log` is owned and released only after
`_commit_qdown_copyback_batch` or `_rollback_qdown_copyback_batch` returns.

### Cost accepted, deliberately

Copy-to-temp sits inside the lock because the batch loop interleaves copy and
promote per tree (`publisher.py:1281-1298`). Restructuring into
copy-all-then-promote-all is a larger change with its own risk and is a non-goal.
The throughput cost is bounded by `_COPYBACK_MAX_TOTAL_BYTES` (100 GiB,
`publisher.py:49`) and by the fact that every contending writer is a node-22
process on one host. One knock-on: the q_down lane runs inside the open
SQLAlchemy `Session` at `publisher.py:197-198`, so a lock wait extends that
session's lifetime by up to the deadline. Holding the session across the copy
itself is pre-existing; the deadline is what bounds the new increment, and it is
one more reason for 300 s rather than 1800 s.

### Lock mechanics

- **Idiom**: copied from `packages/common/node27_timeseries_lifecycle_lock.py` —
  `O_RDWR|O_NOFOLLOW|O_CLOEXEC`, `O_CREAT|O_EXCL` then fall back to plain open,
  mode `0o600`, and `_require_lock_identity`-style assertions (regular file, not
  a symlink, exactly one hard link, owned by the effective uid, path/fd
  `(st_dev, st_ino)` identical) re-checked after `fchmod` and after the `flock`.
  The lock file is never unlinked by this code.
- **Location: `<copyback_root>/.nhms-copyback-batch.lock`, a fixed name with no
  env override.** Rejected alternatives and why:
  - *A `/tmp` path keyed by the root's `(st_dev, st_ino)`* — systemd
    `PrivateTmp=true` and Slurm `job_container/tmpfs` each give a process a
    private `/tmp`. Two writers under different namespaces would `flock` two
    different inodes and the mutex would silently do nothing, which is the exact
    failure shape this change exists to remove. (`infra/systemd/nhms-scheduler-file-provider-refresh.service:16-17`
    shows the repo already reasons about `PrivateTmp` for a copyback-adjacent
    unit.) A `/tmp` reaper or reboot removing a long-idle lock file has the same
    effect.
  - *A workspace-local path* — the publisher, the orchestrator and the two
    standalone backfill CLIs do not share one workspace root.
  The copyback root is the one path every writer has already resolved by
  construction, so all six writers provably reach one inode. It also removes the
  `(st_dev, st_ino)` keying entirely: distinct roots (every pytest `tmp_path`)
  get distinct lock files, so the suite does not self-serialize, and an aliased
  root reaches the same file through either alias.
- **Weakness B does not apply to it**: it is a *file* at a root that already
  exists, created `0o600` by its owner. Traversal of the root is not at issue, and
  every writer runs as the same uid.
- **NFS**: correct in both `flock` modes for same-host writers. With server-side
  locking, `flock` is honoured across the export; with `local_lock=flock`/`nolock`
  it degrades to node-local, which still serializes two node-22 processes — the
  only contention this change claims to prevent. Cross-host exclusion is a
  recorded non-goal.
- **Bootstrap ordering**: prepare/verify the copyback root
  (`publisher._prepare_copyback_root` `:1388`, `run_tree_copyback.py:49`) → run the
  root's **identity and overlap guards** → open the lock file under it → acquire →
  plan → copy → promote → commit/rollback → release.
  The guards must precede the lock, not just the root preparation. All three lanes
  return `skipped` when the copyback root resolves to the object-store root itself
  (`publisher.py:1227-1228`, the same identity check in `_copyback_qdown_products`
  noted at `:918`, `run_tree_copyback.py:56-61`), and `_prepare_copyback_root`
  deliberately only *verifies* in that case (`:1401`, under the branch at
  `:1400`). Acquiring before the guards
  would create a lock file inside the production object-store root on a path that
  writes nothing.
- **Blocking with a deadline**: contention must wait, not refuse — refusing would
  turn a race into a dropped mirror. Uses `LOCK_EX|LOCK_NB` in a bounded poll loop
  (the `packages/common/evidence_io.acquire_exclusive_flock_until` shape).
  **Default 300 s**, overridable via
  `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS`. Sizing evidence:
  `_mirror_canonical_precip` (`chain_forecast_execution.py:996`) runs inside the
  scheduler pass rather than in a job of its own, so a 30-minute wait would sit on
  the pass — reasoning from where the hook executes, not a cited spec requirement;
  and it wraps the whole publisher call in
  `except Exception` (`:1046`), recording a `failed` receipt rather than failing
  the cycle, so a timeout defers the mirror to the next cycle instead of breaking
  one. Timeout raises a distinct loud error and never falls back to an unlocked
  promote — **as each lane's own error type**, because a foreign exception escapes
  two of the three callers: `_copyback_stage_run_trees` catches only
  `RunTreeCopybackError` (`chain_forecast_execution.py:953`) and runs on the
  `parse` stage of every cycle (`:932`), and `publish_qdown_cycle` catches only
  `PublishError | SQLAlchemyError | OSError | ValueError` (`publisher.py:199-202`).
  So: `RunTreeCopybackError` in the run-tree lane; `PublishError` with a distinct
  `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` code in the q_down/run-products lane; and in
  the canonical lane the acquire sits inside `_copyback_canonical_precip`'s own
  `try:` (`publisher.py:1209`), so its `except Exception` at `:1301` catches
  first and returns the `failed` summary; `_mirror_canonical_precip`'s
  `except Exception` (`chain_forecast_execution.py:1046`) is the outer net, not
  the handler that fires. Same observable receipt either way.
  One asymmetry is deliberate and recorded: the run-tree lane's timeout does *not*
  degrade to a receipt. `_copyback_stage_run_trees` re-raises as
  `_chain.OrchestratorError` (`:971`) from inside the
  `result_status == "succeeded"` branch of `_after_cycle_stage_terminal` (`:859`),
  so the stage's `update_forecast_cycle_status` (`:861`) is skipped. That is the
  existing contract for a run-tree copyback failure and this change does not widen
  it; it only adds one more way to reach it. E13 asserts it rather than leaving it
  as a surprise.
- **Backfill granularity**: `scripts/canonical_precip_copyback_backfill.py`
  acquires **per mirrored tree**, not once for the whole run. A whole-run
  acquisition on a long backfill would hold the lock past the publisher's
  deadline and convert this fix into a mirror outage. Per-tree is sufficient
  rather than merely convenient: the script mirrors file by file and has no
  rollback of its own, so it never needs two trees to land or vanish together.
  The guarantee it buys is the one the spec states — a file it reports as
  `copied` survives a concurrent publisher rollback — and that holds per tree,
  because a publisher batch that starts after the script released took its
  backup from a target that already contained those files (`backup_dir is None`,
  the destructive case, arises only when the target did not exist at promote
  time). Both the cycle loop (one tree per cycle) and the grid loop (one per grid
  id) therefore route through one locked mirror helper.
- **Single uid**: the `0o600` + owner assertion means a writer under a different
  account fails closed with a clear error instead of silently running unlocked.
  All copyback writers are `frd_muziyao` on node-22. Recorded as a known limit.
- **Deadlock ordering**: this lock is the outermost copyback lock.
  `merge_state_snapshot_index_copyback`'s provider lock is taken *inside*
  `copyback_run_trees` (`run_tree_copyback.py:107`), so the order is total and no
  path acquires a provider lock before this one.

### What is deliberately outside the mutex

`packages/common/state_manager.py:2194 merge_state_snapshot_index_copyback` and
`:2405 _copyback_state_checkpoint` also write under the copyback root
(`scheduler/state-index/`, `states/`). They stay outside because they promote no
directory tree — they are per-file provider-atomic writers with their own lock,
on a subtree disjoint from every tree this mutex protects. The mutex requirement
is scoped to directory-tree promote-and-commit batches for exactly this reason,
and the spec delta carries an explicit exemption scenario.

### `run_tree_copyback`'s different terminal state

`_replace_tree` (`services/orchestrator/run_tree_copyback.py:374-392`) has the
same window but a guarded recovery: `if backup.exists() and not target.exists()`
(`:388`). With a competitor's tree in place that predicate is false, so it does
not restore and does not `rmtree` the competitor — the loser gets a spurious
failure and no data is lost. It is brought under the same mutex anyway (it shares
the root), and a comment records that its terminal state was always the benign
one so future readers do not go hunting for a lost update that never existed.

## Weakness B: unconditional caller-side widening of created levels

### Why not a `safe_fs` change

`filesystem-permission-determinism` states: "The helper SHALL NOT `chmod` a
directory after creating it, and SHALL NOT modify the mode of a directory that
already exists," and "Where a directory must be shared across uids, the sharing
SHALL be established by the caller after creation." Adding a widening kwarg to
`ensure_directory_no_follow` would contradict the first sentence; the second
already names the correct seam. `safe_fs` is untouched.

### Why the widening is unconditional

An earlier draft made the `chmod` conditional on the absence of an inherited
POSIX default ACL, to avoid clamping `mask::rwx` (#1631). **Measurement on
node-27 shows that probe would be dead code.** Under a parent carrying
`default:user:X:rwx` / `default:mask::rwx`:

| creation | resulting mask |
|---|---|
| `os.mkdir(path, 0o755)` — what `safe_fs` does | `mask::r-x`, grant `#effective:r-x` |
| mode-less `os.mkdir(path)` — `run_tree_copyback.py:440` | `mask::rwx` preserved |
| the `0o755`-created directory, then `chmod 0755` | `mask::r-x` — **unchanged** |

`safe_fs`'s explicit mode has already performed the clamp before any caller-side
`chmod` runs — stated verbatim by the repo at `run_tree_copyback.py:427-434` and
by the existing capability at
`openspec/specs/filesystem-permission-determinism/spec.md:99-103`. So the
widening is **mask-neutral** under an ACL and **corrective** without one, and one
unconditional rule is both correct and simpler. Recovering that clamped mask is
#1631's open question and an explicit non-goal here.

Live `getfacl` on the shared root, which sets where B actually bites today:

```
object-store/   no default ACL          (0775 frd_muziyao:nfsdata, other::r-x)
runs/           default:user:nwm:rwx, default:mask::rwx
forcing/        default:user:nwm:rwx, default:mask::rwx
states/         default:user:nwm:rwx, default:mask::rwx
canonical/      no ACL at all           (0755, other::r-x)
```

`canonical/` carries no ACL, so mode bits alone decide traversal for node-27's
reader (a different uid, reaching it through `other::r-x`). Under `umask 027`
`other` loses `r-x` and the mirror becomes unreadable. The ACL-bearing lanes are
reachable through their inherited grant regardless of umask, so B does not bite
them today — but the same rule keeps them correct if an ACL is ever removed.

Only levels this call created are touched, so #1513's "never fchmod an existing
path" holds verbatim.

**The copyback root itself is in scope.** Issue #2035's own `umask 027`
measurement lists `0o750 .` — the root — first, and a `0o750` root defeats
traversal regardless of what sits below it. `publisher.py:1403` and
`run_tree_copyback.py:49` are the two sites that create it, and they pass no
`containment_root`. The helper must therefore determine "levels this call created"
by probing upward for missing components **before** creating them (the
`canonical_precip_copyback_backfill._ensure_target_directory` idiom), not by
walking `relative.parts` against a containment root: `_ensure_copyback_state_parent`
(`state_manager.py:2472`) uses the latter, and its parts list is empty when the
path *is* the root — a silent no-op that would never widen it. `containment_root`
stays an optional pass-through to `safe_fs` for symlink containment only.

### Level-by-level creation

Copied from `scripts/canonical_precip_copyback_backfill.py:189-225`
(`_ensure_target_directory`) and from
`state_manager._ensure_copyback_state_parent` (`:2464`): probe upwards for the
missing components, then create and widen them outermost-first, one at a time. A
single `mkdir(parents=True)` followed by a widening loop leaves ancestors at the
umask forever whenever the leaf fails, and a later run's existence probe no longer
counts them as created.

## Alternatives rejected

- **Per-tree lock** — insufficient, see the four-step sequence above.
- **Documented ops mutex only** — no CI oracle, and the recurring `convert` hook
  makes it a permanent obligation rather than a one-shot.
- **`umask 022` assertion in the publisher wrapper** — bypassed by exactly the
  manual bring-up shells the defect targets.
- **ACL-conditional widening** — the probe can never change an observable
  outcome; measured above.
- **Lock file in `/tmp`** — private-`/tmp` namespaces and reapers can silently
  split the mutex.
- **Widening `safe_fs` itself** — contradicts the existing capability.

## Invariant Matrix

Governing invariant (A): **once a copyback writer's batch reports success, every
tree that batch promoted is present at its target until a later writer's own
completed batch replaces it — and no writer's rollback path ever removes a tree it
did not itself promote.**

Governing invariant (B): **every directory level that a copyback call creates —
the copyback root itself included — is traversable by the consuming account, and
no level the call did not create has its mode changed.**

Source-of-truth identity/contract: the `_CopybackRollbackEntry` batch
(`target_dir`, `backup_dir`) owned by the single holder of the
`<copyback_root>/.nhms-copyback-batch.lock` flock.

Surfaces:

- Producers: `publisher._copyback_run_products` (def `publisher.py:721`,
  non-batch loop `:823` — **no production caller today**; `publish_cycle` `:181`
  delegates to `publish_qdown_cycle`, and the only callers are
  `tests/test_tile_publisher.py:1192`/`:1264`. Locked anyway so the two siblings
  cannot diverge), `publisher._copyback_qdown_products` (def `:873`, batch
  `:982-1133`), `publisher._copyback_canonical_precip` (def `:1157`, batch
  `:1209-1335`),
  `services/tile_publisher/forcing_copyback_backfill.py:744`,
  `scripts/canonical_precip_copyback_backfill.py` `_backfill_cycle` (def `:388`),
  `services/orchestrator/run_tree_copyback.copyback_run_trees` (def `:36`)
- Validators/preflight: `publisher._plan_canonical_precip_tree` (`:1365`),
  `_verify_copyback_target_tree_clean`, `_prepare_copyback_root` (`:1388`)
- Storage/cache/query: `LocalObjectStore` under the copyback root; node-27
  display API read path over the same NFS export
- Public routes/entrypoints: `publisher.publish_qdown_cycle`;
  `services/orchestrator/chain_forecast_execution.py:996 _mirror_canonical_precip`
  and its `:1045 publisher.copyback_canonical_precip` call;
  `services/orchestrator/cli.py:116`; both backfill CLIs
- Frontend/downstream consumers: node-27 display API reading `canonical/**` as a
  different uid
- Failure paths/rollback/stale state: `_replace_directory_tree_for_qdown_batch`
  (`:2357`), `_replace_directory_tree_no_follow` (`:2296`),
  `_rollback_qdown_copyback_batch` (`:2416`), `_commit_qdown_copyback_batch`
  (`:2455`), `_restore_copyback_backup` (`:2676`),
  `run_tree_copyback._replace_tree` (`:374`)
- Evidence/audit/readiness: copyback summary payloads (`status`, `trees[].status`,
  `file_count`); the `canonical_precip_mirror` pipeline event
  (`chain_forecast_execution.py:1061`); node-27 traversal receipt

Regression rows:

- publisher batch × publisher batch, competitor injected in the `:2380`→`:2382`
  window → the second writer blocks until the first batch commits; both report
  truthfully; neither tree is destroyed.
- publisher batch × `canonical_precip_copyback_backfill` → the script blocks on
  the same lock; no file it counted as `copied` is removed by the publisher's
  rollback.
- **batch-scope row** (the per-tree-lock discriminator): A promotes `prcp` into an
  empty slot (`backup_dir=None`), B commits into `prcp`, A then fails on `grid` →
  A's batch rollback must not delete B's tree.
- `run_tree_copyback._replace_tree` × publisher batch → serialized; the loser
  reports failure, the winner's tree is intact.
- Lock timeout exceeded → loud distinct error, no unlocked promote; for the
  canonical mirror the cycle still records a `failed` receipt and survives.
- Lock file is a symlink / wrong mode / foreign owner → fail closed, no promote.
- Two distinct copyback roots → two lock files, no cross-blocking.
- `umask 027` publisher canonical copyback → the copyback root, `canonical/`,
  `canonical/<S>/`, `canonical/<S>/<cycle>/`, `canonical/<S>/grid/` each land
  `0o755`.
- Copyback root created from scratch under `umask 027` → the root itself is
  `0o755`, not `0o750`.
- Lock timeout in the run-tree lane → `RunTreeCopybackError`, caught by
  `chain_forecast_execution.py:953`; in the q_down lane → `PublishError` with the
  copyback-lock-timeout code, caught by `publisher.py:199-202`.
- Copyback root identical to the object-store root → lane returns `skipped`, no
  lock file is created anywhere under the object-store root.
- `umask 002` (node-27) and `umask 022` (node-22) → unchanged `0o755`; no
  group/other write bit appears.
- Parent carrying a default ACL → the created level's mask is `r-x` both before
  and after the widening; the widening changes nothing (#1631 boundary held, not
  regressed).
- Pre-existing intermediate directory at a restrictive mode → left alone (#1513
  "never fchmod an existing path").
- Unchanged sibling consumer: `run_tree_copyback._copy_tree_no_symlinks`'s
  mode-less `mkdir` (`:440`) still creates `mask::rwx` interiors.
- Unchanged sibling consumer: `provider_atomic` lock-parent gate still refuses a
  `0o022` parent; `provider_lock_parent_unsafe` tests stay green.
- Unchanged sibling consumer: `state_manager` per-file copyback writers
  (`:2194`, `:2405`) still succeed without this mutex.

## Boundary-surface checklist

- **Shared helper roots**: `packages/common/safe_fs.py` (unchanged — asserted),
  new `packages/common/copyback_guard.py`,
  `packages/common/node27_timeseries_lifecycle_lock.py` (pattern source,
  unchanged), `packages/common/evidence_io.py` (deadline-poll shape, unchanged),
  `packages/common/state_manager.py` (unchanged, explicitly exempt).
- **Public entrypoints**: `publish_qdown_cycle`, `copyback_run_trees`,
  `services/orchestrator/cli.py:116`, both backfill CLIs.
- **Read surfaces**: node-27 display API over `canonical/**`;
  `apps/api/routes/precip.py`.
- **Write/delete/overwrite surfaces**: every `os.replace` / `rmtree_no_follow` in
  the copyback promote/rollback/commit paths.
- **Staging/publish/rollback surfaces**: temp-tree staging, backup rename,
  promote, commit, rollback, `_restore_copyback_backup`.
- **Producer/consumer evidence boundaries**: copyback summary payload statuses and
  the `canonical_precip_mirror` event; a writer must never report `ok` for a tree
  it no longer owns.
- **Stale-state/idempotency boundaries**: `trees_already_mirrored` skip path;
  re-running a backfill after a lock timeout; a stale lock file left by a killed
  writer (flock is released by the kernel on process exit).
- **Unchanged downstream consumers**: `provider_atomic` gates,
  `state_manager._ensure_copyback_state_parent`,
  `run_tree_copyback._copy_tree_no_symlinks`, `services/orchestrator/retention.py`
  (descends only `root/<prefix>` for the cycle-scoped prefixes and `root/runs`; it
  never enumerates root-level files, so the lock file is invisible to it).
