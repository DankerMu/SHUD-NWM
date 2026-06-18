# live-meteorology-ingestion Specification

## Purpose
TBD - created by archiving change m10-production-closure. Update Purpose after archive.
## Requirements
### Requirement: Live source configuration is explicit and redacted

The system SHALL define production-ready source configuration for GFS, IFS, ERA5, and restricted CLDAS without exposing credentials.

#### Scenario: Source config reports enabled and restricted states

- **WHEN** source configuration is inspected for production closure
- **THEN** each source reports enabled, disabled, or restricted status with reason
- **AND** each source reports execution mode as `deterministic_fixture`, `live_executed`, `skipped`, `restricted`, or `not_executed`
- **AND** credential values, tokens, passwords, and signed URLs are redacted from logs, manifests, API payloads, and evidence

#### Scenario: Missing live credentials do not block fast validation

- **WHEN** production met validation runs without live source credentials or external network access
- **THEN** deterministic production-like fixture sources are used or the source is reported as skipped/restricted
- **AND** the evidence records that live execution was not performed
- **AND** the command does not claim live GFS, IFS, ERA5, or CLDAS success

### Requirement: Live cycle download produces raw lineage

The system SHALL discover and download at least one available live or production-like GFS/IFS/ERA5 cycle with manifest and QC evidence.

#### Scenario: Live source cycle is downloaded and checked

- **WHEN** a configured source cycle is available
- **THEN** raw files are downloaded or confirmed present under the object-store raw prefix
- **AND** file count, size, checksum, retry count, source URL identity, cycle time, and status are recorded
- **AND** incomplete or unavailable cycles fail with stable source-status evidence rather than silent success

#### Scenario: Source download evidence is bounded and redacted

- **WHEN** raw source files are enumerated, downloaded, or verified
- **THEN** manifest enumeration, per-file reads/downloads, retry/backoff, network timeouts, forecast-hour counts, and evidence payload sizes are bounded
- **AND** source URLs, object prefixes, stdout, and evidence redact credential-shaped values
- **AND** existing raw/canonical/forcing evidence for a different run is not overwritten unless the caller explicitly opts into replacing the same run bundle

#### Scenario: Production met validation is run-scoped and idempotent

- **WHEN** validation writes raw, canonical, forcing, or evidence objects for a run
- **THEN** validation-created objects stay under the current run identifier or evidence lane directory
- **AND** validation refuses to overwrite objects from a different run
- **AND** replacing an existing same-run bundle requires explicit force or cleanup behavior that is recorded in evidence

### Requirement: Canonical and forcing products preserve source lineage

The system SHALL convert live raw data into canonical products and model forcing with traceable source/QC metadata.

#### Scenario: Live cycle becomes forcing for a Basins-backed model

- **WHEN** raw live source data is converted and forcing is produced for a Basins-backed model
- **THEN** canonical product metadata records variables, units, time axis, source cycle, and object URI
- **AND** forcing QC records continuity, missing values, variable ranges, and pass/fail status
- **AND** best-available lineage records selected source per valid time or an explicit skipped/restricted reason

#### Scenario: Malformed source data blocks downstream forcing readiness

- **WHEN** raw or canonical source data is missing required variables, has malformed time coordinates, contains non-finite values, or violates configured ranges
- **THEN** production met validation records stable QC failure evidence
- **AND** downstream forcing readiness is not reported as successful for that source cycle
- **AND** successful sibling source evidence remains intact

