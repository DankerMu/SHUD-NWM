## ADDED Requirements

### Requirement: Retention periods SHALL be bounded above and an out-of-range period SHALL leave a receipt

The runner SHALL block at preflight, delete nothing, exit 2 and write its `preflight_blocked` summary (when a summary path is configured) when `NODE27_RAW_RETENTION_FORCING_DAYS`, or the raw retention period from `--retention-days` or `NODE27_RAW_RETENTION_DAYS`, exceeds 3650 days. The blocker SHALL name the field (`forcing_retention_days` or `retention_days`), use the reason `above_ceiling`, and carry the value and the ceiling. A period at or below the ceiling SHALL behave as before. For any reference time the display watermark can produce, no period within or above the ceiling SHALL make the run end with an unhandled exception.

#### Scenario: a huge forcing period

- **WHEN** `NODE27_RAW_RETENTION_FORCING_DAYS` is `1000000` and a summary path is configured
- **THEN** the run is blocked at preflight with a `forcing_retention_days` blocker whose reason is `above_ceiling` and whose ceiling is 3650
- **AND** a `preflight_blocked` summary is written and the exit code is 2
- **AND** no file under any lane is removed

#### Scenario: a huge raw period

- **WHEN** `NODE27_RAW_RETENTION_DAYS` is `1000000` and a summary path is configured
- **THEN** the run is blocked at preflight with a `retention_days` blocker whose reason is `above_ceiling`
- **AND** a `preflight_blocked` summary is written and the exit code is 2

#### Scenario: the ceiling itself

- **WHEN** both periods are 3650
- **THEN** preflight passes
