# model-asset-version-lineage Specification

## Purpose
TBD - created by archiving change m14-model-asset-management-ui. Update Purpose after archive.
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

