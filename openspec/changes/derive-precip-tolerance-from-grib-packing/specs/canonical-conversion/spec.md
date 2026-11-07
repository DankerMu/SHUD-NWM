## ADDED Requirements

### Requirement: IFS precipitation de-accumulation tolerance follows GRIB quantisation

The canonical converter SHALL classify a negative IFS `tp` de-accumulation delta as significant only when its magnitude reaches `max(PRECIP_NEGATIVE_NOISE_TOLERANCE_MM, sum of the known GRIB quantisation steps of the two records, in mm)`, where a record's step is `2^binaryScaleFactor / 10^decimalScaleFactor` for `grid_simple` packing with `bitsPerValue > 0`. When no step is known (netcdf4 fallback, missing packing keys, constant field, other packing types) the tolerance MUST equal `PRECIP_NEGATIVE_NOISE_TOLERANCE_MM` and the anomaly entries MUST be unchanged. Precipitation values MUST remain `max(0, delta)` rescaled to mm/day. When a step is known, the applied tolerance and the steps MUST be recorded in the precipitation anomaly entries.

#### Scenario: One-step quantisation artefact on 12-bit IFS tp stays usable

- **GIVEN** two consecutive IFS `tp` records, each with quantisation step `2^-13` m
- **WHEN** the current accumulation is exactly one step below the previous one
- **THEN** the precipitation product is written with `quality_flag` `ok`, a value of 0, and a small-negative anomaly whose `tolerance_mm` is `0.244140625`

#### Scenario: Real accumulation regression is still flagged

- **GIVEN** the same records with a delta of minus three steps
- **WHEN** the lead is converted
- **THEN** the product is flagged `warning_negative_precip`

#### Scenario: Records without packing information keep the env tolerance

- **GIVEN** IFS `tp` records with no quantisation step
- **WHEN** a delta of minus 0.122 mm is converted with the default env floor of 0.1 mm
- **THEN** the product is flagged `warning_negative_precip` and its anomaly entry is identical to the one emitted before this change

#### Scenario: Constant fields carry no quantisation step

- **GIVEN** a GRIB2 `tp` message encoded as a constant field (`bitsPerValue` 0)
- **WHEN** the converter reads it
- **THEN** the record's quantisation step is unknown and contributes nothing to the tolerance
