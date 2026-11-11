## ADDED Requirements

### Requirement: The capacity watch SHALL derive its prune window from the scheduler cycle lag

The daily capacity watch SHALL compute the retention window it passes to the `prune-retention` dry-run as `max(DEFAULT_STATE_INDEX_RETENTION_DAYS, minimum_days)`. `minimum_days` is `floor_hours // 24 + 1`, the smallest whole number of days whose hours strictly exceed the prune floor (`MAX_LOOKBACK_HOURS + cycle_lag_hours + STATE_INDEX_RETENTION_STEP_BACK_HOURS`). The floor SHALL come from the same single helper that the repair's window check uses.

The cycle lag SHALL be read from `NHMS_SCHEDULER_CYCLE_LAG_HOURS`. When it is missing, not an integer, or negative, the watch SHALL use the default window, SHALL pass no lag of its own to the repair, and SHALL surface the repair's existing `repair_cycle_lag_unset` refusal (exit 2) unchanged.

Every receipt SHALL carry a top-level `retention_days_source` key. It SHALL be `default` or `derived_from_cycle_lag` on every receipt produced after the window is derived, and `null` only on a refusal that occurs before the repair module could be imported. The receipt `schema_version` SHALL stay unchanged.

#### Scenario: Normal lag keeps the default window

- **WHEN** `NHMS_SCHEDULER_CYCLE_LAG_HOURS` is 16 or 119
- **THEN** the watch uses 21 days with `retention_days_source=default`

#### Scenario: Lag at the boundary widens by one day

- **WHEN** `NHMS_SCHEDULER_CYCLE_LAG_HOURS=120` (floor 504 h, equal to 21 days)
- **THEN** the watch uses 22 days with `retention_days_source=derived_from_cycle_lag`

#### Scenario: Catch-up lag widens the window instead of refusing

- **WHEN** `NHMS_SCHEDULER_CYCLE_LAG_HOURS=294` (floor 678 h)
- **THEN** the watch uses 29 days with `retention_days_source=derived_from_cycle_lag`
- **AND** the receipt is evaluated rather than refused with `repair_retention_window_too_short`

#### Scenario: Invalid lag still refuses with the existing error

- **WHEN** `NHMS_SCHEDULER_CYCLE_LAG_HOURS` is unset, not an integer, or negative
- **THEN** the watch exits 2 with `repair_cycle_lag_unset`
- **AND** it does not exit 4

### Requirement: The capacity warning log line SHALL distinguish indexes without exposing paths

When capacity `warning` is true, the logged warning SHALL carry, in addition to the existing redacted index label, a short non-reversible fingerprint computed as a pure string hash of the index uri as received by the call site. It SHALL NOT touch the filesystem and SHALL NOT raise. It SHALL be computed only when `warning` is true. Two different indexes therefore produce different fingerprints, and a public helper SHALL return the same label so operators can map a fingerprint to an index. The line SHALL NOT contain any filesystem path or URI, and SHALL keep the `run the prune-retention repair (known-issues-state-index 8.12)` guidance. The `index` value and the `capacity` object in evidence and publish results SHALL remain unchanged. Exactly one warning SHALL still be logged per publish and per uncached load.

#### Scenario: Two indexes over threshold are distinguishable in the log

- **WHEN** both the private and the destination index are at or above the 0.70 threshold
- **THEN** their warning lines carry different `index_fp` values
- **AND** neither line contains a path
- **AND** the evidence `index` values are the same redacted labels as before
