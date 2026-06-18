# Database Migration

Capability: `database-migration`
Status: draft
Parent: m0-engineering-init

## ADDED Requirements

### Requirement: Migration files are ordered and cover the complete schema

The `db/migrations/` directory MUST contain exactly 10 SQL migration files, numbered sequentially from `000001` to `000010`. Each file MUST be self-contained, executable independently in order, and MUST respect foreign key dependency chains.

#### Scenario: All 10 migration files exist with correct naming

WHEN a developer lists files in `db/migrations/`
THEN the following files MUST exist in exactly this order:
  - `000001_extensions.sql`
  - `000002_schemas.sql`
  - `000003_enums.sql`
  - `000004_core.sql`
  - `000005_met.sql`
  - `000006_hydro.sql`
  - `000007_flood.sql`
  - `000008_map.sql`
  - `000009_ops.sql`
  - `000010_indexes.sql`
AND no other `.sql` files MUST exist in the directory at M0 delivery

#### Scenario: Migration files follow the dependency order

WHEN the migration runner executes files in filename order (000001 through 000010)
THEN every `REFERENCES` clause MUST refer to a table created in the current or a previously executed migration file
AND no migration file MUST produce a "relation does not exist" error
AND no migration file MUST produce a "type does not exist" error

### Requirement: Extensions are created idempotently

Migration `000001_extensions.sql` MUST enable all required PostgreSQL extensions.

#### Scenario: Required extensions are activated

WHEN `000001_extensions.sql` has been executed
THEN the following extensions MUST be present in the database:
  - `postgis` (spatial data types and functions)
  - `timescaledb` (hypertable support)
  - `pgcrypto` (UUID generation, optional but recommended)
AND querying `SELECT extname FROM pg_extension` MUST include all three names

#### Scenario: Extension creation is idempotent

WHEN `000001_extensions.sql` is executed twice in succession
THEN the second execution MUST NOT produce any errors
AND the extension versions MUST remain unchanged

### Requirement: All 6 schemas are created

Migration `000002_schemas.sql` MUST create the 6 logical schemas defined in the design document.

#### Scenario: All schemas exist after migration

WHEN `000002_schemas.sql` has been executed
THEN the following schemas MUST exist:
  - `core` -- system core objects, basins, models, versions
  - `met` -- meteorological data sources, cycles, canonical products, forcing
  - `hydro` -- SHUD runs, state snapshots, river segment results
  - `flood` -- frequency curves and return period results
  - `map` -- tile publishing, layers, styles
  - `ops` -- jobs, logs, quality control, audit
AND `CREATE SCHEMA IF NOT EXISTS` MUST be used so re-execution is safe

### Requirement: All 4 ENUM types are created with exact values

Migration `000003_enums.sql` MUST define the 4 ENUM types specified in `docs/spec/03_database_design.md` section 4.

#### Scenario: hydro.run_type ENUM contains the correct values

WHEN `000003_enums.sql` has been executed
THEN type `hydro.run_type` MUST exist
AND it MUST contain exactly these values in order: `analysis`, `forecast`, `hindcast`
AND no other values MUST be present

#### Scenario: hydro.run_status ENUM contains the correct values

WHEN `000003_enums.sql` has been executed
THEN type `hydro.run_status` MUST exist
AND it MUST contain exactly these values: `created`, `staged`, `submitted`, `running`, `succeeded`, `parsed`, `frequency_done`, `published`, `failed`, `cancelled`, `superseded`

#### Scenario: met.source_status ENUM contains the correct values

WHEN `000003_enums.sql` has been executed
THEN type `met.source_status` MUST exist
AND it MUST contain exactly these values: `enabled`, `restricted`, `planned`, `mock`, `deprecated`

#### Scenario: met.cycle_status ENUM contains the correct values

WHEN `000003_enums.sql` has been executed
THEN type `met.cycle_status` MUST exist
AND it MUST contain exactly these values: `discovered`, `downloading`, `raw_complete`, `canonical_ready`, `forcing_ready_partial`, `forcing_ready`, `forecast_running`, `parsed_partial`, `complete`, `published`, `failed_download`, `failed_convert`, `failed_forcing`, `failed_run`, `failed_parse`, `failed_publish`

#### Scenario: ENUM creation is idempotent

WHEN `000003_enums.sql` is executed twice in succession
THEN the second execution MUST NOT produce "type already exists" errors
AND the ENUM values MUST remain identical

### Requirement: Core schema tables are created with correct structure

Migration `000004_core.sql` MUST create all tables in the `core` schema with columns, types, constraints, and defaults matching `docs/spec/03_database_design.md` sections 5.1-5.5.

#### Scenario: core.basin table exists with correct columns

WHEN `000004_core.sql` has been executed
THEN table `core.basin` MUST exist with columns: `basin_id` (TEXT PK), `basin_name` (TEXT NOT NULL), `basin_group` (TEXT), `description` (TEXT), `created_at` (TIMESTAMPTZ NOT NULL DEFAULT now())

#### Scenario: core.basin_version table exists with geometry column

WHEN `000004_core.sql` has been executed
THEN table `core.basin_version` MUST exist
AND column `geom` MUST be of type `geometry(MultiPolygon, 4490)`
AND column `basin_id` MUST have a foreign key reference to `core.basin(basin_id)`
AND a GiST index `basin_version_geom_gix` MUST exist on the `geom` column

#### Scenario: core.river_network_version table exists with correct columns

WHEN `000004_core.sql` has been executed
THEN table `core.river_network_version` MUST exist
AND column `river_network_version_id` MUST be TEXT PRIMARY KEY
AND column `basin_version_id` MUST have a foreign key reference to `core.basin_version(basin_version_id)`
AND column `created_at` MUST be TIMESTAMPTZ NOT NULL DEFAULT now()

#### Scenario: core.river_segment table uses composite primary key

WHEN `000004_core.sql` has been executed
THEN table `core.river_segment` MUST exist
AND the primary key MUST be composite: `(river_segment_id, river_network_version_id)`
AND column `geom` MUST be of type `geometry(LineString, 4490)`
AND a GiST index `river_segment_geom_gix` MUST exist on the `geom` column

#### Scenario: core.model_instance table references basin_version and river_network_version

WHEN `000004_core.sql` has been executed
THEN table `core.model_instance` MUST exist
AND column `basin_version_id` MUST reference `core.basin_version(basin_version_id)`
AND column `river_network_version_id` MUST reference `core.river_network_version(river_network_version_id)`
AND column `resource_profile` MUST be of type JSONB with default `'{}'`

### Requirement: Met schema tables are created with correct structure

Migration `000005_met.sql` MUST create all tables in the `met` schema matching sections 5.6-5.13 of the database design document.

#### Scenario: All 9 met tables are created

WHEN `000005_met.sql` has been executed
THEN the following tables MUST exist in the `met` schema:
  - `met.data_source`
  - `met.forecast_cycle`
  - `met.canonical_met_product`
  - `met.met_station`
  - `met.interp_weight`
  - `met.forcing_version`
  - `met.forcing_version_component`
  - `met.forcing_station_timeseries`
  - `met.best_available_selection`

#### Scenario: met.data_source uses met.source_status ENUM

WHEN `000005_met.sql` has been executed
THEN column `met.data_source.status` MUST be of type `met.source_status`

#### Scenario: met.forecast_cycle uses met.cycle_status ENUM and has unique constraint

WHEN `000005_met.sql` has been executed
THEN column `met.forecast_cycle.status` MUST be of type `met.cycle_status`
AND a unique constraint MUST exist on `(source_id, cycle_time)`

#### Scenario: met.forcing_station_timeseries is a TimescaleDB hypertable

WHEN `000005_met.sql` has been executed
THEN table `met.forcing_station_timeseries` MUST be converted to a hypertable on column `valid_time`
AND the primary key MUST be `(forcing_version_id, station_id, variable, valid_time)`
AND querying `SELECT hypertable_name FROM timescaledb_information.hypertables WHERE hypertable_schema = 'met'` MUST include `forcing_station_timeseries`

#### Scenario: met.best_available_selection is a TimescaleDB hypertable

WHEN `000005_met.sql` has been executed
THEN table `met.best_available_selection` MUST be a hypertable on column `valid_time`
AND a unique constraint MUST exist on `(valid_time, variable)`

### Requirement: Hydro schema tables are created with correct structure

Migration `000006_hydro.sql` MUST create all tables in the `hydro` schema matching sections 5.14-5.16.

#### Scenario: hydro.hydro_run uses both ENUM types

WHEN `000006_hydro.sql` has been executed
THEN table `hydro.hydro_run` MUST exist
AND column `run_type` MUST be of type `hydro.run_type`
AND column `status` MUST be of type `hydro.run_status`
AND column `model_id` MUST reference `core.model_instance(model_id)`
AND column `forcing_version_id` MUST reference `met.forcing_version(forcing_version_id)` (nullable)

#### Scenario: hydro.state_snapshot references hydro_run

WHEN `000006_hydro.sql` has been executed
THEN table `hydro.state_snapshot` MUST exist
AND column `run_id` MUST reference `hydro.hydro_run(run_id)`
AND a unique constraint MUST exist on `(model_id, valid_time)`

#### Scenario: hydro.river_timeseries is a hypertable with composite foreign key

WHEN `000006_hydro.sql` has been executed
THEN table `hydro.river_timeseries` MUST exist
AND the primary key MUST be `(run_id, river_network_version_id, river_segment_id, variable, valid_time)`
AND a composite foreign key `(river_segment_id, river_network_version_id)` MUST reference `core.river_segment(river_segment_id, river_network_version_id)`
AND the table MUST be converted to a hypertable on column `valid_time`
AND index `river_ts_segment_time_idx` MUST exist on `(river_segment_id, variable, valid_time DESC)`

### Requirement: Flood schema tables are created with correct structure

Migration `000007_flood.sql` MUST create all tables in the `flood` schema matching sections 5.17-5.18.

#### Scenario: flood.flood_frequency_curve has correct unique constraint

WHEN `000007_flood.sql` has been executed
THEN table `flood.flood_frequency_curve` MUST exist
AND a unique constraint MUST exist on `(model_id, river_network_version_id, river_segment_id, duration, method, sample_period_start, sample_period_end)`
AND columns `q2`, `q5`, `q10`, `q20`, `q50`, `q100` MUST be of type DOUBLE PRECISION

#### Scenario: flood.return_period_result is a hypertable

WHEN `000007_flood.sql` has been executed
THEN table `flood.return_period_result` MUST exist
AND the primary key MUST be `(run_id, river_segment_id, duration, valid_time)`
AND the table MUST be converted to a hypertable on column `valid_time`
AND column `run_id` MUST reference `hydro.hydro_run(run_id)`

### Requirement: Map schema tables are created with correct structure

Migration `000008_map.sql` MUST create all tables in the `map` schema matching sections 5.19-5.20.

#### Scenario: map.tile_layer and map.tile_cache are created

WHEN `000008_map.sql` has been executed
THEN table `map.tile_layer` MUST exist with primary key `layer_id`
AND table `map.tile_cache` MUST exist with composite primary key `(layer_id, z, x, y)`
AND column `map.tile_cache.layer_id` MUST reference `map.tile_layer(layer_id)`
AND column `map.tile_cache.tile_data` MUST be of type BYTEA (nullable)

### Requirement: Ops schema tables are created for quality control and audit

Migration `000009_ops.sql` MUST create the operations tables defined in `docs/spec/07_devops_ops_security.md`.

#### Scenario: ops.qc_result table is created with correct structure

WHEN `000009_ops.sql` has been executed
THEN table `ops.qc_result` MUST exist
AND column `qc_id` MUST be BIGSERIAL PRIMARY KEY
AND column `checks_json` MUST be of type JSONB NOT NULL
AND column `passed` MUST be of type BOOLEAN NOT NULL
AND index `qc_result_target_idx` MUST exist on `(target_type, target_id, created_at DESC)`

#### Scenario: ops.pipeline_job table is created with correct structure

WHEN `000009_ops.sql` has been executed
THEN table `ops.pipeline_job` MUST exist
AND column `job_id` MUST be TEXT PRIMARY KEY
AND column `job_type` MUST be TEXT NOT NULL
AND column `status` MUST be TEXT NOT NULL
AND columns `submitted_at`, `started_at`, `finished_at` MUST be TIMESTAMPTZ (nullable)
AND column `retry_count` MUST be INTEGER with default 0

#### Scenario: ops.pipeline_event table is created with correct structure

WHEN `000009_ops.sql` has been executed
THEN table `ops.pipeline_event` MUST exist
AND column `event_id` MUST be BIGSERIAL PRIMARY KEY
AND column `job_id` MUST reference `ops.pipeline_job(job_id)`
AND column `event_type` MUST be TEXT NOT NULL
AND column `created_at` MUST be TIMESTAMPTZ NOT NULL DEFAULT now()

#### Scenario: ops.audit_log table is created with correct structure

WHEN `000009_ops.sql` has been executed
THEN table `ops.audit_log` MUST exist
AND column `audit_id` MUST be BIGSERIAL PRIMARY KEY
AND column `actor` MUST be TEXT NOT NULL
AND column `action` MUST be TEXT NOT NULL
AND column `target_type` MUST be TEXT NOT NULL
AND column `target_id` MUST be TEXT NOT NULL
AND column `created_at` MUST be TIMESTAMPTZ NOT NULL DEFAULT now()
AND column `detail` MUST be JSONB (nullable)

### Requirement: Performance indexes are created in the final migration

Migration `000010_indexes.sql` MUST create any composite or partial indexes not already created inline with their table definitions.

#### Scenario: Supplementary indexes are created

WHEN `000010_indexes.sql` has been executed
THEN at minimum the following indexes MUST exist (unless already created in earlier migrations):
  - `canonical_met_source_cycle_idx` on `met.canonical_met_product(source_id, cycle_time, variable)`
  - `met_station_basin_idx` on `met.met_station(basin_version_id)`
  - `river_ts_segment_time_idx` on `hydro.river_timeseries(river_segment_id, variable, valid_time DESC)`
AND all index creation statements MUST use `IF NOT EXISTS` or be guarded against duplicate creation

#### Scenario: Index migration is idempotent

WHEN `000010_indexes.sql` is executed twice
THEN the second execution MUST NOT produce errors
AND no duplicate indexes MUST be created

### Requirement: Full migration sequence is idempotent

The entire migration suite (000001 through 000010) MUST be safely re-executable.

#### Scenario: Running all migrations twice produces no errors

WHEN a developer runs `make migrate` on a fully migrated database
THEN all 10 migration files MUST execute without errors
AND the database schema MUST be identical before and after the second run
AND stdout MUST indicate that 0 new migrations were applied

#### Scenario: Table count matches the design document

WHEN all 10 migrations have been executed
THEN the total number of user tables MUST be 21 tables across core/met/hydro/flood/map schemas + 4 tables in ops schema = 25 user tables total
AND querying `SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('core','met','hydro','flood','map','ops')` MUST return 25 rows

### Requirement: Migration failure is transactional

Each migration file MUST be wrapped in a transaction by the migration runner so that partial failures do not leave the database in an inconsistent state.

#### Scenario: Failed migration rolls back and does not update tracking

WHEN a migration file fails partway through execution (e.g., due to a syntax error or constraint violation)
THEN the entire transaction for that migration MUST be rolled back
AND no partial DDL or DML changes from the failed migration MUST persist in the database
AND the `schema_migrations` tracking table MUST NOT record that migration as applied
AND re-running `make migrate` MUST re-attempt the failed migration from scratch

#### Scenario: Successful migrations before a failure are preserved

WHEN migration files 000001 through 000005 succeed but 000006 fails
THEN migrations 000001 through 000005 MUST remain applied and their changes MUST persist
AND only migration 000006 MUST be rolled back
AND `make migrate` MUST resume from 000006 on the next invocation

### Requirement: Rollback support exists for each migration

Each migration file MUST have a corresponding rollback section or companion file that reverses the migration.

#### Scenario: Rollback drops objects in reverse dependency order

WHEN a rollback is executed for migrations 000010 through 000001 (in reverse order)
THEN each rollback MUST drop the objects created by its corresponding forward migration
AND foreign key dependencies MUST NOT cause errors during rollback
AND after full rollback, the 6 schemas MAY be dropped or retained (configurable)

#### Scenario: Rollback followed by re-migration produces identical schema

WHEN a developer rolls back all migrations and then runs `make migrate`
THEN the resulting database schema MUST be identical to a fresh migration on an empty database
AND `pg_dump --schema-only` output MUST match between the two scenarios (ignoring OID differences)

### Requirement: make migrate and make reset-db targets integrate with migration files

The Makefile targets MUST invoke the migration runner correctly.

#### Scenario: make migrate applies only unapplied migrations

WHEN some migrations have already been applied
AND a new migration file is added to `db/migrations/`
AND a developer runs `make migrate`
THEN only the new migration file MUST be executed
AND previously applied migrations MUST NOT be re-executed
AND a migration tracking table (e.g., `public.schema_migrations`) MUST record which files have been applied

#### Scenario: make reset-db provides a clean slate

WHEN a developer runs `make reset-db`
THEN the database MUST be dropped and recreated
AND all 10 migrations MUST be re-applied
AND seed data MUST be re-inserted
AND the final state MUST be identical to a first-time setup
