# Design — route retention deletes through the copyback mutex (#2238)

## Reference frame

One frame, with symbol anchors: every `path:line` here names a line in this
branch's final tree and carries the symbol it points into
(`retention._collect_run_targets:415-463`, not a bare `retention._collect_run_targets:415-463`).
Historical measurements are written as plain numbers, never as citations. The
two-frame rule this replaces, and why it could not work for files this change
edits, is recorded in `tasks.md`'s own reference-frame note.

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
  matches the copyback root's owner (`copyback_guard._require_lock_identity:213-221`), so a
  mismatched account would turn *every* copyback-root removal into a `failed[]`
  entry and silently stop reclaiming that root. Measured:
  `/ghdc/data/nwm/object-store` is owned by uid 1103 (`frd_muziyao`), and the
  scheduler unit runs as uid 1103. They match, and no lock file exists there yet.
- **Scheduler lease.** The pass lease TTL is 3600 s
  (`scheduler.module:297`), but it is renewed by a daemon
  thread started before retention runs and stopped after it
  (`scheduler_runtime.run_once:740-741`, `:1505`; `_LeaseHeartbeat` at
  `scheduler_lease._LeaseHeartbeat:86-118`, interval `ttl // 3`). A
  blocking acquisition in the main thread therefore does **not** consume the
  lease. The bound this change needs is on pass *duration*, not on the lease —
  see D9.

The read used the pinned active interpreter `/scratch/frd_muziyao/NWM/.venv/bin/python`
(3.12.7) and fails closed if it is missing; no `uv sync`, no bare `uv run`, no
environment rebuild. The helper script was delivered with `ssh 'cat > file'` and
removed afterwards. Nothing was written under any object-store or workspace root.

## D2 — where the lock goes, and why per tree

`run_retention` (`retention.run_retention:855-940`) plans first and
then loops over `result.planned` (`retention._CopybackLockBudget:838-852`), calling
`_delete_entry` (`retention._delete_entry:978-1032`) once per entry. `_delete_entry` is the single removal
site for every root, so the mutex goes there and nowhere else.

Per tree, not per pass. A whole-pass hold would span `plan_retention`'s
`_dir_size` rglob over every candidate on an NFS mount, and the mutex's budget
is derived for a promote-sized critical section — `copyback_guard.module:54`
reasons about ~36 s per acquisition and ~24 queued waiters inside the 900 s
default (`DEFAULT_COPYBACK_LOCK_TIMEOUT_SECONDS`, `copyback_guard.module:54`). A
whole-pass hold would therefore be charged against a deadline sized for a
promote, not for a sweep. How much of it a real pass would consume is not
measured here, and the argument does not need it: per-tree holds make the
question moot.
Per-tree is also what the capability already requires of the backfill writers,
for the same starvation reason.

Acquire immediately before the removal call and release immediately after, so
the held window is one `remove_tree_allow_symlinks`.

## D3 — how the copyback root is identified

`run_retention` today receives `runs_only_roots` as an untagged positional
tuple holding `WORKSPACE_ROOT` then the copyback root at both call sites
(`cli._run_cleanup:196-199`,
`scheduler_runtime._run_retention:2106-2109`). `_delete_entry` gets
only `containment_root`, which is set for **both** additional roots
(`retention._CopybackLockBudget:838-852`), so it cannot tell the shared root from the workspace
root.

A new keyword-only `copyback_root` parameter on `run_retention` names it. Both
call sites already hold the value and pass the identical expression they pass
inside `runs_only_roots`.

Matching is done on the **resolved** root string, against `result.extra_roots`,
which is where `_resolve_runs_only_roots`
(`retention._resolve_runs_only_roots:515-592`, called from `plan_retention` at `retention.plan_retention:735`) has already put the sanitised, `expanduser().resolve()`d,
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
  `result.extra_roots` silently by the resolved-path dedup against the primary at
  `retention._resolve_runs_only_roots:515-592`
  (`admitted` seeds with the primary at `retention._resolve_runs_only_roots:515-592`, and the drop
  records no `skipped` entry), yet `plan_retention` still collects `runs/` there
  through the primary arm (`retention.plan_retention:735`), and those entries take the
  unlocked `shutil.rmtree` branch. Not locking them is nevertheless correct, for
  a reason that has nothing to do with the deletion surface: in that
  configuration the entry is not a member of `result.extra_roots`, and a root
  that is not a member is not locked. That is the whole rule this function
  applies, and it is the only thing asserted here.

  Whether a writer could nonetheless hold this mutex on a root that is also some
  process's primary object store is a property of the **writers**, not of the
  deleter. Four earlier drafts of this bullet answered it with an enumeration of
  the six writer lanes and a universal — "no writer ever acquires there" — and
  that universal was wrong: one lane,
  `scripts/canonical_precip_copyback_backfill.py`, decides the same-root case by
  comparing two operator-supplied arguments and reads no object-store root at
  all, so its refusal is a property of how it was invoked. The enumeration is
  deleted rather than re-qualified, because re-qualifying it is what rounds 2, 3
  and 4 each tried. **Issue #2252 owns the writer-side question**, and it has to
  settle a prior one first: node-27 is the NFS server for this export and
  node-22 its client, so whether a server-local `flock` excludes a client-side
  one is not established anywhere in this repository.

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
  (`run_tree_copyback._replace_tree:457-491`) renames that same
  inode to its `.backup` name. Without the mutex retention keeps deleting
  through the rename and destroys the writer's rollback material; if the
  writer's promote then fails, it restores from a half-deleted backup.
- Retention deleting between the writer's `os.replace(target, backup)` and its
  `os.replace(temp, target)` (`run_tree_copyback._replace_tree:457-491`), which today costs a spurious `result.failed`
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
own docstring (`retention._delete_entry:978-1032`) already explains what escaping costs:
`scheduler_runtime._run_retention:2106-2109` collapses the pass receipt to
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
the copyback root (`copyback_guard.module:54`) and never unlinks it. Retention
cannot select it: additional roots enumerate only the directories one level
under the root's own `runs/` via `_collect_run_targets` (`retention._collect_run_targets:415-463`)
and `_iter_dirs` (`retention._iter_dirs:466-471`), which keeps directories only, and the
primary root enumerates the three cycle-scoped prefixes two levels down
(`retention._collect_cycle_targets:351-374`, `CYCLE_SCOPED_PREFIXES` at `retention.module:71`) **and** its
own `runs/` one level down, because `plan_retention` calls both
`_collect_cycle_targets` and `_collect_run_targets` on the primary root
(`retention.plan_retention:735`). No root-level file is ever
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
`design.md:535-546` (the bullet opening "Unchanged *writer* that this change
does not bring into the protocol") already reclassifies `retention.py` from consumer to
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

The probe has two costs that belong here rather than in a footnote. It is
**intrusive**: holding the lock from a second process across a pass blocks every
#2035 copyback writer on that root for the same window, not just retention, so
it is run in a window where no promotion is owed and not during a live forecast
cycle. And its observable is **erasable**: the failure entries the probe reads
carry their `error` text only in an uncompacted receipt —
`scheduler_evidence_payload._compact_retention` (`:767-802`) replaces
`planned`/`deleted`/`skipped`/`failed` with `*_count` scalars under
`pre_write_size_pressure`, which drops the per-entry error text the probe is
looking for. The receipt is therefore read for the lists, and a compacted
receipt is a void run of the probe, not a failed one.

## D9 — the pass-level lock-wait budget

Per-tree acquisition bounds how long any one promote can be blocked by
retention. It does not bound the reverse: how long retention can be blocked in
total. With the guard's 900 s default and the measured 48-54 copyback removals
per pass, a holder that outlasts every one of those individual deadlines stalls
one pass for up to about 13.5 h, which exceeds the 12 h pass cadence. That
figure needs a holder wedged indefinitely — it is not what the finite NFS
lease-expiry hold produces, and the two are kept apart in the first bullet
below. Neither state has been observed in production here; the figure is an
arithmetic bound on the existing 900 s default, not a report.

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
  `resolve_copyback_lock_timeout_seconds` (`copyback_guard.resolve_copyback_lock_timeout_seconds:124-155`) reads the
  environment only when the caller passes no explicit timeout, and this lane
  always passes one — the remaining pass budget. All five other production
  acquire sites (`publisher.TilePublisher._copyback_batch_mutex:747`, `:1308`, `run_tree_copyback._run_tree_batch_lock:247`,
  `forcing_copyback_backfill._copy_package:760`,
  `canonical_precip_copyback_backfill._mirror_tree_under_batch_lock:467`; D3 maps them to the six
  writer lanes) pass none and do honour the override; retention is the one that
  does not.

  The choice stands anyway, but the earlier draft's reason for it was wrong too.
  Two different stuck states hide behind "a holder that does not release". A
  live hung holder is **unbounded**: no deadline an operator sets reclaims
  anything there, and a longer one only stalls the pass further. The NFS
  lease-expiry hold is **finite** — the server does drop it, and waiting it out
  is the response `copyback_guard` itself prescribes — so 300 s may simply be
  shorter than that lease. This change does not know that lease: the export's
  server-side setting was not read, and guessing it would be the same class of
  error this fixture keeps correcting. What the budget does in that case is
  stated instead of sized: the first blocked tree waits out the budget, the rest
  are refused before acquiring, all of them land in `failed[]`, and the next
  pass retries — deferred reclamation, not lost reclamation. The 13.5 h figure
  above needs the *first* state, not this one: 48-54 acquisitions only
  accumulate 900 s each if every one of them times out. So the budget is a
  module constant plus a test-only keyword, and the absence of a production knob
  is recorded in Known limits instead of being explained away.
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
capability needs. `node27_raw_retention.run_retention:553` is a second such remover,
on node-27, on the same NFS export, and it is out of scope by issue #2238's own
boundary. The requirement therefore carries an explicit clause naming it as a
known-violating implementation with its own tracking — issue #2252 — exactly as
#2035's requirement does for the `forcing_copyback_backfill` skip path and
#2236. The
alternative — narrowing the obligation to "the scheduler pass's retention" — was
rejected: it would make the spec true by construction and leave the real gap
unrecorded.

## D11 — verification routed locally, and why that is a divergence

`CLAUDE.md`'s oracle table routes "后端单测/集成" to node-27 as a whole row. This
change verifies locally instead, and that is a recorded divergence rather than an
oversight. The change has no DB, display or API surface, so the reason that row
exists — a real Postgres and a real display API — does not apply here.

That much was never in dispute. What was, and what had to be measured, is
whether node-27 would exercise `flock` on the *production* filesystem where the
local Mac cannot. An earlier draft asserted it would not, a round-2 review
asserted it would (on the grounds that node-27 mounts the same NFSv4.2 export),
and **both were wrong about the topology**. Measured read-only on 2026-09-11:

- `hostname` on node-27 is `ghdc`, and it is the **server** of this export:
  `/etc/exports` publishes `/home/ghdc` and `nfsd` is running. (It is also a
  client of unrelated mounts — `stor:` serves it `/data/SpatialData` and
  `/data/ForcingData` over NFSv3 — which is why the claim is scoped to this
  export rather than to the host.) `/home/ghdc/nwm` is local ext4 on
  `/dev/mapper/ubuntu--vg-home`
  (`stat -f` reports `ext2/ext3`), and node-22 mounts exactly that directory as
  `/ghdc/data/nwm` — which is what `copyback_guard.acquire_copyback_batch_lock:244-251`'s
  "NFSv4.2 mount of `ghdc:/home/ghdc`" names.
- `/home/nwm/tmp`, the `TMPDIR` the repo's node-27 pytest discipline mandates,
  is on the same local ext4 volume.

So node-27 has no NFS-client side to exercise: a suite run there takes `flock`
on local ext4, the local Mac takes it on local APFS, and neither reaches the
client semantics that make this mutex interesting. The only host that sees them
is node-22, which is where the deployment receipt is owed (EF-17) and which is
pre-maintenance-window, so it cannot produce that receipt from this branch.

The divergence is therefore "the oracle for this change is node-22, post-deploy,
not node-27", and the cost — `flock` exercised on a local filesystem, not
through an NFSv4.2 client, until that receipt is taken — is recorded in Known
limits rather than argued away. The same measurement is why issue #2252 leads
with an interoperability question rather than with a patch: node-27's own
deleter would be taking a server-local lock against node-22's client-side one,
and whether those two exclude each other is not something this change
established.
