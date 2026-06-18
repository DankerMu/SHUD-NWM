## MODIFIED Requirements

### Requirement: SHUD forcing package is produced

The system SHALL materialize SHUD-ready forcing files from persisted station forcing using the processed basin's file contract, and the `PRCP` column SHALL use a single output unit that matches the SHUD runtime consumer contract across all forecast sources.

#### Scenario: PRCP output unit matches the SHUD contract

- **WHEN** forcing files are generated for any source
- **THEN** the `PRCP` value and recorded unit follow the verified SHUD `qhh.tsd.forc` consumer contract (the unit documented in `SHUD/VersionUpdate.md` and the rSHUD/AutoSHUD ingestion contract)
- **AND** `OUTPUT_UNITS["PRCP"]` reflects that same unit.

#### Scenario: All sources agree on the PRCP unit

- **WHEN** GFS (`mm`), ERA5 (`mm/day`), and IFS (per-step `mm`) canonical precipitation are converted for forcing
- **THEN** each source's `_precip_to_timestep_factor` produces the same output unit for `PRCP`
- **AND** the per-source numeric magnitude is pinned by regression tests at representative time steps so no source diverges by a `24 / step_hours` factor.

#### Scenario: Canonical precip unit must map to a documented conversion

- **WHEN** a canonical precipitation product carries a unit accepted by `EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]`
- **THEN** that unit maps to exactly one documented output conversion in `_precip_to_timestep_factor`
- **AND** an accepted unit without an explicit conversion branch is rejected rather than silently passed through as a per-step amount.

#### Scenario: Unknown step blocks rate conversion

- **WHEN** a source whose conversion factor depends on the native time step has an unknown, zero, or non-finite step
- **THEN** forcing generation fails with an invalid-step blocker
- **AND** no forcing values are written for that cycle.
