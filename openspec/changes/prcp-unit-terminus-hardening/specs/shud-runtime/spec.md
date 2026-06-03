# SHUD Runtime Adapter

Capability: `shud-runtime`
Status: draft
Parent: prcp-unit-terminus-hardening

## MODIFIED Requirements

### Requirement: Workspace preparation

The SHUD runtime adapter MUST prepare a local workspace directory before execution by pulling all required artifacts from object storage, verifying forcing package checksums, and asserting the forcing package's declared `PRCP` unit is `mm/day` before staging forcing into the SHUD `PRCP` column. The unit assertion MUST reuse the package manifest already fetched for checksum verification and MUST NOT introduce an additional network fetch. Missing unit metadata (legacy packages) MUST be tolerated; only an explicitly declared non-`mm/day` PRCP unit MUST fail the run.

#### Scenario: Staging rejects an explicit non-mm/day PRCP unit

- **WHEN** the forcing package manifest declares `units["PRCP"]` equal to a value other than `mm/day` (for example per-step `mm`)
- **THEN** the adapter MUST raise `SHUDRuntimeError` with error code `FORCING_PRCP_UNIT_MISMATCH`
- **AND** the error message MUST include both the observed unit and the expected `mm/day`
- **AND** no forcing files MUST be staged into the SHUD workspace.

#### Scenario: Staging accepts a mm/day PRCP unit

- **WHEN** the forcing package manifest declares `units["PRCP"]` equal to `mm/day`
- **THEN** the adapter MUST stage the forcing package normally
- **AND** the staged SHUD project forcing files MUST be present in the workspace.

#### Scenario: Staging tolerates missing unit metadata

- **WHEN** the forcing package manifest has no `units` block, or no `PRCP` entry within it
- **THEN** the adapter MUST NOT raise a unit-mismatch error
- **AND** the forcing package MUST be staged normally (backward compatibility with legacy packages).

#### Scenario: Unit assertion reuses the already-fetched package manifest

- **WHEN** the adapter verifies forcing package checksums for a manifest carrying `forcing.package_manifest_uri`
- **THEN** the PRCP unit assertion MUST read the same package manifest URI rather than issuing an additional remote fetch of a different artifact
- **AND** an unreadable package manifest MUST surface as `FORCING_PACKAGE_MANIFEST_READ_FAILED`
- **AND** a package manifest that is not valid JSON MUST surface as `FORCING_PACKAGE_MANIFEST_INVALID`.
