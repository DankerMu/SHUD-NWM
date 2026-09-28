## MODIFIED Requirements

### Requirement: A database built from db/migrations SHALL reproduce node-27 production's catalog

For the application schemas (`core`, `met`, `hydro`, `map`, `ops`, `public`, and any retired schema that still exists), a throwaway database built with all migrations and node-27 production SHALL have the same schemas, relations, indexes (by `indexdef`), constraints (by definition), columns (type, nullability, default, generation), enum types (labels in order), non-extension functions, triggers, hypertables and extensions.

#### Scenario: hydro_run partial indexes match the ledger

- **WHEN** `000063` has been applied and `000065` has not
- **THEN** each of `hydro_run_latest_ready_run_idx`, `hydro_run_qhh_latest_candidate_idx`, `hydro_run_qhh_latest_candidate_parsed_idx`, `hydro_run_display_product_basin_status_idx`, `hydro_run_display_ready_candidate_idx` and `hydro_run_display_ready_basin_status_idx` SHALL have the column list and the `status IN ('succeeded', 'parsed', 'published')` predicate of its defining migration, on a fresh database and on a database whose index carried a stale predicate

#### Scenario: Each hydro_run index definition exists once

- **WHEN** `000065` has been applied
- **THEN** `hydro_run_qhh_latest_candidate_parsed_idx`, `hydro_run_display_ready_candidate_idx` and `hydro_run_display_ready_basin_status_idx` SHALL NOT exist, `hydro_run_qhh_latest_candidate_idx` and `hydro_run_display_product_basin_status_idx` SHALL keep their `000063` definitions, and no two `hydro.hydro_run` indexes SHALL share one definition

#### Scenario: The status-free forecast candidate index exists

- **WHEN** `000065` has been applied, including a rerun after a failed `CREATE INDEX CONCURRENTLY` left an INVALID copy
- **THEN** exactly one valid partial index on `hydro.hydro_run` led by `basin_version_id` with predicate `run_type = 'forecast' AND cycle_time IS NOT NULL` and no `status` predicate SHALL exist

#### Scenario: run_status has the same labels in the same order

- **WHEN** `000062` has been applied
- **THEN** `hydro.run_status` SHALL list `created, staged, pending, submitted, running, succeeded, parsed, frequency_done, published, failed, cancelled, superseded` in `enumsortorder`, and `frequency_done` SHALL remain a convergence-only value that no writer produces

#### Scenario: The retired flood schema is gone

- **WHEN** `000064` has been applied to a database whose `flood` tables are empty
- **THEN** the `flood` schema and its tables, indexes, constraints and triggers SHALL no longer exist

#### Scenario: A populated or depended-on flood object stops the drop

- **WHEN** `000064` runs while any `flood` table has a row, or while an object outside `flood` depends on a `flood` table
- **THEN** the migration SHALL fail, SHALL drop nothing, and SHALL leave no ledger row

#### Scenario: A failed index rebuild is safe to rerun

- **WHEN** a `CREATE INDEX CONCURRENTLY` of `000063` fails and leaves an INVALID index, so `000063` and every later migration (including `000065`) are still pending
- **THEN** rerunning the pending migrations in order SHALL drop the INVALID index and rebuild it, and the result SHALL equal a clean run through `000065`
