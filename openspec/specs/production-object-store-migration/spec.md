# production-object-store-migration Specification

## Purpose
Define how production Basins packages move into object-store storage. This covers reuse of the M9 copied-data evidence, extension of M9 publication to production-like storage, safe publish/import rollback, and the path-safety resource guarantees of the production-closure validators that check that storage.

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

### Requirement: Object-store package closure extends M9 publication to production-like storage

The system SHALL reuse M9 Basins package publication against production-like object storage and verify stored object bytes before registry/API/runtime consumption.

#### Scenario: Published package is verified from object storage

- **WHEN** a Basins package is published to the configured object store prefix
- **THEN** manifest URI, package URI, per-file checksums, and package checksum are verified from stored bytes
- **AND** default fast evidence prepares local registry import sources and API/runtime contract responses using stable object URIs rather than local development source paths
- **AND** when live registry import is explicitly enabled, the registry DB import runs against the configured database URL and missing or failed import evidence blocks validation
- **AND** evidence explicitly records whether API contract smoke was sourced from local import sources or live registry import, and does not claim live DB/API success unless those integrations actually ran

### Requirement: Publish/import rollback is safe

The system SHALL provide cleanup or rollback evidence for failed object-store publication and registry import attempts.

#### Scenario: Failed publish/import leaves no ambiguous active model

- **WHEN** package publication or registry import fails after partial work
- **THEN** the failure evidence identifies written object keys or DB rows
- **AND** cleanup/rollback can remove or quarantine partial artifacts
- **AND** no new model becomes active without an explicit activation action

### Requirement: Runtime staging prefix open releases its descriptor on every rejection

Every owner of the runtime staging prefix directory open (`_open_runtime_prefix_dir`) SHALL close the directory descriptor exactly once on every exception raised after `os.open()` succeeds. This covers a post-open `fstat` failure, a containment or no-follow rejection, and a not-a-directory rejection. The owner SHALL then raise the same `ProductionObjectStoreValidationError` code and message as before. A failure to close SHALL NOT replace that error. On success, the owner SHALL return a live descriptor that is owned by the caller.

#### Scenario: Containment rejection after open closes the descriptor

- **WHEN** the directory opens but `stat_no_follow()` rejects it because it lies outside the containment root
- **THEN** the owner raises `PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE`, as before
- **AND** the descriptor that was opened is closed

#### Scenario: Not-a-directory rejection after open closes the descriptor

- **WHEN** the opened descriptor is not a directory
- **THEN** the owner raises `Runtime staging prefix is not a directory: <path>` with the existing code
- **AND** the descriptor is closed exactly once

#### Scenario: Success returns a live descriptor

- **WHEN** every check passes
- **THEN** the returned descriptor is open, and the caller's existing cleanup closes it once
