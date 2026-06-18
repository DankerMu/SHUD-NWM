# forcing-production Specification

## Purpose
TBD - created by archiving change m1-gfs-forecast-loop. Update Purpose after archive.
## Requirements
### Requirement: Met station definition loading

The forcing producer SHALL read meteorological station definitions from the `met.met_station` table. Each station record includes `station_id` (PK), `basin_version_id` (FK), `station_name`, `geom` (PostGIS Point SRID 4490), `elevation_m`, `station_role`, `active_flag`, and `properties_json`. Stations are bound to `basin_version_id`, NOT to `model_id`. Model-specific weights are resolved via `met.interp_weight`.

#### Scenario: Load stations for a specific model

- **WHEN** `nhms-forcing produce --source gfs --cycle 2026050700 --model-id yangtze_shud_v12` is called
- **THEN** the producer resolves the `basin_version_id` for the given model, then queries `met.met_station` for all stations matching that `basin_version_id` with `active_flag=true`
- **THEN** each loaded station record includes `station_id`, `station_name`, `geom` (PostGIS Point), `elevation_m`, `station_role`, and `basin_version_id`

#### Scenario: No stations found for model raises error

- **WHEN** the producer is invoked with a `model_id` whose resolved `basin_version_id` has no active entries in `met.met_station`
- **THEN** an error is raised stating no meteorological stations are defined for the given basin version
- **THEN** no forcing files are generated and no database records are created

#### Scenario: Station coordinates are validated

- **WHEN** stations are loaded from the database
- **THEN** `geom` (PostGIS Point SRID 4490) is validated: latitude within [-90, 90], longitude within [-180, 180] (extracted via `ST_Y(geom)` / `ST_X(geom)`)
- **THEN** `elevation_m` is validated to be a non-null finite number
- **THEN** any station with invalid geometry or elevation is flagged and excluded with a warning

---

### Requirement: Interpolation weight computation

The forcing producer SHALL compute or load precomputed IDW (Inverse Distance Weighting) grid-to-station interpolation weights. Weights are stored in `met.interp_weight` for reuse across forecast cycles.

#### Scenario: Compute IDW weights for new station-grid combination

- **WHEN** forcing production runs and no `met.interp_weight` records exist for the current station set and source grid
- **THEN** IDW weights are computed for each station based on the nearest grid points from the canonical product grid
- **THEN** computed weights are stored in `met.interp_weight` with fields: `weight_id` (PK serial), `source_id` (FK), `grid_id`, `model_id` (FK), `station_id` (FK), `variable`, `grid_cell_id`, `weight`, `method`
- **THEN** uniqueness is enforced on `(source_id, grid_id, model_id, station_id, variable, grid_cell_id)`

#### Scenario: Reuse precomputed weights

- **WHEN** `met.interp_weight` records already exist for the current station set and source grid
- **THEN** the producer loads existing weights without recomputation
- **THEN** loaded weights are identical to what would be computed fresh

#### Scenario: Weight values are normalized

- **WHEN** IDW weights are computed for a station
- **THEN** the sum of weights for each station equals 1.0 (within floating-point tolerance of 1e-6)
- **THEN** all individual weights are non-negative

---

### Requirement: Forcing variable generation

The forcing producer SHALL generate 6 SHUD forcing variables from canonical meteorological products: PRCP (precipitation), TEMP (temperature), RH (relative humidity), wind (wind speed), Rn (net radiation derived from shortwave_down), and Press (surface pressure). Each variable is interpolated from the canonical grid to station locations using the IDW weights.

#### Scenario: All 6 forcing variables generated from canonical inputs

- **WHEN** canonical products for all 7 standard variables are available for the requested cycle
- **THEN** the producer generates timeseries for: `PRCP`, `TEMP`, `RH`, `wind`, `Rn`, `Press`
- **THEN** `wind` is computed as `wind_speed = sqrt(wind_u_10m² + wind_v_10m²)` (explicit square root of sum of squares)
- **THEN** `Rn` is derived from `shortwave_down` using a configurable conversion method

#### Scenario: Interpolation applied per station per timestep

- **WHEN** forcing variables are generated
- **THEN** for each station, each timestep, and each variable, the value is the IDW-weighted sum of neighboring canonical grid point values
- **THEN** the result is a station-by-time matrix for each forcing variable

#### Scenario: Missing canonical product blocks generation

- **WHEN** one or more required canonical products are missing for the requested cycle
- **THEN** the producer raises an error identifying the missing variables and valid_times
- **THEN** no partial forcing output is produced
- **THEN** no `met.forcing_version` record is created (or if partially created, `checksum` is left null and `lineage_json` documents the failure)

---

### Requirement: File output

The forcing producer SHALL write output in two formats: `.tsd.forc` (SHUD-readable text format) and CSV (debug/inspection format). Files are stored at `forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/` in S3-compatible object storage.

#### Scenario: Generate .tsd.forc file in SHUD format

- **WHEN** forcing production completes successfully
- **THEN** a `.tsd.forc` file is written containing all 6 forcing variables for all stations across all timesteps
- **THEN** the file format is readable by the SHUD model runtime
- **THEN** the file is stored at `forcing/gfs/{cycle_time}/{basin_version_id}/{model_id}/{filename}.tsd.forc`

#### Scenario: Generate CSV debug file

- **WHEN** forcing production completes successfully
- **THEN** a CSV file is written in long form with columns: `valid_time`, `station_id`, `variable`, `value`, `unit`
- **THEN** the CSV file is stored alongside the `.tsd.forc` file in the same object storage prefix
- **THEN** the CSV is human-readable and importable into standard tools (pandas, Excel)

#### Scenario: Output file checksum is computed and recorded

- **WHEN** output files are uploaded to object storage
- **THEN** a SHA-256 checksum is computed for each file
- **THEN** the checksum is recorded in the corresponding database record

---

### Requirement: Data lineage

Every forcing version MUST record its provenance through `met.forcing_version_component`, linking each forcing output back to the canonical products used as input. This enables full blood lineage from raw data through canonical conversion to forcing.

#### Scenario: forcing_version record created on successful production

- **WHEN** forcing production completes successfully
- **THEN** a `met.forcing_version` record is created with fields: `forcing_version_id` (PK), `model_id` (FK), `source_id` (FK), `cycle_time`, `start_time`, `end_time`, `station_count`, `forcing_package_uri`, `checksum`, `lineage_json`
- **THEN** `lineage_json` includes provenance details; record is considered valid when all variables and stations are complete

#### Scenario: forcing_version_component links to canonical products

- **WHEN** a `met.forcing_version` record is created
- **THEN** one `met.forcing_version_component` row is created for each canonical product used
- **THEN** each component row includes: `forcing_version_id` (FK), `canonical_product_id` (FK), `variable`, `valid_time_start`, `valid_time_end`, `role` (e.g., `input`)
- **THEN** PK is `(forcing_version_id, canonical_product_id, variable)`
- **THEN** the set of components fully enumerates all canonical products consumed

#### Scenario: Lineage is traversable from forcing to raw

- **WHEN** a `met.forcing_version` record is queried with its components
- **THEN** each `met.forcing_version_component` references a `met.canonical_met_product` record
- **THEN** each `met.canonical_met_product` record contains `lineage_json` referencing raw GRIB2 files
- **THEN** the full chain forcing → canonical → raw is traceable without gaps

---

### Requirement: Forcing timeseries persistence

The forcing producer SHALL write per-station per-variable per-timestep values to `met.forcing_station_timeseries` (LONG form: one row per variable per timestep) for queryability and downstream analysis.

#### Scenario: Timeseries records written for all stations and timesteps

- **WHEN** forcing production completes successfully
- **THEN** `met.forcing_station_timeseries` contains one row per station per variable per timestep per forcing version (LONG form)
- **THEN** each row includes: `forcing_version_id`, `basin_version_id`, `station_id`, `valid_time`, `source_id`, `variable`, `value`, `unit`, `native_resolution`, `quality_flag`
- **THEN** PK is `(forcing_version_id, station_id, variable, valid_time)`

#### Scenario: Timeseries values match file output

- **WHEN** `met.forcing_station_timeseries` rows are compared with the CSV debug output
- **THEN** values match within floating-point tolerance (1e-6)
- **THEN** the row count equals `station_count * variable_count * timestep_count` (LONG form)

#### Scenario: Timeseries supports time-range queries

- **WHEN** a user queries `met.forcing_station_timeseries` for a specific station and time range
- **THEN** results are returned ordered by `valid_time`
- **THEN** the query can be filtered by `forcing_version_id`, `station_id`, and `valid_time` range

---

### Requirement: Idempotent execution

The forcing production MUST be idempotent. Re-running the CLI command for a cycle and model that has already been processed SHALL NOT create duplicate records or overwrite valid outputs.

#### Scenario: Skip production when forcing_version already exists with pass

- **WHEN** `nhms-forcing produce --source gfs --cycle 2026050700 --model-id yangtze_shud_v12` is called
- **THEN** if a `met.forcing_version` record exists with matching `source_id`, `cycle_time`, `model_id`, and a valid `checksum`
- **THEN** production is skipped and status `already_done` is returned
- **THEN** no files are re-uploaded and no database records are modified

#### Scenario: Re-production triggered when quality_flag is fail

- **WHEN** a `met.forcing_version` record exists with a null or invalid `checksum` (indicating a failed previous run)
- **THEN** the producer re-runs the full pipeline for that cycle and model
- **THEN** the existing `met.forcing_version` and related `met.forcing_version_component` records are replaced (not duplicated)
- **THEN** `met.forcing_station_timeseries` rows for the old version are replaced

#### Scenario: No duplicate records after repeated CLI invocations

- **WHEN** `nhms-forcing produce` is run three times with identical arguments
- **THEN** exactly one `met.forcing_version` record exists for the given `source_id`, `cycle_time`, and `model_id`
- **THEN** `met.forcing_station_timeseries` row count remains constant across runs

