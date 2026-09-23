## ADDED Requirements

### Requirement: An explicit dry-run SHALL override the enforce environment toggle

The node-27 time-series retention runner SHALL resolve its mode with this precedence: an explicit `--dry-run` resolves to dry-run whatever `NODE27_TIMESERIES_RETENTION_ENFORCE` says; otherwise an explicit `--enforce` resolves to enforce; otherwise the environment toggle decides as before. A dry-run SHALL NOT call `drop_chunk` and SHALL NOT enter the enforce-only measurement.

#### Scenario: Dry-run with the enforce toggle set drops nothing

- **GIVEN** `NODE27_TIMESERIES_RETENTION_ENFORCE=1` in the environment and at least one chunk eligible for retention
- **WHEN** the runner is invoked with `--dry-run`
- **THEN** the receipt records `mode=dry-run` and `outcome=dry-run` and no chunk is dropped
