## MODIFIED Requirements

### Requirement: IFS precipitation de-accumulation tolerance follows GRIB quantisation

The canonical converter SHALL compute the IFS `tp` de-accumulation tolerance as `max(PRECIP_NEGATIVE_NOISE_TOLERANCE_MM, sum of the known GRIB quantisation steps of the two records, in mm)`, where a record's step is `2^binaryScaleFactor / 10^decimalScaleFactor` for `grid_simple` packing with `bitsPerValue > 0`. When at least one step is known, the tolerance is an inclusive noise bound: a negative delta with `delta >= -tol * (1 + 1e-9)` (relative slack 1e-9, no absolute slack) SHALL be classified as small noise, and only a delta below that SHALL be classified as significant. When no step is known (netcdf4 fallback, missing packing keys, constant field, other packing types) the tolerance MUST equal `PRECIP_NEGATIVE_NOISE_TOLERANCE_MM`, a delta whose magnitude reaches it MUST remain significant, and the anomaly entries MUST be unchanged. Precipitation values MUST remain `max(0, delta)` rescaled to mm/day. When a step is known, the applied tolerance and the steps MUST be recorded in the precipitation anomaly entries.

#### Scenario: One-step quantisation artefact on 12-bit IFS tp stays usable

- **GIVEN** two consecutive IFS `tp` records, each with quantisation step `2^-13` m
- **WHEN** the current accumulation is exactly one step below the previous one
- **THEN** the precipitation product is written with `quality_flag` `ok`, a value of 0, and a small-negative anomaly whose `tolerance_mm` is `0.244140625`

#### Scenario: Delta exactly at the packing-derived tolerance stays usable

- **GIVEN** two consecutive IFS `tp` records, each with quantisation step `2^-14` m (0.06103515625 mm)
- **WHEN** the current accumulation is exactly two steps below the previous one, so the delta equals `-0.1220703125` mm and the tolerance is `0.1220703125` mm
- **THEN** the product is written with `quality_flag` `ok`, a value of 0, and a small-negative anomaly carrying `tolerance_mm` `0.1220703125` and both steps

#### Scenario: Floating-point noise at the bound does not flip the classification

- **GIVEN** a known tolerance `tol`
- **WHEN** the delta is `-tol * (1 + 1e-12)`, i.e. outward of the bound by rounding-sized noise
- **THEN** it is still classified as small noise with `quality_flag` `ok`

#### Scenario: A delta clearly beyond the bound is significant

- **GIVEN** records with a known quantisation step of `2^-14` m each
- **WHEN** a delta of minus 2.5 steps is converted
- **THEN** the product is flagged `warning_negative_precip`

#### Scenario: Real accumulation regression is still flagged

- **GIVEN** records with a known quantisation step
- **WHEN** a delta of minus three steps is converted
- **THEN** the product is flagged `warning_negative_precip`

#### Scenario: Records without packing information keep the env tolerance

- **GIVEN** IFS `tp` records with no quantisation step
- **WHEN** a delta of minus 0.122 mm, or exactly minus the 0.1 mm env floor, is converted with the default env floor of 0.1 mm
- **THEN** the product is flagged `warning_negative_precip` and its anomaly entry is identical to the one emitted before this change

#### Scenario: Constant fields carry no quantisation step

- **GIVEN** a GRIB2 `tp` message encoded as a constant field (`bitsPerValue` 0)
- **WHEN** the converter reads it
- **THEN** the record's quantisation step is unknown and contributes nothing to the tolerance
