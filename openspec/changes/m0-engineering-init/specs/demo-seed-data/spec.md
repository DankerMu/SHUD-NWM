# Capability Spec: demo-seed-data

## Context

M0 engineering initialization requires a demo dataset that allows developers to verify migrations, exercise API endpoints, and develop the frontend against realistic data. The seed must insert records into all 6 schemas (core/met/hydro/flood/map/ops) using the ID naming conventions defined in `docs/appendices/A_id_and_versioning_convention.md`. The seed is invoked via `make seed-demo` and must be idempotent so it can run repeatedly without errors or duplicate data.

---

## ADDED Requirements

### Requirement: Seed script directory structure and entry point

The seed system must live under `db/seeds/` with a clear entry point script and per-table insert files. The `make seed-demo` target in the project root Makefile must invoke the seed runner against the local development database.

#### Scenario: Developer runs `make seed-demo` for the first time after fresh migration

WHEN a developer has completed `make migrate` against a fresh database
AND runs `make seed-demo`
THEN the seed script executes without errors
AND all demo records are inserted into the database
AND the script exits with code 0
AND stdout reports the number of records inserted per table.

#### Scenario: `db/seeds/` directory contains ordered seed files

WHEN a developer inspects the `db/seeds/` directory
THEN there is a runner script (`seed_demo.py` or `seed_demo.sql`)
AND there are per-table or per-schema seed files in a well-defined execution order
AND the order respects foreign key dependencies (core tables before met, met before hydro, hydro before flood).

#### Scenario: Seed target is wired in Makefile

WHEN a developer runs `make help` or inspects the root Makefile
THEN there is a `seed-demo` target documented with a brief description
AND the target depends on or warns about `migrate` having been run first.

---

### Requirement: Core schema seed data (basin, basin_version, river_network, river_segments, model_instance)

The seed must insert a complete demo basin hierarchy for the Yangtze River basin, including geometry data that can be rendered on a map.

#### Scenario: Demo basin record is created

WHEN the seed script runs
THEN a record exists in `core.basin` with `basin_id = 'yangtze'`
AND `basin_name` is set to a human-readable Chinese or English name
AND `created_at` is populated.

#### Scenario: Basin version with geometry is created

WHEN the seed script runs
THEN a record exists in `core.basin_version` with `basin_version_id = 'yangtze_v2026_01'`
AND `basin_id = 'yangtze'`
AND `geom` is a valid MultiPolygon in SRID 4490 representing an approximate Yangtze basin boundary
AND `active_flag = true`
AND `version_label` is set (e.g., `'v2026.01'`).

#### Scenario: River network version is created

WHEN the seed script runs
THEN a record exists in `core.river_network_version` with `river_network_version_id = 'yangtze_rivnet_v01'`
AND `basin_version_id = 'yangtze_v2026_01'`
AND `segment_count` equals the number of river_segment rows inserted (between 10 and 50).

#### Scenario: River segments with LineString geometries are created

WHEN the seed script runs
THEN between 10 and 50 records exist in `core.river_segment`
AND each `river_segment_id` follows the pattern `yangtze_rivnet_v01_riv_{zero_padded_index}` (e.g., `yangtze_rivnet_v01_riv_0001`)
AND each `river_network_version_id = 'yangtze_rivnet_v01'`
AND each row has a valid LineString geometry in SRID 4490 within the Yangtze basin extent
AND at least some rows have `downstream_segment_id` populated to form a connected network
AND `length_m` is a positive number for each segment.

#### Scenario: Model instance is created

WHEN the seed script runs
THEN a record exists in `core.model_instance` with `model_id = 'yangtze_shud_v12'`
AND `basin_version_id = 'yangtze_v2026_01'`
AND `river_network_version_id = 'yangtze_rivnet_v01'`
AND `model_package_uri` points to a valid S3 prefix (e.g., `s3://nhms/models/yangtze_shud_v12/model_package.tar.gz`)
AND `active_flag = true`
AND `shud_code_version` is set.

---

### Requirement: Met schema seed data (data_source, met_stations, forecast_cycle, forcing_version)

The seed must insert meteorological metadata that represents a mock GFS data source with associated stations and a single forecast cycle.

#### Scenario: GFS mock data source is created

WHEN the seed script runs
THEN a record exists in `met.data_source` with `source_id = 'GFS'`
AND `source_name = 'GFS Mock'`
AND `source_type = 'global_forecast'`
AND `status = 'mock'`
AND `adapter_name` is set (e.g., `'gfs_adapter'`).

#### Scenario: Met stations with Point geometries are created

WHEN the seed script runs
THEN between 3 and 5 records exist in `met.met_station`
AND each `station_id` follows a pattern like `yangtze_v2026_01_stn_0001`
AND each `basin_version_id = 'yangtze_v2026_01'`
AND each row has a valid Point geometry in SRID 4490 located within the Yangtze basin extent
AND `active_flag = true`
AND `station_role = 'forcing_proxy'`.

#### Scenario: Forecast cycle is created

WHEN the seed script runs
THEN a record exists in `met.forecast_cycle`
AND `cycle_id` follows the naming convention (e.g., `'gfs_2026050100'`)
AND `source_id = 'GFS'`
AND `cycle_time` is a valid TIMESTAMPTZ
AND `status` is set to `'complete'` or `'published'`.

#### Scenario: Mock forcing version is created

WHEN the seed script runs
THEN a record exists in `met.forcing_version`
AND `forcing_version_id` follows the convention (e.g., `'forc_gfs_2026050100_yangtze_shud_v12'`)
AND `model_id = 'yangtze_shud_v12'`
AND `source_id = 'GFS'`
AND `start_time` and `end_time` span the demo forecast period
AND `forcing_package_uri` points to a valid S3 prefix.

---

### Requirement: Hydro schema seed data (hydro_run, river_timeseries)

The seed must insert a mock hydro run and 7 days of river timeseries data to populate time-series queries and charts.

#### Scenario: Mock hydro run is created

WHEN the seed script runs
THEN a record exists in `hydro.hydro_run`
AND `run_id` follows the convention (e.g., `'fcst_gfs_2026050100_yangtze_shud_v12'`)
AND `run_type = 'forecast'`
AND `scenario_id = 'forecast_gfs_deterministic'`
AND `model_id = 'yangtze_shud_v12'`
AND `basin_version_id = 'yangtze_v2026_01'`
AND `forcing_version_id` matches the seeded forcing version
AND `status = 'published'`
AND `run_manifest_uri` points to a valid S3 path
AND `start_time` and `end_time` span 7 days.

#### Scenario: 7 days of river timeseries data is created

WHEN the seed script runs
THEN `hydro.river_timeseries` contains records for the demo run
AND data spans 7 days from `start_time` to `end_time`
AND at least the variables `q_down`, `y_stage`, and `stage` are included
AND every seeded river_segment has timeseries data
AND `valid_time` values are at hourly or sub-daily intervals
AND `unit` is set (e.g., `'m3/s'` for discharge, `'m'` for stage)
AND total row count is at minimum `num_segments * num_variables * (7 * 24)` for hourly data.

#### Scenario: Timeseries data references valid foreign keys

WHEN the seed script runs
THEN every `river_timeseries` row has a valid `run_id` referencing `hydro.hydro_run`
AND every `(river_segment_id, river_network_version_id)` pair references a valid `core.river_segment` row
AND `basin_version_id` matches the seeded basin version.

---

### Requirement: Flood schema seed data (return_period_result)

The seed must insert at least one set of return period results so that the flood warning layer can be tested.

#### Scenario: Return period results are created

WHEN the seed script runs
THEN `flood.return_period_result` contains records for the demo run
AND `run_id` matches the seeded hydro run
AND `scenario_id = 'forecast_gfs_deterministic'`
AND `river_segment_id` values reference seeded river segments
AND `duration` is set (e.g., `'1h'`)
AND `q_value` is a positive number
AND `q_unit = 'm3/s'`
AND `return_period` is populated (e.g., 5, 10, 20, 50)
AND at least some rows have `warning_level` set (e.g., `'yellow'`, `'orange'`, `'red'`).

#### Scenario: Return period results cover multiple segments

WHEN the seed script runs
THEN return period results exist for at least 5 distinct river segments
AND each segment has at least one `valid_time` entry
AND results span a realistic forecast window.

#### Scenario: Flood frequency curve seed data is created

WHEN the seed script runs
THEN `flood.flood_frequency_curve` contains at least one record for the demo basin
AND `river_segment_id` references a seeded river segment
AND `return_period` values include at least 2, 5, 10, 20, 50, and 100 years
AND `q_value` is a positive number representing the design discharge for each return period
AND `q_unit = 'm3/s'`
AND the curve data enables the flood warning layer to compute exceedance thresholds.

---

### Requirement: Map schema seed data (tile_layer)

The seed must insert at least one map tile layer record so that the layer listing endpoint returns data.

#### Scenario: At least one tile_layer record is created

WHEN the seed script runs
THEN at least 1 record exists in `map.tile_layer`
AND the record includes `layer_id`, `layer_name`, `layer_type`, and `source_config`
AND `layer_type` is one of the supported types (e.g., `'vector'`, `'raster'`)
AND the layer configuration references valid basin or run data from the demo seed.

---

### Requirement: Ops schema seed data (pipeline_job, qc_result)

The seed must insert operational monitoring records so that the pipeline and QC endpoints return data.

#### Scenario: At least one pipeline_job record is created

WHEN the seed script runs
THEN at least 1 record exists in `ops.pipeline_job`
AND the record includes `job_id`, `job_type`, `source`, `cycle_time`, `status`, `submitted_at`
AND `source` references the seeded data source (e.g., `'GFS'`)
AND `status` is a valid pipeline job status (e.g., `'succeeded'`)
AND foreign key references to model_id or run_id (if present) are valid.

#### Scenario: At least one qc_result record is created

WHEN the seed script runs
THEN at least 1 record exists in `ops.qc_result`
AND the record includes `qc_id`, `qc_checkpoint`, `target_type`, `target_id`, `passed`, `severity`, `checks_json`, `created_at`
AND `target_type` is a valid entity type (e.g., `'forcing_version'`, `'hydro_run'`)
AND `target_id` references a seeded entity
AND `passed` is a boolean value.

---

### Requirement: source_id casing consistency

All seed data MUST use consistent casing for `source_id` to avoid foreign key mismatches and query failures.

#### Scenario: source_id uses uppercase consistently in database records

WHEN examining all seeded records that reference `source_id`
THEN `source_id` MUST be uppercase `'GFS'` in all tables: `met.data_source`, `met.forecast_cycle`, `met.forcing_version`
AND the run_id and forcing_version_id string patterns use lowercase `gfs` as a convention for composite ID construction (e.g., `'fcst_gfs_2026050100_yangtze_shud_v12'`)
AND object storage keys (S3 URI path components) use lowercase `gfs` (e.g., `s3://nhms/forcing/gfs/...`)
AND this distinction MUST be documented in a comment in the seed script.

---

### Requirement: Time interval semantics for timeseries data

The seed must define and apply consistent time interval semantics for all timeseries records.

#### Scenario: Timeseries uses half-open intervals with hourly points

WHEN examining all seeded `hydro.river_timeseries` records
THEN the time range MUST use half-open interval semantics: `[start_time, end_time)`
AND `start_time` is inclusive (the first `valid_time` equals `start_time`)
AND `end_time` is exclusive (no `valid_time` equals `end_time`)
AND consecutive `valid_time` values are spaced at exactly 1-hour intervals
AND this convention MUST be documented in the seed script header or README.

---

### Requirement: ID naming convention compliance

All seeded IDs must strictly follow the conventions defined in `docs/appendices/A_id_and_versioning_convention.md`.

#### Scenario: Basin and version IDs follow convention

WHEN examining all seeded records
THEN `basin_id` is a lowercase name string (`yangtze`)
AND `basin_version_id` matches `{basin}_vYYYY_MM` pattern
AND `river_network_version_id` matches `{basin}_rivnet_vNN` pattern
AND `model_id` matches `{basin}_shud_vNN` pattern.

#### Scenario: Run and forcing IDs follow convention

WHEN examining all seeded records
THEN `run_id` matches `{run_type}_{source}_{cycle}_{model}` pattern
AND `forcing_version_id` matches `forc_{source}_{cycle}_{model}` pattern
AND `river_segment_id` matches `{river_network_version_id}_riv_{zero_padded_index}` pattern.

#### Scenario: S3 URIs follow object storage prefix convention

WHEN examining all seeded `*_uri` fields
THEN model_package_uri starts with `s3://nhms/models/{model_id}/`
AND forcing_package_uri starts with `s3://nhms/forcing/{source}/{cycle_time}/{basin_version_id}/{model_id}/`
AND run_manifest_uri and output_uri start with `s3://nhms/runs/{run_id}/`
AND all URIs use the `nhms` bucket name.

---

### Requirement: Idempotency

The seed script must be re-runnable without producing errors or duplicate data.

#### Scenario: Running seed twice produces no errors

WHEN a developer runs `make seed-demo` twice in succession
THEN both invocations exit with code 0
AND no duplicate key violation errors are raised
AND the total record count remains the same after the second run.

#### Scenario: Seed uses upsert or conditional insert logic

WHEN the seed script executes
THEN it uses `INSERT ... ON CONFLICT DO NOTHING` or `INSERT ... ON CONFLICT DO UPDATE` (upsert)
OR it checks for record existence before inserting
AND the mechanism is consistent across all seed files.

#### Scenario: Seed works after partial failure

WHEN the seed script fails midway (e.g., database connection drop)
AND the developer fixes the issue and re-runs `make seed-demo`
THEN the script completes successfully
AND all expected records are present in the database.

---

### Requirement: Seed works after fresh migration

The seed must be compatible with a freshly migrated database that has no pre-existing data.

#### Scenario: Seed on empty database after migration

WHEN a developer runs `make migrate` to create all schemas and tables on an empty database
AND then runs `make seed-demo`
THEN all seed records are inserted successfully
AND no foreign key constraint violations occur
AND no missing schema or table errors occur.

#### Scenario: Seed insert order respects foreign key dependencies

WHEN the seed script executes
THEN `core.basin` is inserted before `core.basin_version`
AND `core.basin_version` is inserted before `core.river_network_version`
AND `core.river_network_version` is inserted before `core.river_segment`
AND `core.basin_version` is inserted before `core.model_instance`
AND `met.data_source` is inserted before `met.forecast_cycle`
AND `met.data_source` is inserted before `met.forcing_version`
AND `core.model_instance` is inserted before `met.forcing_version`
AND `met.forcing_version` is inserted before `hydro.hydro_run`
AND `hydro.hydro_run` is inserted before `hydro.river_timeseries`
AND `hydro.hydro_run` is inserted before `flood.return_period_result`.
