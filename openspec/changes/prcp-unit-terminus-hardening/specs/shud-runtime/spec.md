# SHUD Runtime Adapter

Capability: `shud-runtime`
Status: draft
Parent: prcp-unit-terminus-hardening

## MODIFIED Requirements

### Requirement: Workspace preparation

The SHUD runtime adapter MUST prepare a local workspace directory before execution by pulling all required artifacts from object storage, verifying forcing package checksums, and applying a best-effort assertion that the forcing package's declared `PRCP` unit is `mm/day` before staging forcing into the SHUD `PRCP` column. The unit assertion MUST reuse the package manifest already fetched for checksum verification and MUST NOT introduce an additional network fetch. The assertion MUST fail the run ONLY when `units["PRCP"]` is explicitly present and, after case/whitespace normalisation, is not `mm/day`; every other condition (manifest unreadable, manifest over the read cap, manifest not valid JSON, `units` block absent, `PRCP` key absent, or `PRCP` value `None`) MUST be tolerated and the run MUST proceed, because package content integrity is already guaranteed by the checksum verified before the assertion.

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
- **THEN** the PRCP unit assertion MUST read the same package manifest URI rather than issuing an additional remote fetch of a different artifact.

#### Scenario: Staging tolerates an unreadable or over-cap package manifest

- **WHEN** reading the package manifest for the PRCP unit peek fails (object missing, transient read error) or the manifest exceeds the read cap (for example a large multi-station manifest)
- **THEN** the adapter MUST NOT raise a unit-related error
- **AND** the forcing package MUST be staged normally, because content integrity is already guaranteed by the checksum verified before the unit peek.

#### Scenario: Staging tolerates a package manifest that is not valid JSON

- **WHEN** the package manifest read for the PRCP unit peek cannot be decoded as UTF-8 JSON
- **THEN** the adapter MUST NOT raise a unit-related error
- **AND** the forcing package MUST be staged normally.
