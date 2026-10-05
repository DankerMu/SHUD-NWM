## ADDED Requirements

### Requirement: Forecast fact rows lie inside the run window

The output parser SHALL refuse a forecast run in which any row's `valid_time` is earlier than the run's `cycle_time` or later than the run's `end_time`. The refusal SHALL use the error code `VALID_TIME_OUTSIDE_RUN_WINDOW`, mark the run failed, and write no row. Both bounds are inclusive. Analysis runs are not subject to this check.

#### Scenario: A row before the cycle time is refused

- **WHEN** a forecast product contains a row whose `valid_time` is earlier than `cycle_time`
- **THEN** parsing fails with `VALID_TIME_OUTSIDE_RUN_WINDOW`, the run is marked failed, and no row is written to `hydro.river_timeseries`

#### Scenario: A row after the end time is refused

- **WHEN** a forecast product contains a row whose `valid_time` is later than `end_time`
- **THEN** parsing fails with `VALID_TIME_OUTSIDE_RUN_WINDOW`, the run is marked failed, and no row is written

#### Scenario: Rows on the bounds are accepted

- **WHEN** a forecast product's first row is at `cycle_time` and its last row is at `end_time`
- **THEN** the product parses and its rows are written
