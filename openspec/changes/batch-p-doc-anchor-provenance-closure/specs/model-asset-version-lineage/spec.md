## ADDED Requirements

### Requirement: A republish that replaces model ids is recorded in a living receipt

When a republish replaces the production model ids of a basin, the replacement SHALL be recorded in a living repository receipt. The receipt SHALL contain, for each replaced row, the basin, the source, the old and new `model_id`, and the package checksums, with commands that re-derive them from the manifest backups. It SHALL also contain the first-admitted-run transition evidence for each new id, and SHALL record separately any id that did not continue warm or never ran.

#### Scenario: An operator traces a current model id back to its predecessor

- **WHEN** an operator searches the repository for a current model id that a republish introduced
- **THEN** a receipt names the predecessor id, the republish issue and the first-admitted-run transition evidence
