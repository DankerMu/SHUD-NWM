# run-independent-display-catalog Specification

## Purpose
Keep run-independent display capabilities discoverable while preserving explicit hydrological run errors and catalog pagination.
## Requirements
### Requirement: Run-independent precipitation remains in the layer catalog
GET /api/v1/layers without run_id SHALL include the existing precip entry even when display_ready_run returns no run. The response SHALL NOT include run-scoped discharge, river-network or met-stations entries in that state and SHALL NOT resolve a fabricated run identity or run-dependent digest.

#### Scenario: No hydrological run exists
- **WHEN** the default layers catalog is requested and no display-ready run exists
- **THEN** HTTP 200 contains only precip, with the same entry and metadata as a ready-run-scoped catalog
- **AND** existing limit/offset pagination continues to apply to that catalog

#### Scenario: Ready and explicitly invalid runs retain their contracts
- **WHEN** a ready run is available or run_id explicitly identifies a ready, missing, or not-ready run
- **THEN** ready-run catalogs remain unchanged, missing explicit runs retain HTTP 404 RUN_NOT_FOUND, and not-ready explicit runs retain HTTP 409 DISPLAY_PRODUCT_NOT_READY

