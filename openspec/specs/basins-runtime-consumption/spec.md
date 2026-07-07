# basins-runtime-consumption Specification

## Purpose
TBD - created by archiving change m9-basins-model-assets. Update Purpose after archive.
## Requirements
### Requirement: Basins packages are consumable by SHUD runtime staging

The system SHALL verify that at least one Basins-backed `model_package_uri` can be staged by `SHUDRuntime` into a run workspace without requiring a live SHUD solver.

#### Scenario: Runtime dry-run stages Basins package

- **WHEN** a run manifest references an imported Basins model package and dry-run or mock runtime execution is requested
- **THEN** SHUD runtime stages the package into `runs/{run_id}/input`, finds the model control files, and returns a deterministic success or validation result without executing real `shud`

### Requirement: Basins-backed API surfaces expose model and river data

The system SHALL provide API smoke coverage showing imported Basins models and their river segments can be listed and inspected through existing model and river-segment endpoints.

#### Scenario: Active model discovery can include Basins model

- **WHEN** an imported Basins model is explicitly activated
- **THEN** model listing and active-model discovery return the Basins `model_id`, `basin_version_id`, `river_network_version_id`, `mesh_version_id`, and `model_package_uri`
- **AND** inactive Basins models remain excluded from default active listings before explicit activation

#### Scenario: River map pages can load Basins segments

- **WHEN** frontend map loading requests river segments for an imported Basins basin version
- **THEN** the backend returns paginated river features with stable IDs and geometry suitable for MapLibre rendering

### Requirement: Frontend asset-management work has real Basins data

The system SHALL provide enough Basins-backed metadata for the planned model asset management page to display basin list, model detail cards, version relationships, and package status without fabricated placeholder data.

#### Scenario: Asset management data contract is populated

- **WHEN** the frontend or API consumer requests model asset details for an imported Basins model
- **THEN** available fields include basin/model names, segment count, mesh ID, calibration ID, package URI/checksum, active flag, and source path or source URI lineage
- **AND** the model detail endpoint returns those fields through the shared API success envelope rather than requiring a frontend-local fixture patch

#### Scenario: OpenAPI and generated frontend types include Basins asset fields

- **WHEN** Basins asset detail fields are added or exposed through model APIs
- **THEN** `openapi/nhms.v1.yaml` and generated frontend API types are updated so frontend code does not rely on local placeholder-only type patches

