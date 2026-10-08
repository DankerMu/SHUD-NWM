## ADDED Requirements

### Requirement: GFS interval row planning rejects a non-increasing interval end

The forcing producer SHALL plan one row per GFS interval at the interval's start, and SHALL raise a forcing production error when an interval end is not strictly after the previous interval end (the cycle time for the first interval), instead of planning duplicate row times.

#### Scenario: Interval product at the cycle time
- **WHEN** an interval variable has a product whose valid time equals the cycle time
- **THEN** planning fails with an error naming the cycle time and that valid time, and no row is produced

#### Scenario: Production-shaped input
- **WHEN** interval products exist at forecast hours 3 and 6 only
- **THEN** rows are planned at the cycle time and at hour 3, as before

### Requirement: The validate-met deterministic lane mirrors the production GFS product set

The deterministic GFS fixture of validate-met SHALL omit, at forecast hour 0, the variables the GFS adapter declares unavailable at f000, and the lane's forcing continuity expectation SHALL be the producer's interval row-time rule applied to the configured forecast hours, not to the products the producer consumed.

#### Scenario: Default deterministic lane
- **WHEN** validate-met runs its default deterministic lane for forecast hours 0 and 3
- **THEN** the GFS manifest has no `apcp` or `dswrf` entry at f000, the forcing evidence holds one row time (the cycle time), continuity passes and the lane is not blocked

#### Scenario: A canonical product is missing
- **WHEN** the lane is configured for forecast hours 0, 3 and 6 and the canonical products of hour 6 are absent
- **THEN** the continuity expectation still holds two row times and the check fails

#### Scenario: No forecast hour above zero
- **WHEN** validate-met is configured with forecast hours that contain no hour above 0
- **THEN** the configuration is refused with `PRODUCTION_MET_FORECAST_HOURS_INVALID` before anything is written
