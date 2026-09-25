## Context

### #2048: live drift (node-27, 2026-09-25)

The probe (`evidence/catalog_diff.py`; baseline output `/home/nwm/tmp/m/catalog-diff.json` on node-27) builds a throwaway `nhms_it_<uuid>` database with the tests' `apply_migrations_from_zero` and drops it afterwards. It snapshots the same catalog views on that database and, read-only, on production. Categories: schema, relation, index, constraint, column, enum, function (non-extension), trigger, hypertable, extension. Schemas: `core`, `met`, `hydro`, `map`, `ops`, `flood`, `public`.

| category | fresh | prod | ledger-only | prod-only |
|---|---|---|---|---|
| schema | 6 | 7 | 0 | `flood` |
| relation | 46 | 49 | 0 | the 3 `flood` tables |
| index | 88 | 104 | the 6 `hydro_run` partial indexes (ledger predicate) | the same 6 (stale predicate) + 16 `flood` indexes |
| constraint | 89 | 115 | 0 | 26, all on `flood` tables |
| column | 385 | 444 | 0 | 59, all `flood` |
| enum | 10 | 10 | `run_status` (11 values) | `run_status` (12 values, `frequency_done` after `parsed`) |
| function | 4 | 4 | 0 | 0 |
| trigger | 8 | 9 | 0 | `flood.return_period_result.ts_insert_blocker` |
| hypertable | 4 | 5 | 0 | `flood.return_period_result` |
| extension | 6 | 6 | 0 | 0 |

Nothing else differs. In particular the other enums (`met.source_status`, `met.cycle_status`, `hydro.run_type`, `hydro.river_variable`, `hydro.river_unit`, `hydro.river_quality_flag`, …) are identical label-for-label and in the same order.

Live `hydro_run` predicates and the ledger's:

| index | defining file | ledger | production |
|---|---|---|---|
| `hydro_run_latest_ready_run_idx` | `000021` | `succeeded, parsed, published` | `frequency_done, published` |
| `hydro_run_qhh_latest_candidate_idx` | `000024` | same | `frequency_done, published` |
| `hydro_run_qhh_latest_candidate_parsed_idx` | `000030` | same | `parsed, frequency_done, published` |
| `hydro_run_display_product_basin_status_idx` | `000031_search_discovery_performance` | same | `parsed, frequency_done, published` |
| `hydro_run_display_ready_candidate_idx` | `000040` | same | `succeeded, parsed, frequency_done, published` |
| `hydro_run_display_ready_basin_status_idx` | `000040` | same | `succeeded, parsed, frequency_done, published` |

- `hydro.hydro_run` statuses live: `published 7578`, `superseded 2297`, `succeeded 270`, `failed 2`. `frequency_done` has 0 rows.
- Ownership: the `hydro_run` and `flood` tables belong to `nhms_ingest_rw`; the `flood` schema and `run_status` type belong to `nhms`. Production migrations run as `nhms`, which is a superuser (`docs/runbooks/production-ops/artifacts-and-storage.md:46`), via `.venv/bin/python -m packages.common.migrate`. The precedent receipt is `docs/runbooks/receipts/2026-09-12-issue-2145-geometry-generation-migration-node27.md`.
- Ledger: 62 rows; 55 files; all 55 files applied; 7 applied versions absent from disk:
  - `000007_flood.sql`
  - `000015_flood_return_period_identity_indexes.sql`
  - `000017_return_period_max_over_window_identity.sql`
  - `000020_valid_time_discovery_indexes.sql`
  - `000031_search_discovery_return_period_performance.sql` (renamed; its successor `000031_search_discovery_performance.sql` was applied again on 2026-07-06)
  - `000034_return_period_run_quality_materialization.sql`
  - `000036_run_product_quality_explicit_source.sql`

### #2048: the 13 `b97c16e2` files, both directions

The catalog diff gives the answer for each file directly, because every object those files create or alter lives in the compared categories.

| file | `b97c16e2` action | ledger has, production lacks | production has, ledger lacks |
|---|---|---|---|
| `000002_schemas` | dropped `CREATE SCHEMA flood` | — | schema `flood` |
| `000003_enums` | removed `frequency_done` from `run_status`; `hindcast` from `run_type` | — | `run_status.frequency_done` (`run_type.hindcast` was re-added by `000045`; converged) |
| `000007_flood` | deleted | — | `flood.flood_frequency_curve`, `flood.return_period_result` (hypertable) and their keys/FKs |
| `000015_flood_return_period_identity_indexes` | deleted | — | 5 `return_period_result_*` indexes, pkey reshape |
| `000017_return_period_max_over_window_identity` | deleted | — | `return_period_result.max_over_window` column + pkey |
| `000020_valid_time_discovery_indexes` | deleted | — | `return_period_result_valid_time_discovery_idx` (its `river_timeseries` sibling was dropped by `000049`; converged) |
| `000021_latest_ready_run_discovery_idx` | predicate rewritten; 2 `flood` indexes removed | ledger predicate of `hydro_run_latest_ready_run_idx` | stale predicate; `return_period_result_mvt_selected_identity_{lookup,valid_time_discovery}` |
| `000024_qhh_latest_display_product_indexes` | predicate rewritten | ledger predicate | stale predicate |
| `000030_qhh_latest_display_parsed_status_index` | predicate rewritten | ledger predicate | stale predicate |
| `000031_*` | renamed; predicate rewritten; `flood` index removed | ledger predicate of `hydro_run_display_product_basin_status_idx` | stale predicate; `return_period_result_run_quality_idx`; duplicate ledger row |
| `000034_return_period_run_quality_materialization` | deleted | — | `flood.run_product_quality` |
| `000036_run_product_quality_explicit_source` | deleted | — | `run_product_quality` CHECKs / columns |
| `000040_display_ready_succeeded_status_index` | `frequency_done` removed from 2 predicates | ledger predicates | superset predicates |

## Goals / Non-Goals

- **Goals:**
  - After `000062`–`000064` are applied, the same catalog diff against production is empty in every category.
  - The runner refuses a ledger/disk mismatch that is not a recorded retirement.
  - The migration inventory test can fail.
  - Both clone-lineage planes pick the same row.
- **Non-goals:**
  - Editing any already-applied migration file (user rule for this batch).
  - Rewriting the ledger rows of the 7 retired versions: they are true history and stay.
  - Deduplicating the `hydro_run` indexes. After convergence, three of them share one definition and two share another (Risks); filed as #2634.
  - The OpenAPI `RunStatus` (#2037's removal stands; L1 made the runtime model a `str`).
  - A CHECK constraint on `cloned_from_model_id` (user decision).

## Governing invariants

- **I1:** no file that exists in `db/migrations/` on `e340dbc10` changes in this PR (`git diff --stat e340dbc10 -- db/migrations/` lists only added files).
- **I2:** the three migrations are idempotent, both on a fresh database and on production, and a failed attempt is safe to rerun.
- **I3:** `000064` drops nothing if any `flood` table has a row, or if anything outside `flood` depends on a `flood` object.
- **I4:** for every clone-row set, the DB plane and the file plane resolve the same `LineageCutover`.
- **I5 (#2392 acceptance oracle):** `openspec/specs/cross-cycle-warm-start-chaining/spec.md`'s existing rule that disqualification is per row, not per model. A disqualified earliest row SHALL NOT disqualify the model, and `t*` SHALL resolve to the legitimate row.

## Decisions

### D1 (#2048): `000062_hydro_run_status_frequency_done_convergence.sql`

```sql
ALTER TYPE hydro.run_status ADD VALUE IF NOT EXISTS 'frequency_done' AFTER 'parsed';
```

- On production it is a no-op (the value exists at `enumsortorder` 7). On a fresh database it lands between `parsed` and `published` (`enumsortorder` 6.5), so the label order equals production's.
- The header states it is convergence-only: no writer may produce it, no reader may treat it as a display-ready state, and PR #2037's OpenAPI removal stands. It follows `000045_hydro_run_type_hindcast.sql`'s pattern.
- It is a separate file from `000063` so that no statement uses a value added in the same transaction. The runner autocommits each statement anyway (`packages/common/migrate.py:327`).

### D2 (#2048): `000063_hydro_run_partial_index_predicate_convergence.sql`

- For each of the six indexes, in the order of the table above:

  ```sql
  DROP INDEX CONCURRENTLY IF EXISTS hydro.<name>;
  CREATE INDEX CONCURRENTLY IF NOT EXISTS <name>
    ON hydro.hydro_run (<the defining file's column list>)
    WHERE <the defining file's predicate>;
  ```

- The column list and the predicate are copied byte-for-byte from the defining migration. A test pins that copy (tasks 2.2), so the two sources cannot drift.
- `CONCURRENTLY` for all six, including `000021`/`000024`, which used plain `CREATE INDEX`: `hydro_run` is a plain table (not a hypertable), and `CONCURRENTLY` avoids a write lock on production. `apply_migration` splits statements and runs each in autocommit, which `CONCURRENTLY` requires.
- **Unconditional rebuild.** It also runs on a fresh database, where the indexes are already correct. That costs nothing (the table is empty or small) and avoids a conditional that `CONCURRENTLY` cannot live inside (no `DO` block).
- **Failure and rerun.** A failed `CREATE INDEX CONCURRENTLY` (for example `lock_timeout` while waiting for an old snapshot) leaves an INVALID index and no ledger row. Because the ledger row is missing, the rerun replays the whole file: all six DROP/CREATE pairs run again (the operator sees six rebuilds), and the failed index's `DROP INDEX CONCURRENTLY IF EXISTS` removes the INVALID copy before its rebuild. The end state equals a clean run. This is why DROP precedes CREATE for each index rather than a create-new-then-rename scheme.
- Between each DROP and its CREATE the planner has one index fewer. With ~10k rows, the fallback is a sequential scan of about the same cost.

### D3 (#2048): `000064_drop_retired_flood_schema.sql`, fail-closed

One `DO` block, so the guard and the drops are one transaction:

1. For each of `flood.flood_frequency_curve`, `flood.return_period_result` and `flood.run_product_quality` that exists (`to_regclass`): `LOCK TABLE … IN ACCESS EXCLUSIVE MODE`, then `RAISE EXCEPTION` when it has any row. Taking the lock first means no insert can slip in between the count and the drop.
2. `DROP TABLE` each existing table **without `CASCADE`**, so a dependent object anywhere (a view, an FK into `flood`) aborts the migration. `flood.return_period_result` is a hypertable, and TimescaleDB drops its chunks with the table.
3. `DROP SCHEMA IF EXISTS flood` **without `CASCADE`**, so an unexpected leftover object aborts it.

- On a fresh database there is no `flood` and every step is a no-op.
- The FKs that go with the tables (`→ hydro.hydro_run(run_id)`, `→ core.model_instance(model_id)`) belong to the `flood` tables, so dropping them removes FK checks from `hydro_run`/`model_instance` deletes. That matches the ledger.
- **Sibling surface:** `db/roles/node27_write_roles.sql` generates its schema grants per existing schema, so a missing `flood` is already handled (its comment at `:204`). But its ledger allow-list trusts `pg_catalog.jsonb_typeof` only because `flood.run_product_quality`'s two `jsonb_typeof(...) = 'array'` CHECKs reference it (`:727`, `tests/test_node27_write_roles.py` `_LEDGER_ALLOW_LIST`). That entry and its reason are removed. The test's "ledger list is non-empty" assertion is replaced by an assertion that the list has no entry sourced from a retired `flood` migration.
- **Production sequencing against the role audit.** The roles script is also the mandatory audit-only check before every superuser write (`docs/runbooks/tier-node27-timeseries-storage.md` §9.6 "run the audit-only invocation BEFORE every superuser-write session", and `production-ops-readiness`). With this change's SQL, that strict audit would flag `jsonb_typeof` as untrusted while the `flood` CHECKs still exist, which is before `000064` has run. So the pre-write audit (5.3 step 2) runs from node-27's checkout **before** `git pull`. That checkout's roles SQL is master's, which still trusts `jsonb_typeof`. The order in the window is: stop timers → audit-only (old checkout) → `git pull --ff-only` → migrate → full `scripts/node27_provision_write_roles.sh` with a clean strict audit (new SQL, `flood` gone) → restart timers.
- The one-shot `scripts/ops/node27_1729_*` scripts (batch K3, already executed) read `flood` tables. They are not changed. Their integration test builds its own `flood` tables in a throwaway database, so it is unaffected.
- **Backup and rollback (production):** before the apply, `pg_dump -n flood` (schema + data, custom format) into `/home/nwm/tmp/m/`. Rollback is `pg_restore` of that dump plus `DELETE FROM public.schema_migrations WHERE version = '000064_…'`. Rolling back `000062` is impossible (and unnecessary: on production it is a no-op). `000063` has no meaningful rollback, since the old predicates are the defect.

### D4 (#2048): the runner's ledger/disk check

`packages/common/migrate.py` gains `RETIRED_LEDGER_VERSIONS: Mapping[str, str]`: the 7 names above, each with a reason naming `b97c16e2` / #2048. It also gains one check, run in `main()` after `configure_migration_session` and `ensure_schema_migrations_table`, **before any migration is applied**, and inside the existing `psycopg2.Error` handler, so that a lock or statement timeout on the ledger read gets the same diagnostics (`_report_migration_failure` with no migration name reports it as the ledger check):

- **R1:** every ledger version that has no file on disk is in `RETIRED_LEDGER_VERSIONS`.
- **R2:** no two files on disk share their first 6 characters.
- **R3:** no file on disk is named like a retired version (a retired version must not come back, and a rename back would silently skip).

- Any violation prints every offending name (no DSN or password: the existing `redact_migration_text` path) and exits 1 with nothing applied.
- The duplicate `000031` ledger row passes, because its retired half is in R1's list and its live half is on disk.
- The check reads only `public.schema_migrations`, so it can also be run read-only against production (tasks 3.2 and 5.4).
- It is a function taking the ledger versions and the migration file names, which keeps it unit-testable without a database. `main()` wires it.
- **Callers that gain the refusal:** `Makefile:20`, `scripts/run_qhh_cycle.sh:425`, `scripts/run_qhh_backend_smoke.sh:157` and the runbooks' `python -m packages.common.migrate`. On a fresh or correctly retired ledger they see no change. On an unrecorded mismatch they now get exit 1 instead of a silent pass, which is the point.
- **Not covered:** `scripts/apply_smoke_migrations.py` has its own apply loop for disposable smoke databases and does not call `main()`. It is left unguarded on purpose (non-goal): it never runs against a ledger that has history.

### D5 (#2048): no fresh-DB vs production schema diff in CI (user decision)

- A CI diff needs a checked-in production catalog snapshot and a refresh discipline (who re-captures it after every production migration, and how a stale snapshot is detected). That is a standing cost on every migration PR.
- What guards the same failure instead:
  - D4 turns a deleted or renamed applied file into a hard failure at the next production migration run.
  - I1 is a review rule for migration PRs, and the 13-file history shows why it matters.
  - This change's one-off probe, rerun post-apply (5.4), proves convergence once.
- The probe is checked in with this change as `evidence/catalog_diff.py` (it moves into the archive with it), so a manual re-audit uses the same code the receipts used.

### D6 (#2510): the inventory test

- `EXPECTED_MIGRATIONS` becomes a checked-in literal tuple of all 58 file names (55 + `000062`–`000064`), with a comment:
  - adding a migration is a two-place edit;
  - the six prefix gaps (`000007`, `000015`, `000017`, `000020`, `000034`, `000036`) are the `b97c16e2` retirements, not missing files, and are listed in `packages/common/migrate.py`'s `RETIRED_LEDGER_VERSIONS`.
- `test_all_migration_files_exist_with_expected_names` asserts `sorted(glob) == list(EXPECTED_MIGRATIONS)`. A new structural test asserts every name matches `^\d{6}_[a-z0-9_]+\.sql$`, with prefixes strictly increasing and unique.
- An agreement test pins that no `EXPECTED_MIGRATIONS` entry is in `RETIRED_LEDGER_VERSIONS`, and that the prefix gaps equal the retired prefixes minus the prefixes still on disk. That is 6 gaps from 7 retired names: `000031`'s retired name was a rename, and the prefix lives on in `000031_search_discovery_performance.sql`.
- The #2510 reverse checks (rename, remove, duplicate prefix, unpadded name) are run as temporary local edits and their output is recorded in the PR body.

### D7 (#2392): the DB plane normalises like the file plane

- The SQL of `get_earliest_clone_row_for_model_source` becomes:

  ```sql
  WHERE model_id = %s
    AND source_id = %s
    AND cloned_from_model_id IS NOT NULL
    AND btrim(cloned_from_model_id, %s) <> ''
    AND btrim(cloned_from_model_id, %s) <> model_id
  ORDER BY valid_time ASC, created_at ASC
  LIMIT 1
  ```

- The trim set parameter is a module constant: a string literal of the 29 characters for which `str.isspace()` is true, which is exactly the set Python's argument-less `str.strip()` removes. A unit test asserts the literal equals `"".join(ch for ch in map(chr, range(sys.maxunicode + 1)) if ch.isspace())`. The literal avoids scanning 1.1M code points at the import of a widely imported module. `btrim(x, set)` and `x.strip()` then agree for every string, given a UTF8 server encoding (node-27 and CI are UTF8; the real-DB test asserts it). Plain `btrim(x)` would strip only spaces and leave the tab / newline / U+3000 shapes divergent.
- Both masking shapes from the file plane's docstring are closed: the whitespace-only parent (emptiness test) and the padded self-reference (self test). The file plane compares the stripped parent with the raw `model_id` (`cloned_from == key[0]`), and so does the SQL.
- Kept unchanged:
  - `_from_clone_row`'s `.strip()` (defence in depth).
  - `get_latest_clone_row_for_model_source` (the publisher's reader keeps its fingerprint filter; out of scope).
  - The #1739 fingerprint ruling.
- Docstrings: the "What survives is a different axis …" passage of `_clone_entries_for_model_source` and the DB reader's docstring now describe the convergence. They no longer describe a divergence.

## Must-preserve consumers

- **`hydro.run_status`:**
  - `tests/test_hydro_status_set_parity.py` sweeps every `ADD VALUE` and checks subsets; `frequency_done` must not enter any display-ready or writable set.
  - `tests/test_retry_cancel_consistency.py:1270`.
  - OpenAPI `RunStatus` is unchanged (without `frequency_done`). `HydroRun.status` stays a runtime `str` (L1).
- **The six indexes:**
  - `services/tiles/mvt.py` `display_ready_run` (~:1822);
  - the QHH latest-product / display-coverage selectors and `packages/common/forecast_store.py` candidate reads;
  - #2424's latest-cycle CTE.
  - Results cannot change, since an index does not change a result; 5.4 records the plan.
- **`flood`:**
  - `db/roles/node27_write_roles.sql`'s per-existing-schema grants and `scripts/node27_provision_write_roles.sh`'s `APP_SCHEMAS_SQL` (~:105), both of which already tolerate a missing schema;
  - the executed one-shot `scripts/ops/node27_1729_*`.
- **The earliest-clone reader:**
  - `services/orchestrator/scheduler_lineage.py` `_from_clone_row` is its only consumer.
  - Docstrings that describe the old divergence are updated: `state_manager.py` near the publisher reader (~:876) and `scheduler_lineage.py` (~:246).

## Sibling surfaces

- `tests/integration_helpers.py:apply_migrations_from_zero` walks the same glob. It is unchanged; the literal inventory (D6) now catches a missing file.
- The `real-db-integration` CI job applies migrations from zero, so it exercises `000062`–`000064` on a fresh database. Their production-shaped behaviour (stale predicates, a populated `flood`, the retired ledger rows) is exercised by the new integration test (tasks 2.3).
- `services/tiles/mvt.py` `display_ready_run`: its predicate test is extended to assert that the migration's `WHERE` equals the query's (tasks 2.2).
- **`scripts/apply_smoke_migrations.py`:** a separate apply loop for disposable smoke databases. It is not guarded by D4 (non-goal, see D4).
- **Text made false by `000062`.** #2047 aligned these to "`frequency_done` is not a ledger member"; they are updated to "a convergence-only ledger member since `000062`, never written" (tasks 2.9):
  - `docs/spec/01_architecture_and_flow.md:130`
  - `docs/spec/03_database_design.md:62`
  - `docs/appendices/C_database_schema_draft.md:27`
  - `docs/runbooks/forcing-copyback-backfill.md:135` (the "raises `invalid input value` on a fresh database" claim)
  - `docs/modules/14_tile_publication_service_design.md:69` and `docs/modules/14_tile_publication_service_spec.md:77`
  - the comment at `apps/api/response_models/forecast.py:33`
  - the comment at `tests/test_hydro_run_public_projection.py:256-257`
- **`openspec/specs/production-ops-readiness`:** its migration-ledger requirement says the drift rows "are never visited by `packages/common/migrate.py`'s loop". D4 makes that false; it gets a MODIFIED delta.

## Risks / Trade-offs

- **Duplicate indexes after convergence.** `hydro_run_qhh_latest_candidate_idx`, `…_parsed_idx` and `hydro_run_display_ready_candidate_idx` end up with one identical definition, and so do the two `…_basin_status_idx`. That is what the ledger says today; converging production to it is this change. Removing the duplicates is a separate ruling (which name each consumer's EXPLAIN receipts rely on), filed as #2634.
- **`CREATE INDEX CONCURRENTLY` on production waits for older transactions** and can hit `lock_timeout` (the #2145 receipt needed 5 attempts). D2 makes the retry safe. The apply step records every attempt.
- **Irreversibility.** `000064` is reversible only through the pre-apply dump (D3). The dump is verified readable (`pg_restore --list`) before the apply.
