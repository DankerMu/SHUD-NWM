## ADDED Requirements

### Requirement: Published packages never rewrite calibrated values

The system SHALL publish calibration files byte-identical to their source. No
publication path may alter a calibrated parameter value on the grounds that it
falls outside an operational bound.

Publication MAY still repair a *missing* required file by supplying a template
into a private staging copy, because that path adds an absent artifact rather
than overriding a value a human chose. Any such repair SHALL be recorded in the
package manifest.

#### Scenario: A calibration multiplier outside any historical bound is published unchanged

- **WHEN** a Basins model's `cfg.calib` declares `SOIL_ALPHA` or `GEOL_DMAC`
  whose product with the corresponding `para.*` column maximum exceeds any
  previously enforced operational bound
- **THEN** the published package's `cfg.calib` SHALL be byte-identical to the
  source `cfg.calib`
- **AND** publication SHALL NOT refuse on the grounds of that bound
- **AND** the package manifest SHALL record no calibration repair

#### Scenario: Publication is a pure copy with respect to calibration

- **WHEN** a Basins model is published twice from an unchanged source
- **THEN** both packages' calibration files SHALL be byte-identical to the
  source and to each other

#### Scenario: A missing radiation template is still supplied and recorded

- **WHEN** a Basins model is missing only `*.tsd.rl` and template repair is
  requested
- **THEN** the package SHALL contain the supplied template
- **AND** the package manifest SHALL record the repair
- **AND** the model's calibration files SHALL remain byte-identical to source
