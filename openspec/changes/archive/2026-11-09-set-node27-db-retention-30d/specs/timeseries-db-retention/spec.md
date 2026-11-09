## ADDED Requirements

### Requirement: The DB retention window SHALL default to 30 days and the shipped template SHALL match the default

The node-27 timeseries retention runner's default drop window (`DEFAULT_RETENTION_WINDOW_DAYS` in `packages/common/storage.py`, used when `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` is absent or empty) SHALL be 30 days, matching the 2026-11-09 owner retention policy. The committed env template `infra/env/node27-timeseries-retention.example` SHALL ship an uncommented `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` whose value equals that default, so re-copying the template onto a box running the policy value cannot silently narrow the window. `configured_retention_window_days` SHALL continue to return `None` for an absent window, so the display coverage refresh keeps its fail-closed behaviour and never borrows the default.

#### Scenario: Absent window takes the 30-day default
- **WHEN** the retention runner's env has no `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` assignment, or an empty one
- **THEN** the resolved `window_days` is 30

#### Scenario: Template value equals the shared default
- **WHEN** the committed env template is parsed for its uncommented `NODE27_TIMESERIES_RETENTION_WINDOW_DAYS` assignment
- **THEN** exactly one such assignment exists and its integer value equals `DEFAULT_RETENTION_WINDOW_DAYS`

#### Scenario: Coverage refresh still has no default
- **WHEN** `configured_retention_window_days` is called with an env lacking the window variable
- **THEN** it returns `None`
