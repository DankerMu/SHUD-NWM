## MODIFIED Requirements

### Requirement: Precipitation is converted to a daily rate at the converter

The canonical converter SHALL convert source precipitation (GFS `apcp`, IFS `tp`, ERA5 `total_precipitation`) to `mm/day` using the converter's own actual frame-to-frame step, and SHALL persist the canonical precipitation unit as `mm/day` for all forecast sources.

#### Scenario: GFS APCP is converted to mm/day

- **WHEN** GFS `apcp` is converted between two forecast hours
- **THEN** the per-step accumulation delta (in `mm`) is rescaled by `24 / step_hours`, where `step_hours` is `_step_hours(forecast_hour, previous_forecast_hour)`
- **AND** the canonical product unit is `mm/day`.

#### Scenario: GFS APCP first frame with a non-zero forecast start hour

- **WHEN** GFS `apcp` is converted for the first frame of a cycle (no previous forecast hour) and `forecast_hour > 0`
- **THEN** `step_hours` is `forecast_hour` (the since-cycle accumulation spans `0 -> forecast_hour`), mirroring the IFS first-frame semantics, rather than the shared `_step_hours` default of `1.0`
- **AND** the canonical product unit is `mm/day`.

#### Scenario: IFS precipitation is converted to mm/day

- **WHEN** IFS `tp` is converted between two forecast hours
- **THEN** the per-step accumulation delta (in `mm`) is rescaled by `24 / step_hours`, where `step_hours` is `_ifs_step_hours(forecast_hour, previous_forecast_hour)`
- **AND** the canonical product unit is `mm/day`
- **AND** the actual `step_hours` is recorded in the conversion lineage for audit.

#### Scenario: All sources agree on the canonical precip unit

- **WHEN** GFS, IFS, and ERA5 precipitation are converted
- **THEN** each source persists `prcp_rate_or_amount` in `mm/day`
- **AND** `STANDARD_UNITS`, `IFS_STANDARD_UNITS`, and `ERA5_STANDARD_UNITS` all report `mm/day` for `prcp_rate_or_amount`.

#### Scenario: Negative-delta audit preserves the raw amount

- **WHEN** a negative precipitation delta is detected during conversion
- **THEN** the recorded anomaly retains the raw `mm` delta for audit
- **AND** the emitted value is clamped to `0.0` before the `mm/day` rescale.
