## MODIFIED Requirements

### Requirement: Basin retirement on node-27 runs in a fixed, receipt-gated order

The node-27 basin retirement tool SHALL run the steps `exclude`, `supersede`, `deactivate`, `verify` in that
order for one basin version per run, SHALL write one receipt per completed step into a directory of that
basin version under the succession, and MUST refuse a step whose preceding step has no receipt. It MUST
refuse to run any step unless the compute role's succession of the same id has finished with kind `remove_basin`,
the plan removed at least one model of the basin version, and the canonical scheduler manifest holds no row
of the basin: neither of the basin version nor of any other version of the same basin.

#### Scenario: Full retirement

- **WHEN** the tool is applied for a basin version whose removal from the compute role's scheduler has finished
- **THEN** the four step receipts are written in order
- **AND** the basin key is in `AUTOPIPE_EXCLUDE_BASINS`, the runs of the basin version in `succeeded`,
  `parsed` or `published` are `superseded`, and no `core.model_instance` row of it is active

#### Scenario: The node-22 removal has not finished

- **WHEN** the succession has no finish receipt of kind `remove_basin`
- **THEN** the tool refuses before any file or database write

#### Scenario: Another version of the basin is still scheduled

- **WHEN** the manifest holds a row of the same basin under another basin version
- **THEN** the tool refuses before any file or database write

#### Scenario: Two basins of one succession

- **WHEN** the tool is run for a second basin version of the same succession
- **THEN** its steps run and its receipts and backups are separate from those of the first

#### Scenario: A step before its predecessor

- **WHEN** `supersede`, `deactivate` or `verify` is reached without the receipt of the step before it
- **THEN** the step is refused and nothing is changed
