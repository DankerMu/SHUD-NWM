# Design — route retention deletes through the copyback mutex (#2238)

## Reference convention

Code is named by symbol (`retention._delete_entry`,
`copyback_guard.resolve_copyback_lock_timeout_seconds`), never by line number.
Historical measurements are plain numbers, never citations. The convention is
enforced by one grep over this change's documents, which must return nothing:

```
grep -rnE '[`A-Za-z_)]:[0-9]+|(#L|::|:L)[0-9]+|line [0-9]+|第 ?[0-9]+ ?行' openspec/changes/route-retention-deletes-through-copyback-mutex
```

What that buys is bounded, and the bound is named rather than gestured at. The
pattern covers `sym:n`, `#L`, `::`, `:L` and singular lower-case `line N`. It
does **not** catch `第 29/42 行` (the tail cannot span the `/` — and that is the
spelling issue #2238's own body uses), `lines 40-52` (plural), or `Line 42`
(`grep -E` is case-sensitive); it would also fire on an ordinary URL carrying a
port, though none appears here. So the claim is "no line-number citation is
present, in any form this pattern covers" — not "a line number cannot be
written", which would be the self-certifying shape this change exists to
remove.

## D1 — the premise was re-measured, not inherited

Issue #2238 states the production facts; this change re-collected them
independently on node-22 on 2026-09-11, read-only, before any code was written.

- Checkout `/scratch/frd_muziyao/NWM` is at `99bd9d22` and
  `packages/common/copyback_guard.py` does **not** exist there: the #2035 mutex
  is not deployed on node-22 yet.
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
  `runs`), **294 on `/ghdc/data/nwm/object-store`, all `runs`**, over six passes
  from 2026-09-08T00:04:45Z to 2026-09-10T12:05:57Z. Sample entry:
  `{"key": "runs/fcst_gfs_2026080900_dg_0a50ecb006879e7336361fc0b6191766",
  "reason": "run_cycle_aged_out", "size_bytes": 1611349}`.

Two preconditions were measured on the same visit, because neither is decidable
from the tree:

- **Lock ownership.** `copyback_guard._require_lock_identity` fails closed unless
  the lock file's owner matches the copyback root's owner, so a mismatched
  account would turn *every* copyback-root removal into a `failed[]` entry and
  silently stop reclaiming that root. Measured:
  `/ghdc/data/nwm/object-store` is owned by uid 1103 (`frd_muziyao`), the
  scheduler unit runs as uid 1103, and no lock file exists there yet.
- **Scheduler lease.** The pass lease TTL is 3600 s, but
  `scheduler_lease._LeaseHeartbeat` renews it from a daemon thread started
  before retention runs and stopped after it (interval `ttl // 3`). A blocking
  acquisition in the main thread therefore does **not** consume the lease. The
  bound this change needs is on pass *duration*, not on the lease — see D9.

The read used the pinned active interpreter
`/scratch/frd_muziyao/NWM/.venv/bin/python` (3.12.7) and fails closed if it is
missing; no `uv sync`, no bare `uv run`, no environment rebuild. The helper
script was delivered with `ssh 'cat > file'` and removed afterwards. Nothing was
written under any object-store or workspace root.

## D2 — where the lock goes, and why per tree

`run_retention` plans first and then loops over `result.planned`, calling
`retention._delete_entry` once per entry. `_delete_entry` is the single removal
site for every root, so the mutex goes there and nowhere else.

Per tree, not per pass. A whole-pass hold would span `plan_retention`'s
`_dir_size` rglob over every candidate on an NFS mount, and would be charged
against a deadline the guard derives for a promote-sized critical section
(~36 s per acquisition, ~24 queued waiters inside the 900 s default). Per-tree
holds make the question moot, and per-tree is what the capability already
requires of the backfill writers, for the same starvation reason.

Acquire immediately before the removal call and release immediately after, so
the held window is one `remove_tree_allow_symlinks`.

## D3 — how the copyback root is identified

`run_retention` receives `runs_only_roots` as an untagged positional tuple
holding `WORKSPACE_ROOT` then the copyback root at both call sites.
`_delete_entry` gets only `containment_root`, which is set for **both**
additional roots, so it cannot tell the shared root from the workspace root. A
new keyword-only `copyback_root` parameter on `run_retention` names it; both
call sites already hold the value.

Matching is on the **resolved** root string, against `result.extra_roots` —
where `retention._resolve_runs_only_roots` has already put the sanitised,
`expanduser().resolve()`d, de-duplicated, overlap-adjudicated roots. The rule is
membership, and nothing else. Consequences, each asserted by an Evidence Floor
clause rather than assumed:

- A blank or unset `copyback_root` selects nothing — the normal non-db-free
  deployment, where the whole lane is absent (EF-7).
- A copyback root that `_resolve_runs_only_roots` dropped for a relative shape,
  or that lost an overlap adjudication to a *different additional* root, is not
  in `result.extra_roots`: the copyback lane neither locks it nor removes
  anything under it (EF-6). That is what membership buys, and it is all it
  buys. Overlap rejection fires precisely when the two roots' potential target
  trees intersect, so in that geometry the *winning* root's own `runs/` sweep
  can still reach the rejected path, unlocked — pre-existing #1617 shape,
  neither created nor widened here.
- A copyback root that resolves to the **same path as the primary object store**
  is dropped from `result.extra_roots` by the resolved-path dedup against the
  primary, recording no `skipped` entry. `plan_retention` still collects `runs/`
  there through the primary arm, and those entries take the unlocked
  `shutil.rmtree` branch. Not a member, so not locked (EF-5).

  Whether a writer could nonetheless hold this mutex on a root that is also some
  process's primary object store is a property of the **writers**, not of the
  deleter, and it is not settled here. No universal about the writer lanes is
  asserted: at least one of them,
  `scripts/canonical_precip_copyback_backfill.py`, decides the same-root case by
  comparing two operator-supplied arguments and reads no object-store root at
  all, so its refusal is a property of how it was invoked. Nothing in this
  change depends on the answer — membership decides the deleter's behaviour
  either way.
- `WORKSPACE_ROOT` and the copyback root resolving to the same path is the
  #1318 silent dedup: one admitted root, which *is* the copyback root, and it is
  locked (EF-4).
- The primary object-store root is never in `result.extra_roots`, so the
  `shutil.rmtree` branch is never locked. #1615/D6's split stands (EF-3).

## D4 — what the mutex does and does not guarantee

It closes interleavings that reach **inside** another process's
promote-and-commit critical section, which are the damaging ones. Two of them,
offered as instances rather than as an exhaustive list:

- Retention `rmtree`-walking one run directory under the root's `runs/` while
  `run_tree_copyback._replace_tree` renames that same inode to its `.backup`
  name. Without the mutex retention keeps deleting through the rename and
  destroys the writer's rollback material; if the writer's promote then fails,
  it restores from a half-deleted backup.
- Retention deleting between the writer's `os.replace(target, backup)` and its
  `os.replace(temp, target)`, which today costs a spurious `result.failed`
  ENOENT entry.

It does **not** close the plan-to-delete window: retention plans a tree as aged
out, a replay lane promotes that same run directory and commits inside the
mutex, retention then acquires the mutex and removes it. Both operations are
individually correct under their own contracts — the run's cycle really is past
`extra_roots_retention_days`, and the writer really did commit. Closing it would
require either an inode-identity recheck under the lock or a change to the
selection predicate; the predicate is out of scope by the issue's own boundary
("只关删的时候是否持锁"), and the recheck is a separate decision. The spec states
the non-guarantee and Phase 8 routes it as a tracked follow-up.

The same reasoning applies to `size_bytes`: measured by `_dir_size` during
planning, outside the mutex, and `result.freed_bytes` accumulates that planned
number. Accounting may therefore be stale by whatever a writer changed between
planning and the removal. An accounting imprecision, stated rather than fixed.

## D5 — the exception trap

`CopybackLockError` and `CopybackLockTimeout` are `RuntimeError` subclasses, not
`OSError`. `_delete_entry`'s current `except (OSError, SafeFilesystemError)`
would not catch them, and an escape costs the pass:
`scheduler_runtime._run_retention` collapses the receipt to
`{"status": "error"}`, and the `cleanup` CLI wraps nothing, so the sweep aborts
mid-pass. Both violate this module's "failures never abort the pass" contract.

`CopybackLockError` is therefore added to the `except` tuple.
`CopybackLockTimeout` is covered as a subclass, but the tests assert each
direction separately so a future re-parenting cannot silently un-cover one
(EF-9, EF-10).

The failure is recorded the way every other removal failure is: one entry
appended to `result.failed` carrying the planned entry plus `error`. It does not
go into `result.deleted` and does not add to `freed_bytes`.

## D6 — the lock file is not in retention's deletion surface

`acquire_copyback_batch_lock` creates the fixed-name lock file directly under
the copyback root and never unlinks it. Retention cannot select it: additional
roots enumerate only the directories one level under the root's own `runs/`
(`retention._collect_run_targets` via `retention._iter_dirs`, which keeps
directories only), and the primary root enumerates the three cycle-scoped
prefixes two levels down (`retention._collect_cycle_targets`) **and** its own
`runs/` one level down. No root-level file is ever enumerated on either path.
Re-asserted by test (EF-14) rather than inherited from #2035's claims audit.

## D7 — why the delta is `ADDED`, not `MODIFIED`

`openspec/specs/object-store-copyback-mutual-exclusion/` does not exist. The
capability is introduced by the still-unarchived
`harden-copyback-batch-mutex-and-dir-traversal` change, so at archive time the
target spec may or may not be there yet. openspec refuses `MODIFIED` against a
non-existent spec and refuses an `ADDED` whose requirement name already exists.
`ADDED` with a name distinct from #2035's therefore applies correctly in
**either** archive order, and neither change's requirement is at risk of the
wholesale-replacement semantics `MODIFIED` carries.

No edit is made to the #2035 change directory, and none is owed: its `design.md`
at base `6fdb2015` already reclassifies `retention.py` from consumer to
"Unchanged **writer** that this change does not bring into the protocol", says
outright that the consumer listing was the direct cause of the miss, and routes
the gap to #2238. Issue #2238's documentation-correction criterion is satisfied
upstream and recorded as such.

## D8 — verification route, and what the deployment receipt must discriminate

Local. Every claim is a filesystem/exception-handling assertion reachable from
`uv run pytest`, and the module has no DB or display surface. node-22 is where
the *deployment* receipt is owed; that is post-merge ops, recorded as a known
limit (EF-17), not claimed here.

EF-17's shape is a decision, not a formality. A post-deploy pass that still
deletes under the copyback root and still reports `completed` is satisfied byte
for byte by a build with the mutex removed — D1's measurements show the current
mutex-less node-22 build already satisfying it. The batch lock file's presence
does not discriminate either: the deploy that brings retention's acquirer is the
same deploy that brings #2035's **writers**, and copyback promotion runs earlier
in the pass, so the file only proves that some participant ran. What
discriminates is a **contended probe** — hold the lock from a second process
across one pass and require the copyback root's entries to land in `failed[]`
with the lock error while the workspace and primary roots still land in
`deleted[]`. A mutex-less build deletes straight through a held lock.

The probe has two costs. It is **intrusive**: it blocks every #2035 copyback
writer on that root for the window it holds the lock, so it runs when no
promotion is owed and never during a live forecast cycle. And its observable is
**erasable**: `scheduler_evidence_payload._compact_retention` replaces
`planned`/`deleted`/`skipped`/`failed` with `*_count` scalars under
`pre_write_size_pressure`, dropping the per-entry error text the probe reads. A
compacted receipt is a void run of the probe, not a failed one.

## D9 — the pass-level lock-wait budget

Per-tree acquisition keeps each hold to one tree's removal rather than a whole
sweep. It does **not** bound the wait a promoting writer may see: that writer
can still queue behind however many consecutive single-tree holds the sweep
takes. The spec delta says exactly that. Nothing bounds a single hold either —
`safe_fs.remove_tree_allow_symlinks` takes no deadline — which is recorded in
Known limits, not in the spec. What follows is about the other direction:
retention's own aggregate wait across one pass.

With the guard's 900 s default and the measured 48-54 copyback removals per
pass, a holder that outlasts every one of
those individual deadlines stalls one pass for up to about 13.5 h, which exceeds
the 12 h pass cadence. That figure is an arithmetic bound on the existing
default, not a report: neither stuck state below has been observed here.

So the pass carries one budget for **acquisition wait only**, defaulting to
300 s and overridable by keyword for tests. Each removal is given what is left of
it as its `timeout_seconds`; when nothing is left, the entry is recorded as a
failure without an acquisition attempt. Only the acquisition is charged, never
the removal — the `rmtree` runs after the charge is closed — so a large
uncontended tree cannot consume the budget (EF-11). "Acquisition", not "wait":
the charged span covers the guard's own identity syscalls as well as the
blocking poll, which is the honest description of what the clock measures.

Two deliberate choices:

- **No new environment variable**, with the consequence stated rather than
  waved at. `copyback_guard.resolve_copyback_lock_timeout_seconds` reads
  `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` only when the caller passes
  no explicit timeout, and this lane always passes one — the remaining pass
  budget. Every other production acquire site passes none and does honour the
  override; retention is the one that does not. The choice stands because the
  two stuck states behind "a holder that does not release" want opposite
  deadlines. A live hung holder is **unbounded**: no deadline reclaims anything
  there, and a longer one only stalls the pass further. The NFS lease-expiry
  hold is **finite** — the server does drop it, and waiting it out is the
  response `copyback_guard` prescribes — so 300 s may simply be shorter than
  that lease, and this change does not know it: the export's server-side setting
  was not read. What the budget does in that case is stated instead of sized —
  the first blocked tree waits out the budget, the rest are refused before
  acquiring, all land in `failed[]`, and the next pass retries. Deferred
  reclamation, not lost reclamation. The 13.5 h figure needs the *first* state,
  not this one. The budget is therefore a module constant plus a test-only
  keyword, and the absence of a production knob is a recorded known limit.
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
capability needs. `node27_raw_retention.run_retention` is a second such remover,
on node-27, on the same NFS export, out of scope by issue #2238's own boundary.
The requirement therefore carries an explicit clause naming it as a
known-violating implementation with its own tracking — issue #2252 — exactly as
#2035's requirement does for the `forcing_copyback_backfill` skip path and
#2236. Narrowing the obligation to "the scheduler pass's retention" was
rejected: it would make the spec true by construction and leave the real gap
unrecorded.

## D11 — the oracle for this change is node-22, post-deploy

`CLAUDE.md`'s oracle table routes "后端单测/集成" to node-27 as a whole row. This
change verifies locally instead, and that is a recorded divergence. The change
has no DB, display or API surface, so the reason that row exists does not apply.

The remaining question was whether node-27 would exercise `flock` on the
*production* filesystem where the local Mac cannot. Measured read-only on
2026-09-11:

- `hostname` on node-27 is `ghdc`, and it is the **server** of this export:
  `/etc/exports` publishes `/home/ghdc` and `nfsd` is running. (It is also a
  client of unrelated mounts — `stor:` serves it `/data/SpatialData` and
  `/data/ForcingData` over NFSv3 — which is why this is scoped to the export,
  not to the host.) `/home/ghdc/nwm` is local ext4 on
  `/dev/mapper/ubuntu--vg-home` (`stat -f` reports `ext2/ext3`), and node-22
  mounts exactly that directory as `/ghdc/data/nwm`.
- `/home/nwm/tmp`, the `TMPDIR` the repo's node-27 pytest discipline mandates,
  is on the same local ext4 volume.

So node-27 has no NFS-client side to exercise: a suite run there takes `flock`
on local ext4, the local Mac takes it on local APFS, and neither reaches the
client semantics that make this mutex interesting. The only host that sees them
is node-22, which is where the deployment receipt is owed (EF-17) and which is
pre-maintenance-window, so it cannot produce that receipt from this branch.

The divergence is "the oracle for this change is node-22, post-deploy, not
node-27", and the cost — `flock` exercised on a local filesystem until that
receipt is taken — is a recorded known limit. The same measurement is why issue
#2252 leads with an interoperability question rather than with a patch:
node-27's own deleter would be taking a server-local lock against node-22's
client-side one, and whether those exclude each other is not something this
change established.
