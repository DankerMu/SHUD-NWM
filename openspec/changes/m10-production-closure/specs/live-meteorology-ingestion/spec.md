## ADDED Requirements

### Requirement: Live source configuration is explicit and redacted

The system SHALL define production-ready source configuration for GFS, IFS, ERA5, and restricted CLDAS without exposing credentials.

#### Scenario: Source config reports enabled and restricted states

- **WHEN** source configuration is inspected for production closure
- **THEN** each source reports enabled, disabled, or restricted status with reason
- **AND** credential values, tokens, passwords, and signed URLs are redacted from logs, manifests, API payloads, and evidence

### Requirement: Live cycle download produces raw lineage

The system SHALL discover and download at least one available live or production-like GFS/IFS/ERA5 cycle with manifest and QC evidence.

#### Scenario: Live source cycle is downloaded and checked

- **WHEN** a configured source cycle is available
- **THEN** raw files are downloaded or confirmed present under the object-store raw prefix
- **AND** file count, size, checksum, retry count, source URL identity, cycle time, and status are recorded
- **AND** incomplete or unavailable cycles fail with stable source-status evidence rather than silent success

### Requirement: Canonical and forcing products preserve source lineage

The system SHALL convert live raw data into canonical products and model forcing with traceable source/QC metadata.

#### Scenario: Live cycle becomes forcing for a Basins-backed model

- **WHEN** raw live source data is converted and forcing is produced for a Basins-backed model
- **THEN** canonical product metadata records variables, units, time axis, source cycle, and object URI
- **AND** forcing QC records continuity, missing values, variable ranges, and pass/fail status
- **AND** best-available lineage records selected source per valid time or an explicit skipped/restricted reason
