## MODIFIED Requirements

### Requirement: Backend scheduler entrypoint

The system SHALL provide a backend scheduler entrypoint that can run once or
continuously and create production forecast work for all selected registered
basins. Production live passes SHALL make bounded progress, release their lock
on completion or stable blocker, and remain business-runnable on node-22 in
DB-free mode under configured concurrent submission.

#### Scenario: one-shot scheduler pass

WHEN an operator runs the scheduler in one-shot mode
THEN it scans configured GFS/IFS cycles, resolves active basin/model candidates,
records a pass summary, and exits with non-zero status only for scheduler-level
failures or configured fatal candidate failures.

#### Scenario: continuous scheduler pass

WHEN the scheduler runs continuously
THEN it uses a lock or equivalent lease to prevent concurrent duplicate scans
AND it records each pass start, finish, candidate count, and
selected/skipped/failed counts.

#### Scenario: bounded node-22 live pass

WHEN the DB-free node-22 scheduler runs a live pass
THEN the pass either submits, reconciles, skips, or blocks candidates with
bounded progress evidence before the configured progress limit
AND it releases the scheduler lock before exit
AND it MUST NOT spin indefinitely while holding the production scheduler lock.

#### Scenario: concurrent node-22 business pass

WHEN node-22 runs the compute scheduler with a concurrent submit bound greater
than `1` and at least two eligible candidates or array tasks exist
THEN candidate selection, retry, Slurm submission, and reconcile use
source/cycle/model/stage/task identity to avoid duplicate work
AND the pass records concurrent bound evidence
AND no stale lock or duplicate submission remains after the pass.

#### Scenario: no-work pass is not business readiness

WHEN node-22 runs the scheduler and no eligible candidate or array task exists
THEN the pass may record safe no-work evidence and release the lock
AND the pass MUST NOT be used as the business-operation receipt for this
capability.
