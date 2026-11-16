# display-cutover-rehearsal-receipt Specification

## Purpose
TBD - created by archiving change direct-grid-display-cutover. Update Purpose after archive.

## Requirements

### Requirement: Flow-curve cross-cutover continuity receipt is explicitly deferred, not silently skipped

The change SHALL explicitly record that the flow-curve cross-cutover continuity receipt is DEFERRED to the pilot's first real cutover, so the absence is a recorded deferral bound to backfill and is never misread as a passed verification.

#### Scenario: deferral is written into the receipt

- **WHEN** the rehearsal receipt is assembled
- **THEN** it records the flow-curve cross-cutover continuity receipt as DEFERRED-to-pilot
- **AND** it records the rationale that no production basin is activated here, so no real cross-cutover flow curve exists to sample.

#### Scenario: deferral is a recorded absence, not a certification gap

- **WHEN** the change is certified
- **THEN** the deferred flow-curve receipt is declared a recorded absence of evidence, not a certification gap
- **AND** it is bound to be backfilled at the pilot's first real cutover, so the deferral cannot be misread as a passed verification.
