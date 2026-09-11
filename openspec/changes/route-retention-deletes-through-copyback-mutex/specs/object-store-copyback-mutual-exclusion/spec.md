## ADDED Requirements

### Requirement: Destructive removal of a copyback directory tree holds the same mutex

A process SHALL hold the copyback root's batch mutex for the whole of any
removal of a directory tree under that shared object-store copyback root. The
capability's other requirement binds the writers that promote trees; this one
binds the deleters, because a removal that lands inside a writer's
rename-to-backup-to-promote window destroys that writer's rollback material just
as surely as a competing promote would.

The mutex SHALL be taken per removed tree, not once for a whole retention pass:
a pass's planning walk sizes every candidate on the shared mount, and holding
the mutex across it would consume the acquisition budget the promoting writers
depend on.

The obligation is scoped to the **copyback** root. A run-workspace root that is
not shared SHALL NOT be locked — creating a lock file there buys no mutual
exclusion and adds a fail-closed ownership check to a lane that has no second
party. Nor SHALL a root be locked in the configuration where the copyback root
and the process's own primary object store are the same directory; that root's
aged run trees SHALL still be removed there, unlocked.

Why that second carve-out is safe rather than merely convenient is a property of
the **writers**, not of the deleter this requirement binds, so it is recorded
with them and not asserted here. One writer establishes it only from
operator-supplied arguments rather than from the process's own object-store
root, which is exactly why this requirement does not rest on a universal claim
about all of them.

One implementation is known to violate this requirement and is not brought into
compliance by the change that adds it: `scripts/node27_raw_retention.py` removes
directory trees without taking the mutex, on node-27, under the very directory
node-22 mounts as the shared copyback root. It is recorded here rather than
excluded by narrowing the requirement to the scheduler's own retention pass,
which would make the requirement true by construction and leave the gap
unrecorded. It is tracked by issue #2252, which must first settle whether a
lock taken on the NFS server's local filesystem excludes one taken by an NFS
client at all.

Failure to acquire SHALL be recorded as that pass's own per-entry failure and
SHALL NOT abort the pass or propagate out of the removal, because the mutex's
error type is a `RuntimeError` rather than an `OSError` and every retention
caller's contract is that one entry's failure never interrupts the sweep.

This requirement covers the *overlap* between a removal and a promotion. It does
**not** make a removal conditional on what a writer did after the pass planned
it: see the non-guarantee scenario below.

#### Scenario: a removal on the copyback root cannot enter a writer's promote window

- **WHEN** a retention pass removes one run directory under the shared copyback
  root's `runs/`
- **AND** a copyback writer holds that root's batch mutex for its
  promote-and-commit critical section
- **THEN** the removal MUST block until that writer's batch has committed or
  rolled back
- **AND** the writer's backup tree MUST NOT be partially removed while it is
  still the writer's rollback material
- **AND** conversely, while the removal holds the mutex, a writer MUST NOT be
  able to promote into that same target.

#### Scenario: only the shared copyback root is locked

- **WHEN** the same pass removes trees under an additional run-workspace root,
  or under its own primary object store, in the same sweep as copyback-root
  removals
- **THEN** those removals MUST proceed without acquiring the mutex
- **AND** no lock file MAY be created under either of those roots
- **AND** a configuration in which the run-workspace root and the copyback root
  resolve to the same directory MUST lock that single root, because it *is* the
  shared one
- **AND** a configuration in which the copyback root and the primary object
  store resolve to the same directory MUST still remove that root's aged run
  trees and MUST NOT lock them.

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
  MUST be recorded as a failure without any further acquisition attempt
- **AND** the pass MUST still return normally, with the other roots' removals
  unaffected.

#### Scenario: an unavailable mutex is a recorded failure, never an aborted pass

- **WHEN** the mutex cannot be acquired within the deadline, or the lock file
  fails the protocol's fail-closed identity checks
- **THEN** the tree MUST NOT be removed
- **AND** the pass MUST record that entry as a failure carrying the error text,
  leave it out of the deleted set and out of the freed-bytes total, continue
  with the remaining entries, and return normally
- **AND** neither the timeout error nor the unsafe-lock error MAY escape the
  removal, because the surrounding scheduler collapses its whole pass receipt to
  an error status on any escaping exception and the command-line sweep aborts
  outright.

#### Scenario: a pass that provably removes nothing acquires nothing

- **WHEN** retention is disabled, or is in dry-run, so no removal executes
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
  any other root: every enumeration descends into `runs/` or into a
  cycle-scoped prefix before it collects anything — the primary root is walked
  by both — and each level keeps directory entries only, so no walk both
  reaches a regular file sitting directly under the root and admits it
- **AND** this MUST be enforced by test rather than left as an inherited
  argument, because removing the lock file under a live holder splits the mutex:
  that holder keeps its `flock` on the detached inode while the next acquirer
  creates and locks a fresh file, so the two run unserialised for the rest of
  that hold. The root converges again once the stale holder exits, so the damage
  is a window rather than a permanent poisoning — and a window of exactly the
  interval this requirement exists to serialise.
