# model-asset-version-lineage Specification

## Purpose
Define how model asset versions keep traceable lineage across publishes. When a republish replaces model ids, a living receipt records the new-to-old id mapping and the evidence that the new ids continued from their predecessors' state.


## Requirements

### Requirement: Model asset version lineage
The page SHALL show version history and dependency graph for model package lineage.

#### Scenario: Lineage available
WHEN version/dependency data is available
THEN timeline and graph show model, mesh, river, calibration, package checksum, and source lineage

#### Scenario: Lineage partial
WHEN some dependency fields are missing
THEN graph marks missing nodes instead of inventing relationships

#### Scenario: Endpoint decision
WHEN dependency graph or product assets need fields beyond existing model detail
THEN an endpoint decision is documented before adding backend/API/OpenAPI changes

### Requirement: A republish that replaces model ids is recorded in a living receipt

When a republish replaces the production model ids of a basin, the replacement SHALL be recorded in a living repository receipt. The receipt SHALL contain, for each replaced row, the basin, the source, the old and new `model_id`, and the package checksums, with commands that re-derive them from the manifest backups. It SHALL also contain the first-admitted-run transition evidence for each new id, and SHALL record separately any id that did not continue warm or never ran.

#### Scenario: An operator traces a current model id back to its predecessor

- **WHEN** an operator searches the repository for a current model id that a republish introduced
- **THEN** a receipt names the predecessor id, the republish issue and the first-admitted-run transition evidence
