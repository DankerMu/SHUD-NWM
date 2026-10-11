## ADDED Requirements

### Requirement: The purge tool SHALL require an explicit receipt root

`scripts/node27_purge_superseded_weights.py` SHALL refuse to run without an explicit `--receipt-root`, before connecting to the database or writing anything. It SHALL NOT derive a default receipt directory from the object-store root.

#### Scenario: The operator omits the receipt root

- **WHEN** the tool is invoked without `--receipt-root`
- **THEN** it exits with a usage error and no database connection, directory or file has been created
