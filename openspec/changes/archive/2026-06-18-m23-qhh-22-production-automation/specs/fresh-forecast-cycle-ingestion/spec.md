## ADDED Requirements

### Requirement: Fresh forecast cycle discovery
The system SHALL discover fresh forecast cycles for configured sources using source-specific availability and lag policies.

#### Scenario: GFS cycle available
- **WHEN** a configured GFS cycle is available within lookback and lag policy
- **THEN** scheduler evidence records the source, cycle time, selected lead window, discovery timestamp, and candidate identity
- **AND** downstream download/canonical stages can proceed for the active QHH model.

#### Scenario: Source unavailable or forbidden
- **WHEN** a source probe returns unavailable, forbidden, missing, stale, or otherwise blocked status
- **THEN** scheduler evidence records a typed unavailable/block reason and the probed cycle identity
- **AND** no canonical-ready or production-ready state is fabricated for that cycle.

#### Scenario: Source filters are auditable
- **WHEN** an operator limits sources, model ids, basin ids, lookback, lag, or max cycles
- **THEN** the scheduler evidence records those filters
- **AND** later readiness checks can distinguish reduced-scope runs from full default automation.

### Requirement: Forecast download and canonical completeness
Each accepted cycle SHALL download and canonicalize all required meteorological variables for SHUD forcing before forcing generation.

#### Scenario: Required variables complete
- **WHEN** download and canonical conversion complete for a cycle
- **THEN** `met.forecast_cycle` and `met.canonical_met_product` record complete coverage for the source-specific canonical variable ids required by the forcing producer
- **AND** GFS-ready coverage includes `prcp_rate_or_amount`, `air_temperature_2m`, `relative_humidity_2m`, `wind_u_10m`, `wind_v_10m`, `pressure_surface`, and `shortwave_down`
- **AND** IFS-ready coverage includes `prcp_rate_or_amount`, `air_temperature_2m`, `relative_humidity_2m`, `wind_u_10m`, `wind_v_10m`, `surface_pressure`, and `shortwave_down`
- **AND** evidence includes per-variable valid-time counts, lead/horizon coverage, source, cycle, checksums or object references, and canonical status.

#### Scenario: Variable coverage incomplete
- **WHEN** one or more required variables or lead times are missing **for a cycle that already has canonical rows** (`candidate_row_count > 0`)
- **THEN** canonical status is blocked or incomplete with safe missing-variable/lead details
- **AND** forcing generation and SHUD submission do not proceed for that cycle.

#### Scenario: Zero canonical rows trigger fresh full-chain ingestion
- **WHEN** an accepted cycle has no canonical rows at all (`candidate_row_count == 0`) for the source/cycle and the source policy yields a non-empty expected lead horizon
- **THEN** the generic production daemon treats it as a fresh ingestion rather than a hard block, and admits a full-chain cohort with no restart stage so the Slurm chain runs download → convert → forcing → forecast → parse → frequency → publish via the gateway
- **AND** a cycle that already has canonical rows but fails identity/variable/lead checks keeps the hard block, and an empty expected horizon or provider-unavailable readiness keeps the hard block (never reclassified as fresh).

#### Scenario: Source-specific horizon policy
- **WHEN** source-specific lead availability differs, including shorter IFS horizons
- **THEN** canonical completeness is evaluated against the configured source policy for that source/cycle
- **AND** evidence records the accepted horizon and whether the run is full scope, reduced scope, blocked, or unavailable.

#### Scenario: Repeat scan reuses completed canonical products
- **WHEN** a scheduler pass sees an already complete canonical product for the same source/cycle/object identity
- **THEN** it reuses the existing canonical product
- **AND** it does not redownload or duplicate rows unless the source identity or policy explicitly changed.

### Requirement: Forecast source errors are retryable and bounded
The system SHALL separate transient source/download errors from permanent or policy-blocked conditions.

#### Scenario: Transient download failure
- **WHEN** a download fails due to a retryable network or source error
- **THEN** the pipeline records retryable failure evidence with attempt count and next eligible retry behavior
- **AND** downstream forcing, SHUD, parse, and publish stages remain unsubmitted.

#### Scenario: Permanent or policy blocked source
- **WHEN** a source is outside policy, unsupported, or repeatedly unavailable past retry limits
- **THEN** the pipeline records a permanent or policy-blocked state
- **AND** automatic retries stop until operator policy or manual retry changes.
