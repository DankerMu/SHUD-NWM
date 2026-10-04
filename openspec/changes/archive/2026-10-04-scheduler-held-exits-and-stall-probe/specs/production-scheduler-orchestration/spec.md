## ADDED Requirements

### Requirement: Rollback quiescence fence uses the filesystem clock

Rollback quiescence discovery SHALL decide whether a journal file changed since prepare by comparing the file's `st_mtime_ns` with the `st_mtime_ns` of the fence sidecar file `reconcile-inventory-rollback-fence-v1`, written by a fresh prepare (replacing any orphan), left untouched by resumed prepares and removed by rollforward together with the receipt. The receipt schema SHALL be unchanged. A receipt without a fence sidecar SHALL fall back to the wall-clock `prepared_at` comparison.

#### Scenario: File written within one coarse tick after the fence
- **WHEN** a continuation segment's mtime is earlier than wall-clock `prepared_at` but not earlier than the fence sidecar's mtime
- **THEN** its unsettled job is returned by `query_rollback_unsettled_jobs()`

#### Scenario: Historical file
- **WHEN** a file's mtime is earlier than the fence sidecar's mtime
- **THEN** it is excluded as before
