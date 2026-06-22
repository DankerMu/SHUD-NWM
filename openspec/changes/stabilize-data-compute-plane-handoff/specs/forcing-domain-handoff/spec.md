## ADDED Requirements

### Requirement: Object-store forcing-domain handoff is canonical

The system SHALL define an object-store forcing-domain handoff that contains
enough identity and payload metadata for node-27 ingest to reconstruct display
forcing readiness without querying an active node-22 database. The handoff MUST
include or reference `source_id`, `cycle_time`, `start_time`, `end_time`,
`run_id`, `model_id`, `basin_id`, `basin_version_id`, `forcing_version_id`,
package directory URI, canonical forcing package manifest URI/checksum,
forcing-domain package manifest URI/checksum, station inventory payload/checksum,
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
  count, canonical forcing package manifest checksum, forcing-domain package
  manifest checksum, and row counts
- **AND** the canonical `forcing_package_manifest_uri` points to
  `<forcing_package_uri>/forcing_package.json`
- **AND** the canonical `forcing_package_manifest_checksum_sha256` is the
  checksum used to write or verify `met.forcing_version.checksum` during the
  #644 database apply step, while
  `forcing_domain_package_manifest_checksum_sha256` only protects the handoff
  reconstruction contract

#### Scenario: Incomplete handoff fails with a stable reason

- **WHEN** required forcing-domain handoff fields, temporal bounds, or payloads
  are missing, malformed, or checksum-mismatched
- **THEN** node-27 ingest MUST NOT silently fabricate forcing readiness or fall
  back to historical latest data
- **AND** the run summary includes a stable unavailable reason that names the
  missing component without leaking credentials
- **AND** missing or malformed top-level contract, identity, temporal, or
  compatibility URI fields stop package and payload validation before any
  package/payload evidence is reported
- **AND** if package, payload, station-timeseries lattice, or row-count
  validation adds any unavailable reason, the final unavailable result MUST NOT
  expose readiness-style `payloads` or `table_row_counts` evidence

#### Scenario: Handoff payloads prove complete DB reconstruction lattice

- **WHEN** station-timeseries payload metadata declares stations, variables, and
  `time_lattice` segments containing `valid_time_start`, `valid_time_end`, and
  `native_resolution`, optionally scoped by `variable` or `variables`
- **THEN** validation MUST derive the expected
  `(station_id, variable, valid_time)` lattice from unique station inventory ids
  and each inclusive temporal segment's declared variable scope; a segment with
  no `variable`/`variables` applies to all declared variables
- **AND** each station-timeseries row-level `native_resolution` MUST match the
  `native_resolution` of the segment containing that row's
  `(variable, valid_time)` pair
- **AND** duplicate station inventory `station_id` values, duplicate declared
  station-timeseries variables, and unique station-count mismatches make the
  handoff unavailable with stable reason codes
- **AND** missing, extra, or duplicate station-timeseries tuples make the
  handoff unavailable with stable reason codes using bounded samples and counts
- **AND** row-level missing or invalid field diagnostics for
  `station_inventory`, `station_timeseries`, and `interpolation_weights` MUST be
  bounded by role/field/code counts plus limited samples instead of unbounded
  per-row reason expansion
- **AND** duplicate interpolation-weight keys over
  `(source_id, grid_id, model_id, station_id, variable, grid_cell_id)` make the
  handoff unavailable with a stable reason and bounded samples
- **AND** lattice cardinality above the validator hard limit MUST return
  `HANDOFF_TIMESERIES_LATTICE_TOO_LARGE`, not a temporal-window reason
- **AND** compatibility/provenance URIs MUST normalize to object-store keys
  bound to the declared `run_id`, `model_id`, and forcing package scope
- **AND** `forcing_package_manifest_uri` MUST normalize exactly to
  `<forcing_package_uri>/forcing_package.json` before any package or payload
  material is read
- **AND** `forcing_domain_package_manifest_uri` MUST normalize under the same
  forcing package scope and outside the package `payloads/` directory before
  any package or payload material is read

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

The system SHALL keep any transitional node-22 forcing mirror path explicit-DSN,
audited, sunset-bound, and limited to operator-controlled compatibility runs.

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
