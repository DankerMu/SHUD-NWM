# Design — route retention deletes through the copyback mutex (#2238)

## Reference frame

Line citations to code this change does **not** add name the line at the branch
base, `6fdb2015` (master at the time this fixture was written). Citations to
code this change adds name the line in the branch tree. Historical
measurements are written as plain numbers, never as `path:line` citations.

## D1 — the premise was re-measured, not inherited

Issue #2238 states the production facts; this change re-collected them
independently on node-22 on 2026-09-11, read-only, before any code was written.

- Checkout `/scratch/frd_muziyao/NWM` is at `99bd9d22` and
  `packages/common/copyback_guard.py` does **not** exist there: the #2035 mutex
  is not deployed on node-22 yet. `grep -c copyback_batch_lock
  services/orchestrator/retention.py` is `0` on that checkout as well.
- `infra/env/compute.scheduler-dbfree.env` (untracked, the file the systemd unit
  loads) carries `NHMS_RETENTION_ENABLED=true`, `NHMS_RETENTION_DRY_RUN=false`,
  `NHMS_RETENTION_EXTRA_ROOTS_ENABLED=true`,
  `NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store`,
  `WORKSPACE_ROOT=/scratch/frd_muziyao/nhms-prod/workspace`,
  `OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store`.
- `nhms-compute-scheduler.timer` is `enabled` and `active`.
- 450 scheduler pass receipts are present; 413 carry a `retention` block, and
  every one of those reports `dry_run=False` and `extra_roots.enabled=True`.
- `deleted[]` counted per root: 420 on the primary object store (`raw` 12,
  `canonical` 12, `forcing` 12, `runs` 384), 354 on the workspace root (all
  `runs`), **294 on `/ghdc/data/nwm/object-store`, all `runs`**, spread over six
  passes from 2026-09-08T00:04:45Z to 2026-09-10T12:05:57Z. Sample entry:
  `{"key": "runs/fcst_gfs_2026080900_dg_0a50ecb006879e7336361fc0b6191766",
  "reason": "run_cycle_aged_out", "size_bytes": 1611349}`.

Two preconditions were measured on the same visit, because a review challenged
both and neither is decidable from the tree:

- **Lock ownership.** `copyback_guard` fails closed unless the lock file's owner
  matches the copyback root's owner (`copyback_guard.py:181-188`), so a
  mismatched account would turn *every* copyback-root removal into a `failed[]`
  entry and silently stop reclaiming that root. Measured:
  `/ghdc/data/nwm/object-store` is owned by uid 1103 (`frd_muziyao`), and the
  scheduler unit runs as uid 1103. They match, and no lock file exists there yet.
- **Scheduler lease.** The pass lease TTL is 3600 s
  (`services/orchestrator/scheduler.py:297`), but it is renewed by a daemon
  thread started before retention runs and stopped after it
  (`scheduler_runtime.py:740-741`, `:1505`; `_LeaseHeartbeat` at
  `services/orchestrator/scheduler_lease.py:86-118`, interval `ttl // 3`). A
  blocking acquisition in the main thread therefore does **not** consume the
  lease. The bound this change needs is on pass *duration*, not on the lease —
  see D9.

The read used the pinned active interpreter `/scratch/frd_muziyao/NWM/.venv/bin/python`
(3.12.7) and fails closed if it is missing; no `uv sync`, no bare `uv run`, no
environment rebuild. The helper script was delivered with `ssh 'cat > file'` and
removed afterwards. Nothing was written under any object-store or workspace root.

## D2 — where the lock goes, and why per tree

`run_retention` (`services/orchestrator/retention.py:795-849`) plans first and
then loops over `result.planned` (`retention.py:841-848`), calling
`_delete_entry` (`retention.py:852-888`) once per entry. `_delete_entry` is the single removal
site for every root, so the mutex goes there and nowhere else.

Per tree, not per pass. A whole-pass hold would span `plan_retention`'s
`_dir_size` rglob over every candidate on an NFS mount, and the mutex's budget
is derived for a promote-sized critical section — `copyback_guard.py:55-67`
reasons about ~36 s per acquisition and ~24 queued waiters inside the 900 s
default (`DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS`, `copyback_guard.py:67`). A
retention pass's wall-clock would eat that budget and starve every publisher.
Per-tree is also what the capability already requires of the backfill writers,
for the same starvation reason.

Acquire immediately before the removal call and release immediately after, so
the held window is one `remove_tree_allow_symlinks`.

## D3 — how the copyback root is identified

`run_retention` today receives `runs_only_roots` as an untagged positional
tuple holding `WORKSPACE_ROOT` then the copyback root at both call sites
(`services/orchestrator/cli.py:196-199`,
`services/orchestrator/scheduler_runtime.py:2106-2109`). `_delete_entry` gets
only `containment_root`, which is set for **both** additional roots
(`retention.py:841-848`), so it cannot tell the shared root from the workspace
root.

A new keyword-only `copyback_root` parameter on `run_retention` names it. Both
call sites already hold the value and pass the identical expression they pass
inside `runs_only_roots`.

Matching is done on the **resolved** root string, against `result.extra_roots`,
which is where `_resolve_runs_only_roots`
(`retention.py:473-550`, called from `plan_retention` at `retention.py:693`) has already put the sanitised, `expanduser().resolve()`d,
de-duplicated, overlap-adjudicated roots. Consequences that fall out for free
and are asserted rather than assumed:

- A blank or unset `copyback_root` selects nothing — the normal non-db-free
  deployment, where the whole lane is absent.
- A copyback root that `_resolve_runs_only_roots` dropped for a relative shape
  is not in `result.extra_roots`, and nothing is deleted or locked there: the
  root was never swept.
- A copyback root that lost an overlap adjudication to a *different additional*
  root is likewise absent and unswept.
- A copyback root that resolves to the **same path as the primary object store**
  is a different case, and D3's first draft got it wrong. It is dropped from
  `result.extra_roots` silently by the identity test at `retention.py:535-536`
  (`admitted` seeds with the primary at `retention.py:513-514`, and the drop
  records no `skipped` entry), yet `plan_retention` still collects `runs/` there
  through the primary arm (`retention.py:725`), and those entries take the
  unlocked `shutil.rmtree` branch. Not locking them is nevertheless correct, for
  a reason that has nothing to do with the deletion surface: in that
  configuration **no copyback writer exists at all**.
  `run_tree_copyback.copyback_run_trees` returns
  `status="skipped", reason="copyback_root_matches_object_store_root"`
  (`services/orchestrator/run_tree_copyback.py:64-77`) before it ever acquires,
  and the publisher lanes carry the same `same_root` skip. There is no second
  party to exclude, and acquiring would create a lock file on a root whose only
  user is this process.
- `WORKSPACE_ROOT` and the copyback root resolving to the same path is the
  #1318 silent dedup: one admitted root, which *is* the copyback root, and it is
  locked.
- The primary object-store root is never in `result.extra_roots`, so the
  `shutil.rmtree` branch is never locked. #1615/D6's split stands.

## D4 — what the mutex does and does not guarantee (explicit)

It closes the interleavings that reach **inside** another process's promote-and-
commit critical section, which are the damaging ones:

- Retention `rmtree`-walking one run directory under the root's `runs/` while
  `run_tree_copyback._replace_tree`
  (`services/orchestrator/run_tree_copyback.py:457-491`) renames that same
  inode to its `.backup` name. Without the mutex retention keeps deleting
  through the rename and destroys the writer's rollback material; if the
  writer's promote then fails, it restores from a half-deleted backup.
- Retention deleting between the writer's `os.replace(target, backup)` and its
  `os.replace(temp, target)` (`run_tree_copyback.py:479-481`), which today costs a spurious `result.failed`
  ENOENT entry.

It does **not** close the plan-to-delete window. Retention plans a tree as aged
out, a replay lane promotes that same run directory and commits inside the
mutex, retention then acquires the mutex and removes it. Both operations are
individually correct under their own contracts — the run's cycle really is past
`extra_roots_retention_days`, and the writer really did commit — and closing it
would require either an inode-identity recheck under the lock or a change to the
selection predicate. The predicate is out of scope by the issue's own boundary
("只关删的时候是否持锁"), and the recheck is a separate decision that must be
taken deliberately, not smuggled in here. The spec states the non-guarantee and
Phase 8 routes it as a tracked follow-up.

The same reasoning applies to `size_bytes`: it is measured by `_dir_size` during
planning, outside the mutex, and `result.freed_bytes` accumulates that planned
number. Accounting may therefore be stale by whatever a writer changed between
planning and the removal. That is an accounting imprecision, not a deletion
defect, and it is stated rather than fixed.

## D5 — the exception trap

`CopybackLockError` and `CopybackLockTimeout` are `RuntimeError` subclasses
(`packages/common/copyback_guard.py:74,78`), not `OSError`. `_delete_entry`'s
current `except (OSError, SafeFilesystemError)` would not catch them, and its
own docstring (`retention.py:867-871`) already explains what escaping costs:
`scheduler_runtime.py:2111-2112` collapses the pass receipt to
`{"status": "error"}`, and the `cleanup` CLI wraps nothing, so the sweep aborts
mid-pass. Both violate this module's "failures never abort the pass" contract.

`CopybackLockError` is therefore added to the `except` tuple —
`CopybackLockTimeout` is a subclass and is covered by it, but the tests assert
each direction separately so a future re-parenting cannot silently un-cover one.

The failure is recorded the same way every other removal failure is: one entry
appended to `result.failed` carrying the planned entry plus `error`. It does not
go into `result.deleted` and does not add to `freed_bytes`.

## D6 — the lock file is not in retention's deletion surface

`acquire_copyback_batch_lock` creates the fixed-name lock file directly under
the copyback root (`copyback_guard.py:86-89`) and never unlinks it. Retention
cannot select it: additional roots enumerate only the directories one level
under the root's own `runs/` via `_collect_run_targets` (`retention.py:373-421`)
and `_iter_dirs` (`retention.py:424-429`), which keeps directories only, and the
primary root enumerates the three cycle-scoped prefixes two levels down
(`retention.py:317`, `CYCLE_SCOPED_PREFIXES` at `retention.py:57`) **and** its
own `runs/` one level down, because `plan_retention` calls both
`_collect_cycle_targets` and `_collect_run_targets` on the primary root
(`retention.py:724-725`). No root-level file is ever
enumerated on either path. This is the same fact #2035's claims audit recorded
as HOLDS; it is re-asserted here by test rather than inherited as prose.

## D7 — why the delta is `ADDED`, not `MODIFIED`

`openspec/specs/object-store-copyback-mutual-exclusion/` does not exist.
The capability is introduced by the still-unarchived
`harden-copyback-batch-mutex-and-dir-traversal` change, so at archive time the
target spec may or may not be there yet. openspec refuses `MODIFIED` against a
non-existent spec ("target spec does not exist; only ADDED requirements are
allowed for new specs") and refuses an `ADDED` whose requirement name already
exists. `ADDED` with a name distinct from #2035's therefore applies correctly in
**either** archive order, and neither change's requirement is at risk of the
wholesale-replacement semantics `MODIFIED` carries.

No edit is made to the #2035 change directory, and none is owed. Its
`design.md:535-546` already reclassifies `retention.py` from consumer to
"Unchanged **writer** that this change does not bring into the protocol", says
outright that the consumer listing "was wrong and is the direct cause of it
being missed", and routes the gap to #2238. Issue #2238's sixth acceptance
criterion — that the wording be corrected — is therefore satisfied upstream at
base `6fdb2015` and is recorded as such rather than re-done here. Editing
another issue's live change directory would in any case be a cross-workflow
write that lands nowhere permanent if that change is archived by its own session
first.

## D8 — verification route

Local. Every claim is a filesystem/exception-handling assertion reachable from
`uv run pytest`, and the module has no DB or display surface. node-27 is not the
oracle for this change; node-22 is where the *deployment* receipt will be taken,
and that is post-merge ops, recorded as a known limit and routed, not claimed
here (see `tasks.md` Evidence Floor EF-17).

That clause needed sharpening, and the reason is worth recording because the
obvious sharpening does not work. As first written, EF-17 asked only that a
post-deploy pass still delete under the copyback root and still report
`completed` — criteria a build with the mutex deleted satisfies byte for byte,
and which D1's own measurements show the *current* mutex-less node-22 build
already satisfying. The tempting fix is to require the batch lock file to appear
under the copyback root, since the guard creates it and never unlinks it. That
is not attributable: the deploy that brings retention's acquirer to node-22 is
the same deploy that brings #2035's **writers**, and copyback promotion runs
earlier in the pass than retention, so the file's presence only proves that some
participant in the protocol ran. What discriminates is a contended probe — hold
the lock from a second process across one pass and require the copyback root's
entries to land in `failed[]` with the lock error while the workspace and
primary roots still land in `deleted[]`. A mutex-less build deletes straight
through a held lock, so that observation separates the two builds. EF-17 now
carries both halves: the non-regression check, labelled as such, and the probe.

## D11 — verification routed locally, and why that is a divergence

`CLAUDE.md`'s oracle table routes "后端单测/集成" to node-27 as a whole row. This
change verifies locally instead, and that is a recorded divergence rather than an
oversight. The change has no DB surface, no display surface and no API surface:
every assertion is a filesystem or exception-handling fact about `flock` and
`rmtree`, which node-27 would exercise no more faithfully than the local
machine — and node-27 is not where this code runs. The node it runs on is
node-22, which is where the deployment receipt is owed (EF-17) and which is
pre-maintenance-window, so it cannot produce that receipt from this branch. The
divergence is therefore "the oracle for this change is node-22, post-deploy, not
node-27", and the cost is recorded in Known limits: `flock` semantics are
exercised on local APFS/ext4, not on the production NFSv4.2 export.

## D9 — the pass-level lock-wait budget

Per-tree acquisition bounds how long any one promote can be blocked by
retention. It does not bound the reverse: how long retention can be blocked in
total. With the guard's 900 s default and the measured 48-54 copyback removals
per pass, a single stuck holder stalls one pass for up to about 13.5 h, which
exceeds the 12 h pass cadence. The stuck case is not hypothetical —
`copyback_guard.py:212-219` documents the NFS state in which the lock is
correctly owned, has no local holder, and is still held until the server's lease
expires, and says the only correct response is to wait it out.

So the pass carries one budget for **acquisition wait only**, defaulting to
300 s and overridable by keyword for tests. Each removal is given what is left of
it as its `timeout_seconds`; when nothing is left, the entry is recorded as a
failure without an acquisition attempt. Only the acquisition is charged, never
the removal — the `rmtree` runs after the charge is closed — so a large
uncontended tree cannot consume the budget. "Acquisition", not "wait": the
charged span covers the guard's own identity syscalls as well as the blocking
poll, which is the honest description of what the clock measures.

Two deliberate choices:

- **No new environment variable**, and the consequence stated plainly rather
  than waved at. An earlier draft of this bullet justified the choice by saying
  the guard's existing `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` already
  answers every operational question the new knob would. **That was false.**
  `resolve_copyback_lock_timeout_seconds` (`copyback_guard.py:102-119`) reads the
  environment only when the caller passes no explicit timeout, and this lane
  always passes one — the remaining pass budget. Every other production acquirer
  (`publisher.py:747`, `:1308`, `run_tree_copyback.py:247`,
  `forcing_copyback_backfill.py:760`) passes none and does honour the override;
  retention is the one that does not. The choice stands anyway: the state this
  budget exists for is a holder that will not release until an NFS server lease
  expires, and no deadline an operator can set reclaims anything in that state —
  a longer deadline only stalls the pass further. So the budget is a module
  constant plus a test-only keyword, and the absence of a production knob is
  recorded in Known limits instead of being explained away.
- **`acquire_copyback_batch_lock` / `release_copyback_batch_lock` directly**,
  not the `copyback_batch_lock` context manager, because the elapsed acquisition
  time has to be measured between those two calls to be charged. The release is
  in a `finally`.

The lease is explicitly *not* the constraint this budget serves — the heartbeat
measured in D1 renews it throughout. The constraint is pass duration against a
12-hourly cadence.

## D10 — what this change does not make true of the whole export

The spec requirement is written as an obligation on any process removing a
directory tree under the shared copyback root, because that is the invariant the
capability needs. `scripts/node27_raw_retention.py:553` is a second such remover,
on node-27, on the same NFS export, and it is out of scope by issue #2238's own
boundary. The requirement therefore carries an explicit clause naming it as a
known-violating implementation with its own tracking, exactly as #2035's
requirement does for the `forcing_copyback_backfill` skip path and #2236. The
alternative — narrowing the obligation to "the scheduler pass's retention" — was
rejected: it would make the spec true by construction and leave the real gap
unrecorded.
