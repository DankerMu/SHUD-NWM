## ADDED Requirements

### Requirement: Provisioning is a dry-run unless applied and leaves a succession receipt

The direct-grid scheduler-registry provisioning tool SHALL write nothing to the database, the object store or
its output registry unless invoked with `--apply`. A dry-run SHALL predict each variant's `model_id` and
whether it would be inserted. Every run given a succession id SHALL write one receipt that is never
overwritten, and an applied run SHALL refuse unless a dry-run receipt of the same succession predicted exactly
the set of variants it registers.

#### Scenario: A dry-run writes nothing and predicts the model id

- **GIVEN** a baseline registry row whose direct-grid variant is not built
- **WHEN** the tool runs without `--apply`
- **THEN** the database receives only SELECT statements
- **AND** no file is created or modified under the object store except the dry-run receipt, and none at the
  output registry path
- **AND** the dry-run receipt carries the `model_id` an apply of the same inputs then registers

#### Scenario: Apply without a matching dry-run is refused

- **GIVEN** no dry-run receipt under the succession id, or one whose predicted variants differ
- **WHEN** the tool runs with `--apply`
- **THEN** it exits non-zero, the database transaction is not committed and no registry is published
- **AND** the message names the expected dry-run receipt path

#### Scenario: A receipt is never overwritten

- **GIVEN** a receipt already exists for the succession id and mode
- **WHEN** the tool runs again with the same succession id and mode
- **THEN** it fails and the existing receipt is unchanged
