## ADDED Requirements

### Requirement: Reconcile accounting reads drain every pipe to end-of-file

The reconcile sacct and visibility-probe readers SHALL keep reading until every registered pipe reaches end-of-file or the query deadline expires; process exit alone SHALL NOT end the read. Timeout and saturation errors SHALL be raised as before.

#### Scenario: sacct writes its row and exits during an idle wait

- **WHEN** the sacct child writes its only row and exits after the reader's `select` returned with no events
- **THEN** the reader MUST return that row and `default_sacct_querier` MUST return a record instead of `None`

#### Scenario: Child never closes its pipes

- **WHEN** the child keeps its pipes open past the query deadline
- **THEN** the reader MUST raise `ReconcileQueryUnavailable` within the deadline
