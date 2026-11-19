## REMOVED Requirements

### Requirement: Live source configuration is explicit and redacted

**Reason**: The production met (`validate-met`) validation lane is deleted (#3221): it is no longer an acceptance mechanism and nothing in CI or on either node runs it.
**Migration**: None.

### Requirement: Live cycle download produces raw lineage

**Reason**: Replaced by "Live cycle download records raw lineage": the production met validation (`validate-met`) scenario and run-bundle clause of this requirement are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Live cycle download records raw lineage"; apart from those cuts its body and scenarios are unchanged.

### Requirement: Canonical and forcing products preserve source lineage

**Reason**: Replaced by "Canonical and forcing products keep source lineage": the production met validation (`validate-met`) scenario of this requirement are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "Canonical and forcing products keep source lineage"; apart from those cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: Live cycle download records raw lineage

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

### Requirement: Canonical and forcing products keep source lineage

The system SHALL convert live raw data into canonical products and model forcing with traceable source/QC metadata.

#### Scenario: Live cycle becomes forcing for a Basins-backed model

- **WHEN** raw live source data is converted and forcing is produced for a Basins-backed model
- **THEN** canonical product metadata records variables, units, time axis, source cycle, and object URI
- **AND** forcing QC records continuity, missing values, variable ranges, and pass/fail status
- **AND** best-available lineage records selected source per valid time or an explicit skipped/restricted reason
