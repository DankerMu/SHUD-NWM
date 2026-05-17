## ADDED Requirements

### Requirement: Staging forecast chain closes from source to API

The system SHALL provide a staging runbook and validation command that runs a bounded forecast chain from source discovery through API-visible outputs without mock data.

#### Scenario: Bounded staging forecast publishes queryable outputs

- **WHEN** a staging operator runs the production closure command with explicit source cycle, model set, object prefix, Slurm partition, and DB target
- **THEN** the chain completes download, canonical conversion, forcing production, Slurm SHUD run, output parsing, flood frequency calculation, and tile publication for the selected scope
- **AND** the evidence root records run_id and derived identifiers needed for existing APIs, including model_id, basin_version_id, segment_id, source/cycle_time, job_id, and layer_id
- **AND** forecast-series, model detail, flood alerts, pipeline jobs, logs, and tile metadata APIs return records when queried through their existing identifier contracts
- **AND** every artifact can be traced to source cycle, model/version, Slurm job, QC result, and object URI

### Requirement: SHUD output QC blocks unsafe publication

The system SHALL reject malformed SHUD outputs before downstream frequency, tile, and API publication.

#### Scenario: Malformed SHUD output is blocked

- **WHEN** a staging closure run produces missing `.rivqdown`, malformed columns, NaN/Inf values, count mismatches, or time-axis mismatches
- **THEN** output parsing or QC fails with a stable error code
- **AND** downstream frequency computation, tile publication, and API publication for that run are blocked
- **AND** the closure evidence bundle records the QC failure and retained raw output/log paths

### Requirement: Frontend smoke uses published staging data

The system SHALL verify that the frontend can display staging-published data without relying on local placeholder fixtures.

#### Scenario: Frontend loads staging run lineage

- **WHEN** the frontend points at the staging API for a completed closure run
- **THEN** map, forecast curve, monitoring, and alert surfaces load the run data
- **AND** visible or inspectable state includes source, cycle, model, run_id, QC status, and publication time
- **AND** no mock API route or local-only placeholder supplies the displayed data

### Requirement: Closure evidence bundle is durable

The system SHALL emit a durable evidence bundle for each staging closure attempt.

#### Scenario: Evidence bundle maps all stages

- **WHEN** a staging closure run finishes or fails
- **THEN** the evidence bundle records stage statuses, input/output URIs, DB IDs, Slurm jobs, logs, QC results, tile artifacts, frontend smoke result, and redacted config
- **AND** failed stages include stable error codes and next-step guidance
