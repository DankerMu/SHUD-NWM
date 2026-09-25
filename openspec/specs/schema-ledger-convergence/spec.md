# schema-ledger-convergence Specification

## Purpose
Keep `db/migrations/` an exact description of node-27 production's catalog: drift is repaired only by forward migrations (never by editing an applied file), a database built from the ledger reproduces production, and the migration inventory is a checked-in fact rather than a glob the tests assert against itself (#2048, #2510).

## Requirements

### Requirement: Schema drift SHALL be repaired by forward migrations only

A migration file that has been applied to any deployed database SHALL NOT be edited, renamed or deleted to change what a database built from `db/migrations/` contains. A change to the schema SHALL be a new migration file. When `db/migrations/` and node-27 production disagree, the repair SHALL be a new forward migration that brings both to the same catalog.

#### Scenario: The convergence migrations are additions only

- **WHEN** the diff of this change is taken over `db/migrations/`
- **THEN** it SHALL list only added files, and every file present before the change SHALL be byte-identical

### Requirement: A database built from db/migrations SHALL reproduce node-27 production's catalog

For the application schemas (`core`, `met`, `hydro`, `map`, `ops`, `public`, and any retired schema that still exists), a throwaway database built with all migrations and node-27 production SHALL have the same schemas, relations, indexes (by `indexdef`), constraints (by definition), columns (type, nullability, default, generation), enum types (labels in order), non-extension functions, triggers, hypertables and extensions.

#### Scenario: hydro_run partial indexes match the ledger

- **WHEN** `000063` has been applied
- **THEN** each of `hydro_run_latest_ready_run_idx`, `hydro_run_qhh_latest_candidate_idx`, `hydro_run_qhh_latest_candidate_parsed_idx`, `hydro_run_display_product_basin_status_idx`, `hydro_run_display_ready_candidate_idx` and `hydro_run_display_ready_basin_status_idx` SHALL have the column list and the `status IN ('succeeded', 'parsed', 'published')` predicate of its defining migration, on a fresh database and on a database whose index carried a stale predicate

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

- **WHEN** a `CREATE INDEX CONCURRENTLY` of `000063` fails and leaves an INVALID index
- **THEN** rerunning the migration SHALL drop the INVALID index and rebuild it, and the result SHALL equal a clean run

### Requirement: The migration inventory SHALL be a checked-in fact

The set of migration file names SHALL be asserted against a checked-in literal list. Every name SHALL have a 6-digit zero-padded prefix, and prefixes SHALL be strictly increasing and unique. The prefix gaps SHALL equal the prefixes of the versions recorded as retired, minus the prefixes still on disk (a retired rename leaves its prefix in use).

#### Scenario: Removing or renaming a migration fails the inventory test

- **WHEN** a migration file is removed, renamed, or added without updating the list
- **THEN** the inventory test SHALL fail

#### Scenario: A duplicate or unpadded prefix fails

- **WHEN** a file with an already-used prefix, or a prefix that is not 6 zero-padded digits, is added
- **THEN** the structural test SHALL fail
