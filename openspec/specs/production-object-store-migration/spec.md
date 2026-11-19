# production-object-store-migration Specification

## Purpose

Define how production Basins packages move into object-store storage: reuse of the M9 copied-data evidence, and publish/import rollback that is safe.

## Requirements

### Requirement: Production Basins migration reuses M9 copied-data evidence

The system SHALL reuse the M9 Basins migration-report capability in a production-like environment, rejecting symlink-only roots as production evidence and accepting copied roots with count/checksum evidence.

#### Scenario: Copied Basins root passes production readiness

- **WHEN** production migration validation runs against a copied Basins root
- **THEN** the migration report records file count, byte count, inventory checksum, source metadata, target metadata, and `production_ready=true`
- **AND** the report does not rely on `data/Basins` being a development symlink

#### Scenario: Symlink-only Basins root fails production readiness

- **WHEN** production migration validation runs against a symlink Basins root
- **THEN** validation fails with a stable error code
- **AND** no production-ready evidence bundle is emitted

### Requirement: Publish/import rollback is safe

The system SHALL provide cleanup or rollback evidence for failed object-store publication and registry import attempts.

#### Scenario: Failed publish/import leaves no ambiguous active model

- **WHEN** package publication or registry import fails after partial work
- **THEN** the failure evidence identifies written object keys or DB rows
- **AND** cleanup/rollback can remove or quarantine partial artifacts
- **AND** no new model becomes active without an explicit activation action
