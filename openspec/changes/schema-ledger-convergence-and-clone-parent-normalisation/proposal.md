## Triage

```text
Issue type: bugfix (#2048, #2392) + test (#2510)
Fixture level: expanded
Upstream suggested level: absent (#2048 names node-27 real-DB receipts; migrations + persisted state + a production drop are expanded triggers)
Blast radius: a wrong 000063/000064 corrupts production indexes or drops live data; a wrong runner check blocks every node-27 migration; a wrong #2392 predicate moves t* for clone-lineage scheduling
Selected risk packs: Migration, Error handling/rollback, Public API/CLI/script entry, Schema, Legacy compatibility, Auth/permissions, Release/operational, Documentation, PostGIS/TimescaleDB, Published NHMS artifacts (tasks.md)
Evidence floor: tasks.md Evidence Floor 1-7 (node-27 disposable-DB pytest + production post-apply catalog diff)
```

## Why

Batch M of the 10-batch serial run (master `e340dbc10`, after L2 #2629 / #2633).

- **#2048:** commit `b97c16e2` (2026-06-30) retired the frequency pipeline by rewriting or deleting 13 already-applied migration files and adding no forward migration. Applied migrations never replay, so `db/migrations/` stopped describing node-27 production, and nothing reports the gap.
  - **Live measurement (node-27, 2026-09-25, `2024a5e4e`, `nhms_display_ro`).** A throwaway database built from `db/migrations/` was compared with production over schemas, relations, indexes, constraints, columns, enums, functions, triggers, hypertables and extensions of `core`/`met`/`hydro`/`map`/`ops`/`flood`/`public`. The **entire** drift is three items:
    1. the six `hydro.hydro_run` partial indexes carry pre-`b97c16e2` predicates. Four of them (`hydro_run_latest_ready_run_idx`, `hydro_run_qhh_latest_candidate_idx`, `hydro_run_qhh_latest_candidate_parsed_idx`, `hydro_run_display_product_basin_status_idx`) do not contain `succeeded`, so a query with `status IN ('succeeded','parsed','published')` can never use them. The other two contain the extra `frequency_done`.
    2. `hydro.run_status` has 12 values in production (`frequency_done@7`, an original `CREATE TYPE` member) and 11 in the ledger. PostgreSQL cannot drop an enum value.
    3. the `flood` schema: 3 tables (one of them a hypertable), 16 indexes, 26 constraints, 1 trigger. All three tables have 0 rows, and no code under `apps/` `packages/` `services/` `workers/` references `flood.`.
  - `public.schema_migrations` has 62 rows against 55 files. 7 versions are applied but absent from disk (all removed or renamed by `b97c16e2`), and `000031` appears twice. `packages/common/migrate.py` only walks disk files, so it never sees the gap.
- **#2510:** `tests/test_migrations.py:14` computes `EXPECTED_MIGRATIONS` from the same glob that `test_all_migration_files_exist_with_expected_names` asserts against, so deleting, renaming or mis-numbering a migration stays green.
- **#2392:** the DB-plane reader `get_earliest_clone_row_for_model_source` (`packages/common/state_manager.py:914`) filters `cloned_from_model_id IS NOT NULL AND cloned_from_model_id <> model_id` on raw bytes. The file plane (`_clone_entries_for_model_source`) `.strip()`s first. A whitespace-only parent, or a self-reference padded with whitespace, can therefore win the DB plane's `LIMIT 1`. The resolver then rejects it and reports "no lineage", masking a later legitimate clone row the file plane finds.

## What Changes

- **#2048, by forward migrations only.** None of the 13 files `b97c16e2` touched is edited again.
  - `000062`: `ALTER TYPE hydro.run_status ADD VALUE IF NOT EXISTS 'frequency_done' AFTER 'parsed'`, commented as convergence-only, never a writable state. **User decision (2026-09-25):** forward `ADD VALUE`, not an accepted-deviation ADR.
  - `000063`: the six `hydro_run` partial indexes are dropped and recreated, `CONCURRENTLY`, with exactly the column lists and predicates of their defining migrations.
  - `000064`: the retired `flood` schema is dropped, fail-closed: the migration aborts if any of the three tables has a row or if anything outside `flood` depends on them. **User decision (2026-09-25):** drop via forward migration, with a `pg_dump` backup of `flood` taken before the production apply.
  - The migration runner refuses to apply anything when the ledger and the disk disagree: a ledger version missing from disk that is not one of the 7 recorded retired versions, a duplicated 6-digit prefix on disk, or a retired version that reappears on disk.
  - **User decision (2026-09-25):** no fresh-DB vs production schema diff in CI; the reason is recorded in design D5.
- **#2510:** `EXPECTED_MIGRATIONS` becomes a checked-in literal list, plus structural checks (6-digit zero-padded prefix, strictly increasing, unique).
- **#2392:** the DB-plane SQL judges `cloned_from_model_id` after the same whitespace normalisation as the file plane (`btrim` over exactly Python's `str.isspace()` set), for both the emptiness and the self-reference test. **User decision (2026-09-25):** reader-side, no CHECK constraint. The resolver's `.strip()` stays as defence in depth.

## Capabilities

### New Capabilities

- `schema-ledger-convergence`: `db/migrations/` reproduces node-27 production's catalog; drift is repaired by forward migrations only; the migration inventory is a checked-in fact.

### Modified Capabilities

- `migration-runner-lock-safety`: the runner refuses a ledger/disk mismatch before applying anything.
- `fingerprint-gated-state-clone`: both planes normalise `cloned_from_model_id` identically before judging it.
- `production-ops-readiness`: the migration-ledger requirement no longer says the drift rows are never visited; recorded retirements pass, anything else is refused.

## Impact

- `db/migrations/000062_*.sql`, `000063_*.sql`, `000064_*.sql` (new).
- `packages/common/migrate.py`: the ledger/disk check.
- `packages/common/state_manager.py`: `get_earliest_clone_row_for_model_source` SQL and both docstrings.
- `db/roles/node27_write_roles.sql` plus `tests/test_node27_write_roles.py`: the `pg_catalog.jsonb_typeof` ledger allow-list entry existed only for the `flood.run_product_quality` CHECKs and is removed with them.
- Tests: `tests/test_migrations.py`, a new real-DB convergence integration test, `tests/test_real_database_integration.py`, `tests/test_scheduler_lineage.py`, CI test routing.
- **node-27 production:** one migration run (`000062`–`000064`) after merge, **only after an explicit user go-ahead** (tasks 5.2).
