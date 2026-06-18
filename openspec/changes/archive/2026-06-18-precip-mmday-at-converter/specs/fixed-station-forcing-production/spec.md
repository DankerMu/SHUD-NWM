## MODIFIED Requirements

### Requirement: SHUD forcing package is produced

The system SHALL materialize SHUD-ready forcing files from persisted station forcing, and the `PRCP` column SHALL pass canonical precipitation through unchanged, requiring all canonical precipitation to arrive as `mm/day`.

#### Scenario: Canonical precipitation passes through unchanged

- **WHEN** a canonical precipitation product with unit `mm/day` is processed for any source (GFS, IFS, ERA5)
- **THEN** `_precip_to_timestep_factor` returns `1.0`
- **AND** the emitted `PRCP` value equals the canonical value at any native step
- **AND** the recorded `PRCP` unit is `mm/day`.

#### Scenario: Per-step mm precipitation is rejected at the unit gate

- **WHEN** a canonical precipitation product carries a unit other than `mm/day` (for example per-step `mm` drifting from upstream, or `mm/s`)
- **THEN** the canonical unit gate rejects it with a unit-mismatch error before any station timeseries or `forcing_version` is written
- **AND** `EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]` is `("mm/day",)`
- **AND** the cycle status is set to `failed_forcing`.

#### Scenario: End-to-end PRCP magnitude is unchanged

- **WHEN** IFS precipitation of `2.0 mm` per step over a `3h` step is converted and produced
- **THEN** the canonical product is `16.0 mm/day`
- **AND** the produced `PRCP` value is `16.0`, equal to the pre-change end-to-end magnitude.
