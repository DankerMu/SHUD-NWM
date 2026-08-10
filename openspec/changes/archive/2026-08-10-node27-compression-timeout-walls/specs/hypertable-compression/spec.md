# hypertable-compression (delta)

## ADDED Requirements

### Requirement: Compression runner timeout budget chain MUST be operator-configurable and fail closed

The node-27 timeseries compression runner SHALL derive its per-chunk `statement_timeout`, its wrapper wall, and its declared systemd wall from operator-configurable environment variables sourced from the single compression env file, with defaults byte-identical to the previously hardcoded values (840000 ms, 900 s, 940 s), and SHALL reject any configuration that violates either leg of the budget-chain invariant — per-chunk timeout in seconds (rounded up) plus the fixed cleanup margin must not exceed the wrapper wall, and the wrapper wall plus the fixed kill-after margin must not exceed the declared systemd wall — before opening any database connection. The invariant bounds a single chunk's budget, not a whole tick.

#### Scenario: defaults unchanged

WHEN none of the timeout environment variables is set, or any of them is set to the empty string
THEN the runner uses 840000 ms as the per-chunk statement timeout, 900 s as the wrapper wall, and 940 s as the declared systemd wall
AND runtime behavior and the receipt schema are identical to the previous hardcoded configuration.

#### Scenario: override propagates to the database session

WHEN `NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS` is set to a valid value satisfying the budget-chain invariant
THEN every `compress_chunk` call session issues `SET statement_timeout` with exactly the overridden value.

#### Scenario: budget-chain violation is rejected before any DB call

WHEN the configured per-chunk timeout plus the cleanup margin exceeds the configured wrapper wall, or the wrapper wall plus the kill-after margin exceeds the declared systemd wall, or any of the three variables fails positive-integer validation
THEN the runner raises a fail-closed configuration error naming the violated invariant
AND no database connection is attempted.

#### Scenario: wrapper wall guard is fail-closed

WHEN the wrapper wall environment variable is present, non-empty, and not a positive integer
THEN the shell wrapper exits non-zero with a structured error before executing the runner
AND when the variable is absent the wrapper uses the 900 s default.
