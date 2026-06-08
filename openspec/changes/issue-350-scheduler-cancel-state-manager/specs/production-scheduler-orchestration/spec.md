## MODIFIED Requirements

### Requirement: Backend scheduler entrypoint

The system SHALL provide a backend scheduler entrypoint that can run once or continuously and create production forecast work for all selected registered basins.

#### Scenario: one-shot scheduler pass

WHEN an operator runs the scheduler in one-shot mode
THEN it scans configured GFS/IFS cycles, resolves active basin/model candidates, records a pass summary, and exits with non-zero status only for scheduler-level failures or configured fatal candidate failures.

#### Scenario: continuous scheduler pass

WHEN the scheduler runs continuously
THEN it uses a lock or equivalent lease to prevent concurrent duplicate scans
AND it records each pass start, finish, candidate count, and selected/skipped/failed counts.

#### Scenario: cancel-only active Slurm pass without state database

WHEN the scheduler runs with `cancel_active_slurm=True`
AND active Slurm jobs are found for a candidate cycle
AND no replacement submission or state-selection work is required
AND `DATABASE_URL` is absent
THEN the scheduler cancels active Slurm jobs through the cancellation contract without constructing a database-backed state manager
AND records cancellation evidence without submitting replacement work.
