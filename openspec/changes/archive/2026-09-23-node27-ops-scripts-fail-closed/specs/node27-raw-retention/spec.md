## ADDED Requirements

### Requirement: A traversal failure while sizing a target SHALL retire only that target

When measuring a target directory raises an `OSError` during traversal (for example `ESTALE` or `EIO` from directory enumeration), the raw-retention runner SHALL record that target as a skipped entry carrying `error` and `error_type`, SHALL NOT plan or delete it, SHALL continue with every other target and lane, and SHALL write its summary.

#### Scenario: A stale handle on one cycle directory does not kill the tick

- **GIVEN** a production-mode tick in which enumerating one raw-lane cycle directory raises `OSError(ESTALE)`
- **WHEN** the tick runs
- **THEN** it completes with that cycle in `skipped[]` with `error_type` `OSError`, deletes the other eligible targets, and writes the summary file
