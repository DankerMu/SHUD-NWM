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
`backup_dir is None` branch calls `rmtree_no_follow(entry.target_dir)` (`:2514`) —
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

**Sizing the deadline.** Two facts an earlier draft of this section missed, and
which decide the number: `flock` is per *open file description*, so the
scheduler's execution units — threads of one process — contend with each other
exactly as separate hosts would; and copyback runs **at most once per cycle,
never twice**. The gate `_stage_should_copyback_run_trees`
(`chain_forecast_execution.py:931-934`) returns True for `parse`, and for
`state_save_qc` only when `terminal_stage == "forecast_state_save_qc"` — and the
two are never both live. With `terminal_stage` unset the stage list is all of
`M3_STAGES` (`convert, forcing, forecast, parse, state_save_qc, publish`), so
both stages run, but the gate's own condition is false and only `parse` fires.
Under the production `forecast_state_save_qc` the gate admits `state_save_qc`
while `stages_through`'s special case (`chain_stages.py:75-79`) rebuilds the list
as `(convert, forcing, forecast, state_save_qc)` — `parse` is gone. With a
terminal stage of `forecast` or earlier, neither runs and the count is zero.
Corrected 2026-09-10; the sizing below was computed on an earlier "twice per
cycle" reading and is therefore **conservative by a factor of two**, which is why
it is not being re-tuned.

Measured on node-22 against the live NFS export `ghdc:/home/ghdc` (nfs4.2) — measured,
not estimated. This is a dated snapshot of a live system, not a standing invariant:
re-measure before reusing it to justify a different deadline. Throughput re-verified
2026-09-10T14:56Z on a different subtree
(`forcing/gfs/2026090900/basins_byh_vbasins`, 44 MB apparent / 298 files, `cp -R` +
`sync`, two runs: 0.810 s and 0.756 s ≈ **56 MB/s**), i.e. within ~10% of the 62 MB/s
below, so the 900 s sizing still holds with margin. The original `forcing/gfs/`
`2026080600/` subtree named in the table has since been retained away and cannot be
re-copied.

| Quantity | Value | Source |
|---|---|---|
| NFS copy throughput | **62 MB/s** | 68 MB / 455 files in 1.097 s (`cp -R` + `sync`, `forcing/gfs/2026080600/basins_lh_gl_vbasins/`) |
| One cycle cohort | 38 run trees = 1.1 GB, + referenced forcing subtrees 1.1 GB = **2.2 GB** | `ifs/2026090300` |
| Referenced forcing subtree | **4–68 MB**, not the 3.6–3.7 GB `forcing/<source>` tree | keys truncated to five path segments, `run_tree_copyback.py:399-401` |
| Hold per acquisition | 2.2 GB / 62 MB/s ≈ **36 s** | one `copyback_run_trees` call covers all active basins of the cycle (`chain_forecast_execution.py:938-948`) |
| Acquisitions per cycle | **1** (the sizing below assumes 2 — see above; the error is conservative) | only one of `parse` / `state_save_qc` is ever admitted by the gate, so ≈ 36 s of hold per execution unit per cycle, not the ~72 s the deadline was sized against |
| Live steady-state concurrency | **N = 2** execution units | node-22's live `infra/env/compute.scheduler-dbfree.env`: `NHMS_SCHEDULER_SOURCES=gfs,IFS` × `NHMS_SCHEDULER_MAX_CYCLES_PER_SOURCE=1` (the checked-in `.example` carries the sources line at `:93`; the per-source budget is set in the live file only) |
| First-ever copyback into a fresh root | + **73 s** | `models/` is 4.5 GB and is only reused via `_reuse_immutable_model_tree` when the target already exists (`:118`) |

`NHMS_SCHEDULER_SLURM_ARRAY_CONCURRENCY_BOUND=32` bounds Slurm *array tasks*, not
copyback contenders, and must not be cited here. The forcing-key truncation is
what keeps the hold bounded: without it a referenced forcing tree would be
gigabytes rather than tens of megabytes.

At 36 s per acquisition, **300 s admitted only ~8 preceding acquisitions ≈ 4
concurrent execution units** — steady state (N=2 → ~144 s worst wait) fit with
barely 2× margin and a replay/backfill pass with 4+ units exceeded it. The
default is therefore **900 s**, which covers ~24 acquisitions ≈ 12 execution
units. It is still a bounded, loud failure rather than a hang. Those two
per-unit figures both assume the two-acquisitions-per-cycle reading corrected
above; at the real one acquisition per cycle the same 900 s covers ~24 execution
units and the N=2 worst wait is ~72 s, not ~144 s. The deadline is left where it
is: erring long costs nothing here, and re-tuning it would invalidate the
measured basis for no operational gain.

The wait itself is cheap: `_COPYBACK_MAX_TOTAL_BYTES` (100 GiB,
`publisher.py:57`) bounds the copy, and the q_down lane's open SQLAlchemy
`Session` (`publisher.py:205`) is extended by at most the deadline. Holding
that session across the copy is pre-existing; only the wait is new.

**Known limit, not fixed here.** `_flock_until_deadline` polls
`LOCK_EX|LOCK_NB` every 10 ms and is therefore unfair: under sustained
contention a waiter can lose every poll and be starved across passes. A ticket
lock would fix it and is deliberately not built — the failure is loud and
bounded, and on the run-tree lane it is retried on the next pass
(`resume_cycle_stage`, below). It is **not** retried on the canonical-mirror
lane, which records a `failed` receipt and moves on; see "Nothing retries that
mirror" below. Even so, a ticket lock is the wrong trade for that complexity.

**Known limit, not fixed here.** `provider_destination_lock`
(`packages/common/provider_atomic.py:326-339`) is `blocking=True` with no
deadline. That is pre-existing on master, but moving the state-index merge out
of the batch mutex changes who waits on it: before, one writer at a time reached
the merge because the rest queued on the bounded mutex; now N writers can wait
there concurrently. `copyback_guard`'s "never a hang" promise therefore covers
the batch mutex only — `copyback_run_trees` as a whole can still block
indefinitely inside the merge. Bounding the provider lock is a change to a
shared writer used by four other call graphs and is out of scope here.

**Not made non-fatal.** A copyback timeout must keep failing the stage.
`resume_cycle_stage` (`chain_stage_execution.py:974`) unconditionally re-enters
`_after_cycle_stage_terminal`, so a failed run-tree copyback is retried on the
next pass; downgrading it would mark the cycle succeeded and convert a loud
recoverable failure into silent data absence.

### Lock mechanics

- **Idiom**: copied from `packages/common/node27_timeseries_lifecycle_lock.py` —
  `O_RDWR|O_NOFOLLOW|O_CLOEXEC`, `O_CREAT|O_EXCL` then fall back to plain open,
  mode `0o600`, and `_require_lock_identity`-style assertions (regular file, not
  a symlink, exactly one hard link, owned by the effective uid, **owned by the
  copyback root's owner**, path/fd `(st_dev, st_ino)` identical) re-checked after
  `fchmod` and after the `flock`. The lock file is never unlinked by this code.
  The root-owner clause is not decoration: comparing the lock file's owner to the
  *current* euid alone only closes the pre-existing-file direction. A foreign-uid
  writer that creates the file first passes its own euid check, and because the
  file is never unlinked it poisons the mutex for the real writers permanently.
  The root is the shared anchor and is owned by the writer account on the live
  export (`/ghdc/data/nwm/object-store`, `0o775 frd_muziyao:huser`; the group is
  gid 1078, which resolves to `huser` on node-22 and `nfsdata` on node-27 — the
  same group under two host-local names, which is why the `getfacl` block below
  spells it differently), so one
  comparison closes both directions. The create branch is additionally refused
  *before* `O_CREAT|O_EXCL` runs, so a rejected foreign writer leaves no orphan
  lock file behind at all.
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
  (`publisher._prepare_copyback_root` `:1466`, `run_tree_copyback.py:57`) → run the
  root's **identity and overlap guards** → open the lock file under it → acquire →
  plan → copy → promote → commit/rollback → release.
  The guards must precede the lock, not just the root preparation. All three lanes
  return `skipped` when the copyback root resolves to the object-store root itself
  (`publisher.py:1293-1294`, the same identity check in `_copyback_qdown_products`
  noted at `:970`, `run_tree_copyback.py:64-70`), and `_prepare_copyback_root`
  deliberately only *verifies* in that case (`:1479`, under the branch at
  `:1478`). Acquiring before the guards
  would create a lock file inside the production object-store root on a path that
  writes nothing.
- **Blocking with a deadline**: contention must wait, not refuse — refusing would
  turn a race into a dropped mirror. Uses `LOCK_EX|LOCK_NB` in a bounded poll loop
  (the `packages/common/evidence_io.acquire_exclusive_flock_until` shape).
  **Default 900 s**, overridable via
  `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS`; the arithmetic behind that
  number is in "Cost accepted, deliberately" above. It stays finite rather than
  unbounded because `_mirror_canonical_precip`
  (`chain_forecast_execution.py:996`) runs inside the scheduler pass rather than
  in a job of its own, so an unbounded wait would sit on the pass — reasoning
  from where the hook executes, not a cited spec requirement. That hook wraps the
  whole publisher call in `except Exception` (`:1046`), so a timeout there is
  recorded as a `failed` `canonical_precip_mirror` receipt and the cycle
  continues past `convert`. **Nothing retries that mirror**: there is no
  next-cycle re-attempt for the cycle that failed, so a `failed` receipt is an
  operator action item, recovered by running
  `scripts/canonical_precip_copyback_backfill.py` (see
  `docs/runbooks/current-production-ops.md` §5.3). Timeout raises a distinct loud
  error and never falls back to an unlocked
  promote — **as each lane's own error type**, because a foreign exception escapes
  two of the three callers: `_copyback_stage_run_trees` catches only
  `RunTreeCopybackError` (`chain_forecast_execution.py:953`) and runs at most once
  per cycle — on `parse`, or on `state_save_qc` under the production terminal
  stage (`:931-934`, and `chain_stages.py:75-79` for why never both) — and
  `publish_qdown_cycle` catches only
  `PublishError | SQLAlchemyError | OSError | ValueError` (`publisher.py:207-210`).
  So: `RunTreeCopybackError` in the run-tree lane; `PublishError` with a distinct
  `OBJECT_STORE_COPYBACK_LOCK_TIMEOUT` code in the q_down/run-products lane; and in
  the canonical lane the acquire sits inside `_copyback_canonical_precip`'s own
  `try:` (`publisher.py:1275`), so its `except Exception` at `:1376` catches
  first and returns the `failed` summary; `_mirror_canonical_precip`'s
  `except Exception` (`chain_forecast_execution.py:1046`) is the outer net, not
  the handler that fires. Same observable receipt either way.
  One asymmetry is deliberate and recorded: the run-tree lane's timeout does *not*
  degrade to a receipt. `_copyback_stage_run_trees` re-raises as
  `_chain.OrchestratorError` (`:971`) from inside the
  `result_status == "succeeded"` branch of `_after_cycle_stage_terminal` (`:859`),
  so the stage's `update_forecast_cycle_status` (`:862`) is skipped. That is the
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
- **Single uid**: the `0o600` + owner assertions mean a writer under a different
  account fails closed with a clear error instead of silently running unlocked —
  in **both** directions, because the owner is compared to the copyback root's
  owner and not only to the current euid, and because the create branch is
  refused before the file exists. All copyback writers are `frd_muziyao` on
  node-22. Recorded as a known limit.
  **What the operator actually sees, which is not uniform.** Only the *create*
  direction names both uids and the path, because only there does this process
  get to stat the situation. In the pre-existing-file direction a non-root
  writer never reaches the ownership assertions at all: the lock file is
  `0o600` and owned by someone else, so the `O_RDWR` reopen after
  `O_CREAT|O_EXCL` fails `EACCES` and surfaces as `cannot acquire copyback
  batch lock <path>: [Errno 13] Permission denied` — correct lane error type,
  correctly distinct from a timeout, but carrying no uid. The ownership
  assertions are reachable there only for euid 0 or under a test that patches
  `os.geteuid`. `Permission denied` on the lock path *is* the foreign-owner
  signal in that direction, and the runbooks say so; the recovery steps
  (`ls -ln <lock>`, `stat -c '%u' <root>`) work regardless.
- **Deadlock ordering: the two locks never nest, in either direction.**
  `merge_state_snapshot_index_copyback` takes `provider_destination_lock`
  (`packages/common/provider_atomic.py:326-341`) with `blocking=True` and **no
  deadline**. Nesting it inside the batch mutex would leave the mutex's own
  *hold* time unbounded — the 900 s deadline bounds a waiter, not a holder —
  so one stalled provider lock becomes head-of-line blocking that fails every
  other copyback writer under the root. `copyback_run_trees` therefore releases
  the batch mutex completely before it runs the state-index merge, and no path
  acquires the batch mutex while holding a provider lock. There is no lock order
  to get wrong because there is no nesting.

### What is deliberately outside the mutex

`packages/common/state_manager.py:2194 merge_state_snapshot_index_copyback` and
`:2405 _copyback_state_checkpoint` write under the copyback root
(`scheduler/state-index/`, `states/`) and are outside the mutex on every path,
including `copyback_run_trees`'s own `extra_object_keys` call: they promote no
directory tree — they are per-file provider-atomic writers with their own lock,
on a subtree disjoint from every tree this mutex protects. The mutex requirement
is scoped to directory-tree promote-and-commit batches for exactly this reason,
and the spec delta carries an explicit exemption scenario. The other
`extra_object_keys` entries are plain `_replace_file` copies and stay inside the
mutex; the split is only as wide as the unbounded lock demands.

Terminal state of the split, recorded because it is the one behaviour question it
raises: if the merge fails, the run trees are already promoted and the mutex
already released — exactly as they were when the merge ran inside the mutex,
because this lane has no batch rollback (`_replace_tree` restores per tree, under
its own guard). `RunTreeCopybackError` propagates to
`chain_forecast_execution.py:946` unchanged, so the caller-observable outcome is
identical.

### `run_tree_copyback`'s different terminal state

`_replace_tree` (`services/orchestrator/run_tree_copyback.py:467-503`) has the
same window but a guarded recovery: `if backup.exists() and not target.exists()`
(`:497`). With a competitor's tree in place that predicate is false, so it does
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
node-27's local ext4 (`/dev/mapper/ubuntu--vg-home`, which backs the shared
export), 2026-09-08, shows that probe would be dead code.** Under a parent
carrying `default:user:X:rwx` / `default:mask::rwx`:

| creation | resulting mask |
|---|---|
| `os.mkdir(path, 0o755)` — what `safe_fs` does | `mask::r-x`, grant `#effective:r-x` |
| mode-less `os.mkdir(path)` — `run_tree_copyback.py:535` | `mask::rwx` preserved |
| the `0o755`-created directory, then `chmod 0755` | `mask::r-x` — **unchanged** |

`safe_fs`'s explicit mode has already performed the clamp before any caller-side
`chmod` runs — stated verbatim by the repo at `run_tree_copyback.py:522-534` and
by the existing capability at
`openspec/specs/filesystem-permission-determinism/spec.md:99-103`. So the
widening is **mask-neutral** under an ACL and **corrective** without one, and one
unconditional rule is both correct and simpler. Recovering that clamped mask is
#1631's open question and an explicit non-goal here.

Live `getfacl` on the shared root, which sets where B actually bites today.
**Read this on node-27**, and only on node-27 — see the NFS caveat below.
Re-collected 2026-09-10T14:50Z at `/home/ghdc/nwm/object-store`:

```
object-store/   no default ACL          (0775 frd_muziyao:nfsdata = gid 1078 = huser on node-22, other::r-x)
runs/           default:user:nwm:rwx, default:mask::rwx
forcing/        default:user:nwm:rwx, default:mask::rwx
states/         default:user:nwm:rwx, default:mask::rwx
canonical/      no ACL at all           (0755, other::r-x)
```

**The same command on node-22 returns a different answer, and node-22 is the
host that actually performs copyback.** `/ghdc/data` there is an NFSv4.2 mount of
`ghdc:/home/ghdc`, and the client does not surface the server's POSIX ACLs:
`getfacl` on all five paths reports only `user::/group::/other::` with no `default:`
entries at all (measured 2026-09-10T14:49Z). An operator who checks ACLs from
node-22 will conclude there are none. Inheritance itself is unaffected, because the
server applies it: a run tree copied back by node-22 at 2026-09-10 02:09 carries the
full `user:nwm:rwx / mask::rwx / default:*` set when read from node-27, one level
down as well. Note also that `ls` shows the ACL *mask* in the group triad, so an
ACL-bearing directory reading `drwxrwxr-x` has `group::r-x` with `mask::rwx`, not
`group::rwx`.

`canonical/` carries no ACL, so mode bits alone decide traversal for node-27's
reader (a different uid, reaching it through `other::r-x`). Under `umask 027`
`other` loses `r-x` and the mirror becomes unreadable. The ACL-bearing lanes are
reachable through their inherited grant regardless of umask, so B does not bite
them today — but the same rule keeps them correct if an ACL is ever removed.

Only levels this call created are touched, so #1513's "never fchmod an existing
path" holds verbatim.

**The copyback root itself is in scope.** Issue #2035's own `umask 027`
measurement lists `0o750 .` — the root — first, and a `0o750` root defeats
traversal regardless of what sits below it. `publisher.py:1485` and
`run_tree_copyback.py:57` are the two sites that create it, and they pass no
`containment_root`. The helper must therefore determine "levels this call created"
by probing upward for missing components **before** creating them (the
`canonical_precip_copyback_backfill._ensure_target_directory` idiom), not by
walking `relative.parts` against a containment root: `_ensure_copyback_state_parent`
(`state_manager.py:2472`) uses the latter, and its parts list is empty when the
path *is* the root — a silent no-op that would never widen it. `containment_root`
stays an optional pass-through to `safe_fs` for symlink containment only.

### Level-by-level creation

Copied from `scripts/canonical_precip_copyback_backfill.py:198-225`
(`_ensure_target_directory`) and from
`state_manager._ensure_copyback_state_parent` (`:2464`): probe upwards for the
missing components, then create and widen them outermost-first, one at a time.
**One guard is deliberately not copied.** The backfill script creates each level
with a bare `path.mkdir()` and can therefore catch `FileExistsError`
(`scripts/canonical_precip_copyback_backfill.py:226-233`) to skip the `chmod`
for a level it lost to a concurrent creator. `safe_fs.ensure_directory_no_follow`
absorbs that signal (`safe_fs.py:91-93`), so this helper cannot distinguish the
lost race and widens the level anyway. Reproducing the guard would mean giving
up `safe_fs`'s `O_NOFOLLOW`/`dir_fd` walk, which is the whole reason to use it.
The residue is recorded as an accepted limit in the helper docstring and in the
`filesystem-permission-determinism` delta. A
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

Line numbers drift as this branch grows, so this inventory cites symbols and
lets the reader grep. The numbers that remain elsewhere in this document are
governed by a rule, not by a pinned SHA: **every line citation in this change's
fixture is re-verified against the branch head in the same commit that touches a
cited file.** Naming a SHA here would go stale on the next commit, which is the
exact failure this rule exists to stop. The one deliberate exception is
`claims-audit.md`, whose row citations are pinned to the audit head and resolve
with `git show <sha>:<path>`.

- Producers: `publisher._copyback_run_products` (**no production caller today**;
  `publish_cycle` delegates to `publish_qdown_cycle`, and every caller is in
  `tests/test_tile_publisher.py`, at six sites. Locked anyway so the two siblings
  cannot diverge), `publisher._copyback_qdown_products`,
  `publisher._copyback_canonical_precip`,
  `services/tile_publisher/forcing_copyback_backfill._copy_package`,
  `scripts/canonical_precip_copyback_backfill._backfill_cycle`,
  `services/orchestrator/run_tree_copyback.copyback_run_trees`
- Validators/preflight: `publisher._plan_canonical_precip_tree`,
  `publisher._verify_copyback_target_tree_clean`,
  `publisher._prepare_copyback_root`
- Storage/cache/query: `LocalObjectStore` under the copyback root; node-27
  display API read path over the same NFS export
- Public routes/entrypoints: `publisher.publish_qdown_cycle`;
  `services/orchestrator/chain_forecast_execution.py:996 _mirror_canonical_precip`
  and its `:1045 publisher.copyback_canonical_precip` call;
  `services/orchestrator/cli.py:113`; both backfill CLIs
- Frontend/downstream consumers: node-27 display API reading `canonical/**` as a
  different uid
- Failure paths/rollback/stale state: `publisher._replace_directory_tree_for_qdown_batch`,
  `publisher._replace_directory_tree_no_follow`,
  `publisher._rollback_qdown_copyback_batch`,
  `publisher._commit_qdown_copyback_batch`, `publisher._restore_copyback_backup`,
  `run_tree_copyback._replace_tree`
- Evidence/audit/readiness: copyback summary payloads (`status`, `trees[].status`,
  `file_count`); the `canonical_precip_mirror` pipeline event
  (`chain_forecast_execution.py:1061`); node-27 traversal receipt

Regression rows:

- publisher batch × publisher batch, competitor injected in the `:2468`→`:2470`
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
- Lock file owned by a uid other than the copyback root's owner → fail closed.
  A non-root writer gets `EACCES` on the reopen (`Permission denied`, path only,
  no uid — see "Single uid" above); euid 0 and the patched-`geteuid` test reach
  the ownership assertion, which names both uids and the path. A foreign uid
  reaching a root with **no** lock file yet is refused before `O_CREAT` — that
  branch always names both uids and the path — so no orphan is left.
- A raised base `CopybackLockError` (not a `CopybackLockTimeout`) → each lane's
  *unsafe* code, distinct from its timeout code: `PublishError`
  `OBJECT_STORE_COPYBACK_LOCK_UNSAFE` in q_down/run-products,
  `RunTreeCopybackError` `OBJECT_STORE_COPYBACK_LOCK_UNSAFE` in the run-tree
  lane, `error_type: CopybackLockError` in the canonical receipt, and
  `copyback_lock_unavailable` in the forcing backfill report.
- State-index merge inside `copyback_run_trees` → runs with the batch mutex
  provably released (a non-blocking `flock` on the lock file succeeds from a
  second fd while the merge runs); every other `extra_object_keys` entry still
  copies inside it.
- Two distinct copyback roots → two lock files, no cross-blocking.
- `umask 027` publisher canonical copyback → the copyback root, `canonical/`,
  `canonical/<S>/`, `canonical/<S>/<cycle>/`, `canonical/<S>/grid/` each land
  `0o755`.
- Copyback root created from scratch under `umask 027` → the root itself is
  `0o755`, not `0o750`.
- Lock timeout in the run-tree lane → `RunTreeCopybackError`, caught by
  `chain_forecast_execution.py:953`; in the q_down lane → `PublishError` with the
  copyback-lock-timeout code, caught by `publisher.py:207-210`.
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
  mode-less `mkdir` (`:535`) still creates `mask::rwx` interiors.
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
  `services/orchestrator/cli.py:113`, both backfill CLIs.
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
