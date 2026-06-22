## ADDED Requirements

### Requirement: Object-store forcing-domain handoff is canonical

The system SHALL define an object-store forcing-domain handoff that contains
enough identity and payload metadata for node-27 ingest to reconstruct display
forcing readiness without querying an active node-22 database. The handoff MUST
include or reference `source_id`, `cycle_time`, `start_time`, `end_time`,
`run_id`, `model_id`, `basin_id`, `basin_version_id`, `forcing_version_id`,
package directory URI, package manifest URI/checksum, station inventory payload/checksum,
station-timeseries payload/checksum,
interpolation-weight payload/checksum, station count, and per-table row-count
evidence.

#### Scenario: Ingest reconstructs forcing domain from object-store

- **WHEN** node-27 ingest processes a run whose object-store package declares
  the forcing-domain handoff contract
- **THEN** node-27 can populate or verify `met.forcing_version`,
  `met.met_station`, `met.forcing_station_timeseries`, and `met.interp_weight`
  for that run from object-store package material and manifests
- **AND** the ingest report records the handoff manifest URI, checksum, source,
  cycle, forcing time window, model identity, basin version identity, station
  count, and row counts

#### Scenario: Incomplete handoff fails with a stable reason

- **WHEN** required forcing-domain handoff fields, temporal bounds, or payloads
  are missing, malformed, or checksum-mismatched
- **THEN** node-27 ingest MUST NOT silently fabricate forcing readiness or fall
  back to historical latest data
- **AND** the run summary includes a stable unavailable reason that names the
  missing component without leaking credentials

#### Scenario: Named basin live receipt proves completion

- **WHEN** the forcing-domain handoff is declared complete for current
  production display readiness
- **THEN** node-27 live evidence MUST include qhh and heihe forcing/display
  readiness receipts
- **AND** each receipt identifies the evidence path, basin/model/run identity,
  source/cycle, forcing handoff mode, and row-count summary without secrets
- **AND** the receipt proves readiness did not depend on implicit node-22 DB
  access or `infra/env/display.env` mirror fallback

### Requirement: Transitional node-22 mirror is explicit and audited

The system SHALL keep any transitional node-22 forcing mirror path explicit,
audited, and limited to operator-controlled compatibility runs.

#### Scenario: Mirror does not read display runtime configuration

- **WHEN** node-27 forcing mirror runs without `--node22-url` and without
  `N22_DSN`
- **THEN** it MUST NOT read `infra/env/display.env` or any display runtime
  `DATABASE_URL` as a mirror source
- **AND** it exits or reports a stable skip reason indicating that no explicit
  transitional mirror DSN was configured

#### Scenario: Explicit mirror records transition evidence

- **WHEN** node-27 forcing mirror runs with an explicit transitional mirror DSN
- **THEN** the report identifies the source as transitional mirror mode
- **AND** the report records the run id, forcing version id, mirrored table row
  counts, and read-only/mutation boundary without printing the DSN value
- **AND** local writes are limited to node-27 data-plane tables required for
  display readiness
- **AND** the operator-visible docs or issue evidence name the compatibility-only
  purpose and the removal/sunset condition for that mirror path
