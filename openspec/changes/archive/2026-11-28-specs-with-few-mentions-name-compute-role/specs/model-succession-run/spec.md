## MODIFIED Requirements

### Requirement: Compute-side succession steps run in a fixed, receipt-gated order

The model succession tool of the compute role SHALL run the steps of the succession kind in a fixed order: copyback,
preflight, begin, clone, publish, refresh and finish for `recalibration`; copyback, preflight, begin, publish,
refresh and finish for `cold_start` and for `add_basin`; preflight, begin, publish, refresh and finish for
`remove_basin`. It SHALL write one receipt per completed step, and SHALL refuse a step whose preceding step
in that order has no successful receipt.

#### Scenario: Publish before clone

- **WHEN** the publish step of a `recalibration` succession is reached and the clone step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Cold-start publish before begin

- **WHEN** the publish step of a `cold_start` succession is reached and the begin step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Add-basin publish before begin

- **WHEN** the publish step of an `add_basin` succession is reached and the begin step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Remove-basin publish before begin

- **WHEN** the publish step of a `remove_basin` succession is reached and the begin step has no step receipt
  for the succession
- **THEN** the tool exits non-zero naming the missing receipt and neither registry manifest changes

#### Scenario: Resume after a failure

- **WHEN** the same command is run again after the cause of a failed step was removed
- **THEN** the steps that already have a receipt are skipped and the remaining steps run
