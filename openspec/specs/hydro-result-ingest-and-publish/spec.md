# hydro-result-ingest-and-publish Specification

## Purpose
TBD - created by archiving change m23-qhh-22-production-automation. Update Purpose after archive.
## Requirements
### Requirement: SHUD output ingestion
The system SHALL parse real SHUD outputs into hydro database tables using stable QHH output identities.

#### Scenario: Hydro run created
- **WHEN** SHUD output parsing succeeds for a run/model/source/cycle
- **THEN** the system creates or updates `hydro.hydro_run` with run identity, model identity, source/cycle, forcing version, status, output artifact references, and quality metadata
- **AND** it does not create duplicate terminal hydro runs for the same candidate identity.

#### Scenario: River timeseries persisted
- **WHEN** parsed SHUD river outputs contain discharge values
- **THEN** the system writes `hydro.river_timeseries` rows mapped to stable river/segment identities
- **AND** evidence includes parsed row count, segment count, time range, units, and missing segment reasons if any.

#### Scenario: Parse failure is explicit
- **WHEN** expected SHUD outputs are missing, malformed, or cannot be mapped to known output identities
- **THEN** parse records a typed failed or blocked state
- **AND** no latest display product is marked ready for that run.

### Requirement: Parsed q_down publication
The system SHALL publish node-27-readable q_down display manifests and logs after successful parse without requiring flood-frequency products to exist.

#### Scenario: Parsed display artifacts written
- **WHEN** parse and publish complete
- **THEN** q_down display products, run manifest, station/river metadata, and bounded logs are written under the configured published artifact root
- **AND** DB records reference supported `published://`, publish-root `file://`, or allowlisted object-store URIs.

#### Scenario: Frequency products unavailable
- **WHEN** parsed SHUD discharge exists but flood frequency curves, return-period results, or warning thresholds are absent
- **THEN** the run may publish q_down display artifacts with explicit frequency/warning unavailable quality metadata
- **AND** it does not fabricate flood return periods, warning levels, or full frequency publication readiness.

#### Scenario: Frequency products ready
- **WHEN** parsed discharge and required frequency/flood dependencies are both available
- **THEN** frequency or flood products may be marked ready according to their existing publish contract
- **AND** their readiness is separate from parsed q_down display readiness.

#### Scenario: Private workspace paths rejected
- **WHEN** a publish candidate references workspace-only, scratch-only, or non-allowlisted local paths
- **THEN** publication fails with a display-boundary blocker
- **AND** node 27 is not required to mount private compute workspace paths.

#### Scenario: Publish identity is strict
- **WHEN** publication writes latest-product or display manifest state
- **THEN** product identity includes `run_id`, `source`, `cycle_time`, `model_id`, basin version, river network version, forcing version, station count, and segment count
- **AND** downstream 27 evidence can verify it consumed the same run produced by node 22.

### Requirement: Pipeline jobs and events cover every stage
The production path SHALL persist stage/job/event records for download, convert, forcing, SHUD forecast, parse, and publish.

#### Scenario: Stage records complete
- **WHEN** a production cycle finishes, partially finishes, or blocks
- **THEN** `ops.pipeline_job` and `ops.pipeline_event` contain stage status, timestamps, retry count, error code/message, artifact references, and log URI where applicable
- **AND** `/ops` display can explain the run from persisted pipeline state rather than diagnostic script JSON.

#### Scenario: Terminal state is truthful
- **WHEN** any required stage is blocked or failed
- **THEN** the aggregate run status is blocked, failed, or partial according to policy
- **AND** no downstream table or published manifest reports a full pass.

