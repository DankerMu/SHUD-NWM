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
and the process's own primary object store are the same directory: there every
copyback writer skips before acquiring, precisely because the two roots are the
same, so there is no second party to exclude.

One implementation is known to violate this requirement and is not brought into
compliance by the change that adds it: `scripts/node27_raw_retention.py` removes
directory trees on node-27 under the same shared export without taking the
mutex. It is recorded here rather than excluded by narrowing the requirement to
the scheduler's own retention pass, which would make the requirement true by
construction and leave the gap unrecorded; closing it is tracked separately.

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
  trees and MUST NOT lock them, because in that configuration every copyback
  writer returns a skip before acquiring and no second party exists.

#### Scenario: the mutex is acquired per removed tree

- **WHEN** one pass removes N trees under the copyback root
- **THEN** there MUST be N acquisitions and N releases, each spanning only its
  own removal
- **AND** the mutex MUST NOT be held across the pass's planning walk or across
  the gap between two removals, so a long sweep cannot starve a promoting
  writer past its deadline.

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
  identical with and without the mutex, so the mutex cannot be the reason a tree
  survives or dies
- **AND** the size attributed to a removed tree MAY have been measured before
  the mutex was acquired, so the freed-bytes total is an accounting estimate,
  not a post-removal measurement.

#### Scenario: the lock file is never itself a removal candidate

- **WHEN** the lock file sits at its fixed name directly under the copyback root
- **THEN** no retention enumeration MAY select it, on the copyback root or on
  any other root: the additional-root enumeration descends only into the
  `runs/` directory and keeps directories only, and the primary enumeration
  descends only into the cycle-scoped prefixes
- **AND** this MUST be enforced by test rather than left as an inherited
  argument, because the mutex is permanently poisoned if a lock file is removed
  and recreated as a fresh inode under a live holder.
