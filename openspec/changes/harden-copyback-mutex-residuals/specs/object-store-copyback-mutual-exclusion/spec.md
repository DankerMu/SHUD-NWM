## MODIFIED Requirements

### Requirement: Copyback directory-tree promote-and-commit batches are mutually exclusive across processes

A process SHALL hold one exclusive cross-process mutex, fixed per copyback root,
for the whole of any critical section in which it promotes a directory tree under
the shared object-store copyback root — from the first read of the destination
tree it promotes into whose result that promotion depends on, through every
directory-tree promotion, until its batch commit or batch rollback has
returned. The mutex SHALL NOT be
released between the individual tree promotions of one batch.

A lane whose planning reads only the source object store MAY perform that
planning before acquiring: it observes nothing a competitor can change under the
copyback root.

A lane that writes SHALL take its destination-dependent decisions — including
deciding that a tree is already mirrored and need not be promoted — inside the
same critical section as the promotion that decision gates, so a tree it reports
as already present was observed under the mutex exactly as a tree it reports as
promoted. A mode that provably writes nothing MAY read the destination without
acquiring, because creating the lock file is itself a write; every
destination-dependent conclusion it reports SHALL then be marked as not observed
under the mutex.

The mutex SHALL be taken with a lock primitive that every other acquirer on the
same copyback root contends with, from whichever host it runs on. A host that
reaches the root over an NFS client mount SHALL use `flock`; the host that
exports the root from its local filesystem SHALL use a POSIX record lock,
because a local `flock` there does not exclude an NFS client's `flock` (receipt
of 2026-09-14). A POSIX record lock is per process, so an acquirer using it
SHALL be single-threaded with respect to the mutex and SHALL NOT open the lock
file through any other descriptor while holding it.

A writer that cannot acquire the mutex within its configured deadline SHALL fail
loudly with a distinct error and SHALL NOT promote any tree.

#### Scenario: a competing writer cannot enter the backup-to-promote window

- **WHEN** one writer is between renaming an existing copyback target to its
  backup name and promoting its own temporary tree into that name
- **AND** a second copyback writer starts against the same copyback root
- **THEN** the second writer MUST block until the first writer's batch has
  committed or rolled back
- **AND** neither writer's reported status MAY claim success for a tree that is
  no longer present at its target.

#### Scenario: a batch rollback never removes another writer's committed tree

- **WHEN** a writer promotes a tree into a target that did not previously exist,
  so its rollback entry records no backup directory
- **AND** the same writer later fails on a subsequent tree in the same batch
- **THEN** the rollback MUST NOT remove a tree that a different writer committed
  after that promotion
- **AND** the destination MUST NOT be left holding a mixture of two writers'
  trees: it holds either the competitor's complete tree, or — when no
  competitor promoted into that target — nothing at all, because the
  rolling-back writer removes exactly the tree it created and there was no
  earlier tree to restore.

#### Scenario: the one-off backfill writers take the same mutex

- **WHEN** `scripts/canonical_precip_copyback_backfill.py` or the forcing
  copyback backfill writes under the same copyback root as the publisher
- **THEN** it MUST hold the same mutex for its own critical section
- **AND** no file it reports as copied MAY be removed by a concurrent publisher's
  rollback
- **AND** a backfill MUST acquire the mutex per mirrored tree rather than once
  for its whole run, so a long backfill cannot starve a publisher past its
  deadline; per-tree is sufficient because a backfill mirrors file by file with
  no cross-tree rollback of its own
- **AND** a backfill mode that provably writes nothing — `--dry-run` — MUST NOT
  acquire, because creating the lock file is itself a write under the copyback
  root.

#### Scenario: the forcing backfill observes an already-present package under the mutex

- **WHEN** the forcing copyback backfill runs with `--apply`
- **AND** a competitor, holding the mutex, promotes a package tree into a target
  that did not previously exist and then rolls it back before releasing
- **THEN** the backfill MUST NOT report that package as already present
- **AND** for each package the destination inspection, the skip decision, the
  source validation, the copy and the commit or rollback MUST happen within one
  acquisition of the mutex, taken once per package that reads the destination, whether that package
  is then copied, skipped or failed; a package rejected by checksum grouping
  before any destination read takes none
- **AND** a failure to acquire MUST be recorded as that package's failure and
  MUST NOT abort the run.

#### Scenario: the forcing backfill plan mode is marked advisory

- **WHEN** the forcing copyback backfill runs without `--apply`
- **THEN** it MUST NOT acquire the mutex and MUST NOT create the lock file
- **AND** every package record and the report MUST state that its destination
  observations were not made under the mutex.

#### Scenario: the run-tree copyback lane is covered by the same adjudication

- **WHEN** `services/orchestrator/run_tree_copyback` replaces a run tree under
  the shared copyback root
- **THEN** it MUST hold the same mutex
- **AND** its guarded recovery branch — which restores its backup only when the
  target is absent, and therefore never removes a competitor's tree — MUST remain
  in place
- **AND** the state-snapshot index merge it performs in the same call MUST run
  after the mutex has been released, per the per-file exemption below; its
  presence in the call's returned summary is reporting, not a promote held open
  across the release.

#### Scenario: per-file provider-atomic copyback writes are exempt

- **WHEN** a copyback **write** promotes no directory tree — it writes an
  individual file under a disjoint subtree of the copyback root through its own
  provider-atomic lock, as the state-snapshot index merge and the state
  checkpoint copyback do
- **THEN** that write is outside this requirement and MUST proceed without
  holding this mutex
- **AND** when the same call also promotes directory trees, the exempt write
  MUST run only after this mutex has been released — never nested inside it,
  because the provider-atomic lock it takes has no deadline and nesting an
  unbounded wait inside the bounded mutex turns one stalled writer into a
  head-of-line stall for every other writer under the copyback root.

The exemption is scoped to the write, not to the writer: a caller that promotes
directory trees *and* performs such a write is covered by this requirement for
its promotes and exempt for that write.

#### Scenario: the mutex fails closed on an unsafe lock file

- **WHEN** the lock path is a symlink, is not a regular file, has more than one
  hard link, does not have mode `0o600`, is not owned by the effective user, or
  is not owned by the same uid that owns the copyback root
- **THEN** acquisition MUST raise rather than proceed, as each lane's own error
  type and distinctly from a timeout — the q_down publish lane raises a publish
  error carrying a copyback-lock-unsafe code, so an operator can tell a tampered
  lock file from a busy one
- **AND** no copyback tree MAY be promoted
- **AND** these checks MUST be identical for every lock primitive
- **WHEN** the lock file does not exist yet and the effective user is not the
  copyback root's owner
- **THEN** acquisition MUST refuse *before* creating it, because the lock file is
  never unlinked — creating it would leave exactly the foreign-uid orphan that
  poisons the lock for every legitimate writer
- **AND** no lock file MAY be left behind by the refused writer.

#### Scenario: the mutex is anchored where every writer provably shares it

- **WHEN** two copyback writers run under different mount namespaces — a private
  `/tmp` from a service sandbox or a job container, for example
- **THEN** they MUST still contend on the same lock, because the lock is anchored
  under the copyback root each of them already resolved
- **AND** two distinct copyback roots MUST NOT contend with each other.

#### Scenario: the exporting host and an NFS client contend on one lock

- **WHEN** an acquirer on the host exporting the copyback root holds the mutex
  with the POSIX record-lock primitive
- **AND** an acquirer on a host mounting that root over NFS attempts it with
  `flock`, or the other way round
- **THEN** the attempt MUST block
- **AND** an acquirer on the exporting host MUST NOT use `flock`, because that
  combination was measured not to exclude in either direction.

#### Scenario: acquisition deadline is bounded and observable

- **WHEN** the mutex is held by another writer for longer than the configured
  timeout
- **THEN** the waiting writer MUST raise a distinct timeout error
- **AND** it MUST NOT fall back to an unsynchronized promote
- **AND** where the caller already records a failure receipt instead of
  propagating — as the `convert`-stage canonical precipitation mirror does — the
  surrounding cycle MUST survive with that receipt
- **WHEN** a configured timeout — whether passed explicitly or read from the
  environment — is not a positive finite number of seconds
- **THEN** acquisition MUST refuse with a configuration error before touching
  the filesystem, and no lock file MAY be created.

#### Scenario: the timeout surfaces as each lane's own error type

- **WHEN** the mutex acquisition times out
- **THEN** the raised error MUST be of the type each calling lane already handles,
  so no lane gains an uncaught exception: the run-tree copyback lane MUST raise its
  own run-tree copyback error, and the q_down publish lane MUST raise a publish
  error carrying a distinct copyback-lock-timeout code
- **AND** no lane MAY propagate a foreign exception type past a stage handler that
  catches only its own.

#### Scenario: a zero-write skip path creates no lock file

- **WHEN** the configured copyback root resolves to the same directory as the
  object-store root, so the lane returns a skip without writing anything
- **THEN** the mutex MUST NOT be acquired and no lock file MAY be created
- **AND** acquisition MUST therefore happen after the copyback root's identity and
  overlap guards, not merely after the root has been prepared.

### Requirement: Destructive removal of a copyback directory tree holds the same mutex

A process SHALL hold the copyback root's batch mutex for the whole of any
removal of a directory tree under that shared object-store copyback root that
lies in a key space a mutex-holding writer promotes into — `runs/`, `forcing/`
and `canonical/`. The capability's other requirement binds the writers that
promote trees; this one binds the deleters, because a removal that lands inside
a writer's rename-to-backup-to-promote window destroys that writer's rollback
material just as surely as a competing promote would. A key space no acquirer
promotes into — `raw/` — has no window to protect and SHALL NOT be locked.

The mutex SHALL be taken per removed tree, not once for a whole retention pass:
a pass's planning walk sizes every candidate on the shared mount, and holding
the mutex across it would consume the acquisition budget the promoting writers
depend on.

The obligation is scoped to the **copyback** root. A run-workspace root that is
not shared SHALL NOT be locked — creating a lock file there buys no mutual
exclusion and adds a fail-closed ownership check to a lane that has no second
party. Nor SHALL a root be locked in the configuration where a process on an NFS
client host has the copyback root and its own primary object store resolve to
the same directory; that root's aged run trees SHALL still be removed there,
unlocked. On the host that exports the copyback root, the object-store root
*is* the shared root and SHALL be locked, with the POSIX record-lock primitive
the other requirement prescribes for that host: `scripts/node27_raw_retention.py`
is such a deleter for `canonical/`.

Failure to acquire SHALL be recorded as that pass's own per-entry failure and
SHALL NOT abort the pass or propagate out of the removal, because the mutex's
error type is a `RuntimeError` rather than an `OSError` and every retention
caller's contract is that one entry's failure never interrupts the sweep. The
recorded failure SHALL name which of three shapes occurred — the guard's
deadline expired, the lock was unsafe or could not be opened, or the pass's own
wait budget was already spent — and each pass receipt SHALL carry a per-shape
count that survives receipt compaction.

This requirement covers the *overlap* between a removal and a promotion. It does
**not** make a removal conditional on what a writer did after the pass planned
it: see the non-guarantee scenario below.

#### Scenario: a removal on the copyback root cannot enter a writer's promote window

- **WHEN** a retention pass removes one tree under the shared copyback root in
  `runs/` or `canonical/`
- **AND** a copyback writer holds that root's batch mutex for its
  promote-and-commit critical section
- **THEN** the removal MUST block until that writer's batch has committed or
  rolled back
- **AND** the writer's backup tree MUST NOT be partially removed while it is
  still the writer's rollback material
- **AND** conversely, while the removal holds the mutex, a writer MUST NOT be
  able to promote into that same target
- **AND** this MUST hold when the removal runs on the host exporting the root and
  the writer on an NFS client host.

#### Scenario: only the shared copyback root is locked

- **WHEN** the same pass removes trees under an additional run-workspace root,
  or under its own primary object store on an NFS client host, in the same sweep
  as copyback-root removals
- **THEN** those removals MUST proceed without acquiring the mutex
- **AND** no lock file MAY be created under either of those roots
- **AND** a configuration in which the run-workspace root and the copyback root
  resolve to the same directory MUST lock that single root, because it *is* the
  shared one
- **AND** a configuration in which the copyback root and the primary object
  store resolve to the same directory on an NFS client host MUST still remove
  that root's aged run trees and MUST NOT lock them.

#### Scenario: the exporting host locks only the promoted key space

- **WHEN** the node-27 raw retention removes aged trees under its object-store
  root
- **THEN** each `canonical/<storage-source>/<cycle>` removal MUST hold the mutex
- **AND** each `raw/<source>/<cycle>` removal and each precipitation-cache
  removal MUST proceed without acquiring it.

#### Scenario: the mutex is acquired per removed tree

- **WHEN** one pass removes N trees under the copyback root
- **THEN** there MUST be N acquisitions and N releases, each spanning only its
  own removal
- **AND** the mutex MUST NOT be held across the pass's planning walk or across
  the gap between two removals, so the sweep's hold time is bounded by one
  tree's removal rather than by the whole pass. This bounds each hold, not the
  wait a writer may see: a writer can still queue behind however many
  consecutive single-tree holds the sweep takes.

#### Scenario: the total time a sweep spends waiting is bounded

- **WHEN** the mutex is held throughout a pass by a holder that does not release
- **THEN** the pass's cumulative acquisition wait MUST NOT exceed one bounded
  per-pass budget, however many trees were planned, because per-tree deadlines
  alone multiply by the tree count and a periodic sweep that outlasts its own
  cadence is an outage of its own
- **AND** once that budget is spent, each remaining tree on the copyback root
  MUST be recorded as a budget-exhausted failure without any further acquisition
  attempt
- **AND** the pass MUST still return normally, with the other roots' and lanes'
  removals unaffected.

#### Scenario: an unavailable mutex is a recorded failure, never an aborted pass

- **WHEN** the mutex cannot be acquired within the deadline, or the lock file
  fails the protocol's fail-closed identity checks or cannot be opened
- **THEN** the tree MUST NOT be removed
- **AND** the pass MUST record that entry as a failure carrying the error text
  and its failure shape — timeout, unsafe, or budget exhausted — leave it out of
  the deleted set and out of the freed-bytes total, continue with the remaining
  entries, and return normally
- **AND** the pass receipt MUST carry a count for each of the three shapes, zero
  included, and that block MUST remain in a compacted receipt
- **AND** neither the timeout error nor the unsafe-lock error MAY escape the
  removal, because the surrounding scheduler collapses its whole pass receipt to
  an error status on any escaping exception and the command-line sweep aborts
  outright.

#### Scenario: a pass that provably removes nothing acquires nothing

- **WHEN** retention is disabled, is in dry-run, or is preflight-blocked, so no
  removal executes
- **THEN** the mutex MUST NOT be acquired and no lock file MAY be created under
  the copyback root, because creating the lock file is itself a write there
- **WHEN** the additional-root gate is closed, so the copyback root is not swept
- **THEN** likewise nothing MAY be acquired.

#### Scenario: the mutex does not widen or narrow what is removed

- **WHEN** a writer promotes and commits a tree, inside the mutex, that the pass
  had already planned for removal
- **THEN** the pass MAY still remove that tree when it later acquires the mutex:
  this requirement serializes the two operations, it does not re-adjudicate the
  removal predicate under the lock
- **AND** this tolerated window MUST be pinned by a test
- **AND** the removal predicate — which trees are selected, the retention
  window, the frontier bound, the published-artifact protection — MUST be
  identical with and without the mutex: nothing is re-adjudicated under the
  lock, and no tree becomes eligible because the lock was taken
- **AND** the mutex MAY nonetheless be the reason an eligible tree is still on
  disk after a pass — a failed acquisition or an exhausted wait budget records
  that entry as a failure and leaves the tree for the next pass — which is a
  recorded failure of one removal, not a different predicate
- **AND** the size attributed to a removed tree MAY have been measured before
  the mutex was acquired, so the freed-bytes total is an accounting estimate,
  not a post-removal measurement.

#### Scenario: the lock file is never itself a removal candidate

- **WHEN** the lock file sits at its fixed name directly under the copyback root
- **THEN** no retention enumeration MAY select it, on the copyback root or on
  any other root: every enumeration descends into a lane or `runs/` prefix
  before it collects anything, and each level keeps directory entries only, so
  no walk both reaches a regular file sitting directly under the root and
  admits it
- **AND** this MUST be enforced by test rather than left as an inherited
  argument, because removing the lock file under a live holder splits the mutex:
  that holder keeps its lock on the detached inode while the next acquirer
  creates and locks a fresh file, so the two run unserialised for the rest of
  that hold.

## ADDED Requirements

### Requirement: A run-tree replacement never destroys its only backup on a failure path

The run-tree copyback lane SHALL delete a replaced target's backup only once the
target is known to be in place, whenever `services/orchestrator/run_tree_copyback`
replaces an existing run tree or individual file by renaming it to a backup and
promoting a temporary copy: either the promotion succeeded, or the backup was restored to the
target. A failure to clean up the temporary copy SHALL NOT prevent the restore
from being attempted. When the backup is neither promoted over nor restored, it
SHALL be left on disk and the raised run-tree copyback error SHALL name its
path.

#### Scenario: promotion and restore both fail

- **WHEN** the old target has been renamed to its backup
- **AND** promoting the temporary copy fails
- **AND** restoring the backup to the target also fails
- **THEN** the backup MUST still exist
- **AND** the raised run-tree copyback error MUST carry the backup path in its
  details.

#### Scenario: temporary-copy cleanup fails after a failed promotion

- **WHEN** promoting the temporary copy fails
- **AND** removing the temporary copy raises
- **THEN** the restore MUST still be attempted
- **AND** the target MUST NOT be left absent with its backup deleted.

#### Scenario: a successful restore is not undone

- **WHEN** promotion fails and the backup is restored to the target
- **THEN** nothing MAY be deleted from the restored target afterwards
- **AND** no backup name MAY remain.

#### Scenario: a successful replacement leaves no residue

- **WHEN** promotion succeeds
- **THEN** the target holds the new content
- **AND** neither the backup nor the temporary copy MAY remain.
