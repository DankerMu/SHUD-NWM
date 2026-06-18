# staging-end-to-end-closure Specification

## Purpose
TBD - created by archiving change m10-production-closure. Update Purpose after archive.
## Requirements
### Requirement: Staging forecast chain evidence closes from source to API

The system SHALL provide a staging runbook and validation command that records bounded forecast-chain evidence from source discovery through API-visible outputs without claiming live service success unless those checks actually ran.

#### Scenario: Bounded staging forecast records queryable-output evidence

- **WHEN** a staging operator runs the production closure command with explicit source cycle, model set, object prefix, Slurm partition, and DB target
- **THEN** the evidence bundle records deterministic or consumed evidence for download, canonical conversion, forcing production, Slurm SHUD, output parsing, flood frequency calculation, and tile publication for the selected scope
- **AND** the evidence root records run_id and derived identifiers needed for existing APIs, including model_id, basin_version_id, segment_id, source/cycle_time, job_id, and layer_id
- **AND** API evidence records existing forecast-series, model detail, flood alerts, pipeline jobs, logs, and tile metadata identifier contracts, and marks live API execution false or blocked unless a real API check returned records
- **AND** every artifact can be traced to source cycle, model/version, Slurm job, QC result, and object URI

### Requirement: SHUD output QC blocks unsafe publication

The system SHALL reject malformed SHUD outputs before downstream frequency, tile, and API publication.

#### Scenario: Malformed SHUD output is blocked

- **WHEN** a staging closure run produces missing `.rivqdown`, malformed columns, NaN/Inf values, count mismatches, or time-axis mismatches
- **THEN** output parsing or QC fails with a stable error code
- **AND** downstream frequency computation, tile publication, and API publication for that run are blocked
- **AND** the closure evidence bundle records the QC failure and retained raw output/log paths

### Requirement: Frontend smoke evidence preserves staging lineage

The system SHALL record frontend smoke lineage without relying on mock-only placeholder success or claiming staging frontend readiness unless a real frontend smoke actually ran.

#### Scenario: Frontend lineage evidence is recorded

- **WHEN** the closure lane records frontend evidence for a completed or deterministic closure run
- **THEN** map, forecast curve, monitoring, and alert surfaces are represented with source, cycle, model, run_id, QC status, and publication time lineage
- **AND** the evidence explicitly records whether live frontend execution ran
- **AND** mock API routes or local-only placeholders cannot be reported as staging frontend readiness

### Requirement: Closure evidence bundle is durable

The system SHALL emit a durable evidence bundle for each staging closure attempt.

#### Scenario: Evidence bundle maps all stages

- **WHEN** a staging closure run finishes or fails
- **THEN** the evidence bundle records stage statuses, input/output URIs, DB IDs, Slurm jobs, logs, QC results, tile artifacts, frontend smoke result, and redacted config
- **AND** failed stages include stable error codes and next-step guidance

